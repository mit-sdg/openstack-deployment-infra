from __future__ import annotations

import copy
import json
import unittest
import uuid
from unittest import mock

from openstack_platform import floating_ip, openstack
from openstack_platform.controller import database as db
from openstack_platform.controller import public_ip_service as service
from openstack_platform.controller.http import HttpError
from openstack_platform.runtime import CommandResult
from tests import test_application_sizing as sizing_fixture


def identifier(number):
    return f"00000000-0000-4000-8000-{number:012d}"


EXTERNAL, NETWORK, SUBNET, ROUTER, ROUTER_PORT, GROUP, INGRESS, RULE, FIP = (
    identifier(n) for n in range(1, 10)
)


class Cloud:
    """Run the real provider validation against realistic OSC JSON, offline."""

    def __init__(self, config):
        self.platform = config.platform
        self.project = self.platform.project_id
        self.token_project = self.project
        self.quota = 10
        self.routers = True
        self.fips = {}
        self.ports = {}
        self.servers = {}
        self.calls = []
        self.mutations = []
        self.fault = None
        self.fault_after = False
        self.group = dict(id=GROUP, project_id=self.project, name=f"{self.platform.prefix}-worker")
        self.ingress = dict(
            id=INGRESS, project_id=self.project, name=f"{self.platform.prefix}-ingress"
        )
        self.rule = dict(
            id=RULE,
            project_id=self.project,
            security_group_id=GROUP,
            direction="ingress",
            remote_group_id=INGRESS,
            remote_ip_prefix=None,
        )

    def worker(self, slot, slug, value):
        short = slot.replace("-", "")[:12]
        value["serverName"] = f"{self.platform.prefix}-worker-{short}"
        value["portName"] = value["serverName"] + "-v4"
        self.ports[value["portId"]] = dict(
            id=value["portId"],
            project_id=self.project,
            name=value["portName"],
            device_id=value["serverId"],
            network_id=NETWORK,
            port_security_enabled=True,
            security_group_ids=[GROUP],
            description=f"managed-by=platform;application-id={slot};application-slug={slug}",
            fixed_ips=[dict(ip_address=f"10.0.0.{len(self.ports) + 10}", subnet_id=SUBNET)],
        )
        prefix = openstack._metadata_prefix(self.platform)
        self.servers[value["serverId"]] = dict(
            id=value["serverId"],
            project_id=self.project,
            name=value["serverName"],
            properties={
                f"{prefix}_managed_by": "platform",
                f"{prefix}_application_id": slot,
                f"{prefix}_application_slug": slug,
            },
        )

    def supplied(self, fid=FIP):
        self.fips[fid] = dict(
            id=fid,
            project_id=self.project,
            floating_network_id=EXTERNAL,
            floating_ip_address="198.51.100.70",
            port_id=None,
            fixed_ip_address=None,
            description="operator-owned",
            status="DOWN",
            router_id=None,
        )
        return fid

    def __call__(self, argv, **_bounds):
        args = list(argv[1:])
        if args[-2:] == ["--format", "json"]:
            args = args[:-2]
        self.calls.append(tuple(args))
        mutation = tuple(args[:3]) in {
            ("floating", "ip", "create"),
            ("floating", "ip", "set"),
            ("floating", "ip", "unset"),
            ("floating", "ip", "delete"),
        }
        fail = mutation and args[2] == self.fault
        if mutation:
            self.mutations.append(tuple(args))
        if fail and not self.fault_after:
            self.fault = None
            raise openstack.OpenStackError("ambiguous provider call before response")
        value = self.dispatch(args)
        if fail:
            self.fault = None
            raise openstack.OpenStackError("ambiguous provider call after commit")
        return CommandResult(tuple(argv), 0, json.dumps(value).encode(), b"", False, False)

    def dispatch(self, args):
        if args[:2] == ["token", "issue"]:
            return {"project_id": self.token_project}
        if args == ["quota", "show", "--network"]:
            return [{"Resource": "floating_ips", "Limit": self.quota}]
        if args[:2] == ["network", "show"]:
            if args[2] == EXTERNAL:
                return {"id": EXTERNAL, "router:external": True}
            assert args[2] == self.platform.network
            return dict(id=NETWORK, name=self.platform.network, subnets=[SUBNET])
        if args[:2] == ["subnet", "show"]:
            assert args[2] == SUBNET
            return dict(id=SUBNET, network_id=NETWORK, ip_version=4)
        if args[:2] == ["router", "list"]:
            return [{"ID": ROUTER}] if self.routers else []
        if args[:2] == ["router", "show"]:
            return dict(
                id=ROUTER,
                project_id=self.project,
                external_gateway_info=dict(network_id=EXTERNAL, enable_snat=True),
            )
        if args[:2] == ["port", "list"]:
            return [{"ID": ROUTER_PORT}]
        if args[:2] == ["port", "show"]:
            if args[2] == ROUTER_PORT:
                return dict(
                    id=ROUTER_PORT,
                    project_id=self.project,
                    device_id=ROUTER,
                    network_id=NETWORK,
                    device_owner="network:router_interface",
                    fixed_ips=[dict(subnet_id=SUBNET, ip_address="10.0.0.1")],
                )
            return copy.deepcopy(self.ports[args[2]])
        if args[:2] == ["server", "show"]:
            return copy.deepcopy(self.servers[args[2]])
        if args[:3] == ["security", "group", "show"]:
            return copy.deepcopy(self.group if args[3] == GROUP else self.ingress)
        if args[:4] == ["security", "group", "rule", "list"]:
            return [{"ID": RULE}]
        if args[:4] == ["security", "group", "rule", "show"]:
            return copy.deepcopy(self.rule)
        if args[:3] == ["floating", "ip", "list"]:
            return [{"ID": fid} for fid in self.fips]
        if args[:3] == ["floating", "ip", "show"]:
            return copy.deepcopy(self.fips[args[3]])
        if args[:3] == ["floating", "ip", "create"]:
            assert FIP not in self.fips, "must never repeat an uncertain allocation"
            self.supplied()
            self.fips[FIP]["description"] = args[args.index("--description") + 1]
            return copy.deepcopy(self.fips[FIP])
        if args[:3] == ["floating", "ip", "set"]:
            self.fips[args[-1]].update(
                port_id=args[args.index("--port") + 1],
                fixed_ip_address=args[args.index("--fixed-ip-address") + 1],
                router_id=ROUTER,
                status="ACTIVE",
            )
            return None
        if args[:3] == ["floating", "ip", "unset"]:
            self.fips[args[-1]].update(
                port_id=None, fixed_ip_address=None, status="DOWN", router_id=None
            )
            return None
        if args[:3] == ["floating", "ip", "delete"]:
            del self.fips[args[-1]]
            return None
        raise AssertionError(args)


