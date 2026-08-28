#!/usr/bin/env python3
"""Render reviewed persistent-host cloud-init into a protected output file."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openstack_platform.config import load_platform  # noqa: E402
from openstack_platform.host_user_data import (  # noqa: E402
    PERSISTENT_ROLES,
    HostUserDataInputs,
    render_host_user_data_file,
)
from openstack_platform.validation import ValidationError, uuid  # noqa: E402


def _platform_path(value: Path | None) -> Path:
    if value is not None:
        return value
    configured = os.environ.get("PLATFORM_CONFIG")
    if configured:
        return Path(configured)
    repository_config = ROOT / "config" / "platform.json"
    if repository_config.is_file():
        return repository_config
    raise ValidationError("platform.json was not found; set PLATFORM_CONFIG")


def _volumes(values: list[list[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, identifier in values:
        if not name or "\x00" in name or len(name.encode()) > 128 or name in result:
            raise ValidationError("retained volume arguments are malformed")
        result[name] = uuid(identifier, field="retained volume UUID")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True, choices=PERSISTENT_ROLES)
    parser.add_argument("--platform-config", type=Path)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--operator-public-key", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--pki-directory", type=Path, required=True)
    parser.add_argument("--cloudflare-tunnel-token-file", type=Path)
    parser.add_argument("--disable-cloudflared", action="store_true")
    parser.add_argument("--volume", nargs=2, action="append", default=[], metavar=("NAME", "UUID"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        render_host_user_data_file(
            load_platform(_platform_path(args.platform_config)),
            args.role,
            HostUserDataInputs(
                template=args.template,
                operator_public_key=args.operator_public_key,
                secret_file=args.secret_file,
                pki_directory=args.pki_directory,
                cloudflare_tunnel_token_file=args.cloudflare_tunnel_token_file,
                enable_cloudflared=not args.disable_cloudflared,
            ),
            _volumes(args.volume),
            args.output,
        )
    except (ValidationError, FileNotFoundError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
