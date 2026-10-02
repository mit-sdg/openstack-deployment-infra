"""Operator reactivation of a verified retained pair and its own config snapshot."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from .. import contracts, release_manifest
from ..config import load_platform, platform_config_identity
from ..management_release import COMPATIBILITY, record, safe_name, tar_files, verify, verify_files
from .activation import pairs, read
from .installation import (
    RUNTIME,
    development_root,
    directory,
    launcher,
    request_activation,
    select,
    smoke,
    verify_tree,
)
from .settings import configuration


def retained(
    root: Path,
    name: str,
    mode: str,
    gid: int,
    *,
    python: Path,
    allow_unsigned_development: bool,
    allow_unsigned_production: bool,
) -> tuple[Path, dict[str, Any], bytes]:
    if not re.fullmatch(r"[0-9a-f]{40}-[0-9a-f]{16}-[0-9a-f]{16}", name):
        raise ValueError("retained release must be a complete release basename")
    directory(root, gid)
    directory(root / "releases", gid)
    release = root / "releases" / name
    directory(release, gid)
    directory(release / "evidence", gid)
    directory(release / "config", gid)
    uid = os.geteuid()
    evidence: dict[str, bytes] = {}
    for path in (release / "evidence").iterdir():
        if path.name not in {
            "release-manifest.json",
            "management-artifacts.json",
            "release.sbom.json",
            "release.provenance.json",
            "management.sbom.json",
            "management.provenance.json",
            "release-manifest.sig",
            "management-artifacts.sig",
            "release-trust-root.pem",
        }:
            raise ValueError("retained evidence name differs")
        evidence[path.name] = read(path, uid, gid, 0o440, 1_048_576)
    descriptor = json.loads(evidence["management-artifacts.json"], object_pairs_hook=pairs)
    # Refuse incompatible metadata before running any retained code.
    if descriptor.get("compatibility") != COMPATIBILITY:
        raise ValueError(
            "schema-incompatible rollback is refused; explicit migration review required"
        )
    for key in descriptor["archives"][mode]["files"]:
        safe_name(key)
    files = {
        key: read(release / key, uid, gid, 0o440, 32 * 1024**2)
        for key in descriptor["archives"][mode]["files"]
    }
    if {key: record(raw) for key, raw in files.items()} != descriptor["archives"][mode]["files"]:
        raise ValueError("retained payload hashes differ")
    with tempfile.TemporaryDirectory(prefix="management-rollback-verify-") as temporary:
        frozen = Path(temporary)
        for key, raw in evidence.items():
            (frozen / key).write_bytes(raw)
        source = frozen / "source"
        source.mkdir()
        source_files = tar_files(files["source.tar"])
        for key, raw in source_files.items():
            target = source / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        policy: dict[str, Any] = {
            "trust_root": frozen / "release-trust-root.pem"
            if "release-trust-root.pem" in evidence
            else None,
            "allow_unsigned_development": allow_unsigned_development,
            "allow_unsigned_production": allow_unsigned_production,
        }
        release_manifest.verify(
            source,
            frozen / "release-manifest.json",
            expected_commit=descriptor["sourceCommit"],
            signature=frozen / "release-manifest.sig"
            if "release-manifest.sig" in evidence
            else None,
            **policy,
        )
        verified = verify(
            frozen / "release-manifest.json",
            frozen / "management-artifacts.json",
            signature=frozen / "management-artifacts.sig"
            if "management-artifacts.sig" in evidence
            else None,
            **policy,
        )
        if verified != descriptor:
            raise ValueError("retained descriptor differs from verified evidence")
        verify_files(files, descriptor, mode, source_files)
    inventory = read(release / "config/platform.json", uid, gid, 0o440, 1_048_576)
    platform = load_platform(release / "config/platform.json")
    if (
        descriptor["sourceCommit"] != name[:40]
        or descriptor["pairIdentity"][:16] != name[41:57]
        or record(inventory)["sha256"][:16] != name[58:]
    ):
        raise ValueError("retained release identity differs")
    installed = {**files, **{"evidence/" + key: raw for key, raw in evidence.items()}}
    installed["config/platform.json"] = inventory
    installed["config/management.json"] = release_manifest._canonical(
        configuration(platform, Path("platform.json"))
    )
    if mode == "broker":
        installed["config/identity.json"] = release_manifest._canonical(
            {
                "commonsOrigin": platform.get("ownerPortal.commonsOrigin"),
                "socket": f"/run/{platform.namespace}-management-identity/identity.sock",
                "development": False,
            }
        )
    for service in ("broker", "identity") if mode == "broker" else ("web",):
        installed[f"bin/management-{service}"] = launcher(service, python)
    if (
        read(release / ".complete", uid, gid, 0o440, 41)
        != (descriptor["sourceCommit"] + "\n").encode()
    ):
        raise ValueError("retained release is incomplete")
    verify_tree(release, installed, gid)
    return release, descriptor, inventory


def reactivate(
    platform_path: Path,
    broker_name: str,
    web_name: str,
    *,
    allow_unsigned_development: bool = False,
    allow_unsigned_production: bool = False,
    expected_groups: dict[str, int] | None = None,
) -> tuple[Path, Path]:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        raise ValueError("retained reactivation requires the unprivileged operator")
    platform = load_platform(platform_path.resolve(strict=True))
    state = Path(platform.get("paths.adminState"))
    if allow_unsigned_development:
        if os.environ.get("PLATFORM_ENVIRONMENT") == "production":
            raise ValueError("development rollback is refused on admin")
        development_root(state)
    elif not os.path.ismount(state):
        raise ValueError("retained reactivation requires mounted admin state")
    accounts = json.loads(contracts._contract_bytes())["accounts"]
    groups = expected_groups or {
        mode: int(accounts["management" + mode.title()]["gid"]) for mode in ("broker", "web")
    }
    roots = {mode: state / f"management-{mode}-releases" for mode in groups}
    for mode, root in roots.items():
        directory(root, groups[mode])
    fd = os.open(
        roots["broker"] / ".install.lock", os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("retained reactivation lock differs")
        fcntl.flock(fd, fcntl.LOCK_EX)
        has_active = os.path.lexists(state / "management-active/current")
        for mode in groups:
            active = (
                state / "management-active/current" / mode / "evidence/management-artifacts.json"
            )
            if has_active:
                try:
                    active_descriptor = json.loads(
                        read(active, os.geteuid(), groups[mode], 0o440, 1_048_576),
                        object_pairs_hook=pairs,
                    )
                except OSError as error:
                    raise ValueError(
                        f"{active}: active selector is dangling or incomplete; rollback is refused"
                    ) from error
                if active_descriptor.get("compatibility") != COMPATIBILITY:
                    raise ValueError("schema-incompatible active release; rollback is refused")
        python = Path(sys.executable).absolute() if allow_unsigned_development else RUNTIME
        selected = {
            mode: retained(
                roots[mode],
                name,
                mode,
                groups[mode],
                python=python,
                allow_unsigned_development=allow_unsigned_development,
                allow_unsigned_production=allow_unsigned_production,
            )
            for mode, name in (("broker", broker_name), ("web", web_name))
        }
        broker, descriptor, inventory = selected["broker"]
        web, peer, peer_inventory = selected["web"]
        if descriptor != peer or inventory != peer_inventory:
            raise ValueError("retained releases do not form one config-matched pair")
        old_platform = load_platform(broker / "config/platform.json")
        if platform_config_identity(old_platform) != platform_config_identity(platform):
            raise ValueError("retained inventory belongs to a different deployment")
        # Validate both selectors and the marker before the first mutation.
        for root in roots.values():
            link = root / "current"
            if os.path.lexists(link) and (
                not link.is_symlink() or link.lstat().st_uid != os.geteuid()
            ):
                raise ValueError("staged selector is not operator-owned")
        marker = roots["broker"] / "activate-request"
        if os.path.lexists(marker):
            read(marker, os.geteuid(), groups["broker"], 0o640, 128)
        for mode, (release, _, _) in selected.items():
            smoke(release, mode)
        select(roots["broker"], broker)
        select(roots["web"], web)
        request_activation(
            roots["broker"],
            descriptor["sourceCommit"],
            descriptor["pairIdentity"],
            groups["broker"],
        )
        return broker, web
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Request activation of a compatible retained management pair"
    )
    parser.add_argument("--platform-config", type=Path, required=True)
    parser.add_argument("--broker-release", required=True)
    parser.add_argument("--web-release", required=True)
    parser.add_argument("--allow-unsigned-development", action="store_true")
    parser.add_argument("--allow-unsigned-production", action="store_true")
    args = parser.parse_args()
    broker, web = reactivate(
        args.platform_config,
        args.broker_release,
        args.web_release,
        allow_unsigned_development=args.allow_unsigned_development,
        allow_unsigned_production=args.allow_unsigned_production,
    )
    print(f"management-reactivation=requested broker={broker.name} web={web.name}")


if __name__ == "__main__":
    main()
