"""Staff snapshots of an app's database: requests, progress and download links.

Only staff and admins reach these routes. Every request and every issued
download link writes an admin_audit row, and a staff member can request only a
few snapshots of one database an hour.
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ...controller.http import HttpError, Request, Response
from ...validation import uuid as checked_uuid
from .accounts import audit
from .client import ControllerUnavailable

if TYPE_CHECKING:
    import sqlite3

    from .api import Broker

REQUEST_LIMIT = 3
LINK_LIMIT = 30
WINDOW_SECONDS = 3600
# The controller makes a link within 20 s; the web server waits 35 s.
LINK_TIMEOUT_SECONDS = 30
# Request keys are per account, controller keys are global: derive one per pair.
_KEYS = uuid.UUID("5f1bd0a4-3c55-4a7e-9a3e-6c2f4f0f6a51")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_FILENAME = re.compile(r"[a-z0-9][a-z0-9-]{0,100}-[0-9]{8}T[0-9]{6}Z\.zip")
_STATES = {"running": "preparing", "succeeded": "ready", "failed": "failed", "expired": "expired"}
_FAILURES = {
    "EXPORT_TOO_LARGE": "This database is larger than the 2 GiB snapshot limit.",
    "INSTANCE_NOT_READY": "The database didn't answer. Try again in a few minutes.",
    "INSTANCE_MANAGER_UNAVAILABLE": "The database didn't answer. Try again in a few minutes.",
    "DEADLINE_EXPIRED": "The snapshot took too long and was stopped.",
}
_FAILED = "The snapshot couldn't be made. Try again in a few minutes."


def _timestamp(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 40 else None


class Snapshots:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        # Serializes the rate check, controller admission and audit row.
        self.lock = threading.Lock()

    def routes(self) -> list[tuple[str, str, Any]]:
        base = "/v1/apps/{app}/storage/{resource}/snapshots"
        return [
            ("POST", base, self.request),
            ("GET", base + "/{snapshot}", self.status),
            ("POST", base + "/{snapshot}/link", self.link),
        ]

    def database(self, app: dict[str, Any], request: Request) -> dict[str, Any]:
        resource_id = checked_uuid(request.path_parameters["resource"])
        status, resource = self.broker.client.request("GET", f"/v1/storage/{resource_id}")
        if (
            status != 200
            or resource.get("applicationId") != app["id"]
            or resource.get("resourceId") != resource_id
            or resource.get("type") not in {"mongo", "postgres"}
        ):
            raise HttpError(404, "NOT_FOUND", "Database not found for this application.")
        return resource

    @staticmethod
    def recorded(db: sqlite3.Connection, action: str, snapshot: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM admin_audit WHERE action=? AND json_extract(details,'$.snapshotId')=?",
                (action, snapshot),
            ).fetchone()
            is not None
        )

    def recent(
        self, db: sqlite3.Connection, actor: str, action: str, resource: str | None = None
    ) -> int:
        clause, values = (
            ("", ())
            if resource is None
            else (" AND json_extract(details,'$.resourceId')=?", (resource,))
        )
        return int(
            db.execute(
                "SELECT COUNT(*) FROM admin_audit WHERE actor_id=? AND action=? AND created>?"
                + clause,
                (actor, action, self.broker.auth.clock() - WINDOW_SECONDS, *values),
            ).fetchone()[0]
        )

    @staticmethod
    def details(app: dict[str, Any], resource: dict[str, Any], snapshot: str) -> dict[str, object]:
        return {
            "applicationId": app["id"],
            "appSlug": app["slug"],
            "resourceId": resource["resourceId"],
            "resourceType": resource["type"],
            "snapshotId": snapshot,
        }

    def request(self, request: Request) -> Response:
        b = self.broker
        actor, app = b.own(request, mutation=True)
        b.mutation_body(request, set())
        resource = self.database(app, request)
        # A retried request reaches the same controller operation.
        snapshot = str(uuid.uuid5(_KEYS, f"{actor['id']}:{request.idempotency_key()}"))
        with self.lock:
            with b.database.connect() as db:
                if (
                    not self.recorded(db, "storage_snapshot_requested", snapshot)
                    and self.recent(
                        db, actor["id"], "storage_snapshot_requested", resource["resourceId"]
                    )
                    >= REQUEST_LIMIT
                ):
                    raise HttpError(
                        429,
                        "RATE_LIMITED",
                        "You can take three snapshots of a database each hour. Try again later.",
                        retryable=True,
                    )
            status, result = b.client.request(
                "POST", f"/v1/storage/{resource['resourceId']}/exports", {}, snapshot
            )
            if status != 202 or result.get("operationId") != snapshot:
                raise self.refused(status, result)
            with b.database.connect(write=True) as db:
                b.app_management.check(db)
                if not self.recorded(db, "storage_snapshot_requested", snapshot):
                    audit(
                        db,
                        actor["id"],
                        app["user_id"],
                        "storage_snapshot_requested",
                        self.details(app, resource, snapshot),
                        b.auth.clock(),
                    )
        return Response(
            202,
            {
                "data": {
                    "snapshotId": snapshot,
                    "resourceId": resource["resourceId"],
                    "state": "preparing",
                }
            },
        )

    @staticmethod
    def refused(status: int, result: dict[str, Any]) -> HttpError:
        detail = result.get("error")
        code = detail.get("code") if isinstance(detail, dict) else None
        if status == 409 and code == "EXPORT_BUSY":
            return HttpError(
                409,
                "SNAPSHOT_BUSY",
                "Another snapshot is being prepared. Try again in a few minutes.",
                retryable=True,
            )
        if status == 409 and code == "OPERATION_CONFLICT":
            return HttpError(
                409,
                "SNAPSHOT_IN_PROGRESS",
                "A snapshot of this database is already being prepared.",
                retryable=True,
            )
        if status == 409 and code in {"EXPORT_UNAVAILABLE", "EXPORT_UNSUPPORTED"}:
            return HttpError(
                409, "SNAPSHOT_UNAVAILABLE", "This database can't be exported right now."
            )
        if status == 404:
            return HttpError(404, "NOT_FOUND", "Database not found for this application.")
        raise ControllerUnavailable("unknown snapshot admission")

    def status(self, request: Request) -> Response:
        _actor, app = self.broker.own(request)
        resource = self.database(app, request)
        snapshot = checked_uuid(request.path_parameters["snapshot"])
        status, result = self.broker.client.request(
            "GET", f"/v1/storage/{resource['resourceId']}/exports/{snapshot}"
        )
        if status == 404:
            raise HttpError(404, "NOT_FOUND", "Snapshot not found.")
        state = _STATES.get(str(result.get("status")))
        export = result.get("export")
        if status != 200 or result.get("operationId") != snapshot or state is None:
            raise ControllerUnavailable("invalid snapshot observation")
        model: dict[str, object] = {
            "snapshotId": snapshot,
            "resourceId": resource["resourceId"],
            "state": state,
            "startedAt": _timestamp(result.get("startedAt")),
            "filename": None,
            "bytes": None,
            "sha256": None,
            "takenAt": None,
            "expiresAt": None,
            "error": None,
        }
        if state == "ready":
            model.update(self.file(export))
            model["expiresAt"] = (
                _timestamp(export.get("expiresAt")) if isinstance(export, dict) else None
            )
        if state == "failed":
            model["error"] = _FAILURES.get(str(result.get("errorCode")), _FAILED)
        return Response(200, {"data": model})

    @staticmethod
    def file(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ControllerUnavailable("invalid snapshot file")
        filename, size, digest = value.get("filename"), value.get("bytes"), value.get("sha256")
        if (
            not isinstance(filename, str)
            or not _FILENAME.fullmatch(filename)
            or type(size) is not int
            or size <= 0
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or _timestamp(value.get("takenAt")) is None
        ):
            raise ControllerUnavailable("invalid snapshot file")
        return {"filename": filename, "bytes": size, "sha256": digest, "takenAt": value["takenAt"]}

    def link(self, request: Request) -> Response:
        b = self.broker
        actor, app = b.own(request, mutation=True)
        b.mutation_body(request, set())
        resource = self.database(app, request)
        snapshot = checked_uuid(request.path_parameters["snapshot"])
        with b.database.connect() as db:
            if self.recent(db, actor["id"], "storage_snapshot_link_issued") >= LINK_LIMIT:
                raise HttpError(
                    429,
                    "RATE_LIMITED",
                    "Too many downloads in the last hour. Try again later.",
                    retryable=True,
                )
        status, result = b.client.request(
            "POST",
            f"/v1/storage/{resource['resourceId']}/exports/{snapshot}/link",
            {},
            timeout_seconds=LINK_TIMEOUT_SECONDS,
        )
        if status == 404:
            raise HttpError(404, "NOT_FOUND", "Snapshot not found.")
        if status == 409:
            raise HttpError(409, "SNAPSHOT_NOT_READY", "This snapshot isn't ready to download.")
        if status == 410:
            raise HttpError(
                410, "SNAPSHOT_EXPIRED", "Snapshots are deleted after a day. Take a new one."
            )
        url, expires = result.get("url"), _timestamp(result.get("expiresAt"))
        parsed = urlsplit(url) if isinstance(url, str) and len(url) <= 4096 else None
        if status != 200 or parsed is None or parsed.scheme != "https" or expires is None:
            raise ControllerUnavailable("invalid snapshot link")
        file = self.file(result)
        # The link leaves the broker only after its audit row is committed.
        with b.database.connect(write=True) as db:
            b.app_management.check(db)
            audit(
                db,
                actor["id"],
                app["user_id"],
                "storage_snapshot_link_issued",
                {
                    **self.details(app, resource, snapshot),
                    "sha256": file["sha256"],
                    "bytes": file["bytes"],
                    "linkExpiresAt": expires,
                },
                b.auth.clock(),
            )
        return Response(200, {"data": {"url": url, "expiresAt": expires, **file}})
