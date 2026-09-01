"""Disposable, deployment-scoped live acceptance orchestration.

The cloud-specific implementation is deliberately outside this repository: a protected
runner supplies a small JSON driver.  This module owns the safety boundary, plan binding,
checkpoints, evidence minimisation, and release-gate semantics.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from openstack_platform.runtime import ensure_private_directory

SCHEMA_VERSION = 1
MAX_DRIVER_OUTPUT = 256 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024
_NAMESPACE = re.compile(r"acceptance-[a-z0-9](?:[a-z0-9-]{0,10}[a-z0-9])?-[0-9a-f]{8}")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")

# The ordering is part of the gate.  In particular, the injected interruption is
# checkpointed separately from the same-operation resume, and cleanup is last.
ACTION_CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "greenfield_setup",
        ("emptyScopeObserved", "planApplied", "platformHealthy", "deploymentScoped"),
    ),
    ("application_create", ("applicationCreated", "disabledInitially")),
    ("postgres_lifecycle", ("created", "resourceVerified")),
    ("mongo_lifecycle", ("created", "resourceVerified")),
    ("s3_lifecycle", ("created", "resourceVerified")),
    (
        "application_deploy",
        (
            "exactCommitDeployed",
            "candidateVerified",
            "publicRouteHealthy",
            "storageBound",
            "postgresWriteReadVerified",
            "mongoWriteReadVerified",
            "s3WriteReadVerified",
        ),
    ),
    (
        "interrupted_resume_injection",
        ("durableOperationStarted", "interruptionInjected", "stableServiceUnaffected"),
    ),
    (
        "interrupted_resume",
        ("sameOperationResumed", "operationConverged", "noDuplicateResource"),
    ),
    (
        "application_disable_enable",
        ("disabledWithoutDataLoss", "enabledWithoutRebuild", "publicRouteHealthy"),
    ),
    (
        "operator_sqlite_restore",
        ("encryptedBackupVerified", "offlineRestoreVerified", "deploymentIdentityMatched"),
    ),
    (
        "hosted_sqlite_restore",
        ("encryptedBackupVerified", "offlineRestoreVerified", "controllerReady"),
    ),
    (
        "managed_data_restore",
        ("postgresRestored", "mongoRestored", "s3Restored", "restoreManifestVerified"),
    ),
    (
        "persistent_host_replacement",
        (
            "oldHostRetainedUntilReady",
            "exactIdentityVerified",
            "postgresContentRetained",
            "mongoContentRetained",
            "s3ContentRetained",
            "dataRetained",
        ),
    ),
    (
        "admin_recovery",
        ("externalRecoveryUsed", "controllerStateRetained", "applicationReconciled"),
    ),
    (
        "application_delete",
        ("applicationAbsent", "postgresAbsent", "mongoAbsent", "s3Absent"),
    ),
    (
        "cleanup_verify",
        ("ownedResourcesAbsent", "unrelatedFingerprintUnchanged", "backupsDispositionRecorded"),
    ),
)
ACTION_NAMES = tuple(name for name, _checks in ACTION_CHECKS)


class AcceptanceError(RuntimeError):
    """An operator-safe acceptance failure."""


class Driver(Protocol):
    def request(self, document: Mapping[str, object], *, timeout: int) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class Plan:
    deployment_id: str
    project_id: str
    namespace: str
    driver_sha256: str
    driver_configuration_sha256: str
    baseline_fingerprint: str
    created_at: str
    expires_at: str
    max_minutes: int
    step_timeout_seconds: int

    def document(self) -> dict[str, object]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": "live-acceptance-plan",
            "deploymentId": self.deployment_id,
            "projectId": self.project_id,
            "namespace": self.namespace,
            "driverSha256": self.driver_sha256,
            "driverConfigurationSha256": self.driver_configuration_sha256,
            "baselineFingerprint": self.baseline_fingerprint,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "maxMinutes": self.max_minutes,
            "stepTimeoutSeconds": self.step_timeout_seconds,
            "actions": [
                {"name": action, "requiredChecks": list(checks)} for action, checks in ACTION_CHECKS
            ],
        }


class SubprocessDriver:
    def __init__(self, executable: Path) -> None:
        self.executable = executable

    def request(self, document: Mapping[str, object], *, timeout: int) -> Mapping[str, object]:
        payload = _canonical(document)
        try:
            completed = subprocess.run(
                (str(self.executable),),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AcceptanceError(
                "protected acceptance driver was unavailable or timed out"
            ) from error
        if completed.returncode != 0:
            raise AcceptanceError("protected acceptance driver rejected the bounded request")
        if len(completed.stdout) > MAX_DRIVER_OUTPUT:
            raise AcceptanceError("protected acceptance driver output exceeded the limit")
        document_out = _load_json(completed.stdout, "driver response")
        if not isinstance(document_out, dict):
            raise AcceptanceError("protected acceptance driver returned a non-object")
        return document_out


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AcceptanceError("JSON contains a duplicate key")
        result[key] = value
    return result


def _load_json(data: bytes, label: str) -> object:
    try:
        return json.loads(data, object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"{label} is not strict JSON") from error


def _canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AcceptanceError("plan timestamp is invalid")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise AcceptanceError("plan timestamp is invalid") from error


def _validate_identity(deployment_id: str, project_id: str, namespace: str) -> tuple[str, str]:
    try:
        deployment = str(UUID(deployment_id))
        project = str(UUID(project_id))
    except ValueError as error:
        raise AcceptanceError("deployment and project IDs must be canonical UUIDs") from error
    if deployment != deployment_id or project != project_id:
        raise AcceptanceError("deployment and project IDs must be canonical UUIDs")
    if not _NAMESPACE.fullmatch(namespace) or not namespace.endswith(deployment[:8]):
        raise AcceptanceError(
            "namespace must be disposable acceptance-<label>-<deployment UUID first eight characters>"
        )
    return deployment, project


def _validate_executable(path: Path) -> None:
    if not path.is_absolute():
        raise AcceptanceError("acceptance driver path must be absolute")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise AcceptanceError("acceptance driver must be a direct regular file")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise AcceptanceError("acceptance driver must be owned by root or the current user")
    if metadata.st_mode & 0o022 or not os.access(path, os.X_OK):
        raise AcceptanceError("acceptance driver must be executable and not group/world writable")


def _atomic_private_write(path: Path, data: bytes) -> None:
    if len(data) > MAX_EVIDENCE_BYTES:
        raise AcceptanceError("acceptance state exceeded its bound")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _private_read(path: Path, label: str) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise AcceptanceError(f"{label} must be a direct current-user-owned mode-0600 file")
    data = path.read_bytes()
    if len(data) > MAX_EVIDENCE_BYTES:
        raise AcceptanceError(f"{label} exceeds its size bound")
    return data


def _request_base(plan: Plan, mode: str, action: str) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "mode": mode,
        "action": action,
        "scope": {
            "deploymentId": plan.deployment_id,
            "projectId": plan.project_id,
            "namespace": plan.namespace,
        },
    }


def create_plan(
    driver: Driver,
    *,
    deployment_id: str,
    project_id: str,
    namespace: str,
    driver_sha256: str,
    max_minutes: int,
    step_timeout_seconds: int,
) -> Plan:
    deployment_id, project_id = _validate_identity(deployment_id, project_id, namespace)
    if not _FINGERPRINT.fullmatch(driver_sha256):
        raise AcceptanceError("driver checksum is invalid")
    if not 15 <= max_minutes <= 720 or not 30 <= step_timeout_seconds <= 3600:
        raise AcceptanceError("acceptance bounds are outside the allowed range")
    now = _now()
    provisional = Plan(
        deployment_id,
        project_id,
        namespace,
        driver_sha256,
        "0" * 64,
        "0" * 64,
        _timestamp(now),
        _timestamp(now + timedelta(hours=24)),
        max_minutes,
        step_timeout_seconds,
    )
    response = driver.request(
        {
            **_request_base(provisional, "plan", "full_drill"),
            "requiredActions": list(ACTION_NAMES),
            "bounds": {
                "maxMinutes": max_minutes,
                "stepTimeoutSeconds": step_timeout_seconds,
            },
        },
        timeout=min(step_timeout_seconds, 300),
    )
    expected = {
        "schemaVersion",
        "ok",
        "deploymentId",
        "projectId",
        "namespace",
        "capabilities",
        "driverConfigurationSha256",
        "baselineFingerprint",
        "ownedResources",
    }
    if set(response) != expected or response.get("schemaVersion") != SCHEMA_VERSION:
        raise AcceptanceError("driver plan response has an unexpected shape")
    if response.get("ok") is not True:
        raise AcceptanceError("driver preflight did not accept the live drill")
    if (
        response.get("deploymentId") != deployment_id
        or response.get("projectId") != project_id
        or response.get("namespace") != namespace
    ):
        raise AcceptanceError("driver preflight returned a different deployment scope")
    if response.get("capabilities") != list(ACTION_NAMES):
        raise AcceptanceError("driver does not implement the exact live-acceptance action set")
    if response.get("ownedResources") != []:
        raise AcceptanceError("greenfield scope already contains deployment-owned resources")
    configuration_sha = response.get("driverConfigurationSha256")
    if not isinstance(configuration_sha, str) or not _FINGERPRINT.fullmatch(configuration_sha):
        raise AcceptanceError("driver returned an invalid protected configuration checksum")
    baseline = response.get("baselineFingerprint")
    if not isinstance(baseline, str) or not _FINGERPRINT.fullmatch(baseline):
        raise AcceptanceError("driver returned an invalid unrelated-resource fingerprint")
    return Plan(
        deployment_id,
        project_id,
        namespace,
        driver_sha256,
        configuration_sha,
        baseline,
        provisional.created_at,
        provisional.expires_at,
        max_minutes,
        step_timeout_seconds,
    )


def _plan_from_document(document: object, *, allow_expired_resume: bool = False) -> Plan:
    if not isinstance(document, dict):
        raise AcceptanceError("plan must be an object")
    expected = {
        "schemaVersion",
        "kind",
        "deploymentId",
        "projectId",
        "namespace",
        "driverSha256",
        "driverConfigurationSha256",
        "baselineFingerprint",
        "createdAt",
        "expiresAt",
        "maxMinutes",
        "stepTimeoutSeconds",
        "actions",
    }
    if set(document) != expected:
        raise AcceptanceError("plan has an unexpected shape")
    expected_actions = [
        {"name": name, "requiredChecks": list(checks)} for name, checks in ACTION_CHECKS
    ]
    if (
        document.get("schemaVersion") != SCHEMA_VERSION
        or document.get("kind") != "live-acceptance-plan"
        or document.get("actions") != expected_actions
    ):
        raise AcceptanceError("plan version or action contract is unsupported")
    fields = (
        "deploymentId",
        "projectId",
        "namespace",
        "driverSha256",
        "driverConfigurationSha256",
        "baselineFingerprint",
    )
    if any(not isinstance(document.get(field), str) for field in fields):
        raise AcceptanceError("plan identity is invalid")
    max_minutes = document.get("maxMinutes")
    timeout = document.get("stepTimeoutSeconds")
    if not isinstance(max_minutes, int) or isinstance(max_minutes, bool):
        raise AcceptanceError("plan duration is invalid")
    if not isinstance(timeout, int) or isinstance(timeout, bool):
        raise AcceptanceError("plan timeout is invalid")
    plan = Plan(
        str(document["deploymentId"]),
        str(document["projectId"]),
        str(document["namespace"]),
        str(document["driverSha256"]),
        str(document["driverConfigurationSha256"]),
        str(document["baselineFingerprint"]),
        str(document["createdAt"]),
        str(document["expiresAt"]),
        max_minutes,
        timeout,
    )
    _validate_identity(plan.deployment_id, plan.project_id, plan.namespace)
    if (
        not _FINGERPRINT.fullmatch(plan.driver_sha256)
        or not _FINGERPRINT.fullmatch(plan.driver_configuration_sha256)
        or not _FINGERPRINT.fullmatch(plan.baseline_fingerprint)
    ):
        raise AcceptanceError("plan checksum is invalid")
    if not 15 <= plan.max_minutes <= 720 or not 30 <= plan.step_timeout_seconds <= 3600:
        raise AcceptanceError("plan bounds are invalid")
    _parse_time(plan.created_at)
    if not allow_expired_resume and _now() > _parse_time(plan.expires_at):
        raise AcceptanceError("plan expired; create and review a new plan")
    return plan


def _accepted_checks(
    response: Mapping[str, object], plan: Plan, action: str, required: Sequence[str]
) -> dict[str, bool]:
    expected = {
        "schemaVersion",
        "ok",
        "deploymentId",
        "projectId",
        "namespace",
        "action",
        "checks",
    }
    if set(response) != expected or response.get("schemaVersion") != SCHEMA_VERSION:
        raise AcceptanceError(f"driver response for {action} has an unexpected shape")
    if (
        response.get("ok") is not True
        or response.get("deploymentId") != plan.deployment_id
        or response.get("projectId") != plan.project_id
        or response.get("namespace") != plan.namespace
        or response.get("action") != action
    ):
        raise AcceptanceError(f"driver did not accept the exact scope for {action}")
    checks = response.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(required):
        raise AcceptanceError(f"driver returned incomplete checks for {action}")
    if any(checks.get(name) is not True for name in required):
        raise AcceptanceError(f"a required live check failed for {action}")
    # Only fixed check names and booleans cross into retained evidence. Provider
    # IDs, command output, credentials, and response payloads are never retained.
    return {name: True for name in required}


def _checkpoint_events(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > len(ACTION_CHECKS):
        raise AcceptanceError("checkpoint contains an invalid event list")
    events: list[dict[str, object]] = []
    previous = "0" * 64
    for index, raw_event in enumerate(value):
        if not isinstance(raw_event, dict):
            raise AcceptanceError("checkpoint event is invalid")
        action, required = ACTION_CHECKS[index]
        if set(raw_event) != {
            "sequence",
            "action",
            "completedAt",
            "checks",
            "previousSha256",
            "eventSha256",
        }:
            raise AcceptanceError("checkpoint event has an unexpected shape")
        checks = raw_event.get("checks")
        if (
            raw_event.get("sequence") != index + 1
            or raw_event.get("action") != action
            or raw_event.get("previousSha256") != previous
            or not isinstance(checks, dict)
            or set(checks) != set(required)
            or any(checks.get(name) is not True for name in required)
        ):
            raise AcceptanceError("checkpoint event ordering or checks are invalid")
        _parse_time(raw_event.get("completedAt"))
        without_hash = {key: item for key, item in raw_event.items() if key != "eventSha256"}
        event_hash = raw_event.get("eventSha256")
        if not isinstance(event_hash, str) or not hmac.compare_digest(
            event_hash, _sha256(_canonical(without_hash))
        ):
            raise AcceptanceError("checkpoint event hash verification failed")
        event = dict(raw_event)
        events.append(event)
        previous = event_hash
    return events


def run_plan(
    driver: Driver,
    plan: Plan,
    *,
    plan_sha256: str,
    state_directory: Path,
    signing_key: bytes,
    monotonic: Any = time.monotonic,
) -> Path:
    if len(signing_key) < 32:
        raise AcceptanceError("evidence signing key must contain at least 32 bytes")
    state_directory = ensure_private_directory(state_directory)
    lock_path = state_directory / "run.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(lock_fd)
        raise AcceptanceError("another acceptance run holds this state directory") from error
    try:
        state_path = state_directory / "checkpoint.json"
        if state_path.exists():
            state = _load_json(_private_read(state_path, "checkpoint"), "checkpoint")
            if not isinstance(state, dict):
                raise AcceptanceError("checkpoint has an invalid shape")
        else:
            state = {
                "schemaVersion": SCHEMA_VERSION,
                "deploymentId": plan.deployment_id,
                "planSha256": plan_sha256,
                "startedAt": _timestamp(_now()),
                "events": [],
            }
        if (
            set(state) != {"schemaVersion", "deploymentId", "planSha256", "startedAt", "events"}
            or state.get("schemaVersion") != SCHEMA_VERSION
            or state.get("deploymentId") != plan.deployment_id
            or state.get("planSha256") != plan_sha256
        ):
            raise AcceptanceError("checkpoint is not bound to this exact plan")
        _parse_time(state.get("startedAt"))
        events = _checkpoint_events(state.get("events"))
        state["events"] = events
        started = monotonic()
        for action, required in ACTION_CHECKS[len(events) :]:
            elapsed = monotonic() - started
            if elapsed > plan.max_minutes * 60:
                raise AcceptanceError("acceptance run reached its total duration bound")
            response = driver.request(
                {
                    **_request_base(plan, "execute", action),
                    "planSha256": plan_sha256,
                    "driverConfigurationSha256": plan.driver_configuration_sha256,
                    "baselineFingerprint": plan.baseline_fingerprint,
                },
                timeout=min(
                    plan.step_timeout_seconds, max(1, int(plan.max_minutes * 60 - elapsed))
                ),
            )
            checks = _accepted_checks(response, plan, action, required)
            previous_value = events[-1]["eventSha256"] if events else "0" * 64
            if not isinstance(previous_value, str):
                raise AcceptanceError("checkpoint event hash is invalid")
            previous = previous_value
            event_without_hash = {
                "sequence": len(events) + 1,
                "action": action,
                "completedAt": _timestamp(_now()),
                "checks": checks,
                "previousSha256": previous,
            }
            event = {**event_without_hash, "eventSha256": _sha256(_canonical(event_without_hash))}
            events.append(event)
            _atomic_private_write(state_path, _canonical(state) + b"\n")

        evidence = {
            "schemaVersion": SCHEMA_VERSION,
            "kind": "live-acceptance-evidence",
            "deploymentId": plan.deployment_id,
            "projectId": plan.project_id,
            "namespace": plan.namespace,
            "planSha256": plan_sha256,
            "driverSha256": plan.driver_sha256,
            "driverConfigurationSha256": plan.driver_configuration_sha256,
            "baselineFingerprint": plan.baseline_fingerprint,
            "startedAt": state["startedAt"],
            "completedAt": _timestamp(_now()),
            "result": "passed",
            "events": events,
            "sanitization": "fixed-checks-only-v1",
        }
        evidence_bytes = _canonical(evidence) + b"\n"
        evidence_path = state_directory / "evidence.json"
        _atomic_private_write(evidence_path, evidence_bytes)
        checksum = _sha256(evidence_bytes)
        _atomic_private_write(
            state_directory / "evidence.json.sha256", f"{checksum}  evidence.json\n".encode()
        )
        signature = hmac.new(signing_key, evidence_bytes, hashlib.sha256).hexdigest()
        _atomic_private_write(
            state_directory / "evidence.json.hmac-sha256",
            f"{signature}  evidence.json\n".encode(),
        )
        return evidence_path
    finally:
        os.close(lock_fd)


def verify_evidence(directory: Path, signing_key: bytes) -> None:
    if len(signing_key) < 32:
        raise AcceptanceError("evidence signing key must contain at least 32 bytes")
    evidence = _private_read(directory / "evidence.json", "evidence")
    checksum_text = _private_read(directory / "evidence.json.sha256", "evidence checksum").decode()
    signature_text = _private_read(
        directory / "evidence.json.hmac-sha256", "evidence signature"
    ).decode()
    checksum = checksum_text.split()[0] if checksum_text.split() else ""
    signature = signature_text.split()[0] if signature_text.split() else ""
    if not hmac.compare_digest(checksum, _sha256(evidence)):
        raise AcceptanceError("evidence checksum verification failed")
    expected = hmac.new(signing_key, evidence, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise AcceptanceError("evidence signature verification failed")
    document = _load_json(evidence, "evidence")
    expected_fields = {
        "schemaVersion",
        "kind",
        "deploymentId",
        "projectId",
        "namespace",
        "planSha256",
        "driverSha256",
        "driverConfigurationSha256",
        "baselineFingerprint",
        "startedAt",
        "completedAt",
        "result",
        "events",
        "sanitization",
    }
    if (
        not isinstance(document, dict)
        or set(document) != expected_fields
        or document.get("schemaVersion") != SCHEMA_VERSION
        or document.get("kind") != "live-acceptance-evidence"
        or document.get("result") != "passed"
        or document.get("sanitization") != "fixed-checks-only-v1"
    ):
        raise AcceptanceError("evidence does not contain a valid passing gate result")
    deployment = document.get("deploymentId")
    project = document.get("projectId")
    namespace = document.get("namespace")
    if (
        not isinstance(deployment, str)
        or not isinstance(project, str)
        or not isinstance(namespace, str)
    ):
        raise AcceptanceError("evidence scope is invalid")
    _validate_identity(deployment, project, namespace)
    for field in (
        "planSha256",
        "driverSha256",
        "driverConfigurationSha256",
        "baselineFingerprint",
    ):
        value = document.get(field)
        if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
            raise AcceptanceError("evidence fingerprint is invalid")
    _parse_time(document.get("startedAt"))
    _parse_time(document.get("completedAt"))
    events = _checkpoint_events(document.get("events"))
    if len(events) != len(ACTION_CHECKS):
        raise AcceptanceError("evidence action set is incomplete")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openstack-platform-acceptance")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser(
        "plan", help="perform non-mutating preflight and write a reviewable plan"
    )
    plan.add_argument("--deployment-id", required=True)
    plan.add_argument("--project-id", required=True)
    plan.add_argument("--namespace", required=True)
    plan.add_argument("--driver", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--max-minutes", type=int, default=360)
    plan.add_argument("--step-timeout-seconds", type=int, default=1800)
    run = commands.add_parser("run", help="apply or resume one exact reviewed plan")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--driver", type=Path, required=True)
    run.add_argument("--state-directory", type=Path, required=True)
    run.add_argument("--signing-key", type=Path, required=True)
    run.add_argument("--apply", action="store_true")
    run.add_argument("--confirm", required=True)
    verify = commands.add_parser("verify", help="verify retained evidence checksum and signature")
    verify.add_argument("--evidence-directory", type=Path, required=True)
    verify.add_argument("--signing-key", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            key = _private_read(args.signing_key, "signing key")
            verify_evidence(args.evidence_directory, key)
            print("live-acceptance-evidence=verified result=passed")
            return 0
        _validate_executable(args.driver)
        driver = SubprocessDriver(args.driver)
        if args.command == "plan":
            output_parent = ensure_private_directory(args.output.parent)
            output = output_parent / args.output.name
            if output.exists() or output.is_symlink():
                raise AcceptanceError("plan output already exists; choose a new private path")
            plan = create_plan(
                driver,
                deployment_id=args.deployment_id,
                project_id=args.project_id,
                namespace=args.namespace,
                driver_sha256=_file_sha256(args.driver),
                max_minutes=args.max_minutes,
                step_timeout_seconds=args.step_timeout_seconds,
            )
            plan_bytes = _canonical(plan.document()) + b"\n"
            _atomic_private_write(output, plan_bytes)
            print(
                f"live-acceptance-plan={output} sha256={_sha256(plan_bytes)} actions={len(ACTION_NAMES)}"
            )
            return 0
        if not args.apply or os.environ.get("LIVE_ACCEPTANCE_APPLY") != "1":
            raise AcceptanceError("live run requires --apply and LIVE_ACCEPTANCE_APPLY=1")
        plan_bytes = _private_read(args.plan, "plan")
        checkpoint_exists = (args.state_directory / "checkpoint.json").exists()
        plan = _plan_from_document(
            _load_json(plan_bytes, "plan"), allow_expired_resume=checkpoint_exists
        )
        if args.confirm != f"LIVE-ACCEPTANCE:{plan.deployment_id}":
            raise AcceptanceError(
                "confirmation must exactly match LIVE-ACCEPTANCE:<deployment UUID>"
            )
        if _file_sha256(args.driver) != plan.driver_sha256:
            raise AcceptanceError("protected driver changed after the reviewed plan")
        key = _private_read(args.signing_key, "signing key")
        result = run_plan(
            driver,
            plan,
            plan_sha256=_sha256(plan_bytes),
            state_directory=args.state_directory,
            signing_key=key,
        )
        print(f"live-acceptance=passed evidence={result}")
        return 0
    except (AcceptanceError, OSError) as error:
        # AcceptanceError messages are fixed; OSError details may contain a private path.
        message = (
            str(error)
            if isinstance(error, AcceptanceError)
            else "private acceptance file operation failed"
        )
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
