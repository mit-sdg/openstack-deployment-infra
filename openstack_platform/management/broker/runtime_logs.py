"""Recent output of a running app, for its owner and for admins.

The controller answers each read by asking Nomad while it holds its shared
lock, so the broker shares one read per app and stream for a few seconds and
runs at most one at a time: open log views can't crowd out deploys and status
reads.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...controller.http import HttpError, Request, Response
from ..common import utc
from .client import ControllerUnavailable

if TYPE_CHECKING:
    from .api import Broker

STREAMS = ("stdout", "stderr")
LINES = 500
TEXT_BYTES = 262_144
SHARE_SECONDS = 5.0
WAIT_SECONDS = 4.0


def tail(text: str, limit: int = TEXT_BYTES) -> tuple[str, bool]:
    """The end of text within limit bytes, from a line start; and whether it was cut."""
    raw = text.encode()
    if len(raw) <= limit:
        return text, False
    kept = raw[-limit:]
    newline = kept.find(b"\n")
    if 0 <= newline < len(kept) - 1:
        kept = kept[newline + 1 :]
    else:
        # No line start to cut at: drop a character split by the cut.
        kept = kept.lstrip(bytes(range(0x80, 0xC0)))
    return kept.decode("utf-8", errors="replace"), True


class RuntimeLogs:
    def __init__(self, broker: Broker, clock: Callable[[], float] = time.monotonic) -> None:
        self.broker = broker
        self.clock = clock
        self.lock = threading.Lock()
        self.reading = threading.BoundedSemaphore(1)
        self.shared: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def handle(self, request: Request) -> Response:
        _user, app = self.broker.own(request)
        stream = request.query.get("stream", ("stdout",))
        if len(stream) != 1 or stream[0] not in STREAMS:
            raise HttpError(400, "INVALID_REQUEST", "Invalid log query.")
        return Response(200, {"data": self.read(app, stream[0])})

    def recent(self, key: tuple[str, str]) -> dict[str, Any] | None:
        with self.lock:
            now = self.clock()
            for old in [k for k, (at, _v) in self.shared.items() if now - at >= SHARE_SECONDS]:
                del self.shared[old]
            entry = self.shared.get(key)
            return None if entry is None else entry[1]

    def read(self, app: dict[str, Any], stream: str) -> dict[str, Any]:
        key = (app["id"], stream)
        result = self.recent(key)
        if result is not None:
            return result
        if not self.reading.acquire(timeout=WAIT_SECONDS):
            raise HttpError(
                503,
                "LOGS_BUSY",
                "Logs are busy right now. Try again in a few seconds.",
                retryable=True,
            )
        try:
            # Another request may have read it while this one waited.
            result = self.recent(key)
            if result is None:
                result = self.fetch(app, stream)
                with self.lock:
                    self.shared[key] = (self.clock(), result)
            return result
        finally:
            self.reading.release()

    def fetch(self, app: dict[str, Any], stream: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stream": stream,
            "running": False,
            "text": "",
            "truncated": False,
            "lines": LINES,
            "observedAt": utc(time.time()),
        }
        model = self.broker.app_model(app)
        if (
            model["lifecycleState"] != "ready"
            or not model["desiredRunning"]
            or not model["activeDeploymentId"]
        ):
            return result
        query = f"lines={LINES}" + ("&stream=stderr" if stream == "stderr" else "")
        status, value = self.broker.client.request(
            "GET", f"/v1/applications/{app['id']}/runtime-log?{query}"
        )
        error = value.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if status == 502 and code == "ALLOCATION_NOT_FOUND":
            # Stopped, restarting or crashed: nothing is running to read from.
            return result
        if status == 400 and stream == "stderr":
            # Controllers from before runtime-log streams serve only stdout.
            raise HttpError(409, "LOG_STREAM_UNAVAILABLE", "Error output isn't available yet.")
        text = value.get("text")
        if (
            status != 200
            or value.get("applicationId") != app["id"]
            or value.get("stream", "stdout") != stream
            or not isinstance(text, str)
        ):
            raise ControllerUnavailable("invalid runtime log result")
        text, cut = tail(text)
        return {
            **result,
            "running": True,
            "text": text,
            "truncated": cut or value.get("truncated") is True,
        }
