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

from ...controller.http import HttpError, Request, Response
from ...validation import ValidationError, slug
from ...validation import uuid as checked_uuid
from ..common import canonical, strict_json, utc
from .anonymous import client_address_bucket
from .client import ControllerUnavailable
from .journal import attention_guidance, controller_error_code, deploy_failure_guidance
from .staff_policy import AUDIT_ROWS, AUDIT_SECONDS, RESPONSE_BYTES

if TYPE_CHECKING:
    from .api import Broker


@dataclass
class Bucket:
    tokens: float
    updated: float
    active: int = 0


class ReadLimits:
    """Token buckets per person and network address, plus active-read caps.

    burst/rate are a person's reads at once and per second; an address gets twice
    that. Staff use the defaults; admin app pages read more per view.
    """

    def __init__(self, burst: int = 10, rate: float = 1) -> None:
        self.burst, self.rate = burst, rate
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
            person = self.users.setdefault(user, Bucket(self.burst, now))
            network = self.addresses.setdefault(address, Bucket(2 * self.burst, now))
            for item, rate, maximum in (
                (person, self.rate, self.burst),
                (network, 2 * self.rate, 2 * self.burst),
            ):
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
        "builder_size",
        "default_builder_size",
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
        "app_restart",
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


class ClassReads:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.limits = ReadLimits()

    def query(self, request: Request, route: str) -> None:
        allowed: set[str] = set()
        if route in {
            "/v1/people",
            "/v1/activity",
        }:
            allowed |= {"limit", "cursor"}
        if route == "/v1/activity":
            allowed.add("ownerId")
        if route == "/v1/people":
            allowed.add("q")
        if route == "/v1/activity":
            allowed |= {"applicationId", "attention"}
        if (
            request.body is not None
            or set(request.query) - allowed
            or any(len(v) != 1 or not v[0] for v in request.query.values())
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid staff read fields.")
        for key in {"cursor", "ownerId", "applicationId"} & set(request.query):
            checked_uuid(request.query[key][0])
        if "q" in request.query and len(request.query["q"][0]) > 64:
            raise HttpError(400, "INVALID_REQUEST", "Search is too long.")
        if "attention" in request.query and request.query["attention"][0] != "1":
            raise HttpError(400, "INVALID_REQUEST", "Invalid attention filter.")
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

    def owner_record(self, owner: str) -> dict[str, Any]:
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT id,username,display_name,enabled,role,status FROM users WHERE id=?",
                (owner,),
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
            "status": enum(row["status"], {"active", "pending"}),
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
                "SELECT a.id,a.user_id,a.slug,a.lifecycle,a.revision,a.created,c.repository,u.username AS owner_username,u.display_name AS owner_display_name FROM apps a LEFT JOIN configurations c ON c.app_id=a.id AND c.revision=a.revision LEFT JOIN users u ON u.id=a.user_id WHERE a.id=?",
                (app,),
            ).fetchone()
        if row is None:
            raise HttpError(404, "NOT_FOUND", "Application not found.")
        if row["lifecycle"] == "deleted" or self.broker.journal.observe_deleted(row["id"]):
            raise HttpError(
                410, "APPLICATION_DELETED", "This application was deleted by an administrator."
            )
        return dict(row)

    def local_page(self, request: Request, table: str) -> dict[str, Any]:
        sources = {
            "users": (
                "users a",
                "a.id,a.username,a.display_name,a.enabled,a.role,a.status",
                self.owner_model,
            ),
            "intents": (
                "intents a JOIN users u ON u.id=a.user_id LEFT JOIN apps p ON p.id=a.app_id",
                "a.id,a.user_id,a.app_id,a.kind,a.state,a.operation,a.operation_id,a.created,a.updated,"
                "u.username AS owner_username,u.display_name AS owner_display_name,"
                "p.slug AS app_slug",
                self.with_owner(self.activity_model),
            ),
        }
        source, columns, model = sources[table]
        conditions = ["a.app_id IS NOT NULL"] if table == "intents" else ["1=1"]
        parameters: list[object] = []
        owner = request.query.get("ownerId", (None,))[0]
        app = request.query.get("applicationId", (None,))[0]
        search = request.query.get("q", ("",))[0].strip()
        if search:
            pattern = (
                "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            conditions.append(
                "(a.username LIKE ? ESCAPE '\\' OR a.display_name LIKE ? ESCAPE '\\')"
            )
            parameters.extend([pattern, pattern])
        if "attention" in request.query:
            conditions.append("a.state IN ('blocked','unknown')")
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
        # Staff and admin accounts have no limits: their limit is null.
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
            "operationId": row["operation_id"],
            "canResume": row["kind"] not in {"env_set", "env_delete"}
            and state in {"blocked", "unknown"},
            "requiresResubmit": row["kind"] in {"env_set", "env_delete"}
            and state in {"blocked", "unknown"},
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
            "guidance": attention_guidance(row["kind"], state)
            or deploy_failure_guidance(
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
