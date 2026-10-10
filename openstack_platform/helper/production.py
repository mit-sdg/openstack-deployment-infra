"""Concrete, lazily initialized protocol-v1 handlers for the admin host.

Importing this module performs no I/O. Each request opens only the trusted
clients needed by its action and closes them before returning. This keeps the
release smoke gate meaningful without requiring live credentials.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import shutil
import ssl
import stat
import subprocess
import tempfile
import time
import urllib.parse
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from .. import durable
from ..config import PlatformConfig, RuntimeImages, load_platform
from ..contracts import (
    CONTROLLER_BACKUP_DIRECTORY,
    GARAGE_RPC_PORT,
    GARAGE_S3_PORT,
    MONGODB_PORT,
    NOMAD_HTTP_PORT,
    POSTGRES_PORT,
    REGISTRY_PORT,
)
from ..controller import application_runtime as application
from ..controller.application_models import Manifest
from ..controller.deployment_config import branch_name, parse_configuration, validate_checkout
from ..controller.nomad_jobs import deployment_worker_ids
from ..log_timestamps import TimestampedBuildLog
from ..runtime import (
    CommandFailure,
    bounded_http,
    child_environment,
    ensure_private_directory,
    run,
)
from ..runtime_versions import RuntimeVersionError
from ..validation import (
    ValidationError,
    bounded_text,
    commit,
    flavor_reference,
    repository_url,
    slug,
    uuid,
)
from . import application_actions as app_actions
from . import storage as storage_actions
from .main import Handler, HelperActionError, backup_handler
from .nomad import NomadClient
from .runtime_images import RuntimeLookupError, resolve_runtime

APP_ACTIONS = (
    "app.build",
    "app.build.cleanup",
    "app.build.logs",
    "app.builder.delete",
    "app.deploy",
    "app.env.list",
    "app.env.remove",
    "app.env.set",
    "app.health",
    "app.logs",
    "app.manifest.delete",
    "app.manifest.retain",
    "app.manifest.verify",
    "app.promote",
    "app.remove",
    "app.restart",
    "app.source.check",
    "app.source.commits",
    "app.source.key",
    "app.source.preflight",
    "app.startup",
    "app.stop",
    "app.worker.capacity",
    "app.worker.create",
    "app.worker.delete",
    "app.worker.observe",
)
_PROVIDER_APP_ACTIONS = frozenset(
    {
        "app.build",
        "app.build.cleanup",
        "app.build.logs",
        "app.builder.delete",
        "app.manifest.delete",
        "app.manifest.retain",
        "app.manifest.verify",
        "app.source.check",
        "app.source.commits",
        "app.source.preflight",
        "app.source.key",
        "app.worker.capacity",
        "app.worker.create",
        "app.worker.delete",
        "app.worker.observe",
    }
)
_PER_RESOURCE_STORAGE_ACTIONS = tuple(
    f"storage.{resource_type}.{operation}"
    for resource_type in ("mongo", "postgres", "s3")
    for operation in ("create", "observe", "remove", "rotate", "verify", "limits", "usage")
)
STORAGE_ACTIONS = (
    *_PER_RESOURCE_STORAGE_ACTIONS,
    "storage.host.observe",
    "storage.backup.ensure",
    "storage.instances.network",
    "storage.instances.migrate",
    "storage.instances.abort",
    "storage.instances.available",
)
ACTION_MANIFEST = tuple(sorted(("backup.accept", *APP_ACTIONS, *STORAGE_ACTIONS)))


@dataclass(frozen=True, slots=True)
class HelperRuntime:
    """Validated live inventory and the helper paths derived from it."""

    platform_path: Path
    platform: PlatformConfig
    root: Path
    admin_state: Path
    backups: Path
    data: Path
    diagnostic_directory: Path


def _deployment_path(platform: PlatformConfig, name: str) -> Path:
    try:
        value = platform.get(f"paths.{name}")
    except (KeyError, TypeError) as error:
        raise ValidationError(f"paths.{name} is unavailable") from error
    if not isinstance(value, str) or len(value) > 256 or "\x00" in value:
        raise ValidationError(f"paths.{name} must be a canonical absolute path")
    path = Path(value)
    if not path.is_absolute() or path != Path(os.path.normpath(value)) or path == Path("/"):
        raise ValidationError(f"paths.{name} must be a canonical absolute path")
    return path


def helper_runtime() -> HelperRuntime:
    """Load the launcher-selected live config through the shared strict loader."""
    configured = os.environ.get("PLATFORM_CONFIG")
    if not configured:
        raise ValidationError("PLATFORM_CONFIG must select the live helper inventory")
    platform_path = Path(configured)
    if not platform_path.is_absolute():
        raise ValidationError("PLATFORM_CONFIG must be an absolute path")
    platform = load_platform(platform_path)
    root = _deployment_path(platform, "root")
    admin_state = _deployment_path(platform, "adminState")
    backups = _deployment_path(platform, "backups")
    data = _deployment_path(platform, "data")
    if len({root, admin_state, backups, data}) != 4:
        raise ValidationError("helper deployment paths must be distinct")
    return HelperRuntime(
        platform_path=platform_path,
        platform=platform,
        root=root,
        admin_state=admin_state,
        backups=backups,
        data=data,
        diagnostic_directory=admin_state / "controller/helper-diagnostics",
    )


def _nomad_secrets(runtime: HelperRuntime) -> Path:
    return runtime.root / "secrets/nomad-cli"


def _read_environment(path: Path, *, maximum_bytes: int = 65_536) -> dict[str, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise HelperActionError(
            "DEPENDENCY_UNAVAILABLE", "trusted helper secrets are unavailable"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise HelperActionError("DEPENDENCY_UNAVAILABLE", "trusted helper secrets are invalid")
        raw = b""
        while chunk := os.read(descriptor, min(16_384, maximum_bytes + 1 - len(raw))):
            raw += chunk
            if len(raw) > maximum_bytes:
                raise HelperActionError(
                    "DEPENDENCY_UNAVAILABLE", "trusted helper secrets are invalid"
                )
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HelperActionError(
            "DEPENDENCY_UNAVAILABLE", "trusted helper secrets are invalid"
        ) from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise HelperActionError("DEPENDENCY_UNAVAILABLE", "trusted helper secrets are invalid")
        key, value = line.split("=", 1)
        if not key or key in values or "\x00" in value:
            raise HelperActionError("DEPENDENCY_UNAVAILABLE", "trusted helper secrets are invalid")
        values[key] = value
    return values


def _tls_context(runtime: HelperRuntime | None = None) -> ssl.SSLContext:
    selected = helper_runtime() if runtime is None else runtime
    secrets = _nomad_secrets(selected)
    context = ssl.create_default_context(cafile=str(secrets / "internal-ca.pem"))
    context.load_cert_chain(
        str(secrets / "nomad-cli.pem"),
        str(secrets / "nomad-cli-key.pem"),
    )
    return context


def _nomad_client(runtime: HelperRuntime | None = None) -> NomadClient:
    selected = helper_runtime() if runtime is None else runtime
    token = _read_environment(selected.root / "secrets/nomad-tokens.env").get(
        "NOMAD_CONTROLLER_TOKEN"
    )
    if not token:
        raise HelperActionError("DEPENDENCY_UNAVAILABLE", "Nomad credentials are unavailable")
    return NomadClient(
        f"https://127.0.0.1:{NOMAD_HTTP_PORT}",
        token=token,
        ssl_context=_tls_context(selected),
        timeout_seconds=20,
    )


def _exact_args(args: Mapping[str, Any], expected: set[str], action: str) -> None:
    if args.keys() != expected:
        raise HelperActionError("INVALID_ARGS", f"{action} arguments are invalid")


def _positive_int(value: object, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise HelperActionError("INVALID_ARGS", f"{field} is invalid")
    return value


def _operation_deadline(value: object) -> tuple[str, float]:
    if not isinstance(value, str) or len(value) > 64:
        raise HelperActionError("INVALID_ARGS", "build operation deadline is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise HelperActionError("INVALID_ARGS", "build operation deadline is malformed") from None
    if parsed.tzinfo is None:
        raise HelperActionError("INVALID_ARGS", "build operation deadline needs a timezone")
    remaining = (parsed - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise HelperActionError("DEADLINE_EXPIRED", "build operation deadline was reached")
    return value, time.monotonic() + remaining


def _worker_result(observed: application.WorkerObservation) -> Mapping[str, Any]:
    result = {
        "applicationId": observed.application_id,
        "slug": observed.application_slug,
        "serverId": observed.server_id,
        "serverName": observed.server_name,
        "portId": observed.port_id,
        "portName": observed.port_name,
        "imageId": observed.image_id,
        "flavorName": observed.flavor_name,
        "ready": observed.ready,
        "absent": observed.absent,
    }

    if observed.address is not None:
        result["address"] = observed.address
    return result


def _build_log_paths(runtime: HelperRuntime, app_slug: str, build_id: str) -> tuple[Path, Path]:
    controller = ensure_private_directory(runtime.admin_state / "controller", create=True)
    root = ensure_private_directory(controller / "build-logs", create=True)
    directory = ensure_private_directory(root / slug(app_slug), create=True)
    identifier = uuid(build_id, field="build ID")
    return directory / f"{identifier}.log", directory / f"{identifier}.state"


SOURCE_KEY = "id_ed25519"
# Deploy-key repository reads; the controller (30 s), broker and web (35 s)
# wait a little longer each, so a slow but successful read still arrives.
SOURCE_READ_SECONDS = 25


def _source_key_directory(runtime: HelperRuntime, app_slug: str) -> Path:
    """Per-app deploy keys, beside build logs and outside the controller's state."""
    controller = ensure_private_directory(runtime.admin_state / "controller", create=True)
    return ensure_private_directory(controller / "source-keys", create=True) / slug(app_slug)


