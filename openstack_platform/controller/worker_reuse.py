"""Read-only validation of the exact accepted worker; never provision a substitute."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..config import Config
from ..validation import ValidationError, bounded_text, uuid
from . import application_runtime as app
from . import database as db
from . import fixed_ip_service, sizing
from .service_support import HelperCaller


def identity(connection: sqlite3.Connection, application_id: str) -> dict[str, Any]:
    current = db.get_application(connection, application_id)
    deployment = db.get_deployment(connection, application_id)
    if current is None or deployment is None or current.worker_server_id is None:
        raise ValidationError("worker reuse requires an accepted deployment and an existing worker")
    slot = app.nomad_placement_id(deployment.nomad_job)
    if slot not in {application_id, *app.deployment_worker_ids(application_id)}:
        raise ValidationError("accepted worker placement is outside the application slots")
    retained = fixed_ip_service.get(connection, application_id)
    if retained is not None:
        fixed_ip_service.helper_identity(retained)
        if (
            retained["worker_create_pending"]
            or retained["worker_slot_id"] != slot
            or retained["port_id"] != current.worker_port_id
        ):
            raise ValidationError(
                "worker reuse requires the already-attached retained primary port"
            )
    return {
        "slot_id": slot,
        "server_id": uuid(current.worker_server_id, field="accepted worker server UUID"),
        "server_name": bounded_text(
            current.worker_server_name, field="accepted worker name", maximum=128
        ),
        "port_id": uuid(current.worker_port_id, field="accepted worker port UUID"),
        "port_name": bounded_text(
            current.worker_port_name, field="accepted worker port name", maximum=128
        ),
        "flavor": current.worker_flavor,
        "cpu_mhz": current.scheduler_cpu_mhz,
        "memory_mib": current.scheduler_memory_mib,
    }


def observe(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    *,
    expected: dict[str, Any] | None = None,
    helper_caller: HelperCaller,
    deadline: float,
) -> dict[str, Any]:
    """Pin actual image/placement and recheck capacity before any process stop."""
    selected = identity(connection, application_id)
    if expected is not None and any(expected.get(key) != value for key, value in selected.items()):
        raise app.ApplicationError("reused worker identity or allocation drifted")
    current = db.get_application(connection, application_id)
    assert current is not None
    arguments = {"applicationId": selected["slot_id"], "slug": current.slug}
    worker = helper_caller(config, "app.worker.observe", arguments, deadline=deadline)
    fields = {
        "serverId": "server_id",
        "serverName": "server_name",
        "portId": "port_id",
        "portName": "port_name",
        "flavorName": "flavor",
    }
    if (
        worker.get("ready") is not True
        or worker.get("absent") is True
        or any(worker.get(key) != selected[field] for key, field in fields.items())
    ):
        raise app.ApplicationError("exact reused worker readiness or identity was not confirmed")
    selected["image_id"] = uuid(worker.get("imageId"), field="reused worker image UUID")
    if expected is not None and selected != expected:
        raise app.ApplicationError("reused worker image drifted")
    capacity = helper_caller(config, "app.worker.capacity", arguments, deadline=deadline)
    cpu, memory = sizing.worker_budget(capacity, selected["server_id"], selected["flavor"])
    if selected["cpu_mhz"] > cpu or selected["memory_mib"] > memory:
        raise app.ApplicationError("pinned allocation exceeds reused worker capacity after reserve")
    return selected


def prepared(record: dict[str, Any], refs: dict[str, Any]) -> app.DeploymentWorker:
    return app.DeploymentWorker(
        record["server_id"],
        record["server_name"],
        record["port_id"],
        record["port_name"],
        {
            **refs,
            "worker_application_id": record["slot_id"],
            "worker_server_id": record["server_id"],
            "worker_server_name": record["server_name"],
            "worker_port_id": record["port_id"],
            "worker_port_name": record["port_name"],
        },
    )
