from __future__ import annotations

import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from openstack_platform import remote
from openstack_platform.config import (
    Config,
    Limits,
    PlatformConfig,
    Policy,
    RuntimeImages,
    StandardProfile,
)
from openstack_platform.controller import database as db
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.http import HttpError, Response


class ControllerAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = db.connect(self.root / "platform.sqlite3")
        db.migrate(self.connection)
        platform = PlatformConfig(
            "project",
            "00000000-0000-4000-8000-000000000099",
            "test",
            "test-platform",
            "example.test",
            "dc1",
            "region1",
            "network",
            {"paths": {"root": "/srv/openstack-platform"}, "flavors": {"builder": "builder-small"}},
        )
        policy = Policy(
            StandardProfile(  # type: ignore[arg-type]
                "worker-small", 500, 512, 20, 1_000_000, 1_000_000, 1_000_000, 100
            ),
            RuntimeImages(
                "registry.example/bun@sha256:" + "a" * 64,
                "registry.example/node@sha256:" + "b" * 64,
            ),
            "age1" + "q" * 58,
            Limits(
                1_000_000,
                262_144,
                65_536,
                262_144,
                1_000_000,
                1_000_000,
                1_000_000,
                10,
                10,
                30,
                30,
                1,
            ),
        )
        self.config = Config(platform, policy)
        self.helper_calls: list[tuple[str, dict[str, object]]] = []

        def helper(_config, action, values, *, deadline=None):
            self.helper_calls.append((action, dict(values)))
            if action == "app.env.set":
                return {
                    "keys": sorted(values["updates"]),
                    "modifyIndex": 1,
                    "restarted": False,
                    "schedulerHealthy": False,
                    "publicHealthy": False,
                }
            if action == "app.remove":
                return {"jobAbsent": True, "variableAbsent": True}
            if action == "app.source.key" and values["mode"] == "delete":
                return {"slug": values["slug"], "present": False}
            if action == "app.worker.delete":
                return {"absent": True}
            raise AssertionError(f"unexpected helper action {action}")

        self.api = ControllerAPI(
            self.connection,
            self.config,
            self.root,
            helper_caller=helper,
        )
        self.router = self.api.router()

    def tearDown(self) -> None:
        self.api.close()
        self.connection.close()
        self.temporary.cleanup()

    @staticmethod
    def headers(key: str) -> dict[str, str]:
        return {"Idempotency-Key": key}

    def dispatch(self, method, path, body=None, headers=None):
        return self.router.dispatch(method, path, headers or {}, body)

    def create_application(self, key="00000000-0000-4000-8000-000000000001"):
        return self.dispatch(
            "POST",
            "/v1/applications",
            {"slug": "demo-app"},
            self.headers(key),
        )

    def test_socket_capability_routers_separate_project_and_privileged_routes(self) -> None:
        project = self.api.router("project")
        privileged = self.api.router("privileged")
        self.assertEqual(
            project.dispatch("GET", "/v1/health", {}, None).body,
            {"status": "ok", "capability": "project"},
        )
        for router, method, path in (
            (project, "GET", "/v1/admin/applications"),
            (project, "POST", "/v1/applications/00000000-0000-4000-8000-000000000001/delete"),
            (privileged, "GET", "/v1/health"),
            (privileged, "POST", "/v1/applications"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(HttpError) as raised:
                router.dispatch(method, path, {}, None)
            self.assertEqual(raised.exception.code, "NOT_FOUND")

    def test_task_restart_is_project_scoped_idempotent_and_busy_guarded(self) -> None:
        application = self.create_application().body["applicationId"]
        service = mock.Mock()
        with mock.patch(
            "openstack_platform.controller.api.ApplicationService", return_value=service
        ):
            route = f"/v1/applications/{application}/restart"
            key = "00000000-0000-4000-8000-000000000081"
            response = self.dispatch("POST", route, {}, self.headers(key))
            self.assertEqual(response.status, 202)
            self.api.wait_for_operations()
            replay = self.dispatch("POST", route, {}, self.headers(key))
            self.assertEqual(replay.body["operationId"], response.body["operationId"])
            self.assertEqual(service.restart.call_count, 1)
            with self.assertRaises(HttpError):
                self.dispatch("POST", route, {"allocationId": "other"}, self.headers(key))

    def test_private_source_routes_validate_inputs_and_project_metadata(self) -> None:
        application = self.create_application().body["applicationId"]
        source = f"/v1/applications/{application}/source/"
        repository = "https://github.com/ada/notes"
        configuration = {
            "schemaVersion": 1,
            "build": {
                "runtime": "node",
                "packages": ["."],
                "buildScript": "build",
                "startScript": "start",
            },
            "runtime": {"port": 3000, "healthPath": "/health"},
            "storageBindings": [],
        }
        self.api.helper_caller = mock.Mock(
            return_value={
                "keyPresent": True,
                "items": [
                    {
                        "sha": "a" * 40,
                        "message": "Change",
                        "author": "Ada",
                        "date": "2026-10-04T00:00:00Z",
                        "secret": "hidden",
                    }
                ],
            }
        )
        response = self.dispatch(
            "POST", source + "commits", {"repository": repository, "branch": "main"}
        )
        self.assertNotIn("secret", repr(response.body))
        self.api.helper_caller.return_value = {
            "keyPresent": True,
            "items": [
                {"id": name, "label": name, "state": "ok"}
                for name in ("package-json", "script:start", "script:build", "lockfile:.")
            ],
        }
        response = self.dispatch(
            "POST",
            source + "check",
            {"repository": repository, "commit": "a" * 40, "configuration": configuration},
        )
        self.assertEqual(len(response.body["items"]), 4)
        runtime = {
            "id": "runtime-version",
            "label": "Node.js >=22 <23 from engines.node",
            "state": "ok",
        }
        self.api.helper_caller.return_value["items"].append(runtime)
        response = self.dispatch(
            "POST",
            source + "check",
            {"repository": repository, "commit": "a" * 40, "configuration": configuration},
        )
        self.assertEqual(response.body["items"][-1], runtime)
        self.api.helper_caller.return_value["items"].append(
            {"id": "runtime-default", "label": "Node.js (platform default)", "state": "ok"}
        )
        with self.assertRaises(HttpError):
            self.dispatch(
                "POST",
                source + "check",
                {"repository": repository, "commit": "a" * 40, "configuration": configuration},
            )
        with self.assertRaises(HttpError):
            self.dispatch(
                "POST",
                source + "check",
                {"repository": repository, "commit": "bad", "configuration": configuration},
            )
        self.api.helper_caller.side_effect = remote.HelperError(
            "SOURCE_UNAVAILABLE", "unsafe stderr"
        )
        with self.assertRaises(HttpError) as error:
            self.dispatch("POST", source + "commits", {"repository": repository, "branch": "main"})
        self.assertEqual(error.exception.code, "SOURCE_UNAVAILABLE")
        self.assertNotIn("unsafe", str(error.exception))

    def test_runtime_log_reads_the_selected_stream(self) -> None:
        application = self.create_application().body["applicationId"]
        calls: list[dict[str, object]] = []

        def helper(_config, action, values, *, deadline=None):
            self.assertEqual(action, "app.logs")
            calls.append(dict(values))
            return {"text": "stream " + ("err" if values["stderr"] else "out") + "\n"}

        self.api.logs.helper_caller = helper
        path = f"/v1/applications/{application}/runtime-log"
        default = self.dispatch("GET", path + "?lines=20")
        errors = self.dispatch("GET", path + "?stream=stderr&lines=20")
        self.assertEqual((default.body["stream"], default.body["text"]), ("stdout", "stream out\n"))
        self.assertEqual((errors.body["stream"], errors.body["text"]), ("stderr", "stream err\n"))
        self.assertEqual(
            calls,
            [
                {"slug": "demo-app", "stderr": False, "lines": 20},
                {"slug": "demo-app", "stderr": True, "lines": 20},
            ],
        )
        for query in ("stream=both", "stream=stdout&stream=stderr", "stream=", "offset=1"):
            with self.subTest(query=query), self.assertRaises(HttpError) as raised:
                self.dispatch("GET", f"{path}?{query}")
            self.assertEqual(raised.exception.code, "INVALID_QUERY")
        self.assertEqual(len(calls), 2)

    def test_project_deployment_validates_and_forwards_plan_and_maintenance(self) -> None:
        application = self.create_application().body["applicationId"]
        body = {
            "repository": "https://github.com/example/app",
            "commit": "a" * 40,
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
        project = self.api.router("project")

        def capture(request, work, **kwargs):
            work(self.connection, "00000000-0000-4000-8000-000000000083")
            return Response(202, {})

        with (
            mock.patch.object(self.api, "_external", side_effect=capture),
            mock.patch("openstack_platform.controller.api.DeploymentService") as service,
        ):
            for extras in (
                {},
                {"maintenance": True},
                {"maintenance": True, "plan": {"flavor": {"name": "worker-large"}}},
            ):
                project.dispatch(
                    "POST", f"/v1/applications/{application}/deployments", {}, {**body, **extras}
                )
                accepted = service.return_value.deploy.call_args.args[0]
                self.assertEqual(accepted.maintenance, extras.get("maintenance", False))
                self.assertEqual(accepted.sizing_plan, extras.get("plan"))
                self.assertFalse(accepted.reuse_worker)
            for extras in (
                {"maintenance": "true"},
                {"plan": None},
                {"plan": []},
                {"reuseWorker": True},
            ):
                with self.assertRaises(HttpError):
                    project.dispatch(
                        "POST",
                        f"/v1/applications/{application}/deployments",
                        {},
                        {**body, **extras},
                    )

    def test_database_create_replays_and_changed_input_conflicts(self) -> None:
        first = self.create_application()
        replay = self.create_application()
        self.assertEqual(first.status, 201)
        self.assertEqual(first.body, replay.body)
        self.assertEqual(
            first.body["applicationId"],
            "00000000-0000-4000-8000-000000000001",
        )
        self.assertFalse(first.body["enabled"])
        self.assertEqual(len(db.list_applications(self.connection)), 1)

        with self.assertRaises(HttpError) as raised:
            self.dispatch(
                "POST",
                "/v1/applications",
                {"slug": "other-app"},
                self.headers("00000000-0000-4000-8000-000000000001"),
            )
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_unknown_fields_and_noncanonical_keys_are_rejected(self) -> None:
        with self.assertRaises(HttpError) as raised:
            self.dispatch(
                "POST",
                "/v1/applications",
                {"slug": "demo-app", "providerId": "forbidden"},
                self.headers("00000000-0000-4000-8000-000000000002"),
            )
        self.assertEqual(raised.exception.code, "INVALID_BODY")
        with self.assertRaises(HttpError) as raised:
            self.dispatch(
                "POST",
                "/v1/applications",
                {"slug": "demo-app"},
                self.headers("00000000-0000-4000-8000-0000000000AA"),
            )
        self.assertEqual(raised.exception.code, "INVALID_IDEMPOTENCY_KEY")

    @mock.patch("openstack_platform.controller.application_service.openstack.verify_project")
    def test_lost_environment_response_replays_without_value_persistence(
        self, verify_project
    ) -> None:
        verify_project.return_value = None
        application_id = self.create_application().body["applicationId"]
        secret = "sentinel-value-never-persisted"
        request_key = "00000000-0000-4000-8000-000000000003"
        path = f"/v1/applications/{application_id}/environment/API_TOKEN"
        first = self.dispatch("PUT", path, {"value": secret}, self.headers(request_key))
        replay = self.dispatch("PUT", path, {"value": secret}, self.headers(request_key))
        self.assertEqual(first.status, 202)
        self.assertEqual(first.body, replay.body)
        self.api.wait_for_operations()
        self.assertEqual(len(self.helper_calls), 1)
        operation = db.get_operation(self.connection, request_key)
        self.assertIsNotNone(operation)
        self.assertEqual(operation.status, "succeeded")
        revision = db.get_environment_revision(self.connection, application_id)
        self.assertEqual(revision.revision, 1)
        rendered = "\n".join(self.connection.iterdump())
        self.assertNotIn(secret, rendered)
        for path in self.root.glob("platform.sqlite3*"):
            self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertNotIn(secret, str(first.body))
        self.assertNotIn(secret, repr(operation.refs))

        environment = self.dispatch("GET", f"/v1/applications/{application_id}/environment")
        self.assertEqual(environment.body["revision"], 1)
        self.assertEqual(environment.body["keys"], [{"name": "API_TOKEN", "owner": "staff"}])
        self.assertNotIn("value", str(environment.body).lower())

    @mock.patch("openstack_platform.controller.application_service.openstack.verify_project")
    def test_external_acceptance_is_prompt_reads_stay_responsive_and_scope_conflicts(
        self, verify_project
    ) -> None:
        verify_project.return_value = None
        application_id = self.create_application().body["applicationId"]
        entered = threading.Event()
        release = threading.Event()

        def blocking_helper(_config, action, values, *, deadline=None):
            if action != "app.env.set":
                raise AssertionError(f"unexpected helper action {action}")
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return {
                "keys": sorted(values["updates"]),
                "modifyIndex": 1,
                "restarted": False,
                "schedulerHealthy": False,
                "publicHealthy": False,
            }

        self.api.helper_caller = blocking_helper
        started = time.monotonic()
        first = self.dispatch(
            "PUT",
            f"/v1/applications/{application_id}/environment/API_TOKEN",
            {"value": "transient-secret"},
            self.headers("00000000-0000-4000-8000-000000000030"),
        )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(first.status, 202)
        self.assertTrue(entered.wait(timeout=2))

        started = time.monotonic()
        environment = self.dispatch("GET", f"/v1/applications/{application_id}/environment")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(environment.status, 200)
        replay = self.dispatch(
            "PUT",
            f"/v1/applications/{application_id}/environment/API_TOKEN",
            {"value": "transient-secret"},
            self.headers("00000000-0000-4000-8000-000000000030"),
        )
        self.assertEqual(replay.body, first.body)
        with self.assertRaises(HttpError) as raised:
            self.dispatch(
                "DELETE",
                f"/v1/applications/{application_id}/environment/OTHER",
                None,
                self.headers("00000000-0000-4000-8000-000000000031"),
            )
        self.assertEqual(raised.exception.code, "OPERATION_CONFLICT")
        release.set()
        self.api.wait_for_operations()

    @mock.patch("openstack_platform.controller.environment_service.openstack.verify_project")
    def test_restart_allows_identical_recovery_and_blocks_a_competing_key(
        self, verify_project
    ) -> None:
        verify_project.return_value = None
        application_id = self.create_application().body["applicationId"]
        operation_id = "00000000-0000-4000-8000-000000000040"
        request_path = f"/v1/applications/{application_id}/environment/API_TOKEN"
        request_body = {"value": "retry-only-secret"}
        db.claim_idempotency_request(
            self.connection,
            request_id=operation_id,
            request_fingerprint=db.request_fingerprint(
                {"method": "PUT", "path": request_path, "body": request_body}
            ),
        )
        db.enqueue_operation_dispatch(
            self.connection,
            operation_id=operation_id,
            kind="app.env.set",
            scope=f"app-{application_id}",
        )
        db.complete_idempotency_request(
            self.connection,
            request_id=operation_id,
            result_kind="operation",
            result_id=operation_id,
        )
        db.set_operation_dispatch_status(self.connection, operation_id, "running")
        db.begin_operation(
            self.connection,
            operation_id=operation_id,
            kind="app.env.set",
            scope=f"app-{application_id}",
            phase="intent_recorded",
            deadline_at=db.utc_now(),
            refs={"key_names": ["API_TOKEN"], "mutation": "set"},
        )

        queued_application = self.dispatch(
            "POST",
            "/v1/applications",
            {"slug": "queued-app"},
            self.headers("00000000-0000-4000-8000-000000000041"),
        ).body["applicationId"]
        queued_id = "00000000-0000-4000-8000-000000000042"
        db.claim_idempotency_request(
            self.connection,
            request_id=queued_id,
            request_fingerprint="b" * 64,
        )
        db.enqueue_operation_dispatch(
            self.connection,
            operation_id=queued_id,
            kind="app.env.set",
            scope=f"app-{queued_application}",
        )
        db.complete_idempotency_request(
            self.connection,
            request_id=queued_id,
            result_kind="operation",
            result_id=queued_id,
        )

        self.api.close()
        recovery_calls: list[str] = []

        def recovery_helper(_config, action, values, *, deadline=None):
            recovery_calls.append(action)
            if action == "app.env.list":
                return {"keys": []}
            if action == "app.env.set":
                return {
                    "keys": sorted(values["updates"]),
                    "modifyIndex": 2,
                    "restarted": False,
                    "schedulerHealthy": False,
                    "publicHealthy": False,
                }
            raise AssertionError(f"unexpected recovery helper action {action}")

        self.api = ControllerAPI(
            self.connection,
            self.config,
            self.root,
            helper_caller=recovery_helper,
        )
        self.router = self.api.router()

        response = self.dispatch("GET", f"/v1/operations/{operation_id}")
        self.assertEqual(response.body["status"], "recovery_required")
        self.assertEqual(response.body["phase"], "intent_recorded")
        operation = db.get_operation(self.connection, operation_id)
        self.assertIsNotNone(operation)
        self.assertEqual(operation.status, "recovery_required")  # type: ignore[union-attr]

        with self.assertRaises(HttpError) as changed:
            self.dispatch(
                "PUT",
                request_path,
                {"value": "different-secret"},
                self.headers(operation_id),
            )
        self.assertEqual(changed.exception.code, "IDEMPOTENCY_CONFLICT")

        with self.assertRaises(HttpError) as raised:
            self.dispatch(
                "DELETE",
                f"/v1/applications/{application_id}/environment/OTHER",
                None,
                self.headers("00000000-0000-4000-8000-000000000043"),
            )
        self.assertEqual(raised.exception.code, "OPERATION_CONFLICT")
        self.assertEqual(raised.exception.operation_id, operation_id)

        retried = self.dispatch("PUT", request_path, request_body, self.headers(operation_id))
        self.assertEqual(retried.status, 202)
        self.api.wait_for_operations()
        recovered = db.get_operation(self.connection, operation_id)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.status, "succeeded")  # type: ignore[union-attr]
        polled = self.dispatch("GET", f"/v1/operations/{operation_id}")
        self.assertEqual(polled.body["status"], "succeeded")
        self.assertEqual(recovery_calls, ["app.env.list", "app.env.set"])
        self.assertEqual(
            db.get_environment_revision(self.connection, application_id).revision,  # type: ignore[union-attr]
            1,
        )
        for database_file in self.root.glob("platform.sqlite3*"):
            self.assertNotIn(b"retry-only-secret", database_file.read_bytes())

        queued = self.dispatch("GET", f"/v1/operations/{queued_id}")
        self.assertEqual(queued.body["status"], "failed")
        self.assertEqual(queued.body["phase"], "startup_interrupted")
        self.assertEqual(queued.body["cleanupState"], "not_required")

    def test_slow_app_reads_do_not_wait_for_or_hold_the_api_lock(self) -> None:
        application = self.create_application().body["applicationId"]

        def helper(_config, action, values, *, deadline=None):
            if action == "app.logs":
                return {"text": "ready\n"}
            if action == "app.source.key":
                return {"present": False}
            if action == "app.source.check":
                return {"keyPresent": False}
            if action in {"app.source.commits", "app.source.preflight"}:
                return {"keyPresent": False}
            raise AssertionError(action)

        self.api.helper_caller = helper
        self.api.logs.helper_caller = helper
        resource = db.put_managed_resource(
            self.connection,
            application_id=application,
            resource_type="postgres",
            provider_name="app_demo",
            lifecycle_state="active",
        ).resource_id
        reads = [
            # The app status read: no deployment yet, so nothing to probe.
            ("GET", f"/v1/applications/{application}", None),
            # The app page's settings reads: database only, from a snapshot.
            ("GET", f"/v1/applications/{application}/environment", None),
            ("GET", f"/v1/applications/{application}/storage", None),
            ("GET", f"/v1/storage/{resource}", None),
            ("GET", f"/v1/applications/{application}/runtime-log", None),
            ("GET", f"/v1/applications/{application}/source-key", None),
            (
                "POST",
                f"/v1/applications/{application}/source-key/check",
                {"repository": "https://github.com/example/demo-app", "branch": "main"},
            ),
            (
                "POST",
                f"/v1/applications/{application}/source/commits",
                {"repository": "https://github.com/example/demo-app", "branch": "main"},
            ),
        ]
        # A locked request (a deploy admission, say) is in progress: these reads
        # still answer, from their own snapshot.
        pool = ThreadPoolExecutor(max_workers=len(reads))
        self.api._lock.acquire()
        try:
            futures = [
                pool.submit(self.dispatch, method, path, body) for method, path, body in reads
            ]
            statuses = [future.result(timeout=5).status for future in futures]
        finally:
            self.api._lock.release()
            pool.shutdown(wait=True)
        self.assertEqual(statuses, [200] * len(reads))
        with self.assertRaises(HttpError) as missing:
            self.dispatch("GET", f"/v1/applications/{uuid.uuid4()}/runtime-log")
        self.assertEqual(missing.exception.code, "APPLICATION_NOT_FOUND")
        for path, code in (
            (f"/v1/applications/{uuid.uuid4()}", "APPLICATION_NOT_FOUND"),
            (f"/v1/applications/{uuid.uuid4()}/environment", "APPLICATION_NOT_FOUND"),
            (f"/v1/applications/{uuid.uuid4()}/storage", "APPLICATION_NOT_FOUND"),
            (f"/v1/storage/{uuid.uuid4()}", "STORAGE_NOT_FOUND"),
        ):
            with self.assertRaises(HttpError) as missing:
                self.dispatch("GET", path)
            self.assertEqual(missing.exception.code, code)

    def test_operation_read_omits_refs_and_admin_pagination_is_bounded(self) -> None:
        self.create_application()
        for index, slug in enumerate(("alpha-app", "bravo-app"), 10):
            self.dispatch(
                "POST",
                "/v1/applications",
                {"slug": slug},
                self.headers(f"00000000-0000-4000-8000-{index:012d}"),
            )
        operation_id = "00000000-0000-4000-8000-000000000020"
        db.begin_operation(
            self.connection,
            operation_id=operation_id,
            kind="test.operation",
            scope="test-scope",
            phase="started",
            deadline_at=db.utc_now(),
            refs={"internal": "not-an-api-field"},
        )
        operation = self.dispatch("GET", f"/v1/operations/{operation_id}")
        self.assertNotIn("refs", operation.body)
        page = self.dispatch("GET", "/v1/admin/applications?limit=1")
        self.assertEqual(len(page.body["items"]), 1)
        self.assertTrue(page.body["truncated"])
        cursor = page.body["nextCursor"]
        second = self.dispatch("GET", f"/v1/admin/applications?limit=1&cursor={cursor}")
        self.assertNotEqual(
            page.body["items"][0]["applicationId"],
            second.body["items"][0]["applicationId"],
        )


if __name__ == "__main__":
    unittest.main()
