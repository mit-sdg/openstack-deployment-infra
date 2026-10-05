"""Age-and-count retention for locally committed encrypted SQLite backups.

The hosted-controller and management-broker units each commit one trio per run
(ciphertext, ``.sha256``, then ``.manifest``); the manifest is the commit
marker. Retention keeps every committed set whose name is from the last
``RETENTION_DAYS`` days, and always the newest ``MINIMUM_KEPT`` complete sets,
so off-site export always finds a newest committed set however old it is.

Only direct, current-user-owned regular files whose names match a backup
pattern are removed, through the root's directory handle. A removal unlinks
the manifest first, so an interrupted removal leaves uncommitted debris that
can never be mistaken for a backup. ``.staging`` is never entered.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

RETENTION_DAYS = 14
MINIMUM_KEPT = 3
# Commit order; removal walks it backwards.
_SUFFIXES = ("", ".sha256", ".manifest")
_EXPECTED = frozenset({".staging"})
_MAXIMUM_MANIFEST_BYTES = 65_536
_LOGGED_IGNORED = 20


class RetentionError(RuntimeError):
    """Retention stopped early; ``reason`` is a short log token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RetentionResult:
    removed: int
    kept: int
    ignored: tuple[str, ...]


@dataclass(slots=True)
class _Set:
    name: str
    created: datetime
    present: set[str] = field(default_factory=set)
    unsafe: bool = False
    manifest: dict[str, object] | None = None

    @property
    def committed(self) -> bool:
        return ".manifest" in self.present

    @property
    def complete(self) -> bool:
        return (
            not self.unsafe
            and self.present == set(_SUFFIXES)
            and self.manifest is not None
            and self.manifest.get("name") == self.name
        )


def _pattern(series: str, extension: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?P<name>{re.escape(series)}-(?P<timestamp>[0-9]{{8}}T[0-9]{{6}}Z)"
        rf"{re.escape(extension)})(?P<suffix>\.sha256|\.manifest)?"
    )


def _open_root(root: Path) -> int:
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        raise RetentionError("backup-root-unavailable") from None
    if os.fstat(descriptor).st_uid != os.geteuid():
        os.close(descriptor)
        raise RetentionError("backup-root-not-owned")
    return descriptor


