"""Bounded, read-only evidence acquisition for the operator dashboard.

Every source in this module observes; none mutates:

* One pinned SSH round trip runs a fixed reader on admin. It sends only ``GET``
  requests to the privileged controller socket's database-backed routes, reads
  the platform-health timer's secret-free snapshot, and asks systemd whether
  the controller units are active. It deliberately avoids ``/v1/admin/status``
  and ``/v1/admin/hosts``: those run live provider and helper observations
  while holding the controller's shared API lock.
* Credential-free HTTPS probes check public ingress and accepted application
  routes with the same contract as deployment acceptance: no redirects, the
  configured health path, and the exact ``X-Platform-Deployment`` marker.

Remote output is untrusted input. Parsers keep allowlisted fields with strict
types and drop malformed records instead of passing them to the browser.
"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import remote, runtime
from ..config import PlatformConfig
from ..contracts import DEPLOYMENT_ROUTE_HEADER, IMAGE_ROLES
from ..validation import ValidationError, commit, health_path, repository_url, slug, uuid

READER_VERSION = 1
# Each admin deployment page rebuilds the complete attempt list inside the
# controller, so keep the per-refresh page budget small and fixed.
DEPLOYMENT_PAGES = 2
# Unfinished operations can sit behind newer terminal ones; read three pages.
OPERATION_PAGES = 3
ADMIN_READ_LIMIT = 16 * 1_048_576
HEALTH_STATUS_LIMIT = 65_536
_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_TIMESTAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d{1,6})?(?:Z|\+00:00)")
_KIND = re.compile(r"[a-z][a-z0-9.-]{0,63}")
_PHASE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")
_SCOPE = re.compile(r"infrastructure|app-[0-9a-f-]{36}")
_STATE_WORD = re.compile(r"[a-z][a-z_-]{0,31}")
_DISPLAY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_FLAVOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_IMAGE_DIGEST = re.compile(r"[^\s/@]+(?:/[^\s/@]+)+@sha256:[0-9a-f]{64}")
_FEATURE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ERROR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}")
_DEPLOYMENT_STATUSES = frozenset(
    {
        "queued",
        "building",
        "deploying",
        "succeeded",
        "failed",
        "recovery_required",
    }
)
_OPERATION_STATUSES = frozenset({"running", "succeeded", "failed", "recovery_required"})
_RESOURCE_TYPES = frozenset({"postgres", "mongo", "s3"})
_RESOURCE_STATES = frozenset({"creating", "active", "removing", "recovery_required"})
_HOST_STATES = frozenset({"active", "building", "error", "missing", "stopped", "unknown"})
_RUNTIMES = frozenset({"bun", "node"})
_QUOTA_KEYS = ("postgresConnections", "measuredTargetBytes", "s3Bytes", "s3Objects")

# The reader executes with the admin guest's system Python as the operator
# account already admitted to privileged.sock. Its argv is fixed except for the
# socket and units derived from the validated namespace, the inventory-derived
# snapshot path, and the page budget.
ADMIN_READER = r"""
import json
import re
import socket
import subprocess
import sys
from http.client import HTTPConnection

SOCKET = sys.argv[1]
NAMESPACE = sys.argv[2]
STATUS_PATH = sys.argv[3]
DEPLOYMENT_PAGES = int(sys.argv[4])
OPERATION_PAGES = int(sys.argv[5])
BODY_LIMIT = 4194304
CURSOR = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Connection(HTTPConnection):
    def connect(self):
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream.settimeout(self.timeout)
        stream.connect(SOCKET)
        self.sock = stream


def get(path):
    connection = Connection("localhost", timeout=30)
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        status = response.status
        body = response.read(BODY_LIMIT + 1)
    except Exception as error:
        return {"error": type(error).__name__}
    finally:
        connection.close()
    if len(body) > BODY_LIMIT:
        return {"error": "ResponseTooLarge"}
    try:
        return {"status": status, "body": json.loads(body)}
    except ValueError:
        return {"status": status, "error": "InvalidJson"}


