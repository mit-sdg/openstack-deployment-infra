"""Fixed operator alarm thresholds for the shared storage VM."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ALARM_CODES = frozenset(
    {
        "postgres_connections_high",
        "mongo_connections_high",
        "storage_volume_high",
        "storage_memory_high",
        "storage_container_memory_high",
        "storage_write_blocked",
        "instance_unavailable",
        "instance_connections_high",
    }
)


def alarms(host: Mapping[str, Any]) -> list[str]:
    result = []
    # Leave 20% connection headroom before connection refusal disrupts apps.
    for provider in ("postgres", "mongo"):
        count = host[f"{provider}Connections"]
        if count["current"] * 100 >= count["limit"] * 80:
            result.append(f"{provider}_connections_high")
    # Warn early enough to expand/clean the actual data volume.
    volume = host["dataVolume"]
    if volume["usedBytes"] * 100 >= volume["totalBytes"] * 85:
        result.append("storage_volume_high")
    memory = host["memory"]
    if memory["availableBytes"] * 100 <= memory["totalBytes"] * 10:
        result.append("storage_memory_high")
    for container in host["containers"]:
        if container["usedBytes"] * 100 >= container["limitBytes"] * 90:
            result.append("storage_container_memory_high")
            break
    for instance in host.get("instances", []):
        if not instance["available"]:
            if "instance_unavailable" not in result:
                result.append("instance_unavailable")
        elif (
            instance["currentConnections"] * 100 >= instance["connectionLimit"] * 80
            and "instance_connections_high" not in result
        ):
            result.append("instance_connections_high")
    if host["writeBlockedResources"]:
        result.append("storage_write_blocked")
    return result
