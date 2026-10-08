"""Durable intent admission and bounded same-key controller reconciliation."""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import Any

from ..common import canonical, strict_json, utc
from .client import ControllerUnavailable, ProjectClient
from .database import Database


def controller_error_code(value: object) -> str | None:
    """Only a bounded public machine code, never upstream free text."""
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else None
    )


BUILD_GUIDANCE = "The build couldn't use this commit; its build output says why. Check that the commit is pushed to GitHub (a private repository needs the app's deploy key), that the repository root contains package.json, each package directory contains its runtime lockfile, and build/start scripts are defined in the root package.json."
HEALTH_GUIDANCE = "Check that the health path returns HTTP 2xx with a body of at most 4 KB; use a small endpoint such as /health rather than a full HTML page."
BUSY_GUIDANCE = "The platform is busy with maintenance. Try again in a few minutes."
RUNTIME_GUIDANCE = "The build couldn't look up the Node.js or Bun version this commit asks for. Try deploying again in a few minutes."


def deploy_failure_guidance(kind: str, state: str, code: object, phase: object) -> str | None:
    """Map known deploy evidence to release-owned text; never parse free text."""
    if kind != "deploy" or state not in {"failed", "blocked"}:
        return None
    code = controller_error_code(code)
    phase = phase if isinstance(phase, str) else None
    if state == "failed" and (code == "PLATFORM_BUSY" or phase == "platform_busy"):
        # The deploy stopped before creating anything; the same commit can be retried.
        return BUSY_GUIDANCE
    if state == "failed" and code == "RUNTIME_UNAVAILABLE":
        # The version lookup failed before any builder existed; retrying may work.
        return RUNTIME_GUIDANCE
    if (
        code in {"BUILD_REJECTED", "BUILD_FAILED", "SOURCE_REJECTED", "INVALID_BUILD_CONFIGURATION"}
        or phase == "build_rejected"
    ):
        return BUILD_GUIDANCE
    if code in {
        "CANDIDATE_UNHEALTHY",
        "PUBLIC_HEALTH_FAILED",
        "PUBLIC_HEALTH_TIMEOUT",
        "HEALTH_CHECK_FAILED",
        "HEALTH_CHECK_TIMEOUT",
        "HEALTH_TIMEOUT",
    }:
        return HEALTH_GUIDANCE
    if code in {"DEADLINE_EXCEEDED", "DEADLINE_EXPIRED"} and phase in {
        "worker_ready",
        "job_submitted",
        "verifying",
    }:
        return HEALTH_GUIDANCE
    return None


def attention_guidance(kind: str, state: str, *, can_resume: bool = True) -> str | None:
    """One inline instruction; the Resume control supplies its location."""
    if state not in {"blocked", "unknown"}:
        return None
    if kind in {"env_set", "env_delete"}:
        return (
            "The person who started this environment edit must enter the value again in Settings."
        )
    change = "deployment" if kind == "deploy" else "change"
    if not can_resume:
        return f"Ask staff to resume this {change}."
    return (
        f"Resume this {change} to finish it."
        if state == "blocked"
        else f"Resume this {change} to check its outcome."
    )


def report_exception(error: Exception, intent_id: str = "recovery-scan") -> None:
    # A dedicated message-only handler never formats exception values/tracebacks,
    # which could contain submitted bodies or credential material.
    logger = logging.getLogger("management.journal")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    logger.error("journal exception=%s intent=%s", type(error).__name__, intent_id)


