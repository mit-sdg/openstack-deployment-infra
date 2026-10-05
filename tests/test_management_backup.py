"""Online broker snapshots, encrypted evidence and session-invalidating restore."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from openstack_platform import backup_retention, durable, recovery_bundle
from openstack_platform.config import load_platform
from openstack_platform.management import backup
from openstack_platform.management.backup import (
    backup_database,
    restore_database,
    validate_database,
)
from tests import test_full_loss_recovery_drill as drill_fixtures
from tests import test_recovery_bundle as bundle_fixtures
from tests.test_management import ROOT, ManagementCase


class ManagementBackupTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.app = self.create()
        self.destination = self.root / "management-broker"
        self.age = self.root / "fake-age"
        self.age.write_text(
            "#!/usr/bin/env python3\nimport pathlib,sys\npathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(b'age-encryption.org/v1\\n'+pathlib.Path(sys.argv[-1]).read_bytes())\n"
        )
        os.chmod(self.age, 0o700)

    def make_backup(
        self, created_at: datetime = datetime(2026, 10, 2, 2, 30, tzinfo=UTC)
    ) -> tuple[str, str]:
        return backup_database(
            self.broker.database.path,
            self.destination,
            "age1" + "q" * 58,
            str(self.age),
            created_at=created_at,
        )

    def names(self) -> set[str]:
        return {path.name for path in self.destination.iterdir()}

    @staticmethod
    def trio(name: str) -> set[str]:
        return {name, name + ".sha256", name + ".manifest"}

    def test_retention_keeps_two_weeks_and_the_newest_three_sets(self) -> None:
        now = datetime(2026, 10, 2, 2, 30, tzinfo=UTC)
        names = [self.make_backup(now - timedelta(days=days))[0] for days in (30, 20, 16, 10, 0)]
        leftover = "management-broker-20260801T023000Z.sqlite3.age"
        (self.destination / leftover).write_bytes(b"age-encryption.org/v1\n")
        (self.destination / "management-broker-20260802T023000Z.sqlite3.age.manifest").symlink_to(
            self.destination / names[0]
        )
        (self.destination / "README").write_text("operator note\n")
        result = backup_retention.prune(
            self.destination, series="management-broker", keep=names[-1], now=now
        )
        # Ten days and today are inside the window; the floor keeps a third.
        self.assertEqual(
            self.names(),
            {
                ".staging",
                "README",
                "management-broker-20260802T023000Z.sqlite3.age.manifest",
                *(item for name in names[2:] for item in self.trio(name)),
            },
        )
        self.assertEqual((result.removed, result.kept, len(result.ignored)), (3, 3, 2))
        selected = recovery_bundle._selected_component_files("management-broker", self.destination)
        self.assertEqual({path.name for path in selected}, self.trio(names[-1]))

    def run_command(self, now: datetime) -> tuple[int, str, str]:
        recipient = self.root / "recipient.txt"
        recipient.write_text("age1" + "q" * 58 + "\n")
        argv = [
            "openstack-platform-management-broker-backup",
            "backup",
            f"--database={self.broker.database.path}",
            f"--destination={self.destination}",
            f"--recipient-file={recipient}",
            f"--age={self.age}",
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(backup, "datetime", mock.Mock(now=lambda _zone: now)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = backup.main()
        return status, stdout.getvalue(), stderr.getvalue()

    def test_backup_command_prunes_after_commit_and_fails_on_retention_failure(self) -> None:
        now = datetime(2026, 10, 2, 2, 30, tzinfo=UTC)
        for days in (90, 80, 70):
            self.make_backup(now - timedelta(days=days))
        status, stdout, _stderr = self.run_command(now)
        self.assertEqual(status, 0)
        self.assertIn("management-broker-backup=management-broker-20261002T023000Z", stdout)
        self.assertIn("retention=ok removed=1 kept=3", stdout)

        self.make_backup(now - timedelta(days=60))
        later = now + timedelta(hours=1)
        with mock.patch.object(
            backup_retention, "_remove", side_effect=PermissionError(13, "denied")
        ):
            status, stdout, stderr = self.run_command(later)
        self.assertEqual(status, 1)
        self.assertIn("management-broker-backup=management-broker-20261002T033000Z", stdout)
        self.assertIn("retention=failed reason=filesystem-EACCES", stderr)
        self.assertTrue(self.trio("management-broker-20261002T033000Z.sqlite3.age") <= self.names())

    def test_online_backup_has_committed_encrypted_evidence_and_no_plaintext(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE apps SET revision=0 WHERE id=?", (self.app,))
        name, checksum = self.make_backup()
        manifest = json.loads((self.destination / (name + ".manifest")).read_text())
        self.assertEqual(manifest["format"], "openstack-platform-management-broker-backup-v1")
        self.assertEqual(manifest["sha256"], checksum)
        self.assertEqual((self.destination / name).stat().st_mode & 0o777, 0o640)
        self.assertEqual(list((self.destination / ".staging").iterdir()), [])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.make_backup()

    def test_restore_preserves_owners_intents_and_invalidates_sessions_challenges(self) -> None:
        name, _checksum = self.make_backup()
        decrypted = self.root / "decrypted.sqlite3"
        decrypted.write_bytes((self.destination / name).read_bytes().split(b"\n", 1)[1])
        decrypted.chmod(0o600)
        target = self.root / "replacement/management.sqlite3"
        restore_database(decrypted, target)
        connection = sqlite3.connect(target)
        try:
            validate_database(connection)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertIsNone(
                connection.execute("SELECT name FROM sqlite_master WHERE name='flows'").fetchone()
            )
            self.assertEqual(connection.execute("SELECT id FROM apps").fetchone()[0], self.app)
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM intents").fetchone()[0], 0)
        finally:
            connection.close()

    def test_failed_encryption_does_not_commit_any_evidence(self) -> None:
        bad = self.root / "bad-age"
        bad.write_text("#!/bin/sh\nexit 1\n")
        bad.chmod(0o700)
        from openstack_platform.runtime import RuntimeFailure

        with self.assertRaises(RuntimeFailure):
            backup_database(
                self.broker.database.path, self.destination, "age1" + "q" * 58, str(bad)
            )
        self.assertEqual(list(self.destination.glob("*.manifest")), [])
        self.assertEqual(list((self.destination / ".staging").iterdir()), [])

    def test_four_class_export_verify_and_import_with_legacy_compatibility(self) -> None:
        fixture = bundle_fixtures.RecoveryBundleTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.make_backup()
        sources = {**fixture.sources, "management-broker": self.destination}
        exported = recovery_bundle.export_bundle(
            fixture.destination, sources, deployment="owner-portal"
        )
        manifest = recovery_bundle.verify_bundle(exported)
        self.assertEqual(manifest["format"], "openstack-platform-offsite-recovery-v2")
        imported_root = self.root / "import"
        imported_root.mkdir(mode=0o700)
        imported = recovery_bundle.import_bundle(exported, imported_root)
        self.assertEqual(recovery_bundle.verify_bundle(imported), manifest)
        legacy = fixture.export()
        self.assertEqual(
            recovery_bundle.verify_bundle(legacy)["format"],
            "openstack-platform-offsite-recovery-v1",
        )

    def test_v3_export_includes_broker_and_paired_deploy_keys(self) -> None:
        fixture = bundle_fixtures.RecoveryBundleTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        key_name = fixture._key_trio()
        self.make_backup()
        exported = recovery_bundle.export_bundle(
            fixture.destination,
            {**fixture.sources, "management-broker": self.destination},
            deployment="portal-keys",
        )
        manifest = recovery_bundle.verify_bundle(exported)
        self.assertEqual(manifest["format"], "openstack-platform-offsite-recovery-v3")
        self.assertIn("management-broker", recovery_bundle.bundle_components(manifest))
        self.assertIn("hosted-controller/" + key_name, {item["path"] for item in manifest["files"]})

    def test_source_discovery_cannot_omit_required_broker_backup(self) -> None:
        fixture = bundle_fixtures.RecoveryBundleTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        platform, _config, _mountinfo = fixture._scheduled_environment()
        backup_root = Path(json.loads(platform.read_text())["paths"]["backups"])
        with self.assertRaises(recovery_bundle.RecoveryBundleError):
            recovery_bundle.discover_latest_sources(
                backup_root, "production", bounds=recovery_bundle.Bounds(), require_management=True
            )

    def test_active_or_staged_broker_requires_evidence_when_other_selector_is_absent(self) -> None:
        value = json.loads((ROOT / "config/platform.example.json").read_text())
        value["paths"]["adminState"] = str(self.root / "admin-state")
        path = self.root / "platform.json"
        path.write_text(json.dumps(value))
        platform = load_platform(path)
        self.assertFalse(recovery_bundle._management_backup_required(platform))
        for name in ("management-active/current", "management-broker-releases/current"):
            selector = Path(value["paths"]["adminState"]) / name
            selector.parent.mkdir(parents=True)
            selector.symlink_to("missing-retained-release")
            self.assertTrue(recovery_bundle._management_backup_required(platform))
            selector.unlink()
            self.assertFalse(recovery_bundle._management_backup_required(platform))

    def test_restore_rejects_identity_sidecars_and_invalid_key_before_replacement(self) -> None:
        name, _checksum = self.make_backup()
        decrypted = self.root / "decrypted.sqlite3"
        decrypted.write_bytes((self.destination / name).read_bytes().split(b"\n", 1)[1])
        decrypted.chmod(0o600)
        target = self.root / "replacement/management.sqlite3"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            restore_database(decrypted, target, identity="different-platform")
        self.assertFalse(target.exists())
        sidecar = Path(str(target) + "-wal")
        sidecar.touch()
        with self.assertRaisesRegex(ValueError, "sidecars"):
            restore_database(decrypted, target)
        sidecar.unlink()
        key = target.parent / "anonymous.key"
        key.write_bytes(b"x" * 32)
        key.chmod(0o644)
        with self.assertRaises(durable.DurableReplaceError):
            restore_database(decrypted, target)
        self.assertFalse(target.exists())
        key.chmod(0o600)
        restore_database(decrypted, target)
        self.assertFalse(key.exists())

    def test_scheduled_four_class_receipt_and_status_require_initialized_broker(self) -> None:
        fixture = bundle_fixtures.RecoveryBundleTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        platform, config, mountinfo = fixture._scheduled_environment()
        value = json.loads(platform.read_text())
        state = self.root / "admin-state"
        value["paths"]["adminState"] = str(state)
        platform.write_text(json.dumps(value))
        receipt = fixture.root / "status/offsite-export.json"
        receipt.parent.mkdir(mode=0o700)
        arguments = dict(
            now=datetime(2026, 8, 30, 13, tzinfo=UTC),
            mountinfo_path=mountinfo,
            device_resolver=fixture._different_devices,
        )
        recovery_bundle.scheduled_export(platform, config, receipt, **arguments)
        launcher = state / "management-broker-releases/current/bin/management-broker"
        launcher.parent.mkdir(parents=True)
        launcher.touch()
        with self.assertRaisesRegex(recovery_bundle.RecoveryBundleError, "fourth-class"):
            recovery_bundle.recovery_status(platform, config, receipt, **arguments)
        self.destination = Path(value["paths"]["backups"]) / "management-broker"
        self.make_backup()
        arguments["now"] = datetime(2026, 8, 30, 14, tzinfo=UTC)
        recovery_bundle.scheduled_export(platform, config, receipt, **arguments)
        self.assertEqual(
            json.loads(receipt.read_text())["format"], "openstack-platform-offsite-recovery-v2"
        )
        self.assertTrue(
            recovery_bundle.recovery_status(platform, config, receipt, **arguments)["verified"]
        )

    def test_full_loss_drill_restores_fourth_class_and_invalidates_sessions(self) -> None:
        fixture = drill_fixtures.FullLossRecoveryDrillTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        name, _checksum = self.make_backup()
        directory = fixture.bundle / "management-broker"
        directory.mkdir(mode=0o700)
        # The drill's fake age tool passes through an already decrypted fixture.
        (directory / name).write_bytes((self.destination / name).read_bytes().split(b"\n", 1)[1])
        result, work = fixture._run("--full")
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads((work / "DRILL-EVIDENCE.json").read_text())
        self.assertEqual(evidence["managementBroker"]["sessions"], "invalidated")
