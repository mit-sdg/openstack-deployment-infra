"""Typed product-service entry points independent of CLI parsing and rendering.

This module is the extraction boundary shared temporarily by the staff CLI and,
later, the local controller API. Requests contain validated product data rather
than ``argparse`` namespaces, and results contain typed identities rather than
rendered output.
"""

from __future__ import annotations

import sqlite3
import time
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from . import app, db, openstack, remote, runtime, status, storage
from .config import Config
from .storage_contract import PLATFORM_ENVIRONMENT_KEYS, RESERVED_ENVIRONMENT_PREFIX
from .validation import ValidationError, env_key, resource_name, slug

StorageAction = Literal["create", "verify", "rotate", "remove"]
EnvironmentAction = Literal["set", "unset", "import"]


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
    """Own database-only application product mutations."""

    def __init__(self, connection: sqlite3.Connection, config: Config) -> None:
        self.connection = connection
        self.config = config

    def declare(self, application_slug: str) -> ApplicationCreated:
        """Create one inert disabled application before storage or deployment."""
        checked_slug = slug(application_slug)
        if db.get_application(self.connection, checked_slug) is not None:
            raise ValidationError("application already exists")
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
        if db.get_application(self.connection, application_identifier) is None:
            raise ValidationError("application does not exist")
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
