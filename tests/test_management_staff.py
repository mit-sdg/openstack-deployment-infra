"""Cross-owner metadata projections and immutable hierarchical role sessions."""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from openstack_platform.controller.http import HttpError
from openstack_platform.management.backup import restore_database
from openstack_platform.management.broker.accounts import security_change
from openstack_platform.management.broker.database import (
    MIGRATION_2,
    SCHEMA_V1,
    Database,
    validate_database,
)
from openstack_platform.management.broker.staff import ReadLimits
from openstack_platform.management.broker.staff_policy import public_url
from openstack_platform.management.common import canonical, digest
from tests.test_management import ManagementCase


class StaffTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = self.login()
        self.bob = self.login("bob")

    def staff(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET role='staff' WHERE id=?", (self.alice,))
            security_change(db, self.alice)
        self.login()

    def test_role_is_db_decided_and_staff_can_manage_only_own_apps(self) -> None:
        self.staff()
        own = self.create()
        self.save(own)
        foreign = self.create("bob", "bob-project")
        calls = len(self.fixture.calls)
        for method, path, body in (
            ("GET", f"/v1/apps/{foreign}", None),
            ("PUT", f"/v1/apps/{foreign}/configuration", {}),
            ("POST", f"/v1/apps/{foreign}/deployments", {}),
        ):
            self.assert_error(
                "NOT_FOUND", lambda m=method, p=path, b=body: self.call(m, p, b, "alice")
            )
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertEqual(
            self.call("GET", "/v1/session", owner="alice").body["data"]["role"], "staff"
        )
        self.assertEqual(
            len(self.call("GET", "/v1/staff/apps", owner="alice").body["data"]["items"]), 2
        )
        self.assert_error(
            "ACCESS_DENIED",
            lambda: self.call("GET", "/v1/staff/owners", owner="bob", headers={"x-role": "admin"}),
        )

    def test_catalog_owner_quota_repository_and_operations_are_projected_without_fanout(
        self,
    ) -> None:
        app = self.create()
        self.save(app)
        self.staff()
        with self.broker.database.connect(write=True) as db:
            db.execute("INSERT INTO quotas VALUES(?,7,3)", (self.alice,))
        calls = len(self.fixture.calls)
        owner = self.call("GET", f"/v1/staff/owners/{self.alice}", owner="alice").body["data"]
        self.assertEqual(owner["quota"]["apps"], {"limit": 7, "used": 1, "reserved": 0})
        apps = self.call("GET", "/v1/staff/apps", owner="alice").body["data"]["items"]
        self.assertEqual(apps[0]["repository"], "https://github.com/example/student-app")
        self.call("GET", "/v1/staff/operations", owner="alice")
        self.assertEqual(
            self.fixture.calls[calls:], [("GET", f"/v1/applications/{app}/environment", None)]
        )
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT row_count FROM staff_read_state").fetchone()[0], 3)

    def test_csrf_origin_fetch_metadata_and_closed_queries(self) -> None:
        self.staff()
        for headers, code in (
            ({"x-csrf-token": ""}, "CSRF_REJECTED"),
            ({"origin": "null"}, "ORIGIN_REJECTED"),
            ({"sec-fetch-site": "cross-site"}, "ORIGIN_REJECTED"),
        ):
            self.assert_error(
                code,
                lambda h=headers: self.call("GET", "/v1/staff/owners", owner="alice", headers=h),
            )
        for query in (
            "limit=51",
            "limit=-1",
            "limit=",
            "limit=1&limit=2",
            "scope=all",
            "cursor=bad",
        ):
            self.broker.staff.limits = ReadLimits()
            self.assert_error(
                "INVALID_REQUEST",
                lambda q=query: self.call("GET", "/v1/staff/owners?" + q, owner="alice"),
            )

    def test_scoped_pagination_and_unknown_controller_app(self) -> None:
        self.create()
        foreign = self.create("bob", "bob-project")
        self.staff()
        first = self.call("GET", "/v1/staff/owners?limit=1", owner="alice").body["data"]
        second = self.call(
            "GET", "/v1/staff/owners?limit=1&cursor=" + first["nextCursor"], owner="alice"
        ).body["data"]
        self.assertNotEqual(first["items"], second["items"])
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.call(
                "GET", f"/v1/staff/apps?ownerId={self.alice}&cursor={foreign}", owner="alice"
            ),
        )
        calls = len(self.fixture.calls)
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/staff/apps/{uuid.uuid4()}", owner="alice")
        )
        self.assertEqual(len(self.fixture.calls), calls)

    def test_nested_cache_and_controller_fields_are_excluded(self) -> None:
        app = self.create()
        self.save(app)
        intent = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"configurationRevision": 1, "commit": "a" * 40},
            "alice",
        ).body["data"]
        time.sleep(0.7)
        self.broker.journal.dispatch(intent["intentId"])
        self.staff()
        sentinel = "STAFF_SECRET_SENTINEL"
        original = self.broker.client.request

        def extra(*args: object, **kwargs: object) -> tuple[int, dict]:
            status, body = original(*args, **kwargs)
            body["refs"] = {"environment": sentinel}
            body["safeError"] = sentinel
            for item in body.get("items", []):
                item["configuration"] = {"secret": sentinel}
            return status, body

        self.broker.client.request = extra
        for suffix in ("", "/deployments", "/deployments/" + intent["operationId"]):
            result = self.call("GET", "/v1/staff/apps/" + app + suffix, owner="alice")
            self.assertEqual(result.status, 200)
            self.assertNotIn(sentinel, canonical(result.body))
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE intents SET operation=?,safe_error=?",
                (canonical({"phase": sentinel, "refs": sentinel}), sentinel),
            )
        self.assertNotIn(
            sentinel, canonical(self.call("GET", "/v1/staff/operations", owner="alice").body)
        )
        self.broker.client.request = lambda *args, **kwargs: (
            200,
            {"applicationId": str(uuid.uuid4()), "deploymentId": intent["operationId"]},
        )
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "GET", f"/v1/staff/apps/{app}/deployments/{intent['operationId']}", owner="alice"
            ),
        )

    def test_security_change_during_slow_read_suppresses_result(self) -> None:
        app = self.create()
        self.staff()
        with self.broker.database.connect(write=True) as db:
            db.execute("DELETE FROM observations")
        entered, release = threading.Event(), threading.Event()
        original = self.broker.client.request

        def slow(*args: object, **kwargs: object) -> tuple[int, dict]:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("fixture")
            return original(*args, **kwargs)

        self.broker.client.request = slow
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.call, "GET", f"/v1/staff/apps/{app}", None, "alice")
            try:
                self.assertTrue(entered.wait(2))
                with self.broker.database.connect(write=True) as db:
                    security_change(db, self.alice)
            finally:
                release.set()
            with self.assertRaises(HttpError) as error:
                pending.result(timeout=3)
        self.assertEqual(error.exception.status, 401)

    def test_audit_full_and_commit_failure_deny_staff_and_preserve_owner_access(self) -> None:
        self.staff()
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE staff_read_state SET row_count=2000000,pruned_at=?", (time.time(),))
        self.assertEqual(self.call("GET", "/v1/staff/owners", owner="alice").status, 503)
        self.assertEqual(self.call("GET", "/v1/apps", owner="bob").status, 200)
        with patch.object(
            self.broker.staff, "audit", side_effect=sqlite3.OperationalError("sentinel")
        ):
            self.assertEqual(self.call("GET", "/v1/staff/owners", owner="alice").status, 503)

    def test_read_budget_response_size_and_throttle_do_not_refresh_activity(self) -> None:
        now = time.time()
        self.broker.auth.clock = lambda: now
        self.staff()
        for _ in range(10):
            self.call("GET", "/v1/staff/owners", owner="alice")
        self.broker.auth.clock = lambda: now + 0.1
        self.assertEqual(self.call("GET", "/v1/staff/owners", owner="alice").status, 429)
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT last_used FROM sessions WHERE kind='staff'").fetchone()[0], now
            )
        self.broker.staff.limits = ReadLimits()
        with patch("openstack_platform.management.broker.staff.RESPONSE_BYTES", 16):
            self.assertEqual(self.call("GET", "/v1/staff/owners", owner="alice").status, 503)

    def test_restore_keeps_roles_deletes_tokens_and_increments_generation(self) -> None:
        self.staff()
        snapshot = self.root / "snapshot.sqlite3"
        with (
            self.broker.database.connect() as db,
            contextlib.closing(sqlite3.connect(snapshot)) as target,
        ):
            db.backup(target)
        snapshot.chmod(0o600)
        destination = self.root / "restore/management.sqlite3"
        restore_database(snapshot, destination)
        with contextlib.closing(sqlite3.connect(destination)) as db:
            self.assertEqual(validate_database(db)[0], 3)
            self.assertEqual(
                db.execute("SELECT role FROM users WHERE id=?", (self.alice,)).fetchone()[0],
                "staff",
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM account_tokens").fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT valid_after FROM token_policy").fetchone()[0], 0)

    def test_schema_two_upgrade_preserves_quota_invalidates_sessions_and_defaults_owner(
        self,
    ) -> None:
        path = self.root / "schema-two"
        path.mkdir()
        with contextlib.closing(sqlite3.connect(path / "management.sqlite3")) as db:
            db.executescript(SCHEMA_V1 + MIGRATION_2)
            db.execute(
                "INSERT INTO metadata VALUES(2,?)",
                (digest(self.config.portal_origin + "\n" + self.config.issuer),),
            )
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT)")
            db.executemany(
                "INSERT INTO schema_migrations VALUES(?,?)",
                [(1, digest(SCHEMA_V1)), (2, digest(MIGRATION_2))],
            )
            db.execute(
                "INSERT INTO users VALUES(?,?,?,'alice','Alice',1,0,0)",
                (self.alice, self.config.issuer, str(uuid.uuid4())),
            )
            db.execute("INSERT INTO sessions VALUES('old',?,0,0,9999999999)", (self.alice,))
            db.execute("INSERT INTO quotas VALUES(?,9,2)", (self.alice,))
            db.commit()
        migrated = Database(dataclasses.replace(self.config, state_directory=path))
        with migrated.connect() as db:
            self.assertEqual(validate_database(db)[0], 3)
            self.assertEqual(
                db.execute("SELECT role,generation FROM users").fetchone()[:], ("owner", 1)
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT apps FROM quotas").fetchone()[0], 9)

    def test_repository_sanitization(self) -> None:
        for url in (
            "http://example.com",
            "https://user:pw@example.com",
            "https://example.com/?secret=x",
            "https://example.com/#secret",
            "https://example.com/\n",
            "https://example.com/" + "x" * 512,
        ):
            self.assertIsNone(public_url(url))
        self.assertEqual(public_url("https://example.com/repo"), "https://example.com/repo")
