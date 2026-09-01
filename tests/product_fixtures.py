"""Test-only builders for current product state."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid

from openstack_platform.controller import database as db
from openstack_platform.controller.deployment_config import parse_configuration


def accept_deployment(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    source_commit: str,
    recipe_hash: str,
    image_digest: str,
    nomad_job: str,
    nomad_version: int,
    build_log_path: str,
    nomad_job_sha256: str | None = None,
    health_path: str = "/",
    application_port: int = 8080,
) -> db.DeploymentAttempt:
    """Create and accept a strict deployment attempt for a test application."""
    request_id = str(uuid.uuid4())
    deployment_id = str(uuid.uuid4())
    configuration = parse_configuration(
        {
            "schemaVersion": 1,
            "build": {
                "runtime": "node",
                "packages": ["."],
                "buildScript": None,
                "startScript": "start",
            },
            "runtime": {"port": application_port, "healthPath": health_path},
            "storageBindings": [],
        }
    )
    db.claim_idempotency_request(
        connection,
        request_id=request_id,
        request_fingerprint=db.request_fingerprint({"deploymentId": deployment_id}),
    )
    db.create_deployment_attempt(
        connection,
        deployment_id=deployment_id,
        application_id=application_id,
        source_commit=source_commit,
        requested_ref="refs/heads/main",
        configuration_revision=1,
        configuration=configuration,
        environment_revision=0,
        idempotency_request_id=request_id,
    )
    return db.checkpoint_deployment_attempt(
        connection,
        deployment_id,
        status="succeeded",
        recipe_hash=recipe_hash,
        image_digest=image_digest,
        nomad_job=nomad_job,
        nomad_job_sha256=nomad_job_sha256 or hashlib.sha256(nomad_job.encode()).hexdigest(),
        nomad_version=nomad_version,
        build_log_path=build_log_path,
    )
