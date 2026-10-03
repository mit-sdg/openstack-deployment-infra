"""Staff authority, disclosure bounds, read audits and offline recovery evidence."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from openstack_platform.controller.http import HttpError
from openstack_platform.management.backup import restore_database
from openstack_platform.management.broker.api import DEFAULT_CONFIGURATION
from openstack_platform.management.broker.database import (
    MIGRATION_2,
    MIGRATION_3,
    SCHEMA_V1,
    Database,
    validate_database,
)
from openstack_platform.management.broker.staff import ReadLimits
from openstack_platform.management.broker.staff_admin import change_grant, run
from openstack_platform.management.broker.staff_policy import public_url
from openstack_platform.management.common import canonical, digest
from openstack_platform.management.web.server import WebServer
from tests.test_management import ManagementCase


class StaffTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = self.login("alice")
        self.bob = self.login("bob")

    def grant(self, action: str = "grant", **kwargs: object) -> int:
        with self.broker.database.connect(write=True) as db:
            return change_grant(
                db,
                self.config,
                action=action,
                user_id=self.alice,
                issuer=self.config.issuer,
                subject="11111111-1111-4111-8111-111111111111",
                review="local-test",
                **kwargs,
            )

    def mode_login(
        self, mode: str = "staff", name: str = "alice", password: str | None = None
    ) -> str:
        options = self.call("GET", "/v1/auth/options").body
        result = self.call(
            "POST",
            "/v1/auth/login",
            {
                "csrfToken": options["data"]["csrfToken"],
                "username": name,
                "password": password or self.commons.passwords[name],
                "mode": mode,
            },
            headers={
                "cookie": self.config.login_cookie + "=" + options["browser"]["cookies"][0]["value"]
            },
        ).body
        self.tokens[name] = result["browser"]["cookies"][1]["value"]
        bootstrap = self.call("GET", "/v1/session", owner=name).body["data"]
        self.csrf[name] = bootstrap["csrfToken"]
        self.assertEqual(bootstrap["kind"], "staff_read" if mode == "staff" else "owner")
        self.assertEqual(
            result["data"]["returnPath"], "/staff/owners" if mode == "staff" else "/apps"
        )
        return self.tokens[name]

    def staff(self) -> None:
        self.grant()
        self.mode_login()

    def test_staff_status_is_not_disclosed_until_credentials_succeed(self) -> None:
        self.assert_error(
            "INVALID_CREDENTIALS", lambda: self.mode_login(name="bob", password="wrong")
        )
        self.assert_error("STAFF_UNAVAILABLE", lambda: self.mode_login(name="bob"))
        self.assert_error("INVALID_REQUEST", lambda: self.mode_login("admin"))
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM staff_grants").fetchone()[0], 0)

    def test_owner_and_staff_sessions_coexist_are_scoped_and_revoked_together(self) -> None:
        alice_app = self.create()
        bob_app = self.create("bob", "bob-project")
        self.grant()
        owner = self.mode_login("owner")
        owner_csrf = self.csrf["alice"]
        staff = self.mode_login()
        self.assertNotEqual(owner, staff)
        owner_headers = {
            "cookie": self.config.session_cookie + "=" + owner,
            "x-csrf-token": owner_csrf,
        }
        self.assertEqual(
            self.call("GET", f"/v1/apps/{alice_app}", headers=owner_headers).status, 200
        )
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/apps/{bob_app}", headers=owner_headers)
        )
        self.assert_error(
            "ACCESS_DENIED", lambda: self.call("GET", "/v1/staff/owners", headers=owner_headers)
        )
        saved = {
            "expectedRevision": 0,
            "repository": "https://github.com/example/student-app",
            "branch": "main",
            "configuration": DEFAULT_CONFIGURATION,
        }
        self.assertEqual(
            self.call(
                "PUT", f"/v1/apps/{alice_app}/configuration", saved, headers=owner_headers
            ).status,
            200,
        )
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "PUT", f"/v1/apps/{bob_app}/configuration", saved, headers=owner_headers
            ),
        )
        self.assertEqual(
            len(self.call("GET", "/v1/staff/apps", owner="alice").body["data"]["items"]), 2
        )
        self.grant("revoke")
        for token in (owner, staff):
            self.assert_error(
                "SESSION_EXPIRED",
                lambda t=token: self.call(
                    "GET", "/v1/session", headers={"cookie": self.config.session_cookie + "=" + t}
                ),
            )
        self.grant()
        self.assert_error(
            "SESSION_EXPIRED", lambda: self.call("GET", "/v1/session", headers=owner_headers)
        )

    def test_staff_cannot_mutate_own_or_foreign_records_even_with_valid_csrf(self) -> None:
        own = self.create()
        foreign = self.create("bob", "bob-project")
        self.save(own)
        self.staff()
        with self.broker.database.connect() as db:
            before = {
                table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                for table in ("apps", "configurations", "intents", "quotas", "staff_grants")
            }
        calls = len(self.fixture.calls)
        for app in (own, foreign):
            for method, path, body in (
                ("POST", "/v1/apps", {"slug": "staff-cannot-create"}),
                (
                    "PUT",
                    f"/v1/apps/{app}/configuration",
                    {
                        "expectedRevision": 1,
                        "repository": "https://example.com/repo",
                        "branch": "main",
                        "configuration": DEFAULT_CONFIGURATION,
                    },
                ),
                (
                    "POST",
                    f"/v1/apps/{app}/deployments",
                    {"configurationRevision": 1, "commit": "a" * 40},
                ),
                ("POST", f"/v1/intents/{uuid.uuid4()}/resume", {}),
                ("GET", f"/v1/apps/{app}/configuration", None),
                ("GET", f"/v1/apps/{app}/deployments/{uuid.uuid4()}/build-log", None),
            ):
                self.assert_error(
                    "ACCESS_DENIED", lambda m=method, p=path, b=body: self.call(m, p, b, "alice")
                )
        self.assertEqual(len(self.fixture.calls), calls)
        with self.broker.database.connect() as db:
            after = {
                table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                for table in before
            }
        self.assertEqual(before, after)
        for route in self.router._routes:
            if route.method in {"POST", "PUT", "PATCH", "DELETE"} and not any(
                name in route.pattern.pattern for name in ("auth", "logout")
            ):
                # Every registered domain write must be guarded before field parsing.
                path = route.pattern.pattern.removeprefix("^").removesuffix("$")
                path = path.replace("(?P<app>[^/]+)", own).replace(
                    "(?P<intent>[^/]+)", str(uuid.uuid4())
                )
                self.assert_error(
                    "ACCESS_DENIED", lambda r=route, p=path: self.call(r.method, p, {}, "alice")
                )
        self.assertEqual(
            self.call("POST", "/v1/logout", {}, "alice").body["browser"]["status"], 204
        )

    def test_nonstaff_and_unregistered_apps_cause_no_controller_calls(self) -> None:
        calls = len(self.fixture.calls)
        for path in ("/v1/staff/owners", f"/v1/staff/apps/{uuid.uuid4()}"):
            self.assert_error("ACCESS_DENIED", lambda p=path: self.call("GET", p, owner="bob"))
        self.staff()
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/staff/apps/{uuid.uuid4()}", owner="alice")
        )
        self.assertEqual(len(self.fixture.calls), calls)

    def test_catalog_owner_quota_repository_and_operation_projection(self) -> None:
        app = self.create()
        self.save(app)
        self.staff()
        with self.broker.database.connect(write=True) as db:
            db.execute("INSERT INTO quotas VALUES(?,7,3)", (self.alice,))
        owner = self.call("GET", f"/v1/staff/owners/{self.alice}", owner="alice").body["data"]
        self.assertEqual(
            set(owner), {"ownerId", "username", "displayName", "portalEnabled", "quota"}
        )
        self.assertEqual(owner["quota"]["apps"], {"limit": 7, "used": 1, "reserved": 0})
        calls = len(self.fixture.calls)
        catalog = self.call("GET", "/v1/staff/apps", owner="alice").body["data"]
        self.assertEqual(
            catalog["items"][0]["repository"], "https://github.com/example/student-app"
        )
        self.call("GET", "/v1/staff/operations", owner="alice")
        self.assertEqual(len(self.fixture.calls), calls)
        with self.broker.database.connect() as db:
            audits = db.execute("SELECT * FROM staff_read_audit").fetchall()
            self.assertEqual(len(audits), 3)
            self.assertEqual(db.execute("SELECT row_count FROM staff_read_state").fetchone()[0], 3)
            self.assertTrue(all(row["actor_id"] == self.alice for row in audits))

    def test_csrf_origin_and_fetch_metadata(self) -> None:
        self.staff()
        for headers, code in (
            ({"x-csrf-token": ""}, "CSRF_REJECTED"),
            ({"origin": "null"}, "ORIGIN_REJECTED"),
            ({"sec-fetch-site": "cross-site"}, "ORIGIN_REJECTED"),
            ({"origin": "https://foreign.example.com"}, "ORIGIN_REJECTED"),
        ):
            self.assert_error(
                code,
                lambda h=headers: self.call("GET", "/v1/staff/owners", owner="alice", headers=h),
            )
        self.assertEqual(self.call("GET", "/v1/staff/owners", owner="alice").status, 200)
        self.assert_error(
            "ORIGIN_REJECTED",
            lambda: self.call(
                "GET", "/v1/session", owner="alice", headers={"sec-fetch-site": "cross-site"}
            ),
        )
        # Browsers may omit Origin on GET; session-bound CSRF still authorizes this read.
        supplied = {
            "cookie": self.config.session_cookie + "=" + self.tokens["alice"],
            "x-csrf-token": self.csrf["alice"],
            "x-portal-client-address": "127.0.0.1",
        }
        self.assertEqual(
            self.router.dispatch("GET", "/v1/staff/owners", supplied, None).status, 200
        )

    def test_staff_expiry_grant_generation_and_enabled_rechecked(self) -> None:
        now = time.time()
        self.grant(now=now)
        self.broker.auth.clock = lambda: now
        self.mode_login()
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT expires,last_used FROM sessions WHERE kind='staff_read'"
            ).fetchone()
        self.assertEqual(row["expires"], now + 3600)
        self.broker.auth.clock = lambda: now + 600
        self.assert_error("SESSION_EXPIRED", lambda: self.call("GET", "/v1/session", owner="alice"))
        self.broker.auth.clock = lambda: now + 1
        self.mode_login()
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE sessions SET last_used=? WHERE kind='staff_read'", (now + 3600,))
        self.broker.auth.clock = lambda: now + 3601
        self.assert_error("SESSION_EXPIRED", lambda: self.call("GET", "/v1/session", owner="alice"))
        self.broker.auth.clock = lambda: now + 2
        self.mode_login()
        for sql, code in (
            ("UPDATE staff_grants SET generation=generation+1", "SESSION_EXPIRED"),
            ("UPDATE staff_grants SET valid_until=0", "SESSION_EXPIRED"),
        ):
            with self.broker.database.connect(write=True) as db:
                db.execute(sql)
            self.assert_error(code, lambda: self.call("GET", "/v1/session", owner="alice"))
            self.grant(now=now)
            self.mode_login()
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET enabled=0 WHERE id=?", (self.alice,))
        self.assert_error(
            "ACCOUNT_DISABLED", lambda: self.call("GET", "/v1/session", owner="alice")
        )

    def test_pagination_filters_and_malformed_queries_are_bounded(self) -> None:
        app = self.create()
        foreign = self.create("bob", "bob-project")
        self.staff()
        first = self.call("GET", "/v1/staff/owners?limit=1", owner="alice").body["data"]
        second = self.call(
            "GET", "/v1/staff/owners?limit=1&cursor=" + first["nextCursor"], owner="alice"
        ).body["data"]
        self.assertNotEqual(first["items"][0]["ownerId"], second["items"][0]["ownerId"])
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.call(
                "GET", f"/v1/staff/apps?ownerId={self.alice}&cursor={foreign}", owner="alice"
            ),
        )
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "GET", f"/v1/staff/operations?ownerId={self.bob}&applicationId={app}", owner="alice"
            ),
        )
        calls = len(self.fixture.calls)
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
                lambda q=query: self.call("GET", "/v1/staff/apps?" + q, owner="alice"),
            )
        self.assertEqual(len(self.fixture.calls), calls)

    def test_nested_secret_sentinels_and_bad_associations_do_not_escape(self) -> None:
        app = self.create()
        self.save(app)
        deployed = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "a" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        time.sleep(0.7)
        self.broker.journal.dispatch(deployed["intentId"])
        self.staff()
        sentinel = "STAFF_SECRET_SENTINEL"
        original = self.broker.client.request

        def extra(method: str, path: str, *args: object, **kwargs: object) -> tuple[int, dict]:
            status, body = original(method, path, *args, **kwargs)
            body["refs"] = {"secret": sentinel}
            body["safeError"] = sentinel
            if "items" in body:
                for item in body["items"]:
                    item["configuration"] = {"environment": sentinel}
            return status, body

        self.broker.client.request = extra
        for suffix in ("", "/deployments", "/deployments/" + deployed["operationId"]):
            response = self.call("GET", "/v1/staff/apps/" + app + suffix, owner="alice")
            self.assertEqual(response.status, 200)
            self.assertNotIn(sentinel, canonical(response.body))
            self.assertNotIn('"operationId"', canonical(response.body))
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE intents SET operation=?,safe_error=?",
                (canonical({"phase": sentinel, "refs": sentinel}), sentinel),
            )
        operations = self.call("GET", "/v1/staff/operations", owner="alice")
        self.assertNotIn(sentinel, canonical(operations.body))
        self.broker.client.request = lambda *args, **kwargs: (
            200,
            {"applicationId": str(uuid.uuid4()), "deploymentId": deployed["operationId"]},
        )
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "GET", f"/v1/staff/apps/{app}/deployments/{deployed['operationId']}", owner="alice"
            ),
        )
        with self.broker.database.connect() as db:
            self.assertNotIn(
                sentinel, str([tuple(row) for row in db.execute("SELECT * FROM staff_read_audit")])
            )

    def test_revoke_during_slow_read_suppresses_pending_response(self) -> None:
        app = self.create()
        self.staff()
        with self.broker.database.connect(write=True) as db:
            db.execute("DELETE FROM observations")
        entered, release = threading.Event(), threading.Event()
        original = self.broker.client.request

        def slow(*args: object, **kwargs: object) -> tuple[int, dict]:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("fixture timeout")
            return original(*args, **kwargs)

        self.broker.client.request = slow
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.call, "GET", f"/v1/staff/apps/{app}", None, "alice")
            try:
                self.assertTrue(entered.wait(2))
                self.grant("revoke")
            finally:
                release.set()
            with self.assertRaises(HttpError) as error:
                pending.result(timeout=3)
        self.assertEqual(error.exception.status, 401)

    def test_audit_full_or_failed_commit_denies_staff_without_harming_owner(self) -> None:
        self.staff()
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE staff_read_state SET row_count=2000000,pruned_at=?", (time.time(),))
        result = self.call("GET", "/v1/staff/owners", owner="alice")
        self.assertEqual(result.status, 503)
        self.assertNotIn("items", canonical(result.body))
        self.assertEqual(self.call("GET", "/v1/apps", owner="bob").status, 200)
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE staff_read_state SET row_count=0")
            db.execute(
                "CREATE TRIGGER fail_staff_audit BEFORE INSERT ON staff_read_audit BEGIN SELECT RAISE(ABORT,'fixture'); END"
            )
        # Integrity failures must also fail closed, with no metadata body.
        with patch.object(
            self.broker.staff, "audit", side_effect=sqlite3.OperationalError("sentinel")
        ):
            self.assertEqual(self.call("GET", "/v1/staff/owners", owner="alice").status, 503)

    def test_read_budget_capacity_refill_and_pruning(self) -> None:
        limits = ReadLimits()
        for _ in range(10):
            with limits.reserve("user", "address", 1):
                pass
        with self.assertRaises(HttpError):
            with limits.reserve("user", "address", 1):
                pass
        with limits.reserve("user", "address", 2):
            with limits.reserve("user", "address", 3):
                with self.assertRaises(HttpError):
                    with limits.reserve("user", "address", 4):
                        pass
        self.assertEqual(limits.active, 0)
        self.staff()
        self.call("GET", "/v1/staff/owners", owner="alice")
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE staff_read_audit SET created=0")
            db.execute("UPDATE staff_read_state SET pruned_at=0")
        self.call("GET", "/v1/staff/owners", owner="alice")
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT row_count FROM staff_read_state").fetchone()[0], 1)

    def test_repository_urls_are_sanitized(self) -> None:
        for value in (
            "http://example.com/repo",
            "https://user:password@example.com/repo",
            "https://example.com/repo?token=secret",
            "https://example.com/#secret",
            "https://example.com/\nrepo",
            "https://example.com:" + "9" * 8,
            "https://example.com/" + "a" * 512,
        ):
            self.assertIsNone(public_url(value))
        self.assertEqual(public_url("https://example.com/repo"), "https://example.com/repo")

    def test_helper_identity_transaction_and_membership_bound(self) -> None:
        self.assert_error("STAFF_UNAVAILABLE", lambda: self.mode_login())
        with self.broker.database.connect(write=True) as db:
            with self.assertRaises(ValueError):
                change_grant(
                    db,
                    self.config,
                    action="grant",
                    user_id=self.alice,
                    issuer=self.config.issuer,
                    subject="22222222-2222-4222-8222-222222222222",
                    review="review",
                )
        self.grant()
        generation = self.grant("renew")
        self.assertEqual(generation, 2)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            run(self.config, ["inspect", "--user-id", self.alice])
        self.assertIn('"subject"', output.getvalue())
        with patch(
            "openstack_platform.management.broker.staff_admin.management_peer",
            return_value=(os.geteuid() + 1, os.getegid()),
        ):
            with self.assertRaises(ValueError):
                run(dataclasses.replace(self.config, development=False), ["list"])
        self.config.broker_socket.touch()
        with self.assertRaises(ValueError):
            run(self.config, ["list"])
        self.config.broker_socket.unlink()
        with self.broker.database.connect(write=True) as db:
            for _ in range(99):
                user = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO users VALUES(?,?,?,'fixture','Fixture',1,0,0)",
                    (user, self.config.issuer, user),
                )
                db.execute("INSERT INTO staff_grants VALUES(?,1,1,9999999999,0,0)", (user,))
            with self.assertRaises(ValueError):
                change_grant(
                    db,
                    self.config,
                    action="grant",
                    user_id=self.bob,
                    issuer=self.config.issuer,
                    subject="22222222-2222-4222-8222-222222222222",
                    review="review",
                )

    def test_schema_two_migration_invalidates_sessions_preserves_identity_and_checksums(
        self,
    ) -> None:
        path = self.root / "schema-two"
        path.mkdir()
        db = sqlite3.connect(path / "management.sqlite3")
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
        db.close()
        migrated = Database(dataclasses.replace(self.config, state_directory=path))
        with migrated.connect() as connection:
            self.assertEqual(validate_database(connection)[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM staff_grants").fetchone()[0], 0
            )
            self.assertEqual(connection.execute("SELECT apps FROM quotas").fetchone()[0], 9)
            self.assertEqual(
                connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version=3"
                ).fetchone()[0],
                digest(MIGRATION_3),
            )

    def test_restore_disables_stale_grants_and_revokes_all_sessions(self) -> None:
        self.staff()
        snapshot = self.root / "snapshot.sqlite3"
        with (
            self.broker.database.connect() as db,
            contextlib.closing(sqlite3.connect(snapshot)) as backup,
        ):
            db.backup(backup)
        snapshot.chmod(0o600)
        destination = self.root / "restored/management.sqlite3"
        restore_database(snapshot, destination)
        with contextlib.closing(sqlite3.connect(destination)) as db:
            self.assertEqual(validate_database(db)[0], 3)
            self.assertEqual(
                db.execute("SELECT enabled,generation FROM staff_grants").fetchone(), (0, 2)
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(
                db.execute(
                    "SELECT action FROM staff_grant_audit ORDER BY sequence DESC LIMIT 1"
                ).fetchone()[0],
                "restore_disable",
            )

    def test_web_staff_paths_methods_and_return_path_are_closed(self) -> None:
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text("<html>fixture</html>")
        web = WebServer(("127.0.0.1", 0), self.config, assets)
        self.addCleanup(web.server_close)
        headers = {"host": "127.0.0.1:18080"}
        for path in ("/signin?mode=staff", "/staff/owners", "/staff/apps", "/staff/operations"):
            self.assertEqual(web.handle("GET", path, headers, b"").status, 200)
        for path in ("/api/v1/staff/apps", "/api/v1/staff/owners"):
            self.assertEqual(web.handle("POST", path, headers, b"").status, 405)
        for path in ("/api/v1/staff/quotas", "/api/v1/staff/apps/../admin", "/api/v1/admin/apps"):
            self.assertIn(web.handle("GET", path, headers, b"").status, {400, 404})
        web.broker.request = lambda *args, **kwargs: (
            200,
            {"data": {"returnPath": "/staff/owners"}},
        )
        self.assertEqual(web.handle("POST", "/auth/login", headers, b"").status, 200)
        web.broker.request = lambda *args, **kwargs: (
            200,
            {"data": {"returnPath": "/staff/quotas"}},
        )
        self.assertEqual(web.handle("POST", "/auth/login", headers, b"").status, 400)

    def test_identity_rename_preserves_grant_but_reused_username_does_not(self) -> None:
        from openstack_platform.management.dev.commons import USERS

        self.grant()
        subject = USERS["alice"][0]
        with (
            patch.dict(USERS, {"renamed": (subject, "Renamed Instructor")}),
            patch.dict(self.commons.passwords, {"renamed": "local-renamed-password"}),
        ):
            self.mode_login(name="renamed")
            self.assertEqual(
                self.call("GET", "/v1/session", owner="renamed").body["data"]["user"]["id"],
                self.alice,
            )
        with patch.dict(USERS, {"alice": (str(uuid.uuid4()), "New Account")}):
            self.assert_error("STAFF_UNAVAILABLE", lambda: self.mode_login())

    def test_forged_mode_headers_and_forbidden_project_reads_never_elevate(self) -> None:
        self.grant()
        self.mode_login("owner")
        calls = len(self.fixture.calls)
        self.assert_error(
            "ACCESS_DENIED",
            lambda: self.call(
                "GET",
                "/v1/staff/apps",
                owner="alice",
                headers={"x-staff": "true", "x-session-kind": "staff_read"},
            ),
        )
        for path in (
            "/v1/admin/applications",
            "/v1/operations/" + str(uuid.uuid4()),
            "/v1/applications/" + str(uuid.uuid4()) + "/environment",
        ):
            from openstack_platform.management.broker.client import ControllerUnavailable

            with self.assertRaises(ControllerUnavailable):
                self.broker.staff.project(path)
        self.assertEqual(len(self.fixture.calls), calls)

    def test_staff_transport_over_actual_unix_and_web_sockets(self) -> None:
        import http.client

        from openstack_platform.controller.http import ControllerServer, PeerPolicy
        from openstack_platform.management.broker.client import ProjectClient

        self.staff()
        server = ControllerServer(
            str(self.config.broker_socket),
            self.router,
            peer_policy=PeerPolicy(frozenset({(os.geteuid(), os.getegid())})),
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        assets = self.root / "transport-assets"
        assets.mkdir()
        (assets / "index.html").write_text("<html>fixture</html>")
        web = WebServer(("127.0.0.1", 0), self.config, assets)
        web_thread = threading.Thread(
            target=web.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        web_thread.start()
        try:
            headers = {
                "Host": "127.0.0.1:18080",
                "Cookie": self.config.session_cookie + "=" + self.tokens["alice"],
                "X-CSRF-Token": self.csrf["alice"],
            }
            connection = http.client.HTTPConnection("127.0.0.1", web.server_port, timeout=3)
            try:
                connection.request("GET", "/api/v1/staff/owners", headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                self.assertIn(b"ownerId", response.read())
                connection.request("POST", "/api/v1/staff/apps", headers=headers)
                denied = connection.getresponse()
                self.assertEqual(denied.status, 405)
                denied.read()
            finally:
                connection.close()
            status, body = ProjectClient(self.config.broker_socket).request(
                "GET",
                "/v1/staff/owners",
                headers={
                    "cookie": headers["Cookie"],
                    "x-csrf-token": self.csrf["alice"],
                    "x-portal-client-address": "127.0.0.1",
                },
            )
            self.assertEqual(status, 200)
            self.assertNotIn("issuer", canonical(body))
        finally:
            web.shutdown()
            web.server_close()
            server.shutdown()
            server.server_close()

    def test_response_size_audit_projection_and_limiter_memory_bound(self) -> None:
        self.staff()
        with patch("openstack_platform.management.broker.staff.RESPONSE_BYTES", 16):
            result = self.call("GET", "/v1/staff/owners", owner="alice")
        self.assertEqual(result.status, 503)
        self.assertNotIn("items", canonical(result.body))
        limits = ReadLimits()
        for index in range(4096):
            from openstack_platform.management.broker.staff import Bucket

            limits.addresses[str(index)] = Bucket(20, 100)
        with self.assertRaises(HttpError):
            with limits.reserve("user", "new-address", 101):
                pass
        self.assertEqual(len(limits.addresses), 4096)
        with limits.reserve("user", "new-address", 161):
            pass
        with contextlib.ExitStack() as stack:
            for index in range(8):
                stack.enter_context(limits.reserve(str(index // 2), "shared", 200))
            with self.assertRaises(HttpError):
                with limits.reserve("another-user", "shared", 200):
                    pass
        self.assertEqual(limits.active, 0)

    def test_throttled_reads_do_not_refresh_session_activity_or_flood_denial_audit(self) -> None:
        now = time.time()
        self.broker.auth.clock = lambda: now
        self.staff()
        for _ in range(10):
            self.assertEqual(self.call("GET", "/v1/staff/owners", owner="alice").status, 200)
        self.broker.auth.clock = lambda: now + 0.1
        for _ in range(5):
            self.assertEqual(self.call("GET", "/v1/staff/owners", owner="alice").status, 429)
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT last_used FROM sessions WHERE kind='staff_read'").fetchone()[0],
                now,
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM staff_read_audit").fetchone()[0], 11)

    def test_disabled_account_in_staff_mode_gets_generic_availability_after_password(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET enabled=0 WHERE id=?", (self.bob,))
        self.assert_error(
            "INVALID_CREDENTIALS", lambda: self.mode_login(name="bob", password="wrong")
        )
        self.assert_error("STAFF_UNAVAILABLE", lambda: self.mode_login(name="bob"))
        self.assert_error("ACCOUNT_DISABLED", lambda: self.mode_login("owner", name="bob"))
