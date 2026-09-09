"""Exact Neutron floating IPv4 operations. No ingress or firewall mutations.

The caller journals intent before every mutation. Neutron has no compare-and-set
association API: project credentials must be exclusive to cooperating operators.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from typing import Any

from . import openstack as os_api
from . import runtime
from .config import PlatformConfig
from .validation import uuid


class Provider:
    def __init__(
        self,
        platform: PlatformConfig,
        *,
        deadline: float,
        command_runner: os_api.Runner = runtime.run,
        executable: str = os_api._DEFAULT_OPENSTACK_EXECUTABLE,
    ) -> None:
        self.platform = platform
        self.deadline = deadline
        self.runner = command_runner
        self.executable = executable

    def _bounds(self) -> dict[str, Any]:
        import time

        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise os_api.OpenStackError("floating IP operation exceeded its deadline")
        return dict(
            timeout_seconds=remaining, command_runner=self.runner, executable=self.executable
        )

    def json(self, arguments: Sequence[str]) -> Any:
        return os_api._json_command(arguments, **self._bounds())

    def mutate(self, arguments: Sequence[str]) -> None:
        os_api._run(arguments, **self._bounds())

    def verify(self) -> None:
        os_api.verify_project(self.platform, **self._bounds())

    def network(self, identifier: str) -> None:
        value = self.json(("network", "show", uuid(identifier, field="external network UUID")))
        if (
            not isinstance(value, dict)
            or os_api._provider_uuid(value.get("id"), field="network UUID") != identifier
            or value.get("router:external") is not True
        ):
            raise os_api.DriftError("floating IP network is not the exact external network")

    def worker_network(self) -> dict[str, Any]:
        value = self.json(("network", "show", self.platform.network))
        if not isinstance(value, dict) or value.get("name") != self.platform.network:
            raise os_api.DriftError("configured worker network identity is ambiguous")
        os_api._provider_uuid(value.get("id"), field="worker network UUID")
        return value

    def plan(self, network_id: str, *, allocating: bool = True) -> dict[str, Any]:
        """Read-only capability evidence; never creates routers or networks."""
        self.verify()
        self.network(network_id)
        worker = self.worker_network()
        quota = self.json(("quota", "show", "--network"))
        if not isinstance(quota, list):
            raise os_api.OpenStackError("network quota projection is malformed")
        limits = [
            row.get("Limit")
            for row in quota
            if isinstance(row, dict) and row.get("Resource") == "floating_ips"
        ]
        if (
            len(limits) != 1
            or isinstance(limits[0], bool)
            or not isinstance(limits[0], int)
            or limits[0] < -1
        ):
            raise os_api.OpenStackError("floating IP quota is unavailable or malformed")
        limit = limits[0]
        used = len(self.inventory())
        reasons = []
        if allocating and limit != -1 and used >= limit:
            reasons.append("floating_ip_quota_exhausted")
        subnets = worker.get("subnets")
        if not isinstance(subnets, list) or not subnets:
            raise os_api.DriftError("configured worker subnet inventory is incomplete")
        ipv4_subnets = set()
        for identifier in subnets:
            subnet = self.json(("subnet", "show", uuid(identifier, field="worker subnet UUID")))
            if (
                not isinstance(subnet, dict)
                or subnet.get("id") != identifier
                or subnet.get("network_id") != worker["id"]
            ):
                raise os_api.DriftError("worker subnet ownership drifted")
            if subnet.get("ip_version") == 4:
                ipv4_subnets.add(identifier)
        routers = self.json(("router", "list", "--project", self.platform.project_id))
        if not isinstance(routers, list):
            raise os_api.OpenStackError("router inventory is malformed")
        matching = []
        for row in routers:
            identifier = os_api._provider_uuid(os_api._field(row, "id"), field="router UUID")
            router = self.json(("router", "show", identifier))
            if (
                not isinstance(router, dict)
                or router.get("id") != identifier
                or router.get("project_id") != self.platform.project_id
            ):
                raise os_api.DriftError("router UUID or project ownership drifted")
            gateway = router.get("external_gateway_info")
            if (
                not isinstance(gateway, dict)
                or gateway.get("network_id") != network_id
                or gateway.get("enable_snat") is not True
            ):
                continue
            ports = self.json(
                ("port", "list", "--device-id", identifier, "--network", worker["id"])
            )
            if not isinstance(ports, list):
                raise os_api.OpenStackError("router interface inventory is malformed")
            connected: set[str] = set()
            for port_row in ports:
                pid = os_api._provider_uuid(os_api._field(port_row, "id"), field="router port UUID")
                port = self.json(("port", "show", pid))
                if not isinstance(port, dict) or any(
                    (
                        port.get("id") != pid,
                        port.get("project_id") != self.platform.project_id,
                        port.get("device_id") != identifier,
                        port.get("network_id") != worker["id"],
                        port.get("device_owner")
                        not in {
                            "network:router_interface",
                            "network:router_interface_distributed",
                            "network:ha_router_replicated_interface",
                        },
                    )
                ):
                    raise os_api.DriftError("router interface ownership drifted")
                fixed = port.get("fixed_ips")
                if not isinstance(fixed, list) or any(not isinstance(item, dict) for item in fixed):
                    raise os_api.DriftError("router interface subnet evidence is malformed")
                connected.update(
                    uuid(item.get("subnet_id"), field="router interface subnet UUID")
                    for item in fixed
                )
            if len(ipv4_subnets) == 1 and ipv4_subnets <= connected:
                matching.append(identifier)
        if len(matching) != 1:
            reasons.append("no_unique_visible_snat_router_for_worker_subnet")
        return dict(
            supported=not reasons,
            reasons=reasons,
            externalNetworkId=network_id,
            workerNetworkId=worker["id"],
            floatingIpQuota=limit,
            floatingIpsUsed=used,
            routerId=matching[0] if len(matching) == 1 else None,
            purpose="stable-outbound-ipv4",
            ingressPolicy="unsupported",
        )

    def inventory(self) -> list[dict[str, Any]]:
        rows = self.json(("floating", "ip", "list", "--project", self.platform.project_id))
        if not isinstance(rows, list):
            raise os_api.OpenStackError("floating IP inventory is malformed")
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise os_api.OpenStackError("floating IP inventory is malformed")
            identifier = os_api._provider_uuid(os_api._field(row, "id"), field="floating IP UUID")
            if identifier in seen:
                raise os_api.DriftError("duplicate floating IP inventory identity")
            seen.add(identifier)
            result.append(self.show(identifier))
        return result

    def show(self, identifier: str) -> dict[str, Any]:
        value = self.json(("floating", "ip", "show", uuid(identifier, field="floating IP UUID")))
        if not isinstance(value, dict):
            raise os_api.OpenStackError("floating IP observation is malformed")
        if (
            os_api._provider_uuid(value.get("id"), field="floating IP UUID") != identifier
            or os_api._provider_uuid(value.get("project_id"), field="floating IP project UUID")
            != self.platform.project_id
        ):
            raise os_api.DriftError("floating IP UUID or project ownership drifted")
        return value

    def check(self, value: dict[str, Any], record: dict[str, Any]) -> None:
        if (
            value.get("id") != record["floating_ip_id"]
            or value.get("project_id") != record["project_id"]
            or value.get("floating_network_id") != record["network_id"]
            or value.get("floating_ip_address") != record["address"]
            or (value.get("description") or "") != record["description"]
        ):
            raise os_api.DriftError("recorded floating IP identity drifted")
        ipv4(value.get("floating_ip_address"))

    def port(self, target: dict[str, Any]) -> str:
        """Prove full slot ownership, not merely a short resource-name prefix."""
        value = self.json(("port", "show", uuid(target["port_id"], field="worker port UUID")))
        slot = uuid(target["slot_id"], field="worker slot UUID")
        expected_description = (
            f"managed-by=platform;application-id={slot};application-slug={target['slug']}"
        )
        if not isinstance(value, dict) or any(
            (
                value.get("id") != target["port_id"],
                value.get("project_id") != self.platform.project_id,
                value.get("device_id") != target["server_id"],
                value.get("name") != target["port_name"],
                value.get("description") != expected_description,
                value.get("port_security_enabled") is not True,
            )
        ):
            raise os_api.DriftError("worker port ownership or port security drifted")
        if value.get("network_id") != self.worker_network()["id"]:
            raise os_api.DriftError("worker port is outside the configured network")
        # Opt-in must not expose a worker whose groups have drifted to public
        # ingress. Existing policy only admits ingress-tier group traffic.
        groups = value.get("security_group_ids")
        if not isinstance(groups, list) or len(groups) != 1:
            raise os_api.DriftError("worker must retain exactly its worker security group")
        group = self.json(
            ("security", "group", "show", uuid(groups[0], field="worker security group UUID"))
        )
        if (
            not isinstance(group, dict)
            or group.get("id") != groups[0]
            or group.get("project_id") != self.platform.project_id
            or group.get("name") != f"{self.platform.prefix}-worker"
        ):
            raise os_api.DriftError("worker security group ownership drifted")
        ingress = self.json(("security", "group", "show", f"{self.platform.prefix}-ingress"))
        if (
            not isinstance(ingress, dict)
            or ingress.get("project_id") != self.platform.project_id
            or ingress.get("name") != f"{self.platform.prefix}-ingress"
        ):
            raise os_api.DriftError("ingress security group ownership drifted")
        ingress_id = os_api._provider_uuid(ingress.get("id"), field="ingress security group UUID")
        rules = self.json(("security", "group", "rule", "list", groups[0]))
        if not isinstance(rules, list):
            raise os_api.DriftError("worker security rules are malformed")
        for row in rules:
            rule_id = os_api._provider_uuid(os_api._field(row, "id"), field="security rule UUID")
            rule = self.json(("security", "group", "rule", "show", rule_id))
            if (
                not isinstance(rule, dict)
                or rule.get("id") != rule_id
                or rule.get("security_group_id") != groups[0]
                or rule.get("project_id") != self.platform.project_id
            ):
                raise os_api.DriftError("worker security rule ownership drifted")
            if rule.get("direction") == "egress":
                continue
            if (
                rule.get("direction") != "ingress"
                or rule.get("remote_group_id") != ingress_id
                or rule.get("remote_ip_prefix") is not None
                or rule.get("remote_address_group_id") is not None
            ):
                raise os_api.DriftError(
                    "direct worker ingress is unsupported for stable outbound IPv4"
                )
        server = self.json(
            ("server", "show", uuid(target["server_id"], field="worker server UUID"))
        )
        prefix = os_api._metadata_prefix(self.platform)
        properties = server.get("properties") if isinstance(server, dict) else None
        expected = {
            f"{prefix}_managed_by": "platform",
            f"{prefix}_application_id": slot,
            f"{prefix}_application_slug": target["slug"],
        }
        if (
            not isinstance(server, dict)
            or server.get("id") != target["server_id"]
            or server.get("name") != target["server_name"]
            or server.get("project_id", server.get("tenant_id")) != self.platform.project_id
            or not isinstance(properties, dict)
            or any(properties.get(key) != item for key, item in expected.items())
        ):
            raise os_api.DriftError("worker server ownership drifted")
        fixed = value.get("fixed_ips")
        if not isinstance(fixed, list):
            raise os_api.DriftError("worker fixed addresses are malformed")
        addresses = []
        for item in fixed:
            if not isinstance(item, dict):
                raise os_api.DriftError("worker fixed addresses are malformed")
            try:
                address = ipaddress.ip_address(str(item.get("ip_address")))
            except ValueError as error:
                raise os_api.DriftError("worker fixed address is malformed") from error
            if address.version == 4:
                addresses.append(str(address))
        if len(addresses) != 1:
            raise os_api.DriftError("worker must have exactly one fixed IPv4 address")
        if target.get("fixed_ip") is not None and target["fixed_ip"] != addresses[0]:
            raise os_api.DriftError("recorded worker fixed IPv4 address drifted")
        return addresses[0]

    def create(self, network_id: str, description: str) -> dict[str, Any]:
        value = self.json(("floating", "ip", "create", "--description", description, network_id))
        if not isinstance(value, dict):
            raise os_api.OpenStackError("floating IP creation response is malformed")
        return value

    def assign(self, identifier: str, target: dict[str, Any]) -> None:
        self.mutate(
            (
                "floating",
                "ip",
                "set",
                "--port",
                target["port_id"],
                "--fixed-ip-address",
                target["fixed_ip"],
                identifier,
            )
        )

    def detach(self, identifier: str) -> None:
        self.mutate(("floating", "ip", "unset", "--port", identifier))

    def delete(self, identifier: str) -> None:
        self.mutate(("floating", "ip", "delete", identifier))


class CapabilityUnavailable(os_api.OpenStackError):
    """The observed cloud cannot currently support this optional feature."""


def ipv4(value: Any) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except (ValueError, TypeError) as error:
        raise os_api.DriftError("floating address must be IPv4") from error
