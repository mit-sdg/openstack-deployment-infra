from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from openstack_platform import recovery_bundle


class RecoveryBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.destination = self.root / "offsite"
        self.destination.mkdir(mode=0o700)
        self.sources: dict[str, Path] = {}
        for component in ("hosted-controller", "operator-state", "managed-data"):
            path = self.root / component
            path.mkdir(mode=0o700)
            self.sources[component] = path
        self._sqlite_trio("hosted-controller", "hosted-controller-20260830T120000Z.sqlite3.age")
        self._sqlite_trio("operator-state", "platform-20260830T120000Z.sqlite3.age")
        managed_sums = b""
        for name in ("postgres.age", "mongodb.age", "garage.age", "registry.age"):
            payload = b"age-encryption.org/v1\nevidence-" + name.encode()
            self._file(self.sources["managed-data"] / name, payload)
            managed_sums += f"{hashlib.sha256(payload).hexdigest()}  {name}\n".encode()
        self._file(self.sources["managed-data"] / "SHA256SUMS", managed_sums)
        self._file(
            self.sources["managed-data"] / "MANIFEST",
            b"created_at=20260830T120000Z\nformat_version=2\npostgres=pg_dumpall-clean-if-exists\nmongodb=mongodump-archive-gzip\nobject_storage=garage-s3-catalog-tar-gzip\nregistry=distribution-artifacts-tar-gzip\n",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _file(path: Path, content: bytes) -> None:
        path.write_bytes(content)
        path.chmod(0o600)

    def _sqlite_trio(self, component: str, name: str) -> None:
        root = self.sources[component]
        payload = b"age-encryption.org/v1\nsqlite-evidence"
        digest = hashlib.sha256(payload).hexdigest()
        self._file(root / name, payload)
        self._file(root / f"{name}.sha256", f"{digest}  {name}\n".encode())
        if component == "hosted-controller":
            manifest = {
                "format": "openstack-platform-hosted-controller-backup-v1",
                "name": name,
                "sha256": digest,
                "createdAt": "20260830T120000Z",
            }
            content = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        else:
            content = (
                f"format_version=1\nname={name}\ncreated_at=20260830T120000Z\n"
                f"encryption=age-v1\nciphertext_sha256={digest}\nplaintext_sha256={'a' * 64}\n"
                "sqlite_integrity=ok\nintegrity_checked_at=20260830T120001Z\n"
            ).encode()
        self._file(root / f"{name}.manifest", content)

    def export(self) -> Path:
        return recovery_bundle.export_bundle(
            self.destination,
            self.sources,
            deployment="production",
            created_at=datetime(2026, 8, 30, 12, 30, tzinfo=UTC),
        )

    def test_export_verify_and_full_loss_import_without_original_sources(self) -> None:
        bundle = self.export()
        manifest = recovery_bundle.verify_bundle(bundle)
        self.assertEqual(manifest["format"], "openstack-platform-offsite-recovery-v1")
        self.assertIn("managed-data/registry.age", {item["path"] for item in manifest["files"]})

        # Model loss of the cloud backup volume and registry.  Only the copied
        # off-site directory remains available to a fresh recovery workspace.
        for source in self.sources.values():
            for path in source.iterdir():
                path.unlink()
            source.rmdir()
        recovery_root = self.root / "fresh-recovery-host"
        recovery_root.mkdir(mode=0o700)
        imported = recovery_bundle.import_bundle(bundle, recovery_root)
        self.assertEqual(recovery_bundle.verify_bundle(imported), manifest)
        self.assertTrue((imported / "managed-data" / "registry.age").is_file())

    def test_manifest_or_payload_tampering_is_refused(self) -> None:
        bundle = self.export()
        payload = bundle / "managed-data" / "garage.age"
        payload.chmod(0o600)
        payload.write_bytes(b"changed")
        with self.assertRaisesRegex(recovery_bundle.RecoveryBundleError, "checksum|age v1"):
            recovery_bundle.verify_bundle(bundle)

    def test_import_and_export_never_replace_existing_evidence(self) -> None:
        bundle = self.export()
        with self.assertRaisesRegex(recovery_bundle.RecoveryBundleError, "already exists"):
            self.export()
        recovery_root = self.root / "recovery"
        recovery_root.mkdir(mode=0o700)
        recovery_bundle.import_bundle(bundle, recovery_root)
        marker = recovery_root / bundle.name / "MANIFEST.json"
        before = marker.read_bytes()
        with self.assertRaisesRegex(recovery_bundle.RecoveryBundleError, "never replaced"):
            recovery_bundle.import_bundle(bundle, recovery_root)
        self.assertEqual(marker.read_bytes(), before)

    def test_export_refuses_managed_backup_without_registry_artifacts(self) -> None:
        (self.sources["managed-data"] / "registry.age").unlink()
        with self.assertRaisesRegex(recovery_bundle.RecoveryBundleError, "OCI artifacts"):
            self.export()

    def test_receipt_is_secret_free_monitoring_evidence(self) -> None:
        status = self.root / "status"
        status.mkdir(mode=0o700)
        receipt = status / "offsite-export.json"
        bundle = recovery_bundle.export_bundle(
            self.destination,
            self.sources,
            deployment="production",
            created_at=datetime(2026, 8, 30, 12, 30, tzinfo=UTC),
            receipt=receipt,
        )
        value = json.loads(receipt.read_text())
        self.assertEqual(value["bundle"], bundle.name)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("path", value)


if __name__ == "__main__":
    unittest.main()
