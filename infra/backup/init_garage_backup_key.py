#!/usr/bin/env python3
"""Create and persist the non-expiring Garage read-only backup key once."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.http import bounded_json  # noqa: E402
from lib.platform_config import load  # noqa: E402
from lib.platform_contract import CONTRACT  # noqa: E402
from lib.tls import internal_ca_context  # noqa: E402

CONFIG = load()
HOST = CONFIG["addresses"]["storage"]
ROOT = Path(CONFIG["paths"]["root"])
STORAGE_SECRETS = Path(os.environ.get("STORAGE_SECRETS", ROOT / "secrets/storage-bootstrap.env"))
OUTPUT = Path(os.environ.get("GARAGE_BACKUP_SECRETS", ROOT / "secrets/garage-backup.env"))
CA_FILE = os.environ.get("GARAGE_CA_FILE", str(ROOT / "secrets/nomad-cli/internal-ca.pem"))
GARAGE_RPC_PORT = CONTRACT["ports"]["garageRpc"]


def env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def main() -> int:
    if OUTPUT.exists():
        if OUTPUT.stat().st_mode & 0o077:
            raise RuntimeError("Garage backup secret file permissions are too broad")
        print("garage-backup-key=existing")
        return 0
    token = env(STORAGE_SECRETS)["GARAGE_ADMIN_TOKEN"]
    key = bounded_json(
        f"https://{HOST}:{GARAGE_RPC_PORT}/v2/CreateKey",
        data=json.dumps({"name": "platform-backup", "neverExpires": True}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        ssl_context=internal_ca_context(CA_FILE),
        timeout_seconds=15,
        response_limit=65_536,
    )
    if not isinstance(key, dict):
        raise RuntimeError("Garage returned malformed backup key data")
    access = key.get("accessKeyId")
    secret = key.get("secretAccessKey")
    if not access or not secret:
        raise RuntimeError("Garage did not return the new backup key secret")
    fd = os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"GARAGE_BACKUP_ACCESS_KEY={access}\n")
        handle.write(f"GARAGE_BACKUP_SECRET_KEY={secret}\n")
    print("garage-backup-key=created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