def pages(path, maximum):
    items = []
    cursor = None
    for _ in range(maximum):
        query = "?limit=100" if cursor is None else "?limit=100&cursor=" + cursor
        result = get(path + query)
        body = result.get("body")
        if (
            result.get("status") != 200
            or not isinstance(body, dict)
            or not isinstance(body.get("items"), list)
        ):
            return result
        items.extend(body["items"])
        if body.get("truncated") is not True:
            return {"status": 200, "body": {"items": items, "truncated": False}}
        cursor = body.get("nextCursor")
        if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
            break
    return {"status": 200, "body": {"items": items, "truncated": True}}


def units():
    names = [NAMESPACE + "-controller.service", NAMESPACE + "-controller-readiness.service"]
    try:
        completed = subprocess.run(
            ["/run/current-system/sw/bin/systemctl", "is-active"] + names,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception as error:
        return {"error": type(error).__name__}
    states = completed.stdout.decode("ascii", "replace").split()
    if len(states) != len(names):
        return {"error": "UnexpectedOutput"}
    return {"states": {"controller": states[0], "readiness": states[1]}}


def health():
    try:
        with open(STATUS_PATH, "rb") as handle:
            raw = handle.read(65537)
    except FileNotFoundError:
        return {"error": "Missing"}
    except Exception as error:
        return {"error": type(error).__name__}
    if len(raw) > 65536:
        return {"error": "TooLarge"}
    try:
        return {"body": json.loads(raw)}
    except ValueError:
        return {"error": "InvalidJson"}


sys.stdout.write(
    json.dumps(
        {
            "version": 1,
            "capabilities": get("/v1/admin/capabilities"),
            "images": get("/v1/admin/images"),
            "applications": pages("/v1/admin/applications", 10),
            "deployments": pages("/v1/admin/deployments", DEPLOYMENT_PAGES),
            "storage": pages("/v1/admin/storage", 10),
            "operations": pages("/v1/admin/operations", OPERATION_PAGES),
            "units": units(),
            "health": health(),
        },
        separators=(",", ":"),
    )
)
"""


class SourceError(RuntimeError):
    """A safe, presentation-ready reason that one source is unavailable."""


@dataclass(frozen=True, slots=True)
class ImageRecord:
    role: str
    display_name: str | None
    source_commit: str | None
    selected_at: str | None


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    application_id: str
    slug: str
    enabled: bool
    active_deployment_id: str | None
    url: str | None
    worker_flavor: str | None
    cpu_mhz: int | None
    memory_mib: int | None
    created_at: str | None
    updated_at: str | None
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    deployment_id: str
    application_id: str
    status: str
    source_commit: str | None
    requested_ref: str | None
    repository: str | None
    runtime: str | None
    port: int | None
    health_path: str | None
    configuration_revision: int | None
    image_digest: str | None
    safe_error: str | None
    cleanup_state: str | None
    requested_at: str | None
    updated_at: str | None
    accepted_at: str | None
    last_healthy_at: str | None


@dataclass(frozen=True, slots=True)
class StorageRecord:
    resource_id: str
    application_id: str
    resource_type: str
    name: str
    display_label: str | None
    lifecycle_state: str
    quotas: Mapping[str, int]
    last_verified_at: str | None


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    kind: str
    scope: str
    status: str
    phase: str | None
    started_at: str | None
    updated_at: str | None
    deadline_at: str | None
    safe_error: str | None
    cleanup_state: str | None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The admin platform-health timer's last snapshot."""

    checked_at: str | None
    healthy: bool
    checks: Mapping[str, Any]
    error: str | None


@dataclass(frozen=True, slots=True)
class AdminReads:
    api_version: int | None
    features: tuple[str, ...]
    images: tuple[ImageRecord, ...]
    applications: tuple[ApplicationRecord, ...]
    deployments: tuple[DeploymentRecord, ...]
    deployments_truncated: bool
    storage: tuple[StorageRecord, ...]
    operations: tuple[OperationRecord, ...]
    operations_truncated: bool
    controller_units: Mapping[str, str] | None
    health: HealthReport | None
    health_error: str | None
    section_errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HostRecord:
    role: str
    image: ImageRecord | None
    available: bool
    state: str
    checked_at: str | None


@dataclass(frozen=True, slots=True)
class OperatorReads:
    """Operator-plane projection: ``infra list`` hosts plus unfinished work."""

    hosts: tuple[HostRecord, ...]
    operations: tuple[OperationRecord, ...]


@dataclass(frozen=True, slots=True)
class Probe:
    """One credential-free public HTTPS observation."""

    url: str
    checked_at: str
    status: int | None
    latency_ms: int | None
    marker: str | None = None
    body_ok: bool | None = None
    error: str | None = None


def now_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value: object) -> str | None:
    """Normalize a UTC ISO-8601 timestamp to whole seconds; reject anything else."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    match = _TIMESTAMP.fullmatch(value)
    return None if match is None else f"{match.group(1)}Z"


def safe_text(value: object, maximum: int = 300) -> str | None:
    """Return printable, redacted, bounded text for display, or ``None``."""
    if not isinstance(value, str):
        return None
    printable = "".join(character if character.isprintable() else " " for character in value)
    cleaned = " ".join(runtime.redact_text(printable).split())
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= maximum else cleaned[: maximum - 1].rstrip() + "…"


def _match(pattern: re.Pattern[str], value: object) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _member(value: object, allowed: frozenset[str]) -> str | None:
    """Return ``value`` when it is one allowed string; JSON may carry unhashable values."""
    return value if isinstance(value, str) and value in allowed else None


def _uuid(value: object) -> str | None:
    try:
        return uuid(value)
    except ValidationError:
        return None


def _commit(value: object) -> str | None:
    try:
        return commit(value)
    except ValidationError:
        return None


def _integer(value: object, *, minimum: int = 0, maximum: int = 2**53) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        return None
    return value


def _https_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 512 or not value.startswith("https://"):
        return None
    if any(character.isspace() or not character.isprintable() for character in value):
        return None
    return value


def _image(value: object, *, role: str | None = None) -> ImageRecord | None:
    if not isinstance(value, Mapping):
        return None
    selected_role = role if role is not None else value.get("role")
    if selected_role not in IMAGE_ROLES:
        return None
    return ImageRecord(
        role=str(selected_role),
        display_name=_match(_DISPLAY_NAME, value.get("displayName")),
        source_commit=_commit(value.get("sourceCommit")),
        selected_at=timestamp(value.get("selectedAt")),
    )


def _application(value: object) -> ApplicationRecord | None:
    if not isinstance(value, Mapping):
        return None
    identifier = _uuid(value.get("applicationId"))
    try:
        application_slug = slug(value.get("slug"))
    except ValidationError:
        return None
    enabled = value.get("enabled")
    if identifier is None or not isinstance(enabled, bool):
        return None
    sizing = value.get("sizing")
    sizing = sizing if isinstance(sizing, Mapping) else {}
    return ApplicationRecord(
        application_id=identifier,
        slug=application_slug,
        enabled=enabled,
        active_deployment_id=_uuid(value.get("activeDeploymentId")),
        url=_https_url(value.get("url")),
        worker_flavor=_match(_FLAVOR, sizing.get("workerFlavor")),
        cpu_mhz=_integer(sizing.get("cpuMHz"), minimum=1),
        memory_mib=_integer(sizing.get("memoryMiB"), minimum=1),
        created_at=timestamp(value.get("createdAt")),
        updated_at=timestamp(value.get("updatedAt")),
        deleted_at=timestamp(value.get("deletedAt")),
    )


def _deployment(value: object) -> DeploymentRecord | None:
    if not isinstance(value, Mapping):
        return None
    identifier = _uuid(value.get("deploymentId"))
    application_id = _uuid(value.get("applicationId"))
    status = _member(value.get("status"), _DEPLOYMENT_STATUSES)
    if identifier is None or application_id is None or status is None:
        return None
    configuration = value.get("configuration")
    configuration = configuration if isinstance(configuration, Mapping) else {}
    build = configuration.get("build")
    build = build if isinstance(build, Mapping) else {}
    runtime_settings = configuration.get("runtime")
    runtime_settings = runtime_settings if isinstance(runtime_settings, Mapping) else {}
    try:
        checked_path: str | None = health_path(runtime_settings.get("healthPath"))
    except ValidationError:
        checked_path = None
    try:
        repository: str | None = repository_url(value.get("sourceRepository"))
    except ValidationError:
        repository = None
    return DeploymentRecord(
        deployment_id=identifier,
        application_id=application_id,
        status=status,
        source_commit=_commit(value.get("repositoryCommit")),
        requested_ref=safe_text(value.get("requestedRef"), 128),
        repository=repository,
        runtime=_member(build.get("runtime"), _RUNTIMES),
        port=_integer(runtime_settings.get("port"), minimum=1, maximum=65_535),
        health_path=checked_path,
        configuration_revision=_integer(value.get("configurationRevision")),
        image_digest=_match(_IMAGE_DIGEST, value.get("imageDigest")),
        safe_error=safe_text(value.get("safeError"), 600),
        cleanup_state=_match(_STATE_WORD, value.get("cleanupState")),
        requested_at=timestamp(value.get("requestedAt")),
        updated_at=timestamp(value.get("updatedAt")),
        accepted_at=timestamp(value.get("acceptedAt")),
        last_healthy_at=timestamp(value.get("lastHealthyAt")),
    )


def _storage(value: object) -> StorageRecord | None:
    if not isinstance(value, Mapping):
        return None
    identifier = _uuid(value.get("resourceId"))
    application_id = _uuid(value.get("applicationId"))
    resource_type = _member(value.get("type"), _RESOURCE_TYPES)
    state = _member(value.get("lifecycleState"), _RESOURCE_STATES)
    name = value.get("name")
    if (
        identifier is None
        or application_id is None
        or resource_type is None
        or state is None
        or not isinstance(name, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name)
    ):
        return None
    raw_quotas = value.get("quotas")
    raw_quotas = raw_quotas if isinstance(raw_quotas, Mapping) else {}
    quotas = {
        key: quota for key in _QUOTA_KEYS if (quota := _integer(raw_quotas.get(key))) is not None
    }
    return StorageRecord(
        resource_id=identifier,
        application_id=application_id,
        resource_type=resource_type,
        name=name,
        display_label=safe_text(value.get("displayLabel"), 80),
        lifecycle_state=state,
        quotas=quotas,
        last_verified_at=timestamp(value.get("lastVerifiedAt")),
    )


def _operation(value: object) -> OperationRecord | None:
    if not isinstance(value, Mapping):
        return None
    identifier = _uuid(value.get("operationId"))
    kind = _match(_KIND, value.get("kind"))
    scope = _match(_SCOPE, value.get("scope"))
    status = _member(value.get("status"), _OPERATION_STATUSES)
    if identifier is None or kind is None or scope is None or status is None:
        return None
    return OperationRecord(
        operation_id=identifier,
        kind=kind,
        scope=scope,
        status=status,
        phase=_match(_PHASE, value.get("phase")),
        started_at=timestamp(value.get("startedAt")),
        updated_at=timestamp(value.get("updatedAt")),
        deadline_at=timestamp(value.get("deadlineAt")),
        safe_error=safe_text(value.get("safeError"), 600),
        cleanup_state=_match(_STATE_WORD, value.get("cleanupState")),
    )


def _health_report(value: object) -> HealthReport | None:
    if not isinstance(value, Mapping) or not isinstance(value.get("healthy"), bool):
        return None
    checks = value.get("checks")
    error = safe_text(value.get("error"), 400)
    if error is not None and error.startswith("RuntimeError: "):
        # check_platform.py raises RuntimeError for its own named failures;
        # the message alone reads better than the exception class.
        error = error.removeprefix("RuntimeError: ")
        error = error[:1].upper() + error[1:]
    return HealthReport(
        checked_at=timestamp(value.get("checked_at")),
        healthy=value["healthy"],
        checks=dict(checks) if isinstance(checks, Mapping) else {},
        error=error,
    )


def _reader_error(section: Mapping[str, Any]) -> str:
    name = _match(_ERROR_NAME, section.get("error")) or "UnknownError"
    status = _integer(section.get("status"), minimum=100, maximum=599)
    readable = {
        "ConnectionRefusedError": "the controller socket refused the connection",
        "FileNotFoundError": "the controller socket is absent",
        "PermissionError": "the controller socket rejected the operator identity",
        "TimeoutError": "the controller did not answer in time",
        "ResponseTooLarge": "the controller response exceeded its limit",
        "InvalidJson": "the controller returned malformed JSON",
    }.get(name, f"the admin read failed ({name})")
    return readable if status is None else f"{readable} (HTTP {status})"


def _section(document: Mapping[str, Any], key: str) -> tuple[object | None, str | None]:
    section = document.get(key)
    if not isinstance(section, Mapping):
        return None, "the admin read omitted this section"
    if "error" in section:
        return None, _reader_error(section)
    status = section.get("status")
    if status != 200:
        code: str | None = None
        body = section.get("body")
        if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
            code = _match(re.compile(r"[A-Z][A-Z0-9_]{0,63}"), body["error"].get("code"))
        summary = f"the controller returned HTTP {_integer(status) or 'error'}"
        return None, summary if code is None else f"{summary} ({code})"
    return section.get("body"), None


def _record(parser: Callable[[object], Any], raw: object) -> Any:
    """Parse one record; a malformed record is dropped, never fatal to the read."""
    try:
        return parser(raw)
    except (TypeError, ValueError, AttributeError, KeyError):
        return None


def _items(
    document: Mapping[str, Any],
    key: str,
    parser: Callable[[object], Any],
    errors: dict[str, str],
) -> tuple[tuple[Any, ...], bool]:
    body, error = _section(document, key)
    if error is not None:
        errors[key] = error
        return (), False
    if not isinstance(body, Mapping) or not isinstance(body.get("items"), list):
        errors[key] = "the controller returned an unexpected list shape"
        return (), False
    parsed = tuple(item for raw in body["items"] if (item := _record(parser, raw)) is not None)
    return parsed, body.get("truncated") is True


def parse_admin_reads(payload: bytes) -> AdminReads:
    """Validate the fixed reader's envelope into allowlisted records."""
    try:
        document = remote._json_object(payload, maximum_bytes=ADMIN_READ_LIMIT, name="admin read")
    except remote.ProtocolError as error:
        raise SourceError("the admin reader returned malformed output") from error
    if document.get("version") != READER_VERSION:
        raise SourceError("the admin reader reported an unsupported version")
    errors: dict[str, str] = {}

    capabilities, capability_error = _section(document, "capabilities")
    api_version: int | None = None
    features: tuple[str, ...] = ()
    if capability_error is not None:
        errors["capabilities"] = capability_error
    elif isinstance(capabilities, Mapping):
        api_version = _integer(capabilities.get("apiVersion"), minimum=1, maximum=1_000)
        raw_features = capabilities.get("features")
        if isinstance(raw_features, list):
            features = tuple(
                feature for feature in raw_features if _match(_FEATURE, feature) is not None
            )

    images, _ = _items(document, "images", _image, errors)
    applications, _ = _items(document, "applications", _application, errors)
    deployments, deployments_truncated = _items(document, "deployments", _deployment, errors)
    storage, _ = _items(document, "storage", _storage, errors)
    operations, operations_truncated = _items(document, "operations", _operation, errors)

    units: dict[str, str] | None = None
    unit_section = document.get("units")
    if isinstance(unit_section, Mapping) and isinstance(unit_section.get("states"), Mapping):
        states = unit_section["states"]
        units = {
            name: state
            for name in ("controller", "readiness")
            if (state := _match(_STATE_WORD, states.get(name))) is not None
        }
    else:
        errors["units"] = "systemd unit state was unavailable"

    health: HealthReport | None = None
    health_error: str | None = None
    health_section = document.get("health")
    if isinstance(health_section, Mapping) and "body" in health_section:
        health = _health_report(health_section["body"])
        if health is None:
            health_error = "the platform-health snapshot is malformed"
    else:
        name = (
            health_section.get("error") if isinstance(health_section, Mapping) else None
        ) or "Unavailable"
        health_error = {
            "Missing": "the platform-health timer has not written a snapshot yet",
            "PermissionError": "the platform-health snapshot is not readable by the operator",
            "TooLarge": "the platform-health snapshot exceeded its limit",
            "InvalidJson": "the platform-health snapshot is malformed",
        }.get(str(name), "the platform-health snapshot could not be read")

    return AdminReads(
        api_version=api_version,
        features=features,
        images=images,
        applications=applications,
        deployments=deployments,
        deployments_truncated=deployments_truncated,
        storage=storage,
        operations=operations,
        operations_truncated=operations_truncated,
        controller_units=units,
        health=health,
        health_error=health_error,
        section_errors=errors,
    )


