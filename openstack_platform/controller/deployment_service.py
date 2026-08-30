"""Typed application deployment orchestration independent of CLI parsing and output."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid as uuid_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from .. import durable, openstack, remote, runtime
from ..config import Config
from ..contracts import REGISTRY_PORT
from ..validation import (
    ValidationError,
    bounded_text,
    commit,
    env_key,
    oci_digest_pin,
    relative_path,
    repository_url,
    sha256_hex,
    slug,
    uuid,
)
from . import application_runtime as app
from . import database as db
from .deployment_config import DeploymentConfiguration, branch_name
from .storage_contract import (
    PLATFORM_ENVIRONMENT_KEYS,
    canonical_secret_key,
    platform_environment_values,
)

_PLATFORM_ENVIRONMENT = PLATFORM_ENVIRONMENT_KEYS


class DeploymentDeadlineError(RuntimeError):
    """A deployment exhausted its whole-operation deadline."""


class HelperCaller(Protocol):
    def __call__(
        self,
        config: Config,
        action: str,
        args: Mapping[str, Any],
        *,
        deadline: float | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DeploymentRequest:
    application: str
    repository: str
    requested_ref: str
    source_commit: str
    configuration_revision: int
    configuration: DeploymentConfiguration
    request_id: str | None = None


RecoveryKind = Literal["candidate-removed", "accepted", "deployment-healthy"]


@dataclass(frozen=True, slots=True)
class DeploymentOutcome:
    application_slug: str
    image: str | None = None
    nomad_version: int | None = None
    recovered: RecoveryKind | None = None


@dataclass(frozen=True, slots=True)
class DeploymentRecovery:
    operation: db.Operation | None
    completed: RecoveryKind | None = None


def _default_helper(
    config: Config,
    action: str,
    args: Mapping[str, Any],
    *,
    deadline: float | None = None,
) -> Mapping[str, Any]:
    timeout = float(config.policy.limits.helper_seconds)
    if deadline is not None:
        timeout = _remaining(deadline, timeout)
    return remote.call_helper(
        action,
        args,
        timeout_seconds=timeout,
        request_limit=config.policy.limits.helper_request_bytes,
        response_limit=config.policy.limits.helper_response_bytes,
        stderr_limit=config.policy.limits.stderr_bytes,
        helper_command=remote.helper_command_path(config.platform.get("paths.root")),
    )


def _remaining(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeploymentDeadlineError("operation exceeded its whole-command deadline")
    return min(float(maximum), remaining)


def _wall_deadline(deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeploymentDeadlineError("operation exceeded its whole-command deadline")
    return (
        (datetime.now(UTC) + timedelta(seconds=remaining))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _ownership(connection: sqlite3.Connection, application_id: str) -> dict[str, str]:
    return {
        item.key_name: item.owner
        for item in db.list_environment_keys(connection, application_id=application_id)
    }


def _configuration_manifest(
    connection: sqlite3.Connection,
    application_id: str,
    configuration: DeploymentConfiguration,
) -> app.Manifest:
    resources = db.list_managed_resources(connection, application_id=application_id)
    active = {
        item.resource_id: (item.resource_type, item.resource_name)
        for item in resources
        if item.lifecycle_state == "active"
    }
    manifest = configuration.manifest(active)
    declared = {binding.resource_id for binding in configuration.storage_bindings}
    undeclared = [
        item
        for item in resources
        if item.lifecycle_state == "active" and item.resource_id not in declared
    ]
    if undeclared:
        raise ValidationError(
            f"active {undeclared[0].resource_type} storage {undeclared[0].resource_name!r} has no deployment binding"
        )
    return manifest


def _write_build_log(
    state_directory: Path,
    application_id: str,
    operation_id: str,
    source_commit: str,
    text: str,
) -> str:
    payload = text.encode("utf-8")
    root = runtime.ensure_private_directory(state_directory / "build-logs", create=True)
    directory = runtime.ensure_private_directory(root / application_id, create=True)
    name = f"{source_commit}-{operation_id}.log"
    destination = directory / name
    try:
        durable.atomic_write(
            destination,
            payload,
            mode=0o600,
            maximum_bytes=104_857_600,
        )
    except durable.DurableReplaceError as error:
        raise app.ApplicationError("build log could not be committed durably") from error
    return destination.relative_to(state_directory).as_posix()


def _apply_registry_retention(
    connection: sqlite3.Connection,
    config: Config,
    *,
    helper_caller: HelperCaller,
    application_id: str,
    application_slug: str,
    current_image: str,
    deadline: float | None = None,
) -> None:
    successful = list(db.list_application_successful_manifest_history(connection, application_id))
    if current_image in successful:
        successful.remove(current_image)
    successful.insert(0, current_image)
    all_images = db.list_application_manifest_images(connection, application_id)
    # Retention counts accepted deployments, while exposing other pushed
    # candidates for deletion unless an unfinished operation references them.
    history = [*successful, *(image for image in all_images if image not in successful)]
    references = list(db.list_active_application_manifest_references(connection, application_id))
    result = helper_caller(
        config,
        "app.manifest.retain",
        {
            "slug": application_slug,
            "history": history,
            "references": references,
        },
        deadline=deadline,
    )
    if not isinstance(result.get("deleted"), list) or not isinstance(result.get("protected"), list):
        raise app.ApplicationError("registry retention evidence was malformed")


def _prepare_deployment_build(
    connection: sqlite3.Connection,
    config: Config,
    *,
    helper_caller: HelperCaller,
    state_directory: Path,
    operation: db.Operation,
    application_id: str,
    application_slug: str,
    repository: str,
    requested_ref: str,
    source_commit: str,
    configuration_revision: int,
    configuration: DeploymentConfiguration,
    manifest: app.Manifest,
    deadline: float,
) -> app.DeploymentBuild:
    refs = dict(operation.refs)
    candidate = operation.candidate_digest
    if candidate is None:
        db.checkpoint_deployment_attempt(connection, operation.operation_id, status="building")
        with runtime.lock(state_directory, "infrastructure", deadline=deadline):
            builder_image = db.get_image_selection(connection, "builder")
            if builder_image is None:
                raise ValidationError("select a builder image before deployment")
            refs = {**refs, "builder_image_id": builder_image.image_id}
            db.checkpoint_operation(
                connection,
                operation.operation_id,
                phase="builder_creating",
                refs=refs,
            )
        built = helper_caller(
            config,
            "app.build",
            {
                "buildId": operation.operation_id,
                "slug": application_slug,
                "repository": repository,
                "requestedRef": requested_ref,
                "commit": source_commit,
                "configurationRevision": configuration_revision,
                "configuration": json.loads(configuration.canonical_json()),
                "builderImageId": builder_image.image_id,
                "runtimeImages": {
                    "bun": config.policy.runtime_images.bun,
                    "node": config.policy.runtime_images.node,
                },
                "sourceLimit": config.policy.limits.source_bytes,
                "buildLogLimit": config.policy.limits.build_log_bytes,
                "connectSeconds": config.policy.limits.connect_seconds,
                "deadlineAt": operation.deadline_at,
            },
            deadline=deadline,
        )
        if built.get("builderAbsent") is not True:
            raise app.ApplicationError("builder cleanup was not confirmed")
        candidate = oci_digest_pin(built.get("image"), field="built image")
        expected_repository = f"{config.platform.get('addresses.storage')}:{REGISTRY_PORT}/projects/{application_slug}/app@"
        if not candidate.startswith(expected_repository):
            raise app.ApplicationError(
                "build helper returned an image outside the application repository"
            )
        recipe = app.generate_recipe(manifest, config.policy.runtime_images)
        recipe_hash = sha256_hex(built.get("recipeHash"), field="recipe hash")
        if recipe_hash != recipe.sha256:
            raise app.ApplicationError(
                "build helper recipe identity did not match validated policy"
            )
        log = built.get("log")
        if not isinstance(log, str) or len(log.encode()) > config.policy.limits.build_log_bytes:
            raise app.ApplicationError("build helper returned invalid build log evidence")
        build_log_path = _write_build_log(
            state_directory,
            application_id,
            operation.operation_id,
            source_commit,
            log,
        )
        refs = {
            **refs,
            "recipe_hash": recipe_hash,
            "build_log_path": build_log_path,
        }
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase="image_pushed",
            refs=refs,
            candidate_digest=candidate,
            cleanup_state="confirmed",
        )
        db.checkpoint_deployment_attempt(
            connection,
            operation.operation_id,
            status="building",
            recipe_hash=recipe_hash,
            image_digest=candidate,
            build_log_path=build_log_path,
            cleanup_state="confirmed",
        )
    else:
        recipe_hash = sha256_hex(refs.get("recipe_hash"), field="recipe hash")
        build_log_path = relative_path(
            refs.get("build_log_path"), field="build log path", allow_dot=False
        )
    return app.DeploymentBuild(candidate, manifest, recipe_hash, build_log_path, refs)


def _record_platform_environment_ownership(
    connection: sqlite3.Connection,
    application_id: str,
    key_names: Sequence[str],
) -> None:
    """Transfer canonical key names to platform ownership without losing others."""
    claimed = {env_key(name) for name in key_names}
    existing = db.list_environment_keys(connection, application_id=application_id)
    for owner in sorted({item.owner for item in existing} - {"platform"}):
        owned = {item.key_name for item in existing if item.owner == owner}
        if owned & claimed:
            db.set_environment_keys(
                connection,
                application_id=application_id,
                owner=owner,
                keys=sorted(owned - claimed),
            )
    db.set_environment_keys(
        connection,
        application_id=application_id,
        owner="platform",
        keys=sorted(claimed),
    )


def _environment_update_accepted(result: Mapping[str, Any], *, allow_stopped: bool) -> bool:
    evidence = (
        result.get("restarted"),
        result.get("schedulerHealthy"),
        result.get("publicHealthy"),
    )
    return evidence == (True, True, True) or (allow_stopped and evidence == (False, False, False))


def _prepare_platform_environment(
    connection: sqlite3.Connection,
    config: Config,
    *,
    helper_caller: HelperCaller,
    operation_id: str,
    application_id: str,
    application_slug: str,
    build: app.DeploymentBuild,
    deadline: float,
) -> dict[str, Any]:
    _validate_storage_bindings(
        connection,
        config,
        helper_caller=helper_caller,
        application_id=application_id,
        application_slug=application_slug,
        manifest=build.manifest,
        deadline=deadline,
    )
    platform_values = platform_environment_values(
        application_id,
        application_slug,
        build.manifest.port,
    )
    previous = db.get_deployment(connection, application_id)
    prior_platform_values = (
        {}
        if previous is None
        else platform_environment_values(
            application_id,
            application_slug,
            previous.application_port,
        )
    )
    refs = {
        **build.refs,
        "platform_key_names": sorted(platform_values),
        "desired_platform_values": platform_values,
        "prior_platform_values": prior_platform_values,
    }
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="platform_environment_mutating",
        refs=refs,
        candidate_digest=build.image,
    )
    ownership = _ownership(connection, application_id)
    result = app.set_environment(
        application_slug,
        platform_values,
        {**ownership, **{name: "staff" for name in platform_values}},
        timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
        helper_caller=lambda action, values, **_bounds: helper_caller(
            config, action, values, deadline=deadline
        ),
    )
    names = result.get("keys")
    if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
        raise app.ApplicationError("helper returned invalid platform environment evidence")
    if not set(platform_values).issubset(names):
        raise app.ApplicationError("platform environment keys were not all recorded")
    previous = db.get_deployment(connection, application_id)
    if previous is not None and not _environment_update_accepted(result, allow_stopped=True):
        raise app.ApplicationError("platform environment restart and health were not confirmed")
    _record_platform_environment_ownership(
        connection,
        application_id,
        sorted(platform_values),
    )
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="platform_environment_ready",
        refs=refs,
        candidate_digest=build.image,
    )
    return refs


def _prepare_deployment_worker(
    connection: sqlite3.Connection,
    config: Config,
    *,
    helper_caller: HelperCaller,
    state_directory: Path,
    operation_id: str,
    application_id: str,
    application_slug: str,
    worker_flavor: str,
    candidate: str,
    refs: dict[str, Any],
    deadline: float,
) -> app.DeploymentWorker:
    # Alternate bounded worker slots so staged and accepted fixed ports coexist.
    previous = db.get_deployment(connection, application_id)
    if previous is None:
        worker_application_id = application_id
    else:
        worker_ids = app.deployment_worker_ids(application_id)
        worker_application_id = worker_ids[
            1 if app.nomad_job_id(previous.nomad_job, application_slug) == application_slug else 0
        ]
    refs = {**refs, "worker_application_id": worker_application_id}
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="worker_creating",
        refs=refs,
        candidate_digest=candidate,
    )
    worker = helper_caller(
        config,
        "app.worker.observe",
        {"applicationId": worker_application_id, "slug": application_slug},
        deadline=deadline,
    )
    if worker.get("absent") is True:
        with runtime.lock(state_directory, "infrastructure", deadline=deadline):
            worker_image = db.get_image_selection(connection, "worker")
            if worker_image is None:
                raise ValidationError("select a worker image before deployment")
            refs = {**refs, "worker_image_id": worker_image.image_id}
            db.checkpoint_operation(
                connection,
                operation_id,
                phase="worker_creating",
                refs=refs,
                candidate_digest=candidate,
            )
        observed_flavor_name = openstack.observe_flavor(
            config.platform,
            worker_flavor,
            require_one_vcpu=True,
            timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
        )
        if observed_flavor_name != worker_flavor:
            raise openstack.OpenStackError("configured worker flavor resolved to a different name")
        worker = helper_caller(
            config,
            "app.worker.create",
            {
                "applicationId": worker_application_id,
                "slug": application_slug,
                "workerImageId": worker_image.image_id,
                "standardFlavor": worker_flavor,
            },
            deadline=deadline,
        )
    if worker.get("ready") is not True:
        raise app.ApplicationError("worker readiness was not confirmed")
    server_id = uuid(worker.get("serverId"), field="worker server UUID")
    port_id = uuid(worker.get("portId"), field="worker port UUID")
    server_name = bounded_text(worker.get("serverName"), field="worker server name", maximum=128)
    port_name = bounded_text(worker.get("portName"), field="worker port name", maximum=128)
    refs = {
        **refs,
        "worker_server_id": server_id,
        "worker_server_name": server_name,
        "worker_port_id": port_id,
        "worker_port_name": port_name,
    }
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="worker_ready",
        refs=refs,
        candidate_digest=candidate,
    )
    return app.DeploymentWorker(server_id, server_name, port_id, port_name, refs)


def _validate_storage_bindings(
    connection: sqlite3.Connection,
    config: Config,
    *,
    helper_caller: HelperCaller,
    application_id: str,
    application_slug: str,
    manifest: app.Manifest,
    deadline: float,
) -> None:
    if not manifest.storage_bindings:
        return
    resources = {
        (item.resource_type, item.resource_name): item
        for item in db.list_managed_resources(connection, application_id=application_id)
    }
    required_keys: set[str] = set()
    targets: set[str] = set()
    declared = {(binding.resource_type, binding.name) for binding in manifest.storage_bindings}
    active = {
        identity for identity, resource in resources.items() if resource.lifecycle_state == "active"
    }
    undeclared = sorted(active - declared)
    if undeclared:
        resource_type, resource_name = undeclared[0]
        raise ValidationError(
            f"active {resource_type} storage {resource_name!r} has no deployment binding"
        )
    for binding in manifest.storage_bindings:
        resource = resources.get((binding.resource_type, binding.name))
        if resource is None or resource.lifecycle_state != "active":
            raise ValidationError(
                f"storage binding {binding.name!r} references a missing or inactive {binding.resource_type} resource"
            )
        for output, target in binding.environment:
            required_keys.add(canonical_secret_key(binding.resource_type, binding.name, output))
            targets.add(target)
    observed = app.list_environment(
        application_slug,
        timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
        helper_caller=lambda action, values, **_bounds: helper_caller(
            config, action, values, deadline=deadline
        ),
    )
    names = observed.get("keys")
    if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
        raise app.ApplicationError("helper returned invalid environment-key evidence")
    present = set(names)
    missing = sorted(required_keys - present)
    if missing:
        raise ValidationError("storage binding outputs are missing from the application variable")
    conflicts = sorted(targets & present)
    if conflicts:
        raise ValidationError(
            f"storage binding target {conflicts[0]!r} conflicts with an existing environment key"
        )


def _cleanup_deployment(
    config: Config,
    application_slug: str,
    job_id: str,
    worker_application_id: str,
    identity: tuple[str, str],
    *,
    helper_caller: HelperCaller,
    deadline: float,
) -> None:
    cleaned = helper_caller(
        config,
        "app.remove",
        {
            "slug": application_slug,
            "jobId": job_id,
            "candidateJobSha256": identity[0],
            "candidateImage": identity[1],
        },
        deadline=deadline,
    )
    worker = helper_caller(
        config,
        "app.worker.delete",
        {
            "applicationId": worker_application_id,
            "slug": application_slug,
            "single": True,
        },
        deadline=deadline,
    )
    if cleaned.get("jobAbsent") is not True or worker.get("absent") is not True:
        raise app.ApplicationError("deployment job or worker cleanup was not confirmed")


def _deploy_and_accept_application(
    connection: sqlite3.Connection,
    config: Config,
    spec: app.DeploymentSpec,
    build: app.DeploymentBuild,
    worker: app.DeploymentWorker,
    *,
    helper_caller: HelperCaller,
    operation_id: str,
    deadline: float,
) -> app.DeploymentResult:
    previous = db.get_deployment(connection, spec.application_id)
    _validate_storage_bindings(
        connection,
        config,
        helper_caller=helper_caller,
        application_id=spec.application_id,
        application_slug=spec.application_slug,
        manifest=build.manifest,
        deadline=deadline,
    )
    updating = previous is not None
    previous_job_id = (
        app.nomad_job_id(previous.nomad_job, spec.application_slug) if previous else None
    )
    target_candidate_slot = updating and previous_job_id == spec.application_slug
    route_priority = (
        max(100, app.nomad_route_priority(previous.nomad_job) + 100) if previous else 100
    )
    placement = worker.refs.get("worker_application_id", spec.application_id)
    job = app.render_nomad_job(
        application_id=spec.application_id,
        application_slug=spec.application_slug,
        image=build.image,
        manifest=build.manifest,
        platform=config.platform,
        cpu_mhz=spec.cpu_mhz,
        memory_mib=spec.memory_mib,
        source_commit=spec.source_commit,
        recipe_hash=build.recipe_hash,
        candidate=target_candidate_slot,
        placement_id=placement,
        staged=updating,
        route_marker=operation_id,
    )
    promoted_job = (
        app.render_nomad_job(
            application_id=spec.application_id,
            application_slug=spec.application_slug,
            image=build.image,
            manifest=build.manifest,
            platform=config.platform,
            cpu_mhz=spec.cpu_mhz,
            memory_mib=spec.memory_mib,
            source_commit=spec.source_commit,
            recipe_hash=build.recipe_hash,
            candidate=target_candidate_slot,
            placement_id=placement,
            staged=True,
            promoted=True,
            route_marker=operation_id,
            route_priority=route_priority,
        )
        if updating
        else job
    )

    def observe(deployment_job: str, *, preview: bool) -> app.DeploymentResult:
        return app.deploy_and_cleanup(
            spec.application_slug,
            deployment_job,
            attempts=max(
                1,
                min(
                    300,
                    int(_remaining(deadline, config.policy.limits.process_seconds))
                    // config.policy.limits.poll_interval_seconds,
                ),
            ),
            poll_interval_seconds=config.policy.limits.poll_interval_seconds,
            helper_timeout_seconds=config.policy.limits.helper_seconds,
            helper_caller=lambda action, values, **_bounds: helper_caller(
                config, action, values, deadline=deadline
            ),
            public_health_check=lambda: app.check_public_health(
                spec.application_slug,
                config.platform,
                build.manifest.health_path,
                timeout_seconds=_remaining(deadline, config.policy.limits.http_seconds),
                preview=preview,
                expected_marker=operation_id,
            ),
            sleep=lambda seconds: time.sleep(_remaining(deadline, seconds)),
        )

    active_attempt_job = job
    try:
        result = observe(job, preview=updating)
        candidate_identity = app.nomad_candidate_identity(job)
        if updating:
            assert previous is not None
            result = app.promote_candidate(
                spec.application_slug,
                promoted_job,
                result.nomad_version,
                candidate_identity,
                app.nomad_route_priority(previous.nomad_job),
                helper_timeout_seconds=config.policy.limits.helper_seconds,
                helper_caller=lambda action, values, **_bounds: helper_caller(
                    config, action, values, deadline=deadline
                ),
            )
            active_attempt_job = promoted_job
            result = observe(promoted_job, preview=False)
    except app.DeploymentFailed as error:
        if not error.cleanup_succeeded:
            raise
        if updating:
            _cleanup_deployment(
                config,
                spec.application_slug,
                app.nomad_job_id(active_attempt_job, spec.application_slug),
                uuid(
                    worker.refs.get("worker_application_id"),
                    field="candidate worker application ID",
                ),
                app.nomad_candidate_identity(active_attempt_job),
                helper_caller=helper_caller,
                deadline=deadline,
            )
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="candidate_removed",
            refs=worker.refs,
            candidate_digest=build.image,
            cleanup_state="confirmed",
        )
        if previous is None or previous.image_digest != build.image:
            removed = helper_caller(
                config,
                "app.manifest.delete",
                {
                    "slug": spec.application_slug,
                    "image": build.image,
                    "references": ([] if previous is None else [previous.image_digest]),
                },
                deadline=deadline,
            )
            if removed.get("absent") is not True:
                raise app.ApplicationError(
                    "candidate manifest cleanup was not confirmed"
                ) from error
        db.mark_failed(connection, operation_id, error, cleanup_state="confirmed")
        db.checkpoint_deployment_attempt(
            connection,
            operation_id,
            status="failed",
            error=error,
            cleanup_state="confirmed",
        )
        raise

    accepted_job = promoted_job if updating else job
    accepted_identity = app.nomad_candidate_identity(accepted_job)
    accepted_refs = {
        **worker.refs,
        "nomad_version": result.nomad_version,
        "candidate_job_sha256": accepted_identity[0],
        "accepted_job_id": app.nomad_job_id(accepted_job, spec.application_slug),
        "route_priority": route_priority,
        "worker_server_id": worker.server_id,
        "worker_server_name": worker.server_name,
        "worker_port_id": worker.port_id,
        "worker_port_name": worker.port_name,
    }
    if previous is not None and previous_job_id is not None:
        accepted_refs.update(
            {
                "predecessor_job_id": previous_job_id,
                "predecessor_job_sha256": previous.nomad_job_sha256,
                "predecessor_image": previous.image_digest,
                "predecessor_worker_application_id": app.nomad_placement_id(previous.nomad_job),
            }
        )
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="deployment_healthy",
        refs=accepted_refs,
        candidate_digest=build.image,
    )
    app.accept_healthy_deployment(
        connection,
        app.DeploymentAcceptance(
            application_id=spec.application_id,
            application_slug=spec.application_slug,
            repository=spec.repository,
            deployment_id=operation_id,
            worker_server_id=worker.server_id,
            worker_server_name=worker.server_name,
            worker_port_id=worker.port_id,
            worker_port_name=worker.port_name,
            worker_flavor=spec.worker_flavor,
            cpu_mhz=spec.cpu_mhz,
            memory_mib=spec.memory_mib,
            source_commit=spec.source_commit,
            recipe_hash=build.recipe_hash,
            image=build.image,
            nomad_job=accepted_job,
            nomad_job_sha256=accepted_identity[0],
            nomad_version=result.nomad_version,
            health_path=build.manifest.health_path,
            application_port=build.manifest.port,
            build_log_path=build.build_log_path,
            public_url=f"https://{spec.application_slug}.{config.platform.domain}",
        ),
        helper_timeout_seconds=config.policy.limits.helper_seconds,
        helper_caller=lambda action, values, **_bounds: helper_caller(
            config, action, values, deadline=deadline
        ),
        public_health_check=lambda: app.check_public_health(
            spec.application_slug,
            config.platform,
            build.manifest.health_path,
            timeout_seconds=_remaining(deadline, config.policy.limits.http_seconds),
            expected_marker=operation_id,
        ),
    )
    if updating:
        assert previous is not None and previous_job_id is not None
        _cleanup_deployment(
            config,
            spec.application_slug,
            previous_job_id,
            app.nomad_placement_id(previous.nomad_job),
            (previous.nomad_job_sha256, previous.image_digest),
            helper_caller=helper_caller,
            deadline=deadline,
        )
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="accepted",
        refs=accepted_refs,
        candidate_digest=build.image,
    )
    _apply_registry_retention(
        connection,
        config,
        helper_caller=helper_caller,
        application_id=spec.application_id,
        application_slug=spec.application_slug,
        current_image=build.image,
        deadline=deadline,
    )
    db.mark_succeeded(connection, operation_id)
    return result


def _recover_app_deployment(
    connection: sqlite3.Connection,
    config: Config,
    spec: app.DeploymentSpec,
    operation: db.Operation,
    *,
    helper_caller: HelperCaller,
    deadline: float,
) -> DeploymentRecovery:
    """Reconcile one recorded deployment phase; return work still to run."""
    application_id = spec.application_id
    application_slug = spec.application_slug
    operation_id = operation.operation_id
    if operation.phase in {"platform_environment_mutating", "platform_environment_ready"}:
        if operation.refs.get("platform_key_names") != sorted(_PLATFORM_ENVIRONMENT):
            raise app.ApplicationError("platform environment recovery intent is malformed")
        desired_values = operation.refs.get("desired_platform_values")
        prior_values = operation.refs.get("prior_platform_values")
        if (
            not isinstance(desired_values, dict)
            or set(desired_values) != _PLATFORM_ENVIRONMENT
            or any(not isinstance(value, str) for value in desired_values.values())
            or not isinstance(prior_values, dict)
            or not set(prior_values).issubset(_PLATFORM_ENVIRONMENT)
            or any(not isinstance(value, str) for value in prior_values.values())
        ):
            raise app.ApplicationError("platform environment recovery values are malformed")
        ownership = _ownership(connection, application_id)

        def call_helper(
            action: str, values: Mapping[str, Any], **_bounds: object
        ) -> Mapping[str, Any]:
            return helper_caller(config, action, values, deadline=deadline)

        if prior_values:
            restored = app.set_environment(
                application_slug,
                prior_values,
                {**ownership, **{name: "staff" for name in prior_values}},
                timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
                helper_caller=call_helper,
            )
        else:
            restored = app.remove_environment(
                application_slug,
                sorted(_PLATFORM_ENVIRONMENT),
                {**ownership, **{name: "staff" for name in _PLATFORM_ENVIRONMENT}},
                timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
                helper_caller=call_helper,
            )
        names = restored.get("keys")
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise app.ApplicationError(
                "helper returned invalid restored platform environment evidence"
            )
        present_platform = set(names) & _PLATFORM_ENVIRONMENT
        if present_platform != set(prior_values):
            raise app.ApplicationError("prior platform environment key set was not restored")
        previous = db.get_deployment(connection, application_id)
        if previous is not None and not _environment_update_accepted(restored, allow_stopped=True):
            raise app.ApplicationError("prior platform environment health was not restored")
        _record_platform_environment_ownership(
            connection,
            application_id,
            sorted(prior_values),
        )
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="image_pushed",
            refs=operation.refs,
            candidate_digest=operation.candidate_digest,
        )
        refreshed = db.get_operation(connection, operation_id)
        assert refreshed is not None
        operation = refreshed

    if operation.phase == "candidate_removed":
        candidate = oci_digest_pin(operation.candidate_digest, field="removed candidate digest")
        previous = db.get_deployment(connection, application_id)
        if previous is None or previous.image_digest != candidate:
            result = helper_caller(
                config,
                "app.manifest.delete",
                {
                    "slug": application_slug,
                    "image": candidate,
                    "references": ([] if previous is None else [previous.image_digest]),
                },
                deadline=deadline,
            )
            if result.get("absent") is not True:
                raise app.ApplicationError("candidate manifest cleanup was not confirmed")
        message = "confirmed candidate removal recovered and manifest cleaned"
        db.mark_failed(
            connection,
            operation_id,
            message,
            cleanup_state="confirmed",
        )
        db.checkpoint_deployment_attempt(
            connection,
            operation_id,
            status="failed",
            error=message,
            cleanup_state="confirmed",
        )
        return DeploymentRecovery(None, "candidate-removed")

    if operation.phase == "accepted":
        accepted = db.get_deployment(connection, application_id)
        if accepted is None or accepted.image_digest != operation.candidate_digest:
            raise app.ApplicationError(
                "accepted deployment recovery evidence does not match SQLite"
            )
        assert operation.candidate_digest is not None
        _apply_registry_retention(
            connection,
            config,
            helper_caller=helper_caller,
            application_id=application_id,
            application_slug=application_slug,
            current_image=operation.candidate_digest,
            deadline=deadline,
        )
        db.mark_succeeded(connection, operation_id)
        return DeploymentRecovery(None, "accepted")

    recovery_action = app.deployment_recovery_action(
        operation.phase, candidate_digest=operation.candidate_digest
    )
    if recovery_action == "accept_deployment":
        candidate = oci_digest_pin(operation.candidate_digest, field="candidate digest")
        attempt = db.get_deployment_attempt(connection, operation_id)
        if attempt is None or attempt.configuration is None:
            raise app.ApplicationError("deployment configuration snapshot is missing")
        manifest = _configuration_manifest(connection, application_id, attempt.configuration)
        _validate_storage_bindings(
            connection,
            config,
            helper_caller=helper_caller,
            application_id=application_id,
            application_slug=application_slug,
            manifest=manifest,
            deadline=deadline,
        )
        accepted_job_id = operation.refs.get("accepted_job_id", application_slug)
        if accepted_job_id not in {application_slug, f"{application_slug}-candidate"}:
            raise app.ApplicationError("healthy deployment recovery job ID was invalid")
        has_predecessor = operation.refs.get("predecessor_job_id") is not None
        recipe_hash = sha256_hex(operation.refs.get("recipe_hash"), field="recipe hash")
        job = app.render_nomad_job(
            application_id=application_id,
            application_slug=application_slug,
            image=candidate,
            manifest=manifest,
            platform=config.platform,
            cpu_mhz=spec.cpu_mhz,
            memory_mib=spec.memory_mib,
            source_commit=spec.source_commit,
            recipe_hash=recipe_hash,
            candidate=accepted_job_id.endswith("-candidate"),
            placement_id=operation.refs.get("worker_application_id", application_id),
            staged=has_predecessor,
            promoted=has_predecessor,
            route_marker=operation_id,
            route_priority=operation.refs.get("route_priority", 100),
        )
        candidate_identity = app.nomad_candidate_identity(job)
        if (
            candidate_identity is None
            or operation.refs.get("candidate_job_sha256") != candidate_identity[0]
            or candidate_identity[1] != candidate
        ):
            raise app.ApplicationError("healthy deployment recovery candidate identity drifted")
        nomad_version = operation.refs.get("nomad_version")
        if isinstance(nomad_version, bool) or not isinstance(nomad_version, int):
            raise app.ApplicationError("healthy deployment recovery omitted its Nomad version")
        worker_server_id = uuid(operation.refs.get("worker_server_id"), field="worker server UUID")
        worker_port_id = uuid(operation.refs.get("worker_port_id"), field="worker port UUID")
        worker_server_name = bounded_text(
            operation.refs.get("worker_server_name"),
            field="worker server name",
            maximum=128,
        )
        worker_port_name = bounded_text(
            operation.refs.get("worker_port_name"),
            field="worker port name",
            maximum=128,
        )
        build_log_path = relative_path(
            operation.refs.get("build_log_path"), field="build log path", allow_dot=False
        )
        app.accept_healthy_deployment(
            connection,
            app.DeploymentAcceptance(
                application_id=application_id,
                application_slug=application_slug,
                repository=spec.repository,
                deployment_id=operation_id,
                worker_server_id=worker_server_id,
                worker_server_name=worker_server_name,
                worker_port_id=worker_port_id,
                worker_port_name=worker_port_name,
                worker_flavor=spec.worker_flavor,
                cpu_mhz=spec.cpu_mhz,
                memory_mib=spec.memory_mib,
                source_commit=spec.source_commit,
                recipe_hash=recipe_hash,
                image=candidate,
                nomad_job=job,
                nomad_job_sha256=candidate_identity[0],
                nomad_version=nomad_version,
                health_path=manifest.health_path,
                application_port=manifest.port,
                build_log_path=build_log_path,
                public_url=f"https://{application_slug}.{config.platform.domain}",
            ),
            helper_timeout_seconds=config.policy.limits.helper_seconds,
            helper_caller=lambda action, values, **_bounds: helper_caller(
                config, action, values, deadline=deadline
            ),
            public_health_check=lambda: app.check_public_health(
                application_slug,
                config.platform,
                manifest.health_path,
                timeout_seconds=_remaining(deadline, config.policy.limits.http_seconds),
                expected_marker=operation_id,
            ),
        )
        if has_predecessor:
            predecessor_job_id = operation.refs.get("predecessor_job_id")
            predecessor_worker_id = operation.refs.get("predecessor_worker_application_id")
            predecessor_hash = operation.refs.get("predecessor_job_sha256")
            predecessor_image = operation.refs.get("predecessor_image")
            if not all(
                isinstance(value, str)
                for value in (
                    predecessor_job_id,
                    predecessor_worker_id,
                    predecessor_hash,
                    predecessor_image,
                )
            ):
                raise app.ApplicationError("predecessor cleanup recovery evidence was incomplete")
            assert isinstance(predecessor_job_id, str)
            assert isinstance(predecessor_worker_id, str)
            assert isinstance(predecessor_hash, str)
            assert isinstance(predecessor_image, str)
            _cleanup_deployment(
                config,
                application_slug,
                predecessor_job_id,
                predecessor_worker_id,
                (predecessor_hash, predecessor_image),
                helper_caller=helper_caller,
                deadline=deadline,
            )
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="accepted",
            refs=operation.refs,
            candidate_digest=candidate,
        )
        _apply_registry_retention(
            connection,
            config,
            helper_caller=helper_caller,
            application_id=application_id,
            application_slug=application_slug,
            current_image=candidate,
            deadline=deadline,
        )
        db.mark_succeeded(connection, operation_id)
        return DeploymentRecovery(None, "deployment-healthy")

    if recovery_action.startswith("cleanup_builder"):
        result = helper_caller(
            config,
            "app.builder.delete",
            {"buildId": operation_id},
            deadline=deadline,
        )
        if result.get("absent") is not True:
            raise app.ApplicationError("builder cleanup was not confirmed")
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="image_pushed" if operation.candidate_digest is not None else "validated",
            refs=operation.refs,
            candidate_digest=operation.candidate_digest,
            cleanup_state="confirmed",
        )
        refreshed = db.get_operation(connection, operation_id)
        assert refreshed is not None
        return DeploymentRecovery(refreshed)
    return DeploymentRecovery(operation)


class DeploymentService:
    """Own deployment locking, deadlines, checkpoints, recovery, and acceptance."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
        *,
        helper_caller: HelperCaller = _default_helper,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory
        self.helper_caller = helper_caller

    def recover_operation(
        self,
        spec: app.DeploymentSpec,
        operation: db.Operation,
        *,
        deadline: float | None = None,
    ) -> DeploymentRecovery:
        """Reconcile one durable deployment checkpoint without rendering output."""
        selected_deadline = (
            time.monotonic() + self.config.policy.limits.process_seconds
            if deadline is None
            else deadline
        )
        return _recover_app_deployment(
            self.connection,
            self.config,
            spec,
            operation,
            helper_caller=self.helper_caller,
            deadline=selected_deadline,
        )

    def deploy(
        self,
        request: DeploymentRequest,
        *,
        deadline: float | None = None,
    ) -> DeploymentOutcome:
        application_slug = slug(request.application)
        repository = repository_url(request.repository)
        requested_ref = branch_name(request.requested_ref)
        source_commit = commit(request.source_commit)
        if (
            isinstance(request.configuration_revision, bool)
            or not isinstance(request.configuration_revision, int)
            or request.configuration_revision < 0
        ):
            raise ValidationError("configuration revision must be a non-negative integer")
        if not isinstance(request.configuration, DeploymentConfiguration):
            raise ValidationError("deployment configuration snapshot is malformed")
        configuration_json = request.configuration.canonical_json()
        configuration_sha256 = hashlib.sha256(configuration_json.encode()).hexdigest()
        existing = db.get_application(self.connection, application_slug)
        if existing is not None:
            unfinished = db.get_unfinished_operation(
                self.connection, f"app-{existing.application_id}"
            )
            if unfinished is not None and unfinished.kind == "app.delete":
                raise db.UnfinishedOperationError(
                    unfinished.scope, unfinished.operation_id, unfinished.kind
                )
        application_id = (
            existing.application_id if existing is not None else str(uuid_module.uuid4())
        )
        _configuration_manifest(self.connection, application_id, request.configuration)
        selected_request_id = (
            None
            if request.request_id is None
            else uuid(request.request_id, field="deployment request ID")
        )
        standard = self.config.policy.standard
        spec = app.DeploymentSpec(
            application_id,
            application_slug,
            repository,
            source_commit,
            requested_ref,
            request.configuration_revision,
            configuration_sha256,
            existing.worker_flavor if existing is not None else standard.worker_flavor,
            existing.scheduler_cpu_mhz if existing is not None else standard.cpu_mhz,
            existing.scheduler_memory_mib if existing is not None else standard.memory_mib,
        )
        selected_deadline = (
            time.monotonic() + self.config.policy.limits.process_seconds
            if deadline is None
            else deadline
        )
        completed_recovery: RecoveryKind | None = None

        def deployment_fingerprint() -> tuple[str, int]:
            environment = db.get_environment_revision(self.connection, application_id)
            if environment is None:
                raise db.DatabaseError("application environment revision is missing")
            return (
                db.request_fingerprint(
                    {
                        "applicationId": application_id,
                        "repository": repository,
                        "requestedRef": requested_ref,
                        "commit": source_commit,
                        "configurationRevision": request.configuration_revision,
                        "configuration": json.loads(configuration_json),
                    }
                ),
                environment.revision,
            )

        if selected_request_id is not None:
            fingerprint, _environment_revision = deployment_fingerprint()
            claimed = db.get_idempotency_request(self.connection, selected_request_id)
            dispatch = db.get_operation_dispatch(self.connection, selected_request_id)
            api_managed = (
                claimed is not None
                and claimed.result_id == selected_request_id
                and dispatch is not None
            )
            if not api_managed:
                claimed = db.claim_idempotency_request(
                    self.connection,
                    request_id=selected_request_id,
                    request_fingerprint=fingerprint,
                )
            assert claimed is not None
            if claimed.result_id is not None:
                if api_managed:
                    attempt = db.get_deployment_attempt(self.connection, claimed.result_id)
                    operation = db.get_operation(self.connection, claimed.result_id)
                    if (
                        attempt is not None
                        and attempt.status in {"succeeded", "failed"}
                        and operation is not None
                        and operation.status in {"succeeded", "failed"}
                    ):
                        return DeploymentOutcome(application_slug, image=attempt.image_digest)
                elif claimed.result_kind == "deployment":
                    attempt = db.get_deployment_attempt(self.connection, claimed.result_id)
                    if attempt is None or attempt.application_id != application_id:
                        raise db.DatabaseError("idempotency result deployment is missing")
                    return DeploymentOutcome(application_slug, image=attempt.image_digest)
                else:
                    raise db.IdempotencyConflictError(
                        "idempotency request already has a different result"
                    )

        def create_attempt(operation_id: str) -> None:
            fingerprint, environment_revision = deployment_fingerprint()
            claimed = db.get_idempotency_request(self.connection, operation_id)
            if not (
                claimed is not None
                and claimed.result_id == operation_id
                and db.get_operation_dispatch(self.connection, operation_id) is not None
            ):
                db.claim_idempotency_request(
                    self.connection,
                    request_id=operation_id,
                    request_fingerprint=fingerprint,
                )
            db.create_deployment_attempt(
                self.connection,
                deployment_id=operation_id,
                application_id=application_id,
                source_commit=source_commit,
                requested_ref=requested_ref,
                configuration_revision=request.configuration_revision,
                configuration=request.configuration,
                environment_revision=environment_revision,
                idempotency_request_id=operation_id,
            )

        def snapshot(operation_id: str) -> tuple[DeploymentConfiguration, app.Manifest]:
            attempt = db.get_deployment_attempt(self.connection, operation_id)
            if (
                attempt is None
                or attempt.configuration is None
                or attempt.source_commit != source_commit
                or attempt.requested_ref != requested_ref
                or attempt.configuration_revision != request.configuration_revision
                or attempt.configuration_sha256 != configuration_sha256
            ):
                raise app.ApplicationError("deployment attempt snapshot does not match request")
            return attempt.configuration, _configuration_manifest(
                self.connection, application_id, attempt.configuration
            )

        def checkpoint_attempt(operation_id: str, error: BaseException) -> None:
            attempt = db.get_deployment_attempt(self.connection, operation_id)
            if attempt is not None and attempt.status not in {"failed", "succeeded"}:
                db.checkpoint_deployment_attempt(
                    self.connection,
                    operation_id,
                    status="recovery_required",
                    error=error,
                )

        def recover(operation: db.Operation) -> db.Operation | None:
            nonlocal completed_recovery
            if (
                operation.phase == "validated"
                and db.get_deployment_attempt(self.connection, operation.operation_id) is None
            ):
                create_attempt(operation.operation_id)
            recovered = self.recover_operation(spec, operation, deadline=selected_deadline)
            completed_recovery = recovered.completed
            return recovered.operation

        def prepare_build(operation: db.Operation) -> app.DeploymentBuild:
            configuration, snapshotted_manifest = snapshot(operation.operation_id)
            return _prepare_deployment_build(
                self.connection,
                self.config,
                helper_caller=self.helper_caller,
                state_directory=self.state_directory,
                operation=operation,
                application_id=application_id,
                application_slug=application_slug,
                repository=repository,
                requested_ref=requested_ref,
                source_commit=source_commit,
                configuration_revision=request.configuration_revision,
                configuration=configuration,
                manifest=snapshotted_manifest,
                deadline=selected_deadline,
            )

        def prepare_environment(operation_id: str, build: app.DeploymentBuild) -> dict[str, Any]:
            return _prepare_platform_environment(
                self.connection,
                self.config,
                helper_caller=self.helper_caller,
                operation_id=operation_id,
                application_id=application_id,
                application_slug=application_slug,
                build=build,
                deadline=selected_deadline,
            )

        def prepare_worker(
            operation_id: str,
            build: app.DeploymentBuild,
            refs: dict[str, Any],
        ) -> app.DeploymentWorker:
            db.checkpoint_deployment_attempt(self.connection, operation_id, status="deploying")
            return _prepare_deployment_worker(
                self.connection,
                self.config,
                helper_caller=self.helper_caller,
                state_directory=self.state_directory,
                operation_id=operation_id,
                application_id=application_id,
                application_slug=application_slug,
                worker_flavor=spec.worker_flavor,
                candidate=build.image,
                refs=refs,
                deadline=selected_deadline,
            )

        def deploy_and_accept(
            operation_id: str,
            build: app.DeploymentBuild,
            worker: app.DeploymentWorker,
        ) -> app.DeploymentResult:
            return _deploy_and_accept_application(
                self.connection,
                self.config,
                spec,
                build,
                worker,
                helper_caller=self.helper_caller,
                operation_id=operation_id,
                deadline=selected_deadline,
            )

        def verify_project() -> None:
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=_remaining(
                    selected_deadline,
                    self.config.policy.limits.process_seconds,
                ),
            )

        result = app.execute_deployment_workflow(
            self.connection,
            self.config,
            self.state_directory,
            spec,
            deadline_at=_wall_deadline(selected_deadline),
            deadline=selected_deadline,
            verify_project=verify_project,
            recover=recover,
            prepare_build=prepare_build,
            prepare_environment=prepare_environment,
            prepare_worker=prepare_worker,
            deploy_and_accept=deploy_and_accept,
            create_attempt=create_attempt,
            checkpoint_attempt=checkpoint_attempt,
            operation_id=selected_request_id,
        )
        if result is None:
            if completed_recovery is None:
                raise app.ApplicationError("deployment recovery produced no terminal result")
            return DeploymentOutcome(application_slug, recovered=completed_recovery)
        candidate_digest, deployment_result = result
        return DeploymentOutcome(
            application_slug,
            image=candidate_digest,
            nomad_version=deployment_result.nomad_version,
        )
