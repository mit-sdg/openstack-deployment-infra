"""Direct application build, deployment, environment, and log operations.

The public functions in this module deliberately compose concrete operations.
They do not accept shell text, provider objects, or application-supplied Docker
instructions. External work is performed by bounded injected callables so the
same paths can be tested without cloud or scheduler access.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import time
import uuid as uuid_module
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from ..config import Config, PlatformConfig, RuntimeImages
from ..contracts import (
    BUILDER_EXECUTABLE_NAME,
    DEPLOYMENT_ROUTE_HEADER_LOWER,
    OPERATOR_ACCOUNT_NAME,
)
from ..remote import call_helper
from ..runtime import bounded_http, ensure_private_directory, lock, run
from ..validation import (
    ValidationError,
    bounded_text,
    commit,
    env_key,
    health_path,
    oci_digest_pin,
    relative_path,
    repository_url,
    script_name,
    sha256_hex,
    slug,
    uuid,
)
from . import database as db
from .application_models import Manifest, Recipe, StorageBinding
from .nomad_jobs import (
    deployment_worker_ids,
    nomad_candidate_identity,
    nomad_job_id,
    nomad_placement_id,
    nomad_route_marker,
    nomad_route_priority,
    render_nomad_job,
)

_RECIPE_GENERATOR_VERSION = 2
_IMAGE_NAME = re.compile(r"[a-z0-9.-]+(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_BUILD_METADATA = 65_536
_MAX_PROVIDER_RESPONSE = 65_536
_PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROVIDER_INHERIT_ENV = (
    "HOME",
    "USER",
    "SSH_AUTH_SOCK",
    "OS_AUTH_URL",
    "OS_APPLICATION_CREDENTIAL_ID",
    "OS_APPLICATION_CREDENTIAL_SECRET",
    "OS_AUTH_TYPE",
    "OS_CACERT",
    "OS_IDENTITY_API_VERSION",
    "OS_INTERFACE",
    "OS_PROJECT_ID",
    "OS_PROJECT_NAME",
    "OS_REGION_NAME",
    "PLATFORM_CONFIG",
    "PKI_DIR",
    "STORAGE_SECRETS_FILE",
    "BUILDER_OPERATOR_PUBLIC_KEY",
)


def provider_command(platform: PlatformConfig, tool: str) -> tuple[str, ...]:
    """Absolute path of a namespace-scoped admin CLI.

    The role image links these into ``<paths.root>/bin`` as
    ``<namespace>-<tool>``. The module constants below name one particular
    deployment, so any deployment whose namespace or root differs could not run
    a build or touch a worker at all.
    """
    root = PurePosixPath(str(platform.get("paths.root")))
    if not root.is_absolute():
        raise ValidationError("deployment root must be an absolute path")
    return (str(root / "bin" / f"{platform.namespace}-{tool}"),)


def builder_identity_path(platform: PlatformConfig) -> str:
    """Absolute path of the private key that reaches a disposable builder.

    ``<paths.root>/secrets`` is a symlink onto the admin state volume, so the
    key survives an admin replacement. A default identity under the admin's
    home directory does not, and its absence surfaces only as an opaque SSH
    authentication failure part-way through a deployment.
    """
    root = PurePosixPath(str(platform.get("paths.root")))
    if not root.is_absolute():
        raise ValidationError("deployment root must be an absolute path")
    return str(root / "secrets" / "builder_operator_ed25519")


DEFAULT_BUILDER_SSH_COMMAND = ("ssh",)
_BUILDER_REMOTE_COMMAND = f"/run/current-system/sw/bin/{BUILDER_EXECUTABLE_NAME}"
_DEPLOY_PHASES = {
    "validated",
    "source_acquired",
    "builder_creating",
    "builder_created",
    "source_transferred",
    "building",
    "image_pushed",
    "builder_cleaned",
    "worker_creating",
    "worker_ready",
    "job_submitted",
    "deployment_healthy",
    "accepted",
}


def _deadline_timeout(maximum: float, deadline: float | None, *, operation: str) -> float:
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or maximum <= 0:
        raise ValueError(f"{operation} timeout must be positive")
    if deadline is None:
        return float(maximum)
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise ValueError(f"{operation} deadline is malformed")
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise ApplicationError(f"{operation} exceeded its whole-operation deadline")
    return min(float(maximum), remaining)


def _deadline_timestamp(deadline: float) -> str:
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise ApplicationError("build operation exceeded its whole-operation deadline")
    return (
        (datetime.now(UTC) + timedelta(seconds=remaining))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


class ApplicationError(RuntimeError):
    """An application operation failed with an operator-safe summary."""


class DeploymentFailed(ApplicationError):
    def __init__(
        self,
        message: str,
        *,
        cleanup_succeeded: bool,
        cleanup_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.cleanup_succeeded = cleanup_succeeded
        self.cleanup_evidence = dict(cleanup_evidence or {})
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BuildExecution:
    metadata: bytes = field(repr=False)
    log: bytes = field(repr=False)
    log_truncated: bool = False


@dataclass(frozen=True, slots=True)
class BuildResult:
    build_id: str
    image: str
    digest: str
    cleanup_confirmed: bool
    build_log: bytes = field(default=b"", repr=False)
    build_log_truncated: bool = False


@dataclass(frozen=True, slots=True)
class BuilderObservation:
    build_id: str
    server_id: str | None
    server_name: str
    port_id: str | None
    port_name: str
    address: str | None
    image_id: str | None
    flavor_name: str | None
    ready: bool

    @property
    def absent(self) -> bool:
        return self.server_id is None and self.port_id is None


@dataclass(frozen=True, slots=True)
class WorkerObservation:
    application_id: str
    application_slug: str
    server_id: str | None
    server_name: str
    port_id: str | None
    port_name: str
    image_id: str | None
    flavor_name: str | None
    ready: bool

    @property
    def absent(self) -> bool:
        return self.server_id is None and self.port_id is None


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    nomad_version: int
    observations: int


@dataclass(frozen=True, slots=True)
class RegistryRetentionResult:
    protected: tuple[str, ...]
    deleted: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    application_id: str
    application_slug: str
    repository: str
    source_commit: str
    requested_ref: str
    configuration_revision: int
    configuration_sha256: str
    worker_flavor: str
    cpu_mhz: int
    memory_mib: int


@dataclass(frozen=True, slots=True)
class DeploymentBuild:
    image: str
    manifest: Manifest
    recipe_hash: str
    build_log_path: str
    refs: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DeploymentWorker:
    server_id: str
    server_name: str
    port_id: str
    port_name: str
    refs: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DeploymentAcceptance:
    application_id: str
    application_slug: str
    repository: str
    deployment_id: str
    worker_server_id: str
    worker_server_name: str
    worker_port_id: str
    worker_port_name: str
    worker_flavor: str
    cpu_mhz: int
    memory_mib: int
    source_commit: str
    recipe_hash: str
    image: str
    nomad_job: str
    nomad_job_sha256: str
    nomad_version: int
    health_path: str
    application_port: int
    build_log_path: str
    public_url: str


# Typed source and recipe preparation


def parse_dotenv(payload: bytes | str, *, maximum_bytes: int = 262_144) -> dict[str, str]:
    """Parse the deliberately strict, non-executable dotenv subset."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > maximum_bytes:
        raise ValidationError(f"dotenv exceeds its {maximum_bytes}-byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("dotenv must be UTF-8") from error
    if "\x00" in text:
        raise ValidationError("dotenv must not contain NUL bytes")
    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export ") or "=" not in line:
            raise ValidationError(f"dotenv line {number} is malformed")
        name, raw_value = line.split("=", 1)
        if name != name.strip():
            raise ValidationError(f"dotenv line {number} has whitespace around its key")
        key = env_key(name)
        if key in result:
            raise ValidationError(f"dotenv repeats key {key!r}")
        value = raw_value
        if value.startswith("'"):
            if len(value) < 2 or not value.endswith("'") or "'" in value[1:-1]:
                raise ValidationError(f"dotenv line {number} has malformed single quotes")
            value = value[1:-1]
        elif value.startswith('"'):
            if len(value) < 2 or not value.endswith('"'):
                raise ValidationError(f"dotenv line {number} has malformed double quotes")
            encoded = value[1:-1]
            if re.search(r"\\(?![\\\"])", encoded):
                raise ValidationError(f"dotenv line {number} has an unsupported escape")
            value = encoded.replace(r"\"", '"').replace(r"\\", "\\")
        elif any(character.isspace() for character in value) or "#" in value:
            raise ValidationError(f"dotenv line {number} must quote whitespace or #")
        if "$" in value or "`" in value or "$(" in value or "${" in value:
            raise ValidationError(f"dotenv line {number} contains interpolation or command syntax")
        result[key] = bounded_text(value, field=f"environment value for {key}", maximum=65_536)
    return result


def _source_size(root: Path, maximum_bytes: int) -> int:
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            path = Path(directory) / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                total += len(os.readlink(path).encode())
            elif stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
            elif stat.S_ISDIR(metadata.st_mode):
                continue
            else:
                raise ValidationError("source contains an unsupported special file")
            if total > maximum_bytes:
                raise ValidationError(f"source exceeds its {maximum_bytes}-byte limit")
    return total


def acquire_github_commit(
    repository: str,
    source_commit: str,
    destination: str | Path,
    *,
    maximum_bytes: int = 52_428_800,
    timeout_seconds: float = 300,
    deadline: float | None = None,
    command_runner: Callable[..., Any] = run,
) -> Path:
    """Acquire exactly one full commit from a credential-free public GitHub URL.

    Git receives no credential environment and cannot use file transport or an
    interactive prompt. The commit identity is checked before repository
    metadata is removed from the bounded build context. Dockerfile-named files
    may remain as inert source; the builder selects a separately generated
    recipe rather than a file from this checkout.
    """
    canonical_repository = repository_url(repository)
    expected_commit = commit(source_commit)
    root = Path(destination)
    if root.exists() or root.is_symlink():
        raise ValidationError("source destination must not already exist")
    root.mkdir(mode=0o700, parents=False)
    git_options = (
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "http.followRedirects=false",
        "-c",
        "filter.lfs.required=false",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.process=",
        "-c",
        "advice.detachedHead=false",
    )
    environment = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }

    def bounds() -> dict[str, Any]:
        return {
            "timeout_seconds": _deadline_timeout(
                timeout_seconds, deadline, operation="source acquisition"
            ),
            "stdout_limit": 65_536,
            "stderr_limit": 262_144,
            "env": environment,
        }

    try:
        command_runner(("git", *git_options, "init", "--quiet", str(root)), **bounds())
        command_runner(
            (
                "git",
                *git_options,
                "-C",
                str(root),
                "fetch",
                "--quiet",
                "--depth=1",
                "--no-tags",
                canonical_repository,
                expected_commit,
            ),
            **bounds(),
        )
        observed = (
            command_runner(
                (
                    "git",
                    *git_options,
                    "-C",
                    str(root),
                    "rev-parse",
                    "--verify",
                    "FETCH_HEAD^{commit}",
                ),
                **bounds(),
            )
            .stdout.decode("ascii", errors="strict")
            .strip()
        )
        if observed != expected_commit:
            raise ApplicationError("GitHub returned a different source commit")
        command_runner(
            (
                "git",
                *git_options,
                "-C",
                str(root),
                "checkout",
                "--quiet",
                "--detach",
                expected_commit,
            ),
            **bounds(),
        )
        index = command_runner(
            ("git", *git_options, "-C", str(root), "ls-files", "--stage", "-z"),
            **bounds(),
        ).stdout
        if not isinstance(index, bytes):
            raise ValidationError("Git index evidence was malformed")
        for entry in index.split(b"\0"):
            if not entry:
                continue
            try:
                prefix, _path = entry.split(b"\t", 1)
                mode, object_id, stage = prefix.split(b" ")
            except ValueError as error:
                raise ValidationError("Git index evidence was malformed") from error
            if mode == b"160000":
                raise ValidationError("source repositories with gitlinks are not supported")
            if mode not in {b"100644", b"100755", b"120000"} or stage != b"0":
                raise ValidationError("source Git index contains an unsupported entry mode")
            if not re.fullmatch(rb"[0-9a-f]{40,64}", object_id):
                raise ValidationError("Git index evidence was malformed")
        head = (
            command_runner(
                ("git", *git_options, "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"),
                **bounds(),
            )
            .stdout.decode("ascii", errors="strict")
            .strip()
        )
        if head != expected_commit:
            raise ApplicationError("checked out source did not match the requested commit")
        for path in root.rglob("*"):
            if path.name == ".gitmodules":
                raise ValidationError("source repositories with .gitmodules are not supported")
        shutil.rmtree(root / ".git")
        _source_size(root, maximum_bytes)
        return root
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def generate_recipe(manifest: Manifest, runtime_images: RuntimeImages) -> Recipe:
    """Generate a deterministic Dockerfile from package-script names and pins."""
    start_script = script_name(manifest.start_script)
    build_script = None if manifest.build_script is None else script_name(manifest.build_script)
    if (
        isinstance(manifest.port, bool)
        or not isinstance(manifest.port, int)
        or not 1 <= manifest.port <= 65_535
    ):
        raise ValidationError("application port must be an integer from 1 through 65535")
    if manifest.runtime == "bun":
        image = oci_digest_pin(runtime_images.bun, field="runtimeImages.bun")
        install = '["bun","install","--frozen-lockfile"]'
        build = (
            None
            if build_script is None
            else json.dumps(["bun", "run", build_script], separators=(",", ":"))
        )
        start = json.dumps(["bun", "run", start_script], separators=(",", ":"))
    elif manifest.runtime == "node":
        image = oci_digest_pin(runtime_images.node, field="runtimeImages.node")
        install = '["npm","ci"]'
        build = (
            None
            if build_script is None
            else json.dumps(["npm", "run", build_script], separators=(",", ":"))
        )
        start = json.dumps(["npm", "run", start_script], separators=(",", ":"))
    else:  # Manifest is public and can also be constructed by a caller.
        raise ValidationError("runtime must be bun or node")
    packages = tuple(relative_path(value, field="package path") for value in manifest.packages)
    if not packages or len(packages) != len(set(packages)):
        raise ValidationError("application packages must be a non-empty unique list")
    lines = [
        f"FROM {image}",
        "WORKDIR /app",
        "COPY --chown=65532:65532 . /app",
    ]
    for package in packages:
        workdir = "/app" if package == "." else f"/app/{package}"
        lines.extend((f"WORKDIR {workdir}", f"RUN {install}"))
    lines.append("WORKDIR /app")
    if build is not None:
        lines.append(f"RUN {build}")
    lines.extend(
        (
            "ENV NODE_ENV=production",
            "USER 65532:65532",
            f"EXPOSE {manifest.port}",
            f"CMD {start}",
            "",
        )
    )
    content = "\n".join(lines).encode()
    identity = json.dumps(
        {
            "generatorVersion": _RECIPE_GENERATOR_VERSION,
            "runtime": manifest.runtime,
            "runtimeImage": image,
            "packages": list(packages),
            "build": build_script,
            "start": start_script,
            "port": manifest.port,
            "healthPath": health_path(manifest.health_path),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return Recipe(dockerfile=content, sha256=hashlib.sha256(identity).hexdigest())


def parse_build_metadata(payload: bytes | str) -> str:
    """Extract only BuildKit's final pushed image digest from bounded metadata."""
    raw = payload.encode() if isinstance(payload, str) else payload
    if len(raw) > _MAX_BUILD_METADATA:
        raise ApplicationError("builder metadata exceeded its size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApplicationError("builder returned malformed image metadata") from error
    if not isinstance(value, dict) or "containerimage.digest" not in value:
        raise ApplicationError("builder metadata omitted the pushed image digest")
    allowed = {
        "containerimage.digest",
        "containerimage.config.digest",
        "containerimage.descriptor",
        # The image exporter always reports the reference it pushed.
        "image.name",
        "buildkit/trace",
    }
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise ApplicationError("builder metadata contained an unexpected field")
    digest = value["containerimage.digest"]
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ApplicationError("builder returned an invalid pushed image digest")
    config_digest = value.get("containerimage.config.digest")
    if config_digest is not None and (
        not isinstance(config_digest, str) or not _DIGEST.fullmatch(config_digest)
    ):
        raise ApplicationError("builder returned an invalid image config digest")
    descriptor = value.get("containerimage.descriptor")
    if descriptor is not None:
        try:
            parsed_descriptor = (
                json.loads(descriptor) if isinstance(descriptor, str) else descriptor
            )
        except json.JSONDecodeError as error:
            raise ApplicationError("builder returned a malformed image descriptor") from error
        if not isinstance(parsed_descriptor, dict) or parsed_descriptor.get("digest") != digest:
            raise ApplicationError("builder image descriptor did not match the pushed digest")
    return digest


def _fixed_command(value: Sequence[str], *, field_name: str) -> tuple[str, ...]:
    command = tuple(value)
    if not command or any(
        not isinstance(item, str) or not item or "\x00" in item for item in command
    ):
        raise ValueError(f"{field_name} must be a fixed non-empty argv")
    return command


def _provider_name(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _PROVIDER_NAME.fullmatch(value):
        raise ValidationError(f"{field_name} is malformed")
    return value


def _project_environment(
    project_name: str | None,
    project_id: str | None,
) -> dict[str, str]:
    if project_name is None and project_id is None:
        return {}
    if project_name is None or project_id is None:
        raise ValueError("project name and UUID must be supplied together")
    return {
        "EXPECTED_PROJECT_NAME": _provider_name(project_name, field_name="project name"),
        "EXPECTED_PROJECT_ID": uuid(project_id, field="project UUID"),
    }


def _fixed_executable(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or "\x00" in value
        or any(character.isspace() for character in value)
    ):
        raise ValidationError(f"{field_name} is malformed")
    return value


def _provider_result(
    command_runner: Callable[..., Any],
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    stdin: bytes | None = None,
    stdout_limit: int = _MAX_PROVIDER_RESPONSE,
    stderr_limit: int = 262_144,
    env: Mapping[str, str] | None = None,
    inherit_env: Sequence[str] = _PROVIDER_INHERIT_ENV,
    allow_stderr_truncation: bool = False,
    stderr_sink: BinaryIO | None = None,
) -> Any:
    try:
        result = command_runner(
            tuple(argv),
            timeout_seconds=timeout_seconds,
            stdin=stdin,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            inherit_env=tuple(inherit_env),
            env=env,
            check=True,
            stderr_sink=stderr_sink,
        )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise ApplicationError(
            "application provider command failed; details were withheld"
        ) from error
    if bool(getattr(result, "stdout_truncated", False)) or (
        bool(getattr(result, "stderr_truncated", False)) and not allow_stderr_truncation
    ):
        raise ApplicationError("application provider command output exceeded its safety limit")
    return result


def _json_object(payload: bytes | str, *, field_name: str) -> Mapping[str, Any]:
    raw = payload.encode() if isinstance(payload, str) else payload
    if len(raw) > _MAX_PROVIDER_RESPONSE:
        raise ApplicationError(f"{field_name} exceeded its size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApplicationError(f"{field_name} was malformed") from error
    if not isinstance(value, dict):
        raise ApplicationError(f"{field_name} was not an object")
    return value


def _optional_uuid(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        return uuid(value, field=field_name)
    except ValidationError as error:
        raise ApplicationError(f"provider returned a malformed {field_name}") from error


def _resource_mapping(value: object, *, field_name: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApplicationError(f"provider returned malformed {field_name} evidence")
    return value


# Disposable builder lifecycle


def parse_builder_observation(
    payload: bytes | str,
    *,
    build_id: str,
    prefix: str,
) -> BuilderObservation:
    """Parse the redacted, single-object builder lifecycle contract."""
    identifier = uuid(build_id, field="build ID")
    safe_prefix = _provider_name(prefix, field_name="platform prefix")
    short = identifier.replace("-", "")[:12]
    server_name = f"{safe_prefix}-builder-{short}"
    port_name = f"{server_name}-v4"
    value = _json_object(payload, field_name="builder observation")
    if value.keys() != {"buildId", "server", "port", "ready"} or value["buildId"] != identifier:
        raise ApplicationError("builder observation identity did not match the request")
    server = _resource_mapping(value["server"], field_name="builder server")
    port = _resource_mapping(value["port"], field_name="builder port")
    ready = value["ready"]
    if not isinstance(ready, bool):
        raise ApplicationError("builder observation readiness was malformed")
    server_id = None
    image_id = None
    flavor_name = None
    if server is not None:
        if server.keys() != {
            "id",
            "name",
            "status",
            "imageId",
            "flavorName",
            "managedBy",
            "buildId",
        }:
            raise ApplicationError("builder server evidence was malformed")
        server_id = _optional_uuid(server["id"], field_name="builder server UUID")
        image_id = _optional_uuid(server["imageId"], field_name="builder image UUID")
        flavor_name = _provider_name(server["flavorName"], field_name="builder flavor")
        if (
            server_id is None
            or image_id is None
            or server["name"] != server_name
            or not isinstance(server["status"], str)
            or server["managedBy"] != "platform"
            or server["buildId"] != identifier
        ):
            raise ApplicationError("builder server identity did not match the request")
    port_id = None
    address = None
    if port is not None:
        if port.keys() != {"id", "name", "deviceId", "address", "description"}:
            raise ApplicationError("builder port evidence was malformed")
        port_id = _optional_uuid(port["id"], field_name="builder port UUID")
        if (
            port_id is None
            or port["name"] != port_name
            or port["description"] != f"managed-by=platform;build-id={identifier}"
        ):
            raise ApplicationError("builder port identity did not match the request")
        device_id = _optional_uuid(port["deviceId"], field_name="builder port device UUID")
        if device_id != server_id:
            raise ApplicationError("builder port was not attached to the observed server")
        try:
            address = str(ipaddress.ip_address(port["address"]))
        except (TypeError, ValueError) as error:
            raise ApplicationError("builder port address was malformed") from error
    if (server is None) != (port is None):
        raise ApplicationError("builder server and fixed port evidence was incomplete")
    if ready and (server_id is None or port_id is None or address is None):
        raise ApplicationError("builder was reported ready without complete resources")
    return BuilderObservation(
        identifier,
        server_id,
        server_name,
        port_id,
        port_name,
        address,
        image_id,
        flavor_name,
        ready,
    )


def observe_builder(
    build_id: str,
    *,
    prefix: str,
    timeout_seconds: float,
    deadline: float | None = None,
    project_name: str | None = None,
    project_id: str | None = None,
    command_runner: Callable[..., Any] = run,
    builder_command: Sequence[str] = (),
) -> BuilderObservation:
    identifier = uuid(build_id, field="build ID")
    command = _fixed_command(builder_command, field_name="builder command")
    result = _provider_result(
        command_runner,
        (*command, "show", identifier),
        timeout_seconds=_deadline_timeout(
            timeout_seconds, deadline, operation="builder observation"
        ),
        env=_project_environment(project_name, project_id) or None,
    )
    return parse_builder_observation(result.stdout, build_id=identifier, prefix=prefix)


def create_builder(
    build_id: str,
    *,
    prefix: str,
    selected_image_id: str,
    flavor_name: str,
    timeout_seconds: float,
    deadline: float | None = None,
    project_name: str | None = None,
    project_id: str | None = None,
    command_runner: Callable[..., Any] = run,
    builder_command: Sequence[str] = (),
) -> BuilderObservation:
    identifier = uuid(build_id, field="build ID")
    image_id = uuid(selected_image_id, field="selected builder image UUID")
    flavor = _provider_name(flavor_name, field_name="builder flavor")
    command = _fixed_command(builder_command, field_name="builder command")
    _provider_result(
        command_runner,
        (*command, "create", identifier),
        timeout_seconds=_deadline_timeout(timeout_seconds, deadline, operation="builder creation"),
        env={
            "IMAGE_NAME": image_id,
            "FLAVOR_NAME": flavor,
            **_project_environment(project_name, project_id),
        },
    )
    observed = observe_builder(
        identifier,
        prefix=prefix,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
        project_name=project_name,
        project_id=project_id,
        command_runner=command_runner,
        builder_command=command,
    )
    if not observed.ready or observed.image_id != image_id or observed.flavor_name != flavor:
        raise ApplicationError("builder did not match the selected image and flavor")
    return observed


def delete_builder(
    build_id: str,
    *,
    prefix: str,
    timeout_seconds: float,
    deadline: float | None = None,
    project_name: str | None = None,
    project_id: str | None = None,
    command_runner: Callable[..., Any] = run,
    builder_command: Sequence[str] = (),
) -> BuilderObservation:
    identifier = uuid(build_id, field="build ID")
    command = _fixed_command(builder_command, field_name="builder command")
    _provider_result(
        command_runner,
        (*command, "delete", identifier),
        timeout_seconds=_deadline_timeout(timeout_seconds, deadline, operation="builder cleanup"),
        env=_project_environment(project_name, project_id) or None,
    )
    observed = observe_builder(
        identifier,
        prefix=prefix,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
        project_name=project_name,
        project_id=project_id,
        command_runner=command_runner,
        builder_command=command,
    )
    if not observed.absent:
        raise ApplicationError("builder server or port remained after cleanup")
    return observed


def create_build_archive(
    source_directory: str | Path,
    recipe: Recipe,
    *,
    maximum_bytes: int = 52_428_800,
) -> tuple[bytes, str]:
    """Create a deterministic archive with source and generated recipe kept separate."""
    source = Path(source_directory).resolve(strict=True)
    if not source.is_dir():
        raise ValidationError("source directory is not a directory")
    _source_size(source, maximum_bytes)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        source_root = tarfile.TarInfo("source")
        source_root.type = tarfile.DIRTYPE
        source_root.mode = 0o755
        source_root.mtime = 0
        source_root.uid = source_root.gid = 0
        archive.addfile(source_root)
        directories: set[Path] = {Path(".")}
        paths = sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix())
        for path in paths:
            relative = path.relative_to(source)
            directories.update(relative.parents)
        for relative in sorted(directories, key=lambda item: (len(item.parts), item.as_posix())):
            if relative == Path("."):
                continue
            info = tarfile.TarInfo(f"source/{relative.as_posix()}")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            info.uid = info.gid = 0
            archive.addfile(info)
        for path in paths:
            relative = path.relative_to(source)
            metadata = path.lstat()
            name = f"source/{relative.as_posix()}"
            if stat.S_ISDIR(metadata.st_mode):
                continue
            info = tarfile.TarInfo(name)
            info.mtime = 0
            info.uid = info.gid = 0
            if stat.S_ISREG(metadata.st_mode):
                info.size = metadata.st_size
                info.mode = 0o755 if metadata.st_mode & 0o111 else 0o644
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            elif stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                resolved = (path.parent / target).resolve(strict=False)
                if os.path.isabs(target) or not resolved.is_relative_to(source):
                    raise ValidationError("source symlink escapes the source checkout")
                info.type = tarfile.SYMTYPE
                info.linkname = target
                info.mode = 0o777
                archive.addfile(info)
            else:
                raise ValidationError("source contains an unsupported special file")
        directory = tarfile.TarInfo("recipe")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = 0
        directory.uid = directory.gid = 0
        archive.addfile(directory)
        dockerfile = tarfile.TarInfo("recipe/Dockerfile")
        dockerfile.size = len(recipe.dockerfile)
        dockerfile.mode = 0o600
        dockerfile.mtime = 0
        dockerfile.uid = dockerfile.gid = 0
        archive.addfile(dockerfile, io.BytesIO(recipe.dockerfile))
    payload = output.getvalue()
    if len(payload) > maximum_bytes + 1_048_576:
        raise ValidationError("builder archive exceeds its size limit")
    return payload, hashlib.sha256(payload).hexdigest()


def _builder_identity(identity_path: str | Path) -> Path:
    """Require a direct, private regular file before offering it to SSH."""
    identity = Path(identity_path)
    if not identity.is_absolute():
        raise ValidationError("builder identity must be an absolute path")
    try:
        metadata = identity.lstat()
    except OSError:
        raise ValidationError("builder identity is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("builder identity must be a direct regular file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValidationError("builder identity must not be group or world accessible")
    return identity


def _builder_ssh_argv(
    command: Sequence[str],
    address: str,
    known_hosts: Path,
    connect_timeout_seconds: int,
    remote_args: Sequence[str],
    identity_file: Path,
) -> tuple[str, ...]:
    if not 1 <= connect_timeout_seconds <= 120:
        raise ValueError("builder SSH connect timeout is invalid")
    host = str(ipaddress.ip_address(address))
    return (
        *_fixed_command(command, field_name="builder SSH command"),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout_seconds}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "IdentitiesOnly=yes",
        "-i",
        str(identity_file),
        "--",
        f"{OPERATOR_ACCOUNT_NAME}@{host}",
        *remote_args,
    )


def pin_builder_host_key(
    observation: BuilderObservation,
    known_hosts_path: str | Path,
    *,
    timeout_seconds: float,
    deadline: float | None = None,
    command_runner: Callable[..., Any] = run,
    pin_command: Sequence[str] = (),
) -> Path:
    if not observation.ready or observation.address is None:
        raise ApplicationError("builder is not ready for host-key verification")
    path = Path(known_hosts_path)
    ensure_private_directory(path.parent)
    if path.exists():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("builder known-hosts path must be a direct file")
    command = _fixed_command(pin_command, field_name="builder host-key command")
    _provider_result(
        command_runner,
        (*command, observation.server_name, observation.address, str(path)),
        timeout_seconds=_deadline_timeout(
            timeout_seconds, deadline, operation="builder host-key pinning"
        ),
        inherit_env=("HOME", "USER", *_PROVIDER_INHERIT_ENV),
    )
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ApplicationError("builder host-key command did not create a private pin")
    return path


def execute_builder_build(
    observation: BuilderObservation,
    source_directory: str | Path,
    recipe: Recipe,
    image_name: str,
    known_hosts_path: str | Path,
    identity_path: str | Path,
    *,
    source_limit: int,
    build_log_limit: int,
    timeout_seconds: float,
    connect_timeout_seconds: int,
    deadline: float | None = None,
    deadline_at: str | None = None,
    command_runner: Callable[..., Any] = run,
    ssh_command: Sequence[str] = DEFAULT_BUILDER_SSH_COMMAND,
    build_log_sink: BinaryIO | None = None,
) -> BuildExecution:
    if not observation.ready or observation.address is None:
        raise ApplicationError("builder is not ready")
    identity = _builder_identity(identity_path)
    if not _IMAGE_NAME.fullmatch(image_name):
        raise ValidationError("image name must be a lowercase registry repository without a tag")
    if isinstance(connect_timeout_seconds, bool) or not 1 <= connect_timeout_seconds <= 120:
        raise ValueError("builder SSH connect timeout is invalid")
    known_hosts = Path(known_hosts_path)
    metadata = known_hosts.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("builder known-hosts pin is invalid")
    operation_deadline = deadline if deadline is not None else time.monotonic() + timeout_seconds
    _deadline_timeout(timeout_seconds, operation_deadline, operation="builder execution")
    operation_deadline_at = deadline_at or _deadline_timestamp(operation_deadline)
    try:
        parsed_deadline = datetime.fromisoformat(operation_deadline_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValidationError("builder operation deadline is malformed") from None
    if parsed_deadline.tzinfo is None or parsed_deadline <= datetime.now(UTC):
        raise ValidationError("builder operation deadline must be in the future")
    archive, archive_sha = create_build_archive(
        source_directory, recipe, maximum_bytes=source_limit
    )
    receive_timeout = _deadline_timeout(
        timeout_seconds, operation_deadline, operation="source transfer"
    )
    receive_connect_timeout = max(1, min(connect_timeout_seconds, int(receive_timeout)))
    receive_argv = _builder_ssh_argv(
        ssh_command,
        observation.address,
        known_hosts,
        receive_connect_timeout,
        (
            _BUILDER_REMOTE_COMMAND,
            operation_deadline_at,
            "receive",
            observation.build_id,
            archive_sha,
            str(len(archive)),
        ),
        identity,
    )
    received = _provider_result(
        command_runner,
        receive_argv,
        timeout_seconds=receive_timeout,
        stdin=archive,
        stderr_limit=65_536,
        inherit_env=("HOME", "USER", "SSH_AUTH_SOCK"),
    )
    receipt = _json_object(received.stdout, field_name="builder archive receipt")
    if receipt != {"buildId": observation.build_id, "sha256": archive_sha}:
        raise ApplicationError("builder did not verify the transferred source archive")
    build_timeout = _deadline_timeout(
        timeout_seconds, operation_deadline, operation="BuildKit execution"
    )
    build_connect_timeout = max(1, min(connect_timeout_seconds, int(build_timeout)))
    build_argv = _builder_ssh_argv(
        ssh_command,
        observation.address,
        known_hosts,
        build_connect_timeout,
        (
            _BUILDER_REMOTE_COMMAND,
            operation_deadline_at,
            "build",
            observation.build_id,
            image_name,
        ),
        identity,
    )
    result = _provider_result(
        command_runner,
        build_argv,
        timeout_seconds=build_timeout,
        stdout_limit=_MAX_BUILD_METADATA,
        stderr_limit=build_log_limit,
        inherit_env=("HOME", "USER", "SSH_AUTH_SOCK"),
        allow_stderr_truncation=True,
        stderr_sink=build_log_sink,
    )
    parse_build_metadata(result.stdout)
    return BuildExecution(
        metadata=result.stdout,
        log=result.stderr,
        log_truncated=bool(result.stderr_truncated),
    )


def build_with_disposable_builder(
    *,
    build_id: str,
    source_directory: str | Path,
    recipe: Recipe,
    image_name: str,
    prefix: str,
    selected_builder_image_id: str,
    builder_flavor: str,
    known_hosts_directory: str | Path,
    identity_path: str | Path,
    source_limit: int = 52_428_800,
    build_log_limit: int = 10_485_760,
    timeout_seconds: float = 900,
    connect_timeout_seconds: int = 10,
    deadline: float | None = None,
    deadline_at: str | None = None,
    project_name: str | None = None,
    project_id: str | None = None,
    command_runner: Callable[..., Any] = run,
    builder_command: Sequence[str] = (),
    pin_command: Sequence[str] = (),
    ssh_command: Sequence[str] = DEFAULT_BUILDER_SSH_COMMAND,
    build_log_sink: BinaryIO | None = None,
) -> BuildResult:
    """Run the complete selected-image, pinned-host, single-use builder path."""
    identifier = uuid(build_id, field="build ID")
    _deadline_timeout(timeout_seconds, deadline, operation="builder operation")
    operation_deadline_at = deadline_at or _deadline_timestamp(
        deadline if deadline is not None else time.monotonic() + timeout_seconds
    )
    known_hosts_root = ensure_private_directory(known_hosts_directory)
    known_hosts = known_hosts_root / f"builder-{identifier}.known_hosts"
    primary_error: BaseException | None = None
    digest: str | None = None
    build_log = b""
    build_log_truncated = False
    try:
        observed = create_builder(
            identifier,
            prefix=prefix,
            selected_image_id=selected_builder_image_id,
            flavor_name=builder_flavor,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
            project_name=project_name,
            project_id=project_id,
            command_runner=command_runner,
            builder_command=builder_command,
        )
        pin_builder_host_key(
            observed,
            known_hosts,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
            command_runner=command_runner,
            pin_command=pin_command,
        )
        execution = execute_builder_build(
            observed,
            source_directory,
            recipe,
            image_name,
            known_hosts,
            identity_path,
            source_limit=source_limit,
            build_log_limit=build_log_limit,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
            deadline_at=operation_deadline_at,
            connect_timeout_seconds=connect_timeout_seconds,
            command_runner=command_runner,
            ssh_command=ssh_command,
            build_log_sink=build_log_sink,
        )
        digest = parse_build_metadata(execution.metadata)
        build_log = execution.log
        build_log_truncated = execution.log_truncated
    except BaseException as error:
        primary_error = error
    cleanup_error: BaseException | None = None
    try:
        delete_builder(
            identifier,
            prefix=prefix,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
            project_name=project_name,
            project_id=project_id,
            command_runner=command_runner,
            builder_command=builder_command,
        )
    except BaseException as error:
        cleanup_error = error
    finally:
        known_hosts.unlink(missing_ok=True)
    if cleanup_error is not None:
        if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
            raise cleanup_error
        if primary_error is not None:
            raise ApplicationError(
                "builder operation and server/port cleanup both failed"
            ) from None
        raise ApplicationError("builder server/port cleanup failed") from None
    if primary_error is not None:
        if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
            raise primary_error
        raise ApplicationError(
            "builder operation failed; server and port cleanup completed"
        ) from None
    assert digest is not None
    return BuildResult(
        build_id=identifier,
        image=f"{image_name}@{digest}",
        digest=digest,
        cleanup_confirmed=True,
        build_log=build_log,
        build_log_truncated=build_log_truncated,
    )


# Application worker lifecycle


def parse_worker_observation(
    payload: bytes | str,
    *,
    application_id: str,
    application_slug: str,
    prefix: str,
) -> WorkerObservation:
    identifier = uuid(application_id, field="application ID")
    app_slug = slug(application_slug)
    safe_prefix = _provider_name(prefix, field_name="platform prefix")
    short = identifier.replace("-", "")[:12]
    server_name = f"{safe_prefix}-worker-{short}"
    port_name = f"{server_name}-v4"
    value = _json_object(payload, field_name="worker observation")
    if value.keys() != {"applicationId", "slug", "server", "port", "ready"}:
        raise ApplicationError("worker observation fields were invalid")
    if value["applicationId"] != identifier or value["slug"] != app_slug:
        raise ApplicationError("worker observation identity did not match the request")
    server = _resource_mapping(value["server"], field_name="worker server")
    port = _resource_mapping(value["port"], field_name="worker port")
    ready = value["ready"]
    if not isinstance(ready, bool):
        raise ApplicationError("worker observation readiness was malformed")
    server_id = None
    image_id = None
    flavor_name = None
    if server is not None:
        if server.keys() != {
            "id",
            "name",
            "status",
            "imageId",
            "flavorName",
            "managedBy",
            "applicationId",
            "applicationSlug",
        }:
            raise ApplicationError("worker server evidence was malformed")
        server_id = _optional_uuid(server["id"], field_name="worker server UUID")
        image_id = _optional_uuid(server["imageId"], field_name="worker image UUID")
        flavor_name = _provider_name(server["flavorName"], field_name="worker flavor")
        if (
            server_id is None
            or image_id is None
            or server["name"] != server_name
            or not isinstance(server["status"], str)
            or server["managedBy"] != "platform"
            or server["applicationId"] != identifier
            or server["applicationSlug"] != app_slug
        ):
            raise ApplicationError("worker server identity did not match the request")
    port_id = None
    if port is not None:
        if port.keys() != {"id", "name", "deviceId", "address", "description"}:
            raise ApplicationError("worker port evidence was malformed")
        port_id = _optional_uuid(port["id"], field_name="worker port UUID")
        description = f"managed-by=platform;application-id={identifier};application-slug={app_slug}"
        if port_id is None or port["name"] != port_name or port["description"] != description:
            raise ApplicationError("worker port identity did not match the request")
        device_id = _optional_uuid(port["deviceId"], field_name="worker port device UUID")
        if device_id != server_id:
            raise ApplicationError("worker port was not attached to the observed server")
        try:
            ipaddress.ip_address(port["address"])
        except (TypeError, ValueError) as error:
            raise ApplicationError("worker port address was malformed") from error
    if (server is None) != (port is None):
        raise ApplicationError("worker server and fixed port evidence was incomplete")
    if ready and (server_id is None or port_id is None):
        raise ApplicationError("worker was reported ready without complete resources")
    return WorkerObservation(
        identifier,
        app_slug,
        server_id,
        server_name,
        port_id,
        port_name,
        image_id,
        flavor_name,
        ready,
    )


def observe_worker(
    application_id: str,
    application_slug: str,
    *,
    prefix: str,
    timeout_seconds: float,
    nomad_command: str | None = None,
    project_name: str | None = None,
    project_id: str | None = None,
    command_runner: Callable[..., Any] = run,
    worker_command: Sequence[str] = (),
) -> WorkerObservation:
    identifier = uuid(application_id, field="application ID")
    app_slug = slug(application_slug)
    command = _fixed_command(worker_command, field_name="worker command")
    environment = _project_environment(project_name, project_id)
    if nomad_command is not None:
        environment["NOMAD"] = _fixed_executable(nomad_command, field_name="Nomad command")
    result = _provider_result(
        command_runner,
        (*command, "show", identifier, app_slug),
        timeout_seconds=timeout_seconds,
        env=environment or None,
    )
    return parse_worker_observation(
        result.stdout,
        application_id=identifier,
        application_slug=app_slug,
        prefix=prefix,
    )


def create_worker(
    application_id: str,
    application_slug: str,
    *,
    prefix: str,
    selected_image_id: str | None,
    standard_flavor: str | None,
    nomad_command: str,
    timeout_seconds: float,
    project_name: str | None = None,
    project_id: str | None = None,
    command_runner: Callable[..., Any] = run,
    worker_command: Sequence[str] = (),
) -> WorkerObservation:
    identifier = uuid(application_id, field="application ID")
    app_slug = slug(application_slug)
    nomad = _fixed_executable(nomad_command, field_name="Nomad command")
    command = _fixed_command(worker_command, field_name="worker command")
    existing = observe_worker(
        identifier,
        app_slug,
        prefix=prefix,
        timeout_seconds=timeout_seconds,
        nomad_command=nomad,
        project_name=project_name,
        project_id=project_id,
        command_runner=command_runner,
        worker_command=command,
    )
    image_id: str | None = None
    flavor: str | None = None
    if existing.absent:
        image_id = uuid(selected_image_id, field="selected worker image UUID")
        flavor = _provider_name(standard_flavor, field_name="standard worker flavor")
        environment = {
            "IMAGE_NAME": image_id,
            "FLAVOR_NAME": flavor,
            "NOMAD": nomad,
            **_project_environment(project_name, project_id),
        }
    else:
        if existing.server_id is None or existing.port_id is None:
            raise ApplicationError(
                "existing worker resources were incomplete; replacement is required"
            )
        # Existing infrastructure is authoritative. Image selection applies only
        # when allocating a new worker; it must never relabel or mutate this VM.
        environment = {"NOMAD": nomad, **_project_environment(project_name, project_id)}
    _provider_result(
        command_runner,
        (*command, "create", identifier, app_slug),
        timeout_seconds=timeout_seconds,
        env=environment,
    )
    observed = observe_worker(
        identifier,
        app_slug,
        prefix=prefix,
        timeout_seconds=timeout_seconds,
        nomad_command=nomad,
        project_name=project_name,
        project_id=project_id,
        command_runner=command_runner,
        worker_command=command,
    )
    if not observed.ready:
        raise ApplicationError("worker readiness was not confirmed")
    if existing.absent and (observed.image_id != image_id or observed.flavor_name != flavor):
        raise ApplicationError("new worker did not match the selected image and standard flavor")
    if not existing.absent and (
        observed.server_id != existing.server_id
        or observed.port_id != existing.port_id
        or observed.image_id != existing.image_id
        or observed.flavor_name != existing.flavor_name
    ):
        raise ApplicationError("existing worker authoritative identity changed unexpectedly")
    return observed


def delete_worker(
    application_id: str,
    application_slug: str,
    *,
    prefix: str,
    timeout_seconds: float,
    project_name: str | None = None,
    project_id: str | None = None,
    command_runner: Callable[..., Any] = run,
    worker_command: Sequence[str] = (),
) -> WorkerObservation:
    identifier = uuid(application_id, field="application ID")
    app_slug = slug(application_slug)
    command = _fixed_command(worker_command, field_name="worker command")
    _provider_result(
        command_runner,
        (*command, "delete", identifier, app_slug),
        timeout_seconds=timeout_seconds,
        env=_project_environment(project_name, project_id) or None,
    )
    observed = observe_worker(
        identifier,
        app_slug,
        prefix=prefix,
        timeout_seconds=timeout_seconds,
        project_name=project_name,
        project_id=project_id,
        command_runner=command_runner,
        worker_command=command,
    )
    if not observed.absent:
        raise ApplicationError("worker server or port remained after cleanup")
    return observed


def deployment_recovery_action(phase: str, *, candidate_digest: str | None) -> str:
    """Map every durable deploy phase to one explicit, idempotent next action."""
    if phase not in _DEPLOY_PHASES:
        raise ApplicationError("deployment operation has an unknown recovery phase")
    if candidate_digest is not None:
        oci_digest_pin(candidate_digest, field="candidate digest")
    if phase in {"validated", "source_acquired"}:
        return "acquire_source"
    if phase in {"builder_creating", "builder_created", "source_transferred", "building"}:
        return "cleanup_builder_then_rebuild"
    if phase == "image_pushed":
        if candidate_digest is None:
            raise ApplicationError("image-pushed phase omitted its candidate digest")
        return "cleanup_builder_then_reconcile_worker"
    if phase in {"builder_cleaned", "worker_creating"}:
        return "reconcile_worker"
    if phase == "worker_ready":
        return "submit_job"
    if phase == "job_submitted":
        return "observe_health_or_remove_candidate"
    if phase == "deployment_healthy":
        return "accept_deployment"
    return "complete"


# Scheduler deployment and promotion


def _call_helper(
    helper_caller: Callable[..., Mapping[str, Any]],
    action: str,
    args: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    return helper_caller(action, args, timeout_seconds=timeout_seconds)


def check_public_health(
    application_slug: str,
    platform: PlatformConfig,
    path: str,
    *,
    timeout_seconds: float,
    preview: bool = False,
    expected_marker: str,
    http_caller: Callable[..., Any] = bounded_http,
) -> bool:
    """Check the canonical public HTTPS route without redirects or response bodies."""
    app_slug = slug(application_slug)
    checked_path = health_path(path)
    route = f"{app_slug}-preview" if preview else app_slug
    marker = uuid(expected_marker, field="expected route marker")
    url = f"https://{route}.{platform.domain}{checked_path}"
    result = http_caller(
        url,
        timeout_seconds=timeout_seconds,
        response_limit=4_096,
        allow_redirects=False,
    )
    return (
        isinstance(result.status, int)
        and 200 <= result.status < 300
        and result.headers.get(DEPLOYMENT_ROUTE_HEADER_LOWER) == marker
    )


def deploy_and_cleanup(
    application_slug: str,
    nomad_job: str,
    *,
    attempts: int = 90,
    poll_interval_seconds: float = 2,
    helper_timeout_seconds: float = 30,
    helper_caller: Callable[..., Mapping[str, Any]] = call_helper,
    public_health_check: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DeploymentResult:
    """Deploy, observe bounded health, and remove an unhealthy candidate."""
    app_slug = slug(application_slug)
    job = bounded_text(nomad_job, field="Nomad job", maximum=262_144)
    if not 1 <= attempts <= 300 or not 0 < poll_interval_seconds <= 30:
        raise ValueError("health observation bounds are invalid")
    candidate = nomad_candidate_identity(job)
    job_id = nomad_job_id(job, app_slug)
    deployed = _call_helper(
        helper_caller,
        "app.deploy",
        {"slug": app_slug, "job": job},
        timeout_seconds=helper_timeout_seconds,
    )
    if deployed.get("jobId") != job_id:
        raise ApplicationError("helper observed an unexpected deployment job")
    version = deployed.get("nomadVersion")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ApplicationError("helper returned an invalid Nomad version")
    if (
        deployed.get("candidateJobSha256") != candidate[0]
        or deployed.get("candidateImage") != candidate[1]
    ):
        raise ApplicationError("helper did not observe the exact deployment candidate")

    def health_args(
        observed_version: int,
        observed_candidate: tuple[str, str],
    ) -> dict[str, Any]:
        return {
            "slug": app_slug,
            "version": observed_version,
            "candidateJobSha256": observed_candidate[0],
            "candidateImage": observed_candidate[1],
            "jobId": job_id,
        }

    def exact_health(
        observation: Mapping[str, Any],
        observed_version: int,
        observed_candidate: tuple[str, str],
    ) -> bool:
        reported_version = observation.get("version")
        current_version = observation.get("currentVersion")
        allocations = observation.get("allocations")
        return (
            isinstance(reported_version, int)
            and not isinstance(reported_version, bool)
            and reported_version == observed_version
            and isinstance(current_version, int)
            and not isinstance(current_version, bool)
            and current_version == observed_version
            and observation.get("candidateJobSha256") == observed_candidate[0]
            and observation.get("candidateImage") == observed_candidate[1]
            and isinstance(allocations, int)
            and not isinstance(allocations, bool)
            and 0 <= allocations <= 128
        )

    failure: BaseException | None = None
    for observation_number in range(1, attempts + 1):
        try:
            observation = _call_helper(
                helper_caller,
                "app.health",
                health_args(version, candidate),
                timeout_seconds=helper_timeout_seconds,
            )
        except Exception as error:
            failure = error
            break
        healthy = observation.get("healthy")
        terminal = observation.get("terminal")
        if (
            not isinstance(healthy, bool)
            or not isinstance(terminal, bool)
            or not exact_health(observation, version, candidate)
        ):
            failure = ApplicationError("helper returned invalid exact-candidate health evidence")
            break
        if healthy:
            try:
                publicly_healthy = public_health_check is None or public_health_check()
            except Exception as error:
                failure = error
                publicly_healthy = False
            if publicly_healthy:
                return DeploymentResult(nomad_version=version, observations=observation_number)
        if terminal:
            break
        if observation_number < attempts:
            sleep(poll_interval_seconds)

    cleanup_succeeded = False
    cleanup_evidence: dict[str, Any] = {}
    try:
        removed = _call_helper(
            helper_caller,
            "app.remove",
            {
                "slug": app_slug,
                "jobId": job_id,
                "candidateJobSha256": candidate[0],
                "candidateImage": candidate[1],
            },
            timeout_seconds=helper_timeout_seconds,
        )
        cleanup_succeeded = removed.get("jobAbsent") is True
        cleanup_evidence = {
            "action": "remove-candidate",
            "confirmed": cleanup_succeeded,
            "terminal": "absent" if cleanup_succeeded else "unconfirmed",
            "jobAbsent": removed.get("jobAbsent"),
        }
    except Exception:
        cleanup_evidence = {
            "action": "remove-candidate",
            "confirmed": False,
            "terminal": "unconfirmed",
        }
    message = (
        "deployment health check failed; candidate removal completed"
        if cleanup_succeeded
        else "deployment health check failed; candidate removal also failed"
    )
    raise DeploymentFailed(
        message,
        cleanup_succeeded=cleanup_succeeded,
        cleanup_evidence=cleanup_evidence,
    ) from failure


def promote_candidate(
    application_slug: str,
    promoted_job: str,
    candidate_version: int,
    candidate_identity: tuple[str, str],
    predecessor_priority: int,
    *,
    helper_timeout_seconds: float = 30,
    helper_caller: Callable[..., Mapping[str, Any]] = call_helper,
) -> DeploymentResult:
    """Explicitly add the stable route to one exact healthy staged job."""
    app_slug = slug(application_slug)
    job = bounded_text(promoted_job, field="promoted Nomad job", maximum=262_144)
    job_id = nomad_job_id(job, app_slug)
    expected = nomad_candidate_identity(job)
    candidate_hash = sha256_hex(candidate_identity[0], field="candidate job SHA-256")
    if isinstance(predecessor_priority, bool) or not 0 <= predecessor_priority < 1_000_000_000:
        raise ValidationError("predecessor route priority is invalid")
    candidate_image = oci_digest_pin(candidate_identity[1], field="candidate image")
    if expected[1] != candidate_image:
        raise ValidationError("promoted route image does not match the candidate")
    result = _call_helper(
        helper_caller,
        "app.promote",
        {
            "slug": app_slug,
            "job": job,
            "candidateJobId": job_id,
            "candidateVersion": candidate_version,
            "candidateJobSha256": candidate_hash,
            "candidateImage": candidate_image,
            "routeMarker": nomad_route_marker(job),
            "predecessorPriority": predecessor_priority,
        },
        timeout_seconds=helper_timeout_seconds,
    )
    version = result.get("nomadVersion")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version < 0
        or result.get("jobId") != job_id
        or result.get("candidateJobSha256") != expected[0]
        or result.get("candidateImage") != expected[1]
    ):
        raise ApplicationError("helper did not confirm the exact route promotion")
    return DeploymentResult(version, 1)


def execute_deployment_workflow(
    connection: sqlite3.Connection,
    config: Config,
    state_directory: Path,
    spec: DeploymentSpec,
    *,
    deadline_at: str,
    deadline: float | None = None,
    verify_project: Callable[[], None],
    recover: Callable[[db.Operation], db.Operation | None],
    prepare_build: Callable[[db.Operation], DeploymentBuild],
    prepare_environment: Callable[[str, DeploymentBuild], dict[str, Any]],
    prepare_worker: Callable[[str, DeploymentBuild, dict[str, Any]], DeploymentWorker],
    deploy_and_accept: Callable[[str, DeploymentBuild, DeploymentWorker], DeploymentResult],
    create_attempt: Callable[[str], None],
    checkpoint_attempt: Callable[[str, BaseException], None],
    operation_id: str | None = None,
) -> tuple[str, DeploymentResult] | None:
    """Own the durable top-level deploy sequence outside CLI presentation.

    Provider work remains in bounded concrete callables, while operation
    identity, locking, recovery selection, and phase ordering live here.
    """
    identifier = uuid(spec.application_id, field="application ID")
    app_slug = slug(spec.application_slug)
    scope = f"app-{identifier}"
    lock_deadline = deadline
    if lock_deadline is None:
        try:
            parsed_deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            raise ApplicationError("deployment operation deadline is malformed") from None
        if parsed_deadline.tzinfo is None:
            raise ApplicationError("deployment operation deadline needs a timezone")
        remaining = (parsed_deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise ApplicationError("deployment operation deadline was reached")
        lock_deadline = time.monotonic() + remaining
    base_refs = {
        "application_id": identifier,
        "slug": app_slug,
        "repository": repository_url(spec.repository),
        "source_commit": commit(spec.source_commit),
        "requested_ref": spec.requested_ref,
        "configuration_revision": spec.configuration_revision,
        "configuration_sha256": sha256_hex(
            spec.configuration_sha256, field="configuration SHA-256"
        ),
    }
    with lock(state_directory, scope, deadline=lock_deadline):
        verify_project()
        if db.get_application(connection, identifier) is None:
            db.put_application(
                connection,
                application_id=identifier,
                application_slug=app_slug,
                repository_url=spec.repository,
                desired_running=True,
                url=f"https://{app_slug}.{config.platform.domain}",
                worker_flavor=spec.worker_flavor,
                scheduler_cpu_mhz=spec.cpu_mhz,
                scheduler_memory_mib=spec.memory_mib,
            )
        unfinished = db.get_unfinished_operation(connection, scope)
        if unfinished is not None:
            if unfinished.kind != "app.deploy" or any(
                unfinished.refs.get(key) != value for key, value in base_refs.items()
            ):
                raise db.UnfinishedOperationError(scope, unfinished.operation_id, unfinished.kind)
            # This attempt has its own deadline. Record it before recovering,
            # so the helper is not handed the spent deadline of the attempt
            # that stranded this operation.
            unfinished = db.renew_operation_deadline(
                connection, unfinished.operation_id, deadline_at
            )
            unfinished = recover(unfinished)
            if unfinished is None:
                return None
            operation_id = unfinished.operation_id
        else:
            operation_id = (
                str(uuid_module.uuid4())
                if operation_id is None
                else uuid(operation_id, field="deployment operation ID")
            )
            db.begin_operation(
                connection,
                operation_id=operation_id,
                kind="app.deploy",
                scope=scope,
                phase="validated",
                deadline_at=deadline_at,
                refs=base_refs,
            )
            unfinished = db.get_operation(connection, operation_id)
            assert unfinished is not None
            try:
                create_attempt(operation_id)
            except Exception as error:
                db.mark_failed(connection, operation_id, error, cleanup_state="not_required")
                raise
        try:
            build = prepare_build(unfinished)
            refs = prepare_environment(operation_id, build)
            worker = prepare_worker(operation_id, build, refs)
            result = deploy_and_accept(operation_id, build, worker)
        except Exception as error:
            current = db.get_operation(connection, operation_id)
            if current is not None and current.status == "running":
                db.mark_recovery_required(connection, operation_id, error)
                checkpoint_attempt(operation_id, error)
            raise
    return build.image, result


def accept_healthy_deployment(
    connection: sqlite3.Connection,
    acceptance: DeploymentAcceptance,
    *,
    helper_timeout_seconds: float,
    helper_caller: Callable[..., Mapping[str, Any]] = call_helper,
    public_health_check: Callable[[], bool],
) -> DeploymentResult:
    """Reobserve and persist one exact candidate through the shared acceptance gate.

    Both the normal path and ``deployment_healthy`` recovery call this function;
    a durable healthy checkpoint is evidence to re-check, never authorization to
    write accepted state without observing the exact live job and public route.
    """
    identifier = uuid(acceptance.application_id, field="application ID")
    app_slug = slug(acceptance.application_slug)
    expected_hash = sha256_hex(acceptance.nomad_job_sha256, field="Nomad job SHA-256")
    expected_image = oci_digest_pin(acceptance.image, field="accepted image")
    expected_job_id = nomad_job_id(acceptance.nomad_job, app_slug)
    version = acceptance.nomad_version
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValidationError("accepted Nomad version must be non-negative")
    observation = _call_helper(
        helper_caller,
        "app.health",
        {
            "slug": app_slug,
            "version": version,
            "candidateJobSha256": expected_hash,
            "candidateImage": expected_image,
            "jobId": expected_job_id,
        },
        timeout_seconds=helper_timeout_seconds,
    )
    reported_version = observation.get("version")
    current_version = observation.get("currentVersion")
    allocations = observation.get("allocations")
    if (
        isinstance(reported_version, bool)
        or not isinstance(reported_version, int)
        or reported_version != version
        or isinstance(current_version, bool)
        or not isinstance(current_version, int)
        or current_version != version
        or observation.get("candidateJobSha256") != expected_hash
        or observation.get("candidateImage") != expected_image
        or isinstance(allocations, bool)
        or not isinstance(allocations, int)
        or allocations != 1
        or observation.get("healthy") is not True
        or observation.get("terminal") is not False
    ):
        raise ApplicationError("exact deployment health could not be reobserved for acceptance")
    try:
        route_healthy = public_health_check()
    except Exception as error:
        raise ApplicationError("public deployment route could not be reobserved") from error
    if route_healthy is not True:
        raise ApplicationError("public deployment route was not healthy for acceptance")
    attempt = db.get_deployment_attempt(connection, acceptance.deployment_id)
    if attempt is None or attempt.application_id != identifier:
        raise ApplicationError("deployment attempt snapshot is missing for acceptance")
    db.put_application(
        connection,
        application_id=identifier,
        application_slug=app_slug,
        repository_url=acceptance.repository,
        desired_running=True,
        url=acceptance.public_url,
        worker_server_id=acceptance.worker_server_id,
        worker_server_name=acceptance.worker_server_name,
        worker_port_id=acceptance.worker_port_id,
        worker_port_name=acceptance.worker_port_name,
        worker_flavor=acceptance.worker_flavor,
        scheduler_cpu_mhz=acceptance.cpu_mhz,
        scheduler_memory_mib=acceptance.memory_mib,
    )
    db.checkpoint_deployment_attempt(
        connection,
        acceptance.deployment_id,
        status="succeeded",
        recipe_hash=acceptance.recipe_hash,
        image_digest=expected_image,
        nomad_job=acceptance.nomad_job,
        nomad_job_sha256=expected_hash,
        nomad_version=version,
        build_log_path=acceptance.build_log_path,
        cleanup_state="not_required",
    )
    return DeploymentResult(nomad_version=version, observations=1)


# Runtime environment, logs, and registry retention


def set_environment(
    application_slug: str,
    updates: Mapping[str, str],
    ownership: Mapping[str, str],
    *,
    timeout_seconds: float,
    helper_caller: Callable[..., Mapping[str, Any]] = call_helper,
) -> Mapping[str, Any]:
    validated_updates = {
        env_key(key): bounded_text(value, field=f"environment value for {key}", maximum=65_536)
        for key, value in updates.items()
    }
    return _call_helper(
        helper_caller,
        "app.env.set",
        {
            "slug": slug(application_slug),
            "updates": validated_updates,
            "ownership": dict(ownership),
        },
        timeout_seconds=timeout_seconds,
    )


def remove_environment(
    application_slug: str,
    keys: Sequence[str],
    ownership: Mapping[str, str],
    *,
    timeout_seconds: float,
    helper_caller: Callable[..., Mapping[str, Any]] = call_helper,
) -> Mapping[str, Any]:
    return _call_helper(
        helper_caller,
        "app.env.remove",
        {
            "slug": slug(application_slug),
            "keys": [env_key(key) for key in keys],
            "ownership": dict(ownership),
        },
        timeout_seconds=timeout_seconds,
    )


def list_environment(
    application_slug: str,
    *,
    timeout_seconds: float,
    helper_caller: Callable[..., Mapping[str, Any]] = call_helper,
) -> Mapping[str, Any]:
    return _call_helper(
        helper_caller,
        "app.env.list",
        {"slug": slug(application_slug)},
        timeout_seconds=timeout_seconds,
    )


def application_logs(
    application_slug: str,
    *,
    stderr: bool = False,
    lines: int = 200,
    follow: bool = False,
    timeout_seconds: float,
    helper_caller: Callable[..., Mapping[str, Any]] = call_helper,
) -> Mapping[str, Any]:
    if not isinstance(stderr, bool):
        raise ValidationError("log stream selector must be boolean")
    if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 2_000:
        raise ValidationError("log line count must be from 1 through 2000")
    if not isinstance(follow, bool):
        raise ValidationError("log follow selector must be boolean")
    args: dict[str, Any] = {"slug": slug(application_slug), "stderr": stderr, "lines": lines}
    if follow:
        args["follow"] = True
    return _call_helper(
        helper_caller,
        "app.logs",
        args,
        timeout_seconds=timeout_seconds,
    )


def delete_registry_manifest(
    application_slug: str,
    image_digest: str,
    *,
    timeout_seconds: float,
    referenced_images: Sequence[str] = (),
    command_runner: Callable[..., Any] = run,
    registry_command: Sequence[str] = (),
) -> bool:
    app_slug = slug(application_slug)
    digest = image_digest.rsplit("@", 1)[-1]
    if not _DIGEST.fullmatch(digest):
        raise ValidationError("registry manifest digest is malformed")
    referenced = {
        oci_digest_pin(image, field="referenced registry image").rsplit("@", 1)[-1]
        for image in referenced_images
    }
    if digest in referenced:
        raise ApplicationError("refusing to delete a referenced registry manifest")
    repository = f"projects/{app_slug}/app"
    command = _fixed_command(registry_command, field_name="registry command")
    result = _provider_result(
        command_runner,
        (*command, "delete", repository, digest),
        timeout_seconds=timeout_seconds,
    )
    evidence = _json_object(result.stdout, field_name="registry deletion evidence")
    if evidence != {"repository": repository, "digest": digest, "absent": True}:
        raise ApplicationError("registry did not confirm manifest absence")
    return True


def apply_registry_retention(
    application_slug: str,
    manifests_newest_first: Sequence[str],
    *,
    referenced_images: Sequence[str],
    prior_successful_to_keep: int = 5,
    timeout_seconds: float,
    command_runner: Callable[..., Any] = run,
    registry_command: Sequence[str] = (),
) -> RegistryRetentionResult:
    """Keep current/referenced manifests and five prior successes, deleting only the rest.

    ``manifests_newest_first`` must be the authoritative successful-manifest
    history, not an age-derived registry listing. Every caller-supplied current,
    accepted, or an unfinished candidate reference remains protected even when older than the
    retention window.
    """
    app_slug = slug(application_slug)
    if not 0 <= prior_successful_to_keep <= 100:
        raise ValueError("registry prior-success retention must be from 0 through 100")
    repository_prefix = f"projects/{app_slug}/app@"

    def checked(image: str, *, field_name: str) -> str:
        pin = oci_digest_pin(image, field=field_name)
        path = pin.split("/", 1)[-1]
        if not path.startswith(repository_prefix):
            raise ValidationError(f"{field_name} is outside the application repository")
        return pin

    history: list[str] = []
    for value in manifests_newest_first:
        pin = checked(value, field_name="registry history image")
        if pin not in history:
            history.append(pin)
    references = {
        checked(value, field_name="referenced registry image") for value in referenced_images
    }
    # The first history entry is current; retain it plus the configured number
    # of prior successes. Explicit references are authoritative at every age.
    protected = references | set(history[: prior_successful_to_keep + 1])
    deleted: list[str] = []
    for image in history:
        if image in protected:
            continue
        delete_registry_manifest(
            app_slug,
            image,
            timeout_seconds=timeout_seconds,
            referenced_images=tuple(protected),
            command_runner=command_runner,
            registry_command=registry_command,
        )
        deleted.append(image)
    return RegistryRetentionResult(tuple(sorted(protected)), tuple(deleted))
