"""CAS review plans for redeploying retained history with current secrets and data."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..validation import ValidationError, uuid
from . import database as db
from .deployment_reads import source_repository


def target(
    connection: sqlite3.Connection, application_id: str, deployment_id: object
) -> db.DeploymentAttempt:
    attempt = db.get_deployment_attempt(
        connection, uuid(deployment_id, field="rollback deployment ID")
    )
    if (
        attempt is None
        or attempt.application_id != application_id
        or attempt.status != "succeeded"
        or attempt.configuration is None
        or attempt.configuration_revision is None
        or attempt.requested_ref is None
        or attempt.recipe_hash is None
        or attempt.image_digest is None
        or attempt.build_log_path is None
        or source_repository(connection, attempt) is None
    ):
        raise ValidationError(
            "rollback requires a complete accepted deployment and source snapshot"
        )
    return attempt


def plan(
    connection: sqlite3.Connection, application_id: str, deployment_id: object
) -> dict[str, Any]:
    application = db.get_application(connection, application_id)
    active = db.get_active_deployment(connection, application_id)
    if application is None or active is None or not application.desired_running:
        raise ValidationError(
            "rollback requires an enabled application with an accepted deployment"
        )
    attempt = target(connection, application_id, deployment_id)
    if active.deployment_id == attempt.deployment_id:
        raise ValidationError("rollback target is already the active deployment")
    environment = db.get_environment_revision(connection, application_id)
    if environment is None:
        raise db.DatabaseError("application environment revision is missing")
    projection = {
        "applicationId": application_id,
        "activeDeploymentId": active.deployment_id,
        "targetDeploymentId": attempt.deployment_id,
        "sourceRepository": source_repository(connection, attempt),
        "sourceCommit": attempt.source_commit,
        "configurationSha256": attempt.configuration_sha256,
        "imageDigest": attempt.image_digest,
        "environmentRevision": environment.revision,
        "sizing": {
            "workerFlavor": application.worker_flavor,
            "cpuMHz": application.scheduler_cpu_mhz,
            "memoryMiB": application.scheduler_memory_mib,
        },
        "storage": [
            {
                "resourceId": item.resource_id,
                "type": item.resource_type,
                "name": item.resource_name,
                "state": item.lifecycle_state,
                "providerId": item.provider_id,
                "providerName": item.provider_name,
                "updatedAt": item.updated_at,
            }
            for item in sorted(
                db.list_managed_resources(connection, application_id=application_id),
                key=lambda resource: resource.resource_id,
            )
        ],
        "environmentPolicy": "current-secrets",
        "dataPolicy": "no-data-rollback",
    }
    return {**projection, "fingerprint": db.request_fingerprint(projection)}


def validate_plan(connection: sqlite3.Connection, application_id: str, value: object) -> None:
    if not isinstance(value, dict):
        raise ValidationError("rollback plan must be an exact plan response")
    fresh = plan(connection, application_id, value.get("targetDeploymentId"))
    if value != fresh:
        raise ValidationError("rollback plan drifted; obtain and review a fresh plan")
