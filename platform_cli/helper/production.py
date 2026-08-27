"""Concrete, lazily initialized protocol-v1 handlers for the admin host.

Importing this module performs no I/O. Each request opens only the trusted
clients needed by its action and closes them before returning. This keeps the
release smoke gate meaningful without requiring live credentials.
"""

from __future__ import annotations

import json
import os
import ssl
import stat
import tempfile
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import app as application
from ..config import PlatformConfig, RuntimeImages, load_platform
from ..deployment_config import branch_name, parse_configuration, validate_checkout
from ..runtime import bounded_http, ensure_private_directory, run
from ..validation import (
    ValidationError,
    bounded_text,
    slug,
    uuid,
)
from . import app as app_actions
from . import storage as storage_actions
from .main import Handler, HelperActionError, backup_handler
from .nomad import NomadClient

APP_ACTIONS = (
    "app.build",
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
    "app.promote",
    "app.remove",
    "app.worker.create",
    "app.worker.delete",
    "app.worker.observe",
)
_PROVIDER_APP_ACTIONS = frozenset(
    {
        "app.build",
        "app.build.logs",
        "app.builder.delete",
        "app.manifest.delete",
        "app.manifest.retain",
        "app.worker.create",
        "app.worker.delete",
        "app.worker.observe",
    }
)
_PER_RESOURCE_STORAGE_ACTIONS = tuple(
    f"storage.{resource_type}.{operation}"
    for resource_type in ("mongo", "postgres", "s3")
    for operation in ("create", "observe", "remove", "rotate", "verify")
)
STORAGE_ACTIONS = _PER_RESOURCE_STORAGE_ACTIONS
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
        "https://127.0.0.1:4646",
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
    return {
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


def _build_log_paths(runtime: HelperRuntime, app_slug: str, build_id: str) -> tuple[Path, Path]:
    controller = ensure_private_directory(runtime.admin_state / "controller", create=True)
    root = ensure_private_directory(controller / "build-logs", create=True)
    directory = ensure_private_directory(root / slug(app_slug), create=True)
    identifier = uuid(build_id, field="build ID")
    return directory / f"{identifier}.log", directory / f"{identifier}.state"


def _write_build_log_state(path: Path, state: str) -> None:
    if state not in {"running", "complete", "failed"}:
        raise ValueError("build log state is invalid")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="ascii") as stream:
            os.chmod(temporary, 0o600)
            stream.write(state + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
            "runtimeImages",
            "sourceLimit",
            "buildLogLimit",
            "connectSeconds",
            "deadlineAt",
        },
        action,
    )
    build_id = uuid(args["buildId"], field="build ID")
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
        with log_path.open("wb") as build_log:
            os.chmod(log_path, 0o600)
            marker = f"--- build {build_id} started ---\n".encode()
            build_log.write(marker[:build_log_limit])
            build_log.flush()
            streamed_log_limit = max(0, build_log_limit - build_log.tell())
            with tempfile.TemporaryDirectory(prefix="m1-source-") as directory:
                source = Path(directory) / "source"
                application.acquire_github_commit(
                    args["repository"],
                    args["commit"],
                    source,
                    maximum_bytes=source_limit,
                    timeout_seconds=300,
                    deadline=operation_deadline,
                )
                validate_checkout(configuration, source)
                manifest = application.Manifest(
                    configuration.runtime,
                    configuration.packages,
                    configuration.build_script,
                    configuration.start_script,
                    configuration.port,
                    configuration.health_path,
                )
                recipe = application.generate_recipe(manifest, images)
                result = application.build_with_disposable_builder(
                    builder_command=application.provider_command(platform, "builder"),
                    pin_command=application.provider_command(platform, "pin-builder-host-key"),
                    build_id=build_id,
                    source_directory=source,
                    recipe=recipe,
                    image_name=f"{platform.get('addresses.storage')}:5000/projects/{app_slug}/app",
                    prefix=platform.prefix,
                    selected_builder_image_id=args["builderImageId"],
                    builder_flavor=platform.get("flavors.builder"),
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
                    build_log_sink=build_log,
                )
    except BaseException:
        _write_build_log_state(state_path, "failed")
        raise
    _write_build_log_state(state_path, "complete")
    # Protocol v1 is bounded to 1 MiB. Preserve a useful staff-only tail while
    # keeping source/build output out of errors and operation records.
    log = result.build_log[-524_288:]
    return {
        "buildId": result.build_id,
        "image": result.image,
        "recipeHash": recipe.sha256,
        "log": log.decode("utf-8", errors="replace"),
        "logTruncated": result.build_log_truncated or len(log) != len(result.build_log),
        "builderAbsent": result.cleanup_confirmed,
    }


