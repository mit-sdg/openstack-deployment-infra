"""Pure projection from dashboard evidence to the browser snapshot document.

Missing evidence is never upgraded to health. A failed source keeps its last
successful records with their age, mirroring the platform invariant that an
unavailable observation does not erase accepted state. Every status carries a
key, a sentence-case label, and one tone: ``good``, ``warning``, ``critical``,
``info``, or ``neutral``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import PlatformConfig
from ..contracts import IMAGE_ROLES
from .sources import (
    AdminReads,
    ApplicationRecord,
    DeploymentRecord,
    HealthReport,
    HostRecord,
    ImageRecord,
    OperationRecord,
    OperatorReads,
    Probe,
    StorageRecord,
)

SCHEMA_VERSION = 1
# The admin platform-health timer runs every five minutes.
HEALTH_SNAPSHOT_STALE_SECONDS = 15 * 60
RECENT_OPERATIONS = 30
APPLICATION_HISTORY = 6
_TONES = ("critical", "warning", "info", "neutral", "good")

_ROLES: Mapping[str, tuple[str, str, str]] = {
    "admin": ("Admin", "persistent", "Nomad control, controller, helper, monitoring, and backups"),
    "ingress": ("Ingress", "persistent", "Public HTTPS entry and routing to healthy applications"),
    "storage": ("Storage", "persistent", "PostgreSQL, MongoDB, Garage S3, and the image registry"),
    "worker": ("Worker", "replaceable", "One dedicated Nomad worker per running application"),
    "builder": ("Builder", "single-use", "Disposable rootless BuildKit host for one source build"),
}
_LIFETIMES = {"persistent": "Persistent", "replaceable": "Replaceable", "single-use": "Single use"}
_HOST_STATES = {
    "active": ("Active", "good"),
    "building": ("Building", "info"),
    "stopped": ("Stopped", "critical"),
    "error": ("Error", "critical"),
    "missing": ("Missing", "critical"),
}
# Ordered exactly as infra/monitor/check_platform.py runs them; a failure stops
# the remaining checks, so the first missing key after a failure is the cause.
HEALTH_CHECKS = (
    ("openstack", "OpenStack servers", "critical"),
    ("public_ingress", "Public ingress", "critical"),
    ("managed_services", "Managed services", "critical"),
    ("nomad", "Nomad cluster", "critical"),
    ("backup", "Encrypted backups", "warning"),
    ("offsite_recovery", "Off-site recovery", "warning"),
)
_KIND_LABELS = {
    "app.deploy": "Deployment",
    "app.enable": "Enable",
    "app.disable": "Disable",
    "app.delete": "Deletion",
    "app.env.set": "Environment update",
    "app.env.unset": "Environment removal",
    "app.env.import": "Environment import",
    "app.fixed-ip": "Fixed IPv4 change",
    "app.public-ip": "Public IPv4 change",
    "storage.create": "Storage creation",
    "storage.verify": "Storage verification",
    "storage.rotate": "Credential rotation",
    "storage.remove": "Storage removal",
    "infra.image.set": "Image selection",
    "infra.image.prune.plan": "Image prune plan",
    "infra.image.prune.apply": "Image prune",
    "infra.start": "Host start",
    "infra.stop": "Host stop",
    "infra.reboot": "Host reboot",
    "infra.replace": "Host replacement",
}
_ACTIVE_VERBS = {
    "app.deploy": "Deploying",
    "app.enable": "Starting",
    "app.disable": "Stopping",
    "app.delete": "Deleting",
}
_OPERATION_STATUSES = {
    "running": ("Running", "info"),
    "succeeded": ("Succeeded", "good"),
    "failed": ("Failed", "critical"),
    "recovery_required": ("Needs recovery", "warning"),
}
_DEPLOYMENT_STATUSES = {
    "queued": ("Queued", "info"),
    "building": ("Building", "info"),
    "deploying": ("Deploying", "info"),
    "succeeded": ("Succeeded", "good"),
    "failed": ("Failed", "critical"),
    "recovery_required": ("Needs recovery", "warning"),
}
_RESOURCE_STATES = {
    "active": ("Active", "good"),
    "creating": ("Creating", "info"),
    "removing": ("Removing", "info"),
    "recovery_required": ("Needs recovery", "warning"),
}
_RESOURCE_TYPES = {"postgres": "PostgreSQL", "mongo": "MongoDB", "s3": "S3"}
# Reader sections that come from the controller API itself.
CONTROLLER_SECTIONS = (
    "capabilities",
    "images",
    "applications",
    "deployments",
    "storage",
    "operations",
)
# Sections whose failure leaves product records stale.
RECORD_SECTIONS = CONTROLLER_SECTIONS[1:]


@dataclass(frozen=True, slots=True)
class Observed[T]:
    """The latest attempt for one source plus its last successful value."""

    value: T | None = None
    ok: bool = False
    observed_at: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    platform: PlatformConfig
    generated_at: str
    admin: Observed[AdminReads]
    operator: Observed[OperatorReads]
    ingress: tuple[Probe, ...] = ()
    routes: Mapping[str, Probe] = field(default_factory=dict)
    history: Mapping[str, Sequence[tuple[str, str]]] = field(default_factory=dict)
    release: str | None = None
    admin_sections: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Check:
    key: str
    label: str
    state: str
    tone: str
    detail: str


def _status(key: str, label: str, tone: str) -> dict[str, str]:
    return {"key": key, "label": label, "tone": tone}


def _signal(label: str, value: str, tone: str, detail: str | None = None) -> dict[str, Any]:
    return {"label": label, "value": value, "tone": tone, "detail": detail}


def _worst(tones: Iterable[str]) -> str | None:
    ranked = [tone for tone in tones if tone in _TONES]
    return min(ranked, key=_TONES.index) if ranked else None


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _age_seconds(value: str | None, now: str) -> int | None:
    then, current = _parse(value), _parse(now)
    if then is None or current is None:
        return None
    return max(0, int((current - then).total_seconds()))


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def _sentence(text: str) -> str:
    """Uppercase the first letter only; ``str.capitalize`` would turn DNS into Dns."""
    return text[:1].upper() + text[1:]


def _humanize(identifier: str | None) -> str | None:
    if identifier is None:
        return None
    words = identifier.replace("_", " ").replace("-", " ").replace(".", " ").replace(":", " ")
    text = " ".join(words.split())
    return text[:1].upper() + text[1:] if text else None


def _kind_label(kind: str) -> str:
    known = _KIND_LABELS.get(kind)
    if known is not None:
        return known
    return _humanize(kind.split(".", 1)[-1]) or kind


def _image(record: ImageRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "name": record.display_name,
        "commit": record.source_commit,
        "selectedAt": record.selected_at,
    }


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value


def _check_detail(key: str, value: object) -> str:
    details = value if isinstance(value, Mapping) else {}
    if key == "openstack":
        core, workers = _number(details.get("core_active")), _number(details.get("workers_active"))
        if core is not None and workers is not None:
            return f"{_plural(int(core), 'core host')} and {_plural(int(workers), 'worker')} active"
    if key == "public_ingress":
        return "Platform and wildcard health routes answered OK"
    if key == "managed_services":
        return "PostgreSQL, MongoDB, Garage, and the registry responded"
    if key == "nomad":
        ready = _number(details.get("ready_clients"))
        if ready is not None:
            return f"Raft healthy with {_plural(int(ready), 'ready client')}"
    if key == "backup":
        age = _number(details.get("age_hours"))
        if age is not None:
            return f"Newest encrypted set is {age:.1f} h old"
    if key == "offsite_recovery":
        age = _number(details.get("ageHours"))
        if age is not None:
            return f"Verified off-site bundle exported {age:.1f} h ago"
    return "Passed"


def _health_checks(
    report: HealthReport | None, unavailable: str | None, stale: bool
) -> list[_Check]:
    if report is None:
        reason = unavailable or "The platform-health snapshot is unavailable"
        return [
            _Check(key, label, "unavailable", "neutral", reason) for key, label, _ in HEALTH_CHECKS
        ]
    failed = None
    if not report.healthy:
        failed = next((key for key, _, _ in HEALTH_CHECKS if key not in report.checks), None)
    results: list[_Check] = []
    halted = False
    for key, label, failure_tone in HEALTH_CHECKS:
        if key in report.checks and not halted:
            detail = _check_detail(key, report.checks[key])
            results.append(_Check(key, label, "passed", "neutral" if stale else "good", detail))
        elif key == failed:
            detail = report.error or "The check failed"
            results.append(
                _Check(key, label, "failed", "neutral" if stale else failure_tone, detail)
            )
            halted = True
        else:
            detail = "Skipped after an earlier check failed" if halted else "Not reported"
            results.append(_Check(key, label, "not-run", "neutral", detail))
    return results


def _check_signal(label: str, check: _Check | None) -> dict[str, Any]:
    if check is None:
        return _signal(label, "Unknown", "neutral", "No platform-health evidence")
    value = {
        "passed": "Healthy",
        "failed": "Failing",
        "not-run": "Not checked",
        "unavailable": "Unknown",
    }[check.state]
    if check.state == "passed" and check.tone == "neutral":
        value = "Last seen healthy"
    return _signal(label, value, check.tone, check.detail)


def _health_count(reads: AdminReads | None, check: str, name: str) -> int | None:
    if reads is None or reads.health is None:
        return None
    details = reads.health.checks.get(check)
    value = _number(details.get(name)) if isinstance(details, Mapping) else None
    return None if value is None else int(value)


def _counted(signal: dict[str, Any], count: int | None, noun: str) -> dict[str, Any]:
    """Replace a passed check's generic value with its observed count."""
    if count is not None and signal["value"] == "Healthy":
        signal["value"] = f"{count} {noun}"
    return signal


