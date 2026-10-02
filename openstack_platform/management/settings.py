"""Public owner-portal inventory policy and operator-owned configuration rendering."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from ..owner_portal_config import LIMITS, validate


def write_document(folder: Path, path: Path, raw: bytes) -> None:
    if path.is_symlink() or (
        path.exists()
        and (path.stat().st_uid != os.geteuid() or path.stat().st_gid != folder.stat().st_gid)
    ):
        raise ValueError("management configuration ownership is invalid")
    fd, name = tempfile.mkstemp(prefix=".management-", dir=folder)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o640)
        if os.fstat(fd).st_gid != folder.stat().st_gid:
            raise ValueError("management configuration did not inherit its service group")
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def configuration(platform: Any, platform_path: Path) -> dict[str, Any]:
    settings = validate(platform.document.get("ownerPortal", {"enabled": False}))
    if not settings["enabled"]:
        raise ValueError("ownerPortal is disabled")
    namespace = platform.namespace
    state = Path(platform.get("paths.adminState"))
    return {
        "portalOrigin": f"https://{platform.domain}",
        "commonsOrigin": settings["commonsOrigin"],
        "classLabel": settings["classLabel"],
        "stateDirectory": str(state / "management-broker"),
        "brokerSocket": f"/run/{namespace}-management-broker/broker.sock",
        "controllerSocket": f"/run/{namespace}-controller/project.sock",
        "identitySocket": f"/run/{namespace}-management-identity/identity.sock",
        "development": False,
        "platformConfig": str(platform_path),
        "clientAddressHeader": "cf-connecting-ip",
        **{name: settings[name] for name in LIMITS},
    }


def render(
    platform_path: Path, *, expected_groups: dict[str, int] | None = None
) -> tuple[Path, Path, Path]:
    from .. import contracts
    from ..config import _plain, load_platform

    if os.geteuid() == 0:
        raise ValueError("management rendering requires the unprivileged operator")

    if expected_groups is None:
        accounts = json.loads(contracts._contract_bytes())["accounts"]
        expected_groups = {
            mode: int(accounts["management" + mode.title()]["gid"]) for mode in ("broker", "web")
        }
    platform_path = platform_path.resolve(strict=True)
    platform = load_platform(platform_path)
    paths = []
    for component in ("broker", "web", "identity"):
        root = (
            Path(platform.get("paths.adminState"))
            / f"management-{'broker' if component == 'identity' else component}-releases"
        )
        folder = root / "config"
        for directory in (root, folder):
            metadata = directory.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or directory.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o2750
                or metadata.st_gid
                != expected_groups["broker" if component == "identity" else component]
            ):
                raise ValueError(
                    "management config requires prepared operator-owned setgid directories"
                )
        prepared = folder / "platform.json"
        if component != "identity":
            write_document(
                folder,
                prepared,
                (
                    json.dumps(_plain(platform.document), sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode(),
            )
        document = configuration(platform, prepared)
        path = folder / ("identity.json" if component == "identity" else "management.json")
        value = (
            {
                "commonsOrigin": document["commonsOrigin"],
                "socket": document["identitySocket"],
                "development": False,
            }
            if component == "identity"
            else document
        )
        write_document(
            folder, path, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        paths.append(path)
    return paths[0], paths[1], paths[2]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render public production owner-portal configuration"
    )
    parser.add_argument("--platform-config", required=True, type=Path)
    args = parser.parse_args()
    render(args.platform_config)
    print("management-config=rendered development=false")
