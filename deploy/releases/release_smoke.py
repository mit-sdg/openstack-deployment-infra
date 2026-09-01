#!/usr/bin/env python3
"""Smoke-test the entrypoint that an immutable release will expose."""

from __future__ import annotations

import argparse
import importlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast

_ACTION = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+")


class SmokeFailure(RuntimeError):
    """The candidate is not a complete production release."""


def _load_action_manifest(source: Path) -> tuple[str, ...]:
    path = source / "openstack_platform/helper/actions-v1.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise SmokeFailure("the integration-owned helper action manifest is missing") from None

    actions = tuple(line.strip() for line in lines if line.strip() and not line.startswith("#"))
    if not actions or any(not _ACTION.fullmatch(action) for action in actions):
        raise SmokeFailure("the helper action manifest is malformed")
    if actions != tuple(sorted(set(actions))):
        raise SmokeFailure("the helper action manifest must be sorted and contain no duplicates")
    return actions


def _production_handlers(module: ModuleType, *, platform_config: Path) -> Mapping[str, Any]:
    captured: dict[str, Mapping[str, Any]] = {}

    def capture(
        _input_stream: Any,
        _output_stream: Any,
        handlers: Mapping[str, Any],
        **_limits: Any,
    ) -> int:
        captured["handlers"] = handlers
        return 0

    original = module.__dict__.get("serve_once")
    if not callable(original):
        raise SmokeFailure("the helper entrypoint does not expose serve_once")
    module.__dict__["serve_once"] = capture
    previous_platform_config = os.environ.get("PLATFORM_CONFIG")
    os.environ["PLATFORM_CONFIG"] = str(platform_config)
    try:
        entrypoint = cast(Callable[[], int], module.__dict__.get("main"))
        result = entrypoint()
    finally:
        if previous_platform_config is None:
            os.environ.pop("PLATFORM_CONFIG", None)
        else:
            os.environ["PLATFORM_CONFIG"] = previous_platform_config
        module.__dict__["serve_once"] = original
    if result != 0 or "handlers" not in captured:
        raise SmokeFailure("the helper entrypoint did not register a production action map")
    return captured["handlers"]


def smoke_operator(source: Path, launcher: Path, restore_launcher: Path) -> None:
    module = importlib.import_module("openstack_platform.operator")
    if not callable(getattr(module, "main", None)):
        raise SmokeFailure("openstack_platform.operator:main is not callable")
    platform_example = source / "config/platform.example.json"
    policy_example = source / "config/platform-policy.example.json"
    if not platform_example.is_file() or not policy_example.is_file():
        raise SmokeFailure("the release is missing sanitized configuration examples")
    if not launcher.is_file() or stat.S_IMODE(launcher.stat().st_mode) & 0o111 == 0:
        raise SmokeFailure("the candidate operator launcher is unavailable")
    if not restore_launcher.is_file() or stat.S_IMODE(restore_launcher.stat().st_mode) & 0o111 == 0:
        raise SmokeFailure("the candidate controller restore launcher is unavailable")
    if not (source / "openstack_platform/restore.py").is_file():
        raise SmokeFailure("the release is missing its controller restore implementation")

    with tempfile.TemporaryDirectory(prefix="platform-release-smoke-") as directory:
        root = Path(directory)
        environment = {
            "HOME": str(root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            (launcher, "status"),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise SmokeFailure("the installed operator command rejected sanitized configuration")
        restore = subprocess.run(
            (restore_launcher, "--help"),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if restore.returncode != 0:
            raise SmokeFailure("the installed controller restore launcher rejected --help")


def smoke_helper(source: Path) -> None:
    expected = _load_action_manifest(source)
    module = importlib.import_module("openstack_platform.helper.main")
    if not callable(getattr(module, "main", None)):
        raise SmokeFailure("openstack_platform.helper.main:main is not callable")

    # Registration must not depend on or read an ambient private inventory.
    handlers = _production_handlers(
        module,
        platform_config=(source / "config/platform.example.json").resolve(strict=True),
    )
    actual = tuple(sorted(handlers))
    if actual != expected:
        raise SmokeFailure("the production helper action map does not exactly match actions-v1.txt")
    if "backup.accept" not in handlers:
        raise SmokeFailure("the production helper action map omits backup.accept")
    for family in ("app.", "storage."):
        if not any(action.startswith(family) for action in actual):
            raise SmokeFailure(f"the production helper action map omits the {family[:-1]} family")
    if any(not callable(handler) for handler in handlers.values()):
        raise SmokeFailure("the production helper action map contains a non-callable handler")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("operator", "helper"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--launcher", type=Path)
    parser.add_argument("--restore-launcher", type=Path)
    args = parser.parse_args(argv)

    if sys.version_info[:2] != (3, 14):
        raise SmokeFailure("release entrypoints require Python 3.14")
    source = args.source.resolve(strict=True)
    sys.path.insert(0, str(source))
    if args.mode == "operator":
        if args.launcher is None or args.restore_launcher is None:
            raise SmokeFailure("the operator smoke requires its installed launchers")
        smoke_operator(
            source,
            args.launcher.resolve(strict=True),
            args.restore_launcher.resolve(strict=True),
        )
    else:
        smoke_helper(source)
    print(f"release-smoke={args.mode}:ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as error:
        print(f"release smoke failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
