"""Local-admin app actions, project capability delta and secret-free reuse."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from openstack_platform.management.broker import bootstrap
from openstack_platform.management.broker.accounts import security_change
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
        self.prefix = f"/v1/admin-apps/{self.app_id}"

    def complete(self, response: Any) -> Any:
        result = response.body["data"]
        self.broker.journal.dispatch(result["intentId"])
        return self.call("GET", f"/v1/intents/{result['intentId']}", owner="admin").body["data"]

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
        self.call("POST", "/v1/admin-apps/adopt", {"applicationId": identifier}, "admin")
        return identifier

    def test_owner_and_staff_denied_every_admin_action_before_lookup(self) -> None:
        staff_id = self.login("taylor")
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET role='staff' WHERE id=?", (staff_id,))
            security_change(db, staff_id)
        self.login("taylor")
        for owner in ("alice", "taylor"):
            for method, route, _handler in self.broker.admin_apps.routes():
                path = (
                    route.replace("{app}", self.app_id)
                    .replace("{resource}", str(uuid.uuid4()))
                    .replace("{deployment}", str(uuid.uuid4()))
                    .replace("{key}", "TOKEN")
                )
                with self.subTest(owner=owner, path=path, method=method):
                    self.assert_error(
                        "ACCESS_DENIED",
                        lambda method=method, path=path, owner=owner: self.call(
                            method, path, {} if method != "GET" else None, owner
                        ),
                    )

    def test_adoption_unknown_already_owned_idempotency_import_round_trip(self) -> None:
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "POST", "/v1/admin-apps/adopt", {"applicationId": str(uuid.uuid4())}, "admin"
            ),
        )
        self.assert_error(
            "ALREADY_OWNED",
            lambda: self.call(
                "POST", "/v1/admin-apps/adopt", {"applicationId": self.app_id}, "admin"
            ),
        )
        imported = self.adopted(bindings=True)
        saved = self.call("GET", f"/v1/admin-apps/{imported}/configuration", owner="admin").body[
            "data"
        ]
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
                "/v1/admin-apps/adopt",
                {"applicationId": imported},
                "admin",
                row["client_key"],
            ).status,
            200,
        )
        self.assert_error(
            "ALREADY_OWNED",
            lambda: self.call("POST", "/v1/admin-apps/adopt", {"applicationId": imported}, "admin"),
        )
        self.assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.call(
                "POST",
                "/v1/admin-apps/adopt",
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
                    "POST", "/v1/admin-apps/adopt", {"applicationId": identifier}, "admin"
                ),
            )

    def test_create_for_any_owner_and_reassign_without_bypassing_owner_routes(self) -> None:
        app = self.call(
            "POST", "/v1/admin-apps", {"slug": "managed-other", "ownerId": self.owner}, "admin"
        ).body["data"]["app"]["applicationId"]
        self.assertEqual(self.call("GET", f"/v1/apps/{app}", owner="alice").status, 200)
        self.assert_error("NOT_FOUND", lambda: self.call("GET", f"/v1/apps/{app}", owner="admin"))
        body = {"ownerId": self.admin_user, "expectedOwnerId": self.owner}
        self.call("PUT", f"/v1/admin-apps/{app}/owner", body, "admin")
        self.assert_error("NOT_FOUND", lambda: self.call("GET", f"/v1/apps/{app}", owner="alice"))
        self.assertEqual(self.call("GET", f"/v1/apps/{app}", owner="admin").status, 200)
        self.assert_error(
            "OWNER_CONFLICT", lambda: self.call("PUT", f"/v1/admin-apps/{app}/owner", body, "admin")
        )

    def test_target_owner_quota_not_admin_quota(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.execute("INSERT INTO quotas VALUES(?,1,1)", (self.owner,))
        self.assert_error(
            "QUOTA_EXCEEDED",
            lambda: self.call(
                "POST",
                "/v1/admin-apps",
                {"slug": "over-owner-quota", "ownerId": self.owner},
                "admin",
            ),
        )

    def test_admin_config_and_project_deploy_extras_owners_cannot_forge(self) -> None:
        saved = self.call("GET", self.prefix + "/configuration", owner="admin").body["data"]
        self.call(
            "PUT",
            self.prefix + "/configuration",
            {
                "expectedRevision": 1,
                "repository": saved["repository"],
                "branch": "other",
                "configuration": saved["configuration"],
            },
            "admin",
        )
        body = {
            "configurationRevision": 2,
            "commit": "a" * 40,
            "maintenance": True,
            "plan": {"flavor": {"name": "worker-large"}},
        }
        result = self.call("POST", self.prefix + "/deployments", body, "admin")
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE id=?", (result.body["data"]["intentId"],)
            ).fetchone()
        self.assertIs(strict_json(row["body"].encode())["_portalAdmin"], True)
        self.assertIn('"maintenance":true', row["body"])
        self.assertIn('"plan":', row["body"])
        for field in ("maintenance", "plan", "role", "identityProviderConfirmed"):
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

    def test_retained_ipv4_and_identity_provider_confirmations(self) -> None:
        imported = self.adopted()
        route = f"/v1/admin-apps/{imported}"
        detail = self.call("GET", route, owner="admin").body["data"]
        self.assertTrue(detail["requiresMaintenance"])
        self.assertTrue(detail["identityProvider"])
        body = {"configurationRevision": 7, "commit": "a" * 40}
        self.assert_error(
            "IDENTITY_CONFIRMATION_REQUIRED",
            lambda: self.call("POST", route + "/deployments", body, "admin"),
        )
        body["identityProviderConfirmed"] = True
        self.assert_error(
            "MAINTENANCE_REQUIRED", lambda: self.call("POST", route + "/deployments", body, "admin")
        )
        self.assert_error(
            "IDENTITY_CONFIRMATION_REQUIRED",
            lambda: self.call("POST", route + "/state", {"desiredRunning": False}, "admin"),
        )
        self.assert_error(
            "IDENTITY_CONFIRMATION_REQUIRED",
            lambda: self.call("POST", route + "/storage", {"type": "s3"}, "admin"),
        )
        body["maintenance"] = True
        self.assertEqual(self.call("POST", route + "/deployments", body, "admin").status, 202)

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

    def test_admin_storage_delete_requires_step_up_and_typed_confirmation_and_audit(self) -> None:
        result = self.call("POST", self.prefix + "/storage", {"type": "s3"}, "admin")
        self.assertEqual(self.complete(result)["state"], "succeeded")
        storage = self.call("GET", self.prefix + "/storage", owner="admin").body["data"]["items"][0]
        path = self.prefix + "/storage/" + storage["resourceId"]
        key = str(uuid.uuid4())
        self.now += 301
        self.assert_error(
            "STEP_UP_REQUIRED",
            lambda: self.call(
                "DELETE",
                path,
                {"confirmation": "student-app s3"},
                "admin",
                headers={"idempotency-key": key},
            ),
        )
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE sessions SET reauthenticated_at=? WHERE kind='admin'", (self.now,))
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

    def test_storage_delete_refuses_saved_bindings_and_class_changes_need_consent(self) -> None:
        app = self.adopted(bindings=True)
        resource = next(
            item for item in self.fixture.resources.values() if item["applicationId"] == app
        )
        path = f"/v1/admin-apps/{app}/storage/{resource['resourceId']}"
        body = {"confirmation": "operator-class-fixture postgres"}
        self.assert_error(
            "IDENTITY_CONFIRMATION_REQUIRED",
            lambda: self.call(
                "DELETE", path, body, "admin", headers={"idempotency-key": str(uuid.uuid4())}
            ),
        )
        body["identityProviderConfirmed"] = True
        self.assert_error(
            "STORAGE_BOUND",
            lambda: self.call(
                "DELETE", path, body, "admin", headers={"idempotency-key": str(uuid.uuid4())}
            ),
        )

    def test_admin_web_closed_routes_origin_and_csrf(self) -> None:
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text('<div id="root"></div>')
        web = WebServer(("127.0.0.1", 0), self.config, assets)
        self.addCleanup(web.server_close)
        self.assertEqual(
            web.handle(
                "GET", "/admin/apps", {"host": urlsplit(self.config.portal_origin).netloc}, b""
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
        self.assert_error(
            "CSRF_REJECTED",
            lambda: self.call(
                "GET", "/v1/admin-apps", owner="admin", headers={"x-csrf-token": "wrong"}
            ),
        )
        self.assert_error(
            "ORIGIN_REJECTED",
            lambda: self.call(
                "POST", "/v1/admin-apps/adopt", {}, "admin", headers={"origin": "null"}
            ),
        )
