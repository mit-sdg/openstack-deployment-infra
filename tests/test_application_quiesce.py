from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from openstack_platform.contracts import NOMAD_CANDIDATE_IMAGE_KEY, NOMAD_CANDIDATE_JOB_SHA_KEY
from openstack_platform.helper.application_quiesce import quiesce
from openstack_platform.helper.main import HelperActionError
from openstack_platform.runtime import CommandTimedOut
from openstack_platform.validation import ValidationError


class ApplicationQuiesceTests(unittest.TestCase):
    def setUp(self):
        self.image = "registry.example/projects/any-app/app@sha256:" + "a" * 64
        self.args = {
            "slug": "any-app",
            "jobId": "any-app",
            "candidateJobSha256": "b" * 64,
            "candidateImage": self.image,
        }
        self.job = {
            "ID": "any-app",
            "Version": 1,
            "Stop": False,
            "TaskGroups": [
                {"Name": "app", "Tasks": [{"Name": "app", "Config": {"image": self.image}}]}
            ],
            "Meta": {NOMAD_CANDIDATE_IMAGE_KEY: self.image, NOMAD_CANDIDATE_JOB_SHA_KEY: "b" * 64},
        }
        self.allocations = [
            {
                "JobID": "any-app",
                "DesiredStatus": "stop",
                "ClientStatus": "complete",
                "TaskStates": {"app": {"State": "dead"}},
            }
        ]
        self.calls = []
        self.clock = 0.0
        self.on_sleep = lambda: None
        self.on_stop = lambda: None

    def runner(self, argv, **bounds):
        self.calls.append(tuple(argv))
        self.assertGreater(bounds["timeout_seconds"], 0)
        self.assertLessEqual(bounds["timeout_seconds"], 30)
        if "inspect" in argv:
            if self.job is None:
                return SimpleNamespace(
                    returncode=1,
                    stdout=b"",
                    stderr=b'No job(s) with prefix or ID "any-app" found\n',
                )
            payload = self.job
        elif "allocs" in argv:
            self.assertIn("-all", argv)
            payload = self.allocations
        elif "stop" in argv:
            self.assertNotIn("-purge", argv)
            self.assertIn("-detach", argv)
            self.job["Stop"] = True
            self.on_stop()
            payload = {}
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")

    def sleep(self, seconds):
        self.clock += seconds
        self.on_sleep()

    def call(self, args=None, timeout=90):
        return quiesce(
            self.args if args is None else args,
            nomad_command=("fixed-nomad",),
            command_runner=self.runner,
            sleep=self.sleep,
            monotonic=lambda: self.clock,
            timeout_seconds=timeout,
        )

    def test_waits_for_client_exit_not_desired_stop_and_keeps_job_for_checkpoint(self):
        self.allocations[0].update(ClientStatus="running", TaskStates={"app": {"State": "running"}})

        def finish():
            self.allocations[0].update(
                ClientStatus="complete", TaskStates={"app": {"State": "dead"}}
            )

        self.on_sleep = finish
        result = self.call()
        self.assertEqual(result, {**self.args, "jobStopped": True, "allocationsStopped": True})
        self.assertEqual(self.clock, 1)
        self.assertTrue(self.job["Stop"])
        self.assertEqual(sum("stop" in call for call in self.calls), 1)
        self.assertFalse(any("-purge" in call for call in self.calls))

    def test_retry_of_stopped_job_reobserves_without_stopping_again(self):
        self.call()
        self.calls.clear()
        self.assertTrue(self.call()["allocationsStopped"])
        self.assertFalse(any("stop" in call for call in self.calls))
        self.assertTrue(any("allocs" in call for call in self.calls))

    def test_absence_drift_and_invalid_stop_state_never_authorize_mutation(self):
        original = copy.deepcopy(self.job)
        for job in (
            None,
            {**original, "Stop": None},
            {**original, "Meta": {**original["Meta"], NOMAD_CANDIDATE_JOB_SHA_KEY: "c" * 64}},
        ):
            with self.subTest(job=job):
                self.job = job
                self.calls.clear()
                with self.assertRaises(HelperActionError):
                    self.call()
                self.assertFalse(any("stop" in call for call in self.calls))

    def test_lost_client_missing_task_evidence_and_other_job_fail_closed(self):
        original = copy.deepcopy(self.allocations[0])
        for allocation in (
            {**original, "ClientStatus": "lost"},
            {**original, "ClientStatus": "unknown"},
            {**original, "TaskStates": {}},
            {**original, "TaskStates": {"app": {"State": "running"}}},
            {**original, "JobID": "another-app"},
        ):
            with self.subTest(allocation=allocation):
                self.allocations = [allocation]
                with self.assertRaises(HelperActionError):
                    self.call()

    def test_checks_identity_again_after_stop(self):
        def drift():
            self.job["Meta"][NOMAD_CANDIDATE_JOB_SHA_KEY] = "d" * 64

        self.on_stop = drift
        with self.assertRaises(HelperActionError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "CANDIDATE_MISMATCH")

    def test_whole_quiesce_deadline_is_bounded(self):
        self.allocations[0]["ClientStatus"] = "running"
        with self.assertRaises(CommandTimedOut):
            self.call(timeout=2)
        self.assertEqual(self.clock, 2)

    def test_strict_arguments(self):
        for args in ({**self.args, "extra": True}, {**self.args, "jobId": "another-app"}):
            with self.assertRaises((HelperActionError, ValidationError)):
                self.call(args)
        self.assertEqual(self.calls, [])
