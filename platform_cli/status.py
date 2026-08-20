"""Compact, safe read models for the M1 operator CLI.

The public functions in this module are the read-side integration interface:

``status_show(connection, *, observe_infrastructure=None,
observe_application=None, observe_storage=None)``
    Return an aggregate platform summary.
``infra_list``
    Return accepted role-image selections plus a small live health projection.
``app_list`` / ``app_show``
    Return accepted application state plus a small scheduler/route projection.
``storage_list`` / ``storage_show``
    Return accepted resource quotas plus a small non-mutating health projection.
Live dependencies are deliberately injected as one-item callables.  They must
return the frozen observation records below, never raw OpenStack, Nomad, or
storage responses.  Observer failure is represented as ``available: false``
and does not hide accepted database state or prevent other items from being
read.  These functions perform no database writes.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from . import db, openstack, remote, runtime
from .config import Config, PlatformConfig
from .storage_contract import RESOURCE_TYPES, canonical_secret_keys

ROLES = ("admin", "builder", "ingress", "storage", "worker")
_RESOURCE_TYPES = RESOURCE_TYPES
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SAFE_PROVIDER_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,252}")
_HEALTH = {"healthy", "unhealthy", "unknown"}
_INFRA_STATES = {"active", "building", "error", "missing", "stopped", "unknown"}
_SCHEDULER_STATES = {"dead", "pending", "running", "stopped", "unknown"}


@dataclass(frozen=True, slots=True)
class InfrastructureObservation:
    """Allowlisted live projection produced by an infrastructure observer."""

    role: str
    state: Literal["active", "building", "error", "missing", "stopped", "unknown"]
    health: Literal["healthy", "unhealthy", "unknown"] = "unknown"
    checked_at: str | None = None


@dataclass(frozen=True, slots=True)
class ApplicationObservation:
    """Allowlisted projection of one Nomad job and its public route."""

    application_id: str
    scheduler_state: Literal["dead", "pending", "running", "stopped", "unknown"]
    allocation_healthy: bool | None = None
    route_healthy: bool | None = None
    checked_at: str | None = None
    scheduler_available: bool = True
    route_available: bool = True


@dataclass(frozen=True, slots=True)
class StorageObservation:
    """Allowlisted usage projection; it contains no names, rows, or objects."""

    application_id: str
    resource_type: Literal["postgres", "mongo", "s3"]
    resource_name: str = "default"
    health: Literal["healthy", "unhealthy", "unknown"] = "unknown"
    used_bytes: int | None = None
    object_count: int | None = None
    connections: int | None = None
    checked_at: str | None = None


InfrastructureObserver = Callable[[str], InfrastructureObservation]
ApplicationObserver = Callable[[str], ApplicationObservation]
StorageObserver = Callable[[str, str, str], StorageObservation]


@dataclass(frozen=True, slots=True)
class LiveObservers:
    """Concrete safe observers ready to pass to the read-model functions."""

    infrastructure: InfrastructureObserver
    application: ApplicationObserver
    storage: StorageObserver


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_name(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_NAME.fullmatch(value) else None


def _safe_provider_identity(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_PROVIDER_IDENTITY.fullmatch(value) else None


def _safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    # Timestamps are presentation evidence, not provider text.  Keep the
    # accepted UTC shape narrow so an observer cannot smuggle arbitrary data.
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value):
        return None
    return value


def _safe_public_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 512:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value


def _nonnegative(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _infra_observation(role: str, observer: InfrastructureObserver | None) -> dict[str, object]:
    unavailable: dict[str, object] = {
        "available": False,
        "state": "unknown",
        "health": "unknown",
        "checkedAt": None,
    }
    if observer is None:
        return unavailable
    try:
        observed = observer(role)
        if not isinstance(observed, InfrastructureObservation) or observed.role != role:
            return unavailable
        state = observed.state if observed.state in _INFRA_STATES else "unknown"
        health = observed.health if observed.health in _HEALTH else "unknown"
        return {
            "available": True,
            "state": state,
            "health": health,
            "checkedAt": _safe_timestamp(observed.checked_at),
        }
    except Exception:
        return unavailable


def _application_observation(
    application_id: str, observer: ApplicationObserver | None
) -> dict[str, object]:
    unavailable: dict[str, object] = {
        "available": False,
        "schedulerAvailable": False,
        "routeAvailable": False,
        "schedulerState": "unknown",
        "allocationHealthy": None,
        "routeHealthy": None,
        "checkedAt": None,
    }
    if observer is None:
        return unavailable
    try:
        observed = observer(application_id)
        if (
            not isinstance(observed, ApplicationObservation)
            or observed.application_id != application_id
        ):
            return unavailable
        scheduler_state = (
            observed.scheduler_state if observed.scheduler_state in _SCHEDULER_STATES else "unknown"
        )
        scheduler_available = observed.scheduler_available is True
        route_available = observed.route_available is True
        return {
            "available": scheduler_available or route_available,
            "schedulerAvailable": scheduler_available,
            "routeAvailable": route_available,
            "schedulerState": scheduler_state if scheduler_available else "unknown",
            "allocationHealthy": (
                observed.allocation_healthy
                if scheduler_available and isinstance(observed.allocation_healthy, bool)
                else None
            ),
            "routeHealthy": (
                observed.route_healthy
                if route_available and isinstance(observed.route_healthy, bool)
                else None
            ),
            "checkedAt": _safe_timestamp(observed.checked_at),
        }
    except Exception:
        return unavailable


def _storage_observation(
    application_id: str,
    resource_type: str,
    resource_name: str,
    observer: StorageObserver | None,
) -> dict[str, object]:
    unavailable: dict[str, object] = {
        "available": False,
        "health": "unknown",
        "usedBytes": None,
        "objectCount": None,
        "connections": None,
        "checkedAt": None,
    }
    if observer is None:
        return unavailable
    try:
        observed = observer(application_id, resource_type, resource_name)
        if (
            not isinstance(observed, StorageObservation)
            or observed.application_id != application_id
            or observed.resource_type != resource_type
            or observed.resource_name != resource_name
        ):
            return unavailable
        return {
            "available": True,
            "health": observed.health if observed.health in _HEALTH else "unknown",
            "usedBytes": _nonnegative(observed.used_bytes),
            "objectCount": _nonnegative(observed.object_count),
            "connections": _nonnegative(observed.connections),
            "checkedAt": _safe_timestamp(observed.checked_at),
        }
    except Exception:
        return unavailable


def infrastructure_observer(
    platform: PlatformConfig,
    connection: sqlite3.Connection,
    *,
    timeout_seconds: float = 30,
    command_runner: Callable[..., Any] = runtime.run,
) -> InfrastructureObserver:
    """Observe persistent hosts through the typed OpenStack safety boundary.

    Worker and builder state is not guessed from Nova.  The former is reported
    through application/Nomad observations; the latter is projected only from
    a durable unfinished build operation.
    """
    hosts: dict[str, openstack.PersistentHost] = {}
    provider_available = True
    try:
        hosts = {
            host.role: host
            for host in openstack.list_persistent_hosts(
                platform,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
            )
        }
    except Exception:
        provider_available = False

    unfinished = incomplete_operations(connection)
    builders = [item for item in unfinished if item["builder"]]

    def observe(role: str) -> InfrastructureObservation:
        if role in openstack.PERSISTENT_ROLES:
            if not provider_available or role not in hosts:
                raise RuntimeError("infrastructure observation unavailable")
            host = hosts[role]
            states = {
                None: "missing",
                "ACTIVE": "active",
                "BUILD": "building",
                "ERROR": "error",
                "SHUTOFF": "stopped",
                "STOPPED": "stopped",
            }
            state = states.get(host.status, "unknown")
            # Nova power/build state is not role health.  Only an independent,
            # role-aware service observer may claim healthy or unhealthy.
            return InfrastructureObservation(role, state, "unknown", _now())  # type: ignore[arg-type]
        if role == "builder" and builders:
            checked_at = max(str(item["updatedAt"]) for item in builders)
            return InfrastructureObservation(role, "building", "unknown", checked_at)
        raise RuntimeError("typed live observation is unavailable for this role")

    return observe


def _configured_helper_caller(
    config: Config, helper_caller: Callable[..., Mapping[str, Any]] | None
) -> Callable[..., Mapping[str, Any]]:
    if helper_caller is not None:
        return helper_caller
    root = config.platform.get("paths.root")
    if not isinstance(root, str):
        raise RuntimeError("configured helper path is unavailable")
    helper_command = remote.helper_command_path(root)

    def call(action: str, args: Mapping[str, Any], **bounds: Any) -> Mapping[str, Any]:
        return remote.call_helper(action, args, helper_command=helper_command, **bounds)

    return call


def application_observer(
    connection: sqlite3.Connection,
    config: Config,
    *,
    helper_caller: Callable[..., Mapping[str, Any]] | None = None,
    http_get: Callable[..., runtime.HttpResult] = runtime.bounded_http,
) -> ApplicationObserver:
    """Compose bounded Nomad-helper and credential-free route observations."""
    helper_caller = _configured_helper_caller(config, helper_caller)
    applications = {item.application_id: item for item in db.list_applications(connection)}
    deployments = {
        application_id: deployment
        for application_id in applications
        if (deployment := db.get_deployment(connection, application_id)) is not None
    }

    def observe(application_id: str) -> ApplicationObservation:
        application = applications.get(application_id)
        if application is None:
            raise RuntimeError("application observation unavailable")
        scheduler_available = False
        scheduler_state = "unknown"
        allocation_healthy: bool | None = None
        deployment = deployments.get(application_id)
        if deployment is not None:
            try:
                result = helper_caller(
                    "app.health",
                    {
                        "slug": application.slug,
                        "version": deployment.nomad_version,
                        "candidateJobSha256": deployment.nomad_job_sha256,
                        "candidateImage": deployment.image_digest,
                    },
                    timeout_seconds=config.policy.limits.helper_seconds,
                    request_limit=config.policy.limits.helper_request_bytes,
                    response_limit=config.policy.limits.helper_response_bytes,
                    stderr_limit=config.policy.limits.stderr_bytes,
                )
                healthy = result.get("healthy")
                terminal = result.get("terminal")
                if not isinstance(healthy, bool) or not isinstance(terminal, bool):
                    raise ValueError("invalid helper projection")
                scheduler_available = True
                allocation_healthy = healthy
                scheduler_state = "running" if healthy else "dead" if terminal else "pending"
            except Exception:
                pass
        elif not application.desired_running:
            scheduler_available = True
            scheduler_state = "stopped"

        route_available = False
        route_healthy: bool | None = None
        if application.url is not None:
            try:
                response = http_get(
                    application.url,
                    timeout_seconds=config.policy.limits.http_seconds,
                    response_limit=65_536,
                    allow_redirects=False,
                )
                route_available = True
                route_healthy = 200 <= response.status < 300
            except Exception:
                pass
        return ApplicationObservation(
            application_id,
            scheduler_state,  # type: ignore[arg-type]
            allocation_healthy,
            route_healthy,
            _now(),
            scheduler_available,
            route_available,
        )

    return observe


def storage_observer(
    connection: sqlite3.Connection,
    config: Config,
    *,
    helper_caller: Callable[..., Mapping[str, Any]] | None = None,
) -> StorageObserver:
    """Compose accepted identities with non-mutating scoped health observation."""
    helper_caller = _configured_helper_caller(config, helper_caller)
    applications = {item.application_id: item for item in db.list_applications(connection)}
    resources = {
        (item.application_id, item.resource_type, item.resource_name): item
        for item in db.list_managed_resources(connection)
    }

    def observe(
        application_id: str, resource_type: str, resource_name: str = "default"
    ) -> StorageObservation:
        application = applications.get(application_id)
        resource = resources.get((application_id, resource_type, resource_name))
        if application is None or resource is None:
            raise RuntimeError("storage observation unavailable")
        health = "unknown"
        helper_available = False
        if resource.provider_id is not None:
            try:
                result = helper_caller(
                    f"storage.{resource_type}.observe",
                    {
                        "applicationId": application_id,
                        "applicationSlug": application.slug,
                        "providerId": resource.provider_id,
                        "providerName": resource.provider_name,
                        "resourceName": resource.resource_name,
                    },
                    timeout_seconds=config.policy.limits.helper_seconds,
                    request_limit=config.policy.limits.helper_request_bytes,
                    response_limit=config.policy.limits.helper_response_bytes,
                    stderr_limit=config.policy.limits.stderr_bytes,
                )
                if (
                    set(result) != {"observed", "keyNames", "modifyIndex"}
                    or result.get("keyNames")
                    != list(canonical_secret_keys(resource_type, resource_name))
                    or isinstance(result.get("modifyIndex"), bool)
                    or not isinstance(result.get("modifyIndex"), int)
                    or result["modifyIndex"] < 0
                    or not isinstance(result.get("observed"), bool)
                ):
                    raise ValueError("invalid storage observation projection")
                helper_available = True
                health = "healthy" if result["observed"] is True else "unhealthy"
            except Exception:
                pass
        if not helper_available:
            raise RuntimeError("storage observation unavailable")
        return StorageObservation(
            application_id,
            resource_type,  # type: ignore[arg-type]
            resource_name,
            health,  # type: ignore[arg-type]
            None,
            None,
            None,
            _now(),
        )

    return observe


def live_observers(
    connection: sqlite3.Connection,
    config: Config,
    *,
    command_runner: Callable[..., Any] = runtime.run,
    helper_caller: Callable[..., Mapping[str, Any]] | None = None,
    http_get: Callable[..., runtime.HttpResult] = runtime.bounded_http,
) -> LiveObservers:
    """Build the three concrete live observers used by CLI read commands."""
    return LiveObservers(
        infrastructure_observer(
            config.platform,
            connection,
            timeout_seconds=config.policy.limits.process_seconds,
            command_runner=command_runner,
        ),
        application_observer(connection, config, helper_caller=helper_caller, http_get=http_get),
        storage_observer(connection, config, helper_caller=helper_caller),
    )


def incomplete_operations(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """Project unfinished operations without refs, errors, or provider payloads."""
    scopes = [
        "infrastructure",
        *(f"app-{item.application_id}" for item in db.list_applications(connection)),
    ]
    result: list[dict[str, object]] = []
    for scope in scopes:
        operation = db.get_unfinished_operation(connection, scope)
        if operation is None:
            continue
        builder = "build" in operation.kind or operation.phase.startswith("build")
        result.append(
            {
                "operationId": operation.operation_id,
                "scope": operation.scope,
                "kind": operation.kind,
                "status": operation.status,
                "phase": operation.phase,
                "updatedAt": _safe_timestamp(operation.updated_at),
                "deadlineAt": _safe_timestamp(operation.deadline_at),
                "builder": builder,
            }
        )
    return result


def infra_list(
    connection: sqlite3.Connection,
    *,
    observe: InfrastructureObserver | None = None,
) -> list[dict[str, object]]:
    """List all fixed roles without exposing server, port, or image documents."""
    selected = {item.role: item for item in db.list_image_selections(connection)}
    result: list[dict[str, object]] = []
    for role in ROLES:
        image = selected.get(role)
        result.append(
            {
                "role": role,
                "accepted": image is not None,
                "image": (
                    None
                    if image is None
                    else {
                        "displayName": _safe_name(image.display_name),
                        "sourceCommit": image.source_commit,
                        "selectedAt": _safe_timestamp(image.selected_at),
                    }
                ),
                "live": _infra_observation(role, observe),
            }
        )
    return result


def _application_model(
    connection: sqlite3.Connection,
    application: db.Application,
    observer: ApplicationObserver | None,
) -> dict[str, object]:
    deployment = db.get_deployment(connection, application.application_id)
    return {
        "applicationId": application.application_id,
        "slug": application.slug,
        "desiredRunning": application.desired_running,
        "url": _safe_public_url(application.url),
        "sizing": {
            "workerFlavor": _safe_name(application.worker_flavor),
            "cpuMHz": application.scheduler_cpu_mhz,
            "memoryMiB": application.scheduler_memory_mib,
        },
        "deployment": (
            None
            if deployment is None
            else {
                "sourceCommit": deployment.source_commit,
                "imageDigest": deployment.image_digest,
                "nomadVersion": deployment.nomad_version,
                "acceptedAt": _safe_timestamp(deployment.accepted_at),
                "lastHealthyAt": _safe_timestamp(deployment.last_healthy_at),
            }
        ),
        "live": _application_observation(application.application_id, observer),
    }


def app_list(
    connection: sqlite3.Connection,
    *,
    observe: ApplicationObserver | None = None,
) -> list[dict[str, object]]:
    """List compact accepted application records and independent live health."""
    return [
        _application_model(connection, application, observe)
        for application in db.list_applications(connection)
    ]


def app_show(
    connection: sqlite3.Connection,
    identifier: str,
    *,
    observe: ApplicationObserver | None = None,
) -> dict[str, object] | None:
    """Show an application by UUID or slug without provider or Nomad details."""
    application = db.get_application(connection, identifier)
    if application is None:
        return None
    return _application_model(connection, application, observe)


def _storage_model(
    resource: db.ManagedResource,
    application: db.Application,
    observer: StorageObserver | None,
) -> dict[str, object]:
    quotas: dict[str, object] = {}
    if resource.postgres_connections is not None:
        quotas["connections"] = resource.postgres_connections
    if resource.measured_target_bytes is not None:
        quotas["measuredBytes"] = resource.measured_target_bytes
    if resource.s3_bytes is not None:
        quotas["bytes"] = resource.s3_bytes
    if resource.s3_objects is not None:
        quotas["objects"] = resource.s3_objects
    enforcement: dict[str, str] = {}
    if resource.postgres_connections is not None:
        enforcement["connections"] = "hard"
    if resource.measured_target_bytes is not None:
        enforcement["measuredBytes"] = "report-target"
    if resource.s3_bytes is not None:
        enforcement["bytes"] = "hard"
    if resource.s3_objects is not None:
        enforcement["objects"] = "hard"
    return {
        "applicationId": application.application_id,
        "slug": application.slug,
        "type": resource.resource_type,
        "name": resource.resource_name,
        "providerId": _safe_provider_identity(resource.provider_id),
        "providerName": _safe_provider_identity(resource.provider_name),
        "lifecycleState": resource.lifecycle_state,
        "quotas": quotas,
        "quotaEnforcement": enforcement,
        "lastVerifiedAt": _safe_timestamp(resource.last_verified_at),
        "live": _storage_observation(
            application.application_id, resource.resource_type, resource.resource_name, observer
        ),
    }


def storage_list(
    connection: sqlite3.Connection,
    *,
    application_identifier: str | None = None,
    observe: StorageObserver | None = None,
) -> list[dict[str, object]]:
    """List named managed-resource quota/usage projections; never contents."""
    application_id: str | None = None
    if application_identifier is not None:
        application = db.get_application(connection, application_identifier)
        if application is None:
            return []
        application_id = application.application_id
    applications = {item.application_id: item for item in db.list_applications(connection)}
    result: list[dict[str, object]] = []
    for resource in db.list_managed_resources(connection, application_id=application_id):
        application = applications.get(resource.application_id)
        if application is not None and resource.resource_type in _RESOURCE_TYPES:
            result.append(_storage_model(resource, application, observe))
    return result


def storage_show(
    connection: sqlite3.Connection,
    application_identifier: str,
    resource_type: str,
    resource_name: str = "default",
    *,
    observe: StorageObserver | None = None,
) -> dict[str, object] | None:
    """Show one app resource, or ``None`` when the app/resource is absent."""
    if resource_type not in _RESOURCE_TYPES:
        return None
    resources = storage_list(
        connection,
        application_identifier=application_identifier,
        observe=observe,
    )
    return next(
        (
            item
            for item in resources
            if item["type"] == resource_type and item["name"] == resource_name
        ),
        None,
    )


def status_show(
    connection: sqlite3.Connection,
    *,
    observe_infrastructure: InfrastructureObserver | None = None,
    observe_application: ApplicationObserver | None = None,
    observe_storage: StorageObserver | None = None,
) -> dict[str, object]:
    """Return a partial-availability summary assembled from the safe reads."""
    infrastructure = infra_list(connection, observe=observe_infrastructure)
    applications = app_list(connection, observe=observe_application)
    resources = storage_list(connection, observe=observe_storage)
    operations = incomplete_operations(connection)
    # Before the helper release and before any application exists, only the
    # three persistent foundation hosts are expected live dependencies.  A
    # disposable builder is expected only while its durable build operation is
    # unfinished; worker resources are represented by application observations.
    expected_infrastructure = [
        item
        for item in infrastructure
        if item["role"] in openstack.PERSISTENT_ROLES
        or (item["role"] == "builder" and any(item["builder"] for item in operations))
    ]
    live = [item["live"] for item in [*expected_infrastructure, *applications, *resources]]
    available = sum(1 for item in live if isinstance(item, dict) and item["available"])
    unhealthy = sum(
        1
        for item in live
        if isinstance(item, dict)
        and (
            item.get("health") == "unhealthy"
            or item.get("allocationHealthy") is False
            or item.get("routeHealthy") is False
            or item.get("state") == "error"
        )
    )
    state = (
        "healthy" if available == len(live) and unhealthy == 0 and not operations else "degraded"
    )
    return {
        "state": state,
        "accepted": {
            "infrastructureRoles": sum(1 for item in infrastructure if item["accepted"]),
            "applications": len(applications),
            "storageResources": len(resources),
        },
        "observations": {
            "available": available,
            "unavailable": len(live) - available,
            "unhealthy": unhealthy,
        },
        "operations": {
            "incomplete": len(operations),
            "builders": sum(1 for item in operations if item["builder"]),
            "items": operations,
        },
    }


def status_show_live(
    connection: sqlite3.Connection,
    config: Config,
    *,
    command_runner: Callable[..., Any] = runtime.run,
    helper_caller: Callable[..., Mapping[str, Any]] | None = None,
    http_get: Callable[..., runtime.HttpResult] = runtime.bounded_http,
) -> dict[str, object]:
    """Concrete CLI composition for accepted state plus bounded live reads."""
    observers = live_observers(
        connection,
        config,
        command_runner=command_runner,
        helper_caller=helper_caller,
        http_get=http_get,
    )
    return status_show(
        connection,
        observe_infrastructure=observers.infrastructure,
        observe_application=observers.application,
        observe_storage=observers.storage,
    )