def health_status_path(platform: PlatformConfig) -> str:
    """Return the admin guest's platform-health snapshot path from inventory."""
    root = platform.get("paths.root")
    if not isinstance(root, str) or not root.startswith("/") or "\x00" in root:
        raise ValidationError("inventory paths.root must be an absolute guest path")
    return f"{root.rstrip('/')}/persistent/status/{admin_namespace(platform)}.json"


def admin_namespace(platform: PlatformConfig) -> str:
    namespace = platform.namespace
    if not _NAMESPACE.fullmatch(namespace):
        raise ValidationError("inventory namespace is not safe for a controller socket path")
    return namespace


def read_admin(
    platform: PlatformConfig,
    *,
    ssh_config: str | Path = remote.DEFAULT_SSH_CONFIG,
    timeout_seconds: float = 120,
    command_runner: Callable[..., runtime.CommandResult] = runtime.run,
) -> AdminReads:
    """Run the fixed admin reader once through the pinned SSH alias."""
    command = remote.pinned_admin_command(
        (
            "/run/current-system/sw/bin/python3",
            "-I",
            "-c",
            ADMIN_READER,
            f"/run/{admin_namespace(platform)}-controller/privileged.sock",
            admin_namespace(platform),
            health_status_path(platform),
            str(DEPLOYMENT_PAGES),
            str(OPERATION_PAGES),
        ),
        ssh_config_path=ssh_config,
    )
    try:
        result = command_runner(
            command,
            timeout_seconds=timeout_seconds,
            stdout_limit=ADMIN_READ_LIMIT,
            stderr_limit=65_536,
            inherit_env=("HOME", "USER", "SSH_AUTH_SOCK"),
        )
    except runtime.CommandTimedOut:
        raise SourceError("the admin read timed out over the pinned SSH bridge") from None
    except runtime.CommandFailure as error:
        returncode = None if error.result is None else error.result.returncode
        if returncode == 255:
            raise SourceError("the pinned SSH bridge could not reach the admin host") from None
        raise SourceError("the admin reader failed on the admin host") from None
    if result.stdout_truncated:
        raise SourceError("the admin read exceeded its output limit")
    return parse_admin_reads(result.stdout)


