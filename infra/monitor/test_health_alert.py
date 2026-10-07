#!/usr/bin/env python3
"""Send an operator test notification without changing health alert state."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.health_alert_config import validate  # noqa: E402
from lib.health_alerts import payload, send  # noqa: E402
from lib.platform_config import load  # noqa: E402


def main() -> int:
    config = load()
    settings = validate(config.get("healthAlerts", {}))
    if not settings["enabled"]:
        print("health-alert=disabled")
        return 1
    webhook = Path(config["paths"]["adminState"]) / "operator/secrets/health-alert-webhook"
    delivered = send(webhook, payload(config["namespace"], "test", {}, settings["format"]))
    print("health-alert=sent" if delivered else "health-alert=delivery-failed")
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
