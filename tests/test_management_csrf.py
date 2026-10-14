"""Stable session CSRF, revocation, expiry, and the removal of token storage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from openstack_platform.controller.http import Request
from openstack_platform.management.broker.accounts import security_change
from openstack_platform.management.broker.auth import Auth
from openstack_platform.management.broker.database import (
    MIGRATION_4,
    Database,
    validate_database,
)
from openstack_platform.management.common import digest
from tests.test_management import ManagementCase


class SessionCsrfTests(ManagementCase):
    def test_parallel_tab_bootstraps_keep_the_first_token_past_thirty_minutes(self) -> None:
        now = self.broker.auth.clock()
        self.broker.auth.clock = lambda: now
        user = self.login()
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET role='staff' WHERE id=?", (user,))
            security_change(db, user)
        self.login()
        first = self.csrf["alice"]
        with ThreadPoolExecutor(max_workers=10) as pool:
            for _refresh in range(10):
                tabs = list(
                    pool.map(
                        lambda _tab: self.call("GET", "/v1/session", owner="alice").body["data"],
                        range(10),
                    )
                )
                self.assertEqual({tab["csrfToken"] for tab in tabs}, {first})
                self.assertEqual(self.call("GET", "/v1/people", owner="alice").status, 200)
                now += 240
        # The session survives broker restart without retaining raw CSRF tokens.
        restarted = Auth(self.config, self.broker.database, clock=lambda: now)
        request = Request(
            "GET",
            "/v1/session",
            {},
            {},
            {
                "cookie": self.config.session_cookie + "=" + self.tokens["alice"],
            },
            None,
        )
        self.assertEqual(restarted.bootstrap(request).body["data"]["csrfToken"], first)
        self.assertEqual(self.call("GET", "/v1/people", owner="alice").status, 200)
        self.assertEqual(
            self.call("POST", "/v1/apps", {"slug": "original-tab-token"}, owner="alice").status,
            201,
        )

    def test_another_session_token_and_malformed_tokens_reject_before_mutation(self) -> None:
        self.login()
        self.login("bob")
        calls = list(self.fixture.calls)
        with self.broker.database.connect() as db:
            last_used = db.execute("SELECT last_used FROM sessions").fetchall()
        for token in (self.csrf["bob"], "", "x" * 43, "é" * 43):
            self.assert_error(
                "CSRF_REJECTED",
                lambda token=token: self.call(
                    "POST",
                    "/v1/apps",
                    {"slug": "csrf-fixture"},
                    owner="alice",
                    headers={"x-csrf-token": token},
                ),
            )
        self.assertEqual(self.fixture.calls, calls)
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM intents").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM apps").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT last_used FROM sessions").fetchall(), last_used)
        # Distinct sessions of the same account also receive distinct CSRF tokens.
        first = self.csrf["alice"]
        self.login()
        self.assertNotEqual(self.csrf["alice"], first)
        self.assert_error(
            "CSRF_REJECTED",
            lambda: self.call(
                "POST",
                "/v1/logout",
                {},
                owner="alice",
                headers={"x-csrf-token": first},
            ),
        )

    def test_logout_and_session_rotation_revoke_the_old_token(self) -> None:
        user = self.login()
        old_cookie, old_csrf = self.tokens["alice"], self.csrf["alice"]
        request = Request(
            "POST",
            "/v1/auth/login",
            {},
            {},
            {
                "cookie": self.config.session_cookie + "=" + old_cookie,
            },
            None,
        )
        with self.broker.database.connect(write=True) as db:
            record = dict(db.execute("SELECT * FROM users WHERE id=?", (user,)).fetchone())
            minted = self.broker.auth.mint(db, record, request)
        self.assert_error(
            "SESSION_EXPIRED",
            lambda: self.call(
                "POST",
                "/v1/logout",
                {},
                owner="alice",
            ),
        )
        self.tokens["alice"] = next(
            cookie["value"]
            for cookie in minted["browser"]["cookies"]
            if cookie["name"] == "session"
        )
        self.csrf["alice"] = self.call("GET", "/v1/session", owner="alice").body["data"][
            "csrfToken"
        ]
        self.assertNotEqual(self.csrf["alice"], old_csrf)
        self.assert_error(
            "CSRF_REJECTED",
            lambda: self.call(
                "POST",
                "/v1/logout",
                {},
                owner="alice",
                headers={"x-csrf-token": old_csrf},
            ),
        )
        self.assertEqual(self.call("POST", "/v1/logout", {}, owner="alice").status, 200)
        self.assert_error(
            "SESSION_EXPIRED",
            lambda: self.call(
                "POST",
                "/v1/apps",
                {"slug": "after-logout"},
                owner="alice",
            ),
        )

    def test_absolute_and_idle_expiry_revoke_csrf_at_the_boundary(self) -> None:
        for expiry in ("absolute", "idle"):
            with self.subTest(expiry=expiry):
                self.broker.auth.clock = lambda: 1000
                self.login()
                with self.broker.database.connect(write=True) as db:
                    db.execute(
                        "UPDATE sessions SET expires=?,last_used=? WHERE token=?",
                        (
                            1100 if expiry == "absolute" else 10000,
                            1099 if expiry == "absolute" else 1100 - self.config.idle_seconds,
                            digest(self.tokens["alice"]),
                        ),
                    )
                self.broker.auth.clock = lambda: 1099
                self.assertEqual(self.call("POST", "/v1/logout", {}, owner="alice").status, 200)
                # Mint again for the exact boundary, keeping its original token.
                self.broker.auth.clock = lambda: 1000
                self.login()
                with self.broker.database.connect(write=True) as db:
                    db.execute(
                        "UPDATE sessions SET expires=?,last_used=? WHERE token=?",
                        (
                            1100 if expiry == "absolute" else 10000,
                            1099 if expiry == "absolute" else 1100 - self.config.idle_seconds,
                            digest(self.tokens["alice"]),
                        ),
                    )
                self.broker.auth.clock = lambda: 1100
                self.assert_error(
                    "SESSION_EXPIRED",
                    lambda: self.call(
                        "POST",
                        "/v1/logout",
                        {},
                        owner="alice",
                    ),
                )

    def test_schema_four_drops_token_table_preserves_sessions_and_checks_checksum(self) -> None:
        self.login()
        before = self.call("GET", "/v1/session", owner="alice").body
        # Reconstruct schema 3 from the otherwise identical current schema.
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "CREATE TABLE csrf_tokens (token TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(token) ON DELETE CASCADE, expires REAL NOT NULL, created REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO csrf_tokens VALUES('old',?,9999999999,0)",
                (digest(self.tokens["alice"]),),
            )
            db.execute("DELETE FROM schema_migrations WHERE version=4")
            db.execute("UPDATE metadata SET version=3")
        upgraded = Database(self.config)
        self.assertEqual(self.call("GET", "/v1/session", owner="alice").body, before)
        with upgraded.connect() as db:
            self.assertEqual(validate_database(db)[0], 4)
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='csrf_tokens'").fetchone()
            )
            self.assertEqual(
                db.execute("SELECT checksum FROM schema_migrations WHERE version=4").fetchone()[0],
                digest(MIGRATION_4),
            )
        Database(self.config)  # Reopening does not reapply the migration.
        with upgraded.connect(write=True) as db:
            db.execute("UPDATE schema_migrations SET checksum='wrong' WHERE version=4")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            Database(self.config)
        with upgraded.connect() as db, self.assertRaisesRegex(ValueError, "checksum mismatch"):
            validate_database(db)