class PublicIPTests(unittest.TestCase):
    def setUp(self):
        real_verify_project = openstack.verify_project
        self.fixture = sizing_fixture.ApplicationSizingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.connection = self.fixture.connection
        self.config = self.fixture.config
        self.app_id = self.fixture.app_id
        self.cloud = Cloud(self.config)
        provider_type = floating_ip.Provider

        def provider_factory(platform, deadline):
            provider = provider_type(platform, deadline=deadline, command_runner=self.cloud)
            provider.verify = lambda: real_verify_project(platform, **provider._bounds())
            return provider

        patch = mock.patch.object(floating_ip, "Provider", side_effect=provider_factory)
        patch.start()
        self.addCleanup(patch.stop)
        helper = self.fixture.helper

        def wrapped(config, action, values, **kwargs):
            if action == "app.worker.delete":
                worker = self.fixture.workers.get(values["applicationId"])
                if worker:
                    self.assertFalse(
                        any(ip["port_id"] == worker["portId"] for ip in self.cloud.fips.values()),
                        "predecessor cannot be deleted before FIP handover",
                    )
                    self.cloud.ports.pop(worker["portId"], None)
                    self.cloud.servers.pop(worker["serverId"], None)
            result = helper(config, action, values, **kwargs)
            if action == "app.worker.create":
                self.cloud.worker(values["applicationId"], values["slug"], result)
            return result

        self.fixture.api.helper_caller = wrapped
        self.fixture.api.applications.helper_caller = wrapped
        self.svc = service.PublicIPService(self.connection, self.config, self.fixture.root)

    def mutate(self, action, key=None, **kwargs):
        if action in {"allocate", "attach"}:
            kwargs["network_id"] = EXTERNAL
        if action == "attach":
            kwargs["floating_ip_id"] = FIP
        return self.svc.mutate(
            self.app_id, action=action, request_id=key or str(uuid.uuid4()), **kwargs
        )

    def deploy(self):
        key, operation = self.fixture.deploy()
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        return key

    def test_worker_reuse_keeps_existing_floating_ip_association_without_mutation(self):
        helper = self.fixture.api.helper_caller

        def wrapped(config, action, values, **bounds):
            if action == "app.stop":
                return {"jobStopped": True}
            if action == "app.manifest.verify":
                return {**values, "available": True}
            if action == "app.env.list":
                return {"keys": []}
            result = helper(config, action, values, **bounds)
            if action == "app.worker.create":
                result["imageId"] = values["workerImageId"]
            return result

        self.fixture.api.helper_caller = wrapped
        self.deploy()
        self.mutate("allocate")
        before = copy.deepcopy(self.cloud.fips)
        workers = copy.deepcopy(self.fixture.workers)
        self.cloud.mutations.clear()
        self.fixture.calls.clear()
        _, operation = self.fixture.post(
            f"/v1/admin/applications/{self.app_id}/deployments",
            {**self.fixture.body, "reuseWorker": True, "maintenance": True},
        )
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertEqual(self.cloud.fips, before)
        self.assertEqual(self.cloud.mutations, [])
        self.assertEqual(self.fixture.workers, workers)
        self.assertFalse(
            {"app.worker.create", "app.worker.delete"}
            & {action for action, _ in self.fixture.calls}
        )

    def test_default_deploy_resize_disable_enable_delete_never_contacts_floating_ip_provider(self):
        self.deploy()
        _, operation = self.fixture.resize(self.fixture.plan())
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        for action, body in (
            ("disable", {}),
            ("enable", {}),
            ("delete", {"confirmation": "commons"}),
        ):
            _, operation = self.fixture.post(f"/v1/applications/{self.app_id}/{action}", body)
            self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertEqual(self.cloud.calls, [])

    def test_plan_zero_quota_and_missing_router_prevents_allocation_without_mutation(self):
        self.cloud.quota = 0
        self.cloud.routers = False
        result = self.svc.plan(self.app_id, EXTERNAL)
        self.assertFalse(result["supported"])
        self.assertEqual(
            set(result["reasons"]),
            {"floating_ip_quota_exhausted", "no_unique_visible_snat_router_for_worker_subnet"},
        )
        with self.assertRaises(floating_ip.CapabilityUnavailable):
            self.mutate("allocate")
        self.assertIsNone(service.get(self.connection, self.app_id))
        self.assertIsNone(db.get_unfinished_operation(self.connection, f"app-{self.app_id}"))
        self.assertEqual(self.cloud.mutations, [])

    def test_reserve_before_first_deploy_then_resize_and_redeploy_keep_exact_address(self):
        reserved = self.mutate("allocate")
        self.assertIsNone(reserved["portId"])
        self.deploy()
        first = service.model(self.connection, self.app_id)
        _, resized = self.fixture.resize(self.fixture.plan())
        self.assertEqual(resized.status, "succeeded", resized.safe_error)
        second = service.model(self.connection, self.app_id)
        self.assertNotEqual(first["portId"], second["portId"])
        self.deploy()
        third = service.model(self.connection, self.app_id)
        self.assertEqual(
            {first["floatingIpId"], second["floatingIpId"], third["floatingIpId"]}, {FIP}
        )
        self.assertEqual(first["address"], third["address"])
        self.assertEqual(len(self.cloud.fips), 1)
        self.assertEqual(len(self.fixture.workers), 1)
        self.assertEqual(
            db.get_application(self.connection, self.app_id).url, "https://commons.example.test"
        )

    def test_candidate_failure_before_or_after_route_promotion_keeps_old_address(self):
        self.deploy()
        self.mutate("allocate")
        before = copy.deepcopy(self.cloud.fips[FIP])
        for promoted in (False, True):
            self.fixture.fail_health = not promoted
            self.fixture.fail_after_promotion = promoted
            _, operation = self.fixture.resize(self.fixture.plan())
            self.assertEqual(operation.status, "failed", operation.safe_error)
            self.assertEqual(self.cloud.fips[FIP], before)
            self.assertEqual(len(self.fixture.workers), 1)
            self.fixture.fail_health = False
            self.fixture.fail_after_promotion = False

    def test_reassignment_ambiguous_before_and_after_commit_retains_predecessor_and_recovers(self):
        self.deploy()
        self.mutate("allocate")
        for after in (False, True):
            plan = self.fixture.plan()
            self.cloud.fault, self.cloud.fault_after = "set", after
            key, operation = self.fixture.resize(plan)
            self.assertEqual(operation.status, "recovery_required", operation.safe_error)
            self.assertEqual(operation.phase, "deployment_healthy")
            self.assertEqual(len(self.fixture.workers), 2)
            pending = service.get(self.connection, self.app_id)
            self.assertEqual(pending["phase"], "assigning")
            set_count = sum(call[2] == "set" for call in self.cloud.mutations)
            _, resumed = self.fixture.resize(plan, key)
            self.assertEqual(resumed.status, "succeeded", resumed.safe_error)
            self.assertEqual(
                sum(call[2] == "set" for call in self.cloud.mutations), set_count + (not after)
            )
            self.assertEqual(len(self.fixture.workers), 1)

    def test_recovery_rechecks_candidate_health_and_does_not_cleanup_or_mutate_on_drift(self):
        self.deploy()
        self.mutate("allocate")
        plan = self.fixture.plan()
        self.cloud.fault = "set"
        key, operation = self.fixture.resize(plan)
        self.assertEqual(operation.status, "recovery_required")
        calls = len(self.cloud.mutations)
        self.fixture.fail_health = True
        _, operation = self.fixture.resize(plan, key)
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(len(self.cloud.mutations), calls)
        self.assertEqual(len(self.fixture.workers), 2)
        self.fixture.fail_health = False
        self.cloud.fips[FIP].update(port_id=identifier(999), fixed_ip_address="10.0.0.250")
        _, operation = self.fixture.resize(plan, key)
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(len(self.cloud.mutations), calls)
        self.assertEqual(len(self.fixture.workers), 2)

    def test_crash_after_verified_handover_before_cleanup_resumes_without_reassigning(self):
        self.deploy()
        self.mutate("allocate")
        self.fixture.fail_action = "app.remove"
        plan = self.fixture.plan()
        key, operation = self.fixture.resize(plan)
        self.assertEqual(operation.status, "recovery_required", operation.safe_error)
        self.assertEqual(service.get(self.connection, self.app_id)["phase"], "active")
        self.assertEqual(len(self.fixture.workers), 2)
        mutations = len(self.cloud.mutations)
        _, operation = self.fixture.resize(plan, key)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertEqual(len(self.cloud.mutations), mutations)
        self.assertEqual(len(self.fixture.workers), 1)

    def test_disable_preserves_charged_reservation_enable_gets_same_address(self):
        self.deploy()
        self.mutate("allocate")
        old = service.model(self.connection, self.app_id)
        _, operation = self.fixture.post(f"/v1/applications/{self.app_id}/disable", {})
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        reserved = service.model(self.connection, self.app_id)
        self.assertEqual(reserved["phase"], "reserved")
        self.assertIsNone(reserved["portId"])
        self.assertEqual(len(self.cloud.fips), 1)
        _, operation = self.fixture.post(f"/v1/applications/{self.app_id}/enable", {})
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        new = service.model(self.connection, self.app_id)
        self.assertEqual(new["address"], old["address"])
        self.assertNotEqual(new["portId"], old["portId"])

    def test_supplied_owned_ip_attaches_without_allocation_and_release_never_deletes_it(self):
        self.deploy()
        self.cloud.supplied()
        self.cloud.quota = 0  # Attaching an already owned allocation consumes no quota.
        key = str(uuid.uuid4())
        self.mutate("attach", key)
        count = len(self.cloud.mutations)
        self.mutate("attach", key)
        self.assertEqual(len(self.cloud.mutations), count)
        self.mutate("release")
        self.assertIsNone(self.cloud.fips[FIP]["port_id"])
        self.assertEqual(self.cloud.fips[FIP]["description"], "operator-owned")
        self.assertIsNone(service.get(self.connection, self.app_id))
        self.assertEqual([call[2] for call in self.cloud.mutations], ["set", "unset"])

    def test_supplied_foreign_or_associated_or_wrong_network_ip_is_never_touched(self):
        self.cloud.supplied()
        for key, bad in (
            ("project_id", identifier(199)),
            ("port_id", identifier(99)),
            ("floating_network_id", identifier(99)),
        ):
            original = copy.deepcopy(self.cloud.fips[FIP])
            self.cloud.fips[FIP][key] = bad
            with self.assertRaises(openstack.DriftError):
                self.mutate("attach")
            self.assertIsNone(service.get(self.connection, self.app_id))
            self.cloud.fips[FIP] = original
        self.assertEqual(self.cloud.mutations, [])

    def test_exact_floating_identity_and_association_drift_blocks_release(self):
        self.deploy()
        self.mutate("allocate")
        original = copy.deepcopy(self.cloud.fips[FIP])
        request = str(uuid.uuid4())
        for key, bad in (
            ("id", identifier(99)),
            ("project_id", identifier(199)),
            ("floating_network_id", identifier(99)),
            ("floating_ip_address", "198.51.100.71"),
            ("description", "unrelated"),
            ("port_id", identifier(99)),
            ("fixed_ip_address", "10.0.0.250"),
        ):
            self.cloud.fips[FIP][key] = bad
            count = len(self.cloud.mutations)
            with self.assertRaises(openstack.DriftError):
                self.mutate("release", request)
            self.assertEqual(len(self.cloud.mutations), count, key)
            self.cloud.fips[FIP] = copy.deepcopy(original)
        self.mutate("release", request)
        self.assertEqual(self.cloud.fips, {})

    def test_exact_port_server_project_slot_network_and_security_drift_blocks_mutation(self):
        self.deploy()
        self.mutate("allocate")
        record = service.get(self.connection, self.app_id)
        port = self.cloud.ports[record["port"]["port_id"]]
        server = self.cloud.servers[record["port"]["server_id"]]
        changes = [
            (port, "id", identifier(99)),
            (port, "project_id", identifier(199)),
            (port, "device_id", identifier(99)),
            (port, "description", "other-app"),
            (port, "network_id", identifier(99)),
            (port, "port_security_enabled", False),
            (port, "security_group_ids", []),
            (server, "id", identifier(99)),
            (server, "project_id", identifier(199)),
            (server, "properties", {}),
            (self.cloud.rule, "remote_group_id", None),
            (self.cloud.rule, "remote_ip_prefix", "0.0.0.0/0"),
        ]
        request = str(uuid.uuid4())
        for value, key, bad in changes:
            original = copy.deepcopy(value[key])
            value[key] = bad
            count = len(self.cloud.mutations)
            with self.assertRaises(openstack.DriftError):
                self.mutate("reconcile", request)
            self.assertEqual(len(self.cloud.mutations), count, key)
            value[key] = original
        self.mutate("reconcile", request)

    def test_ambiguous_create_reconciles_marker_never_reallocates(self):
        self.cloud.fault, self.cloud.fault_after = "create", True
        request = str(uuid.uuid4())
        with self.assertRaises(openstack.OpenStackError):
            self.mutate("allocate", request)
        self.assertEqual(service.get(self.connection, self.app_id)["phase"], "allocating")
        self.mutate("allocate", request)
        self.assertEqual([call[2] for call in self.cloud.mutations], ["create"])

    def test_ambiguous_create_with_zero_or_multiple_marker_matches_never_guesses(self):
        self.cloud.fault = "create"
        request = str(uuid.uuid4())
        with self.assertRaises(openstack.OpenStackError):
            self.mutate("allocate", request)
        with self.assertRaises(openstack.RecoveryRequired):
            self.mutate("allocate", request)
        record = service.get(self.connection, self.app_id)
        for fid in (FIP, identifier(10)):
            self.cloud.supplied(fid)
            self.cloud.fips[fid]["description"] = record["description"]
        with self.assertRaises(openstack.RecoveryRequired):
            self.mutate("allocate", request)
        self.assertEqual([call[2] for call in self.cloud.mutations], ["create"])

    def test_ambiguous_detach_and_delete_are_retryable_and_preserve_unrelated_resources(self):
        self.deploy()
        self.mutate("allocate")
        unrelated = identifier(100)
        self.cloud.supplied(unrelated)
        original = copy.deepcopy(self.cloud.fips[unrelated])
        request = str(uuid.uuid4())
        self.cloud.fault, self.cloud.fault_after = "unset", True
        with self.assertRaises(openstack.OpenStackError):
            self.mutate("release", request)
        self.cloud.fault, self.cloud.fault_after = "delete", True
        with self.assertRaises(openstack.OpenStackError):
            self.mutate("release", request)
        self.assertEqual(service.get(self.connection, self.app_id)["phase"], "deleting")
        self.mutate("release", request)
        self.assertIsNone(service.get(self.connection, self.app_id))
        self.assertEqual(self.cloud.fips, {unrelated: original})

    def test_app_deletion_blocks_worker_delete_on_ambiguous_fip_delete_then_resumes(self):
        self.deploy()
        self.mutate("allocate")
        self.cloud.fault, self.cloud.fault_after = "delete", True
        path = f"/v1/applications/{self.app_id}/delete"
        body = {"confirmation": "commons"}
        key, operation = self.fixture.post(path, body)
        self.assertEqual(operation.status, "recovery_required", operation.safe_error)
        self.assertEqual(len(self.fixture.workers), 1)
        self.assertIsNotNone(db.get_application(self.connection, self.app_id))
        _, operation = self.fixture.post(path, body, key)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertIsNone(db.get_application(self.connection, self.app_id))
        self.assertIsNone(service.get(self.connection, self.app_id))

    def test_conflicting_deploy_disable_delete_release_are_rejected(self):
        self.cloud.fault = "create"
        request = str(uuid.uuid4())
        with self.assertRaises(openstack.OpenStackError):
            self.mutate("allocate", request)
        with self.assertRaises(db.UnfinishedOperationError):
            self.mutate("release")
        for action, body in (
            ("deployments", self.fixture.body),
            ("disable", {}),
            ("delete", {"confirmation": "commons"}),
        ):
            with self.assertRaises(HttpError) as error:
                self.fixture.post(f"/v1/applications/{self.app_id}/{action}", body)
            self.assertEqual(error.exception.status, 409)

    def test_project_capability_cannot_access_public_ip_staff_api(self):
        router = self.fixture.api.router("project")
        for method, suffix, body in (
            ("GET", "", None),
            ("POST", "/plan", {"externalNetworkId": EXTERNAL}),
            ("POST", "", {"action": "allocate", "externalNetworkId": EXTERNAL}),
        ):
            with self.assertRaises(HttpError) as error:
                router.dispatch(
                    method, f"/v1/admin/applications/{self.app_id}/public-ip{suffix}", {}, body
                )
            self.assertEqual(error.exception.status, 404)
        path = f"/v1/admin/applications/{self.app_id}/public-ip"
        result = self.fixture.router.dispatch(
            "POST", path + "/plan", {}, {"externalNetworkId": EXTERNAL}
        )
        self.assertTrue(result.body["supported"])
        key, operation = self.fixture.post(
            path, {"action": "allocate", "externalNetworkId": EXTERNAL}
        )
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        result = self.fixture.router.dispatch("GET", path, {}, None)
        self.assertEqual(result.body["floatingIpId"], FIP)
        with self.assertRaises(HttpError):
            self.fixture.post(path, {"action": "release", "floatingIpId": FIP})

    def test_token_project_is_verified_before_any_provider_mutation(self):
        self.cloud.token_project = identifier(999)
        with self.assertRaises(openstack.OpenStackError):
            self.mutate("allocate")
        self.assertEqual(self.cloud.mutations, [])

    def test_staff_api_requires_exact_fields_and_immutable_idempotency_key(self):
        path = f"/v1/admin/applications/{self.app_id}/public-ip"
        for body in (
            {"action": "allocate"},
            {"action": "attach", "externalNetworkId": EXTERNAL},
            {"action": "release", "externalNetworkId": EXTERNAL},
            {"action": "allocate", "externalNetworkId": "network-name"},
            {"action": "allocate", "externalNetworkId": EXTERNAL, "ingress": True},
        ):
            with self.assertRaises(HttpError) as error:
                self.fixture.post(path, body)
            self.assertEqual(error.exception.status, 400)
        body = {"action": "allocate", "externalNetworkId": EXTERNAL}
        key, operation = self.fixture.post(path, body)
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        mutations = len(self.cloud.mutations)
        self.fixture.post(path, body, key)
        self.assertEqual(len(self.cloud.mutations), mutations)
        with self.assertRaises(HttpError) as error:
            self.fixture.post(path, {"action": "release"}, key)
        self.assertEqual(error.exception.status, 409)

    def test_one_owned_unassociated_address_cannot_be_reserved_by_two_apps(self):
        self.cloud.supplied()
        self.mutate("attach")
        second = identifier(999)
        self.fixture.router.dispatch(
            "POST", "/v1/applications", {"Idempotency-Key": second}, {"slug": "other-app"}
        )
        with self.assertRaises(openstack.DriftError):
            self.svc.mutate(second, action="attach", network_id=EXTERNAL, floating_ip_id=FIP)
        self.assertIsNone(service.get(self.connection, second))
        self.assertEqual(self.cloud.mutations, [])

    def test_supplied_ip_app_deletion_detaches_but_does_not_delete_address(self):
        self.deploy()
        self.cloud.supplied()
        self.mutate("attach")
        _, operation = self.fixture.post(
            f"/v1/applications/{self.app_id}/delete", {"confirmation": "commons"}
        )
        self.assertEqual(operation.status, "succeeded", operation.safe_error)
        self.assertIsNone(self.cloud.fips[FIP]["port_id"])
        self.assertIsNone(service.get(self.connection, self.app_id))
        self.assertFalse(any(call[2] == "delete" for call in self.cloud.mutations))


if __name__ == "__main__":
    unittest.main()
