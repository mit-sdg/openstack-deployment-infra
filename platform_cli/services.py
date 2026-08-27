"""Typed product-service entry points independent of CLI parsing and rendering.

This module is the extraction boundary shared temporarily by the staff CLI and,
later, the local controller API. Requests contain validated product data rather
than ``argparse`` namespaces, and results contain typed identities rather than
rendered output.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
import uuid as uuid_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from . import app, db, openstack, remote, runtime, status, storage
from .config import Config
from .storage_contract import (
    PLATFORM_ENVIRONMENT_KEYS,
    RESERVED_ENVIRONMENT_PREFIX,
    storage_owner,
)
from .validation import ValidationError, bounded_text, env_key, resource_name, slug, uuid

StorageAction = Literal["create", "verify", "rotate", "remove"]
EnvironmentAction = Literal["set", "unset", "import"]
HelperCaller = Callable[..., Mapping[str, object]]


class ServiceDeadlineError(RuntimeError):
    """A product service exhausted its whole-operation deadline."""


@dataclass(frozen=True, slots=True)
class ApplicationCreated:
    application_id: str
    slug: str
    url: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class StorageMutationRequest:
    action: StorageAction
    application: str
    resource_types: tuple[str, ...]
    resource_name: str = "default"
    confirm_name: str | None = None
    purge_s3: bool = False


@dataclass(frozen=True, slots=True)
class StorageMutationResult:
    requested: tuple[str, ...]
    completed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EnvironmentMutationRequest:
    action: EnvironmentAction
    application: str
    updates: Mapping[str, str] = field(default_factory=dict, repr=False)
    removals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnvironmentMutationResult:
    key_names: tuple[str, ...]
    modify_index: int | None


@dataclass(frozen=True, slots=True)
class BuildAttempt:
    build_id: str
    status: str
    phase: str
    started_at: str
    source_commit: str | None


@dataclass(frozen=True, slots=True)
class LogChunk:
    text: str
    state: str
    next_offset: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ApplicationRemoved:
    application_id: str
    slug: str


@dataclass(frozen=True, slots=True)
class ApplicationLifecycleChanged:
    application_id: str
    slug: str
    state: Literal["enabled", "disabled"]


def _deadline(config: Config) -> float:
    return time.monotonic() + config.policy.limits.process_seconds


def _remaining(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ServiceDeadlineError("operation exceeded its whole-operation deadline")
    return min(float(maximum), remaining)


def _call_helper(
    config: Config,
    action: str,
    values: Mapping[str, object],
    *,
    deadline: float,
) -> Mapping[str, object]:
    return remote.call_helper(
        action,
        values,
        timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
        request_limit=config.policy.limits.helper_request_bytes,
        response_limit=config.policy.limits.helper_response_bytes,
        stderr_limit=config.policy.limits.stderr_bytes,
        helper_command=remote.helper_command_path(config.platform.get("paths.root")),
    )


def _wall_deadline(deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ServiceDeadlineError("operation exceeded its whole-operation deadline")
    return (
        (datetime.now(UTC) + timedelta(seconds=remaining))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _reject_deleting(connection: sqlite3.Connection, application: db.Application) -> None:
    operation = db.get_unfinished_operation(
        connection, f"app-{application.application_id}"
    )
    if operation is not None and operation.kind in {"app.delete", "app.remove"}:
        raise db.UnfinishedOperationError(
            operation.scope, operation.operation_id, operation.kind
        )


def _tail_file(path: Path, *, lines: int, maximum_bytes: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise ValidationError("accepted build log is not a bounded direct file")
        position = metadata.st_size
        chunks: list[bytes] = []
        newlines = 0
        while position and newlines <= lines:
            amount = min(65_536, position)
            position -= amount
            os.lseek(descriptor, position, os.SEEK_SET)
            chunk = os.read(descriptor, amount)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
        return "\n".join(
            b"".join(reversed(chunks))
            .decode("utf-8", errors="replace")
            .splitlines()[-lines:]
        )
    finally:
        os.close(descriptor)


class ProductReadService:
    """Return safe product models without table or process rendering."""

    def __init__(self, connection: sqlite3.Connection, config: Config) -> None:
        self.connection = connection
        self.config = config

    def applications(self) -> tuple[Mapping[str, object], ...]:
        observe = status.application_observer(self.connection, self.config)
        return tuple(status.app_list(self.connection, observe=observe))

    def application(self, application_identifier: str) -> Mapping[str, object]:
        observe = status.application_observer(self.connection, self.config)
        result = status.app_show(
            self.connection,
            application_identifier,
            observe=observe,
        )
        if result is None:
            raise ValidationError("application does not exist")
        return result

    def storage(
        self,
        application_identifier: str | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        observe = status.storage_observer(self.connection, self.config)
        return tuple(
            status.storage_list(
                self.connection,
                application_identifier=application_identifier,
                observe=observe,
            )
        )

    def storage_resource(
        self,
        application_identifier: str,
        resource_type: str,
        resource_name_value: str,
    ) -> Mapping[str, object]:
        observe = status.storage_observer(self.connection, self.config)
        result = status.storage_show(
            self.connection,
            application_identifier,
            resource_type,
            resource_name_value,
            observe=observe,
        )
        if result is None:
            raise ValidationError("managed storage does not exist")
        return result


class ApplicationService:
    """Own application declaration and destructive lifecycle orchestration."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
        *,
        helper_caller: HelperCaller = _call_helper,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory
        self.helper_caller = helper_caller

    def declare(self, application_slug: str) -> ApplicationCreated:
        """Create one inert disabled application before storage or deployment."""
        checked_slug = slug(application_slug)
        if db.get_application(self.connection, checked_slug) is not None:
            raise ValidationError("application already exists")
        if db.get_slug_tombstone(self.connection, checked_slug) is not None:
            raise ValidationError("application slug is permanently reserved")
        identifier = str(uuid_module.uuid4())
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

    def _worker_identity(
        self, result: Mapping[str, object]
    ) -> tuple[str, str, str, str]:
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
    ) -> tuple[db.Operation, bool]:
        scope = f"app-{application.application_id}"
        unfinished = db.get_unfinished_operation(self.connection, scope)
        if unfinished is not None:
            if unfinished.kind != kind or any(
                unfinished.refs.get(key) != value for key, value in refs.items()
            ):
                raise db.UnfinishedOperationError(
                    scope, unfinished.operation_id, unfinished.kind
                )
            operation = db.renew_operation_deadline(
                self.connection, unfinished.operation_id, _wall_deadline(deadline)
            )
            return operation, True
        return (
            db.begin_operation(
                self.connection,
                operation_id=str(uuid_module.uuid4()),
                kind=kind,
                scope=scope,
                phase="validated",
                deadline_at=_wall_deadline(deadline),
                refs=refs,
            ),
            False,
        )

    def disable(self, application_identifier: str) -> ApplicationLifecycleChanged:
        """Stop only the accepted job and its exact worker slot, preserving data."""
        application = db.get_application(self.connection, application_identifier)
        if application is None:
            raise ValidationError("application does not exist")
        deadline = _deadline(self.config)
        scope = f"app-{application.application_id}"
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=_remaining(deadline, self.config.policy.limits.process_seconds),
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
            operation, _resuming = self._operation(current, "app.disable", refs, deadline)
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
                    if identity[0] != refs["worker_server_id"] or identity[2] != refs["worker_port_id"]:
                        raise app.ApplicationError("active worker identity did not match SQLite")
                worker = self.helper_caller(
                    self.config,
                    "app.worker.delete",
                    {"applicationId": placement_id, "slug": current.slug, "single": True},
                    deadline=deadline,
                )
                if worker.get("absent") is not True:
                    raise app.ApplicationError("exact worker or fixed-port absence was not confirmed")
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

    def enable(self, application_identifier: str) -> ApplicationLifecycleChanged:
        """Recreate runtime from the accepted immutable job without a source build."""
        application = db.get_application(self.connection, application_identifier)
        if application is None:
            raise ValidationError("application does not exist")
        deadline = _deadline(self.config)
        scope = f"app-{application.application_id}"
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=_remaining(deadline, self.config.policy.limits.process_seconds),
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
                image_id = uuid(
                    unfinished.refs.get("worker_image_id"), field="worker image UUID"
                )
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
            operation, _resuming = self._operation(current, "app.enable", refs, deadline)
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
                        timeout_seconds=_remaining(
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
                    key in refs and refs[key] != value
                    for key, value in observed_identity.items()
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
                            int(_remaining(deadline, self.config.policy.limits.process_seconds))
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
                        timeout_seconds=_remaining(
                            deadline, self.config.policy.limits.http_seconds
                        ),
                        expected_marker=marker,
                    ),
                    sleep=lambda seconds: time.sleep(_remaining(deadline, seconds)),
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
        _operation_kind: str = "app.delete",
    ) -> ApplicationRemoved:
        """Resumably purge all app-owned provider/runtime resources and tombstone it."""
        application = db.get_application(self.connection, application_identifier)
        if application is None:
            raise ValidationError("application does not exist")
        if confirmation != application.slug:
            raise ValidationError("application deletion requires the exact slug")
        deadline = _deadline(self.config)
        scope = f"app-{application.application_id}"
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=_remaining(deadline, self.config.policy.limits.process_seconds),
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
                current, _operation_kind, refs, deadline
            )
            try:
                deployment = db.get_deployment(
                    self.connection, current.application_id
                )
                if deployment is not None:
                    try:
                        job_id = app.nomad_job_id(deployment.nomad_job, current.slug)
                    except ValidationError:
                        # Pre-product legacy snapshots have no exact job marker;
                        # their bounded generic cleanup still runs below.
                        job_id = None
                    if job_id is not None:
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
                            raise app.ApplicationError(
                                "accepted workload withdrawal was not confirmed"
                            )
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
                        owner=storage_owner(
                            resource.resource_type, resource.resource_name
                        ),
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
                if removed.get("jobAbsent") is not True or removed.get("variableAbsent") is not True:
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

    def remove(self, application_identifier: str, *, confirmation: str) -> ApplicationRemoved:
        """Compatibility name for the product cascade-delete service."""
        return self.delete(
            application_identifier,
            confirmation=confirmation,
            _operation_kind="app.remove",
        )


