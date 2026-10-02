"""Post-trust management staging with inherited groups and pair activation."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .. import contracts
from ..config import _plain, load_platform, platform_config_identity
from ..management_release import COMPATIBILITY, record
from .settings import configuration

RUNTIME = Path("/run/current-system/sw/bin/management-python3.14")


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def directory(path: Path, gid: int, *, create: bool = False) -> None:
    if create and not path.exists():
        # mkdir inherits S_ISGID and the parent group even when the operator
        # is not a member. Never chmod a directory: Linux may clear that bit.
        prior = os.umask(0o027)
        try:
            path.mkdir(mode=0o2750)
        finally:
            os.umask(prior)
    meta = path.lstat()
    if (
        not stat.S_ISDIR(meta.st_mode)
        or path.is_symlink()
        or meta.st_uid != os.geteuid()
        or meta.st_gid != gid
        or stat.S_IMODE(meta.st_mode) != 0o2750
    ):
        raise ValueError(
            "management directory must be operator-owned setgid 2750 with its service group"
        )


def write(path: Path, data: bytes, gid: int, mode: int = 0o440) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        meta = os.fstat(fd)
        if meta.st_uid != os.geteuid() or meta.st_gid != gid:
            raise ValueError("management file did not inherit its service group")
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def launcher(mode: str, python: Path) -> bytes:
    config = "identity.json" if mode == "identity" else "management.json"
    return f"""#!/bin/sh
