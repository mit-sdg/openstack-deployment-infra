from __future__ import annotations

import copy
import threading
import time
import unittest
import uuid
from unittest import mock

from openstack_platform import remote
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import finishing_retries
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.http import HttpError
from openstack_platform.controller.service_support import logged_helper
from tests import test_application_sizing as sizing_fixture


class ControllerFinishingTests(unittest.TestCase):
    def setUp(self):
        self.f = sizing_fixture.ApplicationSizingTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.connection = self.f.connection
        self.failures = {}
        self.environment = {}
        self.f.api.close()
        self.restart()
        _, operation = self.f.deploy()
        self.assertEqual(operation.status, "succeeded", operation.safe_error)

    def helper(self, config, action, values, **bounds):
        if self.failures.get(action, 0):
            self.failures[action] -= 1
            raise remote.HelperError(
                "CLEANUP_UNCONFIRMED", "secret-env-value password=credential-test " + "x" * 4000
            )
        if action == "app.env.set":
            self.environment.update(values["updates"])
            return {
                "keys": sorted(self.environment),
                "modifyIndex": 1,
                "restarted": True,
                "schedulerHealthy": True,
                "publicHealthy": True,
            }
        if action == "app.env.remove":
            for key in values["keys"]:
                self.environment.pop(key, None)
            return {
                "keys": sorted(self.environment),
                "modifyIndex": 1,
                "restarted": True,
                "schedulerHealthy": True,
                "publicHealthy": True,
            }
        if action == "app.restart":
            return {"restarted": True}
        return self.f.helper(config, action, values, **bounds)

    def restart(self):
        self.f.api.close()
        self.f.api = ControllerAPI(
            self.connection, self.f.config, self.f.root, helper_caller=self.helper
        )
        self.f.fixture.api = self.f.api
        self.f.router = self.f.api.router()

    def pending(self, action="app.remove", failures=1):
        self.failures[action] = failures
        key, operation = self.f.deploy()
        self.assertEqual(operation.status, "running", operation.safe_error)
        self.assertTrue(operation.finishing)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.f.app_id).deployment_id, key
        )
        self.assertEqual(db.get_deployment_attempt(self.connection, key).status, "succeeded")
        self.assertIsNotNone(operation.refs["finishing_retry"]["next_at"])
        return key

    def make_due(self, key):
        operation = db.get_operation(self.connection, key)
        retry = {**operation.refs["finishing_retry"], "next_at": "2000-01-01T00:00:00Z"}
        db.checkpoint_operation(
            self.connection,
            key,
            phase=operation.phase,
            refs={"finishing_retry": retry},
            merge_refs=True,
        )

    def until(self, predicate):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("background retry did not reach expected state")

    def test_automatic_cleanup_recovers_without_rebuilding_or_reaccepting(self):
        key = self.pending()
        jobs = copy.deepcopy(self.f.jobs)
        calls = len(self.f.calls)
        with mock.patch.object(
            app, "accept_healthy_deployment", side_effect=AssertionError("must not reaccept")
        ):
            self.make_due(key)
            self.until(lambda: db.get_operation(self.connection, key).status == "succeeded")
        operation = db.get_operation(self.connection, key)
        self.assertEqual(operation.refs["finishing_retry"]["attempts"], 1)
        self.assertIsNone(operation.refs["finishing_retry"]["next_at"])
        self.assertEqual(len(self.f.workers), 1)
        self.assertEqual(
            self.f.jobs,
            {name: job for name, job in jobs.items() if name == operation.refs["accepted_job_id"]},
        )
        self.assertFalse(
            {"app.build", "app.worker.create", "app.deploy", "app.promote"}
            & {a for a, _ in self.f.calls[calls:]}
        )

    def test_retry_schedule_survives_restart_and_attempts_exhaust(self):
        key = self.pending("app.manifest.retain", failures=10)
        before = db.get_operation(self.connection, key).refs["finishing_retry"]
        self.restart()
        self.assertEqual(db.get_operation(self.connection, key).refs["finishing_retry"], before)
        for attempt in range(1, 6):
            self.make_due(key)
            self.until(
                lambda attempt=attempt: (
                    db.get_operation(self.connection, key).refs["finishing_retry"]["attempts"]
                    == attempt
                    and db.get_operation_dispatch(self.connection, key).status
                    == "recovery_required"
                )
            )
            current = db.get_operation(self.connection, key)
            self.assertEqual(current.status, "recovery_required" if attempt == 5 else "running")
        self.assertIsNone(current.refs["finishing_retry"]["next_at"])
        self.assertEqual(sum(finishing_retries.RETRY_DELAYS), 1500)
        self.restart()
        self.assertEqual(db.get_operation(self.connection, key).status, "recovery_required")
        self.failures.clear()
        self.f.post(f"/v1/applications/{self.f.app_id}/deployments", self.f.body, key)
        self.assertEqual(db.get_operation(self.connection, key).status, "succeeded")

    def test_interrupted_retry_consumes_attempt_and_reschedules_on_restart(self):
        key = self.pending("app.manifest.retain")
        self.f.api.close()
        self.make_due(key)
        operation = db.get_operation(self.connection, key)
        db.requeue_recovery_dispatch(
            self.connection,
            operation_id=key,
            kind=operation.kind,
            scope=operation.scope,
            automatic=True,
        )
        db.set_operation_dispatch_status(self.connection, key, "running")
        self.restart()
        operation = db.get_operation(self.connection, key)
        self.assertEqual(operation.status, "running")
        self.assertEqual(operation.refs["finishing_retry"]["attempts"], 1)
        self.assertIsNotNone(operation.refs["finishing_retry"]["next_at"])
        self.make_due(key)
        self.until(lambda: db.get_operation(self.connection, key).status == "succeeded")

    def test_environment_set_delete_import_and_restart_are_admitted(self):
        key = self.pending()
        base = f"/v1/applications/{self.f.app_id}"
        for method, path, body in (
            ("PUT", base + "/environment/MESSAGE", {"value": "secret-env-value"}),
            ("DELETE", base + "/environment/MESSAGE", None),
            ("POST", base + "/environment/import", {"dotenv": "OTHER=secret-env-value\n"}),
            ("POST", base + "/restart", {}),
            ("POST", base + "/enable", {}),
        ):
            response = self.f.router.dispatch(
                method, path, {"Idempotency-Key": str(uuid.uuid4())}, body
            )
            self.f.api.wait_for_operations()
            self.assertEqual(
                db.get_operation(self.connection, response.body["operationId"]).status, "succeeded"
            )
        self.failures.clear()
        self.f.post(base + "/deployments", self.f.body, key)
        self.assertEqual(self.environment["OTHER"], "secret-env-value")
        self.assertNotIn("MESSAGE", self.environment)
        self.assertEqual(len(self.f.workers), 1)

    def test_disable_after_job_absence_stays_disabled_after_finishing(self):
        key = self.pending("app.worker.delete")
        self.assertTrue(db.get_operation(self.connection, key).refs["predecessor_job_absent"])
        base = f"/v1/applications/{self.f.app_id}"
        _, operation = self.f.post(base + "/disable", {})
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        with self.assertRaises(HttpError) as raised:
            self.f.post(base + "/enable", {})
        self.assertEqual(raised.exception.code, "POST_ACCEPTANCE_CONFLICT")
        self.assertEqual(self.f.jobs, {})
        self.make_due(key)
        self.until(lambda: db.get_operation(self.connection, key).status == "succeeded")
        self.assertFalse(db.get_application(self.connection, self.f.app_id).desired_running)
        self.assertEqual(self.f.workers, {})
        self.assertEqual(self.f.jobs, {})

    def test_disable_is_blocked_while_predecessor_route_is_still_present(self):
        key = self.pending()
        jobs = copy.deepcopy(self.f.jobs)
        with self.assertRaises(HttpError) as raised:
            self.f.post(f"/v1/applications/{self.f.app_id}/disable", {})
        self.assertEqual(raised.exception.code, "POST_ACCEPTANCE_CONFLICT")
        self.assertEqual(raised.exception.operation_id, key)
        self.assertEqual(self.f.jobs, jobs)
        self.assertTrue(db.get_application(self.connection, self.f.app_id).desired_running)

    def test_conflicting_operations_stay_blocked_after_exhaustion(self):
        key = self.pending()
        db.mark_recovery_required(self.connection, key, "retry budget exhausted")
        for kind in (
            "app.deploy",
            "app.delete",
            "storage.create",
            "storage.remove",
            "app.public_ip.allocate",
            "app.fixed_ip.allocate",
        ):
            with self.subTest(kind=kind), self.assertRaises(db.FinishingOperationConflictError):
                db.begin_operation(
                    self.connection,
                    operation_id=str(uuid.uuid4()),
                    kind=kind,
                    scope=f"app-{self.f.app_id}",
                    phase="validated",
                    deadline_at="2030-01-01T00:00:00Z",
                )
        with self.assertRaises(HttpError) as raised:
            self.f.deploy()
        self.assertEqual(raised.exception.code, "POST_ACCEPTANCE_CONFLICT")
        self.assertEqual(raised.exception.operation_id, key)
        for kind in ("storage.verify", "storage.rotate"):
            identifier = str(uuid.uuid4())
            db.claim_idempotency_request(
                self.connection, request_id=identifier, request_fingerprint="a" * 64
            )
            db.enqueue_operation_dispatch(
                self.connection, operation_id=identifier, kind=kind, scope=f"app-{self.f.app_id}"
            )
            db.begin_operation(
                self.connection,
                operation_id=identifier,
                kind=kind,
                scope=f"app-{self.f.app_id}",
                phase="validated",
                deadline_at="2030-01-01T00:00:00Z",
            )
            db.mark_succeeded(self.connection, identifier)
            db.set_operation_dispatch_status(self.connection, identifier, "finished")

    def test_automatic_retry_and_manual_replay_do_not_overlap_or_lose_foreground_work(self):
        key = self.pending("app.manifest.retain")
        entered, release = threading.Event(), threading.Event()
        helper = self.f.api.helper_caller
        invocations = []

        def paused(config, action, values, **bounds):
            if action == "app.manifest.retain":
                invocations.append(action)
                entered.set()
                self.assertTrue(release.wait(20))
            return helper(config, action, values, **bounds)

        self.f.api.helper_caller = paused
        self.addCleanup(release.set)
        self.make_due(key)
        self.assertTrue(entered.wait(20))
        base = f"/v1/applications/{self.f.app_id}"
        replay = self.f.router.dispatch(
            "POST", base + "/deployments", {"Idempotency-Key": key}, self.f.body
        )
        self.assertEqual(replay.body["operationId"], key)
        env = self.f.router.dispatch(
            "PUT",
            base + "/environment/MESSAGE",
            {"Idempotency-Key": str(uuid.uuid4())},
            {"value": "kept"},
        )
        self.assertEqual(env.status, 202)
        self.assertNotIn("MESSAGE", self.environment)
        release.set()
        self.f.api.wait_for_operations()
        self.assertEqual(invocations, ["app.manifest.retain"])
        self.assertEqual(self.environment["MESSAGE"], "kept")
        self.assertEqual(
            db.get_operation(self.connection, env.body["operationId"]).status, "succeeded"
        )

    def test_helper_failure_is_bounded_and_secret_free_in_log_and_record(self):
        with self.assertLogs(
            "openstack_platform.controller.service_support", level="WARNING"
        ) as captured:
            key = self.pending()
        message = "\n".join(captured.output)
        self.assertIn("action=app.remove class=HelperError code=CLEANUP_UNCONFIRMED", message)
        self.assertLess(len(message), 300)
        operation = self.f.router.dispatch("GET", f"/v1/operations/{key}", {}, None).body
        self.assertEqual(operation["safeError"], "HelperError: details redacted")
        self.assertEqual(operation["finishingRetryAttempts"], 0)
        self.assertIsNotNone(operation["nextRetryAt"])
        self.assertTrue(operation["finishing"])
        for secret in ("secret-env-value", "credential-test", "password=", "x" * 20):
            self.assertNotIn(secret, message)
            self.assertNotIn(secret, str(operation))


class HelperFailureLoggingTests(unittest.TestCase):
    def test_environment_request_and_arbitrary_error_output_are_never_logged(self):
        secret = "ENV_VALUE_SECRET credentials-token-test"
        for failure in (
            remote.HelperError("NOMAD_UNAVAILABLE", secret),
            remote.HelperError("CREDENTIALS_TOKEN_TEST", secret),
            remote.ProtocolError(secret),
            TimeoutError(secret),
            OSError(secret),
        ):
            with self.subTest(failure=type(failure).__name__):

                def failing(*_args, failure=failure, **_kwargs):
                    raise failure

                with self.assertLogs(
                    "openstack_platform.controller.service_support", level="WARNING"
                ) as captured:
                    with self.assertRaises(type(failure)):
                        logged_helper(failing, config_shaped=False)(
                            "app.env.set", {"updates": {"TOKEN": secret}, "body": secret}
                        )
                output = "\n".join(captured.output)
                self.assertIn("action=app.env.set", output)
                self.assertNotIn(secret, output)
                self.assertNotIn("CREDENTIALS_TOKEN_TEST", output)
                self.assertLess(len(output), 300)
