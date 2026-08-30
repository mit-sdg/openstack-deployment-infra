#!/usr/bin/env python3
"""Write a secret-free health snapshot from inside the platform control plane."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.http import bounded_request  # noqa: E402
from lib.platform_config import load  # noqa: E402

CONFIG = load()
ROOT = Path(CONFIG["paths"]["root"])
NAMESPACE = CONFIG["namespace"]
OPENSTACK = os.environ.get("OPENSTACK", str(ROOT / "bin/platform-openstack"))
NOMAD = os.environ.get("NOMAD", str(ROOT / f"bin/{NAMESPACE}-nomad"))
SERVICE_CHECK_PYTHON = os.environ.get(
    "SERVICE_CHECK_PYTHON", str(ROOT / "tools/service-check-venv/bin/python")
)
CHECK_SERVICES = os.environ.get(
    "CHECK_SERVICES", str(ROOT / "persistent/platform/infra/monitor/check_services.py")
)
STATUS = ROOT / f"persistent/status/{NAMESPACE}.json"
BACKUPS = Path(CONFIG["paths"]["backups"]) / NAMESPACE


def command(*args: str, timeout: int = 45) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout).stdout


def admin_command(*args: str, timeout: int = 45) -> str:
    return command(*args, timeout=timeout)


def main() -> int:
    checks: dict[str, object] = {}
    error: str | None = None
    try:
        servers = json.loads(command(OPENSTACK, "server", "list", "-f", "json"))
        by_name = {server["Name"]: server for server in servers}
        core = set(CONFIG["hosts"].values())
        if not core <= by_name.keys() or any(by_name[name]["Status"] != "ACTIVE" for name in core):
            raise RuntimeError("a core OpenStack server is not ACTIVE")
        workers = [
            server for server in servers if server["Name"].startswith(f"{CONFIG['prefix']}-worker-")
        ]
        if any(server["Status"] != "ACTIVE" for server in workers):
            raise RuntimeError("an application worker is not ACTIVE")
        checks["openstack"] = {"core_active": 3, "workers_active": len(workers)}

        # The origin is deliberately not a health surface in tunnel mode and
        # is provider-CIDR restricted in direct mode. Exercise only the public
        # ingress contract from here.
        for hostname in (CONFIG["domain"], f"wildcard-health.{CONFIG['domain']}"):
            response = bounded_request(
                f"https://{hostname}/healthz",
                headers={"User-Agent": f"{NAMESPACE}-health/1.0"},
                timeout_seconds=15,
                response_limit=16,
            )
            if response.strip() != b"OK":
                raise RuntimeError(
                    f"public ingress health response is unexpected for {hostname}"
                )
        checks["public_ingress"] = "healthy"

        admin_command(SERVICE_CHECK_PYTHON, CHECK_SERVICES)
        checks["managed_services"] = "healthy"

        nodes = json.loads(admin_command(NOMAD, "node", "status", "-json"))
        ready = [node for node in nodes if node.get("Status") == "ready"]
        if len(ready) < len(workers):
            raise RuntimeError("fewer ready Nomad clients than active workers")
        admin_command(NOMAD, "operator", "raft", "list-peers")
        checks["nomad"] = {"ready_clients": len(ready), "raft": "healthy"}

        backup_dirs = sorted(path for path in BACKUPS.glob("20??????T??????Z") if path.is_dir())
        if not backup_dirs:
            raise RuntimeError("no encrypted platform backup exists")
        latest = backup_dirs[-1]
        created = datetime.strptime(latest.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        age_hours = (datetime.now(UTC) - created).total_seconds() / 3600
        if age_hours > 36:
            raise RuntimeError("latest encrypted platform backup is stale")
        required = {"postgres.age", "mongodb.age", "garage.age", "SHA256SUMS", "MANIFEST"}
        if not required <= {path.name for path in latest.iterdir()}:
            raise RuntimeError("latest platform backup is incomplete")
        checks["backup"] = {"age_hours": round(age_hours, 2), "encrypted": True}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    document = {
        "checked_at": datetime.now(UTC).isoformat(),
        "healthy": error is None,
        "checks": checks,
        "error": error,
    }
    STATUS.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=STATUS.parent, delete=False) as handle:
        json.dump(document, handle, sort_keys=True)
        handle.write("\n")
        temp = Path(handle.name)
    temp.chmod(0o640)
    temp.replace(STATUS)
    print("platform-health=healthy" if error is None else "platform-health=failed")
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
