"""Durable, exact allocation-exit witnesses for retained-worker cutovers.

The helper journals allocation identities before stopping a job and persists
positive client/task exit observations before replying. Missing Nomad records
are never substituted for an unobserved exit. A completed witness survives
lost replies and subsequent Nomad GC; unresolved loss requires worker fencing.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import durable
from ..runtime import CommandTimedOut, ensure_private_directory, lock, run
from ..validation import ValidationError, oci_digest_pin, sha256_hex, slug, uuid
from .application_actions import _inspected_job, _parse_json, inspected_job_identity
from .main import HelperActionError

_MAX_RECEIPT = 65_536


def quiesce(
    args: Mapping[str, Any],
    *,
    state_directory: Path,
    nomad_command: Sequence[str],
    command_runner: Callable[..., Any] = run,
    timeout_seconds: float = 90,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Mapping[str, Any]:
    if set(args) != {
        "operationId",
        "nodeId",
        "slug",
        "jobId",
        "candidateJobSha256",
        "candidateImage",
    }:
        raise HelperActionError("INVALID_ARGS", "app.quiesce arguments are invalid")
    identity = {
        "operationId": uuid(args["operationId"], field="operation ID"),
        "nodeId": uuid(args["nodeId"], field="worker Nomad node ID"),
        "slug": slug(args["slug"]),
        "candidateJobSha256": sha256_hex(args["candidateJobSha256"], field="candidate job SHA-256"),
        "candidateImage": oci_digest_pin(args["candidateImage"], field="candidate image"),
    }
    job_id = args["jobId"]
    if job_id not in {identity["slug"], f"{identity['slug']}-candidate"}:
        raise ValidationError("quiesce requires an exact application job ID")
    identity["jobId"] = job_id
    expected = identity["candidateJobSha256"], identity["candidateImage"]
    if not 0 < timeout_seconds <= 120:
        raise ValueError("quiesce timeout must be positive and at most 120 seconds")
    deadline = monotonic() + timeout_seconds
    command = tuple(nomad_command)
    root = ensure_private_directory(state_directory)
    key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    destination = root / f"{identity['operationId']}-{key}.json"

    def remaining() -> float:
        value = deadline - monotonic()
        if value <= 0:
            raise CommandTimedOut("application process stop was not confirmed before its deadline")
        return min(30.0, value)

    def inspect() -> Mapping[str, Any] | None:
        value = _inspected_job(
            job_id,
            command_runner=command_runner,
            nomad_command=command,
            timeout_seconds=remaining(),
            response_limit=262_144,
        )
        if value is not None:
            if inspected_job_identity(value, job_id)[1:] != expected:
                raise HelperActionError("CANDIDATE_MISMATCH", "predecessor job identity drifted")
            if type(value.get("Stop")) is not bool:
                raise HelperActionError(
                    "NOMAD_RESPONSE_INVALID", "predecessor stop state is malformed"
                )
        return value

    def allocations() -> dict[str, dict[str, Any]]:
        result = command_runner(
            (*command, "job", "allocs", "-all", "-json", job_id),
            timeout_seconds=remaining(),
            stdout_limit=1_048_576,
            stderr_limit=65_536,
            check=True,
        )
        rows = _parse_json(result.stdout, field="quiescing allocations")
        if not isinstance(rows, list) or len(rows) > 128:
            raise HelperActionError("NOMAD_RESPONSE_INVALID", "predecessor allocations are invalid")
        observed: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("JobID") != job_id:
                raise HelperActionError(
                    "NOMAD_RESPONSE_INVALID", "predecessor allocation identity drifted"
                )
            alloc_id = uuid(row.get("ID"), field="allocation ID")
            node_id = uuid(row.get("NodeID"), field="allocation node ID")
            version = row.get("JobVersion")
            if type(version) is not int or version < 0 or alloc_id in observed:
                raise HelperActionError(
                    "NOMAD_RESPONSE_INVALID", "allocation identity/version is malformed"
                )
            status = row.get("ClientStatus")
            exited = status in {"complete", "failed"}
            if exited:
                states = row.get("TaskStates")
                if (
                    not isinstance(states, dict)
                    or not states
                    or any(
                        not isinstance(s, dict) or s.get("State") != "dead" for s in states.values()
                    )
                ):
                    raise HelperActionError(
                        "STOP_UNCONFIRMED", "allocation task exit is unconfirmed"
                    )
            elif status not in {"pending", "running"}:
                raise HelperActionError(
                    "STOP_UNCONFIRMED", "allocation client exit is unknown; fencing may be required"
                )
            elif node_id != identity["nodeId"]:
                raise HelperActionError(
                    "CANDIDATE_MISMATCH", "live allocation belongs to a different worker"
                )
            observed[alloc_id] = {"nodeId": node_id, "jobVersion": version, "exited": exited}
        return observed

    def persist(record: Mapping[str, Any]) -> str:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        durable.atomic_write(destination, payload, mode=0o600, maximum_bytes=_MAX_RECEIPT)
        return hashlib.sha256(payload).hexdigest()

    def load_receipt() -> dict[str, Any] | None:
        try:
            fd = os.open(destination, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise HelperActionError(
                    "INVALID_STATE", "quiesce witness is not an owner-private file"
                )
            payload = stream.read(_MAX_RECEIPT + 1)
        if len(payload) > _MAX_RECEIPT:
            raise HelperActionError("INVALID_STATE", "quiesce witness exceeds its bound")
        record = _parse_json(payload, field="quiesce witness")
        if (
            not isinstance(record, dict)
            or set(record) != {"format", "identity", "allocations", "stopSeen", "complete"}
            or type(record["format"]) is not int
            or record["format"] != 1
            or record["identity"] != identity
            or type(record["stopSeen"]) is not bool
            or type(record["complete"]) is not bool
            or not isinstance(record["allocations"], dict)
            or not 1 <= len(record["allocations"]) <= 128
        ):
            raise HelperActionError("INVALID_STATE", "quiesce witness identity is invalid")
        for alloc_id, entry in record["allocations"].items():
            uuid(alloc_id, field="witness allocation ID")
            if not isinstance(entry, dict) or set(entry) != {"nodeId", "jobVersion", "exited"}:
                raise HelperActionError("INVALID_STATE", "quiesce allocation witness is invalid")
            uuid(entry["nodeId"], field="witness node ID")
            if (
                type(entry["jobVersion"]) is not int
                or entry["jobVersion"] < 0
                or type(entry["exited"]) is not bool
            ):
                raise HelperActionError("INVALID_STATE", "quiesce allocation witness is invalid")
        if record["complete"] and (
            not record["stopSeen"] or not all(e["exited"] for e in record["allocations"].values())
        ):
            raise HelperActionError(
                "INVALID_STATE", "quiesce completion lacks positive exit witnesses"
            )
        return record

    def merge(record: dict[str, Any], observed: Mapping[str, dict[str, Any]]) -> None:
        known = record["allocations"]
        for alloc_id, entry in observed.items():
            old = known.get(alloc_id)
            if old is not None and (
                old["nodeId"] != entry["nodeId"]
                or old["jobVersion"] != entry["jobVersion"]
                or (old["exited"] and not entry["exited"])
            ):
                raise HelperActionError(
                    "CANDIDATE_MISMATCH", "allocation witness drifted or restarted"
                )
            known[alloc_id] = entry
        if len(known) > 128:
            raise HelperActionError("INVALID_STATE", "quiesce allocation witness exceeds its bound")

    # Serialize late/lost-response retries independently of the caller process.
    with lock(root, f"app-{identity['operationId']}", deadline=time.monotonic() + remaining()):
        record = load_receipt()
        current = inspect()
        if record is not None and record["complete"]:
            if current is not None:
                if current["Stop"] is not True:
                    raise HelperActionError("CANDIDATE_MISMATCH", "quiesced job was reactivated")
                merge(record, allocations())
                if not all(e["exited"] for e in record["allocations"].values()):
                    raise HelperActionError(
                        "STOP_UNCONFIRMED", "quiesced job acquired a new live allocation"
                    )
            digest = persist(record)
        else:
            if current is None:
                raise HelperActionError(
                    "STOP_UNCONFIRMED", "unwitnessed job was lost; exact worker fencing is required"
                )
            observed = allocations()
            if record is None:
                if not observed:
                    raise HelperActionError(
                        "STOP_UNCONFIRMED", "empty allocation inventory is not exit evidence"
                    )
                record = {
                    "format": 1,
                    "identity": identity,
                    "allocations": {},
                    "stopSeen": False,
                    "complete": False,
                }
            merge(record, observed)
            persist(record)  # Intent and exact allocation identities precede stop.
            if current["Stop"] is False:
                command_runner(
                    (*command, "job", "stop", "-detach", "-yes", job_id),
                    timeout_seconds=remaining(),
                    stdout_limit=65_536,
                    stderr_limit=65_536,
                    check=True,
                )
            while True:
                current = inspect()
                if current is None or current["Stop"] is not True:
                    raise HelperActionError(
                        "STOP_UNCONFIRMED", "job stop is unconfirmed; fencing may be required"
                    )
                observed = allocations()
                merge(record, observed)
                # A vanished, previously live allocation cannot be guessed dead.
                if any(
                    not entry["exited"] and alloc_id not in observed
                    for alloc_id, entry in record["allocations"].items()
                ):
                    persist(record)
                    raise HelperActionError(
                        "STOP_UNCONFIRMED",
                        "unwitnessed allocation was lost; exact worker fencing is required",
                    )
                record["stopSeen"] = True
                record["complete"] = bool(observed) and all(
                    e["exited"] for e in record["allocations"].values()
                )
                digest = persist(record)
                if record["complete"]:
                    break
                sleep(min(1.0, remaining()))
        return {**identity, "jobStopped": True, "allocationsStopped": True, "receiptSha256": digest}
