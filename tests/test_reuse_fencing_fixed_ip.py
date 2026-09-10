from __future__ import annotations

import unittest
import uuid

from openstack_platform.controller import database as db
from openstack_platform.controller.api import ControllerAPI
from tests import test_worker_reuse_fixed_ip as fixtures


class RetainedReuseFencingTests(unittest.TestCase):
    """Real worker helper -> lifecycle shell -> offline OSC, including lost reply."""

    def setUp(self):
        self.base = fixtures.FixedPortWorkerReuseTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.connection = self.base.connection
        self.app_id = self.base.app_id
        self.interrupt = False
        self.fixture.api.close()
        self.fixture.api = ControllerAPI(
            self.connection, self.fixture.config, self.fixture.root, helper_caller=self.helper
        )
        self.fixture.fixture.api = self.fixture.api
        self.fixture.router = self.fixture.api.router()

    def helper(self, config, action, args, **kwargs):
        if action == "app.quiesce" and self.interrupt:
            raise RuntimeError("Nomad exit records unavailable")
        result = self.base.helper(config, action, args, **kwargs)
        if action == "app.builder.delete":
            result = {**result, "buildId": args["buildId"]}
        return result

    def test_lost_physical_delete_reply_retains_primary_and_enable_restores_old_artifact(self):
        before = self.base.prepare()
        accepted = db.get_deployment(self.connection, self.app_id)
        self.interrupt = True
        original, failed = self.base.update()
        self.assertEqual(failed.status, "recovery_required", failed.safe_error)
        self.fixture.jobs.clear()  # Unknown exit; no inference from scheduler absence.
        cloud = self.base.base
        cloud.change(lambda state: state.update(fault="server.delete.after"))
        key = str(uuid.uuid4())
        path = f"/v1/applications/{self.app_id}/disable"
        body = {"interruptedDeploymentId": original}
        _, lost = self.fixture.post(path, body, key)
        self.assertEqual(lost.status, "recovery_required", lost.safe_error)
        self.assertEqual(cloud.state()["servers"], {})
        self.assertEqual(cloud.port()["id"], before.worker_port_id)
        self.assertEqual(cloud.port()["device_id"], "")
        self.assertEqual(db.get_operation(self.connection, original).status, "running")
        calls = len(cloud.state()["calls"])
        _, retried = self.fixture.post(path, body, key)
        self.assertEqual(retried.status, "succeeded", retried.safe_error)
        self.assertFalse(
            any(call[:2] == ["server", "delete"] for call in cloud.state()["calls"][calls:])
        )
        self.assertFalse(
            any(
                call[:2] == ["port", "delete"] and call[-1] == before.worker_port_id
                for call in cloud.state()["calls"]
            )
        )
        self.assertEqual(db.get_operation_dispatch(self.connection, original).status, "finished")
        self.assertEqual(db.get_deployment(self.connection, self.app_id), accepted)
        self.assertIsNone(db.get_application(self.connection, self.app_id).worker_server_id)
        self.base.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/enable", {}))
        current = db.get_application(self.connection, self.app_id)
        self.assertTrue(current.desired_running)
        self.assertNotEqual(current.worker_server_id, before.worker_server_id)
        self.assertEqual(current.worker_port_id, before.worker_port_id)
        self.assertEqual(cloud.port()["device_id"], current.worker_server_id)
        self.assertEqual(
            db.get_deployment(self.connection, self.app_id).image_digest, accepted.image_digest
        )
        mutations = len(cloud.state()["calls"])
        self.base.assert_success(self.fixture.post(path, body, key))
        self.assertEqual(len(cloud.state()["calls"]), mutations)


if __name__ == "__main__":
    unittest.main()
