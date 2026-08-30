"""Offline, integrity-checked replacement of the controller database."""

from __future__ import annotations

import argparse
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from . import runtime
from .config import load_platform
from .controller import database as db
from .installation import OPERATOR_BIN
from .validation import ValidationError

_MAX_BACKUP_BYTES = 1_073_741_824
_AGE_HEADER = b"age-encryption.org/v1\n"
_SQLITE_HEADER = b"SQLite format 3\x00"
_AGE_CANDIDATES = (
    OPERATOR_BIN / "age",
    Path("/usr/bin/age"),
    Path("/bin/age"),
)


class RestoreError(RuntimeError):
    """A restore was refused or could not be verified safely."""


@dataclass(frozen=True, slots=True)
class RestoreResult:
    destination: Path
    schema_version: int
    integrity: str


def _fail(message: str) -> NoReturn:
    raise RestoreError(message)


def _direct_private_file(
    path: Path,
    *,
    label: str,
    maximum_bytes: int | None = None,
    require_nonempty: bool = True,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail(f"{label} was not found")
    except OSError:
        _fail(f"{label} is unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        _fail(f"{label} must be a direct current-user-owned mode-0600 file")
    if require_nonempty and metadata.st_size == 0:
        _fail(f"{label} is empty")
    if maximum_bytes is not None and metadata.st_size > maximum_bytes:
        _fail(f"{label} exceeds its configured size limit")
    return metadata


def _private_state_directory(path: Path) -> None:
    try:
        runtime.ensure_private_directory(path, create=True)
    except (OSError, runtime.RuntimeFailure) as error:
        raise RestoreError(
            "restore state directory must be a direct current-user mode-0700 directory"
        ) from error


def _sidecar_paths(destination: Path) -> tuple[Path, Path]:
    return destination.with_name(destination.name + "-wal"), destination.with_name(
        destination.name + "-shm"
    )


def _validate_sidecars(destination: Path) -> tuple[Path, ...]:
    sidecars = tuple(path for path in _sidecar_paths(destination) if os.path.lexists(path))
    for sidecar in sidecars:
        _direct_private_file(
            sidecar,
            label="SQLite WAL/SHM sidecar",
            maximum_bytes=_MAX_BACKUP_BYTES,
            require_nonempty=False,
        )
    return sidecars


def _remove_sidecars(destination: Path) -> None:
    for sidecar in _validate_sidecars(destination):
        sidecar.unlink()


def _copy_file(source: Path, destination: Path, *, maximum_bytes: int) -> None:
    _direct_private_file(source, label="SQLite backup", maximum_bytes=maximum_bytes)
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            copied = 0
            while chunk := os.read(source_descriptor, 1_048_576):
                copied += len(chunk)
                if copied > maximum_bytes:
                    _fail("SQLite backup exceeds its configured size limit")
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(source_descriptor)
    finally:
        os.close(descriptor)
    os.chmod(destination, 0o600)


def _age_executable() -> str:
    for candidate in _AGE_CANDIDATES:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid in {0, os.geteuid()}
            and metadata.st_mode & 0o111
            and not metadata.st_mode & 0o022
        ):
            return str(resolved)
    _fail("age is unavailable; provision the pinned or system-managed age executable first")


def _looks_like_age(path: Path) -> bool:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        return os.read(descriptor, len(_AGE_HEADER)) == _AGE_HEADER
    finally:
        os.close(descriptor)


def _materialize_backup(
    backup: Path,
    destination: Path,
    *,
    age_identity: Path | None,
    age_command: str | None,
    maximum_bytes: int,
) -> Path:
    metadata = _direct_private_file(
        backup,
        label="SQLite backup",
        maximum_bytes=maximum_bytes,
    )
    if os.path.lexists(destination):
        _direct_private_file(
            destination,
            label="restore temporary file",
            maximum_bytes=maximum_bytes,
            require_nonempty=False,
        )
        destination.unlink()
    try:
        encrypted = _looks_like_age(backup)
    except OSError as error:
        raise RestoreError("SQLite backup could not be read safely") from error
    if not encrypted and metadata.st_size < len(_SQLITE_HEADER):
        _fail("SQLite backup is too small to be a controller database")
    if not encrypted:
        descriptor = os.open(backup, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            if os.read(descriptor, len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                _fail("SQLite backup is neither age-v1 ciphertext nor a SQLite database")
        finally:
            os.close(descriptor)
        _copy_file(backup, destination, maximum_bytes=maximum_bytes)
        return destination

    if age_identity is None:
        _fail("encrypted SQLite backup requires a private age identity file")
    _direct_private_file(age_identity, label="age identity", maximum_bytes=65_536)
    executable = age_command or _age_executable()
    try:
        completed = runtime.run(
            (
                executable,
                "--decrypt",
                "--identity",
                str(age_identity),
                "--output",
                str(destination),
                str(backup),
            ),
            timeout_seconds=900,
            stdout_limit=65_536,
            stderr_limit=262_144,
        )
    except runtime.RuntimeFailure as error:
        raise RestoreError("age could not decrypt the SQLite backup") from error
    if completed.stdout_truncated or completed.stderr_truncated:
        _fail("age decryption exceeded its bounded output limit")
    try:
        metadata = destination.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            _fail("decrypted SQLite backup must be a direct current-user-owned file")
        os.chmod(destination, 0o600)
        _direct_private_file(
            destination, label="decrypted SQLite backup", maximum_bytes=maximum_bytes
        )
    except (OSError, RestoreError):
        destination.unlink(missing_ok=True)
        raise
    return destination


def _unfinished_operations(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT 1 FROM operations WHERE status IN ('running', 'recovery_required') LIMIT 1"
        ).fetchone()
        if row is not None:
            return True
        dispatch_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_dispatches'"
        ).fetchone()
        if dispatch_table is None:
            return False
        row = connection.execute(
            "SELECT 1 FROM operation_dispatches "
            "WHERE status IN ('pending', 'running', 'recovery_required') LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _verify_candidate(path: Path, *, identity: db.DeploymentIdentity | None = None) -> int:
    try:
        connection = db.connect(path, create=False, identity=identity)
    except (OSError, db.DatabaseError, sqlite3.Error) as error:
        raise RestoreError("candidate is not a valid private SQLite database") from error
    try:
        try:
            schema_row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
        except sqlite3.Error as error:
            raise RestoreError("candidate schema could not be read") from error
        if schema_row is None:
            _fail("candidate is not a controller SQLite database")
        try:
            db.migrate(connection, identity=identity)
            db.validate_complete_schema(connection, identity=identity)
            integrity = [tuple(row) for row in connection.execute("PRAGMA integrity_check")]
            foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        except (db.DatabaseError, sqlite3.Error) as error:
            raise RestoreError("candidate migrations or integrity verification failed") from error
        if integrity != [("ok",)] or foreign_keys:
            _fail("candidate SQLite integrity verification failed")
        if _unfinished_operations(connection):
            _fail(
                "restore refuses a backup with unfinished operations; resolve the recorded operation before backing it up"
            )
        return db.schema_version(connection)
    finally:
        connection.close()


def _verify_existing_destination(
    path: Path, *, identity: db.DeploymentIdentity | None = None
) -> None:
    if not os.path.lexists(path):
        return
    _direct_private_file(
        path, label="existing controller database", maximum_bytes=_MAX_BACKUP_BYTES
    )
    _validate_sidecars(path)
    if identity is not None:
        try:
            connection = db.connect(path, create=False, identity=identity)
        except (OSError, db.DatabaseError, sqlite3.Error) as error:
            raise RestoreError(
                "existing controller database belongs to a different deployment identity"
            ) from error
    else:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise RestoreError(
                "existing controller database could not be opened offline"
            ) from error
    try:
        try:
            unfinished = _unfinished_operations(connection)
        except sqlite3.DatabaseError as error:
            raise RestoreError(
                "existing controller database could not be inspected for unfinished operations"
            ) from error
        if unfinished:
            _fail(
                "restore refuses while the current database has unfinished operations; recover or complete that operation first"
            )
    finally:
        connection.close()


def restore_database(
    backup: str | Path,
    destination: str | Path,
    *,
    age_identity: str | Path | None = None,
    age_command: str | None = None,
    maximum_bytes: int = _MAX_BACKUP_BYTES,
    identity: db.DeploymentIdentity | None = None,
) -> RestoreResult:
    """Verify an offline controller backup and atomically replace ``destination``.

    The source and destination are private mode-0600 files below a private
    mode-0700 state directory.  Older known schemas are migrated in a
    temporary copy; future schemas, corrupt databases, and unfinished
    operations are refused.  No cloud, helper, or provider API is contacted.
    """
    if not 1 <= maximum_bytes <= _MAX_BACKUP_BYTES:
        raise ValueError("restore size limit must be from 1 through 1073741824 bytes")
    backup_path = Path(backup)
    destination_path = Path(destination)
    if not destination_path.is_absolute():
        _fail("restore destination must be an absolute path")
    _private_state_directory(destination_path.parent)
    if backup_path.absolute() == destination_path.absolute():
        _fail("restore source and destination must be different files")
    _verify_existing_destination(destination_path, identity=identity)
    old_sidecars = _validate_sidecars(destination_path)
    if old_sidecars and not os.path.lexists(destination_path):
        _fail("SQLite WAL/SHM sidecars exist without a controller database")

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.restore-",
            suffix=".tmp",
            dir=destination_path.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, 0o600)
        _materialize_backup(
            backup_path,
            temporary_path,
            age_identity=None if age_identity is None else Path(age_identity),
            age_command=age_command,
            maximum_bytes=maximum_bytes,
        )
        schema_version = _verify_candidate(temporary_path, identity=identity)
        _direct_private_file(
            temporary_path,
            label="verified restore candidate",
            maximum_bytes=maximum_bytes,
        )
        _remove_sidecars(temporary_path)
        os.replace(temporary_path, destination_path)
        temporary_path = None
        for sidecar in old_sidecars:
            sidecar.unlink(missing_ok=True)
        directory_descriptor = os.open(
            destination_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return RestoreResult(destination_path, schema_version, "ok")
    except RestoreError:
        raise
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        raise RestoreError("offline SQLite restore failed before atomic replacement") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
            for sidecar in _sidecar_paths(temporary_path):
                sidecar.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify and atomically restore an offline controller SQLite backup",
        allow_abbrev=False,
    )
    parser.add_argument("backup", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--platform-config", type=Path, required=True)
    parser.add_argument("--age-identity", type=Path)
    parser.add_argument(
        "--yes", action="store_true", help="confirm replacement of the destination database"
    )
    args = parser.parse_args(argv)
    if not args.yes:
        parser.error("--yes is required because restore replaces the destination database")
    try:
        identity = db.deployment_identity(load_platform(args.platform_config))
        with runtime.lock(args.destination.parent, "database-maintenance", wait=False):
            result = restore_database(
                args.backup,
                args.destination,
                age_identity=args.age_identity,
                identity=identity,
            )
    except (RestoreError, ValidationError, FileNotFoundError, runtime.RuntimeFailure) as error:
        print(f"offline restore failed: {error}", file=sys.stderr)
        return 1
    print(f"restore=verified schema-version={result.schema_version} integrity={result.integrity}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RestoreError as error:
        print(f"offline restore failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
