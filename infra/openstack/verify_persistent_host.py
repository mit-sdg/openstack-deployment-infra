#!/usr/bin/env python3
"""Verify one existing persistent host before an apply script reuses it."""

from __future__ import annotations

import argparse
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MAX_JSON_BYTES = 1_048_576
_MISSING = object()


def fail(message: str) -> NoReturn:
    raise SystemExit(message)


def bounded_json(path: Path, label: str) -> Any:
    try:
        raw = path.read_bytes()
    except OSError:
        fail(f"{label} could not be read")
    if len(raw) > _MAX_JSON_BYTES:
        fail(f"{label} exceeds its safety limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail(f"{label} is malformed JSON")


def field(value: Mapping[str, Any], name: str, default: Any = _MISSING) -> Any:
    wanted = name.lower().replace(" ", "_")
    for key, item in value.items():
        if str(key).lower().replace(" ", "_") == wanted:
            return item
    return default


def canonical_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        fail(f"{label} is malformed")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        fail(f"{label} is malformed")
    if parsed != value:
        fail(f"{label} is not canonical")
    return value


def resource_uuid(value: Any, label: str) -> str:
    if isinstance(value, Mapping):
        value = field(value, "id")
    if isinstance(value, str) and " (" in value:
        if value.count(" (") != 1 or not value.endswith(")"):
            fail(f"{label} is ambiguous")
        value = value.rsplit(" (", 1)[1][:-1]
    return canonical_uuid(value, label)


def resource_name(value: Any, label: str) -> str:
    if isinstance(value, Mapping):
        value = field(value, "original_name", field(value, "name"))
    elif isinstance(value, str):
        if value.count(" (") != 1 or not value.endswith(")"):
            fail(f"{label} is malformed")
        value = value.rsplit(" (", 1)[0]
    if not isinstance(value, str) or not value:
        fail(f"{label} is malformed")
    return value


def properties(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail("existing persistent host metadata is malformed")
    return value


def deletion_flag(value: Any, label: str) -> bool:
    if value is False or (isinstance(value, str) and value.lower() == "false"):
        return False
    if value is True or (isinstance(value, str) and value.lower() == "true"):
        fail(f"{label} has delete_on_termination enabled")
    fail(f"{label} has an ambiguous delete_on_termination value")


def expected_volumes(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            fail("expected persistent volume attachment is malformed")
        volume_id, device = value.split("=", 1)
        volume_id = canonical_uuid(volume_id, "expected persistent volume UUID")
        if not re.fullmatch(r"/dev/[A-Za-z0-9._/-]{1,60}", device):
            fail("expected persistent volume device is malformed")
        if volume_id in result or device in result.values():
            fail("expected persistent volume attachments are ambiguous")
        result[volume_id] = device
    return result


def verify(args: argparse.Namespace) -> None:
    server = bounded_json(args.server_json, "server projection")
    port = bounded_json(args.port_json, "port projection")
    server_ports = bounded_json(args.server_ports_json, "server port projection")
    attachments = bounded_json(args.attachments_json, "volume attachment projection")
    if not isinstance(server, Mapping) or not isinstance(port, Mapping):
        fail("persistent host projections must be JSON objects")
    if not isinstance(server_ports, list) or any(
        not isinstance(item, Mapping) for item in server_ports
    ):
        fail("existing server port projection is malformed")
    if not isinstance(attachments, list) or any(
        not isinstance(item, Mapping) for item in attachments
    ):
        fail("persistent volume attachment projection is malformed")

    server_id = canonical_uuid(field(server, "id"), "existing server UUID")
    if server_id != args.server_id:
        fail("existing server UUID changed during verification")
    if field(server, "name") != args.server_name:
        fail("existing persistent host name does not match the configured identity")
    if resource_uuid(field(server, "image"), "existing server image UUID") != args.image_id:
        fail("existing persistent host image UUID does not match the configured image")
    if resource_uuid(field(server, "flavor"), "existing server flavor UUID") != args.flavor_id:
        fail("existing persistent host flavor UUID does not match the configured flavor")
    if resource_name(field(server, "flavor"), "existing server flavor name") != args.flavor_name:
        fail("existing persistent host flavor name does not match the configured flavor")

    prefix = args.metadata_prefix
    expected_metadata = {
        f"{prefix}_managed_by": "platform",
        f"{prefix}_role": args.role,
        f"{prefix}_project_id": args.project_id,
        f"{prefix}_namespace": args.namespace,
        f"{prefix}_prefix": args.platform_prefix,
        f"{prefix}_metadata_version": "1",
        f"{prefix}_image_id": args.image_id,
        f"{prefix}_flavor_id": args.flavor_id,
    }
    observed_metadata = properties(field(server, "properties"))
    for key, expected in expected_metadata.items():
        if observed_metadata.get(key) != expected:
            fail("existing persistent host metadata identity does not match the deployment")

    port_id = canonical_uuid(field(port, "id"), "configured port UUID")
    if port_id != args.port_id or field(port, "name") != args.port_name:
        fail("existing persistent host port identity does not match the configured port")
    if resource_uuid(field(port, "device_id"), "configured port device UUID") != server_id:
        fail("configured port is not attached to the existing server UUID")
    attached_port_ids = {
        resource_uuid(field(item, "id"), "observed server port UUID") for item in server_ports
    }
    if attached_port_ids != {args.port_id} or len(server_ports) != 1:
        fail("existing persistent host has unexpected port attachments")
    fixed_ips = field(port, "fixed_ips")
    if not isinstance(fixed_ips, list) or not any(
        isinstance(item, Mapping) and field(item, "ip_address") == args.address
        for item in fixed_ips
    ):
        fail("configured port does not own its configured fixed address")

    expected = expected_volumes(args.volume)
    observed_attachments: dict[str, str] = {}
    for item in attachments:
        volume_id = resource_uuid(field(item, "id"), "observed volume UUID")
        device = field(item, "device")
        if not isinstance(device, str) or not re.fullmatch(r"/dev/[A-Za-z0-9._/-]{1,60}", device):
            fail("observed persistent volume device is malformed")
        if volume_id in observed_attachments or device in observed_attachments.values():
            fail("observed persistent volume attachments are ambiguous")
        observed_attachments[volume_id] = device
    if observed_attachments != expected:
        fail("existing persistent host volume identity does not match the configured volumes")

    raw_flags = field(server, "volumes_attached")
    if not isinstance(raw_flags, list) or any(not isinstance(item, Mapping) for item in raw_flags):
        fail("existing persistent host volume deletion projection is malformed")
    flags: dict[str, bool] = {}
    for item in raw_flags:
        volume_id = resource_uuid(field(item, "id"), "observed volume deletion UUID")
        if volume_id in flags:
            fail("observed persistent volume deletion projection is ambiguous")
        flags[volume_id] = deletion_flag(
            field(item, "delete_on_termination"), "persistent volume attachment"
        )
    if set(flags) != set(expected) or any(flags[volume_id] is not False for volume_id in expected):
        fail("existing persistent host volume deletion identity does not match")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True)
    parser.add_argument("--server-json", type=Path, required=True)
    parser.add_argument("--port-json", type=Path, required=True)
    parser.add_argument("--server-ports-json", type=Path, required=True)
    parser.add_argument("--attachments-json", type=Path, required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--flavor-id", required=True)
    parser.add_argument("--flavor-name", required=True)
    parser.add_argument("--port-id", required=True)
    parser.add_argument("--port-name", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--metadata-prefix", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--platform-prefix", required=True)
    parser.add_argument("--volume", action="append", default=[])
    args = parser.parse_args(argv)
    verify(args)
    print(f"persistent-host=verified role={args.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
