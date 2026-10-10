"""Local privileged rollout command; prints a durable ID before submission."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid as uuid_module
from pathlib import Path
from typing import Any

from ..management.broker.client import UnixConnection
from ..validation import uuid


def repair(
    socket_path: Path,
    request_id: str,
    *,
    seconds: int,
    output: Any,
    route: str = "/v1/admin/storage/repair-postgres",
    body: dict[str, Any] | None = None,
) -> None:
    request_id = uuid(request_id, field="request ID")
    deadline = time.monotonic() + seconds
    label = "Instance migration" if route.endswith("migrate-instances") else "PostgreSQL repair"
    print(f"{label} request-id={request_id}", file=output, flush=True)

    def request(method: str, path: str) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("repair deadline exceeded; retry with the printed request ID")
        connection = UnixConnection(socket_path, min(remaining, 30))
        try:
            options: dict[str, Any] = {
                "headers": {"Idempotency-Key": request_id} if method == "POST" else {}
            }
            if method == "POST" and body is not None:
                options["body"] = json.dumps(body)
                options["headers"]["Content-Type"] = "application/json"
            connection.request(method, path, **options)
            response = connection.getresponse()
            payload = response.read(65_537)
            if len(payload) > 65_536 or response.status not in {200, 202}:
                raise RuntimeError("controller rejected repair; retry with the printed request ID")
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise RuntimeError("controller repair response is invalid")
            return value
        finally:
            connection.close()

    result = request("POST", route)
    if result.get("operationId") != request_id:
        raise RuntimeError("controller repair operation identity is invalid")
    while True:
        operation = request("GET", f"/v1/admin/operations/{request_id}")
        state = operation.get("status")
        if state == "succeeded":
            print(label + " completed", file=output)
            return
        if state in {"failed", "recovery_required"}:
            raise RuntimeError("repair requires reconciliation; retry with the printed request ID")
        time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair current PostgreSQL roles through the local privileged controller socket"
    )
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if not args.socket.is_absolute() or not 1 <= args.timeout <= 3600:
        parser.error("socket must be absolute and timeout must be 1..3600 seconds")
    try:
        repair(
            args.socket,
            args.request_id or str(uuid_module.uuid4()),
            seconds=args.timeout,
            output=sys.stdout,
        )
    except (OSError, ValueError, RuntimeError):
        print(
            "PostgreSQL repair did not complete; retry with the printed request ID", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
