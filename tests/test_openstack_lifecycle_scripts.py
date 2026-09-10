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
SERVER_ID = "00000000-0000-4000-8000-000000000088"
OTHER_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
IMAGE_ID = "00000000-0000-4000-8000-000000000099"
NETWORK_ID = "00000000-0000-4000-8000-000000000001"
SUBNET_ID = "00000000-0000-4000-8000-000000000002"
GROUP_ID = "00000000-0000-4000-8000-000000000003"
MARKER = "offline bootstrap ready"

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
    # Snapshot this command's response, then inject drift for the next read.
    output = json.dumps(payload) if isinstance(payload, (dict, list)) else payload
    for hook in state.get("after", []):
        prefix = hook["command"]
        count = sum(call[:len(prefix)] == prefix for call in state["calls"])
        if args[:len(prefix)] == prefix and count == hook.get("occurrence", 1):
            for key, value in hook.get("patch", {}).items():
                if isinstance(value, dict) and isinstance(state.get(key), dict):
                    state[key].update(value)
                else:
                    state[key] = value
            status = hook.get("exit", status)
    state_path.write_text(json.dumps(state))
    if output is not None:
        print(output)
    raise SystemExit(status)


def option(name):
    return args[args.index(name) + 1]


if state.get("fail") == args[:2]:
    # Valid-looking output with a failing CLI exit must still fail closed.
    finish([], status=1)
if args[:2] == ["token", "issue"]:
    finish(state["project_id"])
if args[:2] == ["project", "show"]:
    project = state.get('project_name', 'example-project')
    if state.get('project_output') == 'lines':
        finish(f"{state['project_id']}\n{project}")
    finish(f"{state['project_id']} {project}")
if args[:2] in (["server", "list"], ["port", "list"]):
    resource = state.get(args[0])
    rows = [] if resource is None else [{"ID": resource["id"], "Name": resource["name"]}]
    if "--server" in args:
        rows = rows if resource and resource["device_id"] == option("--server") else []
        finish(state.get("attached_ports", rows))
    finish(state.get(args[0] + "_rows", rows))
if args[:2] in (["server", "show"], ["port", "show"]):
    resource = state.get(args[0])
    if resource is None or args[2] != resource["id"]:
        finish(status=1)
    if "value" in args:
        finish(resource.get("status", "ACTIVE"))
    finish(state.get(args[0] + "_show", resource))
if args[:3] == ["console", "log", "show"]:
    if args[-1] != state["server"]["id"]:
        finish(status=1)
    logs = state.get("console_logs", [])
    finish(logs.pop(0) if logs else state.get("marker", "offline bootstrap ready"))
if args[:2] == ["node", "status"]:
    server = state["server"]
    props = server["properties"]
    node = dict(ID=server["id"], Name=server["name"], Status="ready",
                NodeClass="app-platform-app",
                Meta=dict(application_id=props["app_platform_application_id"],
                          application_slug=props["app_platform_application_slug"],
                          managed_by="app-platform-platform"),
                Drivers=dict(docker=dict(Detected=True, Healthy=True)))
    node.update(state.get("node", {}))
    finish([node] if len(args) == 3 else node)
if args[:2] == ["flavor", "show"]:
    finish(dict(id="100", name=args[2], vcpus=1))
if args[:2] == ["port", "create"]:
    if state.get("port") is not None:
        finish(status=1)
    state["port"] = dict(id=state["new_port_id"], name=args[-1],
                         device_id="", device_owner="", fixed_ips=[dict(ip_address="192.0.2.40")],
                         description=option("--description"))
    finish(state["port"])
if args[:2] == ["server", "create"]:
    port = state["port"]
    if state.get("server") or option("--port") != port["id"] or port["device_id"]:
        finish(status=1)
    state["user_data"] = Path(option("--user-data")).read_text()
    state["server"] = dict(id=state["new_server_id"], name=args[-1],
                           project_id=state["project_id"], status="ACTIVE",
                           image=dict(id=option("--image")), flavor=dict(original_name="worker-small"),
                           properties=dict(args[i+1].split("=", 1) for i, arg in enumerate(args)
                                           if arg == "--property"))
    port.update(device_id=state["server"]["id"], device_owner="compute:nova")
    finish(state["server"])
