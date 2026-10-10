"""Authoritative resource limits, cached usage, and journaled reconciliation."""

from __future__ import annotations

import logging
import sqlite3
import uuid as uuid_module
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import runtime
from ..config import Config
from ..validation import ValidationError, uuid
from . import application_runtime as app
from . import database as db
from .service_support import HelperCaller, operation_deadline, reject_deleting, wall_deadline
from .storage_capacity import disk_reservations, retained_worker, workload_job
from .storage_contract import validate_expected_quotas, validate_quotas

LIMITS_KIND = "storage.limits.set"
COLLECT_KIND = "storage.usage.collect"
REPAIR_KIND = "storage.postgres.repair"
COLLECT_SECONDS = 300
STALE_SECONDS = 900
_LOG = logging.getLogger(__name__)


def quotas(resource: db.ManagedResource) -> dict[str, int]:
    if resource.resource_type == "s3":
        return {"s3Bytes": int(resource.s3_bytes or 0), "s3Objects": int(resource.s3_objects or 0)}
    return {
        "connections": int(resource.postgres_connections or resource.mongo_connections or 10),
        "sizeBytes": int(resource.measured_target_bytes or 0),
        "memoryBytes": int(resource.memory_bytes or 536870912),
        "cpuMillicores": int(resource.cpu_millicores or 500),
    }


def stale(measured_at: object) -> bool:
    if not isinstance(measured_at, str):
        return True
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(measured_at)).total_seconds()
        return age > STALE_SECONDS or age < -60
    except (ValueError, TypeError):
        return True


def usage_model(resource: db.ManagedResource) -> dict[str, object]:
    return {
        **{
            name: resource.usage.get(name)
            for name in (
                "usedBytes",
                "objectCount",
                "currentConnections",
                "instanceMemoryBytes",
                "cpuTimeMilliseconds",
                "measuredAt",
            )
        },
        "stale": stale(resource.usage.get("measuredAt")),
    }


def block_model(resource: db.ManagedResource) -> dict[str, object]:
    return {
        "blocked": resource.write_blocked,
        "reason": "size_limit_exceeded" if resource.write_blocked else None,
        "since": resource.blocked_since,
    }


def host_model(connection: sqlite3.Connection) -> dict[str, Any] | None:
    usage = db.get_storage_host_usage(connection)
    return None if usage is None else {**usage, "stale": stale(usage.get("measuredAt"))}


def _usage(result: Mapping[str, object]) -> tuple[dict[str, Any], bool]:
    value = result.get("usage")
    blocked = result.get("writeBlocked")
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "usedBytes",
            "objectCount",
            "currentConnections",
            "instanceMemoryBytes",
            "cpuTimeMilliseconds",
            "measuredAt",
        }
        or not isinstance(blocked, bool)
    ):
        raise ValidationError("storage usage evidence is invalid")
    for name in (
        "usedBytes",
        "objectCount",
        "currentConnections",
        "instanceMemoryBytes",
        "cpuTimeMilliseconds",
    ):
        number = value[name]
        if number is None and name != "usedBytes":
            continue
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ValidationError("storage usage evidence is invalid")
    if stale(value["measuredAt"]):
        raise ValidationError("storage usage evidence is stale")
    return value, blocked


