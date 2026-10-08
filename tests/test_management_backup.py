"""Online broker snapshots, encrypted evidence and session-invalidating restore."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openstack_platform import backup_retention, durable, recovery_bundle
from openstack_platform.management.backup import (
    backup_database,
    restore_database,
    validate_database,
)
from tests import test_recovery_bundle as bundle_fixtures
from tests.test_management import ManagementCase


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
