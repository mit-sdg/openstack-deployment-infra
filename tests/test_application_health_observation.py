from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform.config import Config, load_platform, load_policy
from openstack_platform.contracts import DEPLOYMENT_ROUTE_HEADER_LOWER, NOMAD_ROUTE_MARKER_KEY
from openstack_platform.controller import database as db
from openstack_platform.controller import status
from openstack_platform.runtime import HttpResult

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "11111111-1111-4111-8111-111111111111"
MARKER = "22222222-2222-4222-8222-222222222222"


class ApplicationHealthObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(
            load_platform(ROOT / "config/platform.example.json"),
            load_policy(ROOT / "config/platform-policy.example.json", require_private=False),
        )
        self.application = SimpleNamespace(
            application_id=APP_ID,
            slug="demo",
            desired_running=True,
            url="https://untrusted.example/",
        )
        self.deployment = SimpleNamespace(
            health_path="/ready",
            nomad_version=4,
            nomad_job_sha256="a" * 64,
            image_digest="registry.example/app@sha256:" + "b" * 64,
            nomad_job=f'job "demo" {{\n    {NOMAD_ROUTE_MARKER_KEY} = "{MARKER}"\n}}\n',
        )
        self.helper = mock.Mock(return_value={"healthy": True, "terminal": False})
        self.http = mock.Mock(
            return_value=HttpResult(200, {DEPLOYMENT_ROUTE_HEADER_LOWER: MARKER}, b"")
        )

    def observe(self):
        with (
            mock.patch.object(db, "list_applications", return_value=[self.application]),
            mock.patch.object(db, "get_deployment", return_value=self.deployment),
        ):
            observer = status.application_observer(
                mock.Mock(), self.config, helper_caller=self.helper, http_get=self.http
            )
        return observer(APP_ID)

    def test_checks_canonical_configured_path_and_accepted_marker(self) -> None:
        observed = self.observe()
        self.assertTrue(observed.route_available)
        self.assertTrue(observed.route_healthy)
        self.assertTrue(observed.allocation_healthy)
        self.http.assert_called_once_with(
            f"https://demo.{self.config.platform.domain}/ready",
            timeout_seconds=self.config.policy.limits.http_seconds,
            response_limit=4096,
            allow_redirects=False,
        )
        self.assertEqual(self.helper.call_args.args[1]["jobId"], "demo")

    def test_root_or_other_deployment_response_cannot_claim_health(self) -> None:
        for code, headers in (
            (200, {}),
            (200, {DEPLOYMENT_ROUTE_HEADER_LOWER: APP_ID}),
            (302, {DEPLOYMENT_ROUTE_HEADER_LOWER: MARKER}),
            (503, {DEPLOYMENT_ROUTE_HEADER_LOWER: MARKER}),
        ):
            with self.subTest(code=code, headers=headers):
                self.http.return_value = HttpResult(code, headers, b"untrusted response")
                observed = self.observe()
                self.assertTrue(observed.route_available)
                self.assertFalse(observed.route_healthy)

    def test_disabled_accepted_app_skips_expected_absent_runtime_and_route(self) -> None:
        self.application.desired_running = False
        observed = self.observe()
        self.assertEqual(observed.scheduler_state, "stopped")
        self.assertIsNone(observed.allocation_healthy)
        self.assertIsNone(observed.route_healthy)
        self.assertFalse(observed.route_available)
        self.helper.assert_not_called()
        self.http.assert_not_called()

    def test_missing_deployment_does_not_probe_an_unaccepted_url(self) -> None:
        self.deployment = None
        observed = self.observe()
        self.assertFalse(observed.route_available)
        self.assertIsNone(observed.route_healthy)
        self.http.assert_not_called()

    def test_missing_marker_is_unknown_not_root_page_fallback(self) -> None:
        self.deployment.nomad_job = 'job "demo" {\n}\n'
        observed = self.observe()
        self.assertFalse(observed.route_available)
        self.assertIsNone(observed.route_healthy)
        self.http.assert_not_called()

    def test_route_transport_failure_preserves_independent_scheduler_evidence(self) -> None:
        self.http.side_effect = TimeoutError("private diagnostic must not escape")
        observed = self.observe()
        self.assertTrue(observed.allocation_healthy)
        self.assertFalse(observed.route_available)
        self.assertIsNone(observed.route_healthy)


if __name__ == "__main__":
    unittest.main()
