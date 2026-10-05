"""Strict parsing and checkout validation for UI-owned deployment configuration."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_versions import (
    RUNTIME_NAMES,
    VERSION_FILE_BYTES,
    RuntimeRequest,
    RuntimeVersionError,
    runtime_request,
)
from ..validation import (
    ValidationError,
    env_key,
    health_path,
    relative_path,
    resolve_inside,
    resource_name,
    script_name,
    uuid,
)
from .application_models import Manifest, StorageBinding
from .storage_contract import (
    RESERVED_ENVIRONMENT_KEYS,
    RESERVED_ENVIRONMENT_PREFIX,
    RESOURCE_OUTPUTS,
)

_SCHEMA_VERSION = 1
_RUNTIMES = {"node", "bun"}
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_MAX_CONFIGURATION_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class StorageOutputBinding:
    resource_id: str
    outputs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class DeploymentConfiguration:
    schema_version: int
    runtime: str
    packages: tuple[str, ...]
    build_script: str | None
    start_script: str
    port: int
    health_path: str
    storage_bindings: tuple[StorageOutputBinding, ...]

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "schemaVersion": self.schema_version,
                "build": {
                    "runtime": self.runtime,
                    "packages": list(self.packages),
                    "buildScript": self.build_script,
                    "startScript": self.start_script,
                },
                "runtime": {"port": self.port, "healthPath": self.health_path},
                "storageBindings": [
                    {"resourceId": item.resource_id, "outputs": dict(item.outputs)}
                    for item in self.storage_bindings
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def manifest(self, resources: Mapping[str, tuple[str, str]]) -> Manifest:
        bindings: list[StorageBinding] = []
        for binding in self.storage_bindings:
            resource = resources.get(binding.resource_id)
            if resource is None or resource[0] not in RESOURCE_OUTPUTS:
                raise ValidationError("storage binding references missing or inactive storage")
            resource_type, machine_name = resource
            checked_name = resource_name(machine_name)
            allowed = RESOURCE_OUTPUTS[resource_type]
            for output, _target in binding.outputs:
                if output not in allowed:
                    raise ValidationError(
                        f"storage output {output!r} is not supported by {resource_type}"
                    )
            bindings.append(StorageBinding(checked_name, resource_type, binding.outputs))
        return Manifest(
            runtime=self.runtime,
            packages=self.packages,
            build_script=self.build_script,
            start_script=self.start_script,
            port=self.port,
            health_path=self.health_path,
            storage_bindings=tuple(bindings),
        )


def _object(value: object, expected: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValidationError(f"{field} must be an object")
    if set(value) != expected:
        raise ValidationError(f"{field} fields are invalid")
    return value


def _load_json(payload: bytes | str) -> object:
    raw = payload.encode() if isinstance(payload, str) else payload
    if len(raw) > _MAX_CONFIGURATION_BYTES:
        raise ValidationError("deployment configuration exceeds its size limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValidationError("deployment configuration must be strict JSON") from None


def parse_configuration(payload: bytes | str | Mapping[str, Any]) -> DeploymentConfiguration:
    value = _load_json(payload) if isinstance(payload, (bytes, str)) else payload
    document = _object(
        value,
        {"schemaVersion", "build", "runtime", "storageBindings"},
        "deployment configuration",
    )
    if isinstance(document["schemaVersion"], bool) or document["schemaVersion"] != _SCHEMA_VERSION:
        raise ValidationError("deployment configuration schemaVersion must be 1")
    build = _object(
        document["build"],
        {"runtime", "packages", "buildScript", "startScript"},
        "build configuration",
    )
    runtime_name = build["runtime"]
    if runtime_name not in _RUNTIMES:
        raise ValidationError("build runtime must be node or bun")
    raw_packages = build["packages"]
    if not isinstance(raw_packages, list) or not raw_packages or len(raw_packages) > 32:
        raise ValidationError("build packages must contain from 1 through 32 paths")
    packages = tuple(relative_path(item, field="package path") for item in raw_packages)
    if len(packages) != len(set(packages)):
        raise ValidationError("build package paths must be unique")
    raw_build_script = build["buildScript"]
    build_script = None if raw_build_script is None else script_name(raw_build_script)
    start = script_name(build["startScript"])

    runtime = _object(document["runtime"], {"port", "healthPath"}, "runtime configuration")
    port = runtime["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ValidationError("runtime port must be an integer from 1 through 65535")
    checked_health = health_path(runtime["healthPath"])

    raw_bindings = document["storageBindings"]
    if not isinstance(raw_bindings, list) or len(raw_bindings) > 32:
        raise ValidationError("storageBindings must be an array with at most 32 items")
    bindings: list[StorageOutputBinding] = []
    resource_ids: set[str] = set()
    targets: set[str] = set()
    for raw_binding in raw_bindings:
        binding = _object(raw_binding, {"resourceId", "outputs"}, "storage binding")
        resource_id = uuid(binding["resourceId"], field="storage resource ID")
        if resource_id in resource_ids:
            raise ValidationError("storage resource bindings must be unique")
        resource_ids.add(resource_id)
        outputs = binding["outputs"]
        if (
            not isinstance(outputs, dict)
            or not outputs
            or any(not isinstance(key, str) for key in outputs)
        ):
            raise ValidationError("storage binding outputs must be a non-empty object")
        mapped: list[tuple[str, str]] = []
        for output, raw_target in outputs.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", output):
                raise ValidationError("storage output name is malformed")
            target = env_key(raw_target)
            if target in RESERVED_ENVIRONMENT_KEYS or target.startswith(
                RESERVED_ENVIRONMENT_PREFIX
            ):
                raise ValidationError(f"storage binding target {target!r} is reserved")
            if target in targets:
                raise ValidationError(f"storage binding target {target!r} conflicts")
            targets.add(target)
            mapped.append((output, target))
        bindings.append(StorageOutputBinding(resource_id, tuple(sorted(mapped))))
    return DeploymentConfiguration(
        schema_version=_SCHEMA_VERSION,
        runtime=runtime_name,
        packages=packages,
        build_script=build_script,
        start_script=start,
        port=port,
        health_path=checked_health,
        storage_bindings=tuple(sorted(bindings, key=lambda item: item.resource_id)),
    )


def _checkout_lockfile(
    configuration: DeploymentConfiguration, root: Path, package: str, maximum: int
) -> None:
    directory = resolve_inside(root, package, field="package path")
    if not directory.is_dir():
        raise ValidationError(f"package {package!r} is not a source directory")
    names = ("bun.lock", "bun.lockb") if configuration.runtime == "bun" else ("package-lock.json",)
    if not any(_direct_file(directory / name, maximum) for name in names):
        raise ValidationError(f"package {package!r} is missing its supported lockfile")


def _read_direct(path: Path, maximum_bytes: int) -> bytes:
    name = path.name
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError as error:
        raise ValidationError(f"{name} could not be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise ValidationError(f"{name} exceeds its size limit")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining and (chunk := os.read(descriptor, remaining)):
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > maximum_bytes:
        raise ValidationError(f"{name} exceeds its size limit")
    return raw


def _checkout_package(root: Path, maximum_package_bytes: int) -> dict[str, Any]:
    package_json = root / "package.json"
    if not _direct_file(package_json, maximum_package_bytes):
        raise ValidationError("source root is missing a bounded direct package.json")
    value = _load_json(_read_direct(package_json, maximum_package_bytes))
    if not isinstance(value, dict) or not isinstance(value.get("scripts"), dict):
        raise ValidationError("package.json scripts must be an object")
    return value


def _version_file(root: Path, name: str) -> str | None:
    """A root version file's text; None when the checkout has none."""
    path = root / name
    if not path.exists() and not path.is_symlink():
        return None
    if not _direct_file(path, VERSION_FILE_BYTES):
        raise RuntimeVersionError(f"{name} must be a file of at most 1 KB.")
    try:
        return _read_direct(path, VERSION_FILE_BYTES).decode("utf-8", errors="replace")
    except ValidationError:
        raise RuntimeVersionError(f"{name} must be a file of at most 1 KB.") from None


