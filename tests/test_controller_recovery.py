from __future__ import annotations

from unittest import TestCase, mock

from openstack_platform import remote
from openstack_platform.controller import database as db
from openstack_platform.controller import storage
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.http import HttpError
from tests import test_controller_api as fixtures


class ControllerRecoveryTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.ControllerAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.connection = self.fixture.connection
        self.identifier = self.fixture.create_application().body["applicationId"]
        self.application = db.get_application(self.connection, self.identifier)
        self.key = "00000000-0000-4000-8000-000000000040"
        self.other_key = "00000000-0000-4000-8000-000000000041"
        self.project_patch = mock.patch("openstack_platform.openstack.verify_project")
        self.project_patch.start()
        self.addCleanup(self.project_patch.stop)
        self.calls = []
        self.reject = True
        self.cleanup = True
        self.generic_failure = False
        self.fixture.api.helper_caller = self.helper
        for role in ("builder", "worker"):
            db.put_image_selection(
                self.connection,
                role=role,
                image_id=self.other_key,
                display_name=role,
                source_commit="b" * 40,
                compatibility_hash="c" * 64,
            )

    def helper(self, _config, action, values, **kwargs):
        self.calls.append(action)
        if action == "app.build":
            raise remote.HelperError(
                "PROVIDER_UNAVAILABLE" if self.generic_failure else "BUILD_REJECTED",
                "fixed build failure",
            )
        if action == "app.build.cleanup":
            if not self.cleanup:
                raise remote.HelperError("CLEANUP_UNCONFIRMED", "fixed absence failure")
            return {**values, "builderAbsent": True, "artifactAbsent": True}
        if action == "app.builder.delete":
            return {"absent": True}
        if action.endswith(".create") and action.startswith("storage."):
            if self.reject:
                raise remote.HelperError("PROVIDER_UNAVAILABLE", "fixed storage ambiguity")
            resource_type = action.split(".")[1]
            name = storage._provider_name(
                self.fixture.config, self.application, resource_type, "default"
            )
            return {
                "providerId": name,
                "providerName": name,
                "verified": True,
                "evidenceAccepted": True,
                "modifyIndex": 1,
            }
        if action.startswith("storage.") and action.endswith(".remove"):
            if values["preflight"]:
                return {"preflightAccepted": True}
            return {"confirmedAbsent": True, "environmentRemoved": True}
        if action == "app.remove":
            return {"jobAbsent": True, "variableAbsent": True}
        if action == "app.worker.delete":
            return {"absent": True}
        raise AssertionError(action)

    def deploy(self, *, key=None, commit="a" * 40):
        return self.fixture.api.router("project").dispatch(
            "POST",
            f"/v1/applications/{self.identifier}/deployments",
            self.fixture.headers(key or self.key),
            {
                "repository": "https://github.com/example/app",
                "commit": commit,
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
                    "runtime": {"port": 8080, "healthPath": "/"},
                    "storageBindings": [],
                },
            },
        )

    def wait(self, response):
        self.fixture.api.wait_for_operations()
        return db.get_operation(self.connection, response.body["operationId"])

    def restart(self):
        self.fixture.api.close()
        self.fixture.api = ControllerAPI(
            self.connection, self.fixture.config, self.fixture.root, helper_caller=self.helper
        )
        self.fixture.router = self.fixture.api.router()

    def test_storage_retry_runs_the_same_domain_kind_for_each_resource_type(self):
        for index, resource_type in enumerate(("postgres", "mongo", "s3")):
            path = f"/v1/applications/{self.identifier}/storage"
            self.key = f"00000000-0000-4000-8000-{40 + index:012d}"
            self.reject = True
            first = self.fixture.dispatch(
                "POST", path, {"type": resource_type}, self.fixture.headers(self.key)
            )
            self.assertEqual(self.wait(first).status, "recovery_required")
            self.assertEqual(db.get_operation(self.connection, self.key).kind, "storage.create")
            with self.assertRaises(HttpError) as conflict:
                self.fixture.dispatch(
                    "POST",
                    path,
                    {"type": resource_type},
                    self.fixture.headers(f"00000000-0000-4000-8000-{80 + index:012d}"),
                )
            self.assertEqual(conflict.exception.code, "OPERATION_CONFLICT")
            self.restart()
            self.reject = False
            retried = self.fixture.dispatch(
                "POST", path, {"type": resource_type}, self.fixture.headers(self.key)
            )
            self.assertEqual(self.wait(retried).status, "succeeded")
            self.assertEqual(
                db.get_operation_dispatch(self.connection, self.key).kind, "storage.create"
            )

    def test_storage_recovery_requires_exact_dispatch_and_domain_kind(self):
        for index, (dispatch_kind, operation_kind, selected) in enumerate(
            (
                ("storage.postgres.create", "storage.create", ["postgres"]),
                ("storage.mongo.create", "storage.create", ["postgres"]),
                ("storage.postgres.create", "storage.rotate", ["postgres"]),
                ("storage.postgres.create", "storage.create", ["postgres", "mongo"]),
                ("app.deploy", "storage.create", ["postgres"]),
            )
        ):
            key = f"00000000-0000-4000-8000-{90 + index:012d}"
            scope = f"app-{key}"
            db.claim_idempotency_request(
                self.connection, request_id=key, request_fingerprint="a" * 64
            )
            db.enqueue_operation_dispatch(
                self.connection, operation_id=key, kind=dispatch_kind, scope=scope
            )
            db.begin_operation(
                self.connection,
                operation_id=key,
                kind=operation_kind,
                scope=scope,
                phase="validated",
                deadline_at="2030-01-01T00:00:00Z",
                refs={"selected": selected},
            )
            db.mark_recovery_required(self.connection, key, "fixed failure")
            db.set_operation_dispatch_status(self.connection, key, "recovery_required")
            saved_dispatch = db.get_operation_dispatch(self.connection, key)
            saved_operation = db.get_operation(self.connection, key)
            with self.assertRaises(db.DatabaseError):
                db.requeue_recovery_dispatch(
                    self.connection, operation_id=key, kind="storage.create", scope=scope
                )
            self.assertEqual(db.get_operation_dispatch(self.connection, key), saved_dispatch)
            self.assertEqual(db.get_operation(self.connection, key), saved_operation)

    def test_deterministic_rejection_releases_scope_only_after_exact_cleanup(self):
        response = self.deploy()
        operation = self.wait(response)
        self.assertEqual((operation.status, operation.cleanup_state), ("failed", "confirmed"))
        self.assertEqual(db.get_deployment_attempt(self.connection, self.key).status, "failed")
        self.assertEqual(self.calls, ["app.build", "app.build.cleanup"])
        self.wait(self.deploy())
        self.assertEqual(self.calls.count("app.build"), 1)
        # A different corrected request is admitted rather than stuck behind the old build.
        self.wait(self.deploy(key=self.other_key, commit="b" * 40))
        self.assertEqual(self.calls.count("app.build"), 2)

    def test_ambiguous_cleanup_retries_cleanup_only_across_restart(self):
        self.cleanup = False
        response = self.deploy()
        operation = self.wait(response)
        self.assertEqual(
            (operation.status, operation.phase), ("recovery_required", "build_rejected")
        )
        with self.assertRaises(HttpError):
            self.deploy(key=self.other_key, commit="b" * 40)
        self.restart()
        self.cleanup = True
        operation = self.wait(self.deploy())
        self.assertEqual(operation.status, "failed")
        self.assertEqual(self.calls.count("app.build"), 1)
        self.assertEqual(self.calls.count("app.build.cleanup"), 2)

    def test_wrong_cleanup_identity_or_inexact_flags_cannot_terminalize(self):
        original = self.helper
        for bad in (
            {"buildId": self.other_key},
            {"builderAbsent": False},
            {"artifactAbsent": False},
            {"artifactAbsent": 1},
        ):

            def helper(config, action, values, bad=bad, **kwargs):
                result = original(config, action, values, **kwargs)
                return {**result, **bad} if action == "app.build.cleanup" else result

            self.fixture.api.helper_caller = helper
            self.assertEqual(self.wait(self.deploy()).status, "recovery_required")
        self.fixture.api.helper_caller = original
        self.assertEqual(self.wait(self.deploy()).status, "failed")
        self.assertEqual(self.calls.count("app.build"), 1)

    def test_crash_between_attempt_and_operation_terminalization_is_resumable(self):
        with mock.patch.object(db, "mark_failed", side_effect=OSError("simulated crash")):
            self.assertEqual(self.wait(self.deploy()).status, "recovery_required")
        self.assertEqual(db.get_deployment_attempt(self.connection, self.key).status, "failed")
        self.restart()
        self.assertEqual(self.wait(self.deploy()).status, "failed")
        self.assertEqual(self.calls.count("app.build"), 1)

    def test_unknown_build_result_remains_recovery_required(self):
        self.generic_failure = True
        self.assertEqual(self.wait(self.deploy()).status, "recovery_required")
        self.assertNotIn("app.build.cleanup", self.calls)
        with self.assertRaises(HttpError):
            self.deploy(key=self.other_key, commit="b" * 40)

    def test_privileged_destructive_polling_and_replay_use_admin_route(self):
        self.reject = False
        created = self.fixture.dispatch(
            "POST",
            f"/v1/applications/{self.identifier}/storage",
            {"type": "postgres"},
            self.fixture.headers(self.key),
        )
        self.assertEqual(self.wait(created).status, "succeeded")
        resource = db.list_managed_resources(self.connection, application_id=self.identifier)[0]
        routes = (
            ("DELETE", f"/v1/storage/{resource.resource_id}", "default", self.other_key),
            (
                "POST",
                f"/v1/applications/{self.identifier}/delete",
                "demo-app",
                "00000000-0000-4000-8000-000000000043",
            ),
        )
        for method, path, confirmation, key in routes:
            for _ in range(2):
                response = self.fixture.api.router("privileged").dispatch(
                    method, path, self.fixture.headers(key), {"confirmation": confirmation}
                )
                self.wait(response)
                self.assertEqual(response.body["statusUrl"], f"/v1/admin/operations/{key}")
                polled = self.fixture.api.router("privileged").dispatch(
                    "GET", response.body["statusUrl"], {}, None
                )
                self.assertEqual(polled.body["status"], "succeeded")

    def test_restart_without_domain_intent_finishes_unstarted_dispatch(self):
        for index, status in enumerate(("pending", "running")):
            key = f"00000000-0000-4000-8000-{50 + index:012d}"
            db.claim_idempotency_request(
                self.connection, request_id=key, request_fingerprint="a" * 64
            )
            db.enqueue_operation_dispatch(
                self.connection, operation_id=key, kind="app.deploy", scope=f"app-{self.identifier}"
            )
            if status == "running":
                db.set_operation_dispatch_status(self.connection, key, "running")
            self.restart()
            self.assertEqual(db.get_operation(self.connection, key).status, "failed")
            self.assertEqual(db.get_operation_dispatch(self.connection, key).status, "finished")
        self.assertEqual(self.calls, [])
