from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform import backup_retention, recovery_bundle
from openstack_platform.controller import database as db
from openstack_platform.controller import hosted_backup
from openstack_platform.controller import source_key_backup as keys
from openstack_platform.controller.hosted_backup import HostedBackupError, backup_hosted_database

NOW = datetime(2026, 3, 1, 2, 15, tzinfo=UTC)
SUFFIXES = ("", ".sha256", ".manifest")


def stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%SZ")


class HostedBackupFixture(unittest.TestCase):
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


class HostedControllerBackupTests(HostedBackupFixture):
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

    def test_key_archive_leaves_out_deleted_apps(self) -> None:
        root = self._keys()
        deleted = "22222222-2222-4222-8222-222222222222"
        db.put_application(
            self.connection,
            application_id=deleted,
            application_slug="deleted-app",
            worker_flavor="example.1c2g",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=2048,
        )
        shutil.copytree(root / "hosted-app", root / "deleted-app")
        # Deletion keeps the row and retires the slug with a tombstone.
        with self.connection:
            self.connection.execute(
                "INSERT INTO application_slug_tombstones VALUES (?, ?, ?)",
                ("deleted-app", deleted, "2026-01-02T03:04:05Z"),
            )
        keys.write_source_key_archive(self.connection, root, self.root / "keys.tar")
        with tarfile.open(self.root / "keys.tar") as archive:
            self.assertEqual(
                set(archive.getnames()), {"hosted-app/id_ed25519", "hosted-app/id_ed25519.pub"}
            )

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


