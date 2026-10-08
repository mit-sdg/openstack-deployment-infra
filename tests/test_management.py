"""Offline evidence for the owner auth, authorization, journal and web boundaries."""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from openstack_platform.controller.http import (
    ControllerServer,
    HttpError,
    PeerPolicy,
    TransportLimits,
)
from openstack_platform.management.broker.api import DEFAULT_CONFIGURATION, Broker
from openstack_platform.management.broker.client import ProjectClient
from openstack_platform.management.broker.database import Database
from openstack_platform.management.common import canonical, opaque
from openstack_platform.management.config import Config
from openstack_platform.management.dev.controller import FakeController
from openstack_platform.management.identity.client import code_challenge
from openstack_platform.management.web.server import NAVIGATIONS, Reply, WebHandler, WebServer

ROOT = Path(__file__).resolve().parents[1]


class ManagementCase(unittest.TestCase):
    identity_transport = False

    @classmethod
    def setUpClass(cls) -> None:
        from openstack_platform.management.broker import local_security

        # Route tests still hash and verify real passwords, at the cheapest
        # supported cost. SecurityTests exercises the production KDF separately.
        cls.dummy_hash = local_security.hash_password("dummy test password", n=8192, p=1)

    def setUp(self) -> None:
        from openstack_platform.management.broker import local_auth, local_security

        self.enterContext(patch.object(local_security, "SCRYPT_N", 8192))
        self.enterContext(patch.object(local_security, "SCRYPT_P", 1))
        self.enterContext(patch.dict(local_security.hash_password.__kwdefaults__, n=8192, p=1))
        self.enterContext(patch.object(local_auth, "DUMMY_HASH", self.dummy_hash))
        self.enterContext(patch.object(local_security, "DUMMY_HASH", self.dummy_hash))
        self.enterContext(patch.object(local_auth, "sleep"))

        self.temporary = tempfile.TemporaryDirectory(prefix="mt-")
        self.root = Path(self.temporary.name)
        self.sockets = self.root / "s"
        self.sockets.mkdir(mode=0o700)
        self.config = Config(
            "http://127.0.0.1:18080",
            "https://localhost:18081",
            self.root / "db",
            self.sockets / "broker.sock",
            self.sockets / "project.sock",
            self.sockets / "identity.sock",
            development=True,
            web_peer=(os.geteuid(), os.getegid()),
            controller_timeout=0.25,
        )
        if self.identity_transport:
            self.start_identity()
        self.fixture = FakeController()
        self.controller = self.fixture.server(self.config.controller_socket)
        self.thread = threading.Thread(
            target=self.controller.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True
        )
        self.thread.start()
        self.broker = Broker(self.config)
        self.router = self.broker.router()
        self.tokens: dict[str, str] = {}
        self.csrf: dict[str, str] = {}

    def start_identity(self) -> None:
        from openstack_platform.management.dev.__main__ import tls_context
        from openstack_platform.management.dev.commons import Commons
        from openstack_platform.management.identity.client import IdentityConfig
        from openstack_platform.management.identity.main import serve as identity_serve

        ca = self.root / "ca.pem"
        self.commons = Commons(("127.0.0.1", 0), self.config, self.root, tls=tls_context(ca))
        self.config = dataclasses.replace(
            self.config, commons_origin=f"https://localhost:{self.commons.server_port}"
        )
        self.commons.config = self.config
        self.commons_thread = threading.Thread(
            target=self.commons.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True
        )
        self.commons_thread.start()
        self.identity = identity_serve(
            IdentityConfig(
                self.config.commons_origin,
                self.config.identity_socket,
                development=True,
                development_ca=ca,
                broker_peer=(os.geteuid(), os.getegid()),
            )
        )
        self.identity_thread = threading.Thread(
            target=self.identity.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True
        )
        self.identity_thread.start()

    def tearDown(self) -> None:
        self.broker.close()
        if self.identity_transport:
            self.identity.shutdown()
            self.identity.server_close()
            self.commons.shutdown()
            self.commons.server_close()
        self.controller.shutdown()
        self.controller.server_close()
        self.temporary.cleanup()

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        owner: str | None = None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        supplied = {
            "origin": self.config.portal_origin,
            "x-portal-client-address": "127.0.0.1",
            **(headers or {}),
        }
        if owner is not None:
            supplied.setdefault("cookie", self.config.session_cookie + "=" + self.tokens[owner])
            supplied.setdefault("x-csrf-token", self.csrf.get(owner, ""))
        if method in {"POST", "PUT"}:
            supplied.setdefault("idempotency-key", key or str(uuid.uuid4()))
        return self.router.dispatch(method, path, supplied, body)

    def start(self, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Begin Commons sign-in; returns the broker's redirect directive."""
        return dict(self.call("GET", "/v1/auth/commons/start", headers=headers).body["browser"])

    def approve(self, location: str) -> str:
        """Follow the redirect to Commons as a browser would; returns the callback target."""
        parsed = urlsplit(location)
        reply = self.commons.handle("GET", parsed.path + "?" + parsed.query, {}, b"")
        callback = urlsplit(dict(reply.headers)["Location"])
        self.assertEqual(
            (f"{callback.scheme}://{callback.netloc}", callback.path),
            (self.config.portal_origin, "/auth/commons/callback"),
        )
        return "/v1/auth/commons/callback?" + callback.query

    def callback(self, target: str, binder: str, cookie: str = "") -> dict[str, Any]:
        """Return to the portal with Commons' answer; returns the broker's redirect."""
        cookies = (self.config.commons_cookie + "=" + binder if binder else "") + cookie
        return dict(self.call("GET", target, headers={"cookie": cookies}).body["browser"])

    def login(self, owner: str = "alice") -> str:
        started = self.start()
        binder = started["cookies"][0]["value"]
        if self.identity_transport:
            self.commons.approve = owner
            completed = self.callback(self.approve(started["location"]), binder)
        else:
            from openstack_platform.management.dev.commons import USERS

            subject, display = USERS[owner]
            target = "/v1/auth/commons/callback?" + urlencode(
                {
                    "code": str(uuid.uuid4()) + "." + opaque(),
                    "state": self.broker.auth.anonymous.mac("commons-state", binder),
                }
            )
            with patch.object(
                self.broker.auth.identity,
                "request",
                return_value=(
                    200,
                    {
                        "data": {
                            "subject": subject,
                            "username": owner,
                            "displayName": display,
                        }
                    },
                ),
            ):
                completed = self.callback(target, binder)
        self.assertEqual(completed["location"], "/apps")
        self.tokens[owner] = next(
            cookie["value"] for cookie in completed["cookies"] if cookie["name"] == "session"
        )
        bootstrap = self.call("GET", "/v1/session", owner=owner).body["data"]
        self.csrf[owner] = bootstrap["csrfToken"]
        return str(bootstrap["user"]["id"])

    def create(self, owner: str = "alice", slug: str = "student-app") -> str:
        result = self.call("POST", "/v1/apps", {"slug": slug}, owner).body["data"]
        return str(result["app"]["applicationId"])

    def save(self, app: str, owner: str = "alice", revision: int = 0) -> None:
        self.call(
            "PUT",
            f"/v1/apps/{app}/configuration",
            {
                "expectedRevision": revision,
                "repository": "https://github.com/example/student-app",
                "branch": "main",
                "configuration": DEFAULT_CONFIGURATION,
            },
            owner,
        )

    def assert_error(self, code: str, callback: Any) -> None:
        with self.assertRaises(HttpError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)


class CeremonyTests(ManagementCase):
    identity_transport = True

    def attempt(
        self, body: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None
    ) -> Any:
        options = self.call("GET", "/v1/auth/options").body
        return self.call(
            "POST",
            "/v1/auth/login",
            {"csrfToken": options["data"]["csrfToken"], **(body or {})},
            headers={
                "cookie": self.config.login_cookie
                + "="
                + options["browser"]["cookies"][0]["value"],
                **(headers or {}),
            },
        )

    def flow(self, owner: str = "alice") -> tuple[str, str]:
        """Start sign-in and have Commons approve; returns the callback and binder."""
        started = self.start()
        self.commons.approve = owner
        return self.approve(started["location"]), started["cookies"][0]["value"]

    def assert_sign_in_error(self, browser: dict[str, Any], code: str) -> None:
        self.assertEqual(browser["location"], "/sign-in?error=" + code)
        self.assertIn({"name": "commons", "value": "", "maxAge": 0}, browser["cookies"])
        self.assertNotIn("session", [cookie["name"] for cookie in browser["cookies"]])

    def test_start_binds_state_to_the_browser_and_sends_it_to_commons(self) -> None:
        started = self.start()
        self.assertEqual(started["status"], 303)
        [cookie] = started["cookies"]
        self.assertEqual((cookie["name"], cookie["maxAge"]), ("commons", 600))
        binder = cookie["value"]
        anonymous = self.broker.auth.anonymous
        state = anonymous.mac("commons-state", binder)
        self.assertRegex(state, r"^[A-Za-z0-9._~-]{16,256}$")
        # PKCE: the verifier is a separate MAC of the binder and never leaves
        # the broker until it redeems; Commons sees only its S256 challenge.
        verifier = self.broker.auth.commons_verifier(binder)
        self.assertRegex(verifier, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotEqual(verifier, state)
        self.assertNotIn(verifier, started["location"])
        self.assertEqual(
            started["location"],
            self.config.commons_origin
            + "/connect?"
            + urlencode(
                {
                    "app": self.config.portal_origin,
                    "state": state,
                    "code_challenge": code_challenge(verifier),
                    "code_challenge_method": "S256",
                }
            ),
        )
        self.assertIn("app=http%3A%2F%2F127.0.0.1%3A18080&", started["location"])
        # The binder is only good for Commons sign-in, and the reverse.
        now = time.time()
        self.assertTrue(anonymous.valid(binder, now, "commons-binder"))
        self.assertFalse(anonymous.valid(binder, now))
        self.assertFalse(anonymous.valid(anonymous.issue(now), now, "commons-binder"))
        self.assertNotEqual(self.start()["cookies"][0]["value"], binder)
        # Only the portal's own page, or a typed address, starts sign-in.
        for site in ("same-origin", "none"):
            self.assertIn("/connect?", self.start({"sec-fetch-site": site})["location"])
        for site in ("cross-site", "same-site"):
            self.assertEqual(
                self.start({"sec-fetch-site": site}),
                {"status": 303, "location": "/sign-in", "cookies": []},
            )
        self.assert_error(
            "INVALID_REQUEST", lambda: self.call("GET", "/v1/auth/commons/start?app=x")
        )
        self.assert_error(
            "METHOD_NOT_ALLOWED", lambda: self.call("POST", "/v1/auth/commons/start", {})
        )
        self.assertEqual(self.commons.calls, 0)

    def test_callback_provisions_the_class_user_and_mints_an_owner_session(self) -> None:
        target, binder = self.flow()
        completed = self.callback(target, binder)
        self.assertEqual((completed["status"], completed["location"]), (303, "/apps"))
        names = [cookie["name"] for cookie in completed["cookies"]]
        # The anonymous binder and Commons state are cleared; no device cookie.
        self.assertEqual(names, ["login", "session", "commons"])
        self.assertEqual(completed["cookies"][2], {"name": "commons", "value": "", "maxAge": 0})
        self.assertEqual(self.commons.calls, 1)
        self.tokens["alice"] = completed["cookies"][1]["value"]
        session = self.call("GET", "/v1/session", owner="alice").body["data"]
        self.assertEqual(
            (session["role"], session["user"]["username"], session["user"]["displayName"]),
            ("owner", "alice", "Alice Student"),
        )
        with self.broker.database.connect() as db:
            user = dict(db.execute("SELECT * FROM users").fetchone())
            audit = [
                row[0]
                for row in db.execute("SELECT action FROM audit WHERE user_id=?", (user["id"],))
            ]
        self.assertEqual(
            (user["issuer"], user["subject"]),
            (self.config.commons_origin, "11111111-1111-4111-8111-111111111111"),
        )
        self.assertEqual(audit, ["sign_in"])
        # A later sign-in keeps the account and takes Commons' current profile.
        from openstack_platform.management.dev.commons import USERS

        with patch.dict(USERS, {"alice": (user["subject"], "Alice Renamed")}):
            self.assertEqual(self.login(), user["id"])
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT display_name FROM users WHERE id=?", (user["id"],)).fetchone()[
                    0
                ],
                "Alice Renamed",
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_callback_refuses_missing_mismatched_or_expired_state_before_redeeming(self) -> None:
        target, binder = self.flow()
        other = self.start()["cookies"][0]["value"]
        code = urlsplit(target).query.split("code=")[1].split("&")[0]
        state = self.broker.auth.anonymous.mac("commons-state", binder)
        login_binder = self.call("GET", "/v1/auth/options").body["browser"]["cookies"][0]["value"]
        for query, cookie in (
            (urlsplit(target).query, ""),
            (urlsplit(target).query, other),
            (urlsplit(target).query, login_binder),
            (urlencode({"code": code}), binder),
            (urlencode({"state": state}), binder),
            (urlencode({"code": "not a code", "state": state}), binder),
            (urlencode({"code": "x" * 129, "state": state}), binder),
            (urlencode({"code": code, "state": "short"}), binder),
            (urlencode({"code": code, "state": "é" * 43}), binder),
            (urlencode([("code", code), ("state", state), ("state", state)]), binder),
            (urlencode({"code": code, "error": "access_denied", "state": state}), binder),
        ):
            with self.subTest(query=query, cookie=cookie[:8]):
                self.assert_sign_in_error(
                    self.callback("/v1/auth/commons/callback?" + query, cookie),
                    "SIGN_IN_EXPIRED",
                )
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.callback(target + "&next=/apps", binder),
        )
        now = time.time() + 601
        self.broker.auth.clock = lambda: now
        self.assert_sign_in_error(self.callback(target, binder), "SIGN_IN_EXPIRED")
        self.assertEqual(self.commons.calls, 0)

    def test_cancel_and_every_refused_code_stop_without_a_session(self) -> None:
        started = self.start()
        binder = started["cookies"][0]["value"]
        self.commons.deny = True
        cancelled = self.approve(started["location"])
        self.assertIn("error=access_denied", cancelled)
        self.assert_sign_in_error(self.callback(cancelled, binder), "COMMONS_CANCELLED")
        self.commons.deny = False
        self.assertEqual(self.commons.calls, 0)
        # A code works once, for this app, within its minute, for active people.
        target, binder = self.flow()
        self.assertEqual(self.callback(target, binder)["location"], "/apps")
        self.assert_sign_in_error(self.callback(target, binder), "SIGN_IN_EXPIRED")
        target, binder = self.flow()
        with self.commons.code_lock:
            for code, (name, app, _expires, challenge) in list(self.commons.codes.items()):
                self.commons.codes[code] = (name, app, time.monotonic() - 1, challenge)
        self.assert_sign_in_error(self.callback(target, binder), "SIGN_IN_EXPIRED")
        target, binder = self.flow()
        with self.commons.code_lock:
            for code, (name, _app, expires, challenge) in list(self.commons.codes.items()):
                self.commons.codes[code] = (name, "https://other.example.com", expires, challenge)
        self.assert_sign_in_error(self.callback(target, binder), "SIGN_IN_EXPIRED")
        target, binder = self.flow("carol")
        self.assert_sign_in_error(self.callback(target, binder), "SIGN_IN_EXPIRED")
        for status, body in (
            (400, b'{"error":"INVALID_REQUEST"}'),
            (401, b'{"error":"UNAUTHORIZED"}'),
            (500, b'{"error":"INTERNAL_ERROR"}'),
        ):
            target, binder = self.flow()
            self.commons.override = (status, body)
            self.assert_sign_in_error(self.callback(target, binder), "IDENTITY_UNAVAILABLE")
        self.commons.override = None
        with self.broker.database.connect() as db:
            self.assertEqual(
                [row[0] for row in db.execute("SELECT username FROM users")], ["alice"]
            )

    def test_disabled_portal_accounts_are_refused_after_commons_approves(self) -> None:
        user = self.login()
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET enabled=0 WHERE id=?", (user,))
        target, binder = self.flow()
        self.assert_sign_in_error(self.callback(target, binder), "ACCOUNT_DISABLED")
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET enabled=1,status='pending' WHERE id=?", (user,))
        target, binder = self.flow()
        self.assert_sign_in_error(self.callback(target, binder), "ACCOUNT_DISABLED")

    def test_csrf_and_exact_origin_reject_before_any_password_check(self) -> None:
        body = {"method": "local", "username": "alice", "password": "incorrect"}
        for origin in ("null", "", "https://evil.example.com"):
            self.assert_error(
                "ORIGIN_REJECTED",
                lambda origin=origin: self.attempt(body, headers={"origin": origin}),
            )
        self.assert_error(
            "CSRF_REJECTED",
            lambda: self.call("POST", "/v1/auth/login", {"csrfToken": "bad", **body}),
        )
        for route in ("start", "assertion", "complete"):
            self.assert_error(
                "NOT_FOUND", lambda route=route: self.call("POST", "/v1/auth/" + route, {})
            )

    def test_identity_outage_preserves_existing_sessions_and_only_blocks_new_login(self) -> None:
        user = self.login()
        self.identity.shutdown()
        self.identity.server_close()
        self.assertEqual(
            self.call("GET", "/v1/session", owner="alice").body["data"]["user"]["id"], user
        )
        self.assertEqual(self.call("GET", "/v1/apps", owner="alice").status, 200)
        target, binder = self.flow("bob")
        self.assert_sign_in_error(self.callback(target, binder), "IDENTITY_UNAVAILABLE")

    def test_start_and_callback_share_the_per_address_start_limit(self) -> None:
        now = time.time()
        self.broker.auth.clock = lambda: now
        self.broker.auth.address_limits.limits = {"options": 600, "start": 3}
        target, binder = self.flow()
        self.assertEqual(self.callback(target, binder)["location"], "/apps")
        started = self.start()
        self.assertEqual(
            self.start(), {"status": 303, "location": "/sign-in?error=RATE_LIMITED", "cookies": []}
        )
        calls = self.commons.calls
        self.assert_sign_in_error(
            self.callback(self.approve(started["location"]), started["cookies"][0]["value"]),
            "RATE_LIMITED",
        )
        self.assertEqual(self.commons.calls, calls)
        other = {"x-portal-client-address": "192.0.2.1"}
        self.assertIn("/connect?", self.start(other)["location"])
        now += 60
        self.assertIn("/connect?", self.start()["location"])

    def test_rotation_commons_archive_and_local_revoke(self) -> None:
        user = self.login()
        first = self.tokens["alice"]
        # Signing in again replaces the session this browser already had.
        target, binder = self.flow()
        completed = self.callback(target, binder, "; " + self.config.session_cookie + "=" + first)
        self.tokens["alice"] = next(
            cookie["value"] for cookie in completed["cookies"] if cookie["name"] == "session"
        )
        self.assert_error(
            "SESSION_EXPIRED",
            lambda: self.call(
                "GET", "/v1/session", headers={"cookie": self.config.session_cookie + "=" + first}
            ),
        )
        # Archiving in Commons stops new sign-ins but not this session.
        from openstack_platform.management.dev import commons

        with patch.object(commons, "ARCHIVED", {"alice"}):
            target, binder = self.flow()
            self.assert_sign_in_error(self.callback(target, binder), "SIGN_IN_EXPIRED")
        self.assertEqual(self.call("GET", "/v1/session", owner="alice").status, 200)
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET enabled=0 WHERE id=?", (user,))
        self.assert_error(
            "ACCOUNT_DISABLED", lambda: self.call("GET", "/v1/session", owner="alice")
        )

    def test_codes_and_state_are_absent_from_persistent_state_logs_and_responses(self) -> None:
        import contextlib
        import io

        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            target, binder = self.flow()
            completed = self.callback(target, binder)
            failed, failed_binder = self.flow()
            self.commons.override = (500, b'{"error":"INTERNAL_ERROR"}')
            refused = self.callback(failed, failed_binder)
        self.assertIn("identity-check=unavailable", output.getvalue())
        secrets = [
            value.split("=", 1)[1]
            for query in (urlsplit(target).query, urlsplit(failed).query)
            for value in query.split("&")
        ]
        self.assertEqual(len(secrets), 4)
        for secret in secrets:
            self.assertNotIn(secret, canonical([completed, refused]) + output.getvalue())
            with self.broker.database.connect() as db:
                for table in ("users", "sessions", "audit", "intents"):
                    self.assertNotIn(
                        secret,
                        canonical([list(row) for row in db.execute("SELECT * FROM " + table)]),
                    )
            for path in self.config.state_directory.iterdir():
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())


