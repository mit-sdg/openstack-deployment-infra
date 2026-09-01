#!/usr/bin/env python3
"""Authenticated control-host health checks without emitting credentials."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

import psycopg
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.http import bounded_json  # noqa: E402
from lib.platform_config import load  # noqa: E402
from lib.platform_contract import CONTRACT  # noqa: E402
from lib.tls import internal_ca_context  # noqa: E402

CONFIG = load()
HOST = CONFIG["addresses"]["storage"]
ROOT = Path(CONFIG["paths"]["root"])
CA = str(ROOT / "secrets/nomad-cli/internal-ca.pem")
PORTS = CONTRACT["ports"]


def env(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value
    return result


def main() -> int:
    values = env(str(ROOT / "secrets/storage-bootstrap.env"))
    with psycopg.connect(
        host=HOST,
        port=PORTS["postgres"],
        dbname="platform",
        user="platform_admin",
        password=values["POSTGRES_PASSWORD"],
        sslmode="verify-full",
        sslrootcert=CA,
        connect_timeout=8,
    ) as connection:
        if connection.execute("SELECT 1").fetchone() != (1,):
            raise RuntimeError("PostgreSQL query failed")
    mongo: MongoClient[dict[str, Any]] = MongoClient(
        HOST,
        PORTS["mongodb"],
        username="platform_admin",
        password=values["MONGO_PASSWORD"],
        authSource="admin",
        tls=True,
        tlsCAFile=CA,
        serverSelectionTimeoutMS=8_000,
    )
    try:
        if mongo.admin.command("ping")["ok"] != 1.0:
            raise RuntimeError("MongoDB ping failed")
    finally:
        mongo.close()
    context = internal_ca_context(CA)
    garage = bounded_json(
        f"https://{HOST}:{PORTS['garageRpc']}/v2/GetClusterHealth",
        headers={"Authorization": f"Bearer {values['GARAGE_ADMIN_TOKEN']}"},
        ssl_context=context,
        timeout_seconds=8,
        response_limit=65_536,
    )
    if not isinstance(garage, dict) or garage.get("status") != "healthy":
        raise RuntimeError("Garage health failed")
    basic = base64.b64encode(f"builder:{values['REGISTRY_BUILDER_PASSWORD']}".encode()).decode()
    registry = bounded_json(
        f"https://{HOST}:{PORTS['registry']}/v2/",
        headers={"Authorization": f"Basic {basic}"},
        ssl_context=context,
        timeout_seconds=8,
        response_limit=65_536,
    )
    if registry != {}:
        raise RuntimeError("registry health failed")
    print("managed-services=healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