class LogService:
    """Return bounded runtime and build logs without presentation concerns."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
        *,
        helper_caller: HelperCaller = _call_helper,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory
        self.helper_caller = helper_caller

    def _application(self, identifier: str) -> db.Application:
        application = db.get_application(self.connection, identifier)
        if application is None:
            raise ValidationError("application does not exist")
        return application

    def runtime(
        self,
        application_identifier: str,
        *,
        lines: int,
        follow: bool = False,
    ) -> LogChunk:
        application = self._application(application_identifier)
        result = app.application_logs(
            application.slug,
            lines=lines,
            follow=follow,
            timeout_seconds=self.config.policy.limits.helper_seconds,
            helper_caller=lambda action, values, **_bounds: self.helper_caller(
                self.config,
                action,
                values,
                deadline=_deadline(self.config),
            ),
        )
        text = result.get("text")
        if not isinstance(text, str):
            raise app.ApplicationError("helper returned invalid runtime log evidence")
        return LogChunk(text, "running", len(text.encode()), bool(result.get("truncated")))

    def attempts(self, application_identifier: str) -> tuple[BuildAttempt, ...]:
        application = self._application(application_identifier)
        return tuple(
            BuildAttempt(
                item.operation_id,
                item.status,
                item.phase,
                item.started_at,
                item.refs.get("source_commit")
                if isinstance(item.refs.get("source_commit"), str)
                else None,
            )
            for item in db.list_application_deploy_operations(
                self.connection,
                application.application_id,
            )
        )

    def build(
        self,
        application_identifier: str,
        *,
        build_id: str | None,
        lines: int,
        offset: int | None = None,
    ) -> tuple[BuildAttempt, LogChunk]:
        application = self._application(application_identifier)
        operations = db.list_application_deploy_operations(
            self.connection,
            application.application_id,
        )
        if not operations:
            raise ValidationError("application has no build attempts")
        if build_id is None:
            operation = operations[0]
        else:
            selected_id = uuid(build_id, field="build ID")
            operation = next(
                (item for item in operations if item.operation_id == selected_id),
                None,
            )
            if operation is None:
                raise ValidationError("build ID does not belong to the application")
        attempt = BuildAttempt(
            operation.operation_id,
            operation.status,
            operation.phase,
            operation.started_at,
            operation.refs.get("source_commit")
            if isinstance(operation.refs.get("source_commit"), str)
            else None,
        )
        result = self.helper_caller(
            self.config,
            "app.build.logs",
            {
                "slug": application.slug,
                "buildId": operation.operation_id,
                "lines": lines,
                "offset": offset,
            },
            deadline=_deadline(self.config),
        )
        if result.get("exists") is True:
            text = result.get("text")
            next_offset = result.get("nextOffset")
            state = result.get("state")
            if (
                not isinstance(text, str)
                or isinstance(next_offset, bool)
                or not isinstance(next_offset, int)
                or state not in {"running", "complete", "failed", "unknown"}
            ):
                raise app.ApplicationError("helper returned invalid build log evidence")
            return attempt, LogChunk(
                text,
                state,
                next_offset,
                bool(result.get("truncated")),
            )
        if offset is not None:
            raise ValidationError("live build log is unavailable for this historical attempt")
        stored = operation.refs.get("build_log_path")
        if not isinstance(stored, str):
            raise ValidationError("build attempt has no captured output")
        root = self.state_directory.resolve(strict=True)
        path = (self.state_directory / stored).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValidationError("build log path is invalid")
        text = _tail_file(
            path,
            lines=lines,
            maximum_bytes=self.config.policy.limits.build_log_bytes,
        )
        return attempt, LogChunk(
            text + ("\n" if text else ""),
            "complete",
            path.stat().st_size,
            False,
        )


class EnvironmentService:
    """Own write-only environment intent, recovery, CAS evidence, and metadata."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory

    def preflight(self, application_identifier: str) -> None:
        """Reject an absent app or wrong cloud project before accepting a secret value."""
        application = db.get_application(self.connection, application_identifier)
        if application is None:
            raise ValidationError("application does not exist")
        _reject_deleting(self.connection, application)
        deadline = _deadline(self.config)
        openstack.verify_project(
            self.config.platform,
            timeout_seconds=_remaining(
                deadline,
                self.config.policy.limits.process_seconds,
            ),
        )

    def _ownership(self, application_id: str) -> dict[str, str]:
        return {
            item.key_name: item.owner
            for item in db.list_environment_keys(
                self.connection,
                application_id=application_id,
            )
        }

    def _recover_interrupted(
        self,
        application: db.Application,
        ownership: Mapping[str, str],
        operation: db.Operation,
        *,
        deadline: float,
    ) -> None:
        intended = operation.refs.get("key_names")
        mutation = operation.refs.get("mutation")
        if (
            not isinstance(intended, list)
            or any(not isinstance(item, str) for item in intended)
            or mutation not in {"set", "unset"}
        ):
            raise app.ApplicationError("interrupted environment intent is malformed")
        intended_names = {env_key(item) for item in intended}
        if intended_names & PLATFORM_ENVIRONMENT_KEYS or any(
            name.startswith(RESERVED_ENVIRONMENT_PREFIX) for name in intended_names
        ):
            raise app.ApplicationError("interrupted staff environment intent used a reserved key")
        observed = app.list_environment(
            application.slug,
            timeout_seconds=_remaining(deadline, self.config.policy.limits.helper_seconds),
            helper_caller=lambda action, values, **_bounds: _call_helper(
                self.config,
                action,
                values,
                deadline=deadline,
            ),
        )
        names = observed.get("keys")
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise app.ApplicationError("helper returned invalid interrupted environment evidence")
        present = {env_key(item) for item in names}
        recovered = {
            name for name, owner in ownership.items() if owner == "staff" and name in present
        }
        if mutation == "set":
            recovered.update(
                name
                for name in intended_names & present
                if ownership.get(name, "staff") == "staff"
            )
        db.set_environment_keys(
            self.connection,
            application_id=application.application_id,
            owner="staff",
            keys=sorted(recovered),
        )
        db.mark_failed(
            self.connection,
            operation.operation_id,
            "interrupted environment state was observed before explicit retry",
            cleanup_state="not_required",
        )

    def mutate(self, request: EnvironmentMutationRequest) -> EnvironmentMutationResult:
        application = db.get_application(self.connection, request.application)
        if application is None:
            raise ValidationError("application does not exist")
        _reject_deleting(self.connection, application)
        if request.action not in {"set", "unset", "import"}:
            raise ValidationError("environment action is invalid")
        updates = {env_key(key): value for key, value in request.updates.items()}
        removals = tuple(env_key(key) for key in request.removals)
        overlap = set(updates) & set(removals)
        if overlap:
            raise ValidationError("environment keys cannot be both set and removed")
        if request.action == "unset" and not removals:
            raise ValidationError("select at least one environment key to remove")
        if request.action != "unset" and removals:
            raise ValidationError("environment removals require the unset action")
        if request.action == "unset" and updates:
            raise ValidationError("environment updates require set or import")

        deadline = _deadline(self.config)
        scope = f"app-{application.application_id}"
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=_remaining(
                    deadline,
                    self.config.policy.limits.process_seconds,
                ),
            )
            refreshed = db.get_application(self.connection, request.application)
            if refreshed is None or refreshed.application_id != application.application_id:
                raise ValidationError("application does not exist")
            ownership = self._ownership(refreshed.application_id)
            unfinished = db.get_unfinished_operation(self.connection, scope)
            if unfinished is not None:
                if not unfinished.kind.startswith("app.env."):
                    raise db.UnfinishedOperationError(
                        scope,
                        unfinished.operation_id,
                        unfinished.kind,
                    )
                self._recover_interrupted(
                    refreshed,
                    ownership,
                    unfinished,
                    deadline=deadline,
                )
                ownership = self._ownership(refreshed.application_id)

            intended_names = sorted(set(updates) | set(removals))
            protected = {
                name
                for name in intended_names
                if name in PLATFORM_ENVIRONMENT_KEYS
                or name.startswith(RESERVED_ENVIRONMENT_PREFIX)
                or (name in ownership and ownership[name] != "staff")
            }
            if protected:
                first = sorted(protected)[0]
                raise ValidationError(
                    f"environment key {first!r} is reserved for the platform or storage"
                )
            mutation = "unset" if removals else "set"
            operation_id = str(uuid_module.uuid4())
            db.begin_operation(
                self.connection,
                operation_id=operation_id,
                kind=f"app.env.{request.action}",
                scope=scope,
                phase="intent_recorded",
                deadline_at=_wall_deadline(deadline),
                refs={"key_names": intended_names, "mutation": mutation},
            )
            try:
                helper_caller = lambda action, values, **_bounds: _call_helper(
                    self.config,
                    action,
                    values,
                    deadline=deadline,
                )
                if removals:
                    result = app.remove_environment(
                        refreshed.slug,
                        removals,
                        ownership,
                        timeout_seconds=_remaining(
                            deadline,
                            self.config.policy.limits.helper_seconds,
                        ),
                        helper_caller=helper_caller,
                    )
                else:
                    result = app.set_environment(
                        refreshed.slug,
                        updates,
                        ownership,
                        timeout_seconds=_remaining(
                            deadline,
                            self.config.policy.limits.helper_seconds,
                        ),
                        helper_caller=helper_caller,
                    )
                names = result.get("keys")
                if not isinstance(names, list) or any(
                    not isinstance(item, str) for item in names
                ):
                    raise app.ApplicationError("helper returned invalid environment key evidence")
                if (
                    refreshed.desired_running
                    and db.get_deployment(self.connection, refreshed.application_id) is not None
                    and (
                        result.get("restarted") is not True
                        or result.get("schedulerHealthy") is not True
                        or result.get("publicHealthy") is not True
                    )
                ):
                    raise app.ApplicationError(
                        "environment restart and health evidence was not confirmed"
                    )
                present = {env_key(item) for item in names}
                prior_staff = {name for name, owner in ownership.items() if owner == "staff"}
                db.set_environment_keys(
                    self.connection,
                    application_id=refreshed.application_id,
                    owner="staff",
                    keys=sorted((prior_staff | set(updates)) & present),
                )
                db.checkpoint_operation(
                    self.connection,
                    operation_id,
                    phase="accepted",
                    refs={"key_names": intended_names, "mutation": mutation},
                )
                db.mark_succeeded(
                    self.connection,
                    operation_id,
                    cleanup_state="not_required",
                )
            except Exception as error:
                db.mark_recovery_required(self.connection, operation_id, error)
                raise
        modify_index = result.get("modifyIndex")
        if modify_index is not None and (
            isinstance(modify_index, bool) or not isinstance(modify_index, int)
        ):
            raise app.ApplicationError("helper returned invalid environment revision evidence")
        return EnvironmentMutationResult(tuple(sorted(present)), modify_index)

    def list(self, application_identifier: str) -> EnvironmentMutationResult:
        application = db.get_application(self.connection, application_identifier)
        if application is None:
            raise ValidationError("application does not exist")
        result = app.list_environment(
            application.slug,
            timeout_seconds=self.config.policy.limits.helper_seconds,
            helper_caller=lambda action, values, **_bounds: _call_helper(
                self.config,
                action,
                values,
                deadline=_deadline(self.config),
            ),
        )
        names = result.get("keys")
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise app.ApplicationError("helper returned invalid environment key evidence")
        modify_index = result.get("modifyIndex")
        if modify_index is not None and (
            isinstance(modify_index, bool) or not isinstance(modify_index, int)
        ):
            raise app.ApplicationError("helper returned invalid environment revision evidence")
        return EnvironmentMutationResult(
            tuple(sorted(env_key(item) for item in names)),
            modify_index,
        )