class OwnerIntentTests(ManagementCase):
    def test_controller_rejection_codes_are_durable_bounded_and_role_scoped(self) -> None:
        from openstack_platform.management.broker.accounts import security_change
        from openstack_platform.management.common import strict_json

        self.login()
        app = self.create()
        self.save(app)
        for code, expected in (
            ("INVALID_REQUEST", "INVALID_REQUEST"),
            ("NOT_FOUND", "NOT_FOUND"),
            ("free text PRIVATE_VALUE", None),
            ("A" * 65, None),
            (123, None),
        ):
            with self.subTest(code=code):
                original = self.broker.client.request

                def reject(method, path, *args, code=code, original=original, **kwargs):
                    if method == "POST" and path.endswith("/deployments"):
                        return 400, {
                            "error": {"code": code, "summary": "PRIVATE_CONTROLLER_SUMMARY"}
                        }
                    return original(method, path, *args, **kwargs)

                with patch.object(self.broker.client, "request", side_effect=reject):
                    intent = self.call(
                        "POST",
                        f"/v1/apps/{app}/deployments",
                        {"commit": "a" * 40, "configurationRevision": 1},
                        "alice",
                    ).body["data"]
                self.assertEqual(intent["state"], "failed")
                self.assertNotIn("controllerErrorCode", intent)
                self.assertNotIn("PRIVATE_CONTROLLER_SUMMARY", canonical(intent))
                self.assertNotIn("INVALID_REQUEST", canonical(intent))
                with self.broker.database.connect() as db:
                    row = db.execute(
                        "SELECT operation FROM intents WHERE id=?", (intent["intentId"],)
                    ).fetchone()
                    stored = strict_json(row[0].encode()) if row[0] else {}
                self.assertEqual(stored.get("controllerErrorCode"), expected)
                user = self.call("GET", "/v1/session", owner="alice").body["data"]["user"]["id"]
                with self.broker.database.connect(write=True) as db:
                    db.execute("UPDATE users SET role='staff' WHERE id=?", (user,))
                    security_change(db, user)
                self.login()
                detail = self.call("GET", f"/v1/intents/{intent['intentId']}", owner="alice").body[
                    "data"
                ]
                self.assertEqual(detail["controllerErrorCode"], expected)
                operations = self.call("GET", "/v1/activity", owner="alice").body["data"]["items"]
                self.assertEqual(
                    next(item for item in operations if item["intentId"] == intent["intentId"])[
                        "controllerErrorCode"
                    ],
                    expected,
                )
                with self.broker.database.connect(write=True) as db:
                    db.execute("UPDATE users SET role='owner' WHERE id=?", (user,))
                    security_change(db, user)
                self.login()

    def test_history_and_log_invalid_queries_are_rejected_before_controller_calls(self) -> None:
        self.login()
        app = self.create()
        before = len(self.fixture.calls)
        for query in ("limit=0", "limit=101", "limit=bad", "cursor=not-a-uuid", "cursor="):
            self.assert_error(
                "INVALID_QUERY",
                lambda query=query: self.call(
                    "GET", f"/v1/apps/{app}/deployments?{query}", owner="alice"
                ),
            )
        for query in ("lines=0", "lines=1001", "offset=-1", "offset=1048577"):
            self.assert_error(
                "INVALID_REQUEST",
                lambda query=query: self.call(
                    "GET",
                    f"/v1/apps/{app}/deployments/{uuid.uuid4()}/build-log?{query}",
                    owner="alice",
                ),
            )
        self.assertEqual(len(self.fixture.calls), before)

    def test_journal_diagnostics_do_not_include_exception_message_or_body(self) -> None:
        import io
        from contextlib import redirect_stderr

        from openstack_platform.management.broker import journal

        output = io.StringIO()
        # Use a dedicated handler so the capture tests actual stderr formatting.
        logger = journal.logging.getLogger("management.journal")
        old_handlers = logger.handlers[:]
        logger.handlers.clear()
        try:
            with redirect_stderr(output):
                journal.report_exception(ValueError("body-sentinel-not-for-logs"), "intent-safe-id")
        finally:
            logger.handlers = old_handlers
        self.assertIn("ValueError", output.getvalue())
        self.assertIn("intent-safe-id", output.getvalue())
        self.assertNotIn("body-sentinel-not-for-logs", output.getvalue())

    def test_bounded_owner_pagination_and_foreign_cursor(self) -> None:
        self.login()
        self.create(slug="first-project")
        self.create(slug="second-project")
        first = self.call("GET", "/v1/intents?limit=1", owner="alice").body["data"]
        self.assertEqual(len(first["items"]), 1)
        self.assertTrue(first["truncated"])
        second = self.call(
            "GET", "/v1/intents?limit=1&cursor=" + first["nextCursor"], owner="alice"
        ).body["data"]
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["intentId"], second["items"][0]["intentId"])
        self.login("bob")
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.call("GET", "/v1/intents?cursor=" + first["nextCursor"], owner="bob"),
        )

    def test_two_owners_cannot_read_or_mutate_other_apps_and_no_controller_call(self) -> None:
        self.login("alice")
        self.login("bob")
        app = self.create()
        calls = len(self.fixture.calls)
        for method, path, body in (
            ("GET", f"/v1/apps/{app}", None),
            ("GET", f"/v1/apps/{app}/configuration", None),
            ("PUT", f"/v1/apps/{app}/configuration", {}),
            ("POST", f"/v1/apps/{app}/deployments", {}),
            ("GET", f"/v1/apps/{app}/deployments", None),
            ("GET", f"/v1/apps/{app}/deployments/{uuid.uuid4()}", None),
            ("GET", f"/v1/apps/{app}/deployments/{uuid.uuid4()}/build-log", None),
        ):
            self.assert_error(
                "NOT_FOUND", lambda m=method, p=path, b=body: self.call(m, p, b, "bob")
            )
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertEqual(self.call("GET", "/v1/apps", owner="bob").body["data"]["items"], [])

    def test_quota_race_and_slug_collision(self) -> None:
        user = self.login()
        with self.broker.database.connect(write=True) as db:
            db.execute("INSERT INTO quotas VALUES(?,1,1)", (user,))
        barrier = threading.Barrier(2)
        results: list[str] = []

        def create(number: int) -> None:
            barrier.wait()
            try:
                self.create(slug=f"student-app-{number}")
                results.append("created")
            except HttpError as error:
                results.append(error.code)

        threads = [threading.Thread(target=create, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(results, ["created", "QUOTA_EXCEEDED"])
        self.assertEqual(len(self.fixture.apps), 1)

    def test_configuration_cas_idempotency_and_separate_controller_keys(self) -> None:
        self.login()
        key = str(uuid.uuid4())
        first = self.call("POST", "/v1/apps", {"slug": "an-app"}, "alice", key).body["data"]
        second = self.call("POST", "/v1/apps", {"slug": "an-app"}, "alice", key).body["data"]
        app = first["app"]["applicationId"]
        self.assertEqual(app, second["app"]["applicationId"])
        self.assertNotEqual(app, key)
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.call("POST", "/v1/apps", {"slug": "other-app"}, "alice", key),
        )
        self.save(app)
        self.assert_error("REVISION_CONFLICT", lambda: self.save(app))
        self.assertEqual(
            self.call("GET", f"/v1/apps/{app}/configuration", owner="alice").body["data"][
                "revision"
            ],
            1,
        )

    def test_lost_response_replays_same_key_snapshot_and_operation_poll(self) -> None:
        self.login()
        app = self.create()
        self.save(app)
        self.fixture.drop_next = True
        result = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "a" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        identifier = result["intentId"]
        self.assertEqual(result["state"], "unknown")
        self.save(app, revision=1)
        self.broker.journal.dispatch(identifier)
        self.assertEqual(len(self.fixture.deployments), 1)
        self.assertEqual(next(iter(self.fixture.deployments.values()))["configurationRevision"], 1)
        for operation in self.fixture.operations.values():
            operation["ready"] = 0
        self.broker.journal.dispatch(identifier)
        finished = self.call("GET", f"/v1/intents/{identifier}", owner="alice").body["data"]
        self.assertEqual(finished["state"], "succeeded")
        writes = [
            key
            for method, path, key in self.fixture.calls
            if method == "POST" and path.endswith("/deployments")
        ]
        self.assertEqual(len(set(writes)), 1)
        self.login("bob")
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/intents/{identifier}", owner="bob")
        )
        self.assert_error(
            "NOT_FOUND", lambda: self.call("POST", f"/v1/intents/{identifier}/resume", {}, "bob")
        )

    def test_recovery_required_resume_preserves_key_and_prior_accepted_pointer(self) -> None:
        self.login()
        app = self.create()
        self.save(app)
        self.fixture.recovery_next = True
        intent = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "1" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        for operation in self.fixture.operations.values():
            operation["ready"] = 0
        self.broker.journal.dispatch(intent["intentId"])
        self.assertEqual(
            self.call("GET", f"/v1/intents/{intent['intentId']}", owner="alice").body["data"][
                "state"
            ],
            "blocked",
        )
        self.assert_error(
            "APP_BUSY",
            lambda: self.call(
                "POST",
                f"/v1/apps/{app}/deployments",
                {"commit": "2" * 40, "configurationRevision": 1},
                "alice",
            ),
        )
        resumed = self.call("POST", f"/v1/intents/{intent['intentId']}/resume", {}, "alice").body[
            "data"
        ]
        self.assertEqual(resumed["operationId"], intent["operationId"])
        for operation in self.fixture.operations.values():
            operation["ready"] = 0
        self.broker.journal.dispatch(intent["intentId"])
        active = self.fixture.apps[app]["activeDeploymentId"]
        self.fixture.failed_next = True
        failed = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "2" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        for operation in self.fixture.operations.values():
            operation["ready"] = 0
        self.broker.journal.dispatch(failed["intentId"])
        self.assertEqual(self.fixture.apps[app]["activeDeploymentId"], active)

    def test_restart_reconciles_prepared_intent_and_unknown_schema_rejected(self) -> None:
        self.login()
        app = self.create()
        self.save(app)
        original = self.broker.journal.dispatch
        self.broker.journal.dispatch = lambda _identifier: None  # type: ignore[method-assign]
        intent = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "3" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        self.broker.journal.dispatch = original  # type: ignore[method-assign]
        restarted = Broker(self.config)
        restarted.journal.reconcile()
        self.assertEqual(len(self.fixture.deployments), 1)
        self.assertEqual(
            restarted.intent_response(
                intent["intentId"],
                self.call("GET", "/v1/session", owner="alice").body["data"]["user"]["id"],
            ).body["data"]["state"],
            "accepted",
        )
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE metadata SET version=99")
        with self.assertRaises(ValueError):
            Database(self.config)


