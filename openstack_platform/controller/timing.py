"""Structured operation/helper timings without request bodies or error details."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from ..validation import uuid

_LOG = logging.getLogger("openstack_platform.timings")
_OPERATION: ContextVar[str | None] = ContextVar("platform_operation", default=None)
_ACTION = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+")


def configure() -> None:
    """Enable only this allowlisted logger, never general HTTP/provider logging."""
    if not _LOG.handlers:
        _LOG.addHandler(logging.StreamHandler())
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False


def _emit(event: str, operation_id: str, *, action: str | None = None, **values: object) -> None:
    try:
        _LOG.info(
            json.dumps(
                {
                    "event": event,
                    "operationId": operation_id,
                    "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    **({"action": action} if action is not None else {}),
                    **values,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except Exception:
        # Diagnostics must not change deployment/recovery outcomes.
        pass


@contextmanager
def operation(operation_id: str) -> Iterator[None]:
    identifier = uuid(operation_id, field="operation ID")
    token = _OPERATION.set(identifier)
    started = time.monotonic()
    succeeded = False
    _emit("operation.started", identifier)
    try:
        yield
        succeeded = True
    finally:
        _emit(
            "operation.finished",
            identifier,
            elapsedSeconds=round(time.monotonic() - started, 3),
            returned=succeeded,
        )
        _OPERATION.reset(token)


@contextmanager
def helper(action: str) -> Iterator[None]:
    identifier = _OPERATION.get()
    # Never echo arbitrary input as a metric label, even if a future caller
    # accidentally bypasses the fixed-action transport contract.
    if identifier is None or len(action) > 64 or _ACTION.fullmatch(action) is None:
        yield
        return
    started = time.monotonic()
    succeeded = False
    _emit("helper.started", identifier, action=action)
    try:
        yield
        succeeded = True
    finally:
        _emit(
            "helper.finished",
            identifier,
            action=action,
            elapsedSeconds=round(time.monotonic() - started, 3),
            returned=succeeded,
        )
