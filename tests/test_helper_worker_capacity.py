"""Production dispatch coverage for the pinned Nomad 2.0.5 capacity contract.

Schema evidence (no deprecated Resources/Reserved fallback):
https://github.com/hashicorp/nomad/blob/v2.0.5/api/nodes.go#L544-L622
https://github.com/hashicorp/nomad/blob/v2.0.5/command/node_status.go#L976-L986
Worker Name/Meta originate in infra/cloud-init-nixos/worker.yaml.
"""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform import runtime
from openstack_platform.config import load_platform
from openstack_platform.controller import application_runtime as app
from openstack_platform.helper import main, production, worker_capacity

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ID = "12345678-1234-4000-8000-123456789abc"
NODE_ID = "00000000-0000-4000-8000-000000000011"
SERVER_ID = "00000000-0000-4000-8000-000000000012"
PORT_ID = "00000000-0000-4000-8000-000000000013"
REQUEST_ID = "00000000-0000-4000-8000-000000000014"
SENTINEL = "sentinel-provider-private-capacity-output"


class ProductionWorkerCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.diagnostics = Path(self.temporary.name) / "diagnostics"
        self.platform = load_platform(ROOT / "config/platform.example.json")
        # Candidate placement ID, deliberately distinct from the product app ID.
        self.worker_id = app.deployment_worker_ids(APPLICATION_ID)[1]
        self.server_name = f"{self.platform.prefix}-worker-{self.worker_id.replace('-', '')[:12]}"
        self.worker = app.WorkerObservation(
            self.worker_id,
            "commons",
            SERVER_ID,
            self.server_name,
            PORT_ID,
            f"{self.server_name}-v4",
            REQUEST_ID,
            "xl.4core",
            True,
        )
        # api.Node and api.NodeListStub in Nomad 2.0.5; extra provider details
        # must not escape the allowlisted helper capacity result.
        self.node = {
            "ID": NODE_ID,
            "Name": self.server_name,
            "Status": "ready",
            "Drain": False,
            "SchedulingEligibility": "eligible",
            "NodeClass": f"{self.platform.namespace}-app",
            "Meta": {
                "application_id": self.worker_id,
                "application_slug": "commons",
                "managed_by": f"{self.platform.namespace}-platform",
            },
            "Drivers": {"docker": {"Detected": True, "Healthy": True}},
            "NodeResources": {
                "Cpu": {"CpuShares": 10000, "TotalCpuCores": 4, "ReservableCpuCores": [0, 1, 2, 3]},
                "Memory": {"MemoryMB": 16000},
            },
            "ReservedResources": {"Cpu": {"CpuShares": 200}, "Memory": {"MemoryMB": 512}},
            "Attributes": {"private": SENTINEL},
        }
        self.nodes = [{"ID": NODE_ID, "Name": self.server_name, "Status": "ready"}]
        self.calls = []
        self.failure = None
        self.truncated = False
        self.malformed_json = False
        self.nomad_command = app.provider_command(self.platform, "nomad")[0]
        patches = (
            mock.patch.object(
                production, "helper_runtime", return_value=SimpleNamespace(platform=self.platform)
            ),
            mock.patch.object(app, "observe_worker", return_value=self.worker),
            mock.patch.object(worker_capacity, "run", side_effect=self.runner),
        )
        self.observe_worker = patches[1].start()
        self.addCleanup(patches[1].stop)
        for patch in (patches[0], patches[2]):
            patch.start()
            self.addCleanup(patch.stop)

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.failure is not None:
            raise self.failure
        value = self.node if argv[-1] == NODE_ID else self.nodes
        payload = json.dumps(value).encode() if not self.malformed_json else SENTINEL.encode()
        return runtime.CommandResult(tuple(argv), 0, payload, b"", self.truncated, False)

    def dispatch(self, extra_args=None):
        request = {
            "version": 1,
            "requestId": REQUEST_ID,
            "action": "app.worker.capacity",
            "args": {"applicationId": self.worker_id, "slug": "commons", **(extra_args or {})},
        }
        output = io.BytesIO()
        main.serve_once(
            io.BytesIO(json.dumps(request).encode()),
            output,
            production.production_handlers(),
            diagnostic_directory=self.diagnostics,
        )
        response = output.getvalue()
        self.assertNotIn(SENTINEL.encode(), response)
        for path in self.diagnostics.glob("*"):
            if path.is_file():
                self.assertNotIn(SENTINEL.encode(), path.read_bytes())
        return json.loads(response)

    def test_production_dispatch_verifies_provider_worker_and_exact_nomad_identity(self):
        response = self.dispatch()
        self.assertTrue(response["ok"], response)
        self.assertEqual(
            response["result"],
            {
                "nodeId": NODE_ID,
                "serverId": SERVER_ID,
                "flavorName": "xl.4core",
                "cpuMHz": 9000,
                "memoryMiB": 14400,
                "totalCpuMHz": 10000,
                "totalMemoryMiB": 16000,
            },
        )
        self.observe_worker.assert_called_once_with(
            self.worker_id,
            "commons",
            prefix=self.platform.prefix,
            worker_command=app.provider_command(self.platform, "worker"),
            timeout_seconds=120,
            nomad_command=self.nomad_command,
            project_name=self.platform.project_name,
            project_id=self.platform.project_id,
        )
        self.assertEqual(
            [call[0] for call in self.calls],
            [
                (self.nomad_command, "node", "status", "-json"),
                (self.nomad_command, "node", "status", "-json", NODE_ID),
            ],
        )
        self.assertLessEqual(
            self.calls[1][1]["timeout_seconds"], self.calls[0][1]["timeout_seconds"]
        )
        self.assertEqual(self.calls[0][1]["stdout_limit"], 1_048_576)

    def test_request_cannot_select_nomad_command_or_node(self):
        for field in ("nomadCommand", "nodeId", "serverName"):
            with self.subTest(field=field):
                response = self.dispatch({field: SENTINEL})
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "INVALID_ARGS")
        self.assertEqual(self.calls, [])
        self.observe_worker.assert_not_called()

    def test_unknown_duplicate_unready_and_wrong_owner_nodes_fail_closed(self):
        original = copy.deepcopy(self.node)
        mutations = (
            {"ID": REQUEST_ID},
            {"Name": "another-worker"},
            {"Status": "down"},
            {"Drain": True},
            {"SchedulingEligibility": "ineligible"},
            {"NodeClass": "other"},
            {"Meta": {**original["Meta"], "application_id": APPLICATION_ID}},
            {"Meta": {**original["Meta"], "application_slug": "another-app"}},
            {"Meta": {**original["Meta"], "managed_by": "other"}},
            {"Drivers": {"docker": {"Detected": True, "Healthy": False}}},
            {"Meta": SENTINEL},
            {"Drivers": {"docker": SENTINEL}},
            {"NodeResources": None},
            {"ReservedResources": None},
        )
        for mutation in mutations:
            self.node = {**original, **mutation}
            with self.subTest(mutation=mutation):
                self.assertFalse(self.dispatch()["ok"])
        self.node = original
        for nodes in ([], self.nodes * 2):
            self.nodes = nodes
            self.assertFalse(self.dispatch()["ok"])
        self.observe_worker.return_value = app.WorkerObservation(
            self.worker_id,
            "commons",
            None,
            self.server_name,
            None,
            f"{self.server_name}-v4",
            None,
            None,
            False,
        )
        self.calls.clear()
        self.assertFalse(self.dispatch()["ok"])
        self.assertEqual(self.calls, [])

    def test_failures_timeouts_and_malformed_output_are_sanitized_by_production_dispatch(self):
        for failure_type in (runtime.CommandFailure, runtime.CommandTimedOut):
            with self.subTest(failure=failure_type):
                self.failure = failure_type(SENTINEL)
                response = self.dispatch()
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "ACTION_FAILED")
                self.assertIn(REQUEST_ID, response["error"]["message"])
        self.failure = None
        self.malformed_json = True
        self.assertFalse(self.dispatch()["ok"])
        self.malformed_json = False
        self.truncated = True
        self.assertFalse(self.dispatch()["ok"])
        self.truncated = False
        self.observe_worker.side_effect = runtime.CommandTimedOut(SENTINEL)
        self.calls.clear()
        self.assertFalse(self.dispatch()["ok"])
        self.assertEqual(self.calls, [])

    def test_queries_share_one_deadline(self):
        with mock.patch.object(worker_capacity.time, "monotonic", side_effect=[0, 1, 31]):
            response = self.dispatch()
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "ACTION_FAILED")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["timeout_seconds"], 29)
