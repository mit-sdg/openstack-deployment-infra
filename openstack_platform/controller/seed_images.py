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
from ..config import PlatformConfig, load_platform
from ..contracts import IMAGE_ROLES
from ..validation import ValidationError, bounded_text, commit, sha256_hex, uuid
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


def _manifest(
    path: Path,
    *,
    platform: PlatformConfig,
) -> dict[str, openstack.ImageSelection]:
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
        or document.get("projectId") != platform.project_id
        or document.get("namespace") != platform.namespace
    ):
        _fail("hosted image seed identity does not match the deployment")
    images = document.get("images")
    if not isinstance(images, Mapping) or set(images) != set(IMAGE_ROLES):
        _fail("hosted image seed does not contain every platform role")
    expected_compatibility = openstack.image_compatibility_hash(platform)
    result: dict[str, openstack.ImageSelection] = {}
    for role in IMAGE_ROLES:
        raw_item = images[role]
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "imageId",
            "displayName",
            "sourceCommit",
            "compatibilityHash",
        }:
            _fail("hosted image seed role record is malformed")
        display_name = bounded_text(
            raw_item["displayName"], field="image display name", maximum=256
        )
        compatibility_hash = sha256_hex(
            raw_item["compatibilityHash"], field="image compatibility hash"
        )
        if display_name != platform.get(f"images.{role}"):
            _fail("hosted image seed name does not match platform inventory")
        if compatibility_hash != expected_compatibility:
            _fail("hosted image seed compatibility does not match platform inventory")
        result[role] = openstack.ImageSelection(
            role=role,
            image_id=uuid(raw_item["imageId"], field=f"{role} image UUID"),
            display_name=display_name,
            source_commit=commit(raw_item["sourceCommit"]),
            compatibility_hash=compatibility_hash,
        )
    return result


def seed(*, platform_config: Path, state_directory: Path, manifest: Path) -> None:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        _fail("hosted image seeding must run as the controller account")
    platform = load_platform(platform_config.resolve(strict=True))
    state = runtime.ensure_private_directory(state_directory, create=True)
    selections = _manifest(manifest, platform=platform)
    identity = db.deployment_identity(platform)
    with runtime.lock(state, "database-maintenance", wait=True):
        connection = db.connect(state / "platform.sqlite3", identity=identity)
        try:
            db.migrate(connection, identity=identity)
            for role, item in selections.items():
                existing = db.get_image_selection(connection, role)
                if existing is not None and (
                    existing.image_id != item.image_id
                    or existing.display_name != item.display_name
                    or existing.source_commit != item.source_commit
                    or existing.compatibility_hash != item.compatibility_hash
                ):
                    _fail("hosted controller already selected a different role image")
                if existing is None:
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        seed(
            platform_config=args.platform_config,
            state_directory=args.state_directory,
            manifest=args.manifest,
        )
    except (SeedFailure, ValidationError, OSError):
        print("hosted image seeding failed safely", file=sys.stderr)
        return 1
    print("hosted-image-selections=ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
