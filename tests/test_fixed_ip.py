from __future__ import annotations

import copy
import json
import os
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
from openstack_platform.controller.http import HttpError
from openstack_platform.helper import production
from openstack_platform.validation import ValidationError
from tests import test_application_sizing as fixtures
from tests.fixtures.retained_openstack import start_cli

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
        self.addCleanup(start_cli(self.state_path, self.executable))
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
            TMPDIR=str(self.root),
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

    def assert_success(self, result):
        self.assertEqual(result[1].status, "succeeded", result[1].safe_error)
        return result[0]

    def port(self):
        record = service.get(self.connection, self.app_id)
        return self.state()["ports"][record["port_id"]]

    def test_reserve_deploy_maintenance_and_delete_preserves_then_releases_address(self):
        key = self.assert_success(self.reserve())
        self.assert_success(self.reserve(key))
        port_id = self.port()["id"]
        self.assert_success(self.fixture.deploy())
        current = db.get_application(self.connection, self.app_id)
        self.assertEqual(current.worker_port_id, port_id)
        self.assertEqual(self.port()["device_id"], current.worker_server_id)
        with self.assertRaises(openstack.DriftError):
            service.release_locked(
                self.connection, self.config, self.app_id, deadline=time.monotonic() + 10
            )
        previous_server = current.worker_server_id

        def observe(action, _values):
            if action == "app.build":
                self.assertEqual(set(self.state()["servers"]), {previous_server})
            if action == "app.worker.create":
                self.assertEqual(self.state()["servers"], {})

        self.observe_action = observe
        self.assert_success(
            self.fixture.post(
                f"/v1/applications/{self.app_id}/deployments",
                {**self.fixture.body, "commit": "b" * 40, "maintenance": True},
            )
        )
        current = db.get_application(self.connection, self.app_id)
        self.assertNotEqual(current.worker_server_id, previous_server)
        self.assertEqual(current.worker_port_id, port_id)
        self.assertEqual(self.port()["fixed_ips"], [dict(subnet_id=SUBNET, ip_address=ADDRESS)])
        self.assertEqual(len(self.state()["servers"]), 1)
        self.assert_success(
            self.fixture.post(f"/v1/applications/{self.app_id}/delete", {"confirmation": "commons"})
        )
        self.assertEqual(self.state()["servers"], {})
        self.assertEqual(self.state()["ports"], {})
        self.assertIsNone(service.get(self.connection, self.app_id))

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
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(HttpError):
                self.fixture.post(
                    f"/v1/applications/{self.app_id}/deployments",
                    {**self.fixture.body, "maintenance": value},
                )
        self.assertEqual(self.fixture.calls, [])

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
