"""Local-admin app actions, project capability delta and secret-free reuse."""

from __future__ import annotations

import copy
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from openstack_platform.controller.http import HttpError
from openstack_platform.management.broker import bootstrap
from openstack_platform.management.broker.accounts import security_change
from openstack_platform.management.broker.class_reads import ReadLimits
from openstack_platform.management.broker.client import ControllerUnavailable
from openstack_platform.management.broker.sizing import FlavorCache
from openstack_platform.management.common import canonical, digest, strict_json
from openstack_platform.management.web.server import WebServer
from tests import test_management_accounts as account_fixtures
from tests.test_management import ManagementCase


class AdminApplicationTests(ManagementCase):
    anonymous = account_fixtures.AccountsTests.anonymous
    begin = account_fixtures.AccountsTests.begin
    finish = account_fixtures.AccountsTests.finish
    admin = account_fixtures.AccountsTests.admin

    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()
        self.broker.auth.clock = lambda: self.now
        folder = bootstrap.enrollment_file(self.config).parent
        folder.mkdir(parents=True, mode=0o2750)
        folder.chmod(0o2750)
        self.admin_user = self.admin()["user"]["id"]
        self.owner = self.login("alice")
        self.app_id = self.create()
        self.save(self.app_id)
        self.fixture.delay = 0
        self.prefix = f"/v1/apps/{self.app_id}"

    def complete(self, response: Any, actor: str = "admin") -> Any:
        result = response.body["data"]
        self.broker.journal.dispatch(result["intentId"])
        return self.call("GET", f"/v1/intents/{result['intentId']}", owner=actor).body["data"]

    def staff(self, name: str = "taylor") -> str:
        user = self.login(name)
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET role='staff' WHERE id=?", (user,))
            security_change(db, user)
        self.login(name)
        return user

    def routes(self) -> list[tuple[str, str, str]]:
        """Every app-administration route as (method, route, a concrete path)."""
        return [
            (
                method,
                route,
                route.replace("{app}", self.app_id)
                .replace("{resource}", str(uuid.uuid4()))
                .replace("{deployment}", str(uuid.uuid4()))
                .replace("{user}", str(uuid.uuid4()))
                .replace("{key}", "TOKEN"),
            )
            for method, route, _handler in self.broker.app_management.routes()
        ]

    def adopted(self, *, identity: bool = True, bindings: bool = False) -> str:
        identifier = str(uuid.uuid4())
        self.fixture.seed_operator_app(
            identifier, self.config.commons_origin if identity else "https://operator.example.test"
        )
        if bindings:
            resource = str(uuid.uuid4())
            self.fixture.resources[resource] = {
                "resourceId": resource,
                "applicationId": identifier,
                "type": "postgres",
                "name": "default",
                "lifecycleState": "active",
                "createdAt": time.time(),
                "lastVerifiedAt": None,
            }
            snapshot = self.fixture.deployments[self.fixture.apps[identifier]["activeDeploymentId"]]
            snapshot["configuration"] = copy.deepcopy(snapshot["configuration"])
            snapshot["configuration"]["storageBindings"] = [
                {"resourceId": resource, "outputs": {"url": "DATABASE_URL"}}
            ]
        if bindings:
            snapshot["configurationSha256"] = digest(canonical(snapshot["configuration"]))
        self.call(
            "POST",
            "/v1/all-apps/adopt",
            {"applicationId": identifier},
            "admin",
        )
        return identifier

    def test_catalog_refreshes_stale_observations_without_waiting_and_single_flight(self) -> None:
        started, release = threading.Event(), threading.Event()
        with self.broker.database.connect(write=True) as db:
            db.execute("DELETE FROM observations WHERE app_id=?", (self.app_id,))
        original = self.broker.client.request
        reads: list[str] = []

        def slow(method: str, path: str, *args: Any, **kwargs: Any) -> Any:
            reads.append(path)
            started.set()
            if not release.wait(5):
                raise AssertionError("controller was not released")
            return original(method, path, *args, **kwargs)

        with patch.object(self.broker.client, "request", side_effect=slow):
            with ThreadPoolExecutor(max_workers=1) as caller:
                try:
                    response = caller.submit(self.call, "GET", "/v1/all-apps", owner="admin")
                    page = response.result(timeout=1).body["data"]["items"]
                    self.assertTrue(started.wait(1))
                    self.assertEqual(page[0]["appState"], "unknown")
                    self.assertTrue(page[0]["refreshing"])
                    self.assertIsNone(page[0]["observedAt"])
                    self.call("GET", "/v1/all-apps", owner="admin")
                    self.assertEqual(len(reads), 1)
                finally:
                    release.set()
                    self.broker.app_management.close()
        with self.broker.database.connect() as db:
            cached = db.execute(
                "SELECT * FROM observations WHERE app_id=?", (self.app_id,)
            ).fetchone()
        self.assertIsNotNone(cached)
        self.assertFalse(strict_json(cached["body"].encode())["stale"])
        self.assertEqual(self.broker.app_management.refreshing, set())
        # A successful refresh makes the following response settled and quiet.
        item = self.call("GET", "/v1/all-apps", owner="admin").body["data"]["items"][0]
        self.assertEqual(item["appState"], "not_deployed")
        self.assertFalse(item["refreshing"])
        self.assertIsNotNone(item["observedAt"])

    def test_catalog_refreshes_only_stale_rows_on_the_returned_page(self) -> None:
        second = self.create(slug="second-app")
        third = self.create(owner="admin", slug="third-app")
        with self.broker.database.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM apps")]
        for row in rows:
            self.broker.app_model(row)
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE observations SET updated=? WHERE app_id IN (?,?)",
                (time.time() - 31, second, third),
            )
        with patch.object(self.broker, "app_model", wraps=self.broker.app_model) as read:
            first = self.call("GET", "/v1/all-apps?limit=1", owner="admin").body["data"]
            self.broker.app_management.close()
            self.assertEqual([call.args[0]["id"] for call in read.call_args_list], [third])
            self.assertEqual(len(first["items"]), 1)
        self.assertNotEqual(second, third)

    def test_catalog_skips_fresh_observations(self) -> None:
        with self.broker.database.connect() as db:
            row = dict(db.execute("SELECT * FROM apps WHERE id=?", (self.app_id,)).fetchone())
        self.broker.app_model(row)
        with (
            patch.object(self.broker, "app_model") as read,
            patch.object(self.broker.client, "request") as controller,
        ):
            item = self.call("GET", "/v1/all-apps", owner="admin").body["data"]["items"][0]
            self.broker.app_management.close()
            read.assert_not_called()
            controller.assert_not_called()
        self.assertFalse(item["refreshing"])

    def test_background_refresh_has_four_workers_and_a_bounded_single_flight_queue(self) -> None:
        release = threading.Event()
        condition = threading.Condition()
        active = peak = started = 0
        rows = [
            {"id": str(uuid.uuid4()), "lifecycle": "ready", "observation_updated": None}
            for _ in range(105)
        ]

        def slow(_row: dict[str, Any], **_kwargs: Any) -> None:
            nonlocal active, peak, started
            with condition:
                active += 1
                started += 1
                peak = max(peak, active)
                condition.notify_all()
            release.wait(5)
            with condition:
                active -= 1

        with patch.object(self.broker, "app_model", side_effect=slow) as read:
            try:
                self.broker.app_management.refresh_observations(rows[:100])
                with condition:
                    self.assertTrue(condition.wait_for(lambda: started == 4, timeout=1))
                self.broker.app_management.refresh_observations(rows)
                self.assertEqual(len(self.broker.app_management.refreshing), 100)
                self.assertEqual(read.call_count, 4)
            finally:
                release.set()
                # Wait for queued work too, so every admitted app can be counted.
                self.broker.app_management.refresh_pool.shutdown(wait=True)
            self.assertEqual(read.call_count, 100)
            self.assertEqual(peak, 4)
            self.assertEqual(self.broker.app_management.refreshing, set())

    def test_background_refresh_failure_keeps_last_observation_and_releases_single_flight(
        self,
    ) -> None:
        with self.broker.database.connect() as db:
            row = dict(db.execute("SELECT * FROM apps WHERE id=?", (self.app_id,)).fetchone())
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE observations SET updated=? WHERE app_id=?", (time.time() - 31, self.app_id)
            )
            before = tuple(
                db.execute(
                    "SELECT body,updated FROM observations WHERE app_id=?", (self.app_id,)
                ).fetchone()
            )
        row["observation_updated"] = before[1]
        with patch.object(
            self.broker.client, "request", side_effect=ControllerUnavailable("offline")
        ):
            self.broker.app_management.refresh_observations([row])
            self.broker.app_management.refresh_pool.shutdown(wait=True)
        self.assertEqual(self.broker.app_management.refreshing, set())
        with self.broker.database.connect() as db:
            after = tuple(
                db.execute(
                    "SELECT body,updated FROM observations WHERE app_id=?", (self.app_id,)
                ).fetchone()
            )
        self.assertEqual(after, before)

    def test_background_observation_reconciles_an_externally_deleted_app(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE observations SET updated=0 WHERE app_id=?", (self.app_id,))
        del self.fixture.apps[self.app_id]
        self.call("GET", "/v1/all-apps", owner="admin")
        self.broker.app_management.close()
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT lifecycle FROM apps WHERE id=?", (self.app_id,)).fetchone()[0],
                "deleted",
            )
        self.assertEqual(self.call("GET", "/v1/all-apps", owner="admin").body["data"]["items"], [])

    def test_catalog_uses_last_known_state_for_ten_minutes(self) -> None:
        now = time.time()
        body = canonical(
            {
                "acceptedDeployment": {"acceptedAt": "2026-01-01T00:00:00Z"},
                "desiredRunning": True,
                "health": {"allocationHealthy": True, "routeHealthy": True},
            }
        )
        with (
            patch.object(self.broker.app_management, "refresh_observations", return_value=set()),
            patch(
                "openstack_platform.management.broker.app_management.time.time", return_value=now
            ),
        ):
            for age, expected in (
                (29, "healthy"),
                (31, "healthy"),
                (599, "healthy"),
                (601, "unknown"),
            ):
                with self.subTest(age=age), self.broker.database.connect(write=True) as db:
                    db.execute(
                        "INSERT INTO observations VALUES(?,?,?) ON CONFLICT(app_id) DO UPDATE SET body=excluded.body,updated=excluded.updated",
                        (self.app_id, body, now - age),
                    )
                item = self.call("GET", "/v1/all-apps", owner="admin").body["data"]["items"][0]
                self.assertEqual(item["appState"], expected)
                filtered = self.call("GET", f"/v1/all-apps?status={expected}", owner="admin").body[
                    "data"
                ]["items"]
                self.assertEqual(len(filtered), 1)
            with self.broker.database.connect(write=True) as db:
                db.execute("DELETE FROM observations WHERE app_id=?", (self.app_id,))
            self.assertEqual(
                self.call("GET", "/v1/all-apps", owner="admin").body["data"]["items"][0][
                    "appState"
                ],
                "unknown",
            )

    def test_sizing_enrichment_is_cached_and_remains_staff_only(self) -> None:
        for _ in range(2):
            size = self.call("GET", self.prefix, owner="admin").body["data"]["sizing"]
            self.assertEqual((size["vcpus"], size["ram_mib"]), (1, 2048))
        self.assertEqual(
            sum(path == "/v1/flavors" for _method, path, _key in self.fixture.calls), 1
        )
        self.assertNotIn("sizing", self.call("GET", self.prefix, owner="alice").body["data"])

    def test_staff_and_admin_read_sizes_and_forward_the_exact_plan(self) -> None:
        self.staff()
        for actor in ("taylor", "admin"):
            sizes = self.call("GET", self.prefix + "/sizes", owner=actor).body["data"]["items"]
            self.assertTrue(
                all(
                    set(item) == {"flavor_id", "name", "vcpus", "ram_mib", "disk_gib"}
                    for item in sizes
                )
            )
            plan = self.call("GET", self.prefix + "/resize-plan?flavor=200", owner=actor).body[
                "data"
            ]
            self.assertEqual(plan["flavor"]["name"], "worker-large")
            response = self.call(
                "POST",
                self.prefix + "/deployments",
                {"configurationRevision": 1, "commit": "a" * 40, "plan": plan},
                actor,
            )
            with self.broker.database.connect() as db:
                stored = strict_json(
                    db.execute(
                        "SELECT body FROM intents WHERE id=?", (response.body["data"]["intentId"],)
                    )
                    .fetchone()[0]
                    .encode()
                )
            self.assertEqual(stored["plan"], plan)
            self.assertEqual(self.complete(response, actor)["state"], "succeeded")

    def test_staff_and_admin_set_and_reset_builder_without_changing_worker(self) -> None:
        self.staff()
        self.fixture.apps[self.app_id]["requiresMaintenance"] = True
        before = copy.deepcopy(self.fixture.apps[self.app_id])
        for actor in ("taylor", "admin"):
            current = self.call("GET", self.prefix + "/builder-size", owner=actor).body["data"]
            self.assertTrue(current["useDefault"])
            change = self.call(
                "PUT",
                self.prefix + "/builder-size",
                {"flavor": "200", "expectedFlavor": None},
                actor,
            )
            self.assertEqual(self.complete(change, actor)["state"], "succeeded")
            current = self.call("GET", self.prefix + "/builder-size", owner=actor).body["data"]
            self.assertFalse(current["useDefault"])
            self.assertEqual(current["flavor"]["name"], "worker-large")
            reset = self.call(
                "PUT",
                self.prefix + "/builder-size",
                {"flavor": None, "expectedFlavor": "worker-large"},
                actor,
            )
            self.assertEqual(self.complete(reset, actor)["state"], "succeeded")
        self.assertEqual(self.fixture.apps[self.app_id], before)
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM admin_audit WHERE action='app_builder_size'"
                ).fetchone()[0],
                4,
            )

    def test_only_admins_manage_default_builder_and_its_intents(self) -> None:
        self.staff()
        path = "/v1/settings/default-builder-size"
        calls = len(self.fixture.calls)
        for actor in ("alice", "taylor"):
            for method in ("GET", "PUT"):
                self.assert_error(
                    "ACCESS_DENIED",
                    lambda actor=actor, method=method: self.call(
                        method,
                        path,
                        None
                        if method == "GET"
                        else {"flavor": "200", "expectedFlavor": "builder-small"},
                        actor,
                    ),
                )
        self.assertEqual(len(self.fixture.calls), calls)
        current = self.call("GET", path, owner="admin").body["data"]
        self.assertEqual(current["flavor"]["ram_mib"], 1024)
        changed = self.call(
            "PUT", path, {"flavor": "200", "expectedFlavor": "builder-small"}, "admin"
        )
        identifier = changed.body["data"]["intentId"]
        self.assert_error(
            "ACCESS_DENIED", lambda: self.call("GET", f"/v1/intents/{identifier}", owner="taylor")
        )
        self.assert_error(
            "ACCESS_DENIED",
            lambda: self.call("POST", f"/v1/intents/{identifier}/resume", {}, "taylor"),
        )
        self.assertEqual(self.complete(changed)["state"], "succeeded")
        current = self.call("GET", self.prefix + "/builder-size", owner="admin").body["data"]
        self.assertEqual(current["flavor"]["name"], "worker-large")
        self.assertTrue(current["useDefault"])
        with self.broker.database.connect() as db:
            actions = {row[0] for row in db.execute("SELECT action FROM admin_audit")}
        self.assertIn("default_builder_size_requested", actions)
        self.assertIn("default_builder_size_succeeded", actions)
        self.assertEqual(self.call("GET", "/v1/activity", owner="taylor").status, 200)
        self.assertEqual(self.call("GET", "/v1/activity", owner="admin").status, 200)

    def test_builder_reads_require_csrf_and_same_origin_metadata(self) -> None:
        for path in (
            self.prefix + "/sizes",
            self.prefix + "/resize-plan?flavor=200",
            self.prefix + "/builder-size",
            "/v1/settings/default-builder-size",
        ):
            self.assert_error(
                "CSRF_REJECTED",
                lambda path=path: self.call(
                    "GET", path, owner="admin", headers={"x-csrf-token": ""}
                ),
            )
            self.assert_error(
                "ORIGIN_REJECTED",
                lambda path=path: self.call(
                    "GET",
                    path,
                    owner="admin",
                    headers={
                        "origin": "https://other.example.test",
                        "sec-fetch-site": "cross-site",
                    },
                ),
            )

    def test_older_controller_reports_unavailable_sizes_and_keeps_other_reads(self) -> None:
        original = self.broker.client.request

        def older(method, path, *args, **bounds):
            if path == "/v1/flavors" or path == "/v1/settings/default-builder-size":
                return 404, {"error": {"code": "NOT_FOUND"}}
            return original(method, path, *args, **bounds)

        with patch.object(self.broker.client, "request", side_effect=older):
            self.assert_error(
                "SIZING_UNAVAILABLE",
                lambda: self.call("GET", self.prefix + "/sizes", owner="admin"),
            )
            self.assert_error(
                "SIZING_UNAVAILABLE",
                lambda: self.call("GET", "/v1/settings/default-builder-size", owner="admin"),
            )
            self.assertEqual(
                self.call("GET", self.prefix + "/configuration", owner="admin").status, 200
            )

    def test_owners_denied_every_app_administration_action_before_lookup(self) -> None:
        calls = len(self.fixture.calls)
        for method, _route, path in self.routes():
            with self.subTest(path=path, method=method):
                self.assert_error(
                    "ACCESS_DENIED",
                    lambda method=method, path=path: self.call(
                        method, path, {} if method != "GET" else None, "alice"
                    ),
                )
        self.assertEqual(len(self.fixture.calls), calls)

    def test_staff_pass_every_app_action_without_step_up(self) -> None:
        self.staff()
        for method, _route, path in self.routes():
            body = {} if method != "GET" else None
            with self.subTest(path=path, method=method):
                try:
                    self.call(method, path, body, "taylor")
                except HttpError as error:
                    self.assertNotIn(
                        error.code, {"ACCESS_DENIED", "SESSION_EXPIRED", "STEP_UP_REQUIRED"}
                    )

    def test_staff_manage_any_app_and_admins_see_it_in_the_audit(self) -> None:
        staff = self.staff()
        self.login("bob")
        items = self.call("GET", "/v1/all-apps", owner="taylor").body["data"]["items"]
        self.assertEqual([item["applicationId"] for item in items], [self.app_id])
        detail = self.call("GET", self.prefix, owner="taylor").body["data"]
        self.assertEqual(detail["ownerId"], self.owner)
        saved = self.call("GET", self.prefix + "/configuration", owner="taylor").body["data"]
        self.call(
            "PUT",
            self.prefix + "/configuration",
            {
                "expectedRevision": 1,
                "repository": saved["repository"],
                "branch": "other",
                "configuration": saved["configuration"],
            },
            "taylor",
        )
        value = "STAFF_SECRET_SENTINEL_51734"
        changed = self.call("PUT", self.prefix + "/environment/TOKEN", {"value": value}, "taylor")
        self.assertEqual(self.complete(changed, "taylor")["state"], "succeeded")
        names = self.call("GET", self.prefix + "/environment", owner="taylor").body["data"]
        self.assertEqual([item["name"] for item in names["items"]], ["TOKEN"])
        created = self.call("POST", self.prefix + "/storage", {"type": "s3"}, "taylor")
        self.assertEqual(self.complete(created, "taylor")["state"], "succeeded")
        resource = self.call("GET", self.prefix + "/storage", owner="taylor").body["data"]["items"][
            0
        ]["resourceId"]
        verified = self.call("POST", self.prefix + f"/storage/{resource}/verify", {}, "taylor")
        self.assertEqual(self.complete(verified, "taylor")["state"], "succeeded")
        deployed = self.call(
            "POST",
            self.prefix + "/deployments",
            {"configurationRevision": 2, "commit": "a" * 40, "maintenance": False},
            "taylor",
        )
        self.assertNotIn(self.complete(deployed, "taylor")["state"], {"blocked", "failed"})
        self.call("GET", self.prefix + "/deployments", owner="taylor")
        self.call("POST", self.prefix + "/source-key", {}, "taylor")
        team = self.call("POST", self.prefix + "/members", {"username": "bob"}, "taylor")
        self.assertEqual(len(team.body["data"]["items"]), 2)
        # A member is no admin: bob still can't use app administration.
        self.assertEqual(
            self.call("GET", self.prefix, owner="bob").body["data"]["access"], "member"
        )
        # Staff act without the owner's quota or their own: nothing held them back.
        with self.broker.database.connect() as db:
            intents = db.execute(
                "SELECT kind,state,body FROM intents WHERE user_id=?", (staff,)
            ).fetchall()
            audited = db.execute(
                "SELECT target_id,action,details FROM admin_audit WHERE actor_id=?", (staff,)
            ).fetchall()
            contents = "\n".join(db.iterdump())
        self.assertTrue(intents)
        for row in intents:
            self.assertIs(strict_json(row["body"].encode())["_portalAdmin"], True)
            self.assertNotEqual(row["state"], "blocked")
        self.assertEqual({row["target_id"] for row in audited}, {self.owner})
        self.assertLessEqual(
            {
                "app_save_configuration",
                "app_env_set",
                "app_storage_create",
                "app_storage_verify",
                "app_deploy",
                "app_source_key",
                "app_member_add",
            },
            {row["action"] for row in audited},
        )
        self.assertNotIn(value, contents)
        # Admins read the staff member's changes in the global audit.
        log = self.call("GET", "/v1/audit", owner="admin").body["data"]["items"]
        self.assertIn(
            ("taylor", "app_deploy"), {(row["actorUsername"], row["action"]) for row in log}
        )
        # Staff still can't read that audit or manage accounts.
        self.assert_error("ACCESS_DENIED", lambda: self.call("GET", "/v1/audit", owner="taylor"))

    def test_task_routes_filter_before_pagination_and_remove_old_namespaces(self) -> None:
        self.staff()
        second = self.create("alice", "another-app")
        page = self.call("GET", "/v1/all-apps?q=another&limit=1", owner="taylor").body["data"]
        self.assertEqual([app["applicationId"] for app in page["items"]], [second])
        self.assertEqual(page["items"][0]["appState"], "not_deployed")
        self.assertEqual(
            self.call("GET", "/v1/all-apps?status=healthy", owner="taylor").body["data"]["items"],
            [],
        )
        people = self.call("GET", "/v1/people", owner="taylor").body["data"]["items"]
        self.assertIn("staff", {person["role"] for person in people})
        for path in (
            "/v1/staff/owners",
            "/v1/staff/apps",
            "/v1/staff/operations",
            "/v1/admin-apps",
            "/v1/accounts",
            "/v1/account-audit",
        ):
            self.assert_error("NOT_FOUND", lambda path=path: self.call("GET", path, owner="admin"))
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM app_members WHERE app_id=?", (self.app_id,)
                ).fetchone()[0],
                0,
            )
        for suffix in (
            "",
            "/configuration",
            "/deployments",
            "/environment",
            "/storage",
            "/members",
            "/logs",
        ):
            self.assertEqual(self.call("GET", self.prefix + suffix, owner="taylor").status, 200)
        with self.broker.database.connect() as db:
            self.assertGreater(
                db.execute(
                    "SELECT COUNT(*) FROM staff_read_audit WHERE actor_id=(SELECT id FROM users WHERE username='taylor')"
                ).fetchone()[0],
                6,
            )

    def test_catalog_attention_keeps_environment_resubmission_private_and_health_fresh(
        self,
    ) -> None:
        self.staff()
        changed = self.call(
            "PUT",
            f"/v1/apps/{self.app_id}/environment/TOKEN",
            {"value": "private fixture value"},
            "alice",
        ).body["data"]
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE intents SET state='blocked' WHERE id=?", (changed["intentId"],))
            db.execute("UPDATE observations SET updated=0 WHERE app_id=?", (self.app_id,))
        for role in ("taylor", "admin"):
            page = self.call("GET", "/v1/all-apps?status=attention", owner=role).body["data"]
            self.assertEqual([app["applicationId"] for app in page["items"]], [self.app_id])
            intent = page["items"][0]["attention"][0]
            self.assertIsNone(intent["retryKey"])
            self.assertFalse(intent["requiresResubmit"])
            self.assertFalse(intent["canResume"])
        self.assertIsNotNone(
            self.call("GET", f"/v1/apps/{self.app_id}/activity?attention=1", owner="alice").body[
                "data"
            ]["items"][0]["retryKey"]
        )
        # Cached health must not stay Healthy indefinitely without a fresh read.
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE observations SET body=?,updated=0 WHERE app_id=?",
                (
                    canonical(
                        {
                            "acceptedDeployment": {"acceptedAt": "2026-01-01T00:00:00Z"},
                            "desiredRunning": True,
                            "health": {"allocationHealthy": True, "routeHealthy": True},
                        }
                    ),
                    self.app_id,
                ),
            )
        self.assertEqual(
            self.call("GET", "/v1/all-apps?status=unknown", owner="taylor").body["data"]["items"][
                0
            ]["appState"],
            "unknown",
        )

    def test_staff_deploy_retained_ipv4_without_consent(self) -> None:
        self.staff()
        imported = self.adopted()
        response = self.call(
            "POST",
            f"/v1/apps/{imported}/deployments",
            {"configurationRevision": 7, "commit": "a" * 40, "maintenance": False},
            "taylor",
        )
        self.assertEqual(response.status, 202)
        self.assertNotIn(self.complete(response, "taylor")["state"], {"blocked", "failed"})
        with self.broker.database.connect() as db:
            body = db.execute(
                "SELECT body FROM intents WHERE id=?", (response.body["data"]["intentId"],)
            ).fetchone()[0]
        self.assertIs(strict_json(body.encode())["maintenance"], True)

    def test_staff_create_adopt_reassign_delete_and_audit_without_step_up(self) -> None:
        staff = self.staff()
        self.now += 301
        created = self.call(
            "POST", "/v1/all-apps", {"slug": "staff-created", "ownerId": self.owner}, "taylor"
        )
        self.assertEqual(created.status, 201)
        identifier = str(uuid.uuid4())
        self.fixture.seed_operator_app(identifier, self.config.commons_origin)
        adopted = self.call(
            "POST",
            "/v1/all-apps/adopt",
            {"applicationId": identifier, "ownerId": self.owner},
            "taylor",
        )
        self.assertEqual(adopted.status, 201)
        self.assertEqual(
            self.call(
                "PUT",
                self.prefix + "/owner",
                {"ownerId": staff, "expectedOwnerId": self.owner},
                "taylor",
            ).status,
            200,
        )
        response = self.call("POST", self.prefix + "/storage", {"type": "postgres"}, "taylor")
        self.assertEqual(self.complete(response, "taylor")["state"], "succeeded")
        resource = self.call("GET", self.prefix + "/storage", owner="taylor").body["data"]["items"][
            0
        ]
        path = self.prefix + "/storage/" + resource["resourceId"]
        self.assert_error(
            "CONFIRMATION_REQUIRED",
            lambda: self.call(
                "DELETE",
                path,
                {"confirmation": "wrong"},
                "taylor",
                headers={"idempotency-key": str(uuid.uuid4())},
            ),
        )
        removed = self.call(
            "DELETE",
            path,
            {"confirmation": "student-app postgres"},
            "taylor",
            headers={"idempotency-key": str(uuid.uuid4())},
        )
        self.assertEqual(self.complete(removed, "taylor")["state"], "succeeded")
        with self.broker.database.connect() as db:
            actions = {
                row[0]
                for row in db.execute("SELECT action FROM admin_audit WHERE actor_id=?", (staff,))
            }
            self.assertIsNone(
                db.execute("SELECT 1 FROM local_accounts WHERE user_id=?", (staff,)).fetchone()
            )
        self.assertLessEqual(
            {"app_create_app", "app_adopted", "app_owner_changed", "app_storage_delete"}, actions
        )

    def test_staff_denied_account_management_quotas_audit_and_reauthentication(self) -> None:
        self.staff()
        for method, path, body in (
            ("GET", f"/v1/people/{self.owner}/account", None),
            (
                "POST",
                "/v1/people",
                {"username": "new-user", "displayName": "New", "role": "owner"},
            ),
            ("PATCH", f"/v1/people/{self.owner}/account", {"action": "role", "value": "staff"}),
            ("PUT", f"/v1/people/{self.owner}/quotas", {"apps": 5, "concurrentOperations": 2}),
            ("GET", "/v1/audit", None),
            ("POST", "/v1/reauthenticate", {"password": "unused", "totp": "123456"}),
        ):
            with self.subTest(method=method, path=path):
                self.assert_error(
                    "ACCESS_DENIED",
                    lambda method=method, path=path, body=body: self.call(
                        method, path, body, "taylor"
                    ),
                )

    def test_staff_owner_picker_search_is_scoped_and_bounded(self) -> None:
        self.staff()
        response = self.call("GET", "/v1/people/eligible-owners?q=ali&limit=6", owner="taylor")
        items = response.body["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["userId"], self.owner)
        self.assertEqual(
            set(items[0]), {"userId", "username", "displayName", "role", "enabled", "status"}
        )
        self.assert_error(
            "INVALID_FIELD",
            lambda: self.call("GET", "/v1/people/eligible-owners?q=" + "x" * 65, owner="taylor"),
        )
        self.assertEqual(
            self.call("GET", "/v1/people/eligible-owners?q=%25", owner="taylor").body["data"][
                "items"
            ],
            [],
        )
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.call("GET", "/v1/people/eligible-owners?role=owner", owner="taylor"),
        )

    def test_staff_can_supply_reviewed_sizing_plan(self) -> None:
        self.staff()
        self.test_admin_config_and_project_deploy_extras_owners_cannot_forge("taylor")

    def test_journal_runs_all_staff_app_kinds_but_blocks_owners(
        self,
    ) -> None:
        staff = self.staff()

        def intent(user: str, kind: str, state: str = "prepared") -> str:
            identifier = str(uuid.uuid4())
            with self.broker.database.connect(write=True) as db:
                db.execute(
                    "INSERT INTO intents(id,user_id,app_id,kind,client_key,controller_key,fingerprint,method,path,body,state,created,updated) VALUES(?,?,?,?,?,?,'f','POST',?,'{\"_portalAdmin\":true}',?,?,?)",
                    (
                        identifier,
                        user,
                        self.app_id,
                        kind,
                        str(uuid.uuid4()),
                        str(uuid.uuid4()),
                        f"/v1/applications/{self.app_id}/restart",
                        state,
                        self.now,
                        self.now,
                    ),
                )
            return identifier

        def state(identifier: str) -> tuple[str, str | None]:
            with self.broker.database.connect() as db:
                row = db.execute(
                    "SELECT state,safe_error FROM intents WHERE id=?", (identifier,)
                ).fetchone()
            return row["state"], row["safe_error"]

        review = ("blocked", "Admin review required.")
        for user, kind, blocked in (
            (self.owner, "app_restart", True),
            (staff, "storage_delete", False),
            (staff, "create_app", False),
            (staff, "app_restart", False),
            (staff, "adopt_app", False),
        ):
            with self.subTest(user=user, kind=kind):
                identifier = intent(user, kind)
                self.broker.journal.dispatch(identifier)
                if blocked:
                    self.assertEqual(state(identifier), review)
                else:
                    self.assertNotEqual(state(identifier)[0], "blocked")
        # Staff resume every app kind without step-up.
        for kind in ("app_restart", "storage_delete", "create_app", "adopt_app"):
            identifier = intent(staff, kind, "blocked")
            self.assertEqual(
                self.call("POST", f"/v1/intents/{identifier}/resume", {}, "taylor").status, 202
            )

    def test_adoption_unknown_already_owned_idempotency_import_round_trip(self) -> None:
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "POST", "/v1/all-apps/adopt", {"applicationId": str(uuid.uuid4())}, "admin"
            ),
        )
        self.assert_error(
            "ALREADY_OWNED",
            lambda: self.call(
                "POST", "/v1/all-apps/adopt", {"applicationId": self.app_id}, "admin"
            ),
        )
        imported = self.adopted(bindings=True)
        saved = self.call("GET", f"/v1/apps/{imported}/configuration", owner="admin").body["data"]
        self.assertEqual(saved["repository"], "https://github.com/example/class-app")
        self.assertEqual(saved["branch"], "main")
        self.assertEqual(saved["revision"], 7)
        self.assertEqual(len(saved["configuration"]["storageBindings"]), 1)
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE app_id=? AND kind='adopt_app'", (imported,)
            ).fetchone()
        self.assertEqual(
            self.call(
                "POST",
                "/v1/all-apps/adopt",
                {"applicationId": imported},
                "admin",
                row["client_key"],
            ).status,
            200,
        )
        self.assert_error(
            "ALREADY_OWNED",
            lambda: self.call(
                "POST",
                "/v1/all-apps/adopt",
                {"applicationId": imported},
                "admin",
            ),
        )
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.call(
                "POST",
                "/v1/all-apps/adopt",
                {"applicationId": self.app_id},
                "admin",
                row["client_key"],
            ),
        )

    def test_adoption_refuses_missing_or_legacy_snapshot(self) -> None:
        for active, kind in ((False, "strict"), (True, "imported")):
            identifier = str(uuid.uuid4())
            self.fixture.seed_operator_app(identifier, "https://operator.example.test")
            snapshot = self.fixture.apps[identifier]["activeDeploymentId"]
            if active:
                self.fixture.deployments[snapshot]["snapshotKind"] = kind
            else:
                self.fixture.apps[identifier]["activeDeploymentId"] = None
            self.assert_error(
                "SNAPSHOT_UNAVAILABLE",
                lambda identifier=identifier: self.call(
                    "POST", "/v1/all-apps/adopt", {"applicationId": identifier}, "admin"
                ),
            )

    def test_create_for_any_owner_and_reassign_without_bypassing_owner_routes(self) -> None:
        app = self.call(
            "POST", "/v1/all-apps", {"slug": "managed-other", "ownerId": self.owner}, "admin"
        ).body["data"]["app"]["applicationId"]
        self.assertEqual(self.call("GET", f"/v1/apps/{app}", owner="alice").status, 200)
        self.assertEqual(
            self.call("GET", f"/v1/apps/{app}", owner="admin").body["data"]["access"], "admin"
        )
        body = {"ownerId": self.admin_user, "expectedOwnerId": self.owner}
        self.call("PUT", f"/v1/apps/{app}/owner", body, "admin")
        self.assert_error("NOT_FOUND", lambda: self.call("GET", f"/v1/apps/{app}", owner="alice"))
        self.assertEqual(self.call("GET", f"/v1/apps/{app}", owner="admin").status, 200)
        self.assert_error(
            "OWNER_CONFLICT", lambda: self.call("PUT", f"/v1/apps/{app}/owner", body, "admin")
        )

    def test_list_names_owners_and_reports_cached_url_and_last_deploy(self) -> None:
        operator = self.adopted(identity=False)
        with self.broker.database.connect() as db:
            names = {
                row["id"]: (row["username"], row["display_name"])
                for row in db.execute("SELECT id,username,display_name FROM users")
            }

        def listing() -> dict[str, dict[str, Any]]:
            items = self.call("GET", "/v1/all-apps", owner="admin").body["data"]["items"]
            return {item["applicationId"]: item for item in items}

        before = listing()
        self.assertEqual(set(before), {self.app_id, operator})
        for item in before.values():
            self.assertEqual(
                (item["ownerUsername"], item["ownerDisplayName"]), names[item["ownerId"]]
            )
        # Additive: every earlier field is still present.
        self.assertEqual(
            set(before[operator])
            - {
                "ownerUsername",
                "ownerDisplayName",
                "url",
                "lastDeployedAt",
                "appState",
                "attention",
                "observedAt",
                "refreshing",
            },
            {"applicationId", "slug", "ownerId", "savedRevision", "lifecycleState"},
        )
        # The response uses cached values while a background read warms the cache.
        self.assertIsNone(before[operator]["lastDeployedAt"])
        detail = self.call("GET", f"/v1/apps/{operator}", owner="admin").body["data"]
        self.assertEqual(
            (detail["ownerUsername"], detail["ownerDisplayName"]), names[detail["ownerId"]]
        )
        after = listing()[operator]
        self.assertEqual(after["url"], detail["url"])
        self.assertTrue(after["url"].startswith("https://"))
        self.assertEqual(after["lastDeployedAt"], detail["acceptedDeployment"]["acceptedAt"])
        self.assertIsNone(after.get("observation"))
        # A malformed cached observation yields nulls rather than a failed list.
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE observations SET body=? WHERE app_id=?",
                ('{"url":"http://x.test","acceptedDeployment":[1]}', operator),
            )
        broken = listing()[operator]
        self.assertEqual((broken["url"], broken["lastDeployedAt"]), (None, None))
        # Paging still works across the joins.
        page = self.call("GET", "/v1/all-apps?limit=1", owner="admin").body["data"]
        rest = self.call(
            "GET", "/v1/all-apps?limit=1&cursor=" + page["nextCursor"], owner="admin"
        ).body["data"]
        self.assertEqual(
            {item["applicationId"] for item in page["items"] + rest["items"]},
            {self.app_id, operator},
        )
        # The web layer forwards the new fields unchanged.
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text('<div id="root"></div>')
        web = WebServer(("127.0.0.1", 0), self.config, assets)
        self.addCleanup(web.server_close)
        body = {"data": {"items": [after], "nextCursor": None, "truncated": False}}
        with patch.object(web.broker, "request", return_value=(200, copy.deepcopy(body))):
            reply = web.forward("GET", "/api/v1/all-apps", "", {}, b"")
        self.assertEqual(strict_json(reply.body), body)

    def test_target_owner_quota_not_admin_quota(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.execute("INSERT INTO quotas VALUES(?,1,1)", (self.owner,))
        self.assert_error(
            "QUOTA_EXCEEDED",
            lambda: self.call(
                "POST",
                "/v1/all-apps",
                {"slug": "over-owner-quota", "ownerId": self.owner},
                "admin",
            ),
        )

    def test_staff_and_admin_accounts_have_no_app_or_operation_limits(self) -> None:
        from openstack_platform.management.broker import resources

        staff = self.staff()
        # A stored quota from when the account was an owner no longer applies.
        with self.broker.database.connect(write=True) as db:
            db.execute("INSERT INTO quotas VALUES(?,1,1)", (staff,))
        limit = self.config.app_limit
        own = [self.create("admin", f"admin-own-{index}") for index in range(limit + 2)]
        staff_own = [self.create("taylor", f"staff-own-{index}") for index in range(limit + 2)]
        for account in ("admin", "taylor"):
            session = self.call("GET", "/v1/session", owner=account).body["data"]
            self.assertEqual(
                (
                    session["quota"]["apps"]["limit"],
                    session["quota"]["concurrentOperations"]["limit"],
                ),
                (None, None),
            )
        listing = self.call("GET", "/v1/people", owner="admin").body["data"]["items"]
        accounts = {
            item["ownerId"]: self.call(
                "GET", f"/v1/people/{item['ownerId']}/account", owner="admin"
            ).body["data"]
            for item in listing
        }
        for user in (self.admin_user, staff):
            self.assertEqual(
                (accounts[user]["appLimit"], accounts[user]["concurrencyLimit"]), (None, None)
            )
        self.assertEqual(accounts[self.owner]["appLimit"], limit)
        for user in (self.admin_user, staff):
            staff_view = self.call("GET", f"/v1/people/{user}", owner="admin")
            quota = staff_view.body["data"]["quota"]
            self.assertEqual(
                (quota["apps"]["limit"], quota["concurrentOperations"]["limit"]), (None, None)
            )
            self.assertEqual(quota["apps"]["used"], limit + 2)
            self.assert_error(
                "ADMIN_UNLIMITED",
                lambda user=user: self.call(
                    "PUT",
                    f"/v1/people/{user}/quotas",
                    {"apps": 1, "concurrentOperations": 1},
                    "admin",
                ),
            )
        # Held operations beyond the concurrency limit don't block staff or
        # an admin, but they still block an owner; one change per app still
        # applies.
        held = own[: self.config.concurrency_limit + 1]
        staff_held = staff_own[: self.config.concurrency_limit + 1]
        with self.broker.database.connect(write=True) as db:
            for user, app in (
                [(self.admin_user, app) for app in held]
                + [(staff, app) for app in staff_held]
                + [(self.owner, self.app_id)]
            ):
                db.execute(
                    "INSERT INTO intents(id,user_id,app_id,kind,client_key,controller_key,fingerprint,method,path,body,state,created,updated) VALUES(?,?,?,'deploy',?,?,'f','POST','/v1/x','{}','running',?,?)",
                    (
                        str(uuid.uuid4()),
                        user,
                        app,
                        str(uuid.uuid4()),
                        str(uuid.uuid4()),
                        self.now,
                        self.now,
                    ),
                )
            resources.operation_quota(self.broker, db, self.admin_user, own[-1])
            resources.operation_quota(self.broker, db, staff, staff_own[-1])
            self.assert_error(
                "APP_BUSY",
                lambda: resources.operation_quota(self.broker, db, self.admin_user, held[0]),
            )
            if self.config.concurrency_limit <= 1:
                self.assert_error(
                    "QUOTA_EXCEEDED",
                    lambda: resources.operation_quota(self.broker, db, self.owner, own[-1]),
                )

    def test_admin_failed_intent_exposes_only_bounded_controller_code(self) -> None:
        original = self.broker.client.request

        def rejected(method, path, *args, **kwargs):
            if method == "POST" and path.endswith("/deployments"):
                return 400, {
                    "error": {"code": "INVALID_REQUEST", "summary": "PRIVATE_CONTROLLER_TEXT"}
                }
            return original(method, path, *args, **kwargs)

        with patch.object(self.broker.client, "request", side_effect=rejected):
            result = self.call(
                "POST",
                self.prefix + "/deployments",
                {"configurationRevision": 1, "commit": "a" * 40},
                "admin",
            ).body["data"]
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["controllerErrorCode"], "INVALID_REQUEST")
        detail = self.call("GET", f"/v1/intents/{result['intentId']}", owner="admin").body["data"]
        self.assertEqual(detail["controllerErrorCode"], "INVALID_REQUEST")
        self.assertNotIn("PRIVATE_CONTROLLER_TEXT", canonical(detail))
        listing = self.call("GET", "/v1/intents", owner="admin").body["data"]["items"]
        self.assertEqual(
            next(item for item in listing if item["intentId"] == result["intentId"])[
                "controllerErrorCode"
            ],
            "INVALID_REQUEST",
        )

    def test_admin_config_and_project_deploy_extras_owners_cannot_forge(
        self, actor: str = "admin"
    ) -> None:
        saved = self.call("GET", self.prefix + "/configuration", owner=actor).body["data"]
        self.call(
            "PUT",
            self.prefix + "/configuration",
            {
                "expectedRevision": 1,
                "repository": saved["repository"],
                "branch": "other",
                "configuration": saved["configuration"],
            },
            actor,
        )
        body = {
            "configurationRevision": 2,
            "commit": "a" * 40,
            "maintenance": True,
            "plan": {
                "applicationId": self.app_id,
                "deploymentId": None,
                "activation": "enable-after-healthy-acceptance",
                "current": {
                    "enabled": False,
                    "flavor": "worker-small",
                    "cpuMHz": 500,
                    "memoryMiB": 512,
                },
                "flavor": {
                    "flavor_id": "large",
                    "name": "worker-large",
                    "vcpus": 4,
                    "ram_mib": 8192,
                    "disk_gib": 40,
                },
                "allocation": "measured-worker-capacity-minus-reserve",
                "reserve": {"cpuMHzMinimum": 200, "memoryMiBMinimum": 512, "percentMinimum": 10},
            },
        }
        body["plan"]["fingerprint"] = digest(canonical(body["plan"]))
        result = self.call("POST", self.prefix + "/deployments", body, actor)
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE id=?", (result.body["data"]["intentId"],)
            ).fetchone()
        self.assertIs(strict_json(row["body"].encode())["_portalAdmin"], True)
        self.assertIn('"maintenance":true', row["body"])
        self.assertIn('"plan":', row["body"])
        for field in ("maintenance", "plan", "role"):
            self.assert_error(
                "INVALID_REQUEST",
                lambda field=field: self.call(
                    "POST",
                    f"/v1/apps/{self.app_id}/deployments",
                    {
                        "configurationRevision": 2,
                        "commit": "a" * 40,
                        field: body.get(field, "admin"),
                    },
                    "alice",
                ),
            )

    def test_retained_ipv4_and_identity_provider_need_no_consent(self) -> None:
        imported = self.adopted()
        route = f"/v1/apps/{imported}"
        detail = self.call("GET", route, owner="admin").body["data"]
        self.assertTrue(detail["requiresMaintenance"])
        self.assertTrue(detail["identityProvider"])
        response = self.call(
            "POST",
            route + "/deployments",
            {"configurationRevision": 7, "commit": "a" * 40},
            "admin",
        )
        self.assertEqual(response.status, 202)
        self.complete(response)
        response = self.call("POST", route + "/state", {"desiredRunning": False}, "admin")
        self.assertEqual(response.status, 202)
        self.complete(response)
        self.assertEqual(self.call("POST", route + "/storage", {"type": "s3"}, "admin").status, 202)

    def test_admin_environment_keeps_hmac_and_no_values_in_db_response_or_audit(self) -> None:
        value = "ADMIN_SECRET_SENTINEL_89214"
        key = str(uuid.uuid4())
        result = self.call(
            "PUT", self.prefix + "/environment/TOKEN", {"value": value}, "admin", key
        )
        self.assertEqual(self.complete(result)["state"], "succeeded")
        response = self.call("GET", self.prefix + "/environment", owner="admin")
        self.assertEqual(response.body["data"]["items"][0]["name"], "TOKEN")
        with self.broker.database.connect() as db:
            contents = "\n".join(db.iterdump())
            row = db.execute(
                "SELECT body,fingerprint FROM intents WHERE client_key=?", (key,)
            ).fetchone()
        self.assertEqual(row["body"], canonical({"names": ["TOKEN"], "_portalAdmin": True}))
        self.assertNotIn(value, contents)
        self.assertNotIn(value, canonical(response.body))
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.call(
                "PUT", self.prefix + "/environment/TOKEN", {"value": "different"}, "admin", key
            ),
        )

    def test_app_busy_is_shared_across_owner_and_admin_actors(self) -> None:
        result = self.call(
            "POST",
            f"/v1/apps/{self.app_id}/deployments",
            {"configurationRevision": 1, "commit": "a" * 40},
            "alice",
        )
        self.assert_error(
            "APP_BUSY",
            lambda: self.call("PUT", self.prefix + "/environment/TOKEN", {"value": "x"}, "admin"),
        )
        self.assert_error(
            "APP_BUSY",
            lambda: self.call(
                "PUT",
                self.prefix + "/owner",
                {"ownerId": self.admin_user, "expectedOwnerId": self.owner},
                "admin",
            ),
        )
        self.assertEqual(result.status, 202)

    def test_admin_storage_delete_needs_typed_confirmation_and_audit_without_step_up(self) -> None:
        result = self.call("POST", self.prefix + "/storage", {"type": "s3"}, "admin")
        self.assertEqual(self.complete(result)["state"], "succeeded")
        storage = self.call("GET", self.prefix + "/storage", owner="admin").body["data"]["items"][0]
        path = self.prefix + "/storage/" + storage["resourceId"]
        key = str(uuid.uuid4())
        self.now += 301
        self.assert_error(
            "CONFIRMATION_REQUIRED",
            lambda: self.call(
                "DELETE", path, {"confirmation": "s3"}, "admin", headers={"idempotency-key": key}
            ),
        )
        result = self.call(
            "DELETE",
            path,
            {"confirmation": "student-app s3"},
            "admin",
            headers={"idempotency-key": key},
        )
        self.assertEqual(self.complete(result)["state"], "succeeded")
        replay = self.call(
            "DELETE",
            path,
            {"confirmation": "student-app s3"},
            "admin",
            headers={"idempotency-key": key},
        )
        self.assertEqual(result.body["data"]["intentId"], replay.body["data"]["intentId"])
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT details FROM admin_audit WHERE action='app_storage_delete'"
            ).fetchone()
        self.assertIn(self.app_id, row[0])
        self.assertEqual(
            self.call("GET", self.prefix + "/storage", owner="admin").body["data"]["items"], []
        )

    def test_staff_and_admin_resume_student_deploy_with_original_request_and_audit(self) -> None:
        staff = self.staff()
        self.login("bob")
        for role, actor in (("taylor", staff), ("admin", self.admin_user)):
            with self.subTest(role=role):
                self.fixture.recovery_next = True
                started = self.call(
                    "POST",
                    f"/v1/apps/{self.app_id}/deployments",
                    {"commit": "a" * 40, "configurationRevision": 1},
                    "alice",
                )
                blocked = self.complete(started, "alice")
                identifier = blocked["intentId"]
                self.assertEqual(blocked["state"], "blocked")
                self.assertEqual(blocked["safeError"], "Resume this deployment to finish it.")
                # The attention filter finds it even behind more recent activity.
                with self.broker.database.connect(write=True) as db:
                    original = dict(
                        db.execute("SELECT * FROM intents WHERE id=?", (identifier,)).fetchone()
                    )
                    db.execute("UPDATE intents SET created=0 WHERE id=?", (identifier,))
                activity = self.call("GET", self.prefix + "/activity?attention=1", owner=role).body[
                    "data"
                ]["items"]
                self.assertEqual([item["intentId"] for item in activity], [identifier])
                self.assertEqual(
                    activity[0]["actor"], {"displayName": "Alice Student", "you": False}
                )
                self.assertIsNone(activity[0]["retryKey"])
                calls = len(self.fixture.calls)
                self.assert_error(
                    "NOT_FOUND",
                    lambda identifier=identifier: self.call(
                        "POST", f"/v1/intents/{identifier}/resume", {}, "bob"
                    ),
                )
                self.assert_error(
                    "NOT_FOUND",
                    lambda identifier=identifier: self.call(
                        "GET", f"/v1/intents/{identifier}", owner="bob"
                    ),
                )
                self.assert_error(
                    "NOT_FOUND",
                    lambda: self.call(
                        "GET", f"/v1/apps/{self.app_id}/activity?attention=1", owner="bob"
                    ),
                )
                self.assertEqual(len(self.fixture.calls), calls)
                for viewer, prefix in (("alice", f"/v1/apps/{self.app_id}"), (role, self.prefix)):
                    with self.assertRaises(HttpError) as refused:
                        self.call(
                            "PUT", prefix + "/environment/TOKEN", {"value": "private"}, viewer
                        )
                    self.assertEqual(refused.exception.code, "APP_BUSY")
                    self.assertEqual(
                        refused.exception.summary,
                        "Finish the previous deployment in the app's Overview before trying again.",
                    )
                with patch.object(
                    self.broker.client, "request", wraps=self.broker.client.request
                ) as replay:
                    resumed = self.call("POST", f"/v1/intents/{identifier}/resume", {}, role)
                captured = [call.args for call in replay.call_args_list if call.args[0] == "POST"]
                self.assertEqual(resumed.status, 202)
                self.assertEqual(
                    captured,
                    [
                        (
                            original["method"],
                            original["path"],
                            strict_json(original["body"].encode()),
                            original["controller_key"],
                        )
                    ],
                )
                self.assertEqual(self.complete(resumed, role)["state"], "succeeded")
                with self.broker.database.connect() as db:
                    after = db.execute("SELECT * FROM intents WHERE id=?", (identifier,)).fetchone()
                    for field in (
                        "user_id",
                        "body",
                        "controller_key",
                        "client_key",
                        "fingerprint",
                        "operation_id",
                        "path",
                        "method",
                    ):
                        self.assertEqual(after[field], original[field], field)
                    self.assertEqual(
                        tuple(
                            db.execute(
                                "SELECT user_id,app_id,intent_id FROM audit WHERE action='resume' AND intent_id=?",
                                (identifier,),
                            ).fetchone()
                        ),
                        (actor, self.app_id, identifier),
                    )
                    audit_row = db.execute(
                        "SELECT actor_id,target_id,details FROM admin_audit WHERE action='app_resume' AND json_extract(details,'$.intentId')=?",
                        (identifier,),
                    ).fetchone()
                    self.assertEqual(tuple(audit_row[:2]), (actor, self.owner))
                    self.assertEqual(
                        strict_json(audit_row["details"].encode()),
                        {"applicationId": self.app_id, "intentId": identifier},
                    )
                    self.assertTrue(
                        db.execute(
                            "SELECT 1 FROM staff_read_audit WHERE route='/v1/intents/{intent}' AND actor_id=?",
                            (actor,),
                        ).fetchone()
                    )

    def test_inline_attention_copy_is_current_for_all_viewers_and_retains_replay_rules(
        self,
    ) -> None:
        from openstack_platform.management.broker.journal import intent_model

        self.staff()
        self.fixture.recovery_next = True
        started = self.call(
            "POST",
            self.prefix + "/deployments",
            {"commit": "a" * 40, "configurationRevision": 1},
            "taylor",
        )
        identifier = self.complete(started, "taylor")["intentId"]
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE intents SET safe_error=? WHERE id=?",
                ("Old stored copy points to another page.", identifier),
            )
            row = dict(db.execute("SELECT * FROM intents WHERE id=?", (identifier,)).fetchone())
        self.assertEqual(
            intent_model(row, diagnostic=True)["safeError"], "Resume this deployment to finish it."
        )
        self.assertEqual(intent_model(row)["safeError"], "Ask staff to resume this deployment.")
        row["state"] = "unknown"
        self.assertEqual(
            intent_model(row, diagnostic=True)["safeError"],
            "Resume this deployment to check its outcome.",
        )
        row["kind"] = "env_set"
        message = intent_model(row, diagnostic=True)
        self.assertFalse(message["canResume"])
        self.assertEqual(
            message["safeError"],
            "The person who started this environment edit must enter the value again in Settings.",
        )
        feed = self.call("GET", "/v1/activity?attention=1", owner="taylor").body["data"]["items"]
        self.assertEqual(feed[0]["guidance"], "Resume this deployment to finish it.")

    def test_staff_resume_finishes_after_the_original_account_is_disabled(self) -> None:
        self.staff()
        self.fixture.recovery_next = True
        started = self.call(
            "POST",
            f"/v1/apps/{self.app_id}/deployments",
            {"commit": "a" * 40, "configurationRevision": 1},
            "alice",
        )
        identifier = self.complete(started, "alice")["intentId"]
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET enabled=0 WHERE id=?", (self.owner,))
            security_change(db, self.owner)
        resumed = self.call("POST", f"/v1/intents/{identifier}/resume", {}, "taylor")
        self.assertEqual(resumed.body["data"]["state"], "accepted")
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE intents SET next_retry=0,lease=0 WHERE id=?", (identifier,))
        self.broker.journal.reconcile()
        self.assertEqual(
            self.call("GET", f"/v1/intents/{identifier}", owner="taylor").body["data"]["state"],
            "succeeded",
        )
        # Disabled accounts cannot initiate or replay a prepared request themselves.
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE intents SET state='prepared',next_retry=0,lease=0 WHERE id=?", (identifier,)
            )
        calls = len(self.fixture.calls)
        self.broker.journal.reconcile()
        self.assertEqual(len(self.fixture.calls), calls)

    def test_staff_resume_admin_intent_after_original_actors_role_is_revoked(self) -> None:
        staff = self.staff()
        self.fixture.recovery_next = True
        result = self.call(
            "POST",
            self.prefix + "/deployments",
            {"commit": "b" * 40, "configurationRevision": 1},
            "taylor",
        )
        identifier = self.complete(result, "taylor")["intentId"]
        owner_activity = self.call(
            "GET", f"/v1/apps/{self.app_id}/activity?attention=1", owner="alice"
        ).body["data"]["items"]
        self.assertFalse(owner_activity[0]["canResume"])
        self.assertTrue(
            self.call("GET", self.prefix + "/activity?attention=1", owner="admin").body["data"][
                "items"
            ][0]["canResume"]
        )
        self.assert_error(
            "ACCESS_DENIED",
            lambda: self.call("POST", f"/v1/intents/{identifier}/resume", {}, "alice"),
        )
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET role='owner' WHERE id=?", (staff,))
            security_change(db, staff)
        resumed = self.call("POST", f"/v1/intents/{identifier}/resume", {}, "admin")
        self.assertEqual(self.complete(resumed)["state"], "succeeded")

    def test_admins_resume_their_app_administration_changes(self) -> None:
        result = self.call("POST", self.prefix + "/storage", {"type": "s3"}, "admin")
        self.assertEqual(self.complete(result)["state"], "succeeded")
        storage = self.call("GET", self.prefix + "/storage", owner="admin").body["data"]["items"][0]
        restart = self.call("POST", self.prefix + "/restart", {}, "admin").body["data"]["intentId"]
        deletion = self.call(
            "DELETE",
            self.prefix + "/storage/" + storage["resourceId"],
            {"confirmation": "student-app s3"},
            "admin",
            headers={"idempotency-key": str(uuid.uuid4())},
        ).body["data"]["intentId"]
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE intents SET state='blocked' WHERE id IN (?,?)", (restart, deletion))
        self.assertEqual(
            self.call("POST", f"/v1/intents/{restart}/resume", {}, "admin").status, 202
        )
        self.now += 301
        self.assertEqual(
            self.call("POST", f"/v1/intents/{deletion}/resume", {}, "admin").status, 202
        )

    def test_admin_deploy_key_changes_are_in_the_admin_audit(self) -> None:
        self.call("POST", self.prefix + "/source-key", {}, "admin")
        removed = self.call("DELETE", self.prefix + "/source-key", None, "admin")
        self.assertEqual(removed.body["data"], {"present": False})
        with self.broker.database.connect() as db:
            rows = db.execute(
                "SELECT actor_id,target_id,details FROM admin_audit"
                " WHERE action='app_source_key' ORDER BY rowid"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (
                    self.admin_user,
                    self.owner,
                    canonical({"applicationId": self.app_id, "replace": False}),
                ),
                (
                    self.admin_user,
                    self.owner,
                    canonical({"applicationId": self.app_id, "removed": True}),
                ),
            ],
        )

    def test_revocation_during_controller_read_suppresses_results_and_mutation(self) -> None:
        original = self.broker.client.request

        def request(*args, **kwargs):
            result = original(*args, **kwargs)
            with self.broker.database.connect(write=True) as db:
                security_change(db, self.admin_user)
            return result

        with patch.object(self.broker.client, "request", request):
            self.assert_error(
                "SESSION_EXPIRED", lambda: self.call("GET", self.prefix, owner="admin")
            )

    def test_sizing_plan_rejects_extra_environment_fields_before_journaling(self) -> None:
        body = {
            "configurationRevision": 1,
            "commit": "a" * 40,
            "plan": {"environment": {"TOKEN": "PLAN_SECRET_SENTINEL"}},
        }
        self.assert_error(
            "INVALID_PLAN", lambda: self.call("POST", self.prefix + "/deployments", body, "admin")
        )
        with self.broker.database.connect() as db:
            self.assertNotIn("PLAN_SECRET_SENTINEL", "\n".join(db.iterdump()))

    def test_admin_reads_any_apps_logs(self) -> None:
        app = self.adopted(identity=False)
        result = self.call("GET", f"/v1/apps/{app}/logs?stream=stderr", owner="admin")
        self.assertEqual(result.body["data"]["stream"], "stderr")
        self.assertTrue(result.body["data"]["running"])
        self.assertEqual(result.body["data"]["text"], "Warning: SESSION_SECRET is short\n")
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.call("GET", f"/v1/apps/{app}/logs?lines=5", owner="admin"),
        )

    def test_admin_app_pages_have_room_for_their_reads_but_stay_bounded(self) -> None:
        # One app administration page reads about eight things, for staff as
        # for admins; the staff metadata views keep the smaller budget.
        self.staff()
        for account, address in (("admin", "127.0.0.1"), ("taylor", "127.0.0.2")):
            headers = {"x-portal-client-address": address}
            for _ in range(40):
                self.assertEqual(
                    self.call("GET", self.prefix, owner=account, headers=headers).status, 200
                )
            self.assert_error(
                "RATE_LIMITED",
                lambda account=account, headers=headers: self.call(
                    "GET", self.prefix, owner=account, headers=headers
                ),
            )
        staff = ReadLimits()
        for _ in range(10):
            with staff.reserve("staff", "address", self.now):
                pass
        with self.assertRaises(HttpError) as limited:
            with staff.reserve("staff", "address", self.now):
                pass
        self.assertEqual(limited.exception.code, "RATE_LIMITED")

    def test_admin_cannot_read_cross_owner_logs_or_privileged_controller_routes(self) -> None:
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "GET",
                self.prefix + "/deployments/" + str(uuid.uuid4()) + "/build-log",
                owner="admin",
            ),
        )
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", "/v1/admin/applications", owner="admin")
        )

    def test_owner_save_loses_authority_after_reassignment_before_transaction(self) -> None:
        original = self.broker.own

        def transferred(request, **kwargs):
            result = original(request, **kwargs)
            with self.broker.database.connect(write=True) as db:
                db.execute("UPDATE apps SET user_id=? WHERE id=?", (self.admin_user, self.app_id))
            return result

        with patch.object(self.broker, "own", transferred):
            self.assert_error("NOT_FOUND", lambda: self.save(self.app_id, revision=1))
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT revision FROM apps WHERE id=?", (self.app_id,)).fetchone()[0], 1
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM configurations WHERE app_id=?", (self.app_id,)
                ).fetchone()[0],
                1,
            )

    def test_storage_delete_refuses_saved_bindings_without_identity_consent(self) -> None:
        app = self.adopted(bindings=True)
        resource = next(
            item for item in self.fixture.resources.values() if item["applicationId"] == app
        )
        path = f"/v1/apps/{app}/storage/{resource['resourceId']}"
        self.assert_error(
            "STORAGE_BOUND",
            lambda: self.call(
                "DELETE",
                path,
                {"confirmation": "operator-class-fixture postgres"},
                "admin",
                headers={"idempotency-key": str(uuid.uuid4())},
            ),
        )

    def test_admin_adoption_and_reassignment_need_no_step_up(self) -> None:
        self.now += 301
        for owner in (None, self.admin_user, self.owner):
            identifier = str(uuid.uuid4())
            self.fixture.seed_operator_app(identifier, self.config.commons_origin)
            self.fixture.apps[identifier]["slug"] += "-" + identifier[:8]
            body = {"applicationId": identifier, **({"ownerId": owner} if owner else {})}
            with self.subTest(owner=owner):
                self.assertEqual(self.call("POST", "/v1/all-apps/adopt", body, "admin").status, 201)
        self.assertEqual(
            self.call(
                "PUT",
                self.prefix + "/owner",
                {"expectedOwnerId": self.owner, "ownerId": self.admin_user},
                "admin",
            ).status,
            200,
        )

    def test_retired_identity_confirmation_is_an_unexpected_field(self) -> None:
        self.staff()
        for account in ("alice", "taylor", "admin"):
            routes = [
                (
                    "POST",
                    self.prefix + "/deployments",
                    {"configurationRevision": 1, "commit": "a" * 40},
                ),
                ("POST", self.prefix + "/state", {"desiredRunning": False}),
                ("POST", self.prefix + "/restart", {}),
                ("POST", self.prefix + "/storage", {"type": "postgres"}),
                ("POST", self.prefix + f"/storage/{uuid.uuid4()}/verify", {}),
                ("POST", self.prefix + f"/storage/{uuid.uuid4()}/rotate", {}),
            ]
            if account != "alice":
                routes.extend(
                    [
                        ("POST", "/v1/all-apps/adopt", {"applicationId": str(uuid.uuid4())}),
                        (
                            "PUT",
                            self.prefix + "/owner",
                            {"ownerId": self.admin_user, "expectedOwnerId": self.owner},
                        ),
                        (
                            "DELETE",
                            self.prefix + f"/storage/{uuid.uuid4()}",
                            {"confirmation": "student-app postgres"},
                        ),
                    ]
                )
            with self.broker.database.connect() as db:
                intents = db.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
            for method, path, fields in routes:
                with self.subTest(account=account, method=method, path=path):
                    self.assert_error(
                        "INVALID_REQUEST",
                        lambda method=method, path=path, fields=fields, account=account: self.call(
                            method, path, {**fields, "identityProviderConfirmed": True}, account
                        ),
                    )
            with self.broker.database.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM intents").fetchone()[0], intents)

    def test_sign_in_app_actions_need_no_confirmation_for_owner_and_staff(self) -> None:
        staff = self.staff()
        for account, owner in (("alice", self.owner), ("taylor", staff)):
            identifier = str(uuid.uuid4())
            self.fixture.seed_operator_app(identifier, self.config.commons_origin)
            self.fixture.apps[identifier]["slug"] += "-" + account
            self.fixture.apps[identifier]["requiresMaintenance"] = False
            self.call(
                "POST",
                "/v1/all-apps/adopt",
                {"applicationId": identifier, "ownerId": owner},
                "admin",
            )
            prefix = f"/v1/apps/{identifier}"
            self.assertTrue(
                self.call("GET", prefix, owner=account).body["data"]["identityProvider"]
            )
            for suffix, fields in (
                ("deployments", {"configurationRevision": 7, "commit": "a" * 40}),
                ("state", {"desiredRunning": False}),
                ("restart", {}),
            ):
                with self.subTest(account=account, suffix=suffix):
                    response = self.call("POST", prefix + "/" + suffix, fields, account)
                    self.assertEqual(response.status, 202)
                    self.complete(response, account)
            response = self.call("POST", prefix + "/storage", {"type": "postgres"}, account)
            self.assertEqual(self.complete(response, account)["state"], "succeeded")
            resource = self.call("GET", prefix + "/storage", owner=account).body["data"]["items"][
                0
            ]["resourceId"]
            for action in ("verify", "rotate"):
                response = self.call("POST", prefix + f"/storage/{resource}/{action}", {}, account)
                self.assertEqual(self.complete(response, account)["state"], "succeeded")

    def test_admin_web_closed_routes_origin_and_csrf(self) -> None:
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text('<div id="root"></div>')
        web = WebServer(("127.0.0.1", 0), self.config, assets)
        self.addCleanup(web.server_close)
        self.assertEqual(
            web.handle(
                "GET", "/all-apps", {"host": urlsplit(self.config.portal_origin).netloc}, b""
            ).status,
            200,
        )
        self.assertEqual(
            web.handle(
                "GET",
                "/api/v1/admin/nope",
                {"host": urlsplit(self.config.portal_origin).netloc},
                b"",
            ).status,
            404,
        )
        with patch.object(web.broker, "request") as upstream:
            path = f"/api/v1/admin-apps/{self.app_id}/deployments/{uuid.uuid4()}/build-log"
            self.assertEqual(web.forward("GET", path, "", {}, b"").status, 404)
            upstream.assert_not_called()
        self.assert_error(
            "CSRF_REJECTED",
            lambda: self.call(
                "GET", "/v1/all-apps", owner="admin", headers={"x-csrf-token": "wrong"}
            ),
        )
        self.assert_error(
            "ORIGIN_REJECTED",
            lambda: self.call(
                "POST", "/v1/all-apps/adopt", {}, "admin", headers={"origin": "null"}
            ),
        )