def _provider_app(action: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
    if action == "app.build":
        return _build_application(args)
    if action == "app.build.logs":
        return _read_build_log(args)
    runtime = helper_runtime()
    platform = runtime.platform
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
    if action in {"app.worker.create", "app.worker.delete", "app.worker.observe"}:
        expected = {"applicationId", "slug", "workerImageId", "standardFlavor"}
        if action == "app.worker.delete":
            if args.keys() not in ({"applicationId", "slug"}, {"applicationId", "slug", "single"}):
                raise HelperActionError("INVALID_ARGS", "app.worker.delete arguments are invalid")
            if not isinstance(args.get("single", False), bool):
                raise ValidationError("single-worker selector must be boolean")
        else:
            _exact_args(
                args,
                expected if action == "app.worker.create" else {"applicationId", "slug"},
                action,
            )
        if action == "app.worker.create":
            worker = application.create_worker(
                args["applicationId"],
                args["slug"],
                prefix=platform.prefix,
                worker_command=application.provider_command(platform, "worker"),
                selected_image_id=args["workerImageId"],
                standard_flavor=args["standardFlavor"],
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
                    *application.deployment_worker_ids(args["applicationId"]),
                )
            )
            observations = [
                application.delete_worker(
                    identity,
                    args["slug"],
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
                prefix=platform.prefix,
                worker_command=application.provider_command(platform, "worker"),
                timeout_seconds=120,
                nomad_command=application.provider_command(platform, "nomad")[0],
                project_name=platform.project_name,
                project_id=platform.project_id,
            )
        return _worker_result(worker)
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


def _storage_handlers(action: str) -> tuple[dict[str, Handler], tuple[Any, ...]]:
    parts = action.split(".")
    if len(parts) != 3 or parts[0] != "storage" or action not in _PER_RESOURCE_STORAGE_ACTIONS:
        raise HelperActionError("UNKNOWN_ACTION", "storage helper action is not registered")
    resource_type, operation = parts[1], parts[2]
    runtime = helper_runtime()
    platform = runtime.platform
    host = platform.get("addresses.storage")
    if not isinstance(host, str):
        raise HelperActionError("DEPENDENCY_UNAVAILABLE", "storage inventory is invalid")
    ca = str(_nomad_secrets(runtime) / "internal-ca.pem")
    secrets = _read_environment(runtime.root / "secrets/storage-bootstrap.env")
    nomad = _nomad_client(runtime)
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
            if operation in {"create", "remove", "rotate"}:
                postgres = psycopg.connect(
                    host=host,
                    port=5432,
                    dbname="platform",
                    user="platform_admin",
                    password=secrets["POSTGRES_PASSWORD"],
                    sslmode="verify-full",
                    sslrootcert=ca,
                    connect_timeout=10,
                    autocommit=True,
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
            if operation in {"create", "remove", "rotate"}:
                mongo = MongoClient(
                    host,
                    27017,
                    username="platform_admin",
                    password=secrets["MONGO_PASSWORD"],
                    authSource="admin",
                    tls=True,
                    tlsCAFile=ca,
                    serverSelectionTimeoutMS=10_000,
                )
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
                    endpoint_url=f"https://{host}:9000",
                    region_name="garage",
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    verify=ca,
                    config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
                )

            s3_connect = connect_s3
            if operation in {"create", "observe", "remove", "rotate", "verify"}:
                context = ssl.create_default_context(cafile=ca)
                garage = _GarageAdmin(
                    f"https://{host}:3903/v2", secrets["GARAGE_ADMIN_TOKEN"], context
                )

        nomad_command = (application.provider_command(platform, "nomad")[0],)

        def observe_evidence(
            _application_id: str,
            application_slug: str,
            _resource_type: str,
            modify_index: int,
        ) -> storage_actions.RotationEvidence:
            status = app_actions._status_or_absent(
                application_slug,
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
                application_slug,
                command_runner=run,
                nomad_command=nomad_command,
                timeout_seconds=20,
                response_limit=1_048_576,
            )
            baseline_tokens = {app_actions._allocation_token(item) for item in baseline}
            run(
                (*nomad_command, "job", "restart", "-yes", application_slug),
                timeout_seconds=30,
                stdout_limit=65_536,
                stderr_limit=65_536,
            )
            for attempt in range(60):
                current_job = app_actions._inspected_candidate(
                    application_slug,
                    command_runner=run,
                    nomad_command=nomad_command,
                    timeout_seconds=20,
                    response_limit=1_048_576,
                )
                if current_job is not None:
                    version = current_job[0]
                    allocations = app_actions._allocations(
                        application_slug,
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
            s3_endpoint=f"https://{host}:9000",
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


def _lazy_storage(action: str) -> Handler:
    def handle(args: Mapping[str, Any]) -> Mapping[str, Any]:
        handlers, clients = _storage_handlers(action)
        try:
            return handlers[action](args)
        finally:
            for client in clients:
                try:
                    client.close()
                except Exception:
                    pass

    return handle


def _accept_backup(args: Mapping[str, Any]) -> Mapping[str, Any]:
    runtime = helper_runtime()
    return backup_handler(
        staging_directory=runtime.backups / "m1/.staging",
        backup_directory=runtime.backups / "m1",
    )(args)


def production_handlers() -> dict[str, Handler]:
    """Return the complete protocol-v1 map without opening live dependencies."""
    handlers: dict[str, Handler] = {"backup.accept": _accept_backup}
    handlers.update({action: _lazy_app(action) for action in APP_ACTIONS})
    handlers.update({action: _lazy_storage(action) for action in STORAGE_ACTIONS})
    if tuple(sorted(handlers)) != ACTION_MANIFEST:
        raise RuntimeError("production helper action map is incomplete")
    return handlers