class WebTransportTests(ManagementCase):
    identity_transport = True

    def test_production_requires_ingress_peer_and_single_configured_address(self) -> None:
        from unittest import mock

        self.web.config = dataclasses.replace(
            self.config,
            development=False,
            trusted_ingress_peers=("192.0.2.10",),
            client_address_header="cf-connecting-ip",
        )
        with mock.patch.object(
            self.web.broker, "request", return_value=(200, {"data": {}})
        ) as request:
            refused = self.web.forward(
                "GET",
                "/auth/options",
                "",
                {"_peer_address": "192.0.2.11", "cf-connecting-ip": "198.51.100.10"},
                b"",
            )
            self.assertEqual(refused.status, 403)
            self.assertEqual(request.call_count, 0)
            accepted = self.web.forward(
                "GET",
                "/auth/options",
                "",
                {"_peer_address": "192.0.2.10", "cf-connecting-ip": "198.51.100.10"},
                b"",
            )
            self.assertEqual(accepted.status, 200)
            self.assertEqual(
                request.call_args.kwargs["headers"]["x-portal-client-address"], "198.51.100.10"
            )
            accepted = self.web.forward(
                "GET",
                "/auth/options",
                "",
                {
                    "_peer_address": "192.0.2.10",
                    "cf-connecting-ip": "198.51.100.10",
                    "x-forwarded-for": "evil, 127.0.0.1",
                    "x-real-ip": "127.0.0.1",
                },
                b"",
            )
            self.assertEqual(accepted.status, 200)
            self.assertEqual(
                request.call_args.kwargs["headers"]["x-portal-client-address"], "198.51.100.10"
            )
            for value in (None, "invalid", "198.51.100.10, 192.0.2.1"):
                headers = {"_peer_address": "192.0.2.10"}
                if value is not None:
                    headers["cf-connecting-ip"] = value
                self.assertEqual(
                    self.web.forward("GET", "/auth/options", "", headers, b"").status, 400
                )

    def setUp(self) -> None:
        super().setUp()
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text(
            '<html><script src="/theme.js"></script><body>Owner portal</body></html>'
        )
        (assets / "theme.js").write_text('document.documentElement.dataset.theme="light";')
        self.web = WebServer(("127.0.0.1", 0), self.config, assets, deadline=0.2, capacity=2)
        self.web_thread = threading.Thread(
            target=self.web.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True
        )
        self.web_thread.start()

    def tearDown(self) -> None:
        self.web.shutdown()
        self.web.server_close()
        super().tearDown()

    def web_request(
        self, path: str, headers: dict[str, str] | None = None
    ) -> http.client.HTTPResponse:
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=2)
        connection.request("GET", path, headers={"Host": "127.0.0.1:18080", **(headers or {})})
        response = connection.getresponse()
        response.read()
        connection.close()
        return response

    def test_every_broker_route_reaches_the_broker_through_the_web_server(self) -> None:
        # Browser-unreachable broker routes; everything else must pass the web server.
        internal = {("GET", "/v1/health")}
        samples = {"key": "TOKEN"}
        for route in self.router._routes:
            path = re.sub(
                r"\(\?P<(\w+)>\[\^/\]\+\)",
                lambda match: samples.get(match.group(1), str(uuid.uuid4())),
                route.pattern.pattern[1:-1],
            ).replace("\\", "")
            if (route.method, path) in internal:
                continue
            browser = path.removeprefix("/v1") if path.startswith("/v1/auth/") else "/api" + path
            with self.subTest(method=route.method, path=path):
                self.assertTrue(hasattr(WebHandler, "do_" + route.method))
                reply = self.web.forward(route.method, browser, "", {}, b"")
                if browser in NAVIGATIONS:
                    # Navigations redirect even when the broker can't answer.
                    self.assertEqual(
                        reply.headers[0], ("Location", "/sign-in?error=SIGN_IN_UNAVAILABLE")
                    )
                    continue
                self.assertNotIn(reply.status, {404, 405}, json.loads(reply.body))

    def test_commons_navigations_answer_only_with_checked_redirects(self) -> None:
        state, challenge = "s" * 43, "c" * 43
        approval = (
            self.config.commons_origin
            + "/connect?"
            + urlencode(
                {
                    "app": self.config.portal_origin,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        directive = {"name": "commons", "value": opaque(), "maxAge": 600}

        def navigate(path: str, location: str, status: int = 303, **extra: Any) -> Reply:
            value = {"browser": {"status": status, "location": location, "cookies": [directive]}}
            with patch.object(self.web.broker, "request", return_value=(200, {**value, **extra})):
                return self.web.forward("GET", path, "", {}, b"")

        for path, location in (
            ("/auth/commons/start", approval),
            ("/auth/commons/start", "/sign-in"),
            ("/auth/commons/start", "/sign-in?error=RATE_LIMITED"),
            ("/auth/commons/callback", "/apps"),
            ("/auth/commons/callback", "/sign-in?error=ACCOUNT_DISABLED"),
        ):
            with self.subTest(path=path, location=location):
                reply = navigate(path, location)
                self.assertEqual((reply.status, reply.body, reply.content_type), (303, b"", ""))
                self.assertEqual(
                    reply.headers,
                    (("Location", location), ("Set-Cookie", self.web.cookie(directive))),
                )
        refused = [
            ("/auth/commons/start", "https://evil.example.com/connect?" + approval.split("?")[1]),
            ("/auth/commons/start", approval.replace("/connect?", "/login?")),
            ("/auth/commons/start", approval.replace("127.0.0.1", "127.0.0.2")),
            ("/auth/commons/start", approval + "&next=/apps"),
            ("/auth/commons/start", approval.replace(state, "short")),
            # Every sign-in carries an S256 challenge, never plain or none.
            ("/auth/commons/start", approval.split("&code_challenge=")[0]),
            ("/auth/commons/start", approval.replace("S256", "plain")),
            ("/auth/commons/start", approval.replace(challenge, challenge[:42])),
            ("/auth/commons/start", approval.replace(challenge, challenge + "=")),
            ("/auth/commons/start", "/apps\r\nSet-Cookie: injected=1"),
            # Only start may leave the portal, and only for Commons' approval page.
            ("/auth/commons/callback", approval),
            ("/auth/commons/callback", "//evil.example.com/apps"),
            ("/auth/commons/callback", "https://evil.example.com/apps"),
            ("/auth/commons/callback", "/apps?next=https://evil.example.com"),
            ("/auth/commons/callback", "/sign-in?error=lowercase"),
        ]
        for path, location in refused:
            with self.subTest(path=path, location=location):
                reply = navigate(path, location)
                self.assertEqual(reply.status, 303)
                self.assertEqual(dict(reply.headers)["Location"], "/sign-in?error=SIGN_IN_FAILED")
                self.assertNotIn(("Set-Cookie", self.web.cookie(directive)), reply.headers)
        for status, extra in ((302, {}), (303, {"data": {"returnPath": "/apps"}})):
            reply = navigate("/auth/commons/callback", "/apps", status, **extra)
            self.assertEqual(dict(reply.headers)["Location"], "/sign-in?error=SIGN_IN_FAILED")
        # Other routes never redirect.
        reply = navigate("/auth/options", "/apps")
        self.assertEqual(
            (reply.status, json.loads(reply.body)["error"]["code"]), (400, "INVALID_REQUEST")
        )
        cleared = ("Set-Cookie", self.web.cookie({"name": "commons", "value": "", "maxAge": 0}))
        for status, code in (
            (400, "SIGN_IN_FAILED"),
            (401, "SIGN_IN_EXPIRED"),
            (429, "RATE_LIMITED"),
            (503, "SIGN_IN_UNAVAILABLE"),
        ):
            failure = {"error": {"code": "X", "summary": "x", "retryable": False}}
            with patch.object(self.web.broker, "request", return_value=(status, failure)):
                reply = self.web.forward("GET", "/auth/commons/callback", "", {}, b"")
            self.assertEqual((reply.status, reply.body), (303, b""))
            self.assertEqual(reply.headers[0], ("Location", "/sign-in?error=" + code))
            self.assertEqual(reply.headers[-1], cleared)
            if status == 401:
                self.assertIn(
                    ("Set-Cookie", self.web.cookie({"name": "session", "value": "", "maxAge": 0})),
                    reply.headers,
                )
        self.assertEqual(self.web.forward("POST", "/auth/commons/start", "", {}, b"").status, 405)

    def test_commons_sign_in_round_trip_through_web_broker_and_identity(self) -> None:
        from openstack_platform.management.broker.main import serve as broker_serve

        self.broker.close()
        self.broker, broker_server = broker_serve(self.config)
        thread = threading.Thread(
            target=broker_server.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True
        )
        thread.start()

        def get(target: str, headers: dict[str, str]) -> http.client.HTTPResponse:
            connection = http.client.HTTPConnection("127.0.0.1", self.web.server_port, timeout=5)
            connection.request("GET", target, headers={"Host": "127.0.0.1:18080", **headers})
            response = connection.getresponse()
            self.assertEqual(response.read(), b"")
            connection.close()
            return response

        try:
            started = get("/auth/commons/start", {"Sec-Fetch-Site": "same-origin"})
            self.assertEqual(started.status, 303)
            [cookie] = started.msg.get_all("Set-Cookie")
            binder = cookie.split(";")[0].removeprefix("portal-dev-commons=")
            self.assertEqual(
                cookie.split("; ")[1:], ["Path=/", "Max-Age=600", "HttpOnly", "SameSite=Lax"]
            )
            target = self.approve(started.getheader("Location")).removeprefix("/v1")
            completed = get(target, {"Cookie": "portal-dev-commons=" + binder})
            self.assertEqual((completed.status, completed.getheader("Location")), (303, "/apps"))
            cookies = completed.msg.get_all("Set-Cookie")
            self.assertEqual(
                [value.split("=")[0] for value in cookies],
                [
                    "portal-dev-anonymous",
                    "portal-dev-session",
                    "portal-dev-commons",
                ],
            )
            self.assertTrue(cookies[2].startswith("portal-dev-commons=; Path=/; Max-Age=0;"))
            session = cookies[1].split(";")[0]
            status, value = ProjectClient(self.config.broker_socket).request(
                "GET",
                "/v1/session",
                headers={"cookie": session, "x-portal-client-address": "127.0.0.1"},
            )
            self.assertEqual((status, value["data"]["user"]["username"]), (200, "alice"))
            # A replayed callback finds its code spent.
            replayed = get(target, {"Cookie": "portal-dev-commons=" + binder})
            self.assertEqual(replayed.getheader("Location"), "/sign-in?error=SIGN_IN_EXPIRED")
            # A malformed session cookie still ends on the sign-in page, cleared.
            broken = get(target, {"Cookie": "portal-dev-session=bad"})
            self.assertEqual(broken.getheader("Location"), "/sign-in?error=SIGN_IN_EXPIRED")
            self.assertEqual(
                [value.split("=")[0] for value in broken.msg.get_all("Set-Cookie")],
                ["portal-dev-session", "portal-dev-commons"],
            )
        finally:
            broker_server.shutdown()
            broker_server.server_close()
            thread.join(timeout=5)

    def test_host_routes_headers_and_private_assets(self) -> None:
        response = self.web_request("/apps")
        self.assertEqual(response.status, 200)
        policy = response.getheader("Content-Security-Policy") or ""
        self.assertTrue(
            "script-src 'self'" in policy
            and "style-src-attr 'none'" in policy
            and "unsafe-inline" not in policy
        )
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("Referrer-Policy"), "strict-origin")
        for path in (
            "/api/v1/admin/applications",
            "/missing.js",
            "/../config.json",
            "/assets/%2e%2e/config",
        ):
            self.assertIn(self.web_request(path).status, {400, 404})
        self.assertEqual(self.web_request("/", {"Host": "evil.example.com"}).status, 400)

    def test_cookie_directives_and_response_limit(self) -> None:
        https = dataclasses.replace(self.config, portal_origin="https://platform.example.com")
        self.web.config = https
        cookie = self.web.cookie({"name": "session", "value": opaque(), "maxAge": 100})
        self.assertTrue(cookie.startswith("__Host-portal-session="))
        self.assertEqual(
            cookie.split("; ")[1:], ["Path=/", "Max-Age=100", "Secure", "HttpOnly", "SameSite=Lax"]
        )
        login = self.web.cookie({"name": "login", "value": opaque(), "maxAge": 600})
        self.assertTrue(login.startswith("__Host-portal-anonymous="))
        self.assertEqual(
            login.split("; ")[1:],
            ["Path=/", "Max-Age=600", "Secure", "HttpOnly", "SameSite=Strict"],
        )
        cleared = self.web.cookie({"name": "login", "value": "", "maxAge": 0})
        self.assertEqual(
            cleared,
            "__Host-portal-anonymous=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict",
        )
        # Commons' callback is a navigation from another site, so its state is Lax.
        state = self.web.cookie({"name": "commons", "value": opaque(), "maxAge": 600})
        self.assertTrue(state.startswith("__Host-portal-commons="))
        self.assertEqual(
            state.split("; ")[1:],
            ["Path=/", "Max-Age=600", "Secure", "HttpOnly", "SameSite=Lax"],
        )
        self.assertFalse("Domain=" in cookie)
        with self.assertRaises(ValueError):
            self.web.cookie({"name": "other", "value": "bad\r\n", "maxAge": 0})

    def test_framing_header_timeout_and_connection_cap(self) -> None:
        client = socket.create_connection(("127.0.0.1", self.web.server_port), timeout=2)
        client.sendall(
            b"POST /api/v1/apps HTTP/1.1\r\nHost: 127.0.0.1:18080\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n"
        )
        self.assertTrue(b" 400 " in client.recv(4096))
        client.close()
        client = socket.create_connection(("127.0.0.1", self.web.server_port), timeout=2)
        client.sendall(b"GET / HTTP/1.1\r\nHost:")
        time.sleep(0.35)
        self.assertEqual(client.recv(100), b"")
        client.close()
        clients = [
            socket.create_connection(("127.0.0.1", self.web.server_port), timeout=2)
            for _ in range(3)
        ]
        time.sleep(0.05)
        with self.web.client_lock:
            self.assertLessEqual(len(self.web.clients), 2)
        for client in clients:
            client.close()

    def test_broker_peer_rejected_before_http_parse(self) -> None:
        path = self.sockets / "denied.sock"
        server = ControllerServer(
            str(path),
            self.router,
            peer_policy=PeerPolicy(frozenset({(9876, 9876)})),
            limits=TransportLimits(header_seconds=0.1),
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True
        )
        thread.start()
        try:
            # Send nothing: the rejection must arrive before any HTTP is parsed.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(5)
                peer.connect(str(path))
                raw = b""
                while chunk := peer.recv(65536):
                    raw += chunk
            head, _, body = raw.partition(b"\r\n\r\n")
            self.assertTrue(head.startswith(b"HTTP/1.1 503 "), head)
            self.assertEqual(json.loads(body)["error"]["code"], "PEER_IDENTITY_REJECTED")
            self.assertEqual(path.stat().st_mode & 0o777, 0o660)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
