"""Local enrollment and admin-only account management; no controller capability."""

from __future__ import annotations

import hmac
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ...controller.http import HttpError, Request, Response
from ...validation import uuid as checked_uuid
from ..common import canonical, digest, object_body, opaque, text, utc
from . import bootstrap, known_device, local_security
from .anonymous import client_address_bucket
from .local_auth import authenticate as local_authenticate

if TYPE_CHECKING:
    from .api import Broker


def audit(
    db: sqlite3.Connection,
    actor: str | None,
    target: str | None,
    action: str,
    details: dict[str, object],
    now: float,
) -> None:
    db.execute(
        "INSERT INTO admin_audit(actor_id,target_id,action,details,created) VALUES(?,?,?,?,?)",
        (actor, target, action, canonical(details), now),
    )


def security_change(db: sqlite3.Connection, user: str) -> None:
    db.execute("UPDATE users SET generation=generation+1 WHERE id=?", (user,))
    db.execute("DELETE FROM sessions WHERE user_id=?", (user,))
    db.execute("DELETE FROM account_tokens WHERE user_id=?", (user,))
    db.execute("DELETE FROM enrollments WHERE user_id=?", (user,))
    db.execute("DELETE FROM authentication_failures WHERE user_id=?", (user,))
    db.execute(
        "UPDATE local_accounts SET totp_streak=0,totp_blocked_until=0 WHERE user_id=?", (user,)
    )


