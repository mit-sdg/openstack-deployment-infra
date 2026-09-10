from __future__ import annotations

import copy
import unittest

from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import fixed_ip_service
from openstack_platform.controller.api import ControllerAPI
from tests import test_fixed_ip as fixed_fixtures


class FixedPortWorkerReuseTests(unittest.TestCase):
    """Exercise reuse against the real worker helper and retained-port adapter."""

    def setUp(self):
        self.base = fixed_fixtures.RetainedFixedIPTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.fixture.api.close()
        self.fixture.api = ControllerAPI(
            self.base.connection, self.base.config, self.base.root, helper_caller=self.helper
        )
        self.fixture.fixture.api = self.fixture.api
        self.fixture.router = self.fixture.api.router()
        self.connection = self.base.connection
        self.app_id = self.base.app_id
        self.path = f"/v1/admin/applications/{self.app_id}/deployments"

    def helper(self, config, action, args, **kwargs):
        if action == "app.quiesce":
            self.fixture.calls.append((action, copy.deepcopy(args)))
            job = self.fixture.jobs[args["jobId"]]
            self.assertEqual(
                app.nomad_candidate_identity(job),
                (args["candidateJobSha256"], args["candidateImage"]),
            )
            state = self.base.state()
            self.assertEqual(len(state["servers"]), 1)
            self.assertTrue(
                any(port.get("device_id") in state["servers"] for port in state["ports"].values())
            )
            return {**args, "jobStopped": True, "allocationsStopped": True}
        return self.base.helper(config, action, args, **kwargs)

    def assert_success(self, result):
        self.base.assert_success(result)
        return result[0]

    def prepare(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        self.assert_success(self.base.reserve())
        self.assert_success(
            self.fixture.post(
                self.path,
                {**self.fixture.body, "plan": self.fixture.plan(), "maintenance": True},
            )
        )
        self.fixture.calls.clear()
        return db.get_application(self.connection, self.app_id)

    def update(self):
        return self.fixture.post(
            self.path,
            {
                **self.fixture.body,
                "maintenance": True,
                "workerStrategy": "reuse",
            },
        )

    def test_repeated_reuse_keeps_attached_primary_port_and_server_without_provider_mutations(self):
        before = self.prepare()
        record = copy.deepcopy(fixed_ip_service.get(self.connection, self.app_id))
        call_start = len(self.base.state()["calls"])
        for _ in range(2):
            self.assert_success(self.update())
            current = db.get_application(self.connection, self.app_id)
            self.assertEqual(current.worker_server_id, before.worker_server_id)
            self.assertEqual(current.worker_port_id, before.worker_port_id)
            self.assertEqual(self.base.port()["device_id"], before.worker_server_id)
            self.assertEqual(fixed_ip_service.get(self.connection, self.app_id), record)
        calls = self.base.state()["calls"][call_start:]
        self.assertTrue(calls)  # Real ownership/readiness checks still happened.
        self.assertFalse(
            any(
                c[:2]
                in (
                    ["server", "create"],
                    ["server", "delete"],
                    ["port", "create"],
                    ["port", "delete"],
                    ["port", "set"],
                )
                for c in calls
            )
        )
        self.assertFalse(
            any(
                action in {"app.worker.create", "app.worker.delete", "app.promote"}
                for action, _ in self.fixture.calls
            )
        )

    def test_newly_reserved_different_port_cannot_be_adopted_by_reuse(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        self.assert_success(self.base.reserve())
        self.fixture.calls.clear()
        _, failed = self.update()
        self.assertEqual(failed.status, "failed", failed.safe_error)
        self.assertFalse(
            any(
                action in {"app.build", "app.quiesce", "app.worker.create", "app.worker.delete"}
                for action, _ in self.fixture.calls
            )
        )
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)

    def test_failed_update_can_restore_accepted_code_on_same_fixed_port(self):
        before = self.prepare()
        accepted = db.get_active_deployment(self.connection, self.app_id).deployment_id
        self.fixture.fail_health = True
        _, failed = self.update()
        self.assertEqual(failed.status, "failed", failed.safe_error)
        self.assertEqual(self.base.port()["device_id"], before.worker_server_id)
        self.assertEqual(len(self.base.state()["servers"]), 1)
        self.fixture.fail_health = False
        self.fixture.calls.clear()
        self.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/enable", {}))
        current = db.get_application(self.connection, self.app_id)
        self.assertTrue(current.desired_running)
        self.assertEqual(current.worker_server_id, before.worker_server_id)
        self.assertEqual(current.worker_port_id, before.worker_port_id)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, accepted
        )
        self.assertNotIn("app.worker.create", [action for action, _ in self.fixture.calls])
