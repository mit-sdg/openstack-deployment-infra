"""Commons Connect redeem contract, TLS/peer boundaries, bounded failures and schema upgrade."""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from unittest.mock import patch

from openstack_platform.controller.http import (
    ControllerServer,
    PeerPolicy,
    Response,
    Router,
)
from openstack_platform.management.broker.client import ProjectClient
from openstack_platform.management.broker.database import MIGRATION_2, SCHEMA_V1, Database
from openstack_platform.management.common import digest
from openstack_platform.management.identity.client import (
    CommonsClient,
    IdentityConfig,
    code_challenge,
)
from tests.test_management import ManagementCase


class IdentityTests(ManagementCase):
    identity_transport = True

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

    def code(self, name: str = "alice", app: str = "") -> dict[str, str]:
        """A fresh redeem request for a code Commons issued to the portal."""
        app = app or self.config.portal_origin
        verifier = secrets.token_urlsafe(32)
        code = self.commons.issue(name, app, code_challenge(verifier))
        return {"code": code, "app": self.config.portal_origin, "code_verifier": verifier}

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
        self.assertEqual(
            client.redeem(self.code(app="https://other.example.com"))[0], "invalid_code"
        )
        self.assertEqual(client.redeem(self.code("carol"))[0], "invalid_code")
        # A code is bound to its challenge: another sign-in's verifier spends it.
        stolen = self.code()
        self.assertEqual(
            client.redeem({**stolen, "code_verifier": secrets.token_urlsafe(32)})[0],
            "invalid_code",
        )
        self.assertEqual(client.redeem(stolen)[0], "invalid_code")
        valid = self.code()
        code, verifier = valid["code"], valid["code_verifier"]
        portal = self.config.portal_origin
        for value in (
            {**valid, "extra": True},
            {"code": code, "app": portal},
            {"code": code, "code_verifier": verifier},
            {**valid, "app": None},
            {**valid, "code": None},
            {**valid, "code": ""},
            {**valid, "code": "x" * 129},
            {**valid, "code": "a b"},
            {**valid, "code": "a/b"},
            {**valid, "code": "é"},
            {**valid, "app": portal + "/"},
            {**valid, "app": portal + "/auth"},
            {**valid, "app": portal + "?x=1"},
            {**valid, "app": "HTTP://127.0.0.1:18080"},
            {**valid, "app": "https://user@127.0.0.1:18080"},
            # Development admits loopback apps only.
            {**valid, "app": "https://class.example.com"},
            {**valid, "code_verifier": None},
            {**valid, "code_verifier": ""},
            {**valid, "code_verifier": verifier[:42]},
            {**valid, "code_verifier": "v" * 129},
            {**valid, "code_verifier": verifier[:42] + "/"},
            {**valid, "code_verifier": verifier[:42] + "\n"},
            [code, portal, verifier],
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
        request = {**self.code(), "app": "https://platform.example.com"}
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

        from openstack_platform.management.identity.client import redemption

        code = "11111111-1111-4111-8111-111111111111.credential_-"
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        request = {"code": code, "app": "https://platform.example.com", "code_verifier": verifier}
        self.assertEqual(redemption(request, development=False), request)
        for app in (
            "http://platform.example.com",
            "https://localhost:9443",
            "https://127.0.0.1",
            "https://Platform.example.com",
            "https://platform.example.com/",
        ):
            with self.subTest(app=app), self.assertRaises(ValueError):
                redemption({**request, "app": app}, development=False)

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

    def test_identity_socket_peer_rejected_before_parsing_a_code(self) -> None:
        path = self.sockets / "denied.sock"
        calls = []
        router = Router()
        router.add("POST", "/v1/redeem", lambda req: (calls.append(req), Response(200, {}))[1])
        server = ControllerServer(
            str(path), router, peer_policy=PeerPolicy(frozenset({(os.geteuid() + 1, os.getegid())}))
        )

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
