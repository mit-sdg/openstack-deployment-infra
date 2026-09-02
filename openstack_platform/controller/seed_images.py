"""Seed the hosted controller's immutable role-image selections during setup."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from .. import openstack, runtime
from ..config import load_platform
from ..contracts import IMAGE_ROLES
from ..validation import ValidationError, uuid
from . import database as db

_MAXIMUM_BYTES = 65_536


class SeedFailure(RuntimeError):
    """Hosted image selections could not be seeded safely."""


def _fail(message: str) -> NoReturn:
    raise SeedFailure(message)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _manifest(path: Path, *, project_id: str, namespace: str) -> dict[str, str]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SeedFailure("hosted image seed is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > _MAXIMUM_BYTES
    ):
        _fail("hosted image seed must be a direct current-user-owned mode-0600 file")
    try:
        raw = path.read_bytes()
        document = json.loads(raw, object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SeedFailure("hosted image seed is malformed") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "projectId", "namespace", "images"}
        or document.get("schemaVersion") != 1
        or document.get("projectId") != project_id
        or document.get("namespace") != namespace
    ):
        _fail("hosted image seed identity does not match the deployment")
    images = document.get("images")
    if not isinstance(images, Mapping) or set(images) != set(IMAGE_ROLES):
        _fail("hosted image seed does not contain every platform role")
    return {role: uuid(images[role], field=f"{role} image UUID") for role in IMAGE_ROLES}


def seed(
    *,
    platform_config: Path,
    state_directory: Path,
    manifest: Path,
    openstack_command: str,
    timeout_seconds: float,
) -> None:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        _fail("hosted image seeding must run as the controller account")
    platform = load_platform(platform_config.resolve(strict=True))
    state = runtime.ensure_private_directory(state_directory, create=True)
    references = _manifest(
        manifest,
        project_id=platform.project_id,
        namespace=platform.namespace,
    )
    identity = db.deployment_identity(platform)
    with runtime.lock(state, "database-maintenance", wait=True):
        connection = db.connect(state / "platform.sqlite3", identity=identity)
        try:
            db.migrate(connection, identity=identity)
            missing: dict[str, str] = {}
            for role, image_id in references.items():
                existing = db.get_image_selection(connection, role)
                if existing is None:
                    missing[role] = image_id
                elif existing.image_id != image_id:
                    _fail("hosted controller already selected a different role image")
            if missing:
                selected = openstack.select_images(
                    platform,
                    missing,
                    timeout_seconds=timeout_seconds,
                    executable=openstack_command,
                )
                for role in IMAGE_ROLES:
                    item = selected.get(role)
                    if item is None:
                        continue
                    db.put_image_selection(
                        connection,
                        role=item.role,
                        image_id=item.image_id,
                        display_name=item.display_name,
                        source_commit=item.source_commit,
                        compatibility_hash=item.compatibility_hash,
                    )
        finally:
            connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--platform-config", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--openstack-command", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout_seconds <= 0 or args.timeout_seconds > 900:
        print("hosted image seeding failed: timeout is invalid", file=sys.stderr)
        return 1
    try:
        seed(
            platform_config=args.platform_config,
            state_directory=args.state_directory,
            manifest=args.manifest,
            openstack_command=args.openstack_command,
            timeout_seconds=args.timeout_seconds,
        )
    except (SeedFailure, ValidationError, openstack.OpenStackError, OSError):
        print("hosted image seeding failed safely", file=sys.stderr)
        return 1
    print("hosted-image-selections=ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
