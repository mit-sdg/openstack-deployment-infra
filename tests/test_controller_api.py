from __future__ import annotations

import sqlite3
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
from openstack_platform.contracts import NOMAD_ROUTE_MARKER_KEY
from openstack_platform.controller import application_runtime as app_runtime
from openstack_platform.controller import database as db
from openstack_platform.controller.api import ControllerAPI, _SingleFlight
from openstack_platform.controller.http import HttpError, Response
from tests.product_fixtures import accept_deployment


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
            {"paths": {"root": "/srv/openstack-platform"}},
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

    def test_project_capability_delta_is_only_storage_delete(self) -> None:
        project, privileged, all_routes = (
            self.api.router("project"),
            self.api.router("privileged"),
            self.api.router("all"),
        )
        project_set = {(r.method, r.pattern.pattern) for r in project._routes}
        privileged_set = {(r.method, r.pattern.pattern) for r in privileged._routes}
        complete_set = {(r.method, r.pattern.pattern) for r in all_routes._routes}
        self.assertFalse(project_set & privileged_set)
        self.assertEqual(project_set | privileged_set, complete_set)
        for route in all_routes._routes:
            expected = (
                "/v1/admin/" in route.pattern.pattern
                or route.method == "POST"
                and route.pattern.pattern.endswith("/delete$")
            )
            self.assertEqual((route.method, route.pattern.pattern) in privileged_set, expected)
        identifier = "00000000-0000-4000-8000-000000000081"
        with self.assertRaises(HttpError) as error:
            project.dispatch(
                "DELETE",
                f"/v1/storage/{identifier}",
                self.headers(identifier),
                {"confirmation": "default"},
            )
        self.assertEqual(error.exception.code, "STORAGE_NOT_FOUND")
        with self.assertRaises(HttpError) as error:
            privileged.dispatch(
                "DELETE",
                f"/v1/storage/{identifier}",
                self.headers(identifier),
                {"confirmation": "default"},
            )
        self.assertEqual(error.exception.code, "NOT_FOUND")

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

    def test_admitted_retry_does_not_expose_previous_attempt_as_new_failure(self) -> None:
        application_id = self.create_application().body["applicationId"]
        identifier = "00000000-0000-4000-8000-000000000090"
        db.claim_idempotency_request(
            self.connection, request_id=identifier, request_fingerprint="a" * 64
        )
        db.enqueue_operation_dispatch(
            self.connection,
            operation_id=identifier,
            kind="app.deploy",
            scope=f"app-{application_id}",
        )
        db.begin_operation(
            self.connection,
            operation_id=identifier,
            kind="app.deploy",
            scope=f"app-{application_id}",
            phase="image_pushed",
            deadline_at=db.utc_now(),
            refs={},
        )
        db.mark_recovery_required(self.connection, identifier, "previous attempt failed")
        for dispatch_status in ("pending", "running"):
            if dispatch_status == "running":
                db.set_operation_dispatch_status(self.connection, identifier, dispatch_status)
            for prefix in ("/v1", "/v1/admin"):
                value = self.dispatch("GET", f"{prefix}/operations/{identifier}").body
                self.assertEqual(value["status"], "running")
                self.assertIsNone(value["safeError"])
            items = self.dispatch("GET", "/v1/admin/operations").body["items"]
            self.assertEqual(
                next(item for item in items if item["operationId"] == identifier)["status"],
                "running",
            )
        db.set_operation_dispatch_status(
            self.connection, identifier, "recovery_required", error="retry failed"
        )
        self.assertEqual(
            self.dispatch("GET", f"/v1/admin/operations/{identifier}").body["status"],
            "recovery_required",
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
    def test_cascade_delete_replays_after_application_is_tombstoned(self, verify_project) -> None:
        verify_project.return_value = None
        application_id = self.create_application().body["applicationId"]
        request_key = "00000000-0000-4000-8000-000000000004"
        path = f"/v1/applications/{application_id}/delete"
        first = self.dispatch(
            "POST",
            path,
            {"confirmation": "demo-app"},
            self.headers(request_key),
        )
        self.api.wait_for_operations()
        self.assertIsNone(db.get_application(self.connection, application_id))
        replay = self.dispatch(
            "POST",
            path,
            {"confirmation": "demo-app"},
            self.headers(request_key),
        )
        self.assertEqual(first.body, replay.body)
        self.assertEqual(db.get_operation(self.connection, request_key).status, "succeeded")
        self.assertIsNotNone(db.get_slug_tombstone(self.connection, "demo-app"))

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

    @mock.patch("openstack_platform.controller.application_service.openstack.verify_project")
    def test_shutdown_cannot_strand_an_operation_during_admission(self, verify_project) -> None:
        verify_project.return_value = None
        application_id = self.create_application().body["applicationId"]
        executor = self.api.executor
        original_slots = executor._slots  # noqa: SLF001 - deterministic shutdown race probe
        admission_entered = threading.Event()
        release_admission = threading.Event()

        class BlockingSlots:
            def acquire(self, *, blocking: bool) -> bool:
                admission_entered.set()
                self_waited = release_admission.wait(timeout=5)
                if not self_waited:
                    raise AssertionError("admission test timed out")
                return original_slots.acquire(blocking=blocking)

            def release(self) -> None:
                original_slots.release()

        executor._slots = BlockingSlots()  # type: ignore[assignment]  # noqa: SLF001
        shutdown_waited_for_admission: list[bool] = []
        race_finished = threading.Event()

        def race_shutdown() -> None:
            if not admission_entered.wait(timeout=2):
                release_admission.set()
                return
            close_finished = threading.Event()

            def close() -> None:
                self.api.close()
                close_finished.set()

            close_thread = threading.Thread(target=close)
            close_thread.start()
            shutdown_waited_for_admission.append(not close_finished.wait(timeout=0.2))
            release_admission.set()
            close_thread.join(timeout=5)
            race_finished.set()

        race_thread = threading.Thread(target=race_shutdown)
        race_thread.start()
        response = self.dispatch(
            "PUT",
            f"/v1/applications/{application_id}/environment/API_TOKEN",
            {"value": "shutdown-race-secret"},
            self.headers("00000000-0000-4000-8000-000000000039"),
        )
        race_thread.join(timeout=5)
        self.assertTrue(race_finished.is_set())
        self.assertEqual(shutdown_waited_for_admission, [True])
        self.assertEqual(response.status, 202)
        self.assertEqual(self.helper_calls[-1][0], "app.env.set")

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

    def test_slow_observation_does_not_block_operation_poll_or_health(self) -> None:
        identifier = "00000000-0000-4000-8000-000000000070"
        db.begin_operation(
            self.connection,
            operation_id=identifier,
            kind="app.deploy",
            scope="app-test-poll",
            phase="worker_ready",
            deadline_at="2099-01-01T00:00:00Z",
        )
        entered = threading.Event()
        release = threading.Event()

        def slow_observation(_request):
            entered.set()
            if not release.wait(10):
                raise AssertionError("test observation was not released")
            return Response(200, {})

        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(self.api._safe(slow_observation), None)
            try:
                self.assertTrue(entered.wait(2))
                poll = pool.submit(self.dispatch, "GET", f"/v1/operations/{identifier}")
                response = poll.result(timeout=2)
                self.assertEqual(response.body["phase"], "worker_ready")
                self.assertEqual(self.dispatch("GET", "/v1/health").status, 200)
            finally:
                release.set()
            slow.result(timeout=2)

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

    def test_app_settings_reads_use_a_private_query_only_snapshot(self) -> None:
        application = self.create_application().body["applicationId"]
        resource = db.put_managed_resource(
            self.connection,
            application_id=application,
            resource_type="s3",
            provider_name="app_demo",
            lifecycle_state="active",
        ).resource_id
        snapshots = []

        def private(read):
            def observe(connection, *args, **kwargs):
                self.assertIsNot(connection, self.connection)
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertTrue(connection.in_transaction)
                snapshots.append(read.__name__)
                return read(connection, *args, **kwargs)

            return mock.patch.object(db, read.__name__, side_effect=observe)

        with (
            private(db.get_environment_revision),
            private(db.list_environment_keys),
            private(db.list_managed_resources),
            private(db.get_managed_resource),
        ):
            environment = self.dispatch("GET", f"/v1/applications/{application}/environment")
            listed = self.dispatch("GET", f"/v1/applications/{application}/storage")
            single = self.dispatch("GET", f"/v1/storage/{resource}")
        self.assertEqual(environment.body["keys"], [])
        self.assertEqual([item["resourceId"] for item in listed.body["items"]], [resource])
        self.assertEqual(single.body["type"], "s3")
        self.assertEqual(
            snapshots,
            [
                "get_environment_revision",
                "list_environment_keys",
                "list_managed_resources",
                "get_managed_resource",
            ],
        )

    def test_app_status_probe_holds_no_lock_or_snapshot_and_is_shared(self) -> None:
        application = self.create_application().body["applicationId"]
        job = f'job "demo-app" {{\n    {NOMAD_ROUTE_MARKER_KEY} = "{uuid.uuid4()}"\n}}\n'
        accept_deployment(
            self.connection,
            application_id=application,
            source_commit="a" * 40,
            recipe_hash="b" * 64,
            image_digest="registry.example/app@sha256:" + "c" * 64,
            nomad_job=job,
            nomad_version=4,
            build_log_path="logs/build.log",
        )
        snapshots: list[sqlite3.Connection] = []
        connect = db.connect

        def opened(*args, **kwargs):
            connection = connect(*args, **kwargs)
            snapshots.append(connection)
            return connection

        entered = threading.Event()
        release = threading.Event()
        probes = []

        def observer(action, values, **_bounds):
            still_open = 0
            for connection in snapshots:
                try:
                    connection.execute("SELECT 1")
                    still_open += 1
                except sqlite3.ProgrammingError:
                    pass
            probes.append((action, values["version"], len(snapshots), still_open))
            entered.set()
            if not release.wait(10):
                raise AssertionError("test probe was not released")
            return {"healthy": True, "terminal": False}

        self.api.observer_helper = observer
        # A long reuse window, so a loaded machine cannot turn reuse into a
        # second probe; the window itself is tested with a fake clock.
        self.api._live = _SingleFlight(60.0)
        path = f"/v1/applications/{application}"
        pool = ThreadPoolExecutor(max_workers=2)
        # A locked request (a deploy admission, say) is in progress.
        self.api._lock.acquire()
        locked = True
        try:
            with (
                mock.patch.object(db, "connect", side_effect=opened),
                mock.patch.object(app_runtime, "check_public_health", return_value=True),
            ):
                first = pool.submit(self.dispatch, "GET", path)
                self.assertTrue(entered.wait(5))
                # A burst of page polls waits for the running probe.
                second = pool.submit(self.dispatch, "GET", path)
                self.api._lock.release()
                locked = False
                # Locked work does not queue behind the slow probe. It uses the
                # writer, which belongs to this thread; a regression holding the
                # lock would delay it until the probe's own wait expires.
                created = self.dispatch(
                    "POST",
                    "/v1/applications",
                    {"slug": "other-app"},
                    self.headers("00000000-0000-4000-8000-000000000002"),
                )
                self.assertEqual(created.status, 201)
                self.assertFalse(first.done())
                release.set()
                responses = [first.result(timeout=5), second.result(timeout=5)]
                # Straight after, the same accepted state reuses that probe.
                responses.append(self.dispatch("GET", path))
        finally:
            if locked:
                self.api._lock.release()
            release.set()
            pool.shutdown(wait=True)
        self.assertEqual(probes, [("app.health", 4, 1, 0)])
        for response in responses:
            live = response.body["live"]
            self.assertEqual(
                (live["schedulerState"], live["allocationHealthy"], live["routeHealthy"]),
                ("running", True, True),
            )
            self.assertFalse(response.body["requiresMaintenance"])
        # Disabling changes the accepted state, so the next read does not reuse
        # the running observation.
        db.set_application_runtime(self.connection, application, running=False)
        stopped = self.dispatch("GET", path).body["live"]
        self.assertEqual(stopped["schedulerState"], "stopped")
        self.assertEqual(len(probes), 1)

    def test_single_flight_shares_a_running_probe_and_reuses_it_briefly(self) -> None:
        now = [100.0]
        reads = []
        looked_up = threading.Event()

        def clock():
            reads.append(now[0])
            if len(reads) == 2:
                looked_up.set()
            return now[0]

        flights = _SingleFlight(2.0, clock=clock)
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def probe(value, *, wait=False):
            def run():
                calls.append(value)
                if wait:
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("test probe was not released")
                return {"value": value}

            return run

        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                leader = pool.submit(flights.run, "app", probe(1, wait=True), wait_seconds=5)
                self.assertTrue(entered.wait(5))
                joiner = pool.submit(flights.run, "app", probe(2), wait_seconds=5)
                self.assertTrue(looked_up.wait(5))
                with self.assertRaises(TimeoutError):
                    flights.run("app", probe(3), wait_seconds=0.05)
            finally:
                release.set()
            self.assertEqual(leader.result(timeout=5), {"value": 1})
            self.assertEqual(joiner.result(timeout=5), {"value": 1})
        reused = flights.run("app", probe(4), wait_seconds=0)
        reused["value"] = "changed by a caller"
        now[0] = 101.9
        self.assertEqual(flights.run("app", probe(5), wait_seconds=0), {"value": 1})
        self.assertEqual(flights.run("other", probe(6), wait_seconds=0), {"value": 6})
        now[0] = 102.0
        self.assertEqual(flights.run("app", probe(7), wait_seconds=0), {"value": 7})

        def failing():
            calls.append("failed")
            raise RuntimeError("probe failed")

        now[0] = 200.0
        with self.assertRaises(RuntimeError):
            flights.run("app", failing, wait_seconds=0)
        self.assertEqual(flights.run("app", probe(8), wait_seconds=0), {"value": 8})
        self.assertEqual(calls, [1, 6, 7, "failed", 8])

    def test_deploy_key_removal_asks_the_helper_and_reports_no_key(self) -> None:
        application = self.create_application().body["applicationId"]
        calls = []

        def helper(_config, action, values, *, deadline=None):
            # Removal changes state, so it runs under the API lock like creation.
            self.assertTrue(self.api._lock.locked())
            calls.append((action, values))
            return {"slug": values["slug"], "present": False}

        self.api.helper_caller = helper
        path = f"/v1/applications/{application}/source-key"
        for _attempt in range(2):
            response = self.dispatch("DELETE", path)
            self.assertEqual(
                (response.status, response.body),
                (200, {"applicationId": application, "present": False}),
            )
        self.assertEqual(calls, [("app.source.key", {"slug": "demo-app", "mode": "delete"})] * 2)
        with self.assertRaises(HttpError) as invalid:
            self.dispatch("DELETE", path, {"replace": True})
        self.assertEqual(invalid.exception.code, "INVALID_BODY")
        with self.assertRaises(HttpError) as missing:
            self.dispatch("DELETE", f"/v1/applications/{uuid.uuid4()}/source-key")
        self.assertEqual(missing.exception.code, "APPLICATION_NOT_FOUND")
        self.assertEqual(len(calls), 2)

    def test_both_operation_routes_use_an_independent_query_only_snapshot(self) -> None:
        identifier = "00000000-0000-4000-8000-000000000071"
        db.begin_operation(
            self.connection,
            operation_id=identifier,
            kind="app.deploy",
            scope="app-test-snapshot",
            phase="worker_ready",
            deadline_at="2099-01-01T00:00:00Z",
        )
        original = db.get_operation

        def observe(connection, operation_id):
            self.assertIsNot(connection, self.connection)
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertTrue(connection.in_transaction)
            return original(connection, operation_id)

        with mock.patch.object(db, "get_operation", side_effect=observe):
            for path in (f"/v1/operations/{identifier}", f"/v1/admin/operations/{identifier}"):
                self.assertEqual(self.dispatch("GET", path).status, 200)

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
