from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from openstack_platform.controller import database as db
from openstack_platform.controller import source_key_backup as keys
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

    def _keys(self) -> Path:
        root = self.root / "source-keys"
        root.mkdir(mode=0o700)
        directory = root / "hosted-app"
        directory.mkdir(mode=0o700)
        for name in keys.KEY_NAMES:
            path = directory / name
            path.write_bytes(
                b"private fixture" if name == "id_ed25519" else b"ssh-ed25519 public fixture"
            )
            path.chmod(0o600)
        os.utime(directory / "id_ed25519.pub", ns=(1234567890000000000, 1234567890000000000))
        return root

    def test_key_archive_is_paired_encrypted_and_restores_modes_and_public_mtime(self) -> None:
        root = self._keys()
        # Existing helper versions used ssh-keygen's 0644 public-file default.
        (root / "hosted-app/id_ed25519.pub").chmod(0o644)
        (root / ".new-hosted-app").mkdir()
        (root / "unknown-app").mkdir()
        name, _ = backup_hosted_database(
            self.connection,
            self.backups,
            source_keys_root=root,
            age_recipient="age1testrecipient",
            age_command=str(self._age()),
        )
        manifest = json.loads((self.backups / (name + ".manifest")).read_text())
        key_name = manifest["sourceKeys"]
        self.assertTrue((self.backups / (key_name + ".manifest")).is_file())
        plaintext = self.root / "keys.tar"
        plaintext.write_bytes((self.backups / key_name).read_bytes().split(b"\n", 1)[1])
        plaintext.chmod(0o600)
        with tarfile.open(plaintext) as archive:
            self.assertEqual(
                set(archive.getnames()), {"hosted-app/id_ed25519", "hosted-app/id_ed25519.pub"}
            )
        destination = self.root / "restored-keys"
        staging = keys.prepare_source_key_restore(plaintext, self.connection, destination)
        keys.commit_source_key_restore(staging, destination)
        public = destination / "hosted-app/id_ed25519.pub"
        self.assertEqual(public.stat().st_mtime_ns, 1234567890000000000)
        self.assertEqual(public.stat().st_mode & 0o777, 0o600)
        self.assertEqual(public.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(public.stat().st_uid, os.geteuid())
        self.assertFalse(list((self.state / "backup-work").iterdir()))

    def test_key_backup_retries_a_directory_replacement_and_refuses_links(self) -> None:
        root = self._keys()
        original = keys._read_key
        replaced = False

        def race(directory, name):
            nonlocal replaced
            if not replaced:
                replaced = True
                os.rename(root / "hosted-app", root / ".old-hosted-app")
                shutil.copytree(root / ".old-hosted-app", root / "hosted-app")
                raise FileNotFoundError("replacement race")
            return original(directory, name)

        with mock.patch.object(keys, "_read_key", race):
            keys.write_source_key_archive(self.connection, root, self.root / "race.tar")
        public = root / "hosted-app/id_ed25519.pub"
        public.unlink()
        public.symlink_to(root / ".old-hosted-app/id_ed25519.pub")
        with self.assertRaises(OSError):
            keys.write_source_key_archive(self.connection, root, self.root / "link.tar")

    def test_key_restore_refuses_unknown_slugs_links_duplicates_and_incomplete_pairs(self) -> None:
        for name, kind in (
            ("../id_ed25519", tarfile.REGTYPE),
            ("unknown/id_ed25519", tarfile.REGTYPE),
            ("hosted-app/id_ed25519", tarfile.SYMTYPE),
            ("hosted-app/id_ed25519", tarfile.REGTYPE),
        ):
            with self.subTest(name=name, kind=kind):
                path = self.root / "invalid.tar"
                with tarfile.open(path, "w") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.size = 4
                    archive.addfile(member, io.BytesIO(b"test"))
                path.chmod(0o600)
                with self.assertRaises(keys.SourceKeyBackupError):
                    keys.prepare_source_key_restore(path, self.connection, self.root / "restored")
                self.assertFalse(list(self.root.glob(".source-keys-restore-*")))

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
