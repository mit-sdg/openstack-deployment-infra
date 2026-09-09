"""Exact app-owned retained Neutron primary ports (not floating addresses).

Intent and random ownership markers belong to the controller journal. Provider
credentials must be exclusive to cooperating operators: Neutron has no CAS for
port attachment/deletion. A failed observation is never evidence of absence.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Sequence
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
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise os_api.OpenStackError("retained port operation exceeded its deadline")
        return dict(
            timeout_seconds=remaining, command_runner=self.runner, executable=self.executable
        )

    def json(self, arguments: Sequence[str]) -> Any:
        return os_api._json_command(arguments, **self._bounds())

    def verify(self) -> None:
        os_api.verify_project(self.platform, **self._bounds())

    def plan(self, network_id: str, subnet_id: str, address: str) -> dict[str, Any]:
        self.verify()
        network_id = uuid(network_id, field="worker network UUID")
        subnet_id = uuid(subnet_id, field="worker subnet UUID")
        address = ipv4(address)
        network = self.json(("network", "show", self.platform.network))
        subnet = self.json(("subnet", "show", subnet_id))
        if (
            not isinstance(network, dict)
            or os_api._provider_uuid(network.get("id"), field="network UUID") != network_id
            or network.get("name") != self.platform.network
            or not isinstance(network.get("subnets"), list)
            or subnet_id
            not in {os_api._provider_uuid(item, field="subnet UUID") for item in network["subnets"]}
        ):
            raise os_api.DriftError("retained port must use the exact configured worker network")
        if (
            not isinstance(subnet, dict)
            or os_api._provider_uuid(subnet.get("id"), field="subnet UUID") != subnet_id
            or os_api._provider_uuid(subnet.get("network_id"), field="network UUID") != network_id
            or subnet.get("ip_version") != 4
        ):
            raise os_api.DriftError("retained port subnet identity drifted")
        try:
            cidr = ipaddress.IPv4Network(subnet["cidr"])
        except (KeyError, ValueError, TypeError) as error:
            raise os_api.DriftError("worker subnet CIDR is malformed") from error
        ip = ipaddress.IPv4Address(address)
        if (
            ip not in cidr
            or ip in {cidr.network_address, cidr.broadcast_address}
            or address == subnet.get("gateway_ip")
        ):
            raise os_api.DriftError("requested IPv4 is not a usable address in the worker subnet")
        # Explicit allocations outside allocation_pools are valid Neutron requests.
        # Availability/authorization can only be established by Neutron create.
        group = self.json(("security", "group", "show", f"{self.platform.prefix}-worker"))
        if (
            not isinstance(group, dict)
            or os_api._provider_uuid(group.get("project_id"), field="security group project UUID")
            != self.platform.project_id
            or group.get("name") != f"{self.platform.prefix}-worker"
        ):
            raise os_api.DriftError("worker security group identity drifted")
        group_id = os_api._provider_uuid(group.get("id"), field="worker security group UUID")
        return dict(
            supported=True,
            networkId=network_id,
            subnetId=subnet_id,
            address=address,
            securityGroupId=group_id,
            cidr=str(cidr),
            availability="unproven-until-reserved",
            replacementPolicy="disabled-predecessor-absent",
            purpose="retained-primary-ipv4",
        )

    def show(self, identifier: str) -> dict[str, Any]:
        value = self.json(("port", "show", uuid(identifier, field="retained port UUID")))
        if not isinstance(value, dict):
            raise os_api.DriftError("retained port observation identity is malformed")
        value = normalize_port(value)
        if value["id"] != identifier:
            raise os_api.DriftError("retained port observation identity is malformed")
        return value

    def inventory(self) -> list[dict[str, Any]]:
        rows = self.json(("port", "list", "--project", self.platform.project_id))
        if not isinstance(rows, list):
            raise os_api.OpenStackError("port inventory is malformed")
        ids = [os_api._provider_uuid(os_api._field(row, "id"), field="port UUID") for row in rows]
        if len(set(ids)) != len(ids):
            raise os_api.DriftError("duplicate port inventory identity")
        return [self.show(identifier) for identifier in ids]

    def check(self, value: dict[str, Any], record: dict[str, Any], *, device_id: str = "") -> None:
        check_port(value, record, device_id=device_id)
        if record["project_id"] != self.platform.project_id:
            raise os_api.DriftError("retained port belongs to a different project")
        plan = self.plan(record["network_id"], record["subnet_id"], record["address"])
        if plan["securityGroupId"] != record["security_group_id"]:
            raise os_api.DriftError("retained worker security group changed")

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        value = self.json(
            (
                "port",
                "create",
                "--network",
                record["network_id"],
                "--fixed-ip",
                f"subnet={record['subnet_id']},ip-address={record['address']}",
                "--security-group",
                record["security_group_id"],
                "--enable-port-security",
                "--description",
                record["description"],
                record["name"],
            )
        )
        if not isinstance(value, dict):
            raise os_api.OpenStackError("retained port creation response is malformed")
        return {**value, "id": os_api._provider_uuid(value.get("id"), field="created port UUID")}

    def delete(self, identifier: str) -> None:
        os_api._run(
            ("port", "delete", uuid(identifier, field="retained port UUID")), **self._bounds()
        )


def ipv4(value: Any) -> str:
    try:
        address = str(ipaddress.IPv4Address(value))
    except (ValueError, TypeError) as error:
        raise os_api.DriftError("requested fixed address must be IPv4") from error
    if address != value:
        raise os_api.DriftError("requested fixed address must be canonical IPv4 text")
    return address


def normalize_port(value: dict[str, Any]) -> dict[str, Any]:
    """OSC may emit compact provider UUIDs. Request/journal UUIDs stay strict."""
    result = dict(value)
    for key in ("id", "project_id", "network_id"):
        result[key] = os_api._provider_uuid(value.get(key), field=f"port {key}")
    if value.get("device_id"):
        result["device_id"] = os_api._provider_uuid(value["device_id"], field="port device UUID")
    groups, fixed = value.get("security_group_ids"), value.get("fixed_ips")
    if (
        not isinstance(groups, list)
        or not isinstance(fixed, list)
        or any(not isinstance(item, dict) for item in fixed)
    ):
        raise os_api.DriftError("retained port group/address projection is malformed")
    result["security_group_ids"] = [
        os_api._provider_uuid(item, field="port security group UUID") for item in groups
    ]
    result["fixed_ips"] = [
        {
            **item,
            "subnet_id": os_api._provider_uuid(item.get("subnet_id"), field="port subnet UUID"),
        }
        for item in fixed
    ]
    return result


def check_port(value: dict[str, Any], record: dict[str, Any], *, device_id: str = "") -> None:
    """Closed retained projection, also used immediately before shell mutations."""
    value = normalize_port(value)
    expected = dict(
        id=record["port_id"],
        name=record["name"],
        project_id=record["project_id"],
        network_id=record["network_id"],
        description=record["description"],
        fixed_ips=[dict(subnet_id=record["subnet_id"], ip_address=record["address"])],
        security_group_ids=[record["security_group_id"]],
        port_security_enabled=True,
        allowed_address_pairs=[],
        device_id=device_id,
    )
    if value.get("port_security_enabled") is not True or any(
        value.get(key) != item for key, item in expected.items()
    ):
        raise os_api.DriftError(
            "retained primary port identity, address, security or attachment drifted"
        )
    owner = value.get("device_owner")
    if (not device_id and owner != "") or (
        device_id
        and (not isinstance(owner, str) or not owner.startswith("compute:") or owner == "compute:")
    ):
        raise os_api.DriftError("retained primary port device owner drifted")
    if value.get("trunk_details") not in (None, {}):
        raise os_api.DriftError("retained primary port must not be a trunk")
    if not device_id and any(
        value.get(key) not in (None, "") for key in ("binding:host_id", "binding_host_id")
    ):
        raise os_api.DriftError("retained primary port is still bound to a host")


def validate_request(
    value: Any, platform: PlatformConfig, slot_id: str, application_slug: str
) -> dict[str, Any]:
    """A helper request cannot substitute another app's port or arbitrary names."""
    from .controller.nomad_jobs import deployment_worker_ids

    keys = {
        "application_id",
        "request_id",
        "project_id",
        "network_id",
        "subnet_id",
        "address",
        "security_group_id",
        "port_id",
        "name",
        "description",
    }
    if not isinstance(value, dict) or value.keys() != keys:
        raise os_api.DriftError("retained port request fields are invalid")
    for key in keys - {"address", "name", "description"}:
        uuid(value[key], field=f"retained port {key}")
    ipv4(value["address"])
    app_id = value["application_id"]
    if (
        slot_id not in {app_id, *deployment_worker_ids(app_id)}
        or value["project_id"] != platform.project_id
        or value["name"] != f"{platform.prefix}-app-{app_id}-primary-v4"
        or value["description"] != marker(platform, app_id, value["request_id"])
    ):
        raise os_api.DriftError("retained port request ownership does not match worker slot")
    return value


def marker(platform: PlatformConfig, app_id: str, request_id: str) -> str:
    return f"{platform.namespace}:app-fixed-port:{app_id}:{request_id}"
