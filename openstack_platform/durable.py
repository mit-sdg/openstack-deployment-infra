"""Small crash-durable replacement primitives for trusted local state files."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path


class DurableReplaceError(RuntimeError):
    """A file could not be replaced without weakening local-state checks."""


FaultHook = Callable[[str], None]


def _directory_fd(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise DurableReplaceError("replacement directory could not be opened safely") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise DurableReplaceError("replacement directory must be direct and current-user-owned")
    return descriptor


def validate_file(path: Path, *, mode: int, maximum_bytes: int | None = None) -> os.stat_result:
    """Validate a direct regular file by descriptor, including owner and exact mode."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise DurableReplaceError("replacement file could not be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
            or (maximum_bytes is not None and metadata.st_size > maximum_bytes)
        ):
            raise DurableReplaceError(
                "replacement file has an unexpected type, owner, mode, or size"
            )
        return metadata
    finally:
        os.close(descriptor)


def _validate_destination(path: Path, *, mode: int, maximum_bytes: int | None) -> None:
    if os.path.lexists(path):
        validate_file(path, mode=mode, maximum_bytes=maximum_bytes)


def _remove_stale(path: Path, *, mode: int, directory_fd: int) -> None:
    if not os.path.lexists(path):
        return
    validate_file(path, mode=mode)
    try:
        path.unlink()
        os.fsync(directory_fd)
    except OSError as error:
        raise DurableReplaceError("stale replacement file could not be removed durably") from error


def atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    maximum_bytes: int,
    fault: FaultHook | None = None,
) -> None:
    """Write and durably rename a bounded file, retrying one deterministic temp path.

    ``fault`` exists only for deterministic interruption tests. Its stages are
    ``before_write``, ``after_write``, ``after_file_fsync``, ``after_rename``,
    and ``after_directory_fsync``.
    """
    if len(payload) > maximum_bytes:
        raise DurableReplaceError("replacement payload exceeds its configured size limit")
    hook = fault or (lambda _stage: None)
    temporary = path.with_name(f".{path.name}.tmp")
    directory_fd = _directory_fd(path.parent)
    renamed = False
    descriptor = -1
    try:
        _validate_destination(path, mode=mode, maximum_bytes=maximum_bytes)
        _remove_stale(temporary, mode=mode, directory_fd=directory_fd)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
        )
        os.fchmod(descriptor, mode)
        hook("before_write")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise DurableReplaceError("replacement write made no progress")
            view = view[written:]
        hook("after_write")
        os.fsync(descriptor)
        hook("after_file_fsync")
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size != len(payload)
        ):
            raise DurableReplaceError("replacement temporary file changed before rename")
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        renamed = True
        hook("after_rename")
        os.fsync(directory_fd)
        hook("after_directory_fsync")
    except DurableReplaceError:
        raise
    except OSError as error:
        raise DurableReplaceError("crash-durable file replacement failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not renamed and os.path.lexists(temporary):
            # Leave an unexpected object for explicit attention; remove only
            # the exact private regular temporary file created by this path.
            try:
                validate_file(temporary, mode=mode)
                temporary.unlink()
                os.fsync(directory_fd)
            except (OSError, DurableReplaceError):
                pass
        os.close(directory_fd)


def commit_prepared(
    source: Path,
    destination: Path,
    *,
    mode: int,
    maximum_bytes: int,
    fault: FaultHook | None = None,
) -> None:
    """Fsync and rename a prepared same-directory file over a validated target."""
    if source.parent != destination.parent:
        raise DurableReplaceError("prepared replacement must share the destination directory")
    hook = fault or (lambda _stage: None)
    directory_fd = _directory_fd(destination.parent)
    descriptor = -1
    try:
        _validate_destination(destination, mode=mode, maximum_bytes=maximum_bytes)
        descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > maximum_bytes
        ):
            raise DurableReplaceError(
                "prepared replacement has an unexpected type, owner, mode, or size"
            )
        hook("after_write")
        os.fsync(descriptor)
        hook("after_file_fsync")
        os.close(descriptor)
        descriptor = -1
        os.replace(source, destination)
        hook("after_rename")
        os.fsync(directory_fd)
        hook("after_directory_fsync")
    except DurableReplaceError:
        raise
    except OSError as error:
        raise DurableReplaceError("prepared file could not be committed durably") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
