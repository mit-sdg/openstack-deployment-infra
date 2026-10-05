"""Bounded, no-follow deploy-key archives and offline private restore staging."""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from pathlib import Path

from .. import durable
from ..validation import slug

KEY_NAMES = {"id_ed25519", "id_ed25519.pub"}
MAX_KEY_BYTES = 16_384
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


class SourceKeyBackupError(RuntimeError):
    """Deploy-key evidence has unsafe paths, metadata, or incomplete pairs."""


def _directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise SourceKeyBackupError(
            "deploy-key directory must be direct, private and current-user-owned"
        )


def _read_key(directory: int, name: str) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & (0o022 if name.endswith(".pub") else 0o077)
            or not 0 < metadata.st_size <= MAX_KEY_BYTES
        ):
            raise SourceKeyBackupError("deploy-key file metadata is unsafe")
        payload = os.read(descriptor, MAX_KEY_BYTES + 1)
        observed = os.fstat(descriptor)
        if len(payload) != metadata.st_size or (metadata.st_mtime_ns, metadata.st_size) != (
            observed.st_mtime_ns,
            observed.st_size,
        ):
            raise SourceKeyBackupError("deploy-key file changed during backup")
        return payload, metadata
    finally:
        os.close(descriptor)


def write_source_key_archive(connection: sqlite3.Connection, root: Path, destination: Path) -> None:
    """Include only paired direct files for live apps in this SQLite snapshot.

    Hold one directory handle for both files. Reopen after replacement races;
    .new/.old directories are never enumerated or included. A deleted app keeps
    its row beside a slug tombstone, and its key is never archived again.
    """
    slugs = [
        slug(row[0])
        for row in connection.execute(
            "SELECT application.slug FROM applications AS application "
            "WHERE NOT EXISTS (SELECT 1 FROM application_slug_tombstones AS tombstone "
            "WHERE tombstone.application_id = application.application_id) "
            "ORDER BY application.slug"
        )
    ]
    if root.exists() or root.is_symlink():
        _directory(root)
        root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    else:
        root_descriptor = None
    total = 0
    try:
        archive_descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        with (
            os.fdopen(archive_descriptor, "wb") as stream,
            tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive,
        ):
            for name in slugs if root_descriptor is not None else []:
                pair = None
                for attempt in range(3):
                    descriptor = None
                    try:
                        descriptor = os.open(
                            name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                            dir_fd=root_descriptor,
                        )
                        metadata = os.fstat(descriptor)
                        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                            raise SourceKeyBackupError("deploy-key app directory is unsafe")
                        pair = {key: _read_key(descriptor, key) for key in sorted(KEY_NAMES)}
                        break
                    except FileNotFoundError:
                        if attempt == 2:
                            # No directory means no key; a half pair must fail.
                            if os.path.lexists(root / name):
                                raise SourceKeyBackupError(
                                    "deploy-key pair is incomplete"
                                ) from None
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
                if pair is None:
                    continue
                for key, (payload, metadata) in pair.items():
                    total += len(payload)
                    if total > MAX_ARCHIVE_BYTES // 2:
                        raise SourceKeyBackupError("deploy-key archive exceeds its size bound")
                    member = tarfile.TarInfo(f"{name}/{key}")
                    member.mode = 0o600
                    member.size = len(payload)
                    member.mtime = metadata.st_mtime
                    member.pax_headers = {"platform.mtime_ns": str(metadata.st_mtime_ns)}
                    archive.addfile(member, io.BytesIO(payload))
        destination.chmod(0o600)
        if destination.stat().st_size > MAX_ARCHIVE_BYTES:
            raise SourceKeyBackupError("deploy-key archive exceeds its size bound")
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def prepare_source_key_restore(
    archive_path: Path, connection: sqlite3.Connection, destination: Path
) -> Path:
    """Validate the entire archive into private staging before replacing state."""
    durable.validate_file(archive_path, mode=0o600, maximum_bytes=MAX_ARCHIVE_BYTES)
    _directory(destination.parent)
    if os.path.lexists(destination):
        _directory(destination)
    allowed = {slug(row[0]) for row in connection.execute("SELECT slug FROM applications")}
    staging = Path(tempfile.mkdtemp(prefix=".source-keys-restore-", dir=destination.parent))
    seen: dict[str, set[str]] = {}
    total = 0
    try:
        descriptor = os.open(archive_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        with (
            os.fdopen(descriptor, "rb") as stream,
            tarfile.open(fileobj=stream, mode="r:") as archive,
        ):
            for member in archive:
                parts = member.name.split("/")
                if (
                    len(parts) != 2
                    or parts[0] not in allowed
                    or parts[1] not in KEY_NAMES
                    or not member.isfile()
                    or not 0 < member.size <= MAX_KEY_BYTES
                ):
                    raise SourceKeyBackupError("deploy-key archive contains an invalid entry")
                name, key = parts
                if key in seen.setdefault(name, set()):
                    raise SourceKeyBackupError("deploy-key archive contains duplicate entries")
                seen[name].add(key)
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise SourceKeyBackupError("deploy-key archive exceeds its size bound")
                directory = staging / name
                directory.mkdir(mode=0o700, exist_ok=True)
                payload = archive.extractfile(member)
                if payload is None:
                    raise SourceKeyBackupError("deploy-key archive entry is unreadable")
                with payload:
                    value = payload.read(MAX_KEY_BYTES + 1)
                if len(value) != member.size:
                    raise SourceKeyBackupError("deploy-key archive entry is truncated")
                durable.atomic_write(
                    directory / key, value, mode=0o600, maximum_bytes=MAX_KEY_BYTES
                )
                try:
                    mtime = int(
                        member.pax_headers.get(
                            "platform.mtime_ns", str(int(member.mtime * 1_000_000_000))
                        )
                    )
                except ValueError:
                    raise SourceKeyBackupError("deploy-key timestamp is invalid") from None
                if not 0 <= mtime <= 2**63 - 1:
                    raise SourceKeyBackupError("deploy-key timestamp is invalid")
                os.utime(directory / key, ns=(mtime, mtime), follow_symlinks=False)
                descriptor = os.open(directory / key, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        if any(keys != KEY_NAMES for keys in seen.values()):
            raise SourceKeyBackupError("deploy-key archive contains an incomplete pair")
        for directory in [*(staging / name for name in seen), staging]:
            descriptor = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return staging
    except (tarfile.TarError, EOFError) as error:
        shutil.rmtree(staging)
        raise SourceKeyBackupError("deploy-key archive is malformed") from error
    except BaseException:
        shutil.rmtree(staging)
        raise


def commit_source_key_restore(staging: Path, destination: Path) -> None:
    """Replace the offline key tree; rollback directory selection on a failed swap."""
    old = staging.with_name(staging.name + ".old")
    existed = os.path.lexists(destination)
    if existed:
        _directory(destination)
        os.replace(destination, old)
    try:
        os.replace(staging, destination)
    except BaseException:
        if existed:
            os.replace(old, destination)
        raise
    descriptor = os.open(
        destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if existed:
        shutil.rmtree(old)