class StorageService:
    """Own locking, project verification, and managed-storage dispatch."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory

    def mutate(self, request: StorageMutationRequest) -> StorageMutationResult:
        application = db.get_application(self.connection, request.application)
        if application is None:
            raise ValidationError("application does not exist")
        _reject_deleting(self.connection, application)
        checked_name = resource_name(request.resource_name)
        if request.action not in {"create", "verify", "rotate", "remove"}:
            raise ValidationError("storage action is invalid")
        if not isinstance(request.purge_s3, bool):
            raise ValidationError("purge_s3 must be boolean")
        selected: tuple[str, ...] | None = request.resource_types
        if request.action != "verify" and not selected:
            raise ValidationError("select at least one storage type")
        if request.action == "verify" and not selected:
            selected = None

        deadline = _deadline(self.config)
        with runtime.lock(
            self.state_directory,
            f"app-{application.application_id}",
            deadline=deadline,
        ):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=_remaining(
                    deadline,
                    self.config.policy.limits.process_seconds,
                ),
            )
            refreshed = db.get_application(self.connection, request.application)
            if refreshed is None or refreshed.application_id != application.application_id:
                raise ValidationError("application does not exist")
            durable_deadline = _wall_deadline(deadline)
            if request.action == "create":
                result = storage.create(
                    self.connection,
                    self.config,
                    refreshed.application_id,
                    selected or (),
                    resource_name=checked_name,
                    deadline_at=durable_deadline,
                    process_deadline=deadline,
                )
            elif request.action == "verify":
                result = storage.verify(
                    self.connection,
                    self.config,
                    refreshed.application_id,
                    selected,
                    resource_name=checked_name,
                    deadline_at=durable_deadline,
                    process_deadline=deadline,
                )
            elif request.action == "rotate":
                result = storage.rotate(
                    self.connection,
                    self.config,
                    refreshed.application_id,
                    selected or (),
                    resource_name=checked_name,
                    deadline_at=durable_deadline,
                    process_deadline=deadline,
                )
            else:
                result = storage.remove(
                    self.connection,
                    self.config,
                    refreshed.application_id,
                    selected or (),
                    resource_name=checked_name,
                    confirm_name=request.confirm_name,
                    confirm_destructive=True,
                    purge_s3=request.purge_s3,
                    deadline_at=durable_deadline,
                    process_deadline=deadline,
                )
        return StorageMutationResult(result.requested, result.completed)
