"""Application actions executed beside the Nomad control plane.

Handlers accept only strict JSON data and invoke a fixed Nomad argv. Environment
values remain inside the helper and Nomad Variable CAS operations; handler
results expose key names and indexes only.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..app import nomad_candidate_identity
from ..runtime import CommandTimedOut, bounded_http, run
from ..validation import (
    ValidationError,
    bounded_text,
    env_key,
    health_path,
    oci_digest_pin,
    sha256_hex,
    slug,
)
from .main import Handler, HelperActionError
from .nomad import (
    CasConflict,
    VariableClient,
    VariableSnapshot,
    merge_owned_items,
    variable_path,
)

_OWNERS = {"platform", "staff"}
_STORAGE_OWNER = re.compile(r"storage\.(?:postgres|mongo|s3)\.[a-z][a-z0-9-]{0,39}")


def _exact_args(args: Mapping[str, Any], expected: set[str], action: str) -> None:
    if args.keys() != expected:
        raise HelperActionError("INVALID_ARGS", f"{action} arguments are invalid")


def _command(command: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(command)
    if not selected or any(
        not isinstance(item, str) or not item or "\x00" in item for item in selected
    ):
        raise ValueError("Nomad command must be a fixed non-empty argv")
    return selected


def _parse_json(payload: bytes, *, field: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HelperActionError(
            "NOMAD_RESPONSE_INVALID", f"Nomad {field} response was malformed"
        ) from error


def _exact_absence(stderr: object, application_slug: str) -> bool:
    """Accept only Nomad's bounded, slug-specific absence diagnostics.

    A non-zero CLI status is not absence by itself: authentication, a broken
    wrapper, and an ambiguous prefix can all use the same exit status.  Keep
    this parser anchored and tied to the requested ID before allowing any
    caller to treat the job as absent.
    """
    if not isinstance(stderr, bytes):
        return False
    try:
        text = stderr.decode("utf-8")
    except UnicodeDecodeError:
        return False
    escaped = re.escape(application_slug)
    return (
        re.fullmatch(
            rf"\s*No job(?:\(s\) with prefix or ID| with ID) ['\"]{escaped}['\"] found\s*",
            text,
        )
        is not None
    )


def _status_or_absent(
    application_slug: str,
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
) -> Mapping[str, Any] | None:
    completed = command_runner(
        (*nomad_command, "job", "status", "-json", application_slug),
        timeout_seconds=timeout_seconds,
        stdout_limit=response_limit,
        stderr_limit=65_536,
        check=False,
    )
    if completed.returncode == 0:
        value = _parse_json(completed.stdout, field="job status")
        if isinstance(value, dict) and value.get("ID") == application_slug:
            return value
        # Nomad 2 emits the allocation status projection as a JSON array for
        # stopped jobs. Environment mutation needs only the running/dead
        # distinction, but every row must still belong to the exact job.
        if (
            isinstance(value, list)
            and value
            and all(
                isinstance(item, dict) and item.get("JobID") == application_slug for item in value
            )
        ):
            running = any(
                item.get("ClientStatus") in {"pending", "running"}
                and item.get("DesiredStatus") == "run"
                for item in value
            )
            return {"ID": application_slug, "Status": "running" if running else "dead"}
        raise HelperActionError("NOMAD_RESPONSE_INVALID", "Nomad returned an unexpected job")
    if completed.returncode != 1 or not _exact_absence(completed.stderr, application_slug):
        raise HelperActionError("NOMAD_UNAVAILABLE", "Nomad job state was unavailable")
    return None


def _allocations(
    application_slug: str,
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
) -> list[Mapping[str, Any]]:
    completed = command_runner(
        (*nomad_command, "job", "allocs", "-json", application_slug),
        timeout_seconds=timeout_seconds,
        stdout_limit=response_limit,
        stderr_limit=65_536,
        check=True,
    )
    value = _parse_json(completed.stdout, field="allocations")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HelperActionError("NOMAD_RESPONSE_INVALID", "Nomad returned invalid allocations")
    return value


def _validated_ownership(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValidationError("environment ownership must be an object")
    result: dict[str, str] = {}
    for key, owner in value.items():
        key = env_key(key)
        if owner not in _OWNERS and (
            not isinstance(owner, str) or _STORAGE_OWNER.fullmatch(owner) is None
        ):
            raise ValidationError("environment ownership contains an unknown owner")
        result[key] = owner
    return result


def inspected_job_identity(value: Mapping[str, Any], application_slug: str) -> tuple[int, str, str]:
    """Reduce one live Nomad job to an exact stable definition hash and image.

    M1 jobs carry the generated-job hash and immutable image explicitly. Jobs
    without those markers are not accepted as deployment evidence.
    """
    if value.get("ID") != application_slug:
        raise HelperActionError("NOMAD_RESPONSE_INVALID", "Nomad returned an unexpected job")
    version = value.get("Version")
    groups = value.get("TaskGroups")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version < 0
        or not isinstance(groups, list)
    ):
        raise HelperActionError("NOMAD_RESPONSE_INVALID", "Nomad job identity was malformed")
    observed_images: list[object] = []
    for group in groups:
        if not isinstance(group, dict) or group.get("Name") != "app":
            continue
        tasks = group.get("Tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if isinstance(task, dict) and task.get("Name") == "app":
                config = task.get("Config")
                if isinstance(config, dict):
                    observed_images.append(config.get("image"))
    if len(observed_images) != 1:
        raise HelperActionError("NOMAD_RESPONSE_INVALID", "Nomad job image was ambiguous")
    try:
        image = oci_digest_pin(observed_images[0], field="observed Nomad image")
    except ValidationError as error:
        raise HelperActionError(
            "NOMAD_RESPONSE_INVALID", "Nomad job image was not immutable"
        ) from error
    metadata = value.get("Meta")
    marker_identity = (
        metadata.get("m1_candidate_job_sha256") if isinstance(metadata, dict) else None
    )
    marker_image = metadata.get("m1_candidate_image") if isinstance(metadata, dict) else None
    if (
        not isinstance(marker_identity, str)
        or not re.fullmatch(r"[0-9a-f]{64}", marker_identity)
        or marker_image != image
    ):
        raise HelperActionError(
            "NOMAD_RESPONSE_INVALID", "Nomad candidate metadata was missing or malformed"
        )
    return version, marker_identity, image


def _inspected_candidate(
    application_slug: str,
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
    reject_invalid: bool = True,
) -> tuple[int, str, str] | None:
    """Inspect one job, returning ``None`` only for exact absence.

    ``None`` is intentionally not a generic "could not inspect" result.  A
    dependency failure or malformed/ambiguous job is raised before callers can
    submit a replacement or issue a destructive command.
    """
    completed = command_runner(
        (*nomad_command, "job", "inspect", "-json", application_slug),
        timeout_seconds=timeout_seconds,
        stdout_limit=response_limit,
        stderr_limit=65_536,
        check=False,
    )
    if completed.returncode != 0:
        if completed.returncode == 1 and _exact_absence(completed.stderr, application_slug):
            return None
        raise HelperActionError("NOMAD_UNAVAILABLE", "Nomad job inspection was unavailable")
    value = _parse_json(completed.stdout, field="job inspection")
    if not isinstance(value, dict) or value.get("ID") != application_slug:
        raise HelperActionError("NOMAD_RESPONSE_INVALID", "Nomad returned an unexpected job")
    # Keep the historical keyword for callers outside this module, but never
    # downgrade malformed identity to absence.  Ambiguity is destructive-path
    # blocking evidence regardless of the flag's value.
    return inspected_job_identity(value, application_slug)


def _deploy_handler(
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
) -> Handler:
    def handle(args: Mapping[str, Any]) -> Mapping[str, Any]:
        _exact_args(args, {"slug", "job"}, "app.deploy")
        application_slug = slug(args["slug"])
        job = bounded_text(args["job"], field="Nomad job", maximum=262_144)
        candidate = nomad_candidate_identity(job)
        command_runner(
            (*nomad_command, "job", "validate", "-"),
            timeout_seconds=timeout_seconds,
            stdin=job.encode(),
            stdout_limit=response_limit,
            stderr_limit=65_536,
            check=True,
        )
        current = _inspected_candidate(
            application_slug,
            command_runner=command_runner,
            nomad_command=nomad_command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
            reject_invalid=True,
        )
        if current is not None and current[1:] == candidate:
            return {
                "slug": application_slug,
                "nomadVersion": current[0],
                "candidateJobSha256": candidate[0],
                "candidateImage": candidate[1],
                "submitted": False,
            }
        command_runner(
            (*nomad_command, "job", "run", "-detach", "-"),
            timeout_seconds=timeout_seconds,
            stdin=job.encode(),
            stdout_limit=65_536,
            stderr_limit=65_536,
            check=True,
        )
        observed = _inspected_candidate(
            application_slug,
            command_runner=command_runner,
            nomad_command=nomad_command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
            reject_invalid=True,
        )
        if observed is None or observed[1:] != candidate:
            raise HelperActionError(
                "NOMAD_CANDIDATE_UNCONFIRMED",
                "Nomad did not confirm the exact submitted deployment candidate",
            )
        return {
            "slug": application_slug,
            "nomadVersion": observed[0],
            "candidateJobSha256": candidate[0],
            "candidateImage": candidate[1],
            "submitted": True,
        }

    return handle


def _health_handler(
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
) -> Handler:
    def handle(args: Mapping[str, Any]) -> Mapping[str, Any]:
        _exact_args(
            args,
            {"slug", "version", "candidateJobSha256", "candidateImage"},
            "app.health",
        )
        application_slug = slug(args["slug"])
        version = args["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValidationError("Nomad version must be a non-negative integer")
        expected_identity = sha256_hex(args["candidateJobSha256"], field="candidate job SHA-256")
        expected_image = oci_digest_pin(args["candidateImage"], field="candidate image")
        current = _inspected_candidate(
            application_slug,
            command_runner=command_runner,
            nomad_command=nomad_command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
        )
        current_version = current[0] if current is not None else None
        candidate_matches = current == (version, expected_identity, expected_image)
        version_matches = (
            isinstance(current_version, int)
            and not isinstance(current_version, bool)
            and current_version == version
        )
        allocations = _allocations(
            application_slug,
            command_runner=command_runner,
            nomad_command=nomad_command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
        )
        selected = [
            item
            for item in allocations
            if isinstance(item.get("JobVersion"), int)
            and not isinstance(item.get("JobVersion"), bool)
            and item.get("JobVersion") == version
            and item.get("DesiredStatus") == "run"
        ]
        exactly_one = len(selected) == 1
        healthy = (
            version_matches
            and candidate_matches
            and exactly_one
            and selected[0].get("ClientStatus") == "running"
            and isinstance(selected[0].get("DeploymentStatus"), dict)
            and selected[0]["DeploymentStatus"].get("Healthy") is True
        )
        terminal = (
            not version_matches
            or not candidate_matches
            or len(selected) > 1
            or any(item.get("ClientStatus") in {"failed", "lost"} for item in selected)
        )
        return {
            "slug": application_slug,
            "version": version,
            "currentVersion": current_version,
            "candidateJobSha256": current[1] if current is not None else None,
            "candidateImage": current[2] if current is not None else None,
            "healthy": healthy,
            "terminal": terminal,
            "allocations": len(selected),
        }

    return handle


def _logs_handler(
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
) -> Handler:
    def handle(args: Mapping[str, Any]) -> Mapping[str, Any]:
        if args.keys() not in ({"slug", "stderr", "lines"}, {"slug", "stderr", "lines", "follow"}):
            raise HelperActionError("INVALID_ARGS", "app.logs arguments are invalid")
        application_slug = slug(args["slug"])
        stderr = args["stderr"]
        lines = args["lines"]
        follow = args.get("follow", False)
        if not isinstance(stderr, bool):
            raise ValidationError("stderr must be boolean")
        if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 2_000:
            raise ValidationError("log line count is invalid")
        if not isinstance(follow, bool):
            raise ValidationError("log follow selector must be boolean")
        allocations = _allocations(
            application_slug,
            command_runner=command_runner,
            nomad_command=nomad_command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
        )
        running = sorted(
            (
                item
                for item in allocations
                if item.get("ClientStatus") == "running" and isinstance(item.get("ID"), str)
            ),
            key=lambda item: item.get("CreateIndex", 0),
            reverse=True,
        )
        if not running:
            raise HelperActionError(
                "ALLOCATION_NOT_FOUND", "no running application allocation was found"
            )
        allocation_id = running[0]["ID"]
        if not isinstance(allocation_id, str) or not allocation_id or len(allocation_id) > 128:
            raise HelperActionError(
                "NOMAD_RESPONSE_INVALID", "Nomad allocation identity was invalid"
            )
        argv = [*nomad_command, "alloc", "logs", "-tail", "-n", str(lines)]
        if stderr:
            argv.append("-stderr")
        if follow:
            argv.append("-f")
        argv.extend((allocation_id, "app"))
        timed_out = False
        try:
            completed = command_runner(
                tuple(argv),
                timeout_seconds=timeout_seconds,
                stdout_limit=response_limit,
                stderr_limit=65_536,
                check=True,
            )
        except CommandTimedOut as error:
            if not follow or error.result is None:
                raise
            # A follow deadline is successful bounded completion. Preserve the
            # already-collected tail rather than replacing it with a timeout
            # error that discards useful staff-only output.
            completed = error.result
            timed_out = True
        return {
            "slug": application_slug,
            "stream": "stderr" if stderr else "stdout",
            "text": completed.stdout.decode("utf-8", errors="replace"),
            "truncated": bool(completed.stdout_truncated),
            "followed": follow,
            "deadlineReached": timed_out,
        }

    return handle


def _remove_handler(
    variable_client: VariableClient,
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
) -> Handler:
    def handle(args: Mapping[str, Any]) -> Mapping[str, Any]:
        if args.keys() not in ({"slug"}, {"slug", "preserveVariable"}):
            raise HelperActionError("INVALID_ARGS", "app.remove arguments are invalid")
        application_slug = slug(args["slug"])
        preserve_variable = args.get("preserveVariable", False)
        if not isinstance(preserve_variable, bool):
            raise ValidationError("preserveVariable must be boolean")

        # Establish a safe precondition before issuing the destructive stop.
        # Exact absence is a successful no-op; every other non-zero/ambiguous
        # inspection blocks the stop and therefore cannot remove an unrelated
        # job after a dependency failure.
        before = _inspected_candidate(
            application_slug,
            command_runner=command_runner,
            nomad_command=nomad_command,
            timeout_seconds=timeout_seconds,
            response_limit=65_536,
        )
        if before is not None:
            command_runner(
                (*nomad_command, "job", "stop", "-purge", "-yes", application_slug),
                timeout_seconds=timeout_seconds,
                stdout_limit=65_536,
                stderr_limit=65_536,
                check=False,
            )
            after = _status_or_absent(
                application_slug,
                command_runner=command_runner,
                nomad_command=nomad_command,
                timeout_seconds=timeout_seconds,
                response_limit=65_536,
            )
            if after is not None:
                raise HelperActionError("JOB_REMAINS", "Nomad job remained after removal")
        variable_absent = False
        if not preserve_variable:
            path = variable_path(application_slug)
            command_runner(
                (*nomad_command, "var", "purge", "-force", path),
                timeout_seconds=timeout_seconds,
                stdout_limit=65_536,
                stderr_limit=65_536,
                check=False,
            )
            snapshot = variable_client.read_variable(path)
            variable_absent = snapshot.modify_index == 0 and not snapshot.key_names
            if not variable_absent:
                raise HelperActionError("VARIABLE_REMAINS", "Nomad Variable remained after removal")
        return {
            "slug": application_slug,
            "removed": True,
            "jobAbsent": True,
            "variableAbsent": variable_absent,
        }

    return handle


def _public_health_from_job(
    application_slug: str,
    *,
    trusted_domain: str,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
) -> bool:
    if not isinstance(trusted_domain, str) or not trusted_domain or "\x00" in trusted_domain:
        raise HelperActionError("NOMAD_RESPONSE_INVALID", "trusted public health domain is invalid")
    expected_host = f"{application_slug}.{trusted_domain}"
    completed = command_runner(
        (*nomad_command, "job", "inspect", "-json", application_slug),
        timeout_seconds=timeout_seconds,
        stdout_limit=response_limit,
        stderr_limit=65_536,
        check=True,
    )
    value = _parse_json(completed.stdout, field="job inspection")
    groups = value.get("TaskGroups") if isinstance(value, dict) else None
    hosts: list[str] = []
    paths: list[str] = []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict) or group.get("Name") != "app":
                continue
            services = group.get("Services")
            if not isinstance(services, list):
                continue
            for service in services:
                if (
                    not isinstance(service, dict)
                    or service.get("Name") != f"app-{application_slug}"
                ):
                    continue
                tags = service.get("Tags")
                checks = service.get("Checks")
                if isinstance(tags, list):
                    for tag in tags:
                        if not isinstance(tag, str):
                            continue
                        match = re.fullmatch(
                            rf"traefik\.http\.routers\.{re.escape(application_slug)}\.rule="
                            rf"Host\(`{re.escape(expected_host)}`\)",
                            tag,
                        )
                        if match:
                            hosts.append(expected_host)
                if isinstance(checks, list):
                    for check in checks:
                        if isinstance(check, dict) and check.get("Type") == "http":
                            paths.append(health_path(check.get("Path")))
    if len(set(hosts)) != 1 or len(set(paths)) != 1:
        raise HelperActionError(
            "NOMAD_RESPONSE_INVALID", "Nomad job omitted exact public health evidence"
        )
    result = bounded_http(
        f"https://{hosts[0]}{paths[0]}",
        timeout_seconds=timeout_seconds,
        response_limit=4_096,
        allow_redirects=False,
    )
    return 200 <= result.status < 300


def _allocation_token(allocation: Mapping[str, Any]) -> tuple[object, ...]:
    task_state = allocation.get("TaskStates")
    app_state = task_state.get("app") if isinstance(task_state, dict) else None
    return (
        allocation.get("ID"),
        allocation.get("ModifyIndex"),
        allocation.get("ModifyTime"),
        app_state.get("Restarts") if isinstance(app_state, dict) else None,
        app_state.get("StartedAt") if isinstance(app_state, dict) else None,
    )


def _healthy_allocations(
    allocations: Sequence[Mapping[str, Any]], version: int
) -> list[Mapping[str, Any]]:
    selected = [
        item
        for item in allocations
        if item.get("JobVersion") == version and item.get("DesiredStatus") == "run"
    ]
    return [
        item
        for item in selected
        if item.get("ClientStatus") == "running"
        and isinstance(item.get("DeploymentStatus"), dict)
        and item["DeploymentStatus"].get("Healthy") is True
    ]


def _restart_and_observe_environment(
    application_slug: str,
    modify_index: int,
    baseline: Sequence[Mapping[str, Any]],
    client: VariableClient,
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
    attempts: int,
    poll_interval_seconds: float,
    public_health_check: Callable[[str], bool] | None,
    sleep: Callable[[float], None],
) -> int:
    baseline_tokens = {_allocation_token(item) for item in baseline}
    command_runner(
        (*nomad_command, "job", "restart", "-yes", application_slug),
        timeout_seconds=timeout_seconds,
        stdout_limit=65_536,
        stderr_limit=65_536,
        check=True,
    )
    for attempt in range(attempts):
        current_job = _inspected_candidate(
            application_slug,
            command_runner=command_runner,
            nomad_command=nomad_command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
        )
        if current_job is not None:
            version = current_job[0]
            allocations = _allocations(
                application_slug,
                command_runner=command_runner,
                nomad_command=nomad_command,
                timeout_seconds=timeout_seconds,
                response_limit=response_limit,
            )
            healthy = _healthy_allocations(allocations, version)
            restarted = healthy and any(
                _allocation_token(item) not in baseline_tokens for item in healthy
            )
            current = client.read_variable(variable_path(application_slug))
            if restarted and current.modify_index == modify_index:
                try:
                    publicly_healthy = public_health_check is None or public_health_check(
                        application_slug
                    )
                except Exception:
                    publicly_healthy = False
                if publicly_healthy:
                    return len(healthy)
        if attempt + 1 < attempts:
            sleep(poll_interval_seconds)
    raise HelperActionError(
        "ENVIRONMENT_HEALTH_FAILED",
        "environment change did not pass restart, scheduler, and public health",
    )


def _cas_environment(
    client: VariableClient,
    path: str,
    ownership: Mapping[str, str],
    *,
    updates: Mapping[str, str] | None = None,
    removals: Sequence[str] = (),
    maximum_keys: int,
    maximum_value_bytes: int,
) -> tuple[VariableSnapshot, int, tuple[str, ...]]:
    for attempt in range(3):
        previous = client.read_variable(path)
        merged = merge_owned_items(
            previous.items,
            ownership,
            owner="staff",
            updates=updates,
            removals=removals,
            maximum_keys=maximum_keys,
            maximum_value_bytes=maximum_value_bytes,
        )
        if not previous.items and not merged:
            return previous, previous.modify_index, ()
        try:
            index = client.compare_and_set(path, previous.modify_index, merged)
        except CasConflict:
            if attempt == 2:
                raise
            continue
        return previous, index, tuple(sorted(merged))
    raise AssertionError("bounded environment CAS did not return")


def _environment_handlers(
    client: VariableClient,
    *,
    command_runner: Callable[..., Any],
    nomad_command: tuple[str, ...],
    timeout_seconds: float,
    response_limit: int,
    maximum_keys: int,
    maximum_value_bytes: int,
    health_attempts: int,
    poll_interval_seconds: float,
    public_health_check: Callable[[str], bool] | None,
    sleep: Callable[[float], None],
) -> dict[str, Handler]:
    def set_values(args: Mapping[str, Any]) -> Mapping[str, Any]:
        _exact_args(args, {"slug", "updates", "ownership"}, "app.env.set")
        application_slug = slug(args["slug"])
        updates = args["updates"]
        if not isinstance(updates, dict):
            raise ValidationError("environment updates must be an object")
        validated_updates: dict[str, str] = {}
        for key, value in updates.items():
            key = env_key(key)
            validated_updates[key] = bounded_text(
                value,
                field=f"environment value for {key}",
                maximum=maximum_value_bytes,
            )
        return mutate(
            application_slug, _validated_ownership(args["ownership"]), validated_updates, ()
        )

    def remove_values(args: Mapping[str, Any]) -> Mapping[str, Any]:
        _exact_args(args, {"slug", "keys", "ownership"}, "app.env.remove")
        application_slug = slug(args["slug"])
        keys = args["keys"]
        if not isinstance(keys, list):
            raise ValidationError("environment keys must be an array")
        removals = [env_key(key) for key in keys]
        return mutate(application_slug, _validated_ownership(args["ownership"]), None, removals)

    def mutate(
        application_slug: str,
        ownership: Mapping[str, str],
        updates: Mapping[str, str] | None,
        removals: Sequence[str],
    ) -> Mapping[str, Any]:
        path = variable_path(application_slug)
        status = _status_or_absent(
            application_slug,
            command_runner=command_runner,
            nomad_command=nomad_command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
        )
        running = status is not None and status.get("Status") in {"pending", "running"}
        before = (
            _allocations(
                application_slug,
                command_runner=command_runner,
                nomad_command=nomad_command,
                timeout_seconds=timeout_seconds,
                response_limit=response_limit,
            )
            if running
            else []
        )
        previous, index, names = _cas_environment(
            client,
            path,
            ownership,
            updates=updates,
            removals=removals,
            maximum_keys=maximum_keys,
            maximum_value_bytes=maximum_value_bytes,
        )
        if not running:
            return {
                "slug": application_slug,
                "modifyIndex": index,
                "keys": list(names),
                "restarted": False,
                "schedulerHealthy": False,
                "publicHealthy": False,
                "allocations": 0,
            }
        try:
            allocations = _restart_and_observe_environment(
                application_slug,
                index,
                before,
                client,
                command_runner=command_runner,
                nomad_command=nomad_command,
                timeout_seconds=timeout_seconds,
                response_limit=response_limit,
                attempts=health_attempts,
                poll_interval_seconds=poll_interval_seconds,
                public_health_check=public_health_check,
                sleep=sleep,
            )
        except Exception as health_error:
            # Prior values intentionally live only in this helper invocation.
            # Interruptions (BaseException) bypass this block and are recovered
            # by observing key presence; ordinary health failure restores by CAS.
            try:
                rollback_index = client.compare_and_set(path, index, previous.items)
                rollback_baseline = _allocations(
                    application_slug,
                    command_runner=command_runner,
                    nomad_command=nomad_command,
                    timeout_seconds=timeout_seconds,
                    response_limit=response_limit,
                )
                _restart_and_observe_environment(
                    application_slug,
                    rollback_index,
                    rollback_baseline,
                    client,
                    command_runner=command_runner,
                    nomad_command=nomad_command,
                    timeout_seconds=timeout_seconds,
                    response_limit=response_limit,
                    attempts=health_attempts,
                    poll_interval_seconds=poll_interval_seconds,
                    public_health_check=public_health_check,
                    sleep=sleep,
                )
            except Exception as rollback_error:
                raise HelperActionError(
                    "ENVIRONMENT_ROLLBACK_FAILED",
                    "environment health and in-memory rollback verification both failed",
                ) from rollback_error
            raise HelperActionError(
                "ENVIRONMENT_HEALTH_FAILED",
                "environment health failed; prior values were restored from memory",
            ) from health_error
        return {
            "slug": application_slug,
            "modifyIndex": index,
            "keys": list(names),
            "restarted": True,
            "schedulerHealthy": True,
            "publicHealthy": public_health_check is not None,
            "allocations": allocations,
        }

    def list_values(args: Mapping[str, Any]) -> Mapping[str, Any]:
        _exact_args(args, {"slug"}, "app.env.list")
        application_slug = slug(args["slug"])
        snapshot = client.read_variable(variable_path(application_slug))
        return {
            "slug": application_slug,
            "modifyIndex": snapshot.modify_index,
            "keys": list(snapshot.key_names),
            "interruptionRecovery": "repeat set, or restore an unset value from staff-held material",
        }

    return {
        "app.env.set": set_values,
        "app.env.remove": remove_values,
        "app.env.list": list_values,
    }


def handlers(
    variable_client: VariableClient,
    *,
    nomad_command: Sequence[str],
    command_runner: Callable[..., Any] = run,
    timeout_seconds: float = 30,
    response_limit: int = 1_048_576,
    maximum_environment_keys: int = 128,
    maximum_environment_value_bytes: int = 65_536,
    environment_health_attempts: int = 90,
    environment_poll_interval_seconds: float = 2,
    public_health_check: Callable[[str], bool] | None = None,
    trusted_domain: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Handler]:
    """Return all application protocol-v1 handlers for integrator registration."""
    if (
        timeout_seconds <= 0
        or response_limit < 1
        or not 1 <= environment_health_attempts <= 300
        or not 0 <= environment_poll_interval_seconds <= 30
    ):
        raise ValueError("helper command bounds must be positive")
    command = _command(nomad_command)
    result: dict[str, Handler] = {
        "app.deploy": _deploy_handler(
            command_runner=command_runner,
            nomad_command=command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
        ),
        "app.health": _health_handler(
            command_runner=command_runner,
            nomad_command=command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
        ),
        "app.logs": _logs_handler(
            command_runner=command_runner,
            nomad_command=command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
        ),
        "app.remove": _remove_handler(
            variable_client,
            command_runner=command_runner,
            nomad_command=command,
            timeout_seconds=timeout_seconds,
        ),
    }
    if public_health_check is not None:
        selected_public_health_check = public_health_check
    else:
        # Test/integrator callers may supply their own public-health probe.  If
        # neither is supplied, defer the missing trusted identity to the first
        # probe rather than constructing an unsafe URL.
        selected_domain = trusted_domain or ""

        def selected_public_health_check(application_slug: str) -> bool:
            return _public_health_from_job(
                application_slug,
                trusted_domain=selected_domain,
                command_runner=command_runner,
                nomad_command=command,
                timeout_seconds=timeout_seconds,
                response_limit=response_limit,
            )

    result.update(
        _environment_handlers(
            variable_client,
            command_runner=command_runner,
            nomad_command=command,
            timeout_seconds=timeout_seconds,
            response_limit=response_limit,
            maximum_keys=maximum_environment_keys,
            maximum_value_bytes=maximum_environment_value_bytes,
            health_attempts=environment_health_attempts,
            poll_interval_seconds=environment_poll_interval_seconds,
            public_health_check=selected_public_health_check,
            sleep=sleep,
        )
    )
    return result
