"""Resumable per-app migration admission, quiescence and binding refresh."""

from __future__ import annotations

import sqlite3
import time
import uuid as uuid_module
from pathlib import Path

from .. import runtime
from ..config import Config
from ..instance_contract import GIB, hard_quota, validate_limits
from ..validation import ValidationError
from . import application_runtime as app
from . import database as db
from .service_support import HelperCaller, operation_deadline, wall_deadline
from .storage_capacity import disk_reservations, retained_worker
from .storage_limits import StorageLimitsService, quotas

MIGRATE_KIND = "storage.instances.migrate"


class InstanceMigrationService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        directory: Path,
        *,
        helper_caller: HelperCaller,
    ):
        self.connection, self.config, self.directory, self.helper = (
            connection,
            config,
            directory,
            helper_caller,
        )

    def migrate(self, *, request_id: str) -> None:
        deadline = operation_deadline(self.config)
        with runtime.lock(self.directory, "infrastructure", wait=True, deadline=deadline):
            operation = db.get_unfinished_operation(self.connection, "infrastructure")
            if operation is not None:
                if operation.operation_id != request_id or operation.kind != MIGRATE_KIND:
                    raise db.UnfinishedOperationError(
                        "infrastructure", operation.operation_id, operation.kind
                    )
                operation = db.renew_operation_deadline(
                    self.connection, request_id, wall_deadline(deadline)
                )
            else:
                applications = sorted(
                    {
                        item.application_id
                        for item in db.list_managed_resources(self.connection)
                        if item.resource_type in {"postgres", "mongo"} and item.instance_id is None
                    }
                )
                operation = db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind=MIGRATE_KIND,
                    scope="infrastructure",
                    phase="migrating",
                    deadline_at=wall_deadline(deadline),
                    refs={"applications": applications, "completed": []},
                )
            completed = list(operation.refs["completed"])
            try:
                for identifier in operation.refs["applications"]:
                    if identifier in completed:
                        continue
                    self.application(identifier, request_id, deadline)
                    completed.append(identifier)
                    db.checkpoint_operation(
                        self.connection,
                        request_id,
                        phase="migrating",
                        refs={"completed": completed},
                        merge_refs=True,
                    )
                db.mark_succeeded(self.connection, request_id, cleanup_state="not_required")
            except Exception:
                db.mark_recovery_required(
                    self.connection,
                    request_id,
                    "instance migration requires replay; old shared data remains intact",
                )
                raise

    def application(self, identifier: str, master_id: str, deadline: float) -> None:
        key = str(uuid_module.uuid5(uuid_module.UUID(master_id), identifier))
        scope = f"app-{identifier}"
        with runtime.lock(self.directory, scope, wait=True, deadline=deadline):
            existing = db.get_operation(self.connection, key)
            if existing is not None and existing.status == "succeeded":
                return
            pending = db.get_unfinished_operation(self.connection, scope)
            if pending is not None and (
                pending.operation_id != key or pending.kind != MIGRATE_KIND
            ):
                raise db.UnfinishedOperationError(scope, pending.operation_id, pending.kind)
            application = db.get_application(self.connection, identifier)
            if application is None:
                raise ValidationError("migration application is absent")
            deployment = db.get_deployment(self.connection, identifier)
            running = application.desired_running and deployment is not None
            if pending is None:
                selected = [
                    item.resource_id
                    for item in sorted(
                        db.list_managed_resources(self.connection, application_id=identifier),
                        key=lambda resource: (
                            resource.resource_type != "postgres",
                            resource.resource_name,
                        ),
                    )
                    if item.resource_type in {"postgres", "mongo"} and item.instance_id is None
                ]
                for resource_id in selected:
                    resource = db.get_managed_resource(self.connection, resource_id)
                    assert resource is not None
                    if resource.lifecycle_state != "active":
                        raise ValidationError(
                            "resolve unfinished storage lifecycle before migration"
                        )
                    validate_limits(quotas(resource))
                    probe = StorageLimitsService(
                        self.connection, self.config, self.directory, helper_caller=self.helper
                    )._call(resource, "usage", deadline)
                    used = probe.get("usage", {})
                    if not isinstance(used, dict) or not isinstance(used.get("usedBytes"), int):
                        raise ValidationError("migration size preflight is unavailable")
                    if used["usedBytes"] > 14 * GIB:
                        raise ValidationError(
                            "increase the 16 GiB migration archive reservation before migrating a resource over 14 GiB"
                        )
                    if used["usedBytes"] + (
                        384 if resource.resource_type == "postgres" else 128
                    ) * 1024**2 > hard_quota(resource.measured_target_bytes or 0):
                        raise ValidationError(
                            "raise resource size before migration; source data will not fit the new hard quota"
                        )
                pending = db.begin_operation(
                    self.connection,
                    operation_id=key,
                    kind=MIGRATE_KIND,
                    scope=scope,
                    phase="validated",
                    deadline_at=wall_deadline(deadline),
                    refs={"resources": selected, "completed": [], "was_running": running},
                )
            else:
                pending = db.renew_operation_deadline(self.connection, key, wall_deadline(deadline))

            completed = list(pending.refs["completed"])
            try:
                if (
                    pending.phase == "validated"
                    and pending.refs["was_running"]
                    and deployment is not None
                ):
                    identity = app.nomad_candidate_identity(deployment.nomad_job)
                    result = self.helper(
                        self.config,
                        "app.stop",
                        {
                            "slug": application.slug,
                            "jobId": app.nomad_job_id(deployment.nomad_job, application.slug),
                            "candidateJobSha256": identity[0],
                            "candidateImage": identity[1],
                        },
                        deadline=deadline,
                    )
                    if result.get("jobStopped") is not True:
                        raise ValidationError("application quiescence was not confirmed")
                if pending.phase == "validated":
                    db.checkpoint_operation(self.connection, key, phase="quiesced")
                for resource_id in pending.refs["resources"]:
                    if resource_id in completed:
                        continue
                    resource = db.get_managed_resource(self.connection, resource_id)
                    if resource is None:
                        raise ValidationError("migration resource is absent")
                    with db.transaction(self.connection):
                        self.connection.execute(
                            "UPDATE managed_resources SET migration_state='copying' WHERE resource_id=?",
                            (resource_id,),
                        )
                    result = self.helper(
                        self.config,
                        MIGRATE_KIND,
                        {
                            "applicationId": identifier,
                            "applicationSlug": application.slug,
                            "resourceName": resource.resource_name,
                            "type": resource.resource_type,
                            "providerId": resource.provider_id,
                            "providerName": resource.provider_name,
                            "instanceId": resource.resource_id,
                            "quotas": quotas(resource),
                            "operationId": key,
                            "recover": pending.status == "recovery_required",
                            "workerIds": [identifier, *app.deployment_worker_ids(identifier)],
                            "reservations": disk_reservations(self.connection),
                            "retainedWorker": retained_worker(self.connection, identifier),
                        },
                        deadline=deadline,
                    )
                    if result.get("verified") is not True or result.get("published") is not True:
                        raise ValidationError("instance migration was not verified and published")
                    port = result.get("instancePort")
                    if (
                        isinstance(port, bool)
                        or not isinstance(port, int)
                        or not 30000 <= port <= 30999
                    ):
                        raise ValidationError("instance migration endpoint is invalid")
                    db.set_storage_instance(
                        self.connection, resource_id, resource_id, port, migration_state="switched"
                    )
                    completed.append(resource_id)
                    db.checkpoint_operation(
                        self.connection,
                        key,
                        phase="switched",
                        refs={"completed": completed},
                        merge_refs=True,
                    )
                if pending.refs["was_running"] and deployment is not None:
                    result = self.helper(
                        self.config,
                        "app.deploy",
                        {"slug": application.slug, "job": deployment.nomad_job},
                        deadline=deadline,
                    )
                    version = result.get("nomadVersion")
                    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
                        raise ValidationError("migration restart version is invalid")
                    identity = app.nomad_candidate_identity(deployment.nomad_job)
                    while True:
                        healthy = self.helper(
                            self.config,
                            "app.health",
                            {
                                "slug": application.slug,
                                "jobId": app.nomad_job_id(deployment.nomad_job, application.slug),
                                "version": version,
                                "candidateJobSha256": identity[0],
                                "candidateImage": identity[1],
                            },
                            deadline=deadline,
                        )
                        if healthy.get("healthy") is True:
                            break
                        if healthy.get("terminal") is True or time.monotonic() >= deadline:
                            raise ValidationError("migrated application health was not confirmed")
                        time.sleep(0.5)
                    db.set_application_runtime(
                        self.connection,
                        identifier,
                        running=True,
                        worker_server_id=application.worker_server_id,
                        worker_server_name=application.worker_server_name,
                        worker_port_id=application.worker_port_id,
                        worker_port_name=application.worker_port_name,
                        nomad_version=version,
                    )
                db.mark_succeeded(self.connection, key, cleanup_state="not_required")
            except Exception:
                db.mark_recovery_required(
                    self.connection,
                    key,
                    "application migration requires reconciliation; old shared data remains intact",
                )
                raise
