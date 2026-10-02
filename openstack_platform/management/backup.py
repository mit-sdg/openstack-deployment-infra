"""Consistent encrypted broker backup and explicit offline replacement restore."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .. import durable, runtime
from ..controller.hosted_backup import _fsync_directory, _sha256, _write_file
from ..validation import age_recipient as validate_recipient
from .broker.database import MIGRATION_2, SCHEMA_V1
from .common import digest
from .config import Config

FORMAT = "openstack-platform-management-broker-backup-v1"


def validate_database(
    connection: sqlite3.Connection, identity: str | None = None
) -> tuple[int, str]:
    if (
        connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
        or connection.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise ValueError("broker SQLite integrity or foreign keys failed")
    rows = connection.execute("SELECT version,identity FROM metadata").fetchall()
    if (
        len(rows) != 1
        or rows[0][0] not in (1, 2)
        or (identity is not None and rows[0][1] != identity)
    ):
        raise ValueError("broker schema or identity mismatch")
    migration = connection.execute(
        "SELECT checksum FROM schema_migrations WHERE version=1"
    ).fetchone()
    if migration is None or migration[0] != digest(SCHEMA_V1):
        raise ValueError("broker migration checksum mismatch")
    if rows[0][0] == 2:
        second = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version=2"
        ).fetchone()
        if second is None or second[0] != digest(MIGRATION_2):
            raise ValueError("broker migration checksum mismatch")
    required = {
        "users",
        "sessions",
        "csrf_tokens",
        "apps",
        "configurations",
        "intents",
        "audit",
        "observations",
        "quotas",
    }
    if not required <= {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        raise ValueError("broker schema is incomplete")
    return int(rows[0][0]), str(rows[0][1])


def private_directory(path: Path, mode: int = 0o700) -> None:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    stat = path.lstat()
    if (
        path.is_symlink()
        or not path.is_dir()
        or stat.st_uid != os.geteuid()
        or stat.st_mode & 0o027
    ):
        raise ValueError("backup directory must be direct, owned, and privately writable")


def backup_database(
    database: Path,
    destination: Path,
    recipient: str,
    age: str,
    *,
    created_at: datetime | None = None,
) -> tuple[str, str]:
    validate_recipient(recipient)
    durable.validate_file(database, mode=0o600, maximum_bytes=1024**3)
    private_directory(destination, 0o750)
    private_directory(destination / ".staging", 0o750)
    name = (
        "management-broker-"
        + (created_at or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
        + ".sqlite3.age"
    )
    targets = [
        destination / name,
        destination / (name + ".sha256"),
        destination / (name + ".manifest"),
    ]
    if any(os.path.lexists(path) for path in targets):
        raise ValueError("backup name already exists")
    staging = destination / ".staging"
    with tempfile.TemporaryDirectory(prefix=".broker-backup-", dir=staging) as directory:
        work = Path(directory)
        os.chmod(work, 0o700)
        plaintext = work / "snapshot.sqlite3"
        fd = os.open(plaintext, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        snapshot = sqlite3.connect(plaintext)
        try:
            source.backup(snapshot)
            schema, identity = validate_database(snapshot)
        finally:
            source.close()
            snapshot.close()
        ciphertext = work / name
        runtime.run(
            (age, "-r", recipient, "-o", str(ciphertext), str(plaintext)),
            timeout_seconds=900,
            stdout_limit=1024,
            stderr_limit=1024,
        )
        with ciphertext.open("rb") as stream:
            if stream.read(len(b"age-encryption.org/v1\n")) != b"age-encryption.org/v1\n":
                raise ValueError("encryption did not produce age-v1 ciphertext")
        os.chmod(ciphertext, 0o640)
        checksum = _sha256(ciphertext)
        evidence = {
            "format": FORMAT,
            "name": name,
            "sha256": checksum,
            "createdAt": (created_at or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ"),
            "schemaVersion": schema,
            "identity": identity,
        }
        _write_file(work / (name + ".sha256"), f"{checksum}  {name}\n".encode(), mode=0o640)
        _write_file(
            work / (name + ".manifest"),
            (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            mode=0o640,
        )
        descriptor = os.open(ciphertext, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # The final manifest is the commit marker. Interrupted partial sets are
        # uncommitted and never selected by off-site discovery.
        for target in targets:
            os.replace(work / target.name, target)
            _fsync_directory(destination)
    return name, checksum


def restore_database(source: Path, destination: Path, *, identity: str | None = None) -> None:
    durable.validate_file(source, mode=0o600, maximum_bytes=1024**3)
    private_directory(destination.parent)
    if destination.is_symlink() or source.resolve() == destination.resolve():
        raise ValueError("unsafe restore destination")
    # Operator stops web/broker/backup units before replacing existing state.
    for suffix in ("-wal", "-shm"):
        if os.path.lexists(str(destination) + suffix):
            raise ValueError("restore requires stopped database with no sidecars")
    if destination.exists():
        durable.validate_file(destination, mode=0o600, maximum_bytes=1024**3)
        existing = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
        try:
            identity = validate_database(existing, identity)[1]
        finally:
            existing.close()
    key = destination.parent / "anonymous.key"
    if os.path.lexists(key):
        durable.validate_file(key, mode=0o600, maximum_bytes=32)
    with tempfile.TemporaryDirectory(
        prefix=".broker-restore-", dir=destination.parent
    ) as directory:
        replacement = Path(directory) / "management.sqlite3"
        fd = os.open(replacement, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        restored = sqlite3.connect(replacement)
        try:
            original.backup(restored)
            validate_database(restored, identity)
            restored.execute("PRAGMA foreign_keys=ON")
            with restored:
                restored.execute("DELETE FROM csrf_tokens")
                restored.execute("DELETE FROM sessions")
                if validate_database(restored)[0] == 1:
                    restored.execute("DELETE FROM flows")
                    restored.execute("DELETE FROM replays")
                restored.execute(
                    "UPDATE intents SET lease=0,next_retry=0 WHERE state NOT IN ('succeeded','failed')"
                )
                restored.execute(
                    "INSERT INTO audit(action,created) VALUES('offline_restore_sessions_invalidated',?)",
                    (time.time(),),
                )
            validate_database(restored, identity)
        finally:
            original.close()
            restored.close()
        descriptor = os.open(replacement, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(replacement, destination)
        _fsync_directory(destination.parent)
    # Invalidate anonymous challenges even if the old private directory survives.
    if key.exists():
        key.unlink()
        _fsync_directory(destination.parent)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encrypted broker backup and offline session-invalidating restore"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--recipient-file", type=Path, required=True)
    backup.add_argument("--age", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--yes", action="store_true", required=True)
    restore.add_argument("--config", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("source", type=Path)
    args = parser.parse_args()
    if args.command == "backup":
        if args.recipient_file.is_symlink() or args.recipient_file.stat().st_size > 256:
            raise ValueError("invalid public recipient file")
        name, checksum = backup_database(
            args.database, args.destination, args.recipient_file.read_text().strip(), args.age
        )
        print(f"management-broker-backup={name} sha256={checksum}")
    elif args.command == "restore":
        config = Config.load(args.config) if args.config else None
        identity = digest(config.portal_origin + "\n" + config.issuer) if config else None
        restore_database(args.source, args.destination, identity=identity)
        print("management-broker-restore=verified sessions=invalidated")
    else:
        connection = sqlite3.connect(args.source.as_uri() + "?mode=ro", uri=True)
        try:
            validate_database(connection)
        finally:
            connection.close()
        print("management-broker-backup=verified integrity=ok")


if __name__ == "__main__":
    main()
