from __future__ import annotations

import copy
import hashlib
import json
import unittest
from contextlib import closing
from unittest import mock

from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import public_ip_service
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.deployment_config import parse_configuration
from openstack_platform.controller.http import HttpError
from openstack_platform.controller.storage_contract import canonical_secret_key
from openstack_platform.helper.application_actions import (
    _synchronize_workload_variable,
    variable_path,
)
from openstack_platform.helper.nomad import SecretItems, VariableSnapshot
from tests import test_application_sizing as fixtures
from tests import test_public_ip as ip_fixtures

SECRET = "sentinel-current-secret-never-return"


class Variables:
    def __init__(self):
        self.items = {variable_path("commons"): {"API_TOKEN": "old-secret"}}
        self.index = 1

    def read_variable(self, path):
        return VariableSnapshot(
            path, self.index if path in self.items else 0, SecretItems(self.items.get(path, {}))
        )

    def compare_and_set(self, path, expected_index, items):
        if expected_index != self.read_variable(path).modify_index:
            raise AssertionError("unexpected CAS index")
        self.items[path] = dict(items)
        self.index += 1
        return self.index


class RetainedRollbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ApplicationSizingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.connection = self.fixture.connection
        self.app_id = self.fixture.app_id
        self.available = True
        self.variables = Variables()
        self.fixture.api.helper_caller = self.helper

    def helper(self, config, action, values, **kwargs):
        if action in {"app.manifest.verify", "app.env.list", "app.build"}:
            self.fixture.calls.append((action, copy.deepcopy(values)))
            if action == "app.manifest.verify":
                return {**values, "available": self.available}
            if action == "app.env.list":
                return {"keys": sorted(self.variables.items[variable_path("commons")])}
            with closing(db.connect(self.fixture.root / "platform.sqlite3")) as connection:
                resources = {
                    item.resource_id: (item.resource_type, item.resource_name)
                    for item in db.list_managed_resources(connection, application_id=self.app_id)
                    if item.lifecycle_state == "active"
                }
            manifest = parse_configuration(values["configuration"]).manifest(resources)
            return {
                "image": "storage.internal:5000/projects/commons/app@sha256:"
                + hashlib.sha256(values["commit"].encode()).hexdigest(),
                "recipeHash": app.generate_recipe(manifest, config.policy.runtime_images).sha256,
                "builderAbsent": True,
                "log": "built\n",
            }
        if action == "app.env.set":
            self.variables.items[variable_path("commons")].update(values["updates"])
        if action in {"app.deploy", "app.promote"}:
            _synchronize_workload_variable(
                self.variables, "commons", app.nomad_job_id(values["job"], "commons")
            )
        return self.fixture.helper(config, action, values, **kwargs)

    def history(self):
        first, operation = self.fixture.deploy()
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.fixture.body = copy.deepcopy(self.fixture.body)
        self.fixture.body.update(
            repository="https://github.com/other/new", commit="b" * 40, configurationRevision=2
        )
        self.fixture.body["configuration"]["runtime"] = {"port": 9090, "healthPath": "/new-ready"}
        second, operation = self.fixture.deploy()
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.variables.items[variable_path("commons")]["API_TOKEN"] = SECRET
        return first, second

    def plan(self, deployment_id):
        return self.fixture.router.dispatch(
            "GET",
            f"/v1/admin/applications/{self.app_id}/rollback-plan?deploymentId={deployment_id}",
            {},
            None,
        ).body

    def apply(self, plan, key=None, confirmation="commons"):
        return self.fixture.post(
            f"/v1/admin/applications/{self.app_id}/rollback",
            {"plan": plan, "confirmation": confirmation},
            key,
        )

    def test_rollback_restores_exact_artifact_configuration_repository_with_current_secrets_no_build(
        self,
    ):
        first, second = self.history()
        target = db.get_deployment_attempt(self.connection, first)
        old = db.get_deployment_attempt(self.connection, second)
        plan = self.plan(first)
        before = db.get_environment_revision(self.connection, self.app_id)
        calls = len(self.fixture.calls)
        key, operation = self.apply(plan)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        accepted = db.get_deployment_attempt(self.connection, key)
        self.assertEqual(db.get_active_deployment(self.connection, self.app_id).deployment_id, key)
        self.assertEqual(accepted.image_digest, target.image_digest)
        self.assertNotEqual(accepted.image_digest, old.image_digest)
        self.assertEqual(accepted.configuration, target.configuration)
        self.assertEqual(accepted.configuration_sha256, target.configuration_sha256)
        self.assertEqual(accepted.environment_revision, before.revision)
        self.assertEqual(db.get_environment_revision(self.connection, self.app_id), before)
        self.assertEqual(
            db.get_application(self.connection, self.app_id).repository_url,
            "https://github.com/o/r",
        )
        selected = self.fixture.calls[calls:]
        self.assertFalse(
            {
                "app.build",
                "app.env.set",
                "app.env.remove",
                "app.builder.delete",
                "app.manifest.delete",
            }
            & {action for action, _ in selected}
        )
        job = next(value["job"] for action, value in selected if action == "app.deploy")
        self.assertIn('PORT = "8080"', job)
        self.assertIn('(ne $key "PORT")', job)
        self.assertNotIn(SECRET, job)
        job_id = app.nomad_job_id(job, "commons")
        self.assertEqual(self.variables.items[variable_path(job_id)]["API_TOKEN"], SECRET)
        self.assertEqual(len(self.fixture.workers), 1)
        self.assertIsNone(public_ip_service.get(self.connection, self.app_id))
        calls = len(self.fixture.calls)
        self.apply(plan, key)
        self.assertEqual(len(self.fixture.calls), calls)
        with self.assertRaises(HttpError) as raised:
            self.apply({**plan, "fingerprint": "tampered"}, key)
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_unavailable_artifact_blocks_plan_and_apply_without_candidate_mutation(self):
        first, second = self.history()
        plan = self.plan(first)
        self.available = False
        with self.assertRaises(HttpError):
            self.plan(first)
        self.fixture.calls.clear()
        _, operation = self.apply(plan)
        self.assertEqual(operation.status, "failed")
        self.assertEqual([action for action, _ in self.fixture.calls], ["app.manifest.verify"])
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, second
        )
        self.assertEqual(len(self.fixture.workers), 1)

    def test_stale_plan_confirmation_and_disabled_application_fail_without_mutations(self):
        first, _ = self.history()
        plan = self.plan(first)
        for value, confirmation in (
            ({**plan, "imageDigest": "tampered"}, "commons"),
            ({**plan, "environmentRevision": False}, "commons"),
            ({**plan, "environmentRevision": float(plan["environmentRevision"])}, "commons"),
            (plan, "wrong"),
        ):
            self.fixture.calls.clear()
            _, operation = self.apply(value, confirmation=confirmation)
            self.assertEqual(operation.status, "failed")
            self.assertEqual(self.fixture.calls, [])
        self.fixture.deploy()
        self.fixture.calls.clear()
        _, operation = self.apply(plan)
        self.assertEqual(operation.status, "failed")
        self.assertEqual(self.fixture.calls, [])
        self.fixture.post(f"/v1/applications/{self.app_id}/disable", {})
        with self.assertRaises(HttpError):
            self.plan(first)

    def test_added_storage_dependency_rejects_historical_configuration_and_stale_plan(self):
        first, second = self.history()
        plan = self.plan(first)
        db.put_managed_resource(
            self.connection,
            application_id=self.app_id,
            resource_type="postgres",
            provider_name="commons",
            lifecycle_state="active",
        )
        with self.assertRaises(HttpError):
            self.plan(first)
        self.fixture.calls.clear()
        _, operation = self.apply(plan)
        self.assertEqual(operation.status, "failed")
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, second
        )

    def test_removed_storage_output_blocks_rollback_and_keeps_current_data(self):
        resource = db.put_managed_resource(
            self.connection,
            application_id=self.app_id,
            resource_type="postgres",
            provider_name="commons",
            lifecycle_state="active",
        )
        self.fixture.body["configuration"]["storageBindings"] = [
            {"resourceId": resource.resource_id, "outputs": {"url": "DATABASE_URL"}}
        ]
        key = canonical_secret_key("postgres", "default", "url")
        self.variables.items[variable_path("commons")][key] = SECRET
        first, second = self.history()
        plan = self.plan(first)
        self.variables.items[variable_path("commons")].pop(key)
        self.fixture.calls.clear()
        _, operation = self.apply(plan)
        self.assertEqual(operation.status, "failed")
        self.assertEqual([action for action, _ in self.fixture.calls], ["app.env.list"])
        self.assertEqual(db.get_managed_resource(self.connection, resource.resource_id), resource)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, second
        )

    def test_health_failure_preserves_previous_job_and_historical_artifact(self):
        first, second = self.history()
        plan = self.plan(first)
        prior = dict(self.fixture.jobs)
        self.fixture.fail_after_promotion = True
        self.fixture.calls.clear()
        _, operation = self.apply(plan)
        self.assertEqual(operation.status, "failed", operation.safe_error)
        self.assertEqual(operation.cleanup_state, "confirmed")
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, second
        )
        self.assertEqual(self.fixture.jobs, prior)
        self.assertEqual(len(self.fixture.workers), 1)
        self.assertFalse(any(action == "app.manifest.delete" for action, _ in self.fixture.calls))

    def test_interrupted_acceptance_is_atomic_and_retry_reobserves_without_rebuilding(self):
        first, second = self.history()
        plan = self.plan(first)
        original = db.checkpoint_deployment_attempt

        def fail_accept(*args, **kwargs):
            if kwargs.get("status") == "succeeded":
                raise db.DatabaseError("injected acceptance failure")
            return original(*args, **kwargs)

        with mock.patch.object(db, "checkpoint_deployment_attempt", side_effect=fail_accept):
            key, operation = self.apply(plan)
        self.assertEqual(operation.status, "recovery_required", operation.safe_error)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, second
        )
        self.assertEqual(len(self.fixture.workers), 2)
        self.fixture.calls.clear()
        _, operation = self.apply(plan, key)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertTrue(any(action == "app.health" for action, _ in self.fixture.calls))
        self.assertFalse(
            any(action in {"app.build", "app.worker.create"} for action, _ in self.fixture.calls)
        )
        self.assertEqual(len(self.fixture.workers), 1)

    def test_rollback_pins_only_worker_image_and_retains_it_across_selection_rollover(self):
        first, _ = self.history()
        plan = self.plan(first)
        selected = db.get_image_selection(self.connection, "worker")
        self.fixture.fail_action = "app.worker.create"
        self.fixture.calls.clear()
        key, interrupted = self.apply(plan)
        self.assertEqual(interrupted.status, "recovery_required", interrupted.safe_error)
        self.assertEqual(interrupted.refs["worker_image_id"], selected.image_id)
        self.assertNotIn("builder_image_id", interrupted.refs)
        db.put_image_selection(
            self.connection,
            role="worker",
            image_id="22222222-2222-4222-8222-222222222222",
            display_name="replacement-worker",
            source_commit="d" * 40,
            compatibility_hash="e" * 64,
        )
        _, recovered = self.apply(plan, key)
        self.assertEqual(recovered.status, "succeeded", recovered.safe_error)
        self.assertEqual(recovered.refs["worker_image_id"], selected.image_id)
        creates = [value for action, value in self.fixture.calls if action == "app.worker.create"]
        self.assertTrue(creates)
        self.assertTrue(all(value["workerImageId"] == selected.image_id for value in creates))
        self.assertFalse(any(action == "app.build" for action, _ in self.fixture.calls))
        self.assertEqual(len(self.fixture.workers), 1)

    def test_post_acceptance_restart_retry_finishes_cleanup_with_original_plan(self):
        first, _ = self.history()
        plan = self.plan(first)
        self.fixture.fail_action = "app.worker.delete"
        key, operation = self.apply(plan)
        self.assertEqual(operation.status, "recovery_required", operation.safe_error)
        self.assertEqual(db.get_active_deployment(self.connection, self.app_id).deployment_id, key)
        self.fixture.api.close()
        db.renew_operation_deadline(self.connection, key, operation.deadline_at)
        db.set_operation_dispatch_status(self.connection, key, "running")
        self.fixture.api = ControllerAPI(
            self.connection, self.fixture.config, self.fixture.root, helper_caller=self.helper
        )
        self.fixture.fixture.api = self.fixture.api
        self.fixture.router = self.fixture.api.router()
        self.fixture.calls.clear()
        _, operation = self.apply(plan, key)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertFalse(
            any(action in {"app.build", "app.worker.create"} for action, _ in self.fixture.calls)
        )
        self.assertEqual(len(self.fixture.workers), 1)

    def test_reads_are_authoritative_secret_free_and_rollback_is_staff_only(self):
        first, second = self.history()
        for path in (f"/v1/deployments/{first}",):
            result = self.fixture.router.dispatch("GET", path, {}, None).body
            self.assertEqual(result["sourceRepository"], "https://github.com/o/r")
            target = db.get_deployment_attempt(self.connection, first)
            self.assertEqual(
                result["configuration"], json.loads(target.configuration.canonical_json())
            )
            self.assertEqual(result["configurationSha256"], target.configuration_sha256)
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertNotIn("environment", result)
        result = self.fixture.router.dispatch("GET", "/v1/admin/applications", {}, None).body
        self.assertEqual(result["items"][0]["activeDeploymentId"], second)
        with mock.patch(
            "openstack_platform.controller.api.status.application_observer", return_value=None
        ):
            result = self.fixture.router.dispatch(
                "GET", f"/v1/applications/{self.app_id}", {}, None
            ).body
        self.assertEqual(result["activeDeploymentId"], second)
        project = self.fixture.api.router("project")
        for method, suffix in (
            ("GET", "rollback-plan?deploymentId=" + first),
            ("POST", "rollback"),
        ):
            with self.assertRaises(HttpError) as raised:
                project.dispatch(method, f"/v1/admin/applications/{self.app_id}/{suffix}", {}, None)
            self.assertEqual(raised.exception.status, 404)

    def test_fip_handover_precedes_predecessor_cleanup_and_retries_forward(self):
        fixture = ip_fixtures.PublicIPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first, operation = fixture.fixture.deploy()
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        fixture.mutate("allocate")
        fixture.fixture.deploy()
        helper = fixture.fixture.api.helper_caller

        def verified(config, action, values, **kwargs):
            if action == "app.manifest.verify":
                return {**values, "available": True}
            return helper(config, action, values, **kwargs)

        fixture.fixture.api.helper_caller = verified
        path = f"/v1/admin/applications/{fixture.app_id}"
        plan = fixture.fixture.router.dispatch(
            "GET", path + "/rollback-plan?deploymentId=" + first, {}, None
        ).body
        old_workers = set(fixture.fixture.workers)
        fixture.cloud.fault, fixture.cloud.fault_after = "set", True
        body = {"plan": plan, "confirmation": "commons"}
        key, operation = fixture.fixture.post(path + "/rollback", body)
        self.assertEqual(operation.status, "recovery_required", operation.safe_error)
        self.assertEqual(
            db.get_active_deployment(fixture.connection, fixture.app_id).deployment_id, key
        )
        self.assertTrue(old_workers <= fixture.fixture.workers.keys())
        _, operation = fixture.fixture.post(path + "/rollback", body, key)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        accepted = db.get_application(fixture.connection, fixture.app_id)
        self.assertEqual(fixture.cloud.fips[ip_fixtures.FIP]["port_id"], accepted.worker_port_id)
        self.assertEqual(len(fixture.fixture.workers), 1)
