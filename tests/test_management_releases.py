"""Commit/archive trust, asset binding and real-filesystem management selection."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openstack_platform import management_release as releases
from openstack_platform import release_manifest
from openstack_platform.management.activation import activate
from openstack_platform.management.rollback import reactivate
from tests.repository_fixtures import clean_repository

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "management_release_installer", ROOT / "deploy/releases/install_release.py"
)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INSTALLER
SPEC.loader.exec_module(INSTALLER)


class ManagementReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="release-fixture-")
        cls.root = Path(cls.temporary.name)
        cls.repository, cls.commit = clean_repository(ROOT, cls.root / "source")
        cls.prepare_assets()
        cls.artifacts = cls.root / "artifacts"
        cls.manifest_name = releases.generate(
            cls.repository, cls.commit, cls.artifacts, unsigned=True
        ).name

    @classmethod
    def prepare_assets(cls) -> None:
        folder = cls.repository / "frontend/owner-portal/dist"
        (folder / "assets").mkdir(parents=True)
        (folder / ".vite").mkdir()
        (folder / "index.html").write_text('<html><script src="/assets/app.js"></script></html>')
        (folder / "assets/app.js").write_text('console.log("public fixture");')
        (folder / ".vite/manifest.json").write_text('{"index.html":{"file":"assets/app.js"}}')
        assets = {
            path.relative_to(folder).as_posix(): releases.record(path.read_bytes())
            for path in folder.rglob("*")
            if path.is_file()
        }
        receipt = {
            "format": "openstack-platform-owner-build-v1",
            "sourceCommit": cls.commit,
            "dirty": False,
            "npmLockSha256": hashlib.sha256(
                (cls.repository / "frontend/package-lock.json").read_bytes()
            ).hexdigest(),
            "nodeVersion": "24.19.0",
            "assetManifestSha256": assets[".vite/manifest.json"]["sha256"],
            "assets": assets,
        }
        (cls.repository / "frontend/owner-portal/build-receipt.json").write_text(
            json.dumps(receipt)
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="release-install-", dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.output = self.root / "artifacts"
        shutil.copytree(self.artifacts, self.output)
        self.manifest = self.output / self.manifest_name
        self.document = releases.verify(
            self.output / "release-manifest.json",
            self.manifest,
            signature=None,
            trust_root=None,
            allow_unsigned_development=True,
        )
        platform = json.loads((ROOT / "config/platform.example.json").read_text())
        platform["paths"]["adminState"] = str(self.root / "state")
        platform["namespace"] = "release-test"
        platform["ownerPortal"] = {"enabled": True, "commonsOrigin": "https://class.example.com"}
        self.platform = self.root / "platform.json"
        self.platform.write_text(json.dumps(platform))
        self.groups = {}
        available = [group for group in os.getgroups() if group != os.getegid()]
        for mode in ("broker", "web"):
            group = available[0] if mode == "broker" and available else os.getegid()
            folder = self.root / "state" / f"management-{mode}-releases"
            folder.mkdir(parents=True)
            os.chown(folder, -1, group)
            folder.chmod(0o2750)
            for name in ("releases", "config"):
                child = folder / name
                child.mkdir(mode=0o2750)
                child.chmod(0o2750)
            prepared = folder / "config/platform.json"
            prepared.write_bytes(self.platform.read_bytes())
            prepared.chmod(0o640)
            self.groups[mode] = group

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(self, mode: str):
        archive = self.output / self.document["archives"][mode]["file"]
        return INSTALLER._parser().parse_args(
            [
                "--mode",
                mode,
                "--archive",
                str(archive),
                "--archive-sha256",
                self.document["archives"][mode]["sha256"],
                "--commit",
                self.commit,
                "--release-manifest",
                str(self.output / "release-manifest.json"),
                "--management-manifest",
                str(self.manifest),
                "--platform-config",
                str(self.platform),
                "--allow-unsigned-development",
                "--python",
                sys.executable,
            ]
        )

    def test_tampered_archive_and_schema_are_rejected_before_candidate_execution(self) -> None:
        args = self.args("web")
        archive = Path(args.archive)
        raw = archive.read_bytes()
        archive.write_bytes(raw[:2048] + b"corrupt" + raw[2055:])
        with (
            patch.object(
                INSTALLER.importlib.util,
                "spec_from_file_location",
                side_effect=AssertionError("candidate executed"),
            ),
            self.assertRaises(INSTALLER.InstallFailure),
        ):
            INSTALLER.install(args)
        self.assertFalse((self.root / "state/management-web-releases/current").exists())
        archive.write_bytes(raw)
        document = json.loads(self.manifest.read_bytes())
        document["compatibility"]["brokerSchemaVersion"] = 1
        self.manifest.write_text(json.dumps(document))
        with self.assertRaises(INSTALLER.InstallFailure):
            INSTALLER.install(args)

    def test_link_duplicate_device_escape_and_size_bounds(self) -> None:
        for kind in ("symlink", "hardlink", "device", "duplicate", "escape", "size"):
            if kind == "size":
                member = tarfile.TarInfo("payload")
                member.size = 33 * 1024**2
                with self.assertRaisesRegex(INSTALLER.InstallFailure, "unpacked size"):
                    INSTALLER._management_members(member.tobuf() + b"\0" * 1024)
                continue
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:") as bundle:
                member = tarfile.TarInfo("../escape" if kind == "escape" else "payload")
                if kind == "symlink":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/tmp/escape"
                elif kind == "hardlink":
                    member.type = tarfile.LNKTYPE
                    member.linkname = "payload"
                elif kind == "device":
                    member.type = tarfile.CHRTYPE
                bundle.addfile(member)
                if kind == "duplicate":
                    bundle.addfile(tarfile.TarInfo("payload"))
            with self.assertRaises(INSTALLER.InstallFailure):
                INSTALLER._management_members(stream.getvalue())

    def test_signed_descriptor_requires_the_source_trust_root_and_asset_tamper_fails(self) -> None:
        private = self.root / "signing.pem"
        public = self.root / "public.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", private],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["openssl", "pkey", "-in", private, "-pubout", "-out", public],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        output = self.root / "signed"
        path = releases.generate(self.repository, self.commit, output, signing_key=private)
        document = releases.verify(
            output / "release-manifest.json",
            path,
            signature=output / "management-artifacts.sig",
            trust_root=public,
        )
        self.assertEqual(document["releaseChannel"], "production")
        with self.assertRaises(release_manifest.ReleaseVerificationError):
            releases.verify(
                output / "release-manifest.json", path, signature=None, trust_root=public
            )
        build = releases.build_record(self.repository, self.commit)
        self.assertIn("assets/app.js", build["assets"])
        file = self.repository / "frontend/owner-portal/dist/assets/app.js"
        original = file.read_bytes()
        try:
            file.write_bytes(original + b"changed")
            with self.assertRaises(ValueError):
                releases.build_record(self.repository, self.commit)
        finally:
            file.write_bytes(original)

    def test_failed_smoke_leaves_all_running_configuration_and_selectors_identical(self) -> None:
        broker = INSTALLER.install(self.args("broker"))
        web = INSTALLER.install(self.args("web"))
        state = self.root / "state"
        activate(state, os.geteuid(), self.groups)
        for release in (broker, web):
            (release.parent.parent / "config/management.json").write_bytes(
                b"prior prepared config\n"
            )
        paths = [
            p
            for release in (broker, web)
            for folder in (release / "config", release.parent.parent / "config")
            for p in folder.rglob("*")
            if p.is_file()
        ]
        marker = broker.parent.parent / "activate-request"
        before_marker = marker.read_bytes()
        before_active = os.readlink(state / "management-active/current")
        before = {path: path.read_bytes() for path in paths}
        platform = json.loads(self.platform.read_bytes())
        platform["ownerPortal"]["classLabel"] = "Updated class account"
        self.platform.write_text(json.dumps(platform))
        python = self.root / "reject-smoke"
        python.write_text(
            "#!/bin/sh\ncase \"$*\" in *sys.version_info*) printf '(3, 14)\\n';; *) exit 1;; esac\n"
        )
        python.chmod(0o550)
        for mode in ("broker", "web"):
            args = self.args(mode)
            args.python = python
            with self.assertRaisesRegex(ValueError, "smoke failed"):
                INSTALLER.install(args)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual((broker.parent.parent / "current").resolve(), broker)
        self.assertEqual((web.parent.parent / "current").resolve(), web)
        self.assertEqual(marker.read_bytes(), before_marker)
        self.assertEqual(os.readlink(state / "management-active/current"), before_active)
        self.assertFalse(
            any(p.name.startswith(".") and p.is_dir() for p in broker.parent.iterdir())
        )

    def test_install_activate_rollback_preserves_inventory_and_refuses_tamper(self) -> None:
        broker = INSTALLER.install(self.args("broker"))
        web = INSTALLER.install(self.args("web"))
        state = self.root / "state"
        activate(state, os.geteuid(), self.groups)
        self.assertEqual(INSTALLER.install(self.args("broker")), broker)
        self.assertEqual((broker.parent.parent / "activate-request").stat().st_mode & 0o777, 0o640)
        for mode, release in (("broker", broker), ("web", web)):
            self.assertTrue((release / ".complete").is_file())
            for folder in (release, *[p for p in release.rglob("*") if p.is_dir()]):
                self.assertEqual(folder.stat().st_gid, self.groups[mode])
                self.assertEqual(folder.stat().st_mode & 0o7777, 0o2750)
        original = (broker / "config/platform.json").read_bytes()
        platform = json.loads(self.platform.read_bytes())
        platform["ownerPortal"]["classLabel"] = "Current inventory differs"
        self.platform.write_text(json.dumps(platform))
        newer_broker = INSTALLER.install(self.args("broker"))
        newer_web = INSTALLER.install(self.args("web"))
        activate(state, os.geteuid(), self.groups)
        result = reactivate(
            self.platform,
            broker.name,
            web.name,
            allow_unsigned_development=True,
            expected_groups=self.groups,
        )
        self.assertEqual(result, (broker, web))
        self.assertEqual((state / "management-active/current/broker").resolve(), newer_broker)
        activate(state, os.geteuid(), self.groups)
        self.assertEqual((state / "management-active/current/broker").resolve(), broker)
        self.assertEqual((broker / "config/platform.json").read_bytes(), original)
        self.assertNotEqual(
            json.loads(original)["ownerPortal"].get("classLabel"),
            platform["ownerPortal"]["classLabel"],
        )

        marker = broker.parent.parent / "activate-request"
        before = marker.read_bytes()
        descriptor = broker / "evidence/management-artifacts.json"
        raw = descriptor.read_bytes()
        value = json.loads(raw)
        value["compatibility"]["brokerSchemaVersion"] = 4
        descriptor.chmod(0o640)
        descriptor.write_text(json.dumps(value))
        descriptor.chmod(0o440)
        with self.assertRaisesRegex(ValueError, "schema-incompatible"):
            reactivate(
                self.platform,
                broker.name,
                web.name,
                allow_unsigned_development=True,
                expected_groups=self.groups,
            )
        descriptor.chmod(0o640)
        descriptor.write_bytes(raw)
        descriptor.chmod(0o440)
        payload = web / "static/assets/app.js"
        payload.chmod(0o640)
        original = payload.read_bytes()
        payload.write_bytes(b"changed retained payload")
        payload.chmod(0o440)
        with self.assertRaisesRegex(ValueError, "hashes differ"):
            reactivate(
                self.platform,
                broker.name,
                web.name,
                allow_unsigned_development=True,
                expected_groups=self.groups,
            )
        payload.chmod(0o640)
        payload.write_bytes(original)
        payload.chmod(0o440)
        other_web = newer_web
        with self.assertRaisesRegex(ValueError, "config-matched pair"):
            reactivate(
                self.platform,
                broker.name,
                other_web.name,
                allow_unsigned_development=True,
                expected_groups=self.groups,
            )
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual((self.root / "state/management-active/current/web").resolve(), web)

    def test_activation_refuses_linked_or_writable_state_hierarchy(self) -> None:
        state = self.root / "state"
        alias = self.root / "state-link"
        alias.symlink_to(state)
        with self.assertRaises(OSError):
            activate(alias, os.geteuid(), self.groups)
        original = self.root.stat().st_mode & 0o7777
        self.root.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, "root-controlled"):
                activate(state, os.geteuid(), self.groups)
        finally:
            self.root.chmod(original)
        self.assertFalse((state / "management-active").exists())


@unittest.skipUnless(
    os.environ.get("OWNER_PORTAL_RELEASE_INTEGRATION") == "1", "explicit Node 24 build integration"
)
class BuildIntegrationTests(unittest.TestCase):
    def test_actual_vite_build_and_two_archives_on_real_filesystem(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-build-", dir=ROOT / ".tmp") as directory:
            repository, commit = clean_repository(ROOT, Path(directory) / "source")
            frontend = repository / "frontend/owner-portal"
            subprocess.run(
                ["npm", "ci"], cwd=frontend.parent, check=True, stdout=subprocess.DEVNULL
            )
            subprocess.run(
                ["npm", "run", "build"], cwd=frontend, check=True, stdout=subprocess.DEVNULL
            )
            output = Path(directory) / "artifacts"
            manifest = releases.generate(repository, commit, output, unsigned=True)
            document = releases.verify(
                output / "release-manifest.json",
                manifest,
                signature=None,
                trust_root=None,
                allow_unsigned_development=True,
            )
            self.assertEqual(set(document["archives"]), {"broker", "web"})
            for mode in ("broker", "web"):
                releases.verify_archive(output / document["archives"][mode]["file"], document, mode)
            evidence = ROOT / ".tmp/phase1c-built-fixture"
            if evidence.exists():
                shutil.rmtree(evidence)
            shutil.copytree(output, evidence)