class Journal:
    def __init__(self, database: Database, client: ProjectClient) -> None:
        self.database, self.client = database, client
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.next_app_scan = 0.0
        self.app_scan_cursor: tuple[float, str] | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, name="management-recovery", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=self.client.timeout + 2)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.reconcile()
            except Exception as error:
                report_exception(error)
            self.wake.wait(0.25)
            self.wake.clear()

    def reconcile(self) -> None:
        now = time.time()
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT intents.id FROM intents JOIN users ON users.id=intents.user_id WHERE intents.state IN ('prepared','unknown','accepted') AND (intents.kind NOT IN ('env_set','env_delete') OR intents.state='accepted') AND next_retry<=? AND lease<=? AND (users.enabled=1 OR intents.state='accepted') ORDER BY intents.created LIMIT 4",
                (now, now),
            ).fetchall()
        for row in rows:
            try:
                self.dispatch(row["id"])
            except Exception as error:
                report_exception(error, row["id"])

        if time.monotonic() >= self.next_app_scan:
            self.next_app_scan = time.monotonic() + 5
            self.reconcile_apps()

    def observe_deleted(self, app_id: str, *, missing: bool = False) -> bool:
        """Only definitive project-not-found evidence retires a known ready app."""
        with self.database.connect() as db:
            app = db.execute("SELECT user_id,lifecycle FROM apps WHERE id=?", (app_id,)).fetchone()
        if app is None or app["lifecycle"] != "ready":
            return app is not None and app["lifecycle"] == "deleted"
        if not missing:
            try:
                status, result = self.client.request(
                    "GET", f"/v1/applications/{app_id}/environment"
                )
            except ControllerUnavailable:
                return False
            error = result.get("error")
            missing = (
                status == 404
                and isinstance(error, dict)
                and error.get("code") == "APPLICATION_NOT_FOUND"
            )
        if not missing:
            return False
        now = time.time()
        with self.database.connect(write=True) as db:
            changed = db.execute(
                "UPDATE apps SET lifecycle='deleted' WHERE id=? AND lifecycle='ready'", (app_id,)
            ).rowcount
            if changed:
                db.execute("UPDATE observations SET updated=0 WHERE app_id=?", (app_id,))
                db.execute(
                    "UPDATE intents SET state='failed',safe_error='This application was deleted by an administrator.',updated=?,lease=0 WHERE app_id=? AND state IN ('prepared','unknown','blocked')",
                    (now, app_id),
                )
                db.execute(
                    "INSERT INTO audit(user_id,app_id,action,created) VALUES(?,?,'app_deleted_by_administrator',?)",
                    (app["user_id"], app_id, now),
                )
        return True

    def reconcile_apps(self) -> None:
        with self.database.connect() as db:
            clause, values = (
                ("", ())
                if self.app_scan_cursor is None
                else (
                    " AND (created>? OR (created=? AND id>?))",
                    (self.app_scan_cursor[0], self.app_scan_cursor[0], self.app_scan_cursor[1]),
                )
            )
            rows = db.execute(
                "SELECT id,created FROM apps WHERE lifecycle='ready'"
                + clause
                + " ORDER BY created,id LIMIT 4",
                values,
            ).fetchall()
        for row in rows:
            self.observe_deleted(row["id"])
        self.app_scan_cursor = (rows[-1]["created"], rows[-1]["id"]) if len(rows) == 4 else None

    def reconcile_page(self, rows: list[dict[str, Any]]) -> None:
        # Cheap metadata reads, not health/provider observations; bound work per
        # list read and let the round-robin background scan settle other pages.
        started = time.monotonic()
        for row in rows[:4]:
            if time.monotonic() - started >= 2:
                break
            if row.get("lifecycle") == "ready" and self.observe_deleted(row["id"]):
                row["lifecycle"] = "deleted"

    def dispatch(self, identifier: str, *, secret_body: object = None) -> None:
        now = time.time()
        with self.database.connect(write=True) as db:
            row = db.execute("SELECT * FROM intents WHERE id=?", (identifier,)).fetchone()
            if (
                row is None
                or (
                    row["state"] not in {"prepared", "unknown", "accepted"}
                    and not (
                        row["kind"] in {"env_set", "env_delete"}
                        and row["state"] == "blocked"
                        and secret_body is not None
                    )
                )
                or row["lease"] > now
            ):
                return
            if (
                row["kind"] in {"env_set", "env_delete"}
                and row["state"] != "accepted"
                and secret_body is None
            ):
                return
            if (
                strict_json(row["body"].encode()).get("_portalAdmin") is True
                or row["kind"] == "default_builder_size"
            ) and row["state"] != "accepted":
                # A reviewed resume grants app authority without changing the
                # original actor, controller key, or stored request body.
                resumed = db.execute(
                    "SELECT user_id FROM audit WHERE intent_id=? AND action='resume' ORDER BY rowid DESC LIMIT 1",
                    (identifier,),
                ).fetchone()
                actor = db.execute(
                    "SELECT role,enabled,status FROM users WHERE id=?",
                    (resumed[0] if resumed is not None else row["user_id"],),
                ).fetchone()
                if (
                    actor is None
                    or actor["role"]
                    not in (
                        {"admin"} if row["kind"] == "default_builder_size" else {"staff", "admin"}
                    )
                    or not actor["enabled"]
                    or actor["status"] != "active"
                ):
                    db.execute(
                        "UPDATE intents SET state='blocked',safe_error='Admin review required.' WHERE id=?",
                        (identifier,),
                    )
                    return
            db.execute(
                "UPDATE intents SET lease=?, attempts=attempts+1 WHERE id=?",
                (now + self.client.timeout + 5, identifier),
            )
            intent = dict(row)
        controller_body = strict_json(intent["body"].encode())
        controller_body.pop("_portalAdmin", None)
        operation_id = intent["operation_id"]
        state = "accepted" if intent["state"] == "accepted" and operation_id else "unknown"
        operation = (
            None if intent["operation"] is None else strict_json(intent["operation"].encode())
        )
        error = None
        try:
            if intent["state"] == "accepted" and operation_id:
                status, result = self.client.request("GET", f"/v1/operations/{operation_id}")
                if (
                    status != 200
                    or result.get("operationId") != operation_id
                    or result.get("status")
                    not in {"running", "succeeded", "failed", "recovery_required"}
                ):
                    raise ControllerUnavailable("invalid operation evidence")
                expected_scope = (
                    "infrastructure"
                    if intent["kind"] == "default_builder_size"
                    else f"app-{intent['app_id']}"
                )
                if result.get("scope") != expected_scope:
                    raise ControllerUnavailable("mismatched operation scope")
                operation = {
                    key: result.get(key)
                    for key in ("operationId", "status", "phase", "cleanupState", "updatedAt")
                }
                if (
                    intent["kind"] in {"deploy", "app_enable"}
                    and result.get("finishing") is True
                    and result.get("phase")
                    in {"deployment_healthy", "predecessor_cleanup", "accepted"}
                ):
                    operation["finishing"] = True
                code = controller_error_code(result.get("errorCode"))
                if code is not None:
                    operation["controllerErrorCode"] = code
                state = (
                    "accepted"
                    if result["status"] == "running"
                    else "blocked"
                    if result["status"] == "recovery_required"
                    else result["status"]
                )
                if state == "blocked":
                    error = attention_guidance(intent["kind"], state)
                if state == "failed":
                    error = (
                        "The controller rejected this operation. Review its status before retrying."
                    )
                if state in {"succeeded", "failed"} and result.get("cleanupState") not in {
                    "confirmed",
                    "not_required",
                }:
                    # An uncertain cleanup is still a held application scope.
                    state = "blocked"
                    error = attention_guidance(intent["kind"], state)
            else:
                status, result = self.client.request(
                    intent["method"],
                    intent["path"],
                    secret_body if intent["kind"] in {"env_set", "env_delete"} else controller_body,
                    intent["controller_key"],
                )
                if status == 201 and intent["kind"] == "create_app":
                    if result.get("applicationId") != intent["app_id"]:
                        raise ControllerUnavailable("mismatched application identity")
                    state = "succeeded"
                elif status == 202:
                    if result.get("operationId") != intent["controller_key"]:
                        raise ControllerUnavailable("mismatched operation identity")
                    operation_id, state = result["operationId"], "accepted"
                elif status in {400, 404, 409, 413, 415, 422}:
                    detail = result.get("error")
                    code = controller_error_code(
                        detail.get("code") if isinstance(detail, dict) else None
                    )
                    # Rejection happens before an operation exists. Keep its
                    # diagnostic in existing JSON without inventing an operation.
                    operation = {"controllerErrorCode": code} if code else None
                    if code in {"RECOVERY_REQUIRED", "UNFINISHED_OPERATION", "OPERATION_CONFLICT"}:
                        state, error = (
                            "blocked",
                            attention_guidance(intent["kind"], "blocked"),
                        )
                    else:
                        state, error = (
                            "failed",
                            "Restart is not available yet. Ask an admin to update the platform."
                            if intent["kind"] == "app_restart" and status == 404
                            else "Sizes are not available yet. Ask an admin to update the platform."
                            if intent["kind"] in {"builder_size", "default_builder_size"}
                            and status == 404
                            else "The request was rejected. The slug may be unavailable or the input invalid.",
                        )
                else:
                    raise ControllerUnavailable("unknown controller admission")
        except ControllerUnavailable as failure:
            report_exception(failure, identifier)
        now = time.time()
        with self.database.connect(write=True) as db:
            db.execute(
                "UPDATE intents SET state=?,operation_id=?,operation=?,safe_error=?,updated=?,next_retry=?,lease=0 WHERE id=?",
                (
                    state,
                    operation_id,
                    None if operation is None else canonical(operation),
                    error,
                    now,
                    now + min(10, 0.5 * 2 ** min(intent["attempts"], 4)),
                    identifier,
                ),
            )
            if intent["kind"] == "create_app":
                if state == "succeeded":
                    db.execute("UPDATE apps SET lifecycle='ready' WHERE id=?", (intent["app_id"],))
                elif state == "failed":
                    db.execute(
                        "UPDATE apps SET lifecycle='rejected' WHERE id=?", (intent["app_id"],)
                    )
            if state in {"succeeded", "failed", "blocked"} and state != intent["state"]:
                db.execute("UPDATE observations SET updated=0 WHERE app_id=?", (intent["app_id"],))
                db.execute(
                    "INSERT INTO audit(user_id,app_id,intent_id,action,created) VALUES(?,?,?,?,?)",
                    (intent["user_id"], intent["app_id"], identifier, state, now),
                )
                if intent["kind"] == "default_builder_size":
                    from .accounts import audit

                    audit(
                        db,
                        intent["user_id"],
                        None,
                        "default_builder_size_" + state,
                        {
                            "intentId": identifier,
                            "flavor": controller_body["flavor"],
                            "expectedFlavor": controller_body["expectedFlavor"],
                        },
                        now,
                    )