def _checkout_runtime(
    configuration: DeploymentConfiguration, root: Path, package: dict[str, Any]
) -> RuntimeRequest | None:
    return runtime_request(configuration.runtime, package, lambda name: _version_file(root, name))


def validate_checkout(
    configuration: DeploymentConfiguration,
    source_root: str | Path,
    *,
    maximum_package_bytes: int = 65_536,
    maximum_lockfile_bytes: int = 1_048_576,
) -> RuntimeRequest | None:
    """Validate UI-selected package inputs in an acquired exact checkout.

    Return the runtime version the checkout asks for, if any. A request that
    can never be built raises RuntimeVersionError, whose message is the owner's.
    """
    if not isinstance(configuration, DeploymentConfiguration):
        raise ValidationError("deployment configuration snapshot is malformed")
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("source checkout is not a directory")
    for package in configuration.packages:
        _checkout_lockfile(configuration, root, package, maximum_lockfile_bytes)
    manifest = _checkout_package(root, maximum_package_bytes)
    scripts = manifest["scripts"]
    for name in dict.fromkeys((configuration.start_script, configuration.build_script)):
        if name is None:
            continue
        command = scripts.get(name)
        if not isinstance(command, str) or not command:
            raise ValidationError(f"package.json is missing configured script {name!r}")
    return _checkout_runtime(configuration, root, manifest)


