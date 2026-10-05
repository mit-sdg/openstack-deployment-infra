"""Commons Connect and local sign-in, opaque sessions and session-bound CSRF."""

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
from urllib.parse import urlencode

from ...controller.http import HttpError, Request, Response
from ..common import MANAGEMENT_REQUESTS, digest, object_body, opaque, utc
from ..config import Config
from ..identity.client import CONNECT_CODE
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
    """Reserve possible failures before checking a password, in fixed 60s windows."""

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
            if name not in {
                self.config.login_cookie,
                self.config.session_cookie,
                self.config.commons_cookie,
            }:
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

    @staticmethod
    def redirect(location: str, cookies: list[dict[str, object]]) -> Response:
        """Ask web for a 303; it checks the location against what each route may send."""
        return Response(200, {"browser": {"status": 303, "location": location, "cookies": cookies}})

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
    ) -> dict[str, Any]:
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
        body = request.body
        required = {"csrfToken", "username", "password"}
        if (
            not isinstance(body, dict)
            or not required <= set(body)
            or set(body) - required - {"method", "totp", "role"}
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid sign-in fields.")
        method = body.get("method", "local")
        if method == "commons":
            # Class accounts sign in on Commons itself; the portal never takes
            # their passwords. Pages from before that change still send this.
            raise HttpError(
                400,
                "INVALID_REQUEST",
                f"Use “Sign in with your {self.config.class_label}” instead. Reload this page.",
            )
        if method != "local":
            raise HttpError(400, "INVALID_REQUEST", "Invalid sign-in method.")
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
        with self.database.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM users WHERE issuer='local' AND subject=?", (verified["subject"],)
            ).fetchone()
            if row is not None and (not row["enabled"] or row["status"] != "active"):
                raise HttpError(
                    403,
                    "ACCOUNT_DISABLED",
                    "Your portal account is disabled. Contact course staff.",
                )
            if row is None or row["generation"] != verified["generation"]:
                raise HttpError(
                    401,
                    "INVALID_CREDENTIALS",
                    "Username, password or authentication code is incorrect.",
                )
            response = self.mint(db, dict(row), request, reauthenticated=row["role"] == "admin")
        return Response(200, response)

    def commons_start(self, request: Request) -> Response:
        """Bind a fresh state to this browser, then send it to Commons to approve."""
        # Only the portal's own button (or a typed URL) starts sign-in; another
        # site can link to the sign-in page instead.
        if request.headers.get("sec-fetch-site", "none") not in ("same-origin", "none"):
            return self.redirect("/sign-in", [])
        now = self.clock()
        try:
            self.address_limits.check(request, "start", now)
        except HttpError as error:
            if error.status != 429:
                raise
            return self.redirect("/sign-in?error=RATE_LIMITED", [])
        binder = self.anonymous.issue(now, "commons-binder")
        query = urlencode(
            {
                "app": self.config.portal_origin,
                "state": self.anonymous.mac("commons-state", binder),
            }
        )
        return self.redirect(
            f"{self.config.commons_origin}/connect?{query}",
            [self.directive("commons", binder, 600)],
        )

    def commons_callback(self, request: Request) -> Response:
        """Redeem the code Commons approved, only in the browser that asked for it."""
        now, cleared = self.clock(), [self.directive("commons")]
        try:
            self.address_limits.check(request, "start", now)
        except HttpError as error:
            if error.status != 429:
                raise
            return self.redirect("/sign-in?error=RATE_LIMITED", cleared)
        query = {name: values[0] for name, values in request.query.items() if len(values) == 1}
        if len(query) != len(request.query):
            return self.redirect("/sign-in?error=SIGN_IN_EXPIRED", cleared)
        if set(query) == {"error", "state"}:
            # Cancel at Commons. There is no code, so nothing is redeemed.
            return self.redirect("/sign-in?error=COMMONS_CANCELLED", cleared)
        binder = self.cookies(request).get(self.config.commons_cookie, "")
        state = query.get("state", "")
        if (
            set(query) != {"code", "state"}
            or not CONNECT_CODE.fullmatch(query["code"])
            or not re.fullmatch(r"[A-Za-z0-9._~-]{16,256}", state)
            or not self.anonymous.valid(binder, now, "commons-binder")
            or not hmac.compare_digest(self.anonymous.mac("commons-state", binder), state)
        ):
            return self.redirect("/sign-in?error=SIGN_IN_EXPIRED", cleared)
        profile = self.redeem(query["code"])
        if isinstance(profile, str):
            return self.redirect("/sign-in?error=" + profile, cleared)
        issuer, subject = self.config.issuer, profile["subject"]
        with self.database.connect(write=True) as db:
            existing = db.execute(
                "SELECT * FROM users WHERE issuer=? AND subject=?", (issuer, subject)
            ).fetchone()
            if existing is not None and (not existing["enabled"] or existing["status"] != "active"):
                return self.redirect("/sign-in?error=ACCOUNT_DISABLED", cleared)
            identifier = existing["id"] if existing is not None else str(uuid.uuid4())
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
                return self.redirect("/sign-in?error=SIGN_IN_EXPIRED", cleared)
            minted = self.mint(db, dict(row), request)
        return self.redirect(minted["data"]["returnPath"], minted["browser"]["cookies"] + cleared)

    def redeem(self, code: str) -> dict[str, str] | str:
        """Exchange a code through identity; a string is the sign-in page's error."""
        try:
            status, result = self.identity.request(
                "POST", "/v1/redeem", {"code": code, "app": self.config.portal_origin}
            )
        except ControllerUnavailable:
            return "IDENTITY_UNAVAILABLE"
        error = result.get("error")
        if status == 400 and isinstance(error, dict) and error.get("code") == "invalid_code":
            # Used, expired, withdrawn or for another app: Commons doesn't say.
            return "SIGN_IN_EXPIRED"
        user = result.get("data")
        if (
            status != 200
            or not isinstance(user, dict)
            or set(user) != {"subject", "username", "displayName"}
            or not all(isinstance(value, str) for value in user.values())
        ):
            return "IDENTITY_UNAVAILABLE"
        return user

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