if args[:2] == ["server", "delete"]:
    server = state.get("server")
    server_id = args[-1]
    if server is None or server_id != server["id"]:
        finish(status=1)
    state["server"] = None
    if (state.get("port") or {}).get("device_id") == server_id:
        state["port"].update(device_id="", device_owner="", **{"binding:host_id": ""})
    finish()
if args[:2] == ["port", "delete"]:
    port = state.get("port")
    if port is None or args[2] != port["id"] or port["device_id"]:
        finish(status=1)
    state["port"] = None
    finish()
finish(status=64)
"""


class LifecycleScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.fake_openstack = self.directory / "openstack"
        self.fake_openstack.write_text(textwrap.dedent(FAKE_OPENSTACK))
        self.fake_openstack.chmod(0o755)
        self.state_path = self.directory / "state.json"
        self.pki = self.directory / "pki"
        self.pki.mkdir()
        for name in ("internal-ca.pem", "nomad-worker.pem", "nomad-worker-key.pem"):
            path = self.pki / name
            path.write_text("offline fixture\n")
            path.chmod(0o600)
        self.secrets = self.directory / "secrets"
        self.secrets.write_text(
            "REGISTRY_RUNTIME_PASSWORD=offline\nREGISTRY_BUILDER_PASSWORD=offline\n"
        )
        self.secrets.chmod(0o600)
        self.public_key = self.directory / "key.pub"
        self.public_key.write_text("ssh-ed25519 offline-fixture\n")

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
        self, script_name: str, arguments: tuple[str, ...], **overrides: str
    ) -> subprocess.CompletedProcess[str]:
        # Do not inherit a host's live resource selectors or readiness settings.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(
                ("OS_", "PLATFORM_", "EXPECTED_", "RETAINED_", "BOOTSTRAP_", "NOMAD_")
            )
            and key not in {"TEMPLATE", "FLAVOR_ID"}
        }
        environment.update(
            {
                "OSC": str(self.fake_openstack),
                "FAKE_OPENSTACK_STATE": str(self.state_path),
                "PLATFORM_CONFIG": str(ROOT / "config" / "platform.example.json"),
                "OS_PROJECT_NAME": "example-project",
                "PKI_DIR": str(self.pki),
                "STORAGE_SECRETS_FILE": str(self.secrets),
                "BUILDER_OPERATOR_PUBLIC_KEY": str(self.public_key),
                "NOMAD": str(self.fake_openstack),
                "NOMAD_ATTEMPTS": "1",
                "NOMAD_POLL_INTERVAL": "0",
                "BOOTSTRAP_MARKER": MARKER,
                "BOOTSTRAP_ATTEMPTS": "3",
                "BOOTSTRAP_POLL_INTERVAL": "0",
                "FLAVOR_NAME": "worker-small",
                "IMAGE_NAME": IMAGE_ID,
                "TMPDIR": str(self.directory),
            }
        )
        environment.update(overrides)
        return subprocess.run(
            [str(ROOT / "infra" / "openstack" / script_name), *arguments],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
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

    def test_restricted_project_verification_does_not_call_project_show(self) -> None:
        self.write_state(port_name="unused", description="unused", project_name="wrong-project")
        result = self.run_lifecycle("builder_lifecycle.sh", ("show", APP_ID))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.read_state()["calls"]
        self.assertEqual(calls[0], ["token", "issue", "-f", "value", "-c", "project_id"])
        self.assertFalse(any(call[:2] == ["project", "show"] for call in calls))

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

    def seed(self, role: str, *, existing=True, port_exists=True, retained=False):
        short_id = APP_ID.replace("-", "")[:12]
        name = f"example-{role}-{short_id}"
        props = {"app_platform_managed_by": "platform"}
        if role == "builder":
            props["app_platform_build_id"] = APP_ID
            description = f"managed-by=platform;build-id={APP_ID}"
        else:
            props.update(
                app_platform_application_id=APP_ID, app_platform_application_slug="demo-app"
            )
            description = f"managed-by=platform;application-id={APP_ID};application-slug=demo-app"
        self.write_state(port_name=f"{name}-v4", description=description)
        state = self.read_state()
        state.update(new_server_id=SERVER_ID, new_port_id=PORT_ID)
        if existing:
            state["server"] = dict(
                id=SERVER_ID,
                name=name,
                status="ACTIVE",
                project_id=PROJECT_ID,
                image={"id": IMAGE_ID},
                flavor={"original_name": "worker-small"},
                properties=props,
            )
            state["port"].update(device_id=SERVER_ID, device_owner="compute:nova")
        if retained:
            identity = dict(
                application_id=APP_ID,
                request_id=OTHER_ID,
                project_id=PROJECT_ID,
                port_id=PORT_ID,
                network_id=NETWORK_ID,
                subnet_id=SUBNET_ID,
                security_group_id=GROUP_ID,
                address="192.0.2.40",
                name=f"example-app-{APP_ID}-primary-v4",
                description=f"app-platform:app-fixed-port:{APP_ID}:{OTHER_ID}",
            )
            state["retained"] = identity
            state["port"].update(
                name=identity["name"],
                description=identity["description"],
                project_id=PROJECT_ID,
                network_id=NETWORK_ID,
                fixed_ips=[dict(subnet_id=SUBNET_ID, ip_address=identity["address"])],
                security_group_ids=[GROUP_ID],
                port_security_enabled=True,
                allowed_address_pairs=[],
                device_owner="compute:nova" if existing else "",
                **{"binding:host_id": ""},
            )
        if not port_exists:
            state["port"] = None
        self.save_state(state)
        return state

    def save_state(self, state):
        self.state_path.write_text(json.dumps(state))

    def invoke(self, role, action, **environment):
        if identity := self.read_state().get("retained"):
            environment["RETAINED_PORT_JSON"] = json.dumps(identity)
        arguments = (action, APP_ID) if role == "builder" else (action, APP_ID, "demo-app")
        return self.run_lifecycle(f"{role}_lifecycle.sh", arguments, **environment)

    def calls(self, *prefix):
        return [call for call in self.read_state()["calls"] if call[: len(prefix)] == list(prefix)]

    def mutations(self):
        return [call for call in self.calls() if call[1] in {"create", "delete"}]

    def assert_counts(self, *, server_list, server_show, port_list, port_show, console=0, node=0):
        for prefix, count in (
            (("server", "list"), server_list),
            (("server", "show"), server_show),
            (("port", "list"), port_list),
            (("port", "show"), port_show),
            (("console", "log"), console),
            (("node", "status"), node),
        ):
            self.assertEqual(len(self.calls(*prefix)), count, (prefix, self.calls()))
        self.assertEqual(len(self.calls("token", "issue")), 1)
        self.assertFalse(self.calls("project", "show"))

    def test_show_uses_status_from_the_single_full_observation(self):
        for role in ("builder", "worker"):
            for status in ("ACTIVE", "BUILD", "ERROR"):
                for status_key in ("status", "Status"):
                    with self.subTest(role=role, status=status, key=status_key):
                        state = self.seed(role)
                        state["server"].pop("status")
                        state["server"][status_key] = status
                        self.save_state(state)
                        result = self.invoke(role, "show")
                        self.assertEqual(result.returncode, 0, result.stderr)
                        observed = json.loads(result.stdout)
                        self.assertEqual(observed["server"]["id"], SERVER_ID)
                        self.assertEqual(observed["port"]["id"], PORT_ID)
                        self.assertEqual(observed["server"]["status"], status)
                        self.assertEqual(observed["ready"], status == "ACTIVE")
                        # Previously: 1 server list + 2 shows (full, status).
                        self.assert_counts(
                            server_list=1,
                            server_show=1,
                            port_list=1,
                            port_show=1,
                            console=int(status == "ACTIVE"),
                            node=2 if role == "worker" and status == "ACTIVE" else 0,
                        )
                        self.assertFalse(self.mutations())

    def test_existing_create_reuses_validated_ids_and_seeds_first_bootstrap_poll(self):
        for role, retained in (("builder", False), ("worker", False), ("worker", True)):
            with self.subTest(role=role, retained=retained):
                self.seed(role, retained=retained)
                result = self.invoke(role, "create", FLAVOR_NAME="", IMAGE_NAME="")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"{role} bootstrap ready: {SERVER_ID}", result.stdout)
                # Previously builder: 3 lists + 6 shows; worker: 2 lists + 4 shows.
                # The second show now is only the unchanged final CLI output.
                self.assert_counts(
                    server_list=1,
                    server_show=2,
                    port_list=1,
                    port_show=1,
                    console=1,
                    node=2 if role == "worker" else 0,
                )
                self.assertFalse(self.calls("flavor", "show"))
                self.assertFalse(self.mutations())
                self.assertEqual(
                    self.calls("server", "show")[-1],
                    ["server", "show", SERVER_ID, "-f", "value", "-c", "status", "-c", "addresses"],
                )

    def test_new_create_reobserves_each_mutation_without_redundant_name_resolution(self):
        for role, retained, port_exists in (
            ("builder", False, False),
            ("builder", False, True),
            ("worker", False, False),
            ("worker", False, True),
            ("worker", True, True),
        ):
            with self.subTest(role=role, retained=retained, port_exists=port_exists):
                self.seed(role, existing=False, port_exists=port_exists, retained=retained)
                result = self.invoke(role, "create")
                self.assertEqual(result.returncode, 0, result.stderr)
                # Former server read totals: builder 8, ordinary/retained worker 9.
                server_lists = (3 if role == "worker" else 2) + int(not port_exists)
                self.assert_counts(
                    server_list=server_lists,
                    server_show=2,
                    port_list=1 if retained else server_lists,
                    port_show=3 if role == "worker" else 2,
                    console=1,
                    node=2 if role == "worker" else 0,
                )
                create = self.calls("server", "create")
                self.assertEqual(len(create), 1)
                self.assertEqual(create[0][create[0].index("--port") + 1], PORT_ID)
                self.assertIn("--use-config-drive", create[0])
                self.assertEqual("--wait" in create[0], role == "worker")
                self.assertNotIn("--network", create[0])
                self.assertEqual(len(self.calls("port", "create")), int(not port_exists))
                calls = self.calls()
                create_index = calls.index(create[0])
                self.assertTrue(any(call[:2] == ["port", "show"] for call in calls[:create_index]))
                self.assertTrue(
                    any(call[:2] == ["port", "show"] for call in calls[create_index + 1 :])
                )
                if role == "worker":
                    self.assertIn("192.0.2.40", self.read_state()["user_data"])

    def test_delete_skips_unused_readiness_but_reobserves_after_mutation(self):
        for role, retained in (("builder", False), ("worker", False), ("worker", True)):
            with self.subTest(role=role, retained=retained):
                self.seed(role, retained=retained)
                result = self.invoke(role, "delete")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_counts(
                    server_list=2,
                    server_show=1,
                    port_list=1 if retained else 2,
                    port_show=2 if retained else 1,
                )
                expected = [["server", "delete", "--wait", SERVER_ID]]
                if not retained:
                    expected.append(["port", "delete", PORT_ID])
                self.assertEqual(self.mutations(), expected)
                state = self.read_state()
                self.assertIsNone(state["server"])
                if retained:
                    self.assertEqual(state["port"]["id"], PORT_ID)
                    self.assertEqual(state["port"]["device_id"], "")
                else:
                    self.assertIsNone(state["port"])

    def test_bootstrap_only_subsequent_polls_read_status_and_fail_on_error(self):
        for role in ("builder", "worker"):
            for outcome in ("ready", "initial-error", "later-error", "timeout"):
                with self.subTest(role=role, outcome=outcome):
                    state = self.seed(role)
                    state["console_logs"] = ["not ready", MARKER]
                    if outcome == "initial-error":
                        state["server"]["status"] = "ERROR"
                    elif outcome == "later-error":
                        state["after"] = [
                            dict(command=["console", "log"], patch={"server": {"status": "ERROR"}})
                        ]
                    elif outcome == "timeout":
                        state["console_logs"] = ["not ready"] * 3
                    self.save_state(state)
                    result = self.invoke(role, "create")
                    self.assertEqual(result.returncode == 0, outcome == "ready", result.stderr)
                    if "error" in outcome:
                        self.assertIn("entered ERROR state", result.stderr)
                    elif outcome == "timeout":
                        self.assertIn("timed out", result.stderr)
                    self.assert_counts(
                        server_list=1,
                        server_show={
                            "ready": 3,
                            "initial-error": 1,
                            "later-error": 2,
                            "timeout": 3,
                        }[outcome],
                        port_list=1,
                        port_show=1,
                        console={"ready": 2, "initial-error": 0, "later-error": 1, "timeout": 3}[
                            outcome
                        ],
                        node=2 if role == "worker" and outcome == "ready" else 0,
                    )
                    polls = [
                        call
                        for call in self.calls("server", "show")
                        if call[-2:] == ["-c", "status"]
                    ]
                    self.assertEqual(
                        len(polls),
                        2 if outcome == "timeout" else int(outcome in {"ready", "later-error"}),
                    )
                    self.assertFalse(self.mutations())

    def test_worker_readiness_still_requires_healthy_nomad_identity(self):
        for action in ("show", "create"):
            with self.subTest(action=action):
                state = self.seed("worker")
                state["node"] = {"Meta": {"application_id": OTHER_ID}}
                self.save_state(state)
                result = self.invoke("worker", action)
                if action == "show":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(json.loads(result.stdout)["ready"])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("timed out waiting for worker Nomad", result.stderr)
                self.assert_counts(
                    server_list=1, server_show=1, port_list=1, port_show=1, console=1, node=2
                )

    def test_captured_observations_fail_closed_on_lookup_show_and_ownership_errors(self):
        for role in ("builder", "worker"):
            for action in ("show", "create", "delete"):
                for fault in (
                    "duplicate-server",
                    "duplicate-port",
                    "server-uuid",
                    "port-uuid",
                    "metadata",
                    "attachment",
                    "port-owner",
                    "missing-port",
                    "list-failure",
                    "show-failure",
                    "malformed-list",
                ):
                    with self.subTest(role=role, action=action, fault=fault):
                        state = self.seed(role)
                        if fault.startswith("duplicate-"):
                            resource = fault.removeprefix("duplicate-")
                            state[resource + "_rows"] = [
                                {"ID": identifier, "Name": state[resource]["name"]}
                                for identifier in (state[resource]["id"], OTHER_ID)
                            ]
                        elif fault.endswith("-uuid"):
                            resource = fault.removesuffix("-uuid")
                            state[resource + "_show"] = {**state[resource], "id": OTHER_ID}
                        elif fault == "metadata":
                            state["server"]["properties"]["app_platform_managed_by"] = "foreign"
                        elif fault == "attachment":
                            state["port"]["device_id"] = OTHER_ID
                        elif fault == "port-owner":
                            state["port"]["description"] = "foreign"
                        elif fault == "missing-port":
                            state["port"] = None
                        elif fault == "list-failure":
                            state["fail"] = ["server", "list"]
                        elif fault == "show-failure":
                            state["fail"] = ["server", "show"]
                        elif fault == "malformed-list":
                            state["server_rows"] = {"ID": SERVER_ID}
                        self.save_state(state)
                        result = self.invoke(role, action)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertFalse(self.mutations())
                        self.assertFalse(
                            list(self.directory.glob("tmp.*")), "observation scratch leaked"
                        )

    def test_both_roles_recheck_authenticated_project_for_every_invocation(self):
        for role in ("builder", "worker"):
            state = self.seed(role)
            self.assertEqual(self.invoke(role, "show").returncode, 0)
            state = self.read_state()
            state["project_id"] = OTHER_ID
            state["calls"] = []
            self.save_state(state)
            result = self.invoke(role, "delete")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("project UUID", result.stderr)
            self.assertEqual(self.calls(), [["token", "issue", "-f", "value", "-c", "project_id"]])

    def test_mutation_boundaries_reject_port_and_server_drift_before_readiness(self):
        for role in ("builder", "worker"):
            for mutation, drift in (
                ("port", "ownership"),
                ("server", "metadata"),
                ("server", "attachment"),
                ("server", "port-uuid"),
            ):
                with self.subTest(role=role, mutation=mutation, drift=drift):
                    state = self.seed(role, existing=False, port_exists=False)
                    patch = {
                        "ownership": {"port": {"description": "foreign"}},
                        "metadata": {"server": {"properties": {}}},
                        "attachment": {"port": {"device_id": OTHER_ID}},
                        "port-uuid": {"port": {"id": OTHER_ID}},
                    }[drift]
                    state["after"] = [dict(command=[mutation, "create"], patch=patch)]
                    self.save_state(state)
                    result = self.invoke(role, "create")
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertFalse(self.calls("console", "log"))
                    self.assertFalse(self.calls("node", "status"))
                    self.assertEqual(len(self.calls("server", "create")), int(mutation == "server"))
                    self.assertFalse(self.calls("server", "delete"))
                    self.assertFalse(self.calls("port", "delete"))

    def test_worker_pre_attach_observation_rejects_changed_selected_port(self):
        for retained in (False, True):
            for drift in ("address", "uuid", "security"):
                with self.subTest(retained=retained, drift=drift):
                    state = self.seed("worker", existing=False, retained=retained)
                    patch = {
                        "address": {
                            "fixed_ips": [dict(subnet_id=SUBNET_ID, ip_address="192.0.2.41")]
                        },
                        "uuid": {"id": OTHER_ID},
                        "security": {"description": "foreign"},
                    }[drift]
                    state["after"] = [dict(command=["flavor", "show"], patch={"port": patch})]
                    self.save_state(state)
                    result = self.invoke("worker", "create")
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertFalse(self.mutations())
                    self.assertFalse(self.calls("console", "log"))

    def test_worker_pre_attach_observation_refuses_newly_appeared_server(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                state = self.seed("worker", retained=retained)
                server = state["server"]
                state["server"] = None
                state["port"].update(device_id="", device_owner="")
                state["after"] = [
                    dict(
                        command=["flavor", "show"],
                        patch={
                            "server": server,
                            "port": {"device_id": SERVER_ID, "device_owner": "compute:nova"},
                        },
                    )
                ]
                self.save_state(state)
                result = self.invoke("worker", "create")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("changed before attachment", result.stderr)
                self.assertFalse(self.mutations())
                self.assertFalse(self.calls("console", "log"))

    def test_retained_security_and_exact_primary_nic_checks_run_in_captured_observations(self):
        for action in ("create", "delete"):
            for field, value in (
                ("project_id", OTHER_ID),
                ("network_id", OTHER_ID),
                ("security_group_ids", [OTHER_ID]),
                ("port_security_enabled", False),
                ("allowed_address_pairs", [{"ip_address": "0.0.0.0/0"}]),
                ("fixed_ips", [dict(subnet_id=OTHER_ID, ip_address="192.0.2.40")]),
                ("device_owner", "network:router_interface"),
                ("trunk_details", {"trunk_id": OTHER_ID}),
                ("server-project", OTHER_ID),
                ("extra-nic", OTHER_ID),
            ):
                with self.subTest(action=action, field=field):
                    state = self.seed("worker", retained=True)
                    if field == "server-project":
                        state["server"]["project_id"] = value
                    elif field == "extra-nic":
                        state["attached_ports"] = [{"ID": PORT_ID}, {"ID": value}]
                    else:
                        state["port"][field] = value
                    self.save_state(state)
                    result = self.invoke("worker", action)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertFalse(self.mutations())

    def test_retained_delete_rechecks_detachment_security_after_server_mutation(self):
        for field, value in (
            ("binding:host_id", "foreign"),
            ("port_security_enabled", False),
            ("device_owner", "compute:nova"),
        ):
            with self.subTest(field=field):
                state = self.seed("worker", retained=True)
                state["after"] = [
                    dict(command=["server", "delete"], patch={"port": {field: value}})
                ]
                self.save_state(state)
                result = self.invoke("worker", "delete")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.mutations(), [["server", "delete", "--wait", SERVER_ID]])
                self.assertEqual(len(self.calls("port", "show")), 2)
                self.assertIsNotNone(self.read_state()["port"])

    def test_lost_create_response_retry_freshly_validates_instead_of_creating_twice(self):
        for role, retained in (("builder", False), ("worker", False), ("worker", True)):
            for drift in (False, True):
                with self.subTest(role=role, retained=retained, drift=drift):
                    state = self.seed(role, existing=False, port_exists=retained, retained=retained)
                    state["after"] = [dict(command=["server", "create"], exit=1)]
                    self.save_state(state)
                    first = self.invoke(role, "create")
                    self.assertNotEqual(first.returncode, 0)
                    state = self.read_state()
                    self.assertIsNotNone(state["server"])
                    self.assertEqual(state["port"]["device_id"], SERVER_ID)
                    if drift:
                        state["server"]["properties"]["app_platform_managed_by"] = "foreign"
                    state["calls"] = []
                    self.save_state(state)
                    second = self.invoke(role, "create")
                    self.assertEqual(second.returncode == 0, not drift, second.stderr)
                    self.assertFalse(self.mutations())
                    self.assertEqual(len(self.calls("server", "list")), 1)
                    self.assertEqual(len(self.calls("port", "show")), 1)
                    self.assertEqual(
                        self.calls()[0], ["token", "issue", "-f", "value", "-c", "project_id"]
                    )


if __name__ == "__main__":
    unittest.main()
