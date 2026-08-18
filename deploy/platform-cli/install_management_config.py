#!/usr/bin/env python3
"""Atomically install non-secret management configuration without privileges."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn

# Direct execution sets sys.path to deploy/platform-cli rather than the archive
# root.  Configuration must be installable before a release (and its virtual
# environment) exists, so resolve the package from this exact source archive.
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from platform_cli.config import load_platform, load_policy  # noqa: E402
from platform_cli.validation import ValidationError  # noqa: E402

_MAXIMUM_BYTES = 1_048_576


class ConfigInstallFailure(RuntimeError):
    """The requested configuration cannot be installed safely."""


def _fail(message: str) -> NoReturn:
    raise ConfigInstallFailure(message)


def _metadata(path: Path, *, directory: bool, source: bool = False) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail("a required configuration input is missing" if source else "configuration is missing")
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected or stat.S_ISLNK(metadata.st_mode):
        _fail("configuration inputs must be direct directories and regular files")
    if metadata.st_uid != os.geteuid():
        _fail("configuration inputs and destinations must be owned by the current user")
    if source and metadata.st_mode & 0o022:
        _fail("configuration inputs must not be writable by group or other")
    return metadata


def _ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = _metadata(path, directory=True)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        _fail("configuration directories must have mode 0700")


def _json_object(raw: bytes, *, field: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        document = json.loads(raw, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ConfigInstallFailure(f"{field} must be strict UTF-8 JSON") from error
    if not isinstance(document, dict):
        _fail(f"{field} must be a JSON object")
    return document


def _read_source(path: Path, *, field: str) -> tuple[bytes, dict[str, Any]]:
    metadata = _metadata(path, directory=False, source=True)
    if metadata.st_size > _MAXIMUM_BYTES:
        _fail(f"{field} exceeds its 1048576-byte limit")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ConfigInstallFailure("configuration input could not be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_ino != metadata.st_ino
            or opened.st_dev != metadata.st_dev
        ):
            _fail("configuration input changed while it was being opened")
        raw = b""
        while chunk := os.read(descriptor, min(65_536, _MAXIMUM_BYTES + 1 - len(raw))):
            raw += chunk
            if len(raw) > _MAXIMUM_BYTES:
                _fail(f"{field} exceeds its 1048576-byte limit")
    finally:
        os.close(descriptor)
    return raw, _json_object(raw, field=field)


def _check_destination(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    metadata = _metadata(path, directory=False)
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        _fail("installed configuration files must have mode 0600")


def _atomic_write(path: Path, raw: bytes) -> None:
    _check_destination(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def install(
    platform_source: Path,
    policy_source: Path,
    config_root: Path,
    state_root: Path,
) -> int:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        _fail("run the configuration installer as the unprivileged /srv/openstack-platform owner")
    if not config_root.is_absolute() or not state_root.is_absolute():
        _fail("configuration destinations must be absolute")

    platform_raw, _document = _read_source(platform_source, field="platform inventory")
    policy_raw, _document = _read_source(policy_source, field="platform policy")
    try:
        load_platform(platform_source)
        load_policy(policy_source, require_private=True)
    except (FileNotFoundError, ValidationError) as error:
        raise ConfigInstallFailure(
            "platform inventory or private policy failed validation"
        ) from error

    _ensure_directory(config_root)
    _ensure_directory(state_root)
    _atomic_write(config_root / "platform.json", platform_raw)
    _atomic_write(state_root / "policy.json", policy_raw)

    print("management-config=installed")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the current platform inventory and private M1 policy atomically.",
        allow_abbrev=False,
    )
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, default=Path("/srv/openstack-platform/config"))
    parser.add_argument("--state-root", type=Path, default=Path("/srv/openstack-platform/state"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return install(args.platform, args.policy, args.config_root, args.state_root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigInstallFailure, ValidationError) as error:
        print(f"configuration install failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except OSError:
        print("configuration install failed: filesystem operation failed safely", file=sys.stderr)
        raise SystemExit(1) from None
