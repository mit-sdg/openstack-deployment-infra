"""Local-admin app actions, project capability delta and secret-free reuse."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from openstack_platform.controller.http import HttpError
from openstack_platform.management.broker import bootstrap
from openstack_platform.management.broker.accounts import security_change
from openstack_platform.management.broker.staff import ReadLimits
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
            for method, route, _handler in self.broker.admin_apps.routes()
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
            "/v1/admin-apps/adopt",
            {"applicationId": identifier, "identityProviderConfirmed": identity},
            "admin",
        )
        return identifier

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

    def test_staff_pass_every_app_action_except_ownership_and_storage_deletion(self) -> None:
        self.staff()
        admin_only = {
            ("POST", "/v1/admin-apps"),
            ("POST", "/v1/admin-apps/adopt"),
            ("PUT", "/v1/admin-apps/{app}/owner"),
            ("DELETE", "/v1/admin-apps/{app}/storage/{resource}"),
        }
        self.assertLess(admin_only, {(m, r) for m, r, _path in self.routes()})
        for method, route, path in self.routes():
            body = {} if method != "GET" else None
            with self.subTest(path=path, method=method):
                if (method, route) in admin_only:
                    calls = len(self.fixture.calls)
                    self.assert_error(
                        "ACCESS_DENIED",
                        lambda method=method, path=path, body=body: self.call(
                            method, path, body, "taylor"
                        ),
                    )
                    self.assertEqual(len(self.fixture.calls), calls)
                    continue
                # Empty bodies fail validation; any answer but a role denial
                # shows the request got past the staff-or-admin gate.
                try:
                    self.call(method, path, body, "taylor")
                except HttpError as error:
                    self.assertNotIn(
                        error.code, {"ACCESS_DENIED", "SESSION_EXPIRED", "STEP_UP_REQUIRED"}
                    )

    def test_staff_manage_any_app_and_admins_see_it_in_the_audit(self) -> None:
        staff = self.staff()
        self.login("bob")
        items = self.call("GET", "/v1/admin-apps", owner="taylor").body["data"]["items"]
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
        self.assert_error("ACCESS_DENIED", lambda: self.call("GET", self.prefix, owner="bob"))
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
        log = self.call("GET", "/v1/account-audit", owner="admin").body["data"]["items"]
        self.assertIn(
            ("taylor", "app_deploy"), {(row["actorUsername"], row["action"]) for row in log}
        )
        # Staff still can't read that audit or manage accounts.
        self.assert_error(
            "ACCESS_DENIED", lambda: self.call("GET", "/v1/account-audit", owner="taylor")
        )

    def test_staff_cannot_allow_maintenance_or_resize_a_deployment(self) -> None:
        self.staff()
        imported = self.adopted()
        for extra in ({"maintenance": True}, {"plan": {}}):
            with self.subTest(extra=extra):
                self.assert_error(
                    "ADMIN_REQUIRED",
                    lambda extra=extra: self.call(
                        "POST",
                        self.prefix + "/deployments",
                        {"configurationRevision": 1, "commit": "a" * 40, **extra},
                        "taylor",
                    ),
                )
        # An app keeping a fixed IP address needs the maintenance outage, so
        # only an admin can deploy it.
        self.assert_error(
            "ADMIN_REQUIRED",
            lambda: self.call(
                "POST",
                f"/v1/admin-apps/{imported}/deployments",
                {"configurationRevision": 7, "commit": "a" * 40, "identityProviderConfirmed": True},
                "taylor",
            ),
        )
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM intents WHERE kind='deploy'").fetchone()[0], 0
            )
        self.assertFalse(
            [
                path
                for method, path, _body in self.fixture.calls
                if method == "POST" and path.endswith("/deployments")
            ]
        )

    def test_journal_runs_staff_app_administration_but_not_owners_or_admin_only_kinds(
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
            (staff, "storage_delete", True),
            (staff, "create_app", True),
            (staff, "app_restart", False),
        ):
            with self.subTest(user=user, kind=kind):
                identifier = intent(user, kind)
                self.broker.journal.dispatch(identifier)
                if blocked:
                    self.assertEqual(state(identifier), review)
                else:
                    self.assertNotEqual(state(identifier)[0], "blocked")
        # Staff resume their own blocked app changes, never admin-only kinds.
        self.assertEqual(
            self.call(
                "POST",
                f"/v1/intents/{intent(staff, 'app_restart', 'blocked')}/resume",
                {},
                "taylor",
            ).status,
            202,
        )
        deletion = intent(staff, "storage_delete", "blocked")
        self.assert_error(
            "ACCESS_DENIED",
            lambda: self.call("POST", f"/v1/intents/{deletion}/resume", {}, "taylor"),
        )
        self.assertEqual(state(deletion)[0], "blocked")

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
                {"applicationId": imported, "identityProviderConfirmed": True},
                "admin",
                row["client_key"],
            ).status,
            200,
        )
        self.assert_error(
            "ALREADY_OWNED",
            lambda: self.call(
                "POST",
                "/v1/admin-apps/adopt",
                {"applicationId": imported, "identityProviderConfirmed": True},
                "admin",
            ),
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

    def test_list_names_owners_and_reports_cached_url_and_last_deploy(self) -> None:
        operator = self.adopted(identity=False)
        with self.broker.database.connect() as db:
            names = {
                row["id"]: (row["username"], row["display_name"])
                for row in db.execute("SELECT id,username,display_name FROM users")
            }

        def listing() -> dict[str, dict[str, Any]]:
            items = self.call("GET", "/v1/admin-apps", owner="admin").body["data"]["items"]
            return {item["applicationId"]: item for item in items}

        before = listing()
        self.assertEqual(set(before), {self.app_id, operator})
        for item in before.values():
            self.assertEqual(
                (item["ownerUsername"], item["ownerDisplayName"]), names[item["ownerId"]]
            )
        # Additive: every earlier field is still present.
        self.assertEqual(
            set(before[operator]) - {"ownerUsername", "ownerDisplayName", "url", "lastDeployedAt"},
            {"applicationId", "slug", "ownerId", "savedRevision", "lifecycleState"},
        )
        # The list never reads the controller; values come from the last observation.
        self.assertIsNone(before[operator]["lastDeployedAt"])
        detail = self.call("GET", f"/v1/admin-apps/{operator}", owner="admin").body["data"]
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
        page = self.call("GET", "/v1/admin-apps?limit=1", owner="admin").body["data"]
        rest = self.call(
            "GET", "/v1/admin-apps?limit=1&cursor=" + page["nextCursor"], owner="admin"
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
            reply = web.forward("GET", "/api/v1/admin-apps", "", {}, b"")
        self.assertEqual(strict_json(reply.body), body)

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
        listing = self.call("GET", "/v1/accounts", owner="admin").body["data"]["items"]
        accounts = {item["userId"]: item for item in listing}
        for user in (self.admin_user, staff):
            self.assertEqual(
                (accounts[user]["appLimit"], accounts[user]["concurrencyLimit"]), (None, None)
            )
        self.assertEqual(accounts[self.owner]["appLimit"], limit)
        for user in (self.admin_user, staff):
            staff_view = self.call("GET", f"/v1/staff/owners/{user}", owner="admin")
            quota = staff_view.body["data"]["quota"]
            self.assertEqual(
                (quota["apps"]["limit"], quota["concurrentOperations"]["limit"]), (None, None)
            )
            self.assertEqual(quota["apps"]["used"], limit + 2)
            self.assert_error(
                "ADMIN_UNLIMITED",
                lambda user=user: self.call(
                    "PUT",
                    f"/v1/accounts/{user}/quotas",
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
        result = self.call("POST", self.prefix + "/deployments", body, "admin")
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
        # Resuming a deletion still needs a recent password and code.
        self.now += 301
        self.assert_error(
            "STEP_UP_REQUIRED",
            lambda: self.call("POST", f"/v1/intents/{deletion}/resume", {}, "admin"),
        )
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE sessions SET reauthenticated_at=? WHERE kind='admin'", (self.now,))
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
        result = self.call("GET", f"/v1/admin-apps/{app}/logs?stream=stderr", owner="admin")
        self.assertEqual(result.body["data"]["stream"], "stderr")
        self.assertTrue(result.body["data"]["running"])
        self.assertEqual(result.body["data"]["text"], "Warning: SESSION_SECRET is short\n")
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.call("GET", f"/v1/admin-apps/{app}/logs?lines=5", owner="admin"),
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

    def test_adoption_requires_step_up_even_when_owner_is_admin(self) -> None:
        self.now += 301
        for owner in (None, self.admin_user, self.owner):
            body = {"applicationId": str(uuid.uuid4()), **({"ownerId": owner} if owner else {})}
            with self.subTest(owner=owner):
                self.assert_error(
                    "STEP_UP_REQUIRED",
                    lambda body=body: self.call("POST", "/v1/admin-apps/adopt", body, "admin"),
                )

    def test_identity_confirmation_required_for_adoption_and_reassignment(self) -> None:
        identifier = str(uuid.uuid4())
        self.fixture.seed_operator_app(identifier, self.config.commons_origin)
        body = {"applicationId": identifier, "ownerId": self.owner}
        self.assert_error(
            "IDENTITY_CONFIRMATION_REQUIRED",
            lambda: self.call("POST", "/v1/admin-apps/adopt", body, "admin"),
        )
        body["identityProviderConfirmed"] = True
        self.call("POST", "/v1/admin-apps/adopt", body, "admin")
        transfer = {"expectedOwnerId": self.owner, "ownerId": self.admin_user}
        path = f"/v1/admin-apps/{identifier}/owner"
        self.assert_error(
            "IDENTITY_CONFIRMATION_REQUIRED", lambda: self.call("PUT", path, transfer, "admin")
        )
        transfer["identityProviderConfirmed"] = True
        self.assertEqual(self.call("PUT", path, transfer, "admin").status, 200)

    def test_identity_confirmation_follows_app_onto_owner_and_staff_routes(self) -> None:
        staff = self.login("taylor")
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET role='staff' WHERE id=?", (staff,))
            security_change(db, staff)
        self.login("taylor")
        for account, owner in (("alice", self.owner), ("taylor", staff)):
            identifier = str(uuid.uuid4())
            self.fixture.seed_operator_app(identifier, self.config.commons_origin)
            self.fixture.apps[identifier]["slug"] += "-" + account
            self.call(
                "POST",
                "/v1/admin-apps/adopt",
                {"applicationId": identifier, "ownerId": owner, "identityProviderConfirmed": True},
                "admin",
            )
            prefix = f"/v1/apps/{identifier}"
            self.assertTrue(
                self.call("GET", prefix, owner=account).body["data"]["identityProvider"]
            )
            for suffix, fields in (
                ("deployments", {"configurationRevision": 7, "commit": "a" * 40}),
                ("storage", {"type": "postgres"}),
            ):
                with self.subTest(account=account, suffix=suffix):
                    self.assert_error(
                        "IDENTITY_CONFIRMATION_REQUIRED",
                        lambda fields=fields, suffix=suffix, prefix=prefix, account=account: (
                            self.call("POST", prefix + "/" + suffix, fields, account)
                        ),
                    )
            self.assert_error(
                "IDENTITY_CONFIRMATION_REQUIRED",
                lambda prefix=prefix, account=account: self.call(
                    "POST", prefix + "/state", {"desiredRunning": False}, account
                ),
            )
            storage = self.call(
                "POST",
                prefix + "/storage",
                {"type": "postgres", "identityProviderConfirmed": True},
                account,
            ).body["data"]
            self.broker.journal.dispatch(storage["intentId"])
            resource = self.call("GET", prefix + "/storage", owner=account).body["data"]["items"][
                0
            ]["resourceId"]
            for action in ("verify", "rotate"):
                self.assert_error(
                    "IDENTITY_CONFIRMATION_REQUIRED",
                    lambda prefix=prefix, account=account, action=action, resource=resource: (
                        self.call("POST", prefix + f"/storage/{resource}/{action}", {}, account)
                    ),
                )
            with self.broker.database.connect() as db:
                self.assertNotIn("identityProviderConfirmed", "\n".join(db.iterdump()))

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
        with patch.object(web.broker, "request") as upstream:
            path = f"/api/v1/admin-apps/{self.app_id}/deployments/{uuid.uuid4()}/build-log"
            self.assertEqual(web.forward("GET", path, "", {}, b"").status, 404)
            upstream.assert_not_called()
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
