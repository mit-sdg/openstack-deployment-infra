from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openstack_platform import release_manifest
from tests.repository_fixtures import clean_repository

ROOT = Path(__file__).resolve().parents[1]


class ReleaseManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_temporary = tempfile.TemporaryDirectory()
        cls.repository, cls.commit = clean_repository(
            ROOT, Path(cls.repository_temporary.name) / "repository"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.repository_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_temporary_repository_does_not_launch_background_git_maintenance(self) -> None:
        trace = self.root / "git-trace.jsonl"
        config = self.root / "gitconfig"
        config.write_text("[maintenance]\n auto = true\n[gc]\n auto = 1\n autoDetach = true\n")
        with mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TRACE2_EVENT": str(trace),
            },
        ):
            repository, commit = clean_repository(ROOT, self.root / "isolated")
            observed = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(observed.stdout, "")
        self.assertEqual(len(commit), 40)
        children = [
            event.get("argv", [])
            for line in trace.read_text().splitlines()
            if (event := json.loads(line)).get("event") == "child_start"
        ]
        self.assertFalse(any("maintenance" in args or "gc" in args for args in children), children)

    def test_production_signature_binds_every_component_and_evidence_file(self) -> None:
        key = self.root / "key.pem"
        public = self.root / "trust-root.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", key], check=True)
        subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", public], check=True)
        evidence = self.root / "evidence"
        manifest_path = release_manifest.generate(
            self.repository, self.commit, evidence, signing_key=key, unsigned=False
        )

        document = release_manifest.verify(
            self.repository,
            manifest_path,
            expected_commit=self.commit,
            signature=evidence / "release-manifest.sig",
            trust_root=public,
        )

        components = document["components"]
        self.assertEqual(set(components["roleImages"]), set(release_manifest.ROLES))
        self.assertEqual(components["controller"]["apiVersion"], 1)
        self.assertGreaterEqual(components["controller"]["schemaVersion"], 1)
        self.assertEqual(components["ui"]["status"], "shipped")
        self.assertEqual(components["ui"]["authProtocolVersion"], 2)
        self.assertEqual(
            components["ui"]["artifactEvidenceFormat"], "openstack-platform-management-artifacts-v1"
        )
        self.assertEqual(document["releaseChannel"], "production")

    def test_manifest_signature_and_sbom_tampering_are_rejected(self) -> None:
        key = self.root / "key.pem"
        public = self.root / "public.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", key], check=True)
        subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", public], check=True)
        evidence = self.root / "evidence"
        manifest = release_manifest.generate(
            self.repository, self.commit, evidence, signing_key=key, unsigned=False
        )
        signature = evidence / "release-manifest.sig"

        original = manifest.read_bytes()
        manifest.write_bytes(original.replace(b'"apiVersion":1', b'"apiVersion":2'))
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "signature"):
            release_manifest.verify(
                self.repository,
                manifest,
                expected_commit=self.commit,
                signature=signature,
                trust_root=public,
            )
        manifest.write_bytes(original)

        sbom = evidence / "release.sbom.json"
        sbom.write_bytes(sbom.read_bytes() + b" ")
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "SBOM|sbom"):
            release_manifest.verify(
                self.repository,
                manifest,
                expected_commit=self.commit,
                signature=signature,
                trust_root=public,
            )

    def test_each_source_binding_rejects_tampering(self) -> None:
        evidence = self.root / "evidence"
        manifest = release_manifest.generate(
            self.repository, self.commit, evidence, signing_key=None, unsigned=True
        )
        fixture = self.root / "source"
        fixture.mkdir()
        for name in ("pyproject.toml", "uv.lock", "flake.nix", "flake.lock"):
            shutil.copy2(self.repository / name, fixture / name)
        for directory in ("infra", "nix", "openstack_platform"):
            shutil.copytree(self.repository / directory, fixture / directory)

        targets = (
            "infra/lib/platform_contract.json",
            "uv.lock",
            "openstack_platform/helper/actions-v1.txt",
            "openstack_platform/controller/api.py",
            "openstack_platform/operator.py",
            "nix/roles/admin.nix",
        )
        for relative in targets:
            with self.subTest(relative=relative):
                path = fixture / relative
                before = path.read_bytes()
                path.write_bytes(before + b"\n# tamper\n")
                with self.assertRaises(release_manifest.ReleaseVerificationError):
                    release_manifest.verify(
                        fixture,
                        manifest,
                        expected_commit=self.commit,
                        signature=None,
                        trust_root=None,
                        allow_unsigned_development=True,
                    )
                path.write_bytes(before)

    def test_unsigned_mode_is_visibly_development_only_and_never_production(self) -> None:
        evidence = self.root / "evidence"
        manifest = release_manifest.generate(
            self.repository, self.commit, evidence, signing_key=None, unsigned=True
        )
        document = json.loads(manifest.read_text())
        self.assertEqual(document["releaseChannel"], "development-unsigned")
        self.assertEqual(document["trust"]["warning"], "NOT FOR PRODUCTION")

        with self.assertRaisesRegex(
            release_manifest.ReleaseVerificationError, "explicit non-production"
        ):
            release_manifest.verify(
                self.repository,
                manifest,
                expected_commit=self.commit,
                signature=None,
                trust_root=None,
            )
        with mock.patch.dict(os.environ, {"PLATFORM_ENVIRONMENT": "production"}):
            with self.assertRaisesRegex(
                release_manifest.ReleaseVerificationError, "explicit non-production"
            ):
                release_manifest.verify(
                    self.repository,
                    manifest,
                    expected_commit=self.commit,
                    signature=None,
                    trust_root=None,
                    allow_unsigned_development=True,
                )

    def test_management_identities_ignore_checkout_and_tmpdir_component_names(self) -> None:
        expected = release_manifest.component_set(self.repository, self.commit)["ui"]
        for name in ("broker", "identity", "web", "dev"):
            with self.subTest(parent=name):
                parent = self.root / name
                parent.mkdir()
                copy = parent / "checkout"
                shutil.copytree(self.repository, copy)
                with mock.patch.dict(os.environ, {"TMPDIR": str(parent)}):
                    actual = release_manifest.component_set(copy, self.commit)["ui"]
                self.assertEqual(actual, expected)

    def test_workspace_sbom_binds_local_sources_and_requires_registry_integrity(self) -> None:
        repository, _commit = clean_repository(ROOT, self.root / "workspace")
        lockfile = repository / "frontend/package-lock.json"
        packages = release_manifest.npm_spdx_packages(lockfile)
        shared = next(row for row in packages if row["name"] == "@openstack-platform/ui")
        self.assertRegex(shared["comment"], r"workspace source sha256: [0-9a-f]{64}")
        source = repository / "frontend/shared/src/Mark.tsx"
        source.write_text(source.read_text() + "\n// changed shared source\n")
        changed = release_manifest.npm_spdx_packages(lockfile)
        self.assertNotEqual(
            shared["comment"],
            next(row for row in changed if row["name"] == shared["name"])["comment"],
        )
        document = json.loads(lockfile.read_bytes())
        registry = next(row for row in document["packages"].values() if "integrity" in row)
        registry.pop("integrity")
        lockfile.write_text(json.dumps(document))
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "integrity"):
            release_manifest.npm_spdx_packages(lockfile)

    def test_workspace_links_cannot_escape_the_declared_packages(self) -> None:
        repository, _commit = clean_repository(ROOT, self.root / "workspace")
        lockfile = repository / "frontend/package-lock.json"
        document = json.loads(lockfile.read_bytes())
        link = document["packages"]["node_modules/@openstack-platform/ui"]
        link["resolved"] = "../outside"
        lockfile.write_text(json.dumps(document))
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "workspace link"):
            release_manifest.npm_spdx_packages(lockfile)

    def test_owner_identity_includes_shared_source_and_workspace_configuration(self) -> None:
        repository, commit = clean_repository(ROOT, self.root / "workspace")
        before = release_manifest.component_set(repository, commit)["ui"]["frontendSha256"]
        source = repository / "frontend/shared/src/Mark.tsx"
        source.write_text(source.read_text() + "\n// shared change\n")
        after = release_manifest.component_set(repository, commit)["ui"]["frontendSha256"]
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
