"""Handle-based admin preparation; never mutate a path after checking its type."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def observed(value: os.stat_result | None) -> str:
    if value is None:
        return "uid:gid:mode:nlink=unavailable"
    return f"uid:gid:mode:nlink={value.st_uid}:{value.st_gid}:{stat.S_IMODE(value.st_mode):04o}:{value.st_nlink} type={stat.S_IFMT(value.st_mode):06o} bytes={value.st_size}"


def refusal(path: Path, expected: str, value: os.stat_result | None) -> ValueError:
    return ValueError(f"{path}: expected {expected}; observed {observed(value)}")


def open_at(parent: int, name: str, flags: int, path: Path) -> int:
    try:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    except OSError as error:
        try:
            value = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            value = None
        raise refusal(path, f"direct accessible object ({error.strerror})", value) from error


@contextmanager
def directory(path: Path, owners: set[int], *, refusals: list[str] | None = None) -> Iterator[int]:
    """Open every component without following links, retaining the final handle."""
    if not path.is_absolute() or ".." in path.parts:
        raise refusal(path, "direct absolute path without ..", None)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        current = Path("/")
        for part in path.parts[1:]:
            current /= part
            child = open_at(fd, part, os.O_RDONLY | os.O_DIRECTORY, current)
            os.close(fd)
            fd = child
            if os.fstat(fd).st_uid not in owners:
                error = refusal(current, f"directory owner in {sorted(owners)}", os.fstat(fd))
                if refusals is None:
                    raise error
                refusals.append(str(error))
        yield fd
    finally:
        os.close(fd)


def metadata(
    fd: int,
    uid: int,
    groups: set[int] | None,
    modes: set[int],
    *,
    is_directory: bool,
    path: Path,
    single_link: bool = True,
) -> None:
    value = os.fstat(fd)
    kind = stat.S_ISDIR if is_directory else stat.S_ISREG
    if (
        not kind(value.st_mode)
        or value.st_uid != uid
        or (groups is not None and value.st_gid not in groups)
        or stat.S_IMODE(value.st_mode) not in modes
        or (single_link and not is_directory and value.st_nlink != 1)
    ):
        raise refusal(
            path,
            f"{'directory' if is_directory else 'regular object type'} uid={uid} gid={sorted(groups) if groups is not None else 'any'} modes={[f'{m:04o}' for m in sorted(modes)]} nlink={'1' if single_link and not is_directory else 'any'}",
            value,
        )


def normalize_metadata(
    fd: int, path: Path, uid: int, groups: set[int], modes: set[int], *, is_directory: bool
) -> None:
    # A private 0600 file / 0700 directory grants no group access. Its current
    # group is irrelevant; readable/traversable group access remains allowlisted.
    readable_groups = groups if os.fstat(fd).st_mode & 0o070 else None
    metadata(fd, uid, readable_groups, modes, is_directory=is_directory, path=path)


def normalize(
    path: Path,
    uid: int,
    groups: set[int],
    modes: set[int],
    gid: int,
    mode: int,
    *,
    is_directory: bool = False,
) -> None:
    with directory(path if is_directory else path.parent, {0, uid}) as parent:
        fd = (
            os.dup(parent)
            if is_directory
            else open_at(parent, path.name, os.O_RDONLY | os.O_NONBLOCK, path)
        )
        try:
            normalize_metadata(fd, path, uid, groups, modes, is_directory=is_directory)
            # Ownership/type are checked on this same inode. Unprivileged
            # replacement of the directory entry cannot redirect either call.
            os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)


def check(
    path: Path,
    uid: int,
    groups: set[int] | None,
    modes: set[int],
    *,
    single_link: bool = False,
    refusals: list[str] | None = None,
) -> None:
    with directory(path.parent, {0, uid}, refusals=refusals) as parent:
        fd = open_at(parent, path.name, os.O_RDONLY | os.O_NONBLOCK, path)
        try:
            metadata(fd, uid, groups, modes, is_directory=False, path=path, single_link=single_link)
        finally:
            os.close(fd)


def copy(source: Path, destination: Path, source_uid: int, uid: int, gid: int) -> None:
    """Copy a bounded operator file to a fresh inode in the service directory."""
    with directory(source.parent, {0, source_uid}) as parent:
        fd = open_at(parent, source.name, os.O_RDONLY | os.O_NONBLOCK, source)
        with os.fdopen(fd, "rb") as stream:
            metadata(
                stream.fileno(),
                source_uid,
                None,
                {0o600},
                is_directory=False,
                path=source,
                single_link=False,
            )
            raw = stream.read(1_048_577)
            if len(raw) > 1_048_576:
                raise refusal(
                    source, "regular source at most 1048576 bytes", os.fstat(stream.fileno())
                )
    with directory(destination.parent, {0, uid}) as parent:
        destination_parent(parent, destination.parent, uid)
        # Replace only the directory entry. Never open or mutate the old inode:
        # its group, ownership and hardlinks do not affect an atomic replacement.
        try:
            value = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISDIR(value.st_mode):
                raise refusal(destination, "non-directory destination or absent", value)
        name = ".prepare-" + secrets.token_hex(16)
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fchown(fd, uid, gid)
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            os.replace(name, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def destination_parent(fd: int, path: Path, uid: int) -> None:
    value = os.fstat(fd)
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != uid or value.st_mode & 0o022:
        raise refusal(
            path, f"directory uid={uid} without group/other writes (gid unrestricted)", value
        )


def directory_check(
    path: Path, uid: int, gid: int, mode: int, *, ancestors: set[int] | None = None
) -> None:
    """No-follow postcondition for tmpfiles, which may otherwise exit zero."""
    with directory(path, {0, uid, os.geteuid()} | (ancestors or set())) as fd:
        metadata(fd, uid, {gid}, {mode}, is_directory=True, path=path)


def preflight(plan: dict[str, Any]) -> list[str]:
    """Check every planned input/output without reading contents or mutating."""
    errors: list[str] = []
    for action in plan["operations"]:
        kind = action["kind"]
        if kind == "copy":
            tasks = [
                ("source", Path(action["source"])),
                ("destination", Path(action["destination"])),
            ]
        else:
            tasks = [(kind, Path(action["path"]))]
        for category, path in tasks:
            try:
                if category == "source":
                    check(path, action["sourceUid"], None, {0o600}, refusals=errors)
                    with directory(
                        path.parent, {0, action["sourceUid"]}, refusals=errors
                    ) as parent:
                        value = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                        if value.st_size > 1_048_576:
                            raise refusal(path, "source at most 1048576 bytes", value)
                elif category == "destination":
                    with directory(path.parent, {0, action["uid"]}, refusals=errors) as fd:
                        destination_parent(fd, path.parent, action["uid"])
                        try:
                            value = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            if stat.S_ISDIR(value.st_mode):
                                raise refusal(path, "non-directory destination or absent", value)
                elif category == "normalize" and action.get("directory", False):
                    with directory(path, {0, action["uid"]}, refusals=errors) as fd:
                        normalize_metadata(
                            fd,
                            path,
                            action["uid"],
                            set(action["allowedGids"]),
                            set(action["allowedModes"]),
                            is_directory=True,
                        )
                else:
                    if category == "normalize":
                        with directory(path.parent, {0, action["uid"]}, refusals=errors) as parent:
                            fd = open_at(parent, path.name, os.O_RDONLY | os.O_NONBLOCK, path)
                            try:
                                normalize_metadata(
                                    fd,
                                    path,
                                    action["uid"],
                                    set(action["allowedGids"]),
                                    set(action["allowedModes"]),
                                    is_directory=False,
                                )
                            finally:
                                os.close(fd)
                    else:
                        check(
                            path,
                            action["uid"],
                            set(action["allowedGids"]) if "allowedGids" in action else None,
                            set(action["allowedModes"]),
                            refusals=errors,
                        )
            except (OSError, ValueError) as error:
                errors.append(f"{path}: {error}")
    return list(dict.fromkeys(errors))


def apply(plan: dict[str, Any]) -> None:
    errors = preflight(plan)
    if errors:
        raise ValueError("\n".join(errors))
    for action in plan["operations"]:
        if action["kind"] == "copy":
            copy(
                Path(action["source"]),
                Path(action["destination"]),
                action["sourceUid"],
                action["uid"],
                action["gid"],
            )
        elif action["kind"] == "normalize":
            normalize(
                Path(action["path"]),
                action["uid"],
                set(action["allowedGids"]),
                set(action["allowedModes"]),
                action["gid"],
                action["mode"],
                is_directory=action.get("directory", False),
            )
        else:
            check(
                Path(action["path"]),
                action["uid"],
                set(action["allowedGids"]) if "allowedGids" in action else None,
                set(action["allowedModes"]),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Handle-based root admin file preparation")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("apply", "preflight"):
        command = commands.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
    command = commands.add_parser("directory-check")
    command.add_argument("path", type=Path)
    command.add_argument("--uid", type=int, required=True)
    command.add_argument("--gid", type=int, required=True)
    command.add_argument("--mode", type=lambda value: int(value, 8), required=True)
    command.add_argument("--ancestor-uid", type=int, action="append", default=[])
    for name in ("normalize", "check"):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
        command.add_argument("--uid", type=int, required=True)
        command.add_argument(
            "--allowed-gid", type=int, action="append", required=name == "normalize"
        )
        command.add_argument(
            "--allowed-mode", type=lambda value: int(value, 8), action="append", required=True
        )
        if name == "normalize":
            command.add_argument("--gid", type=int, required=True)
            command.add_argument("--mode", type=lambda value: int(value, 8), required=True)
            command.add_argument("--directory", action="store_true")
    command = commands.add_parser("copy")
    command.add_argument("source", type=Path)
    command.add_argument("destination", type=Path)
    for name in ("source-uid", "uid", "gid"):
        command.add_argument("--" + name, type=int, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("admin preparation entry point requires root")
    if args.command in {"preflight", "apply"}:
        plan = json.loads(args.plan.read_bytes())
        if args.command == "apply":
            apply(plan)
        else:
            errors = preflight(plan)
            for error in errors:
                print(error)
            print(f"root-path-preflight={'refused' if errors else 'ok'} refusals={len(errors)}")
            raise SystemExit(bool(errors))
    elif args.command == "directory-check":
        directory_check(args.path, args.uid, args.gid, args.mode, ancestors=set(args.ancestor_uid))
    elif args.command == "copy":
        copy(args.source, args.destination, args.source_uid, args.uid, args.gid)
    elif args.command == "check":
        check(
            args.path,
            args.uid,
            set(args.allowed_gid) if args.allowed_gid else None,
            set(args.allowed_mode),
        )
    else:
        normalize(
            args.path,
            args.uid,
            set(args.allowed_gid),
            set(args.allowed_mode),
            args.gid,
            args.mode,
            is_directory=args.directory,
        )


if __name__ == "__main__":
    main()
