"""Small, safety-focused OpenStack image and persistent-host operations.

Names are used only to resolve configured resources.  Every mutation uses a
UUID after verifying the authenticated project name and UUID.  Provider output
is reduced to the records below; arbitrary properties, console output, and
user-data never escape this module.
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import inspect
import json
import math
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

from . import host_keys, remote, runtime
from .config import PlatformConfig, load_platform
from .validation import ValidationError, commit, openstack_uuid, sha256_hex, uuid

IMAGE_ROLES = ("admin", "ingress", "storage", "worker", "builder")
PERSISTENT_ROLES = ("admin", "ingress", "storage")
_METADATA_VERSION = "1"
_IMAGE_INVENTORY_LIMIT = 500
_IMAGE_DETAIL_CONCURRENCY = 8
_IMAGE_DETAIL_OUTPUT_LIMIT = 32_768
# The installed launcher forces this to the protected authenticated wrapper.
# The fallback preserves explicit library and test use outside that launcher.
_DEFAULT_OPENSTACK_EXECUTABLE = os.environ.get("PLATFORM_OPENSTACK_COMMAND", "openstack")
_OS_ENV = (
    "OS_AUTH_TYPE",
    "OS_AUTH_URL",
    "OS_IDENTITY_API_VERSION",
    "OS_INTERFACE",
    "OS_PASSWORD",
    "OS_PROJECT_DOMAIN_ID",
    "OS_PROJECT_DOMAIN_NAME",
    "OS_PROJECT_ID",
    "OS_PROJECT_NAME",
    "OS_REGION_NAME",
    "OS_USER_DOMAIN_ID",
    "OS_USER_DOMAIN_NAME",
    "OS_USERNAME",
)

Runner = Callable[..., Any]
_P = ParamSpec("_P")
_R = TypeVar("_R")
Checkpoint = Callable[[str, Mapping[str, Any]], None]
HealthCheck = Callable[[str, "PersistentHost", float], None]

__all__ = [
    "DriftError",
    "Flavor",
    "HostResources",
    "Image",
    "ImageSelection",
    "OpenStackError",
    "PersistentHost",
    "PowerResult",
    "ProjectIdentity",
    "PrunePlan",
    "PruneRecoveryResult",
    "PruneResult",
    "RecoveryRequired",
    "RecoveryResult",
    "ReplacementResult",
    "VolumeAttachment",
    "apply_image_prune",
    "check_role_health",
    "continue_host_replacement",
    "image_compatibility_hash",
    "host_logs",
    "list_images",
    "list_persistent_hosts",
    "observe_flavor",
    "observe_host_resources",
    "inspect_host_replacement",
    "plan_image_prune",
    "power_host",
    "publisher_metadata",
    "recover_host_replacement",
    "recover_image_prune",
    "recover_power_host",
    "replace_host",
    "rollback_host_replacement",
    "select_image",
    "select_images",
    "verify_project",
]


class OpenStackError(RuntimeError):
    """A bounded, operator-safe OpenStack failure."""


class DriftError(OpenStackError):
    pass


class RecoveryRequired(OpenStackError):
    """A mutation stopped because replay or rollback would require guessing."""

    def __init__(self, message: str, *, refs: Mapping[str, Any]) -> None:
        self.refs = dict(refs)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    project_name: str


@dataclass(frozen=True, slots=True)
class Image:
    image_id: str
    name: str
    status: str
    created_at: str
    owner_id: str
    role: str | None
    managed_by: str | None
    source_commit: str | None
    project_id: str | None
    namespace: str | None
    prefix: str | None
    compatibility_hash: str | None
    metadata_version: str | None
    platform_metadata_present: bool


@dataclass(frozen=True, slots=True)
class ImageSelection:
    role: str
    image_id: str
    display_name: str
    source_commit: str
    compatibility_hash: str

    def operation_refs(self) -> Mapping[str, Any]:
        return {
            "role": self.role,
            "image_id": self.image_id,
            "source_commit": self.source_commit,
            "compatibility_hash": self.compatibility_hash,
        }


@dataclass(frozen=True, slots=True)
class PrunePlan:
    image_ids: tuple[str, ...]
    protected_image_ids: tuple[str, ...]
    review_image_ids: tuple[str, ...]
    drift_hash: str
    created_at: str
    expires_at: str
    retain_newest: int
    candidate_fingerprints: tuple[tuple[str, str], ...] = ()
    selected_image_ids: tuple[str, ...] = ()
    operation_image_ids: tuple[str, ...] = ()
    server_image_ids: tuple[str, ...] = ()
    inventory_hash: str | None = None

    def operation_refs(self) -> Mapping[str, Any]:
        refs: dict[str, Any] = {
            "image_ids": list(self.image_ids),
            "protected_image_ids": list(self.protected_image_ids),
            "review_image_ids": list(self.review_image_ids),
            "drift_hash": self.drift_hash,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "retain_newest": self.retain_newest,
            "candidate_fingerprints": [
                {"image_id": image_id, "sha256": fingerprint}
                for image_id, fingerprint in self.candidate_fingerprints
            ],
            "selected_image_ids": list(self.selected_image_ids),
            "operation_image_ids": list(self.operation_image_ids),
            "server_image_ids": list(self.server_image_ids),
        }
        if self.inventory_hash is not None:
            refs["inventory_hash"] = self.inventory_hash
        return refs


@dataclass(frozen=True, slots=True)
class PruneResult:
    planned_image_ids: tuple[str, ...]
    deleted_image_ids: tuple[str, ...]
    drift_hash: str
    protected_image_ids: tuple[str, ...]
    review_image_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PruneRecoveryResult:
    action: str
    planned_image_ids: tuple[str, ...]
    deleted_image_ids: tuple[str, ...]
    pending_image_ids: tuple[str, ...]
    drift_hash: str


@dataclass(frozen=True, slots=True)
class Flavor:
    flavor_id: str
    name: str
    vcpus: int
    ram_mib: int


@dataclass(frozen=True, slots=True)
class PersistentHost:
    role: str
    configured_name: str
    server_id: str | None
    status: str | None
    image_id: str | None
    flavor_id: str | None
    flavor_name: str | None
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VolumeAttachment:
    volume_id: str
    name: str
    device: str
    delete_on_termination: bool


@dataclass(frozen=True, slots=True)
class HostResources:
    host: PersistentHost
    port_id: str
    port_name: str
    volumes: tuple[VolumeAttachment, ...]

    def operation_refs(self) -> Mapping[str, Any]:
        return {
            "role": self.host.role,
            "old_server_id": self.host.server_id,
            "old_image_id": self.host.image_id,
            "old_flavor_id": self.host.flavor_id,
            "old_status": self.host.status,
            "port_id": self.port_id,
            "volume_ids": [volume.volume_id for volume in self.volumes],
            "volume_attachments": [
                {"volume_id": volume.volume_id, "device": volume.device} for volume in self.volumes
            ],
        }


@dataclass(frozen=True, slots=True)
class PowerResult:
    role: str
    server_id: str
    action: str
    status: str
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplacementResult:
    role: str
    accepted: bool
    active_server_id: str
    selected_image_id: str
    old_server_id: str
    cleanup_state: str
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    role: str
    phase: str
    action: str
    active_server_id: str
    old_server_id: str
    replacement_server_id: str | None
    cleanup_state: str


def _role(value: str, *, persistent: bool = False) -> str:
    choices = PERSISTENT_ROLES if persistent else IMAGE_ROLES
    if value not in choices:
        raise ValidationError(f"role must be one of {', '.join(choices)}")
    return value


def _inventory_text(platform: PlatformConfig, dotted_name: str) -> str:
    value = platform.get(dotted_name)
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode()) > 256:
        raise ValidationError(f"{dotted_name} must be bounded non-empty text")
    return value


def _metadata_prefix(platform: PlatformConfig) -> str:
    return platform.namespace.replace("-", "_")


def image_compatibility_hash(platform: PlatformConfig) -> str:
    """Hash the stable image/deployment compatibility projection.

    Image display names are deliberately excluded: publication derives
    commit-addressed names.  The projection covers the identity and PKI naming
    embedded in every role image.
    """
    projection = {
        "format": 1,
        "namespace": platform.namespace,
        "pkiInternalCaFile": _inventory_text(platform, "pki.internalCaFile"),
        "prefix": platform.prefix,
        "projectId": platform.project_id,
    }
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def publisher_metadata(
    platform: PlatformConfig, role: str, source_commit: str
) -> Mapping[str, str]:
    """Return the one canonical Glance metadata projection."""
    role = _role(role)
    source_commit = commit(source_commit)
    key = _metadata_prefix(platform)
    return {
        f"{key}_compatibility_sha256": image_compatibility_hash(platform),
        f"{key}_managed_by": "platform",
        f"{key}_metadata_version": _METADATA_VERSION,
        f"{key}_namespace": platform.namespace,
        f"{key}_prefix": platform.prefix,
        f"{key}_project_id": platform.project_id,
        f"{key}_role": role,
        f"{key}_source_commit": source_commit,
    }


def _system_monotonic() -> float:
    return time.monotonic()


_ACTIVE_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "openstack_active_deadline", default=None
)
_CLOCK: contextvars.ContextVar[Callable[[], float]] = contextvars.ContextVar(
    "openstack_monotonic", default=_system_monotonic
)


def _monotonic() -> float:
    return _CLOCK.get()()


def _timeout_value(value: object, *, operation: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpenStackError(f"OpenStack {operation} timeout is malformed")
    timeout = float(value)
    if timeout <= 0 or not math.isfinite(timeout):
        raise OpenStackError(f"OpenStack {operation} timeout must be positive and finite")
    return timeout


@contextmanager
def _command_scope(
    timeout_seconds: object,
    *,
    operation: str,
    clock: Callable[[], float] | None = None,
) -> Iterator[float]:
    """Share one absolute deadline across every provider call in an operation."""
    existing = _ACTIVE_DEADLINE.get()
    if existing is not None:
        _deadline_remaining(existing, operation=operation)
        yield existing
        return

    timeout = _timeout_value(timeout_seconds, operation=operation)
    selected_clock = _system_monotonic if clock is None else clock
    if not callable(selected_clock):
        raise OpenStackError(f"OpenStack {operation} clock is malformed")
    clock_token = _CLOCK.set(selected_clock)
    deadline = _monotonic() + timeout
    deadline_token = _ACTIVE_DEADLINE.set(deadline)
    try:
        _deadline_remaining(deadline, operation=operation)
        yield deadline
    finally:
        _ACTIVE_DEADLINE.reset(deadline_token)
        _CLOCK.reset(clock_token)


def _command_deadline(parameter: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Decorate a public operation without restarting nested phase budgets."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        signature = inspect.signature(function)
        parameter_info = signature.parameters[parameter]

        @wraps(function)
        def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            injected_clock = kwargs.pop("clock", None)
            alternate_clock = kwargs.pop("monotonic", None)
            if injected_clock is not None and alternate_clock is not None:
                raise OpenStackError("OpenStack operation received two clocks")
            clock = cast(
                Callable[[], float] | None,
                injected_clock if injected_clock is not None else alternate_clock,
            )
            bound = signature.bind_partial(*args, **kwargs)
            seconds = bound.arguments.get(parameter, parameter_info.default)
            if seconds is inspect.Parameter.empty:
                raise OpenStackError(f"OpenStack {function.__name__} timeout is missing")
            with _command_scope(seconds, operation=function.__name__, clock=clock):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def _run(
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
    check: bool = True,
    stdin: bytes | None = None,
    stdout_limit: int = 1_048_576,
) -> Any:
    active_deadline = _ACTIVE_DEADLINE.get()
    if active_deadline is not None:
        timeout_seconds = min(
            _timeout_value(timeout_seconds, operation="command"),
            _deadline_remaining(active_deadline, operation="command"),
        )
    else:
        timeout_seconds = _timeout_value(timeout_seconds, operation="command")
    try:
        result = command_runner(
            (executable, *arguments),
            timeout_seconds=timeout_seconds,
            stdin=stdin,
            stdout_limit=stdout_limit,
            stderr_limit=262_144,
            inherit_env=_OS_ENV,
            check=check,
        )
    except runtime.CommandFailure as error:
        raise OpenStackError("OpenStack command failed; provider details were withheld") from error
    if result.stdout_truncated or result.stderr_truncated:
        raise OpenStackError("OpenStack command output exceeded its safety limit")
    return result


def _json_command(
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
    stdout_limit: int = 1_048_576,
) -> Any:
    result = _run(
        (*arguments, "--format", "json"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
        stdout_limit=stdout_limit,
    )
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise OpenStackError("OpenStack returned malformed JSON") from error


def _field(document: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    lowered = {str(key).lower().replace(" ", "_"): value for key, value in document.items()}
    for name in names:
        key = name.lower().replace(" ", "_")
        if key in lowered:
            return lowered[key]
    return default


def _provider_uuid(value: Any, *, field: str) -> str:
    """Normalize a UUID only after it crosses the trusted OpenStack output boundary."""
    try:
        return openstack_uuid(value, field=field)
    except ValidationError as error:
        raise OpenStackError(f"OpenStack returned a malformed {field}") from error


def _deadline_remaining(deadline: float, *, operation: str) -> float:
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise OpenStackError(f"OpenStack {operation} exceeded its deadline")
    return remaining


def _active_deadline(*, operation: str) -> float:
    deadline = _ACTIVE_DEADLINE.get()
    if deadline is None:
        raise OpenStackError(f"OpenStack {operation} has no active deadline")
    _deadline_remaining(deadline, operation=operation)
    return deadline


@_command_deadline("timeout_seconds")
def verify_project(
    platform: PlatformConfig,
    *,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> ProjectIdentity:
    """Verify the token's project by UUID and the project's observed name."""
    deadline = _active_deadline(operation="project verification")
    token = _json_command(
        ("token", "issue", "--column", "project_id"),
        timeout_seconds=_deadline_remaining(deadline, operation="project verification"),
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(token, Mapping):
        raise OpenStackError("OpenStack token projection was not an object")
    raw_project_id = _field(token, "project_id")
    project_id = _provider_uuid(raw_project_id, field="project UUID")
    configured_project_id = uuid(platform.project_id, field="configured project UUID")
    if project_id != configured_project_id:
        raise OpenStackError("authenticated OpenStack project UUID does not match configuration")
    assert isinstance(raw_project_id, str)  # Established by the provider UUID parser above.
    project = _json_command(
        ("project", "show", raw_project_id, "--column", "id", "--column", "name"),
        timeout_seconds=_deadline_remaining(deadline, operation="project verification"),
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(project, Mapping):
        raise OpenStackError("OpenStack project projection was not an object")
    observed_id = _provider_uuid(_field(project, "id"), field="project UUID")
    observed_name = _field(project, "name")
    if observed_id != project_id or observed_name != platform.project_name:
        raise OpenStackError("authenticated OpenStack project name does not match configuration")
    return ProjectIdentity(project_id=project_id, project_name=observed_name)


def _properties(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_property(properties: Mapping[str, Any], key: str, expected: str) -> str | None:
    if key not in properties:
        return None
    return expected if properties[key] == expected else "<incompatible>"


def _validated_property(
    properties: Mapping[str, Any], key: str, validator: Callable[[object], str]
) -> str | None:
    if key not in properties:
        return None
    try:
        return validator(properties[key])
    except ValidationError:
        return "<incompatible>"


def _image_from_show(platform: PlatformConfig, document: Mapping[str, Any]) -> Image:
    key = _metadata_prefix(platform)
    raw_properties = _field(document, "properties", default={})
    properties = _properties(raw_properties)
    malformed_properties = not isinstance(raw_properties, Mapping)
    owner = _field(document, "owner", "owner_id", "project")
    role_key = f"{key}_role"
    role = properties.get(role_key)
    if role_key in properties and role not in IMAGE_ROLES:
        role = "<incompatible>"
    source = _validated_property(properties, f"{key}_source_commit", commit)
    project_id = _validated_property(
        properties, f"{key}_project_id", lambda value: uuid(value, field="image project UUID")
    )
    compatibility = _validated_property(
        properties,
        f"{key}_compatibility_sha256",
        lambda value: sha256_hex(value, field="image compatibility hash"),
    )
    created_at = _field(document, "created_at", "created", default="")
    try:
        datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        created_at = ""
    status = str(_field(document, "status", default="")).lower()
    if status not in {
        "active",
        "queued",
        "saving",
        "killed",
        "deleted",
        "pending_delete",
        "deactivated",
    }:
        status = "<incompatible>"
    return Image(
        image_id=_provider_uuid(_field(document, "id"), field="image UUID"),
        name=str(_field(document, "name", default=""))[:256],
        status=status,
        created_at=str(created_at),
        owner_id=_provider_uuid(owner, field="image owner UUID"),
        role=role,
        managed_by=_safe_property(properties, f"{key}_managed_by", "platform"),
        source_commit=source,
        project_id=project_id,
        namespace=_safe_property(properties, f"{key}_namespace", platform.namespace),
        prefix=_safe_property(properties, f"{key}_prefix", platform.prefix),
        compatibility_hash=compatibility,
        metadata_version=_safe_property(properties, f"{key}_metadata_version", _METADATA_VERSION),
        platform_metadata_present=malformed_properties
        or any(str(name).startswith(f"{key}_") for name in properties),
    )


def _long_image_row_is_complete(row: Mapping[str, Any]) -> bool:
    missing = object()
    return all(
        _field(row, *names, default=missing) is not missing
        for names in (("owner", "owner_id", "project"), ("created_at", "created"), ("properties",))
    )


def _show_image_for_inventory(
    image_id: str,
    *,
    deadline: float,
    command_runner: Runner,
    executable: str,
) -> Mapping[str, Any]:
    shown = _json_command(
        (
            "image",
            "show",
            image_id,
            "--column",
            "id",
            "--column",
            "name",
            "--column",
            "status",
            "--column",
            "created_at",
            "--column",
            "owner",
            "--column",
            "properties",
        ),
        timeout_seconds=_deadline_remaining(deadline, operation="image observation"),
        command_runner=command_runner,
        executable=executable,
        stdout_limit=_IMAGE_DETAIL_OUTPUT_LIMIT,
    )
    if not isinstance(shown, Mapping):
        raise OpenStackError("OpenStack image projection was not an object")
    if _provider_uuid(_field(shown, "id"), field="image UUID") != image_id:
        raise OpenStackError("OpenStack image identity changed during observation")
    return shown


def _list_images_verified(
    platform: PlatformConfig,
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> list[Image]:
    """Read one long inventory, with a fixed-width detail fallback for older OSCs.

    Current python-openstackclient releases omit creation time and properties
    from ``image list --long``. When those fields are absent, detail reads run
    in at most eight concurrent slots under the same deadline. The 500-row
    ceiling and 32-KiB detail limit bound fallback provider output to 16 MiB.
    """
    deadline = _active_deadline(operation="image observation")
    listed = _json_command(
        ("image", "list", "--private", "--long"),
        timeout_seconds=_deadline_remaining(deadline, operation="image observation"),
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(listed, list):
        raise OpenStackError("OpenStack image list was not an array")
    if len(listed) > _IMAGE_INVENTORY_LIMIT:
        raise OpenStackError("OpenStack image inventory exceeds its 500-image safety limit")

    documents: list[Mapping[str, Any] | None] = []
    detail_ids: list[tuple[int, str]] = []
    for row in listed:
        if not isinstance(row, Mapping):
            raise OpenStackError("OpenStack image list contained a malformed row")
        image_id = _provider_uuid(_field(row, "id"), field="image UUID")
        if _long_image_row_is_complete(row):
            documents.append(row)
        else:
            detail_ids.append((len(documents), image_id))
            documents.append(None)

    if detail_ids:
        with ThreadPoolExecutor(
            max_workers=_IMAGE_DETAIL_CONCURRENCY, thread_name_prefix="openstack-image"
        ) as executor:
            pending = {
                executor.submit(
                    contextvars.copy_context().run,
                    _show_image_for_inventory,
                    image_id,
                    deadline=deadline,
                    command_runner=command_runner,
                    executable=executable,
                ): (index, image_id)
                for index, image_id in detail_ids
            }
            try:
                for future in as_completed(
                    pending,
                    timeout=_deadline_remaining(deadline, operation="image observation"),
                ):
                    index, _image_id = pending[future]
                    documents[index] = future.result()
            except TimeoutError as error:
                for future in pending:
                    future.cancel()
                raise OpenStackError("OpenStack image observation exceeded its deadline") from error
            except Exception:
                for future in pending:
                    future.cancel()
                raise

    images: list[Image] = []
    for document in documents:
        if document is None:
            raise OpenStackError("OpenStack image observation was incomplete")
        image = _image_from_show(platform, document)
        if image.platform_metadata_present:
            images.append(image)
    return sorted(images, key=lambda image: (image.created_at, image.image_id), reverse=True)


@_command_deadline("timeout_seconds")
def list_images(
    platform: PlatformConfig,
    *,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> list[Image]:
    deadline = _active_deadline(operation="image observation")
    verify_project(
        platform,
        timeout_seconds=_deadline_remaining(deadline, operation="image observation"),
        command_runner=command_runner,
        executable=executable,
    )
    return _list_images_verified(
        platform,
        timeout_seconds=_deadline_remaining(deadline, operation="image observation"),
        command_runner=command_runner,
        executable=executable,
    )


def _complete_image(platform: PlatformConfig, image: Image, role: str) -> bool:
    try:
        commit(image.source_commit)
        sha256_hex(image.compatibility_hash, field="image compatibility hash")
        uuid(image.project_id, field="image project UUID")
        created_at = datetime.fromisoformat(image.created_at.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            return False
    except (ValidationError, ValueError):
        return False
    return (
        image.owner_id == platform.project_id
        and image.status == "active"
        and image.managed_by == "platform"
        and image.role == role
        and image.project_id == platform.project_id
        and image.namespace == platform.namespace
        and image.prefix == platform.prefix
        and image.compatibility_hash == image_compatibility_hash(platform)
        and image.metadata_version == _METADATA_VERSION
    )


def _select_image_from_inventory(
    platform: PlatformConfig,
    role: str,
    reference: str,
    images: Sequence[Image],
) -> ImageSelection:
    role = _role(role)
    try:
        image_id = uuid(reference, field="image UUID")
    except ValidationError:
        matches = [image for image in images if image.name == reference]
    else:
        matches = [image for image in images if image.image_id == image_id]
    if len(matches) != 1:
        raise OpenStackError("image reference must resolve to exactly one project image")
    image = matches[0]
    conflicting_names = [
        other
        for other in images
        if other.image_id != image.image_id
        and other.name == image.name
        and (other.source_commit != image.source_commit or other.role != image.role)
    ]
    if conflicting_names:
        raise OpenStackError("image display name has conflicting provenance")
    if _complete_image(platform, image, role):
        assert image.source_commit is not None and image.compatibility_hash is not None
        return ImageSelection(
            role=role,
            image_id=image.image_id,
            display_name=image.name,
            source_commit=image.source_commit,
            compatibility_hash=image.compatibility_hash,
        )
    raise OpenStackError("image metadata is incompatible with the configured deployment and role")


@_command_deadline("timeout_seconds")
def select_images(
    platform: PlatformConfig,
    references: Mapping[str, str],
    *,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> dict[str, ImageSelection]:
    """Resolve multiple roles against one authenticated image inventory."""
    requested = [(_role(role), reference) for role, reference in references.items()]
    if any(not isinstance(reference, str) for _role_name, reference in requested):
        raise ValidationError("image reference must be text")
    images = list_images(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    return {
        role: _select_image_from_inventory(
            platform,
            role,
            reference,
            images,
        )
        for role, reference in requested
    }


@_command_deadline("timeout_seconds")
def select_image(
    platform: PlatformConfig,
    role: str,
    reference: str,
    *,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> ImageSelection:
    """Resolve and validate one active image without changing a running host."""
    selected = select_images(
        platform,
        {role: reference},
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    return selected[role]


def _ids(values: Iterable[str], *, field: str) -> tuple[str, ...]:
    return tuple(sorted({uuid(value, field=field) for value in values}))


def _image_fingerprint(image: Image) -> str:
    projection = {
        "id": image.image_id,
        "name": image.name,
        "status": image.status,
        "created_at": image.created_at,
        "owner_id": image.owner_id,
        "role": image.role,
        "managed_by": image.managed_by,
        "source_commit": image.source_commit,
        "project_id": image.project_id,
        "namespace": image.namespace,
        "prefix": image.prefix,
        "compatibility_hash": image.compatibility_hash,
        "metadata_version": image.metadata_version,
        "platform_metadata_present": image.platform_metadata_present,
    }
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_MISSING = object()


def _server_image_id(value: Any) -> str | None:
    """Parse Nova's image projection without treating malformed data as boot-from-volume."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        if not any(str(key).lower().replace(" ", "_") == "id" for key in value):
            raise OpenStackError("OpenStack server image projection is malformed")
        value = _field(value, "id", default=_MISSING)
        if not isinstance(value, str) or not value:
            raise OpenStackError("OpenStack server image projection is malformed")
    if not isinstance(value, str):
        raise OpenStackError("OpenStack server image projection is malformed")
    text = value.strip()
    if text == "" or text.upper() == "N/A":
        return None
    if " (" in text:
        if text.count(" (") != 1 or not text.endswith(")"):
            raise OpenStackError("OpenStack server image projection is ambiguous")
        text = text.rsplit(" (", 1)[1][:-1]
        if not text or "(" in text or ")" in text:
            raise OpenStackError("OpenStack server image projection is ambiguous")
    try:
        return openstack_uuid(text, field="server image UUID")
    except ValidationError as error:
        raise OpenStackError("OpenStack returned a malformed server image UUID") from error


