"""Explicit physical fencing when a reused worker's process exit is unprovable.

The fence has a separate dispatch/domain scope, but holds the original app lock.
It never treats missing Nomad records as process-exit evidence. Only exact worker
absence authorizes purging the bounded recorded jobs; accepted artifacts and data
are untouched. The original dispatch stays blocked until atomic completion.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .. import openstack, remote, runtime
from ..config import Config
from ..helper.errors import HelperActionError
from ..validation import ValidationError, bounded_text, oci_digest_pin, sha256_hex, uuid
from . import application_runtime as app
from . import database as db
from . import fixed_ip_service, public_ip_service
from .application_service import ApplicationLifecycleChanged
from .service_support import HelperCaller, operation_deadline, remaining_seconds, wall_deadline


@dataclass(frozen=True)
class Target:
    operation: db.Operation
    application: db.Application
    worker: dict[str, str]
    jobs: tuple[dict[str, str], ...]


def _target(
    connection: sqlite3.Connection, application_id: str, interrupted_id: str, fence_id: str
) -> Target:
    current = db.get_application(connection, application_id)
    original = db.get_operation(connection, interrupted_id)
    dispatch = db.get_operation_dispatch(connection, interrupted_id)
    attempt = db.get_deployment_attempt(connection, interrupted_id)
    if (
        current is None
        or original is None
        or dispatch is None
        or attempt is None
        or original.kind != "app.deploy"
        or original.scope != f"app-{application_id}"
        or (dispatch.kind, dispatch.scope) != (original.kind, original.scope)
        or dispatch.status not in {"recovery_required", "finished"}
        or attempt.application_id != application_id
        or attempt.status != "recovery_required"
        or attempt.idempotency_request_id != interrupted_id
        or attempt.accepted_at is not None
        or original.refs.get("application_id") != application_id
        or original.refs.get("slug") != current.slug
        or original.refs.get("worker_strategy") != "reuse"
    ):
        raise ValidationError("fencing requires an interrupted, unaccepted same-worker deployment")
    owner = original.refs.get("fence_operation_id")
    if owner is None:
        if original.status != "recovery_required":
            raise ValidationError("original deployment is queued or running")
    elif owner != fence_id or original.status != "running":
        raise ValidationError("original deployment belongs to another fence request")
    accepted = db.get_deployment(connection, application_id)
    predecessor = original.refs.get("maintenance_predecessor")
    reused = original.refs.get("reused_worker")
    if accepted is None or not isinstance(predecessor, dict) or not isinstance(reused, dict):
        raise ValidationError("fencing requires recorded predecessor and reused worker identities")
    # Additional evidence (for example node_id) is not a destructive selector.
    worker = {
        key: uuid(reused.get(key), field=f"reused worker {key}")
        for key in ("deployment_id", "placement_id", "server_id", "port_id")
    }
    worker.update(
        {
            key: bounded_text(reused.get(key), field=f"reused worker {key}", maximum=128)
            for key in ("server_name", "port_name")
        }
    )
    if (
        accepted.deployment_id == interrupted_id
        or accepted.deployment_id != worker["deployment_id"]
        or app.nomad_placement_id(accepted.nomad_job) != worker["placement_id"]
        or worker["placement_id"]
        not in {application_id, *app.deployment_worker_ids(application_id)}
        or any(
            predecessor.get(key) != worker[key]
            for key in ("deployment_id", "placement_id", "server_id", "port_id")
        )
        or any(
            getattr(current, f"worker_{key}") != worker[key]
            for key in ("server_id", "port_id", "server_name", "port_name")
        )
        or predecessor.get("job_id") != app.nomad_job_id(accepted.nomad_job, current.slug)
        or predecessor.get("job_sha256") != accepted.nomad_job_sha256
        or predecessor.get("image") != accepted.image_digest
    ):
        raise ValidationError("fencing accepted predecessor or exact worker identity drifted")
    retained = fixed_ip_service.get(connection, application_id)
    if retained is not None:
        fixed_ip_service.helper_identity(retained)
        if (
            retained["port_id"] != worker["port_id"]
            or retained["worker_slot_id"] != worker["placement_id"]
            or retained["worker_create_pending"]
        ):
            raise ValidationError("fencing retained primary port identity is unresolved")
    floating = public_ip_service.get(connection, application_id)
    if floating is not None and floating.get("port") is not None:
        port = floating["port"]
        if not isinstance(port, dict) or any(
            port.get(key) != value
            for key, value in {
                "slot_id": worker["placement_id"],
                "slug": current.slug,
                "server_id": worker["server_id"],
                "port_id": worker["port_id"],
                "server_name": worker["server_name"],
                "port_name": worker["port_name"],
            }.items()
        ):
            raise ValidationError("fencing floating IP points to another worker")
    jobs = [
        {
            "slug": current.slug,
            "jobId": predecessor["job_id"],
            "candidateJobSha256": predecessor["job_sha256"],
            "candidateImage": predecessor["image"],
        }
    ]
    candidate_id = original.refs.get("candidate_job_id")
    candidate_hash = original.refs.get("candidate_job_sha256")
    if candidate_id is not None or candidate_hash is not None:
        if candidate_id not in {current.slug, f"{current.slug}-candidate"}:
            raise ValidationError("fencing candidate job is outside the bounded application jobs")
        jobs.insert(
            0,
            {
                "slug": current.slug,
                "jobId": candidate_id,
                "candidateJobSha256": sha256_hex(candidate_hash, field="candidate job SHA-256"),
                "candidateImage": oci_digest_pin(
                    original.candidate_digest, field="candidate image"
                ),
            },
        )
    elif original.phase not in {"image_pushed", "builder_cleaned"}:
        # Older journals with an uncertain submission but no exact candidate
        # identity cannot authorize purging a guessed job or later enable.
        raise ValidationError(
            "fencing requires durable candidate identity after worker preparation"
        )
    return Target(original, current, worker, tuple(jobs))


def _claim(
    connection: sqlite3.Connection,
    application_id: str,
    interrupted_id: str,
    fence_id: str,
    deadline_at: str,
) -> Target:
    # This transaction races the original API requeue transaction: either it
    # becomes pending first (and we refuse), or its running fence marker makes
    # requeue_recovery_dispatch refuse. There is no check/update gap.
    with db.transaction(connection):
        target = _target(connection, application_id, interrupted_id, fence_id)
        refs = {
            "application_id": application_id,
            "slug": target.application.slug,
            "interrupted_deployment_id": interrupted_id,
        }
        scope = f"app-fence-{application_id}"
        own = db.get_operation(connection, fence_id)
        if own is None:
            active = db.get_unfinished_operation(connection, scope)
            if active is not None:
                raise db.UnfinishedOperationError(scope, active.operation_id, active.kind)
            connection.execute(
                "INSERT INTO operations(operation_id,kind,scope,status,phase,started_at,updated_at,"
                "deadline_at,refs_json,cleanup_state) VALUES (?,?,?,'running','fencing',?,?,?,?, 'pending')",
                (
                    fence_id,
                    "app.disable.fence",
                    scope,
                    db.utc_now(),
                    db.utc_now(),
                    deadline_at,
                    db._refs_json(refs),
                ),
            )
        else:
            if (
                own.kind != "app.disable.fence"
                or own.scope != scope
                or own.status not in {"running", "recovery_required"}
                or any(own.refs.get(key) != value for key, value in refs.items())
            ):
                raise ValidationError("fence operation does not match this request")
            connection.execute(
                "UPDATE operations SET status='running',deadline_at=?,updated_at=?,safe_error=NULL "
                "WHERE operation_id=?",
                (deadline_at, db.utc_now(), fence_id),
            )
        connection.execute(
            "UPDATE operations SET status='running',cleanup_state='pending',refs_json=?,updated_at=? WHERE operation_id=?",
            (
                db._refs_json({**target.operation.refs, "fence_operation_id": fence_id}),
                db.utc_now(),
                interrupted_id,
            ),
        )
    return target


def _purge_jobs(config: Config, target: Target, caller: HelperCaller, deadline: float) -> None:
    # A same-worker candidate can replace the predecessor under the SAME job
    # ID. Try its exact identity first, falling back only on an explicit mismatch
    # (never on transport loss). Any unrecorded identity leaves the fence blocked.
    for job_id in dict.fromkeys(job["jobId"] for job in target.jobs):
        choices = [job for job in target.jobs if job["jobId"] == job_id]
        for index, job in enumerate(choices):
            try:
                result = caller(config, "app.remove", job, deadline=deadline)
            except (remote.HelperError, HelperActionError) as error:
                if error.code != "CANDIDATE_MISMATCH" or index == len(choices) - 1:
                    raise
                continue
            if result.get("jobAbsent") is not True:
                raise app.ApplicationError("fenced exact job absence was not confirmed")
            break


def _complete(
    connection: sqlite3.Connection, application_id: str, interrupted_id: str, fence_id: str
) -> None:
    with db.transaction(connection):
        _target(connection, application_id, interrupted_id, fence_id)
        now = db.utc_now()
        message = "interrupted deployment explicitly disabled after exact physical worker fencing"
        connection.execute(
            "UPDATE applications SET desired_running=0,worker_server_id=NULL,worker_server_name=NULL,"
            "worker_port_id=NULL,worker_port_name=NULL,updated_at=? WHERE application_id=?",
            (now, application_id),
        )
        connection.execute(
            "UPDATE active_deployments SET lifecycle_state='stopped',updated_at=? WHERE application_id=?",
            (now, application_id),
        )
        db.checkpoint_deployment_attempt(
            connection,
            interrupted_id,
            status="failed",
            error=message,
            cleanup_state="confirmed",
            _within_transaction=True,
        )
        connection.execute(
            "UPDATE operations SET status='failed',cleanup_state='confirmed',safe_error=?,updated_at=? "
            "WHERE operation_id=?",
            (message, now, interrupted_id),
        )
        connection.execute(
            "UPDATE operation_dispatches SET status='finished',safe_error=NULL,updated_at=? "
            "WHERE operation_id=?",
            (now, interrupted_id),
        )
        cursor = connection.execute(
            "UPDATE operations SET status='succeeded',phase='fenced',cleanup_state='confirmed',"
            "safe_error=NULL,updated_at=? WHERE operation_id=? AND status='running'",
            (now, fence_id),
        )
        if cursor.rowcount != 1:
            raise db.DatabaseError("fence completion operation changed")


def disable_interrupted_deployment(
    connection: sqlite3.Connection,
    config: Config,
    state_directory: Path,
    application_id: str,
    interrupted_deployment_id: str,
    *,
    request_id: str,
    helper_caller: HelperCaller,
) -> ApplicationLifecycleChanged:
    application_id = uuid(application_id, field="application ID")
    interrupted_id = uuid(interrupted_deployment_id, field="interrupted deployment ID")
    fence_id = uuid(request_id, field="fence request ID")
    if fence_id == interrupted_id:
        raise ValidationError("fencing requires its own idempotency key")
    deadline = operation_deadline(config)
    with runtime.lock(state_directory, f"app-{application_id}", deadline=deadline):
        own = db.get_operation(connection, fence_id)
        if own is not None and own.status == "succeeded":
            if (
                own.kind != "app.disable.fence"
                or own.scope != f"app-fence-{application_id}"
                or own.refs.get("interrupted_deployment_id") != interrupted_id
            ):
                raise ValidationError("completed fence does not match this request")
            # In particular, never reobserve/delete a later deployment on replay.
            return ApplicationLifecycleChanged(application_id, str(own.refs["slug"]), "disabled")
        target = _claim(
            connection, application_id, interrupted_id, fence_id, wall_deadline(deadline)
        )
        try:
            openstack.verify_project(
                config.platform,
                timeout_seconds=remaining_seconds(deadline, config.policy.limits.process_seconds),
            )
            caller = fixed_ip_service.worker_helper(connection, helper_caller)
            public_ip_service.release_locked(
                connection,
                config,
                application_id,
                deadline=deadline,
                reserve_only=True,
            )
            result = caller(
                config,
                "app.worker.delete",
                {
                    "applicationId": target.worker["placement_id"],
                    "slug": target.application.slug,
                    "single": True,
                    "expectedServerId": target.worker["server_id"],
                    "expectedPortId": target.worker["port_id"],
                },
                deadline=deadline,
            )
            if result.get("absent") is not True:
                raise app.ApplicationError("exact physical worker absence was not confirmed")
            db.checkpoint_operation(connection, fence_id, phase="worker_absent")
            _purge_jobs(config, target, caller, deadline)
            result = caller(
                config, "app.builder.delete", {"buildId": interrupted_id}, deadline=deadline
            )
            if result.get("absent") is not True or result.get("buildId") != interrupted_id:
                raise app.ApplicationError("exact interrupted builder absence was not confirmed")
            _complete(connection, application_id, interrupted_id, fence_id)
        except Exception as error:
            # Do NOT make the original eligible for a retry. Its running marker
            # plus recovery dispatch survives restart and reserves the app scope.
            own = db.get_operation(connection, fence_id)
            if own is not None and own.status == "running":
                db.mark_recovery_required(connection, fence_id, error)
            raise
    return ApplicationLifecycleChanged(application_id, target.application.slug, "disabled")
