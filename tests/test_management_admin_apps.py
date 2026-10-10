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
                "quotas": {
                    "connections": 10,
                    "sizeBytes": 2147483648,
                    "memoryBytes": 536870912,
                    "cpuMillicores": 500,
                },
                "isolation": "instance",
                "hardQuotaBytes": 2684354560,
                "usage": {
                    "instanceMemoryBytes": None,
                    "cpuTimeMilliseconds": None,
                    "usedBytes": None,
                    "objectCount": None,
                    "currentConnections": None,
                    "measuredAt": None,
                    "stale": True,
                },
                "writeBlock": {"blocked": False, "reason": None, "since": None},
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

    def test_storage_limits_admin_authority_projection_and_durable_intent(self) -> None:
        self.staff()
        created = self.call("POST", self.prefix + "/storage", {"type": "mongo"}, "admin")
        self.complete(created)
        resource = self.call("GET", self.prefix + "/storage", owner="admin").body["data"]["items"][
            0
        ]
        identifier = resource["resourceId"]
        provider = self.fixture.resources[identifier]
        provider.update(
            providerId="private-provider",
            providerName="private-name",
            usageError="private-error",
            instanceId=str(uuid.uuid4()),
            instancePort=54321,
            migrationState="copying",
        )
        provider["usage"]["providerSecret"] = "private-secret"
        owner = self.call("GET", self.prefix + "/storage", owner="alice").body["data"]["items"][0]
        self.assertEqual(owner["quotas"], provider["quotas"])
        self.assertEqual(owner["writeBlock"], provider["writeBlock"])
        self.assertFalse(
            {
                "providerId",
                "providerName",
                "usageError",
                "instanceId",
                "instancePort",
                "migrationState",
            }
            & owner.keys()
        )
        self.assertNotIn("providerSecret", owner["usage"])
        body = {
            "quotas": {**owner["quotas"], "sizeBytes": 4294967296},
            "expectedQuotas": owner["quotas"],
        }
        path = self.prefix + f"/storage/{identifier}/limits"
        calls = len(self.fixture.calls)
        for headers, code in (
            ({"x-csrf-token": ""}, "CSRF_REJECTED"),
            ({"origin": "https://other.example.test"}, "ORIGIN_REJECTED"),
        ):
            self.assert_error(
                code, lambda headers=headers: self.call("PUT", path, body, "admin", headers=headers)
            )
        for actor in ("alice", "taylor"):
            self.assert_error(
                "ACCESS_DENIED", lambda actor=actor: self.call("PUT", path, body, actor)
            )
        self.assertEqual(len(self.fixture.calls), calls)
        for delta in (
            {"connections": 101},
            {"memoryBytes": 1073741825},
            {"memoryBytes": 8589934593},
            {"cpuMillicores": 99},
            {"cpuMillicores": 4001},
        ):
            self.assert_error(
                "INVALID_REQUEST",
                lambda delta=delta: self.call(
                    "PUT",
                    path,
                    {"quotas": {**body["quotas"], **delta}, "expectedQuotas": owner["quotas"]},
                    "admin",
                ),
            )
        key = str(uuid.uuid4())
        response = self.call("PUT", path, body, "admin", key)
        intent = response.body["data"]["intentId"]
        self.assertEqual(
            self.call("PUT", path, body, "admin", key).body["data"]["intentId"], intent
        )
        self.assertEqual(self.complete(response)["state"], "succeeded")
        with self.broker.database.connect() as db:
            row = db.execute("SELECT * FROM intents WHERE id=?", (intent,)).fetchone()
            self.assertEqual(row["kind"], "storage_limits")
            self.assertEqual(strict_json(row["body"].encode())["resourceId"], identifier)
            self.assertEqual(row["operation_id"], row["controller_key"])
            actions = [r[0] for r in db.execute("SELECT action FROM admin_audit")]
        self.assertEqual(actions.count("storage_limits_requested"), 1)
        self.assertIn("storage_limits_succeeded", actions)
        self.assertEqual(self.fixture.resources[identifier]["quotas"], body["quotas"])
        activities = self.call("GET", "/v1/activity", owner="taylor").body["data"]["items"]
        self.assertEqual(
            next(item["kind"] for item in activities if item["intentId"] == intent),
            "storage_limits",
        )
        for actor in ("alice", "taylor"):
            self.assert_error(
                "ACCESS_DENIED",
                lambda actor=actor: self.call("POST", f"/v1/intents/{intent}/resume", {}, actor),
            )

    def test_storage_limits_reject_invalid_values_and_surface_stale_expectation(self) -> None:
        self.complete(self.call("POST", self.prefix + "/storage", {"type": "s3"}, "admin"))
        resource = self.call("GET", self.prefix + "/storage", owner="admin").body["data"]["items"][
            0
        ]
        path = self.prefix + f"/storage/{resource['resourceId']}/limits"
        current = resource["quotas"]
        before = len([call for call in self.fixture.calls if call[0] == "PUT"])
        for invalid in (
            {"s3Bytes": True, "s3Objects": 1},
            {"s3Bytes": 1048575, "s3Objects": 1},
            {"s3Bytes": 549755813889, "s3Objects": 1},
            {"s3Bytes": 1048576.0, "s3Objects": 1},
            {"s3Bytes": 1048576, "s3Objects": 100000001},
            {"s3Bytes": 1048576},
            {**current, "connections": 10},
        ):
            self.assert_error(
                "INVALID_REQUEST",
                lambda invalid=invalid: self.call(
                    "PUT", path, {"quotas": invalid, "expectedQuotas": current}, "admin"
                ),
            )
        self.assertEqual(len([call for call in self.fixture.calls if call[0] == "PUT"]), before)
        response = self.call(
            "PUT",
            path,
            {
                "quotas": {"s3Bytes": 1048576, "s3Objects": 1},
                "expectedQuotas": {"s3Bytes": 1048576, "s3Objects": 1},
            },
            "admin",
        )
        self.assertEqual(response.body["data"]["state"], "accepted")
        completed = self.complete(response)
        self.assertEqual(completed["state"], "failed")
        self.assertTrue(completed["safeError"])
        self.assertEqual(self.fixture.resources[resource["resourceId"]]["quotas"], current)

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

    def test_app_administration_role_matrix(self) -> None:
        self.staff()
        for actor in ("alice", "taylor", "admin"):
            calls = len(self.fixture.calls)
            for method, _route, path in self.routes():
                with self.subTest(actor=actor, path=path, method=method):
                    body = {} if method != "GET" else None
                    if actor == "alice":
                        self.assert_error(
                            "ACCESS_DENIED",
                            lambda method=method, path=path, body=body, actor=actor: self.call(
                                method, path, body, actor
                            ),
                        )
                    else:
                        try:
                            self.call(method, path, body, actor)
                        except HttpError as error:
                            # Empty bodies may fail validation after authorization.
                            self.assertNotIn(
                                error.code, {"ACCESS_DENIED", "SESSION_EXPIRED", "STEP_UP_REQUIRED"}
                            )
            if actor == "alice":
                self.assertEqual(len(self.fixture.calls), calls)

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
