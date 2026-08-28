"""Controller orchestration for PostgreSQL, MongoDB, and S3.

SQLite contains platform-created provider identities, fixed policy values, key
ownership, and operation checkpoints. Scoped credentials never cross the helper
boundary. Each storage mutation has a deliberately small, phase-specific
restart path.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid as uuid_module
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import remote
from . import database as db
from ..config import Config
from .storage_contract import (
    RESOURCE_TYPES,
    canonical_secret_keys,
    storage_owner,
)
from ..validation import ValidationError, slug, uuid
from ..validation import resource_name as validate_resource_name

HelperCaller = Callable[..., Mapping[str, Any]]


class StorageOperationError(RuntimeError):
    """A storage mutation needs operator attention; the message is secret-free."""


@dataclass(frozen=True, slots=True)
class StorageResult:
    requested: tuple[str, ...]
    completed: tuple[str, ...]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _deadline(config: Config, *, process_deadline: float | None = None) -> str:
    if process_deadline is None:
        remaining = float(config.policy.limits.helper_seconds)
    else:
        remaining = process_deadline - time.monotonic()
        if remaining <= 0:
            raise StorageOperationError("storage operation deadline was reached")
    value = datetime.now(UTC) + timedelta(seconds=remaining)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _fresh_recovery_deadline(config: Config, *, process_deadline: float | None = None) -> str:
    """Give one retry a bounded attempt window without rewriting its evidence."""
    remaining = float(config.policy.limits.helper_seconds)
    if process_deadline is not None:
        remaining = min(remaining, process_deadline - time.monotonic())
    if remaining <= 0:
        raise StorageOperationError("storage operation deadline was reached")
    value = datetime.now(UTC) + timedelta(seconds=remaining)
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _attempt_deadline(
    config: Config,
    operation: db.Operation,
    *,
    recovering: bool,
    process_deadline: float | None,
) -> str:
    """Select an attempt deadline while preserving the durable operation evidence."""
    if not recovering:
        return operation.deadline_at
    try:
        deadline = datetime.fromisoformat(operation.deadline_at.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise StorageOperationError("storage operation deadline is malformed") from None
    if deadline > datetime.now(UTC):
        return operation.deadline_at
    return _fresh_recovery_deadline(config, process_deadline=process_deadline)


def _selected(resource_types: Iterable[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in resource_types:
        if not isinstance(value, str) or value not in RESOURCE_TYPES:
            raise ValidationError("storage type must be postgres, mongo, or s3")
        normalized.add(value)
    if not normalized:
        raise ValidationError("select at least one storage type")
    return tuple(item for item in RESOURCE_TYPES if item in normalized)


def _application(connection: sqlite3.Connection, identifier: str) -> db.Application:
    application = db.get_application(connection, identifier)
    if application is None:
        raise ValidationError("application does not exist")
    return application


def _resources(
    connection: sqlite3.Connection, application_id: str
) -> dict[tuple[str, str], db.ManagedResource]:
    return {
        (item.resource_type, item.resource_name): item
        for item in db.list_managed_resources(connection, application_id=application_id)
    }


def _provider_name(
    config: Config, application: db.Application, resource_type: str, name: str
) -> str:
    digest = hashlib.sha256(
        f"{application.application_id}:{resource_type}:{name}".encode()
    ).hexdigest()
    if resource_type in {"postgres", "mongo"}:
        base = (
            application.application_id.replace("-", "")[:20] if name == "default" else digest[:20]
        )
        return f"p_{base}"
    prefix = config.platform.prefix
    suffix = application.application_id.replace("-", "")[:8] if name == "default" else digest[:10]
    visible = application.slug if name == "default" else f"{application.slug}-{name}"
    visible_limit = 63 - len(prefix) - len(suffix) - 2
    return f"{prefix}-{visible[:visible_limit].rstrip('-')}-{suffix}"


def _standard_storage_args(config: Config, resource_type: str) -> dict[str, int]:
    """Return only the current fixed policy inputs for a new resource.

    Resource rows are evidence, not a source of sizing authority. In
    particular, a row must never cause an operation to reuse values from an
    untrusted or pre-greenfield database.
    """
    standard = config.policy.standard
    if resource_type == "postgres":
        return {
            "postgresConnections": standard.postgres_connections,
            "measuredTargetBytes": standard.postgres_measured_bytes,
        }
    if resource_type == "mongo":
        return {"measuredTargetBytes": standard.mongo_measured_bytes}
    return {"s3Bytes": standard.s3_bytes, "s3Objects": standard.s3_objects}


def _resource_values(config: Config, resource_type: str) -> dict[str, int | None]:
    standard = config.policy.standard
    return {
        "postgres_connections": standard.postgres_connections
        if resource_type == "postgres"
        else None,
        "measured_target_bytes": (
            standard.postgres_measured_bytes
            if resource_type == "postgres"
            else standard.mongo_measured_bytes
            if resource_type == "mongo"
            else None
        ),
        "s3_bytes": standard.s3_bytes if resource_type == "s3" else None,
        "s3_objects": standard.s3_objects if resource_type == "s3" else None,
    }


def _call(
    caller: HelperCaller,
    config: Config,
    action: str,
    args: Mapping[str, Any],
    *,
    deadline_at: str,
    process_deadline: float | None = None,
) -> Mapping[str, Any]:
    try:
        deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ValueError
        remaining = (deadline - datetime.now(UTC)).total_seconds()
    except (AttributeError, TypeError, ValueError):
        raise StorageOperationError("storage operation deadline is malformed") from None
    if process_deadline is not None:
        remaining = min(remaining, process_deadline - time.monotonic())
    if remaining <= 0:
        raise StorageOperationError("storage operation deadline was reached")
    return caller(
        action,
        args,
        timeout_seconds=min(config.policy.limits.helper_seconds, remaining),
        request_limit=config.policy.limits.helper_request_bytes,
        response_limit=config.policy.limits.helper_response_bytes,
        stderr_limit=config.policy.limits.stderr_bytes,
        helper_command=remote.helper_command_path(config.platform.get("paths.root")),
    )


def _common(application: db.Application, resource_name: str) -> dict[str, str]:
    return {
        "applicationId": application.application_id,
        "applicationSlug": application.slug,
        "resourceName": resource_name,
    }


def _identity_result(result: Mapping[str, Any]) -> tuple[str, str]:
    provider_id = result.get("providerId")
    provider_name = result.get("providerName")
    if (
        not isinstance(provider_id, str)
        or not provider_id
        or len(provider_id) > 253
        or "\x00" in provider_id
    ):
        raise StorageOperationError("storage helper returned invalid provider identity")
    if (
        not isinstance(provider_name, str)
        or not provider_name
        or len(provider_name) > 253
        or "\x00" in provider_name
    ):
        raise StorageOperationError("storage helper returned invalid provider identity")
    if result.get("verified") is not True:
        raise StorageOperationError("storage helper did not confirm scoped access")
    return provider_id, provider_name


def _rotation_identity_result(result: Mapping[str, Any], resource: db.ManagedResource) -> None:
    provider_id, provider_name = _identity_result(result)
    if provider_id != resource.provider_id or provider_name != resource.provider_name:
        raise StorageOperationError("storage rotation provider identity drifted")
    credential_name = result.get("credentialName")
    if (
        not isinstance(credential_name, str)
        or not credential_name
        or len(credential_name) > 253
        or "\x00" in credential_name
    ):
        raise StorageOperationError("storage rotation credential identity was not confirmed")


def _put_resource(
    connection: sqlite3.Connection,
    config: Config,
    application: db.Application,
    resource_type: str,
    resource_name: str,
    *,
    lifecycle_state: str,
    provider_id: str | None,
    provider_name: str,
    existing: db.ManagedResource | None = None,
    verified: bool = False,
) -> None:
    # Sizing is always sourced from the fixed current policy. Existing rows
    # are retained only as provider-operation evidence and never as an
    # authority for quotas or connection limits.
    values = _resource_values(config, resource_type)
    db.put_managed_resource(
        connection,
        application_id=application.application_id,
        resource_type=resource_type,
        resource_name=resource_name,
        provider_id=provider_id,
        provider_name=provider_name,
        lifecycle_state=lifecycle_state,
        postgres_connections=values["postgres_connections"],
        measured_target_bytes=values["measured_target_bytes"],
        s3_bytes=values["s3_bytes"],
        s3_objects=values["s3_objects"],
        last_verified_at=(
            _now() if verified else existing.last_verified_at if existing is not None else None
        ),
    )


def _refs_list(refs: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = refs.get(name)
    if not isinstance(value, list) or any(item not in RESOURCE_TYPES for item in value):
        raise StorageOperationError("unfinished storage operation metadata is malformed")
    result = tuple(str(item) for item in value)
    if len(result) != len(set(result)):
        raise StorageOperationError("unfinished storage operation metadata is malformed")
    return result


def _existing_operation(
    connection: sqlite3.Connection,
    application: db.Application,
    *,
    kind: str,
    selected: tuple[str, ...],
    extra_refs: Mapping[str, Any] | None = None,
) -> db.Operation | None:
    scope = f"app-{application.application_id}"
    operation = db.get_unfinished_operation(connection, scope)
    if operation is None:
        return None
    if operation.kind != kind:
        raise db.UnfinishedOperationError(scope, operation.operation_id, operation.kind)
    recorded = _refs_list(operation.refs, "selected")
    if recorded != selected or any(
        operation.refs.get(key) != value for key, value in (extra_refs or {}).items()
    ):
        raise db.UnfinishedOperationError(scope, operation.operation_id, operation.kind)
    _refs_list(operation.refs, "completed")
    return operation


def _begin(
    connection: sqlite3.Connection,
    config: Config,
    application: db.Application,
    *,
    kind: str,
    selected: tuple[str, ...],
    phase: str,
    operation_id: str | None,
    deadline_at: str | None,
    extra_refs: Mapping[str, Any] | None = None,
) -> db.Operation:
    identifier = (
        uuid(operation_id, field="operation_id") if operation_id else str(uuid_module.uuid4())
    )
    refs = {"selected": list(selected), "completed": [], **(extra_refs or {})}
    selected_deadline = deadline_at or _deadline(config)
    try:
        parsed_deadline = datetime.fromisoformat(selected_deadline.replace("Z", "+00:00"))
        if parsed_deadline.tzinfo is None or parsed_deadline <= datetime.now(UTC):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("storage operation deadline must be a future timestamp") from None
    return db.begin_operation(
        connection,
        operation_id=identifier,
        kind=kind,
        scope=f"app-{application.application_id}",
        phase=phase,
        deadline_at=selected_deadline,
        refs=refs,
    )


def _checkpoint_refs(
    selected: tuple[str, ...], completed: list[str], current: str, **extra: Any
) -> dict[str, Any]:
    return {
        "selected": list(selected),
        "completed": completed,
        "current": current,
        **extra,
    }


def _mark_ambiguous(
    connection: sqlite3.Connection,
    config: Config,
    application: db.Application,
    operation: db.Operation,
    resource_type: str,
    *,
    phase: str,
    provider_id: str | None,
    provider_name: str,
    message: str,
) -> None:
    resource_name = validate_resource_name(operation.refs.get("resource_name", "default"))
    existing = _resources(connection, application.application_id).get(
        (resource_type, resource_name)
    )
    _put_resource(
        connection,
        config,
        application,
        resource_type,
        resource_name,
        lifecycle_state="recovery_required",
        provider_id=provider_id,
        provider_name=provider_name,
        existing=existing,
    )
    action = "retry the original controller request with the same Idempotency-Key"
    db.mark_recovery_required(
        connection, operation.operation_id, f"{message}; {action}", phase=phase
    )
    raise StorageOperationError(f"{message}; recovery is required; {action}") from None


def _mutation_args(operation: db.Operation, *, recovering: bool) -> dict[str, Any]:
    return {"operationId": operation.operation_id, "recover": recovering}


def _checkpoint_observed_index(
    connection: sqlite3.Connection,
    operation: db.Operation,
    *,
    phase: str,
    refs: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    value = result.get("modifyIndex")
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StorageOperationError("storage helper returned an invalid Nomad ModifyIndex")
    db.checkpoint_operation(
        connection,
        operation.operation_id,
        phase=phase,
        refs={**refs, "modify_index": value},
        merge_refs=True,
    )


def _complete_operation(
    connection: sqlite3.Connection,
    operation: db.Operation,
    selected: tuple[str, ...],
    completed: list[str],
    *,
    extra_refs: Mapping[str, Any] | None = None,
) -> StorageResult:
    current = db.get_operation(connection, operation.operation_id)
    preserved = {} if current is None else dict(current.refs)
    refs = {
        **preserved,
        "selected": list(selected),
        "completed": completed,
        **(extra_refs or {}),
    }
    db.checkpoint_operation(
        connection,
        operation.operation_id,
        phase="accepted",
        refs=refs,
        cleanup_state="confirmed",
        merge_refs=True,
    )
    db.mark_succeeded(connection, operation.operation_id)
    return StorageResult(selected, tuple(completed))


def create(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    resource_types: Iterable[str],
    *,
    resource_name: str = "default",
    helper_caller: HelperCaller = remote.call_helper,
    operation_id: str | None = None,
    deadline_at: str | None = None,
    process_deadline: float | None = None,
) -> StorageResult:
    """Create missing resources or reconcile the same interrupted create."""
    application = _application(connection, application_id)
    checked_name = validate_resource_name(resource_name)
    selected = _selected(resource_types)
    operation = _existing_operation(
        connection,
        application,
        kind="storage.create",
        selected=selected,
        extra_refs={"resource_name": checked_name},
    )
    recovering = operation is not None
    resources = _resources(connection, application.application_id)
    if operation is None:
        duplicates = [item for item in selected if (item, checked_name) in resources]
        if duplicates:
            raise ValidationError(f"storage already exists: {', '.join(duplicates)}")
        operation = _begin(
            connection,
            config,
            application,
            kind="storage.create",
            selected=selected,
            phase="validated",
            operation_id=operation_id,
            deadline_at=(deadline_at or _deadline(config, process_deadline=process_deadline)),
            extra_refs={"resource_name": checked_name},
        )
    attempt_deadline_at = _attempt_deadline(
        config,
        operation,
        recovering=recovering,
        process_deadline=process_deadline,
    )
    completed = list(_refs_list(operation.refs, "completed"))
    for resource_type in selected:
        if resource_type in completed:
            continue
        anticipated_name = _provider_name(config, application, resource_type, checked_name)
        current = resources.get((resource_type, checked_name))
        if not recovering or current is None:
            _put_resource(
                connection,
                config,
                application,
                resource_type,
                checked_name,
                lifecycle_state="creating",
                provider_id=None,
                provider_name=anticipated_name,
            )
        refs = _checkpoint_refs(selected, completed, resource_type)
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"create_{resource_type}",
            refs=refs,
            merge_refs=True,
        )
        try:
            result = _call(
                helper_caller,
                config,
                f"storage.{resource_type}.create",
                {
                    **_common(application, checked_name),
                    **_standard_storage_args(config, resource_type),
                    **_mutation_args(operation, recovering=recovering),
                },
                deadline_at=attempt_deadline_at,
                process_deadline=process_deadline,
            )
            provider_id, provider_name = _identity_result(result)
            if result.get("evidenceAccepted") is not True:
                raise StorageOperationError(
                    "storage helper did not confirm restart, scheduler, and public health"
                )
            _checkpoint_observed_index(
                connection,
                operation,
                phase=f"create_observed_{resource_type}",
                refs=refs,
                result=result,
            )
            if provider_name != anticipated_name or (
                resource_type in {"postgres", "mongo"} and provider_id != anticipated_name
            ):
                raise StorageOperationError("storage helper returned mismatched provider identity")
        except Exception as error:
            if isinstance(error, remote.HelperError) and error.code == "CREATE_ROLLED_BACK":
                db.delete_managed_resource(
                    connection,
                    application_id=application.application_id,
                    resource_type=resource_type,
                    resource_name=checked_name,
                )
                db.mark_failed(
                    connection,
                    operation.operation_id,
                    f"{resource_type} creation failed; rollback was confirmed",
                    cleanup_state="confirmed",
                )
                raise StorageOperationError(
                    f"{resource_type} creation failed; rollback was confirmed"
                ) from None
            _mark_ambiguous(
                connection,
                config,
                application,
                operation,
                resource_type,
                phase=f"create_{resource_type}",
                provider_id=current.provider_id if current is not None else None,
                provider_name=anticipated_name,
                message=f"{resource_type} creation was not confirmed",
            )
        accepted = _resources(connection, application.application_id).get(
            (resource_type, checked_name)
        )
        _put_resource(
            connection,
            config,
            application,
            resource_type,
            checked_name,
            lifecycle_state="active",
            provider_id=provider_id,
            provider_name=provider_name,
            existing=accepted,
            verified=True,
        )
        db.set_environment_keys(
            connection,
            application_id=application.application_id,
            owner=storage_owner(resource_type, checked_name),
            keys=canonical_secret_keys(resource_type, checked_name),
        )
        completed.append(resource_type)
        recovering = False
        resources = _resources(connection, application.application_id)
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"created_{resource_type}",
            refs={"selected": list(selected), "completed": completed},
            merge_refs=True,
        )
    return _complete_operation(connection, operation, selected, completed)


def verify(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    resource_types: Iterable[str] | None = None,
    *,
    resource_name: str = "default",
    helper_caller: HelperCaller = remote.call_helper,
    operation_id: str | None = None,
    deadline_at: str | None = None,
    process_deadline: float | None = None,
) -> StorageResult:
    """Verify credentials, retrying an interrupted verification without reading secrets."""
    application = _application(connection, application_id)
    checked_name = validate_resource_name(resource_name)
    resources = _resources(connection, application.application_id)
    selected = (
        _selected(resource_types)
        if resource_types is not None
        else tuple(item for item in RESOURCE_TYPES if (item, checked_name) in resources)
    )
    if not selected:
        raise ValidationError("application has no managed storage")
    operation = _existing_operation(
        connection,
        application,
        kind="storage.verify",
        selected=selected,
        extra_refs={"resource_name": checked_name},
    )
    recovering = operation is not None
    missing = [item for item in selected if (item, checked_name) not in resources]
    if missing:
        if operation is not None:
            raise StorageOperationError("unfinished storage verification metadata is inconsistent")
        raise ValidationError(f"storage does not exist: {', '.join(missing)}")
    if operation is None:
        operation = _begin(
            connection,
            config,
            application,
            kind="storage.verify",
            selected=selected,
            phase="validated",
            operation_id=operation_id,
            deadline_at=(deadline_at or _deadline(config, process_deadline=process_deadline)),
            extra_refs={"resource_name": checked_name},
        )
    attempt_deadline_at = _attempt_deadline(
        config,
        operation,
        recovering=recovering,
        process_deadline=process_deadline,
    )
    completed = list(_refs_list(operation.refs, "completed"))
    for resource_type in selected:
        if resource_type in completed:
            continue
        resource = resources[(resource_type, checked_name)]
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"verify_{resource_type}",
            refs=_checkpoint_refs(selected, completed, resource_type),
            merge_refs=True,
        )
        try:
            result = _call(
                helper_caller,
                config,
                f"storage.{resource_type}.verify",
                {
                    **_common(application, checked_name),
                    "providerId": resource.provider_id,
                    "providerName": resource.provider_name,
                    **_mutation_args(operation, recovering=recovering),
                },
                deadline_at=attempt_deadline_at,
                process_deadline=process_deadline,
            )
            if result.get("verified") is not True:
                raise StorageOperationError("verification evidence was rejected")
            _checkpoint_observed_index(
                connection,
                operation,
                phase=f"verify_observed_{resource_type}",
                refs=_checkpoint_refs(selected, completed, resource_type),
                result=result,
            )
        except Exception:
            _mark_ambiguous(
                connection,
                config,
                application,
                operation,
                resource_type,
                phase=f"verify_{resource_type}",
                provider_id=resource.provider_id,
                provider_name=resource.provider_name,
                message=f"{resource_type} verification was not confirmed",
            )
        _put_resource(
            connection,
            config,
            application,
            resource_type,
            checked_name,
            lifecycle_state="active",
            provider_id=resource.provider_id,
            provider_name=resource.provider_name,
            existing=resource,
            verified=True,
        )
        completed.append(resource_type)
        recovering = False
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"verified_{resource_type}",
            refs={"selected": list(selected), "completed": completed},
            merge_refs=True,
        )
    return _complete_operation(connection, operation, selected, completed)


def rotate(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    resource_types: Iterable[str],
    *,
    resource_name: str = "default",
    helper_caller: HelperCaller = remote.call_helper,
    operation_id: str | None = None,
    deadline_at: str | None = None,
    process_deadline: float | None = None,
) -> StorageResult:
    """Rotate selected credentials or reconcile the same interrupted rotation."""
    application = _application(connection, application_id)
    checked_name = validate_resource_name(resource_name)
    selected = _selected(resource_types)
    operation = _existing_operation(
        connection,
        application,
        kind="storage.rotate",
        selected=selected,
        extra_refs={"resource_name": checked_name},
    )
    recovering = operation is not None
    resources = _resources(connection, application.application_id)
    missing = [item for item in selected if (item, checked_name) not in resources]
    if missing:
        if operation is not None:
            raise StorageOperationError("unfinished storage rotation metadata is inconsistent")
        raise ValidationError(f"storage does not exist: {', '.join(missing)}")
    if operation is None:
        operation = _begin(
            connection,
            config,
            application,
            kind="storage.rotate",
            selected=selected,
            phase="validated",
            operation_id=operation_id,
            deadline_at=(deadline_at or _deadline(config, process_deadline=process_deadline)),
            extra_refs={"resource_name": checked_name},
        )
    attempt_deadline_at = _attempt_deadline(
        config,
        operation,
        recovering=recovering,
        process_deadline=process_deadline,
    )
    completed = list(_refs_list(operation.refs, "completed"))
    for resource_type in selected:
        if resource_type in completed:
            continue
        resource = resources[(resource_type, checked_name)]
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"rotate_{resource_type}",
            refs=_checkpoint_refs(selected, completed, resource_type),
            merge_refs=True,
        )
        args: dict[str, Any] = {
            **_common(application, checked_name),
            "providerId": resource.provider_id,
            "providerName": resource.provider_name,
            **_mutation_args(operation, recovering=recovering),
        }
        if resource_type == "postgres":
            args["postgresConnections"] = config.policy.standard.postgres_connections
        try:
            result = _call(
                helper_caller,
                config,
                f"storage.{resource_type}.rotate",
                args,
                deadline_at=attempt_deadline_at,
                process_deadline=process_deadline,
            )
            _rotation_identity_result(result, resource)
            _checkpoint_observed_index(
                connection,
                operation,
                phase=f"rotate_observed_{resource_type}",
                refs=_checkpoint_refs(selected, completed, resource_type),
                result=result,
            )
        except Exception:
            _mark_ambiguous(
                connection,
                config,
                application,
                operation,
                resource_type,
                phase=f"rotate_{resource_type}",
                provider_id=resource.provider_id,
                provider_name=resource.provider_name,
                message=f"{resource_type} rotation was not confirmed",
            )
        if result.get("evidenceAccepted") is not True:
            if result.get("rolledBack") is not True:
                _mark_ambiguous(
                    connection,
                    config,
                    application,
                    operation,
                    resource_type,
                        phase=f"rotate_{resource_type}",
                    provider_id=resource.provider_id,
                    provider_name=resource.provider_name,
                    message=f"{resource_type} rotation rollback was not confirmed",
                )
            _put_resource(
                connection,
                config,
                application,
                resource_type,
                checked_name,
                lifecycle_state="active",
                provider_id=resource.provider_id,
                provider_name=resource.provider_name,
                existing=resource,
            )
            db.mark_failed(
                connection,
                operation.operation_id,
                "rotation health evidence was rejected",
                cleanup_state="confirmed",
            )
            raise StorageOperationError(f"{resource_type} rotation health evidence was rejected")
        if result.get("retired") is not True:
            _mark_ambiguous(
                connection,
                config,
                application,
                operation,
                resource_type,
                phase=f"retire_{resource_type}",
                provider_id=resource.provider_id,
                provider_name=resource.provider_name,
                message=f"{resource_type} old credential retirement was not confirmed",
            )
        _put_resource(
            connection,
            config,
            application,
            resource_type,
            checked_name,
            lifecycle_state="active",
            provider_id=resource.provider_id,
            provider_name=resource.provider_name,
            existing=resource,
            verified=True,
        )
        completed.append(resource_type)
        recovering = False
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"rotated_{resource_type}",
            refs={"selected": list(selected), "completed": completed},
            merge_refs=True,
        )
    return _complete_operation(connection, operation, selected, completed)


def remove(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    resource_types: Iterable[str],
    *,
    resource_name: str = "default",
    confirm_name: str | None = None,
    confirm_slug: str | None = None,
    confirm_destructive: bool,
    purge_s3: bool = False,
    helper_caller: HelperCaller = remote.call_helper,
    operation_id: str | None = None,
    deadline_at: str | None = None,
    process_deadline: float | None = None,
) -> StorageResult:
    """Remove resources, reconciling absence before deleting accepted metadata."""
    application = _application(connection, application_id)
    checked_name = validate_resource_name(resource_name)
    selected = _selected(resource_types)
    if confirm_destructive is not True or confirm_name != checked_name:
        raise ValidationError("destructive storage removal requires the exact resource name")
    if confirm_slug is not None and slug(confirm_slug) != application.slug:
        raise ValidationError("destructive storage removal application confirmation is invalid")
    if not isinstance(purge_s3, bool):
        raise ValidationError("purge_s3 must be boolean")
    operation = _existing_operation(
        connection,
        application,
        kind="storage.remove",
        selected=selected,
        extra_refs={"purge_s3": purge_s3, "resource_name": checked_name},
    )
    recovering = operation is not None
    resources = _resources(connection, application.application_id)
    completed = list(_refs_list(operation.refs, "completed")) if operation is not None else []
    if any(item not in selected for item in completed):
        raise StorageOperationError("unfinished storage removal metadata is inconsistent")
    missing = [
        item for item in selected if (item, checked_name) not in resources and item not in completed
    ]
    if missing:
        if operation is not None:
            raise StorageOperationError("unfinished storage removal metadata is inconsistent")
        raise ValidationError(f"storage does not exist: {', '.join(missing)}")
    active_references = set(
        db.active_storage_resource_ids(connection, application.application_id)
    )
    accepted = db.get_deployment(connection, application.application_id)
    referenced = [
        item
        for item in selected
        if (resource := resources.get((item, checked_name))) is not None
        and (
            resource.resource_id in active_references
            or (
                accepted is not None
                and any(
                    key in accepted.nomad_job
                    for key in canonical_secret_keys(item, checked_name)
                )
            )
        )
    ]
    if referenced:
        raise ValidationError(
            f"active deployment references {referenced[0]} storage {checked_name!r}"
        )
    if operation is None:
        operation = _begin(
            connection,
            config,
            application,
            kind="storage.remove",
            selected=selected,
            phase="confirmed",
            operation_id=operation_id,
            deadline_at=(deadline_at or _deadline(config, process_deadline=process_deadline)),
            extra_refs={"purge_s3": purge_s3, "resource_name": checked_name},
        )
    attempt_deadline_at = _attempt_deadline(
        config,
        operation,
        recovering=recovering,
        process_deadline=process_deadline,
    )
    for resource_type in selected:
        if resource_type in completed:
            continue

        # A preflight is a point-in-time assertion, not an authorization that
        # survives another provider deletion. Re-run every still-pending,
        # non-mutating preflight immediately before each irreversible delete.
        # Completed entries are already confirmed absent and no longer have an
        # accepted resource row; they are deliberately excluded.
        pending = tuple(item for item in selected if item not in completed)
        try:
            for pending_type in pending:
                pending_resource = resources.get((pending_type, checked_name))
                if pending_resource is None:
                    raise StorageOperationError(
                        "unfinished storage removal metadata is inconsistent"
                    )
                preflight_args: dict[str, Any] = {
                    **_common(application, checked_name),
                    "providerId": pending_resource.provider_id,
                    "providerName": pending_resource.provider_name,
                    "confirmName": checked_name,
                    "preflight": True,
                    **_mutation_args(operation, recovering=recovering),
                }
                if pending_type == "s3":
                    preflight_args["purge"] = purge_s3
                result = _call(
                    helper_caller,
                    config,
                    f"storage.{pending_type}.remove",
                    preflight_args,
                    deadline_at=attempt_deadline_at,
                    process_deadline=process_deadline,
                )
                if result.get("preflightAccepted") is not True:
                    raise StorageOperationError("storage removal preflight was rejected")
        except Exception:
            message = "storage removal preflight failed before another deletion"
            refs = {
                "selected": list(selected),
                "completed": completed,
                "current": resource_type,
                "purge_s3": purge_s3,
            }
            requires_recovery = recovering or bool(completed)
            if requires_recovery:
                db.checkpoint_operation(
                    connection,
                    operation.operation_id,
                    phase=f"preflight_{resource_type}",
                    refs=refs,
                    merge_refs=True,
                )
                db.mark_recovery_required(
                    connection,
                    operation.operation_id,
                    message,
                    phase=f"preflight_{resource_type}",
                )
            else:
                db.mark_failed(
                    connection,
                    operation.operation_id,
                    "storage removal preflight failed before deletion",
                    cleanup_state="confirmed",
                )
            detail = (
                message
                if requires_recovery
                else "storage removal preflight failed before any deletion"
            )
            raise StorageOperationError(detail) from None
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"preflight_{resource_type}",
            refs={
                "selected": list(selected),
                "completed": completed,
                "current": resource_type,
                "purge_s3": purge_s3,
            },
            merge_refs=True,
        )

        resource = resources[(resource_type, checked_name)]
        if not recovering:
            _put_resource(
                connection,
                config,
                application,
                resource_type,
                checked_name,
                lifecycle_state="removing",
                provider_id=resource.provider_id,
                provider_name=resource.provider_name,
                existing=resource,
            )
        refs = _checkpoint_refs(selected, completed, resource_type, purge_s3=purge_s3)
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"remove_{resource_type}",
            refs=refs,
            merge_refs=True,
        )
        args: dict[str, Any] = {
            **_common(application, checked_name),
            "providerId": resource.provider_id,
            "providerName": resource.provider_name,
            "confirmName": checked_name,
            "preflight": False,
            **_mutation_args(operation, recovering=recovering),
        }
        if resource_type == "s3":
            args["purge"] = purge_s3
        try:
            result = _call(
                helper_caller,
                config,
                f"storage.{resource_type}.remove",
                args,
                deadline_at=attempt_deadline_at,
                process_deadline=process_deadline,
            )
            if (
                result.get("confirmedAbsent") is not True
                or result.get("environmentRemoved") is not True
            ):
                raise StorageOperationError("storage removal evidence was incomplete")
            _checkpoint_observed_index(
                connection,
                operation,
                phase=f"remove_observed_{resource_type}",
                refs=refs,
                result=result,
            )
        except Exception:
            _mark_ambiguous(
                connection,
                config,
                application,
                operation,
                resource_type,
                phase=f"remove_{resource_type}",
                provider_id=resource.provider_id,
                provider_name=resource.provider_name,
                message=f"{resource_type} absence or environment-key removal was not confirmed",
            )
        db.set_environment_keys(
            connection,
            application_id=application.application_id,
            owner=storage_owner(resource_type, checked_name),
            keys=(),
        )
        db.delete_managed_resource(
            connection,
            application_id=application.application_id,
            resource_type=resource_type,
            resource_name=checked_name,
        )
        completed.append(resource_type)
        recovering = False
        resources = _resources(connection, application.application_id)
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase=f"removed_{resource_type}",
            refs={
                "selected": list(selected),
                "completed": completed,
                "purge_s3": purge_s3,
            },
            merge_refs=True,
        )
    return _complete_operation(
        connection,
        operation,
        selected,
        completed,
        extra_refs={"purge_s3": purge_s3, "resource_name": checked_name},
    )
