#!/usr/bin/env python3
"""Fail closed rather than advertise incomplete legacy logical backup coverage."""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request
from typing import Any


def require_shared(kind: str, inventory: Any) -> None:
    if not isinstance(inventory, dict) or not isinstance(inventory.get("items"), list):
        raise ValueError("instance backup inventory is unavailable")
    if any(
        not isinstance(item, dict) or item.get("type") not in {"postgres", "mongo"}
        for item in inventory["items"]
    ):
        raise ValueError("instance backup inventory is invalid")
    if any(item["type"] == kind for item in inventory["items"]):
        raise ValueError(
            "isolated instances require verified managed-volume snapshots; shared-only logical export is incomplete"
        )


def main() -> int:
    host, port, ca, kind = sys.argv[1:]
    request = urllib.request.Request(
        f"https://{host}:{port}/platform/instances",
        data=b'{"action":"list"}',
        headers={
            "Authorization": "Bearer " + os.environ["GARAGE_ADMIN_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, context=ssl.create_default_context(cafile=ca), timeout=30
        ) as response:
            payload = response.read(1048577)
        if len(payload) > 1048576:
            raise ValueError("instance backup inventory exceeded its bound")
        require_shared(kind, json.loads(payload))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("instance backup inventory is unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
