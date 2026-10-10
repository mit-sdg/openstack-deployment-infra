"""Authoritative resource limits, cached usage, and journaled reconciliation."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
import uuid as uuid_module
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import runtime
from ..config import Config
from ..instance_contract import MIB, CapacityError
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
FAST_COLLECT_SECONDS = 60
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


def validate_reduction(resource: db.ManagedResource, target: Mapping[str, int]) -> None:
    if resource.resource_type == "s3" or target["sizeBytes"] >= (
        resource.measured_target_bytes or 0
    ):
        return
    used = resource.usage.get("usedBytes")
    if stale(resource.usage.get("measuredAt")) or not isinstance(used, int):
        raise CapacityError(
            "USAGE_NOT_FRESH", "a fresh usage sample is required before reducing size"
        )
    margin = (384 if resource.resource_type == "postgres" else 128) * MIB
    if target["sizeBytes"] < used + margin:
        raise CapacityError(
            "SIZE_BELOW_USAGE", "size must cover current usage plus database safety headroom"
        )


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
        self._next_due: dict[str, float] = {}
        self._next_host_due = 0.0

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
            "stagedInstanceId": resource.resource_id
            if resource.instance_id is None and resource.migration_state == "aborted"
            else None,
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
    ) -> None:
        resource = self._resource(resource_id)
        target = validate_quotas(resource.resource_type, target)
        expected = validate_expected_quotas(resource.resource_type, expected)
        accepted = quotas(resource)
        if (
            resource.resource_type != "s3"
            and resource.instance_id is None
            and (
                target["connections"] > accepted["connections"]
                or resource.resource_type == "mongo"
                and target["connections"] != accepted["connections"]
            )
        ):
            raise ValidationError(
                "instance migration is required before increasing shared connection caps"
            )
        request_id = uuid(request_id, field="storage operation ID")
        deadline = operation_deadline(self.config)
        scope = f"app-{resource.application_id}"
        kind = LIMITS_KIND
        intent = {"resource_id": resource_id, "quotas": target, "expected_quotas": expected}
        with runtime.lock(self.state_directory, scope, wait=True, deadline=deadline):
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
                validate_reduction(resource, target)
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
                    if resource.resource_type in {"postgres", "mongo"}:
                        self.quiesce_dns_job(resource.application_id, deadline)
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
                if resource.resource_type in {"postgres", "mongo"}:
                    self.prepare_dns_job(resource.application_id, deadline)
                db.put_storage_limits(self.connection, resource_id, target, usage, blocked=blocked)
                db.mark_succeeded(self.connection, request_id, cleanup_state="not_required")
            except Exception:
                db.mark_recovery_required(
                    self.connection, request_id, "storage limit assignment requires reconciliation"
                )
                raise

    def quiesce_dns_job(self, application_id: str, deadline: float) -> None:
        """Stop an old job before its binding changes, then redeploy it once."""
        from .nomad_jobs import storage_hosts_job

        deployment = db.get_deployment(self.connection, application_id)
        application = db.get_application(self.connection, application_id)
        if (
            deployment is None
            or application is None
            or not application.desired_running
            or storage_hosts_job(deployment.nomad_job, self.config.platform) == deployment.nomad_job
        ):
            return
        identity = app.nomad_candidate_identity(deployment.nomad_job)
        result = self.helper_caller(
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
            raise ValidationError("DNS mapping quiescence was not confirmed")

    def prepare_dns_job(self, application_id: str, deadline: float) -> None:
        """Redeploy the accepted mapping after binding publication has completed."""
        from .nomad_jobs import storage_hosts_job

        deployment = db.get_deployment(self.connection, application_id)
        application = db.get_application(self.connection, application_id)
        if deployment is None or application is None:
            return
        job = storage_hosts_job(deployment.nomad_job, self.config.platform)
        if job == deployment.nomad_job:
            return
        version = deployment.nomad_version
        if application.desired_running:
            result = self.helper_caller(
                self.config, "app.deploy", {"slug": application.slug, "job": job}, deadline=deadline
            )
            new_version = result.get("nomadVersion")
            if not isinstance(new_version, int) or isinstance(new_version, bool):
                raise ValidationError("DNS mapping deployment was not confirmed")
            version = new_version
        with db.transaction(self.connection):
            self.connection.execute(
                "UPDATE deployment_attempts SET nomad_job=?,nomad_job_sha256=?,nomad_version=? WHERE deployment_id=?",
                (job, hashlib.sha256(job.encode()).hexdigest(), version, deployment.deployment_id),
            )

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
                            if operation.refs.get("dns_pending_resource") != identifier:
                                self.quiesce_dns_job(resource.application_id, deadline)
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
                                operation = db.checkpoint_operation(
                                    self.connection,
                                    request_id,
                                    phase="repairing",
                                    refs={"dns_pending_resource": identifier},
                                    merge_refs=True,
                                )
                            self.prepare_dns_job(resource.application_id, deadline)
                    completed.append(identifier)
                    db.checkpoint_operation(
                        self.connection,
                        request_id,
                        phase="repairing",
                        refs={"completed": completed, "dns_pending_resource": None},
                        merge_refs=True,
                    )
                db.mark_succeeded(self.connection, request_id, cleanup_state="not_required")
            except Exception:
                db.mark_recovery_required(
                    self.connection, request_id, "PostgreSQL role repair requires reconciliation"
                )
                raise

    def collect(self, *, scheduled: bool = False, _resource_id: str | None = None) -> None:
        now = time.monotonic()
        resources = db.list_managed_resources(self.connection)
        selected = [
            resource.resource_id
            for resource in resources
            if resource.lifecycle_state == "active"
            and (not scheduled or now >= self._next_due.get(resource.resource_id, 0))
        ]
        path = self.connection.execute("PRAGMA database_list").fetchone()[2]
        if _resource_id is None and len(selected) > 1 and path:

            def sample(identifier: str) -> tuple[str, float]:
                connection = db.connect(Path(path))
                try:
                    worker = StorageLimitsService(
                        connection,
                        self.config,
                        self.state_directory,
                        helper_caller=self.helper_caller,
                    )
                    worker.collect(_resource_id=identifier)
                    return identifier, worker._next_due.get(identifier, now + FAST_COLLECT_SECONDS)
                finally:
                    connection.close()

            # Sixteen bounded provider probes keep 100 resources within a minute
            # even when many endpoints time out. Each thread owns its SQLite
            # connection and takes only its app's nonblocking runtime lock.
            with ThreadPoolExecutor(max_workers=16) as pool:
                for identifier, due in pool.map(sample, selected):
                    self._next_due[identifier] = due
            _resource_id = ""  # only the one host sample remains on this thread
        for resource in resources:
            if _resource_id is not None and resource.resource_id != _resource_id:
                continue
            if resource.lifecycle_state != "active" or (
                scheduled and now < self._next_due.get(resource.resource_id, 0)
            ):
                continue
            try:
                deadline = time.monotonic() + min(10, self.config.policy.limits.helper_seconds)
                with runtime.lock(
                    self.state_directory, f"app-{resource.application_id}", deadline=deadline
                ):
                    pending = db.get_unfinished_operation(
                        self.connection, f"app-{resource.application_id}"
                    )
                    if pending is not None and pending.kind == COLLECT_KIND:
                        db.mark_failed(
                            self.connection,
                            pending.operation_id,
                            "collector reconciliation is superseded by a new sample",
                            cleanup_state="not_required",
                        )
                        pending = None
                    if pending is not None:
                        continue
                    refreshed = self._resource(resource.resource_id)
                    # Derived role reconciliation is idempotent. It never owns a
                    # durable app reservation or changes accepted quota values.
                    mutate = refreshed.resource_type == "mongo"
                    result = self._call(
                        refreshed,
                        "limits" if mutate else "usage",
                        deadline,
                        target=quotas(refreshed) if mutate else None,
                        operation_id=str(uuid_module.uuid4()) if mutate else None,
                    )
                    usage, blocked = _usage(result)
                    db.put_storage_usage(
                        self.connection, resource.resource_id, usage, blocked=blocked
                    )
                    limit = (
                        refreshed.measured_target_bytes
                        if refreshed.resource_type != "s3"
                        else refreshed.s3_bytes
                    )
                    near = bool(limit and usage["usedBytes"] * 100 >= limit * 80)
                    self._next_due[resource.resource_id] = now + (
                        FAST_COLLECT_SECONDS if near or blocked else COLLECT_SECONDS
                    )
            except (
                runtime.LockBusy,
                db.UnfinishedOperationError,
                db.FinishingOperationConflictError,
            ):
                continue
            except Exception:
                self._next_due[resource.resource_id] = now + FAST_COLLECT_SECONDS
                with db.transaction(self.connection):
                    self.connection.execute(
                        "UPDATE managed_resources SET usage_error='collection_failed' WHERE resource_id=?",
                        (resource.resource_id,),
                    )
                _LOG.warning(
                    "storage usage collection failed for resource %s", resource.resource_id
                )
        if _resource_id not in {None, ""}:
            return
        if scheduled and now < self._next_host_due:
            return
        self._next_host_due = now + COLLECT_SECONDS
        try:
            result = self.helper_caller(
                self.config, "storage.host.observe", {}, deadline=operation_deadline(self.config)
            )
            db.put_storage_host_usage(self.connection, result)
        except Exception:
            _LOG.warning("storage host usage collection failed")