def _server_signal(host: HostRecord | None, operator: Observed[OperatorReads]) -> dict[str, Any]:
    if host is None or not host.available:
        detail = operator.error if not operator.ok else "The provider did not report this server"
        return _signal("Server", "Unknown", "neutral", detail)
    label, tone = _HOST_STATES.get(host.state, ("Unknown", "neutral"))
    if not operator.ok:
        return _signal(
            "Server",
            f"Last seen {label.lower()}",
            "neutral",
            f"Provider unavailable now: {operator.error}",
        )
    return _signal("Server", label, tone, f"Nova reports {host.state.upper()}")


def _route_probe_signal(label: str, probe: Probe | None) -> dict[str, Any]:
    if probe is None:
        return _signal(label, "Not checked", "neutral", None)
    if probe.status is None:
        return _signal(label, _sentence(probe.error or "unreachable"), "critical", probe.url)
    if probe.status == 200 and probe.body_ok:
        return _signal(label, f"OK · {probe.latency_ms} ms", "good", probe.url)
    reason = f"HTTP {probe.status}" if probe.status != 200 else "Unexpected response"
    return _signal(label, reason, "critical", probe.url)


def _role_status(role: str, signals: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    tones = [str(signal["tone"]) for signal in signals]
    if role == "builder":
        if "info" in tones:
            return _status("building", "Building", "info")
        if tones and all(tone == "good" for tone in tones):
            return _status("idle", "Idle", "neutral")
    worst = _worst(tones)
    if worst == "critical":
        return _status("failing", "Failing", "critical")
    if worst == "warning":
        return _status("degraded", "Degraded", "warning")
    if worst == "info":
        return _status("changing", "Changing", "info")
    if tones and all(tone == "good" for tone in tones):
        return _status("operational", "Operational", "good")
    return _status("unverified", "Unverified", "neutral")


def _admin_signals(admin: Observed[AdminReads]) -> list[dict[str, Any]]:
    reads = admin.value
    signals: list[dict[str, Any]] = []
    if reads is None or not admin.ok:
        signals.append(
            _signal(
                "Controller API",
                "Unreachable",
                "critical",
                admin.error or "The admin read has not completed",
            )
        )
    elif failed := [item for item in CONTROLLER_SECTIONS if item in reads.section_errors]:
        error = reads.section_errors[failed[0]]
        if len(failed) == len(CONTROLLER_SECTIONS):
            signals.append(_signal("Controller API", "Not responding", "critical", error))
        else:
            detail = f"The {', '.join(failed)} read failed: {error}"
            signals.append(_signal("Controller API", "Partially responding", "warning", detail))
    else:
        version = f"v{reads.api_version}" if reads.api_version is not None else "responding"
        features = ", ".join(reads.features) or None
        signals.append(_signal("Controller API", f"Responding · {version}", "good", features))

    units = None if reads is None else reads.controller_units
    if reads is None or units is None:
        signals.append(_signal("Controller service", "Unknown", "neutral", "systemd state unknown"))
    else:
        controller = units.get("controller", "unknown")
        readiness = units.get("readiness", "unknown")
        detail = f"controller {controller} · readiness {readiness}"
        if not admin.ok:
            signals.append(
                _signal("Controller service", f"Last seen {controller}", "neutral", detail)
            )
        elif controller == "active" and readiness == "active":
            signals.append(_signal("Controller service", "Active", "good", detail))
        else:
            value = controller.capitalize() if controller != "active" else "Not ready"
            signals.append(_signal("Controller service", value, "critical", detail))
    return signals


def _operation_model(
    record: OperationRecord,
    slugs: Mapping[str, str],
    *,
    plane: str,
) -> dict[str, Any]:
    application_id = record.scope[4:] if record.scope.startswith("app-") else None
    label, tone = _OPERATION_STATUSES[record.status]
    return {
        "id": record.operation_id,
        "kind": record.kind,
        "label": _kind_label(record.kind),
        "plane": plane,
        "applicationId": application_id,
        "subject": (
            "Infrastructure"
            if application_id is None
            else slugs.get(application_id, f"Application {application_id[:8]}")
        ),
        "status": _status(record.status, label, tone),
        "phase": _humanize(record.phase),
        "startedAt": record.started_at,
        "updatedAt": record.updated_at,
        "deadlineAt": record.deadline_at,
        "error": record.safe_error,
        "cleanupState": _humanize(record.cleanup_state),
    }


def route_outcome(probe: Probe, deployment_id: str, application_id: str) -> tuple[str, str]:
    """Classify one public route probe as deployment acceptance would."""
    if probe.status is None:
        return "unreachable", "critical"
    if not 200 <= probe.status < 300:
        return "http-error", "critical"
    # Every deployment path marks its route with the deployment's operation ID;
    # jobs rendered without an explicit marker default to the application ID.
    if probe.marker in {deployment_id, application_id}:
        return "serving", "good"
    return "wrong-deployment", "warning"


def _route(
    application: ApplicationRecord,
    deployment: DeploymentRecord | None,
    probe: Probe | None,
    *,
    deployments_truncated: bool,
) -> dict[str, Any]:
    def unchecked(reason: str) -> dict[str, Any]:
        return {
            "outcome": "not-checked",
            "tone": "neutral",
            "summary": "Not checked",
            "detail": reason,
            "url": None,
            "httpStatus": None,
            "latencyMs": None,
            "checkedAt": None,
        }

    if application.deleted_at is not None:
        return unchecked("The application is deleted")
    if not application.enabled:
        return unchecked("Stopped applications have no public route to check")
    if application.active_deployment_id is None:
        return unchecked("No deployment has been accepted yet")
    if deployment is None:
        return unchecked(
            "The accepted deployment is older than the recent deployment window"
            if deployments_truncated
            else "The accepted deployment record is unavailable"
        )
    if probe is None:
        return unchecked("The accepted deployment has no recorded health path")
    outcome, tone = route_outcome(probe, deployment.deployment_id, application.application_id)
    if outcome == "unreachable":
        summary = _sentence(probe.error or "connection failed")
        detail = f"{summary} while requesting the health path"
    elif outcome == "http-error":
        summary = f"HTTP {probe.status}"
        detail = f"The health path returned HTTP {probe.status}"
    elif outcome == "serving":
        summary = f"HTTP {probe.status}"
        detail = "The accepted deployment answered its health path"
    else:
        summary = f"HTTP {probe.status} · unverified"
        detail = (
            "The response did not carry the deployment marker"
            if probe.marker is None
            else f"Deployment {probe.marker[:8]} answered instead of the accepted deployment"
        )
    return {
        "outcome": outcome,
        "tone": tone,
        "summary": summary,
        "detail": detail,
        "url": probe.url,
        "httpStatus": probe.status,
        "latencyMs": probe.latency_ms,
        "checkedAt": probe.checked_at,
    }


def _application_status(
    application: ApplicationRecord,
    operation: OperationRecord | None,
    route: Mapping[str, Any],
    pending_recovery: bool = False,
) -> dict[str, str]:
    if application.deleted_at is not None:
        return _status("deleted", "Deleted", "neutral")
    if operation is not None and operation.status == "running":
        verb = _ACTIVE_VERBS.get(operation.kind)
        if verb is None:
            verb = "Updating storage" if operation.kind.startswith("storage.") else "Updating"
        return _status("changing", verb, "info")
    recovery = pending_recovery or (
        operation is not None and operation.status == "recovery_required"
    )
    if not application.enabled:
        if recovery:
            return _status("needs-recovery", "Needs recovery", "warning")
        return _status("stopped", "Stopped", "neutral")
    if application.active_deployment_id is None:
        if recovery:
            return _status("needs-recovery", "Needs recovery", "warning")
        return _status("not-deployed", "Not deployed", "warning")
    outcome = route["outcome"]
    if outcome in {"unreachable", "http-error"}:
        return _status("failing", "Failing", "critical")
    if recovery:
        return _status("needs-recovery", "Needs recovery", "warning")
    if outcome == "wrong-deployment":
        return _status("unverified", "Unverified", "warning")
    if outcome == "not-checked":
        return _status("unknown", "Unknown", "neutral")
    return _status("serving", "Serving", "good")


def _deployment_model(record: DeploymentRecord) -> dict[str, Any]:
    label, tone = _DEPLOYMENT_STATUSES[record.status]
    commit_url = (
        f"{record.repository}/commit/{record.source_commit}"
        if record.repository is not None and record.source_commit is not None
        else None
    )
    return {
        "id": record.deployment_id,
        "status": _status(record.status, label, tone),
        "commit": record.source_commit,
        "commitUrl": commit_url,
        "ref": record.requested_ref,
        "repository": record.repository,
        "runtime": {"bun": "Bun", "node": "Node.js"}.get(record.runtime or ""),
        "port": record.port,
        "healthPath": record.health_path,
        "configurationRevision": record.configuration_revision,
        "imageDigest": record.image_digest,
        "error": record.safe_error,
        "cleanupState": _humanize(record.cleanup_state),
        "requestedAt": record.requested_at,
        "updatedAt": record.updated_at,
        "acceptedAt": record.accepted_at,
        "lastHealthyAt": record.last_healthy_at,
    }


def _storage_model(record: StorageRecord) -> dict[str, Any]:
    label, tone = _RESOURCE_STATES[record.lifecycle_state]
    return {
        "id": record.resource_id,
        "type": record.resource_type,
        "typeLabel": _RESOURCE_TYPES[record.resource_type],
        "name": record.name,
        "label": record.display_label,
        "status": _status(record.lifecycle_state, label, tone),
        "quotas": dict(record.quotas),
        "lastVerifiedAt": record.last_verified_at,
    }


def _applications(
    evidence: Evidence, operations: Sequence[OperationRecord]
) -> list[dict[str, Any]]:
    reads = evidence.admin.value
    if reads is None:
        return []
    deployments = {item.deployment_id: item for item in reads.deployments}
    attempts: dict[str, list[DeploymentRecord]] = {}
    for item in reads.deployments:
        attempts.setdefault(item.application_id, []).append(item)
    storage: dict[str, list[StorageRecord]] = {}
    for resource in reads.storage:
        storage.setdefault(resource.application_id, []).append(resource)
    active_operations: dict[str, OperationRecord] = {}
    for record in operations:
        if record.scope.startswith("app-") and record.status in {"running", "recovery_required"}:
            active_operations.setdefault(record.scope[4:], record)

    result: list[dict[str, Any]] = []
    for application in reads.applications:
        deployment = (
            None
            if application.active_deployment_id is None
            else deployments.get(application.active_deployment_id)
        )
        route = _route(
            application,
            deployment,
            evidence.routes.get(application.application_id),
            deployments_truncated=reads.deployments_truncated,
        )
        operation = active_operations.get(application.application_id)
        history = attempts.get(application.application_id, [])
        resources = storage.get(application.application_id, [])
        # The operation list is a bounded window, but a stuck operation also
        # leaves its deployment attempt or storage resource recovery_required.
        recovery_note = None
        if operation is None:
            if history and history[0].status == "recovery_required":
                recovery_note = (
                    "A deployment attempt needs recovery; resume it with its original request"
                    " and key"
                )
            elif stuck := next(
                (item for item in resources if item.lifecycle_state == "recovery_required"), None
            ):
                recovery_note = f"{_RESOURCE_TYPES[stuck.resource_type]} storage needs recovery"
        result.append(
            {
                "id": application.application_id,
                "slug": application.slug,
                "url": application.url,
                "enabled": application.enabled,
                "status": _application_status(
                    application, operation, route, recovery_note is not None
                ),
                "recoveryNote": recovery_note,
                "route": route,
                "checks": [
                    {"at": at, "tone": tone}
                    for at, tone in evidence.history.get(application.application_id, ())
                ],
                "deployment": None if deployment is None else _deployment_model(deployment),
                "attempts": [_deployment_model(item) for item in history[:APPLICATION_HISTORY]],
                "operation": (
                    None
                    if operation is None
                    else _operation_model(
                        operation, {application.application_id: application.slug}, plane="hosted"
                    )
                ),
                "sizing": {
                    "flavor": application.worker_flavor,
                    "cpuMHz": application.cpu_mhz,
                    "memoryMiB": application.memory_mib,
                },
                "storage": [_storage_model(item) for item in resources],
                "createdAt": application.created_at,
                "updatedAt": application.updated_at,
                "deletedAt": application.deleted_at,
            }
        )
    return result


def _roles(
    evidence: Evidence,
    checks: Mapping[str, _Check],
    operations: Sequence[OperationRecord],
    applications: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    reads = evidence.admin.value
    operator = evidence.operator.value
    hosts = {} if operator is None else {host.role: host for host in operator.hosts}
    hosted_images = {} if reads is None else {image.role: image for image in reads.images}
    ingress = {probe.url: probe for probe in evidence.ingress}
    domain = evidence.platform.domain
    running = [item for item in applications if item["enabled"] and item["deployment"]]
    builds = [
        item
        for item in operations
        if item.status == "running" and (item.phase or "").startswith("build")
    ]
    storage_counts: dict[str, int] = {}
    for resource in () if reads is None else reads.storage:
        storage_counts[resource.resource_type] = storage_counts.get(resource.resource_type, 0) + 1
    try:
        mode = str(evidence.platform.get("publicIngress.mode"))
    except KeyError:
        mode = "unknown"

    result: list[dict[str, Any]] = []
    for role in IMAGE_ROLES:
        name, lifetime, summary = _ROLES.get(role, (role.capitalize(), "persistent", ""))
        signals: list[dict[str, Any]] = []
        facts: list[dict[str, str]] = []
        host = hosts.get(role)
        # Persistent images are authoritative in operator state; worker and
        # builder provisioning uses the hosted controller's selections.
        image = host.image if host is not None and host.image is not None else None
        if image is None or role in {"worker", "builder"}:
            image = hosted_images.get(role, image)
        if role in {"admin", "ingress", "storage"}:
            signals.append(_server_signal(hosts.get(role), evidence.operator))
        if role == "admin":
            signals.extend(_admin_signals(evidence.admin))
            nomad = _check_signal("Nomad", checks.get("nomad"))
            if nomad["value"] == "Healthy":
                nomad["value"] = "Raft healthy"
            signals.append(nomad)
        elif role == "ingress":
            signals.append(
                _route_probe_signal("Platform route", ingress.get(f"https://{domain}/healthz"))
            )
            signals.append(
                _route_probe_signal(
                    "Wildcard route", ingress.get(f"https://wildcard-health.{domain}/healthz")
                )
            )
            facts.append(
                {
                    "label": "Mode",
                    "value": {"tunnel": "Cloudflare tunnel", "direct": "Direct"}.get(
                        mode, mode.capitalize()
                    ),
                }
            )
        elif role == "storage":
            signals.append(_check_signal("Managed services", checks.get("managed_services")))
            described = [
                f"{count}\u00a0{_RESOURCE_TYPES[kind]}"
                for kind in ("postgres", "mongo", "s3")
                if (count := storage_counts.get(kind))
            ]
            facts.append({"label": "Resources", "value": " · ".join(described) or "None"})
        elif role == "worker":
            openstack = checks.get("openstack")
            if (
                openstack is not None
                and openstack.state == "failed"
                and "worker" not in (openstack.detail.lower())
            ):
                signals.append(_signal("Servers", "Not checked", "neutral", openstack.detail))
            else:
                signals.append(
                    _counted(
                        _check_signal("Servers", openstack),
                        _health_count(reads, "openstack", "workers_active"),
                        "active",
                    )
                )
            signals.append(
                _counted(
                    _check_signal("Nomad clients", checks.get("nomad")),
                    _health_count(reads, "nomad", "ready_clients"),
                    "ready",
                )
            )
            facts.append({"label": "Running apps", "value": str(len(running))})
        elif role == "builder":
            if reads is None:
                signals.append(_signal("Builds", "Unknown", "neutral", evidence.admin.error))
            elif builds:
                subjects = sorted({item.scope for item in builds})
                signals.append(
                    _signal(
                        "Builds",
                        f"{len(builds)} in progress",
                        "info",
                        f"{_plural(len(subjects), 'application')} building",
                    )
                )
            else:
                signals.append(_signal("Builds", "None in progress", "good", None))
        result.append(
            {
                "role": role,
                "name": name,
                "lifetime": lifetime,
                "lifetimeLabel": _LIFETIMES[lifetime],
                "summary": summary,
                "status": _role_status(role, signals),
                "signals": signals,
                "facts": facts,
                "image": _image(image),
            }
        )
    return result


def _issues(
    roles: Sequence[Mapping[str, Any]],
    applications: Sequence[Mapping[str, Any]],
    check_list: Sequence[_Check],
    evidence: Evidence,
    health_stale_age: int | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for role in roles:
        for signal in role["signals"]:
            if signal["tone"] in {"critical", "warning"}:
                issues.append(
                    {
                        "tone": signal["tone"],
                        "scope": "role",
                        "target": role["role"],
                        "subject": role["name"],
                        "summary": f"{signal['label']}: {signal['value']}",
                        "detail": signal["detail"],
                    }
                )
    for application in applications:
        status = application["status"]
        if status["tone"] not in {"critical", "warning"}:
            continue
        operation = application["operation"]
        detail = application["route"]["detail"]
        if status["key"] == "needs-recovery" and operation is not None:
            detail = operation["error"] or f"{operation['label']} is waiting for recovery"
        elif status["key"] == "needs-recovery":
            detail = application["recoveryNote"]
        elif status["key"] == "not-deployed":
            detail = "The application is enabled but has no accepted deployment"
        issues.append(
            {
                "tone": status["tone"],
                "scope": "application",
                "target": application["id"],
                "subject": application["slug"],
                "summary": status["label"],
                "detail": detail,
            }
        )
    for check in check_list:
        if check.key in {"backup", "offsite_recovery"} and check.state == "failed":
            issues.append(
                {
                    "tone": check.tone if check.tone != "neutral" else "warning",
                    "scope": "platform",
                    "target": check.key,
                    "subject": check.label,
                    "summary": "Check failed",
                    "detail": check.detail,
                }
            )
    reads = evidence.admin.value
    if reads is not None and evidence.admin.ok:
        if reads.health is None:
            issues.append(
                {
                    "tone": "warning",
                    "scope": "platform",
                    "target": "health",
                    "subject": "Platform health",
                    "summary": "Snapshot unavailable",
                    "detail": reads.health_error,
                }
            )
        elif health_stale_age is not None:
            issues.append(
                {
                    "tone": "warning",
                    "scope": "platform",
                    "target": "health",
                    "subject": "Platform health",
                    "summary": "Snapshot is stale",
                    "detail": (
                        f"The five-minute health timer last reported "
                        f"{health_stale_age // 60} minutes ago"
                    ),
                }
            )
    operator = evidence.operator.value
    unreported = (
        []
        if operator is None or not evidence.operator.ok
        else [
            host.role
            for host in operator.hosts
            if host.role in {"admin", "ingress", "storage"} and not host.available
        ]
    )
    if unreported:
        issues.append(
            {
                "tone": "warning",
                "scope": "platform",
                "target": "provider",
                "subject": "OpenStack",
                "summary": "Server state unavailable",
                "detail": f"No provider observation for {', '.join(unreported)}",
            }
        )
    if not evidence.operator.ok:
        issues.append(
            {
                "tone": "warning",
                "scope": "platform",
                "target": "provider",
                "subject": "OpenStack",
                "summary": "Provider observation unavailable",
                "detail": evidence.operator.error,
            }
        )
    issues.sort(key=lambda issue: _TONES.index(issue["tone"]))
    return issues


def _summary(
    evidence: Evidence,
    roles: Sequence[Mapping[str, Any]],
    applications: Sequence[Mapping[str, Any]],
    operations: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    checks: Mapping[str, _Check],
) -> dict[str, Any]:
    visible = [item for item in applications if item["deletedAt"] is None]
    tone_counts = {tone: 0 for tone in _TONES}
    for application in visible:
        tone_counts[application["status"]["tone"]] += 1
    critical = sum(1 for issue in issues if issue["tone"] == "critical")
    warnings = sum(1 for issue in issues if issue["tone"] == "warning")
    unverified = sum(1 for role in roles if role["status"]["key"] == "unverified") + sum(
        1 for item in visible if item["status"]["key"] == "unknown"
    )
    observed = evidence.admin.value is not None or evidence.operator.value is not None
    healthy_roles = sum(
        1
        for role in roles
        if role["status"]["tone"] in {"good", "info"} or role["status"]["key"] == "idle"
    )
    if not observed:
        tone, headline = "neutral", "Status unavailable"
        detail = "No platform source has answered yet"
    elif critical:
        tone, headline = "critical", "Disruption detected"
        detail = _plural(critical, "critical issue") + (
            f" and {_plural(warnings, 'warning')}" if warnings else ""
        )
    elif warnings:
        tone, headline = "warning", "Needs attention"
        detail = _plural(warnings, "warning")
    elif unverified:
        tone, headline = "neutral", "No issues detected"
        detail = f"{_plural(unverified, 'item')} could not be fully verified"
    else:
        tone, headline = "good", "All systems operational"
        detail = (
            f"{_plural(len(roles), 'role')} and {_plural(len(visible), 'application')} verified"
        )
    backup = checks.get("backup")
    offsite = checks.get("offsite_recovery")
    reads = evidence.admin.value
    backup_age = None
    if reads is not None and reads.health is not None:
        raw = reads.health.checks.get("backup")
        if isinstance(raw, Mapping):
            backup_age = _number(raw.get("age_hours"))
    return {
        "tone": tone,
        "headline": headline,
        "detail": detail,
        "counts": {
            "roles": {"total": len(roles), "healthy": healthy_roles},
            "applications": {
                "total": len(visible),
                "serving": sum(1 for item in visible if item["status"]["key"] == "serving"),
                "attention": tone_counts["critical"] + tone_counts["warning"],
                "changing": tone_counts["info"],
                "stopped": sum(1 for item in visible if item["status"]["key"] == "stopped"),
            },
            "operations": {
                "running": sum(1 for item in operations if item["status"]["key"] == "running"),
                "recovery": sum(
                    1 for item in operations if item["status"]["key"] == "recovery_required"
                ),
            },
            "backup": {
                "ageHours": backup_age,
                "tone": "neutral" if backup is None else backup.tone,
                "state": None if backup is None else backup.state,
                "offsite": None if offsite is None else offsite.state,
            },
        },
    }


def _source(key: str, label: str, observed: Observed[Any]) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": observed.ok,
        "observedAt": observed.observed_at,
        "error": None if observed.ok else observed.error,
    }


def _controller_source(evidence: Evidence) -> dict[str, Any]:
    source = _source("controller", "Controller", evidence.admin)
    reads = evidence.admin.value
    if not evidence.admin.ok or reads is None:
        return source
    failed = [item for item in RECORD_SECTIONS if item in reads.section_errors]
    if not failed:
        return source
    # Some records were carried forward; report the oldest one still shown.
    successes = [
        evidence.admin_sections[item] for item in failed if item in evidence.admin_sections
    ]
    return source | {
        "ok": False,
        "observedAt": min(successes) if successes else None,
        "error": f"the {', '.join(failed)} read failed: {reads.section_errors[failed[0]]}",
    }


def _routes_source(evidence: Evidence) -> dict[str, Any]:
    probes = [*evidence.ingress, *evidence.routes.values()]
    answered = [probe for probe in probes if probe.status is not None]
    return {
        "key": "routes",
        "label": "Public routes",
        "ok": bool(answered) or not probes,
        "observedAt": max((probe.checked_at for probe in probes), default=None),
        "error": (
            None
            if answered or not probes
            else "no public route answered; check the operator host's network and DNS"
        ),
    }


def _platform(evidence: Evidence) -> dict[str, Any]:
    def inventory(name: str) -> str | None:
        try:
            value = evidence.platform.get(name)
        except KeyError:
            return None
        return value if isinstance(value, str) and value else None

    reads = evidence.admin.value
    return {
        "name": inventory("displayName") or evidence.platform.project_name,
        "organization": inventory("organization"),
        "domain": evidence.platform.domain,
        "region": evidence.platform.region,
        "namespace": evidence.platform.namespace,
        "apiVersion": None if reads is None else reads.api_version,
        "features": [] if reads is None else list(reads.features),
        "release": evidence.release,
    }


def build_snapshot(evidence: Evidence) -> dict[str, Any]:
    """Project one complete, JSON-serializable dashboard snapshot."""
    reads = evidence.admin.value
    health = None if reads is None else reads.health
    health_age = None if health is None else _age_seconds(health.checked_at, evidence.generated_at)
    stale = health_age is not None and health_age > HEALTH_SNAPSHOT_STALE_SECONDS
    check_list = _health_checks(health, None if reads is None else reads.health_error, stale)
    checks = {check.key: check for check in check_list}

    hosted = [] if reads is None else list(reads.operations)
    seen = {item.operation_id for item in hosted}
    operator_operations = [
        item
        for item in (() if evidence.operator.value is None else evidence.operator.value.operations)
        if item.operation_id not in seen
    ]
    ordered = sorted(
        [*hosted, *operator_operations],
        key=lambda item: (item.updated_at or "", item.operation_id),
        reverse=True,
    )
    applications = _applications(evidence, ordered)
    slugs = {item["id"]: item["slug"] for item in applications}
    operator_ids = {item.operation_id for item in operator_operations}
    operations = [
        _operation_model(
            item, slugs, plane="operator" if item.operation_id in operator_ids else "hosted"
        )
        for item in ordered
    ]
    active = [
        item for item in operations if item["status"]["key"] in {"running", "recovery_required"}
    ]
    recent = [item for item in operations if item["status"]["key"] in {"succeeded", "failed"}][
        : max(0, RECENT_OPERATIONS - len(active))
    ]
    roles = _roles(evidence, checks, ordered, applications)
    issues = _issues(roles, applications, check_list, evidence, health_age if stale else None)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "state": "ready",
        "generatedAt": evidence.generated_at,
        "platform": _platform(evidence),
        "summary": _summary(evidence, roles, applications, operations, issues, checks),
        "issues": issues,
        "roles": roles,
        "applications": applications,
        "operations": [*active, *recent],
        "operationsTruncated": reads is not None and reads.operations_truncated,
        "checks": {
            "checkedAt": None if health is None else health.checked_at,
            "stale": stale,
            "available": health is not None,
            "items": [
                {
                    "key": check.key,
                    "label": check.label,
                    "state": check.state,
                    "tone": check.tone,
                    "detail": check.detail,
                }
                for check in check_list
            ],
        },
        "sources": [
            _controller_source(evidence),
            _source("provider", "OpenStack", evidence.operator),
            {
                "key": "health",
                "label": "Health timer",
                "ok": health is not None and not stale,
                "observedAt": None if health is None else health.checked_at,
                "error": (
                    evidence.admin.error or "no admin read has completed"
                    if reads is None
                    else reads.health_error
                ),
            },
            _routes_source(evidence),
        ],
    }
