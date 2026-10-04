"""Bounded, audited metadata reads over the broker catalog, never controller-wide reads."""

from __future__ import annotations

import re
import sqlite3
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from ...controller.http import HttpError, Request, Response
from ...validation import ValidationError, slug
from ...validation import uuid as checked_uuid
from ..common import canonical, strict_json, utc
from .anonymous import client_address_bucket
from .client import ControllerUnavailable
from .journal import controller_error_code, deploy_failure_guidance
from .staff_policy import AUDIT_ROWS, AUDIT_SECONDS, RESPONSE_BYTES, public_url

if TYPE_CHECKING:
    from .api import Broker


@dataclass
class Bucket:
    tokens: float
    updated: float
    active: int = 0


class ReadLimits:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users: dict[str, Bucket] = {}
        self.addresses: dict[str, Bucket] = {}
        self.active = 0
        self.denied: dict[str, float] = {}
        self.probes: dict[str, int] = {}
        self.probe_reported_at = 0.0

    def probe(self, code: str, now: float) -> None:
        reason = (
            code
            if code in {"ACCESS_DENIED", "SESSION_EXPIRED", "ACCOUNT_DISABLED", "UNAUTHENTICATED"}
            else "REJECTED"
        )
        with self.lock:
            self.probes[reason] = min(1_000_000, self.probes.get(reason, 0) + 1)
            if now - self.probe_reported_at >= 60:
                print("staff probe_denials=" + canonical(self.probes), file=sys.stderr)
                self.probes.clear()
                self.probe_reported_at = now

    @contextmanager
    def reserve(self, user: str, address: str, now: float) -> Iterator[None]:
        with self.lock:
            for mapping in (self.users, self.addresses):
                expired = [
                    key
                    for key, item in mapping.items()
                    if not item.active and now - item.updated >= 60
                ]
                for key in expired:
                    del mapping[key]
            if (user not in self.users and len(self.users) >= 100) or (
                address not in self.addresses and len(self.addresses) >= 4096
            ):
                raise HttpError(
                    429, "RATE_LIMITED", "Staff reads are temporarily limited.", retryable=True
                )
            person = self.users.setdefault(user, Bucket(10, now))
            network = self.addresses.setdefault(address, Bucket(20, now))
            for item, rate, maximum in ((person, 1, 10), (network, 2, 20)):
                item.tokens = min(maximum, item.tokens + max(0, now - item.updated) * rate)
                item.updated = now
            if person.tokens < 1 or network.tokens < 1 or person.active >= 2 or self.active >= 8:
                raise HttpError(
                    429, "RATE_LIMITED", "Staff reads are temporarily limited.", retryable=True
                )
            person.tokens -= 1
            network.tokens -= 1
            person.active += 1
            network.active += 1
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                person.active -= 1
                network.active -= 1
                self.active -= 1

    def log_denial(self, user: str, now: float) -> bool:
        with self.lock:
            self.denied = {key: value for key, value in self.denied.items() if now - value < 60}
            if user in self.denied or len(self.denied) >= 100:
                return False
            self.denied[user] = now
            return True


ROLES = {"owner", "staff", "admin"}

# Every intent kind the broker records (owner, staff-owned and admin
# mutations). Staff activity shows the kind only, never names or values. The
# portal's activity titles cover exactly this set (owner-portal tests read it).
INTENT_KINDS = frozenset(
    {
        "create_app",
        "save_configuration",
        "deploy",
        "env_set",
        "env_delete",
        "storage_create",
        "storage_verify",
        "storage_rotate",
        "storage_delete",
        "adopt_app",
        "app_enable",
        "app_disable",
    }
)


