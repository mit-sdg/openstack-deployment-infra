from __future__ import annotations

import copy
import json
import unittest
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from openstack_platform import openstack
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import sizing
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.deployment_config import parse_configuration
from openstack_platform.controller.http import HttpError
from openstack_platform.controller.storage_contract import PLATFORM_ENVIRONMENT_KEYS
from openstack_platform.helper.worker_capacity import observe_capacity
from openstack_platform.validation import ValidationError, flavor_reference
from tests import test_controller_api as api_fixtures

XL = openstack.Flavor("4200", "xl.4core", 4, 16384, 64)
SMALL = openstack.Flavor("100", "worker-small", 1, 2048, 20)


class ApplicationSizingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = api_fixtures.ControllerAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.api.close()
        self.connection = self.fixture.connection
        self.root = self.fixture.root
        self.config = replace(
            self.fixture.config,
            platform=replace(
                self.fixture.config.platform,
                document={
                    "paths": {"root": "/srv/openstack-platform"},
                    "addresses": {"storage": "storage.internal"},
                },
            ),
        )
        self.calls = []
        self.workers = {}
        self.jobs = {}
        self.fail_health = False
        self.fail_after_promotion = False
        self.fail_action = None
        self.capacity_override = None
        self.api = ControllerAPI(self.connection, self.config, self.root, helper_caller=self.helper)
        self.fixture.api = self.api
        self.router = self.api.router()
        self.app_id = str(uuid.uuid4())
        self.router.dispatch(
            "POST", "/v1/applications", {"Idempotency-Key": self.app_id}, {"slug": "commons"}
        )
        self.body = {
            "repository": "https://github.com/o/r",
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
                "runtime": {"port": 8080, "healthPath": "/ready"},
                "storageBindings": [],
            },
        }
        for role in ("worker", "builder"):
            db.put_image_selection(
                self.connection,
                role=role,
                image_id=str(uuid.uuid4()),
                display_name=role,
                source_commit="a" * 40,
                compatibility_hash="b" * 64,
            )
        for patch in (
            mock.patch.object(openstack, "verify_project"),
            mock.patch.object(
                openstack, "observe_flavor", side_effect=lambda _p, ref, **kw: self.flavor(ref).name
            ),
            mock.patch.object(
                openstack,
                "observe_flavor_capacity",
                side_effect=lambda _p, ref, **kw: self.flavor(ref),
            ),
            mock.patch.object(app, "check_public_health", return_value=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def flavor(self, ref):
        return XL if ref in ("4200", "xl.4core") else SMALL

    def helper(self, config, action, values, **kwargs):
        self.calls.append((action, copy.deepcopy(values)))
        if action == self.fail_action:
            self.fail_action = None
            raise app.ApplicationError("injected helper outage")
        if action == "app.build":
            manifest = parse_configuration(values["configuration"]).manifest({})
            return {
                "image": "storage.internal:5000/projects/commons/app@sha256:" + "c" * 64,
                "recipeHash": app.generate_recipe(manifest, config.policy.runtime_images).sha256,
                "builderAbsent": True,
                "log": "built\n",
            }
        if action == "app.env.set":
            return {
                "keys": sorted(PLATFORM_ENVIRONMENT_KEYS),
                "modifyIndex": 1,
                "restarted": bool(self.jobs),
                "schedulerHealthy": bool(self.jobs),
                "publicHealthy": bool(self.jobs),
            }
        if action == "app.worker.observe":
            return self.workers.get(values["applicationId"], {"absent": True})
        if action == "app.worker.create":
            worker = {
                "ready": True,
                "serverId": str(uuid.uuid4()),
                "portId": str(uuid.uuid4()),
                "serverName": "test-worker",
                "portName": "test-worker-v4",
                "flavorName": values["standardFlavor"],
            }
            self.workers[values["applicationId"]] = worker
            return worker
        if action == "app.worker.capacity":
            worker = self.workers[values["applicationId"]]
            budget = (
                sizing.capacity_budget(10000, 16000)
                if worker["flavorName"] == XL.name
                else (1800, 1536)
            )
            cpu, ram = self.capacity_override or budget
            return {
                "serverId": worker["serverId"],
                "flavorName": worker["flavorName"],
                "cpuMHz": cpu,
                "memoryMiB": ram,
            }
        if action in {"app.deploy", "app.promote"}:
            if action == "app.promote" and self.fail_after_promotion:
                self.fail_health = True
            job = values["job"]
            job_id = app.nomad_job_id(job, "commons")
            self.jobs[job_id] = job
            identity = app.nomad_candidate_identity(job)
            return {
                "jobId": job_id,
                "nomadVersion": 1,
                "candidateJobSha256": identity[0],
                "candidateImage": identity[1],
            }
        if action == "app.health":
            return {
                "healthy": not self.fail_health,
                "terminal": self.fail_health,
                "version": 1,
                "currentVersion": 1,
                "allocations": 1,
                "candidateJobSha256": values["candidateJobSha256"],
                "candidateImage": values["candidateImage"],
            }
        if action == "app.remove":
            job_id = values.get("jobId", "commons")
            self.jobs.pop(job_id, None)
            return {"jobAbsent": True, "variableAbsent": True}
        if action == "app.worker.delete":
            self.workers.pop(values["applicationId"], None)
            return {"absent": True}
        if action in {"app.manifest.delete", "app.builder.delete"}:
            return {"absent": True}
        if action == "app.manifest.retain":
            return {"deleted": [], "protected": values["references"]}
        raise AssertionError((action, values))

    def post(self, path, body, key=None):
        key = key or str(uuid.uuid4())
        response = self.router.dispatch("POST", path, {"Idempotency-Key": key}, body)
        self.assertEqual(response.status, 202)
        self.api.wait_for_operations()
        return key, db.get_operation(self.connection, key)

    def deploy(self, plan=None):
        prefix = "/v1/applications" if plan is None else "/v1/admin/applications"
        body = self.body if plan is None else {**self.body, "plan": plan}
        return self.post(f"{prefix}/{self.app_id}/deployments", body)

    def plan(self):
        response = self.router.dispatch(
            "GET", f"/v1/admin/applications/{self.app_id}/resize-plan?flavor=4200", {}, None
        )
        self.assertEqual(response.status, 200)
        return response.body

    def resize(self, plan, key=None):
        return self.post(
            f"/v1/admin/applications/{self.app_id}/resize",
            {"plan": plan, "confirmation": "commons"},
            key,
        )

    def test_resize_reuses_artifact_accepts_large_budget_and_persists_on_redeploy(self):
        _, first = self.deploy()
        self.assertEqual(first.status, "succeeded", first.safe_error)
        before = db.get_application(self.connection, self.app_id)
        self.assertEqual(
            (before.worker_flavor, before.scheduler_cpu_mhz, before.scheduler_memory_mib),
            ("worker-small", 500, 512),
        )
        plan = self.plan()
        self.assertEqual(plan["flavor"], sizing.flavor_projection(XL))
        calls = len(self.calls)
        key, operation = self.resize(plan)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        resize_calls = self.calls[calls:]
        self.assertNotIn("app.build", [action for action, _ in resize_calls])
        self.assertNotIn("app.env.set", [action for action, _ in resize_calls])
        created = next(values for action, values in resize_calls if action == "app.worker.create")
        self.assertEqual(created["flavorId"], "4200")
        self.assertTrue(any(action == "app.promote" for action, _ in resize_calls))
        accepted = db.get_application(self.connection, self.app_id)
        self.assertEqual(
            (accepted.worker_flavor, accepted.scheduler_cpu_mhz, accepted.scheduler_memory_mib),
            ("xl.4core", 9000, 14400),
        )
        deployment = db.get_deployment(self.connection, self.app_id)
        self.assertRegex(deployment.nomad_job, r"cpu\s*=\s*9000")
        call_count = len(self.calls)
        self.resize(plan, key)
        self.assertEqual(len(self.calls), call_count)
        # Fresh normal deployment keeps the accepted size, not policy defaults
        # or newly available excess capacity on a different hypervisor.
        self.capacity_override = (11000, 14500)
        _, redeploy = self.deploy()
        self.assertEqual(redeploy.status, "succeeded", redeploy.safe_error)
        persisted = db.get_application(self.connection, self.app_id)
        self.assertEqual(
            (persisted.worker_flavor, persisted.scheduler_cpu_mhz, persisted.scheduler_memory_mib),
            ("xl.4core", 9000, 14400),
        )
        self.assertEqual(len(self.workers), 1)

    def test_custom_flavor_first_deployment_and_student_plan_is_not_public(self):
        plan = self.plan()
        self.assertIsNone(plan["deploymentId"])
        _, operation = self.deploy(plan)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertEqual(db.get_application(self.connection, self.app_id).worker_flavor, XL.name)
        project = self.api.router("project")
        for method, suffix in (
            ("GET", "resize-plan?flavor=4200"),
            ("POST", "resize"),
            ("POST", "deployments"),
        ):
            with self.assertRaises(HttpError) as error:
                project.dispatch(method, f"/v1/admin/applications/{self.app_id}/{suffix}", {}, None)
            self.assertEqual(error.exception.status, 404)
        with self.assertRaises(HttpError):
            self.router.dispatch(
                "POST",
                f"/v1/applications/{self.app_id}/deployments",
                {"Idempotency-Key": str(uuid.uuid4())},
                {**self.body, "plan": plan},
            )

    def test_failed_candidate_rolls_back_size_job_and_worker_without_deleting_reused_image(self):
        self.deploy()
        prior = db.get_application(self.connection, self.app_id)
        prior_deployment = db.get_active_deployment(self.connection, self.app_id)
        plan = self.plan()
        self.fail_health = True
        self.calls.clear()
        _, operation = self.resize(plan)
        self.assertEqual(operation.status, "failed", operation.safe_error)
        self.assertEqual(operation.cleanup_state, "confirmed")
        self.assertEqual(db.get_application(self.connection, self.app_id), prior)
        self.assertEqual(db.get_active_deployment(self.connection, self.app_id), prior_deployment)
        self.assertEqual(set(self.jobs), {"commons"})
        self.assertEqual(len(self.workers), 1)
        self.assertFalse(any(action == "app.manifest.delete" for action, _ in self.calls))
        self.fail_health = False
        _, retry = self.resize(plan)
        self.assertEqual(retry.status, "succeeded", retry.safe_error)

    def test_unavailable_capacity_retries_identical_intent_without_duplicate_worker(self):
        self.deploy()
        prior = db.get_application(self.connection, self.app_id)
        plan = self.plan()
        self.fail_action = "app.worker.capacity"
        key, operation = self.resize(plan)
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(db.get_application(self.connection, self.app_id), prior)
        self.assertEqual(len(self.workers), 2)
        with self.assertRaises(HttpError) as error:
            self.resize(plan)
        self.assertEqual(error.exception.status, 409)
        _, recovered = self.resize(plan, key)
        self.assertEqual(recovered.status, "succeeded", recovered.safe_error)
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(sum(action == "app.worker.create" for action, _ in self.calls), 2)
        with self.assertRaises(HttpError) as error:
            self.resize({**plan, "allocation": "tampered"}, key)
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_post_promotion_failure_removes_candidate_route_and_preserves_old_size(self):
        self.deploy()
        prior = db.get_application(self.connection, self.app_id)
        plan = self.plan()
        self.fail_after_promotion = True
        self.calls.clear()
        _, operation = self.resize(plan)
        self.assertEqual(operation.status, "failed", operation.safe_error)
        self.assertTrue(any(action == "app.promote" for action, _ in self.calls))
        self.assertEqual(db.get_application(self.connection, self.app_id), prior)
        self.assertEqual(set(self.jobs), {"commons"})
        self.assertEqual(len(self.workers), 1)

    def test_pinned_allocation_rejects_smaller_worker_and_recovers_without_reduction(self):
        self.deploy(self.plan())
        prior = db.get_application(self.connection, self.app_id)
        self.capacity_override = (8000, 12000)
        self.calls.clear()
        key, operation = self.deploy()
        self.assertEqual(operation.status, "recovery_required", operation.safe_error)
        self.assertFalse(any(action == "app.deploy" for action, _ in self.calls))
        self.assertEqual(db.get_application(self.connection, self.app_id), prior)
        self.capacity_override = (10000, 15000)
        _, recovered = self.post(f"/v1/applications/{self.app_id}/deployments", self.body, key)
        self.assertEqual(recovered.status, "succeeded", recovered.safe_error)
        current = db.get_application(self.connection, self.app_id)
        self.assertEqual((current.scheduler_cpu_mhz, current.scheduler_memory_mib), (9000, 14400))

    def test_invalid_confirmation_and_disabled_app_do_not_resize(self):
        self.deploy()
        plan = self.plan()
        self.calls.clear()
        _, rejected = self.post(
            f"/v1/admin/applications/{self.app_id}/resize", {"plan": plan, "confirmation": "wrong"}
        )
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(self.calls, [])
        _, disabled = self.post(f"/v1/applications/{self.app_id}/disable", {})
        self.assertEqual(disabled.status, "succeeded", disabled.safe_error)
        self.calls.clear()
        _, rejected = self.resize(plan)
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(self.calls, [])

    def test_stale_tampered_and_provider_drifted_plans_mutate_nothing(self):
        self.deploy()
        plan = self.plan()
        tampered = copy.deepcopy(plan)
        tampered["flavor"]["ram_mib"] += 1
        self.calls.clear()
        _, rejected = self.resize(tampered)
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(self.calls, [])
        with mock.patch.object(
            openstack, "observe_flavor_capacity", return_value=replace(XL, disk_gib=80)
        ):
            _, rejected = self.resize(plan)
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(self.calls, [])
        self.deploy()
        self.calls.clear()
        _, rejected = self.resize(plan)
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(self.calls, [])

    def test_post_acceptance_cleanup_interruption_recovers_pinned_size(self):
        self.deploy()
        plan = self.plan()
        self.fail_action = "app.worker.delete"
        key, interrupted = self.resize(plan)
        self.assertEqual(interrupted.status, "recovery_required", interrupted.safe_error)
        self.assertEqual(interrupted.phase, "deployment_healthy")
        accepted = db.get_application(self.connection, self.app_id)
        self.assertEqual(accepted.worker_flavor, XL.name)
        _, recovered = self.resize(plan, key)
        self.assertEqual(recovered.status, "succeeded", recovered.safe_error)
        self.assertEqual(db.get_application(self.connection, self.app_id).scheduler_cpu_mhz, 9000)
        self.assertEqual(len(self.workers), 1)

    def test_controller_restart_preserves_healthy_resize_checkpoint_for_recovery(self):
        self.deploy()
        plan = self.plan()
        self.fail_action = "app.worker.delete"
        key, interrupted = self.resize(plan)
        self.assertEqual(interrupted.phase, "deployment_healthy")
        self.api.close()
        # Model process loss before the executor's finally block records the
        # interruption: retain the real domain checkpoint, mark dispatch started.
        db.renew_operation_deadline(self.connection, key, interrupted.deadline_at)
        db.set_operation_dispatch_status(self.connection, key, "running")
        self.api = ControllerAPI(self.connection, self.config, self.root, helper_caller=self.helper)
        self.fixture.api = self.api
        self.router = self.api.router()
        operation = db.get_operation(self.connection, key)
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(operation.phase, "deployment_healthy")
        _, recovered = self.resize(plan, key)
        self.assertEqual(recovered.status, "succeeded", recovered.safe_error)
        self.assertEqual(db.get_application(self.connection, self.app_id).scheduler_cpu_mhz, 9000)

    def test_acceptance_database_failure_is_atomic_and_recoverable(self):
        self.deploy()
        plan = self.plan()
        prior = db.get_application(self.connection, self.app_id)
        original = db.checkpoint_deployment_attempt

        def fail_accept(*args, **kwargs):
            if kwargs.get("status") == "succeeded":
                raise db.DatabaseError("injected SQLite failure")
            return original(*args, **kwargs)

        with mock.patch.object(db, "checkpoint_deployment_attempt", side_effect=fail_accept):
            key, operation = self.resize(plan)
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(db.get_application(self.connection, self.app_id), prior)
        _, recovered = self.resize(plan, key)
        self.assertEqual(recovered.status, "succeeded", recovered.safe_error)


class FlavorCapacityTests(unittest.TestCase):
    def test_opaque_ids_and_invalid_arguments(self):
        for value in ("4200", "xl.4core", "opaque-ID_9", str(uuid.uuid4())):
            self.assertEqual(flavor_reference(value), value)
        for value in (4200, True, "", "--help", "a/b", "x\n", "x y", "$(id)", "x" * 256):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                flavor_reference(value)

    def test_real_numeric_flavor_capacity_and_name_resolution(self):
        fixture = api_fixtures.ControllerAPITests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        platform = fixture.config.platform
        calls = []
        payload = {"id": "4200", "name": "xl.4core", "vcpus": 4, "ram": 16384, "disk": 64}

        def runner(argv, **kwargs):
            calls.append(argv)
            value = (
                {"project_id": platform.project_id} if argv[1:3] == ("token", "issue") else payload
            )
            return SimpleNamespace(
                stdout=json.dumps(value).encode(), stdout_truncated=False, stderr_truncated=False
            )

        for ref in ("4200", "xl.4core"):
            self.assertEqual(
                openstack.observe_flavor_capacity(platform, ref, command_runner=runner), XL
            )
            self.assertEqual(
                openstack.observe_flavor(platform, ref, command_runner=runner), XL.name
            )
        for field, invalid in (("id", "--bad"), ("vcpus", True), ("ram", -1), ("disk", None)):
            original = payload[field]
            payload[field] = invalid
            with self.assertRaises(openstack.OpenStackError):
                openstack.observe_flavor_capacity(platform, "4200", command_runner=runner)
            payload[field] = original
        count = len(calls)
        with self.assertRaises(ValidationError):
            openstack.observe_flavor_capacity(platform, "--help", command_runner=runner)
        self.assertEqual(len(calls), count)

    def test_capacity_uses_measured_nomad_cpu_ram_and_larger_reported_reserves(self):
        self.assertEqual(sizing.capacity_budget(10000, 16000), (9000, 14400))
        self.assertEqual(sizing.capacity_budget(2000, 2048), (1800, 1536))
        self.assertEqual(sizing.capacity_budget(10000, 16000, 1500, 3000), (8500, 13000))
        for values in ((True, 2048), (2000, "2048"), (200, 512), (2000, 2048, -1)):
            with self.assertRaises(ValidationError):
                sizing.capacity_budget(*values)

    def test_nomad_capacity_is_bound_to_ready_owned_node(self):
        platform = SimpleNamespace(namespace="test")
        app_id, node_id = str(uuid.uuid4()), str(uuid.uuid4())
        node = {
            "ID": node_id,
            "Name": "worker",
            "Status": "ready",
            "Drain": False,
            "SchedulingEligibility": "eligible",
            "NodeClass": "test-app",
            "Meta": {
                "application_id": app_id,
                "application_slug": "commons",
                "managed_by": "test-platform",
            },
            "Drivers": {"docker": {"Detected": True, "Healthy": True}},
            "NodeResources": {"Cpu": {"CpuShares": 10000}, "Memory": {"MemoryMB": 16000}},
            "ReservedResources": {"Cpu": {"CpuShares": 200}, "Memory": {"MemoryMB": 512}},
        }

        def runner(argv, **kwargs):
            value = node if argv[-1] == node_id else [{"ID": node_id, "Name": "worker"}]
            return SimpleNamespace(
                stdout=json.dumps(value).encode(), stdout_truncated=False, stderr_truncated=False
            )

        budget = observe_capacity(
            platform,
            app_id,
            "commons",
            "worker",
            nomad_command="/fixed/nomad",
            command_runner=runner,
        )
        self.assertEqual((budget["cpuMHz"], budget["memoryMiB"]), (9000, 14400))
        node["Meta"]["application_id"] = str(uuid.uuid4())
        with self.assertRaises(ValidationError):
            observe_capacity(
                platform,
                app_id,
                "commons",
                "worker",
                nomad_command="/fixed/nomad",
                command_runner=runner,
            )
