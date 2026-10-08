from __future__ import annotations

import copy
import time
import unittest
from unittest import mock

from openstack_platform import remote
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
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
            self.connection,
            self.f.config,
            self.f.root,
            helper_caller=self.helper,
            retry_poll_seconds=0.01,
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
        self.restart()
        self.assertEqual(db.get_operation(self.connection, key).status, "recovery_required")
        self.failures.clear()
        self.f.post(f"/v1/applications/{self.f.app_id}/deployments", self.f.body, key)
        self.assertEqual(db.get_operation(self.connection, key).status, "succeeded")

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
