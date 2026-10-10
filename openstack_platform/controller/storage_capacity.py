"""Admission reservations under SQLite's short global writer transaction."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..instance_contract import CONNECTION_BUDGET, MEMORY_BUDGET, CapacityError


def check(connection: sqlite3.Connection, resource_id: str | None, target: dict[str, int]) -> None:
    from ..instance_contract import GIB, hard_quota

    if "sizeBytes" in target or "s3Bytes" in target:
        reserved = disk_reservations(connection)
        row = connection.execute(
            "SELECT measured_target_bytes,s3_bytes FROM managed_resources WHERE resource_id=?",
            (resource_id,),
        ).fetchone()
        field = "sizeBytes" if "sizeBytes" in target else "s3Bytes"
        current = (
            (row["measured_target_bytes"] if field == "sizeBytes" else row["s3_bytes"])
            if row
            else 0
        )
        increase = hard_quota(target[field]) - hard_quota(current or 0)
        if increase > 0:
            host = connection.execute(
                "SELECT usage_json FROM storage_host_usage WHERE singleton=1"
            ).fetchone()
            total = (
                json.loads(host["usage_json"]).get("dataVolume", {}).get("totalBytes", 500 * GIB)
                if host
                else 500 * GIB
            )
            if (
                reserved["databaseBytes"] + reserved["garageBytes"] + increase + 197 * GIB
                > total * 85 // 100
            ):
                raise CapacityError(
                    "DISK_BUDGET_EXCEEDED",
                    "hard disk reservations exceed 85% of the data volume; grow the volume before admitting this limit",
                )
    if "s3Bytes" in target:
        return
    resources: dict[str, dict[str, int]] = {
        row["resource_id"]: {
            "memoryBytes": int(row["memory_bytes"] or 536870912),
            "connections": int(row["postgres_connections"] or row["mongo_connections"] or 10),
        }
        for row in connection.execute(
            "SELECT * FROM managed_resources WHERE resource_type IN ('postgres','mongo')"
        )
    }
    for row in connection.execute(
        "SELECT refs_json FROM operations WHERE kind IN ('storage.limits.set','storage.usage.collect') AND status IN ('running','recovery_required')"
    ):
        refs: dict[str, Any] = json.loads(row["refs_json"])
        identifier = refs.get("resource_id")
        quotas = refs.get("quotas", {})
        if identifier in resources:
            for name in ("memoryBytes", "connections"):
                resources[identifier][name] = max(
                    resources[identifier][name], int(quotas.get(name, 0))
                )
    if resource_id in resources:
        previous = resources[resource_id]
        if all(target[name] <= previous[name] for name in ("memoryBytes", "connections")):
            return
        resources[resource_id] = {
            name: max(previous[name], target[name]) for name in ("memoryBytes", "connections")
        }
    else:
        resources[resource_id or "new"] = {
            name: target[name] for name in ("memoryBytes", "connections")
        }
    if sum(item["memoryBytes"] for item in resources.values()) > MEMORY_BUDGET:
        raise CapacityError(
            "MEMORY_BUDGET_EXCEEDED",
            "instance memory reservations exceed the 50 GiB storage budget",
        )
    if sum(item["connections"] for item in resources.values()) > CONNECTION_BUDGET:
        raise CapacityError(
            "CONNECTION_BUDGET_EXCEEDED", "instance application connection reservations exceed 2000"
        )


def disk_reservations(connection: sqlite3.Connection) -> dict[str, int]:
    from ..instance_contract import hard_quota

    values = {
        row["resource_id"]: {
            "sizeBytes": int(row["measured_target_bytes"] or 0),
            "s3Bytes": int(row["s3_bytes"] or 0),
        }
        for row in connection.execute("SELECT * FROM managed_resources")
    }
    for row in connection.execute(
        "SELECT refs_json FROM operations WHERE kind IN ('storage.limits.set','storage.usage.collect') AND status IN ('running','recovery_required')"
    ):
        refs = json.loads(row["refs_json"])
        if refs.get("resource_id") in values:
            for key in ("sizeBytes", "s3Bytes"):
                values[refs["resource_id"]][key] = max(
                    values[refs["resource_id"]][key], refs.get("quotas", {}).get(key, 0)
                )
    return {
        "databaseBytes": sum(hard_quota(item["sizeBytes"]) for item in values.values()),
        "garageBytes": sum(hard_quota(item["s3Bytes"]) for item in values.values()),
    }


def retained_worker(connection: sqlite3.Connection, application_id: str) -> dict[str, str] | None:
    row = connection.execute(
        "SELECT record_json FROM application_fixed_ports WHERE application_id=?", (application_id,)
    ).fetchone()
    if row is None:
        return None
    record = json.loads(row["record_json"])
    return {"slotId": record["worker_slot_id"], "address": record["address"]}


def workload_job(connection: sqlite3.Connection, application_id: str) -> str | None:
    from . import application_runtime as app
    from . import database as db

    application = db.get_application(connection, application_id)
    deployment = db.get_deployment(connection, application_id)
    return (
        None
        if application is None or not application.desired_running or deployment is None
        else app.nomad_job_id(deployment.nomad_job, application.slug)
    )


def helper_metadata(
    connection: sqlite3.Connection, application_id: str, resource: Any
) -> dict[str, Any]:
    from . import application_runtime as app
    from .storage_limits import quotas

    return {
        "resourceId": None if resource is None else resource.resource_id,
        "reservations": disk_reservations(connection),
        "workloadJobId": workload_job(connection, application_id),
        "retainedWorker": retained_worker(connection, application_id),
        "instanceId": None if resource is None else resource.instance_id,
        "instanceQuotas": None
        if resource is None or resource.resource_type == "s3"
        else quotas(resource),
        "workerIds": [application_id, *app.deployment_worker_ids(application_id)],
    }
