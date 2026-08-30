from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from openstack_platform.controller import database as db
from openstack_platform.controller.hosted_backup import HostedBackupError, backup_hosted_database


class HostedControllerBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.database = self.state / "platform.sqlite3"
        self.connection = db.connect(self.database)
        db.migrate(self.connection)
        db.put_application(
            self.connection,
            application_id="11111111-1111-4111-8111-111111111111",
            application_slug="hosted-app",
            worker_flavor="example.1c2g",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=2048,
        )
        self.backups = self.root / "hosted-controller"

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _age(self, *, valid: bool = True) -> Path:
        command = self.root / ("age-good" if valid else "age-bad")
        header = "age-encryption.org/v1\n" if valid else "not-age\n"
        command.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "source = pathlib.Path(sys.argv[-1])\n"
            f"output.write_bytes({header.encode()!r} + source.read_bytes())\n",
            encoding="utf-8",
        )
        command.chmod(0o700)
        return command

    def test_encrypted_backup_commits_manifest_last_and_contains_live_state(self) -> None:
        name, digest = backup_hosted_database(
            self.connection,
            self.backups,
            age_recipient="age1testrecipient",
            age_command=str(self._age()),
            created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        self.assertEqual(name, "hosted-controller-20260102T030405Z.sqlite3.age")
        ciphertext = self.backups / name
        checksum = self.backups / f"{name}.sha256"
        manifest_path = self.backups / f"{name}.manifest"
        self.assertTrue(ciphertext.read_bytes().startswith(b"age-encryption.org/v1\n"))
        self.assertEqual(checksum.read_text(), f"{digest}  {name}\n")
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["format"], "openstack-platform-hosted-controller-backup-v1")
        self.assertEqual(manifest["sha256"], digest)
        self.assertEqual(ciphertext.stat().st_mode & 0o777, 0o640)
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o640)
        self.assertFalse(list((self.backups / ".staging").iterdir()))
        self.assertFalse(list((self.state / "backup-work").iterdir()))

        plaintext = self.root / "recovered.sqlite3"
        plaintext.write_bytes(ciphertext.read_bytes().split(b"\n", 1)[1])
        recovered = sqlite3.connect(plaintext)
        try:
            self.assertEqual(
                recovered.execute("SELECT slug FROM applications").fetchone()[0], "hosted-app"
            )
            self.assertEqual(recovered.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            recovered.close()

    def test_bad_age_output_leaves_no_accepted_or_plaintext_backup(self) -> None:
        with self.assertRaisesRegex(HostedBackupError, "age-v1"):
            backup_hosted_database(
                self.connection,
                self.backups,
                age_recipient="age1testrecipient",
                age_command=str(self._age(valid=False)),
                created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            )
        self.assertFalse(list(self.backups.glob("*.manifest")))
        self.assertFalse(list(self.backups.glob("*.age")))
        self.assertFalse(list((self.backups / ".staging").iterdir()))
        self.assertFalse(list((self.state / "backup-work").iterdir()))

    def test_existing_committed_name_is_never_overwritten(self) -> None:
        arguments = {
            "age_recipient": "age1testrecipient",
            "age_command": str(self._age()),
            "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        }
        backup_hosted_database(self.connection, self.backups, **arguments)
        before = {path.name: path.read_bytes() for path in self.backups.glob("*") if path.is_file()}
        with self.assertRaisesRegex(HostedBackupError, "already exists"):
            backup_hosted_database(self.connection, self.backups, **arguments)
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.backups.glob("*") if path.is_file()},
            before,
        )


if __name__ == "__main__":
    unittest.main()