def _server_image_ids_verified(
    *, timeout_seconds: float, command_runner: Runner, executable: str
) -> tuple[str, ...]:
    rows = _json_command(
        ("server", "list", "--column", "ID", "--column", "Image"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(rows, list):
        raise OpenStackError("OpenStack server list was not an array")
    result: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise OpenStackError("OpenStack server list contained a malformed row")
        value = _field(row, "image", default=_MISSING)
        if value is _MISSING:
            raise OpenStackError("OpenStack server list omitted its image projection")
        image_id = _server_image_id(value)
        if image_id is not None:
            result.add(image_id)
    return tuple(sorted(result))


def _inventory_hash(images: Sequence[Image]) -> str:
    """Return a deletion-updatable hash of exact image projections.

    XOR is intentional: recovery can remove the recorded fingerprints of
    confirmed deletions from the original aggregate while still detecting any
    addition or metadata change in the remaining provider inventory.
    """
    aggregate = 0
    for image in images:
        aggregate ^= int(_image_fingerprint(image), 16)
    return f"{aggregate:064x}"


def _hash_without_fingerprints(value: str, fingerprints: Iterable[str]) -> str:
    aggregate = int(sha256_hex(value, field="image inventory hash"), 16)
    for fingerprint in fingerprints:
        aggregate ^= int(sha256_hex(fingerprint, field="candidate fingerprint"), 16)
    return f"{aggregate:064x}"


def _prune_projection(
    images: Sequence[Image],
    *,
    candidates: Sequence[str],
    protected: Sequence[str],
    review: Sequence[str],
    selected: Sequence[str],
    operations: Sequence[str],
    servers: Sequence[str],
    retain_newest: int,
) -> str:
    projection = {
        "candidates": list(candidates),
        "images": [
            {
                "id": image.image_id,
                "name": image.name,
                "status": image.status,
                "created_at": image.created_at,
                "owner_id": image.owner_id,
                "role": image.role,
                "managed_by": image.managed_by,
                "source_commit": image.source_commit,
                "project_id": image.project_id,
                "namespace": image.namespace,
                "prefix": image.prefix,
                "compatibility_hash": image.compatibility_hash,
                "metadata_version": image.metadata_version,
                "platform_metadata_present": image.platform_metadata_present,
            }
            for image in sorted(images, key=lambda item: item.image_id)
        ],
        "operations": list(operations),
        "protected": list(protected),
        "retain_newest": retain_newest,
        "review": list(review),
        "selected": list(selected),
        "servers": list(servers),
    }
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@_command_deadline("timeout_seconds")
def plan_image_prune(
    platform: PlatformConfig,
    *,
    selected_image_ids: Iterable[str],
    operation_image_ids: Iterable[str] = (),
    retain_newest: int = 2,
    expires_in_seconds: int = 900,
    now: datetime | None = None,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> PrunePlan:
    """Observe and hash an exact conservative deletion plan; delete nothing."""
    if isinstance(retain_newest, bool) or retain_newest < 1 or retain_newest > 100:
        raise ValidationError("retain_newest must be an integer from 1 through 100")
    if not 60 <= expires_in_seconds <= 3600:
        raise ValidationError("prune plan expiry must be from 60 through 3600 seconds")
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    images = _list_images_verified(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    selected_ids = _ids(selected_image_ids, field="selected image UUID")
    operation_ids = _ids(operation_image_ids, field="operation image UUID")
    server_ids = _server_image_ids_verified(
        timeout_seconds=timeout_seconds, command_runner=command_runner, executable=executable
    )
    protected = set(selected_ids)
    protected.update(operation_ids)
    protected.update(server_ids)

    valid_by_role: dict[str, list[Image]] = {role: [] for role in IMAGE_ROLES}
    review: set[str] = set()
    for image in images:
        if image.role in IMAGE_ROLES and _complete_image(platform, image, image.role):
            valid_by_role[image.role].append(image)
        elif image.platform_metadata_present:
            review.add(image.image_id)
    for role_images in valid_by_role.values():
        role_images.sort(key=lambda image: (image.created_at, image.image_id), reverse=True)
        protected.update(image.image_id for image in role_images[:retain_newest])
    candidates = sorted(
        image.image_id
        for role_images in valid_by_role.values()
        for image in role_images
        if image.image_id not in protected
    )
    if len(candidates) > 100:
        raise OpenStackError("image prune plan exceeds its 100-image safety limit")
    protected_ids = tuple(sorted(protected))
    review_ids = tuple(sorted(review))
    drift_hash = _prune_projection(
        images,
        candidates=candidates,
        protected=protected_ids,
        review=review_ids,
        selected=selected_ids,
        operations=operation_ids,
        servers=server_ids,
        retain_newest=retain_newest,
    )
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValidationError("prune plan time must include a timezone")
    by_id = {image.image_id: image for image in images}
    return PrunePlan(
        image_ids=tuple(candidates),
        protected_image_ids=protected_ids,
        review_image_ids=review_ids,
        drift_hash=drift_hash,
        created_at=instant.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        expires_at=(instant + timedelta(seconds=expires_in_seconds))
        .astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        retain_newest=retain_newest,
        candidate_fingerprints=tuple(
            (image_id, _image_fingerprint(by_id[image_id])) for image_id in candidates
        ),
        selected_image_ids=selected_ids,
        operation_image_ids=operation_ids,
        server_image_ids=server_ids,
        inventory_hash=_inventory_hash(images),
    )


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError("prune plan has a malformed expiry") from error
    if parsed.tzinfo is None:
        raise ValidationError("prune plan expiry must include a timezone")
    return parsed


def _resource_exists(
    kind: str,
    resource_id: str,
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> bool:
    arguments = [kind, "list"]
    if kind == "image":
        arguments.append("--private")
    arguments.extend(("--column", "ID"))
    rows = _json_command(
        arguments,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise OpenStackError(f"OpenStack {kind} existence projection was malformed")
    observed_ids = {_provider_uuid(_field(row, "id"), field=f"{kind} UUID") for row in rows}
    return resource_id in observed_ids


def _validate_prune_plan(plan: PrunePlan) -> None:
    image_ids = _ids(plan.image_ids, field="prune image UUID")
    protected_ids = _ids(plan.protected_image_ids, field="protected image UUID")
    review_ids = _ids(plan.review_image_ids, field="review image UUID")
    if image_ids != plan.image_ids or protected_ids != plan.protected_image_ids:
        raise ValidationError("saved image prune plan UUIDs must be unique and sorted")
    if review_ids != plan.review_image_ids:
        raise ValidationError("saved image prune review UUIDs must be unique and sorted")
    if set(image_ids) & (set(protected_ids) | set(review_ids)):
        raise ValidationError("saved image prune candidates overlap protected or review images")
    if len(image_ids) > 100:
        raise ValidationError("saved image prune plan exceeds its 100-image safety limit")
    sha256_hex(plan.drift_hash, field="prune drift hash")
    if isinstance(plan.retain_newest, bool) or not 1 <= plan.retain_newest <= 100:
        raise ValidationError("saved image prune retention is malformed")
    created = _parse_time(plan.created_at)
    expires = _parse_time(plan.expires_at)
    if expires <= created or expires - created > timedelta(hours=1):
        raise ValidationError("saved image prune validity interval is malformed")
    expected_fingerprints = tuple(image_id for image_id, _ in plan.candidate_fingerprints)
    if plan.candidate_fingerprints and expected_fingerprints != image_ids:
        raise ValidationError("saved image prune candidate fingerprints are incomplete")
    for _image_id, fingerprint in plan.candidate_fingerprints:
        sha256_hex(fingerprint, field="candidate fingerprint")
    selected_ids = _ids(plan.selected_image_ids, field="selected image UUID")
    operation_ids = _ids(plan.operation_image_ids, field="operation image UUID")
    server_ids = _ids(plan.server_image_ids, field="server image UUID")
    if (
        selected_ids != plan.selected_image_ids
        or operation_ids != plan.operation_image_ids
        or server_ids != plan.server_image_ids
    ):
        raise ValidationError("saved image prune protection UUIDs must be unique and sorted")
    if not (set(selected_ids) | set(operation_ids) | set(server_ids)) <= set(protected_ids):
        raise ValidationError("saved image prune protection references are incomplete")
    if plan.inventory_hash is not None:
        sha256_hex(plan.inventory_hash, field="image inventory hash")
        if not plan.candidate_fingerprints:
            raise ValidationError("saved image inventory hash requires candidate fingerprints")


def _prune_recovery_refs(
    plan: PrunePlan, deleted: Sequence[str], pending: str | None
) -> dict[str, Any]:
    refs = dict(plan.operation_refs())
    refs["deleted_image_ids"] = list(deleted)
    refs["pending_image_id"] = pending
    return refs


def _delete_planned_images(
    platform: PlatformConfig,
    plan: PrunePlan,
    deleted: list[str],
    *,
    checkpoint: Checkpoint,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> None:
    fingerprints = dict(plan.candidate_fingerprints)
    for image_id in plan.image_ids[len(deleted) :]:
        checkpoint("image_deleting", _prune_recovery_refs(plan, deleted, image_id))
        used = _server_image_ids_verified(
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        if image_id in used:
            error = "planned image became attached to a server before deletion"
            if deleted:
                raise RecoveryRequired(error, refs=_prune_recovery_refs(plan, deleted, image_id))
            raise DriftError(error)
        shown = _json_command(
            (
                "image",
                "show",
                image_id,
                "--column",
                "id",
                "--column",
                "name",
                "--column",
                "status",
                "--column",
                "created_at",
                "--column",
                "owner",
                "--column",
                "properties",
            ),
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        if (
            not isinstance(shown, Mapping)
            or image_id not in fingerprints
            or _image_fingerprint(_image_from_show(platform, shown)) != fingerprints[image_id]
        ):
            error = "planned image metadata drifted before deletion"
            if deleted:
                raise RecoveryRequired(error, refs=_prune_recovery_refs(plan, deleted, image_id))
            raise DriftError(error)
        try:
            _run(
                ("image", "delete", image_id),
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
            )
        except OpenStackError:
            if _resource_exists(
                "image",
                image_id,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
            ):
                raise RecoveryRequired(
                    "image deletion result is ambiguous; inspect the recorded UUIDs",
                    refs=_prune_recovery_refs(plan, deleted, image_id),
                ) from None
        if _resource_exists(
            "image",
            image_id,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        ):
            raise RecoveryRequired(
                "image remained after deletion; inspect the recorded UUID",
                refs=_prune_recovery_refs(plan, deleted, image_id),
            )
        deleted.append(image_id)
        checkpoint("image_deleted", _prune_recovery_refs(plan, deleted, None))


@_command_deadline("timeout_seconds")
def apply_image_prune(
    platform: PlatformConfig,
    plan: PrunePlan,
    *,
    selected_image_ids: Iterable[str],
    operation_image_ids: Iterable[str] = (),
    checkpoint: Checkpoint,
    now: datetime | None = None,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> PruneResult:
    """Re-observe a saved plan and delete only exact UUIDs after no drift."""
    _validate_prune_plan(plan)
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.astimezone(UTC) > _parse_time(plan.expires_at):
        raise DriftError("saved image prune plan has expired")
    selected_ids = _ids(selected_image_ids, field="selected image UUID")
    operation_ids = _ids(operation_image_ids, field="operation image UUID")
    if plan.selected_image_ids and selected_ids != plan.selected_image_ids:
        raise DriftError("selected image protection references drifted since the saved plan")
    if plan.operation_image_ids and operation_ids != plan.operation_image_ids:
        raise DriftError("operation image protection references drifted since the saved plan")
    fresh = plan_image_prune(
        platform,
        selected_image_ids=selected_ids,
        operation_image_ids=operation_ids,
        retain_newest=plan.retain_newest,
        expires_in_seconds=900,
        now=instant,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if (
        fresh.drift_hash != plan.drift_hash
        or fresh.image_ids != plan.image_ids
        or (
            plan.candidate_fingerprints
            and fresh.candidate_fingerprints != plan.candidate_fingerprints
        )
        or (plan.server_image_ids and fresh.server_image_ids != plan.server_image_ids)
        or (plan.inventory_hash is not None and fresh.inventory_hash != plan.inventory_hash)
    ):
        raise DriftError("image inventory or protected references drifted since the saved plan")
    # Older persisted plans remain applicable after the same full re-observation,
    # but only plans with exact fingerprints support interrupted-operation recovery.
    effective = (
        plan
        if plan.candidate_fingerprints
        else replace(plan, candidate_fingerprints=fresh.candidate_fingerprints)
    )
    deleted: list[str] = []
    _delete_planned_images(
        platform,
        effective,
        deleted,
        checkpoint=checkpoint,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    return PruneResult(
        plan.image_ids,
        tuple(deleted),
        plan.drift_hash,
        plan.protected_image_ids,
        plan.review_image_ids,
    )


def _reconciled_prune_deletions(
    platform: PlatformConfig,
    plan: PrunePlan,
    refs: Mapping[str, Any],
    *,
    selected_image_ids: Iterable[str],
    operation_image_ids: Iterable[str],
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> list[str]:
    _validate_prune_plan(plan)
    if plan.inventory_hash is None or not plan.candidate_fingerprints:
        raise ValidationError("saved image prune plan does not support exact recovery")
    if refs.get("drift_hash") != plan.drift_hash:
        raise ValidationError("image prune recovery refs do not match the saved plan")
    selected_ids = _ids(selected_image_ids, field="selected image UUID")
    operation_ids = _ids(operation_image_ids, field="operation image UUID")
    if selected_ids != plan.selected_image_ids or operation_ids != plan.operation_image_ids:
        raise DriftError("image prune protection references drifted during recovery")
    deleted_value = refs.get("deleted_image_ids")
    if not isinstance(deleted_value, list):
        raise ValidationError("image prune recovery deleted UUIDs are missing")
    deleted = list(_ids(deleted_value, field="deleted image UUID"))
    if tuple(deleted) != plan.image_ids[: len(deleted)]:
        raise ValidationError("image prune recovery deletions are not a plan prefix")
    pending_value = refs.get("pending_image_id")
    pending = uuid(pending_value, field="pending image UUID") if pending_value is not None else None
    expected_pending = plan.image_ids[len(deleted)] if len(deleted) < len(plan.image_ids) else None
    if pending not in (None, expected_pending):
        raise ValidationError("image prune recovery pending UUID is not the next candidate")

    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    images = _list_images_verified(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    by_id = {image.image_id: image for image in images}
    if pending is not None and pending not in by_id:
        deleted.append(pending)
    fingerprints = dict(plan.candidate_fingerprints)
    if any(image_id in by_id for image_id in deleted):
        raise DriftError("a confirmed image prune deletion is present again")
    for image_id in plan.image_ids[len(deleted) :]:
        image = by_id.get(image_id)
        if image is None or _image_fingerprint(image) != fingerprints[image_id]:
            raise DriftError("pending image prune candidate inventory drifted")
    expected_hash = _hash_without_fingerprints(
        plan.inventory_hash, (fingerprints[image_id] for image_id in deleted)
    )
    if _inventory_hash(images) != expected_hash:
        raise DriftError("image inventory drifted during prune recovery")
    server_ids = _server_image_ids_verified(
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if server_ids != plan.server_image_ids:
        raise DriftError("server image references drifted during prune recovery")
    return deleted


@_command_deadline("timeout_seconds")
def recover_image_prune(
    platform: PlatformConfig,
    plan: PrunePlan,
    *,
    refs: Mapping[str, Any],
    action: str,
    selected_image_ids: Iterable[str],
    operation_image_ids: Iterable[str] = (),
    checkpoint: Checkpoint,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> PruneRecoveryResult:
    """Inspect or continue one exact interrupted provider-side prune."""
    if action not in ("inspect", "continue"):
        raise ValidationError("image prune recovery action must be inspect or continue")
    deleted = _reconciled_prune_deletions(
        platform,
        plan,
        refs,
        selected_image_ids=selected_image_ids,
        operation_image_ids=operation_image_ids,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if action == "continue":
        _delete_planned_images(
            platform,
            plan,
            deleted,
            checkpoint=checkpoint,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
    pending = plan.image_ids[len(deleted) :]
    return PruneRecoveryResult(
        action, plan.image_ids, tuple(deleted), tuple(pending), plan.drift_hash
    )


@_command_deadline("timeout_seconds")
def observe_flavor(
    platform: PlatformConfig,
    reference: str,
    *,
    require_one_vcpu: bool = False,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> Flavor:
    """Observe a flavor, optionally enforcing M1's worker 1-vCPU policy."""
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    shown = _json_command(
        (
            "flavor",
            "show",
            reference,
            "--column",
            "id",
            "--column",
            "name",
            "--column",
            "vcpus",
            "--column",
            "ram",
        ),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(shown, Mapping):
        raise OpenStackError("OpenStack flavor projection was not an object")
    flavor_id = _provider_uuid(_field(shown, "id"), field="flavor UUID")
    name = _field(shown, "name")
    vcpus = _field(shown, "vcpus")
    ram = _field(shown, "ram")
    if not isinstance(name, str) or not isinstance(vcpus, int) or not isinstance(ram, int):
        raise OpenStackError("OpenStack flavor projection was malformed")
    if require_one_vcpu and vcpus != 1:
        raise OpenStackError("configured worker flavor must have exactly one vCPU")
    return Flavor(flavor_id, name, vcpus, ram)


def _resource_id(value: Any, *, field: str) -> str | None:
    if isinstance(value, Mapping):
        value = _field(value, "id")
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, str) and " (" in value:
        value = value.rsplit(" (", 1)[-1].rstrip(")")
    return _provider_uuid(value, field=field)


def _addresses(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        flattened = [
            str(item)
            for entries in value.values()
            for item in (entries if isinstance(entries, list) else [entries])
        ]
    elif isinstance(value, list):
        flattened = [str(item) for item in value]
    elif isinstance(value, str):
        flattened = [part.strip() for part in value.split(",") if part.strip()]
    else:
        flattened = []
    return tuple(sorted(flattened))


def _server_rows(
    name: str, *, timeout_seconds: float, command_runner: Runner, executable: str
) -> list[Mapping[str, Any]]:
    rows = _json_command(
        (
            "server",
            "list",
            "--name",
            name,
            "--column",
            "ID",
            "--column",
            "Name",
            "--column",
            "Status",
        ),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise OpenStackError("OpenStack server list projection was malformed")
    return [row for row in rows if _field(row, "name") == name]


def _show_host(
    role: str,
    name: str,
    server_id: str,
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> PersistentHost:
    shown = _json_command(
        (
            "server",
            "show",
            server_id,
            "--column",
            "id",
            "--column",
            "name",
            "--column",
            "status",
            "--column",
            "image",
            "--column",
            "flavor",
            "--column",
            "addresses",
        ),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(shown, Mapping):
        raise OpenStackError("OpenStack server projection was not an object")
    observed_id = _provider_uuid(_field(shown, "id"), field="server UUID")
    observed_name = _field(shown, "name")
    if observed_id != server_id or observed_name != name:
        raise OpenStackError("OpenStack server identity changed during observation")
    flavor_value = _field(shown, "flavor")
    flavor_id = _resource_id(flavor_value, field="flavor UUID")
    flavor_name = (
        _field(flavor_value, "original_name", "name") if isinstance(flavor_value, Mapping) else None
    )
    return PersistentHost(
        role=role,
        configured_name=name,
        server_id=server_id,
        status=str(_field(shown, "status", default="")).upper(),
        image_id=_resource_id(_field(shown, "image"), field="server image UUID"),
        flavor_id=flavor_id,
        flavor_name=flavor_name if isinstance(flavor_name, str) else None,
        addresses=_addresses(_field(shown, "addresses")),
    )


def _resolve_host_verified(
    platform: PlatformConfig,
    role: str,
    *,
    allow_missing: bool,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> PersistentHost:
    role = _role(role, persistent=True)
    name = _inventory_text(platform, f"hosts.{role}")
    rows = _server_rows(
        name, timeout_seconds=timeout_seconds, command_runner=command_runner, executable=executable
    )
    if not rows and allow_missing:
        return PersistentHost(role, name, None, None, None, None, None, ())
    if len(rows) != 1:
        raise OpenStackError(f"configured {role} host must resolve to exactly one server")
    server_id = _provider_uuid(_field(rows[0], "id"), field="server UUID")
    return _show_host(
        role,
        name,
        server_id,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )


@_command_deadline("timeout_seconds")
def list_persistent_hosts(
    platform: PlatformConfig,
    *,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> list[PersistentHost]:
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    return [
        _resolve_host_verified(
            platform,
            role,
            allow_missing=True,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        for role in PERSISTENT_ROLES
    ]


def _wait_host_status(
    host: PersistentHost,
    wanted: str,
    *,
    deadline: float,
    poll_interval_seconds: float,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
    sleep: Callable[[float], None],
) -> PersistentHost:
    assert host.server_id is not None
    while True:
        remaining = _deadline_remaining(deadline, operation=f"{host.role} server status wait")
        observed = _show_host(
            host.role,
            host.configured_name,
            host.server_id,
            timeout_seconds=min(timeout_seconds, remaining),
            command_runner=command_runner,
            executable=executable,
        )
        if observed.status == wanted:
            return observed
        if observed.status == "ERROR":
            raise OpenStackError(f"{host.role} server entered ERROR status")
        remaining = _deadline_remaining(deadline, operation=f"{host.role} server status wait")
        sleep(min(poll_interval_seconds, remaining))


def _console_marker_counts(
    platform: PlatformConfig,
    host: PersistentHost,
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> tuple[int, int]:
    assert host.server_id is not None
    result = _run(
        ("console", "log", "show", "--lines", "2000", host.server_id),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    text = result.stdout.decode("utf-8", errors="replace")
    ready = f"{platform.namespace} NixOS {host.role} services ready"
    failed = f"{platform.namespace} NixOS {host.role} readiness failed"
    return text.count(ready), text.count(failed)


def _required_role_health_check(
    platform: PlatformConfig,
    health_check: HealthCheck | None,
    *,
    provider_runner: Runner,
    executable: str,
) -> HealthCheck:
    if health_check is not None:
        return health_check

    def concrete(role: str, host: PersistentHost, remaining: float) -> None:
        check_role_health(
            platform,
            role,
            host,
            remaining,
            provider_runner=provider_runner,
            executable=executable,
        )

    return concrete


def _power_refs(
    host: PersistentHost, action: str, baseline_ready: int, baseline_failed: int
) -> dict[str, Any]:
    assert host.server_id is not None
    return {
        "role": host.role,
        "action": action,
        "server_id": host.server_id,
        "baseline_ready_markers": baseline_ready,
        "baseline_failure_markers": baseline_failed,
    }


def _finish_power_observation(
    platform: PlatformConfig,
    host: PersistentHost,
    action: str,
    baseline_ready: int,
    baseline_failed: int,
    *,
    health_check: HealthCheck | None,
    deadline: float,
    poll_interval_seconds: float,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
    sleep: Callable[[float], None],
) -> PowerResult:
    assert host.server_id is not None
    wanted = "SHUTOFF" if action == "stop" else "ACTIVE"
    observed = _wait_host_status(
        host,
        wanted,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
        sleep=sleep,
    )
    checks = [f"nova:{wanted.lower()}", "resources:exact"]
    if action != "stop":
        _wait_console_ready(
            platform,
            observed,
            minimum_ready_markers=baseline_ready + 1,
            minimum_failure_markers=baseline_failed + 1,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
            sleep=sleep,
        )
        exact = _observe_host_resources_verified(
            platform,
            host.role,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        if exact.host.server_id != host.server_id:
            raise RecoveryRequired(
                "configured host changed while observing its power action",
                refs={"role": host.role, "server_id": host.server_id, "action": action},
            )
        checks.append(f"guest:{host.role}-services-ready")
        remaining = _deadline_remaining(deadline, operation=f"{host.role} role health checks")
        required_health_check = _required_role_health_check(
            platform,
            health_check,
            provider_runner=command_runner,
            executable=executable,
        )
        required_health_check(host.role, exact.host, remaining)
        checks.append("role:failed-units-and-authenticated-endpoints-healthy")
    return PowerResult(host.role, host.server_id, action, wanted, tuple(checks))


@_command_deadline("wait_seconds")
def power_host(
    platform: PlatformConfig,
    role: str,
    action: str,
    *,
    checkpoint: Checkpoint | None = None,
    health_check: HealthCheck | None = None,
    wait_seconds: float = 600,
    poll_interval_seconds: float = 2,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
    sleep: Callable[[float], None] = time.sleep,
) -> PowerResult:
    """Start, stop, or reboot one configured singleton, acting only by UUID.

    A durable pre-action checkpoint records the exact server and console-marker
    baseline. Recovery can therefore observe the requested status and fresh
    readiness without ever replaying an ambiguous reboot.
    """
    role = _role(role, persistent=True)
    if action not in ("start", "stop", "reboot"):
        raise ValidationError("power action must be start, stop, or reboot")
    deadline = _active_deadline(operation="power action")
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    resources = _observe_host_resources_verified(
        platform,
        role,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    host = resources.host
    assert host.server_id is not None
    baseline_ready, baseline_failed = (
        _console_marker_counts(
            platform,
            host,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        if action != "stop"
        else (0, 0)
    )
    refs = _power_refs(host, action, baseline_ready, baseline_failed)
    save = checkpoint or (lambda _phase, _refs: None)
    save("powering", refs)
    _run(
        ("server", action, host.server_id),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    save("power_requested", refs)
    return _finish_power_observation(
        platform,
        host,
        action,
        baseline_ready,
        baseline_failed,
        health_check=health_check,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
        sleep=sleep,
    )


@_command_deadline("wait_seconds")
def recover_power_host(
    platform: PlatformConfig,
    role: str,
    action: str,
    *,
    refs: Mapping[str, Any],
    health_check: HealthCheck | None = None,
    wait_seconds: float = 600,
    poll_interval_seconds: float = 2,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
    sleep: Callable[[float], None] = time.sleep,
) -> PowerResult:
    """Observe an interrupted power action without issuing another mutation."""
    role = _role(role, persistent=True)
    if action not in ("start", "stop", "reboot"):
        raise ValidationError("power action must be start, stop, or reboot")
    if refs.get("role") != role or refs.get("action") != action:
        raise ValidationError("power recovery role or action does not match its checkpoint")
    deadline = _active_deadline(operation="power recovery")
    server_id = uuid(refs.get("server_id"), field="power recovery server UUID")
    baseline_ready = refs.get("baseline_ready_markers")
    baseline_failed = refs.get("baseline_failure_markers")
    if (
        isinstance(baseline_ready, bool)
        or not isinstance(baseline_ready, int)
        or baseline_ready < 0
        or isinstance(baseline_failed, bool)
        or not isinstance(baseline_failed, int)
        or baseline_failed < 0
    ):
        raise ValidationError("power recovery console-marker baseline is malformed")
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    resources = _observe_host_resources_verified(
        platform,
        role,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if resources.host.server_id != server_id:
        raise RecoveryRequired(
            "configured host no longer matches the interrupted power action",
            refs={"role": role, "server_id": server_id, "action": action},
        )
    # Deliberately no ``server start|stop|reboot`` call appears on this path.
    return _finish_power_observation(
        platform,
        resources.host,
        action,
        baseline_ready,
        baseline_failed,
        health_check=health_check,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
        sleep=sleep,
    )


@_command_deadline("timeout_seconds")
def host_logs(
    platform: PlatformConfig,
    role: str,
    *,
    lines: int = 200,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> str:
    """Return one bounded Nova serial tail for a configured persistent host."""
    role = _role(role, persistent=True)
    if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 2_000:
        raise ValidationError("serial log line count must be from 1 through 2000")
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    host = _resolve_host_verified(
        platform,
        role,
        allow_missing=False,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    assert host.server_id is not None
    result = _run(
        ("console", "log", "show", "--lines", str(lines), host.server_id),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    output: bytes = result.stdout
    return output.decode("utf-8", errors="replace")


def _configured_volume_names(platform: PlatformConfig, role: str) -> tuple[str, ...]:
    if role == "admin":
        return (platform.get("volumes.adminState.name"), platform.get("volumes.backup.name"))
    if role == "storage":
        return (platform.get("volumes.data.name"),)
    return ()


def _find_exact(
    kind: str, name: str, *, timeout_seconds: float, command_runner: Runner, executable: str
) -> str:
    rows = _json_command(
        (kind, "list", "--name", name, "--column", "ID", "--column", "Name"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(rows, list):
        raise OpenStackError(f"OpenStack {kind} list was not an array")
    matches = [row for row in rows if isinstance(row, Mapping) and _field(row, "name") == name]
    if len(matches) != 1:
        raise OpenStackError(f"configured {kind} {name!r} must resolve exactly once")
    return _provider_uuid(_field(matches[0], "id"), field=f"{kind} UUID")


def _observe_host_resources_verified(
    platform: PlatformConfig,
    role: str,
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> HostResources:
    host = _resolve_host_verified(
        platform,
        role,
        allow_missing=False,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    assert host.server_id is not None
    port_name = _inventory_text(platform, f"ports.{role}")
    port_id = _find_exact(
        "port",
        port_name,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    port = _json_command(
        (
            "port",
            "show",
            port_id,
            "--column",
            "id",
            "--column",
            "name",
            "--column",
            "device_id",
            "--column",
            "fixed_ips",
        ),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(port, Mapping):
        raise OpenStackError(f"configured {role} fixed port is not attached to its server UUID")
    observed_port_id = _provider_uuid(_field(port, "id"), field="port UUID")
    observed_device_id = _provider_uuid(_field(port, "device_id"), field="server UUID")
    if (
        observed_port_id != port_id
        or _field(port, "name") != port_name
        or observed_device_id != host.server_id
    ):
        raise OpenStackError(f"configured {role} fixed port is not attached to its server UUID")
    fixed_ips = _field(port, "fixed_ips", default=[])
    configured_address = _inventory_text(platform, f"addresses.{role}")
    observed_ips = (
        {item.get("ip_address") for item in fixed_ips if isinstance(item, Mapping)}
        if isinstance(fixed_ips, list)
        else set()
    )
    if configured_address not in observed_ips:
        raise OpenStackError(f"configured {role} port does not own its fixed address")

    server_ports = _json_command(
        ("port", "list", "--server", host.server_id, "--column", "ID"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(server_ports, list) or any(
        not isinstance(row, Mapping) for row in server_ports
    ):
        raise OpenStackError("OpenStack server port projection was malformed")
    attached_port_ids = {
        _provider_uuid(_field(row, "id"), field="port UUID") for row in server_ports
    }
    if attached_port_ids != {port_id} or len(server_ports) != 1:
        raise OpenStackError(f"configured {role} host must have exactly its one fixed port")

    attachment_flags = _json_command(
        ("server", "show", host.server_id, "--column", "volumes_attached"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    raw_attachment_flags = _field(attachment_flags, "volumes_attached")
    if not isinstance(raw_attachment_flags, list) or any(
        not isinstance(row, Mapping) for row in raw_attachment_flags
    ):
        raise OpenStackError("OpenStack volume deletion projection was malformed")
    delete_by_id: dict[str, Any] = {}
    for row in raw_attachment_flags:
        volume_id = _provider_uuid(_field(row, "id"), field="volume UUID")
        if volume_id in delete_by_id:
            raise OpenStackError("OpenStack returned a duplicate volume deletion projection")
        delete_by_id[volume_id] = _field(row, "delete_on_termination")

    attachment_rows = _json_command(
        (
            "server",
            "volume",
            "list",
            host.server_id,
            "--column",
            "ID",
            "--column",
            "Device",
        ),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(attachment_rows, list) or any(
        not isinstance(row, Mapping) for row in attachment_rows
    ):
        raise OpenStackError("OpenStack volume attachment projection was malformed")
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in attachment_rows:
        volume_id = _provider_uuid(_field(row, "id"), field="volume UUID")
        if volume_id in by_id:
            raise OpenStackError("OpenStack returned a duplicate volume attachment")
        by_id[volume_id] = row
    volumes: list[VolumeAttachment] = []
    for volume_name in _configured_volume_names(platform, role):
        if not isinstance(volume_name, str):
            raise ValidationError(f"configured {role} volume name is malformed")
        volume_id = _find_exact(
            "volume",
            volume_name,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        row = by_id.get(volume_id)
        if row is None:
            raise OpenStackError(f"configured {role} volume is not attached to its server UUID")
        delete_value = delete_by_id.get(volume_id)
        if delete_value is False or (
            isinstance(delete_value, str) and delete_value.lower() == "false"
        ):
            delete_flag = False
        elif delete_value is True or (
            isinstance(delete_value, str) and delete_value.lower() == "true"
        ):
            raise OpenStackError(
                "persistent volume has delete_on_termination enabled; replacement refused"
            )
        else:
            raise OpenStackError(
                "persistent volume delete_on_termination state is missing or ambiguous"
            )
        device = _field(row, "device")
        if not isinstance(device, str) or not device.startswith("/dev/"):
            raise OpenStackError("persistent volume attachment device is malformed")
        volumes.append(VolumeAttachment(volume_id, volume_name, device, delete_flag))
    expected_volume_ids = {volume.volume_id for volume in volumes}
    if (
        set(by_id) != expected_volume_ids
        or set(delete_by_id) != expected_volume_ids
        or len(attachment_rows) != len(expected_volume_ids)
    ):
        raise OpenStackError(f"configured {role} host has unexpected volume attachments")
    if len({volume.device for volume in volumes}) != len(volumes):
        raise OpenStackError("persistent volume attachment devices are not unique")
    return HostResources(host, port_id, port_name, tuple(volumes))


@_command_deadline("timeout_seconds")
def observe_host_resources(
    platform: PlatformConfig,
    role: str,
    *,
    timeout_seconds: float = 30,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> HostResources:
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    return _observe_host_resources_verified(
        platform,
        _role(role, persistent=True),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )


def _wait_console_ready(
    platform: PlatformConfig,
    host: PersistentHost,
    *,
    minimum_ready_markers: int = 1,
    minimum_failure_markers: int = 1,
    deadline: float,
    poll_interval_seconds: float,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
    sleep: Callable[[float], None],
) -> None:
    assert host.server_id is not None
    ready = f"{platform.namespace} NixOS {host.role} services ready"
    failed = f"{platform.namespace} NixOS {host.role} readiness failed"
    while True:
        remaining = _deadline_remaining(deadline, operation=f"{host.role} console readiness")
        result = _run(
            ("console", "log", "show", "--lines", "400", host.server_id),
            timeout_seconds=min(timeout_seconds, remaining),
            command_runner=command_runner,
            executable=executable,
        )
        text = result.stdout.decode("utf-8", errors="replace")
        ready_count = text.count(ready)
        failed_count = text.count(failed)
        latest_ready = text.rfind(ready)
        latest_failed = text.rfind(failed)
        if ready_count >= minimum_ready_markers and latest_ready > latest_failed:
            return
        if failed_count >= minimum_failure_markers and latest_failed > latest_ready:
            raise OpenStackError(f"{host.role} guest reported failed service readiness")
        remaining = _deadline_remaining(deadline, operation=f"{host.role} console readiness")
        sleep(min(poll_interval_seconds, remaining))


def _pin_admin_host_key(
    platform: PlatformConfig,
    host: PersistentHost,
    *,
    deadline: float,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
    host_key_runner: Runner,
) -> None:
    if host.role != "admin":
        return
    assert host.server_id is not None
    remaining = _deadline_remaining(deadline, operation="admin host-key verification")
    console = _run(
        ("console", "log", "show", "--lines", "400", host.server_id),
        timeout_seconds=min(timeout_seconds, remaining),
        command_runner=command_runner,
        executable=executable,
    )
    try:
        host_keys.pin_verified_admin_host_key(
            _inventory_text(platform, "addresses.admin"),
            console.stdout,
            timeout_seconds=min(
                15.0,
                _deadline_remaining(deadline, operation="admin host-key verification"),
            ),
            command_runner=host_key_runner,
        )
    except Exception as error:
        raise OpenStackError("admin host-key verification and pinning failed") from error


@_command_deadline("remaining_seconds")
def check_role_health(
    platform: PlatformConfig,
    role: str,
    host: PersistentHost,
    remaining_seconds: float,
    *,
    provider_runner: Runner = runtime.run,
    service_runner: Runner = runtime.run,
    http_get: Callable[..., runtime.HttpResult] = runtime.bounded_http,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> None:
    """Run the bounded concrete health contract for one persistent role.

    The authenticated OpenStack console proves the latest guest readiness run
    did not report failed units. Admin and storage checks run on the pinned
    admin control host, where their credentials and deployed paths exist.
    Ingress is checked from this external management process against both its
    fixed address and public TLS origin. No response body or command output
    escapes this boundary.
    """
    role = _role(role, persistent=True)
    if host.role != role or host.server_id is None or remaining_seconds <= 0:
        raise ValidationError("role health check identity or deadline is malformed")
    per_check = min(30.0, remaining_seconds / 3)
    result = _run(
        ("console", "log", "show", "--lines", "400", host.server_id),
        timeout_seconds=per_check,
        command_runner=provider_runner,
        executable=executable,
    )
    text = result.stdout.decode("utf-8", errors="replace")
    ready = f"{platform.namespace} NixOS {role} services ready"
    failed = f"{platform.namespace} NixOS {role} readiness failed"
    if text.rfind(ready) < 0 or text.rfind(failed) > text.rfind(ready):
        raise OpenStackError(f"{role} guest failed-unit readiness is not current")

    root = Path(_inventory_text(platform, "paths.root"))
    command: tuple[str, ...]
    if role == "admin":
        command = (
            str(root / "bin" / f"{platform.namespace}-nomad"),
            "operator",
            "raft",
            "list-peers",
        )
    elif role == "storage":
        command = (
            "/run/current-system/sw/bin/python",
            str(root / "infra" / "monitor" / "check_services.py"),
        )
    else:
        command = ()
    if command:
        # ``paths.root`` names the admin guest's deployment root. It must never
        # be interpreted as an executable path on the external management host.
        # The storage checker imports its config at module load, so run it in a
        # tiny fixed Python wrapper that sets the namespace-specific path before
        # loading the reviewed script. Keeping the assignment in argv also
        # makes the propagated path visible in bounded command evidence.
        if command[-1].endswith("check_services.py"):
            script = command[-1]
            wrapper = (
                "import os,runpy,sys; "
                "os.environ['PLATFORM_CONFIG']=sys.argv[1].split('=',1)[1]; "
                "runpy.run_path(sys.argv[2], run_name='__main__')"
            )
            configured = f"PLATFORM_CONFIG=/etc/{platform.namespace}/platform.json"
            remote_command = (
                command[0],
                "-c",
                wrapper,
                configured,
                script,
            )
            ssh_command = remote.pinned_admin_command(remote_command)
        else:
            ssh_command = remote.pinned_admin_command(
                command,
                environment={
                    "PLATFORM_CONFIG": f"/etc/{platform.namespace}/platform.json",
                },
            )
        try:
            checked = service_runner(
                ssh_command,
                timeout_seconds=per_check,
                stdin=None,
                stdout_limit=65_536,
                stderr_limit=65_536,
                inherit_env=("HOME", "USER", "SSH_AUTH_SOCK"),
                check=True,
            )
        except Exception as error:
            raise OpenStackError(f"{role} authenticated internal health check failed") from error
        if checked.stdout_truncated or checked.stderr_truncated:
            raise OpenStackError(f"{role} authenticated health output exceeded its limit")
    if role == "ingress":
        # These probes intentionally originate outside the OpenStack guests:
        # the TLS check must exercise the same public route as a client.
        urls = (
            f"http://{_inventory_text(platform, 'addresses.ingress')}/healthz",
            f"https://{platform.domain}/healthz",
        )
        for url in urls:
            try:
                response = http_get(
                    url,
                    timeout_seconds=per_check,
                    response_limit=64,
                    allow_redirects=False,
                )
            except Exception as error:
                raise OpenStackError("ingress internal/public health check failed") from error
            if not 200 <= response.status < 300 or response.body.strip() != b"OK":
                raise OpenStackError("ingress internal/public health response was unhealthy")


def _check_port_device(
    port_id: str, expected: str, *, timeout_seconds: float, command_runner: Runner, executable: str
) -> bool:
    shown = _json_command(
        ("port", "show", port_id, "--column", "device_id"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(shown, Mapping):
        return False
    value = _field(shown, "device_id") or ""
    observed = "" if value == "" else _provider_uuid(value, field="server UUID")
    return observed == expected


def _volume_attached(
    server_id: str,
    volume_id: str,
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> bool:
    rows = _json_command(
        ("server", "volume", "list", server_id, "--column", "ID"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    return isinstance(rows, list) and any(
        isinstance(row, Mapping)
        and _provider_uuid(_field(row, "id"), field="volume UUID") == volume_id
        for row in rows
    )


def _mutate_and_verify(
    arguments: Sequence[str],
    verified: Callable[[], bool],
    *,
    message: str,
    refs: Mapping[str, Any],
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> None:
    try:
        _run(
            arguments,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
    except OpenStackError:
        if verified():
            return
        raise RecoveryRequired(message, refs=refs) from None
    if not verified():
        raise RecoveryRequired(message, refs=refs)


def _rename_server(
    server_id: str,
    name: str,
    *,
    refs: Mapping[str, Any],
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> None:
    def renamed_exactly() -> bool:
        rows = _server_rows(
            name,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        return (
            len(rows) == 1
            and _provider_uuid(_field(rows[0], "id"), field="server UUID") == server_id
        )

    _mutate_and_verify(
        ("server", "set", "--name", name, server_id),
        renamed_exactly,
        message="server rename result is ambiguous",
        refs={**refs, "server_id": server_id, "expected_name": name},
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )


def _rollback_replacement(
    platform: PlatformConfig,
    resources: HostResources,
    replacement_id: str | None,
    old_name: str,
    selected_image_id: str,
    *,
    recovery_refs: Mapping[str, Any],
    health_check: HealthCheck | None,
    deadline: float,
    poll_interval_seconds: float,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
    host_key_runner: Runner,
    sleep: Callable[[float], None],
    checkpoint: Checkpoint,
) -> ReplacementResult:
    old_id = resources.host.server_id
    assert old_id is not None
    full_refs = {**resources.operation_refs(), **recovery_refs}
    if replacement_id is not None:
        _mutate_and_verify(
            ("server", "delete", replacement_id),
            lambda: (
                not _resource_exists(
                    "server",
                    replacement_id,
                    timeout_seconds=timeout_seconds,
                    command_runner=command_runner,
                    executable=executable,
                )
            ),
            message="failed replacement deletion is ambiguous; old server remains retained",
            refs={**full_refs, "replacement_server_id": replacement_id},
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        checkpoint("replacement_deleted", {**full_refs, "replacement_server_id": replacement_id})
    _mutate_and_verify(
        ("server", "add", "port", old_id, resources.port_id),
        lambda: _check_port_device(
            resources.port_id,
            old_id,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        ),
        message="fixed port rollback attachment is ambiguous",
        refs={**full_refs, "replacement_server_id": replacement_id},
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    for volume in resources.volumes:
        _mutate_and_verify(
            ("server", "add", "volume", "--device", volume.device, old_id, volume.volume_id),
            lambda volume_id=volume.volume_id: _volume_attached(  # type: ignore[misc]
                old_id,
                volume_id,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
            ),
            message="persistent volume rollback attachment is ambiguous",
            refs={
                **full_refs,
                "replacement_server_id": replacement_id,
                "volume_id": volume.volume_id,
            },
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
    _rename_server(
        old_id,
        old_name,
        refs={**full_refs, "replacement_server_id": replacement_id},
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    restored = _observe_host_resources_verified(
        platform,
        resources.host.role,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if (
        restored.host.server_id != old_id
        or restored.port_id != resources.port_id
        or restored.volumes != resources.volumes
    ):
        raise RecoveryRequired(
            "rollback resource preservation could not be verified",
            refs={**full_refs, "replacement_server_id": replacement_id},
        )
    if resources.host.status == "ACTIVE":
        baseline_ready, baseline_failed = _console_marker_counts(
            platform,
            restored.host,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        try:
            _run(
                ("server", "start", old_id),
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
            )
            old_host = _wait_host_status(
                PersistentHost(
                    resources.host.role,
                    old_name,
                    old_id,
                    None,
                    resources.host.image_id,
                    resources.host.flavor_id,
                    resources.host.flavor_name,
                    (),
                ),
                "ACTIVE",
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
                sleep=sleep,
            )
            remaining = _deadline_remaining(deadline, operation="rollback health")
            _wait_console_ready(
                platform,
                old_host,
                minimum_ready_markers=baseline_ready + 1,
                minimum_failure_markers=baseline_failed + 1,
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
                sleep=sleep,
            )
            _pin_admin_host_key(
                platform,
                old_host,
                deadline=deadline,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
                host_key_runner=host_key_runner,
            )
            required_health_check = _required_role_health_check(
                platform,
                health_check,
                provider_runner=command_runner,
                executable=executable,
            )
            required_health_check(resources.host.role, old_host, remaining)
        except Exception as error:
            raise RecoveryRequired(
                "rollback restored resources but prior-role health was not verified",
                refs={**full_refs, "replacement_server_id": replacement_id},
            ) from error
    checkpoint("rolled_back", full_refs)
    return ReplacementResult(
        resources.host.role,
        False,
        old_id,
        selected_image_id,
        old_id,
        "confirmed",
        ("resources:restored", f"guest:{resources.host.role}-services-ready"),
    )


def _validate_user_data_source(source: str | Path, *, maximum_bytes: int) -> None:
    path = Path(source)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError("replacement user-data must be a readable protected file") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size < 1
            or metadata.st_size > maximum_bytes
        ):
            raise ValidationError(
                "replacement user-data must be a non-empty, owner-only regular file within limit"
            )
    finally:
        os.close(descriptor)


@contextmanager
def _protected_user_data_copy(source: str | Path, *, maximum_bytes: int) -> Iterator[str]:
    path = Path(source)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError("replacement user-data must be a readable protected file") from error
    temporary_name: str | None = None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size < 1
            or metadata.st_size > maximum_bytes
        ):
            raise ValidationError(
                "replacement user-data must be a non-empty, owner-only regular file within limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as input_stream:
            with tempfile.NamedTemporaryFile(
                prefix="platform-user-data-", mode="w+b", delete=False
            ) as temporary:
                temporary_name = temporary.name
                os.chmod(temporary.name, 0o600)
                shutil.copyfileobj(input_stream, temporary, length=65_536)
                if temporary.tell() != metadata.st_size:
                    raise ValidationError("replacement user-data changed while it was being copied")
                temporary.flush()
                os.fsync(temporary.fileno())
        assert temporary_name is not None
        yield temporary_name
    finally:
        os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _replace_host(
    platform: PlatformConfig,
    role: str,
    *,
    selected_image_id: str,
    selected_compatibility_hash: str,
    operation_id: str,
    user_data_path: str | Path,
    checkpoint: Checkpoint,
    expected_resources: HostResources | None = None,
    health_check: HealthCheck | None = None,
    wait_seconds: float = 900,
    poll_interval_seconds: float = 2,
    timeout_seconds: float = 60,
    user_data_limit: int = 1_048_576,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
    host_key_runner: Runner = runtime.run,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplacementResult:
    """Replace one persistent singleton while retaining the old server.

    The caller checkpoints operation refs through ``checkpoint`` and supplies a
    mandatory bounded role health check. The old server is deleted only after console
    readiness and that health check pass.  Ordinary readiness failure rolls
    back to the retained old server; ambiguous mutations raise
    :class:`RecoveryRequired` instead of guessing.
    """
    deadline = _active_deadline(operation="host replacement")
    role = _role(role, persistent=True)
    selected_image_id = uuid(selected_image_id, field="selected image UUID")
    selected_compatibility_hash = sha256_hex(
        selected_compatibility_hash, field="selected compatibility hash"
    )
    operation_id = uuid(operation_id, field="operation UUID")
    if isinstance(user_data_limit, bool) or not 1 <= user_data_limit <= 16_777_216:
        raise ValidationError("replacement user-data limit is malformed")
    _validate_user_data_source(user_data_path, maximum_bytes=user_data_limit)
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    images = _list_images_verified(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    matching = [image for image in images if image.image_id == selected_image_id]
    if len(matching) != 1 or not _complete_image(platform, matching[0], role):
        raise DriftError("selected image UUID is no longer an active compatible role image")
    if matching[0].compatibility_hash != selected_compatibility_hash:
        raise DriftError("selected image compatibility hash drifted")

    resources = _observe_host_resources_verified(
        platform,
        role,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if expected_resources is not None and resources != expected_resources:
        raise DriftError("persistent host resources drifted while user-data was rendered")
    old = resources.host
    assert old.server_id is not None and old.flavor_id is not None and old.image_id is not None
    old_operation_name = f"{old.configured_name}--old-{operation_id[:8]}"
    base_refs = {
        **resources.operation_refs(),
        "selected_image_id": selected_image_id,
        "operation_id": operation_id,
        "old_operation_name": old_operation_name,
    }
    checkpoint("observed", base_refs)
    if old.status == "ACTIVE":
        old_server_id = old.server_id
        assert old_server_id is not None
        try:
            _mutate_and_verify(
                ("server", "stop", old_server_id),
                lambda: (
                    _show_host(
                        role,
                        old.configured_name,
                        old_server_id,
                        timeout_seconds=timeout_seconds,
                        command_runner=command_runner,
                        executable=executable,
                    ).status
                    == "SHUTOFF"
                ),
                message="old-server stop result is ambiguous",
                refs=base_refs,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
            )
            _wait_host_status(
                old,
                "SHUTOFF",
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
                sleep=sleep,
            )
        except RecoveryRequired:
            raise
        except OpenStackError as error:
            raise RecoveryRequired(
                "old-server stop completion could not be verified", refs=base_refs
            ) from error
    elif old.status != "SHUTOFF":
        raise OpenStackError("persistent host must be ACTIVE or SHUTOFF before replacement")
    checkpoint("old_stopped", base_refs)
    _rename_server(
        old.server_id,
        old_operation_name,
        refs=base_refs,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    recovery_refs = base_refs
    checkpoint("old_renamed", recovery_refs)

    _mutate_and_verify(
        ("server", "remove", "port", old.server_id, resources.port_id),
        lambda: _check_port_device(
            resources.port_id,
            "",
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        ),
        message="fixed port detach result is ambiguous; old server remains retained",
        refs=recovery_refs,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    for volume in resources.volumes:
        old_server_id = old.server_id
        volume_id = volume.volume_id
        assert old_server_id is not None

        def volume_detached(
            server_id: str = old_server_id,
            target_volume_id: str = volume_id,
        ) -> bool:
            return not _volume_attached(
                server_id,
                target_volume_id,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
            )

        _mutate_and_verify(
            ("server", "remove", "volume", old_server_id, volume_id),
            volume_detached,
            message="persistent volume detach result is ambiguous; old server remains retained",
            refs={**recovery_refs, "volume_id": volume.volume_id},
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
    checkpoint("resources_detached", recovery_refs)

    assert matching[0].source_commit is not None
    properties = publisher_metadata(platform, role, matching[0].source_commit)
    create = [
        "server",
        "create",
        "--image",
        selected_image_id,
        "--flavor",
        old.flavor_id,
        "--port",
        resources.port_id,
        "--key-name",
        f"{platform.prefix}-admin",
        "--config-drive",
        "true",
        "--format",
        "json",
        "--column",
        "id",
    ]
    for key, value in sorted(properties.items()):
        create.extend(("--property", f"{key}={value}"))
    create.extend(("--property", f"{_metadata_prefix(platform)}_operation_id={operation_id}"))
    for volume in resources.volumes:
        create.extend(
            (
                "--block-device",
                f"uuid={volume.volume_id},source_type=volume,destination_type=volume,device_type=disk,device_name={volume.device},boot_index=-1,delete_on_termination=false",
            )
        )

    replacement_id: str | None = None
    with _protected_user_data_copy(user_data_path, maximum_bytes=user_data_limit) as temporary_name:
        create.extend(("--user-data", temporary_name, old.configured_name))
        try:
            result = _run(
                create,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
            )
            document = json.loads(result.stdout)
            if not isinstance(document, Mapping):
                raise ValueError
            replacement_id = _provider_uuid(_field(document, "id"), field="replacement server UUID")
        except (OpenStackError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise RecoveryRequired(
                "replacement create result is ambiguous; inspect by operation UUID before recovery",
                refs=recovery_refs,
            ) from error
    checkpoint(
        "replacement_created",
        {**recovery_refs, "replacement_server_id": replacement_id},
    )
    assert replacement_id is not None
    replacement = PersistentHost(
        role,
        old.configured_name,
        replacement_id,
        None,
        selected_image_id,
        old.flavor_id,
        old.flavor_name,
        (),
    )
    try:
        # The create response is only a candidate UUID.  Before readiness can
        # become acceptance evidence, re-read the provider object and require
        # the exact selected image, retained flavor, operation provenance, and
        # configured name.  A healthy server with a different image/flavor is
        # never an acceptable replacement.
        _verify_replacement_identity(
            platform,
            role,
            replacement_id,
            operation_id,
            selected_image_id,
            old.flavor_id,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        replacement = _wait_host_status(
            replacement,
            "ACTIVE",
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
            sleep=sleep,
        )
        replacement_resources = _observe_host_resources_verified(
            platform,
            role,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        if (
            replacement_resources.host.server_id != replacement_id
            or replacement_resources.host.image_id != selected_image_id
            or replacement_resources.host.flavor_id != old.flavor_id
            or replacement_resources.port_id != resources.port_id
            or replacement_resources.volumes != resources.volumes
        ):
            raise OpenStackError("replacement did not preserve the exact fixed resources")
        _wait_console_ready(
            platform,
            replacement,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
            sleep=sleep,
        )
        _pin_admin_host_key(
            platform,
            replacement,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
            host_key_runner=host_key_runner,
        )
        remaining = _deadline_remaining(deadline, operation="replacement role health checks")
        required_health_check = _required_role_health_check(
            platform,
            health_check,
            provider_runner=command_runner,
            executable=executable,
        )
        required_health_check(role, replacement, remaining)
    except Exception:
        return _rollback_replacement(
            platform,
            resources,
            replacement_id,
            old.configured_name,
            selected_image_id,
            recovery_refs=recovery_refs,
            health_check=health_check,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
            host_key_runner=host_key_runner,
            sleep=sleep,
            checkpoint=checkpoint,
        )
    checkpoint(
        "accepted",
        {**recovery_refs, "replacement_server_id": replacement_id},
    )
    cleanup_state = "confirmed"
    old_server_id = old.server_id
    assert old_server_id is not None
    try:
        _mutate_and_verify(
            ("server", "delete", old_server_id),
            lambda: (
                not _resource_exists(
                    "server",
                    old_server_id,
                    timeout_seconds=timeout_seconds,
                    command_runner=command_runner,
                    executable=executable,
                )
            ),
            message="replacement is accepted but retained old-server deletion is ambiguous",
            refs={**recovery_refs, "replacement_server_id": replacement_id},
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
    except RecoveryRequired:
        cleanup_state = "old_server_retained"
    checkpoint(
        "complete",
        {
            **recovery_refs,
            "replacement_server_id": replacement_id,
            "cleanup_state": cleanup_state,
        },
    )
    checks = ["nova:active", "resources:exact", f"guest:{role}-services-ready"]
    checks.append("role:failed-units-and-authenticated-endpoints-healthy")
    return ReplacementResult(
        role,
        True,
        replacement_id,
        selected_image_id,
        old_server_id,
        cleanup_state,
        tuple(checks),
    )


@_command_deadline("wait_seconds")
def replace_host(
    platform: PlatformConfig,
    role: str,
    *,
    selected_image_id: str,
    selected_compatibility_hash: str,
    operation_id: str,
    user_data_path: str | Path | None = None,
    checkpoint: Checkpoint,
    health_check: HealthCheck | None = None,
    wait_seconds: float = 900,
    poll_interval_seconds: float = 2,
    timeout_seconds: float = 60,
    user_data_limit: int = 1_048_576,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
    host_key_runner: Runner = runtime.run,
    sleep: Callable[[float], None] = time.sleep,
) -> ReplacementResult:
    """Render or copy protected user-data, stage it, and replace one host.

    The default source is rendered from the reviewed role template, current
    platform inventory, retained resource UUIDs, and the established protected
    input-path environment variables. ``user_data_path`` remains an explicit
    protected-file override for controlled tooling and tests.
    """
    role = _role(role, persistent=True)
    required_health_check = _required_role_health_check(
        platform,
        health_check,
        provider_runner=command_runner,
        executable=executable,
    )
    if isinstance(user_data_limit, bool) or not 1 <= user_data_limit <= 16_777_216:
        raise ValidationError("replacement user-data limit is malformed")

    expected_resources: HostResources | None = None
    if user_data_path is None:
        from . import host_user_data

        # Render before any mutation. A second exact observation in
        # ``_replace_host`` catches resource drift between rendering and stop.
        expected_resources = observe_host_resources(
            platform,
            role,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        volume_ids = {volume.name: volume.volume_id for volume in expected_resources.volumes}
        source = host_user_data.staged_host_user_data(
            platform,
            role,
            volume_ids,
            maximum_bytes=user_data_limit,
        )
    else:
        # Stage before the first provider observation or mutation so a weak or
        # changing explicit source fails closed.
        source = _protected_user_data_copy(user_data_path, maximum_bytes=user_data_limit)

    with source as staged_user_data_path:
        return _replace_host(
            platform,
            role,
            selected_image_id=selected_image_id,
            selected_compatibility_hash=selected_compatibility_hash,
            operation_id=operation_id,
            user_data_path=staged_user_data_path,
            checkpoint=checkpoint,
            expected_resources=expected_resources,
            health_check=required_health_check,
            wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            user_data_limit=user_data_limit,
            command_runner=command_runner,
            executable=executable,
            host_key_runner=host_key_runner,
            sleep=sleep,
        )


def _recovery_text(refs: Mapping[str, Any], name: str) -> str:
    value = refs.get(name)
    if not isinstance(value, str) or not value or len(value.encode()) > 256 or "\x00" in value:
        raise ValidationError(f"replacement recovery {name} is malformed")
    return value


def _operation_replacement_id(
    platform: PlatformConfig,
    operation_id: str,
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> str | None:
    rows = _json_command(
        ("server", "list", "--column", "ID", "--column", "Name"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise OpenStackError("OpenStack replacement recovery inventory was malformed")
    if len(rows) > 500:
        raise OpenStackError("OpenStack server inventory exceeds its 500-server safety limit")
    key = f"{_metadata_prefix(platform)}_operation_id"
    matches: list[str] = []
    for row in rows:
        server_id = _provider_uuid(_field(row, "id"), field="server UUID")
        shown = _json_command(
            ("server", "show", server_id, "--column", "id", "--column", "properties"),
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        if (
            not isinstance(shown, Mapping)
            or _provider_uuid(_field(shown, "id"), field="server UUID") != server_id
        ):
            raise OpenStackError("OpenStack replacement recovery projection was malformed")
        if _properties(_field(shown, "properties", default={})).get(key) == operation_id:
            matches.append(server_id)
    if len(matches) > 1:
        raise RecoveryRequired(
            "multiple servers carry the replacement operation UUID; refusing recovery",
            refs={"operation_id": operation_id, "replacement_server_ids": sorted(matches)},
        )
    return matches[0] if matches else None


def _verify_replacement_identity(
    platform: PlatformConfig,
    role: str,
    replacement_id: str,
    operation_id: str,
    selected_image_id: str,
    old_flavor_id: str,
    *,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> None:
    shown = _json_command(
        (
            "server",
            "show",
            replacement_id,
            "--column",
            "id",
            "--column",
            "name",
            "--column",
            "image",
            "--column",
            "flavor",
            "--column",
            "properties",
        ),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    key = f"{_metadata_prefix(platform)}_operation_id"
    if (
        not isinstance(shown, Mapping)
        or _provider_uuid(_field(shown, "id"), field="server UUID") != replacement_id
        or _field(shown, "name") != _inventory_text(platform, f"hosts.{role}")
        or _resource_id(_field(shown, "image"), field="server image UUID") != selected_image_id
        or _resource_id(_field(shown, "flavor"), field="flavor UUID") != old_flavor_id
        or _properties(_field(shown, "properties", default={})).get(key) != operation_id
    ):
        raise RecoveryRequired(
            "replacement server identity or provenance could not be verified",
            refs={
                "role": role,
                "replacement_server_id": replacement_id,
                "operation_id": operation_id,
            },
        )


def _recovery_resources(
    platform: PlatformConfig,
    role: str,
    refs: Mapping[str, Any],
    *,
    allow_missing_old: bool,
    timeout_seconds: float,
    command_runner: Runner,
    executable: str,
) -> HostResources:
    old_id = uuid(refs.get("old_server_id"), field="old server UUID")
    old_image_id = uuid(refs.get("old_image_id"), field="old image UUID")
    old_flavor_id = uuid(refs.get("old_flavor_id"), field="old flavor UUID")
    old_status = refs.get("old_status")
    if old_status not in ("ACTIVE", "SHUTOFF"):
        raise ValidationError("replacement recovery old status is malformed")
    configured_name = _inventory_text(platform, f"hosts.{role}")
    old_operation_name = refs.get("old_operation_name")
    allowed_names = {configured_name}
    if old_operation_name is not None:
        allowed_names.add(_recovery_text(refs, "old_operation_name"))
    old_exists = _resource_exists(
        "server",
        old_id,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if old_exists:
        shown = _json_command(
            (
                "server",
                "show",
                old_id,
                "--column",
                "id",
                "--column",
                "name",
                "--column",
                "status",
                "--column",
                "image",
                "--column",
                "flavor",
            ),
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        if (
            not isinstance(shown, Mapping)
            or _provider_uuid(_field(shown, "id"), field="server UUID") != old_id
            or _field(shown, "name") not in allowed_names
            or _resource_id(_field(shown, "image"), field="server image UUID") != old_image_id
            or _resource_id(_field(shown, "flavor"), field="flavor UUID") != old_flavor_id
            or str(_field(shown, "status", default="")).upper() not in ("ACTIVE", "SHUTOFF")
        ):
            raise RecoveryRequired(
                "retained old-server identity could not be verified",
                refs={"role": role, "old_server_id": old_id},
            )
    elif not allow_missing_old:
        raise RecoveryRequired(
            "retained old server is absent before replacement acceptance",
            refs={"role": role, "old_server_id": old_id},
        )
    old = PersistentHost(
        role,
        configured_name,
        old_id,
        old_status,
        old_image_id,
        old_flavor_id,
        None,
        (),
    )
    port_name = _inventory_text(platform, f"ports.{role}")
    port_id = uuid(refs.get("port_id"), field="fixed port UUID")
    if (
        _find_exact(
            "port",
            port_name,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        != port_id
    ):
        raise RecoveryRequired(
            "configured fixed-port identity drifted during replacement",
            refs={"role": role, "old_server_id": old_id, "port_id": port_id},
        )
    attachments = refs.get("volume_attachments")
    if not isinstance(attachments, list):
        raise ValidationError("replacement recovery volume attachments are missing")
    configured_volume_names = _configured_volume_names(platform, role)
    if len(attachments) != len(configured_volume_names):
        raise ValidationError("replacement recovery volume attachment count is malformed")
    volumes: list[VolumeAttachment] = []
    for volume_name, item in zip(configured_volume_names, attachments, strict=True):
        if not isinstance(volume_name, str) or not isinstance(item, Mapping):
            raise ValidationError("replacement recovery volume attachment is malformed")
        volume_id = uuid(item.get("volume_id"), field="persistent volume UUID")
        device = item.get("device")
        if not isinstance(device, str) or not device.startswith("/dev/") or len(device) > 64:
            raise ValidationError("replacement recovery volume device is malformed")
        if (
            _find_exact(
                "volume",
                volume_name,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
                executable=executable,
            )
            != volume_id
        ):
            raise RecoveryRequired(
                "configured persistent-volume identity drifted during replacement",
                refs={"role": role, "old_server_id": old_id, "volume_id": volume_id},
            )
        volumes.append(VolumeAttachment(volume_id, volume_name, device, False))
    if len({volume.volume_id for volume in volumes}) != len(volumes) or len(
        {volume.device for volume in volumes}
    ) != len(volumes):
        raise ValidationError("replacement recovery volume attachments are not unique")
    return HostResources(old, port_id, port_name, tuple(volumes))


def _recover_host_replacement(
    platform: PlatformConfig,
    role: str,
    *,
    phase: str,
    refs: Mapping[str, Any],
    action: str,
    checkpoint: Checkpoint,
    health_check: HealthCheck | None = None,
    wait_seconds: float = 900,
    poll_interval_seconds: float = 2,
    timeout_seconds: float = 60,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
    host_key_runner: Runner = runtime.run,
    sleep: Callable[[float], None] = time.sleep,
) -> RecoveryResult:
    """Conservatively recover one recorded replacement phase.

    Pre-acceptance phases permit only rollback to the retained old server.
    Post-acceptance phases permit only cleanup of that old server after the
    replacement's exact resources and role readiness are re-verified.
    """
    deadline = _active_deadline(operation="replacement recovery")
    role = _role(role, persistent=True)
    if not isinstance(refs, Mapping):
        raise ValidationError("replacement recovery refs must be an object")
    allowed = {
        "observed": ("inspect", "rollback"),
        "old_stopped": ("inspect", "rollback"),
        "old_renamed": ("inspect", "rollback"),
        "old_retained": ("inspect", "rollback"),
        "resources_detached": ("inspect", "rollback"),
        "ambiguous": ("inspect", "rollback"),
        "replacement_created": ("inspect", "continue", "rollback"),
        "replacement_deleted": ("inspect", "rollback"),
        "accepted": ("inspect", "continue", "cleanup_old"),
        "complete": ("inspect", "continue", "cleanup_old"),
    }
    if action not in allowed.get(phase, ()):
        raise ValidationError("replacement recovery action is not safe for the recorded phase")
    verify_project(
        platform,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    resources = _recovery_resources(
        platform,
        role,
        refs,
        allow_missing_old=phase in ("accepted", "complete"),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    old_id = resources.host.server_id
    assert old_id is not None
    replacement_value = refs.get("replacement_server_id")
    replacement_id = (
        uuid(replacement_value, field="replacement server UUID")
        if replacement_value is not None
        else None
    )
    operation_id = uuid(refs.get("operation_id"), field="operation UUID")
    if replacement_id is None:
        replacement_id = _operation_replacement_id(
            platform,
            operation_id,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
    if action == "inspect":
        configured = _inventory_text(platform, f"hosts.{role}")
        rows = _server_rows(
            configured,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
        active_id = (
            _provider_uuid(_field(rows[0], "id"), field="server UUID") if len(rows) == 1 else old_id
        )
        state = "continue_required" if phase in ("accepted", "complete") else "rollback_required"
        return RecoveryResult(role, phase, action, active_id, old_id, replacement_id, state)
    selected_image_id = uuid(refs.get("selected_image_id"), field="selected image UUID")
    if replacement_id is not None and phase == "replacement_deleted":
        if not _resource_exists(
            "server",
            replacement_id,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        ):
            replacement_id = None
    if replacement_id is not None:
        assert resources.host.flavor_id is not None
        _verify_replacement_identity(
            platform,
            role,
            replacement_id,
            operation_id,
            selected_image_id,
            resources.host.flavor_id,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
    if action == "rollback":
        result = _rollback_replacement(
            platform,
            resources,
            replacement_id,
            resources.host.configured_name,
            selected_image_id,
            recovery_refs=refs,
            health_check=health_check,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
            host_key_runner=host_key_runner,
            sleep=sleep,
            checkpoint=checkpoint,
        )
        return RecoveryResult(
            role,
            phase,
            action,
            result.active_server_id,
            old_id,
            replacement_id,
            result.cleanup_state,
        )
    if replacement_id is None:
        raise RecoveryRequired(
            "accepted replacement server UUID is unavailable; refusing old-server cleanup",
            refs=resources.operation_refs(),
        )
    replacement_resources = _observe_host_resources_verified(
        platform,
        role,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )
    if (
        replacement_resources.host.server_id != replacement_id
        or replacement_resources.host.image_id != selected_image_id
        or replacement_resources.host.flavor_id != resources.host.flavor_id
        or replacement_resources.port_id != resources.port_id
        or replacement_resources.volumes != resources.volumes
    ):
        raise RecoveryRequired(
            "accepted replacement no longer owns the exact fixed resources",
            refs={**resources.operation_refs(), "replacement_server_id": replacement_id},
        )
    _wait_console_ready(
        platform,
        replacement_resources.host,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
        sleep=sleep,
    )
    _pin_admin_host_key(
        platform,
        replacement_resources.host,
        deadline=deadline,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
        host_key_runner=host_key_runner,
    )
    remaining = _deadline_remaining(deadline, operation="replacement recovery health checks")
    required_health_check = _required_role_health_check(
        platform,
        health_check,
        provider_runner=command_runner,
        executable=executable,
    )
    required_health_check(role, replacement_resources.host, remaining)
    if phase == "replacement_created":
        checkpoint(
            "accepted",
            {**refs, "replacement_server_id": replacement_id},
        )
    if _resource_exists(
        "server",
        old_id,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    ):
        _mutate_and_verify(
            ("server", "delete", old_id),
            lambda: (
                not _resource_exists(
                    "server",
                    old_id,
                    timeout_seconds=timeout_seconds,
                    command_runner=command_runner,
                    executable=executable,
                )
            ),
            message="accepted replacement is healthy but old-server cleanup remains ambiguous",
            refs={**refs, "replacement_server_id": replacement_id},
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
        )
    checkpoint(
        "complete",
        {**refs, "replacement_server_id": replacement_id, "cleanup_state": "confirmed"},
    )
    return RecoveryResult(role, phase, action, replacement_id, old_id, replacement_id, "confirmed")


@_command_deadline("wait_seconds")
def recover_host_replacement(
    platform: PlatformConfig,
    role: str,
    *,
    phase: str,
    refs: Mapping[str, Any],
    action: str,
    checkpoint: Checkpoint,
    health_check: HealthCheck | None = None,
    wait_seconds: float = 900,
    poll_interval_seconds: float = 2,
    timeout_seconds: float = 60,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
    host_key_runner: Runner = runtime.run,
    sleep: Callable[[float], None] = time.sleep,
) -> RecoveryResult:
    """Compatibility dispatcher for exact replacement recovery actions."""
    try:
        return _recover_host_replacement(
            platform,
            role,
            phase=phase,
            refs=refs,
            action=action,
            checkpoint=checkpoint,
            health_check=health_check,
            wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
            timeout_seconds=timeout_seconds,
            command_runner=command_runner,
            executable=executable,
            host_key_runner=host_key_runner,
            sleep=sleep,
        )
    except RecoveryRequired as error:
        full_refs = dict(refs)
        full_refs.update(error.refs)
        raise RecoveryRequired(str(error), refs=full_refs) from None


@_command_deadline("timeout_seconds")
def inspect_host_replacement(
    platform: PlatformConfig,
    role: str,
    *,
    phase: str,
    refs: Mapping[str, Any],
    timeout_seconds: float = 60,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
) -> RecoveryResult:
    """Inspect one recorded replacement without mutating provider resources."""
    return recover_host_replacement(
        platform,
        role,
        phase=phase,
        refs=refs,
        action="inspect",
        checkpoint=lambda _phase, _refs: None,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
    )


@_command_deadline("wait_seconds")
def rollback_host_replacement(
    platform: PlatformConfig,
    role: str,
    *,
    phase: str,
    refs: Mapping[str, Any],
    checkpoint: Checkpoint,
    health_check: HealthCheck | None = None,
    wait_seconds: float = 900,
    poll_interval_seconds: float = 2,
    timeout_seconds: float = 60,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
    host_key_runner: Runner = runtime.run,
    sleep: Callable[[float], None] = time.sleep,
) -> RecoveryResult:
    """Rollback an exact pre-acceptance replacement to the retained host."""
    return recover_host_replacement(
        platform,
        role,
        phase=phase,
        refs=refs,
        action="rollback",
        checkpoint=checkpoint,
        health_check=health_check,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
        host_key_runner=host_key_runner,
        sleep=sleep,
    )


@_command_deadline("wait_seconds")
def continue_host_replacement(
    platform: PlatformConfig,
    role: str,
    *,
    phase: str,
    refs: Mapping[str, Any],
    checkpoint: Checkpoint,
    health_check: HealthCheck | None = None,
    wait_seconds: float = 900,
    poll_interval_seconds: float = 2,
    timeout_seconds: float = 60,
    command_runner: Runner = runtime.run,
    executable: str = _DEFAULT_OPENSTACK_EXECUTABLE,
    host_key_runner: Runner = runtime.run,
    sleep: Callable[[float], None] = time.sleep,
) -> RecoveryResult:
    """Continue an exact created/accepted replacement through acceptance and cleanup."""
    return recover_host_replacement(
        platform,
        role,
        phase=phase,
        refs=refs,
        action="continue",
        checkpoint=checkpoint,
        health_check=health_check,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
        executable=executable,
        host_key_runner=host_key_runner,
        sleep=sleep,
    )


def _metadata_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit canonical platform image metadata")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--role", required=True, choices=IMAGE_ROLES)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args(argv)
    metadata = publisher_metadata(load_platform(Path(args.platform)), args.role, args.source_commit)
    for key, value in sorted(metadata.items()):
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_metadata_main())
