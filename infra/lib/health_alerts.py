"""Persistent, best-effort health notifications with a bounded sender process."""

from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .health_alert_config import validate, validate_webhook

CHECKS = ("openstack", "public_ingress", "managed_services", "nomad", "backup", "offsite_recovery")
SENDER_SECONDS = 10


def read_webhook(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > 4097
        ):
            raise ValueError(
                "health alert webhook file must be direct, owner-owned, mode 0600 and bounded"
            )
        raw = os.read(descriptor, 4098)
    finally:
        os.close(descriptor)
    return validate_webhook(raw.decode("utf-8").removesuffix("\n"))


def payload(
    namespace: str, event: str, snapshot: Mapping[str, Any], format_name: str
) -> dict[str, Any]:
    # Only known check names and fixed text cross the webhook boundary. Never
    # forward exception messages, subprocess output, URLs or provider payloads.
    checked = snapshot.get("checks", {})
    failed = (
        []
        if event != "failing"
        else [next((name for name in CHECKS if name not in checked), "unknown")]
    )
    timestamp = datetime.now(UTC).isoformat()
    summary = f"failed check: {', '.join(failed)}" if failed else "all checks passed"
    if event == "test":
        summary = "test notification; health state unchanged"
    text = f"Platform health {event}: {namespace}; {timestamp}; {summary}"
    if format_name == "slack":
        return {"text": text}
    return {
        "text": text,
        "namespace": namespace,
        "timestamp": timestamp,
        "event": event,
        "failed_checks": failed,
    }


def send(webhook: Path, message: Mapping[str, Any]) -> bool:
    try:
        url = read_webhook(webhook)
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "monitor/send_health_alert.py"),
            ],
            input=json.dumps({"url": url, "payload": message}).encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SENDER_SECONDS,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        # Exceptions can contain the token-bearing URL or response. Do not log them.
        return False


def save(path: Path, state: Mapping[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def transition(
    state: dict[str, Any], healthy: bool, now: float, settings: Mapping[str, Any]
) -> str | None:
    previous_failures = state["failures"]
    state["failures"] = 0 if healthy else min(previous_failures + 1, 100)
    if healthy:
        if state["incident"] and (previous_failures or now - state["last_attempt"] >= 300):
            state["last_attempt"] = now
            return "recovered"
    elif state["failures"] >= settings["failureThreshold"] and (
        not state["incident"] or now - state["last_attempt"] >= settings["repeatSeconds"]
    ):
        state["incident"] = True
        state["last_attempt"] = now
        return "failing"
    return None


def notify(config: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
    try:
        settings = validate(config.get("healthAlerts", {}))
        if not settings["enabled"]:
            return
        directory = Path(config["paths"]["adminState"]) / "operator/status"
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        state_path = directory / "health-alerts.json"
        with (directory / "health-alerts.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = {"failures": 0, "incident": False, "last_attempt": 0.0}
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if (
                    type(state.get("failures")) is not int
                    or not 0 <= state["failures"] <= 100
                    or type(state.get("incident")) is not bool
                    or type(state.get("last_attempt")) not in (int, float)
                    or not math.isfinite(state["last_attempt"])
                    or state["last_attempt"] < 0
                ):
                    raise ValueError("health alert state is invalid")
            event = transition(
                state, snapshot["healthy"] is True, datetime.now(UTC).timestamp(), settings
            )
            # Save the attempt before network I/O so a reboot won't resend it.
            save(state_path, state)
            if event:
                webhook = (
                    Path(config["paths"]["adminState"]) / "operator/secrets/health-alert-webhook"
                )
                delivered = send(
                    webhook, payload(config["namespace"], event, snapshot, settings["format"])
                )
                if event == "recovered" and delivered:
                    state["incident"] = False
                    save(state_path, state)
                print("health-alert=sent" if delivered else "health-alert=delivery-failed")
    except Exception:
        print("health-alert=unavailable")
