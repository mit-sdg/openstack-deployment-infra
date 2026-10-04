"""Same-origin credential login, opaque sessions and session-bound CSRF."""

from __future__ import annotations

import hmac
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...controller.http import HttpError, Request, Response
from ..common import MANAGEMENT_REQUESTS, digest, object_body, opaque, utc
from ..config import Config
from ..identity.client import credentials
from . import known_device
from .anonymous import AddressLimits, AnonymousChallenge, client_address_bucket
from .client import ControllerUnavailable, ProjectClient
from .database import Database
from .local_auth_limits import DeviceFailureLimits, KnownAccountLimits
from .staff_policy import ABSOLUTE_SECONDS, ADMIN_IDLE_SECONDS, IDLE_SECONDS


@dataclass
class FailureWindow:
    expires: float
    failures: int = 0
    pending: int = 0


class FailureLimits:
    """Reserve possible failures before contacting identity, in fixed 60s windows."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures: OrderedDict[tuple[str, str], FailureWindow] = OrderedDict()

    def reserve(self, username: str, address: str, now: float) -> FailureWindow:
        key = digest(username), address
        with self.lock:
            for stale in [
                key
                for key, row in self.failures.items()
                if not row.pending and (now >= row.expires or not row.failures)
            ]:
                del self.failures[stale]
            row = self.failures.get(key)
            if row is None or now >= row.expires:
                if key not in self.failures and len(self.failures) >= 4096:
                    # Preserve both outstanding reservations and blocked windows.
                    # Saturation may shed new attempts, never reopen their budget.
                    oldest = next(
                        (
                            key
                            for key, row in self.failures.items()
                            if not row.pending and row.failures < 5
                        ),
                        None,
                    )
                    if oldest is None:
                        raise HttpError(
                            429, "RATE_LIMITED", "Please wait a minute before trying again."
                        )
                    del self.failures[oldest]
                row = FailureWindow(now + 60)
                self.failures[key] = row
            if row.failures + row.pending >= 5:
                raise HttpError(
                    429,
                    "RATE_LIMITED",
                    "Too many unsuccessful attempts. Wait a minute before trying again.",
                )
            row.pending += 1
            self.failures.move_to_end(key)
            return row

    def finish(self, row: FailureWindow, *, failed: bool, succeeded: bool) -> None:
        with self.lock:
            row.pending -= 1
            if failed:
                row.failures += 1
            elif succeeded:
                row.failures = 0


class Auth:
    def __init__(
        self, config: Config, database: Database, clock: Callable[[], float] = time.time
    ) -> None:
        self.config, self.database, self.clock = config, database, clock
        self.anonymous = AnonymousChallenge(config.state_directory, config.portal_origin)
        self.address_limits = AddressLimits(
            config.anonymous_options_per_minute, config.anonymous_starts_per_minute
        )
        self.failures = FailureLimits()
        self.local_limits = AddressLimits(options_per_minute=6, starts_per_minute=12)
        self.step_up_limits = AddressLimits(options_per_minute=12, starts_per_minute=12)
        self.step_up_failures = FailureLimits()
        self.known_limits = AddressLimits(options_per_minute=600, starts_per_minute=12)
        self.known_failures = FailureLimits()
        self.known_accounts = KnownAccountLimits()
        self.device_failures = DeviceFailureLimits()
        self.recognized_options_limits = AddressLimits(options_per_minute=600, starts_per_minute=12)
        self.identity = ProjectClient(
            config.identity_socket, timeout=10, capacity=MANAGEMENT_REQUESTS
        )

    def cookies(self, request: Request) -> dict[str, str]:
        result: dict[str, str] = {}
        for pair in request.headers.get("cookie", "").split(";"):
            name, sep, value = pair.strip().partition("=")
            if name not in {self.config.login_cookie, self.config.session_cookie}:
                continue
            pattern = (
                r"[A-Za-z0-9_-]{43}"
                if name == self.config.session_cookie
                else r"[A-Za-z0-9_.-]{1,160}"
            )
            if not sep or name in result or not re.fullmatch(pattern, value):
                raise HttpError(401, "UNAUTHENTICATED", "Sign in to continue.")
            result[name] = value
        return result

    @staticmethod
    def directive(name: str, value: str = "", lifetime: int = 0) -> dict[str, object]:
        return {"name": name, "value": value, "maxAge": lifetime}

    def portal_origin(self, request: Request) -> None:
        if request.headers.get("origin") != self.config.portal_origin:
            raise HttpError(403, "ORIGIN_REJECTED", "This action requires the portal origin.")

    def read_origin(self, request: Request) -> None:
        if "origin" in request.headers:
            self.portal_origin(request)
        if request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
            raise HttpError(403, "ORIGIN_REJECTED", "This read requires the portal origin.")

    def recognized_browser(self, request: Request) -> bool:
        token = known_device.read(request, self.config.device_cookie)
        if token is None:
            return False
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (token.split(".")[1],)).fetchone()
            user = dict(row) if row else None
            return bool(
                user
                and user["enabled"]
                and user["status"] == "active"
                and known_device.valid(self.anonymous.key, token, user, self.clock())
            )

    def options(self, request: Request) -> Response:
        if request.body is not None or request.query:
            raise HttpError(400, "INVALID_REQUEST", "Sign-in options do not accept fields.")
        now = self.clock()
        if self.recognized_browser(request):
            self.recognized_options_limits.check(request, "options", now)
        else:
            self.address_limits.check(request, "options", now)
        binder = self.cookies(request).get(self.config.login_cookie, "")
        if not self.anonymous.valid(binder, now):
            binder = self.anonymous.issue(now)
        return Response(
            200,
            {
                "data": {
                    "providerLabel": self.config.class_label,
                    "platformName": self.config.platform_name,
                    "csrfToken": self.anonymous.csrf(binder),
                },
                "browser": {"cookies": [self.directive("login", binder, 600)]},
            },
        )

    def anonymous_post(self, request: Request) -> str:
        self.portal_origin(request)
        binder = self.cookies(request).get(self.config.login_cookie, "")
        body = request.body
        if (
            not isinstance(body, dict)
            or not self.anonymous.valid(binder, self.clock())
            or not isinstance(body.get("csrfToken"), str)
            or not hmac.compare_digest(self.anonymous.csrf(binder), body["csrfToken"])
        ):
            raise HttpError(403, "CSRF_REJECTED", "Reload this page and try again.")
        return binder

    def mint(
        self,
        db: sqlite3.Connection,
        user: dict[str, Any],
        request: Request,
        *,
        reauthenticated: bool = False,
        proof_at: float | None = None,
    ) -> dict[str, object]:
        now, session = self.clock(), opaque()
        lifetime = (
            self.config.absolute_seconds
            if user["role"] == "owner"
            else min(ABSOLUTE_SECONDS, self.config.absolute_seconds)
        )
        old = self.cookies(request).get(self.config.session_cookie)
        if old:
            db.execute("DELETE FROM sessions WHERE token=?", (digest(old),))
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
            (
                digest(session),
                user["id"],
                now,
                now,
                now + lifetime,
                user["role"],
                user["generation"],
                min(now, proof_at)
                if reauthenticated and proof_at is not None
                else now
                if reauthenticated
                else 0,
            ),
        )
        db.execute("UPDATE users SET last_login=? WHERE id=?", (now, user["id"]))
        db.execute(
            "INSERT INTO audit(user_id,action,created) VALUES(?,'sign_in',?)", (user["id"], now)
        )
        return {
            "data": {"returnPath": "/admin/accounts" if user["role"] == "admin" else "/apps"},
            "browser": {
                "cookies": [self.directive("login"), self.directive("session", session, lifetime)]
                + (
                    [
                        self.directive(
                            "device",
                            known_device.issue(
                                self.anonymous.key,
                                user,
                                now,
                                previous=known_device.read(request, self.config.device_cookie),
                            ),
                            known_device.LIFETIME,
                        )
                    ]
                    if user["issuer"] == "local"
                    else []
                )
            },
        }

    def login(self, request: Request) -> Response:
        self.anonymous_post(request)
        now = self.clock()
        body = request.body
        required = {"csrfToken", "username", "password"}
        if (
            not isinstance(body, dict)
            or not required <= set(body)
            or set(body) - required - {"method", "totp", "role"}
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid sign-in fields.")
        method = body.get("method", "commons")
        if method not in ("commons", "local"):
            raise HttpError(400, "INVALID_REQUEST", "Invalid sign-in method.")
        verified = None
        if method == "local":
            from .local_auth import authenticate as local_authenticate
            from .local_security import username as local_username

            try:
                name = local_username(body["username"])
                supplied = body["password"]
                if not isinstance(supplied, str) or len(supplied.encode("utf-8")) > 1024:
                    raise ValueError("invalid credentials")
            except (ValueError, UnicodeError):
                raise HttpError(
                    401,
                    "INVALID_CREDENTIALS",
                    "Username, password or authentication code is incorrect.",
                ) from None
            verified = local_authenticate(
                self,
                name,
                supplied,
                body.get("totp"),
                client_address_bucket(request),
                device=known_device.read(request, self.config.device_cookie),
            )
            issuer, subject = "local", verified["subject"]
            profile = {"username": name, "displayName": verified["display_name"]}
        else:
            self.address_limits.check(request, "start", now)
            try:
                checked = credentials({"username": body["username"], "password": body["password"]})
            except ValueError:
                raise HttpError(
                    400, "INVALID_REQUEST", "Username or password exceeds the allowed bounds."
                ) from None
            profile = self.check_identity(checked, client_address_bucket(request), now)
            issuer, subject = self.config.issuer, profile["subject"]
        with self.database.connect(write=True) as db:
            existing = db.execute(
                "SELECT * FROM users WHERE issuer=? AND subject=?", (issuer, subject)
            ).fetchone()
            if existing is not None and (not existing["enabled"] or existing["status"] != "active"):
                raise HttpError(
                    403,
                    "ACCOUNT_DISABLED",
                    "Your portal account is disabled. Contact course staff.",
                )
            if verified is not None and (
                existing is None or existing["generation"] != verified["generation"]
            ):
                raise HttpError(
                    401,
                    "INVALID_CREDENTIALS",
                    "Username, password or authentication code is incorrect.",
                )
            identifier = existing["id"] if existing is not None else str(uuid.uuid4())
            if issuer != "local":
                db.execute(
                    "INSERT INTO users(id,issuer,subject,username,display_name,created,last_login) VALUES(?,?,?,?,?,?,?) ON CONFLICT(issuer,subject) DO UPDATE SET username=excluded.username,display_name=excluded.display_name",
                    (
                        identifier,
                        issuer,
                        subject,
                        profile["username"],
                        profile["displayName"],
                        self.clock(),
                        self.clock(),
                    ),
                )
            row = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise HttpError(401, "INVALID_CREDENTIALS", "Sign-in could not complete.")
            response = self.mint(
                db, dict(row), request, reauthenticated=issuer == "local" and row["role"] == "admin"
            )
        return Response(200, response)

    def check_identity(self, checked: dict[str, str], address: str, now: float) -> dict[str, Any]:
        reservation = self.failures.reserve(checked["username"], address, now)
        failed = succeeded = False
        try:
            try:
                status, result = self.identity.request("POST", "/v1/authenticate", checked)
            except ControllerUnavailable:
                raise HttpError(
                    503,
                    "IDENTITY_UNAVAILABLE",
                    "Class sign-in is temporarily unavailable. Please try again.",
                    retryable=True,
                ) from None
            if status != 200:
                code = result.get("error", {}).get("code")
                if status == 401 and code == "invalid_credentials":
                    failed = True
                    raise HttpError(
                        401, "INVALID_CREDENTIALS", "Username or password is incorrect."
                    )
                if status == 403 and code == "account_disabled":
                    raise HttpError(
                        403,
                        "ACCOUNT_DISABLED",
                        "Your class account is archived. Contact course staff.",
                    )
                if status == 400 and code == "invalid_request":
                    raise HttpError(400, "INVALID_REQUEST", "The sign-in request is invalid.")
                raise HttpError(
                    503,
                    "IDENTITY_UNAVAILABLE",
                    "Class sign-in is temporarily unavailable. Please try again.",
                    retryable=True,
                )
            user = result.get("data")
            if (
                not isinstance(user, dict)
                or set(user) != {"subject", "username", "displayName"}
                or user["username"] != checked["username"]
            ):
                raise HttpError(
                    503,
                    "IDENTITY_UNAVAILABLE",
                    "Class sign-in is temporarily unavailable.",
                    retryable=True,
                )
            succeeded = True
            return user
        finally:
            self.failures.finish(reservation, failed=failed, succeeded=succeeded)

    def session_row(
        self, db: sqlite3.Connection, sid: str, now: float, kind: str | None = None
    ) -> dict[str, Any]:
        row = db.execute(
            "SELECT users.*,sessions.expires,sessions.last_used,sessions.kind,sessions.generation AS session_generation,sessions.reauthenticated_at FROM sessions JOIN users ON user_id=users.id WHERE token=?",
            (sid,),
        ).fetchone()
        idle = self.config.idle_seconds
        if row is not None and row["kind"] != "owner":
            idle = min(ADMIN_IDLE_SECONDS if row["kind"] == "admin" else IDLE_SECONDS, idle)
        if (
            row is None
            or row["expires"] <= now
            or row["last_used"] + idle <= now
            or row["generation"] != row["session_generation"]
            or row["role"] != row["kind"]
        ):
            raise HttpError(401, "SESSION_EXPIRED", "Sign in to continue.")
        if not row["enabled"] or row["status"] != "active":
            raise HttpError(403, "ACCOUNT_DISABLED", "This account is disabled.")
        if row["kind"] == "admin":
            factor = db.execute(
                "SELECT totp_confirmed FROM local_accounts WHERE user_id=?", (row["id"],)
            ).fetchone()
            if row["issuer"] != "local" or factor is None or not factor["totp_confirmed"]:
                raise HttpError(401, "SESSION_EXPIRED", "Sign in to continue.")
        if (
            kind == "staff"
            and row["kind"] not in ("staff", "admin")
            or kind == "admin"
            and row["kind"] != "admin"
        ):
            raise HttpError(403, "ACCESS_DENIED", "This session cannot access this view.")
        return dict(row)

    @staticmethod
    def check_csrf(db: sqlite3.Connection, request: Request, sid: str, now: float) -> None:
        csrf = request.headers.get("x-csrf-token", "")
        found = db.execute(
            "SELECT 1 FROM csrf_tokens WHERE token=? AND session=? AND expires>?",
            (digest(csrf), sid, now),
        ).fetchone()
        if found is None:
            raise HttpError(403, "CSRF_REJECTED", "Refresh this page before trying again.")

    def authenticate(
        self,
        request: Request,
        *,
        mutation: bool = False,
        kind: str | None = None,
        csrf: bool = False,
        touch: bool = True,
    ) -> tuple[dict[str, Any], str]:
        token = self.cookies(request).get(self.config.session_cookie)
        sid, now = digest(token or ""), self.clock()
        with self.database.connect(write=touch) as db:
            user = self.session_row(db, sid, now, kind)
            if mutation:
                self.portal_origin(request)
            if mutation or csrf:
                self.check_csrf(db, request, sid, now)
            if touch:
                db.execute("UPDATE sessions SET last_used=? WHERE token=?", (now, sid))
            return user, sid

    def bootstrap(self, request: Request) -> Response:
        self.read_origin(request)
        user, sid = self.authenticate(request)
        token, now = opaque(), self.clock()
        with self.database.connect(write=True) as db:
            db.execute("DELETE FROM csrf_tokens WHERE expires<=?", (now,))
            db.execute(
                "INSERT INTO csrf_tokens VALUES(?,?,?,?)",
                (digest(token), sid, min(now + 1800, user["expires"]), now),
            )
            db.execute(
                "DELETE FROM csrf_tokens WHERE session=? AND token NOT IN (SELECT token FROM csrf_tokens WHERE session=? ORDER BY created DESC,token LIMIT 10)",
                (sid, sid),
            )
        return Response(
            200,
            {
                "data": {
                    "user": {
                        "id": user["id"],
                        "username": user["username"],
                        "displayName": user["display_name"],
                    },
                    "csrfToken": token,
                    "expiresAt": utc(user["expires"]),
                    "role": user["kind"],
                    "stepUpExpiresAt": utc(user["reauthenticated_at"] + 300)
                    if user["kind"] == "admin"
                    else None,
                    "platformName": self.config.platform_name,
                    "features": ["apps", "deployments", "build-logs", "runtime-logs"]
                    + (["staff-read"] if user["kind"] != "owner" else [])
                    + (["accounts"] if user["kind"] == "admin" else []),
                }
            },
        )

    def logout(self, request: Request) -> Response:
        user, sid = self.authenticate(request, mutation=True)
        object_body(request.body, set())
        with self.database.connect(write=True) as db:
            db.execute("DELETE FROM sessions WHERE token=?", (sid,))
            db.execute(
                "INSERT INTO audit(user_id,action,created) VALUES(?,'sign_out',?)",
                (user["id"], self.clock()),
            )
        return Response(
            200,
            {
                "browser": {
                    "status": 204,
                    "cookies": [
                        self.directive("login"),
                        self.directive("session"),
                        self.directive("device"),
                    ],
                }
            },
        )
