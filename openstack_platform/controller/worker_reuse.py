"""Fail-closed eligibility and identity checks for same-worker code updates.

This path never creates, resizes, adopts, or deletes a VM or port. It is an
explicit maintenance policy: incompatible workers require a separate reviewed
replacement deployment, not a destructive fallback hidden behind a fast update.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..validation import ValidationError, bounded_text, uuid
from . import application_runtime as app
from . import database as db
from . import sizing

if TYPE_CHECKING:
    from .deployment_service import HelperCaller

_IDENTITY_KEYS = {
    "deployment_id",
    "placement_id",
    "server_id",
    "server_name",
    "port_id",
    "port_name",
    "image_id",
    "flavor_name",
}


def preflight(
    connection: sqlite3.Connection,
    config: Config,
    spec: app.DeploymentSpec,
    *,
    selected_image_id: str,
    expected: object = None,
    allow_stopped: bool = False,
    helper_caller: HelperCaller,
    deadline: float,
) -> dict[str, str]:
    current = db.get_application(connection, spec.application_id)
    previous = db.get_deployment(connection, spec.application_id)
    if current is None or previous is None or (not current.desired_running and not allow_stopped):
        raise ValidationError(
            "worker reuse requires an enabled application with an accepted deployment"
        )
    if current.worker_flavor != spec.worker_flavor:
        raise ValidationError(
            "worker reuse cannot resize an application; use a replacement deployment"
        )
    image_id = uuid(selected_image_id, field="selected worker image UUID")
    identity = {
        "deployment_id": previous.deployment_id,
        "placement_id": app.nomad_placement_id(previous.nomad_job),
        "server_id": uuid(current.worker_server_id, field="accepted worker server UUID"),
        "server_name": bounded_text(
            current.worker_server_name, field="accepted worker name", maximum=128
        ),
        "port_id": uuid(current.worker_port_id, field="accepted worker port UUID"),
        "port_name": bounded_text(
            current.worker_port_name, field="accepted worker port name", maximum=128
        ),
        "image_id": image_id,
        "flavor_name": spec.worker_flavor,
    }
    if expected is not None and (
        not isinstance(expected, dict) or set(expected) != _IDENTITY_KEYS or expected != identity
    ):
        raise app.ApplicationError("recorded reusable worker identity drifted")
    from .fixed_ip_service import get, helper_identity

    retained = get(connection, spec.application_id)
    if retained is not None:
        helper_identity(retained)
        if (
            retained["port_id"] != identity["port_id"]
            or retained["worker_slot_id"] != identity["placement_id"]
            or retained["worker_create_pending"]
        ):
            raise ValidationError(
                "worker reuse cannot change a retained primary port or worker slot"
            )
    # Capacity already performs full provider ownership and Nomad readiness
    # observations. Its combined evidence avoids repeating worker.observe.
    observed = helper_caller(
        config,
        "app.worker.capacity",
        {"applicationId": identity["placement_id"], "slug": spec.application_slug},
        deadline=deadline,
    )
    require_observation(observed, identity)
    if (
        observed.get("applicationId") != identity["placement_id"]
        or observed.get("slug") != spec.application_slug
    ):
        raise ValidationError("reusable worker capacity belongs to a different application")
    cpu, memory = sizing.worker_budget(observed, identity["server_id"], spec.worker_flavor)
    if spec.cpu_mhz > cpu or spec.memory_mib > memory:
        raise ValidationError("pinned allocation exceeds reusable worker capacity after reserve")
    return identity


def require_observation(observed: Mapping[str, Any], identity: Mapping[str, str]) -> None:
    if (
        observed.get("ready") is not True
        or observed.get("absent") is not False
        or any(
            observed.get(output) != identity[key]
            for output, key in (
                ("serverId", "server_id"),
                ("serverName", "server_name"),
                ("portId", "port_id"),
                ("portName", "port_name"),
                ("imageId", "image_id"),
                ("flavorName", "flavor_name"),
            )
        )
    ):
        raise ValidationError(
            "worker reuse requires the exact ready worker, selected role image, and port"
        )


def ready(
    connection: sqlite3.Connection,
    operation_id: str,
    identity: Mapping[str, str],
    refs: Mapping[str, Any],
    image: str,
) -> app.DeploymentWorker:
    if (
        refs.get("worker_strategy") != "reuse"
        or refs.get("maintenance_stopped") is not True
        or refs.get("maintenance_process_stopped") is not True
        or refs.get("reused_worker") != identity
    ):
        raise app.ApplicationError("same-worker cutover lacks durable process-stop evidence")
    selected = {
        **refs,
        "worker_application_id": identity["placement_id"],
        "worker_server_id": identity["server_id"],
        "worker_server_name": identity["server_name"],
        "worker_port_id": identity["port_id"],
        "worker_port_name": identity["port_name"],
    }
    db.checkpoint_operation(
        connection, operation_id, phase="worker_ready", refs=selected, candidate_digest=image
    )
    return app.DeploymentWorker(
        identity["server_id"],
        identity["server_name"],
        identity["port_id"],
        identity["port_name"],
        selected,
    )