class StorageLimitsService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
        *,
        helper_caller: HelperCaller,
    ):
        self.connection = connection
        self.config = config
        self.state_directory = state_directory
        self.helper_caller = helper_caller

    def _resource(self, identifier: str) -> db.ManagedResource:
        resource = db.get_managed_resource(self.connection, identifier)
        if resource is None:
            raise ValidationError("storage resource does not exist")
        return resource

    def _call(
        self,
        resource: db.ManagedResource,
        action: str,
        deadline: float,
        *,
        target: dict[str, int] | None = None,
        operation_id: str | None = None,
        recovering: bool = False,
    ) -> Mapping[str, object]:
        application = db.get_application(self.connection, resource.application_id)
        if application is None:
            raise ValidationError("application does not exist")
        args: dict[str, object] = {
            "applicationId": application.application_id,
            "applicationSlug": application.slug,
            "resourceName": resource.resource_name,
            "providerId": resource.provider_id,
            "providerName": resource.provider_name,
            "resourceId": resource.resource_id,
            "reservations": disk_reservations(self.connection),
            "workloadJobId": workload_job(self.connection, resource.application_id),
            "retainedWorker": retained_worker(self.connection, resource.application_id),
            "instanceId": resource.instance_id,
            "instanceQuotas": quotas(resource) if resource.resource_type != "s3" else None,
            "workerIds": [
                resource.application_id,
                *app.deployment_worker_ids(resource.application_id),
            ],
        }
        if target is not None:
            args.update(quotas=target, operationId=operation_id, recover=recovering)
        return self.helper_caller(
            self.config, f"storage.{resource.resource_type}.{action}", args, deadline=deadline
        )

    def select(
        self,
        resource_id: str,
        target: dict[str, int],
        expected: dict[str, int],
        *,
        request_id: str,
        collecting: bool = False,
    ) -> None:
        resource = self._resource(resource_id)
        target = validate_quotas(resource.resource_type, target)
        expected = validate_expected_quotas(resource.resource_type, expected)
        accepted = quotas(resource)
        if (
            resource.resource_type != "s3"
            and resource.instance_id is None
            and (
                any(target[name] != accepted[name] for name in ("memoryBytes", "cpuMillicores"))
                or target["connections"] > accepted["connections"]
                or resource.resource_type == "mongo"
                and target["connections"] != accepted["connections"]
            )
        ):
            raise ValidationError(
                "instance migration is required before changing compute/connection caps"
            )
        request_id = uuid(request_id, field="storage operation ID")
        deadline = operation_deadline(self.config)
        scope = f"app-{resource.application_id}"
        kind = COLLECT_KIND if collecting else LIMITS_KIND
        intent = {"resource_id": resource_id, "quotas": target, "expected_quotas": expected}
        with runtime.lock(self.state_directory, scope, wait=not collecting, deadline=deadline):
            resource = self._resource(resource_id)
            application = db.get_application(self.connection, resource.application_id)
            assert application is not None
            reject_deleting(self.connection, application)
            operation = db.get_unfinished_operation(self.connection, scope)
            recovering = operation is not None
            if operation is not None:
                if (
                    operation.operation_id != request_id
                    or operation.kind != kind
                    or any(operation.refs.get(key) != value for key, value in intent.items())
                ):
                    raise db.UnfinishedOperationError(scope, operation.operation_id, operation.kind)
                operation = db.renew_operation_deadline(
                    self.connection, request_id, wall_deadline(deadline)
                )
            else:
                if resource.lifecycle_state != "active":
                    raise ValidationError("storage resource must be active")
                if quotas(resource) != expected:
                    raise ValidationError(
                        "storage limits changed; read current limits and submit a new request"
                    )
                operation = db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind=kind,
                    scope=scope,
                    phase="validated",
                    deadline_at=wall_deadline(deadline),
                    refs=intent,
                )
            try:
                if operation.phase not in {"validated", "applying", "applied"}:
                    raise db.DatabaseError("storage limits recovery phase is invalid")
                if quotas(resource) != expected and not (
                    operation.phase == "applied" and quotas(resource) == target
                ):
                    raise db.DatabaseError("accepted storage limits changed during reconciliation")
                if operation.phase != "applied":
                    db.checkpoint_operation(self.connection, request_id, phase="applying")
                    result = self._call(
                        resource,
                        "limits",
                        deadline,
                        target=target,
                        operation_id=request_id,
                        recovering=recovering,
                    )
                    if result.get("applied") is not True:
                        raise ValidationError("storage limit assignment was not confirmed")
                    usage, blocked = _usage(result)
                    db.checkpoint_operation(
                        self.connection,
                        request_id,
                        phase="applied",
                        refs={**intent, "usage": usage, "write_blocked": blocked},
                    )
                else:
                    usage = operation.refs["usage"]
                    blocked = operation.refs["write_blocked"]
                db.put_storage_limits(self.connection, resource_id, target, usage, blocked=blocked)
                db.mark_succeeded(self.connection, request_id, cleanup_state="not_required")
            except Exception:
                db.mark_recovery_required(
                    self.connection, request_id, "storage limit assignment requires reconciliation"
                )
                raise

    def repair_postgres(self, *, request_id: str) -> None:
        deadline = operation_deadline(self.config)
        with runtime.lock(self.state_directory, "infrastructure", wait=True, deadline=deadline):
            operation = db.get_unfinished_operation(self.connection, "infrastructure")
            recovering = operation is not None
            if operation is not None:
                if operation.operation_id != request_id or operation.kind != REPAIR_KIND:
                    raise db.UnfinishedOperationError(
                        "infrastructure", operation.operation_id, operation.kind
                    )
                operation = db.renew_operation_deadline(
                    self.connection, request_id, wall_deadline(deadline)
                )
            else:
                resources = [
                    item.resource_id
                    for item in db.list_managed_resources(self.connection)
                    if item.resource_type == "postgres"
                ]
                operation = db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind=REPAIR_KIND,
                    scope="infrastructure",
                    phase="repairing",
                    deadline_at=wall_deadline(deadline),
                    refs={"resources": resources, "completed": []},
                )
            completed = list(operation.refs["completed"])
            try:
                for identifier in operation.refs["resources"]:
                    if identifier in completed:
                        continue
                    resource = db.get_managed_resource(self.connection, identifier)
                    if resource is not None:
                        scope = f"app-{resource.application_id}"
                        with runtime.lock(
                            self.state_directory, scope, wait=True, deadline=deadline
                        ):
                            pending = db.get_unfinished_operation(self.connection, scope)
                            if pending is not None:
                                raise db.UnfinishedOperationError(
                                    scope, pending.operation_id, pending.kind
                                )
                            resource = self._resource(identifier)
                            if resource.lifecycle_state != "active":
                                raise ValidationError("PostgreSQL repair requires active resources")
                            result = self._call(
                                resource,
                                "limits",
                                deadline,
                                target=quotas(resource),
                                operation_id=request_id,
                                recovering=recovering,
                            )
                            if result.get("applied") is not True:
                                raise ValidationError("PostgreSQL repair was not confirmed")
                    completed.append(identifier)
                    db.checkpoint_operation(
                        self.connection,
                        request_id,
                        phase="repairing",
                        refs={"completed": completed},
                        merge_refs=True,
                    )
                db.mark_succeeded(self.connection, request_id, cleanup_state="not_required")
            except Exception:
                db.mark_recovery_required(
                    self.connection, request_id, "PostgreSQL role repair requires reconciliation"
                )
                raise

    def collect(self) -> None:
        for resource in db.list_managed_resources(self.connection):
            if resource.lifecycle_state != "active":
                continue
            try:
                if resource.resource_type == "mongo":
                    pending = db.get_unfinished_operation(
                        self.connection, f"app-{resource.application_id}"
                    )
                    if (
                        pending is not None
                        and pending.kind == COLLECT_KIND
                        and pending.refs.get("resource_id") == resource.resource_id
                    ):
                        self.select(
                            resource.resource_id,
                            pending.refs["quotas"],
                            pending.refs["expected_quotas"],
                            request_id=pending.operation_id,
                            collecting=True,
                        )
                    elif pending is None:
                        self.select(
                            resource.resource_id,
                            quotas(resource),
                            quotas(resource),
                            request_id=str(uuid_module.uuid4()),
                            collecting=True,
                        )
                else:
                    deadline = operation_deadline(self.config)
                    with runtime.lock(
                        self.state_directory, f"app-{resource.application_id}", deadline=deadline
                    ):
                        if (
                            db.get_unfinished_operation(
                                self.connection, f"app-{resource.application_id}"
                            )
                            is not None
                        ):
                            continue
                        refreshed = self._resource(resource.resource_id)
                        result = self._call(refreshed, "usage", deadline)
                        usage, blocked = _usage(result)
                        db.put_storage_usage(
                            self.connection, refreshed.resource_id, usage, blocked=blocked
                        )
            except (
                runtime.LockBusy,
                db.UnfinishedOperationError,
                db.FinishingOperationConflictError,
            ):
                continue
            except Exception:
                with db.transaction(self.connection):
                    self.connection.execute(
                        "UPDATE managed_resources SET usage_error='collection_failed' WHERE resource_id=?",
                        (resource.resource_id,),
                    )
                _LOG.warning(
                    "storage usage collection failed for resource %s", resource.resource_id
                )
        try:
            result = self.helper_caller(
                self.config, "storage.host.observe", {}, deadline=operation_deadline(self.config)
            )
            db.put_storage_host_usage(self.connection, result)
        except Exception:
            _LOG.warning("storage host usage collection failed")
