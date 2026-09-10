from __future__ import annotations

import copy
import threading
import unittest
import uuid
from unittest import mock

from openstack_platform import remote, runtime
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import reuse_fencing
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.deployment_config import parse_configuration
from openstack_platform.controller.http import HttpError
from tests import test_worker_reuse as fixtures


class ReuseFencingTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.WorkerReuseTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.connection = self.fixture.connection
        self.app_id = self.fixture.app_id
        self.path = f"/v1/applications/{self.app_id}/disable"
        self.fail_before = None
        self.fail_after = None
        self.on_delete = lambda: None
        self.install_api()

    def install_api(self):
        self.fixture.api.close()
        self.fixture.api = ControllerAPI(
            self.connection, self.fixture.config, self.fixture.root, helper_caller=self.helper
        )
        self.fixture.fixture.api = self.fixture.api
        self.fixture.router = self.fixture.api.router()
        self.project = self.fixture.api.router("project")

    def helper(self, config, action, values, **kwargs):
        if action == self.fail_before:
            self.fail_before = None
            raise RuntimeError("injected unconfirmed cleanup")
        if action == "app.worker.delete" and "expectedServerId" in values:
            self.on_delete()
            self.assertIs(values["single"], True)
            worker = self.fixture.workers.get(values["applicationId"])
            if worker and (worker["serverId"], worker["portId"]) != (
                values["expectedServerId"],
                values["expectedPortId"],
            ):
                raise app.ApplicationError("expected worker identity drifted")
        if action == "app.remove":
            self.assertIn("jobId", values)  # Never broad purge or data/Variable deletion.
            job = self.fixture.jobs.get(values["jobId"])
            if job and app.nomad_candidate_identity(job) != (
                values["candidateJobSha256"],
                values["candidateImage"],
            ):
                self.fixture.calls.append((action, copy.deepcopy(values)))
                raise remote.HelperError("CANDIDATE_MISMATCH", "different exact job")
        result = self.base.helper(config, action, values, **kwargs)
        if action == "app.builder.delete":
            result = {**result, "buildId": values["buildId"]}
        if action == self.fail_after:
            self.fail_after = None
            raise RuntimeError("injected lost cleanup reply")
        return result

    def interrupted(self, *, gc=True):
        self.before, self.previous = self.base.first()
        self.accepted = db.get_deployment(self.connection, self.app_id)
        self.base.after_quiesce = lambda: (_ for _ in ()).throw(RuntimeError("lost process exit"))
        self.original, operation = self.base.update()
        self.assertEqual(operation.status, "recovery_required", operation.safe_error)
        self.assertIn(operation.phase, {"image_pushed", "builder_cleaned"})
        if gc:
            self.fixture.jobs.clear()  # Nomad GC is NOT process exit evidence.
        self.fixture.calls.clear()
        return operation

    def fence(self, key=None, interrupted=None):
        key = key or str(uuid.uuid4())
        response = self.project.dispatch(
            "POST",
            self.path,
            {"Idempotency-Key": key},
            {"interruptedDeploymentId": interrupted or self.original},
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(response.body["statusUrl"], f"/v1/operations/{key}")
        self.fixture.api.wait_for_operations()
        operation = self.project.dispatch("GET", response.body["statusUrl"], {}, None).body
        return key, operation

    def actions(self):
        return [action for action, _ in self.fixture.calls]

    def assert_complete(self, key):
        original = db.get_operation(self.connection, self.original)
        own = db.get_operation(self.connection, key)
        self.assertEqual(original.status, "failed")
        self.assertEqual(original.cleanup_state, "confirmed")
        self.assertEqual(original.refs["fence_operation_id"], key)
        self.assertEqual(own.kind, "app.disable.fence")
        self.assertEqual(own.scope, f"app-fence-{self.app_id}")
        self.assertEqual(own.status, "succeeded")
        self.assertEqual(
            db.get_operation_dispatch(self.connection, self.original).status, "finished"
        )
        attempt = db.get_deployment_attempt(self.connection, self.original)
        self.assertEqual((attempt.status, attempt.cleanup_state), ("failed", "confirmed"))
        current = db.get_application(self.connection, self.app_id)
        self.assertFalse(current.desired_running)
        self.assertIsNone(current.worker_server_id)
        self.assertIsNone(current.worker_port_id)
        self.assertEqual(db.get_deployment(self.connection, self.app_id), self.accepted)
        self.assertIsNone(db.get_unfinished_operation(self.connection, f"app-{self.app_id}"))
        self.assertEqual(self.fixture.workers, {})
        self.assertEqual(self.fixture.jobs, {})

    def test_gc_requires_explicit_fence_then_enable_restores_accepted_artifact(self):
        self.interrupted()
        with self.assertRaises(HttpError):
            self.fixture.post(self.path, {})  # Ordinary disable cannot override recovery.
        key, result = self.fence()
        self.assertEqual(result["status"], "succeeded", result)
        self.assert_complete(key)
        self.assertEqual(self.actions(), ["app.worker.delete", "app.remove", "app.builder.delete"])
        values = self.fixture.calls[0][1]
        self.assertEqual(values["expectedServerId"], self.before.worker_server_id)
        self.assertEqual(values["expectedPortId"], self.before.worker_port_id)
        self.assertEqual(self.fixture.calls[-1][1], {"buildId": self.original})
        self.fixture.calls.clear()
        self.base.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/enable", {}))
        self.assertNotIn("app.build", self.actions())
        self.assertNotIn("app.manifest.delete", self.actions())
        deployed = next(
            values["job"] for action, values in self.fixture.calls if action == "app.deploy"
        )
        self.assertEqual(deployed, self.accepted.nomad_job)
        later = db.get_application(self.connection, self.app_id)
        self.assertTrue(later.desired_running)
        self.assertNotEqual(later.worker_server_id, self.before.worker_server_id)
        count = len(self.fixture.calls)
        self.assertEqual(self.fence(key)[1]["status"], "succeeded")
        self.assertEqual(len(self.fixture.calls), count)
        self.assertEqual(db.get_application(self.connection, self.app_id), later)
        # Direct completion replay has the same no-provider-work guarantee.
        reuse_fencing.disable_interrupted_deployment(
            self.connection,
            self.fixture.config,
            self.fixture.root,
            self.app_id,
            self.original,
            request_id=key,
            helper_caller=self.helper,
        )
        self.assertEqual(len(self.fixture.calls), count)

    def test_lost_cleanup_replies_keep_both_scopes_blocked_until_identical_retry(self):
        for action in ("app.worker.delete", "app.remove", "app.builder.delete"):
            with self.subTest(action=action):
                self.interrupted()
                self.fail_after = action
                key, result = self.fence()
                self.assertEqual(result["status"], "recovery_required", result)
                original = db.get_operation(self.connection, self.original)
                self.assertEqual(original.status, "running")
                self.assertEqual(original.refs["fence_operation_id"], key)
                self.assertNotEqual(original.cleanup_state, "confirmed")
                with self.assertRaises(HttpError):
                    self.fence()  # A different fence cannot adopt this journal.
                before = len(self.fixture.calls)
                self.base.update(self.original)  # API replay must not enqueue the old retry.
                self.assertEqual(len(self.fixture.calls), before)
                self.install_api()  # Simulate restart with both durable scopes reserved.
                self.assertEqual(db.get_operation(self.connection, self.original).status, "running")
                self.assertEqual(self.fence(key)[1]["status"], "succeeded")
                self.assert_complete(key)
                self.base.after_quiesce = lambda: None

    def candidate(self, operation, *, separate=False):
        manifest = parse_configuration(self.fixture.body["configuration"]).manifest({})
        job = app.render_nomad_job(
            application_id=self.app_id,
            application_slug="commons",
            image=operation.candidate_digest,
            manifest=manifest,
            platform=self.fixture.config.platform,
            cpu_mhz=self.before.scheduler_cpu_mhz,
            memory_mib=self.before.scheduler_memory_mib,
            source_commit="b" * 40,
            recipe_hash=self.accepted.recipe_hash,
            placement_id=operation.refs["reused_worker"]["placement_id"],
            route_marker=self.original,
            candidate=separate,
        )
        job_id = app.nomad_job_id(job, "commons")
        refs = {
            **operation.refs,
            "candidate_job_id": job_id,
            "candidate_job_sha256": app.nomad_candidate_identity(job)[0],
        }
        db.checkpoint_operation(
            self.connection, self.original, phase="candidate_submitting", refs=refs
        )
        return job_id, job

    def test_purge_only_exact_candidate_or_predecessor_after_physical_fence(self):
        for present in ("candidate", "predecessor", "separate", "foreign"):
            with self.subTest(present=present):
                operation = self.interrupted(gc=False)
                job_id, job = self.candidate(operation, separate=present == "separate")
                if present in {"candidate", "separate"}:
                    self.fixture.jobs[job_id] = job
                if present == "foreign":
                    # Valid platform job with an unrecorded identity: no broad fallback.
                    refs = dict(db.get_operation(self.connection, self.original).refs)
                    refs["candidate_job_sha256"] = "f" * 64
                    refs["maintenance_predecessor"] = {**refs["maintenance_predecessor"]}
                    self.fixture.jobs[job_id] = job
                    db.checkpoint_operation(
                        self.connection, self.original, phase="candidate_rejected", refs=refs
                    )
                key, result = self.fence()
                self.assertLess(
                    self.actions().index("app.worker.delete"), self.actions().index("app.remove")
                )
                if present == "foreign":
                    self.assertEqual(result["status"], "recovery_required")
                    self.assertIn(job_id, self.fixture.jobs)
                    self.assertNotIn("app.builder.delete", self.actions())
                else:
                    self.assertEqual(result["status"], "succeeded", result)
                    self.assert_complete(key)
                self.base.after_quiesce = lambda: None

    def test_rejects_cross_app_wrong_worker_accepted_target_and_missing_candidate_identity(self):
        operation = self.interrupted()
        original_refs = copy.deepcopy(operation.refs)
        for change in (
            "cross-app",
            "worker",
            "slot",
            "predecessor",
            "accepted",
            "candidate-job",
            "candidate-missing",
        ):
            with self.subTest(change=change):
                refs = copy.deepcopy(original_refs)
                phase = "builder_cleaned"
                if change == "cross-app":
                    refs["application_id"] = str(uuid.uuid4())
                elif change in {"worker", "slot"}:
                    field = "server_id" if change == "worker" else "placement_id"
                    refs["reused_worker"][field] = str(uuid.uuid4())
                elif change == "predecessor":
                    refs["maintenance_predecessor"]["deployment_id"] = self.original
                elif change == "accepted":
                    with db.transaction(self.connection):
                        self.connection.execute(
                            "UPDATE deployment_attempts SET accepted_at=? WHERE deployment_id=?",
                            (db.utc_now(), self.original),
                        )
                elif change == "candidate-job":
                    refs.update(candidate_job_id="another-app", candidate_job_sha256="a" * 64)
                elif change == "candidate-missing":
                    phase = "candidate_submitting"
                db.checkpoint_operation(self.connection, self.original, phase=phase, refs=refs)
                _, result = self.fence()
                self.assertEqual(result["status"], "failed", result)
                self.assertFalse(self.fixture.calls)
                self.assertNotIn(
                    "fence_operation_id", db.get_operation(self.connection, self.original).refs
                )
                with db.transaction(self.connection):
                    self.connection.execute(
                        "UPDATE deployment_attempts SET accepted_at=NULL WHERE deployment_id=?",
                        (self.original,),
                    )
        db.checkpoint_operation(
            self.connection, self.original, phase="builder_cleaned", refs=original_refs
        )
        # New non-destructive identity fields do not change the bounded selectors.
        refs = copy.deepcopy(original_refs)
        refs["reused_worker"]["node_id"] = str(uuid.uuid4())
        db.checkpoint_operation(self.connection, self.original, phase="builder_cleaned", refs=refs)
        self.assertEqual(self.fence()[1]["status"], "succeeded")

    def test_already_accepted_candidate_is_not_a_fencing_target(self):
        self.base.first()
        self.fixture.fail_action = "app.manifest.retain"
        self.original, operation = self.base.update()
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(operation.phase, "accepted")
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, self.original
        )
        self.fixture.calls.clear()
        _, result = self.fence()
        self.assertEqual(result["status"], "failed", result)
        self.assertFalse(self.fixture.calls)
        self.assertNotIn(
            "fence_operation_id", db.get_operation(self.connection, self.original).refs
        )
        self.assertEqual(
            db.get_operation(self.connection, self.original).status, "recovery_required"
        )

    def test_completion_rollback_keeps_runtime_and_both_journals_for_identical_retry(self):
        self.interrupted()
        before = db.get_application(self.connection, self.app_id)
        with mock.patch.object(
            db,
            "checkpoint_deployment_attempt",
            side_effect=RuntimeError("injected atomic completion interruption"),
        ):
            key, result = self.fence()
        self.assertEqual(result["status"], "recovery_required", result)
        self.assertEqual(db.get_application(self.connection, self.app_id), before)
        self.assertEqual(db.get_operation(self.connection, self.original).status, "running")
        self.assertEqual(
            db.get_operation_dispatch(self.connection, self.original).status, "recovery_required"
        )
        self.assertEqual(self.fixture.workers, {})
        self.assertEqual(self.fixture.jobs, {})
        self.assertEqual(self.fence(key)[1]["status"], "succeeded")
        self.assert_complete(key)

    def test_lost_completion_reply_cannot_resubmit_completed_fence(self):
        self.interrupted()
        complete = reuse_fencing._complete

        def lose_reply(*args):
            complete(*args)
            raise RuntimeError("injected lost completion reply")

        with mock.patch.object(reuse_fencing, "_complete", side_effect=lose_reply):
            key, result = self.fence()
        self.assertEqual(result["status"], "succeeded", result)
        self.assert_complete(key)
        calls = len(self.fixture.calls)
        self.assertEqual(self.fence(key)[1]["status"], "succeeded")
        self.assertEqual(len(self.fixture.calls), calls)

    def test_provider_worker_drift_keeps_recovery_blocked_without_purging_jobs(self):
        self.interrupted(gc=False)
        worker = next(iter(self.fixture.workers.values()))
        worker["serverId"] = str(uuid.uuid4())
        _, result = self.fence()
        self.assertEqual(result["status"], "recovery_required")
        self.assertNotIn("app.remove", self.actions())
        self.assertNotIn("app.builder.delete", self.actions())
        self.assertEqual(len(self.fixture.workers), 1)

    def test_pending_or_running_original_retry_cannot_be_fenced(self):
        self.interrupted()
        for status in ("pending", "running"):
            with self.subTest(status=status):
                with db.transaction(self.connection):
                    self.connection.execute(
                        "UPDATE operation_dispatches SET status=? WHERE operation_id=?",
                        (status, self.original),
                    )
                _, result = self.fence()
                self.assertEqual(result["status"], "failed", result)
                self.assertNotIn(
                    "fence_operation_id", db.get_operation(self.connection, self.original).refs
                )
                self.assertFalse(self.fixture.calls)

    def test_original_retry_cannot_requeue_while_fence_callback_holds_app_lock(self):
        self.interrupted()
        entered, release = threading.Event(), threading.Event()

        def wait():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test fencing callback timed out")

        self.on_delete = wait
        key = str(uuid.uuid4())
        try:
            response = self.project.dispatch(
                "POST",
                self.path,
                {"Idempotency-Key": key},
                {"interruptedDeploymentId": self.original},
            )
            self.assertTrue(entered.wait(5))
            state = self.project.dispatch("GET", response.body["statusUrl"], {}, None).body
            self.assertEqual(state["status"], "running")
            with self.assertRaises(runtime.LockBusy):
                with runtime.lock(self.fixture.root, f"app-{self.app_id}", wait=False):
                    self.fail("fence failed to hold app lock")
            with self.assertRaises(db.DatabaseError):
                db.requeue_recovery_dispatch(
                    self.connection,
                    operation_id=self.original,
                    kind="app.deploy",
                    scope=f"app-{self.app_id}",
                )
            replay = self.fixture.router.dispatch(
                "POST", self.base.path, {"Idempotency-Key": self.original}, self.base.body()
            )
            self.assertEqual(replay.status, 202)
            self.assertEqual(
                db.get_operation_dispatch(self.connection, self.original).status,
                "recovery_required",
            )
            with self.assertRaises(HttpError):
                self.project.dispatch(
                    "POST",
                    self.path,
                    {"Idempotency-Key": str(uuid.uuid4())},
                    {"interruptedDeploymentId": self.original},
                )
        finally:
            release.set()
            self.fixture.api.wait_for_operations()
        self.assert_complete(key)

    def test_claim_transaction_closes_original_requeue_check_update_race(self):
        self.interrupted()
        checked, update, deleting, finish, raced = (threading.Event() for _ in range(5))
        errors = []
        original_target = reuse_fencing._target
        first = True

        def target(*args):
            nonlocal first
            result = original_target(*args)
            if first:
                first = False
                checked.set()
                if not update.wait(5):
                    raise RuntimeError("test claim gate timed out")
            return result

        def delete():
            deleting.set()
            if not finish.wait(5):
                raise RuntimeError("test delete gate timed out")

        def requeue():
            connection = db.connect(self.fixture.root / "platform.sqlite3", create=False)
            try:
                db.requeue_recovery_dispatch(
                    connection,
                    operation_id=self.original,
                    kind="app.deploy",
                    scope=f"app-{self.app_id}",
                )
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
                raced.set()

        self.on_delete = delete
        key = str(uuid.uuid4())
        thread = threading.Thread(target=requeue)
        with mock.patch.object(reuse_fencing, "_target", side_effect=target):
            try:
                self.project.dispatch(
                    "POST",
                    self.path,
                    {"Idempotency-Key": key},
                    {"interruptedDeploymentId": self.original},
                )
                self.assertTrue(checked.wait(5))
                thread.start()
                self.assertFalse(raced.wait(0.1))  # BEGIN IMMEDIATE keeps requeue outside the gap.
                update.set()
                self.assertTrue(deleting.wait(5))
                self.assertTrue(raced.wait(5))
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], db.DatabaseError)
            finally:
                update.set()
                finish.set()
                if thread.ident is not None:
                    thread.join(5)
                self.fixture.api.wait_for_operations()
        self.assert_complete(key)

    def test_cross_application_request_cannot_claim_the_original_journal(self):
        self.interrupted()
        other = str(uuid.uuid4())
        self.project.dispatch(
            "POST", "/v1/applications", {"Idempotency-Key": other}, {"slug": "another-app"}
        )
        self.path = f"/v1/applications/{other}/disable"
        _, result = self.fence()
        self.assertEqual(result["status"], "failed", result)
        self.assertNotIn(
            "fence_operation_id", db.get_operation(self.connection, self.original).refs
        )
        self.assertFalse(self.fixture.calls)

    def test_floating_ip_detaches_in_reserve_only_mode_before_guarded_delete(self):
        self.interrupted()
        events = []
        with mock.patch.object(
            reuse_fencing.public_ip_service,
            "release_locked",
            side_effect=lambda *a, **kw: events.append(kw),
        ):
            self.on_delete = lambda: self.assertEqual(events[0]["reserve_only"], True)
            self.assertEqual(self.fence()[1]["status"], "succeeded")
        self.assertEqual(len(events), 1)

    def test_strict_body_and_idempotency_do_not_turn_ordinary_disable_into_fencing(self):
        self.interrupted()
        for body in (
            {"interruptedDeploymentId": None},
            {"interruptedDeploymentId": "bad"},
            {"interruptedDeploymentId": self.original, "force": True},
        ):
            with self.assertRaises(HttpError):
                self.project.dispatch(
                    "POST", self.path, {"Idempotency-Key": str(uuid.uuid4())}, body
                )
        key, result = self.fence()
        self.assertEqual(result["status"], "succeeded")
        with self.assertRaises(HttpError):
            self.project.dispatch("POST", self.path, {"Idempotency-Key": key}, {})


if __name__ == "__main__":
    unittest.main()