def _read_manifest(directory: int, name: str) -> dict[str, object] | None:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAXIMUM_MANIFEST_BYTES:
            return None
        payload = os.read(descriptor, _MAXIMUM_MANIFEST_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _scan(
    directory: int,
    entries: list[str],
    patterns: list[re.Pattern[str]],
    ignored: list[str],
) -> list[dict[str, _Set]]:
    found: list[dict[str, _Set]] = [{} for _ in patterns]
    for entry in sorted(entries):
        if entry in _EXPECTED:
            continue
        matches = [
            (sets, match)
            for sets, p in zip(found, patterns, strict=True)
            if (match := p.fullmatch(entry))
        ]
        if not matches:
            ignored.append(f"{entry!r:.120}: unexpected entry")
            continue
        sets, match = matches[0]
        try:
            created = datetime.strptime(match["timestamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            ignored.append(f"{entry!r:.120}: invalid timestamp")
            continue
        try:
            metadata = os.stat(entry, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        backup = sets.setdefault(match["name"], _Set(match["name"], created))
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            # One unsafe member pins the whole set: it is neither removed nor
            # counted, and an unsafe manifest still reads as committed.
            backup.unsafe = True
            ignored.append(f"{entry!r:.120}: not a direct current-user-owned file")
        backup.present.add(match["suffix"] or "")
    for sets in found:
        for backup in sets.values():
            if backup.committed and not backup.unsafe:
                backup.manifest = _read_manifest(directory, backup.name + ".manifest")
                if not backup.complete:
                    ignored.append(f"{backup.name!r:.120}: committed set is incomplete")
    return found


def _remove(directory: int, backup: _Set) -> None:
    for suffix in reversed(_SUFFIXES):
        if suffix in backup.present:
            try:
                os.unlink(backup.name + suffix, dir_fd=directory)
            except FileNotFoundError:
                pass
            if suffix == ".manifest":
                # The set is uncommitted before any of its evidence goes.
                os.fsync(directory)
    backup.present.clear()


def prune(
    root: Path,
    *,
    series: str,
    keep: str,
    now: datetime,
    key_series: str | None = None,
    days: int = RETENTION_DAYS,
    minimum: int = MINIMUM_KEPT,
) -> RetentionResult:
    """Remove committed sets past the policy and old uncommitted leftovers.

    ``keep`` names the set just written; retention refuses to run unless it is
    complete. With ``key_series`` (hosted controller), deploy-key archive sets
    go once no remaining SQLite set references or shares a timestamp with them
    and they are older than the cutoff, which also clears crash orphans.
    """
    if days < 0 or minimum < 1 or now.tzinfo is None:
        raise ValueError("retention needs a non-negative age, a positive floor and aware time")
    cutoff = now - timedelta(days=days)
    ignored: list[str] = []
    directory = _open_root(root)
    try:
        patterns = [_pattern(series, ".sqlite3.age")]
        if key_series is not None:
            patterns.append(_pattern(key_series, ".tar.age"))
        backups, *rest = _scan(directory, os.listdir(directory), patterns, ignored)
        keys = rest[0] if rest else {}
        newest = sorted(
            (backup for backup in backups.values() if backup.complete),
            key=lambda backup: backup.name,
            reverse=True,
        )
        if keep not in {backup.name for backup in newest}:
            raise RetentionError("new-backup-not-committed")
        floor = {backup.name for backup in newest[:minimum]} | {keep}
        removed = 0
        for backup in sorted(backups.values(), key=lambda backup: backup.name):
            if backup.unsafe or backup.name in floor or backup.created >= cutoff:
                continue
            # Complete expired sets, and leftovers that never committed. A
            # committed but incomplete set waits for an operator.
            if backup.complete or not backup.committed:
                _remove(directory, backup)
                removed += 1
        if key_series is not None:
            remaining = [backup for backup in backups.values() if backup.present]
            referenced = {
                backup.manifest.get("sourceKeys")
                for backup in remaining
                if backup.manifest is not None
            }
            paired = {backup.created for backup in remaining}
            for archive in sorted(keys.values(), key=lambda archive: archive.name):
                if (
                    archive.unsafe
                    or archive.name in referenced
                    or archive.created in paired
                    or archive.created >= cutoff
                ):
                    continue
                if archive.complete or not archive.committed:
                    _remove(directory, archive)
                    removed += 1
        os.fsync(directory)
        kept = sum(backup.complete for sets in (backups, keys) for backup in sets.values())
        return RetentionResult(removed=removed, kept=kept, ignored=tuple(ignored))
    finally:
        os.close(directory)


def prune_after_commit(
    root: Path,
    *,
    series: str,
    keep: str,
    now: datetime,
    key_series: str | None = None,
) -> bool:
    """Apply the default policy and report it; ``False`` asks for a failing exit.

    The new backup is already committed and stays so whatever happens here.
    """
    try:
        result = prune(root, series=series, keep=keep, now=now, key_series=key_series)
    except RetentionError as error:
        print(f"retention=failed reason={error.reason}", file=sys.stderr)
        return False
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "unknown")
        print(f"retention=failed reason=filesystem-{code}", file=sys.stderr)
        return False
    for line in result.ignored[:_LOGGED_IGNORED]:
        print(f"retention ignored {line}", file=sys.stderr)
    if len(result.ignored) > _LOGGED_IGNORED:
        print(
            f"retention ignored {len(result.ignored) - _LOGGED_IGNORED} more entries",
            file=sys.stderr,
        )
    print(f"retention=ok removed={result.removed} kept={result.kept}")
    return True