class Accounts:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    def admin(self, request: Request, *, step_up: bool = False) -> tuple[dict[str, Any], str]:
        if request.method == "GET":
            self.broker.auth.read_origin(request)
        user, sid = self.broker.auth.authenticate(
            request, kind="admin", mutation=request.method != "GET", csrf=request.method == "GET"
        )
        if step_up and user["reauthenticated_at"] + 300 <= self.broker.auth.clock():
            raise HttpError(
                403, "STEP_UP_REQUIRED", "Re-enter your password and a fresh authentication code."
            )
        return user, sid

    def checked_actor(
        self, db: sqlite3.Connection, sid: str, *, step_up: bool = False
    ) -> dict[str, Any]:
        actor = self.broker.auth.session_row(db, sid, self.broker.auth.clock(), "admin")
        if step_up and actor["reauthenticated_at"] + 300 <= self.broker.auth.clock():
            raise HttpError(
                403, "STEP_UP_REQUIRED", "Re-enter your password and a fresh authentication code."
            )
        return actor

    @staticmethod
    def target(db: sqlite3.Connection, identifier: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM users WHERE id=?", (checked_uuid(identifier),)).fetchone()
        if row is None:
            raise HttpError(404, "NOT_FOUND", "Account not found.")
        return dict(row)

    @staticmethod
    def link(db: sqlite3.Connection, user: str, purpose: str, origin: str, now: float) -> str:
        token, identifier = opaque(), str(uuid.uuid4())
        generation = db.execute("SELECT generation FROM users WHERE id=?", (user,)).fetchone()[0]
        db.execute(
            "INSERT INTO account_tokens(id,token_hash,user_id,generation,purpose,expires,created) VALUES(?,?,?,?,?,?,?)",
            (
                identifier,
                local_security.token_hash(token),
                user,
                generation,
                purpose,
                now + 72 * 3600,
                now,
            ),
        )
        return origin + "/activate#" + identifier + "." + token

    def resolve_token(self, db: sqlite3.Connection, value: object) -> dict[str, Any]:
        now = self.broker.auth.clock()
        if not isinstance(value, str) or len(value) > 80 or value.count(".") != 1:
            raise ValueError("invalid token")
        identifier, token = value.split(".")
        checked_uuid(identifier)
        if db.execute("SELECT 1 FROM used_tokens WHERE id=?", (identifier,)).fetchone() is not None:
            raise ValueError("consumed token")
        row = db.execute("SELECT * FROM account_tokens WHERE id=?", (identifier,)).fetchone()
        if row is not None:
            user = self.target(db, row["user_id"])
            if (
                row["failures"] >= 5
                or row["expires"] <= now
                or row["generation"] != user["generation"]
                or not hmac.compare_digest(row["token_hash"], local_security.token_hash(token))
                or user["issuer"] != "local"
            ):
                raise ValueError("invalid account token")
            return {**dict(row), "user": user}
        fence = db.execute("SELECT valid_after FROM token_policy WHERE singleton=1").fetchone()[0]
        document = bootstrap.read(self.broker.config, value, now, fence)
        return {
            "id": document["id"],
            "purpose": "bootstrap",
            "expires": document["expires"],
            "user": None,
        }

    def token_info(self, request: Request) -> Response:
        self.broker.auth.anonymous_post(request)
        body = object_body(request.body, {"csrfToken", "token"})
        try:
            with self.broker.database.connect() as db:
                token = self.resolve_token(db, body["token"])
                user = token["user"]
                factor = (
                    db.execute(
                        "SELECT totp_confirmed,password_hash FROM local_accounts WHERE user_id=?",
                        (user["id"],),
                    ).fetchone()
                    if user is not None
                    else None
                )
        except (ValueError, OSError):
            raise HttpError(403, "TOKEN_INVALID", "This link is unavailable or expired.") from None
        return Response(
            200,
            {
                "data": {
                    "purpose": token["purpose"],
                    "username": user["username"] if user else None,
                    "role": user["role"] if user else "admin",
                    "expiresAt": utc(token["expires"]),
                    "totpEnabled": bool(factor and factor["totp_confirmed"]),
                }
            },
        )

    def enroll(self, request: Request) -> Response:
        binder = self.broker.auth.anonymous_post(request)
        self.broker.auth.address_limits.check(request, "start", self.broker.auth.clock())
        if (
            not isinstance(request.body, dict)
            or not {"csrfToken", "token", "password"} <= set(request.body)
            or set(request.body)
            - {
                "csrfToken",
                "token",
                "password",
                "username",
                "displayName",
                "totpEnabled",
                "totp",
                "role",
            }
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid enrollment fields.")
        body = request.body
        try:
            reset_counter = None
            with self.broker.database.connect(write=True) as db:
                token = self.resolve_token(db, body["token"])
                user = token["user"]
                name = (
                    local_security.username(body.get("username"))
                    if user is None
                    else user["username"]
                )
                role = "admin" if user is None else user["role"]
                secret_row = (
                    db.execute(
                        "SELECT totp_secret,totp_confirmed,last_counter FROM local_accounts WHERE user_id=?",
                        (user["id"],),
                    ).fetchone()
                    if user
                    else None
                )
                prior_factor = dict(secret_row) if secret_row is not None else None
                purpose = token["purpose"]
                supplied = (
                    local_security.password(body["password"], name)
                    if purpose != "totp-reset"
                    else None
                )
                if purpose == "password-reset" and prior_factor and prior_factor["totp_confirmed"]:
                    reset_counter = local_security.verify_totp(
                        prior_factor["totp_secret"],
                        body.get("totp"),
                        self.broker.auth.clock(),
                        prior_factor["last_counter"],
                    )
                    if reset_counter is None:
                        # Return inside the transaction so the failure survives.
                        db.execute(
                            "UPDATE account_tokens SET failures=MIN(failures+1,5) WHERE id=?",
                            (token["id"],),
                        )
                        return self.code_failure()
                    db.execute(
                        "UPDATE local_accounts SET last_counter=? WHERE user_id=?",
                        (reset_counter, user["id"]),
                    )
            selected = body.get("totpEnabled", False)
            if type(selected) is not bool:
                raise ValueError("invalid TOTP selection")
            enroll_factor = (
                purpose in {"bootstrap", "totp-reset"}
                or purpose in {"invite", "password-reset"}
                and (role == "admin" or selected)
                and not (
                    purpose == "password-reset" and prior_factor and prior_factor["totp_confirmed"]
                )
            )
            secret = local_security.totp_secret() if enroll_factor else None
            if supplied is not None:
                self.broker.auth.local_limits.check_bucket(
                    client_address_bucket(request), "start", self.broker.auth.clock()
                )
            encoded = local_security.hash_password(supplied) if supplied is not None else None
            handle = opaque()
            with self.broker.database.connect(write=True) as db:
                token = self.resolve_token(db, body["token"])
                user = token["user"]
                if user is None:
                    identifier = str(uuid.uuid4())
                    display = text(body.get("displayName", name), 256)
                    db.execute(
                        "INSERT INTO users(id,issuer,subject,username,display_name,created,last_login,role,status) VALUES(?,'local',?,?,?,?,0,'admin','pending')",
                        (identifier, str(uuid.uuid4()), name, display, self.broker.auth.clock()),
                    )
                    db.execute("INSERT INTO local_accounts(user_id) VALUES(?)", (identifier,))
                    user = self.target(db, identifier)
                if purpose == "password-reset" and prior_factor and prior_factor["totp_confirmed"]:
                    latest = db.execute(
                        "SELECT totp_secret,last_counter FROM local_accounts WHERE user_id=?",
                        (user["id"],),
                    ).fetchone()
                    if (
                        latest["totp_secret"] != prior_factor["totp_secret"]
                        or latest["last_counter"] != reset_counter
                    ):
                        raise HttpError(403, "TOKEN_INVALID", "This enrollment is unavailable.")
                db.execute(
                    "INSERT INTO used_tokens VALUES(?,?,?)",
                    (token["id"], purpose, self.broker.auth.clock()),
                )
                db.execute("DELETE FROM account_tokens WHERE id=?", (token["id"],))
                db.execute("DELETE FROM enrollments WHERE expires<=?", (self.broker.auth.clock(),))
                db.execute(
                    "INSERT INTO enrollments VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        local_security.token_hash(handle),
                        user["id"],
                        user["generation"],
                        digest(binder),
                        purpose,
                        secret,
                        encoded,
                        self.broker.auth.clock() + 600,
                        0,
                    ),
                )
                audit(
                    db,
                    None,
                    user["id"],
                    "enrollment_started",
                    {"purpose": purpose},
                    self.broker.auth.clock(),
                )
        except local_security.HashCapacityError:
            raise HttpError(
                503, "AUTH_UNAVAILABLE", "Enrollment is temporarily unavailable.", retryable=True
            ) from None
        except sqlite3.IntegrityError:
            raise HttpError(409, "ACCOUNT_UNAVAILABLE", "Choose another local username.") from None
        except (ValueError, OSError):
            raise HttpError(
                403, "TOKEN_INVALID", "The link or enrollment fields are invalid."
            ) from None
        return Response(
            200,
            {
                "data": {
                    "enrollmentToken": handle,
                    "totpSecret": secret,
                    "otpauthUri": f"otpauth://totp/Portal:{quote(name)}?secret={secret}&issuer=Portal&algorithm=SHA1&digits=6&period=30"
                    if secret
                    else None,
                }
            },
        )

    @staticmethod
    def code_failure() -> Response:
        return Response(
            401,
            {
                "error": {
                    "code": "INVALID_CREDENTIALS",
                    "summary": "A fresh authentication code is required.",
                    "retryable": False,
                    "correlationId": str(uuid.uuid4()),
                }
            },
        )

    def finish(self, request: Request) -> Response:
        binder = self.broker.auth.anonymous_post(request)
        body = object_body(request.body, {"csrfToken", "enrollmentToken", "totp"})
        try:
            hashed = local_security.token_hash(body["enrollmentToken"])
        except ValueError:
            raise HttpError(403, "TOKEN_INVALID", "This enrollment is unavailable.") from None
        with self.broker.database.connect(write=True) as db:
            row = db.execute("SELECT * FROM enrollments WHERE token_hash=?", (hashed,)).fetchone()
            if (
                row is None
                or row["expires"] <= self.broker.auth.clock()
                or row["failures"] >= 5
                or not hmac.compare_digest(row["binder_hash"], digest(binder))
            ):
                raise HttpError(403, "TOKEN_INVALID", "This enrollment is unavailable or expired.")
            user = self.target(db, row["user_id"])
            if user["generation"] != row["generation"]:
                raise HttpError(403, "TOKEN_INVALID", "This enrollment is unavailable.")
            counter = None
            if row["secret"]:
                counter = local_security.verify_totp(
                    row["secret"], body["totp"], self.broker.auth.clock(), -1
                )
                if counter is None:
                    db.execute(
                        "UPDATE enrollments SET failures=failures+1 WHERE token_hash=?", (hashed,)
                    )
                    return Response(
                        401,
                        {
                            "error": {
                                "code": "INVALID_CREDENTIALS",
                                "summary": "The authentication code is incorrect.",
                                "retryable": False,
                                "correlationId": str(uuid.uuid4()),
                            }
                        },
                    )
            elif body["totp"]:
                factor = db.execute(
                    "SELECT totp_secret,totp_confirmed,last_counter FROM local_accounts WHERE user_id=?",
                    (user["id"],),
                ).fetchone()
                counter = (
                    local_security.verify_totp(
                        factor["totp_secret"],
                        body["totp"],
                        self.broker.auth.clock(),
                        factor["last_counter"],
                    )
                    if factor and factor["totp_confirmed"]
                    else None
                )
                if counter is None:
                    db.execute(
                        "UPDATE enrollments SET failures=failures+1 WHERE token_hash=?", (hashed,)
                    )
                    return self.code_failure()
                db.execute(
                    "UPDATE local_accounts SET last_counter=? WHERE user_id=?",
                    (counter, user["id"]),
                )
            if (
                user["role"] == "admin"
                and row["purpose"] not in ("password-reset",)
                and counter is None
            ):
                raise HttpError(
                    401,
                    "INVALID_CREDENTIALS",
                    "Admin accounts require authentication-code enrollment.",
                )
            if row["password_hash"] is not None:
                db.execute(
                    "UPDATE local_accounts SET password_hash=? WHERE user_id=?",
                    (row["password_hash"], user["id"]),
                )
            if row["secret"]:
                db.execute(
                    "UPDATE local_accounts SET totp_secret=?,totp_confirmed=1,last_counter=? WHERE user_id=?",
                    (row["secret"], counter, user["id"]),
                )
            if row["purpose"] in ("bootstrap", "invite", "totp-reset", "password-reset"):
                db.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
            security_change(db, user["id"])
            audit(
                db,
                None,
                user["id"],
                "enrollment_completed",
                {"purpose": row["purpose"], "generation": user["generation"] + 1},
                self.broker.auth.clock(),
            )
            latest = self.target(db, user["id"])
            if row["purpose"] == "totp-reset" or not latest["enabled"]:
                return Response(200, {"data": {"returnPath": "/sign-in"}})
            response = self.broker.auth.mint(
                db,
                latest,
                request,
                reauthenticated=latest["role"] == "admin" and counter is not None,
                proof_at=self.broker.auth.clock() if counter is not None else None,
            )
        return Response(200, response)

    def reauthenticate(self, request: Request) -> Response:
        user, sid = self.admin(request)
        body = object_body(request.body, {"password", "totp"})
        if not isinstance(body["password"], str) or len(body["password"].encode("utf-8")) > 1024:
            raise HttpError(
                401, "INVALID_CREDENTIALS", "Password or authentication code is incorrect."
            )
        verified = local_authenticate(
            self.broker.auth,
            user["username"],
            body["password"],
            body["totp"],
            client_address_bucket(request),
            step_up=True,
            device=known_device.read(request, self.broker.config.device_cookie),
        )
        with self.broker.database.connect(write=True) as db:
            current = self.broker.auth.session_row(db, sid, self.broker.auth.clock(), "admin")
            if current["generation"] != verified["generation"]:
                raise HttpError(401, "SESSION_EXPIRED", "Sign in to continue.")
            db.execute(
                "UPDATE sessions SET reauthenticated_at=? WHERE token=?",
                (self.broker.auth.clock(), sid),
            )
            audit(db, user["id"], user["id"], "step_up", {}, self.broker.auth.clock())
        return Response(200, {"data": {"stepUpExpiresAt": utc(self.broker.auth.clock() + 300)}})

    def create(self, request: Request) -> Response:
        actor, sid = self.admin(request, step_up=True)
        body = object_body(request.body, {"username", "displayName", "role"})
        try:
            name = local_security.username(body["username"])
            display = text(body["displayName"], 256)
        except ValueError:
            raise HttpError(400, "INVALID_FIELD", "Invalid local account name.") from None
        role = body["role"]
        if role not in ("owner", "staff", "admin"):
            raise HttpError(400, "INVALID_ROLE", "Invalid role.")
        identifier = str(uuid.uuid4())
        try:
            with self.broker.database.connect(write=True) as db:
                self.checked_actor(db, sid, step_up=True)
                db.execute(
                    "INSERT INTO users(id,issuer,subject,username,display_name,created,last_login,role,status) VALUES(?,'local',?,?,?,?,0,?,'pending')",
                    (identifier, str(uuid.uuid4()), name, display, self.broker.auth.clock(), role),
                )
                db.execute("INSERT INTO local_accounts(user_id) VALUES(?)", (identifier,))
                url = self.link(
                    db,
                    identifier,
                    "invite",
                    self.broker.config.portal_origin,
                    self.broker.auth.clock(),
                )
                audit(
                    db,
                    actor["id"],
                    identifier,
                    "account_invited",
                    {"role": role},
                    self.broker.auth.clock(),
                )
        except sqlite3.IntegrityError:
            raise HttpError(
                409, "ACCOUNT_UNAVAILABLE", "This local username is unavailable."
            ) from None
        return Response(201, {"data": {"userId": identifier, "setupUrl": url}})

    def change(self, request: Request) -> Response:
        actor, sid = self.admin(request, step_up=True)
        body = object_body(request.body, {"action", "value"})
        action, value = body["action"], body["value"]
        if action not in (
            "role",
            "enabled",
            "revoke-sessions",
            "password-reset",
            "totp-reset",
            "invite",
        ):
            raise HttpError(400, "INVALID_FIELD", "Unknown account action.")
        result: dict[str, object] = {}
        with self.broker.database.connect(write=True) as db:
            self.checked_actor(db, sid, step_up=True)
            user = self.target(db, request.path_parameters["user"])
            details: dict[str, object] = {}
            if action == "role":
                if (
                    value not in ("owner", "staff", "admin")
                    or value == "admin"
                    and user["issuer"] != "local"
                ):
                    raise HttpError(400, "INVALID_ROLE", "Only a local account can be an admin.")
                db.execute("UPDATE users SET role=? WHERE id=?", (value, user["id"]))
                details = {"previousRole": user["role"], "role": value}
            elif action == "enabled":
                if type(value) is not bool:
                    raise HttpError(400, "INVALID_FIELD", "Enabled must be boolean.")
                db.execute("UPDATE users SET enabled=? WHERE id=?", (int(value), user["id"]))
                details = {"enabled": value}
            elif value is not None:
                raise HttpError(400, "INVALID_FIELD", "This action accepts no value.")
            if action in ("password-reset", "totp-reset", "invite") and user["issuer"] != "local":
                raise HttpError(
                    400, "INVALID_FIELD", "Commons credentials are managed outside the portal."
                )
            if action == "invite" and user["status"] != "pending":
                raise HttpError(400, "INVALID_FIELD", "Only a pending account can be invited.")
            security_change(db, user["id"])
            if action == "password-reset":
                db.execute(
                    "UPDATE local_accounts SET password_hash=NULL WHERE user_id=?", (user["id"],)
                )
            elif action == "totp-reset":
                db.execute(
                    "UPDATE local_accounts SET totp_secret=NULL,totp_confirmed=0,last_counter=-1 WHERE user_id=?",
                    (user["id"],),
                )
            if action in ("password-reset", "totp-reset"):
                # Optional-MFA accounts must not fall back to password-only login.
                db.execute("UPDATE users SET status='pending' WHERE id=?", (user["id"],))
            latest = self.target(db, user["id"])
            factor = db.execute(
                "SELECT totp_confirmed,password_hash FROM local_accounts WHERE user_id=?",
                (user["id"],),
            ).fetchone()
            need_enrollment = latest["role"] == "admin" and not (
                factor and factor["totp_confirmed"]
            )
            if need_enrollment:
                db.execute("UPDATE users SET status='pending' WHERE id=?", (user["id"],))
            purpose = (
                action
                if action in ("password-reset", "totp-reset", "invite")
                else "invite"
                if need_enrollment and factor and not factor["password_hash"]
                else "totp-reset"
                if need_enrollment
                else None
            )
            if purpose:
                result["setupUrl"] = self.link(
                    db,
                    user["id"],
                    purpose,
                    self.broker.config.portal_origin,
                    self.broker.auth.clock(),
                )
            details["generation"] = latest["generation"]
            audit(db, actor["id"], user["id"], action, details, self.broker.auth.clock())
            result["userId"] = user["id"]
        return Response(200, {"data": result})

    def quotas(self, request: Request) -> Response:
        actor, sid = self.admin(request)
        body = object_body(request.body, {"apps", "concurrentOperations"})
        if (
            type(body["apps"]) is not int
            or not 0 <= body["apps"] <= 1000
            or type(body["concurrentOperations"]) is not int
            or not 0 <= body["concurrentOperations"] <= 16
        ):
            raise HttpError(
                400,
                "INVALID_FIELD",
                "Quota limits must be 0–1000 apps and 0–16 concurrent operations.",
            )
        with self.broker.database.connect(write=True) as db:
            self.checked_actor(db, sid)
            user = self.target(db, request.path_parameters["user"])
            db.execute(
                "INSERT INTO quotas VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET apps=excluded.apps,concurrent=excluded.concurrent",
                (user["id"], body["apps"], body["concurrentOperations"]),
            )
            audit(db, actor["id"], user["id"], "quotas", dict(body), self.broker.auth.clock())
        return Response(200, {"data": self.broker.quota(user["id"])})

    def listing(self, request: Request) -> Response:
        self.admin(request)
        if set(request.query) - {"limit", "cursor", "q"} or any(
            len(v) != 1 or not v[0] for v in request.query.values()
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid account page fields.")
        limit = self.broker.staff.page_limit(request)
        search = request.query.get("q", ("",))[0]
        if len(search) > 64 or any(ord(c) < 32 for c in search):
            raise HttpError(400, "INVALID_FIELD", "Account search exceeds its bounds.")
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clause = "(username LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\')"
        args: list[object] = [pattern, pattern]
        with self.broker.database.connect() as db:
            cursor = request.query.get("cursor", (None,))[0]
            if cursor:
                point = db.execute(
                    f"SELECT created,id FROM users WHERE {clause} AND id=?",
                    (*args, checked_uuid(cursor)),
                ).fetchone()
                if point is None:
                    raise HttpError(400, "INVALID_REQUEST", "Unknown account page cursor.")
                clause += " AND (created<? OR (created=? AND id<?))"
                args += [point["created"], point["created"], point["id"]]
            rows = db.execute(
                f"SELECT id,username,display_name,role,enabled,status,last_login,issuer,COALESCE((SELECT apps FROM quotas WHERE user_id=users.id),?) AS app_limit,COALESCE((SELECT concurrent FROM quotas WHERE user_id=users.id),?) AS concurrent_limit,(SELECT COUNT(*) FROM apps WHERE user_id=users.id AND lifecycle!='rejected') AS app_count,(SELECT totp_confirmed FROM local_accounts WHERE user_id=users.id) AS totp_enabled FROM users WHERE {clause} ORDER BY created DESC,id DESC LIMIT ?",
                (
                    self.broker.config.app_limit,
                    self.broker.config.concurrency_limit,
                    *args,
                    limit + 1,
                ),
            ).fetchall()
        items = [
            {
                "userId": row["id"],
                "username": row["username"],
                "displayName": row["display_name"],
                "role": row["role"],
                "enabled": bool(row["enabled"]),
                "status": row["status"],
                "method": "local" if row["issuer"] == "local" else "commons",
                "lastSignIn": utc(row["last_login"]) if row["last_login"] else None,
                "appCount": row["app_count"],
                "appLimit": row["app_limit"],
                "concurrencyLimit": row["concurrent_limit"],
                "totpEnabled": bool(row["totp_enabled"]),
            }
            for row in rows[:limit]
        ]
        return Response(
            200,
            {
                "data": {
                    "items": items,
                    "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None,
                    "truncated": len(rows) > limit,
                }
            },
        )

    def history(self, request: Request) -> Response:
        self.admin(request)
        if set(request.query) - {"limit", "cursor"} or any(
            len(v) != 1 for v in request.query.values()
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid audit page fields.")
        limit = self.broker.staff.page_limit(request)
        cursor = request.query.get("cursor", ("9223372036854775807",))[0]
        if not cursor.isdecimal() or len(cursor) > 19 or not 0 < int(cursor) <= 9223372036854775807:
            raise HttpError(400, "INVALID_REQUEST", "Invalid audit cursor.")
        from ..common import strict_json

        with self.broker.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM admin_audit WHERE sequence<? ORDER BY sequence DESC LIMIT ?",
                (int(cursor), limit + 1),
            ).fetchall()
        return Response(
            200,
            {
                "data": {
                    "items": [
                        {
                            "id": row["sequence"],
                            "actorId": row["actor_id"],
                            "targetId": row["target_id"],
                            "action": row["action"],
                            "details": strict_json(row["details"].encode()),
                            "createdAt": utc(row["created"]),
                        }
                        for row in rows[:limit]
                    ],
                    "nextCursor": str(rows[limit - 1]["sequence"]) if len(rows) > limit else None,
                    "truncated": len(rows) > limit,
                }
            },
        )
