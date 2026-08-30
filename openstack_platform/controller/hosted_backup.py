"""Encrypted, locally committed backups of the admin-hosted controller database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from .. import runtime
from ..config import load
from ..validation import ValidationError
from . import database as db

_AGE_HEADER = b"age-encryption.org/v1\n"


class HostedBackupError(RuntimeError):
    """A hosted-controller backup could not be safely committed."""


def _fail(message: str) -> NoReturn:
    raise HostedBackupError(message)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_directory(path: Path, *, mode: int) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        metadata = path.lstat()
    except OSError as error:
        raise HostedBackupError("hosted-controller backup directory is unavailable") from error
    if path.is_symlink() or not path.is_dir() or metadata.st_uid != os.geteuid():
        _fail("hosted-controller backup directory must be direct and current-user-owned")
    actual_mode = metadata.st_mode & 0o7777
    if actual_mode & 0o007 or actual_mode & 0o020:
        _fail("hosted-controller backup directory must not be writable by group or other")
    return path


def _write_file(path: Path, content: bytes, *, mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        view = memoryview(content)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def backup_hosted_database(
    connection: sqlite3.Connection,
    backup_root: str | Path,
    *,
    age_recipient: str,
    age_command: str,
    created_at: datetime | None = None,
) -> tuple[str, str]:
    """Create an online SQLite backup, encrypt it, and commit an evidence trio.

    The final manifest is the commit marker. Plaintext exists only in a private
    temporary directory and is removed on success or failure.
    """
    if not age_recipient.startswith("age1") or any(
        character.isspace() for character in age_recipient
    ):
        _fail("hosted-controller backup requires a valid public age recipient")
    root = _private_directory(Path(backup_root), mode=0o750)
    staging = _private_directory(root / ".staging", mode=0o750)
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"hosted-controller-{timestamp}.sqlite3.age"
    final_ciphertext = root / name
    final_checksum = root / f"{name}.sha256"
    final_manifest = root / f"{name}.manifest"
    if any(os.path.lexists(path) for path in (final_ciphertext, final_checksum, final_manifest)):
        _fail("hosted-controller backup name already exists")

    database_rows = connection.execute("PRAGMA database_list").fetchall()
    database_name = next((row[2] for row in database_rows if row[1] == "main"), "")
    if not database_name:
        _fail("hosted controller database has no direct filesystem path")
    work_root = _private_directory(Path(database_name).parent / "backup-work", mode=0o700)
    plaintext: Path | None = None
    staged_ciphertext: Path | None = None
    staged_checksum: Path | None = None
    staged_manifest: Path | None = None
    try:
        descriptor, plaintext_name = tempfile.mkstemp(
            prefix=".hosted-backup-", suffix=".sqlite3", dir=work_root
        )
        os.close(descriptor)
        plaintext = Path(plaintext_name)
        plaintext.unlink()
        db.backup_database(connection, plaintext)

        descriptor, ciphertext_name = tempfile.mkstemp(
            prefix=f".{name}.", suffix=".tmp", dir=staging
        )
        os.close(descriptor)
        staged_ciphertext = Path(ciphertext_name)
        staged_ciphertext.unlink()
        completed = runtime.run(
            (age_command, "-r", age_recipient, "-o", str(staged_ciphertext), str(plaintext)),
            timeout_seconds=900,
            stdout_limit=65_536,
            stderr_limit=262_144,
        )
        if completed.stdout_truncated or completed.stderr_truncated:
            _fail("age encryption exceeded its bounded output limit")
        try:
            with staged_ciphertext.open("rb") as handle:
                if handle.read(len(_AGE_HEADER)) != _AGE_HEADER:
                    _fail("age did not produce age-v1 ciphertext")
        except OSError as error:
            raise HostedBackupError("age did not produce a readable ciphertext") from error
        os.chmod(staged_ciphertext, 0o640)
        with staged_ciphertext.open("rb") as handle:
            os.fsync(handle.fileno())
        digest = _sha256(staged_ciphertext)

        staged_checksum = staging / f".{name}.{os.getpid()}.sha256.tmp"
        staged_manifest = staging / f".{name}.{os.getpid()}.manifest.tmp"
        _write_file(staged_checksum, f"{digest}  {name}\n".encode(), mode=0o640)
        manifest = {
            "format": "openstack-platform-hosted-controller-backup-v1",
            "name": name,
            "sha256": digest,
            "createdAt": timestamp,
        }
        _write_file(
            staged_manifest,
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            mode=0o640,
        )
        _fsync_directory(staging)
        os.replace(staged_ciphertext, final_ciphertext)
        staged_ciphertext = None
        os.replace(staged_checksum, final_checksum)
        staged_checksum = None
        _fsync_directory(root)
        os.replace(staged_manifest, final_manifest)
        staged_manifest = None
        _fsync_directory(root)
        return name, digest
    except (OSError, db.DatabaseError, runtime.RuntimeFailure) as error:
        raise HostedBackupError("hosted-controller backup failed before commit") from error
    finally:
        for path in (plaintext, staged_ciphertext, staged_checksum, staged_manifest):
            if path is not None:
                path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the admin-hosted controller database")
    parser.add_argument("--platform-config", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--age-command", required=True)
    args = parser.parse_args(argv)
    try:
        configuration = load(args.platform_config, args.policy)
        identity = db.deployment_identity(configuration.platform)
        with runtime.lock(args.state_directory, "database-maintenance", wait=True):
            connection = db.connect(
                args.state_directory / "platform.sqlite3", create=False, identity=identity
            )
            try:
                db.migrate(connection, identity=identity)
                name, digest = backup_hosted_database(
                    connection,
                    args.backup_root,
                    age_recipient=configuration.policy.backup_age_recipient,
                    age_command=args.age_command,
                )
            finally:
                connection.close()
    except (
        HostedBackupError,
        ValidationError,
        db.DatabaseError,
        runtime.RuntimeFailure,
        OSError,
        sqlite3.Error,
    ) as error:
        print(f"hosted controller backup failed: {error}", file=sys.stderr)
        return 1
    print(f"hosted-controller-backup={name} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
