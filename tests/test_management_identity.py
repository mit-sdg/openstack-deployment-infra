"""Commons Connect redeem contract, TLS/peer boundaries, bounded failures and schema upgrade."""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from openstack_platform.controller.http import (
    ControllerServer,
    PeerPolicy,
    Response,
    Router,
)
from openstack_platform.management.broker.client import ProjectClient
from openstack_platform.management.broker.database import MIGRATION_2, SCHEMA_V1, Database
from openstack_platform.management.broker.main import serve as broker_serve
from openstack_platform.management.common import digest
from openstack_platform.management.dev.commons import USERS
from openstack_platform.management.identity.client import CommonsClient, IdentityConfig
from tests.test_management import ManagementCase


class IdentityTests(ManagementCase):
    def client(self, **overrides):
        options = {"connect_seconds": 0.2, "read_seconds": 0.2, **overrides}
        return CommonsClient(
            IdentityConfig(
                self.config.commons_origin,
                self.config.identity_socket,
                development=True,
                development_ca=self.root / "ca.pem",
                **options,
            )
        )

    def code(self, name: str = "alice") -> dict[str, str]:
        """A fresh redeem request for a code Commons issued to the portal."""
        app = self.config.portal_origin
        return {"code": self.commons.issue(name, app), "app": app}

    def test_redeem_exact_request_and_typed_status_contract(self) -> None:
        client = self.client()
        request = self.code()
        error, user = client.redeem(request)
        self.assertIsNone(error)
        # Email is part of Commons' answer but never leaves the identity service.
        self.assertEqual(
            user,
            {
                "subject": "11111111-1111-4111-8111-111111111111",
                "username": "alice",
                "displayName": "Alice Student",
            },
        )
        self.assertEqual(client.redeem(request)[0], "invalid_code")
        other = self.commons.issue("alice", "https://other.example.com")
        self.assertEqual(
            client.redeem({"code": other, "app": self.config.portal_origin})[0], "invalid_code"
        )
        self.assertEqual(client.redeem(self.code("carol"))[0], "invalid_code")
        code = self.commons.issue("alice", self.config.portal_origin)
        for value in (
            {"code": code, "app": self.config.portal_origin, "extra": True},
            {"code": code},
            {"code": code, "app": None},
            {"code": None, "app": self.config.portal_origin},
            {"code": "", "app": self.config.portal_origin},
            {"code": "x" * 129, "app": self.config.portal_origin},
            {"code": "a b", "app": self.config.portal_origin},
            {"code": "a/b", "app": self.config.portal_origin},
            {"code": "é", "app": self.config.portal_origin},
            {"code": code, "app": self.config.portal_origin + "/"},
            {"code": code, "app": self.config.portal_origin + "/auth"},
            {"code": code, "app": self.config.portal_origin + "?x=1"},
            {"code": code, "app": "HTTP://127.0.0.1:18080"},
            {"code": code, "app": "https://user@127.0.0.1:18080"},
            # Development admits loopback apps only.
            {"code": code, "app": "https://class.example.com"},
            [code, self.config.portal_origin],
        ):
            before = self.commons.calls
            self.assertEqual(client.redeem(value)[0], "invalid_request")
            self.assertEqual(self.commons.calls, before)
        for status, body, expected in (
            (400, {"error": "CONNECT_CODE_INVALID"}, "invalid_code"),
            (400, {"error": "INVALID_REQUEST"}, "invalid_request"),
            (400, {"error": "UNAUTHORIZED"}, "unavailable"),
            (400, {"error": "CONNECT_CODE_INVALID", "reason": "used"}, "unavailable"),
            (401, {"error": "UNAUTHORIZED"}, "unavailable"),
            (403, {"error": "FORBIDDEN"}, "unavailable"),
            (404, {"error": "NOT_FOUND"}, "unavailable"),
            (500, {"error": "INTERNAL_ERROR"}, "unavailable"),
        ):
            self.commons.override = (status, json.dumps(body).encode())
            self.assertEqual(client.redeem(self.code())[0], expected)

    def test_production_identity_accepts_only_https_app_origins(self) -> None:
        from openstack_platform.management.identity.client import redemption

        code = "11111111-1111-4111-8111-111111111111.credential_-"
        self.assertEqual(
            redemption({"code": code, "app": "https://platform.example.com"}, development=False),
            {"code": code, "app": "https://platform.example.com"},
        )
        for app in (
            "http://platform.example.com",
            "https://localhost:9443",
            "https://127.0.0.1",
            "https://Platform.example.com",
            "https://platform.example.com/",
        ):
            with self.subTest(app=app), self.assertRaises(ValueError):
                redemption({"code": code, "app": app}, development=False)

    def test_response_duplicate_extra_invalid_uuid_and_size_refuse_without_echo(self) -> None:
        import contextlib
        import io

        client = self.client()
        profile = {
            "user": "11111111-1111-4111-8111-111111111111",
            "username": "alice",
            "displayName": "Alice",
            "email": "alice@example.com",
        }
        bodies = [
            b"not-json",
            b'{"user":"x","user":"y"}',
            b'{"error":NaN}',
            json.dumps({**profile, "user": "invalid"}).encode(),
            json.dumps({**profile, "user": "ABCDEF01-1111-4111-8111-111111111111"}).encode(),
            json.dumps({**profile, "user": profile["user"].replace("-", "")}).encode(),
            json.dumps({**profile, "user": "{" + profile["user"] + "}"}).encode(),
            json.dumps({**profile, "username": ""}).encode(),
            json.dumps({**profile, "username": "x" * 33}).encode(),
            json.dumps({**profile, "displayName": "x" * 257}).encode(),
            json.dumps({**profile, "email": None}).encode(),
            json.dumps({**profile, "extra": True}).encode(),
            json.dumps({key: profile[key] for key in ("user", "username", "displayName")}).encode(),
            b"x" * 9000,
        ]
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            for body in bodies:
                self.commons.override = (200, body)
                request = self.code()
                self.assertEqual(client.redeem(request), ("unavailable", None))
                # Operators get a reason; never the code or what Commons sent.
                self.assertNotIn(request["code"], output.getvalue())
        self.assertNotIn("alice", output.getvalue())
        self.assertIn("identity-check=unavailable reason=", output.getvalue())
        self.commons.override = (200, json.dumps({**profile, "displayName": ""}).encode())
        self.assertEqual(client.redeem(self.code())[1]["displayName"], "alice")

    def test_redirect_refused_proxy_ignored_and_read_timeout_bounded(self) -> None:
        client = self.client()
        self.commons.override = (302, b"{}")
        self.assertEqual(client.redeem(self.code())[0], "unavailable")
        self.commons.override = None
        with patch.dict(
            os.environ, {"HTTPS_PROXY": "http://192.0.2.1:9", "HTTP_PROXY": "http://192.0.2.1:9"}
        ):
            self.assertIsNone(client.redeem(self.code())[0])
        self.commons.delay_seconds = 0.4
        started = time.monotonic()
        self.assertEqual(client.redeem(self.code())[0], "unavailable")
        self.assertLess(time.monotonic() - started, 0.8)

    def test_system_ca_rejects_self_signed_and_production_dev_trust_is_rejected(self) -> None:
        config = IdentityConfig(
            self.config.commons_origin,
            self.config.identity_socket,
            connect_seconds=0.2,
            read_seconds=0.2,
        )
        client = CommonsClient(config)
        # Production refuses loopback apps, so this request names a public one.
        request = {"code": self.code()["code"], "app": "https://platform.example.com"}
        self.assertEqual(client.redeem(request)[0], "unavailable")
        with patch.dict(os.environ, {"SSL_CERT_FILE": str(self.root / "ca.pem")}):
            self.assertEqual(CommonsClient(config).redeem(request)[0], "unavailable")
        with self.assertRaises(ValueError):
            CommonsClient(dataclasses.replace(config, development_ca=self.root / "ca.pem"))
        path = self.root / "identity-config.json"
        path.write_text(
            json.dumps(
                {
                    "commonsOrigin": "https://class.example.com",
                    "socket": str(self.config.identity_socket),
                    "development": False,
                    "developmentCa": str(self.root / "ca.pem"),
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "system CAs"):
            IdentityConfig.load(path)

    def test_connect_deadline_includes_slow_dns_and_readiness_never_contacts_commons(
        self,
    ) -> None:
        client = self.client()
        before = self.commons.calls
        resolve = socket.getaddrinfo

        def slow_resolve(*args, **kwargs):
            time.sleep(0.5)
            return resolve(*args, **kwargs)

        with patch.object(socket, "getaddrinfo", side_effect=slow_resolve):
            started = time.monotonic()
            self.assertEqual(client.redeem(self.code())[0], "unavailable")
            self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(self.commons.calls, before)
        self.assertEqual(
            ProjectClient(self.config.identity_socket).request("GET", "/v1/health"),
            (200, {"ready": True}),
        )
        self.assertEqual(self.commons.calls, before)

    def test_blackholed_first_address_does_not_starve_the_next(self) -> None:
        # A listener whose accept queue is full silently drops new SYNs, like a
        # filtered IPv6 path. It is offered first, before the real class app.
        blackhole = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blackhole.bind(("127.0.0.1", 0))
        blackhole.listen(0)
        queued = []
        for _ in range(4):
            filler = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            filler.setblocking(False)
            filler.connect_ex(blackhole.getsockname())
            queued.append(filler)
        time.sleep(0.05)
        resolve = socket.getaddrinfo

        def candidates(host, port, *args, **kwargs):
            real = resolve(host, port, socket.AF_INET, socket.SOCK_STREAM)
            dropped = (real[0][0], real[0][1], real[0][2], "", blackhole.getsockname())
            return [dropped, *real]

        try:
            client = self.client(connect_seconds=1.0)
            with patch.object(socket, "getaddrinfo", side_effect=candidates):
                started = time.monotonic()
                error, user = client.redeem(self.code())
            self.assertIsNone(error)
            self.assertEqual(user["username"], "alice")
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            for filler in queued:
                filler.close()
            blackhole.close()

    def test_connection_slot_queues_within_shared_connect_deadline(self) -> None:
        client = self.client()
        client.connect_capacity = threading.BoundedSemaphore(1)
        client.connect_capacity.acquire()
        release = threading.Timer(0.05, client.connect_capacity.release)
        release.start()
        try:
            self.assertIsNone(client.redeem(self.code())[0])
        finally:
            release.join()
        # Queue time and DNS/TLS time share the same deadline, rather than each
        # receiving a fresh timeout. A held slot never starts another worker.
        client.connect_capacity.acquire()
        started = time.monotonic()
        before = self.commons.calls
        try:
            self.assertEqual(client.redeem(self.code())[0], "unavailable")
            self.assertLess(time.monotonic() - started, 0.4)
            self.assertEqual(self.commons.calls, before)
        finally:
            client.connect_capacity.release()

    def test_fifty_concurrent_classroom_logins_through_real_unix_transports(self) -> None:
        self.broker.journal.close()
        self.broker, broker_server = broker_serve(self.config)
        self.router = self.broker.router()
        broker_thread = threading.Thread(
            target=broker_server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        broker_thread.start()
        client = ProjectClient(self.config.broker_socket, timeout=10, capacity=50)
        self.commons.delay_seconds = 0.15
        fixtures = {
            f"student-{index}": (str(uuid.uuid4()), f"Student {index}") for index in range(50)
        }
        barrier = threading.Barrier(50)
        headers = {"x-portal-client-address": "192.0.2.1"}

        def login(name: str) -> int:
            status, started = client.request("GET", "/v1/auth/commons/start", headers=headers)
            if status != 200:
                return status
            binder = started["browser"]["cookies"][0]["value"]
            state = urlsplit(started["browser"]["location"]).query.split("state=")[1]
            code = self.commons.issue(name, self.config.portal_origin)
            barrier.wait(timeout=5)
            status, completed = client.request(
                "GET",
                "/v1/auth/commons/callback?" + urlencode({"code": code, "state": state}),
                headers={**headers, "cookie": self.config.commons_cookie + "=" + binder},
            )
            return status if completed["browser"]["location"] == "/apps" else 0

        started = time.monotonic()
        try:
            with patch.dict(USERS, fixtures), ThreadPoolExecutor(max_workers=50) as executor:
                statuses = list(executor.map(login, fixtures))
            self.assertEqual(statuses, [200] * 50)
            self.assertEqual(self.commons.calls, 50)
            self.assertLess(time.monotonic() - started, 6)
        finally:
            broker_server.shutdown()
            broker_server.server_close()

    def test_identity_socket_peer_rejected_before_parsing_a_code(self) -> None:
        path = self.sockets / "denied.sock"
        calls = []
        router = Router()
        router.add("POST", "/v1/redeem", lambda req: (calls.append(req), Response(200, {}))[1])
        server = ControllerServer(
            str(path), router, peer_policy=PeerPolicy(frozenset({(os.geteuid() + 1, os.getegid())}))
        )
        import threading

        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            # Rejection happens at accept, before HTTP parsing. Reading it
            # without sending request bytes proves that boundary and avoids
            # racing the server's close with a client's code-body send.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(str(path))
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 503)
                body = json.loads(response.read())
            self.assertEqual(body["error"]["code"], "PEER_IDENTITY_REJECTED")
            self.assertFalse(body["error"]["retryable"])
            self.assertEqual(calls, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_schema_three_preserves_ownership_and_invalidates_legacy_authentication(self) -> None:
        old = self.root / "legacy"
        old.mkdir(mode=0o700)
        path = old / "management.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(SCHEMA_V1)
        db.execute(
            "INSERT INTO metadata VALUES(1,?)",
            (digest(self.config.portal_origin + "\n" + self.config.commons_origin),),
        )
        db.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        db.execute("INSERT INTO schema_migrations VALUES(1,?)", (digest(SCHEMA_V1),))
        db.execute(
            "INSERT INTO users VALUES('legacy-user',?,?,'alice','Alice',1,0,0)",
            (self.config.commons_origin, "11111111-1111-4111-8111-111111111111"),
        )
        db.execute(
            "INSERT INTO sessions VALUES('old-token','legacy-user',0,0,9999999999,'old-key')"
        )
        db.commit()
        db.close()
        path.chmod(0o600)
        migrated = Database(dataclasses.replace(self.config, state_directory=old))
        with migrated.connect() as connection:
            self.assertEqual(connection.execute("SELECT version FROM metadata").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT id FROM users").fetchone()[0], "legacy-user"
            )
            for name in ("flows", "replays"):
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name=?", (name,)
                    ).fetchone()
                )
            self.assertEqual(
                connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version=2"
                ).fetchone()[0],
                digest(MIGRATION_2),
            )
        Database(dataclasses.replace(self.config, state_directory=old))
