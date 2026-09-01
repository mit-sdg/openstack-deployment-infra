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
        self.assertEqual(components["ui"]["status"], "not-shipped")
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


if __name__ == "__main__":
    unittest.main()
