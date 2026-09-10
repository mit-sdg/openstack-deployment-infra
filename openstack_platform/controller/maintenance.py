"""Journal a single-process cutover after the replacement artifact is built.

Called under the deployment's application lock. Never creates a second
operation or changes the accepted deployment pointer.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..validation import sha256_hex
from . import application_runtime as app
from . import database as db

if TYPE_CHECKING:
    from .deployment_service import HelperCaller


def stop_predecessor(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    operation_id: str,
    *,
    helper_caller: HelperCaller,
    deadline: float,
) -> dict[str, Any]:
    operation = db.get_operation(connection, operation_id)
    current = db.get_application(connection, application_id)
    if (
        operation is None
        or current is None
        or operation.kind != "app.deploy"
        or operation.scope != f"app-{application_id}"
        or operation.refs.get("maintenance") is not True
    ):
        raise app.ApplicationError("maintenance application/operation consent is missing")
    refs = dict(operation.refs)
    reuse = refs.get("worker_strategy") == "reuse"
    reusable = refs.get("reused_worker")
    if reuse and not isinstance(reusable, dict):
        raise app.ApplicationError("same-worker maintenance requires verified worker identity")
    intent = refs.get("maintenance_predecessor")
    if intent is None:
        if not current.desired_running:
            return refs
        accepted = db.get_active_deployment(connection, application_id)
        deployment = db.get_deployment(connection, application_id)
        if accepted is None or deployment is None:
            # An application declaration with no accepted process needs no stop.
            return refs
        if current.worker_server_id is None or current.worker_port_id is None:
            raise app.ApplicationError("maintenance predecessor identity is incomplete")
        intent = {
            "deployment_id": accepted.deployment_id,
            "job_id": app.nomad_job_id(deployment.nomad_job, current.slug),
            "job_sha256": deployment.nomad_job_sha256,
            "image": deployment.image_digest,
            "placement_id": app.nomad_placement_id(deployment.nomad_job),
            "server_id": current.worker_server_id,
            "port_id": current.worker_port_id,
        }
        refs["maintenance_predecessor"] = intent
        db.checkpoint_operation(connection, operation_id, phase=operation.phase, refs=refs)
    accepted = db.get_active_deployment(connection, application_id)
    if (
        not isinstance(intent, dict)
        or accepted is None
        or accepted.deployment_id != intent.get("deployment_id")
    ):
        raise app.ApplicationError("maintenance accepted deployment identity drifted")
    if refs.get("maintenance_stopped") is True and not current.desired_running:
        return refs

    # Same-worker preparation just supplied full provider/capacity evidence;
    # no VM or port mutation occurs between it and process quiescence. A retry
    # repeats that preflight before returning here, rather than trusting a
    # persisted readiness bit. Replacement retains its independent observation.
    if reuse:
        assert isinstance(reusable, dict)
        if any(
            reusable.get(key) != intent[key]
            for key in ("deployment_id", "placement_id", "server_id", "port_id")
        ):
            raise app.ApplicationError("reusable worker does not match the maintenance predecessor")
        if refs.get("maintenance_process_stopped") is not True:
            stopped = helper_caller(
                config,
                "app.quiesce",
                {
                    "operationId": operation_id,
                    "nodeId": reusable["node_id"],
                    "slug": current.slug,
                    "jobId": intent["job_id"],
                    "candidateJobSha256": intent["job_sha256"],
                    "candidateImage": intent["image"],
                },
                deadline=deadline,
            )
            if (
                stopped.get("jobId") != intent["job_id"]
                or stopped.get("candidateJobSha256") != intent["job_sha256"]
                or stopped.get("candidateImage") != intent["image"]
                or stopped.get("jobStopped") is not True
                or stopped.get("allocationsStopped") is not True
            ):
                raise app.ApplicationError("exact predecessor process exit is unconfirmed")
            # Purging a Nomad job is not itself proof that its client stopped.
            # Persist positive exit evidence before discarding that job record.
            refs["maintenance_quiesce_receipt"] = sha256_hex(
                stopped.get("receiptSha256"), field="process exit receipt SHA-256"
            )
            refs["maintenance_process_stopped"] = True
            db.checkpoint_operation(connection, operation_id, phase=operation.phase, refs=refs)
    else:
        observed = helper_caller(
            config,
            "app.worker.observe",
            {"applicationId": intent["placement_id"], "slug": current.slug},
            deadline=deadline,
        )
        if observed.get("absent") is not True and (
            observed.get("serverId") != intent["server_id"]
            or observed.get("portId") != intent["port_id"]
        ):
            raise app.ApplicationError("maintenance predecessor worker identity drifted")
    # Until the stopped checkpoint is durable, no candidate may be created.
    removed = helper_caller(
        config,
        "app.remove",
        {
            "slug": current.slug,
            "jobId": intent["job_id"],
            "candidateJobSha256": intent["job_sha256"],
            "candidateImage": intent["image"],
        },
        deadline=deadline,
    )
    if removed.get("jobAbsent") is not True:
        raise app.ApplicationError("maintenance predecessor job absence is unconfirmed")
    if reuse:
        assert isinstance(reusable, dict)
        refs["maintenance_stopped"] = True
        db.checkpoint_operation(connection, operation_id, phase=operation.phase, refs=refs)
        # Keep the exact worker visible while stopped. Enable can restore the
        # accepted artifact on it; disable/delete can still reclaim it.
        db.set_application_runtime(
            connection,
            application_id,
            running=False,
            worker_server_id=reusable["server_id"],
            worker_server_name=reusable["server_name"],
            worker_port_id=reusable["port_id"],
            worker_port_name=reusable["port_name"],
        )
        return refs
    from .public_ip_service import release_locked

    release_locked(connection, config, application_id, deadline=deadline, reserve_only=True)
    deleted = helper_caller(
        config,
        "app.worker.delete",
        {"applicationId": intent["placement_id"], "slug": current.slug, "single": True},
        deadline=deadline,
    )
    if deleted.get("absent") is not True:
        raise app.ApplicationError("maintenance predecessor worker absence is unconfirmed")
    refs["maintenance_stopped"] = True
    # Journal before changing desired state. If interrupted between these
    # writes, a retry rechecks exact absence and completes the state change.
    db.checkpoint_operation(connection, operation_id, phase=operation.phase, refs=refs)
    db.set_application_runtime(connection, application_id, running=False)
    return refs
