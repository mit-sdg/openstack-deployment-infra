#!/usr/bin/env python3
"""One fixed authenticated read-only storage-host endpoint, behind local TLS nginx."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import subprocess
import tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

STATUS_PATH = "/platform/host-status"


def snapshot(data: Path, names: list[str]) -> dict[str, Any]:
    names = list(names)
    memory = {
        key: int(value.split()[0]) * 1024
        for key, value in (
            line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
        )
    }
    volume = os.statvfs(data)
    import uuid

    namespace = names[0].removesuffix("-postgres")
    limits = {name: 4 * 1024**3 if name.endswith("-garage") else 2 * 1024**3 for name in names}
    instance_root = data / "instances"
    if instance_root.exists():
        for directory in instance_root.iterdir():
            config = directory / "config.json"
            if (
                not directory.is_dir()
                or directory.name.endswith(".deleting")
                or not config.is_file()
            ):
                continue
            identifier = str(uuid.UUID(directory.name))
            value = json.loads(config.read_text())
            name = f"{namespace}-db-{identifier}"
            names.append(name)
            limits[name] = int(value["quotas"]["memoryBytes"])
    inspected = subprocess.run(
        ["podman", "inspect", *names], check=False, capture_output=True, timeout=5
    )
    objects = {
        item["Name"]: item
        for item in json.loads(inspected.stdout or b"[]")
        if item.get("Name") in names
    }
    containers = []
    for name in names:
        item = objects.get(name)
        used = 0
        available = False
        if item is not None and int(item["State"]["Pid"]) > 0:
            pid = int(item["State"]["Pid"])
            try:
                relative = next(
                    line.removeprefix("0::")
                    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                    if line.startswith("0::")
                )
                group = (Path("/sys/fs/cgroup") / relative.lstrip("/")).resolve(strict=True)
                if not group.is_relative_to("/sys/fs/cgroup"):
                    raise ValueError("invalid cgroup")
                used = int((group / "memory.current").read_text())
                available = True
            except (OSError, ValueError, StopIteration):
                pass
        containers.append(
            {"name": name, "usedBytes": used, "limitBytes": limits[name], "available": available}
        )
    return {
        "cpuCount": os.cpu_count(),
        "loadAverage": list(os.getloadavg()),
        "memory": {"totalBytes": memory["MemTotal"], "availableBytes": memory["MemAvailable"]},
        "dataVolume": {
            "totalBytes": volume.f_blocks * volume.f_frsize,
            "usedBytes": (volume.f_blocks - volume.f_bfree) * volume.f_frsize,
        },
        "containers": containers,
    }


def handler(token: str, data: Path, names: list[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def do_GET(self) -> None:
            if self.path != STATUS_PATH:
                self.send_error(404)
                return
            if not hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()
            ):
                self.send_error(403)
                return
            try:
                body = json.dumps(snapshot(data, names), separators=(",", ":")).encode()
            except Exception:
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: Any) -> None:
            # Neither authorization headers nor provider exceptions reach logs.
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    args = parser.parse_args()
    credential = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "garage-config"
    config = tomllib.loads(credential.read_text())
    names = [f"{args.namespace}-{kind}" for kind in ("postgres", "mongodb", "garage", "registry")]
    HTTPServer(
        ("127.0.0.1", 19001), handler(config["admin"]["admin_token"], args.data, names)
    ).serve_forever()


if __name__ == "__main__":
    main()
