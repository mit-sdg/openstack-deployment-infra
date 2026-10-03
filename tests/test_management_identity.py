"""Commons bb78c5e contract, TLS/peer boundaries, bounded failures and schema upgrade."""

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

    def test_bb78c5e_exact_request_and_typed_status_contract(self) -> None:
        client = self.client()
        error, user = client.authenticate({"username": "alice", "password": "local-alice-password"})
        self.assertIsNone(error)
        self.assertEqual(
            user,
            {
                "subject": "11111111-1111-4111-8111-111111111111",
                "username": "alice",
                "displayName": "Alice Student",
            },
        )
        for value in (
            {"username": "ALICE", "password": "local-alice-password"},
            {"username": " alice", "password": "local-alice-password"},
            {"username": "alice", "password": " local-alice-password"},
        ):
            self.assertEqual(client.authenticate(value)[0], "invalid_credentials")
        self.assertEqual(
            client.authenticate({"username": "carol", "password": "local-carol-password"})[0],
            "account_disabled",
        )
        for value in (
            {"username": "alice", "password": "local-alice-password", "origin": "ignored"},
            {"username": "x" * 33, "password": "p"},
            {"username": "alice", "password": "x" * 129},
            {"username": "😀" * 17, "password": "p"},
        ):
            before = self.commons.calls
            self.assertEqual(client.authenticate(value)[0], "invalid_request")
            self.assertEqual(self.commons.calls, before)
        for status, code, expected in (
            (400, "INVALID_REQUEST", "invalid_request"),
            (401, "UNAUTHORIZED", "invalid_credentials"),
            (403, "FORBIDDEN", "account_disabled"),
            (500, "INTERNAL_ERROR", "unavailable"),
        ):
            self.commons.override = (status, json.dumps({"error": code}).encode())
            self.assertEqual(
                client.authenticate({"username": "alice", "password": "p"})[0], expected
            )

    def test_response_duplicate_extra_invalid_uuid_and_size_refuse_without_echo(self) -> None:
        client = self.client()
        bodies = [
            b"not-json",
            b'{"user":"x","user":"y"}',
            b'{"error":NaN}',
            json.dumps(
                {
                    "user": "invalid",
                    "username": "alice",
                    "displayName": "Alice",
                    "email": "alice@example.com",
                }
            ).encode(),
            json.dumps(
                {
                    "user": "11111111-1111-4111-8111-111111111111",
                    "username": "alice",
                    "displayName": "Alice",
                    "email": "alice@example.com",
                    "extra": True,
                }
            ).encode(),
            b"x" * 9000,
        ]
        for body in bodies:
            self.commons.override = (200, body)
            self.assertEqual(
                client.authenticate({"username": "alice", "password": "private-sentinel"}),
                ("unavailable", None),
            )
        self.commons.override = (401, b'{"error":"UNAUTHORIZED","extra":1}')
        self.assertEqual(
            client.authenticate({"username": "alice", "password": "p"})[0], "unavailable"
        )

    def test_redirect_refused_proxy_ignored_and_read_timeout_bounded(self) -> None:
        client = self.client()
        self.commons.override = (302, b"{}")
        self.assertEqual(
            client.authenticate({"username": "alice", "password": "p"})[0], "unavailable"
        )
        self.commons.override = None
        with patch.dict(
            os.environ, {"HTTPS_PROXY": "http://192.0.2.1:9", "HTTP_PROXY": "http://192.0.2.1:9"}
        ):
            self.assertIsNone(
                client.authenticate({"username": "alice", "password": "local-alice-password"})[0]
            )
        self.commons.delay_seconds = 0.4
        started = time.monotonic()
        self.assertEqual(
            client.authenticate({"username": "alice", "password": "p"})[0], "unavailable"
        )
        self.assertLess(time.monotonic() - started, 0.8)

    def test_system_ca_rejects_self_signed_and_production_dev_trust_is_rejected(self) -> None:
        config = IdentityConfig(
            self.config.commons_origin,
            self.config.identity_socket,
            connect_seconds=0.2,
            read_seconds=0.2,
        )
        client = CommonsClient(config)
        self.assertEqual(
            client.authenticate({"username": "alice", "password": "local-alice-password"})[0],
            "unavailable",
        )
        with patch.dict(os.environ, {"SSL_CERT_FILE": str(self.root / "ca.pem")}):
            self.assertEqual(
                CommonsClient(config).authenticate(
                    {"username": "alice", "password": "local-alice-password"}
                )[0],
                "unavailable",
            )
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

    def test_connect_deadline_includes_slow_dns_and_readiness_never_checks_credentials(
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
            self.assertEqual(
                client.authenticate({"username": "alice", "password": "p"})[0], "unavailable"
            )
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
                error, user = client.authenticate(
                    {"username": "alice", "password": "local-alice-password"}
                )
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
            self.assertIsNone(
                client.authenticate({"username": "alice", "password": "local-alice-password"})[0]
            )
        finally:
            release.join()
        # Queue time and DNS/TLS time share the same deadline, rather than each
        # receiving a fresh timeout. A held slot never starts another worker.
        client.connect_capacity.acquire()
        started = time.monotonic()
        before = self.commons.calls
        try:
            self.assertEqual(
                client.authenticate({"username": "alice", "password": "p"})[0], "unavailable"
            )
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
        self.commons.passwords.update({name: "fixture-password" for name in fixtures})
        barrier = threading.Barrier(50)
        headers = {"origin": self.config.portal_origin, "x-portal-client-address": "192.0.2.1"}

        def login(name: str) -> int:
            status, options = client.request("GET", "/v1/auth/options", headers=headers)
            if status != 200:
                return status
            binder = options["browser"]["cookies"][0]["value"]
            barrier.wait(timeout=5)
            status, _ = client.request(
                "POST",
                "/v1/auth/login",
                {
                    "csrfToken": options["data"]["csrfToken"],
                    "username": name,
                    "password": "fixture-password",
                },
                headers={**headers, "cookie": self.config.login_cookie + "=" + binder},
            )
            return status

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

    def test_identity_socket_peer_rejected_before_parsing_password(self) -> None:
        path = self.sockets / "denied.sock"
        calls = []
        router = Router()
        router.add(
            "POST", "/v1/authenticate", lambda req: (calls.append(req), Response(200, {}))[1]
        )
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
            # racing the server's close with a client's credential-body send.
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
