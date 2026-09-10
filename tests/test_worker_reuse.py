from __future__ import annotations

import copy
import unittest
import uuid
from unittest import mock

from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import image_service
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.http import HttpError
from tests import test_application_sizing as sizing_fixtures


class WorkerReuseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sizing_fixtures.ApplicationSizingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.api.close()
        self.original_helper = self.fixture.helper
        self.after_build = lambda: None
        self.after_quiesce = lambda: None
        self.after_remove = lambda: None
        self.after_submit = lambda: None
        self.health_calls = 0
        self.fail_health_call = None
        self.quiesced = set()
        self.fixture.api = ControllerAPI(
            self.fixture.connection,
            self.fixture.config,
            self.fixture.root,
            helper_caller=self.helper,
        )
        self.fixture.fixture.api = self.fixture.api
        self.fixture.router = self.fixture.api.router()
        self.connection = self.fixture.connection
        self.app_id = self.fixture.app_id
        self.path = f"/v1/admin/applications/{self.app_id}/deployments"

    def helper(self, config, action, values, **kwargs):
        if action == "app.quiesce":
            self.fixture.calls.append((action, copy.deepcopy(values)))
            job = self.fixture.jobs[values["jobId"]]
            self.assertEqual(
                app.nomad_candidate_identity(job),
                (values["candidateJobSha256"], values["candidateImage"]),
            )
            self.quiesced.add(values["jobId"])
            self.after_quiesce()
            return {
                **values,
                "jobStopped": True,
                "allocationsStopped": True,
                "receiptSha256": "c" * 64,
            }
        if action == "app.manifest.verify":
            self.fixture.calls.append((action, copy.deepcopy(values)))
            return {**values, "available": True}
        if action == "app.health":
            self.health_calls += 1
            if self.health_calls == self.fail_health_call:
                raise RuntimeError("injected acceptance observation interruption")
        if action == "app.deploy" and values.get("requireExact"):
            job_id = app.nomad_job_id(values["job"], "commons")
            existing = self.fixture.jobs.get(job_id)
            if values.get("resumeOnly") and existing is None:
                raise app.ApplicationError("lost submitted job requires fencing")
            if existing is not None and app.nomad_candidate_identity(
                existing
            ) != app.nomad_candidate_identity(values["job"]):
                raise app.ApplicationError("injected exact candidate mismatch")
        result = self.original_helper(config, action, values, **kwargs)
        if action == "app.worker.create":
            result.update(imageId=values["workerImageId"], absent=False, nodeId=str(uuid.uuid4()))
        if action == "app.worker.capacity":
            result = {
                **self.fixture.workers[values["applicationId"]],
                **result,
                "applicationId": values["applicationId"],
                "slug": values["slug"],
            }
        if action == "app.build":
            self.after_build()
        if action == "app.remove":
            self.after_remove()
        if action == "app.deploy":
            self.after_submit()
        return result

    def first(self, *, runtime="node"):
        self.fixture.body["configuration"]["build"]["runtime"] = runtime
        key, operation = self.fixture.deploy()
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.fixture.calls.clear()
        self.health_calls = 0
        return db.get_application(self.connection, self.app_id), key

    def body(self):
        return {
            **self.fixture.body,
            "commit": "b" * 40,
            "maintenance": True,
            "workerStrategy": "reuse",
        }

    def update(self, key=None):
        return self.fixture.post(self.path, self.body(), key)

    def actions(self):
        return [action for action, _ in self.fixture.calls]

    def assert_success(self, result):
        key, operation = result
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        return key

    def test_code_updates_reuse_exact_worker_and_start_only_once(self):
        before, previous = self.first()

        def still_serving():
            connection = db.connect(self.fixture.root / "platform.sqlite3", create=False)
            try:
                self.assertTrue(db.get_application(connection, self.app_id).desired_running)
                self.assertTrue(self.fixture.jobs)
                self.assertEqual(
                    db.get_active_deployment(connection, self.app_id).deployment_id, previous
                )
            finally:
                connection.close()

        self.after_build = still_serving
        key = self.assert_success(self.update())
        current = db.get_application(self.connection, self.app_id)
        self.assertEqual(current.worker_server_id, before.worker_server_id)
        self.assertEqual(current.worker_port_id, before.worker_port_id)
        self.assertEqual(current.worker_flavor, before.worker_flavor)
        self.assertEqual(current.scheduler_cpu_mhz, before.scheduler_cpu_mhz)
        self.assertEqual(current.scheduler_memory_mib, before.scheduler_memory_mib)
        self.assertEqual(db.get_active_deployment(self.connection, self.app_id).deployment_id, key)
        self.assertEqual(len(self.fixture.workers), 1)
        self.assertEqual(self.actions().count("app.deploy"), 1)
        self.assertEqual(self.actions().count("app.quiesce"), 1)
        self.assertLess(self.actions().index("app.build"), self.actions().index("app.quiesce"))
        self.assertLess(self.actions().index("app.quiesce"), self.actions().index("app.remove"))
        self.assertLess(self.actions().index("app.remove"), self.actions().index("app.deploy"))
        for forbidden in (
            "app.worker.create",
            "app.worker.delete",
            "app.promote",
            "app.worker.observe",
        ):
            self.assertNotIn(forbidden, self.actions())
        calls = len(self.fixture.calls)
        self.assert_success(self.update(key))
        self.assertEqual(len(self.fixture.calls), calls)

    def test_bun_uses_the_same_generic_path(self):
        before, _ = self.first(runtime="bun")
        self.assert_success(self.update())
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_server_id,
            before.worker_server_id,
        )
        self.assertNotIn("app.worker.create", self.actions())

    def test_reuse_can_follow_an_accepted_candidate_job_slot(self):
        self.first()
        self.assert_success(self.fixture.deploy())
        previous = db.get_deployment(self.connection, self.app_id)
        self.assertEqual(app.nomad_job_id(previous.nomad_job, "commons"), "commons-candidate")
        before = db.get_application(self.connection, self.app_id)
        self.fixture.calls.clear()
        self.assert_success(self.update())
        current = db.get_deployment(self.connection, self.app_id)
        self.assertEqual(app.nomad_job_id(current.nomad_job, "commons"), "commons-candidate")
        self.assertEqual(
            app.nomad_placement_id(current.nomad_job), app.nomad_placement_id(previous.nomad_job)
        )
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_server_id,
            before.worker_server_id,
        )
        self.assertNotIn("app.promote", self.actions())

    def test_initial_or_incompatible_requests_fail_before_build_without_blocking_scope(self):
        _, failed = self.update()
        self.assertEqual(failed.status, "failed")
        self.assertNotIn("app.build", self.actions())
        self.first()
        worker = next(iter(self.fixture.workers.values()))
        for change in (
            {"imageId": str(uuid.uuid4())},
            {"portId": str(uuid.uuid4())},
            {"ready": False},
        ):
            original = copy.deepcopy(worker)
            worker.update(change)
            self.fixture.calls.clear()
            _, failed = self.update()
            self.assertEqual(failed.status, "failed", failed.safe_error)
            self.assertNotIn("app.build", self.actions())
            self.assertNotIn("app.quiesce", self.actions())
            self.assertIsNone(db.get_unfinished_operation(self.connection, f"app-{self.app_id}"))
            worker.clear()
            worker.update(original)

    def test_unavailable_capacity_is_rejected_before_stopping_the_app(self):
        self.first()
        self.fixture.capacity_override = (100, 64)
        _, failed = self.update()
        self.assertEqual(failed.status, "failed")
        self.assertNotIn("app.build", self.actions())
        self.assertNotIn("app.quiesce", self.actions())
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)

    def test_drift_during_build_is_rechecked_before_cutover(self):
        self.first()
        self.after_build = lambda: next(iter(self.fixture.workers.values())).update(
            portId=str(uuid.uuid4())
        )
        _, failed = self.update()
        self.assertEqual(failed.status, "recovery_required")
        self.assertNotIn("app.quiesce", self.actions())
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)

    def test_build_failure_leaves_the_existing_process_and_worker_untouched(self):
        before, previous = self.first()
        self.fixture.fail_action = "app.build"
        _, failed = self.update()
        self.assertEqual(failed.status, "recovery_required")
        self.assertEqual(db.get_application(self.connection, self.app_id), before)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, previous
        )
        self.assertNotIn("app.quiesce", self.actions())
        self.assertNotIn("app.remove", self.actions())

    def test_lost_quiesce_response_is_reobserved_on_identical_retry(self):
        before, _ = self.first()

        def interrupt():
            self.after_quiesce = lambda: None
            raise RuntimeError("injected lost process-stop response")

        self.after_quiesce = interrupt
        key, failed = self.update()
        self.assertEqual(failed.status, "recovery_required")
        self.assertNotIn("app.remove", self.actions())
        self.assert_success(self.update(key))
        self.assertEqual(self.actions().count("app.build"), 1)
        self.assertEqual(self.actions().count("app.quiesce"), 2)
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_server_id,
            before.worker_server_id,
        )

    def test_exit_evidence_survives_a_lost_purge_response(self):
        self.first()

        def interrupt():
            self.after_remove = lambda: None
            raise RuntimeError("injected lost job removal response")

        self.after_remove = interrupt
        key, failed = self.update()
        self.assertEqual(failed.status, "recovery_required")
        self.assertTrue(failed.refs["maintenance_process_stopped"])
        self.assertEqual(self.fixture.jobs, {})
        self.assert_success(self.update(key))
        self.assertEqual(self.actions().count("app.quiesce"), 1)
        self.assertEqual(self.actions().count("app.build"), 1)

    def test_failed_candidate_keeps_worker_and_enable_restores_previous_artifact(self):
        before, previous = self.first()
        self.fixture.fail_health = True
        _, failed = self.update()
        self.assertEqual(failed.status, "failed", failed.safe_error)
        self.assertEqual(failed.cleanup_state, "confirmed")
        self.assertFalse(db.get_application(self.connection, self.app_id).desired_running)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, previous
        )
        self.assertEqual(self.fixture.jobs, {})
        self.assertEqual(len(self.fixture.workers), 1)
        self.assertNotIn("app.worker.delete", self.actions())
        self.fixture.fail_health = False
        self.fixture.calls.clear()
        self.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/enable", {}))
        current = db.get_application(self.connection, self.app_id)
        self.assertTrue(current.desired_running)
        self.assertEqual(current.worker_server_id, before.worker_server_id)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, previous
        )
        self.assertNotIn("app.build", self.actions())
        self.assertNotIn("app.worker.create", self.actions())

    def test_disable_reclaims_retained_worker_after_failed_update(self):
        self.first()
        self.fixture.fail_health = True
        _, failed = self.update()
        self.assertEqual(failed.status, "failed")
        self.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/disable", {}))
        self.assertEqual(self.fixture.workers, {})
        self.assertIsNone(db.get_application(self.connection, self.app_id).worker_server_id)

    def test_replacement_after_failed_reuse_rejects_before_admission_and_disable_still_works(self):
        self.first()
        self.fixture.fail_health = True
        _, failed = self.update()
        self.assertEqual(failed.status, "failed")
        self.fixture.fail_health = False
        self.fixture.calls.clear()
        _, rejected = self.fixture.deploy()
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(self.actions(), [])
        self.assertIsNone(db.get_unfinished_operation(self.connection, f"app-{self.app_id}"))
        self.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/disable", {}))

    def test_pre_image_pin_interruption_can_resume(self):
        before, _ = self.first()
        with mock.patch.object(
            image_service,
            "pin_deployment_images",
            side_effect=RuntimeError("interrupted before pins"),
        ):
            key, interrupted = self.update()
        self.assertEqual(interrupted.status, "recovery_required")
        self.assertEqual(interrupted.phase, "validated")
        self.assertNotIn("worker_image_id", interrupted.refs)
        self.assert_success(self.update(key))
        self.assertEqual(self.actions().count("app.build"), 1)
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_server_id,
            before.worker_server_id,
        )

    def test_recovery_after_healthy_checkpoint_does_not_stop_or_rebuild_again(self):
        before, _ = self.first()
        self.fail_health_call = 2
        key, interrupted = self.update()
        self.assertEqual(interrupted.status, "recovery_required")
        self.assertEqual(interrupted.phase, "deployment_healthy")
        self.fixture.calls.clear()
        self.assert_success(self.update(key))
        self.assertNotIn("app.quiesce", self.actions())
        self.assertNotIn("app.build", self.actions())
        self.assertNotIn("app.worker.delete", self.actions())
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_server_id,
            before.worker_server_id,
        )

    def test_lost_submit_reply_recovers_exact_candidate_without_environment_restart(self):
        self.first()

        def lose_reply():
            self.after_submit = lambda: None
            raise RuntimeError("lost Nomad submission reply")

        self.after_submit = lose_reply
        key, interrupted = self.update()
        self.assertEqual(interrupted.phase, "candidate_submitting")
        self.assertIn("candidate_job_sha256", interrupted.refs)
        self.fixture.calls.clear()
        self.assert_success(self.update(key))
        self.assertNotIn("app.env.set", self.actions())
        self.assertNotIn("app.build", self.actions())
        self.assertNotIn("app.quiesce", self.actions())
        self.assertNotIn("app.worker.create", self.actions())
        submitted = next(values for action, values in self.fixture.calls if action == "app.deploy")
        self.assertIs(submitted["requireExact"], True)

    def test_lost_submitted_job_cannot_be_recreated_without_fencing(self):
        self.first()

        def lose_reply():
            self.after_submit = lambda: None
            raise RuntimeError("lost submission reply")

        self.after_submit = lose_reply
        key, interrupted = self.update()
        self.assertEqual(interrupted.phase, "candidate_submitting")
        self.fixture.jobs.clear()
        self.fixture.calls.clear()
        _, blocked = self.update(key)
        self.assertEqual(blocked.status, "recovery_required")
        self.assertEqual(self.fixture.jobs, {})
        self.assertNotIn("app.env.set", self.actions())
        self.assertNotIn("app.worker.create", self.actions())

    def test_unconfirmed_candidate_stop_keeps_recovery_blocked_and_is_cleanup_only_on_retry(self):
        self.first()
        self.fixture.fail_health = True
        calls = 0

        def lose_candidate_reply():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("lost candidate process-stop reply")

        self.after_quiesce = lose_candidate_reply
        key, interrupted = self.update()
        self.assertEqual(interrupted.status, "recovery_required")
        self.assertEqual(interrupted.phase, "candidate_rejected")
        self.assertNotIn("candidate_process_stopped", interrupted.refs)
        self.fixture.calls.clear()
        _, failed = self.update(key)
        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.refs["candidate_process_stopped"])
        self.assertNotIn("app.env.set", self.actions())
        self.assertNotIn("app.deploy", self.actions())
        self.assertNotIn("app.worker.delete", self.actions())
        self.assertIn("app.quiesce", self.actions())

    def test_candidate_exit_checkpoint_survives_a_lost_cleanup_reply(self):
        self.first()
        self.fixture.fail_health = True
        calls = 0

        def lose_candidate_reply():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("lost candidate purge reply")

        self.after_remove = lose_candidate_reply
        key, interrupted = self.update()
        self.assertEqual(interrupted.status, "recovery_required")
        self.assertEqual(interrupted.phase, "candidate_rejected")
        self.assertTrue(interrupted.refs["candidate_process_stopped"])
        self.fixture.calls.clear()
        _, failed = self.update(key)
        self.assertEqual(failed.status, "failed")
        self.assertNotIn("app.quiesce", self.actions())
        self.assertNotIn("app.deploy", self.actions())
        self.assertNotIn("app.env.set", self.actions())

    def test_worker_drift_blocks_acceptance_recovery(self):
        self.first()
        self.fail_health_call = 2
        key, interrupted = self.update()
        self.assertEqual(interrupted.phase, "deployment_healthy")
        worker = next(iter(self.fixture.workers.values()))
        previous_id = worker["serverId"]
        worker["serverId"] = str(uuid.uuid4())
        self.fixture.calls.clear()
        _, blocked = self.update(key)
        self.assertEqual(blocked.status, "recovery_required")
        self.assertNotIn("app.health", self.actions())
        self.assertNotIn("app.worker.delete", self.actions())
        worker["serverId"] = previous_id
        self.assert_success(self.update(key))

    def test_recovery_after_acceptance_does_not_delete_reused_worker(self):
        before, _ = self.first()
        self.fixture.fail_action = "app.manifest.retain"
        key, interrupted = self.update()
        self.assertEqual(interrupted.status, "recovery_required")
        self.assertEqual(interrupted.phase, "accepted")
        self.fixture.calls.clear()
        self.assert_success(self.update(key))
        self.assertEqual(self.actions(), ["app.manifest.retain"])
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_server_id,
            before.worker_server_id,
        )

    def test_strategy_is_explicit_privileged_and_cannot_resize(self):
        features = self.fixture.router.dispatch("GET", "/v1/admin/capabilities", {}, None).body[
            "features"
        ]
        self.assertIn("worker-reuse-v1", features)
        for body in (
            {**self.body(), "maintenance": False},
            {**self.body(), "workerStrategy": True},
            {**self.body(), "workerStrategy": {}},
            {**self.body(), "workerStrategy": "automatic"},
            {**self.body(), "plan": {}},
        ):
            with self.subTest(body=body), self.assertRaises(HttpError):
                self.fixture.post(self.path, body)
        with self.assertRaises(HttpError):
            self.fixture.post(f"/v1/applications/{self.app_id}/deployments", self.body())