def enum(value: object, allowed: set[str] | frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def identifier(value: object) -> str:
    try:
        return checked_uuid(value)
    except ValidationError:
        raise ControllerUnavailable("invalid metadata identity") from None


def number(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ControllerUnavailable("invalid metadata number")
    return value


def profile(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ControllerUnavailable("invalid metadata profile")
    return value


def commit(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) else None


class StaffReads:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.limits = ReadLimits()
        self.upstream = threading.BoundedSemaphore(1)

    def query(self, request: Request, route: str) -> None:
        allowed: set[str] = set()
        if route in {
            "/v1/staff/owners",
            "/v1/staff/apps",
            "/v1/staff/operations",
        } or route.endswith("/deployments"):
            allowed |= {"limit", "cursor"}
        if route in {"/v1/staff/apps", "/v1/staff/operations"}:
            allowed.add("ownerId")
        if route == "/v1/staff/owners":
            allowed.add("role")
        if route == "/v1/staff/operations":
            allowed.add("applicationId")
        if (
            request.body is not None
            or set(request.query) - allowed
            or any(len(v) != 1 or not v[0] for v in request.query.values())
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid staff read fields.")
        for key in {"cursor", "ownerId", "applicationId"} & set(request.query):
            checked_uuid(request.query[key][0])
        if "role" in request.query and request.query["role"][0] not in ROLES:
            raise HttpError(400, "INVALID_REQUEST", "Invalid staff read fields.")
        self.page_limit(request)

    @staticmethod
    def page_limit(request: Request) -> int:
        raw = request.query.get("limit", ("25",))[0]
        if not re.fullmatch(r"[0-9]{1,2}", raw) or not 1 <= int(raw) <= 50:
            raise HttpError(400, "INVALID_REQUEST", "Staff page limit must be 1–50.")
        return int(raw)

    def audit(
        self,
        request: Request,
        route: str,
        user: str,
        sid: str,
        correlation: str,
        status: int,
        outcome: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        now = self.broker.auth.clock()
        with self.broker.database.connect(write=True) as db:
            if status == 200:
                self.broker.auth.session_row(db, sid, now, "staff")
            state = db.execute(
                "SELECT row_count,pruned_at FROM staff_read_state WHERE singleton=1"
            ).fetchone()
            if state is None:
                raise ControllerUnavailable("staff audit state missing")
            if now - state["pruned_at"] >= 86400:
                deleted = db.execute(
                    "DELETE FROM staff_read_audit WHERE sequence IN (SELECT sequence FROM staff_read_audit WHERE created<? ORDER BY created,sequence LIMIT 1000)",
                    (now - AUDIT_SECONDS,),
                ).rowcount
                db.execute(
                    "UPDATE staff_read_state SET row_count=row_count-?,pruned_at=? WHERE singleton=1",
                    (deleted, now if deleted < 1000 else state["pruned_at"]),
                )
            count = db.execute(
                "SELECT row_count FROM staff_read_state WHERE singleton=1"
            ).fetchone()[0]
            if count >= AUDIT_ROWS:
                raise ControllerUnavailable("staff audit full")
            targets: list[str | None] = []
            for path_name, query_name in (
                ("owner", "ownerId"),
                ("app", "applicationId"),
                ("deployment", ""),
            ):
                raw = (
                    request.path_parameters.get(path_name)
                    or request.query.get(query_name, (None,))[0]
                )
                try:
                    targets.append(checked_uuid(raw) if raw is not None else None)
                except ValidationError:
                    targets.append(None)
            raw_limit = request.query.get("limit", ("25",))[0]
            page_limit = (
                int(raw_limit)
                if re.fullmatch(r"[0-9]{1,2}", raw_limit) and 1 <= int(raw_limit) <= 50
                else None
            )
            data = result or {}
            db.execute(
                "INSERT INTO staff_read_audit(actor_id,correlation,route,owner_id,app_id,deployment_id,page_limit,cursor_used,result_count,outcome,status,stale,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user,
                    correlation,
                    route,
                    *targets,
                    page_limit,
                    int("cursor" in request.query),
                    len(data["items"])
                    if isinstance(data.get("items"), list)
                    else int(result is not None),
                    outcome,
                    status,
                    int(data.get("stale") is True),
                    now,
                ),
            )
            db.execute("UPDATE staff_read_state SET row_count=row_count+1 WHERE singleton=1")

    def handle(
        self, request: Request, handler: Callable[[Request], dict[str, Any]], route: str
    ) -> Response:
        if request.method != "GET":
            raise HttpError(405, "METHOD_NOT_ALLOWED", "Staff views permit only reads.")
        try:
            user, sid = self.broker.auth.authenticate(request, kind="staff", touch=False)
        except HttpError as failure:
            self.limits.probe(failure.code, self.broker.auth.clock())
            raise
        correlation = str(uuid.uuid4())
        try:
            with self.limits.reserve(
                user["id"], client_address_bucket(request), self.broker.auth.clock()
            ):
                self.broker.auth.read_origin(request)
                self.query(request, route)
                with self.broker.database.connect(write=True) as db:
                    now = self.broker.auth.clock()
                    self.broker.auth.session_row(db, sid, now, "staff")
                    self.broker.auth.check_csrf(db, request, sid, now)
                    db.execute("UPDATE sessions SET last_used=? WHERE token=?", (now, sid))
                result = handler(request)
                if len(canonical({"data": result}).encode()) > RESPONSE_BYTES:
                    raise ControllerUnavailable("staff response size")
                self.audit(request, route, user["id"], sid, correlation, 200, "read", result)
                return Response(200, {"data": result})
        except ValidationError:
            error = HttpError(400, "INVALID_REQUEST", "Invalid staff resource identifier.")
        except HttpError as failure:
            error = failure
        except (ControllerUnavailable, sqlite3.DatabaseError, ValueError, TypeError, KeyError):
            error = HttpError(
                503,
                "STATE_UNAVAILABLE",
                "Staff metadata is temporarily unavailable.",
                retryable=True,
            )
        if error.status != 429 or self.limits.log_denial(user["id"], self.broker.auth.clock()):
            try:
                self.audit(request, route, user["id"], sid, correlation, error.status, error.code)
            except (ControllerUnavailable, sqlite3.DatabaseError):
                error = HttpError(
                    503,
                    "STATE_UNAVAILABLE",
                    "Staff metadata is temporarily unavailable.",
                    retryable=True,
                )
        if error.status in {429, 503}:
            return Response(
                error.status,
                {
                    "error": {
                        "code": error.code,
                        "summary": error.summary,
                        "retryable": True,
                        "retryAfterSeconds": 30,
                        "correlationId": correlation,
                    }
                },
            )
        raise error

    @contextmanager
    def controller_slot(self) -> Iterator[None]:
        if not self.upstream.acquire(blocking=False):
            raise HttpError(
                429, "RATE_LIMITED", "Staff observations are temporarily limited.", retryable=True
            )
        try:
            yield
        finally:
            self.upstream.release()

    def owner_record(self, owner: str) -> dict[str, Any]:
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT id,username,display_name,enabled,role FROM users WHERE id=?", (owner,)
            ).fetchone()
        if row is None:
            raise HttpError(404, "NOT_FOUND", "Owner not found.")
        return self.owner_model(dict(row))

    @staticmethod
    def owner_model(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "ownerId": identifier(row["id"]),
            "username": profile(row["username"], 32),
            "displayName": profile(row["display_name"], 256),
            "portalEnabled": row["enabled"] == 1,
            "role": enum(row["role"], ROLES),
        }

    @staticmethod
    def with_owner(
        model: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        """Adds the owner's name to list rows so staff lists need no per-owner reads."""

        def project(row: dict[str, Any]) -> dict[str, Any]:
            return {
                **model(row),
                "ownerUsername": profile(row["owner_username"], 32),
                "ownerDisplayName": profile(row["owner_display_name"], 256),
            }

        return project

    def app_record(self, app: str) -> dict[str, Any]:
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT a.id,a.user_id,a.slug,a.lifecycle,a.revision,a.created,c.repository FROM apps a LEFT JOIN configurations c ON c.app_id=a.id AND c.revision=a.revision WHERE a.id=?",
                (app,),
            ).fetchone()
        if row is None:
            raise HttpError(404, "NOT_FOUND", "Application not found.")
        if row["lifecycle"] == "deleted" or self.broker.journal.observe_deleted(row["id"]):
            raise HttpError(
                410, "APPLICATION_DELETED", "This application was deleted by an administrator."
            )
        return dict(row)

    @staticmethod
    def catalog_model(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "applicationId": identifier(row["id"]),
            "ownerId": identifier(row["user_id"]),
            "slug": slug(row["slug"]),
            "lifecycleState": enum(row["lifecycle"], {"creating", "ready", "rejected"}),
            "savedRevision": number(row["revision"]),
            "createdAt": utc(row["created"]),
            "repository": public_url(row["repository"]),
        }

    def local_page(self, request: Request, table: str) -> dict[str, Any]:
        sources = {
            "users": (
                "users a",
                "a.id,a.username,a.display_name,a.enabled,a.role",
                self.owner_model,
            ),
            "apps": (
                "apps a LEFT JOIN configurations c ON c.app_id=a.id AND c.revision=a.revision"
                " JOIN users u ON u.id=a.user_id",
                "a.id,a.user_id,a.slug,a.lifecycle,a.revision,a.created,c.repository,"
                "u.username AS owner_username,u.display_name AS owner_display_name",
                self.with_owner(self.catalog_model),
            ),
            "intents": (
                "intents a JOIN users u ON u.id=a.user_id LEFT JOIN apps p ON p.id=a.app_id",
                "a.id,a.user_id,a.app_id,a.kind,a.state,a.operation,a.created,a.updated,"
                "u.username AS owner_username,u.display_name AS owner_display_name,"
                "p.slug AS app_slug",
                self.with_owner(self.activity_model),
            ),
        }
        source, columns, model = sources[table]
        conditions = ["a.lifecycle NOT IN ('rejected','deleted')"] if table == "apps" else ["1=1"]
        parameters: list[object] = []
        owner = request.query.get("ownerId", (None,))[0]
        app = request.query.get("applicationId", (None,))[0]
        role = request.query.get("role", (None,))[0]
        if role is not None:
            conditions.append("a.role=?")
            parameters.append(role)
        if owner is not None:
            self.owner_record(owner)
            conditions.append("a.user_id=?")
            parameters.append(owner)
        if app is not None:
            record = self.app_record(app)
            if owner is not None and record["user_id"] != owner:
                raise HttpError(404, "NOT_FOUND", "Application not found.")
            conditions.append("a.app_id=?")
            parameters.append(app)
        limit = self.page_limit(request)
        with self.broker.database.connect() as db:
            cursor = request.query.get("cursor", (None,))[0]
            if cursor is not None:
                point = db.execute(
                    f"SELECT a.created,a.id FROM {source} WHERE {' AND '.join(condition for condition in conditions if not condition.startswith('a.lifecycle')) or '1=1'} AND a.id=?",
                    (*parameters, cursor),
                ).fetchone()
                if point is None:
                    raise HttpError(400, "INVALID_REQUEST", "Unknown staff page cursor.")
                conditions.append("(a.created<? OR (a.created=? AND a.id<?))")
                parameters.extend([point["created"], point["created"], point["id"]])
            rows = db.execute(
                f"SELECT {columns} FROM {source} WHERE {' AND '.join(conditions)} ORDER BY a.created DESC,a.id DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        projected = [dict(row) for row in rows]
        if table == "apps":
            self.broker.journal.reconcile_page(projected)
        return {
            "items": [model(row) for row in projected[:limit] if row.get("lifecycle") != "deleted"],
            "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            "truncated": len(rows) > limit,
        }

    def owners(self, request: Request) -> dict[str, Any]:
        return self.local_page(request, "users")

    def owner(self, request: Request) -> dict[str, Any]:
        owner = checked_uuid(request.path_parameters["owner"])
        result = self.owner_record(owner)
        quota = self.broker.quota(owner)
        # Admin accounts have no limits: their limit is null.
        result["quota"] = {
            key: {
                field: None
                if field == "limit" and quota[key][field] is None
                else number(quota[key][field])
                for field in ("limit", "used", "reserved")
            }
            for key in ("apps", "concurrentOperations")
        }
        return result

    def apps(self, request: Request) -> dict[str, Any]:
        return self.local_page(request, "apps")

    def app(self, request: Request) -> dict[str, Any]:
        app = self.app_record(checked_uuid(request.path_parameters["app"]))
        result = self.catalog_model(app)
        with self.controller_slot():
            observed = self.broker.app_model(app, cache_seconds=15)
        accepted = observed.get("acceptedDeployment")
        active = observed.get("activeDeploymentId")
        accepted_model = None
        if active is not None:
            identifier(active)
            if not isinstance(accepted, dict) or accepted.get("deploymentId") != active:
                raise ControllerUnavailable("invalid accepted staff evidence")
            accepted_model = {
                "deploymentId": active,
                "sourceCommit": commit(accepted.get("sourceCommit")),
                "acceptedAt": utc(accepted.get("acceptedAt")),
            }
        live = observed.get("health")
        live = live if isinstance(live, dict) else {}
        running = observed.get("desiredRunning") is True
        result.update(
            url=public_url(observed.get("url")),
            desiredRunning=running,
            activeDeploymentId=active,
            acceptedDeployment=accepted_model,
            health={
                "process": "unknown"
                if observed.get("stale") is not False
                else "stopped"
                if not running
                else "healthy"
                if live.get("allocationHealthy") is True
                else "unhealthy"
                if live.get("allocationHealthy") is False
                else "unknown",
                "route": "unknown"
                if observed.get("stale") is not False
                else "healthy"
                if live.get("routeHealthy") is True
                else "unhealthy"
                if live.get("routeHealthy") is False
                else "unknown",
            },
            observedAt=utc(observed.get("observedAt")),
            stale=observed.get("stale") is not False,
        )
        return result

    def deployments(self, request: Request) -> dict[str, Any]:
        app = checked_uuid(request.path_parameters["app"])
        self.app_record(app)
        query = {"limit": str(self.page_limit(request))}
        if "cursor" in request.query:
            query["cursor"] = request.query["cursor"][0]
        body = self.project(f"/v1/applications/{app}/deployments?{urlencode(query)}")
        items = body.get("items")
        if not isinstance(items, list) or len(items) > int(query["limit"]):
            raise ControllerUnavailable("invalid staff deployment page")
        models = [self.deployment_model(item, app) for item in items]
        cursor = body.get("nextCursor")
        truncated = body.get("truncated")
        if (
            type(truncated) is not bool
            or (
                cursor is not None
                and (not models or identifier(cursor) != models[-1]["deploymentId"])
            )
            or truncated != (cursor is not None)
        ):
            raise ControllerUnavailable("invalid staff deployment cursor")
        return {"items": models, "nextCursor": cursor, "truncated": truncated}

    def deployment(self, request: Request) -> dict[str, Any]:
        app = checked_uuid(request.path_parameters["app"])
        self.app_record(app)
        deployment = checked_uuid(request.path_parameters["deployment"])
        body = self.project(f"/v1/deployments/{deployment}")
        if body.get("deploymentId") != deployment:
            raise HttpError(404, "NOT_FOUND", "Deployment not found.")
        return self.deployment_model(body, app)

    def project(self, path: str) -> dict[str, Any]:
        # Never let this adapter become a general project API proxy.
        key = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
        if not re.fullmatch(
            rf"/v1/(?:deployments/{key}|applications/{key}/deployments\?limit=[0-9]{{1,2}}(?:&cursor={key})?)",
            path,
        ):
            raise ControllerUnavailable("unregistered staff project read")
        with self.controller_slot():
            status, body = self.broker.client.request("GET", path)
        if status in {400, 404}:
            raise HttpError(
                status,
                "NOT_FOUND" if status == 404 else "INVALID_REQUEST",
                "Deployment page or resource unavailable.",
            )
        if status != 200:
            raise ControllerUnavailable("staff project read unavailable")
        return body

    @staticmethod
    def deployment_model(body: object, app: str) -> dict[str, Any]:
        if not isinstance(body, dict) or body.get("applicationId") != app:
            raise HttpError(404, "NOT_FOUND", "Deployment not found.")
        return {
            "deploymentId": identifier(body.get("deploymentId")),
            "applicationId": app,
            "status": enum(
                body.get("status"),
                {"queued", "building", "deploying", "succeeded", "failed", "recovery_required"},
            ),
            "repositoryCommit": commit(body.get("repositoryCommit")),
            "configurationRevision": number(body["configurationRevision"])
            if body.get("configurationRevision") is not None
            else None,
            "cleanupState": enum(
                body.get("cleanupState"), {"confirmed", "not_required", "pending"}
            ),
            **{
                key: utc(body.get(key))
                for key in ("requestedAt", "updatedAt", "acceptedAt", "lastHealthyAt")
            },
        }

    def operations(self, request: Request) -> dict[str, Any]:
        return self.local_page(request, "intents")

    @classmethod
    def activity_model(cls, row: dict[str, Any]) -> dict[str, Any]:
        """An operation as a staff activity row, named by its app's slug."""
        return {**cls.operation_model(row), "applicationSlug": slug(row["app_slug"])}

    @staticmethod
    def operation_model(row: dict[str, Any]) -> dict[str, Any]:
        operation = strict_json(row["operation"].encode()) if row["operation"] is not None else {}
        if not isinstance(operation, dict):
            raise ControllerUnavailable("invalid cached staff operation")
        phase = operation.get("phase")
        stages = {
            "queued": "queued",
            "validated": "queued",
            "builder_creating": "building",
            "image_pushed": "deploying",
            "worker_creating": "deploying",
            "worker_ready": "verifying",
            "deployment_healthy": "verifying",
            "accepted": "settled",
            "build_rejected": "settled",
            "startup_interrupted": "recovery",
        }
        state = enum(
            row["state"], {"prepared", "unknown", "accepted", "succeeded", "failed", "blocked"}
        )
        stage = stages.get(phase, "unknown") if isinstance(phase, str) else "unknown"
        if state in {"succeeded", "failed"}:
            stage = "settled"
        if state == "blocked":
            stage = "recovery"
        return {
            "intentId": identifier(row["id"]),
            "applicationId": identifier(row["app_id"]),
            "ownerId": identifier(row["user_id"]),
            "kind": enum(row["kind"], INTENT_KINDS),
            "state": state,
            "stage": stage,
            "cleanupState": enum(
                operation.get("cleanupState"), {"confirmed", "not_required", "pending"}
            ),
            "createdAt": utc(row["created"]),
            "updatedAt": utc(row["updated"]),
            "statusObservedAt": utc(operation.get("updatedAt")),
            "controllerErrorCode": controller_error_code(operation.get("controllerErrorCode")),
            "guidance": deploy_failure_guidance(
                row["kind"],
                row["state"],
                controller_error_code(operation.get("controllerErrorCode")),
                phase,
            ),
            "attention": "recovery_required"
            if state == "blocked"
            else "failed"
            if state == "failed"
            else "awaiting_controller"
            if state == "unknown"
            else "none",
        }
