"""Explicit operator sizing intent and capacity budgets (no provider mutations)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

from .. import openstack
from ..config import Config
from ..validation import ValidationError, flavor_reference
from . import database as db

# Leave room for the kernel, Docker, Nomad, CNI, logs and platform services.
# CPU comes from Nomad's measured MHz, never an assumed vCPU clock speed.
CPU_RESERVE_MHZ = 200
MEMORY_RESERVE_MIB = 512
RESERVE_PERCENT = 10


def reserve(total: int, minimum: int) -> int:
    return max(minimum, (total * RESERVE_PERCENT + 99) // 100)


def capacity_budget(
    cpu: object, memory: object, reserved_cpu: object = 0, reserved_memory: object = 0
) -> tuple[int, int]:
    values = (cpu, memory, reserved_cpu, reserved_memory)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValidationError("worker capacity must contain non-negative integers")
    assert isinstance(cpu, int) and isinstance(memory, int)
    assert isinstance(reserved_cpu, int) and isinstance(reserved_memory, int)
    available_cpu = cpu - max(reserve(cpu, CPU_RESERVE_MHZ), reserved_cpu)
    available_memory = memory - max(reserve(memory, MEMORY_RESERVE_MIB), reserved_memory)
    if available_cpu < 100 or available_memory < 64:
        raise ValidationError("worker capacity cannot accommodate the OS/service reserve")
    return available_cpu, available_memory


def worker_budget(capacity: Mapping[str, Any], server_id: str, flavor: str) -> tuple[int, int]:
    if capacity.get("serverId") != server_id or capacity.get("flavorName") != flavor:
        raise ValidationError("worker capacity identity or flavor drifted")
    cpu, memory = capacity.get("cpuMHz"), capacity.get("memoryMiB")
    if type(cpu) is not int or type(memory) is not int or cpu < 100 or memory < 64:
        raise ValidationError("worker allocatable capacity is malformed")
    return cpu, memory


def allocation_budget(cpu: int, memory: int) -> tuple[int, int]:
    # Same-flavor guests vary: keep 32 MiB before rounding RAM down to 128 MiB.
    # CPU is a soft share on a dedicated worker; half the budget leaves room
    # for different hypervisor clocks without limiting the app's CPU usage.
    return max(100, cpu // 200 * 100), max(64, (memory - 32) // 128 * 128)


def flavor_projection(flavor: openstack.Flavor) -> dict[str, Any]:
    return asdict(flavor)


def plan(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    reference: str,
    *,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    return prepare_plan(
        connection, config, application_id, reference, timeout_seconds=timeout_seconds
    )()


def prepare_plan(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    reference: str,
    *,
    timeout_seconds: float = 30,
) -> Callable[[], dict[str, Any]]:
    """Capture accepted sizing before the caller closes its snapshot and probes."""
    application = db.get_application(connection, application_id)
    if application is None:
        raise ValidationError("application does not exist")
    active = db.get_active_deployment(connection, application_id)
    reference = flavor_reference(reference)

    def probe() -> dict[str, Any]:
        flavor = openstack.observe_flavor_capacity(
            config.platform,
            flavor_reference(reference),
            timeout_seconds=timeout_seconds,
        )
        if flavor.ram_mib < MEMORY_RESERVE_MIB + 64:
            raise ValidationError("flavor cannot accommodate the OS/service reserve")
        projection = {
            "applicationId": application_id,
            "deploymentId": None if active is None else active.deployment_id,
            "activation": "enable-after-healthy-acceptance",
            "current": {
                "enabled": application.desired_running,
                "flavor": application.worker_flavor,
                "cpuMHz": application.scheduler_cpu_mhz,
                "memoryMiB": application.scheduler_memory_mib,
            },
            "flavor": flavor_projection(flavor),
            "allocation": "measured-worker-capacity-minus-reserve",
            "reserve": {
                "cpuMHzMinimum": CPU_RESERVE_MHZ,
                "memoryMiBMinimum": MEMORY_RESERVE_MIB,
                "percentMinimum": RESERVE_PERCENT,
            },
        }
        return {**projection, "fingerprint": db.request_fingerprint(projection)}

    return probe


def validate_plan(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    value: object,
    *,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("flavor"), dict):
        raise ValidationError("sizing plan must be an exact plan response")
    reference = flavor_reference(value["flavor"].get("flavor_id"))
    fresh = plan(connection, config, application_id, reference, timeout_seconds=timeout_seconds)
    if db.request_fingerprint(value) != db.request_fingerprint(fresh):
        raise ValidationError("sizing plan drifted; obtain and review a fresh plan")
    return fresh
