"""Root-only activation of one descriptor-matched management release pair.

This file is invoked from the immutable admin image, never from a candidate.
Only standard-library imports are used so the Nix wrapper can reference this
single source file rather than embedding the repository.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

COMPATIBILITY = {
    "brokerProtocolVersion": 2,
    "webProtocolVersion": 2,
    "authProtocolVersion": 2,
    "brokerSchemaVersion": 2,
    "controllerApiVersion": 1,
}


def sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def direct_directory(path: Path, uid: int, gid: int, mode: int) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise ValueError("management activation directory ownership or mode differs")


def read_at(
    parent: int | None, name: str | Path, uid: int, gid: int, mode: int, maximum: int
) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > maximum
        ):
            raise ValueError(
                f"{name}: management activation input differs: uid:gid:mode:nlink={metadata.st_uid}:{metadata.st_gid}:{stat.S_IMODE(metadata.st_mode):04o}:{metadata.st_nlink}"
            )
        raw = stream.read(maximum + 1)
        if len(raw) != metadata.st_size:
            raise ValueError(f"{name}: management activation input changed during reading")
        return raw


def read(path: Path, uid: int, gid: int, mode: int, maximum: int) -> bytes:
    return read_at(None, path, uid, gid, mode, maximum)


@contextmanager
def directory_at(parent: int, name: str, uid: int, gid: int, mode: int) -> Iterator[int]:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        metadata = os.fstat(fd)
        if (
            metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise ValueError(
                f"{name}: management activation directory differs: uid:gid:mode:nlink={metadata.st_uid}:{metadata.st_gid}:{stat.S_IMODE(metadata.st_mode):04o}:{metadata.st_nlink}"
            )
        yield fd
    finally:
        os.close(fd)


def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in items:
        if name in result:
            raise ValueError("duplicate management activation field")
        result[name] = value
    return result


@contextmanager
def state_directory(state: Path) -> Iterator[int]:
    """Root writes active selectors only beneath a root-controlled hierarchy."""
    if not state.is_absolute() or ".." in state.parts:
        raise ValueError("management activation state must be direct and absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in state.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = child
            metadata = os.fstat(fd)
            if metadata.st_uid not in {0, os.geteuid()} or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise ValueError("management activation state ancestors must be root-controlled")
        metadata = os.fstat(fd)
        if (
            metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o755
        ):
            raise ValueError("management activation state must be root-owned 0755")
        yield fd
    finally:
        os.close(fd)


def activate(state: Path, operator_uid: int, groups: dict[str, int]) -> Path:
    """Read each operator-controlled component through a held checked handle."""
    with ExitStack() as stack:
        state_fd = stack.enter_context(state_directory(state))
        broker_fd = stack.enter_context(
            directory_at(
                state_fd, "management-broker-releases", operator_uid, groups["broker"], 0o2750
            )
        )
        lock = os.open(
            ".install.lock",
            os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=broker_fd,
        )
        try:
            metadata = os.fstat(lock)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != operator_uid
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError("management activation lock ownership or mode differs")
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = read_at(
                broker_fd, "activate-request", operator_uid, groups["broker"], 0o640, 128
            )
            lines = marker.decode("ascii").splitlines()
            if (
                len(lines) != 2
                or not re.fullmatch(r"[0-9a-f]{40}", lines[0])
                or not re.fullmatch(r"[0-9a-f]{64}", lines[1])
            ):
                raise ValueError("management activation marker must identify a commit and pair")
            commit, pair = lines
            selected: dict[str, Path] = {}
            platforms: list[bytes] = []
            for mode in ("broker", "web"):
                root = state / f"management-{mode}-releases"
                root_fd = (
                    broker_fd
                    if mode == "broker"
                    else stack.enter_context(
                        directory_at(state_fd, root.name, operator_uid, groups[mode], 0o2750)
                    )
                )
                releases_fd = stack.enter_context(
                    directory_at(root_fd, "releases", operator_uid, groups[mode], 0o2750)
                )
                link = os.stat("current", dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISLNK(link.st_mode) or link.st_uid != operator_uid:
                    raise ValueError("management staged selector is not operator-owned")
                relative = Path(os.readlink("current", dir_fd=root_fd))
                if (
                    relative.is_absolute()
                    or len(relative.parts) != 2
                    or relative.parts[0] != "releases"
                    or ".." in relative.parts
                ):
                    raise ValueError("management staged selector escapes releases")
                release = root / relative
                release_fd = stack.enter_context(
                    directory_at(releases_fd, relative.name, operator_uid, groups[mode], 0o2750)
                )
                evidence_fd = stack.enter_context(
                    directory_at(release_fd, "evidence", operator_uid, groups[mode], 0o2750)
                )
                config_fd = stack.enter_context(
                    directory_at(release_fd, "config", operator_uid, groups[mode], 0o2750)
                )
                bin_fd = stack.enter_context(
                    directory_at(release_fd, "bin", operator_uid, groups[mode], 0o2750)
                )
                if (
                    read_at(release_fd, ".complete", operator_uid, groups[mode], 0o440, 41)
                    != (commit + "\n").encode()
                ):
                    raise ValueError("management staged commit is incomplete or differs")
                descriptor = json.loads(
                    read_at(
                        evidence_fd,
                        "management-artifacts.json",
                        operator_uid,
                        groups[mode],
                        0o440,
                        1_048_576,
                    ),
                    object_pairs_hook=pairs,
                )
                if (
                    not isinstance(descriptor, dict)
                    or descriptor.get("sourceCommit") != commit
                    or descriptor.get("pairIdentity") != pair
                    or descriptor.get("compatibility") != COMPATIBILITY
                ):
                    raise ValueError(
                        "management staged descriptors do not match the requested pair"
                    )
                for service in ("broker", "identity") if mode == "broker" else ("web",):
                    read_at(
                        bin_fd, f"management-{service}", operator_uid, groups[mode], 0o550, 16384
                    )
                platforms.append(
                    read_at(
                        config_fd, "platform.json", operator_uid, groups[mode], 0o440, 1_048_576
                    )
                )
                selected[mode] = release
            if platforms[0] != platforms[1]:
                raise ValueError("management staged configuration snapshots differ")
            return publish_pair(state, selected, platforms, pair)
        finally:
            os.close(lock)


def publish_pair(state: Path, selected: dict[str, Path], platforms: list[bytes], pair: str) -> Path:
    active = state / "management-active"
    active.mkdir(mode=0o755, exist_ok=True)
    direct_directory(active, os.geteuid(), os.getegid(), 0o755)
    collection = active / "pairs"
    collection.mkdir(mode=0o755, exist_ok=True)
    direct_directory(collection, os.geteuid(), os.getegid(), 0o755)
    identity = pair + "-" + hashlib.sha256(platforms[0]).hexdigest()[:16]
    final = collection / identity
    if final.exists():
        direct_directory(final, os.geteuid(), os.getegid(), 0o755)
        if set(p.name for p in final.iterdir()) != {"broker", "web"}:
            raise ValueError("management active pair has unexpected entries")
        for mode, release in selected.items():
            link = final / mode
            if (
                not link.is_symlink()
                or link.lstat().st_uid != os.geteuid()
                or os.readlink(link) != str(release)
            ):
                raise ValueError("management retained active pair differs")
    else:
        stage = collection / (".pair-" + secrets.token_hex(8))
        stage.mkdir(mode=0o755)
        try:
            for mode, release in selected.items():
                (stage / mode).symlink_to(release)
            sync(stage)
            os.rename(stage, final)
            sync(collection)
        except BaseException:
            import shutil

            shutil.rmtree(stage)
            raise
    link = active / "current"
    if os.path.lexists(link) and (not link.is_symlink() or link.lstat().st_uid != os.geteuid()):
        raise ValueError("management active selector is not root-owned")
    temporary = active / (".active-" + secrets.token_hex(8))
    temporary.symlink_to(final.relative_to(active))
    os.replace(temporary, link)
    sync(active)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Activate a reviewed management release pair")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--operator-uid", type=int, required=True)
    parser.add_argument("--broker-gid", type=int, required=True)
    parser.add_argument("--web-gid", type=int, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not os.path.ismount(args.state):
        raise ValueError("management activation requires root and mounted admin state")
    activate(args.state, args.operator_uid, {"broker": args.broker_gid, "web": args.web_gid})


if __name__ == "__main__":
    main()
