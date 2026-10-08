"""The controller checks, records and exposes the runtime each build used."""

from __future__ import annotations

import unittest
from unittest import mock

from openstack_platform import remote
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller.deployment_config import parse_configuration
from tests import test_application_sizing as fixtures

NODE_IMAGE = "docker.io/library/node@sha256:" + "1" * 64
RESOLVED = {
    "runtime": "node",
    "version": "22.11.0",
    "image": NODE_IMAGE,
    "source": "engines.node >=22 <23",
}


class DeploymentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ApplicationSizingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture

    def stranded(self) -> tuple[db.Operation, str]:
        """Deploy, expecting the build's evidence to be refused; return why."""
        reasons: list[str] = []
        record = db.mark_recovery_required

        def recorded(connection, operation_id, error, **kwargs):
            reasons.append(str(error))
            return record(connection, operation_id, error, **kwargs)

        with mock.patch.object(db, "mark_recovery_required", side_effect=recorded):
            _key, operation = self.f.deploy()
        self.assertEqual(operation.status, "recovery_required")
        self.assertNotIn("app.deploy", [action for action, _ in self.f.calls])
        return operation, reasons[0]

    def model(self, deployment_id: str) -> dict[str, object]:
        response = self.f.router.dispatch("GET", f"/v1/deployments/{deployment_id}", {}, None)
        self.assertEqual(response.status, 200)
        return response.body

    def test_a_resolved_runtime_is_recorded_and_exposed(self) -> None:
        self.f.build_runtime = RESOLVED
        key, operation = self.f.deploy()
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertEqual(operation.refs["runtime"], RESOLVED)
        self.assertEqual(db.deployment_runtime(self.f.connection, key), RESOLVED)
        self.assertEqual(self.model(key)["runtime"], RESOLVED)
        listed = self.f.router.dispatch(
            "GET", f"/v1/applications/{self.f.app_id}/deployments", {}, None
        )
        self.assertEqual(listed.body["items"][0]["runtime"], RESOLVED)
        build = next(values for action, values in self.f.calls if action == "app.build")
        # Policy pins still travel unchanged; the helper resolves from them.
        self.assertEqual(
            build["runtimeImages"],
            {
                "bun": self.f.config.policy.runtime_images.bun,
                "node": self.f.config.policy.runtime_images.node,
            },
        )

    def test_runtime_evidence_outside_the_official_pins_is_refused(self) -> None:
        # test_runtime_versions covers each refusal; this is the controller's use.
        self.f.build_runtime = {**RESOLVED, "image": "registry.example/node@sha256:" + "1" * 64}
        operation, reason = self.stranded()
        self.assertIn("invalid runtime evidence", reason)
        self.assertIsNone(self.model(operation.operation_id)["runtime"])

    def test_the_recipe_hash_must_be_for_the_resolved_image(self) -> None:
        self.f.build_runtime = RESOLVED
        helper = self.f.api.helper_caller

        def built_on_the_policy_image(config, action, values, **bounds):
            result = helper(config, action, values, **bounds)
            if action == "app.build":
                manifest = parse_configuration(values["configuration"]).manifest({})
                policy = app.generate_recipe(manifest, config.policy.runtime_images)
                result = {**result, "recipeHash": policy.sha256}
            return result

        self.f.api.helper_caller = built_on_the_policy_image
        _operation, reason = self.stranded()
        self.assertIn("recipe identity", reason)

    def test_a_retained_image_keeps_the_runtime_of_its_build(self) -> None:
        self.f.build_runtime = RESOLVED
        first, operation = self.f.deploy()
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.f.build_runtime = None
        builds = len([1 for action, _ in self.f.calls if action == "app.build"])
        resized, operation = self.f.resize(self.f.plan())
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertEqual(builds, len([1 for action, _ in self.f.calls if action == "app.build"]))
        self.assertEqual(self.model(resized)["runtime"], RESOLVED)
        self.assertEqual(self.model(first)["runtime"], RESOLVED)

    def test_a_failed_version_lookup_ends_cleanly_and_asks_for_a_retry(self) -> None:
        helper = self.f.api.helper_caller
        cleanup: list[dict[str, object]] = []

        def unavailable(config, action, values, **bounds):
            if action == "app.build":
                raise remote.HelperError(
                    "RUNTIME_UNAVAILABLE", "runtime versions could not be looked up"
                )
            if action == "app.build.cleanup":
                cleanup.append(dict(values))
                return {**values, "builderAbsent": True, "artifactAbsent": True}
            return helper(config, action, values, **bounds)

        self.f.api.helper_caller = unavailable
        key, operation = self.f.deploy()
        self.assertEqual(
            (operation.status, operation.phase, operation.refs.get("rejection")),
            ("failed", "build_rejected", "runtime_unavailable"),
        )
        self.assertEqual(len(cleanup), 1)
        self.assertIn("Try deploying again", operation.safe_error or "")
        attempt = db.get_deployment_attempt(self.f.connection, key)
        assert attempt is not None
        self.assertEqual(attempt.status, "failed")
        model = self.f.router.dispatch("GET", f"/v1/operations/{key}", {}, None).body
        self.assertEqual(model["errorCode"], "RUNTIME_UNAVAILABLE")
        self.assertIsNone(self.model(key)["runtime"])

        self.f.api.helper_caller = helper
        _key, rejected = self.f.deploy()
        self.assertEqual(rejected.status, "succeeded", rejected.safe_error)


if __name__ == "__main__":
    unittest.main()
