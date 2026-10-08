from __future__ import annotations

import io
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from openstack_platform import config, operator, restore
from openstack_platform.controller import database as db

APP_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "22222222-2222-4222-8222-222222222222"


class OfflineRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.source = self.root / "backup.sqlite3"
        self.destination = self.state / "platform.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _database(
        self, path: Path, *, identity: db.DeploymentIdentity | None = None
    ) -> sqlite3.Connection:
        connection = db.connect(path, identity=identity)
        db.migrate(connection, identity=identity)
        return connection

    def _write_backup(
        self, *, unfinished: bool = False, identity: db.DeploymentIdentity | None = None
    ) -> None:
        self.source.unlink(missing_ok=True)
        connection = self._database(
            self.root / ("live-unfinished.sqlite3" if unfinished else "live.sqlite3"),
            identity=identity,
        )
        db.put_application(
            connection,
            application_id=APP_ID,
            application_slug="demo-app",
            worker_flavor="example.1c2g",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=2048,
        )
        if unfinished:
            db.begin_operation(
                connection,
                operation_id=OPERATION_ID,
                kind="app.deploy",
                scope=f"app-{APP_ID}",
                phase="validated",
                deadline_at="2099-01-01T00:00:00Z",
            )
        db.backup_database(connection, self.source)
        connection.close()

    def test_deploy_keys_are_validated_before_database_replacement(self) -> None:
        self._write_backup()
        archive = self.root / "keys.tar"

        def write(name):
            with tarfile.open(archive, "w") as handle:
                for key in ("id_ed25519", "id_ed25519.pub"):
                    member = tarfile.TarInfo(f"{name}/{key}")
                    member.size = 4
                    member.mtime = 1234567890
                    handle.addfile(member, io.BytesIO(b"test"))
            archive.chmod(0o600)

        write("other-app")
        with self.assertRaises(restore.RestoreError):
            restore.restore_database(
                self.source,
                self.destination,
                source_keys_archive=archive,
                source_keys_directory=self.root / "keys",
            )
        self.assertFalse(self.destination.exists())
        write("demo-app")
        restore.restore_database(
            self.source,
            self.destination,
            source_keys_archive=archive,
            source_keys_directory=self.root / "keys",
        )
        self.assertEqual((self.root / "keys/demo-app/id_ed25519").read_bytes(), b"test")
        self.assertEqual((self.root / "keys/demo-app/id_ed25519.pub").stat().st_mtime, 1234567890)

    def test_restore_migrates_verifies_and_atomically_replaces_state(self) -> None:
        self._write_backup()
        current = self._database(self.destination)
        current.close()
        result = restore.restore_database(self.source, self.destination)
        self.assertEqual(result.schema_version, db.MIGRATIONS[-1].version)
        self.assertEqual(result.integrity, "ok")
        connection = db.connect(self.destination)
        self.assertEqual(
            connection.execute("SELECT slug FROM applications").fetchone()[0], "demo-app"
        )
        connection.close()
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)

    def test_restore_refuses_unfinished_source_and_current_operations(self) -> None:
        self._write_backup(unfinished=True)
        with self.assertRaisesRegex(restore.RestoreError, "unfinished operations"):
            restore.restore_database(self.source, self.destination)
        self._write_backup()
        current = self._database(self.destination)
        db.begin_operation(
            current,
            operation_id=OPERATION_ID,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="validated",
            deadline_at="2099-01-01T00:00:00Z",
        )
        current.close()
        with self.assertRaisesRegex(restore.RestoreError, "current database has unfinished"):
            restore.restore_database(self.source, self.destination)

    def test_restore_replaces_only_the_exact_acknowledged_recovery_required_operation(self) -> None:
        self._write_backup()
        current = self._database(self.destination)
        db.put_application(
            current,
            application_id=APP_ID,
            application_slug="current-app",
            worker_flavor="example.1c2g",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=2048,
        )
        db.begin_operation(
            current,
            operation_id=OPERATION_ID,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="validated",
            deadline_at="2099-01-01T00:00:00Z",
        )
        db.mark_recovery_required(current, OPERATION_ID, "expected recovery")
        current.close()

        result = restore.restore_database(
            self.source,
            self.destination,
            expected_recovery_required_operation=OPERATION_ID,
        )
        self.assertEqual(result.integrity, "ok")
        connection = db.connect(self.destination)
        self.assertIsNone(db.get_operation(connection, OPERATION_ID))
        self.assertEqual(
            connection.execute("SELECT slug FROM applications").fetchone()[0], "demo-app"
        )
        connection.close()

    def test_restore_recovery_acknowledgement_fails_closed(self) -> None:
        self._write_backup()
        current = self._database(self.destination)
        db.begin_operation(
            current,
            operation_id=OPERATION_ID,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="validated",
            deadline_at="2099-01-01T00:00:00Z",
        )
        current.close()
        before = self.destination.read_bytes()
        with self.assertRaisesRegex(restore.RestoreError, "does not exactly match"):
            restore.restore_database(
                self.source,
                self.destination,
                expected_recovery_required_operation=OPERATION_ID,
            )
        with self.assertRaisesRegex(restore.RestoreError, "malformed"):
            restore.restore_database(
                self.source,
                self.destination,
                expected_recovery_required_operation="not-a-uuid",
            )
        self.assertEqual(self.destination.read_bytes(), before)

    def test_restore_checks_private_modes_and_leaves_destination_untouched_on_failure(self) -> None:
        self._write_backup()
        current = self._database(self.destination)
        current.close()
        before = self.destination.read_bytes()
        self.source.chmod(0o644)
        with self.assertRaisesRegex(restore.RestoreError, "mode-0600"):
            restore.restore_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_restore_rejects_a_marked_backup_with_a_missing_expected_object(self) -> None:
        self._write_backup()
        source = sqlite3.connect(self.source)
        source.execute("DROP TABLE applications")
        source.commit()
        source.close()
        current = self._database(self.destination)
        current.close()
        before = self.destination.read_bytes()

        with self.assertRaisesRegex(restore.RestoreError, "candidate migrations or integrity"):
            restore.restore_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_cli_restore_is_offline_and_requires_confirmation(self) -> None:
        example = Path("config/platform.example.json")
        identity = db.deployment_identity(config.load_platform(example))
        self._write_backup(identity=identity)
        args = operator.build_parser().parse_args(
            [
                "--platform-config",
                str(example),
                "--state-directory",
                str(self.state),
                "restore",
                str(self.source),
                "--yes",
            ]
        )
        from io import StringIO

        output = StringIO()
        operator.dispatch(args, stdout=output)
        self.assertIn("restore=verified", output.getvalue())


if __name__ == "__main__":
    unittest.main()
