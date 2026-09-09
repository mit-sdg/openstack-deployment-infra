"""Read one exact dedicated Nomad worker's measured allocatable capacity."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from ..config import PlatformConfig
from ..controller.sizing import capacity_budget
from ..runtime import CommandTimedOut, run
from ..validation import ValidationError, slug, uuid


def observe_capacity(
    platform: PlatformConfig,
    application_id: str,
    application_slug: str,
    server_name: str,
    *,
    nomad_command: str,
    command_runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    identifier = uuid(application_id, field="worker application ID")
    application_slug = slug(application_slug)
    runner = run if command_runner is None else command_runner
    deadline = time.monotonic() + 30

    def query(*args: str) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CommandTimedOut("Nomad capacity observation exceeded its deadline")
        result = runner(
            (nomad_command, "node", "status", "-json", *args),
            timeout_seconds=remaining,
            stdout_limit=1_048_576,
            stderr_limit=65_536,
        )
        if result.stdout_truncated or result.stderr_truncated:
            raise ValidationError("Nomad capacity response exceeded its bound")
        return json.loads(result.stdout)

    nodes = query()
    if not isinstance(nodes, list):
        raise ValidationError("Nomad node inventory is malformed")
    matches = [node for node in nodes if isinstance(node, dict) and node.get("Name") == server_name]
    if len(matches) != 1:
        raise ValidationError("worker must resolve to exactly one Nomad node")
    node_id = uuid(matches[0].get("ID"), field="Nomad node ID")
    node = query(node_id)
    if not isinstance(node, dict):
        raise ValidationError("Nomad worker detail is malformed")
    meta = node.get("Meta")
    drivers = node.get("Drivers")
    if not isinstance(meta, dict) or not isinstance(drivers, dict):
        raise ValidationError("Nomad worker metadata/drivers are malformed")
    docker = drivers.get("docker")
    if not isinstance(docker, dict):
        raise ValidationError("Nomad worker Docker readiness is malformed")
    if (
        node.get("ID") != node_id
        or node.get("Name") != server_name
        or node.get("Status") != "ready"
        or node.get("SchedulingEligibility") != "eligible"
        or node.get("Drain") is not False
        or node.get("NodeClass") != f"{platform.namespace}-app"
        or meta.get("application_id") != identifier
        or meta.get("application_slug") != application_slug
        or meta.get("managed_by") != f"{platform.namespace}-platform"
        or docker.get("Detected") is not True
        or docker.get("Healthy") is not True
    ):
        raise ValidationError("Nomad worker capacity identity/readiness did not match")
    # Pinned Nomad v2.0.5: api/nodes.go defines these exact JSON fields;
    # command/node_status.go computeNodeTotalResources uses the same subtraction.
    # Do not fall back to the deprecated Resources/Reserved projections.
    try:
        resources = node["NodeResources"]
        reserved = node["ReservedResources"]
        cpu = resources["Cpu"]["CpuShares"]
        memory = resources["Memory"]["MemoryMB"]
        reserved_cpu = reserved["Cpu"]["CpuShares"]
        reserved_memory = reserved["Memory"]["MemoryMB"]
    except (KeyError, TypeError) as error:
        raise ValidationError("Nomad worker capacity projection is incomplete") from error
    available_cpu, available_memory = capacity_budget(cpu, memory, reserved_cpu, reserved_memory)
    return {
        "nodeId": node_id,
        "cpuMHz": available_cpu,
        "memoryMiB": available_memory,
        "totalCpuMHz": cpu,
        "totalMemoryMiB": memory,
    }
