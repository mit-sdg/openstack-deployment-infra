from __future__ import annotations

import hashlib
import json
import os
import shutil
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

    def _scheduled_environment(self) -> tuple[Path, Path, Path]:
        backups = self.root / "scheduled-backups"
        backups.mkdir(mode=0o700)
        for component, directory in (
            ("hosted-controller", "hosted-controller"),
            ("operator-state", "controller"),
        ):
            target = backups / directory
            shutil.copytree(self.sources[component], target)
            (target / ".staging").mkdir(mode=0o700)
        managed_root = backups / "production"
        managed_root.mkdir(mode=0o700)
        shutil.copytree(
            self.sources["managed-data"],
            managed_root / "20260830T120000Z",
        )

        platform = json.loads(
            (Path(__file__).resolve().parents[1] / "config/platform.example.json").read_text()
        )
        platform["namespace"] = "production"
        platform["paths"]["backups"] = str(backups)
        platform_path = self.root / "platform.json"
        self._file(platform_path, (json.dumps(platform) + "\n").encode())

        config = {
            "destination": str(self.destination),
            "filesystemType": "fuse.recovery",
            "format": "openstack-platform-offsite-export-config-v1",
            "limits": {
                "maximumFileBytes": 1024**3,
                "maximumTotalBytes": 2 * 1024**3,
            },
            "maximumReceiptAgeHours": 36,
            "mountSource": "institutional-recovery",
        }
        config_path = self.root / "offsite-export.json"
        self._file(config_path, (json.dumps(config) + "\n").encode())
        mountinfo = self.root / "mountinfo"
        mountinfo.write_text(
            f"42 30 0:99 / {self.destination} rw,nosuid - fuse.recovery institutional-recovery rw\n"
        )
        mountinfo.chmod(0o600)
        return platform_path, config_path, mountinfo

    def _different_devices(self, path: Path) -> int:
        return 2 if path == self.destination else 1

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

    def test_scheduled_export_refuses_same_filesystem_local_destination(self) -> None:
        platform, config, mountinfo = self._scheduled_environment()
        receipt = self.root / "status" / "offsite-export.json"
        receipt.parent.mkdir(mode=0o700)
        with self.assertRaisesRegex(recovery_bundle.RecoveryBundleError, "local backup filesystem"):
            recovery_bundle.scheduled_export(
                platform,
                config,
                receipt,
                now=datetime(2026, 8, 30, 13, tzinfo=UTC),
                mountinfo_path=mountinfo,
            )
        self.assertFalse(receipt.exists())

    def test_status_refuses_unmounted_and_stale_sink(self) -> None:
        platform, config, mountinfo = self._scheduled_environment()
        status = self.root / "status"
        status.mkdir(mode=0o700)
        receipt = status / "offsite-export.json"
        recovery_bundle.scheduled_export(
            platform,
            config,
            receipt,
            now=datetime(2026, 8, 30, 13, tzinfo=UTC),
            mountinfo_path=mountinfo,
            device_resolver=self._different_devices,
        )
        mountinfo.write_text("")
        with self.assertRaisesRegex(recovery_bundle.RecoveryBundleError, "not a distinct mounted"):
            recovery_bundle.recovery_status(
                platform,
                config,
                receipt,
                now=datetime(2026, 8, 30, 14, tzinfo=UTC),
                mountinfo_path=mountinfo,
                device_resolver=self._different_devices,
            )
        mountinfo.write_text(
            f"42 30 0:99 / {self.destination} rw,nosuid - fuse.recovery institutional-recovery rw\n"
        )
        with self.assertRaisesRegex(recovery_bundle.RecoveryBundleError, "stale"):
            recovery_bundle.recovery_status(
                platform,
                config,
                receipt,
                now=datetime(2026, 9, 1, 14, tzinfo=UTC),
                mountinfo_path=mountinfo,
                device_resolver=self._different_devices,
            )

    def test_schedule_failure_does_not_advance_receipt(self) -> None:
        platform, config, mountinfo = self._scheduled_environment()
        receipt = self.root / "status" / "offsite-export.json"
        receipt.parent.mkdir(mode=0o700)
        accepted = recovery_bundle.scheduled_export(
            platform,
            config,
            receipt,
            now=datetime(2026, 8, 30, 13, tzinfo=UTC),
            mountinfo_path=mountinfo,
            device_resolver=self._different_devices,
        )
        prior_receipt = receipt.read_bytes()
        broken = self.root / "scheduled-backups" / "production" / "20260831T120000Z"
        broken.mkdir(mode=0o700)
        self._file(broken / "MANIFEST", b"format_version=2\n")
        with self.assertRaises(recovery_bundle.RecoveryBundleError):
            recovery_bundle.scheduled_export(
                platform,
                config,
                receipt,
                now=datetime(2026, 8, 31, 13, tzinfo=UTC),
                mountinfo_path=mountinfo,
                device_resolver=self._different_devices,
            )
        self.assertEqual(receipt.read_bytes(), prior_receipt)
        self.assertEqual([path for path in self.destination.iterdir()], [accepted])

    def test_successful_recurring_scheduled_export_keeps_existing_bundles(self) -> None:
        platform, config, mountinfo = self._scheduled_environment()
        receipt = self.root / "status" / "offsite-export.json"
        receipt.parent.mkdir(mode=0o700)
        first = recovery_bundle.scheduled_export(
            platform,
            config,
            receipt,
            now=datetime(2026, 8, 30, 13, tzinfo=UTC),
            mountinfo_path=mountinfo,
            device_resolver=self._different_devices,
        )
        second = recovery_bundle.scheduled_export(
            platform,
            config,
            receipt,
            now=datetime(2026, 8, 31, 13, tzinfo=UTC),
            mountinfo_path=mountinfo,
            device_resolver=self._different_devices,
        )
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        status = recovery_bundle.recovery_status(
            platform,
            config,
            receipt,
            now=datetime(2026, 8, 31, 14, tzinfo=UTC),
            mountinfo_path=mountinfo,
            device_resolver=self._different_devices,
        )
        self.assertEqual(status["bundle"], second.name)
        self.assertTrue(status["verified"])

    def test_admin_role_supervises_daily_export(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "nix/roles/admin.nix").read_text()
        self.assertIn('systemd.services."${namespace}-offsite-export"', source)
        self.assertIn('systemd.timers."${namespace}-offsite-export"', source)
        self.assertIn('OnCalendar = "*-*-* 05:00:00 UTC"', source)
        self.assertIn("ConditionPathExists = offsiteExportConfig", source)
        self.assertIn('TimeoutStartSec = "24h"', source)

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
