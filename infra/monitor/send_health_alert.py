#!/usr/bin/env python3
"""Private sender: URL arrives on stdin, never in arguments or diagnostics."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.health_alert_config import validate_webhook  # noqa: E402
from lib.http import _NoRedirect  # noqa: E402


def main() -> int:
    try:
        document = json.loads(sys.stdin.buffer.read(16384))
        request = urllib.request.Request(
            validate_webhook(document["url"]),
            data=json.dumps(document["payload"]).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Ignore ambient proxy settings; never forward webhook tokens to a proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=8) as response:
            return 0 if 200 <= response.status < 300 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
