"""Wire contract evidence against the real project API and its development double."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Any
from unittest.mock import patch

from openstack_platform.controller.http import ControllerServer, HttpError
from openstack_platform.management.broker.client import UnixConnection
from openstack_platform.management.common import canonical, strict_json
from tests import test_controller_recovery as recovery_fixtures
from tests.test_management import ManagementCase


class RealProjectContractTests(ManagementCase):
    def test_natural_competing_deployments_use_operation_conflict(self) -> None:
        entered, release = threading.Event(), threading.Event()
        original = self.real.fixture.api.helper_caller

        def blocked(config: Any, action: str, values: Any, **kwargs: Any) -> Any:
            if action == "app.build":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("fixture release deadline")
            return original(config, action, values, **kwargs)

        self.real.fixture.api.helper_caller = blocked
        body = {
            "repository": "https://github.com/example/student-app",
            "commit": "c" * 40,
            "requestedRef": "main",
            "configurationRevision": 1,
            "configuration": {
                "schemaVersion": 1,
                "build": {
                    "runtime": "node",
                    "packages": ["."],
                    "buildScript": None,
                    "startScript": "start",
                },
                "runtime": {"port": 3000, "healthPath": "/health"},
                "storageBindings": [],
            },
        }
        try:
            for name, path in (("real", self.real_socket), ("fake", self.config.controller_socket)):
                app = str(uuid.uuid4())
                status, _created, _headers = self.wire(
                    path, "POST", "/v1/applications", {"slug": name + "-conflict-project"}, app
                )
                self.assertEqual(status, 201)
                operation = str(uuid.uuid4())
                status, _admitted, _headers = self.wire(
                    path, "POST", f"/v1/applications/{app}/deployments", body, operation
                )
                self.assertEqual(status, 202)
                if name == "real":
                    self.assertTrue(entered.wait(2))
                status, error, _headers = self.wire(
                    path, "POST", f"/v1/applications/{app}/deployments", body, str(uuid.uuid4())
                )
                self.assertEqual(status, 409)
                self.assertEqual(error["error"]["code"], "OPERATION_CONFLICT")
                self.assertEqual(error["error"]["operationId"], operation)
        finally:
            release.set()
            self.real.fixture.api.wait_for_operations()

    def setUp(self) -> None:
        super().setUp()
        self.real = recovery_fixtures.ControllerRecoveryTests()
        self.real.setUp()
        self.addCleanup(self.real.doCleanups)
        original_helper = self.real.helper

        def helper(config: Any, action: str, values: Any, **kwargs: Any) -> Any:
            if action == "app.build.logs":
                text = "Build rejected by the local helper fixture.\n"
                return {
                    "exists": True,
                    "text": text,
                    "nextOffset": len(text.encode()),
                    "state": "failed",
                    "truncated": False,
                }
            return original_helper(config, action, values, **kwargs)

        # Existing fixtures dispatch in one thread. Hosting the actual router
        # requires the same cross-thread SQLite mode as controller/main.py.
        from openstack_platform.controller import database as db
        from openstack_platform.controller.api import ControllerAPI

        self.real.fixture.api.close()
        self.real.fixture.connection.close()
        connection = db.connect(
            self.real.fixture.root / "platform.sqlite3", check_same_thread=False
        )
        self.real.connection = self.real.fixture.connection = connection
        self.real.fixture.api = ControllerAPI(
            connection, self.real.fixture.config, self.real.fixture.root, helper_caller=helper
        )
        self.real_socket = self.sockets / "real.sock"
        self.real_server = ControllerServer(
            str(self.real_socket), self.real.fixture.api.router("project")
        )
        thread = threading.Thread(
            target=self.real_server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        self.addCleanup(self.real_server.server_close)
        self.addCleanup(self.real_server.shutdown)
        self.broker.client.path = self.real_socket
        self.broker.client.timeout = 5
        self.login()

    def test_real_project_admits_all_storage_outputs_and_replays_existing_operations(self) -> None:
        from openstack_platform.controller import database as db
        from openstack_platform.controller import storage
        from openstack_platform.controller.storage_contract import OUTPUT_ENVIRONMENT_KEYS

        original = self.real.fixture.api.helper_caller

        def helper(config, action, values, **kwargs):
            if action.startswith("storage.") and action.endswith(".create"):
                kind = action.split(".")[1]
                app = db.get_application(self.real.connection, values["applicationId"])
                name = storage._provider_name(config, app, kind, "default")
                return {
                    "providerId": name,
                    "providerName": name,
                    "verified": True,
                    "evidenceAccepted": True,
                    "modifyIndex": 1,
                }
            return original(config, action, values, **kwargs)

        self.real.fixture.api.helper_caller = helper
        for kind, outputs in OUTPUT_ENVIRONMENT_KEYS.items():
            with self.subTest(kind=kind):
                app = str(uuid.uuid4())
                status, _created, _ = self.wire(
                    self.real_socket,
                    "POST",
                    "/v1/applications",
                    {"slug": "all-outputs-" + kind},
                    app,
                )
                self.assertEqual(status, 201)
                key = str(uuid.uuid4())
                status, _storage, _ = self.wire(
                    self.real_socket, "POST", f"/v1/applications/{app}/storage", {"type": kind}, key
                )
                self.assertEqual(status, 202)
                self.real.fixture.api.wait_for_operations()
                status, resources, _ = self.wire(
                    self.real_socket, "GET", f"/v1/applications/{app}/storage"
                )
                self.assertEqual(status, 200)
                resource = resources["items"][0]
                self.assertEqual(resource["lifecycleState"], "active")
                body = {
                    "repository": "https://github.com/example/app",
                    "requestedRef": "main",
                    "commit": "a" * 40,
                    "configurationRevision": 1,
                    "configuration": {
                        "schemaVersion": 1,
                        "build": {
                            "runtime": "node",
                            "packages": ["."],
                            "buildScript": None,
                            "startScript": "start",
                        },
                        "runtime": {"port": 3000, "healthPath": "/health"},
                        "storageBindings": [
                            {"resourceId": resource["resourceId"], "outputs": dict(outputs)}
                        ],
                    },
                }
                deployment = str(uuid.uuid4())
                status, admitted, _ = self.wire(
                    self.real_socket,
                    "POST",
                    f"/v1/applications/{app}/deployments",
                    body,
                    deployment,
                )
                self.assertEqual(status, 202, admitted)
                self.real.fixture.api.wait_for_operations()
                operation = db.get_operation(self.real.connection, deployment)
                self.assertEqual(operation.phase, "build_rejected")
                self.assertIsNotNone(db.get_deployment_attempt(self.real.connection, deployment))
                status, replayed, _ = self.wire(
                    self.real_socket,
                    "POST",
                    f"/v1/applications/{app}/deployments",
                    body,
                    deployment,
                )
                self.assertEqual(status, 202)
                self.assertEqual(replayed["operationId"], deployment)
                changed = {**body, "commit": "b" * 40}
                status, conflict, _ = self.wire(
                    self.real_socket,
                    "POST",
                    f"/v1/applications/{app}/deployments",
                    changed,
                    deployment,
                )
                self.assertEqual(status, 409)
                self.assertEqual(conflict["error"]["code"], "IDEMPOTENCY_CONFLICT")
                changed_binding = copy.deepcopy(body)
                first_output = next(iter(outputs))
                changed_binding["configuration"]["storageBindings"][0]["outputs"][first_output] = (
                    "RENAMED_OUTPUT"
                )
                status, conflict, _ = self.wire(
                    self.real_socket,
                    "POST",
                    f"/v1/applications/{app}/deployments",
                    changed_binding,
                    deployment,
                )
                self.assertEqual(status, 409)
                self.assertEqual(conflict["error"]["code"], "IDEMPOTENCY_CONFLICT")

    @staticmethod
    def wire(
        path: Any, method: str, target: str, body: Any = None, key: str | None = None
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        connection = UnixConnection(path, 5)
        try:
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Idempotency-Key"] = key
            connection.request(
                method, target, None if body is None else canonical(body).encode(), headers
            )
            response = connection.getresponse()
            value = strict_json(response.read())
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def test_broker_real_creation_terminal_failure_history_deployment_and_log(self) -> None:
        app = self.create(slug="contract-project")
        self.save(app)
        intent = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "a" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        self.assertEqual(intent["state"], "accepted")
        self.real.fixture.api.wait_for_operations()
        self.broker.journal.dispatch(intent["intentId"])
        terminal = self.call("GET", f"/v1/intents/{intent['intentId']}", owner="alice").body["data"]
        self.assertEqual(terminal["state"], "failed")
        self.assertEqual(terminal["operation"]["cleanupState"], "confirmed")
        history = self.call("GET", f"/v1/apps/{app}/deployments", owner="alice").body["data"]
        self.assertEqual(len(history["items"]), 1)
        deployment = history["items"][0]["deploymentId"]
        detail = self.call("GET", f"/v1/apps/{app}/deployments/{deployment}", owner="alice").body[
            "data"
        ]
        self.assertEqual(detail["repositoryCommit"], "a" * 40)
        self.assertEqual(detail["sourceRepository"], "https://github.com/example/student-app")
        log = self.call(
            "GET", f"/v1/apps/{app}/deployments/{deployment}/build-log", owner="alice"
        ).body["data"]
        self.assertEqual(log["state"], "failed")
        self.assertIn("Build rejected", log["text"])

    def test_fake_and_real_wire_shapes_codes_envelopes_and_location(self) -> None:
        model_keys: dict[str, set[str]] = {}
        for name, path in (("real", self.real_socket), ("fake", self.config.controller_socket)):
            key = str(uuid.uuid4())
            status, created, headers = self.wire(
                path, "POST", "/v1/applications", {"slug": name + "-wire-project"}, key
            )
            self.assertEqual(status, 201)
            self.assertEqual(headers["Location"], f"/v1/applications/{key}")
            status, app, _headers = self.wire(path, "GET", f"/v1/applications/{key}")
            self.assertEqual(status, 200)
            model_keys[name + "create"] = set(created)
            model_keys[name + "app"] = set(app)
            model_keys[name + "live"] = set(app["live"])
            body = {
                "repository": "https://github.com/example/student-app",
                "commit": "b" * 40,
                "requestedRef": "main",
                "configurationRevision": 1,
                "configuration": {
                    "schemaVersion": 1,
                    "build": {
                        "runtime": "node",
                        "packages": ["."],
                        "buildScript": None,
                        "startScript": "start",
                    },
                    "runtime": {"port": 3000, "healthPath": "/health"},
                    "storageBindings": [],
                },
            }
            deployment_key = str(uuid.uuid4())
            status, admitted, headers = self.wire(
                path, "POST", f"/v1/applications/{key}/deployments", body, deployment_key
            )
            self.assertEqual(status, 202)
            self.assertEqual(headers["Location"], admitted["statusUrl"])
            model_keys[name + "admission"] = set(admitted)
            if name == "real":
                self.real.fixture.api.wait_for_operations()
            status, operation, _headers = self.wire(path, "GET", f"/v1/operations/{deployment_key}")
            self.assertEqual(status, 200)
            model_keys[name + "operation"] = set(operation)
            status, detail, _headers = self.wire(path, "GET", f"/v1/deployments/{deployment_key}")
            self.assertEqual(status, 200)
            model_keys[name + "deployment"] = set(detail)
            self.assertEqual(detail["snapshotKind"], "strict")
            status, page, _headers = self.wire(path, "GET", f"/v1/applications/{key}/deployments")
            self.assertEqual(status, 200)
            model_keys[name + "page"] = set(page)
            status, log, _headers = self.wire(
                path, "GET", f"/v1/deployments/{deployment_key}/build-log"
            )
            self.assertEqual(status, 200)
            model_keys[name + "log"] = set(log)
            for suffix in ("?lines=0", "?lines=1001", "?offset=-1"):
                query_status, query_error, _headers = self.wire(
                    path, "GET", f"/v1/deployments/{deployment_key}/build-log" + suffix
                )
                self.assertEqual(query_status, 400)
                self.assertEqual(query_error["error"]["code"], "INVALID_QUERY")
            for suffix in ("?limit=0", "?limit=101", "?cursor=not-uuid"):
                query_status, _error, _headers = self.wire(
                    path, "GET", f"/v1/applications/{key}/deployments" + suffix
                )
                self.assertEqual(query_status, 400)
            status, error, _headers = self.wire(
                path, "POST", "/v1/applications", {"slug": "changed-project"}, key
            )
            self.assertEqual(status, 409)
            self.assertEqual(error["error"]["code"], "IDEMPOTENCY_CONFLICT")
            self.assertEqual(set(error), {"error"})
            model_keys[name + "error"] = set(error["error"])
            for target, code in (
                (f"/v1/applications/{uuid.uuid4()}", "APPLICATION_NOT_FOUND"),
                (f"/v1/deployments/{uuid.uuid4()}", "DEPLOYMENT_NOT_FOUND"),
                (f"/v1/operations/{uuid.uuid4()}", "OPERATION_NOT_FOUND"),
            ):
                status, error, _headers = self.wire(path, "GET", target)
                self.assertEqual(status, 404)
                self.assertEqual(error["error"]["code"], code)
        for kind in (
            "create",
            "app",
            "live",
            "admission",
            "operation",
            "deployment",
            "page",
            "log",
            "error",
        ):
            self.assertEqual(model_keys["real" + kind], model_keys["fake" + kind], kind)

    def test_retryable_errors_and_conflict_envelopes_match_real_safe_mapping(self) -> None:
        from openstack_platform import remote
        from openstack_platform.controller import database as db

        samples = [
            (503, "DEPENDENCY_UNAVAILABLE", remote.DependencyUnavailable("fixture")),
            (504, "DEADLINE_EXCEEDED", TimeoutError("fixture")),
            (
                409,
                "OPERATION_CONFLICT",
                db.UnfinishedOperationError("app-fixture", str(uuid.uuid4()), "app.deploy"),
            ),
        ]
        for status, code, exception in samples:

            def fail(_request: Any, exception: Exception = exception) -> Any:
                raise exception

            router = self.real.fixture.api.router("project")
            router.add("GET", "/v1/fixture/error", self.real.fixture.api._safe(fail))
            old = self.real_server.router
            self.real_server.router = router
            try:
                real_status, real_error, _headers = self.wire(
                    self.real_socket, "GET", "/v1/fixture/error"
                )
            finally:
                self.real_server.router = old
            self.fixture.error_next = status, code
            fake_status, fake_error, _headers = self.wire(
                self.config.controller_socket, "GET", f"/v1/applications/{uuid.uuid4()}"
            )
            self.assertEqual(real_status, fake_status)
            self.assertEqual(real_error["error"]["code"], fake_error["error"]["code"])
            self.assertEqual(real_error["error"]["retryable"], fake_error["error"]["retryable"])
            self.assertEqual(set(real_error["error"]), set(fake_error["error"]))

    def test_staff_reads_use_real_project_capability_without_global_enumeration(self) -> None:
        from openstack_platform.management.broker.accounts import security_change

        app = self.create(slug="staff-contract-project")
        self.save(app)
        intent = self.call(
            "POST",
            f"/v1/apps/{app}/deployments",
            {"commit": "b" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        self.real.fixture.api.wait_for_operations()
        self.broker.journal.dispatch(intent["intentId"])
        user = self.call("GET", "/v1/session", owner="alice").body["data"]["user"]["id"]
        with self.broker.database.connect(write=True) as connection:
            connection.execute("UPDATE users SET role='staff' WHERE id=?", (user,))
            security_change(connection, user)
        options = self.call("GET", "/v1/auth/options").body
        signed = self.call(
            "POST",
            "/v1/auth/login",
            {
                "csrfToken": options["data"]["csrfToken"],
                "username": "alice",
                "password": self.commons.passwords["alice"],
            },
            headers={
                "cookie": self.config.login_cookie + "=" + options["browser"]["cookies"][0]["value"]
            },
        ).body
        self.tokens["alice"] = signed["browser"]["cookies"][1]["value"]
        self.csrf["alice"] = self.call("GET", "/v1/session", owner="alice").body["data"][
            "csrfToken"
        ]
        # Exercise a substantial per-app history without changing project privilege.
        connection = self.real.fixture.connection
        columns = [row[1] for row in connection.execute("PRAGMA table_info(deployment_attempts)")]
        template = list(
            connection.execute(
                "SELECT * FROM deployment_attempts WHERE deployment_id=?", (intent["operationId"],)
            ).fetchone()
        )
        rows = []
        for _ in range(1999):
            values = template.copy()
            key = str(uuid.uuid4())
            values[columns.index("deployment_id")] = key
            values[columns.index("idempotency_request_id")] = key
            connection.execute(
                "INSERT INTO idempotency_requests VALUES(?,?,'deployment',?,?,?)",
                (
                    key,
                    "a" * 64,
                    key,
                    values[columns.index("requested_at")],
                    values[columns.index("updated_at")],
                ),
            )
            rows.append(values)
        connection.executemany(
            f"INSERT INTO deployment_attempts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            rows,
        )
        connection.commit()
        for path in (
            f"/v1/staff/apps/{app}",
            f"/v1/staff/apps/{app}/deployments",
            f"/v1/staff/apps/{app}/deployments/{intent['operationId']}",
        ):
            response = self.call("GET", path, owner="alice")
            self.assertEqual(response.status, 200)
            self.assertNotIn('"configuration"', canonical(response.body))
            self.assertNotIn('"operationId"', canonical(response.body))
            if path.endswith("/deployments"):
                self.assertEqual(len(response.body["data"]["items"]), 25)
                self.assertTrue(response.body["data"]["truncated"])
        for path in (
            "/v1/admin/applications",
            "/v1/admin/deployments",
            "/v1/admin/operations",
            "/v1/applications",
        ):
            status, _body, _headers = self.wire(self.real_socket, "GET", path)
            self.assertEqual(status, 405 if path == "/v1/applications" else 404)
        unowned = str(uuid.uuid4())
        self.wire(
            self.real_socket, "POST", "/v1/applications", {"slug": "operator-only-project"}, unowned
        )
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/staff/apps/{unowned}", owner="alice")
        )


class DeletedApplicationContractTests(RealProjectContractTests):
    def delete_remote(self, app: str, name: str) -> None:
        response = self.real.fixture.api.router("privileged").dispatch(
            "POST",
            f"/v1/applications/{app}/delete",
            {"idempotency-key": str(uuid.uuid4())},
            {"confirmation": name},
        )
        self.assertEqual(response.status, 202)
        self.real.fixture.api.wait_for_operations()
        from openstack_platform.controller import database as db

        operation = db.get_operation(self.real.connection, response.body["operationId"])
        self.assertEqual((operation.status, operation.phase), ("succeeded", "tombstoned"))
        status, missing, _ = self.wire(self.real_socket, "GET", f"/v1/applications/{app}")
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"]["code"], "APPLICATION_NOT_FOUND")

    def test_operator_deletion_is_retired_by_owner_read_and_preserves_history(self) -> None:
        app = self.create(slug="retire-on-read")
        self.save(app)
        self.assertEqual(
            self.call("GET", "/v1/session", owner="alice").body["data"]["quota"]["apps"]["used"], 1
        )
        self.delete_remote(app, "retire-on-read")
        listing = self.call("GET", "/v1/apps", owner="alice").body["data"]
        self.assertEqual(listing["items"], [])
        self.assertEqual(listing["quota"]["apps"]["used"], 0)
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute("SELECT lifecycle FROM apps WHERE id=?", (app,)).fetchone()[0], "deleted"
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM configurations WHERE app_id=?", (app,)).fetchone()[
                    0
                ],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM audit WHERE app_id=? AND action='app_deleted_by_administrator'",
                    (app,),
                ).fetchone()[0],
                1,
            )
        for method, suffix, body in (
            ("PUT", "/environment/TOKEN", {"value": "not-stored"}),
            ("POST", "/storage", {"type": "postgres"}),
            ("POST", "/deployments", {"configurationRevision": 1, "commit": "a" * 40}),
        ):
            with self.subTest(suffix=suffix), self.assertRaises(HttpError) as caught:
                self.call(method, f"/v1/apps/{app}{suffix}", body, "alice")
            self.assertEqual(caught.exception.code, "APPLICATION_DELETED")
            self.assertIn("deleted by an administrator", caught.exception.summary)
        self.assertEqual(self.call("GET", "/v1/apps", owner="alice").body["data"]["items"], [])
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM audit WHERE action='app_deleted_by_administrator'"
                ).fetchone()[0],
                1,
            )
        self.create(slug="quota-slot-reused")

    def test_background_deletion_scan_and_staff_admin_lists(self) -> None:
        from openstack_platform.management.broker import bootstrap
        from openstack_platform.management.broker.accounts import security_change
        from tests import test_management_accounts as accounts

        self.now = time.time()
        folder = bootstrap.enrollment_file(self.config).parent
        folder.mkdir(parents=True, mode=0o2750)
        folder.chmod(0o2750)
        # Reuse real enrollment/MFA helpers without changing role enforcement.
        self.anonymous = accounts.AccountsTests.anonymous.__get__(self)
        self.begin = accounts.AccountsTests.begin.__get__(self)
        self.finish = accounts.AccountsTests.finish.__get__(self)
        accounts.AccountsTests.admin(self)
        app = self.create(slug="retire-in-background")
        self.delete_remote(app, "retire-in-background")
        self.broker.journal.reconcile()
        self.assertEqual(
            self.call("GET", "/v1/admin-apps", owner="admin").body["data"]["items"], []
        )
        with self.broker.database.connect(write=True) as db:
            user = db.execute("SELECT user_id FROM apps WHERE id=?", (app,)).fetchone()[0]
            db.execute("UPDATE users SET role='staff' WHERE id=?", (user,))
            security_change(db, user)
        self.login()
        self.assertEqual(
            self.call("GET", "/v1/staff/apps", owner="alice").body["data"]["items"], []
        )
        for observer in ("staff", "admin"):
            pending = self.create(slug="lazy-delete-" + observer)
            self.delete_remote(pending, "lazy-delete-" + observer)
            route = "/v1/staff/apps" if observer == "staff" else "/v1/admin-apps"
            actor = "alice" if observer == "staff" else "admin"
            self.assertEqual(self.call("GET", route, owner=actor).body["data"]["items"], [])
            with self.broker.database.connect() as db:
                self.assertEqual(
                    db.execute("SELECT lifecycle FROM apps WHERE id=?", (pending,)).fetchone()[0],
                    "deleted",
                )
        with self.assertRaises(HttpError) as caught:
            self.call(
                "POST",
                f"/v1/admin-apps/{app}/deployments",
                {"commit": "a" * 40, "configurationRevision": 1},
                "admin",
            )
        self.assertEqual(caught.exception.code, "APPLICATION_DELETED")

    def test_dependency_errors_and_creating_apps_are_not_deleted(self) -> None:
        app = self.create(slug="still-present")
        for response in (
            (503, {"error": {"code": "DEPENDENCY_UNAVAILABLE"}}),
            (404, {"error": {"code": "NOT_FOUND"}}),
        ):
            with patch.object(self.broker.client, "request", return_value=response):
                self.assertFalse(self.broker.journal.observe_deleted(app))
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE apps SET lifecycle='creating' WHERE id=?", (app,))
        with patch.object(self.broker.client, "request") as request:
            self.assertFalse(self.broker.journal.observe_deleted(app))
            request.assert_not_called()
