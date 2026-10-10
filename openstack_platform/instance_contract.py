"""Shared, secret-free contract for isolated database instances."""

from __future__ import annotations

from .validation import ValidationError

MIB = 1024**2
GIB = 1024**3
DEFAULT_MEMORY = 512 * MIB
DEFAULT_CPU = 500
DEFAULT_CONNECTIONS = 10
MEMORY_BUDGET = 50 * GIB
TRANSITION_MEMORY_BUDGET = 36 * GIB
MAINTENANCE_MEMORY = 2 * GIB
CONNECTION_BUDGET = 2000
S3_OBJECT_BUDGET = 5_000_000
CPU_WEIGHT = 100
IO_WEIGHT = 100
TASKS_MAX = 256


class CapacityError(ValidationError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_limits(value: object) -> dict[str, int]:
    fields = {"sizeBytes", "connections", "memoryBytes", "cpuMillicores"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(
            "database quotas require sizeBytes, connections, memoryBytes and cpuMillicores"
        )
    result: dict[str, int] = {}
    for name, number in value.items():
        lower, upper = {
            "sizeBytes": (GIB, 500 * GIB),
            "connections": (1, 100),
            "memoryBytes": (DEFAULT_MEMORY, 8 * GIB),
            "cpuMillicores": (100, 4000),
        }[name]
        if isinstance(number, bool) or not isinstance(number, int) or not lower <= number <= upper:
            raise ValidationError(f"{name} must be an integer from {lower} through {upper}")
        if name == "memoryBytes" and number % MIB:
            raise ValidationError("memoryBytes must be a whole number of MiB")
        result[name] = number
    return result


def hard_quota(size: int) -> int:
    return (((size * 3 + 1) // 2 + MIB - 1) // MIB) * MIB


def garage_reservation(size: int, objects: int = 0) -> int:
    # Four KiB/object budgets key/index metadata even for zero-byte objects.
    physical = max((size * 5 + 3) // 4, objects * 4096)
    return ((physical + MIB - 1) // MIB) * MIB
