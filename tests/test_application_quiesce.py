from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform.contracts import NOMAD_CANDIDATE_IMAGE_KEY, NOMAD_CANDIDATE_JOB_SHA_KEY
from openstack_platform.helper.application_quiesce import quiesce
from openstack_platform.helper.main import HelperActionError
from openstack_platform.runtime import CommandTimedOut
from openstack_platform.validation import ValidationError


class ApplicationQuiesceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = "registry.example/projects/any-app/app@sha256:" + "a" * 64
        self.args = {
            "operationId": "11111111-1111-4111-8111-111111111111",
            "nodeId": "22222222-2222-4222-8222-222222222222",
            "jobVersion": 1,
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
                "ID": "33333333-3333-4333-8333-333333333333",
                "NodeID": "22222222-2222-4222-8222-222222222222",
                "JobVersion": 1,
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
            state_directory=self.root,
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
        self.assertEqual(
            {k: v for k, v in result.items() if k != "receiptSha256"},
            {**self.args, "jobStopped": True, "allocationsStopped": True},
        )
        self.assertEqual(len(result["receiptSha256"]), 64)
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

    def test_empty_inventory_is_never_exit_evidence(self):
        self.allocations = []
        with self.assertRaises(HelperActionError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "STOP_UNCONFIRMED")
        self.assertFalse(any("stop" in call for call in self.calls))

    def test_unwitnessed_allocation_gc_does_not_confirm_exit(self):
        self.allocations[0]["ClientStatus"] = "running"

        def disappear():
            self.allocations = []

        self.on_stop = disappear
        with self.assertRaises(HelperActionError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "STOP_UNCONFIRMED")
        record = json.loads(next(self.root.glob("*.json")).read_text())
        self.assertFalse(record["complete"])
        self.assertFalse(next(iter(record["allocations"].values()))["exited"])

    def test_durable_exit_receipt_survives_lost_reply_and_nomad_gc(self):
        result = self.call()
        self.job = None
        self.allocations = []
        self.calls.clear()
        recovered = self.call()
        self.assertEqual(recovered, result)
        self.assertFalse(any("stop" in call for call in self.calls))

    def test_stop_never_precedes_durable_allocation_intent(self):
        with mock.patch(
            "openstack_platform.helper.application_quiesce.durable.atomic_write",
            side_effect=OSError("disk unavailable"),
        ):
            with self.assertRaises(OSError):
                self.call()
        self.assertFalse(any("stop" in call for call in self.calls))

    def test_live_allocation_on_another_node_is_rejected(self):
        self.allocations[0].update(
            ClientStatus="running", NodeID="44444444-4444-4444-8444-444444444444"
        )
        with self.assertRaises(HelperActionError):
            self.call()
        self.assertFalse(any("stop" in call for call in self.calls))

    def test_completed_witness_cannot_authorize_a_reactivated_job(self):
        self.call()
        self.job["Stop"] = False
        with self.assertRaises(HelperActionError):
            self.call()

    def test_historical_rows_cannot_witness_the_current_workload(self):
        self.job["Version"] = 2
        self.args["jobVersion"] = 2
        self.allocations[0]["NodeID"] = "44444444-4444-4444-8444-444444444444"
        with self.assertRaises(HelperActionError):
            self.call()
        self.assertFalse(any("stop" in call for call in self.calls))

    def test_an_unrelated_dead_task_is_not_app_exit_evidence(self):
        self.allocations[0]["TaskStates"] = {"unrelated-task": {"State": "dead"}}
        with self.assertRaises(HelperActionError):
            self.call()
        self.assertFalse(any("stop" in call for call in self.calls))

    def test_reactivation_during_last_observation_cannot_commit_completion(self):
        runner = self.runner
        reads = 0

        def changing(argv, **bounds):
            nonlocal reads
            result = runner(argv, **bounds)
            if "allocs" in argv:
                reads += 1
                if reads == 2:
                    self.job["Stop"] = False
            return result

        with mock.patch.object(self, "runner", side_effect=changing):
            with self.assertRaises(HelperActionError):
                self.call()
        record = json.loads(next(self.root.glob("*.json")).read_text())
        self.assertFalse(record["complete"])

    def test_strict_arguments(self):
        for args in ({**self.args, "extra": True}, {**self.args, "jobId": "another-app"}):
            with self.assertRaises((HelperActionError, ValidationError)):
                self.call(args)
        self.assertEqual(self.calls, [])
