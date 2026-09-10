"""Controller cutovers through real quiescence/receipt code and Nomad-state probes."""

from __future__ import annotations

import copy
import json
import unittest
import uuid
from types import SimpleNamespace

from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.helper.application_quiesce import quiesce
from tests import test_worker_reuse as reuse_fixtures


class ReuseExitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = reuse_fixtures.WorkerReuseTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.fixture.api.close()
        self.fixture.api = ControllerAPI(
            self.fixture.connection,
            self.fixture.config,
            self.fixture.root,
            helper_caller=self.helper,
        )
        self.fixture.fixture.api = self.fixture.api
        self.fixture.router = self.fixture.api.router()
        self.stopped = set()
        self.blocked_hash = None
        self.lose_reply = False
        self.receipts = self.fixture.root / "quiesce"

    def helper(self, config, action, values, **kwargs):
        if action != "app.quiesce":
            result = self.base.helper(config, action, values, **kwargs)
            if action == "app.deploy":
                self.stopped.discard(app.nomad_candidate_identity(values["job"])[0])
            return result
        self.fixture.calls.append((action, copy.deepcopy(values)))
        result = quiesce(
            values,
            state_directory=self.receipts,
            nomad_command=("fixture-nomad",),
            command_runner=lambda argv, **bounds: self.nomad(values, argv, **bounds),
            timeout_seconds=5,
        )
        if self.lose_reply:
            self.lose_reply = False
            # Nomad GC follows a lost helper response. The controller has not
            # checkpointed exit yet, so only the durable helper witness remains.
            self.fixture.jobs.pop(values["jobId"], None)
            raise RuntimeError("lost quiesce reply followed by Nomad GC")
        return result

    def nomad(self, values, argv, **_bounds):
        job = self.fixture.jobs.get(values["jobId"])
        if job is None:
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=f'No job(s) with prefix or ID "{values["jobId"]}" found\n'.encode(),
            )
        job_hash, image = app.nomad_candidate_identity(job)
        if "inspect" in argv:
            payload = {
                "ID": values["jobId"],
                "Version": 1,
                "Stop": job_hash in self.stopped,
                "Meta": {
                    "platform_candidate_job_sha256": job_hash,
                    "platform_candidate_image": image,
                },
                "TaskGroups": [
                    {"Name": "app", "Tasks": [{"Name": "app", "Config": {"image": image}}]}
                ],
            }
        elif "allocs" in argv:
            exited = job_hash in self.stopped and job_hash != self.blocked_hash
            lost = job_hash in self.stopped and job_hash == self.blocked_hash
            payload = [
                {
                    "ID": str(uuid.uuid5(uuid.NAMESPACE_URL, job_hash)),
                    "NodeID": values["nodeId"],
                    "JobID": values["jobId"],
                    "JobVersion": 1,
                    "ClientStatus": "lost" if lost else "complete" if exited else "running",
                    "DesiredStatus": "stop" if job_hash in self.stopped else "run",
                    "TaskStates": {"app": {"State": "dead" if exited else "running"}},
                }
            ]
        elif "stop" in argv:
            self.assertNotIn("-purge", argv)
            self.stopped.add(job_hash)
            payload = {}
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode(), stderr=b"")

    def test_failed_candidate_is_not_purged_until_actual_exit_is_witnessed(self):
        before, accepted = self.base.first()
        self.fixture.fail_health = True

        def block_candidate():
            job = next(iter(self.fixture.jobs.values()))
            self.blocked_hash = app.nomad_candidate_identity(job)[0]

        self.base.after_submit = block_candidate
        key, interrupted = self.base.update()
        self.assertEqual(interrupted.status, "recovery_required")
        self.assertEqual(interrupted.phase, "candidate_rejected")
        self.assertNotIn("candidate_process_stopped", interrupted.refs)
        self.assertTrue(self.fixture.jobs)  # Purge must not hide the live/lost task.
        self.assertEqual(len(self.fixture.workers), 1)
        self.assertEqual(
            db.get_active_deployment(self.fixture.connection, self.base.app_id).deployment_id,
            accepted,
        )
        self.blocked_hash = None
        self.fixture.calls.clear()
        _, failed = self.base.update(key)
        self.assertEqual(failed.status, "failed", failed.safe_error)
        self.assertTrue(failed.refs["candidate_process_stopped"])
        self.assertEqual(self.fixture.jobs, {})
        self.assertNotIn("app.deploy", self.base.actions())
        self.assertNotIn("app.env.set", self.base.actions())
        self.assertNotIn("app.worker.delete", self.base.actions())
        self.assertEqual(
            db.get_application(self.fixture.connection, self.base.app_id).worker_server_id,
            before.worker_server_id,
        )
        records = [json.loads(path.read_text()) for path in self.receipts.glob("*.json")]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["complete"] for record in records))

    def test_lost_predecessor_exit_reply_recovers_from_real_receipt_after_gc(self):
        before, _ = self.base.first()
        self.lose_reply = True
        key, interrupted = self.base.update()
        self.assertEqual(interrupted.status, "recovery_required")
        self.assertNotIn("maintenance_process_stopped", interrupted.refs)
        self.assertEqual(self.fixture.jobs, {})
        self.fixture.calls.clear()
        self.base.assert_success(self.base.update(key))
        self.assertNotIn("app.build", self.base.actions())
        self.assertNotIn("app.worker.create", self.base.actions())
        self.assertEqual(
            db.get_application(self.fixture.connection, self.base.app_id).worker_server_id,
            before.worker_server_id,
        )
