"""Commit/archive trust, asset binding and real-filesystem management selection."""

from __future__ import annotations

import ast
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
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from openstack_platform import management_release as releases
from openstack_platform import release_manifest
from openstack_platform.config import load_platform, platform_config_identity
from openstack_platform.management import activation
from openstack_platform.management.activation import activate
from openstack_platform.management.installation import development_root
from openstack_platform.management.rollback import reactivate
from openstack_platform.management.settings import render
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
        cls.temporary = tempfile.TemporaryDirectory(prefix="release-fixture-", dir=ROOT / ".tmp")
        cls.root = Path(cls.temporary.name)
        cls.repository, cls.commit = clean_repository(ROOT, cls.root / "source")
        cls.prepare_assets()

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
                (cls.repository / "frontend/owner-portal/package-lock.json").read_bytes()
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
        self.manifest = releases.generate(self.repository, self.commit, self.output, unsigned=True)
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

    def test_assets_full_archive_hash_and_compatibility_are_bound_without_node_runtime(
        self,
    ) -> None:
        self.assertEqual(self.document["compatibility"]["authProtocolVersion"], 2)
        self.assertEqual(self.document["ui"]["build"]["nodeVersion"], "24.19.0")
        for mode in ("broker", "web"):
            files = releases.verify_archive(
                self.output / self.document["archives"][mode]["file"], self.document, mode
            )
            self.assertFalse(
                any(
                    "node_modules" in path or "/dev/" in path
                    for path in files
                    if path.startswith("runtime/")
                )
            )
            self.assertFalse(any("assertions.py" in path for path in files))
            self.assertTrue(
                "runtime/openstack_platform/management/identity/main.py" in files
                if mode == "broker"
                else True
            )
        sbom = json.loads((self.output / "management.sbom.json").read_bytes())
        self.assertTrue(any(package["name"] == "node" for package in sbom["packages"]))
        self.assertTrue(any(package["name"] == "react" for package in sbom["packages"]))

    def test_install_realfilesystem_group_inheritance_pair_marker_and_reuse(self) -> None:
        broker = INSTALLER.install(self.args("broker"))
        root = broker.parent.parent
        self.assertFalse((root / "activate-request").exists())
        web = INSTALLER.install(self.args("web"))
        self.assertEqual((root / "activate-request").stat().st_mode & 0o777, 0o640)
        for mode, release in (("broker", broker), ("web", web)):
            self.assertEqual((release.parent.parent / "current").resolve(), release)
            self.assertTrue((release / ".complete").is_file())
            for folder in (release, *[p for p in release.rglob("*") if p.is_dir()]):
                self.assertEqual(folder.stat().st_gid, self.groups[mode])
                self.assertEqual(folder.stat().st_mode & 0o7777, 0o2750)
            for file in release.rglob("*"):
                if file.is_file():
                    self.assertEqual(file.stat().st_gid, self.groups[mode])
        self.assertTrue((broker / "bin/management-identity").is_file())
        self.assertEqual(INSTALLER.install(self.args("broker")), broker)
        (broker / "runtime/openstack_platform/management/broker/auth.py").chmod(0o640)
        with self.assertRaises(ValueError):
            INSTALLER.install(self.args("broker"))

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

    def test_installer_and_smoke_imports_cannot_resolve_the_public_package(self) -> None:
        paths = (
            [ROOT / "openstack_platform" / name for name in releases.BASE]
            + [
                ROOT / "openstack_platform" / name
                for name in (
                    "release_manifest.py",
                    "management_release.py",
                    "openstack.py",
                    "remote.py",
                    "runtime.py",
                    "durable.py",
                    "host_keys.py",
                    "host_user_data.py",
                    "ingress_credentials.py",
                    "installation.py",
                )
            ]
            + [
                ROOT / "openstack_platform/management" / name
                for name in ("installation.py", "entry.py", "settings.py")
            ]
        )
        paths += [ROOT / "openstack_platform/controller" / name for name in releases.CONTROLLER]
        paths += [
            path
            for path in (ROOT / "openstack_platform/management").rglob("*.py")
            if "dev" not in path.relative_to(ROOT / "openstack_platform/management").parts
        ]
        for path in paths:
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(
                            alias.name.split(".")[0] == "openstack_platform" for alias in node.names
                        ),
                        path,
                    )
                elif isinstance(node, ast.ImportFrom):
                    self.assertFalse(
                        node.level == 0
                        and (node.module or "").split(".")[0] == "openstack_platform",
                        path,
                    )

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

    def test_signed_installer_out_of_process_isolated_store_copy(self) -> None:
        base_python = Path(sys._base_executable)

        def fixture(source):
            # Only the signed test source adapts these host constraints; the
            # production installer has no override or test bypass.
            contract = source / "infra/lib/platform_contract.json"
            value = json.loads(contract.read_bytes())
            for mode, group in self.groups.items():
                value["accounts"]["management" + mode.title()]["gid"] = group
            contract.write_text(json.dumps(value))
            installation = source / "openstack_platform/management/installation.py"
            text = installation.read_text().replace(
                'RUNTIME = Path("/run/current-system/sw/bin/management-python3.14")',
                f"RUNTIME = Path({str(base_python)!r})",
            )
            self.assertEqual(text.count("elif not os.path.ismount(state):"), 2)
            text = text.replace(
                "elif not os.path.ismount(state):",
                f"elif state != Path({str(self.root / 'state')!r}):",
            )
            installation.write_text(text)

        repository, commit = clean_repository(ROOT, self.root / "isolated-source", mutate=fixture)
        asset_source = self.repository / "frontend/owner-portal/dist"
        shutil.copytree(asset_source, repository / "frontend/owner-portal/dist")
        receipt = json.loads(
            (self.repository / "frontend/owner-portal/build-receipt.json").read_bytes()
        )
        receipt["sourceCommit"] = commit
        (repository / "frontend/owner-portal/build-receipt.json").write_text(json.dumps(receipt))
        key, public = self.root / "test-key.pem", self.root / "test-public.pem"
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", key],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["openssl", "pkey", "-in", key, "-pubout", "-out", public],
            check=True,
            capture_output=True,
        )
        output = self.root / "isolated-artifacts"
        manifest = releases.generate(repository, commit, output, signing_key=key)
        document = json.loads(manifest.read_bytes())
        # Valid signed JSON need not be canonical. Retain the signed bytes,
        # including whitespace, rather than serializing a parsed descriptor.
        manifest.write_bytes(json.dumps(document, indent=2).encode() + b"\n")
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                key,
                "-in",
                manifest,
                "-out",
                output / "management-artifacts.sig",
            ],
            check=True,
            capture_output=True,
        )
        store = self.root / "store-like/bin"
        store.mkdir(parents=True)
        installer = store / "install_release.py"
        shutil.copyfile(ROOT / "deploy/releases/install_release.py", installer)
        probe = subprocess.run(
            [
                base_python,
                "-I",
                "-c",
                "import importlib.util; assert importlib.util.find_spec('openstack_platform') is None",
            ],
            cwd=store,
            capture_output=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr.decode())
        for mode in ("broker", "web"):
            row = document["archives"][mode]
            result = subprocess.run(
                [
                    base_python,
                    "-I",
                    installer,
                    "--mode",
                    mode,
                    "--archive",
                    output / row["file"],
                    "--archive-sha256",
                    row["sha256"],
                    "--commit",
                    commit,
                    "--release-manifest",
                    output / "release-manifest.json",
                    "--release-signature",
                    output / "release-manifest.sig",
                    "--management-manifest",
                    manifest,
                    "--management-signature",
                    output / "management-artifacts.sig",
                    "--release-trust-root",
                    public,
                    "--platform-config",
                    self.platform,
                    "--expected-platform-namespace",
                    "release-test",
                    "--expected-platform-identity-sha256",
                    platform_config_identity(load_platform(self.platform)),
                ],
                cwd=store,
                env={"PATH": os.environ["PATH"], "PLATFORM_ENVIRONMENT": "production"},
                capture_output=True,
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            release = (self.root / "state" / f"management-{mode}-releases/current").resolve()
            self.assertEqual(
                (release / "evidence/management-artifacts.json").read_bytes(), manifest.read_bytes()
            )
            for folder in [release, *[p for p in release.rglob("*") if p.is_dir()]]:
                self.assertEqual(folder.stat().st_mode & 0o7777, 0o2750)
                self.assertEqual(folder.stat().st_gid, self.groups[mode])
        # Simulate replacement of every original input after the read-once
        # boundary, including signatures and trust root, with hostile symlinks.
        args = self.args("web")
        row = document["archives"]["web"]
        args.archive = output / row["file"]
        args.archive_sha256 = row["sha256"]
        args.commit = commit
        args.release_manifest = output / "release-manifest.json"
        args.release_signature = output / "release-manifest.sig"
        args.management_manifest = manifest
        args.management_signature = output / "management-artifacts.sig"
        args.release_trust_root = public
        args.allow_unsigned_development = False
        args.expected_platform_namespace = "release-test"
        args.expected_platform_identity_sha256 = platform_config_identity(
            load_platform(self.platform)
        )
        inputs = {path: path.read_bytes() for path in (*output.iterdir(), public) if path.is_file()}
        poison = self.root / "poison"
        poison.write_bytes(b"untrusted replacement")
        original = INSTALLER._trusted_manifest_preflight

        def swap(*arguments, **keywords):
            original(*arguments, **keywords)
            for path in inputs:
                path.unlink()
                path.symlink_to(poison)

        with patch.object(INSTALLER, "_trusted_manifest_preflight", side_effect=swap):
            web = INSTALLER.install(args)
        for path, raw in inputs.items():
            if path.suffix == ".tar":
                continue
            name = "release-trust-root.pem" if path == public else path.name
            self.assertEqual((web / "evidence" / name).read_bytes(), raw)

    def test_platform_symlink_and_component_local_readable_inventory(self) -> None:
        link = self.root / "etc-platform.json"
        link.symlink_to(self.platform)
        args = self.args("web")
        args.platform_config = link
        web = INSTALLER.install(args)
        config = json.loads((web / "config/management.json").read_bytes())
        self.assertEqual(config["platformConfig"], "platform.json")
        self.assertEqual((web / "config/platform.json").stat().st_gid, self.groups["web"])
        # Make the broker root genuinely inaccessible. Web smoke must still read
        # its own inventory, even under an isolated interpreter without the repo.
        broker_root = self.root / "state/management-broker-releases"
        broker_root.chmod(0)
        try:
            result = subprocess.run(
                [web / "bin/management-web", "--smoke"], capture_output=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            runtime = web / "runtime"
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    f"import sys; sys.path.insert(0,{str(runtime)!r}); import openstack_platform.management.web.main",
                ],
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        finally:
            broker_root.chmod(0o2750)

    def test_manual_renderer_uses_each_prepared_service_inventory(self) -> None:
        from openstack_platform.management.config import Config

        link = self.root / "etc-platform.json"
        link.symlink_to(self.platform)
        for mode in ("broker", "web"):
            (self.root / "state" / f"management-{mode}-releases/config/platform.json").unlink()
        broker, web, identity = render(link, expected_groups=self.groups)
        for mode, path in (("broker", broker), ("web", web), ("broker", identity)):
            self.assertEqual(path.stat().st_gid, self.groups[mode])
            self.assertEqual(path.stat().st_mode & 0o7777, 0o640)
        self.assertEqual(
            json.loads(web.read_bytes())["platformConfig"], str(web.parent / "platform.json")
        )
        broker_root = broker.parent.parent
        broker_root.chmod(0)
        try:
            self.assertFalse(Config.load(web).development)
        finally:
            broker_root.chmod(0o2750)

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

    def test_inputs_swapped_after_capture_are_not_reopened_or_retained(self) -> None:
        expected = {
            path.name: path.read_bytes()
            for path in self.output.iterdir()
            if path.is_file() and path.suffix != ".tar"
        }
        original = INSTALLER._trusted_manifest_preflight
        reads = []
        reader = INSTALLER._management_input

        def capture(path, *arguments):
            reads.append(path)
            return reader(path, *arguments)

        def swap(*args, **kwargs):
            original(*args, **kwargs)
            for name in expected:
                (self.output / name).write_bytes(b"swapped untrusted input")

        with (
            patch.object(INSTALLER, "_trusted_manifest_preflight", side_effect=swap),
            patch.object(INSTALLER, "_management_input", side_effect=capture),
        ):
            web = INSTALLER.install(self.args("web"))
        self.assertEqual(len(reads), len(set(reads)))
        for name, raw in expected.items():
            self.assertEqual((web / "evidence" / name).read_bytes(), raw)

    def test_traversal_evidence_names_are_rejected_before_candidate_execution(self) -> None:
        for document_path in (self.manifest, self.output / "release-manifest.json"):
            raw = document_path.read_bytes()
            document = json.loads(raw)
            document["evidence"]["sbom"]["file"] = "../../../config/backup-recipient.txt"
            document_path.write_text(json.dumps(document))
            try:
                with (
                    patch.object(
                        INSTALLER.importlib.util,
                        "spec_from_file_location",
                        side_effect=AssertionError("candidate executed"),
                    ),
                    self.assertRaises(INSTALLER.InstallFailure),
                ):
                    INSTALLER.install(self.args("web"))
            finally:
                document_path.write_bytes(raw)
        self.assertFalse(
            (self.root / "state/management-broker-releases/config/backup-recipient.txt").exists()
        )

    def test_candidate_reverification_results_are_required(self) -> None:
        original = INSTALLER.importlib.import_module
        for suffix in (".release_manifest", ".management_release"):

            def import_candidate(name, suffix=suffix):
                module = original(name)
                if name.startswith("_verified_management_release_") and name.endswith(suffix):
                    verify = module.verify
                    module.verify = lambda *args, **kwargs: {
                        **verify(*args, **kwargs),
                        "changed": True,
                    }
                return module

            with (
                patch.object(INSTALLER.importlib, "import_module", side_effect=import_candidate),
                self.assertRaisesRegex(INSTALLER.InstallFailure, "verification result differs"),
            ):
                INSTALLER.install(self.args("web"))
        self.assertFalse((self.root / "state/management-web-releases/current").exists())

    def repack(self, mode, files):
        document = json.loads(self.manifest.read_bytes())
        archive = self.output / document["archives"][mode]["file"]
        archive.unlink()
        releases.write_tar(archive, files)
        document["archives"][mode].update(releases.record(archive.read_bytes()))
        document["archives"][mode]["files"] = {
            name: releases.record(raw) for name, raw in files.items()
        }
        binding = {
            name: document[name]
            for name in (
                "sourceCommit",
                "compatibility",
                "ui",
                "archives",
                "runtime",
                "sourceManifestSha256",
            )
        }
        document["pairIdentity"] = hashlib.sha256(release_manifest._canonical(binding)).hexdigest()
        path = self.output / "management.provenance.json"
        provenance = json.loads(path.read_bytes())
        provenance["subject"] = [
            {"name": row["file"], "digest": {"sha256": row["sha256"]}}
            for row in document["archives"].values()
        ]
        path.write_bytes(release_manifest._canonical(provenance))
        document["evidence"]["provenance"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.manifest.write_bytes(release_manifest._canonical(document))
        self.document = document

    def test_installer_rejects_authenticated_asset_drift_and_unexpected_bin_layout(self) -> None:
        original = releases.tar_files(self.output / self.document["archives"]["web"]["file"])
        for mutate, message in (
            (lambda files: files.update({"bin/unexpected": b"unreviewed executable"}), "layout"),
            (
                lambda files: files.update({"static/assets/app.js": b"different built output"}),
                "assets",
            ),
        ):
            files = dict(original)
            mutate(files)
            self.repack("web", files)
            with self.assertRaisesRegex(ValueError, message):
                INSTALLER.install(self.args("web"))
            self.assertFalse((self.root / "state/management-web-releases/current").exists())

    def test_generate_packages_exact_captured_asset_bytes(self) -> None:
        original = releases.build_snapshot
        asset = self.repository / "frontend/owner-portal/dist/assets/app.js"
        raw = asset.read_bytes()

        def change_after_hash(*args):
            result = original(*args)
            asset.write_bytes(b"changed after capture")
            return result

        try:
            with patch.object(releases, "build_snapshot", side_effect=change_after_hash):
                path = releases.generate(
                    self.repository, self.commit, self.root / "snapshot-build", unsigned=True
                )
            document = json.loads(path.read_bytes())
            files = releases.verify_archive(
                path.parent / document["archives"]["web"]["file"], document, "web"
            )
            self.assertEqual(files["static/assets/app.js"], raw)
        finally:
            asset.write_bytes(raw)

    def test_activation_uses_one_active_selector_and_rejects_one_sided_pair(self) -> None:
        broker = INSTALLER.install(self.args("broker"))
        web = INSTALLER.install(self.args("web"))
        state = self.root / "state"
        first = activate(state, os.geteuid(), self.groups)
        self.assertEqual((state / "management-active/current/broker").resolve(), broker)
        self.assertEqual((state / "management-active/current/web").resolve(), web)
        active_link = os.readlink(state / "management-active/current")
        marker = broker.parent.parent / "activate-request"
        saved = marker.read_bytes()
        marker.write_bytes((self.commit + "\n" + "f" * 64 + "\n").encode())
        with self.assertRaisesRegex(ValueError, "descriptors"):
            activate(state, os.geteuid(), self.groups)
        self.assertEqual(os.readlink(state / "management-active/current"), active_link)
        marker.write_bytes(saved)
        platform = json.loads(self.platform.read_bytes())
        platform["ownerPortal"]["classLabel"] = "Second configuration"
        self.platform.write_text(json.dumps(platform))
        second_broker = INSTALLER.install(self.args("broker"))
        self.assertNotEqual(second_broker, broker)
        self.assertEqual((state / "management-active/current/broker").resolve(), broker)
        with self.assertRaisesRegex(ValueError, "snapshots"):
            activate(state, os.geteuid(), self.groups)
        self.assertEqual(os.readlink(state / "management-active/current"), active_link)
        second_web = INSTALLER.install(self.args("web"))
        self.assertNotEqual(activate(state, os.geteuid(), self.groups), first)
        self.assertEqual((state / "management-active/current/broker").resolve(), second_broker)
        self.assertEqual((state / "management-active/current/web").resolve(), second_web)

    def test_admin_environment_and_symlinked_development_root_are_rejected(self) -> None:
        with (
            patch.dict(os.environ, {"PLATFORM_ENVIRONMENT": "production"}),
            self.assertRaises(INSTALLER.InstallFailure),
        ):
            INSTALLER.install(self.args("broker"))
        base = self.root / "fake-worktree"
        base.mkdir()
        (base / ".tmp").symlink_to(self.root / "state")
        with (
            patch.object(Path, "cwd", return_value=base),
            self.assertRaisesRegex(ValueError, "direct paths"),
        ):
            development_root(base / ".tmp")

    def test_fifo_installer_input_activation_marker_and_lock_fail_promptly(self) -> None:
        broker = INSTALLER.install(self.args("broker"))
        INSTALLER.install(self.args("web"))
        paths = [
            (self.root / "fifo-input", 0o600),
            (broker.parent.parent / "activate-request", 0o640),
            (broker.parent.parent / ".install.lock", 0o600),
        ]
        for path, mode in paths:
            if path.exists():
                path.unlink()
            os.mkfifo(path, mode)
            command = (
                f"import importlib.util,sys; from pathlib import Path; p=Path({str(path)!r}); "
                f"s=importlib.util.spec_from_file_location('fifo_installer',{str(ROOT / 'deploy/releases/install_release.py')!r}); "
                "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m);m._management_input(p)"
                if path.name == "fifo-input"
                else f"import sys;sys.path.insert(0,{str(ROOT)!r}); from openstack_platform.management.activation import activate; from pathlib import Path; activate(Path({str(self.root / 'state')!r}),{os.geteuid()},{self.groups!r})"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", command], capture_output=True, timeout=5
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"differs" if path.name == ".install.lock" else b"input", result.stderr)
            path.unlink()
            # Keep the lock valid while testing the marker, and vice versa.
            if path.name == "activate-request":
                path.write_bytes(
                    (self.commit + "\n" + self.document["pairIdentity"] + "\n").encode()
                )
                path.chmod(mode)

    def test_retained_pair_reactivation_preserves_its_inventory_snapshot(self) -> None:
        broker = INSTALLER.install(self.args("broker"))
        web = INSTALLER.install(self.args("web"))
        state = self.root / "state"
        activate(state, os.geteuid(), self.groups)
        original = (broker / "config/platform.json").read_bytes()
        platform = json.loads(self.platform.read_bytes())
        platform["ownerPortal"]["classLabel"] = "Current inventory differs"
        self.platform.write_text(json.dumps(platform))
        newer_broker = INSTALLER.install(self.args("broker"))
        INSTALLER.install(self.args("web"))
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

    def test_activation_refuses_linked_or_writable_state_hierarchy(self) -> None:
        INSTALLER.install(self.args("broker"))
        INSTALLER.install(self.args("web"))
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

    def test_activation_compatibility_matches_release_compatibility(self) -> None:
        self.assertEqual(activation.COMPATIBILITY, releases.COMPATIBILITY)

    def test_activation_reads_held_directories_after_intermediate_replacement(self) -> None:
        broker = INSTALLER.install(self.args("broker"))
        INSTALLER.install(self.args("web"))
        state = self.root / "state"
        activate(state, os.geteuid(), self.groups)
        real_open = activation.directory_at
        poison = self.root / "poison"
        poison.mkdir()
        for path in (broker.parent, broker, broker / "bin", broker / "evidence", broker / "config"):
            changed = []
            saved = path.with_name(path.name + "-held")

            @contextmanager
            def replace_after_open(
                parent, name, uid, gid, mode, path=path, saved=saved, changed=changed
            ):
                with real_open(parent, name, uid, gid, mode) as fd:
                    if not changed and name == path.name:
                        path.rename(saved)
                        path.symlink_to(poison)
                        changed.append(path)
                    yield fd

            try:
                with patch.object(activation, "directory_at", side_effect=replace_after_open):
                    activate(state, os.geteuid(), self.groups)
                self.assertEqual(changed, [path])
            finally:
                if changed:
                    path.unlink()
                    saved.rename(path)

    def test_retained_rollback_refuses_dangling_active_selector_and_duplicate_descriptor(
        self,
    ) -> None:
        broker = INSTALLER.install(self.args("broker"))
        web = INSTALLER.install(self.args("web"))
        state = self.root / "state"
        activate(state, os.geteuid(), self.groups)
        link = state / "management-active/current"
        original = os.readlink(link)
        link.unlink()
        link.symlink_to("pairs/missing")
        marker = broker.parent.parent / "activate-request"
        before = marker.read_bytes()
        with self.assertRaisesRegex(ValueError, "dangling or incomplete"):
            reactivate(
                self.platform,
                broker.name,
                web.name,
                allow_unsigned_development=True,
                expected_groups=self.groups,
            )
        self.assertEqual(marker.read_bytes(), before)
        link.unlink()
        link.symlink_to(original)
        path = broker / "evidence/management-artifacts.json"
        raw = path.read_bytes()
        path.chmod(0o640)
        path.write_bytes(raw.replace(b"{", b'{"pairIdentity":"ignored duplicate",', 1))
        path.chmod(0o440)
        with self.assertRaisesRegex(ValueError, "duplicate management activation field"):
            reactivate(
                self.platform,
                broker.name,
                web.name,
                allow_unsigned_development=True,
                expected_groups=self.groups,
            )
        self.assertEqual(marker.read_bytes(), before)

    def test_management_only_host_guard_refuses_both_portal_modes_and_restores_environment(
        self,
    ) -> None:
        for mode in ("broker", "web"):
            with patch.dict(os.environ, {"PLATFORM_MANAGEMENT_ENVIRONMENT": "production"}):
                previous = os.environ.pop("PLATFORM_ENVIRONMENT", None)
                try:
                    with self.assertRaises(INSTALLER.InstallFailure):
                        INSTALLER.install(self.args(mode))
                    self.assertNotIn("PLATFORM_ENVIRONMENT", os.environ)
                finally:
                    if previous is not None:
                        os.environ["PLATFORM_ENVIRONMENT"] = previous

    def test_retained_rollback_rejects_schema_tamper_mixed_config_and_payload_tamper(self) -> None:
        broker = INSTALLER.install(self.args("broker"))
        web = INSTALLER.install(self.args("web"))
        activate(self.root / "state", os.geteuid(), self.groups)
        marker = broker.parent.parent / "activate-request"
        before = marker.read_bytes()
        descriptor = broker / "evidence/management-artifacts.json"
        raw = descriptor.read_bytes()
        value = json.loads(raw)
        value["compatibility"]["brokerSchemaVersion"] = 3
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
        platform = json.loads(self.platform.read_bytes())
        platform["ownerPortal"]["classLabel"] = "Different snapshot"
        self.platform.write_text(json.dumps(platform))
        other_web = INSTALLER.install(self.args("web"))
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


@unittest.skipUnless(
    os.environ.get("OWNER_PORTAL_RELEASE_INTEGRATION") == "1", "explicit Node 24 build integration"
)
class BuildIntegrationTests(unittest.TestCase):
    def test_actual_vite_build_and_two_archives_on_real_filesystem(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-build-", dir=ROOT / ".tmp") as directory:
            repository, commit = clean_repository(ROOT, Path(directory) / "source")
            frontend = repository / "frontend/owner-portal"
            subprocess.run(["npm", "ci"], cwd=frontend, check=True, stdout=subprocess.DEVNULL)
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