def checkout_checks(
    configuration: DeploymentConfiguration, source_root: Path
) -> list[dict[str, str]]:
    """Named advisory results from the exact validators the build uses.

    Return only release-owned guidance; JSON input and parser errors stay private.
    """
    root = source_root.resolve(strict=True)
    manifest = {"id": "package-json", "label": "package.json in the repository root", "state": "ok"}
    checks = [manifest]
    package: dict[str, Any] | None = None
    try:
        package = _checkout_package(root, 65_536)
    except (ValidationError, OSError):
        manifest.update(
            state="problem",
            problem='Add a valid package.json with a "scripts" section to the repository root. It must be a direct file at most 64 KB.',
        )
    scripts = None if package is None else package["scripts"]
    for name in dict.fromkeys((configuration.start_script, configuration.build_script)):
        if name is None:
            continue
        command = None if scripts is None else scripts.get(name)
        check = {
            "id": f"script:{name}",
            "label": f'Script "{name}"',
            "state": "unknown"
            if scripts is None
            else "ok"
            if isinstance(command, str) and command
            else "problem",
        }
        if check["state"] == "problem":
            check["problem"] = f'package.json has no "{name}" script.'
        checks.append(check)
    checks.append(_runtime_check(configuration, root, package))
    for directory in configuration.packages:
        where = "the repository root" if directory == "." else directory
        check = {"id": f"lockfile:{directory}", "label": f"Lockfile in {where}", "state": "ok"}
        try:
            _checkout_lockfile(configuration, root, directory, 1_048_576)
        except (ValidationError, OSError):
            lock_name = (
                "bun.lock or bun.lockb" if configuration.runtime == "bun" else "package-lock.json"
            )
            check.update(
                state="problem",
                problem=f"Commit a direct {lock_name} file at most 1 MB in {where}.",
            )
        checks.append(check)
    return checks


def _runtime_check(
    configuration: DeploymentConfiguration, root: Path, package: dict[str, Any] | None
) -> dict[str, str]:
    """What runtime version the checkout asks for, or why it can't be built.

    A request is checked by id runtime-version; a checkout that asks for no
    version gets runtime-default, so the portal can say the build uses the
    platform's image.
    """
    name = RUNTIME_NAMES[configuration.runtime]
    check = {"id": "runtime-version", "label": f"{name} version", "state": "unknown"}
    if package is None:
        return check
    try:
        request = _checkout_runtime(configuration, root, package)
    except RuntimeVersionError as error:
        check.update(state="problem", problem=str(error))
    except (ValidationError, OSError):
        pass
    else:
        if request is None:
            check.update(id="runtime-default", label=f"{name} (platform default)", state="ok")
        else:
            check.update(label=request.describe(), state="ok")
    return check


def _direct_file(path: Path, maximum_bytes: int) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_size <= maximum_bytes
    )


def branch_name(value: object) -> str:
    if not isinstance(value, str) or not _BRANCH.fullmatch(value):
        raise ValidationError("repository branch is malformed")
    if value.endswith(("/", ".", ".lock")) or ".." in value or "//" in value or "@{" in value:
        raise ValidationError("repository branch is malformed")
    return value
