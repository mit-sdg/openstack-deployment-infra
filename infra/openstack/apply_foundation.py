#!/usr/bin/env python3
"""Create the idempotent OpenStack network foundation for the platform.

The configured OpenStack network may be shared and publicly routed, so
isolation is enforced with Neutron security groups. This script creates or
updates named foundation resources and never deletes them.
Authentication is read from the standard OS_* environment variables.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import uuid as uuid_module
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openstack
from openstack import exceptions  # type: ignore[attr-defined]

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.platform_config import load  # noqa: E402
from lib.platform_contract import CONTRACT  # noqa: E402

CONFIG = load()
PROJECT_NAME = CONFIG["project"]
PROJECT_ID = CONFIG["projectId"]
PLATFORM_NAME = CONFIG["displayName"]
PREFIX = CONFIG["prefix"]
PUBLIC_NETWORK = CONFIG["network"]
INGRESS_PUBLIC_PORT = CONFIG["ports"]["ingress"]
OPERATOR_SOURCE = CONFIG["operatorCidr"]

CONTRACT_PORTS = CONTRACT["ports"]
SSH_PORT = CONTRACT_PORTS["ssh"]
HTTP_PORT = CONTRACT_PORTS["http"]
HTTPS_PORT = CONTRACT_PORTS["https"]
NOMAD_HTTP_PORT = CONTRACT_PORTS["nomadHttp"]
NOMAD_RPC_PORT = CONTRACT_PORTS["nomadRpc"]
APPLICATION_PORT = CONTRACT_PORTS["application"]
POSTGRES_PORT = CONTRACT_PORTS["postgres"]
MONGODB_PORT = CONTRACT_PORTS["mongodb"]
REGISTRY_PORT = CONTRACT_PORTS["registry"]
GARAGE_RPC_PORT = CONTRACT_PORTS["garageRpc"]
GARAGE_S3_PORT = CONTRACT_PORTS["garageS3"]
STORAGE_CLIENT_PORTS = (POSTGRES_PORT, MONGODB_PORT, REGISTRY_PORT, GARAGE_S3_PORT)
STORAGE_ADMIN_PORTS = (SSH_PORT, GARAGE_RPC_PORT, *STORAGE_CLIENT_PORTS)


@dataclass(frozen=True)
class Rule:
    direction: str
    protocol: str | None = None
    port_min: int | None = None
    port_max: int | None = None
    remote_ip: str | None = None
    remote_group: str | None = None
    ethertype: str = "IPv4"


SECURITY_GROUPS: dict[str, tuple[str, list[Rule]]] = {
    f"{PREFIX}-admin": (
        "Platform control plane; SSH is restricted to the existing operator host",
        [
            Rule("ingress", "tcp", SSH_PORT, SSH_PORT, remote_ip=OPERATOR_SOURCE),
            Rule("ingress", "icmp", remote_ip=OPERATOR_SOURCE),
        ],
    ),
    f"{PREFIX}-ingress": (
        "Public HTTP(S) ingress and administration from the control plane",
        [
            Rule("ingress", "tcp", HTTP_PORT, HTTP_PORT, remote_ip="0.0.0.0/0"),
            Rule("ingress", "tcp", HTTPS_PORT, HTTPS_PORT, remote_ip="0.0.0.0/0"),
        ],
    ),
    f"{PREFIX}-worker": (
        "Application workers; application traffic arrives only from ingress",
        [],
    ),
    f"{PREFIX}-builder": (
        "Disposable isolated builders; SSH is restricted to the control plane",
        [],
    ),
    f"{PREFIX}-storage": (
        "Managed PostgreSQL, MongoDB, and object storage",
        [],
    ),
}


def one(items: Iterable[Any], kind: str, name: str) -> Any | None:
    matches = [item for item in items if item.name == name]
    if len(matches) > 1:
        raise RuntimeError(f"multiple {kind} resources are named {name!r}")
    return matches[0] if matches else None


def comparable_rule(rule: Any) -> tuple[Any, ...]:
    return (
        rule.direction,
        rule.protocol,
        rule.port_range_min,
        rule.port_range_max,
        rule.remote_ip_prefix,
        rule.remote_group_id,
        rule.ether_type,
    )


def desired_rule(rule: Rule) -> tuple[str | int | None, ...]:
    return (
        rule.direction,
        rule.protocol,
        rule.port_min,
        rule.port_max,
        rule.remote_ip,
        rule.remote_group,
        rule.ethertype,
    )


def ensure_security_groups(conn: Any, apply: bool) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for name, (description, _) in SECURITY_GROUPS.items():
        group = one(conn.network.security_groups(name=name), "security group", name)
        if not group:
            if not apply:
                print(f"would create security group: {name}")
                continue
            group = conn.network.create_security_group(name=name, description=description)
            print(f"created security group: {name} ({group.id})")
        else:
            print(f"existing security group: {name} ({group.id})")
        groups[name] = group

    if len(groups) != len(SECURITY_GROUPS):
        return groups

    expanded = {name: list(spec[1]) for name, spec in SECURITY_GROUPS.items()}
    admin = f"{PREFIX}-admin"
    ingress = f"{PREFIX}-ingress"
    worker = f"{PREFIX}-worker"
    builder = f"{PREFIX}-builder"
    storage = f"{PREFIX}-storage"
    expanded[admin].extend(
        [
            Rule(
                "ingress", "tcp", NOMAD_HTTP_PORT, NOMAD_HTTP_PORT, remote_group=groups[ingress].id
            ),
            Rule(
                "ingress",
                "tcp",
                APPLICATION_PORT,
                APPLICATION_PORT,
                remote_group=groups[ingress].id,
            ),
            Rule(
                "ingress", "tcp", NOMAD_RPC_PORT, NOMAD_RPC_PORT, remote_group=groups[worker].id
            ),
        ]
    )
    expanded[ingress].append(
        Rule("ingress", "tcp", SSH_PORT, SSH_PORT, remote_group=groups[admin].id)
    )
    expanded[worker].append(
        Rule(
            "ingress", "tcp", APPLICATION_PORT, APPLICATION_PORT, remote_group=groups[ingress].id
        )
    )
    expanded[builder].append(
        Rule("ingress", "tcp", SSH_PORT, SSH_PORT, remote_group=groups[admin].id)
    )
    expanded[storage].extend(
        Rule("ingress", "tcp", port, port, remote_group=groups[worker].id)
        for port in STORAGE_CLIENT_PORTS
    )
    expanded[storage].extend(
        Rule("ingress", "tcp", port, port, remote_group=groups[admin].id)
        for port in STORAGE_ADMIN_PORTS
    )
    expanded[storage].append(
        Rule("ingress", "tcp", REGISTRY_PORT, REGISTRY_PORT, remote_group=groups[builder].id)
    )

    for name, rules in expanded.items():
        group = groups[name]
        existing = {
            comparable_rule(rule)
            for rule in conn.network.security_group_rules(security_group_id=group.id)
        }
        for rule in rules:
            wanted = desired_rule(rule)
            if wanted in existing:
                continue
            if not apply:
                print(f"would add security rule: {name} {wanted}")
                continue
            conn.network.create_security_group_rule(
                security_group_id=group.id,
                direction=rule.direction,
                protocol=rule.protocol,
                port_range_min=rule.port_min,
                port_range_max=rule.port_max,
                remote_ip_prefix=rule.remote_ip,
                remote_group_id=rule.remote_group,
                ether_type=rule.ethertype,
            )
            print(f"added security rule: {name} {wanted}")
    return groups


def ensure_port(
    conn: Any,
    name: str,
    network: Any,
    security_groups: Iterable[Any],
    fixed_ip: str,
    apply: bool,
) -> Any | None:
    port = one(conn.network.ports(name=name), "port", name)
    desired_groups = sorted(group.id for group in security_groups)
    if port:
        if port.network_id != network.id:
            raise RuntimeError(f"{name} is attached to an unexpected network")
        current_ips = {entry["ip_address"] for entry in (port.fixed_ips or [])}
        if fixed_ip not in current_ips:
            raise RuntimeError(f"{name} does not own configured address {fixed_ip}")
        current_groups = sorted(port.security_group_ids or [])
        if current_groups != desired_groups:
            if not apply:
                print(f"would update security groups on port: {name}")
            else:
                conn.network.update_port(port, security_group_ids=desired_groups)
                print(f"updated security groups on port: {name}")
        else:
            print(f"existing port: {name} ({port.id})")
        return port
    address = ipaddress.ip_address(fixed_ip)
    matching_subnets = [
        subnet
        for subnet in conn.network.subnets(network_id=network.id)
        if address in ipaddress.ip_network(subnet.cidr)
    ]
    if len(matching_subnets) != 1:
        raise RuntimeError(f"configured address {fixed_ip} does not select exactly one subnet")
    if not apply:
        print(f"would create port: {name} ({fixed_ip})")
        return None
    port = conn.network.create_port(
        name=name,
        description=f"{PLATFORM_NAME} deployment platform foundation port",
        network_id=network.id,
        security_group_ids=desired_groups,
        fixed_ips=[{"subnet_id": matching_subnets[0].id, "ip_address": fixed_ip}],
        is_admin_state_up=True,
    )
    print(f"created port: {name} ({port.id})")
    return port


def _canonical_provider_uuid(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"authenticated project {field} is unavailable")
    try:
        return str(uuid_module.UUID(value))
    except (AttributeError, ValueError) as error:
        raise RuntimeError(f"authenticated project {field} is malformed") from error


def _project_field(project: Any, field: str) -> object:
    if isinstance(project, Mapping):
        return project.get(field)
    return getattr(project, field, None)


def verify_authenticated_project(conn: Any) -> tuple[str, str]:
    """Require both authenticated project identity fields before any mutation."""
    configured_id = _canonical_provider_uuid(PROJECT_ID, field="UUID configuration")
    provider_id = getattr(conn, "current_project_id", None)
    current_id = _canonical_provider_uuid(provider_id, field="UUID")
    if current_id != configured_id:
        raise RuntimeError("authenticated OpenStack project UUID does not match configuration")

    project = conn.identity.get_project(provider_id)
    observed_id = _canonical_provider_uuid(_project_field(project, "id"), field="UUID")
    observed_name = _project_field(project, "name")
    if (
        not isinstance(observed_name, str)
        or observed_id != configured_id
        or observed_name != PROJECT_NAME
    ):
        raise RuntimeError("authenticated OpenStack project name does not match configuration")
    return observed_name, observed_id


def run(apply: bool) -> None:
    if os.environ.get("OS_PROJECT_NAME") != PROJECT_NAME:
        raise RuntimeError("refusing to run outside the configured OpenStack project")
    configured_environment_id = os.environ.get("OS_PROJECT_ID")
    if configured_environment_id is None or _canonical_provider_uuid(
        configured_environment_id, field="environment UUID"
    ) != _canonical_provider_uuid(PROJECT_ID, field="UUID configuration"):
        raise RuntimeError("refusing to run outside the configured OpenStack project")
    ipaddress.ip_network(OPERATOR_SOURCE)

    conn = openstack.connect()  # type: ignore[attr-defined]
    conn.authorize()
    authenticated_name, authenticated_id = verify_authenticated_project(conn)
    print(f"authenticated project: {authenticated_name} ({authenticated_id})")

    public = conn.network.find_network(PUBLIC_NETWORK, ignore_missing=False)
    groups = ensure_security_groups(conn, apply)
    if len(groups) != len(SECURITY_GROUPS):
        print("additional resources depend on the security groups above")
        return

    ensure_port(
        conn,
        INGRESS_PUBLIC_PORT,
        public,
        [groups[f"{PREFIX}-ingress"]],
        CONFIG["addresses"]["ingress"],
        apply,
    )
    ensure_port(
        conn,
        CONFIG["ports"]["admin"],
        public,
        [groups[f"{PREFIX}-admin"]],
        CONFIG["addresses"]["admin"],
        apply,
    )
    ensure_port(
        conn,
        CONFIG["ports"]["storage"],
        public,
        [groups[f"{PREFIX}-storage"]],
        CONFIG["addresses"]["storage"],
        apply,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="create or update resources")
    args = parser.parse_args()
    try:
        run(args.apply)
    except exceptions.HttpException as exc:
        status = getattr(exc, "status_code", None) or "unknown"
        print(f"OpenStack request failed with HTTP status {status}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("foundation reconciliation complete" if args.apply else "foundation plan complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
