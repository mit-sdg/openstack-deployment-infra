"""Resumable per-app migration admission, quiescence and binding refresh."""

from __future__ import annotations

import sqlite3
import time
import uuid as uuid_module
from pathlib import Path
from typing import Any

from .. import runtime
from ..config import Config
from ..instance_contract import GIB, CapacityError, hard_quota, validate_limits
from ..validation import ValidationError, uuid
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

    def migrate(self, *, request_id: str, application_ids: tuple[str, ...] | None = None) -> None:
        selection = (
            None
            if application_ids is None
            else sorted({uuid(value, field="application ID") for value in application_ids})
        )
        if selection is not None and (
            not selection
            or any(db.get_application(self.connection, value) is None for value in selection)
        ):
            raise ValidationError("migration application selection is empty or unknown")
        deadline = operation_deadline(self.config)
        with runtime.lock(self.directory, "infrastructure", wait=True, deadline=deadline):
            operation = db.get_unfinished_operation(self.connection, "infrastructure")
            if operation is not None:
                if operation.operation_id != request_id or operation.kind != MIGRATE_KIND:
                    raise db.UnfinishedOperationError(
                        "infrastructure", operation.operation_id, operation.kind
                    )
                if operation.refs["selection"] != selection:
                    raise ValidationError("migration application selection changed during replay")
                operation = db.renew_operation_deadline(
                    self.connection, request_id, wall_deadline(deadline)
                )
            else:
                applications = sorted(
                    {
                        item.application_id
                        for item in db.list_managed_resources(self.connection)
                        if item.resource_type in {"postgres", "mongo"}
                        and item.instance_id is None
                        and (selection is None or item.application_id in selection)
                    }
                )
                operation = db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind=MIGRATE_KIND,
                    scope="infrastructure",
                    phase="migrating",
                    deadline_at=wall_deadline(deadline),
                    refs={"applications": applications, "completed": [], "selection": selection},
                )
            completed = list(operation.refs["completed"])
            try:
                for identifier in operation.refs["applications"]:
                    if identifier in completed:
                        continue
                    app_deadline = (
                        time.monotonic() + self.config.policy.limits.migration_app_seconds
                    )
                    db.renew_operation_deadline(
                        self.connection, request_id, wall_deadline(app_deadline)
                    )
                    self.application(identifier, request_id, app_deadline)
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
                source_roles = {}
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
                    source_roles[resource_id] = (
                        "platform_size_blocked"
                        if probe.get("writeBlocked") is True
                        else "readWrite"
                    )
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
                    refs={
                        "resources": selected,
                        "completed": [],
                        "was_running": running,
                        "source_roles": source_roles,
                    },
                )
            else:
                pending = db.renew_operation_deadline(self.connection, key, wall_deadline(deadline))

            completed = list(pending.refs["completed"])
            try:
                if pending.phase in {"validated", "quiesced"}:
                    backups = {}
                    for resource_id in pending.refs["resources"]:
                        if resource_id in completed:
                            continue
                        resource = db.get_managed_resource(self.connection, resource_id)
                        assert resource is not None
                        result = self.helper(
                            self.config,
                            "storage.backup.ensure",
                            {
                                "type": resource.resource_type,
                                "database": resource.provider_name,
                                "maxAgeMinutes": self.config.policy.limits.migration_backup_max_age_minutes,
                            },
                            deadline=time.monotonic() + 3600,
                        )
                        from datetime import UTC, datetime

                        measured = result.get("backedUpAt")
                        try:
                            age = (
                                datetime.now(UTC) - datetime.fromisoformat(str(measured))
                            ).total_seconds()
                        except (ValueError, TypeError):
                            age = -1
                        if (
                            result.get("verified") is not True
                            or result.get("shared") is not True
                            or result.get("database") != resource.provider_name
                            or result.get("type") != resource.resource_type
                            or not 0
                            <= age
                            <= self.config.policy.limits.migration_backup_max_age_minutes * 60
                        ):
                            raise ValidationError(
                                "fresh shared-resource backup was not confirmed; cutover refused"
                            )
                        backups[resource_id] = dict(result)
                    # The app remains serving during a resource-scoped backup.
                    # Its downtime/copy budget starts only after the gate passes.
                    deadline = time.monotonic() + self.config.policy.limits.migration_app_seconds
                    db.renew_operation_deadline(self.connection, key, wall_deadline(deadline))
                    db.renew_operation_deadline(self.connection, master_id, wall_deadline(deadline))
                    db.checkpoint_operation(
                        self.connection,
                        key,
                        phase=pending.phase,
                        refs={"backups": backups},
                        merge_refs=True,
                    )
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
                            "copySeconds": max(120, min(7200, int(deadline - time.monotonic()))),
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
                if deployment is not None:
                    import hashlib

                    from .nomad_jobs import storage_hosts_job

                    job = storage_hosts_job(deployment.nomad_job, self.config.platform)
                    if job != deployment.nomad_job:
                        db.checkpoint_deployment_attempt(
                            self.connection,
                            deployment.deployment_id,
                            status="succeeded",
                            nomad_job=job,
                            nomad_job_sha256=hashlib.sha256(job.encode()).hexdigest(),
                        )
                        deployment = db.get_deployment(self.connection, identifier)
                        assert deployment is not None
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

    def abort(self, *, request_id: str, application_ids: tuple[str, ...] | None = None) -> None:
        deadline = time.monotonic() + self.config.policy.limits.migration_app_seconds
        with runtime.lock(self.directory, "infrastructure", wait=True, deadline=deadline):
            own = db.get_operation(self.connection, request_id)
            if own is not None and own.status == "succeeded":
                return
            master = db.get_unfinished_operation(self.connection, "infrastructure")
            if own is not None:
                master = db.get_operation(self.connection, own.refs["master"])
            if master is None or master.kind != MIGRATE_KIND:
                raise ValidationError("no interrupted instance migration exists")
            selected = sorted(set(application_ids or master.refs["applications"]))
            if not set(selected) <= set(master.refs["applications"]):
                raise ValidationError("abort selection is outside the original migration")
            if own is None:
                own = db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind="storage.instances.abort",
                    scope="storage-abort",
                    phase="unfreezing",
                    deadline_at=wall_deadline(deadline),
                    refs={"master": master.operation_id, "applications": selected, "completed": []},
                )
            elif own.refs["applications"] != selected:
                raise ValidationError("abort selection changed during replay")

            def parameters(
                resource: db.ManagedResource, child: db.Operation, mode: str
            ) -> dict[str, Any]:
                application = db.get_application(self.connection, resource.application_id)
                assert application is not None
                return dict(
                    applicationId=resource.application_id,
                    applicationSlug=application.slug,
                    resourceName=resource.resource_name,
                    type=resource.resource_type,
                    providerId=resource.provider_id,
                    providerName=resource.provider_name,
                    instanceId=resource.resource_id,
                    operationId=child.operation_id,
                    sourceRole=child.refs["source_roles"][resource.resource_id],
                    mode=mode,
                )

            try:
                # Check every selected endpoint before unfreezing any source.
                for identifier in selected:
                    if identifier in own.refs["completed"]:
                        continue
                    child_id = str(
                        uuid_module.uuid5(uuid_module.UUID(master.operation_id), identifier)
                    )
                    child = db.get_operation(self.connection, child_id)
                    if child is None:
                        continue
                    for resource_id in child.refs["resources"]:
                        resource = db.get_managed_resource(self.connection, resource_id)
                        assert resource is not None
                        if resource.instance_id is not None:
                            raise CapacityError(
                                "MIGRATION_ALREADY_PUBLISHED",
                                "endpoint is published; replay the migration instead",
                            )
                        confirmed = self.helper(
                            self.config,
                            "storage.instances.abort",
                            parameters(resource, child, "check"),
                            deadline=deadline,
                        )
                        if confirmed.get("unpublished") is not True:
                            raise ValidationError("unpublished source was not confirmed")
                completed = list(own.refs["completed"])
                for identifier in selected:
                    if identifier in completed:
                        continue
                    with runtime.lock(
                        self.directory, f"app-{identifier}", wait=True, deadline=deadline
                    ):
                        child_id = str(
                            uuid_module.uuid5(uuid_module.UUID(master.operation_id), identifier)
                        )
                        child = db.get_operation(self.connection, child_id)
                        if child is not None:
                            for resource_id in child.refs["resources"]:
                                resource = db.get_managed_resource(self.connection, resource_id)
                                assert resource is not None
                                result = self.helper(
                                    self.config,
                                    "storage.instances.abort",
                                    parameters(resource, child, "apply"),
                                    deadline=deadline,
                                )
                                if result.get("unfrozen") is not True:
                                    raise ValidationError("source write access was not restored")
                            deployment = db.get_deployment(self.connection, identifier)
                            application = db.get_application(self.connection, identifier)
                            if (
                                child.refs["was_running"]
                                and deployment is not None
                                and application is not None
                            ):
                                result = self.helper(
                                    self.config,
                                    "app.deploy",
                                    {"slug": application.slug, "job": deployment.nomad_job},
                                    deadline=deadline,
                                )
                                version = result.get("nomadVersion")
                                if not isinstance(version, int) or isinstance(version, bool):
                                    raise ValidationError(
                                        "shared application restart was not confirmed"
                                    )
                                db.checkpoint_deployment_attempt(
                                    self.connection,
                                    deployment.deployment_id,
                                    status="succeeded",
                                    nomad_version=version,
                                )
                            db.mark_failed(
                                self.connection,
                                child_id,
                                "migration aborted before publication",
                                cleanup_state="not_required",
                            )
                            with db.transaction(self.connection):
                                self.connection.execute(
                                    "UPDATE managed_resources SET migration_state='aborted' WHERE application_id=? AND instance_id IS NULL",
                                    (identifier,),
                                )
                        completed.append(identifier)
                        db.checkpoint_operation(
                            self.connection,
                            request_id,
                            phase="unfreezing",
                            refs={"completed": completed},
                            merge_refs=True,
                        )
                skipped = sorted(set(master.refs["completed"]) | set(completed))
                db.checkpoint_operation(
                    self.connection,
                    master.operation_id,
                    phase="migrating",
                    refs={"completed": skipped},
                    merge_refs=True,
                )
                if set(skipped) >= set(master.refs["applications"]):
                    db.mark_failed(
                        self.connection,
                        master.operation_id,
                        "migration aborted; source data retained",
                        cleanup_state="not_required",
                    )
                    if db.get_operation_dispatch(self.connection, master.operation_id) is not None:
                        db.set_operation_dispatch_status(
                            self.connection, master.operation_id, "finished"
                        )
                db.mark_succeeded(self.connection, request_id, cleanup_state="not_required")
            except Exception as error:
                if isinstance(error, CapacityError) and error.code == "MIGRATION_ALREADY_PUBLISHED":
                    db.mark_failed(self.connection, request_id, error, cleanup_state="not_required")
                else:
                    db.mark_recovery_required(
                        self.connection,
                        request_id,
                        "abort requires replay; source and target data retained",
                    )
                raise
