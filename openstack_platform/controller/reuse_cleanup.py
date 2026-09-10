"""Confirm failed candidate process exit before retaining a worker for recovery."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from ..config import Config
from ..validation import oci_digest_pin, sha256_hex
from . import application_runtime as app
from . import database as db
from . import worker_reuse

if TYPE_CHECKING:
    from .deployment_service import HelperCaller


def finish_rejection(
    connection: sqlite3.Connection,
    config: Config,
    operation_id: str,
    application_id: str,
    application_slug: str,
    *,
    helper_caller: HelperCaller,
    deadline: float,
) -> None:
    operation = db.get_operation(connection, operation_id)
    if (
        operation is None
        or operation.phase != "candidate_rejected"
        or operation.refs.get("worker_strategy") != "reuse"
    ):
        raise app.ApplicationError("reused candidate rejection intent is invalid")
    refs = dict(operation.refs)
    worker = refs.get("reused_worker")
    job_id = refs.get("candidate_job_id")
    if not isinstance(worker, dict) or job_id not in {
        application_slug,
        f"{application_slug}-candidate",
    }:
        raise app.ApplicationError("rejected reused candidate identity is incomplete")
    image = oci_digest_pin(operation.candidate_digest, field="rejected candidate image")
    job_hash = sha256_hex(refs.get("candidate_job_sha256"), field="rejected candidate job SHA-256")
    version = refs.get("candidate_nomad_version")
    if type(version) is not int or version < 0:
        raise app.ApplicationError("rejected candidate Nomad version is unavailable")
    observed = helper_caller(
        config,
        "app.worker.observe",
        {"applicationId": worker["placement_id"], "slug": application_slug},
        deadline=deadline,
    )
    worker_reuse.require_observation(observed, worker)
    if refs.get("candidate_process_stopped") is not True:
        stopped = helper_caller(
            config,
            "app.quiesce",
            {
                "operationId": operation_id,
                "nodeId": worker["node_id"],
                "jobVersion": version,
                "slug": application_slug,
                "jobId": job_id,
                "candidateJobSha256": job_hash,
                "candidateImage": image,
            },
            deadline=deadline,
        )
        if (
            stopped.get("operationId") != operation_id
            or stopped.get("nodeId") != worker["node_id"]
            or type(stopped.get("jobVersion")) is not int
            or stopped.get("jobVersion") != version
            or stopped.get("jobId") != job_id
            or stopped.get("candidateJobSha256") != job_hash
            or stopped.get("candidateImage") != image
            or stopped.get("jobStopped") is not True
            or stopped.get("allocationsStopped") is not True
        ):
            raise app.ApplicationError("failed candidate process exit is unconfirmed")
        refs["candidate_quiesce_receipt"] = sha256_hex(
            stopped.get("receiptSha256"), field="candidate exit receipt SHA-256"
        )
        refs["candidate_process_stopped"] = True
        db.checkpoint_operation(connection, operation_id, phase="candidate_rejected", refs=refs)
    else:
        sha256_hex(refs.get("candidate_quiesce_receipt"), field="candidate exit receipt SHA-256")
    removed = helper_caller(
        config,
        "app.remove",
        {
            "slug": application_slug,
            "jobId": job_id,
            "candidateJobSha256": job_hash,
            "candidateImage": image,
        },
        deadline=deadline,
    )
    if removed.get("jobAbsent") is not True:
        raise app.ApplicationError("quiesced candidate job removal is unconfirmed")
    # A rebuild can reproduce a retained artifact, especially with slim output
    # or caching. Never delete an image that any accepted history still needs.
    retained = db.list_application_successful_manifest_history(connection, application_id)
    if image not in retained:
        deleted = helper_caller(
            config,
            "app.manifest.delete",
            {"slug": application_slug, "image": image, "references": list(retained[:6])},
            deadline=deadline,
        )
        if deleted.get("absent") is not True:
            raise app.ApplicationError("rejected candidate image removal is unconfirmed")
    message = (
        "candidate failed; exact process exit and job removal confirmed; accepted worker retained"
    )
    db.checkpoint_deployment_attempt(
        connection, operation_id, status="failed", error=message, cleanup_state="confirmed"
    )
    db.mark_failed(connection, operation_id, message, cleanup_state="confirmed")
