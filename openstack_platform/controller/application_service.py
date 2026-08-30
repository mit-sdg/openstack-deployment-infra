"""Application declaration and destructive lifecycle orchestration."""

from __future__ import annotations

import sqlite3
import time
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import openstack, runtime
from ..config import Config
from ..validation import ValidationError, bounded_text, slug, uuid
from . import application_runtime as app
from . import database as db
from .service_support import (
    HelperCaller,
    operation_deadline,
    remaining_seconds,
    wall_deadline,
)
from .storage_contract import storage_owner

_RESERVED_APPLICATION_SLUGS = {"admin", "api", "auth", "status", "www"}


@dataclass(frozen=True, slots=True)
class ApplicationCreated:
    application_id: str
    slug: str
    url: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ApplicationRemoved:
    application_id: str
    slug: str


@dataclass(frozen=True, slots=True)
class ApplicationLifecycleChanged:
    application_id: str
    slug: str
    state: Literal["enabled", "disabled"]


class ApplicationService:
    """Own application declaration and destructive lifecycle orchestration."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
        *,
        helper_caller: HelperCaller,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory
        self.helper_caller = helper_caller

    def declare(
        self, application_slug: str, *, application_id: str | None = None
    ) -> ApplicationCreated:
        """Create one inert disabled application before storage or deployment."""
        checked_slug = slug(application_slug)
        if checked_slug in _RESERVED_APPLICATION_SLUGS:
            raise ValidationError("application slug is reserved by the platform")
        if db.get_application(self.connection, checked_slug) is not None:
            raise ValidationError("application already exists")
        if db.get_slug_tombstone(self.connection, checked_slug) is not None:
            raise ValidationError("application slug is permanently reserved")
        identifier = (
            str(uuid_module.uuid4())
            if application_id is None
            else uuid(application_id, field="application ID")
        )
        standard = self.config.policy.standard
        url = f"https://{checked_slug}.{self.config.platform.domain}"
        db.put_application(
            self.connection,
            application_id=identifier,
            application_slug=checked_slug,
            worker_flavor=standard.worker_flavor,
            scheduler_cpu_mhz=standard.cpu_mhz,
            scheduler_memory_mib=standard.memory_mib,
            desired_running=False,
            url=url,
        )
        return ApplicationCreated(identifier, checked_slug, url, False)

    def _worker_identity(self, result: Mapping[str, object]) -> tuple[str, str, str, str]:
        if result.get("ready") is not True or result.get("absent") is True:
            raise app.ApplicationError("worker readiness was not confirmed")
        return (
            uuid(result.get("serverId"), field="worker server UUID"),
            bounded_text(result.get("serverName"), field="worker server name", maximum=128),
            uuid(result.get("portId"), field="worker port UUID"),
            bounded_text(result.get("portName"), field="worker port name", maximum=128),
        )

    def _operation(
        self,
        application: db.Application,
        kind: str,
        refs: Mapping[str, object],
        deadline: float,
        *,
        operation_id: str | None = None,
    ) -> tuple[db.Operation, bool]:
        scope = f"app-{application.application_id}"
        unfinished = db.get_unfinished_operation(self.connection, scope)
        if unfinished is not None:
            if unfinished.kind != kind or any(
                unfinished.refs.get(key) != value for key, value in refs.items()
            ):
                raise db.UnfinishedOperationError(scope, unfinished.operation_id, unfinished.kind)
            operation = db.renew_operation_deadline(
                self.connection, unfinished.operation_id, wall_deadline(deadline)
            )
            return operation, True
        return (
            db.begin_operation(
                self.connection,
                operation_id=(
                    str(uuid_module.uuid4())
                    if operation_id is None
                    else uuid(operation_id, field="operation ID")
                ),
                kind=kind,
                scope=scope,
                phase="validated",
                deadline_at=wall_deadline(deadline),
                refs=refs,
            ),
            False,
        )

    def disable(
        self, application_identifier: str, *, request_id: str | None = None
    ) -> ApplicationLifecycleChanged:
        """Stop only the accepted job and its exact worker slot, preserving data."""
        application = db.get_application(self.connection, application_identifier)
        if application is None:
            raise ValidationError("application does not exist")
        deadline = operation_deadline(self.config)
        scope = f"app-{application.application_id}"
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=remaining_seconds(
                    deadline, self.config.policy.limits.process_seconds
                ),
            )
            current = db.get_application(self.connection, application.application_id)
            if current is None:
                raise ValidationError("application does not exist")
            deployment = db.get_deployment(self.connection, current.application_id)
            unfinished = db.get_unfinished_operation(self.connection, scope)
            if not current.desired_running and unfinished is None:
                return ApplicationLifecycleChanged(current.application_id, current.slug, "disabled")
            if deployment is None:
                if unfinished is not None:
                    raise db.UnfinishedOperationError(
                        scope, unfinished.operation_id, unfinished.kind
                    )
                db.set_application_runtime(self.connection, current.application_id, running=False)
                return ApplicationLifecycleChanged(current.application_id, current.slug, "disabled")
            placement_id = app.nomad_placement_id(deployment.nomad_job)
            refs: dict[str, object] = {
                "application_id": current.application_id,
                "slug": current.slug,
                "job_id": app.nomad_job_id(deployment.nomad_job, current.slug),
                "job_sha256": deployment.nomad_job_sha256,
                "image": deployment.image_digest,
                "worker_application_id": placement_id,
                "worker_server_id": current.worker_server_id,
                "worker_port_id": current.worker_port_id,
            }
            operation, _resuming = self._operation(
                current, "app.disable", refs, deadline, operation_id=request_id
            )
            try:
                removed = self.helper_caller(
                    self.config,
                    "app.remove",
                    {
                        "slug": current.slug,
                        "jobId": refs["job_id"],
                        "candidateJobSha256": refs["job_sha256"],
                        "candidateImage": refs["image"],
                    },
                    deadline=deadline,
                )
                if removed.get("jobAbsent") is not True:
                    raise app.ApplicationError("exact accepted job absence was not confirmed")
                db.checkpoint_operation(
                    self.connection, operation.operation_id, phase="job_absent", refs=refs
                )
                observed = self.helper_caller(
                    self.config,
                    "app.worker.observe",
                    {"applicationId": placement_id, "slug": current.slug},
                    deadline=deadline,
                )
                if observed.get("absent") is not True:
                    identity = self._worker_identity(observed)
                    if (
                        identity[0] != refs["worker_server_id"]
                        or identity[2] != refs["worker_port_id"]
                    ):
                        raise app.ApplicationError("active worker identity did not match SQLite")
                worker = self.helper_caller(
                    self.config,
                    "app.worker.delete",
                    {"applicationId": placement_id, "slug": current.slug, "single": True},
                    deadline=deadline,
                )
                if worker.get("absent") is not True:
                    raise app.ApplicationError(
                        "exact worker or fixed-port absence was not confirmed"
                    )
                db.set_application_runtime(self.connection, current.application_id, running=False)
                db.checkpoint_operation(
                    self.connection, operation.operation_id, phase="worker_absent", refs=refs
                )
                db.mark_succeeded(self.connection, operation.operation_id)
            except Exception as error:
                latest = db.get_operation(self.connection, operation.operation_id)
                if latest is not None and latest.status == "running":
                    db.mark_recovery_required(self.connection, operation.operation_id, error)
                raise
        return ApplicationLifecycleChanged(current.application_id, current.slug, "disabled")

    def enable(
        self, application_identifier: str, *, request_id: str | None = None
    ) -> ApplicationLifecycleChanged:
        """Recreate runtime from the accepted immutable job without a source build."""
        application = db.get_application(self.connection, application_identifier)
        if application is None:
            raise ValidationError("application does not exist")
        deadline = operation_deadline(self.config)
        scope = f"app-{application.application_id}"
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=remaining_seconds(
                    deadline, self.config.policy.limits.process_seconds
                ),
            )
            current = db.get_application(self.connection, application.application_id)
            if current is None:
                raise ValidationError("application does not exist")
            deployment = db.get_deployment(self.connection, current.application_id)
            if deployment is None:
                raise ValidationError("enable requires an accepted deployment")
            unfinished = db.get_unfinished_operation(self.connection, scope)
            if current.desired_running and unfinished is None:
                return ApplicationLifecycleChanged(current.application_id, current.slug, "enabled")
            marker = app.nomad_route_marker(deployment.nomad_job)
            placement_id = app.nomad_placement_id(deployment.nomad_job)
            current_resources = {
                item.resource_id: item
                for item in db.list_managed_resources(
                    self.connection, application_id=current.application_id
                )
            }
            for resource_id in db.active_storage_resource_ids(
                self.connection, current.application_id
            ):
                resource = current_resources.get(resource_id)
                if resource is None or resource.lifecycle_state != "active":
                    raise ValidationError(
                        "accepted deployment references missing or inactive storage"
                    )
            if unfinished is None:
                with runtime.lock(self.state_directory, "infrastructure", deadline=deadline):
                    worker_image = db.get_image_selection(self.connection, "worker")
                if worker_image is None:
                    raise ValidationError("select a worker image before enable")
                image_id = worker_image.image_id
            else:
                image_id = uuid(unfinished.refs.get("worker_image_id"), field="worker image UUID")
            refs: dict[str, object] = {
                "application_id": current.application_id,
                "slug": current.slug,
                "deployment_id": db.get_active_deployment(
                    self.connection, current.application_id
                ).deployment_id,  # type: ignore[union-attr]
                "worker_application_id": placement_id,
                "worker_image_id": image_id,
                "job_id": app.nomad_job_id(deployment.nomad_job, current.slug),
                "job_sha256": deployment.nomad_job_sha256,
                "image": deployment.image_digest,
                "route_marker": marker,
            }
            operation, _resuming = self._operation(
                current, "app.enable", refs, deadline, operation_id=request_id
            )
            refs = dict(operation.refs)
            try:
                worker = self.helper_caller(
                    self.config,
                    "app.worker.observe",
                    {"applicationId": placement_id, "slug": current.slug},
                    deadline=deadline,
                )
                if worker.get("absent") is True:
                    flavor = openstack.observe_flavor(
                        self.config.platform,
                        current.worker_flavor,
                        require_one_vcpu=True,
                        timeout_seconds=remaining_seconds(
                            deadline, self.config.policy.limits.process_seconds
                        ),
                    )
                    if flavor != current.worker_flavor:
                        raise app.ApplicationError("configured worker flavor identity drifted")
                    worker = self.helper_caller(
                        self.config,
                        "app.worker.create",
                        {
                            "applicationId": placement_id,
                            "slug": current.slug,
                            "workerImageId": image_id,
                            "standardFlavor": current.worker_flavor,
                        },
                        deadline=deadline,
                    )
                server_id, server_name, port_id, port_name = self._worker_identity(worker)
                observed_identity = {
                    "worker_server_id": server_id,
                    "worker_server_name": server_name,
                    "worker_port_id": port_id,
                    "worker_port_name": port_name,
                }
                if any(
                    key in refs and refs[key] != value for key, value in observed_identity.items()
                ):
                    raise app.ApplicationError("enabling worker identity drifted")
                refs.update(observed_identity)
                db.checkpoint_operation(
                    self.connection, operation.operation_id, phase="worker_ready", refs=refs
                )
                result = app.deploy_and_cleanup(
                    current.slug,
                    deployment.nomad_job,
                    attempts=max(
                        1,
                        min(
                            300,
                            int(
                                remaining_seconds(
                                    deadline, self.config.policy.limits.process_seconds
                                )
                            )
                            // self.config.policy.limits.poll_interval_seconds,
                        ),
                    ),
                    poll_interval_seconds=self.config.policy.limits.poll_interval_seconds,
                    helper_timeout_seconds=self.config.policy.limits.helper_seconds,
                    helper_caller=lambda action, values, **_bounds: self.helper_caller(
                        self.config, action, values, deadline=deadline
                    ),
                    public_health_check=lambda: app.check_public_health(
                        current.slug,
                        self.config.platform,
                        deployment.health_path,
                        timeout_seconds=remaining_seconds(
                            deadline, self.config.policy.limits.http_seconds
                        ),
                        expected_marker=marker,
                    ),
                    sleep=lambda seconds: time.sleep(remaining_seconds(deadline, seconds)),
                )
                db.checkpoint_operation(
                    self.connection,
                    operation.operation_id,
                    phase="deployment_healthy",
                    refs={**refs, "nomad_version": result.nomad_version},
                )
                db.set_application_runtime(
                    self.connection,
                    current.application_id,
                    running=True,
                    worker_server_id=server_id,
                    worker_server_name=server_name,
                    worker_port_id=port_id,
                    worker_port_name=port_name,
                    nomad_version=result.nomad_version,
                )
                db.mark_succeeded(self.connection, operation.operation_id)
            except Exception as error:
                latest = db.get_operation(self.connection, operation.operation_id)
                if latest is not None and latest.status == "running":
                    db.mark_recovery_required(self.connection, operation.operation_id, error)
                raise
        return ApplicationLifecycleChanged(current.application_id, current.slug, "enabled")

    def delete(
        self,
        application_identifier: str,
        *,
        confirmation: str,
        request_id: str | None = None,
    ) -> ApplicationRemoved:
        """Resumably purge all app-owned provider/runtime resources and tombstone it."""
        application = db.get_application(self.connection, application_identifier)
        if application is None:
            raise ValidationError("application does not exist")
        if confirmation != application.slug:
            raise ValidationError("application deletion requires the exact slug")
        deadline = operation_deadline(self.config)
        scope = f"app-{application.application_id}"
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=remaining_seconds(
                    deadline, self.config.policy.limits.process_seconds
                ),
            )
            current = db.get_application(self.connection, application.application_id)
            if current is None:
                raise ValidationError("application does not exist")
            refs: dict[str, object] = {
                "application_id": current.application_id,
                "slug": current.slug,
                "confirmed_slug": confirmation,
            }
            operation, resuming = self._operation(
                current, "app.delete", refs, deadline, operation_id=request_id
            )
            try:
                deployment = db.get_deployment(self.connection, current.application_id)
                if deployment is not None:
                    job_id = app.nomad_job_id(deployment.nomad_job, current.slug)
                    db.checkpoint_operation(
                        self.connection,
                        operation.operation_id,
                        phase="workload_stopping",
                        refs=refs,
                    )
                    stopped = self.helper_caller(
                        self.config,
                        "app.remove",
                        {
                            "slug": current.slug,
                            "jobId": job_id,
                            "candidateJobSha256": deployment.nomad_job_sha256,
                            "candidateImage": deployment.image_digest,
                        },
                        deadline=deadline,
                    )
                    if stopped.get("jobAbsent") is not True:
                        raise app.ApplicationError("accepted workload withdrawal was not confirmed")
                db.checkpoint_operation(
                    self.connection,
                    operation.operation_id,
                    phase="workload_absent",
                    refs=refs,
                )
                while resources := db.list_managed_resources(
                    self.connection, application_id=current.application_id
                ):
                    resource = resources[0]
                    common: dict[str, object] = {
                        "applicationId": current.application_id,
                        "applicationSlug": current.slug,
                        "resourceName": resource.resource_name,
                        "providerId": resource.provider_id,
                        "providerName": resource.provider_name,
                        "confirmName": resource.resource_name,
                        "operationId": operation.operation_id,
                        "recover": resuming or resource.lifecycle_state != "active",
                    }
                    if resource.resource_type == "s3":
                        common["purge"] = True
                    preflight = self.helper_caller(
                        self.config,
                        f"storage.{resource.resource_type}.remove",
                        {**common, "preflight": True},
                        deadline=deadline,
                    )
                    if preflight.get("preflightAccepted") is not True:
                        raise app.ApplicationError("storage deletion preflight was not confirmed")
                    resource_refs = {
                        **refs,
                        "resource_id": resource.resource_id,
                        "resource_type": resource.resource_type,
                        "resource_name": resource.resource_name,
                    }
                    db.checkpoint_operation(
                        self.connection,
                        operation.operation_id,
                        phase="storage_removing",
                        refs=resource_refs,
                    )
                    db.set_managed_resource_lifecycle(
                        self.connection, resource.resource_id, "removing"
                    )
                    try:
                        removed = self.helper_caller(
                            self.config,
                            f"storage.{resource.resource_type}.remove",
                            {**common, "preflight": False},
                            deadline=deadline,
                        )
                    except Exception:
                        db.set_managed_resource_lifecycle(
                            self.connection, resource.resource_id, "recovery_required"
                        )
                        raise
                    if (
                        removed.get("confirmedAbsent") is not True
                        or removed.get("environmentRemoved") is not True
                    ):
                        raise app.ApplicationError("storage absence evidence was incomplete")
                    db.set_environment_keys(
                        self.connection,
                        application_id=current.application_id,
                        owner=storage_owner(resource.resource_type, resource.resource_name),
                        keys=(),
                    )
                    db.delete_managed_resource(
                        self.connection,
                        application_id=current.application_id,
                        resource_type=resource.resource_type,
                        resource_name=resource.resource_name,
                    )
                    db.checkpoint_operation(
                        self.connection,
                        operation.operation_id,
                        phase="storage_removed",
                        refs=resource_refs,
                    )
                    resuming = False
                db.checkpoint_operation(
                    self.connection, operation.operation_id, phase="storage_absent", refs=refs
                )
                db.checkpoint_operation(
                    self.connection,
                    operation.operation_id,
                    phase="runtime_removing",
                    refs=refs,
                )
                removed = self.helper_caller(
                    self.config, "app.remove", {"slug": current.slug}, deadline=deadline
                )
                if (
                    removed.get("jobAbsent") is not True
                    or removed.get("variableAbsent") is not True
                ):
                    raise app.ApplicationError("job or Variable absence was not confirmed")
                db.checkpoint_operation(
                    self.connection, operation.operation_id, phase="variable_absent", refs=refs
                )
                db.checkpoint_operation(
                    self.connection,
                    operation.operation_id,
                    phase="worker_removing",
                    refs=refs,
                )
                worker = self.helper_caller(
                    self.config,
                    "app.worker.delete",
                    {"applicationId": current.application_id, "slug": current.slug},
                    deadline=deadline,
                )
                if worker.get("absent") is not True:
                    raise app.ApplicationError("bounded worker-slot absence was not confirmed")
                db.set_application_runtime(self.connection, current.application_id, running=False)
                db.checkpoint_operation(
                    self.connection, operation.operation_id, phase="worker_absent", refs=refs
                )
                for image in db.list_application_manifest_images(
                    self.connection, current.application_id
                ):
                    db.checkpoint_operation(
                        self.connection,
                        operation.operation_id,
                        phase="manifest_removing",
                        refs={**refs, "image": image},
                    )
                    manifest = self.helper_caller(
                        self.config,
                        "app.manifest.delete",
                        {"slug": current.slug, "image": image, "references": []},
                        deadline=deadline,
                    )
                    if manifest.get("absent") is not True:
                        raise app.ApplicationError("registry manifest absence was not confirmed")
                db.checkpoint_operation(
                    self.connection, operation.operation_id, phase="manifest_absent", refs=refs
                )
                db.complete_application_deletion(
                    self.connection,
                    application_id=current.application_id,
                    operation_id=operation.operation_id,
                )
            except Exception as error:
                latest = db.get_operation(self.connection, operation.operation_id)
                if latest is not None and latest.status == "running":
                    db.mark_recovery_required(self.connection, operation.operation_id, error)
                raise
        return ApplicationRemoved(current.application_id, current.slug)