class FlavorCacheTests(unittest.TestCase):
    def test_cache_expires_after_ten_minutes_and_matches_names_or_ids(self) -> None:
        client = Mock()
        item = {
            "name": "xl.4core",
            "flavor_id": "200",
            "vcpus": 4,
            "ram_mib": 16384,
            "disk_gib": 64,
        }
        cache = FlavorCache(client)
        with (
            patch(
                "openstack_platform.management.broker.sizing.flavors", return_value=[item]
            ) as read,
            patch(
                "openstack_platform.management.broker.sizing.time.monotonic", return_value=100
            ) as clock,
        ):
            for reference in ("xl.4core", "200"):
                enriched = cache.enrich(
                    {"workerFlavor": reference, "cpuMHz": 7916, "memoryMiB": 14395}
                )
                self.assertIsNotNone(enriched)
                self.assertEqual(enriched["vcpus"], 4)
                self.assertEqual(enriched["ram_mib"], 16384)
            clock.return_value = 699
            cache.get()
            self.assertEqual(read.call_count, 1)
            clock.return_value = 700
            cache.get()
            self.assertEqual(read.call_count, 2)
            self.assertEqual(cache.enrich({"workerFlavor": "missing"}), {"workerFlavor": "missing"})

    def test_concurrent_reads_share_one_flavor_request(self) -> None:
        cache = FlavorCache(Mock())
        started, release = threading.Event(), threading.Event()

        def slow(_client: Any) -> list[dict[str, Any]]:
            started.set()
            if not release.wait(5):
                raise AssertionError("flavor read was not released")
            return []

        with (
            patch("openstack_platform.management.broker.sizing.flavors", side_effect=slow) as read,
            ThreadPoolExecutor(max_workers=4) as callers,
        ):
            try:
                futures = [callers.submit(cache.get) for _ in range(4)]
                self.assertTrue(started.wait(1))
            finally:
                release.set()
            self.assertEqual([future.result(timeout=1) for future in futures], [[], [], [], []])
            read.assert_called_once()

    def test_failure_omits_capacity_and_shares_a_short_retry_cooldown(self) -> None:
        cache = FlavorCache(Mock())
        size = {"workerFlavor": "xl.4core", "cpuMHz": 7916, "memoryMiB": 14395}
        with (
            patch(
                "openstack_platform.management.broker.sizing.flavors",
                side_effect=ControllerUnavailable("offline"),
            ) as read,
            patch(
                "openstack_platform.management.broker.sizing.time.monotonic", return_value=100
            ) as clock,
        ):
            self.assertEqual(cache.enrich(size), size)
            self.assertEqual(cache.enrich(size), size)
            read.assert_called_once()
            clock.return_value = 130
            cache.enrich(size)
            self.assertEqual(read.call_count, 2)
        self.assertNotIn("vcpus", size)