def parse_operator_reads(
    roles: Sequence[Mapping[str, Any]], operations: Sequence[Mapping[str, Any]]
) -> OperatorReads:
    """Validate the operator plane's ``infra_list``/unfinished-operation output."""
    hosts: list[HostRecord] = []
    for item in roles:
        role = item.get("role")
        if role not in IMAGE_ROLES:
            continue
        live = item.get("live")
        live = live if isinstance(live, Mapping) else {}
        hosts.append(
            HostRecord(
                role=str(role),
                image=_image(item.get("image"), role=str(role)),
                available=live.get("available") is True,
                state=_member(live.get("state"), _HOST_STATES) or "unknown",
                checked_at=timestamp(live.get("checkedAt")),
            )
        )
    unfinished = tuple(
        record for item in operations if (record := _record(_operation, item)) is not None
    )
    return OperatorReads(tuple(hosts), unfinished)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _req: object,
        _fp: object,
        _code: int,
        _msg: str,
        _headers: object,
        _newurl: str,
    ) -> None:
        return None


def _probe_error(error: BaseException) -> str:
    reason: object = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS certificate was rejected"
    if isinstance(reason, ssl.SSLError):
        return "TLS handshake failed"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timed out"
    return "connection failed"


def _probe_once(
    url: str,
    *,
    timeout_seconds: float,
    expect_body: bytes | None = None,
    opener: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Probe:
    checked_at = now_timestamp()
    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": runtime.HTTP_USER_AGENT}
    )
    open_url = opener or urllib.request.build_opener(_NoRedirect()).open
    started = clock()

    def elapsed() -> int:
        return max(0, round((clock() - started) * 1000))

    try:
        with open_url(request, timeout=timeout_seconds) as response:
            latency = elapsed()
            body = response.read(4_097)
            status = int(response.status)
            marker = _uuid(response.headers.get(DEPLOYMENT_ROUTE_HEADER))
    except urllib.error.HTTPError as error:
        latency = elapsed()
        marker = _uuid(error.headers.get(DEPLOYMENT_ROUTE_HEADER)) if error.headers else None
        error.close()
        return Probe(url, checked_at, int(error.code), latency, marker, False)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
        return Probe(url, checked_at, None, None, error=_probe_error(error))
    if len(body) > 4_096:
        return Probe(url, checked_at, status, latency, marker, False, "response exceeded 4 KiB")
    body_ok = None if expect_body is None else body.strip() == expect_body
    return Probe(url, checked_at, status, latency, marker, body_ok)


