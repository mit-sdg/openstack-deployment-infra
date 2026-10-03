"""Offline evidence for the owner auth, authorization, journal and web boundaries."""

from __future__ import annotations

import dataclasses
import http.client
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
from openstack_platform.management.web.server import WebServer

ROOT = Path(__file__).resolve().parents[1]


class ManagementCase(unittest.TestCase):
    def setUp(self) -> None:
        from openstack_platform.management.dev.__main__ import tls_context
        from openstack_platform.management.dev.commons import Commons
        from openstack_platform.management.identity.client import IdentityConfig
        from openstack_platform.management.identity.main import serve as identity_serve

        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mt-", dir=ROOT / ".tmp")
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
        ca = self.root / "ca.pem"
        self.commons = Commons(("127.0.0.1", 0), self.config, self.root, tls=tls_context(ca))
        self.config = dataclasses.replace(
            self.config, commons_origin=f"https://localhost:{self.commons.server_port}"
        )
        self.commons.config = self.config
        self.commons_thread = threading.Thread(
            target=self.commons.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
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
            target=self.identity.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.identity_thread.start()
        self.fixture = FakeController()
        self.controller = self.fixture.server(self.config.controller_socket)
        self.thread = threading.Thread(
            target=self.controller.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.broker = Broker(self.config)
        self.router = self.broker.router()
        self.tokens: dict[str, str] = {}
        self.csrf: dict[str, str] = {}

    def tearDown(self) -> None:
        self.broker.journal.close()
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

    def login(self, owner: str = "alice") -> str:
        options = self.call("GET", "/v1/auth/options").body
        binder = options["browser"]["cookies"][0]["value"]
        completed = self.call(
            "POST",
            "/v1/auth/login",
            {
                "csrfToken": options["data"]["csrfToken"],
                "username": owner,
                "password": self.commons.passwords[owner],
            },
            headers={"cookie": self.config.login_cookie + "=" + binder},
        ).body
        self.tokens[owner] = completed["browser"]["cookies"][1]["value"]
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
    def attempt(
        self, username: str, password: str, *, headers: dict[str, str] | None = None
    ) -> Any:
        options = self.call("GET", "/v1/auth/options").body
        return self.call(
            "POST",
            "/v1/auth/login",
            {"csrfToken": options["data"]["csrfToken"], "username": username, "password": password},
            headers={
                "cookie": self.config.login_cookie
                + "="
                + options["browser"]["cookies"][0]["value"],
                **(headers or {}),
            },
        )

    def test_csrf_and_exact_origin_reject_before_credentials_leave_broker(self) -> None:
        for origin in ("null", "", "https://evil.example.com"):
            self.assert_error(
                "ORIGIN_REJECTED",
                lambda origin=origin: self.attempt(
                    "alice", "incorrect", headers={"origin": origin}
                ),
            )
        self.assert_error(
            "CSRF_REJECTED",
            lambda: self.call(
                "POST",
                "/v1/auth/login",
                {"csrfToken": "bad", "username": "alice", "password": "incorrect"},
            ),
        )
        self.assertEqual(self.commons.calls, 0)
        for route in ("start", "assertion", "complete"):
            self.assert_error(
                "NOT_FOUND", lambda route=route: self.call("POST", "/v1/auth/" + route, {})
            )

    def test_generic_invalid_disabled_and_unavailable_errors(self) -> None:
        self.assert_error("INVALID_CREDENTIALS", lambda: self.attempt("missing", "incorrect"))
        self.assert_error("INVALID_CREDENTIALS", lambda: self.attempt("alice", "incorrect"))
        self.assert_error("ACCOUNT_DISABLED", lambda: self.attempt("carol", "local-carol-password"))
        self.commons.override = (500, b'{"error":"INTERNAL_ERROR"}')
        self.assert_error(
            "IDENTITY_UNAVAILABLE", lambda: self.attempt("alice", "local-alice-password")
        )

    def test_identity_outage_preserves_existing_sessions_and_only_blocks_new_login(self) -> None:
        user = self.login()
        self.identity.shutdown()
        self.identity.server_close()
        self.assertEqual(
            self.call("GET", "/v1/session", owner="alice").body["data"]["user"]["id"], user
        )
        self.assertEqual(self.call("GET", "/v1/apps", owner="alice").status, 200)
        with self.assertRaises(HttpError) as caught:
            self.attempt("bob", "local-bob-password")
        self.assertEqual(caught.exception.code, "IDENTITY_UNAVAILABLE")
        self.assertEqual(caught.exception.status, 503)
        self.assertTrue(caught.exception.retryable)

    def test_failed_username_address_window_blocks_before_identity_and_expires(self) -> None:
        now = time.time()
        self.broker.auth.clock = lambda: now
        for _ in range(5):
            self.assert_error(
                "INVALID_CREDENTIALS",
                lambda: self.attempt("alice", "incorrect"),
            )
        calls = self.commons.calls
        self.assert_error("RATE_LIMITED", lambda: self.attempt("alice", "local-alice-password"))
        self.assert_error("RATE_LIMITED", lambda: self.attempt("alice", "incorrect"))
        self.assertEqual(self.commons.calls, calls)
        result = self.attempt(
            "alice", "local-alice-password", headers={"x-portal-client-address": "192.0.2.1"}
        )
        self.assertEqual(result.status, 200)
        self.assert_error("RATE_LIMITED", lambda: self.attempt("alice", "local-alice-password"))
        self.assertEqual(self.commons.calls, calls + 1)
        # Rejections (including a valid password) never extend the fixed window.
        now += 59.99
        self.assert_error("RATE_LIMITED", lambda: self.attempt("alice", "local-alice-password"))
        now += 0.01
        self.login()
        self.assertEqual(self.commons.calls, calls + 2)

    def test_concurrent_guesses_reserve_budget_before_identity(self) -> None:
        self.commons.delay_seconds = 0.15
        barrier = threading.Barrier(20)

        def attempt() -> str:
            barrier.wait(timeout=5)
            try:
                self.attempt("alice", "incorrect")
            except HttpError as error:
                return error.code
            return "unexpected_success"

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(lambda _: attempt(), range(20)))
        self.assertEqual(results.count("INVALID_CREDENTIALS"), 5)
        self.assertEqual(results.count("RATE_LIMITED"), 15)
        self.assertEqual(self.commons.calls, 5)
        self.assert_error("RATE_LIMITED", lambda: self.attempt("alice", "local-alice-password"))
        self.assertEqual(self.commons.calls, 5)

    def test_username_throttle_shares_ipv6_prefix_but_not_another_prefix(self) -> None:
        for _ in range(5):
            self.assert_error(
                "INVALID_CREDENTIALS",
                lambda: self.attempt(
                    "alice", "incorrect", headers={"x-portal-client-address": "2001:db8::1"}
                ),
            )
        self.assert_error(
            "RATE_LIMITED",
            lambda: self.attempt(
                "alice", "local-alice-password", headers={"x-portal-client-address": "2001:db8::2"}
            ),
        )
        self.assertEqual(self.commons.calls, 5)
        self.assertEqual(
            self.attempt(
                "alice",
                "local-alice-password",
                headers={"x-portal-client-address": "2001:db8:0:1::1"},
            ).status,
            200,
        )

    def test_per_address_admission_shares_ipv6_prefix(self) -> None:
        from openstack_platform.management.broker.anonymous import AddressLimits

        limits = AddressLimits(1, 1)
        from openstack_platform.controller.http import Request

        req = Request(
            "GET", "/v1/auth/options", {}, {}, {"x-portal-client-address": "2001:db8::1"}, None
        )
        limits.check(req, "start", 100)
        self.assert_error(
            "RATE_LIMITED",
            lambda: limits.check(
                dataclasses.replace(req, headers={"x-portal-client-address": "2001:db8::2"}),
                "start",
                100,
            ),
        )

    def test_rotation_archive_password_change_and_local_revoke(self) -> None:
        user = self.login()
        first = self.tokens["alice"]
        options = self.call("GET", "/v1/auth/options").body
        completed = self.call(
            "POST",
            "/v1/auth/login",
            {
                "csrfToken": options["data"]["csrfToken"],
                "username": "alice",
                "password": self.commons.passwords["alice"],
            },
            headers={
                "cookie": self.config.login_cookie
                + "="
                + options["browser"]["cookies"][0]["value"]
                + "; "
                + self.config.session_cookie
                + "="
                + first
            },
        ).body
        self.tokens["alice"] = completed["browser"]["cookies"][1]["value"]
        self.assert_error(
            "SESSION_EXPIRED",
            lambda: self.call(
                "GET", "/v1/session", headers={"cookie": self.config.session_cookie + "=" + first}
            ),
        )
        self.commons.passwords["alice"] = "changed-fixture-password"
        self.commons.override = (403, b'{"error":"FORBIDDEN"}')
        self.assertEqual(self.call("GET", "/v1/session", owner="alice").status, 200)
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET enabled=0 WHERE id=?", (user,))
        self.assert_error(
            "ACCOUNT_DISABLED", lambda: self.call("GET", "/v1/session", owner="alice")
        )

    def test_password_sentinel_is_absent_from_persistent_state_logs_and_responses(self) -> None:
        import contextlib
        import io
        import secrets

        sentinel = secrets.token_urlsafe(48)
        self.commons.passwords["alice"] = sentinel
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            response = self.attempt("alice", sentinel)
        self.assertNotIn(sentinel, canonical(response.body) + output.getvalue())
        with self.broker.database.connect() as db:
            for table in ("users", "sessions", "audit", "intents"):
                self.assertNotIn(
                    sentinel, canonical([list(row) for row in db.execute("SELECT * FROM " + table)])
                )
        for path in self.config.state_directory.iterdir():
            if path.is_file():
                self.assertNotIn(sentinel.encode(), path.read_bytes())


class OwnerIntentTests(ManagementCase):
    def test_deploy_guidance_uses_only_release_text_and_hides_codes_from_owners(self) -> None:
        from openstack_platform.management.broker.journal import (
            BUILD_GUIDANCE,
            HEALTH_GUIDANCE,
            deploy_failure_guidance,
            intent_model,
        )

        user = self.login()
        app = self.create()
        for code, phase, expected in (
            ("BUILD_REJECTED", None, BUILD_GUIDANCE),
            ("BUILD_FAILED", None, BUILD_GUIDANCE),
            ("HEALTH_TIMEOUT", None, HEALTH_GUIDANCE),
            ("CANDIDATE_UNHEALTHY", None, HEALTH_GUIDANCE),
            ("DEADLINE_EXCEEDED", "worker_ready", HEALTH_GUIDANCE),
            ("DEADLINE_EXCEEDED", "building", None),
            (None, "build_rejected", BUILD_GUIDANCE),
            ("INVALID_REQUEST", None, None),
        ):
            with self.subTest(code=code, phase=phase):
                with self.broker.database.connect(write=True) as db:
                    identifier = self.broker.record(
                        db,
                        user,
                        app,
                        "deploy",
                        str(uuid.uuid4()),
                        "fp",
                        "POST",
                        f"/v1/applications/{app}/deployments",
                        {"commit": "a" * 40},
                        str(uuid.uuid4()),
                    )
                    db.execute(
                        "UPDATE intents SET state='failed',operation=?,safe_error='Generic failure.' WHERE id=?",
                        (canonical({"controllerErrorCode": code, "phase": phase}), identifier),
                    )
                    row = dict(
                        db.execute("SELECT * FROM intents WHERE id=?", (identifier,)).fetchone()
                    )
                owner = self.call("GET", f"/v1/intents/{identifier}", owner="alice").body["data"]
                self.assertEqual(owner["safeError"], expected or "Generic failure.")
                self.assertNotIn("controllerErrorCode", owner)
                admin = intent_model(row, diagnostic=True)
                self.assertEqual(admin["safeError"], expected or "Generic failure.")
                self.assertEqual(admin["controllerErrorCode"], code)
                staff = self.broker.staff.operation_model(row)
                self.assertEqual(staff["guidance"], expected)
                self.assertEqual(staff["controllerErrorCode"], code)
        self.assertIsNone(deploy_failure_guidance("env_set", "failed", "BUILD_REJECTED", None))
        self.assertIsNone(deploy_failure_guidance("deploy", "succeeded", "HEALTH_TIMEOUT", None))
        self.assertIsNone(deploy_failure_guidance("deploy", "failed", {}, {}))

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
                operations = self.call("GET", "/v1/staff/operations", owner="alice").body["data"][
                    "items"
                ]
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

    def test_sidecar_disappearance_does_not_turn_committed_requests_into_failures(self) -> None:
        self.login("bob")
        original = os.chmod
        observed = set()

        def disappearing(path: Any, mode: int) -> None:
            if str(path).endswith(("-wal", "-shm")):
                observed.add(Path(path).suffix)
                raise FileNotFoundError("SQLite closed its final connection")
            original(path, mode)

        with patch("openstack_platform.management.broker.database.os.chmod", disappearing):
            self.assertEqual(self.call("GET", "/v1/apps", owner="bob").status, 200)
            result = self.call("POST", "/v1/apps", {"slug": "committed-app"}, "bob")
            self.assertEqual(result.status, 201)
        self.assertTrue(observed)
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM apps WHERE slug='committed-app'").fetchone()[0], 1
            )
        with (
            patch(
                "openstack_platform.management.broker.database.os.chmod",
                side_effect=PermissionError("permission normalization failed"),
            ),
            self.assertRaises(PermissionError),
        ):
            self.call("GET", "/v1/apps", owner="bob")

    def test_terminal_cleanup_accepts_only_real_controller_settled_values(self) -> None:
        self.login()
        app = self.create()
        self.save(app)
        for cleanup, state in (
            ("confirmed", "succeeded"),
            ("not_required", "succeeded"),
            ("pending", "blocked"),
            ("complete", "blocked"),
            ("completed", "blocked"),
            ("none", "blocked"),
        ):
            # Clear only fixture-held intents between samples, never product state.
            with self.broker.database.connect(write=True) as db:
                db.execute(
                    "UPDATE intents SET state='failed' WHERE kind='deploy' AND state='blocked'"
                )
            result = self.call(
                "POST",
                f"/v1/apps/{app}/deployments",
                {"commit": uuid.uuid4().hex + "a" * 8, "configurationRevision": 1},
                "alice",
            ).body["data"]
            operation = self.fixture.operations[result["operationId"]]
            operation.update(status="succeeded", cleanupState=cleanup)
            self.broker.journal.dispatch(result["intentId"])
            read = self.call("GET", f"/v1/intents/{result['intentId']}", owner="alice").body["data"]
            self.assertEqual(read["state"], state)

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

    def test_unavailable_observation_keeps_accepted_pointer(self) -> None:
        from openstack_platform.management.broker.client import ControllerUnavailable

        self.login()
        app = self.create()
        self.save(app)
        intent = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "e" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        time.sleep(0.65)
        self.broker.journal.dispatch(intent["intentId"])
        accepted = self.call("GET", f"/v1/apps/{app}", owner="alice").body["data"]
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE observations SET updated=0 WHERE app_id=?", (app,))
        original = self.broker.client.request

        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise ControllerUnavailable("fixture outage")

        self.broker.client.request = unavailable  # type: ignore[method-assign]
        try:
            stale = self.call("GET", f"/v1/apps/{app}", owner="alice").body["data"]
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["activeDeploymentId"], accepted["activeDeploymentId"])
            self.assertEqual(stale["acceptedDeployment"], accepted["acceptedDeployment"])
        finally:
            self.broker.client.request = original  # type: ignore[method-assign]

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

    def test_foreign_deployment_under_owned_parent_returns_not_found(self) -> None:
        self.login("alice")
        self.login("bob")
        alice_app = self.create()
        bob_app = self.create("bob", "bob-project")
        self.save(alice_app)
        intent = self.call(
            "POST",
            f"/v1/apps/{alice_app}/deployments",
            {"commit": "d" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        for suffix in ("", "/build-log"):
            self.assert_error(
                "NOT_FOUND",
                lambda suffix=suffix: self.call(
                    "GET",
                    f"/v1/apps/{bob_app}/deployments/{intent['operationId']}{suffix}",
                    owner="bob",
                ),
            )

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
        time.sleep(0.65)
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
        time.sleep(0.65)
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
        time.sleep(0.65)
        self.broker.journal.dispatch(intent["intentId"])
        active = self.fixture.apps[app]["activeDeploymentId"]
        self.fixture.failed_next = True
        failed = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "2" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        time.sleep(0.65)
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
    def test_web_peer_comes_from_canonical_contract(self) -> None:
        from unittest import mock

        from openstack_platform import contracts
        from openstack_platform.management.common import strict_json
        from openstack_platform.management.config import management_web_peer

        value = strict_json(contracts._contract_bytes())
        value["accounts"]["managementWeb"]["uid"] = 1234
        value["accounts"]["managementWeb"]["gid"] = 4321
        with mock.patch.object(
            contracts, "_contract_bytes", return_value=canonical(value).encode()
        ):
            self.assertEqual(management_web_peer(), (1234, 4321))

    def test_development_ignores_spoofed_forwarding_address(self) -> None:
        from unittest import mock

        with mock.patch.object(
            self.web.broker, "request", return_value=(200, {"data": {}})
        ) as request:
            self.web.forward(
                "GET",
                "/auth/options",
                "",
                {
                    "x-forwarded-for": "198.51.100.99",
                    "x-portal-client-address": "198.51.100.98",
                    "_peer_address": "127.0.0.1",
                },
                b"",
            )
        self.assertEqual(
            request.call_args.kwargs["headers"]["x-portal-client-address"], "127.0.0.1"
        )

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
            target=self.web.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
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
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            status, response = ProjectClient(path).request("GET", "/v1/health")
            self.assertEqual(status, 503)
            self.assertEqual(response["error"]["code"], "PEER_IDENTITY_REJECTED")
            self.assertEqual(path.stat().st_mode & 0o777, 0o660)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
