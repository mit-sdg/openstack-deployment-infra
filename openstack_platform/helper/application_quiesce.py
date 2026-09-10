"""Stop an exact application job without discarding evidence of process exit.

A stopped job is deliberately not purged here. The controller must durably
checkpoint this evidence before removing the job or reusing its worker.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..runtime import CommandTimedOut, run
from ..validation import ValidationError, oci_digest_pin, sha256_hex, slug
from .application_actions import _inspected_job, _parse_json, inspected_job_identity
from .main import HelperActionError


def quiesce(
    args: Mapping[str, Any],
    *,
    nomad_command: Sequence[str],
    command_runner: Callable[..., Any] = run,
    timeout_seconds: float = 90,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Mapping[str, Any]:
    if set(args) != {"slug", "jobId", "candidateJobSha256", "candidateImage"}:
        raise HelperActionError("INVALID_ARGS", "app.quiesce arguments are invalid")
    application_slug = slug(args["slug"])
    job_id = args["jobId"]
    if job_id not in {application_slug, f"{application_slug}-candidate"}:
        raise ValidationError("quiesce requires an exact application job ID")
    expected = (
        sha256_hex(args["candidateJobSha256"], field="candidate job SHA-256"),
        oci_digest_pin(args["candidateImage"], field="candidate image"),
    )
    if not 0 < timeout_seconds <= 120:
        raise ValueError("quiesce timeout must be positive and at most 120 seconds")
    deadline = monotonic() + timeout_seconds
    command = tuple(nomad_command)

    def remaining() -> float:
        value = deadline - monotonic()
        if value <= 0:
            raise CommandTimedOut("application process stop was not confirmed before its deadline")
        return min(30.0, value)

    def inspect() -> Mapping[str, Any]:
        value = _inspected_job(
            job_id,
            command_runner=command_runner,
            nomad_command=command,
            timeout_seconds=remaining(),
            response_limit=262_144,
        )
        # Absence is not process-exit evidence: a previous purge could have
        # removed the server-side record while a disconnected client still ran.
        if value is None:
            raise HelperActionError("STOP_UNCONFIRMED", "exact predecessor job is unavailable")
        if inspected_job_identity(value, job_id)[1:] != expected:
            raise HelperActionError("CANDIDATE_MISMATCH", "predecessor job identity drifted")
        if type(value.get("Stop")) is not bool:
            raise HelperActionError("NOMAD_RESPONSE_INVALID", "predecessor stop state is malformed")
        return value

    before = inspect()
    if before["Stop"] is False:
        command_runner(
            (*command, "job", "stop", "-detach", "-yes", job_id),
            timeout_seconds=remaining(),
            stdout_limit=65_536,
            stderr_limit=65_536,
            check=True,
        )
    while True:
        current = inspect()
        if current["Stop"] is not True:
            raise HelperActionError("STOP_UNCONFIRMED", "predecessor job was not stopped")
        result = command_runner(
            (*command, "job", "allocs", "-all", "-json", job_id),
            timeout_seconds=remaining(),
            stdout_limit=1_048_576,
            stderr_limit=65_536,
            check=True,
        )
        allocations = _parse_json(result.stdout, field="quiescing allocations")
        if not isinstance(allocations, list) or len(allocations) > 128:
            raise HelperActionError("NOMAD_RESPONSE_INVALID", "predecessor allocations are invalid")
        stopped = True
        for allocation in allocations:
            if not isinstance(allocation, dict) or allocation.get("JobID") != job_id:
                raise HelperActionError(
                    "NOMAD_RESPONSE_INVALID", "predecessor allocation identity drifted"
                )
            status = allocation.get("ClientStatus")
            if status in {"pending", "running"}:
                stopped = False
                continue
            # DesiredStatus=stop and ClientStatus=lost are not proof of exit.
            if status not in {"complete", "failed"}:
                raise HelperActionError("STOP_UNCONFIRMED", "predecessor client exit is unknown")
            states = allocation.get("TaskStates")
            if (
                not isinstance(states, dict)
                or not states
                or any(
                    not isinstance(state, dict) or state.get("State") != "dead"
                    for state in states.values()
                )
            ):
                raise HelperActionError("STOP_UNCONFIRMED", "predecessor task exit is unconfirmed")
        if stopped:
            if inspect()["Stop"] is not True:
                raise HelperActionError("STOP_UNCONFIRMED", "predecessor stop state changed")
            return {
                "slug": application_slug,
                "jobId": job_id,
                "candidateJobSha256": expected[0],
                "candidateImage": expected[1],
                "jobStopped": True,
                "allocationsStopped": True,
            }
        sleep(min(1.0, remaining()))
