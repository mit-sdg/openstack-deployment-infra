"""Journal a single-process cutover after the replacement artifact is built.

Called under the deployment's application lock. Never creates a second
operation or changes the accepted deployment pointer.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from ..config import Config
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

    # Check provider/helper access and predecessor identity before stopping its
    # process. Worker deletion rechecks ownership after exact job removal.
    reuse = refs.get("reuse_worker") is True
    if not reuse:
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
    if reuse:
        from .worker_reuse import observe

        observe(
            connection,
            config,
            application_id,
            expected=refs["reused_worker"],
            helper_caller=helper_caller,
            deadline=deadline,
        )
        if refs.get("maintenance_quiesced") is not True:
            stopped = helper_caller(
                config,
                "app.stop",
                {
                    "slug": current.slug,
                    "jobId": intent["job_id"],
                    "candidateJobSha256": intent["job_sha256"],
                    "candidateImage": intent["image"],
                },
                deadline=deadline,
            )
            if stopped.get("jobStopped") is not True:
                raise app.ApplicationError("predecessor allocation stop is unconfirmed")
            # Retain the stopped Nomad job until this evidence is durable.
            # Job absence alone cannot prove its former processes have exited.
            refs["maintenance_quiesced"] = True
            db.checkpoint_operation(connection, operation_id, phase=operation.phase, refs=refs)
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
    if not reuse:
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
    db.set_application_runtime(
        connection,
        application_id,
        running=False,
        worker_server_id=current.worker_server_id if reuse else None,
        worker_server_name=current.worker_server_name if reuse else None,
        worker_port_id=current.worker_port_id if reuse else None,
        worker_port_name=current.worker_port_name if reuse else None,
    )
    return refs
