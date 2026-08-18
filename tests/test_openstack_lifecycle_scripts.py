from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "12345678-1234-4000-8000-123456789abc"
PROJECT_ID = "00000000-0000-4000-8000-000000000000"
PORT_ID = "00000000-0000-4000-8000-000000000077"

FAKE_OPENSTACK = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_OPENSTACK_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
state["calls"].append(args)


def finish(payload=None, status=0):
    state_path.write_text(json.dumps(state))
    if payload is not None:
        print(json.dumps(payload) if isinstance(payload, (dict, list)) else payload)
    raise SystemExit(status)


if args[:2] == ["token", "issue"]:
    finish(state["project_id"])
if args[:2] == ["project", "show"]:
    project = state.get('project_name', 'example-project')
    if state.get('project_output') == 'lines':
        finish(f"{state['project_id']}\n{project}")
    finish(f"{state['project_id']} {project}")
if args[:2] == ["server", "list"]:
    server = state.get("server")
    rows = [] if server is None else [{"ID": server["id"], "Name": server["name"]}]
    finish(rows)
if args[:2] == ["port", "list"]:
    port = state.get("port")
    rows = [] if port is None else [{"ID": port["id"], "Name": port["name"]}]
    finish(rows)
if args[:2] == ["server", "show"]:
    server = state.get("server")
    if server is None or args[2] != server["id"]:
        finish(status=1)
    finish(server)
if args[:2] == ["port", "show"]:
    port = state.get("port")
    if port is None or args[2] != port["id"]:
        finish(status=1)
    finish(port)
if args[:2] == ["server", "delete"]:
    server = state.get("server")
    server_id = args[-1]
    if server is None or server_id != server["id"]:
        finish(status=1)
    state["server"] = None
    if state.get("port", {}).get("device_id") == server_id:
        state["port"]["device_id"] = ""
    finish()
if args[:2] == ["port", "delete"]:
    port = state.get("port")
    if port is None or args[2] != port["id"]:
        finish(status=1)
    state["port"] = None
    finish()
finish(status=64)
"""


class LifecycleDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.fake_openstack = self.directory / "openstack"
        self.fake_openstack.write_text(textwrap.dedent(FAKE_OPENSTACK))
        self.fake_openstack.chmod(0o755)
        self.state_path = self.directory / "state.json"

    def lifecycle_cases(self) -> tuple[tuple[str, tuple[str, ...], str, str], ...]:
        short_id = APP_ID.replace("-", "")[:12]
        return (
            (
                "builder_lifecycle.sh",
                ("delete", APP_ID),
                f"example-builder-{short_id}",
                f"managed-by=platform;build-id={APP_ID}",
            ),
            (
                "worker_lifecycle.sh",
                ("delete", APP_ID, "demo-app"),
                f"example-worker-{short_id}",
                f"managed-by=platform;application-id={APP_ID};application-slug=demo-app",
            ),
        )

    def write_state(
        self,
        *,
        port_name: str,
        description: str,
        project_id: str = PROJECT_ID,
        project_name: str = "example-project",
        project_output: str = "single",
    ) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "project_name": project_name,
                    "project_output": project_output,
                    "server": None,
                    "port": {
                        "id": PORT_ID,
                        "name": port_name,
                        "device_id": "",
                        "fixed_ips": [{"ip_address": "192.0.2.40"}],
                        "description": description,
                    },
                    "calls": [],
                }
            )
        )

    def run_lifecycle(
        self, script_name: str, arguments: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "OSC": str(self.fake_openstack),
                "FAKE_OPENSTACK_STATE": str(self.state_path),
                "PLATFORM_CONFIG": str(ROOT / "config" / "platform.example.json"),
                "OS_PROJECT_NAME": "example-project",
                "PKI_DIR": str(self.directory / "unused-pki"),
                "STORAGE_SECRETS_FILE": str(self.directory / "unused-secrets"),
                "BUILDER_OPERATOR_PUBLIC_KEY": str(self.directory / "unused-key"),
            }
        )
        return subprocess.run(
            [str(ROOT / "infra" / "openstack" / script_name), *arguments],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def read_state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text())

    def test_delete_removes_port_left_by_crash_before_server_create_idempotently(self) -> None:
        for script_name, arguments, server_name, description in self.lifecycle_cases():
            with self.subTest(script=script_name):
                self.write_state(port_name=f"{server_name}-v4", description=description)

                first = self.run_lifecycle(script_name, arguments)
                self.assertEqual(first.returncode, 0, first.stderr)
                second = self.run_lifecycle(script_name, arguments)
                self.assertEqual(second.returncode, 0, second.stderr)

                state = self.read_state()
                self.assertIsNone(state["port"])
                delete_calls = [
                    call
                    for call in state["calls"]
                    if call[:2] in (["server", "delete"], ["port", "delete"])
                ]
                self.assertEqual(delete_calls, [["port", "delete", PORT_ID]])

    def test_authenticated_project_mismatch_stops_before_provider_mutation(self) -> None:
        self.write_state(
            port_name="unused",
            description="unused",
            project_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        )
        result = self.run_lifecycle("builder_lifecycle.sh", ("show", APP_ID))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UUID", result.stderr)
        self.assertEqual(
            self.read_state()["calls"],
            [["token", "issue", "-f", "value", "-c", "project_id"]],
        )

        self.write_state(port_name="unused", description="unused", project_name="wrong-project")
        result = self.run_lifecycle("builder_lifecycle.sh", ("show", APP_ID))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("name", result.stderr)
        self.assertEqual(
            self.read_state()["calls"],
            [
                ["token", "issue", "-f", "value", "-c", "project_id"],
                [
                    "project",
                    "show",
                    PROJECT_ID,
                    "-f",
                    "value",
                    "-c",
                    "id",
                    "-c",
                    "name",
                ],
            ],
        )

    def test_project_identity_accepts_openstack_value_formatter_lines(self) -> None:
        self.write_state(port_name="unused", description="unused", project_output="lines")
        result = self.run_lifecycle("builder_lifecycle.sh", ("show", APP_ID))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.read_state()["calls"][:2],
            [
                ["token", "issue", "-f", "value", "-c", "project_id"],
                [
                    "project",
                    "show",
                    PROJECT_ID,
                    "-f",
                    "value",
                    "-c",
                    "id",
                    "-c",
                    "name",
                ],
            ],
        )

    def test_delete_refuses_same_name_orphan_port_owned_by_something_else(self) -> None:
        for script_name, arguments, server_name, description in self.lifecycle_cases():
            with self.subTest(script=script_name):
                self.write_state(
                    port_name=f"{server_name}-v4",
                    description=f"{description};collision=true",
                )

                result = self.run_lifecycle(script_name, arguments)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("mismatched ownership identity", result.stderr)
                state = self.read_state()
                self.assertIsNotNone(state["port"])
                self.assertFalse(
                    any(
                        call[:2] in (["server", "delete"], ["port", "delete"])
                        for call in state["calls"]
                    )
                )


if __name__ == "__main__":
    unittest.main()
