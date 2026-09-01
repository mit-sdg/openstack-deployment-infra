from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from openstack_platform import config, operator, restore
from openstack_platform.controller import database as db
from openstack_platform.validation import ValidationError

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

    def test_restore_rejects_a_marked_backup_with_a_missing_expected_index(self) -> None:
        self._write_backup()
        source = sqlite3.connect(self.source)
        source.execute("DROP INDEX one_unfinished_operation_per_scope")
        source.commit()
        source.close()
        current = self._database(self.destination)
        current.close()
        before = self.destination.read_bytes()

        with self.assertRaisesRegex(restore.RestoreError, "candidate migrations or integrity"):
            restore.restore_database(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_backup_temp_paths_do_not_reuse_or_delete_stale_age_output(self) -> None:
        work = self.state / "backup-work"
        work.mkdir(mode=0o700)
        stale = work / "platform-20260101T000000Z.sqlite3.age"
        stale.write_bytes(b"stale")
        stale.chmod(0o600)
        first = operator._unlinked_backup_temp(work, suffix=".sqlite3.age")
        second = operator._unlinked_backup_temp(work, suffix=".sqlite3.age")
        self.assertNotEqual(first, second)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(stale.read_bytes(), b"stale")

    def test_backup_staging_path_comes_from_configured_backup_root(self) -> None:
        loaded = config.load(
            Path("config/platform.example.json"),
            Path("config/platform-policy.example.json"),
            require_private_policy=False,
        )
        self.assertEqual(
            operator._configured_backup_staging_path(
                loaded, "platform-20260101T000000Z.sqlite3.age"
            ),
            "/srv/app-platform-backups/controller/.staging/platform-20260101T000000Z.sqlite3.age",
        )
        with self.assertRaisesRegex(ValidationError, "backup name"):
            operator._configured_backup_staging_path(loaded, "../backup.sqlite3.age")

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

    def test_latest_restore_verifier_compares_the_real_age_v1_header(self) -> None:
        script = (Path(__file__).parents[1] / "infra/backup/verify_latest_restore.sh").read_text()
        self.assertIn('handle.read(22) != b"age-encryption.org/v1\\n"', script)
        self.assertNotIn('handle.read(22) != b"age-encryption.org/v1\\\\n"', script)


if __name__ == "__main__":
    unittest.main()