def _source_key_file(runtime: HelperRuntime, app_slug: str) -> Path | None:
    """The app's private deploy key, if it has one."""
    path = _source_key_directory(runtime, app_slug) / SOURCE_KEY
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise HelperActionError("INVALID_STATE", "deploy key is not a private direct file")
    return path


def _source_public_key(runtime: HelperRuntime, app_slug: str) -> dict[str, str] | None:
    if _source_key_file(runtime, app_slug) is None:
        return None
    path = _source_key_directory(runtime, app_slug) / f"{SOURCE_KEY}.pub"
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            raise HelperActionError("INVALID_STATE", "deploy public key is malformed")
        parts = os.read(descriptor, 4096).decode("ascii").split()
    finally:
        os.close(descriptor)
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise HelperActionError("INVALID_STATE", "deploy public key is malformed")
    blob = base64.b64decode(parts[1], validate=True)
    fingerprint = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return {
        "publicKey": f"{parts[0]} {parts[1]}",
        "fingerprint": f"SHA256:{fingerprint}",
        "createdAt": datetime.fromtimestamp(metadata.st_mtime, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


def _create_source_key(runtime: HelperRuntime, app_slug: str, *, replace: bool) -> None:
    """Make a new ed25519 deploy key; a replacement takes effect all at once."""
    directory = _source_key_directory(runtime, app_slug)
    if not replace and _source_key_file(runtime, app_slug) is not None:
        return
    staged = Path(tempfile.mkdtemp(prefix=".new-", dir=directory.parent))
    try:
        # runtime.run refuses empty arguments, and an unencrypted key needs -N "".
        # The argv is fixed; output is discarded and the call is bounded.
        subprocess.run(
            (
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"{runtime.platform.prefix} {app_slug} deploy key",
                "-f",
                str(staged / SOURCE_KEY),
            ),
            check=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_environment(),
        )
        os.chmod(staged / SOURCE_KEY, 0o600)
        os.chmod(staged / f"{SOURCE_KEY}.pub", 0o600)
        retired = None
        if directory.exists():
            retired = directory.parent / f".old-{uuid_module.uuid4().hex}"
            os.rename(directory, retired)
        os.rename(staged, directory)
        if retired is not None:
            shutil.rmtree(retired)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _remove_source_key(runtime: HelperRuntime, app_slug: str) -> None:
    """Retire the whole key directory at once; an app without a key is done.

    Backups skip ``.old-*``, so they see either the full pair or no key.
    """
    directory = _source_key_directory(runtime, app_slug)
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise HelperActionError("INVALID_STATE", "deploy key directory is not a direct directory")
    retired = directory.parent / f".old-{uuid_module.uuid4().hex}"
    os.rename(directory, retired)
    shutil.rmtree(retired)


def _write_build_log_state(path: Path, state: str) -> None:
    if state not in {"running", "complete", "failed"}:
        raise ValueError("build log state is invalid")
    try:
        durable.atomic_write(
            path,
            (state + "\n").encode("ascii"),
            mode=0o600,
            maximum_bytes=16,
        )
    except durable.DurableReplaceError as error:
        raise HelperActionError(
            "INVALID_STATE", "build log state could not be committed"
        ) from error


def _read_build_log(args: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_args(args, {"buildId", "slug", "lines", "offset"}, "app.build.logs")
    build_id = uuid(args["buildId"], field="build ID")
    app_slug = slug(args["slug"])
    lines = _positive_int(args["lines"], field="line count", maximum=2000)
    offset = args["offset"]
    if offset is not None and (
        isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
    ):
        raise HelperActionError("INVALID_ARGS", "build log offset is invalid")
    log_path, state_path = _build_log_paths(helper_runtime(), app_slug, build_id)
    try:
        metadata = log_path.lstat()
    except FileNotFoundError:
        return {"buildId": build_id, "exists": False, "state": "unknown", "text": "", "size": 0}
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise HelperActionError("INVALID_STATE", "build log path is not a private direct file")
    size = metadata.st_size
    if offset is None:
        payload = log_path.read_bytes()[-524_288:]
        text = "\n".join(payload.decode("utf-8", errors="replace").splitlines()[-lines:])
        if text:
            text += "\n"
    else:
        with log_path.open("rb") as stream:
            stream.seek(min(offset, size))
            payload = stream.read(524_288)
        text = payload.decode("utf-8", errors="replace")
    try:
        state_metadata = state_path.lstat()
        if not stat.S_ISREG(state_metadata.st_mode) or stat.S_IMODE(state_metadata.st_mode) & 0o077:
            raise HelperActionError("INVALID_STATE", "build log state is not a private direct file")
        state = state_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        state = "unknown"
    if state not in {"running", "complete", "failed"}:
        state = "unknown"
    return {
        "buildId": build_id,
        "exists": True,
        "state": state,
        "text": text,
        "size": size,
        "nextOffset": (size if offset is None else min(offset, size) + len(payload)),
    }


def _build_application(args: Mapping[str, Any]) -> Mapping[str, Any]:
    action = "app.build"
    _exact_args(
        args,
        {
            "buildId",
            "slug",
            "repository",
            "requestedRef",
            "commit",
            "configurationRevision",
            "configuration",
            "builderImageId",
            "builderFlavor",
            "runtimeImages",
            "sourceLimit",
            "buildLogLimit",
            "connectSeconds",
            "deadlineAt",
        },
        action,
    )
    build_id = uuid(args["buildId"], field="build ID")
    builder_flavor = flavor_reference(args["builderFlavor"])
    app_slug = slug(args["slug"])
    branch_name(args["requestedRef"])
    revision = args["configurationRevision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValidationError("configuration revision must be a non-negative integer")
    configuration = parse_configuration(args["configuration"])
    runtime_images = args["runtimeImages"]
    if not isinstance(runtime_images, dict) or runtime_images.keys() != {"bun", "node"}:
        raise HelperActionError("INVALID_ARGS", "runtime image pins are invalid")
    images = RuntimeImages(
        bounded_text(runtime_images["bun"], field="Bun runtime image", maximum=512),
        bounded_text(runtime_images["node"], field="Node runtime image", maximum=512),
    )
    source_limit = _positive_int(args["sourceLimit"], field="source limit", maximum=1_073_741_824)
    build_log_limit = _positive_int(
        args["buildLogLimit"], field="build log limit", maximum=104_857_600
    )
    connect_seconds = _positive_int(args["connectSeconds"], field="connect timeout", maximum=120)
    deadline_at, operation_deadline = _operation_deadline(args["deadlineAt"])
    runtime = helper_runtime()
    platform = runtime.platform
    log_path, state_path = _build_log_paths(runtime, app_slug, build_id)
    if log_path.exists():
        metadata = log_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise HelperActionError("INVALID_STATE", "build log path is not a private direct file")
    _write_build_log_state(state_path, "running")
    try:
        with (
            log_path.open("wb") as raw_log,
            TimestampedBuildLog(raw_log, build_log_limit) as build_log,
        ):
            os.chmod(log_path, 0o600)
            marker = f"--- build {build_id} started ---\n".encode()
            build_log.write(marker[:build_log_limit])
            build_log.flush()
            streamed_log_limit = max(0, build_log_limit - build_log.tell())
            with tempfile.TemporaryDirectory(prefix="platform-source-") as directory:
                source = Path(directory) / "source"

                def note(text: str) -> None:
                    room = build_log_limit - build_log.tell()
                    if room > 0:
                        build_log.write(text.encode()[:room])
                        build_log.flush()

                def fetch(ssh_key: Path | None = None) -> None:
                    application.acquire_github_commit(
                        args["repository"],
                        args["commit"],
                        source,
                        maximum_bytes=source_limit,
                        timeout_seconds=300,
                        deadline=operation_deadline,
                        ssh_key=ssh_key,
                    )

                # Public repositories need no key. A private one fails over
                # HTTPS quickly, then the app's deploy key is tried over SSH.
                try:
                    fetch()
                except CommandFailure:
                    key = _source_key_file(runtime, app_slug)
                    if key is None:
                        note(
                            "Couldn't fetch this commit from GitHub. If the repository is "
                            "private, add the app's deploy key (Settings) to it on GitHub.\n"
                        )
                        raise HelperActionError(
                            "SOURCE_REJECTED", "repository or commit could not be fetched"
                        ) from None
                    note("The public fetch failed; using the app's deploy key.\n")
                    try:
                        fetch(key)
                    except CommandFailure:
                        note(
                            "The deploy key couldn't fetch this commit either. Check that the "
                            "key is added to this repository on GitHub and the commit is pushed.\n"
                        )
                        raise HelperActionError(
                            "SOURCE_REJECTED", "repository or commit could not be fetched"
                        ) from None
                    streamed_log_limit = max(0, build_log_limit - build_log.tell())
                try:
                    request = validate_checkout(configuration, source)
                    resolved = resolve_runtime(
                        configuration.runtime,
                        request,
                        getattr(images, configuration.runtime),
                    )
                except RuntimeVersionError as error:
                    note(f"{error}\n")
                    raise
                except RuntimeLookupError as error:
                    note(f"{error}\n")
                    raise HelperActionError(
                        "RUNTIME_UNAVAILABLE", "runtime versions could not be looked up"
                    ) from None
                note(f"{resolved.describe()}\n")
                streamed_log_limit = max(0, build_log_limit - build_log.tell())
                manifest = Manifest(
                    configuration.runtime,
                    configuration.packages,
                    configuration.build_script,
                    configuration.start_script,
                    configuration.port,
                    configuration.health_path,
                )
                recipe = application.generate_recipe(manifest, resolved.runtime_images(images))
                result = application.build_with_disposable_builder(
                    builder_command=application.provider_command(platform, "builder"),
                    pin_command=application.provider_command(platform, "pin-builder-host-key"),
                    build_id=build_id,
                    source_directory=source,
                    recipe=recipe,
                    image_name=f"{platform.get('addresses.storage')}:{REGISTRY_PORT}/projects/{app_slug}/app",
                    prefix=platform.prefix,
                    selected_builder_image_id=args["builderImageId"],
                    builder_flavor=builder_flavor,
                    known_hosts_directory=Path(directory) / "known-hosts",
                    identity_path=application.builder_identity_path(platform),
                    source_limit=source_limit,
                    build_log_limit=streamed_log_limit,
                    timeout_seconds=900,
                    deadline=operation_deadline,
                    deadline_at=deadline_at,
                    connect_timeout_seconds=connect_seconds,
                    project_name=platform.project_name,
                    project_id=platform.project_id,
                    build_log_sink=cast(BinaryIO, build_log),
                )
    except (ValidationError, application.BuildRejected):
        _write_build_log_state(state_path, "failed")
        raise HelperActionError(
            "BUILD_REJECTED", "application source or build was rejected"
        ) from None
    except BaseException:
        _write_build_log_state(state_path, "failed")
        raise
    _write_build_log_state(state_path, "complete")
    # Protocol v1 is bounded to 1 MiB. Preserve a useful staff-only tail while
    # keeping source/build output out of errors and operation records.
    with log_path.open("rb") as recorded:
        recorded.seek(max(0, log_path.stat().st_size - 524_288))
        log = recorded.read(524_288)
    return {
        "buildId": result.build_id,
        "image": result.image,
        "recipeHash": recipe.sha256,
        "runtime": resolved.evidence(),
        "log": log.decode("utf-8", errors="replace"),
        "logTruncated": result.build_log_truncated
        or log_path.stat().st_size >= build_log_limit
        or log_path.stat().st_size > len(log),
        "builderAbsent": result.cleanup_confirmed,
    }


def _provider_app(action: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
    if action == "app.build":
        return _build_application(args)
    if action == "app.build.logs":
        return _read_build_log(args)
    runtime = helper_runtime()
    platform = runtime.platform
    if action == "app.source.key":
        _exact_args(args, {"slug", "mode"}, action)
        app_slug = slug(args["slug"])
        if args["mode"] not in {"read", "create", "replace", "delete"}:
            raise ValidationError("deploy key mode must be read, create, replace or delete")
        if args["mode"] == "delete":
            _remove_source_key(runtime, app_slug)
        elif args["mode"] != "read":
            _create_source_key(runtime, app_slug, replace=args["mode"] == "replace")
        public = _source_public_key(runtime, app_slug)
        return {"slug": app_slug, "present": public is not None, **(public or {})}
    if action in {"app.source.commits", "app.source.preflight"}:
        expected = (
            {"slug", "repository", "branch"}
            if action == "app.source.commits"
            else {"slug", "repository", "commit", "configuration"}
        )
        _exact_args(args, expected, action)
        key = _source_key_file(runtime, slug(args["slug"]))
        if key is None:
            return {"keyPresent": False}
        try:
            items = (
                application.recent_github_commits(
                    repository_url(args["repository"]),
                    branch_name(args["branch"]),
                    key,
                    timeout_seconds=SOURCE_READ_SECONDS,
                )
                if action == "app.source.commits"
                else application.check_github_checkout(
                    repository_url(args["repository"]),
                    commit(args["commit"]),
                    args["configuration"],
                    key,
                    timeout_seconds=SOURCE_READ_SECONDS,
                )
            )
        except (application.ApplicationError, CommandFailure, OSError, ValueError):
            raise HelperActionError(
                "SOURCE_UNAVAILABLE", "Could not read the repository with its deploy key"
            ) from None
        return {"keyPresent": True, "items": items}
    if action == "app.source.check":
        _exact_args(args, {"slug", "repository", "branch"}, action)
        key = _source_key_file(runtime, slug(args["slug"]))
        if key is None:
            return {"keyPresent": False}
        return {
            "keyPresent": True,
            **application.check_github_access(
                repository_url(args["repository"]),
                branch_name(args["branch"]),
                key,
                timeout_seconds=30,
            ),
        }
    if action == "app.build.cleanup":
        _exact_args(args, {"buildId", "slug"}, action)
        build_id = uuid(args["buildId"], field="build ID")
        app_slug = slug(args["slug"])
        builder = application.delete_builder(
            build_id,
            prefix=platform.prefix,
            builder_command=application.provider_command(platform, "builder"),
            timeout_seconds=120,
            project_name=platform.project_name,
            project_id=platform.project_id,
        )
        if builder.absent is not True:
            raise HelperActionError(
                "CLEANUP_UNCONFIRMED", "exact builder absence was not confirmed"
            )
        application.confirm_build_manifest_absent(
            app_slug,
            build_id,
            registry_command=(
                "/run/current-system/sw/bin/python",
                str(runtime.root / "infra/registry/delete_manifest.py"),
            ),
            timeout_seconds=30,
        )
        return {
            "buildId": build_id,
            "slug": app_slug,
            "builderAbsent": True,
            "artifactAbsent": True,
        }
    if action == "app.builder.delete":
        _exact_args(args, {"buildId"}, action)
        builder = application.delete_builder(
            args["buildId"],
            prefix=platform.prefix,
            builder_command=application.provider_command(platform, "builder"),
            timeout_seconds=120,
            project_name=platform.project_name,
            project_id=platform.project_id,
        )
        return {"buildId": builder.build_id, "absent": builder.absent}
    if action in {
        "app.worker.create",
        "app.worker.delete",
        "app.worker.observe",
        "app.worker.capacity",
    }:
        retained_port = None
        if "retainedPort" in args:
            from .. import fixed_ip

            retained_port = fixed_ip.validate_request(
                args["retainedPort"],
                platform,
                uuid(args.get("applicationId"), field="worker slot UUID"),
                slug(args.get("slug")),
            )
            if action == "app.worker.delete" and args.get("single") is not True:
                raise ValidationError("retained port cleanup requires an exact single worker slot")
            provider = fixed_ip.Provider(
                platform,
                deadline=time.monotonic() + 120,
                executable=application.provider_command(platform, "openstack")[0],
            )
            value = provider.show(retained_port["port_id"])
            device = value.get("device_id")
            if not isinstance(device, str):
                raise ValidationError("retained port attachment evidence is malformed")
            if device:
                uuid(device, field="retained port attached server UUID")
            provider.check(value, retained_port, device_id=device)
            args = {key: value for key, value in args.items() if key != "retainedPort"}
        worker_options: dict[str, Any] = (
            {"retained_port": retained_port} if retained_port is not None else {}
        )
        expected = {"applicationId", "slug", "workerImageId", "standardFlavor"}
        accepted_server_id = None
        if action == "app.worker.capacity" and "acceptedServerId" in args:
            accepted_server_id = uuid(args["acceptedServerId"], field="accepted worker server UUID")
        if action == "app.worker.delete":
            if args.keys() not in ({"applicationId", "slug"}, {"applicationId", "slug", "single"}):
                raise HelperActionError("INVALID_ARGS", "app.worker.delete arguments are invalid")
            if not isinstance(args.get("single", False), bool):
                raise ValidationError("single-worker selector must be boolean")
        else:
            if action == "app.worker.create" and "flavorId" in args:
                expected.add("flavorId")
            _exact_args(
                args,
                expected
                if action == "app.worker.create"
                else {"applicationId", "slug"}
                | ({"acceptedServerId"} if accepted_server_id is not None else set()),
                action,
            )
        if action == "app.worker.create":
            worker = application.create_worker(
                args["applicationId"],
                args["slug"],
                **worker_options,
                prefix=platform.prefix,
                worker_command=application.provider_command(platform, "worker"),
                selected_image_id=args["workerImageId"],
                standard_flavor=args["standardFlavor"],
                flavor_id=args.get("flavorId"),
                nomad_command=application.provider_command(platform, "nomad")[0],
                timeout_seconds=900,
                project_name=platform.project_name,
                project_id=platform.project_id,
            )
        elif action == "app.worker.delete":
            identities = (
                (args["applicationId"],)
                if args.get("single", False)
                else (
                    args["applicationId"],
                    *deployment_worker_ids(args["applicationId"]),
                )
            )
            observations = [
                application.delete_worker(
                    identity,
                    args["slug"],
                    **worker_options,
                    prefix=platform.prefix,
                    worker_command=application.provider_command(platform, "worker"),
                    timeout_seconds=900,
                    project_name=platform.project_name,
                    project_id=platform.project_id,
                )
                for identity in identities
            ]
            if any(not item.absent for item in observations):
                raise application.ApplicationError("worker slot cleanup was not confirmed")
            worker = observations[0]
        else:
            worker = application.observe_worker(
                args["applicationId"],
                args["slug"],
                **worker_options,
                prefix=platform.prefix,
                worker_command=application.provider_command(platform, "worker"),
                timeout_seconds=120,
                nomad_command=application.provider_command(platform, "nomad")[0],
                project_name=platform.project_name,
                project_id=platform.project_id,
            )
        if action == "app.worker.capacity":
            from .worker_capacity import observe_capacity

            # Console history can expire after acceptance. For that exact server
            # only, require fresh provider liveness plus the full Nomad check
            # below instead. New/unaccepted workers still need bootstrap proof.
            ready = (
                worker.ready
                if accepted_server_id is None
                else worker.server_id == accepted_server_id and worker.provider_active is True
            )
            if not ready or worker.server_id is None:
                raise ValidationError("worker is not ready for capacity observation")
            capacity = observe_capacity(
                platform,
                worker.application_id,
                worker.application_slug,
                worker.server_name,
                nomad_command=application.provider_command(platform, "nomad")[0],
            )
            return {**_worker_result(worker), **capacity, "ready": True}
        return _worker_result(worker)
    if action == "app.manifest.verify":
        from .registry_artifact import verify_image

        _exact_args(args, {"slug", "image"}, action)
        password = _read_environment(runtime.root / "secrets/storage-bootstrap.env").get(
            "REGISTRY_BUILDER_PASSWORD"
        )
        if not password:
            raise HelperActionError(
                "DEPENDENCY_UNAVAILABLE", "registry credentials are unavailable"
            )
        authorization = "Basic " + base64.b64encode(f"builder:{password}".encode()).decode()
        return verify_image(
            f"{platform.get('addresses.storage')}:{REGISTRY_PORT}",
            args["slug"],
            args["image"],
            authorization=authorization,
            ssl_context=ssl.create_default_context(
                cafile=str(_nomad_secrets(runtime) / "internal-ca.pem")
            ),
        )
    registry_command = (
        "/run/current-system/sw/bin/python3",
        str(runtime.root / "infra/registry/delete_manifest.py"),
    )
    if action == "app.manifest.delete":
        _exact_args(args, {"slug", "image", "references"}, action)
        references = args["references"]
        if (
            not isinstance(references, list)
            or len(references) > 128
            or any(not isinstance(item, str) for item in references)
        ):
            raise HelperActionError("INVALID_ARGS", "registry references are invalid")
        absent = application.delete_registry_manifest(
            args["slug"],
            args["image"],
            timeout_seconds=120,
            referenced_images=references,
            registry_command=registry_command,
        )
        return {"slug": slug(args["slug"]), "absent": absent}
    if action == "app.manifest.retain":
        _exact_args(args, {"slug", "history", "references"}, action)
        history, references = args["history"], args["references"]
        if (
            not isinstance(history, list)
            or not isinstance(references, list)
            or len(history) > 1_000
            or len(references) > 1_000
            or any(not isinstance(item, str) for item in [*history, *references])
        ):
            raise HelperActionError("INVALID_ARGS", "registry retention arguments are invalid")
        retained = application.apply_registry_retention(
            args["slug"],
            history,
            referenced_images=references,
            timeout_seconds=120,
            registry_command=registry_command,
        )
        return {
            "slug": slug(args["slug"]),
            "protected": list(retained.protected),
            "deleted": list(retained.deleted),
        }
    raise HelperActionError("UNKNOWN_ACTION", "helper action is not registered")


class _GarageAdmin:
    __slots__ = ("base", "token", "context")

    def __init__(self, base: str, token: str, context: ssl.SSLContext) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.context = context

    def request(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(dict(query))
        data = None if body is None else json.dumps(dict(body), separators=(",", ":")).encode()
        try:
            response = bounded_http(
                url,
                method="GET" if body is None else "POST",
                data=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout_seconds=20,
                response_limit=1_048_576,
                ssl_context=self.context,
            )
        except RuntimeError as error:
            raise RuntimeError("Garage request failed") from error
        try:
            return None if not response.body else json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Garage response was malformed JSON") from error


def _storage_handlers(
    action: str,
    instance: Mapping[str, Any] | None = None,
    *,
    workload_id: str | None = None,
    application_slug: str = "bootstrap",
) -> tuple[dict[str, Handler], tuple[Any, ...]]:
    parts = action.split(".")
    if len(parts) != 3 or parts[0] != "storage" or action not in _PER_RESOURCE_STORAGE_ACTIONS:
        raise HelperActionError("UNKNOWN_ACTION", "storage helper action is not registered")
    resource_type, operation = parts[1], parts[2]
    runtime = helper_runtime()
    platform = runtime.platform
    host = platform.get("addresses.storage")
    if not isinstance(host, str):
        raise HelperActionError("DEPENDENCY_UNAVAILABLE", "storage inventory is invalid")
    if resource_type == "s3":
        # Apps get no path-style setting: S3 clients address buckets by path
        # only because the endpoint host is an IP address.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise HelperActionError(
                "DEPENDENCY_UNAVAILABLE", "S3 endpoint host must be an IP address"
            ) from None
    if resource_type != "s3":
        storage_actions._TRUSTED_HOSTS.set((host,))
        host = platform.get("internalNames.storage")
    ca = str(_nomad_secrets(runtime) / "internal-ca.pem")
    secrets = _read_environment(runtime.root / "secrets/storage-bootstrap.env")
    if instance is not None:
        if resource_type == "postgres":
            secrets["POSTGRES_PASSWORD"] = instance["adminPassword"]
            storage_actions._PORT_CONTEXT.set((instance["port"], MONGODB_PORT))
        else:
            secrets["MONGO_PASSWORD"] = instance["adminPassword"]
            storage_actions._PORT_CONTEXT.set((POSTGRES_PORT, instance["port"]))
    from .instances import connect_ready
    from .nomad import WorkloadVariables

    nomad = WorkloadVariables(_nomad_client(runtime), application_slug, workload_id)
    clients: list[Any] = []
    postgres: Any = None
    mongo: Any = None
    garage: Any = None

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise HelperActionError("DEPENDENCY_UNAVAILABLE", "storage backend is unavailable")

    postgres_connect: Any = unavailable
    mongo_connect: Any = unavailable
    s3_connect: Any = unavailable
    try:
        if resource_type == "postgres":
            try:
                import psycopg
            except ImportError as error:
                raise HelperActionError(
                    "DEPENDENCY_UNAVAILABLE", "PostgreSQL library is unavailable"
                ) from error

            def connect_postgres(**kwargs: Any) -> Any:
                kwargs["sslrootcert"] = ca
                kwargs["connect_timeout"] = 10
                kwargs["autocommit"] = True
                return psycopg.connect(**kwargs)

            postgres_connect = connect_postgres
            if operation in {"create", "remove", "rotate", "limits", "usage", "verify"}:
                postgres = connect_ready(
                    lambda: psycopg.connect(
                        host=host,
                        port=storage_actions.postgres_port(),
                        dbname="platform",
                        user="platform_admin",
                        password=secrets["POSTGRES_PASSWORD"],
                        sslmode="verify-full",
                        sslrootcert=ca,
                        connect_timeout=10,
                        autocommit=True,
                    )
                )
                clients.append(postgres)
        elif resource_type == "mongo":
            try:
                from pymongo import MongoClient
            except ImportError as error:
                raise HelperActionError(
                    "DEPENDENCY_UNAVAILABLE", "MongoDB library is unavailable"
                ) from error

            def connect_mongo(*, uri: str) -> Any:
                parsed = urllib.parse.urlsplit(uri)
                query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                query = [(key, value) for key, value in query if key.lower() != "tlscafile"]
                safe_uri = urllib.parse.urlunsplit(
                    parsed._replace(query=urllib.parse.urlencode(query))
                )
                return MongoClient(safe_uri, tlsCAFile=ca, serverSelectionTimeoutMS=10_000)

            mongo_connect = connect_mongo
            if operation in {"create", "remove", "rotate", "limits", "usage", "verify"}:
                mongo = MongoClient(
                    host,
                    storage_actions.mongo_port(),
                    username="platform_admin",
                    password=secrets["MONGO_PASSWORD"],
                    authSource="admin",
                    tls=True,
                    tlsCAFile=ca,
                    serverSelectionTimeoutMS=10_000,
                )
                if instance is not None:
                    connect_ready(lambda: mongo.admin.command("ping"))
                clients.append(mongo)
        else:
            try:
                import boto3
                from botocore.config import Config as BotoConfig
            except ImportError as error:
                raise HelperActionError(
                    "DEPENDENCY_UNAVAILABLE", "S3 library is unavailable"
                ) from error

            def connect_s3(access_key: str, secret_key: str) -> Any:
                return boto3.client(
                    "s3",
                    endpoint_url=f"https://{host}:{GARAGE_S3_PORT}",
                    region_name="garage",
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    verify=ca,
                    config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
                )

            s3_connect = connect_s3
            if operation in {"create", "observe", "remove", "rotate", "verify", "limits", "usage"}:
                context = ssl.create_default_context(cafile=ca)
                garage = _GarageAdmin(
                    f"https://{host}:{GARAGE_RPC_PORT}/v2", secrets["GARAGE_ADMIN_TOKEN"], context
                )

        nomad_command = (application.provider_command(platform, "nomad")[0],)

        def observe_evidence(
            _application_id: str,
            application_slug: str,
            _resource_type: str,
            modify_index: int,
        ) -> storage_actions.RotationEvidence:
            if workload_id is None:
                current = nomad.read_variable(f"nomad/jobs/{application_slug}")
                return storage_actions.RotationEvidence(
                    current.modify_index == modify_index,
                    current.modify_index,
                    public_healthy=current.modify_index == modify_index,
                )
            status = app_actions._status_or_absent(
                workload_id,
                command_runner=run,
                nomad_command=nomad_command,
                timeout_seconds=20,
                response_limit=1_048_576,
            )
            if status is None:
                # Storage must be provisionable after `app create`, before the
                # first deploy. With no consumers to restart, an exact Variable
                # read is the complete bootstrap acceptance evidence.
                current = nomad.read_variable(f"nomad/jobs/{application_slug}")
                observed = current.modify_index == modify_index
                return storage_actions.RotationEvidence(
                    observed, current.modify_index, public_healthy=observed
                )
            baseline = app_actions._allocations(
                workload_id,
                command_runner=run,
                nomad_command=nomad_command,
                timeout_seconds=20,
                response_limit=1_048_576,
            )
            baseline_tokens = {app_actions._allocation_token(item) for item in baseline}
            run(
                (*nomad_command, "job", "restart", "-yes", workload_id),
                timeout_seconds=30,
                stdout_limit=65_536,
                stderr_limit=65_536,
            )
            for attempt in range(60):
                current_job = app_actions._inspected_candidate(
                    workload_id,
                    command_runner=run,
                    nomad_command=nomad_command,
                    timeout_seconds=20,
                    response_limit=1_048_576,
                )
                if current_job is not None:
                    version = current_job[0]
                    allocations = app_actions._allocations(
                        workload_id,
                        command_runner=run,
                        nomad_command=nomad_command,
                        timeout_seconds=20,
                        response_limit=1_048_576,
                    )
                    healthy = app_actions._healthy_allocations(allocations, version)
                    fresh = any(
                        app_actions._allocation_token(item) not in baseline_tokens
                        for item in healthy
                    )
                    current = nomad.read_variable(f"nomad/jobs/{application_slug}")
                    if fresh and current.modify_index == modify_index:
                        try:
                            publicly_healthy = app_actions._public_health_from_job(
                                application_slug,
                                trusted_domain=platform.domain,
                                command_runner=run,
                                nomad_command=nomad_command,
                                timeout_seconds=20,
                                response_limit=1_048_576,
                            )
                        except Exception:
                            publicly_healthy = False
                        if publicly_healthy:
                            return storage_actions.RotationEvidence(
                                True, current.modify_index, public_healthy=True
                            )
                if attempt + 1 < 60:
                    time.sleep(2)
            return storage_actions.RotationEvidence(False, modify_index, public_healthy=False)

        handlers = storage_actions.handlers(
            postgres_admin=postgres,
            postgres_connect=postgres_connect,
            mongo_admin=mongo,
            mongo_connect=mongo_connect,
            garage_admin=garage,
            s3_connect=s3_connect,
            nomad=nomad,
            storage_host=host,
            s3_endpoint=f"https://{host}:{GARAGE_S3_PORT}",
            prefix=platform.prefix,
            observe_evidence=observe_evidence,
        )
        return handlers, tuple(clients)
    except BaseException:
        for client in reversed(clients):
            try:
                client.close()
            except Exception:
                pass
        raise


def _lazy_app(action: str) -> Handler:
    def handle(args: Mapping[str, Any]) -> Mapping[str, Any]:
        if action in _PROVIDER_APP_ACTIONS:
            return _provider_app(action, args)
        runtime = helper_runtime()
        platform = runtime.platform
        nomad_command = (application.provider_command(platform, "nomad")[0],)
        return app_actions.handlers(
            _nomad_client(runtime),
            nomad_command=nomad_command,
            trusted_domain=platform.domain,
            public_health_check=lambda application_slug: app_actions._public_health_from_job(
                application_slug,
                trusted_domain=platform.domain,
                command_runner=run,
                nomad_command=nomad_command,
                timeout_seconds=20,
                response_limit=1_048_576,
            ),
        )[action](args)

    return handle


def _storage_host_observe(args: Mapping[str, Any]) -> Mapping[str, Any]:
    if args:
        raise HelperActionError("INVALID_ARGS", "host observation arguments must be empty")
    import psycopg
    from pymongo import MongoClient

    from .storage_limits import MONGO_BLOCKED_ROLE, host_projection

    runtime = helper_runtime()
    host = runtime.platform.get("addresses.storage")
    ca = str(_nomad_secrets(runtime) / "internal-ca.pem")
    secrets = _read_environment(runtime.root / "secrets/storage-bootstrap.env")
    response = bounded_http(
        f"https://{host}:{GARAGE_RPC_PORT}/platform/host-status",
        headers={"Authorization": f"Bearer {secrets['GARAGE_ADMIN_TOKEN']}"},
        ssl_context=ssl.create_default_context(cafile=ca),
        timeout_seconds=10,
        response_limit=65_536,
    )
    raw = json.loads(response.body)
    names = {
        f"{runtime.platform.namespace}-{kind}"
        for kind in ("postgres", "mongodb", "garage", "registry")
    }
    instance_client = _instance_client()
    inventory = instance_client.call("list")
    instances = inventory["items"]
    for instance in instances:
        names.add(
            f"{runtime.platform.namespace}-db-{uuid(instance['instanceId'], field='instance ID')}"
        )
    # The two read-only samples can straddle instance creation/deletion. Trust
    # only the configured namespace plus a canonical UUID for extra containers.
    for item in raw.get("containers", []):
        name = item.get("name", "")
        prefix = f"{runtime.platform.namespace}-db-"
        if isinstance(name, str) and name.startswith(prefix):
            uuid(name[len(prefix) :], field="host instance ID")
            names.add(name)
    actual_names = {item.get("name") for item in raw.get("containers", [])}
    for missing in sorted(names - actual_names):
        raw["containers"].append(
            {"name": missing, "usedBytes": 0, "limitBytes": 1, "available": False}
        )
    result = host_projection(raw, names)
    result["instanceMemoryBudgetBytes"] = inventory["instanceMemoryBudgetBytes"]
    result["postgresConnections"] = {"current": 0, "limit": 100}
    try:
        with psycopg.connect(
            host=host,
            port=POSTGRES_PORT,
            dbname="platform",
            user="platform_admin",
            password=secrets["POSTGRES_PASSWORD"],
            sslmode="verify-full",
            sslrootcert=ca,
            connect_timeout=8,
            options="-c statement_timeout=5000",
        ) as postgres:
            row = postgres.execute(
                "SELECT (SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend'), current_setting('max_connections')::int"
            ).fetchone()
            if row is None:
                raise HelperActionError(
                    "PROVIDER_RESPONSE_INVALID", "PostgreSQL connections unavailable"
                )
            result["postgresConnections"] = {"current": row[0], "limit": row[1]}
    except Exception:
        pass
    result["mongoConnections"] = {"current": 0, "limit": 800}
    result["writeBlockedResources"] = 0
    try:
        mongo: MongoClient[dict[str, Any]] = MongoClient(
            host,
            MONGODB_PORT,
            username="platform_admin",
            password=secrets["MONGO_PASSWORD"],
            authSource="admin",
            tls=True,
            tlsCAFile=ca,
            serverSelectionTimeoutMS=8_000,
            socketTimeoutMS=8_000,
        )
        try:
            connections = mongo.admin.command("serverStatus")["connections"]
            result["mongoConnections"] = {
                "current": connections["current"],
                "limit": connections["current"] + connections["available"],
            }
            users = mongo.admin.command("usersInfo", {"forAllDBs": True})["users"]
            result["writeBlockedResources"] = len(
                {
                    user["db"]
                    for user in users
                    if any(role.get("role") == MONGO_BLOCKED_ROLE for role in user.get("roles", []))
                }
            )
        finally:
            mongo.close()
    except Exception:
        pass

    def probe(instance: Mapping[str, Any]) -> dict[str, Any]:
        identifier = instance["instanceId"]
        kind = instance["type"]
        count = 0
        limit = instance["quotas"]["connections"] + (5 if kind == "postgres" else 10)
        available = False
        blocked_count = 0
        try:
            credentials = instance_client.call("credentials", identifier)
            if kind == "postgres":
                with psycopg.connect(
                    host=host,
                    port=credentials["port"],
                    dbname="platform",
                    user="platform_admin",
                    password=credentials["adminPassword"],
                    sslmode="verify-full",
                    sslrootcert=ca,
                    connect_timeout=3,
                    options="-c statement_timeout=3000",
                ) as db_client:
                    row = db_client.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend'"
                    ).fetchone()
                    count = int(row[0]) if row is not None else 0
            else:
                db_mongo: MongoClient[dict[str, Any]] = MongoClient(
                    host,
                    credentials["port"],
                    username="platform_admin",
                    password=credentials["adminPassword"],
                    authSource="admin",
                    tls=True,
                    tlsCAFile=ca,
                    serverSelectionTimeoutMS=3000,
                    socketTimeoutMS=3000,
                )
                try:
                    count = int(db_mongo.admin.command("serverStatus")["connections"]["current"])
                    users = db_mongo.admin.command("usersInfo", {"forAllDBs": True})["users"]
                    blocked_count = sum(
                        1
                        for user in users
                        if any(
                            role.get("role") == MONGO_BLOCKED_ROLE for role in user.get("roles", [])
                        )
                    )
                finally:
                    db_mongo.close()
            available = True
        except Exception:
            pass
        return {
            "instanceId": identifier,
            "type": kind,
            "currentConnections": count if available else None,
            "connectionLimit": limit,
            "available": available,
            "blockedCount": blocked_count,
        }

    from concurrent.futures import ThreadPoolExecutor

    projections = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for observed_instance in pool.map(probe, instances):
            kind = observed_instance["type"]
            result[f"{kind}Connections"]["current"] += observed_instance["currentConnections"] or 0
            result[f"{kind}Connections"]["limit"] += observed_instance["connectionLimit"]
            result["writeBlockedResources"] += observed_instance.pop("blockedCount")
            projections.append(observed_instance)
    result["instances"] = projections
    result["measuredAt"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return result


def _instance_client() -> Any:
    from .instances import InstanceClient

    runtime = helper_runtime()
    secrets = _read_environment(runtime.root / "secrets/storage-bootstrap.env")
    ca = str(_nomad_secrets(runtime) / "internal-ca.pem")
    return InstanceClient(
        f"https://{runtime.platform.get('addresses.storage')}:{GARAGE_RPC_PORT}",
        secrets["GARAGE_ADMIN_TOKEN"],
        ssl.create_default_context(cafile=ca),
    )


def _instance_network(args: Mapping[str, Any]) -> Mapping[str, Any]:
    _exact_args(
        args, {"applicationId", "instanceIds", "addresses", "mode"}, "storage.instances.network"
    )
    owner = uuid(args["applicationId"], field="application ID")
    client = _instance_client()
    for identifier in args["instanceIds"]:
        client.call(
            "allow", identifier, applicationId=owner, addresses=args["addresses"], mode=args["mode"]
        )
    return {"applied": True}


def _lazy_storage(action: str) -> Handler:
    def handle(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        if action == "storage.backup.ensure":
            _exact_args(raw, {"type", "database", "maxAgeMinutes"}, action)
            from ..database_backups import ensure_backup

            return ensure_backup(
                helper_runtime().platform.document,
                raw["type"],
                raw["database"],
                raw["maxAgeMinutes"],
            )
        if action == "storage.instances.available":
            _exact_args(raw, set(), action)
            return dict(_instance_client().call("ping"))
        if action == "storage.host.observe":
            return _storage_host_observe(raw)
        if action == "storage.instances.network":
            return _instance_network(raw)
        if action == "storage.instances.abort":
            from .instance_migration import abort

            return abort(
                raw,
                client=_instance_client(),
                runtime=helper_runtime(),
                nomad=_nomad_client(helper_runtime()),
            )
        if action == "storage.instances.migrate":
            from .instance_migration import migrate

            return migrate(
                raw,
                client=_instance_client(),
                runtime=helper_runtime(),
                nomad=_nomad_client(helper_runtime()),
            )
        from .instances import metadata
        from .nomad import WorkloadVariables

        (
            args,
            identifier,
            quotas,
            workers,
            resource_id,
            reservations,
            retained,
            workload_id,
            staged,
        ) = metadata(raw)
        kind, operation = action.split(".")[1:]
        if kind == "mongo" and quotas is not None:
            storage_actions._MONGO_POOL.set(min(10, quotas["connections"]))
        if kind == "s3" and operation in {"create", "limits"}:
            _instance_client().call("reserve", resource_id, reservations=reservations)
        if staged is not None and operation == "limits" and args["quotas"] != quotas:
            _instance_client().call(
                "limits", staged, quotas=args["quotas"], reservations=reservations
            )
        client = _instance_client() if identifier is not None else None
        instance = None
        if client is not None:
            if operation == "create":
                addresses = []
                for worker in workers:
                    if retained is not None and worker == retained["slotId"]:
                        addresses.append(retained["address"])
                        continue
                    observed = _provider_app(
                        "app.worker.observe",
                        {"applicationId": worker, "slug": args["applicationSlug"]},
                    )
                    if observed.get("address") is not None:
                        addresses.append(observed["address"])
                client.call(
                    "create",
                    identifier,
                    applicationId=args["applicationId"],
                    type=kind,
                    quotas=quotas,
                    allowIps=addresses,
                    reservations=reservations,
                )
            elif operation == "limits" and args["quotas"] != quotas:
                client.call("limits", identifier, quotas=args["quotas"], reservations=reservations)
            instance = client.call("credentials", identifier)
            if instance.get("absent") is True and operation == "remove":
                storage_actions._common(args, kind)
                if args["preflight"]:
                    return {"preflightAccepted": True}
                update = storage_actions._remove_environment(
                    WorkloadVariables(
                        _nomad_client(helper_runtime()), args["applicationSlug"], workload_id
                    ),
                    args["applicationSlug"],
                    kind,
                )
                client.call("remove", identifier, deleteData=True)
                return storage_actions._remove_result(kind, update)
        handlers, clients = _storage_handlers(
            action, instance, workload_id=workload_id, application_slug=args["applicationSlug"]
        )
        try:
            result = dict(handlers[action](args))
            if (
                staged is not None
                and operation == "remove"
                and args["preflight"] is False
                and result.get("confirmedAbsent") is True
            ):
                _instance_client().call("remove", staged, deleteData=True)
            if client is not None:
                if (
                    operation == "remove"
                    and args["preflight"] is False
                    and result.get("confirmedAbsent") is True
                ):
                    client.call("remove", identifier, deleteData=True)
                else:
                    observed = client.call("observe", identifier)
                    result["instancePort"] = observed["port"]
                    if isinstance(result.get("usage"), dict):
                        result["usage"].update(
                            {
                                key: observed[key]
                                for key in ("instanceMemoryBytes", "cpuTimeMilliseconds")
                            }
                        )
            return result
        finally:
            for backend in clients:
                try:
                    backend.close()
                except Exception:
                    pass

    return handle


def _accept_backup(args: Mapping[str, Any]) -> Mapping[str, Any]:
    runtime = helper_runtime()
    return backup_handler(
        staging_directory=runtime.backups / CONTROLLER_BACKUP_DIRECTORY / ".staging",
        backup_directory=runtime.backups / CONTROLLER_BACKUP_DIRECTORY,
    )(args)


def production_handlers() -> dict[str, Handler]:
    """Return the complete protocol-v1 map without opening live dependencies."""
    handlers: dict[str, Handler] = {"backup.accept": _accept_backup}
    handlers.update({action: _lazy_app(action) for action in APP_ACTIONS})
    handlers.update({action: _lazy_storage(action) for action in STORAGE_ACTIONS})
    if tuple(sorted(handlers)) != ACTION_MANIFEST:
        raise RuntimeError("production helper action map is incomplete")
    return handlers
