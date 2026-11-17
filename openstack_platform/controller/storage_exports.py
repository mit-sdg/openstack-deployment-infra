"""Staff database snapshots: read-only helper work outside every app scope.

A snapshot runs as its own operation in the scope ``export-<resource>``, so it
never waits for, or blocks, a deploy or storage change of the same app. The
operation records only the object's metadata; download links are made on
request and never stored.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import remote
from ..config import Config
from ..validation import uuid
from . import database as db
from .http import HttpError
from .service_support import HelperCaller, operation_deadline, wall_deadline
from .storage_contract import public_s3_endpoint

EXPORT_KIND = "storage.export"
# Snapshots share the controller's operation workers with deploys, so only
# one runs at a time across the platform.
EXPORT_CONCURRENCY = 1
RETENTION = timedelta(hours=24)
LINK_SECONDS = 20
SWEEP_SECONDS = 3600
_OBJECT_KEY = re.compile(
    r"exports/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
    r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.zip"
)
_FILENAME = re.compile(r"[a-z0-9][a-z0-9-]{0,100}-[0-9]{8}T[0-9]{6}Z\.zip")
_SHA256 = re.compile(r"[a-f0-9]{64}")
# Helper codes a staff member can act on; anything else is a plain failure.
_EXPORT_ERRORS = frozenset(
    {"EXPORT_TOO_LARGE", "INSTANCE_NOT_READY", "INSTANCE_MANAGER_UNAVAILABLE", "DEADLINE_EXPIRED"}
)


def scope(resource_id: str) -> str:
    return f"export-{uuid(resource_id, field='storage resource ID')}"


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("timestamp is malformed")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp needs a timezone")
    return parsed


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def check_exportable(resource: db.ManagedResource) -> None:
    """Only an active Mongo or Postgres database on its own instance has a snapshot."""
    if resource.resource_type not in {"mongo", "postgres"}:
        raise HttpError(409, "EXPORT_UNSUPPORTED", "only MongoDB and PostgreSQL have snapshots")
    if resource.lifecycle_state != "active" or resource.instance_id is None:
        raise HttpError(409, "EXPORT_UNAVAILABLE", "this database can't be exported right now")


def admit(connection: sqlite3.Connection, resource: db.ManagedResource) -> None:
    """Refuse a snapshot while another one is being prepared anywhere."""
    check_exportable(resource)
    stopped = db.get_unfinished_operation(connection, scope(resource.resource_id))
    if stopped is not None and stopped.status == "recovery_required":
        # A snapshot interrupted mid-way changed nothing; end it and start anew.
        db.mark_failed(
            connection,
            stopped.operation_id,
            "the snapshot stopped before it finished",
            cleanup_state="not_required",
        )
        db.set_operation_dispatch_status(connection, stopped.operation_id, "finished")
    running = connection.execute(
        "SELECT COUNT(*) FROM operation_dispatches WHERE kind = ? AND status IN ('pending','running')",
        (EXPORT_KIND,),
    ).fetchone()[0]
    if running >= EXPORT_CONCURRENCY:
        raise HttpError(
            409,
            "EXPORT_BUSY",
            "another snapshot is being prepared; try again in a few minutes",
            retryable=True,
        )


def _export_result(value: Mapping[str, object], resource: db.ManagedResource) -> dict[str, Any]:
    key, filename, size, digest = (
        value.get("objectKey"),
        value.get("filename"),
        value.get("bytes"),
        value.get("sha256"),
    )
    match = _OBJECT_KEY.fullmatch(key) if isinstance(key, str) else None
    if (
        set(value) != {"objectKey", "filename", "bytes", "sha256", "takenAt"}
        or match is None
        or match.group(1) != resource.resource_id
        or not isinstance(filename, str)
        or not _FILENAME.fullmatch(filename)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= 2 * 1024**3
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
    ):
        raise ValueError("snapshot evidence is malformed")
    taken = _parse_time(value["takenAt"])
    return {
        "objectKey": key,
        "filename": filename,
        "bytes": size,
        "sha256": digest,
        "takenAt": _utc(taken),
    }


def run_export(
    connection: sqlite3.Connection,
    config: Config,
    resource_id: str,
    operation_id: str,
    *,
    helper_caller: HelperCaller,
) -> None:
    """Make one snapshot. A failure ends the operation; nothing needs recovery."""
    resource = db.get_managed_resource(connection, resource_id)
    application = (
        None if resource is None else db.get_application(connection, resource.application_id)
    )
    deadline = operation_deadline(config)
    db.begin_operation(
        connection,
        operation_id=operation_id,
        kind=EXPORT_KIND,
        scope=scope(resource_id),
        phase="exporting",
        deadline_at=wall_deadline(deadline),
        refs={"resource_id": resource_id},
    )
    try:
        if resource is None or application is None:
            raise HttpError(404, "STORAGE_NOT_FOUND", "managed storage does not exist")
        check_exportable(resource)
        assert resource.instance_id is not None
        result = helper_caller(
            config,
            f"storage.{resource.resource_type}.export",
            {
                "applicationId": application.application_id,
                "applicationSlug": application.slug,
                "resourceId": resource.resource_id,
                "resourceName": resource.resource_name,
                "displayLabel": resource.display_label,
                "providerName": resource.provider_name,
                "instanceId": resource.instance_id,
                "deadlineAt": wall_deadline(deadline),
            },
            deadline=deadline,
        )
        export = _export_result(result, resource)
    except Exception as error:
        code = (
            error.code
            if isinstance(error, remote.HelperError) and error.code in _EXPORT_ERRORS
            else "EXPORT_FAILED"
        )
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="export_failed",
            refs={"error_code": code},
            merge_refs=True,
        )
        db.mark_failed(
            connection,
            operation_id,
            "the database snapshot could not be made",
            cleanup_state="not_required",
        )
        return
    db.checkpoint_operation(
        connection, operation_id, phase="exported", refs={"export": export}, merge_refs=True
    )
    db.mark_succeeded(connection, operation_id, cleanup_state="not_required")


def _export_operation(
    connection: sqlite3.Connection, resource_id: str, operation_id: str
) -> tuple[db.Operation | None, db.OperationDispatch | None]:
    operation = db.get_operation(connection, operation_id)
    dispatch = db.get_operation_dispatch(connection, operation_id)
    record = operation or dispatch
    if record is None or record.kind != EXPORT_KIND or record.scope != scope(resource_id):
        raise HttpError(404, "EXPORT_NOT_FOUND", "snapshot does not exist for this storage")
    return operation, dispatch


def status_model(
    connection: sqlite3.Connection, resource_id: str, operation_id: str, *, now: datetime
) -> dict[str, object]:
    operation, dispatch = _export_operation(connection, resource_id, operation_id)
    started = operation.started_at if operation is not None else None
    if started is None and dispatch is not None:
        started = dispatch.created_at
    model: dict[str, object] = {
        "operationId": operation_id,
        "resourceId": resource_id,
        "status": "running",
        "errorCode": None,
        "startedAt": started,
        "export": None,
    }
    if operation is None or operation.status == "running":
        return model
    if operation.status == "succeeded" and isinstance(operation.refs.get("export"), dict):
        export = dict(operation.refs["export"])
        expires = _parse_time(export["takenAt"]) + RETENTION
        if now >= expires:
            model["status"] = "expired"
            return model
        model["status"] = "succeeded"
        model["export"] = {
            **{key: export[key] for key in ("filename", "bytes", "sha256", "takenAt")},
            "expiresAt": _utc(expires),
        }
        return model
    # A failed or interrupted snapshot is simply over; read-only work leaves
    # nothing to recover.
    model["status"] = "failed"
    model["errorCode"] = operation.refs.get("error_code", "EXPORT_FAILED")
    return model


def link(
    connection_factory: Any,
    config: Config,
    resource_id: str,
    operation_id: str,
    *,
    helper_caller: HelperCaller,
) -> dict[str, object]:
    """A fresh short-lived download link for a finished snapshot."""
    now = datetime.now(UTC)
    with connection_factory() as connection:
        model = status_model(connection, resource_id, operation_id, now=now)
        operation = db.get_operation(connection, operation_id)
    if model["status"] == "expired":
        raise HttpError(410, "EXPORT_EXPIRED", "this snapshot was deleted")
    if model["status"] != "succeeded" or operation is None:
        raise HttpError(409, "EXPORT_NOT_READY", "this snapshot has no file to download")
    export = operation.refs["export"]
    try:
        result = helper_caller(
            config,
            "storage.export.link",
            {"objectKey": export["objectKey"]},
            deadline=time.monotonic() + LINK_SECONDS,
        )
    except remote.HelperError as error:
        if error.code == "EXPORT_NOT_FOUND":
            raise HttpError(410, "EXPORT_EXPIRED", "this snapshot was deleted") from None
        raise
    url, expires = result.get("url"), result.get("expiresAt")
    prefix = f"{public_s3_endpoint(config.platform.domain)}/"
    if (
        set(result) != {"url", "expiresAt"}
        or not isinstance(url, str)
        or not url.startswith(prefix)
        or len(url) > 4096
        or any(ord(character) < 33 for character in url)
    ):
        raise remote.ProtocolError("snapshot link is malformed")
    return {
        "url": url,
        "expiresAt": _utc(_parse_time(expires)),
        **{key: export[key] for key in ("filename", "bytes", "sha256", "takenAt")},
    }


def sweep(config: Config, helper_caller: HelperCaller) -> None:
    """Delete snapshots older than a day, even when nobody takes a new one."""
    try:
        helper_caller(config, "storage.export.sweep", {}, deadline=time.monotonic() + 300)
    except Exception:
        logging.getLogger(__name__).warning("staff snapshot sweep failed")
