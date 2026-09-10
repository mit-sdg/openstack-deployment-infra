from __future__ import annotations

import copy
import unittest
from unittest import mock

from openstack_platform import openstack
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import public_ip_service as service
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.validation import ValidationError
from tests import test_public_ip as fixtures


class RetainedWorkerPublicIPTests(unittest.TestCase):
    """Real FIP ownership validation against the offline cloud fixture.

    Construct maintenance's durable stopped-but-retained runtime directly; the
    process-exit protocol is independent of floating-IP reconciliation.
    """

    def setUp(self):
        self.base = fixtures.PublicIPTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.connection = self.base.connection
        self.app_id = self.base.app_id
        self.cloud = self.base.cloud
        self.path = f"/v1/admin/applications/{self.app_id}/public-ip"

    def stop_retaining_worker(self):
        current = db.get_application(self.connection, self.app_id)
        deployment = db.get_deployment(self.connection, self.app_id)
        self.fixture.jobs.pop(app.nomad_job_id(deployment.nomad_job, current.slug))
        db.set_application_runtime(
            self.connection,
            self.app_id,
            running=False,
            worker_server_id=current.worker_server_id,
            worker_server_name=current.worker_server_name,
            worker_port_id=current.worker_port_id,
            worker_port_name=current.worker_port_name,
        )
        return db.get_application(self.connection, self.app_id)

    def prepare(self, *, supplied=False, candidate_slot=False):
        self.base.deploy()
        if candidate_slot:
            self.base.deploy()
        if supplied:
            self.cloud.supplied()
        self.base.mutate("attach" if supplied else "allocate")
        current = self.stop_retaining_worker()
        self.cloud.calls.clear()
        self.cloud.mutations.clear()
        self.fixture.calls.clear()
        return current

    def reconcile(self, key=None):
        return self.fixture.post(self.path, {"action": "reconcile"}, key)

    def assert_success(self, result):
        key, operation = result
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertIsNone(db.get_unfinished_operation(self.connection, f"app-{self.app_id}"))
        return key

    def test_reconcile_preserves_attached_stopped_runtime_and_does_not_block_enable(self):
        current = self.prepare(candidate_slot=True)
        accepted = db.get_active_deployment(self.connection, self.app_id)
        reservation = copy.deepcopy(service.get(self.connection, self.app_id))
        address = copy.deepcopy(self.cloud.fips[fixtures.FIP])
        key = self.assert_success(self.reconcile())
        self.assertEqual(db.get_application(self.connection, self.app_id), current)
        self.assertEqual(db.get_active_deployment(self.connection, self.app_id), accepted)
        self.assertEqual(service.get(self.connection, self.app_id), reservation)
        self.assertEqual(self.cloud.fips[fixtures.FIP], address)
        self.assertTrue(self.cloud.calls)  # Ownership was reobserved, not bypassed.
        self.assertEqual(self.cloud.mutations, [])
        self.assertEqual(self.fixture.calls, [])
        calls = len(self.cloud.calls)
        self.assert_success(self.reconcile(key))
        self.assertEqual(len(self.cloud.calls), calls)
        self.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/enable", {}))
        enabled = db.get_application(self.connection, self.app_id)
        self.assertTrue(enabled.desired_running)
        self.assertEqual(enabled.worker_server_id, current.worker_server_id)
        self.assertEqual(enabled.worker_port_id, current.worker_port_id)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id,
            accepted.deployment_id,
        )
        self.assertEqual(self.cloud.fips[fixtures.FIP], address)
        self.assertEqual(self.cloud.mutations, [])
        self.assertFalse(
            {"app.worker.create", "app.worker.delete", "app.build"}
            & {action for action, _ in self.fixture.calls}
        )

    def test_reconcile_supplied_ip_then_disable_detaches_without_deleting_address(self):
        self.prepare(supplied=True)
        self.assert_success(self.reconcile())
        self.assertEqual(self.cloud.mutations, [])
        self.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/disable", {}))
        self.assertEqual(self.fixture.workers, {})
        self.assertIsNone(db.get_application(self.connection, self.app_id).worker_server_id)
        self.assertIsNone(self.cloud.fips[fixtures.FIP]["port_id"])
        self.assertEqual(self.cloud.fips[fixtures.FIP]["description"], "operator-owned")
        self.assertEqual(service.get(self.connection, self.app_id)["phase"], "reserved")
        self.assertEqual([call[2] for call in self.cloud.mutations], ["unset"])
        self.cloud.mutations.clear()
        self.assert_success(self.reconcile())  # Fully disabled remains unassociated.
        self.assertEqual(self.cloud.mutations, [])
        self.assertIsNone(service.get(self.connection, self.app_id)["port"])

    def test_stopped_runtime_still_requires_exact_port_server_and_floating_ip_ownership(self):
        current = self.prepare()
        port = self.cloud.ports[current.worker_port_id]
        server = self.cloud.servers[current.worker_server_id]
        address = self.cloud.fips[fixtures.FIP]
        changes = (
            (port, "device_id", fixtures.identifier(999)),
            (port, "port_security_enabled", False),
            (server, "properties", {}),
            (address, "port_id", fixtures.identifier(999)),
            (address, "project_id", fixtures.identifier(999)),
            (address, "status", "DOWN"),
        )
        key = None
        for value, field, bad in changes:
            with self.subTest(field=field):
                original = copy.deepcopy(value[field])
                value[field] = bad
                key, operation = self.reconcile(key)
                self.assertEqual(operation.status, "recovery_required")
                self.assertEqual(self.cloud.mutations, [])
                self.assertEqual(db.get_application(self.connection, self.app_id), current)
                value[field] = original
        self.assert_success(self.reconcile(key))
        self.assertEqual(self.cloud.mutations, [])
        self.assertEqual(self.fixture.calls, [])

    def test_incomplete_retained_identity_is_not_treated_as_an_unbound_reservation(self):
        current = self.prepare()
        key = None
        identity = {
            "worker_server_id": current.worker_server_id,
            "worker_server_name": current.worker_server_name,
            "worker_port_id": current.worker_port_id,
            "worker_port_name": current.worker_port_name,
        }
        for field in identity:
            with self.subTest(field=field):
                db.set_application_runtime(
                    self.connection, self.app_id, running=False, **{**identity, field: None}
                )
                with self.assertRaises((openstack.DriftError, ValidationError)):
                    service._target(self.connection, self.fixture.config, self.app_id)
                key, operation = self.reconcile(key)
                self.assertEqual(operation.status, "recovery_required")
                self.assertEqual(self.cloud.mutations, [])
        db.set_application_runtime(self.connection, self.app_id, running=False, **identity)
        self.assert_success(self.reconcile(key))
        self.assertEqual(self.cloud.mutations, [])

    def test_interrupted_reconcile_retries_after_controller_restart_without_reassociation(self):
        current = self.prepare()
        reservation = copy.deepcopy(service.get(self.connection, self.app_id))
        with mock.patch.object(db, "mark_succeeded", side_effect=OSError("injected interruption")):
            key, operation = self.reconcile()
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(self.cloud.mutations, [])
        helper = self.fixture.api.helper_caller
        self.fixture.api.close()
        self.fixture.api = ControllerAPI(
            self.connection, self.fixture.config, self.fixture.root, helper_caller=helper
        )
        self.fixture.fixture.api = self.fixture.api
        self.fixture.router = self.fixture.api.router()
        self.assert_success(self.reconcile(key))
        self.assertEqual(db.get_application(self.connection, self.app_id), current)
        self.assertEqual(service.get(self.connection, self.app_id), reservation)
        self.assertEqual(self.cloud.mutations, [])

    def test_explicit_reservation_can_target_the_exact_stopped_retained_worker(self):
        self.base.deploy()
        current = self.stop_retaining_worker()
        reservation = self.base.mutate("allocate")
        self.assertEqual(reservation["portId"], current.worker_port_id)
        self.assertEqual(reservation["phase"], "active")
        self.assertEqual(self.cloud.fips[fixtures.FIP]["port_id"], current.worker_port_id)
        self.assertFalse(db.get_application(self.connection, self.app_id).desired_running)
        self.assertEqual([call[2] for call in self.cloud.mutations], ["create", "set"])
        self.cloud.mutations.clear()
        self.assert_success(self.reconcile())
        self.assertEqual(self.cloud.mutations, [])
