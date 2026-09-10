from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from openstack_platform.config import load_platform
from openstack_platform.controller.application_runtime import (
    Manifest,
    nomad_candidate_identity,
    render_nomad_job,
)
from openstack_platform.helper import application_actions
from openstack_platform.helper.main import HelperActionError
from tests import test_helper_application_actions as fixtures

ROOT = Path(__file__).resolve().parents[1]


class ExactSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.variables = fixtures.FakeVariableClient()
        self.nomad = fixtures.FakeNomad(self.variables)
        self.job = render_nomad_job(
            application_id="11111111-1111-4111-8111-111111111111",
            application_slug="demo-app",
            image=fixtures.CANDIDATE_IMAGE,
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=load_platform(ROOT / "config/platform.example.json"),
            cpu_mhz=1000,
            memory_mib=512,
            source_commit="c" * 40,
            recipe_hash="d" * 64,
        )
        self.identity = nomad_candidate_identity(self.job)
        self.args = {"slug": "demo-app", "job": self.job, "requireExact": True}

    def action(self, runner=None):
        return application_actions.handlers(
            self.variables,
            nomad_command=("fixed-nomad",),
            command_runner=self.nomad if runner is None else runner,
        )["app.deploy"]

    def install_exact(self):
        self.nomad.inspection["ID"] = "demo-app"
        self.nomad.inspection["Meta"]["platform_candidate_job_sha256"] = self.identity[0]

    def test_different_live_hash_is_not_overwritten_or_given_new_environment(self):
        with mock.patch.object(
            application_actions, "_synchronize_workload_variable"
        ) as synchronize:
            with self.assertRaises(HelperActionError) as caught:
                self.action()(self.args)
        self.assertEqual(caught.exception.code, "CANDIDATE_MISMATCH")
        synchronize.assert_not_called()
        self.assertFalse(any("run" in argv for argv, _ in self.nomad.calls))

    def test_exact_retry_does_not_resubmit_or_resynchronize_variables(self):
        self.install_exact()
        with mock.patch.object(
            application_actions, "_synchronize_workload_variable"
        ) as synchronize:
            result = self.action()(self.args)
        self.assertFalse(result["submitted"])
        synchronize.assert_not_called()
        self.assertFalse(any("run" in argv for argv, _ in self.nomad.calls))

    def test_first_submission_uses_atomic_create_only_check(self):
        self.nomad.inspection["ID"] = "absent-job"

        def runner(argv, **bounds):
            if "run" in argv:
                self.assertIn("-check-index=0", argv)
                self.install_exact()
            return self.nomad(argv, **bounds)

        result = self.action(runner)(self.args)
        self.assertTrue(result["submitted"])
        self.assertEqual(sum("run" in argv for argv, _ in self.nomad.calls), 1)
