"""Shared deadlines, helper transport, and mutation guards for product services."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from ..config import Config
from . import database as db

HelperCaller = Callable[..., Mapping[str, object]]


class ServiceDeadlineError(RuntimeError):
    """A product service exhausted its whole-operation deadline."""


def operation_deadline(config: Config) -> float:
    return time.monotonic() + config.policy.limits.process_seconds


def remaining_seconds(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ServiceDeadlineError("operation exceeded its whole-operation deadline")
    return min(float(maximum), remaining)


def wall_deadline(deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ServiceDeadlineError("operation exceeded its whole-operation deadline")
    return (
        (datetime.now(UTC) + timedelta(seconds=remaining))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def reject_deleting(connection: sqlite3.Connection, application: db.Application) -> None:
    operation = db.get_unfinished_operation(connection, f"app-{application.application_id}")
    if operation is not None and operation.kind == "app.delete":
        raise db.UnfinishedOperationError(operation.scope, operation.operation_id, operation.kind)