class HostedControllerRetentionTests(HostedBackupFixture):
    """Daily sets age out after two weeks; the newest few always survive."""

    def _backup_at(self, days_ago: float, *, source_keys: Path | None = None) -> str:
        name, _ = backup_hosted_database(
            self.connection,
            self.backups,
            age_recipient="age1testrecipient",
            age_command=str(self._age()),
            created_at=NOW - timedelta(days=days_ago),
            source_keys_root=source_keys,
        )
        return name

    def _committed(self, name: str, **manifest: str) -> str:
        """A committed fixture set; the fake encryption only adds the header."""
        self.backups.mkdir(mode=0o750, exist_ok=True)
        payload = b"age-encryption.org/v1\nfixture " + name.encode()
        digest = hashlib.sha256(payload).hexdigest()
        evidence = json.dumps({"name": name, "sha256": digest, **manifest}).encode()
        for suffix, content in (
            ("", payload),
            (".sha256", f"{digest}  {name}\n".encode()),
            (".manifest", evidence),
        ):
            (self.backups / (name + suffix)).write_bytes(content)
            (self.backups / (name + suffix)).chmod(0o640)
        return name

    def _prune(self, keep: str, **policy: int) -> backup_retention.RetentionResult:
        return backup_retention.prune(
            self.backups,
            series="hosted-controller",
            keep=keep,
            now=NOW,
            key_series="hosted-controller-source-keys",
            **policy,
        )

    def _names(self) -> set[str]:
        return {path.name for path in self.backups.iterdir()}

    @staticmethod
    def _trio(name: str) -> set[str]:
        return {name + suffix for suffix in SUFFIXES}

    @staticmethod
    def _key_of(name: str) -> str:
        return name.replace("hosted-controller-", "hosted-controller-source-keys-").replace(
            ".sqlite3.age", ".tar.age"
        )

    def test_two_weeks_of_sets_and_their_key_archives_are_kept(self) -> None:
        source_keys = self._keys()
        days = (40, 20, 15, 13.9, 12, 1, 0)
        names = [self._backup_at(age, source_keys=source_keys) for age in days]
        result = self._prune(names[-1])
        # Four sets are inside the 14 days, more than the floor of three.
        kept = names[3:]
        self.assertEqual(
            self._names(),
            {".staging"}
            | {item for name in kept for item in self._trio(name) | self._trio(self._key_of(name))},
        )
        self.assertEqual((result.removed, result.kept, result.ignored), (6, 8, ()))

    def test_newest_sets_survive_any_age_and_the_new_set_always_survives(self) -> None:
        old = [
            self._committed(f"hosted-controller-{stamp(days)}.sqlite3.age") for days in (90, 80, 70)
        ]
        new = self._backup_at(0)
        self.assertEqual(self._prune(new).removed, 1)
        self.assertEqual(
            self._names(), {".staging", *self._trio(new), *self._trio(old[1]), *self._trio(old[2])}
        )
        self.assertEqual(self._prune(new, days=75, minimum=1).removed, 1)
        self.assertEqual(self._names(), {".staging", *self._trio(new), *self._trio(old[2])})
        result = self._prune(new, days=0, minimum=1)
        self.assertEqual((result.removed, result.kept), (1, 1))
        self.assertEqual(self._names(), {".staging", *self._trio(new)})

    def test_retention_refuses_to_run_without_the_new_committed_set(self) -> None:
        old = self._committed(f"hosted-controller-{stamp(90)}.sqlite3.age")
        missing = f"hosted-controller-{stamp(0)}.sqlite3.age"
        (self.backups / missing).write_bytes(b"age-encryption.org/v1\nuncommitted")
        with self.assertRaises(backup_retention.RetentionError) as raised:
            self._prune(missing, days=0, minimum=1)
        self.assertEqual(raised.exception.reason, "new-backup-not-committed")
        self.assertTrue(self._trio(old) <= self._names())

    def test_referenced_key_archives_stay_and_old_orphans_go(self) -> None:
        new = self._backup_at(0, source_keys=self._keys())
        # An older backup may name an archive from another run; it pins it.
        referenced = self._committed(f"hosted-controller-source-keys-{stamp(60)}.tar.age")
        older = self._committed(f"hosted-controller-{stamp(50)}.sqlite3.age", sourceKeys=referenced)
        orphan = self._committed(f"hosted-controller-source-keys-{stamp(20)}.tar.age")
        recent = self._committed(f"hosted-controller-source-keys-{stamp(2)}.tar.age")
        # A crash between the two commits leaves an uncommitted key archive.
        crashed = f"hosted-controller-source-keys-{stamp(30)}.tar.age"
        (self.backups / crashed).write_bytes(b"age-encryption.org/v1\n")
        (self.backups / (crashed + ".sha256")).write_bytes(b"0" * 64 + b"  x\n")
        result = self._prune(new)
        self.assertEqual(
            self._names(),
            {
                ".staging",
                *self._trio(new),
                *self._trio(self._key_of(new)),
                *self._trio(older),
                *self._trio(referenced),
                *self._trio(recent),
            },
        )
        self.assertEqual((result.removed, result.kept), (2, 5))
        self.assertFalse(self._trio(orphan) & self._names())

        # Once its backup expires, the referenced archive is an old orphan too.
        self.assertEqual(self._prune(new, minimum=1).removed, 2)
        self.assertEqual(
            self._names(),
            {".staging", *self._trio(new), *self._trio(self._key_of(new)), *self._trio(recent)},
        )

    def test_unsafe_unexpected_and_incomplete_entries_are_left_alone(self) -> None:
        new = self._backup_at(0)
        expired = self._committed(f"hosted-controller-{stamp(60)}.sqlite3.age")
        # Uncommitted leftovers past the cutoff go; recent ones may be in use.
        leftover = f"hosted-controller-{stamp(30)}.sqlite3.age"
        (self.backups / leftover).write_bytes(b"age-encryption.org/v1\n")
        (self.backups / (leftover + ".sha256")).write_bytes(b"0" * 64 + b"  x\n")
        fresh = f"hosted-controller-{stamp(1)}.sqlite3.age"
        (self.backups / fresh).write_bytes(b"age-encryption.org/v1\n")
        # A committed set missing evidence is for an operator to look at.
        broken = self._committed(f"hosted-controller-{stamp(40)}.sqlite3.age")
        (self.backups / broken).unlink()
        # A link with a backup name pins its whole set, and its target is untouched.
        target = self.root / "outside"
        target.write_text("protected\n")
        linked = self._committed(f"hosted-controller-{stamp(50)}.sqlite3.age")
        (self.backups / (linked + ".manifest")).unlink()
        (self.backups / (linked + ".manifest")).symlink_to(target)
        (self.backups / "notes.txt").write_text("operator note\n")
        (self.backups / "hosted-controller-20250101T000000Z.sqlite3.age.d").mkdir()
        (self.backups / ".staging" / f".{expired}.stale.tmp").write_bytes(b"debris")

        result = self._prune(new, minimum=1)
        self.assertEqual(
            self._names(),
            {
                ".staging",
                "notes.txt",
                "hosted-controller-20250101T000000Z.sqlite3.age.d",
                *self._trio(new),
                fresh,
                broken + ".sha256",
                broken + ".manifest",
                *self._trio(linked),
            },
        )
        self.assertEqual((result.removed, result.kept), (2, 1))
        self.assertEqual(target.read_text(), "protected\n")
        self.assertTrue((self.backups / ".staging" / f".{expired}.stale.tmp").exists())
        reasons = "\n".join(result.ignored)
        self.assertIn("'notes.txt': unexpected entry", reasons)
        self.assertIn(f"'{linked}.manifest': not a direct current-user-owned file", reasons)
        self.assertIn(f"'{broken}': committed set is incomplete", reasons)

    def test_offsite_export_still_selects_the_newest_set_after_retention(self) -> None:
        source_keys = self._keys()
        names = [self._backup_at(days, source_keys=source_keys) for days in (30, 20, 0)]
        self._prune(names[-1], minimum=1)
        selected = recovery_bundle._selected_component_files("hosted-controller", self.backups)
        self.assertEqual(
            {path.name for path in selected},
            self._trio(names[-1]) | self._trio(self._key_of(names[-1])),
        )

    def _main(self, now: datetime) -> tuple[int, str, str]:
        configuration = SimpleNamespace(
            platform=None, policy=SimpleNamespace(backup_age_recipient="age1testrecipient")
        )
        connect = db.connect
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(hosted_backup, "datetime", mock.Mock(now=lambda _zone: now)),
            mock.patch.object(hosted_backup, "load", return_value=configuration),
            mock.patch.object(db, "deployment_identity", return_value=None),
            mock.patch.object(db, "connect", side_effect=lambda path, **_: connect(path)),
            mock.patch.object(db, "migrate"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = hosted_backup.main(
                [
                    "--platform-config=platform.json",
                    "--policy=policy.json",
                    f"--state-directory={self.state}",
                    f"--backup-root={self.backups}",
                    f"--age-command={self._age()}",
                    f"--source-keys-root={self._keys()}",
                ]
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def test_command_prunes_after_commit_and_a_failure_keeps_the_new_set(self) -> None:
        for days in (90, 80, 70):
            self._committed(f"hosted-controller-{stamp(days)}.sqlite3.age")
        status, stdout, _stderr = self._main(NOW)
        self.assertEqual(status, 0)
        self.assertIn(f"hosted-controller-backup=hosted-controller-{stamp(0)}.sqlite3.age", stdout)
        self.assertIn("retention=ok removed=1 kept=4", stdout)

        shutil.rmtree(self.root / "source-keys")
        later = NOW + timedelta(hours=1)
        with mock.patch.object(
            backup_retention, "_remove", side_effect=PermissionError(13, "denied")
        ):
            status, stdout, stderr = self._main(later)
        self.assertEqual(status, 1)
        name = f"hosted-controller-{later.strftime('%Y%m%dT%H%M%SZ')}.sqlite3.age"
        self.assertIn(f"hosted-controller-backup={name}", stdout)
        self.assertIn("retention=failed reason=filesystem-EACCES", stderr)
        self.assertTrue(self._trio(name) <= self._names())


if __name__ == "__main__":
    unittest.main()
