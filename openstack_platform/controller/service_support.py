"""Shared deadlines, helper transport, and mutation guards for product services."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from .. import remote
from ..config import Config
from . import database as db

HelperCaller = Callable[..., Mapping[str, object]]


# Codes are fixed vocabulary, never arbitrary helper output. Messages, tracebacks,
# arguments, stderr and response bodies can contain secrets and are not logged.
_HELPER_FAILURE_CODES = frozenset(
    {
        "ABSENCE_UNCONFIRMED",
        "ACTION_FAILED",
        "ALLOCATION_NOT_FOUND",
        "ARTIFACT_UNAVAILABLE",
        "BACKUP_EXISTS",
        "BACKUP_KEY_MISSING",
        "BACKUP_NOT_FOUND",
        "BUCKET_NOT_EMPTY",
        "BUILD_REJECTED",
        "CANDIDATE_MISMATCH",
        "CANDIDATE_UNHEALTHY",
        "CHECKSUM_MISMATCH",
        "CLEANUP_UNCONFIRMED",
        "CONFIRMATION_REQUIRED",
        "CREATE_ROLLED_BACK",
        "DEADLINE_EXPIRED",
        "MEMORY_BUDGET_EXCEEDED",
        "CONNECTION_BUDGET_EXCEEDED",
        "DISK_BUDGET_EXCEEDED",
        "INSTANCE_OPERATION_FAILED",
        "INVALID_INSTANCE_REQUEST",
        "INSTANCE_NOT_READY",
        "DEPENDENCY_UNAVAILABLE",
        "ENVIRONMENT_CONFLICT",
        "ENVIRONMENT_HEALTH_FAILED",
        "ENVIRONMENT_ROLLBACK_FAILED",
        "EXTERNAL_OPERATION_FAILED",
        "HELPER_ACTION_FAILED",
        "IDENTITY_MISMATCH",
        "INTERNAL_ERROR",
        "INVALID_ARGS",
        "INVALID_BACKUP",
        "INVALID_REQUEST",
        "INVALID_STATE",
        "JOB_AMBIGUOUS",
        "JOB_CONFLICT",
        "JOB_IDENTITY_MISMATCH",
        "JOB_REMAINS",
        "NOMAD_CANDIDATE_UNCONFIRMED",
        "NOMAD_RESPONSE_INVALID",
        "NOMAD_UNAVAILABLE",
        "NOT_FOUND",
        "PROVIDER_RESPONSE_INVALID",
        "PROVIDER_UNAVAILABLE",
        "RECOVERY_REQUIRED",
        "RESOURCE_EXISTS",
        "RESTART_TIMEOUT",
        "RUNTIME_UNAVAILABLE",
        "SOURCE_REJECTED",
        "SOURCE_UNAVAILABLE",
        "STOP_UNCONFIRMED",
        "STORAGE_CONFLICT",
        "UNKNOWN_ACTION",
        "UNSUPPORTED_PRIOR_STATE",
        "VARIABLE_INCOMPLETE",
        "VARIABLE_MALFORMED",
        "VARIABLE_MISSING",
        "VARIABLE_REMAINS",
        "WORKER_NOT_READY",
    }
)


def logged_helper(caller: HelperCaller, *, config_shaped: bool = True) -> HelperCaller:
    if getattr(caller, "_logs_helper_failures", False):
        return caller

    def call(*args: object, **kwargs: object) -> Mapping[str, object]:
        try:
            return caller(*args, **kwargs)
        except Exception as error:
            action = args[1 if config_shaped else 0]
            # All actions come from internal call sites; still constrain diagnostics
            # to the release's fixed action vocabulary.
            known_action = isinstance(action, str) and (
                action
                in {
                    "app.build",
                    "app.build.cleanup",
                    "app.build.logs",
                    "app.builder.delete",
                    "app.deploy",
                    "app.promote",
                    "app.health",
                    "app.restart",
                    "app.stop",
                    "app.remove",
                    "app.startup",
                    "app.logs",
                    "app.env.list",
                    "app.env.set",
                    "app.env.remove",
                    "app.worker.create",
                    "app.worker.observe",
                    "app.worker.capacity",
                    "app.worker.delete",
                    "app.manifest.retain",
                    "app.manifest.verify",
                    "app.manifest.delete",
                    "app.source.key",
                    "app.source.check",
                    "app.source.commits",
                    "app.source.preflight",
                    "backup.accept",
                    "storage.host.observe",
                    "storage.instances.network",
                    "storage.instances.migrate",
                }
                or action
                in {
                    f"storage.{kind}.{verb}"
                    for kind in ("postgres", "mongo", "s3")
                    for verb in (
                        "create",
                        "observe",
                        "verify",
                        "rotate",
                        "remove",
                        "limits",
                        "usage",
                    )
                }
            )
            error_class = "Exception"
            code = "DETAILS_REDACTED"
            if isinstance(error, remote.HelperError):
                error_class = (
                    "DependencyUnavailable"
                    if isinstance(error, remote.DependencyUnavailable)
                    else "HelperError"
                )
                code = (
                    error.code
                    if error.code in _HELPER_FAILURE_CODES
                    else "UNRECOGNIZED_HELPER_ERROR"
                )
            elif isinstance(error, remote.ProtocolError):
                error_class = "ProtocolError"
                code = "INVALID_HELPER_RESPONSE"
            elif isinstance(error, TimeoutError):
                error_class, code = "TimeoutError", "DEADLINE_EXCEEDED"
            elif isinstance(error, OSError):
                error_class, code = "OSError", "HELPER_TRANSPORT_FAILED"
            logging.getLogger(__name__).warning(
                "helper failure action=%s class=%s code=%s",
                action if known_action else "unknown",
                error_class,
                code,
            )
            raise

    call.__dict__["_logs_helper_failures"] = True
    return call


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
