from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform import fixed_ip, floating_ip, openstack
from openstack_platform.config import load_platform
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import fixed_ip_service as service
from openstack_platform.controller import public_ip_service
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.http import ControllerServer, HttpError
from openstack_platform.helper import production
from openstack_platform.validation import ValidationError
from tests import test_application_sizing as fixtures

ROOT = Path(__file__).resolve().parents[1]
NETWORK, SUBNET, GROUP = (f"00000000-0000-4000-8000-{n:012d}" for n in (1, 2, 3))
ADDRESS = "128.52.133.15"
REQUEST = dict(action="reserve", networkId=NETWORK, subnetId=SUBNET, address=ADDRESS)


class RetainedFixedIPTests(unittest.TestCase):
    """HTTP controller -> production worker helper -> real shell -> fake OSC/Nomad.

    Only cloud and scheduler/build/health endpoints are offline. No worker adapter
    mocks: primary-port parsing, config-drive creation, attachment and cleanup all
    execute the installed code's real paths, for ordinary and retained generations.
    """

    def setUp(self):
        real_verify = openstack.verify_project
        self.fixture = fixtures.ApplicationSizingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.api.close()
        self.connection = self.fixture.connection
        self.root = self.fixture.root
        self.app_id = self.fixture.app_id
        platform = load_platform(ROOT / "config/platform.example.json")
        self.config = replace(self.fixture.config, platform=platform)
        self.fixture.config = self.config
        self.executable = self.root / "fake-openstack"
        self.state_path = self.root / "cloud.json"
        self.executable.write_text(
            (ROOT / "tests/fixtures/retained_openstack.py")
            .read_text()
            .replace('Path(os.environ["FAKE_OPENSTACK_STATE"])', f"Path({str(self.state_path)!r})")
        )
        self.executable.chmod(0o755)
        self.state_path.write_text(
            json.dumps(
                dict(
                    project=platform.project_id,
                    prefix=platform.prefix,
                    namespace=platform.namespace,
                    metadata=openstack._metadata_prefix(platform),
                    network=NETWORK,
                    subnet=SUBNET,
                    group=GROUP,
                    network_name=platform.network,
                    ports={},
                    servers={},
                    calls=[],
                )
            )
        )
        pki = self.root / "pki"
        pki.mkdir()
        for name in ("internal-ca.pem", "nomad-worker.pem", "nomad-worker-key.pem"):
            (pki / name).write_text("offline fixture\n")
            (pki / name).chmod(0o600)
        secrets = self.root / "storage-secrets"
        secrets.write_text("REGISTRY_RUNTIME_PASSWORD=offline-fixture\n")
        secrets.chmod(0o600)
        # The installed worker launcher supplies fixed inventory/secret/tool
        # paths. Reproduce that boundary, not a relaxed runtime environment.
        import shlex

        self.worker_launcher = self.root / "worker"
        exports = dict(
            OSC=str(self.executable),
            PLATFORM_CONFIG=str(ROOT / "config/platform.example.json"),
            OS_PROJECT_NAME=platform.project_name,
            PKI_DIR=str(pki),
            STORAGE_SECRETS_FILE=str(secrets),
            NOMAD_ATTEMPTS="1",
            NOMAD_POLL_INTERVAL="0",
            BOOTSTRAP_ATTEMPTS="1",
            BOOTSTRAP_POLL_INTERVAL="0",
        )
        self.worker_launcher.write_text(
            "#!/bin/bash\n"
            + "\n".join(f"export {key}={shlex.quote(value)}" for key, value in exports.items())
            + f'\nexec {shlex.quote(str(ROOT / "infra/openstack/worker_lifecycle.sh"))} "$@"\n'
        )
        self.worker_launcher.chmod(0o755)
        provider_type = fixed_ip.Provider

        def provider_factory(platform, *, deadline, executable=None):
            if executable is not None:
                self.assertEqual(executable, str(self.executable))
            provider = provider_type(platform, deadline=deadline, executable=str(self.executable))
            provider.verify = lambda: real_verify(platform, **provider._bounds())
            return provider

        for patch in (
            mock.patch.dict(
                os.environ,
                dict(
                    OSC=str(self.executable),
                    FAKE_OPENSTACK_STATE=str(self.state_path),
                    PLATFORM_CONFIG=str(ROOT / "config/platform.example.json"),
                    OS_PROJECT_NAME=platform.project_name,
                    PKI_DIR=str(pki),
                    STORAGE_SECRETS_FILE=str(secrets),
                ),
            ),
            mock.patch.object(fixed_ip, "Provider", side_effect=provider_factory),
            mock.patch.object(
                production, "helper_runtime", return_value=SimpleNamespace(platform=platform)
            ),
            mock.patch.object(
                app,
                "provider_command",
                side_effect=lambda _p, tool: (
                    (str(self.worker_launcher),) if tool == "worker" else (str(self.executable),)
                ),
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.fixture.api = ControllerAPI(
            self.connection, self.config, self.root, helper_caller=self.helper
        )
        self.fixture.fixture.api = self.fixture.api
        self.fixture.router = self.fixture.api.router()
        self.base = f"/v1/admin/applications/{self.app_id}/fixed-ip"

    def state(self):
        return json.loads(self.state_path.read_text())

    def change(self, fn):
        value = self.state()
        fn(value)
        self.state_path.write_text(json.dumps(value))

    def helper(self, config, action, values, **kwargs):
        if observer := getattr(self, "observe_action", None):
            observer(action, values)
        if action.startswith("app.worker."):
            self.fixture.calls.append((action, copy.deepcopy(values)))
            result = production._provider_app(action, values)
            if (
                action in {"app.worker.create", "app.worker.observe"}
                and result.get("absent") is not True
            ):
                self.fixture.workers[values["applicationId"]] = result
            elif action == "app.worker.delete":
                self.fixture.workers.pop(values["applicationId"], None)
            return result
        if action == "app.stop":
            job = self.fixture.jobs.get(values["jobId"])
            if job is None or app.nomad_candidate_identity(job) != (
                values["candidateJobSha256"],
                values["candidateImage"],
            ):
                raise app.ApplicationError("exact stop identity unavailable")
            self.fixture.calls.append((action, copy.deepcopy(values)))
            return {"jobStopped": True}
        if action == "app.manifest.verify":
            return {**values, "available": True}
        if action == "app.env.list":
            return {"keys": []}
        result = self.fixture.helper(config, action, values, **kwargs)
        if action == "app.build":
            result["image"] = result["image"].replace(
                "storage.internal", config.platform.get("addresses.storage")
            )
        return result

    def reserve(self, key=None):
        return self.fixture.post(self.base, REQUEST, key)

    def disable(self):
        return self.fixture.post(f"/v1/applications/{self.app_id}/disable", {})

    def assert_success(self, result):
        self.assertEqual(result[1].status, "succeeded", result[1].safe_error)
        return result[0]

    def port(self):
        record = service.get(self.connection, self.app_id)
        return self.state()["ports"][record["port_id"]]

    def test_reuse_failure_and_rollback_preserve_primary_ip_without_provider_mutations(self):
        self.assert_success(self.reserve())
        first = self.assert_success(self.fixture.deploy())
        original_port = copy.deepcopy(self.port())
        original_record = service.get(self.connection, self.app_id)
        server_id = db.get_application(self.connection, self.app_id).worker_server_id
        calls = len(self.state()["calls"])
        base = f"/v1/admin/applications/{self.app_id}"
        body = {**self.fixture.body, "maintenance": True, "reuseWorker": True, "commit": "b" * 40}
        self.assert_success(self.fixture.post(base + "/deployments", body))
        self.fixture.fail_health = True
        _, failed = self.fixture.post(base + "/deployments", {**body, "commit": "c" * 40})
        self.assertEqual(failed.status, "failed", failed.safe_error)
        self.assertEqual(self.fixture.jobs, {})
        self.fixture.fail_health = False
        plan = self.fixture.router.dispatch(
            "GET", base + f"/rollback-plan?deploymentId={first}&reuseWorker=true", {}, None
        ).body
        self.assert_success(
            self.fixture.post(base + "/rollback", {"plan": plan, "confirmation": "commons"})
        )
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_server_id, server_id
        )
        self.assertEqual(self.port(), original_port)
        self.assertEqual(service.get(self.connection, self.app_id), original_record)
        self.assertEqual(set(self.state()["servers"]), {server_id})
        for command in self.state()["calls"][calls:]:
            self.assertFalse(
                set(command)
                & {"create", "delete", "set", "unset", "resize", "rebuild", "add", "remove"},
                command,
            )
        self.assertEqual(len(self.fixture.jobs), 1)

    def test_reuse_cannot_implicitly_migrate_an_ordinary_worker_to_a_reserved_port(self):
        self.assert_success(self.fixture.deploy())
        self.assert_success(self.reserve())
        before = copy.deepcopy(self.state())
        _, rejected = self.fixture.post(
            f"/v1/admin/applications/{self.app_id}/deployments",
            {**self.fixture.body, "maintenance": True, "reuseWorker": True},
        )
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(self.state(), before)
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)

    def test_helper_uses_fixed_authenticated_openstack_command(self):
        self.assert_success(self.reserve())
        record = service.get(self.connection, self.app_id)
        with mock.patch.object(fixed_ip, "Provider") as provider:
            provider.return_value.show.return_value = {"device_id": None}
            with self.assertRaises(ValidationError):
                production._provider_app(
                    "app.worker.observe",
                    {
                        "applicationId": self.app_id,
                        "slug": "commons",
                        "retainedPort": service.helper_identity(record),
                    },
                )
            self.assertEqual(provider.call_args.kwargs["executable"], str(self.executable))
        source = (ROOT / "nix/roles/admin.nix").read_text()
        self.assertIn(
            '"L+ ${root}/bin/${namespace}-openstack - - - - ${openstackClient}/bin/platform-openstack"',
            source,
        )

    def test_plan_explicit_address_outside_pool_and_reserve_idempotency(self):
        plan = self.fixture.router.dispatch(
            "POST",
            self.base + "/plan",
            {},
            {key: value for key, value in REQUEST.items() if key != "action"},
        ).body
        self.assertTrue(plan["supported"])
        self.assertEqual(plan["cidr"], "128.52.128.0/18")
        self.assertEqual(self.state()["ports"], {})
        key = self.assert_success(self.reserve())
        self.assertEqual(self.port()["fixed_ips"], [dict(subnet_id=SUBNET, ip_address=ADDRESS)])
        self.assert_success(self.reserve(key))
        creates = [a for a in self.state()["calls"] if a[:2] == ["port", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertIn(f"subnet={SUBNET},ip-address={ADDRESS}", creates[0])
        self.assertNotIn("--enable-port-security", creates[0])
        self.assertNotIn("--disable-port-security", creates[0])
        self.assertTrue(self.port()["port_security_enabled"])
        self.assertFalse(
            any(a[0] in {"floating", "router", "quota"} for a in self.state()["calls"])
        )
        with self.assertRaises(HttpError):
            self.fixture.post(self.base, {**REQUEST, "address": "128.52.133.16"}, key)
        read = self.fixture.router.dispatch("GET", self.base, {}, None).body
        self.assertEqual(read["attachment"], "detached")
        self.assertIsNone(read["serverId"])

    def test_lost_allocation_response_recovers_only_exact_marker_no_second_create(self):
        self.change(lambda s: s.update(fault="port.create.after"))
        key, operation = self.reserve()
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(service.get(self.connection, self.app_id)["phase"], "allocating")
        dhcp_id = str(uuid.uuid4())
        self.change(
            lambda s: s["ports"].update(
                {
                    dhcp_id: dict(
                        id=dhcp_id,
                        name="unrelated-dhcp",
                        device_owner="network:dhcp",
                        device_id="dhcp-host-opaque-identifier",
                        fixed_ips=[],
                        security_group_ids=[],
                    )
                }
            )
        )
        self.assert_success(self.reserve(key))
        self.assertEqual(len([a for a in self.state()["calls"] if a[:2] == ["port", "create"]]), 1)
        self.assert_success(self.fixture.post(self.base, {"action": "release"}))
        self.assertEqual(set(self.state()["ports"]), {dhcp_id})
        self.assertFalse(any(a[:3] == ["port", "show", dhcp_id] for a in self.state()["calls"]))

    def test_unknown_zero_allocation_never_recreates_or_releases(self):
        self.change(lambda s: s.update(fault="port.create.before"))
        key, operation = self.reserve()
        self.assertEqual(operation.status, "recovery_required")
        _, retry = self.reserve(key)
        self.assertEqual(retry.status, "recovery_required")
        self.assertEqual(len([a for a in self.state()["calls"] if a[:2] == ["port", "create"]]), 1)
        with self.assertRaises(openstack.RecoveryRequired):
            service.release_locked(
                self.connection, self.config, self.app_id, deadline=time.monotonic() + 10
            )

    @unittest.skipUnless(shutil.which("curl"), "curl transport acceptance requires curl")
    def test_curl_over_unix_socket_repeats_maintenance_fixed_port_deployments(self):
        socket = str(self.root / "acceptance.sock")
        connection = db.connect(
            self.root / "platform.sqlite3", create=False, check_same_thread=False
        )
        api = ControllerAPI(connection, self.config, self.root, helper_caller=self.helper)
        server = ControllerServer(socket, api.router("privileged"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def curl(path, body=None, key=None):
            command = [
                "curl",
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--unix-socket",
                socket,
            ]
            if body is not None:
                command += [
                    "-H",
                    "Content-Type: application/json",
                    "-H",
                    f"Idempotency-Key: {key}",
                    "--data-binary",
                    "@-",
                ]
            result = subprocess.run(
                command + ["http://localhost" + path],
                input=None if body is None else json.dumps(body),
                text=True,
                capture_output=True,
                timeout=30,
                check=True,
            )
            return json.loads(result.stdout)

        def post(path, body, key=None):
            key = key or str(uuid.uuid4())
            submitted = curl(path, body, key)
            for _ in range(300):
                operation = curl(submitted["statusUrl"])
                if operation["status"] in {"succeeded", "failed", "recovery_required"}:
                    return key, db.get_operation(self.connection, key)
                time.sleep(0.2)
            self.fail("curl deployment polling timed out")

        try:
            self.assertIn("maintenance-after-build-v1", curl("/v1/admin/capabilities")["features"])
            with mock.patch.object(self.fixture, "post", side_effect=post):
                self._exercise_maintenance_cycles()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            api.close()
            connection.close()

    def test_maintenance_capability_and_consent_are_privileged_and_typed(self):
        value = self.fixture.router.dispatch("GET", "/v1/admin/capabilities", {}, None).body
        self.assertIn("maintenance-after-build-v1", value["features"])
        with self.assertRaises(HttpError):
            self.fixture.api.router("project").dispatch("GET", "/v1/admin/capabilities", {}, None)
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(HttpError):
                self.fixture.post(
                    f"/v1/admin/applications/{self.app_id}/deployments",
                    {**self.fixture.body, "plan": self.fixture.plan(), "maintenance": value},
                )
        with self.assertRaises(HttpError):
            self.fixture.post(
                f"/v1/applications/{self.app_id}/deployments",
                {**self.fixture.body, "maintenance": True},
            )
        self.assertEqual(self.fixture.calls, [])

    def test_maintenance_flavor_drift_after_build_does_not_stop_predecessor(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        self.assert_success(self.reserve())
        events = []

        def observe(action, values):
            events.append(action)
            if action == "app.build":
                patch = mock.patch.object(
                    openstack,
                    "observe_flavor_capacity",
                    return_value=replace(fixtures.XL, ram_mib=8192),
                )
                patch.start()
                self.addCleanup(patch.stop)

        self.observe_action = observe
        _, failed = self.fixture.post(
            f"/v1/admin/applications/{self.app_id}/deployments",
            {**self.fixture.body, "plan": self.fixture.plan(), "maintenance": True},
        )
        self.assertEqual(failed.status, "recovery_required")
        self.assertNotIn("app.remove", events)
        self.assertNotIn("app.worker.delete", events)
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)
        self.assertEqual(len(self.state()["servers"]), 1)

    def test_retained_port_drift_is_detected_before_maintenance_stop(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        self.assert_success(self.reserve())
        pid = self.port()["id"]
        self.change(lambda state: state["ports"][pid].update(port_security_enabled=False))
        events = []
        self.observe_action = lambda action, values: events.append(action)
        _, failed = self.fixture.post(
            f"/v1/admin/applications/{self.app_id}/deployments",
            {**self.fixture.body, "plan": self.fixture.plan(), "maintenance": True},
        )
        self.assertEqual(failed.status, "recovery_required")
        self.assertNotIn("app.remove", events)
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)
        self.assertEqual(len(self.state()["servers"]), 1)

    def test_maintenance_builds_while_serving_then_cuts_over_without_overlap(self):
        self._exercise_maintenance_cycles()

    def _exercise_maintenance_cycles(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        self.assert_success(self.reserve())
        pid = self.port()["id"]
        for _ in range(2):
            previous = db.get_active_deployment(self.connection, self.app_id).deployment_id
            events = []

            def observe(action, values, events=events, previous=previous):
                events.append(action)
                connection = db.connect(self.root / "platform.sqlite3", create=False)
                try:
                    if action in {"app.build", "app.manifest.verify"}:
                        self.assertTrue(db.get_application(connection, self.app_id).desired_running)
                        self.assertEqual(len(self.state()["servers"]), 1)
                        self.assertEqual(
                            db.get_active_deployment(connection, self.app_id).deployment_id,
                            previous,
                        )
                    if action == "app.worker.create":
                        self.assertFalse(
                            db.get_application(connection, self.app_id).desired_running
                        )
                        self.assertEqual(self.state()["servers"], {})
                finally:
                    connection.close()

            self.observe_action = observe
            self.assert_success(
                self.fixture.post(
                    f"/v1/admin/applications/{self.app_id}/deployments",
                    {**self.fixture.body, "plan": self.fixture.plan(), "maintenance": True},
                )
            )
            self.assertLess(events.index("app.build"), events.index("app.remove"))
            self.assertLess(events.index("app.worker.delete"), events.index("app.worker.create"))
            self.assertEqual(db.get_application(self.connection, self.app_id).worker_port_id, pid)
            self.assertEqual(len(self.state()["servers"]), 1)

    def test_ordinary_maintenance_health_failure_can_enable_previous_accepted_code(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        previous = db.get_active_deployment(self.connection, self.app_id).deployment_id
        self.fixture.fail_health = True
        _, failed = self.fixture.post(
            f"/v1/admin/applications/{self.app_id}/deployments",
            {**self.fixture.body, "plan": self.fixture.plan(), "maintenance": True},
        )
        self.assertEqual(failed.status, "failed", failed.safe_error)
        self.assertEqual(failed.cleanup_state, "confirmed")
        self.assertEqual(self.state()["servers"], {})
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, previous
        )
        self.assertFalse(db.get_application(self.connection, self.app_id).desired_running)
        self.fixture.fail_health = False
        self.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/enable", {}))
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, previous
        )
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)
        self.assertEqual(len(self.state()["servers"]), 1)

    def test_maintenance_retry_reuses_build_worker_and_does_not_stop_again(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        self.assert_success(self.reserve())
        body = {**self.fixture.body, "plan": self.fixture.plan(), "maintenance": True}
        events = []
        fail = True

        def observe(action, values):
            nonlocal fail
            events.append(action)
            if action == "app.worker.capacity" and fail:
                fail = False
                raise RuntimeError("injected capacity outage")

        self.observe_action = observe
        key, failed = self.fixture.post(f"/v1/admin/applications/{self.app_id}/deployments", body)
        self.assertEqual(failed.status, "recovery_required")
        self.assertFalse(db.get_application(self.connection, self.app_id).desired_running)
        self.assertEqual(len(self.state()["servers"]), 1)
        events.clear()
        self.assert_success(
            self.fixture.post(f"/v1/admin/applications/{self.app_id}/deployments", body, key)
        )
        self.assertNotIn("app.build", events)
        self.assertNotIn("app.remove", events[: events.index("app.deploy")])
        self.assertNotIn("app.worker.create", events)
        self.assertIsNone(db.get_operation(self.connection, key).safe_error)
        self.assertTrue(db.get_application(self.connection, self.app_id).desired_running)

    def test_maintenance_build_failure_preserves_running_predecessor(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        self.assert_success(self.reserve())
        before = db.get_application(self.connection, self.app_id)
        previous = db.get_active_deployment(self.connection, self.app_id).deployment_id
        events = []
        self.observe_action = lambda action, values: events.append(action)
        self.fixture.fail_action = "app.build"
        key, failed = self.fixture.post(
            f"/v1/admin/applications/{self.app_id}/deployments",
            {**self.fixture.body, "plan": self.fixture.plan(), "maintenance": True},
        )
        self.assertNotEqual(failed.status, "succeeded")
        self.assertNotIn("app.remove", events)
        self.assertNotIn("app.worker.delete", events)
        self.assertNotIn("app.env.set", events)
        self.assertEqual(db.get_application(self.connection, self.app_id), before)
        self.assertEqual(
            db.get_active_deployment(self.connection, self.app_id).deployment_id, previous
        )
        self.assertEqual(len(self.state()["servers"]), 1)

    def test_transition_ordinary_reserve_disable_fixed_then_static_redeploy_and_delete(self):
        self.assert_success(self.fixture.deploy(self.fixture.plan()))
        before = db.get_application(self.connection, self.app_id)
        old_port = before.worker_port_id
        self.assert_success(self.reserve())  # Enabled ordinary worker remains untouched.
        pid = self.port()["id"]
        self.assertEqual(service.get(self.connection, self.app_id)["worker_slot_id"], None)
        self.assertEqual(len(self.state()["servers"]), 1)
        self.assertEqual(self.port()["device_id"], "")
        from openstack_platform.controller.application_service import ApplicationService

        with self.assertRaises(ValidationError):
            ApplicationService(
                self.connection, self.config, self.root, helper_caller=self.helper
            ).enable(self.app_id)
        calls = len(self.fixture.calls)
        _, blocked = self.fixture.deploy()
        self.assertEqual(blocked.status, "failed")
        self.assertEqual(len(self.fixture.calls), calls)  # No build, port or workload mutation.
        self.assert_success(self.disable())
        self.assertNotIn(old_port, self.state()["ports"])
        self.assertEqual(self.port()["device_id"], "")
        first = self.assert_success(self.fixture.deploy())
        current = db.get_application(self.connection, self.app_id)
        self.assertEqual(current.worker_port_id, pid)
        self.assertEqual(current.worker_flavor, fixtures.XL.name)
        self.assertEqual(current.worker_port_name, self.port()["name"])
        self.assert_success(self.disable())
        self.assert_success(self.fixture.deploy())
        self.assertEqual(db.get_application(self.connection, self.app_id).worker_port_id, pid)
        self.assertEqual(len(self.state()["ports"]), 1)
        self.assertEqual(len(self.state()["servers"]), 1)
        self.assert_success(self.disable())
        plan = self.fixture.router.dispatch(
            "GET",
            f"/v1/admin/applications/{self.app_id}/rollback-plan?deploymentId={first}",
            {},
            None,
        ).body
        self.assert_success(
            self.fixture.post(
                f"/v1/admin/applications/{self.app_id}/rollback",
                {"plan": plan, "confirmation": "commons"},
            )
        )
        self.assertEqual(db.get_application(self.connection, self.app_id).worker_port_id, pid)
        self.assert_success(
            self.fixture.post(f"/v1/applications/{self.app_id}/delete", {"confirmation": "commons"})
        )
        self.assertEqual(self.state()["servers"], {})
        self.assertEqual(self.state()["ports"], {})
        self.assertIsNone(service.get(self.connection, self.app_id))

    def test_disable_retains_enable_reattaches_resize_preserves_and_failed_candidate_cleanup(self):
        self.assert_success(self.reserve())
        pid = self.port()["id"]
        self.assert_success(self.fixture.deploy())
        self.assert_success(self.disable())
        self.assertEqual(self.port()["device_id"], "")
        self.assert_success(self.fixture.post(f"/v1/applications/{self.app_id}/enable", {}))
        self.assertEqual(db.get_application(self.connection, self.app_id).worker_port_id, pid)
        self.assert_success(self.disable())
        self.assert_success(self.fixture.resize(self.fixture.plan()))
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_flavor, fixtures.XL.name
        )
        self.assert_success(self.disable())
        self.fixture.fail_health = True
        _, failed = self.fixture.deploy()
        self.assertEqual(failed.status, "failed", failed.safe_error)
        self.assertEqual(failed.cleanup_state, "confirmed")
        self.assertEqual(self.port()["device_id"], "")
        self.assertEqual(self.state()["servers"], {})
        self.fixture.fail_health = False
        self.assert_success(self.fixture.deploy())
        self.assertEqual(db.get_application(self.connection, self.app_id).worker_port_id, pid)
        self.assertEqual(
            db.get_application(self.connection, self.app_id).worker_flavor, fixtures.XL.name
        )

    def test_release_bound_refused_and_lost_delete_response_resumes(self):
        self.assert_success(self.reserve())
        self.assert_success(self.fixture.deploy())
        with self.assertRaises(openstack.DriftError):
            service.release_locked(
                self.connection, self.config, self.app_id, deadline=time.monotonic() + 10
            )
        self.assert_success(self.disable())
        self.change(lambda s: s.update(fault="port.delete.after"))
        key, operation = self.fixture.post(self.base, {"action": "release"})
        self.assertEqual(operation.status, "recovery_required")
        self.assert_success(self.fixture.post(self.base, {"action": "release"}, key))
        self.assertIsNone(service.get(self.connection, self.app_id))

    def test_drift_and_cross_app_fail_before_any_worker_or_release_mutation(self):
        self.assert_success(self.reserve())
        record = service.get(self.connection, self.app_id)
        identity = service.helper_identity(record)
        for key, value in (
            ("application_id", str(uuid.uuid4())),
            ("project_id", str(uuid.uuid4())),
            ("name", "foreign-name"),
            ("description", "foreign-marker"),
        ):
            with self.subTest(key=key), self.assertRaises((openstack.DriftError, ValidationError)):
                production._provider_app(
                    "app.worker.observe",
                    dict(
                        applicationId=self.app_id,
                        slug="commons",
                        retainedPort={**identity, key: value},
                    ),
                )
        port = self.port()
        mutations = [
            a for a in self.state()["calls"] if len(a) > 1 and a[1] in {"create", "delete"}
        ]
        for key, value in (
            ("project_id", str(uuid.uuid4())),
            ("network_id", str(uuid.uuid4())),
            ("security_group_ids", [str(uuid.uuid4())]),
            ("port_security_enabled", False),
            ("allowed_address_pairs", [dict(ip_address="0.0.0.0/0")]),
            ("fixed_ips", port["fixed_ips"] + [dict(subnet_id=SUBNET, ip_address="128.52.133.16")]),
            ("device_owner", "network:router_interface"),
            ("trunk_details", {"trunk_id": str(uuid.uuid4())}),
            ("device_id", str(uuid.uuid4())),
            ("binding:host_id", "foreign-host"),
        ):
            with self.subTest(key=key):
                self.change(
                    lambda s, key=key, value=value: s["ports"].update(
                        {port["id"]: {**port, key: value}}
                    )
                )
                with self.assertRaises((openstack.DriftError, ValidationError)):
                    production._provider_app(
                        "app.worker.delete",
                        dict(
                            applicationId=self.app_id,
                            slug="commons",
                            single=True,
                            retainedPort=identity,
                        ),
                    )
                with self.assertRaises(openstack.DriftError):
                    service.release_locked(
                        self.connection, self.config, self.app_id, deadline=time.monotonic() + 10
                    )
        self.assertEqual(
            mutations,
            [a for a in self.state()["calls"] if len(a) > 1 and a[1] in {"create", "delete"}],
        )

    def test_compact_provider_uuids_normalize_but_request_uuids_remain_strict(self):
        self.change(lambda s: s.update(compact=True))
        self.assert_success(self.reserve())
        pid = self.port()["id"]
        self.assert_success(self.fixture.deploy())
        read = self.fixture.router.dispatch("GET", self.base, {}, None).body
        self.assertEqual(read["portId"], pid)
        self.assertEqual(read["attachment"], "attached")
        self.assert_success(self.disable())
        self.assert_success(self.fixture.post(self.base, {"action": "release"}))
        with self.assertRaises(HttpError):
            self.fixture.post(self.base, {**REQUEST, "networkId": NETWORK.replace("-", "")})

    def test_unknown_worker_create_cannot_duplicate_cleanup_or_release(self):
        self.assert_success(self.reserve())
        self.change(lambda s: s.update(fault="server.create.before"))
        key, operation = self.fixture.deploy()
        self.assertEqual(operation.status, "recovery_required")
        self.assertTrue(service.get(self.connection, self.app_id)["worker_create_pending"])
        _, retry = self.fixture.post(
            f"/v1/applications/{self.app_id}/deployments", self.fixture.body, key
        )
        self.assertEqual(retry.status, "recovery_required")
        self.assertEqual(
            len([a for a in self.state()["calls"] if a[:2] == ["server", "create"]]), 1
        )
        with self.assertRaises(openstack.RecoveryRequired):
            service.release_locked(
                self.connection, self.config, self.app_id, deadline=time.monotonic() + 10
            )
        self.assertEqual(len(self.state()["ports"]), 1)

    def test_lost_worker_create_response_recovers_exact_attached_primary(self):
        self.assert_success(self.reserve())
        self.change(lambda s: s.update(fault="server.create.after"))
        key, operation = self.fixture.deploy()
        self.assertEqual(operation.status, "recovery_required")
        self.assert_success(
            self.fixture.post(f"/v1/applications/{self.app_id}/deployments", self.fixture.body, key)
        )
        self.assertFalse(service.get(self.connection, self.app_id)["worker_create_pending"])
        self.assertEqual(len(self.state()["servers"]), 1)
        self.assertEqual(
            len([a for a in self.state()["calls"] if a[:2] == ["server", "create"]]), 1
        )

    def test_mutual_exclusion_both_directions_and_staff_only_closed_api(self):
        self.assert_success(self.reserve())
        with (
            self.assertRaises(ValidationError),
            mock.patch.object(floating_ip, "Provider") as provider,
        ):
            public_ip_service.PublicIPService(self.connection, self.config, self.root).mutate(
                self.app_id, action="allocate", network_id=NETWORK
            )
        provider.assert_called_once()  # No provider methods, especially no allocation.
        provider.return_value.create.assert_not_called()
        self.assert_success(self.fixture.post(self.base, {"action": "release"}))
        public_ip_service._save(self.connection, self.app_id, {"floating_ip_id": str(uuid.uuid4())})
        _, operation = self.reserve()
        self.assertEqual(operation.status, "failed")
        self.assertIsNone(service.get(self.connection, self.app_id))
        for method, suffix, body in (
            ("GET", "", None),
            ("POST", "/plan", REQUEST),
            ("POST", "", REQUEST),
        ):
            with self.assertRaises(HttpError) as error:
                self.fixture.api.router("project").dispatch(method, self.base + suffix, {}, body)
            self.assertEqual(error.exception.status, 404)
        for body in (
            {"action": "release", "portId": str(uuid.uuid4())},
            {**REQUEST, "floatingIpId": str(uuid.uuid4())},
            {**REQUEST, "address": "128.53.133.15"},
        ):
            if body.get("address") == "128.53.133.15":
                with self.assertRaises(openstack.DriftError):
                    fixed_ip.Provider(self.config.platform, deadline=time.monotonic() + 10).plan(
                        NETWORK, SUBNET, body["address"]
                    )
            else:
                with self.assertRaises(HttpError):
                    self.fixture.post(self.base, body)


if __name__ == "__main__":
    unittest.main()