def intent_model(
    row: Any, *, diagnostic: bool = False, viewer: str | None = None
) -> dict[str, Any]:
    """A browser view of an intent. A teammate who isn't its actor (viewer) can't
    resubmit it, so they get no retry key."""
    body = strict_json(row["body"].encode())
    operation = None if row["operation"] is None else strict_json(row["operation"].encode())
    code = (
        controller_error_code(operation.pop("controllerErrorCode", None))
        if isinstance(operation, dict)
        else None
    )
    can_resume = (
        row["kind"] not in {"env_set", "env_delete"}
        and row["state"] in {"blocked", "unknown"}
        and (body.get("_portalAdmin") is not True or diagnostic)
    )
    model = {
        "intentId": row["id"],
        "appId": row["app_id"],
        "kind": row["kind"],
        "state": row["state"],
        "operationId": row["operation_id"],
        "operation": operation or None,
        "statusUrl": f"/api/v1/intents/{row['id']}",
        "safeError": attention_guidance(row["kind"], row["state"], can_resume=can_resume)
        or deploy_failure_guidance(
            row["kind"],
            row["state"],
            code,
            operation.get("phase") if isinstance(operation, dict) else None,
        )
        or row["safe_error"],
        "createdAt": utc(row["created"]),
        "updatedAt": utc(row["updated"]),
        "commit": body.get("commit") if row["kind"] == "deploy" else None,
        "names": body.get("names", []) if row["kind"] in {"env_set", "env_delete"} else [],
        "retryKey": row["client_key"]
        if row["kind"] in {"env_set", "env_delete"}
        and row["state"] in {"prepared", "unknown", "blocked"}
        else None,
        "canResume": can_resume,
        "requiresResubmit": row["kind"] in {"env_set", "env_delete"}
        and row["state"] in {"prepared", "unknown", "blocked"},
    }

    if viewer is not None and row["user_id"] != viewer:
        model["retryKey"] = None
        model["requiresResubmit"] = False
    if diagnostic:
        model["controllerErrorCode"] = code
    return model
