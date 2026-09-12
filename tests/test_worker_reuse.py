"""Opt-in same-worker releases, failure cleanup, and retained-image recovery."""

from __future__ import annotations

import copy
import unittest
import uuid
from dataclasses import replace
from unittest import mock

from openstack_platform import remote
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import worker_reuse
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.deployment_config import parse_configuration
from openstack_platform.controller.deployment_service import DeploymentRequest, DeploymentService
from openstack_platform.controller.http import HttpError
from tests import test_retained_rollback as fixtures


class WorkerReuseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RetainedRollbackTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.fixture
        self.connection = self.f.connection
        self.app_id = self.f.app_id
        self.base = f"/v1/admin/applications/{self.app_id}"
        self.stopped = set()
        self.before = lambda _action, _values: None
        self.after = lambda _action, _values: None
        self.f.api.helper_caller = self.helper

    def helper(self, config, action, values, **bounds):
        self.before(action, values)
        if action == "app.build.cleanup":
            self.f.calls.append((action, copy.deepcopy(values)))
            result = {**values, "builderAbsent": True, "artifactAbsent": True}
        elif action == "app.stop":
            self.f.calls.append((action, copy.deepcopy(values)))
            job = self.f.jobs.get(values["jobId"])
            if job is None or app.nomad_candidate_identity(job) != (
                values["candidateJobSha256"],
                values["candidateImage"],
            ):
                raise app.ApplicationError("exact stop identity unavailable")
            self.stopped.add(values["jobId"])
            result = {"jobStopped": True}
        else:
            if action == "app.deploy":
                job_id = app.nomad_job_id(values["job"], "commons")
                slot = app.nomad_placement_id(values["job"])
                for other_id, other in self.f.jobs.items():
                    if other_id != job_id and app.nomad_placement_id(other) == slot:
                        self.assertIn(
                            other_id, self.stopped, "two live versions on the same worker"
                        )
                self.stopped.discard(job_id)
            result = self.fixture.helper(config, action, values, **bounds)
            if action == "app.worker.create":
                result["imageId"] = values["workerImageId"]
            if action == "app.remove":
                self.stopped.discard(values.get("jobId", "commons"))
        self.after(action, values)
        return result

    def initial(self):
        key, operation = self.f.deploy()
        self.success(operation)
        self.f.calls.clear()
        return key

    def body(self):
        return {**self.f.body, "maintenance": True, "reuseWorker": True}

    def release(self, body=None, key=None):
        return self.f.post(self.base + "/deployments", body or self.body(), key)

    def plan(self, deployment_id):
        return self.f.router.dispatch(
            "GET",
            self.base + f"/rollback-plan?deploymentId={deployment_id}&reuseWorker=true",
            {},
            None,
        ).body

    def rollback(self, plan, key=None):
        return self.f.post(self.base + "/rollback", {"plan": plan, "confirmation": "commons"}, key)

    def success(self, operation):
        self.assertEqual(operation.status, "succeeded", operation.safe_error)

    def no_worker_mutations(self):
        self.assertFalse({"app.worker.create", "app.worker.delete"} & {a for a, _ in self.f.calls})

    def test_typed_privileged_opt_in_and_explicit_downtime_consent(self):
        self.initial()
        capabilities = self.f.router.dispatch("GET", "/v1/admin/capabilities", {}, None).body
        self.assertIn("reuse-worker-v1", capabilities["features"])
        for invalid in (
            {**self.body(), "maintenance": False},
            {**self.body(), "reuseWorker": 1},
            {**self.body(), "reuseWorker": "true"},
            {**self.body(), "plan": self.f.plan()},
        ):
            with self.subTest(body=invalid), self.assertRaises(HttpError):
                self.release(invalid)
        with self.assertRaises(HttpError):
            self.f.post(f"/v1/applications/{self.app_id}/deployments", self.body())
        with self.assertRaises(HttpError):
            self.f.api.router("project").dispatch(
                "POST", self.base + "/deployments", {}, self.body()
            )
        self.assertEqual(self.f.calls, [])

    def test_direct_service_retry_cannot_remove_the_reuse_mode_from_an_unfinished_operation(self):
        self.initial()
        self.f.fail_action = "app.worker.capacity"
        key, operation = self.release()
        self.assertEqual(operation.status, "recovery_required")
        self.f.calls.clear()
        with self.assertRaises(db.UnfinishedOperationError):
            DeploymentService(
                self.connection, self.f.config, self.f.root, helper_caller=self.helper
            ).deploy(
                DeploymentRequest(
                    "commons",
                    self.f.body["repository"],
                    self.f.body["requestedRef"],
                    self.f.body["commit"],
                    self.f.body["configurationRevision"],
                    parse_configuration(self.f.body["configuration"]),
                    key,
                    maintenance=True,
                )
            )
        self.assertEqual(self.f.calls, [])
        self.success(self.release(key=key)[1])
        self.no_worker_mutations()

    def test_requires_existing_worker_and_never_falls_back_to_creation(self):
        _, operation = self.release()
        self.assertEqual(operation.status, "failed")
        self.assertEqual(self.f.calls, [])
        self.initial()
        self.f.workers.clear()
        _, operation = self.release()
        self.assertEqual(operation.status, "recovery_required")
        self.no_worker_mutations()
        self.assertNotIn("app.build", [a for a, _ in self.f.calls])

    def test_repeated_releases_preserve_vm_port_placement_size_and_actual_image(self):
        self.initial()
        workers = copy.deepcopy(self.f.workers)
        original = db.get_application(self.connection, self.app_id)
        for number in range(3):
            self.f.body["commit"] = str(number + 1) * 40
            db.put_image_selection(
                self.connection,
                role="worker",
                image_id=str(uuid.uuid4()),
                display_name="new selection is not a host upgrade",
                source_commit="f" * 40,
                compatibility_hash="e" * 64,
            )
            key, operation = self.release()
            self.success(operation)
            self.assertEqual(self.f.workers, workers)
            self.assertEqual(len(self.f.jobs), 1)
            current = db.get_application(self.connection, self.app_id)
            self.assertTrue(current.desired_running)
            for field in (
                "worker_server_id",
                "worker_port_id",
                "worker_flavor",
                "scheduler_cpu_mhz",
                "scheduler_memory_mib",
            ):
                self.assertEqual(getattr(current, field), getattr(original, field))
            self.assertEqual(
                operation.refs["worker_image_id"], next(iter(workers.values()))["imageId"]
            )
            self.assertEqual(
                db.get_active_deployment(self.connection, self.app_id).deployment_id, key
            )
            calls = len(self.f.calls)
            self.success(self.release(key=key)[1])
            self.assertEqual(len(self.f.calls), calls)
        self.no_worker_mutations()

    def test_reuse_cutover_checks_capacity_once_for_deploy_and_rollback(self):
        previous = self.initial()
        current = db.get_application(self.connection, self.app_id)
        allocation = {
            "cpuMHz": current.scheduler_cpu_mhz,
            "memoryMiB": current.scheduler_memory_mib,
        }
        self.f.body["commit"] = "b" * 40
        for rollback in (False, True):
            with self.subTest(rollback=rollback):
                plan = self.plan(previous) if rollback else None
                self.f.calls.clear()
                _, operation = self.rollback(plan) if rollback else self.release()
                self.success(operation)
                actions = [action for action, _ in self.f.calls]
                stopped = actions.index("app.stop")
                submitted = actions.index("app.deploy", stopped + 1)
                self.assertEqual(
                    [a for a in actions[stopped + 1 : submitted] if a.startswith("app.worker.")],
                    ["app.worker.capacity"],
                )
                self.assertEqual(operation.refs["allocation"], allocation)
                self.assertEqual(actions.count("app.worker.capacity"), 3)
                self.assertNotIn("app.worker.observe", actions)
                self.assertNotIn("app.promote", actions)
                self.assertEqual(actions.count("app.deploy"), 1)
                job = next(iter(self.f.jobs.values()))
                self.assertNotIn("-preview", job)
                self.assertIn("force_pull      = false", job)
                self.assertTrue(operation.refs["direct_reuse"])
                self.no_worker_mutations()

    def test_replacement_after_reuse_selects_a_different_worker_not_the_job_named_slot(self):
        self.initial()
        self.success(self.f.deploy()[1])
        self.success(self.release()[1])
        previous = db.get_application(self.connection, self.app_id)
        previous_job = copy.deepcopy(self.f.jobs)
        self.f.fail_health = True
        _, rejected = self.f.deploy()
        self.assertEqual(rejected.status, "failed", rejected.safe_error)
        self.assertEqual(self.f.jobs, previous_job)
        self.assertEqual(db.get_application(self.connection, self.app_id), previous)
        self.assertEqual(len(self.f.workers), 1)
        self.f.fail_health = False
        self.success(self.f.deploy()[1])
        current = db.get_application(self.connection, self.app_id)
        self.assertNotEqual(current.worker_server_id, previous.worker_server_id)
        self.assertEqual(len(self.f.workers), 1)
        self.success(self.release()[1])
        previous = db.get_application(self.connection, self.app_id)
        self.success(self.f.resize(self.f.plan())[1])
        self.assertNotEqual(
            db.get_application(self.connection, self.app_id).worker_server_id,
            previous.worker_server_id,
        )
        self.assertEqual(len(self.f.workers), 1)

    def test_first_deployment_health_failure_cleans_up_its_owned_worker_once(self):
        self.f.fail_health = True
        _, operation = self.f.deploy()
        self.assertEqual(operation.status, "failed", operation.safe_error)
        self.assertEqual(self.f.jobs, {})
        self.assertEqual(self.f.workers, {})
        self.assertEqual(sum(action == "app.remove" for action, _ in self.f.calls), 1)
        self.assertEqual(sum(action == "app.worker.delete" for action, _ in self.f.calls), 1)

    def test_build_failure_leaves_old_job_and_worker_serving(self):
        self.initial()
        jobs, workers = copy.deepcopy(self.f.jobs), copy.deepcopy(self.f.workers)

        def fail(action, _values):
            if action == "app.build":
                self.assertEqual(self.f.jobs, jobs)
                raise app.ApplicationError("build failed")

        self.before = fail
        _, operation = self.release()
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(self.f.jobs, jobs)
        self.assertEqual(self.f.workers, workers)
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)
        self.assertFalse({"app.stop", "app.remove"} & {a for a, _ in self.f.calls})
        self.no_worker_mutations()

    def test_rejected_build_cleanup_does_not_touch_the_reused_worker(self):
        self.initial()
        jobs, workers = copy.deepcopy(self.f.jobs), copy.deepcopy(self.f.workers)

        def reject(action, _values):
            if action == "app.build":
                raise remote.HelperError("BUILD_REJECTED", "invalid application build")

        self.before = reject
        _, operation = self.release()
        self.assertEqual(operation.status, "failed")
        self.assertEqual(operation.cleanup_state, "confirmed")
        self.assertEqual(self.f.jobs, jobs)
        self.assertEqual(self.f.workers, workers)
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)
        self.no_worker_mutations()

    def test_capacity_or_identity_drift_after_build_never_stops_predecessor(self):
        self.initial()
        jobs = copy.deepcopy(self.f.jobs)
        originals = copy.deepcopy(self.f.workers)
        for field in ("imageId", "portId", "serverId", "flavorName", "capacity"):
            with self.subTest(field=field):

                def drift(action, _values, field=field):
                    if action == "app.build":
                        if field == "capacity":
                            self.f.capacity_override = (100, 64)
                        else:
                            next(iter(self.f.workers.values()))[field] = str(uuid.uuid4())

                self.after = drift
                key, operation = self.release()
                self.assertEqual(operation.status, "recovery_required")
                self.assertEqual(self.f.jobs, jobs)
                self.assertFalse(any(a == "app.stop" for a, _ in self.f.calls))
                self.f.workers = copy.deepcopy(originals)
                self.f.capacity_override = None
                self.after = lambda _action, _values: None
                self.success(self.release(key=key)[1])
                jobs = copy.deepcopy(self.f.jobs)
                self.f.calls.clear()
        self.no_worker_mutations()

    def test_capacity_drop_after_stop_still_blocks_reuse_and_can_recover(self):
        previous = self.initial()
        workers = copy.deepcopy(self.f.workers)

        def drop_capacity(action, _values):
            if action == "app.stop":
                self.f.capacity_override = (100, 64)

        self.after = drop_capacity
        key, operation = self.release()
        self.assertEqual(operation.status, "recovery_required")
        self.assertTrue(operation.refs["maintenance_stopped"])
        self.assertNotIn("app.deploy", [action for action, _ in self.f.calls])
        self.assertEqual(self.f.workers, workers)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, previous
        )
        self.after = lambda _action, _values: None
        self.f.capacity_override = None
        self.success(self.release(key=key)[1])
        self.assertEqual(self.f.workers, workers)
        self.no_worker_mutations()

    def test_failed_start_preserves_worker_and_can_restore_current_accepted_digest(self):
        previous = self.initial()
        workers = copy.deepcopy(self.f.workers)
        self.f.body["commit"] = "e" * 40
        self.f.fail_health = True
        _, operation = self.release()
        self.assertEqual(operation.status, "failed", operation.safe_error)
        self.assertEqual(operation.cleanup_state, "confirmed")
        self.assertEqual(self.f.jobs, {})
        self.assertEqual(self.f.workers, workers)
        self.assertFalse(db.get_application(self.connection, self.app_id).desired_running)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, previous
        )
        self.no_worker_mutations()
        self.f.fail_health = False
        plan = self.plan(previous)
        self.f.calls.clear()
        key, restored = self.rollback(plan)
        self.success(restored)
        self.assertEqual(self.f.workers, workers)
        self.assertEqual(
            db.get_deployment_attempt(self.connection, key).image_digest,
            db.get_deployment_attempt(self.connection, previous).image_digest,
        )
        self.assertFalse(
            {"app.build", "app.builder.delete", "app.manifest.delete"}
            & {a for a, _ in self.f.calls}
        )
        self.no_worker_mutations()

    def test_rollback_plan_budget_covers_sequential_checks_and_respects_policy(self):
        previous = self.initial()
        self.success(self.release()[1])
        for reuse, limit, budget, succeeds in (
            (True, 300, 120, True),
            (True, 75, 75, True),
            (True, 45, 45, True),
            (True, 30, 30, False),
            (False, 300, 30, True),
            (False, 20, 20, True),
        ):
            with self.subTest(reuse=reuse, limit=limit):
                clock = [100.0]
                deadlines = []
                self.f.calls.clear()
                config = replace(
                    self.f.config,
                    policy=replace(
                        self.f.config.policy,
                        limits=replace(self.f.config.policy.limits, process_seconds=limit),
                    ),
                )

                def slow_read(
                    config, action, values, *, deadline, clock=clock, deadlines=deadlines
                ):
                    deadlines.append(deadline)
                    clock[0] += {
                        "app.worker.observe": 27,
                        "app.worker.capacity": 27,
                        "app.manifest.verify": 6,
                    }[action]
                    if clock[0] >= deadline:
                        raise TimeoutError("read-only preflight budget exhausted")
                    return self.helper(config, action, values, deadline=deadline)

                service = DeploymentService(
                    self.connection, config, self.f.root, helper_caller=slow_read
                )
                with mock.patch(
                    "openstack_platform.controller.deployment_service.time.monotonic",
                    side_effect=lambda clock=clock: clock[0],
                ):
                    if succeeds:
                        plan = service.rollback_plan(self.app_id, previous, reuse_worker=reuse)
                        self.assertEqual(plan.get("reuseWorker", False), reuse)
                    else:
                        with self.assertRaises(TimeoutError):
                            service.rollback_plan(self.app_id, previous, reuse_worker=reuse)
                self.assertTrue(deadlines)
                self.assertEqual(set(deadlines), {100.0 + budget})
                self.no_worker_mutations()

    def test_rollback_old_version_uses_same_worker_and_current_secrets(self):
        previous = self.initial()
        self.f.body["commit"] = "b" * 40
        self.success(self.release()[1])
        workers = copy.deepcopy(self.f.workers)
        self.fixture.variables.items["nomad/jobs/commons"]["API_TOKEN"] = fixtures.SECRET
        plan = self.plan(previous)
        self.assertTrue(plan["reuseWorker"])
        self.f.calls.clear()
        self.success(self.rollback(plan)[1])
        self.assertEqual(self.f.workers, workers)
        job_id = next(iter(self.f.jobs))
        self.assertEqual(
            self.fixture.variables.items[f"nomad/jobs/{job_id}"]["API_TOKEN"], fixtures.SECRET
        )
        self.assertNotIn("app.build", [a for a, _ in self.f.calls])
        self.no_worker_mutations()

    def test_lost_stop_remove_and_submit_responses_recover_without_duplicate_workers(self):
        self.initial()
        workers = copy.deepcopy(self.f.workers)
        for action_to_fail in ("app.stop", "app.remove", "app.deploy"):
            with self.subTest(action=action_to_fail):

                def lose(action, _values, action_to_fail=action_to_fail):
                    if action == action_to_fail:
                        self.after = lambda _action, _values: None
                        raise app.ApplicationError("lost response after mutation")

                self.after = lose
                body = self.body()
                key, operation = self.release(body)
                self.assertEqual(operation.status, "recovery_required", operation.safe_error)
                self.restart()
                self.success(self.release(body, key)[1])
                self.assertEqual(self.f.workers, workers)
                self.assertEqual(len(self.f.jobs), 1)
        self.no_worker_mutations()

    def test_direct_route_requires_explicit_mode_and_all_durable_stop_evidence(self):
        proof = dict.fromkeys(
            (
                "direct_reuse",
                "reuse_worker",
                "maintenance",
                "maintenance_quiesced",
                "maintenance_stopped",
            ),
            True,
        )
        self.assertTrue(worker_reuse.direct_route(proof))
        self.assertFalse(worker_reuse.direct_route({}))
        for field in proof:
            for value in (False, None, "true", 1):
                with self.subTest(field=field, value=value):
                    invalid = {**proof, field: value}
                    if field == "direct_reuse" and value is False:
                        self.assertFalse(worker_reuse.direct_route(invalid))
                    else:
                        with self.assertRaises(app.ApplicationError):
                            worker_reuse.direct_route(invalid)

    def test_old_inflight_promoted_job_keeps_its_rendering_during_recovery(self):
        self.initial()

        def lose(action, _values):
            if action == "app.promote":
                self.after = lambda _action, _values: None
                raise app.ApplicationError("lost legacy promotion response")

        self.after = lose
        with mock.patch.object(worker_reuse, "direct_route", return_value=False):
            key, operation = self.release()
        self.assertEqual(operation.status, "recovery_required")
        # Reproduce a pre-upgrade journal: its jobs are staged/promoted and the
        # new rendering selector was never recorded. No live-state repair.
        refs = dict(operation.refs)
        del refs["direct_reuse"]
        db.checkpoint_operation(self.connection, key, phase=operation.phase, refs=refs)
        self.restart()
        self.success(self.release(key=key)[1])
        job = next(iter(self.f.jobs.values()))
        self.assertIn("-preview", job)
        self.assertIn("force_pull      = true", job)
        self.no_worker_mutations()

    def test_old_healthy_checkpoint_preserves_its_generated_job_on_upgrade(self):
        self.initial()
        original = db.checkpoint_deployment_attempt

        def fail_accept(*args, **kwargs):
            if kwargs.get("status") == "succeeded":
                raise db.DatabaseError("legacy acceptance interrupted")
            return original(*args, **kwargs)

        with (
            mock.patch.object(worker_reuse, "direct_route", return_value=False),
            mock.patch.object(db, "checkpoint_deployment_attempt", side_effect=fail_accept),
        ):
            key, operation = self.release()
        self.assertEqual(operation.phase, "deployment_healthy")
        jobs = copy.deepcopy(self.f.jobs)
        refs = dict(operation.refs)
        del refs["direct_reuse"]
        db.checkpoint_operation(self.connection, key, phase=operation.phase, refs=refs)
        self.restart()
        self.f.calls.clear()
        self.success(self.release(key=key)[1])
        self.assertEqual(self.f.jobs, jobs)
        self.assertNotIn("app.deploy", [a for a, _ in self.f.calls])
        self.no_worker_mutations()

    def test_direct_route_still_requires_canonical_public_health(self):
        self.initial()
        with mock.patch.object(app, "check_public_health", return_value=True) as health:
            key, operation = self.release()
        self.success(operation)
        self.assertGreaterEqual(health.call_count, 2)
        for call in health.call_args_list:
            self.assertFalse(call.kwargs.get("preview", False))
            self.assertEqual(call.kwargs["expected_marker"], key)
        self.no_worker_mutations()

    def restart(self):
        self.f.api.close()
        self.f.api = ControllerAPI(
            self.connection, self.f.config, self.f.root, helper_caller=self.helper
        )
        self.f.fixture.api = self.f.api
        self.f.router = self.f.api.router()

    def test_healthy_checkpoint_and_post_acceptance_recovery_keep_the_reused_worker(self):
        self.initial()
        workers = copy.deepcopy(self.f.workers)
        original = db.checkpoint_deployment_attempt

        def fail_accept(*args, **kwargs):
            if kwargs.get("status") == "succeeded":
                raise db.DatabaseError("acceptance interrupted")
            return original(*args, **kwargs)

        with mock.patch.object(db, "checkpoint_deployment_attempt", side_effect=fail_accept):
            key, operation = self.release()
        self.assertEqual(operation.phase, "deployment_healthy")
        self.assertEqual(operation.status, "recovery_required")
        self.restart()
        self.success(self.release(key=key)[1])
        self.f.fail_action = "app.manifest.retain"
        key, operation = self.release()
        self.assertEqual(operation.phase, "accepted")
        self.assertEqual(operation.status, "recovery_required")
        self.restart()
        self.success(self.release(key=key)[1])
        self.assertEqual(self.f.workers, workers)
        self.no_worker_mutations()

    def test_stop_checkpoint_before_purge_is_recoverable_and_never_infers_absence(self):
        self.initial()
        original = db.checkpoint_operation

        def fail_quiesced(*args, **kwargs):
            if kwargs.get("refs", {}).get("maintenance_quiesced"):
                raise db.DatabaseError("quiescence checkpoint interrupted")
            return original(*args, **kwargs)

        with mock.patch.object(db, "checkpoint_operation", side_effect=fail_quiesced):
            key, operation = self.release()
        self.assertEqual(operation.status, "recovery_required")
        self.assertNotIn("app.remove", [a for a, _ in self.f.calls])
        self.assertEqual(len(self.f.jobs), 1)  # Stopped evidence retained, not purged.
        self.success(self.release(key=key)[1])
        self.no_worker_mutations()

    def test_stop_state_write_interruption_recovers_with_worker_identity_intact(self):
        self.initial()
        workers = copy.deepcopy(self.f.workers)
        original = db.set_application_runtime
        for after_write in (False, True):
            with self.subTest(after_write=after_write):

                def fail(*args, after_write=after_write, **kwargs):
                    if not kwargs.get("running"):
                        if after_write:
                            original(*args, **kwargs)
                        raise db.DatabaseError("interrupted stopped-state write")
                    return original(*args, **kwargs)

                with mock.patch.object(db, "set_application_runtime", side_effect=fail):
                    key, operation = self.release()
                self.assertEqual(operation.status, "recovery_required")
                self.assertTrue(operation.refs["maintenance_stopped"])
                self.restart()
                self.success(self.release(key=key)[1])
                self.assertEqual(self.f.workers, workers)
        self.no_worker_mutations()

    def test_interrupted_rollback_cutover_does_not_clean_up_a_nonexistent_builder(self):
        previous = self.initial()
        self.success(self.release()[1])
        plan = self.plan(previous)

        def lose(action, _values):
            if action == "app.stop":
                self.after = lambda _action, _values: None
                raise app.ApplicationError("lost stop response")

        self.after = lose
        key, operation = self.rollback(plan)
        self.assertEqual(operation.status, "recovery_required")
        self.f.calls.clear()
        self.success(self.rollback(plan, key)[1])
        self.assertFalse({"app.build", "app.builder.delete"} & {a for a, _ in self.f.calls})
        self.no_worker_mutations()

    def test_uncertain_candidate_cleanup_never_deletes_worker_or_claims_terminal_failure(self):
        self.initial()
        self.f.fail_health = True

        def fail(action, values):
            if action == "app.stop" and values["jobId"] == "commons-candidate":
                raise app.ApplicationError("candidate process stop unconfirmed")

        self.before = fail
        key, operation = self.release()
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(len(self.f.workers), 1)
        self.assertIn("commons-candidate", self.f.jobs)
        self.before = lambda _action, _values: None
        self.f.fail_health = False
        self.success(self.release(key=key)[1])
        self.no_worker_mutations()

    def test_explicit_disable_still_deletes_worker_after_failed_reuse(self):
        self.initial()
        self.f.fail_health = True
        self.assertEqual(self.release()[1].status, "failed")
        self.assertEqual(len(self.f.workers), 1)
        self.success(self.f.post(f"/v1/applications/{self.app_id}/disable", {})[1])
        self.assertEqual(self.f.workers, {})
        self.assertIsNone(db.get_application(self.connection, self.app_id).worker_server_id)

    def test_reuse_rollback_plan_rejects_stale_worker_or_forged_mode(self):
        previous = self.initial()
        self.f.body["commit"] = "c" * 40
        self.success(self.release()[1])
        plan = self.plan(previous)
        for tampered in ({**plan, "reuseWorker": 1}, {**plan, "worker": {}}):
            self.f.calls.clear()
            _, operation = self.rollback(tampered)
            self.assertEqual(operation.status, "failed")
            self.assertEqual(self.f.calls, [])
        for query in ("1", "true&reuseWorker=true", "", "yes"):
            with self.assertRaises(HttpError):
                self.f.router.dispatch(
                    "GET",
                    self.base + f"/rollback-plan?deploymentId={previous}&reuseWorker={query}",
                    {},
                    None,
                )