set -eu
PATH=/run/current-system/sw/bin:/usr/bin:/bin
export PATH
release=$(dirname "$(dirname "$(readlink -f -- "$0")")")
case "$#:$*" in 0:) smoke= ;; 1:--smoke) smoke=--smoke ;; *) exit 64 ;; esac
exec env -i PATH="$PATH" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 {shlex.quote(str(python))} -I -B "$release/runtime/openstack_platform/management/entry.py" {mode} --config "$release/config/{config}" --requirements "$release/requirements.json" --assets "$release/static" $smoke
""".encode()


def smoke(release: Path, mode: str) -> None:
    modes = ("broker", "identity") if mode == "broker" else ("web",)
    for service in modes:
        result = subprocess.run(
            [release / "bin" / f"management-{service}", "--smoke"],
            capture_output=True,
            timeout=30,
            env={"PATH": "/run/current-system/sw/bin:/usr/bin:/bin"},
            check=False,
        )
        if result.returncode or result.stdout != f"management-smoke={service}:ok\n".encode():
            raise ValueError("management candidate smoke failed; prior selection is unchanged")


def verify_tree(root: Path, files: dict[str, bytes], gid: int) -> None:
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    allowed = set(files) | ({".complete"} if (root / ".complete").exists() else set())
    if actual != allowed:
        raise ValueError("management installed tree has unexpected or missing files")
    for name, data in files.items():
        path = root / name
        meta = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(meta.st_mode)
            or meta.st_uid != os.geteuid()
            or meta.st_gid != gid
            or stat.S_IMODE(meta.st_mode) & 0o227
            or record(path.read_bytes()) != record(data)
        ):
            raise ValueError("management installed payload differs or is writable by service/other")
    for path in (root, *[p for p in root.rglob("*") if p.is_dir()]):
        directory(path, gid)


def select(root: Path, final: Path) -> None:
    link = root / "current"
    if os.path.lexists(link) and (not link.is_symlink() or link.lstat().st_uid != os.geteuid()):
        raise ValueError("management current selector is not an owned symlink")
    temporary = root / (".current-" + secrets.token_hex(8))
    temporary.symlink_to(final.relative_to(root))
    os.replace(temporary, link)
    sync_directory(root)


def request_activation(root: Path, commit: str, pair: str, gid: int) -> None:
    marker = root / "activate-request"
    if marker.is_symlink():
        raise ValueError("activation marker must not be a symlink")
    temporary = marker.with_name(".activate-" + secrets.token_hex(8))
    try:
        write(temporary, (commit + "\n" + pair + "\n").encode(), gid, 0o640)
        os.replace(temporary, marker)
        sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def current_descriptor(root: Path) -> dict[str, Any] | None:
    link = root / "current"
    if not link.exists():
        return None
    if not link.is_symlink() or link.lstat().st_uid != os.geteuid():
        raise ValueError("management selector ownership differs")
    release = link.resolve(strict=True)
    if not release.is_relative_to(root / "releases") or not (release / ".complete").is_file():
        raise ValueError("management selector is outside complete releases")
    descriptor = release / "evidence/management-artifacts.json"
    if descriptor.is_symlink() or not descriptor.is_file():
        raise ValueError("retained management compatibility evidence is missing")
    value = json.loads(descriptor.read_bytes())
    metadata = descriptor.stat()
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_gid != root.stat().st_gid
        or stat.S_IMODE(metadata.st_mode) != 0o440
    ):
        raise ValueError("retained management evidence ownership or mode differs")
    if value.get("compatibility") != COMPATIBILITY:
        raise ValueError("management schema/protocol rollback requires explicit migration review")
    return cast(dict[str, Any], value)


def development_root(state: Path) -> None:
    temporary = Path.cwd() / ".tmp"
    if (
        temporary.is_symlink()
        or state.absolute() != state.resolve()
        or not state.is_relative_to(temporary.absolute())
    ):
        raise ValueError("development installation requires direct paths under worktree .tmp")


def install(
    args: Any, descriptor: dict[str, Any], files: dict[str, bytes], evidence: dict[str, bytes]
) -> Path:
    if args.platform_config is None:
        raise ValueError("management installation requires platform configuration")
    platform_path = Path(args.platform_config).resolve(strict=True)
    platform = load_platform(platform_path)
    state = Path(platform.get("paths.adminState"))
    development = (
        bool(args.allow_unsigned_development)
        and descriptor["releaseChannel"] == "development-unsigned"
    )
    if development:
        development_root(state)
    elif not os.path.ismount(state):
        raise ValueError("management installation requires mounted admin state before locking")
    root = state / "management-broker-releases"
    if root.is_symlink() or root.stat().st_uid != os.geteuid():
        raise ValueError("management lock root must be operator-owned and direct")
    fd = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.geteuid():
            raise ValueError("management install lock ownership differs")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _install(args, descriptor, files, evidence, platform)
    finally:
        os.close(fd)


def _install(
    args: Any,
    descriptor: dict[str, Any],
    files: dict[str, bytes],
    evidence: dict[str, bytes],
    platform: Any,
) -> Path:
    allowed_evidence = {
        "release-manifest.json",
        "management-artifacts.json",
        "release.sbom.json",
        "release.provenance.json",
        "management.sbom.json",
        "management.provenance.json",
        "release-manifest.sig",
        "management-artifacts.sig",
        "release-trust-root.pem",
    }
    if any(Path(name).name != name or name not in allowed_evidence for name in evidence):
        raise ValueError("management retained evidence name is outside its allowlist")
    if json.loads(evidence["management-artifacts.json"]) != descriptor:
        raise ValueError("management retained descriptor differs from verified input")
    if args.platform_config is None:
        raise ValueError("management installation requires platform configuration")
    state = Path(platform.get("paths.adminState"))
    if (
        args.expected_platform_namespace is not None
        and args.expected_platform_namespace != platform.namespace
    ):
        raise ValueError("management namespace differs")
    if (
        args.expected_platform_identity_sha256 is not None
        and args.expected_platform_identity_sha256 != platform_config_identity(platform)
    ):
        raise ValueError("management deployment identity differs")
    development = (
        bool(args.allow_unsigned_development)
        and descriptor["releaseChannel"] == "development-unsigned"
    )
    if development:
        development_root(state)
    elif not os.path.ismount(state):
        raise ValueError("management installation requires mounted admin state")
    if not development and (
        args.expected_platform_namespace is None or args.expected_platform_identity_sha256 is None
    ):
        raise ValueError("production management installation requires expected deployment identity")
    accounts = json.loads(contracts._contract_bytes())["accounts"]
    roots = {mode: state / f"management-{mode}-releases" for mode in ("broker", "web")}
    groups = {
        mode: (
            roots[mode].stat().st_gid
            if development
            else int(accounts["management" + mode.title()]["gid"])
        )
        for mode in roots
    }
    for component, folder in roots.items():
        directory(folder, groups[component])
        directory(folder / "releases", groups[component])
        directory(folder / "config", groups[component])
        old = current_descriptor(folder)
        if old and old["compatibility"] != descriptor["compatibility"]:
            raise ValueError("management peer protocol/schema differs")
    mode: str = args.mode
    root: Path = roots[mode]
    gid: int = groups[mode]
    if args.release_root is not None and Path(args.release_root).absolute() != root:
        raise ValueError("management release root must come from platform inventory")
    if args.bin_root is not None:
        raise ValueError("management launchers are release-local, not global bin links")
    python = Path(args.python).absolute() if development else RUNTIME
    if not development and Path(args.python).absolute() not in {
        Path(sys.executable).absolute(),
        RUNTIME,
    }:
        raise ValueError("production management requires the stable admin interpreter")
    result = subprocess.run(
        [python, "-I", "-c", "import sys; print(sys.version_info[:2])"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode or result.stdout.strip() != b"(3, 14)":
        raise ValueError("management requires Python 3.14")
    platform_bytes = (
        json.dumps(_plain(platform.document), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    final: Path = (
        root
        / "releases"
        / (
            str(args.commit)
            + "-"
            + descriptor["pairIdentity"][:16]
            + "-"
            + record(platform_bytes)["sha256"][:16]
        )
    )
    installed = {**files, **{"evidence/" + name: raw for name, raw in evidence.items()}}
    # Configuration belongs to this immutable release, not a live shared root.
    installed["config/platform.json"] = platform_bytes
    installed["config/management.json"] = (
        json.dumps(
            configuration(platform, Path("platform.json")), sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode()
    if mode == "broker":
        installed["config/identity.json"] = (
            json.dumps(
                {
                    "commonsOrigin": platform.get("ownerPortal.commonsOrigin"),
                    "socket": f"/run/{platform.namespace}-management-identity/identity.sock",
                    "development": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    for service in ("broker", "identity") if mode == "broker" else ("web",):
        installed[f"bin/management-{service}"] = launcher(service, python)
    if final.exists():
        if not (final / ".complete").is_file():
            raise ValueError("incomplete management release already exists")
        verify_tree(final, installed, gid)
        smoke(final, mode)
    else:
        stage = root / "releases" / ("." + args.commit + "-" + secrets.token_hex(8))
        directory(stage, gid, create=True)
        try:
            for name, data in installed.items():
                target = stage / name
                relative = target.parent.relative_to(stage)
                folder = stage
                for part in relative.parts:
                    folder = folder / part
                    directory(folder, gid, create=True)
                write(target, data, gid, 0o550 if name.startswith("bin/") else 0o440)
            write(stage / ".candidate", (args.commit + "\n").encode(), gid, 0o400)
            smoke(stage, mode)
            (stage / ".candidate").unlink()
            verify_tree(stage, installed, gid)
            write(stage / ".complete", (args.commit + "\n").encode(), gid)
            for folder in reversed([stage, *[p for p in stage.rglob("*") if p.is_dir()]]):
                sync_directory(folder)
            os.rename(stage, final)
            sync_directory(root / "releases")
        except BaseException:
            import shutil

            shutil.rmtree(stage)
            raise
    select(root, final)
    broker = current_descriptor(roots["broker"])
    web = current_descriptor(roots["web"])
    if (
        broker
        and web
        and broker["pairIdentity"] == web["pairIdentity"] == descriptor["pairIdentity"]
        and (roots["broker"] / "current/config/platform.json").read_bytes()
        == (roots["web"] / "current/config/platform.json").read_bytes()
    ):
        request_activation(
            roots["broker"], args.commit, descriptor["pairIdentity"], groups["broker"]
        )
    return final