# A hosted application can drip bytes forever; socket timeouts apply per
# operation, not per request. Each probe therefore runs on a daemon thread with
# a wall-clock bound, and a URL is not probed again while a stuck probe lives.
_INFLIGHT: dict[str, threading.Thread] = {}
_INFLIGHT_LOCK = threading.Lock()


def probe(
    url: str,
    *,
    timeout_seconds: float,
    expect_body: bytes | None = None,
    opener: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Probe:
    """GET one public HTTPS URL without redirects, cookies, or credentials."""
    if not url.startswith("https://"):
        raise ValidationError("dashboard probes require HTTPS")
    checked_at = now_timestamp()
    result: list[Probe] = []

    def run() -> None:
        try:
            result.append(
                _probe_once(
                    url,
                    timeout_seconds=timeout_seconds,
                    expect_body=expect_body,
                    opener=opener,
                    clock=clock,
                )
            )
        except Exception:
            result.append(Probe(url, checked_at, None, None, error="probe failed"))

    with _INFLIGHT_LOCK:
        running = _INFLIGHT.get(url)
        if running is not None and running.is_alive():
            return Probe(url, checked_at, None, None, error="previous probe still running")
        thread = threading.Thread(target=run, name="dashboard-probe", daemon=True)
        _INFLIGHT[url] = thread
        thread.start()
    thread.join(timeout_seconds)
    with _INFLIGHT_LOCK:
        if not thread.is_alive() and _INFLIGHT.get(url) is thread:
            del _INFLIGHT[url]
    return result[0] if result else Probe(url, checked_at, None, None, error="timed out")


def route_url(platform: PlatformConfig, application_slug: str, path: str) -> str:
    """Return the canonical public route checked by deployment acceptance."""
    return f"https://{slug(application_slug)}.{platform.domain}{health_path(path)}"
