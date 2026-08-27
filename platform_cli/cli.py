"""Staff operator command tree and integration-owned orchestration.

The CLI keeps presentation and confirmation here while feature modules retain
provider behavior. It never renders secret values or unrestricted provider
responses.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
import uuid as uuid_module
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile, mkstemp
from typing import Any, cast

from . import app, db, openstack, remote, restore, runtime, services, setup, status, storage
from .config import Config, load, load_platform
from .storage_contract import (
    PLATFORM_ENVIRONMENT_KEYS,
    RESOURCE_TYPES,
    canonical_secret_key,
    platform_environment_values,
)
from .validation import (
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

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_UNAVAILABLE = 4

_MAX_LOG_LINES = 2_000
_PLATFORM_ENVIRONMENT = PLATFORM_ENVIRONMENT_KEYS

_DEFAULT_STATE = Path("/srv/openstack-platform/state")
_DEFAULT_PLATFORM = Path(os.environ.get("PLATFORM_CONFIG", "config/platform.json"))
_ACTIVE_COMMAND_DEADLINE: ContextVar[float | None] = ContextVar(
    "platform_cli_command_deadline", default=None
)


class CliError(RuntimeError):
    """A deliberate, operator-safe CLI failure."""


class DependencyUnavailable(CliError):
    pass


def _deadline(config: Config) -> str:
    return (
        (datetime.now(UTC) + timedelta(seconds=config.policy.limits.process_seconds))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _command_deadline(config: Config) -> float:
    active = _ACTIVE_COMMAND_DEADLINE.get()
    if active is not None:
        if active <= time.monotonic():
            raise CliError("operation exceeded its whole-command deadline")
        return active
    return time.monotonic() + config.policy.limits.process_seconds


def _wall_deadline(deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CliError("operation exceeded its whole-command deadline")
    return (
        (datetime.now(UTC) + timedelta(seconds=remaining))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _remaining(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CliError("operation exceeded its whole-command deadline")
    return min(maximum, remaining)


def _lines(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("lines must be an integer") from error
    if not 1 <= parsed <= _MAX_LOG_LINES:
        raise argparse.ArgumentTypeError(f"lines must be from 1 through {_MAX_LOG_LINES}")
    return parsed


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--platform-config", type=Path, default=_DEFAULT_PLATFORM)
    parser.add_argument("--state-directory", type=Path, default=_DEFAULT_STATE)
    parser.add_argument("--policy", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openstack-platform",
        description="staff operations for an OpenStack application platform",
        allow_abbrev=False,
    )
    _add_global_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    setup_command = commands.add_parser(
        "setup", help="create a complete greenfield deployment from a protected environment file"
    )
    setup_command.add_argument("--env-file", type=Path, required=True)
    setup_command.add_argument(
        "--workspace", type=Path, default=Path("/srv/openstack-platform/setup")
    )
    setup_command.add_argument("--cloudflare-token-file", type=Path)
    setup_command.add_argument(
        "--apply", action="store_true", help="build images and create the deployment"
    )

    commands.add_parser("status", help="show accepted state and bounded live availability")
    commands.add_parser("backup", help="back up and encrypt M1 SQLite state")
    restore_command = commands.add_parser(
        "restore", help="verify and atomically replace M1 SQLite state offline"
    )
    restore_command.add_argument("backup", type=Path)
    restore_command.add_argument("--age-identity", type=Path)
    restore_command.add_argument(
        "--yes", action="store_true", help="confirm replacement of the management database"
    )

    infra = commands.add_parser("infra", help="inspect and operate infrastructure")
    infra_commands = infra.add_subparsers(dest="infra_command", required=True)
    infra_commands.add_parser("list")
    image = infra_commands.add_parser("image")
    image_commands = image.add_subparsers(dest="image_command", required=True)
    image_commands.add_parser("list")
    image_set = image_commands.add_parser("set")
    image_set.add_argument("role", choices=openstack.IMAGE_ROLES)
    image_set.add_argument("image")
    prune = image_commands.add_parser("prune")
    prune.add_argument("--apply", action="store_true")
    prune.add_argument("--yes", action="store_true")
    for action in ("start", "stop", "reboot", "replace"):
        operation = infra_commands.add_parser(action)
        operation.add_argument("host", choices=openstack.PERSISTENT_ROLES)
        if action != "start":
            operation.add_argument("--yes", action="store_true")
        if action == "replace":
            operation.add_argument("--user-data", type=Path, help=argparse.SUPPRESS)
    logs = infra_commands.add_parser("logs")
    logs.add_argument("host", choices=openstack.PERSISTENT_ROLES)
    logs.add_argument("--lines", type=_lines, default=200)

    application = commands.add_parser("app", help="deploy and operate applications")
    app_commands = application.add_subparsers(dest="app_command", required=True)
    create = app_commands.add_parser("create")
    create.add_argument("slug")
    deploy = app_commands.add_parser("deploy")
    deploy.add_argument("slug")
    deploy.add_argument("--repo", required=True)
    deploy.add_argument("--commit", required=True)
    deploy.add_argument("--config", default="platform.yaml")
    remove = app_commands.add_parser("remove")
    remove.add_argument("slug")
    remove.add_argument("--confirm", required=True)
    app_commands.add_parser("list")
    show = app_commands.add_parser("show")
    show.add_argument("slug")
    app_logs = app_commands.add_parser("logs")
    app_logs.add_argument("slug")
    source = app_logs.add_mutually_exclusive_group(required=True)
    source.add_argument("--build", action="store_true")
    source.add_argument("--runtime", action="store_true")
    app_logs.add_argument("--follow", action="store_true")
    app_logs.add_argument("--lines", type=_lines, default=200)
    app_logs.add_argument("--id", dest="build_id")
    app_logs.add_argument("--list", dest="list_builds", action="store_true")
    environment = app_commands.add_parser("env")
    env_commands = environment.add_subparsers(dest="env_command", required=True)
    env_set = env_commands.add_parser("set")
    env_set.add_argument("slug")
    env_set.add_argument("key")
    env_unset = env_commands.add_parser("unset")
    env_unset.add_argument("slug")
    env_unset.add_argument("keys", nargs="+")
    env_import = env_commands.add_parser("import")
    env_import.add_argument("slug")
    env_import.add_argument("--file", required=True)
    env_list = env_commands.add_parser("list")
    env_list.add_argument("slug")

    managed = commands.add_parser("storage", help="operate managed storage")
    storage_commands = managed.add_subparsers(dest="storage_command", required=True)
    storage_list = storage_commands.add_parser("list")
    storage_list.add_argument("slug", nargs="?")
    storage_show = storage_commands.add_parser("show")
    storage_show.add_argument("slug")
    storage_show.add_argument("type", choices=RESOURCE_TYPES)
    storage_show.add_argument("--name", default="default")
    storage_create = storage_commands.add_parser("create")
    storage_create.add_argument("slug")
    storage_create.add_argument("type", choices=RESOURCE_TYPES)
    storage_create.add_argument("--name", default="default")
    storage_verify = storage_commands.add_parser("verify")
    storage_verify.add_argument("slug")
    storage_verify.add_argument("type", nargs="?", choices=RESOURCE_TYPES)
    storage_verify.add_argument("--name", default="default")
    storage_rotate = storage_commands.add_parser("rotate")
    storage_rotate.add_argument("slug")
    storage_rotate.add_argument("type", choices=RESOURCE_TYPES)
    storage_rotate.add_argument("--name", default="default")
    storage_remove = storage_commands.add_parser("remove")
    storage_remove.add_argument("slug")
    storage_remove.add_argument("type", choices=RESOURCE_TYPES)
    storage_remove.add_argument("--name", default="default")
    storage_remove.add_argument("--confirm", required=True)
    storage_remove.add_argument("--purge-s3", action="store_true")
    return parser


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]], *, output: Any) -> None:
    rendered = [["-" if value is None else str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print(
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)), file=output
    )
    for row in rendered:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)), file=output)


def _policy_path(args: argparse.Namespace) -> Path:
    return cast(Path, args.policy or args.state_directory / "policy.json")


def _load_config(args: argparse.Namespace) -> Config:
    return load(args.platform_config, _policy_path(args))


@contextmanager
def _database(
    args: argparse.Namespace, *, deadline: float | None = None
) -> Iterator[sqlite3.Connection]:
    selected_deadline = deadline if deadline is not None else _ACTIVE_COMMAND_DEADLINE.get()
    token = (
        _ACTIVE_COMMAND_DEADLINE.set(selected_deadline) if selected_deadline is not None else None
    )
    try:
        with runtime.lock(
            args.state_directory,
            "database-maintenance",
            wait=True,
            deadline=selected_deadline,
        ):
            identity = db.deployment_identity(load_platform(args.platform_config))
            connection = db.connect(
                args.state_directory / "platform.sqlite3",
                identity=identity,
            )
            try:
                db.migrate(connection, identity=identity)
            except BaseException:
                connection.close()
                raise
        try:
            yield connection
        finally:
            connection.close()
    finally:
        if token is not None:
            _ACTIVE_COMMAND_DEADLINE.reset(token)


def _application(connection: sqlite3.Connection, identifier: str) -> db.Application:
    record = db.get_application(connection, identifier)
    if record is None:
        raise ValidationError("application does not exist")
    return record


def _confirm(message: str, *, yes: bool, input_stream: Any, output: Any) -> None:
    print(message, file=output)
    if yes:
        return
    if input_stream is not sys.stdin or not input_stream.isatty():
        raise ValidationError("confirmation requires --yes when stdin is not a terminal")
    answer = input("Type yes to continue: ")
    if answer != "yes":
        raise ValidationError("operation was not confirmed")


def _begin(
    connection: sqlite3.Connection,
    config: Config,
    *,
    kind: str,
    scope: str,
    phase: str = "validated",
    refs: Mapping[str, Any] | None = None,
) -> str:
    existing = db.get_unfinished_operation(connection, scope)
    if existing is not None:
        raise db.UnfinishedOperationError(scope, existing.operation_id, existing.kind)
    identifier = str(uuid_module.uuid4())
    db.begin_operation(
        connection,
        operation_id=identifier,
        kind=kind,
        scope=scope,
        phase=phase,
        deadline_at=_deadline(config),
        refs=refs,
    )
    return identifier


def _selected_image_ids(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(item.image_id for item in db.list_image_selections(connection))


def _helper(
    config: Config,
    action: str,
    args: Mapping[str, Any],
    *,
    deadline: float | None = None,
) -> Mapping[str, Any]:
    timeout = float(config.policy.limits.helper_seconds)
    if deadline is not None:
        timeout = _remaining(deadline, timeout)
    return remote.call_helper(
        action,
        args,
        timeout_seconds=timeout,
        request_limit=config.policy.limits.helper_request_bytes,
        response_limit=config.policy.limits.helper_response_bytes,
        stderr_limit=config.policy.limits.stderr_bytes,
        helper_command=remote.helper_command_path(config.platform.get("paths.root")),
    )


def _verify_mutation_project(config: Config, *, deadline: float) -> None:
    openstack.verify_project(
        config.platform,
        timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
    )


def _role_health_check(config: Config) -> openstack.HealthCheck:
    def check(role: str, host: openstack.PersistentHost, remaining_seconds: float) -> None:
        openstack.check_role_health(
            config.platform,
            role,
            host,
            remaining_seconds,
        )

    return check


def _status_command(connection: sqlite3.Connection, config: Config, *, output: Any) -> None:
    model = status.status_show_live(connection, config)
    accepted = model["accepted"]
    observations = model["observations"]
    assert isinstance(accepted, dict) and isinstance(observations, dict)
    _table(
        ("STATE", "INFRA", "APPS", "STORAGE", "LIVE", "UNAVAILABLE", "UNHEALTHY"),
        (
            (
                model["state"],
                accepted["infrastructureRoles"],
                accepted["applications"],
                accepted["storageResources"],
                observations["available"],
                observations["unavailable"],
                observations["unhealthy"],
            ),
        ),
        output=output,
    )


def _infra_list(connection: sqlite3.Connection, config: Config, *, output: Any) -> None:
    observe = status.infrastructure_observer(
        config.platform,
        connection,
        timeout_seconds=config.policy.limits.process_seconds,
    )
    rows = status.infra_list(connection, observe=observe)
    _table(
        ("ROLE", "IMAGE", "COMMIT", "LIVE"),
        tuple(
            (
                item["role"],
                item["image"]["displayName"] if isinstance(item["image"], dict) else None,
                item["image"]["sourceCommit"] if isinstance(item["image"], dict) else None,
                item["live"]["state"] if isinstance(item["live"], dict) else "unknown",
            )
            for item in rows
        ),
        output=output,
    )


def _infra_image_list(config: Config, *, output: Any) -> None:
    images = openstack.list_images(
        config.platform, timeout_seconds=config.policy.limits.process_seconds
    )
    _table(
        ("ROLE", "NAME", "UUID", "STATUS", "COMMIT"),
        tuple(
            (item.role, item.name, item.image_id, item.status, item.source_commit)
            for item in images
        ),
        output=output,
    )


def _infra_image_set(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    output: Any,
) -> None:
    deadline = _command_deadline(config)
    with runtime.lock(args.state_directory, "infrastructure", deadline=deadline):
        unfinished = db.get_unfinished_operation(connection, "infrastructure")
        if unfinished is not None:
            if (
                unfinished.kind != "infra.image.set"
                or unfinished.refs.get("role") != args.role
                or unfinished.refs.get("requested_image") != args.image
            ):
                raise db.UnfinishedOperationError(
                    "infrastructure", unfinished.operation_id, unfinished.kind
                )
            operation_id = unfinished.operation_id
            if unfinished.phase == "validated":
                db.mark_failed(
                    connection,
                    operation_id,
                    "image selection stopped before observation",
                    cleanup_state="not_required",
                )
                unfinished = None
            elif unfinished.phase in {"selection_observed", "selection_recorded"}:
                selected = openstack.ImageSelection(
                    role=bounded_text(unfinished.refs.get("role"), field="image role", maximum=16),
                    image_id=uuid(unfinished.refs.get("image_id"), field="image UUID"),
                    display_name=bounded_text(
                        unfinished.refs.get("display_name"),
                        field="image display name",
                        maximum=256,
                    ),
                    source_commit=commit(unfinished.refs.get("source_commit")),
                    compatibility_hash=sha256_hex(
                        unfinished.refs.get("compatibility_hash"),
                        field="compatibility hash",
                    ),
                )
                if selected.role != args.role:
                    raise ValidationError("interrupted image selection role is malformed")
                db.put_image_selection(
                    connection,
                    role=selected.role,
                    image_id=selected.image_id,
                    display_name=selected.display_name,
                    source_commit=selected.source_commit,
                    compatibility_hash=selected.compatibility_hash,
                )
                db.checkpoint_operation(
                    connection,
                    operation_id,
                    phase="selection_recorded",
                    refs=unfinished.refs,
                )
                db.mark_succeeded(connection, operation_id, cleanup_state="not_required")
                print(
                    f"selected role={selected.role} image={selected.image_id} recovered=true",
                    file=output,
                )
                return
            else:
                raise openstack.RecoveryRequired(
                    "image selection has an unknown recovery phase", refs=unfinished.refs
                )
        if unfinished is None:
            operation_id = _begin(
                connection,
                config,
                kind="infra.image.set",
                scope="infrastructure",
                refs={"role": args.role, "requested_image": args.image},
            )
        try:
            selected = openstack.select_image(
                config.platform,
                args.role,
                args.image,
                timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
            )
            refs = {
                "role": selected.role,
                "requested_image": args.image,
                "image_id": selected.image_id,
                "display_name": selected.display_name,
                "source_commit": selected.source_commit,
                "compatibility_hash": selected.compatibility_hash,
            }
            db.checkpoint_operation(
                connection,
                operation_id,
                phase="selection_observed",
                refs=refs,
            )
            db.put_image_selection(
                connection,
                role=selected.role,
                image_id=selected.image_id,
                display_name=selected.display_name,
                source_commit=selected.source_commit,
                compatibility_hash=selected.compatibility_hash,
            )
            db.checkpoint_operation(connection, operation_id, phase="selection_recorded", refs=refs)
            db.mark_succeeded(connection, operation_id, cleanup_state="not_required")
        except Exception as error:
            current = db.get_operation(connection, operation_id)
            if current is not None and current.status == "running":
                db.mark_failed(connection, operation_id, error, cleanup_state="not_required")
            raise
    print(f"selected role={selected.role} image={selected.image_id}", file=output)


def _prune_plan_from_refs(refs: Mapping[str, Any]) -> openstack.PrunePlan:
    try:
        fingerprints = tuple(
            (item["image_id"], item["sha256"])
            for item in refs.get("candidate_fingerprints", [])
            if isinstance(item, dict)
        )
        return openstack.PrunePlan(
            image_ids=tuple(refs["image_ids"]),
            protected_image_ids=tuple(refs["protected_image_ids"]),
            review_image_ids=tuple(refs["review_image_ids"]),
            drift_hash=refs["drift_hash"],
            created_at=refs["created_at"],
            expires_at=refs["expires_at"],
            retain_newest=refs["retain_newest"],
            candidate_fingerprints=fingerprints,
            selected_image_ids=tuple(refs.get("selected_image_ids", ())),
            operation_image_ids=tuple(refs.get("operation_image_ids", ())),
            server_image_ids=tuple(refs.get("server_image_ids", ())),
            inventory_hash=refs.get("inventory_hash"),
        )
    except (KeyError, TypeError) as error:
        raise ValidationError("saved image prune plan is malformed") from error


def _prune_plan_from_database(connection: sqlite3.Connection) -> openstack.PrunePlan:
    row = connection.execute(
        "SELECT refs_json FROM operations WHERE kind='infra.image.prune.plan' AND status='succeeded' ORDER BY updated_at DESC, operation_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValidationError("no accepted image prune plan exists")
    return _prune_plan_from_refs(json.loads(row["refs_json"]))


def _infra_prune(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    input_stream: Any,
    output: Any,
) -> None:
    deadline = _command_deadline(config)
    with runtime.lock(args.state_directory, "infrastructure", deadline=deadline):
        unfinished = db.get_unfinished_operation(connection, "infrastructure")
        if unfinished is not None and unfinished.kind == "infra.image.prune.plan":
            if args.apply:
                raise db.UnfinishedOperationError(
                    "infrastructure", unfinished.operation_id, unfinished.kind
                )
            plan = _prune_plan_from_refs(unfinished.refs)
            db.mark_succeeded(connection, unfinished.operation_id, cleanup_state="not_required")
            _table(("DELETE UUID",), tuple((item,) for item in plan.image_ids), output=output)
            print(
                f"plan={plan.drift_hash} expires={plan.expires_at} review={len(plan.review_image_ids)} recovered=true",
                file=output,
            )
            return
        if unfinished is not None and (
            not args.apply or unfinished.kind != "infra.image.prune.apply"
        ):
            raise db.UnfinishedOperationError(
                "infrastructure", unfinished.operation_id, unfinished.kind
            )
        selected = _selected_image_ids(connection)
        operation_images = db.unfinished_operation_image_ids(
            connection,
            exclude_operation_id=(unfinished.operation_id if unfinished is not None else None),
        )
        if unfinished is not None:
            operation_id = unfinished.operation_id
            if unfinished.phase == "validated":
                db.mark_failed(
                    connection,
                    operation_id,
                    "image prune stopped before provider mutation",
                    cleanup_state="not_required",
                )
                unfinished = None
            else:
                plan = _prune_plan_from_refs(unfinished.refs)

                def recover_checkpoint(phase: str, refs: Mapping[str, Any]) -> None:
                    db.checkpoint_operation(
                        connection,
                        operation_id,
                        phase=phase,
                        refs=refs,
                        merge_refs=True,
                    )

                try:
                    recovered = openstack.recover_image_prune(
                        config.platform,
                        plan,
                        refs=unfinished.refs,
                        action="continue",
                        selected_image_ids=selected,
                        operation_image_ids=operation_images,
                        checkpoint=recover_checkpoint,
                        timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
                    )
                    db.mark_succeeded(connection, operation_id)
                except Exception as error:
                    current = db.get_operation(connection, operation_id)
                    if current is not None and current.status == "running":
                        db.mark_recovery_required(connection, operation_id, error)
                    raise
                print(
                    f"deleted={len(recovered.deleted_image_ids)} plan={recovered.drift_hash} recovered=true",
                    file=output,
                )
                return
        if not args.apply:
            plan = openstack.plan_image_prune(
                config.platform,
                selected_image_ids=selected,
                operation_image_ids=operation_images,
                timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
            )
            operation_id = _begin(
                connection,
                config,
                kind="infra.image.prune.plan",
                scope="infrastructure",
                refs=plan.operation_refs(),
            )
            db.mark_succeeded(connection, operation_id, cleanup_state="not_required")
            _table(("DELETE UUID",), tuple((item,) for item in plan.image_ids), output=output)
            print(
                f"plan={plan.drift_hash} expires={plan.expires_at} review={len(plan.review_image_ids)}",
                file=output,
            )
            return
        plan = _prune_plan_from_database(connection)
        _confirm(
            f"Delete {len(plan.image_ids)} exact image UUID(s); running and selected images stay protected.",
            yes=args.yes,
            input_stream=input_stream,
            output=output,
        )
        operation_id = _begin(
            connection,
            config,
            kind="infra.image.prune.apply",
            scope="infrastructure",
            refs=plan.operation_refs(),
        )

        def checkpoint(phase: str, refs: Mapping[str, Any]) -> None:
            db.checkpoint_operation(
                connection,
                operation_id,
                phase=phase,
                refs=refs,
                merge_refs=True,
            )

        try:
            result = openstack.apply_image_prune(
                config.platform,
                plan,
                selected_image_ids=selected,
                operation_image_ids=operation_images,
                checkpoint=checkpoint,
                timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
            )
            db.mark_succeeded(connection, operation_id)
        except openstack.RecoveryRequired as error:
            db.checkpoint_operation(
                connection,
                operation_id,
                phase="ambiguous",
                refs=error.refs,
                merge_refs=True,
            )
            db.mark_recovery_required(connection, operation_id, error)
            raise
        except Exception as error:
            db.mark_failed(connection, operation_id, error, cleanup_state="not_required")
            raise
    print(f"deleted={len(result.deleted_image_ids)} plan={result.drift_hash}", file=output)


def _infra_power(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    input_stream: Any,
    output: Any,
) -> None:
    action = args.infra_command
    if action != "start":
        _confirm(
            f"{action.capitalize()} {args.host}; this interrupts its platform role.",
            yes=args.yes,
            input_stream=input_stream,
            output=output,
        )
    kind = f"infra.{action}"
    deadline = _command_deadline(config)
    with runtime.lock(args.state_directory, "infrastructure", deadline=deadline):
        unfinished = db.get_unfinished_operation(connection, "infrastructure")
        recovering = False
        recovery_refs: Mapping[str, Any] = {}
        if unfinished is not None:
            if unfinished.kind != kind or unfinished.refs.get("role") != args.host:
                raise db.UnfinishedOperationError(
                    "infrastructure", unfinished.operation_id, unfinished.kind
                )
            operation_id = unfinished.operation_id
            if unfinished.phase == "powered":
                db.mark_succeeded(connection, operation_id, cleanup_state="not_required")
                print(f"role={args.host} recovered=powered", file=output)
                return
            if unfinished.phase in ("powering", "power_requested"):
                recovering = True
                recovery_refs = unfinished.refs
            elif unfinished.phase != "validated":
                raise openstack.RecoveryRequired(
                    "power operation has an unknown recovery phase", refs=unfinished.refs
                )
        else:
            operation_id = _begin(
                connection,
                config,
                kind=kind,
                scope="infrastructure",
                refs={"role": args.host, "action": action},
            )

        def checkpoint(phase: str, refs: Mapping[str, Any]) -> None:
            db.checkpoint_operation(
                connection,
                operation_id,
                phase=phase,
                refs=refs,
                merge_refs=True,
            )

        try:
            power = openstack.recover_power_host if recovering else openstack.power_host
            power_kwargs: dict[str, Any] = {
                "health_check": _role_health_check(config),
                "wait_seconds": _remaining(deadline, config.policy.limits.process_seconds),
                "poll_interval_seconds": config.policy.limits.poll_interval_seconds,
                "timeout_seconds": _remaining(
                    deadline, min(60, config.policy.limits.process_seconds)
                ),
            }
            if recovering:
                power_kwargs["refs"] = recovery_refs
            else:
                power_kwargs["checkpoint"] = checkpoint
            result = power(config.platform, args.host, action, **power_kwargs)
            db.checkpoint_operation(
                connection,
                operation_id,
                phase="powered",
                refs={
                    "server_id": result.server_id,
                    "role": result.role,
                    "action": action,
                    "status": result.status,
                },
            )
            db.mark_succeeded(connection, operation_id, cleanup_state="not_required")
        except Exception as error:
            db.mark_recovery_required(connection, operation_id, error)
            raise
    print(f"role={result.role} server={result.server_id} status={result.status}", file=output)


def _infra_replace(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    input_stream: Any,
    output: Any,
) -> None:
    _confirm(
        f"Replace {args.host}; its retained old server is deleted only after role readiness.",
        yes=args.yes,
        input_stream=input_stream,
        output=output,
    )
    selected = db.get_image_selection(connection, args.host)
    if selected is None:
        raise ValidationError("select an image for this role before replacement")
    deadline = _command_deadline(config)
    user_data = args.user_data
    with runtime.lock(args.state_directory, "infrastructure", deadline=deadline):
        unfinished = db.get_unfinished_operation(connection, "infrastructure")
        if unfinished is not None:
            if unfinished.kind != "infra.replace" or unfinished.refs.get("role") != args.host:
                raise db.UnfinishedOperationError(
                    "infrastructure", unfinished.operation_id, unfinished.kind
                )
            operation_id = unfinished.operation_id
            if unfinished.phase == "validated":
                db.mark_failed(
                    connection,
                    operation_id,
                    "replacement stopped before provider mutation",
                    cleanup_state="not_required",
                )
                unfinished = None
            else:
                actions = {
                    "observed": "rollback",
                    "old_stopped": "rollback",
                    "old_renamed": "rollback",
                    "old_retained": "rollback",
                    "resources_detached": "rollback",
                    "ambiguous": "rollback",
                    "replacement_created": "rollback",
                    "replacement_deleted": "rollback",
                    "accepted": "continue",
                    "complete": "continue",
                }
                action = actions.get(unfinished.phase)
                if action is None:
                    raise openstack.RecoveryRequired(
                        "replacement has an unknown recovery phase", refs=unfinished.refs
                    )

                def recover_checkpoint(phase: str, refs: Mapping[str, Any]) -> None:
                    db.checkpoint_operation(
                        connection,
                        operation_id,
                        phase=phase,
                        refs=refs,
                        merge_refs=True,
                    )

                recovered = openstack.recover_host_replacement(
                    config.platform,
                    args.host,
                    phase=unfinished.phase,
                    refs=unfinished.refs,
                    action=action,
                    checkpoint=recover_checkpoint,
                    health_check=_role_health_check(config),
                    wait_seconds=_remaining(deadline, config.policy.limits.process_seconds),
                    poll_interval_seconds=config.policy.limits.poll_interval_seconds,
                    timeout_seconds=_remaining(
                        deadline, min(60, config.policy.limits.process_seconds)
                    ),
                )
                db.mark_succeeded(connection, operation_id, cleanup_state=recovered.cleanup_state)
                print(
                    f"role={recovered.role} recovered={recovered.action} active={recovered.active_server_id}",
                    file=output,
                )
                return
        if unfinished is None:
            operation_id = _begin(
                connection,
                config,
                kind="infra.replace",
                scope="infrastructure",
                refs={
                    "role": args.host,
                    "selected_image_id": selected.image_id,
                },
            )

        def checkpoint(phase: str, refs: Mapping[str, Any]) -> None:
            db.checkpoint_operation(
                connection,
                operation_id,
                phase=phase,
                refs=refs,
                merge_refs=True,
            )

        try:
            result = openstack.replace_host(
                config.platform,
                args.host,
                selected_image_id=selected.image_id,
                selected_compatibility_hash=selected.compatibility_hash,
                operation_id=operation_id,
                user_data_path=user_data,
                checkpoint=checkpoint,
                health_check=_role_health_check(config),
                wait_seconds=_remaining(deadline, config.policy.limits.process_seconds),
                poll_interval_seconds=config.policy.limits.poll_interval_seconds,
                timeout_seconds=_remaining(deadline, min(60, config.policy.limits.process_seconds)),
            )
            if not result.accepted:
                error = openstack.OpenStackError(
                    "replacement readiness failed; retained host was restored"
                )
                db.mark_failed(connection, operation_id, error, cleanup_state="confirmed")
                raise error
            if result.cleanup_state != "confirmed":
                current = db.get_operation(connection, operation_id)
                refs = {} if current is None else current.refs
                error = openstack.RecoveryRequired(
                    "replacement accepted but retained old-server cleanup remains",
                    refs=refs,
                )
                db.mark_recovery_required(connection, operation_id, error)
                raise error
            db.mark_succeeded(connection, operation_id, cleanup_state=result.cleanup_state)
        except openstack.RecoveryRequired as error:
            current = db.get_operation(connection, operation_id)
            if current is not None and current.status == "running":
                db.checkpoint_operation(
                    connection,
                    operation_id,
                    phase="ambiguous",
                    refs=error.refs,
                    merge_refs=True,
                )
                db.mark_recovery_required(connection, operation_id, error)
            raise
        except Exception as error:
            current = db.get_operation(connection, operation_id)
            if current is not None and current.status == "running":
                if current.phase == "validated":
                    db.mark_failed(connection, operation_id, error, cleanup_state="not_required")
                else:
                    db.mark_recovery_required(connection, operation_id, error)
            raise
    print(
        f"role={result.role} server={result.active_server_id} image={result.selected_image_id}",
        file=output,
    )


def _read_bounded_file(path: Path, *, maximum: int, field: str) -> bytes:
    """Read one direct regular file without a path-swap or unbounded allocation."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise
        raise ValidationError(f"{field} must be a direct regular file") from error
    chunks: list[bytes] = []
    total = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValidationError(f"{field} must be a bounded direct regular file")
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValidationError(f"{field} exceeds its configured limit")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 1_073_741_824
        ):
            raise CliError("backup output was not a bounded private regular file")
        total = 0
        while chunk := os.read(descriptor, 1_048_576):
            total += len(chunk)
            if total > 1_073_741_824:
                raise CliError("backup output exceeded its configured size limit")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _tail_file_lines(path: Path, *, lines: int, maximum_bytes: int) -> list[str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise ValidationError("accepted build log is not a bounded direct file")
        position = metadata.st_size
        chunks: list[bytes] = []
        newlines = 0
        while position and newlines <= lines:
            amount = min(65_536, position)
            position -= amount
            os.lseek(descriptor, position, os.SEEK_SET)
            chunk = os.read(descriptor, amount)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
        return b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-lines:]
    finally:
        os.close(descriptor)


def _read_secret(stdin: Any, *, maximum: int) -> str:
    if stdin is sys.stdin and stdin.isatty():
        value = getpass.getpass("Value: ")
    else:
        raw = (
            stdin.buffer.read(maximum + 1) if hasattr(stdin, "buffer") else stdin.read(maximum + 1)
        )
        if isinstance(raw, str):
            raw = raw.encode()
        if len(raw) > maximum:
            raise ValidationError("environment value exceeds its configured limit")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValidationError("environment value must be UTF-8") from error
        value = value.removesuffix("\n")
    if "\x00" in value or len(value.encode()) > maximum:
        raise ValidationError("environment value is malformed or too large")
    return value


def _ownership(connection: sqlite3.Connection, application_id: str) -> dict[str, str]:
    return {
        item.key_name: item.owner
        for item in db.list_environment_keys(connection, application_id=application_id)
    }


def _environment_mutation(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    stdin: Any,
    output: Any,
) -> None:
    environment_service = services.EnvironmentService(connection, config, args.state_directory)
    environment_service.preflight(args.slug)
    if args.env_command == "set":
        updates = {
            env_key(args.key): _read_secret(
                stdin,
                maximum=config.policy.limits.environment_value_bytes,
            )
        }
        removals: tuple[str, ...] = ()
    elif args.env_command == "unset":
        updates = {}
        removals = tuple(args.keys)
    else:
        if args.file == "-":
            stream = stdin.buffer if hasattr(stdin, "buffer") else stdin
            raw = stream.read(config.policy.limits.dotenv_bytes + 1)
            if isinstance(raw, str):
                raw = raw.encode()
        else:
            source = Path(args.file)
            metadata = source.lstat()
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                print("warning: dotenv file is readable by group or others", file=sys.stderr)
            raw = _read_bounded_file(
                source,
                maximum=config.policy.limits.dotenv_bytes,
                field="dotenv input",
            )
        updates = app.parse_dotenv(raw, maximum_bytes=config.policy.limits.dotenv_bytes)
        removals = ()
    result = environment_service.mutate(
        services.EnvironmentMutationRequest(
            action=args.env_command,
            application=args.slug,
            updates=updates,
            removals=removals,
        )
    )
    print(f"keys={len(result.key_names)} modify-index={result.modify_index}", file=output)


def _app_row(item: Mapping[str, object]) -> tuple[object, ...]:
    deployment = item["deployment"]
    sizing = cast(Mapping[str, object], item["sizing"])
    live = cast(Mapping[str, object], item["live"])
    return (
        item["slug"],
        item["desiredRunning"],
        deployment["sourceCommit"] if isinstance(deployment, dict) else None,
        deployment["imageDigest"] if isinstance(deployment, dict) else None,
        sizing["cpuMHz"],
        sizing["memoryMiB"],
        live["schedulerState"],
    )


def _app_read(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    output: Any,
) -> None:
    reads = services.ProductReadService(connection, config)
    models = (
        reads.applications()
        if args.app_command == "list"
        else (reads.application(args.slug),)
    )
    _table(
        ("SLUG", "RUNNING", "COMMIT", "DIGEST", "CPU", "MEMORY", "LIVE"),
        tuple(_app_row(item) for item in models),
        output=output,
    )


def _manifest_from_build(value: object) -> app.Manifest:
    required = {"runtime", "packages", "buildScript", "startScript", "port", "healthPath"}
    if not isinstance(value, dict) or set(value) not in (required, required | {"storageBindings"}):
        raise app.ApplicationError("build helper returned invalid manifest evidence")
    packages = value["packages"]
    if not isinstance(packages, list) or not packages:
        raise app.ApplicationError("build helper returned invalid package evidence")
    runtime_name = value["runtime"]
    if not isinstance(runtime_name, str) or runtime_name not in {"bun", "node"}:
        raise app.ApplicationError("build helper returned invalid runtime evidence")
    raw_bindings = value.get("storageBindings", [])
    if not isinstance(raw_bindings, list):
        raise app.ApplicationError("build helper returned invalid storage binding evidence")
    bindings: list[app.StorageBinding] = []
    for binding in raw_bindings:
        if not isinstance(binding, dict) or binding.keys() != {"name", "type", "environment"}:
            raise app.ApplicationError("build helper returned invalid storage binding evidence")
        environment = binding["environment"]
        if not isinstance(environment, dict):
            raise app.ApplicationError("build helper returned invalid storage binding evidence")
        bindings.append(
            app.StorageBinding(binding["name"], binding["type"], tuple(sorted(environment.items())))
        )
    return app.Manifest(
        runtime=runtime_name,
        packages=tuple(relative_path(item, field="package path") for item in packages),
        build_script=(None if value["buildScript"] is None else script_name(value["buildScript"])),
        start_script=script_name(value["startScript"]),
        port=value["port"],
        health_path=health_path(value["healthPath"]),
        storage_bindings=tuple(bindings),
    )


def _write_build_log(
    state_directory: Path,
    application_id: str,
    operation_id: str,
    source_commit: str,
    text: str,
) -> str:
    payload = text.encode("utf-8")
    root = runtime.ensure_private_directory(state_directory / "build-logs", create=True)
    directory = runtime.ensure_private_directory(root / application_id, create=True)
    name = f"{source_commit}-{operation_id}.log"
    destination = directory / name
    with NamedTemporaryFile(dir=directory, prefix=f".{name}.", delete=False) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination.relative_to(state_directory).as_posix()


def _apply_registry_retention(
    connection: sqlite3.Connection,
    config: Config,
    *,
    application_id: str,
    application_slug: str,
    current_image: str,
    deadline: float | None = None,
) -> None:
    successful = list(db.list_application_successful_manifest_history(connection, application_id))
    if current_image in successful:
        successful.remove(current_image)
    successful.insert(0, current_image)
    all_images = db.list_application_manifest_images(connection, application_id)
    # Retention counts accepted deployments, while exposing other pushed
    # candidates for deletion unless an unfinished operation references them.
    history = [*successful, *(image for image in all_images if image not in successful)]
    references = list(db.list_active_application_manifest_references(connection, application_id))
    result = _helper(
        config,
        "app.manifest.retain",
        {
            "slug": application_slug,
            "history": history,
            "references": references,
        },
        deadline=deadline,
    )
    if not isinstance(result.get("deleted"), list) or not isinstance(result.get("protected"), list):
        raise app.ApplicationError("registry retention evidence was malformed")


_DeploymentSpec = app.DeploymentSpec
_DeploymentBuild = app.DeploymentBuild
_DeploymentWorker = app.DeploymentWorker


def _prepare_deployment_build(
    connection: sqlite3.Connection,
    config: Config,
    *,
    state_directory: Path,
    operation: db.Operation,
    application_id: str,
    application_slug: str,
    repository: str,
    source_commit: str,
    config_path: str,
    deadline: float,
) -> _DeploymentBuild:
    refs = dict(operation.refs)
    candidate = operation.candidate_digest
    if candidate is None:
        with runtime.lock(state_directory, "infrastructure", deadline=deadline):
            builder_image = db.get_image_selection(connection, "builder")
            if builder_image is None:
                raise ValidationError("select a builder image before deployment")
            refs = {**refs, "builder_image_id": builder_image.image_id}
            db.checkpoint_operation(
                connection,
                operation.operation_id,
                phase="builder_creating",
                refs=refs,
            )
        built = _helper(
            config,
            "app.build",
            {
                "buildId": operation.operation_id,
                "slug": application_slug,
                "repository": repository,
                "commit": source_commit,
                "configPath": config_path,
                "builderImageId": builder_image.image_id,
                "runtimeImages": {
                    "bun": config.policy.runtime_images.bun,
                    "node": config.policy.runtime_images.node,
                },
                "sourceLimit": config.policy.limits.source_bytes,
                "buildLogLimit": config.policy.limits.build_log_bytes,
                "connectSeconds": config.policy.limits.connect_seconds,
                "deadlineAt": operation.deadline_at,
            },
            deadline=deadline,
        )
        if built.get("builderAbsent") is not True:
            raise app.ApplicationError("builder cleanup was not confirmed")
        candidate = oci_digest_pin(built.get("image"), field="built image")
        expected_repository = (
            f"{config.platform.get('addresses.storage')}:5000/projects/{application_slug}/app@"
        )
        if not candidate.startswith(expected_repository):
            raise app.ApplicationError(
                "build helper returned an image outside the application repository"
            )
        manifest = _manifest_from_build(built.get("manifest"))
        recipe = app.generate_recipe(manifest, config.policy.runtime_images)
        recipe_hash = sha256_hex(built.get("recipeHash"), field="recipe hash")
        if recipe_hash != recipe.sha256:
            raise app.ApplicationError(
                "build helper recipe identity did not match validated policy"
            )
        log = built.get("log")
        if not isinstance(log, str) or len(log.encode()) > config.policy.limits.build_log_bytes:
            raise app.ApplicationError("build helper returned invalid build log evidence")
        build_log_path = _write_build_log(
            state_directory,
            application_id,
            operation.operation_id,
            source_commit,
            log,
        )
        refs = {
            **refs,
            "manifest": built["manifest"],
            "recipe_hash": recipe_hash,
            "build_log_path": build_log_path,
        }
        db.checkpoint_operation(
            connection,
            operation.operation_id,
            phase="image_pushed",
            refs=refs,
            candidate_digest=candidate,
            cleanup_state="confirmed",
        )
    else:
        manifest = _manifest_from_build(refs.get("manifest"))
        recipe_hash = sha256_hex(refs.get("recipe_hash"), field="recipe hash")
        build_log_path = relative_path(
            refs.get("build_log_path"), field="build log path", allow_dot=False
        )
    return _DeploymentBuild(candidate, manifest, recipe_hash, build_log_path, refs)


def _record_platform_environment_ownership(
    connection: sqlite3.Connection,
    application_id: str,
    key_names: Sequence[str],
) -> None:
    """Transfer canonical key names to platform ownership without losing others."""
    claimed = {env_key(name) for name in key_names}
    existing = db.list_environment_keys(connection, application_id=application_id)
    for owner in sorted({item.owner for item in existing} - {"platform"}):
        owned = {item.key_name for item in existing if item.owner == owner}
        if owned & claimed:
            db.set_environment_keys(
                connection,
                application_id=application_id,
                owner=owner,
                keys=sorted(owned - claimed),
            )
    db.set_environment_keys(
        connection,
        application_id=application_id,
        owner="platform",
        keys=sorted(claimed),
    )


def _environment_update_accepted(result: Mapping[str, Any], *, allow_stopped: bool) -> bool:
    evidence = (
        result.get("restarted"),
        result.get("schedulerHealthy"),
        result.get("publicHealthy"),
    )
    return evidence == (True, True, True) or (allow_stopped and evidence == (False, False, False))


def _prepare_platform_environment(
    connection: sqlite3.Connection,
    config: Config,
    *,
    operation_id: str,
    application_id: str,
    application_slug: str,
    build: _DeploymentBuild,
    deadline: float,
) -> dict[str, Any]:
    _validate_storage_bindings(
        connection,
        config,
        application_id=application_id,
        application_slug=application_slug,
        manifest=build.manifest,
        deadline=deadline,
    )
    platform_values = platform_environment_values(
        application_id,
        application_slug,
        build.manifest.port,
    )
    previous = db.get_deployment(connection, application_id)
    prior_platform_values = (
        {}
        if previous is None
        else platform_environment_values(
            application_id,
            application_slug,
            previous.application_port,
        )
    )
    refs = {
        **build.refs,
        "platform_key_names": sorted(platform_values),
        "desired_platform_values": platform_values,
        "prior_platform_values": prior_platform_values,
    }
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="platform_environment_mutating",
        refs=refs,
        candidate_digest=build.image,
    )
    ownership = _ownership(connection, application_id)
    result = app.set_environment(
        application_slug,
        platform_values,
        {**ownership, **{name: "staff" for name in platform_values}},
        timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
        helper_caller=lambda action, values, **_bounds: _helper(
            config, action, values, deadline=deadline
        ),
    )
    names = result.get("keys")
    if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
        raise app.ApplicationError("helper returned invalid platform environment evidence")
    if not set(platform_values).issubset(names):
        raise app.ApplicationError("platform environment keys were not all recorded")
    previous = db.get_deployment(connection, application_id)
    if previous is not None and not _environment_update_accepted(result, allow_stopped=True):
        raise app.ApplicationError("platform environment restart and health were not confirmed")
    _record_platform_environment_ownership(
        connection,
        application_id,
        sorted(platform_values),
    )
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="platform_environment_ready",
        refs=refs,
        candidate_digest=build.image,
    )
    return refs


def _prepare_deployment_worker(
    connection: sqlite3.Connection,
    config: Config,
    *,
    state_directory: Path,
    operation_id: str,
    application_id: str,
    application_slug: str,
    worker_flavor: str,
    candidate: str,
    refs: dict[str, Any],
    deadline: float,
) -> _DeploymentWorker:
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="worker_creating",
        refs=refs,
        candidate_digest=candidate,
    )
    worker = _helper(
        config,
        "app.worker.observe",
        {"applicationId": application_id, "slug": application_slug},
        deadline=deadline,
    )
    if worker.get("absent") is True:
        with runtime.lock(state_directory, "infrastructure", deadline=deadline):
            worker_image = db.get_image_selection(connection, "worker")
            if worker_image is None:
                raise ValidationError("select a worker image before deployment")
            refs = {**refs, "worker_image_id": worker_image.image_id}
            db.checkpoint_operation(
                connection,
                operation_id,
                phase="worker_creating",
                refs=refs,
                candidate_digest=candidate,
            )
        observed_flavor_name = openstack.observe_flavor(
            config.platform,
            worker_flavor,
            require_one_vcpu=True,
            timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
        )
        if observed_flavor_name != worker_flavor:
            raise openstack.OpenStackError("configured worker flavor resolved to a different name")
        worker = _helper(
            config,
            "app.worker.create",
            {
                "applicationId": application_id,
                "slug": application_slug,
                "workerImageId": worker_image.image_id,
                "standardFlavor": worker_flavor,
            },
            deadline=deadline,
        )
    if worker.get("ready") is not True:
        raise app.ApplicationError("worker readiness was not confirmed")
    server_id = uuid(worker.get("serverId"), field="worker server UUID")
    port_id = uuid(worker.get("portId"), field="worker port UUID")
    server_name = bounded_text(worker.get("serverName"), field="worker server name", maximum=128)
    port_name = bounded_text(worker.get("portName"), field="worker port name", maximum=128)
    refs = {
        **refs,
        "worker_server_id": server_id,
        "worker_server_name": server_name,
        "worker_port_id": port_id,
        "worker_port_name": port_name,
    }
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="worker_ready",
        refs=refs,
        candidate_digest=candidate,
    )
    return _DeploymentWorker(server_id, server_name, port_id, port_name, refs)


def _validate_storage_bindings(
    connection: sqlite3.Connection,
    config: Config,
    *,
    application_id: str,
    application_slug: str,
    manifest: app.Manifest,
    deadline: float,
) -> None:
    if not manifest.storage_bindings:
        return
    resources = {
        (item.resource_type, item.resource_name): item
        for item in db.list_managed_resources(connection, application_id=application_id)
    }
    required_keys: set[str] = set()
    targets: set[str] = set()
    declared = {(binding.resource_type, binding.name) for binding in manifest.storage_bindings}
    active = {
        identity for identity, resource in resources.items() if resource.lifecycle_state == "active"
    }
    undeclared = sorted(active - declared)
    if undeclared:
        resource_type, resource_name = undeclared[0]
        raise ValidationError(
            f"active {resource_type} storage {resource_name!r} has no deployment binding"
        )
    for binding in manifest.storage_bindings:
        resource = resources.get((binding.resource_type, binding.name))
        if resource is None or resource.lifecycle_state != "active":
            raise ValidationError(
                f"storage binding {binding.name!r} references a missing or inactive {binding.resource_type} resource"
            )
        for output, target in binding.environment:
            required_keys.add(canonical_secret_key(binding.resource_type, binding.name, output))
            targets.add(target)
    observed = app.list_environment(
        application_slug,
        timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
        helper_caller=lambda action, values, **_bounds: _helper(
            config, action, values, deadline=deadline
        ),
    )
    names = observed.get("keys")
    if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
        raise app.ApplicationError("helper returned invalid environment-key evidence")
    present = set(names)
    missing = sorted(required_keys - present)
    if missing:
        raise ValidationError("storage binding outputs are missing from the application variable")
    conflicts = sorted(targets & present)
    if conflicts:
        raise ValidationError(
            f"storage binding target {conflicts[0]!r} conflicts with an existing environment key"
        )


def _deploy_and_accept_application(
    connection: sqlite3.Connection,
    config: Config,
    spec: _DeploymentSpec,
    build: _DeploymentBuild,
    worker: _DeploymentWorker,
    *,
    operation_id: str,
    deadline: float,
) -> app.DeploymentResult:
    previous = db.get_deployment(connection, spec.application_id)
    _validate_storage_bindings(
        connection,
        config,
        application_id=spec.application_id,
        application_slug=spec.application_slug,
        manifest=build.manifest,
        deadline=deadline,
    )
    job = app.render_nomad_job(
        application_id=spec.application_id,
        application_slug=spec.application_slug,
        image=build.image,
        manifest=build.manifest,
        platform=config.platform,
        cpu_mhz=spec.cpu_mhz,
        memory_mib=spec.memory_mib,
        source_commit=spec.source_commit,
        recipe_hash=build.recipe_hash,
    )
    try:
        result = app.deploy_and_cleanup(
            spec.application_slug,
            job,
            attempts=max(
                1,
                min(
                    300,
                    int(_remaining(deadline, config.policy.limits.process_seconds))
                    // config.policy.limits.poll_interval_seconds,
                ),
            ),
            poll_interval_seconds=config.policy.limits.poll_interval_seconds,
            helper_timeout_seconds=config.policy.limits.helper_seconds,
            helper_caller=lambda action, values, **_bounds: _helper(
                config, action, values, deadline=deadline
            ),
            public_health_check=lambda: app.check_public_health(
                spec.application_slug,
                config.platform,
                build.manifest.health_path,
                timeout_seconds=_remaining(deadline, config.policy.limits.http_seconds),
            ),
            sleep=lambda seconds: time.sleep(_remaining(deadline, seconds)),
        )
    except app.DeploymentFailed as error:
        if not error.cleanup_succeeded:
            raise
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="candidate_removed",
            refs=worker.refs,
            candidate_digest=build.image,
            cleanup_state="confirmed",
        )
        if previous is None or previous.image_digest != build.image:
            removed = _helper(
                config,
                "app.manifest.delete",
                {
                    "slug": spec.application_slug,
                    "image": build.image,
                    "references": ([] if previous is None else [previous.image_digest]),
                },
                deadline=deadline,
            )
            if removed.get("absent") is not True:
                raise app.ApplicationError(
                    "candidate manifest cleanup was not confirmed"
                ) from error
        db.mark_failed(connection, operation_id, error, cleanup_state="confirmed")
        raise

    candidate_identity = app.nomad_candidate_identity(job)
    accepted_refs = {
        **worker.refs,
        "nomad_version": result.nomad_version,
        "candidate_job_sha256": candidate_identity[0],
    }
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="deployment_healthy",
        refs=accepted_refs,
        candidate_digest=build.image,
    )
    app.accept_healthy_deployment(
        connection,
        app.DeploymentAcceptance(
            application_id=spec.application_id,
            application_slug=spec.application_slug,
            repository=spec.repository,
            config_path=spec.config_path,
            worker_server_id=worker.server_id,
            worker_server_name=worker.server_name,
            worker_port_id=worker.port_id,
            worker_port_name=worker.port_name,
            worker_flavor=spec.worker_flavor,
            cpu_mhz=spec.cpu_mhz,
            memory_mib=spec.memory_mib,
            source_commit=spec.source_commit,
            recipe_hash=build.recipe_hash,
            image=build.image,
            nomad_job=job,
            nomad_job_sha256=candidate_identity[0],
            nomad_version=result.nomad_version,
            health_path=build.manifest.health_path,
            application_port=build.manifest.port,
            build_log_path=build.build_log_path,
            public_url=f"https://{spec.application_slug}.{config.platform.domain}",
        ),
        helper_timeout_seconds=config.policy.limits.helper_seconds,
        helper_caller=lambda action, values, **_bounds: _helper(
            config, action, values, deadline=deadline
        ),
        public_health_check=lambda: app.check_public_health(
            spec.application_slug,
            config.platform,
            build.manifest.health_path,
            timeout_seconds=_remaining(deadline, config.policy.limits.http_seconds),
        ),
    )
    db.checkpoint_operation(
        connection,
        operation_id,
        phase="accepted",
        refs=accepted_refs,
        candidate_digest=build.image,
    )
    _apply_registry_retention(
        connection,
        config,
        application_id=spec.application_id,
        application_slug=spec.application_slug,
        current_image=build.image,
        deadline=deadline,
    )
    db.mark_succeeded(connection, operation_id)
    return result


def _recover_app_deployment(
    connection: sqlite3.Connection,
    config: Config,
    spec: _DeploymentSpec,
    operation: db.Operation,
    *,
    deadline: float,
    output: Any,
) -> db.Operation | None:
    """Reconcile one recorded deployment phase; return work still to run."""
    application_id = spec.application_id
    application_slug = spec.application_slug
    operation_id = operation.operation_id
    if operation.phase in {"platform_environment_mutating", "platform_environment_ready"}:
        if operation.refs.get("platform_key_names") != sorted(_PLATFORM_ENVIRONMENT):
            raise app.ApplicationError("platform environment recovery intent is malformed")
        desired_values = operation.refs.get("desired_platform_values")
        prior_values = operation.refs.get("prior_platform_values")
        if (
            not isinstance(desired_values, dict)
            or set(desired_values) != _PLATFORM_ENVIRONMENT
            or any(not isinstance(value, str) for value in desired_values.values())
            or not isinstance(prior_values, dict)
            or not set(prior_values).issubset(_PLATFORM_ENVIRONMENT)
            or any(not isinstance(value, str) for value in prior_values.values())
        ):
            raise app.ApplicationError("platform environment recovery values are malformed")
        ownership = _ownership(connection, application_id)

        def helper_caller(
            action: str, values: Mapping[str, Any], **_bounds: object
        ) -> Mapping[str, Any]:
            return _helper(config, action, values, deadline=deadline)

        if prior_values:
            restored = app.set_environment(
                application_slug,
                prior_values,
                {**ownership, **{name: "staff" for name in prior_values}},
                timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
                helper_caller=helper_caller,
            )
        else:
            restored = app.remove_environment(
                application_slug,
                sorted(_PLATFORM_ENVIRONMENT),
                {**ownership, **{name: "staff" for name in _PLATFORM_ENVIRONMENT}},
                timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
                helper_caller=helper_caller,
            )
        names = restored.get("keys")
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise app.ApplicationError(
                "helper returned invalid restored platform environment evidence"
            )
        present_platform = set(names) & _PLATFORM_ENVIRONMENT
        if present_platform != set(prior_values):
            raise app.ApplicationError("prior platform environment key set was not restored")
        previous = db.get_deployment(connection, application_id)
        if previous is not None and not _environment_update_accepted(restored, allow_stopped=True):
            raise app.ApplicationError("prior platform environment health was not restored")
        _record_platform_environment_ownership(
            connection,
            application_id,
            sorted(prior_values),
        )
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="image_pushed",
            refs=operation.refs,
            candidate_digest=operation.candidate_digest,
        )
        refreshed = db.get_operation(connection, operation_id)
        assert refreshed is not None
        operation = refreshed

    if operation.phase == "candidate_removed":
        candidate = oci_digest_pin(operation.candidate_digest, field="removed candidate digest")
        previous = db.get_deployment(connection, application_id)
        if previous is None or previous.image_digest != candidate:
            result = _helper(
                config,
                "app.manifest.delete",
                {
                    "slug": application_slug,
                    "image": candidate,
                    "references": ([] if previous is None else [previous.image_digest]),
                },
                deadline=deadline,
            )
            if result.get("absent") is not True:
                raise app.ApplicationError("candidate manifest cleanup was not confirmed")
        db.mark_failed(
            connection,
            operation_id,
            "confirmed candidate removal recovered and manifest cleaned",
            cleanup_state="confirmed",
        )
        print(f"slug={application_slug} recovered=candidate-removed", file=output)
        return None

    if operation.phase == "accepted":
        accepted = db.get_deployment(connection, application_id)
        if accepted is None or accepted.image_digest != operation.candidate_digest:
            raise app.ApplicationError(
                "accepted deployment recovery evidence does not match SQLite"
            )
        assert operation.candidate_digest is not None
        _apply_registry_retention(
            connection,
            config,
            application_id=application_id,
            application_slug=application_slug,
            current_image=operation.candidate_digest,
            deadline=deadline,
        )
        db.mark_succeeded(connection, operation_id)
        print(f"slug={application_slug} recovered=accepted", file=output)
        return None

    recovery_action = app.deployment_recovery_action(
        operation.phase, candidate_digest=operation.candidate_digest
    )
    if recovery_action == "accept_deployment":
        candidate = oci_digest_pin(operation.candidate_digest, field="candidate digest")
        manifest = _manifest_from_build(operation.refs.get("manifest"))
        _validate_storage_bindings(
            connection,
            config,
            application_id=application_id,
            application_slug=application_slug,
            manifest=manifest,
            deadline=deadline,
        )
        job = app.render_nomad_job(
            application_id=application_id,
            application_slug=application_slug,
            image=candidate,
            manifest=manifest,
            platform=config.platform,
            cpu_mhz=spec.cpu_mhz,
            memory_mib=spec.memory_mib,
            source_commit=spec.source_commit,
            recipe_hash=sha256_hex(operation.refs.get("recipe_hash"), field="recipe hash"),
        )
        candidate_identity = app.nomad_candidate_identity(job)
        if (
            candidate_identity is None
            or operation.refs.get("candidate_job_sha256") != candidate_identity[0]
            or candidate_identity[1] != candidate
        ):
            raise app.ApplicationError("healthy deployment recovery candidate identity drifted")
        nomad_version = operation.refs.get("nomad_version")
        if isinstance(nomad_version, bool) or not isinstance(nomad_version, int):
            raise app.ApplicationError("healthy deployment recovery omitted its Nomad version")
        worker_server_id = uuid(operation.refs.get("worker_server_id"), field="worker server UUID")
        worker_port_id = uuid(operation.refs.get("worker_port_id"), field="worker port UUID")
        worker_server_name = bounded_text(
            operation.refs.get("worker_server_name"),
            field="worker server name",
            maximum=128,
        )
        worker_port_name = bounded_text(
            operation.refs.get("worker_port_name"),
            field="worker port name",
            maximum=128,
        )
        recipe_hash = sha256_hex(operation.refs.get("recipe_hash"), field="recipe hash")
        build_log_path = relative_path(
            operation.refs.get("build_log_path"), field="build log path", allow_dot=False
        )
        app.accept_healthy_deployment(
            connection,
            app.DeploymentAcceptance(
                application_id=application_id,
                application_slug=application_slug,
                repository=spec.repository,
                config_path=spec.config_path,
                worker_server_id=worker_server_id,
                worker_server_name=worker_server_name,
                worker_port_id=worker_port_id,
                worker_port_name=worker_port_name,
                worker_flavor=spec.worker_flavor,
                cpu_mhz=spec.cpu_mhz,
                memory_mib=spec.memory_mib,
                source_commit=spec.source_commit,
                recipe_hash=recipe_hash,
                image=candidate,
                nomad_job=job,
                nomad_job_sha256=candidate_identity[0],
                nomad_version=nomad_version,
                health_path=manifest.health_path,
                application_port=manifest.port,
                build_log_path=build_log_path,
                public_url=f"https://{application_slug}.{config.platform.domain}",
            ),
            helper_timeout_seconds=config.policy.limits.helper_seconds,
            helper_caller=lambda action, values, **_bounds: _helper(
                config, action, values, deadline=deadline
            ),
            public_health_check=lambda: app.check_public_health(
                application_slug,
                config.platform,
                manifest.health_path,
                timeout_seconds=_remaining(deadline, config.policy.limits.http_seconds),
            ),
        )
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="accepted",
            refs=operation.refs,
            candidate_digest=candidate,
        )
        _apply_registry_retention(
            connection,
            config,
            application_id=application_id,
            application_slug=application_slug,
            current_image=candidate,
            deadline=deadline,
        )
        db.mark_succeeded(connection, operation_id)
        print(f"slug={application_slug} recovered=deployment-healthy", file=output)
        return None

    if recovery_action.startswith("cleanup_builder"):
        result = _helper(
            config,
            "app.builder.delete",
            {"buildId": operation_id},
            deadline=deadline,
        )
        if result.get("absent") is not True:
            raise app.ApplicationError("builder cleanup was not confirmed")
        db.checkpoint_operation(
            connection,
            operation_id,
            phase="image_pushed" if operation.candidate_digest is not None else "validated",
            refs=operation.refs,
            candidate_digest=operation.candidate_digest,
            cleanup_state="confirmed",
        )
        refreshed = db.get_operation(connection, operation_id)
        assert refreshed is not None
        return refreshed
    return operation


def _app_create(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    output: Any,
) -> None:
    created = services.ApplicationService(connection, config).declare(args.slug)
    print(f"slug={created.slug} application={created.application_id}", file=output)


def _app_deploy(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    output: Any,
) -> None:
    application_slug = slug(args.slug)
    repository = repository_url(args.repo)
    source_commit = commit(args.commit)
    config_path = relative_path(args.config, field="config path", allow_dot=False)
    existing_application = db.get_application(connection, application_slug)
    application_id = (
        existing_application.application_id
        if existing_application is not None
        else str(uuid_module.uuid4())
    )
    worker_flavor = (
        existing_application.worker_flavor
        if existing_application is not None
        else config.policy.standard.worker_flavor
    )
    cpu_mhz = (
        existing_application.scheduler_cpu_mhz
        if existing_application is not None
        else config.policy.standard.cpu_mhz
    )
    memory_mib = (
        existing_application.scheduler_memory_mib
        if existing_application is not None
        else config.policy.standard.memory_mib
    )
    spec = _DeploymentSpec(
        application_id,
        application_slug,
        repository,
        source_commit,
        config_path,
        worker_flavor,
        cpu_mhz,
        memory_mib,
    )
    deadline = _command_deadline(config)

    def recover(operation: db.Operation) -> db.Operation | None:
        return _recover_app_deployment(
            connection, config, spec, operation, deadline=deadline, output=output
        )

    def prepare_build(operation: db.Operation) -> app.DeploymentBuild:
        return _prepare_deployment_build(
            connection,
            config,
            state_directory=args.state_directory,
            operation=operation,
            application_id=application_id,
            application_slug=application_slug,
            repository=repository,
            source_commit=source_commit,
            config_path=config_path,
            deadline=deadline,
        )

    def prepare_environment(operation_id: str, build: app.DeploymentBuild) -> dict[str, Any]:
        return _prepare_platform_environment(
            connection,
            config,
            operation_id=operation_id,
            application_id=application_id,
            application_slug=application_slug,
            build=build,
            deadline=deadline,
        )

    def prepare_worker(
        operation_id: str, build: app.DeploymentBuild, refs: dict[str, Any]
    ) -> app.DeploymentWorker:
        return _prepare_deployment_worker(
            connection,
            config,
            state_directory=args.state_directory,
            operation_id=operation_id,
            application_id=application_id,
            application_slug=application_slug,
            worker_flavor=worker_flavor,
            candidate=build.image,
            refs=refs,
            deadline=deadline,
        )

    def deploy_and_accept(
        operation_id: str, build: app.DeploymentBuild, worker: app.DeploymentWorker
    ) -> app.DeploymentResult:
        return _deploy_and_accept_application(
            connection,
            config,
            spec,
            build,
            worker,
            operation_id=operation_id,
            deadline=deadline,
        )

    result = app.execute_deployment_workflow(
        connection,
        config,
        args.state_directory,
        spec,
        deadline_at=_wall_deadline(deadline),
        deadline=deadline,
        verify_project=lambda: _verify_mutation_project(config, deadline=deadline),
        recover=recover,
        prepare_build=prepare_build,
        prepare_environment=prepare_environment,
        prepare_worker=prepare_worker,
        deploy_and_accept=deploy_and_accept,
    )
    if result is None:
        return
    candidate_digest, deployment_result = result
    print(
        f"slug={application_slug} image={candidate_digest} nomad-version={deployment_result.nomad_version}",
        file=output,
    )


def _app_remove(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    output: Any,
) -> None:
    if args.confirm != args.slug:
        raise ValidationError("application removal requires the exact slug")
    application = _application(connection, args.slug)
    scope = f"app-{application.application_id}"
    deadline = _command_deadline(config)
    with runtime.lock(args.state_directory, scope, deadline=deadline):
        _verify_mutation_project(config, deadline=deadline)
        # SQLite is re-read on every attempt under the lock shared with storage
        # creation. The fresh database records every platform-created resource;
        # provider inventory reads are deliberately not a removal path.
        application = _application(connection, args.slug)
        resources = db.list_managed_resources(connection, application_id=application.application_id)
        if resources:
            raise ValidationError("remove managed storage before removing the application")
        manifests = db.list_application_manifest_images(connection, application.application_id)
        refs: dict[str, Any] = {
            "application_id": application.application_id,
            "slug": application.slug,
        }
        unfinished = db.get_unfinished_operation(connection, scope)
        if unfinished is not None:
            if unfinished.kind != "app.remove" or any(
                unfinished.refs.get(key) != value for key, value in refs.items()
            ):
                raise db.UnfinishedOperationError(scope, unfinished.operation_id, unfinished.kind)
            operation_id = unfinished.operation_id
            refs = dict(unfinished.refs)
        else:
            operation_id = _begin(
                connection,
                config,
                kind="app.remove",
                scope=scope,
                refs=refs,
            )
        try:
            db.checkpoint_operation(
                connection,
                operation_id,
                phase="storage_absent",
                refs=refs,
            )
            removed = _helper(
                config,
                "app.remove",
                {"slug": application.slug},
                deadline=deadline,
            )
            if removed.get("jobAbsent") is not True or removed.get("variableAbsent") is not True:
                raise app.ApplicationError("job or Variable absence was not confirmed")
            db.checkpoint_operation(connection, operation_id, phase="job_absent", refs=refs)
            worker = _helper(
                config,
                "app.worker.delete",
                {"applicationId": application.application_id, "slug": application.slug},
                deadline=deadline,
            )
            if worker.get("absent") is not True:
                raise app.ApplicationError("worker or fixed-port absence was not confirmed")
            db.checkpoint_operation(connection, operation_id, phase="worker_absent", refs=refs)
            for image in manifests:
                manifest = _helper(
                    config,
                    "app.manifest.delete",
                    {"slug": application.slug, "image": image, "references": []},
                    deadline=deadline,
                )
                if manifest.get("absent") is not True:
                    raise app.ApplicationError("registry manifest absence was not confirmed")
            db.checkpoint_operation(connection, operation_id, phase="manifest_absent", refs=refs)
            db.complete_application_removal(
                connection,
                application_id=application.application_id,
                operation_id=operation_id,
            )
        except Exception as error:
            current = db.get_operation(connection, operation_id)
            if current is not None and current.status == "running":
                db.mark_recovery_required(connection, operation_id, error)
            raise
    print(f"removed={application.slug}", file=output)


def _app_logs(
    args: argparse.Namespace, connection: sqlite3.Connection, config: Config, *, output: Any
) -> None:
    application = _application(connection, args.slug)
    if args.runtime and (args.build_id is not None or args.list_builds):
        raise ValidationError("--id and --list are available only for build logs")
    if args.runtime:
        result = app.application_logs(
            application.slug,
            lines=args.lines,
            follow=args.follow,
            timeout_seconds=config.policy.limits.helper_seconds,
            helper_caller=lambda action, values, **_bounds: _helper(config, action, values),
        )
        print(result.get("text", ""), end="", file=output)
        return
    operations = db.list_application_deploy_operations(connection, application.application_id)
    if not operations:
        raise ValidationError("application has no build attempts")
    if args.list_builds:
        if args.build_id is not None or args.follow:
            raise ValidationError("--list cannot be combined with --id or --follow")
        _table(
            ("BUILD", "STATUS", "PHASE", "STARTED", "COMMIT"),
            tuple(
                (
                    item.operation_id,
                    item.status,
                    item.phase,
                    item.started_at,
                    item.refs.get("source_commit"),
                )
                for item in operations
            ),
            output=output,
        )
        return
    if args.build_id is None:
        operation = operations[0]
    else:
        build_id = uuid(args.build_id, field="build ID")
        matched = next((item for item in operations if item.operation_id == build_id), None)
        if matched is None:
            raise ValidationError("build ID does not belong to the application")
        operation = matched
    print(
        f"build={operation.operation_id} status={operation.status} phase={operation.phase}",
        file=output,
    )
    result = _helper(
        config,
        "app.build.logs",
        {
            "slug": application.slug,
            "buildId": operation.operation_id,
            "lines": args.lines,
            "offset": None,
        },
    )
    if result.get("exists") is True:
        text = result.get("text")
        size = result.get("size")
        next_offset = result.get("nextOffset")
        state = result.get("state")
        if (
            not isinstance(text, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or isinstance(next_offset, bool)
            or not isinstance(next_offset, int)
            or state not in {"running", "complete", "failed", "unknown"}
        ):
            raise app.ApplicationError("helper returned invalid build log evidence")
        print(text, end="", file=output, flush=True)
        while args.follow and state == "running":
            time.sleep(1)
            result = _helper(
                config,
                "app.build.logs",
                {
                    "slug": application.slug,
                    "buildId": operation.operation_id,
                    "lines": args.lines,
                    "offset": next_offset,
                },
            )
            text = result.get("text")
            state = result.get("state")
            candidate_offset = result.get("nextOffset")
            if (
                result.get("exists") is not True
                or not isinstance(text, str)
                or state not in {"running", "complete", "failed", "unknown"}
                or isinstance(candidate_offset, bool)
                or not isinstance(candidate_offset, int)
                or candidate_offset < next_offset
            ):
                raise app.ApplicationError("helper returned invalid build log evidence")
            print(text, end="", file=output, flush=True)
            next_offset = candidate_offset
        return
    if args.follow:
        raise ValidationError("live build log is unavailable for this historical attempt")
    stored = operation.refs.get("build_log_path")
    if not isinstance(stored, str):
        raise ValidationError("build attempt has no captured output")
    path = (args.state_directory / stored).resolve(strict=True)
    root = args.state_directory.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValidationError("build log path is invalid")
    lines = _tail_file_lines(
        path,
        lines=args.lines,
        maximum_bytes=config.policy.limits.build_log_bytes,
    )
    print("\n".join(lines), file=output)


def _storage_row(item: Mapping[str, object]) -> tuple[object, ...]:
    live = cast(Mapping[str, object], item["live"])
    return (
        item["slug"],
        item["type"],
        item["name"],
        item.get("providerId"),
        item.get("providerName"),
        item["lifecycleState"],
        json.dumps(item["quotas"], sort_keys=True),
        item["lastVerifiedAt"],
        live["health"],
    )


def _storage_read(
    args: argparse.Namespace,
    connection: sqlite3.Connection,
    config: Config,
    *,
    output: Any,
) -> None:
    reads = services.ProductReadService(connection, config)
    if args.storage_command == "show":
        models = (reads.storage_resource(args.slug, args.type, args.name),)
    else:
        models = reads.storage(args.slug)
    _table(
        (
            "SLUG",
            "TYPE",
            "NAME",
            "PROVIDER_ID",
            "PROVIDER_NAME",
            "STATE",
            "QUOTA",
            "VERIFIED",
            "HEALTH",
        ),
        tuple(_storage_row(item) for item in models),
        output=output,
    )


def _storage_mutation(
    args: argparse.Namespace, connection: sqlite3.Connection, config: Config, *, output: Any
) -> None:
    resource_types = () if args.storage_command == "verify" and args.type is None else (args.type,)
    result = services.StorageService(connection, config, args.state_directory).mutate(
        services.StorageMutationRequest(
            action=args.storage_command,
            application=args.slug,
            resource_types=resource_types,
            resource_name=args.name,
            confirm_name=args.confirm if args.storage_command == "remove" else None,
            purge_s3=args.purge_s3 if args.storage_command == "remove" else False,
        )
    )
    print(
        f"requested={','.join(result.requested)} completed={','.join(result.completed)}",
        file=output,
    )


def _unlinked_backup_temp(directory: Path, *, suffix: str) -> Path:
    descriptor, name = mkstemp(prefix=".platform-backup-", suffix=suffix, dir=directory)
    path = Path(name)
    try:
        os.close(descriptor)
        descriptor = -1
        path.unlink()
    except OSError:
        if descriptor != -1:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    return path


def _configured_backup_staging_path(config: Config, name: str) -> str:
    """Derive the helper staging path from the installed inventory only."""
    if not re.fullmatch(r"platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age", name):
        raise ValidationError("backup name is malformed")
    try:
        value = config.platform.get("paths.backups")
    except KeyError as error:
        raise ValidationError("configured backup path is missing") from error
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError("configured backup path is malformed")
    root = Path(value)
    if not root.is_absolute() or root == Path("/") or root != Path(os.path.normpath(value)):
        raise ValidationError("configured backup path must be a canonical absolute path")
    staging = root / "m1" / ".staging" / name
    if staging != Path(os.path.normpath(str(staging))) or not staging.is_absolute():
        raise ValidationError("configured backup staging path is unsafe")
    return str(staging)


def _backup(
    args: argparse.Namespace, connection: sqlite3.Connection, config: Config, *, output: Any
) -> None:
    deadline = _command_deadline(config)
    name = datetime.now(UTC).strftime("platform-%Y%m%dT%H%M%SZ.sqlite3.age")
    work = args.state_directory / "backup-work"
    runtime.ensure_private_directory(work)
    plain = _unlinked_backup_temp(work, suffix=".sqlite3")
    encrypted = _unlinked_backup_temp(work, suffix=".sqlite3.age")
    try:
        with runtime.lock(
            args.state_directory,
            "database-maintenance",
            wait=True,
            deadline=deadline,
        ):
            db.backup_database(connection, plain)
            runtime.run(
                (
                    restore._age_executable(),
                    "-r",
                    config.policy.backup_age_recipient,
                    "-o",
                    str(encrypted),
                    str(plain),
                ),
                timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
                stdout_limit=65_536,
                stderr_limit=config.policy.limits.stderr_bytes,
            )
            os.chmod(encrypted, 0o600)
            plaintext_digest = _sha256_file(plain)
            integrity_checked_at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            digest = _sha256_file(encrypted)
            remote_path = _configured_backup_staging_path(config, name)
            runtime.run(
                remote.pinned_admin_scp(encrypted, remote_path),
                timeout_seconds=_remaining(deadline, config.policy.limits.process_seconds),
                stdout_limit=65_536,
                stderr_limit=config.policy.limits.stderr_bytes,
                inherit_env=("HOME", "USER", "SSH_AUTH_SOCK"),
            )
            result = remote.call_helper(
                "backup.accept",
                {
                    "name": name,
                    "sha256": digest,
                    "plaintextSha256": plaintext_digest,
                    "integrityCheckedAt": integrity_checked_at,
                },
                timeout_seconds=_remaining(deadline, config.policy.limits.helper_seconds),
                request_limit=config.policy.limits.helper_request_bytes,
                response_limit=config.policy.limits.helper_response_bytes,
                stderr_limit=config.policy.limits.stderr_bytes,
                helper_command=remote.helper_command_path(config.platform.get("paths.root")),
            )
            if result.get("sha256") != digest:
                raise CliError("backup helper returned mismatched acceptance evidence")
    finally:
        plain.unlink(missing_ok=True)
        encrypted.unlink(missing_ok=True)
    print(f"backup={name} sha256={digest}", file=output)


def _restore(args: argparse.Namespace, *, output: Any) -> None:
    if not args.yes:
        raise ValidationError("offline restore requires --yes")
    identity: db.DeploymentIdentity | None
    try:
        identity = db.deployment_identity(load_platform(args.platform_config))
    except FileNotFoundError:
        if args.platform_config != _DEFAULT_PLATFORM:
            raise
        # Direct source-tree restore tests and controlled legacy offline
        # restores may not have the installed inventory.  Installed launchers
        # validate and pin this file before reaching the module.
        identity = None
    with runtime.lock(args.state_directory, "database-maintenance", wait=False):
        result = restore.restore_database(
            args.backup,
            args.state_directory / "platform.sqlite3",
            age_identity=args.age_identity,
            identity=identity,
        )
    print(
        f"restore=verified schema-version={result.schema_version} integrity={result.integrity}",
        file=output,
    )


def dispatch(
    args: argparse.Namespace,
    *,
    stdin: Any = sys.stdin,
    stdout: Any = sys.stdout,
) -> None:
    if args.command == "restore":
        _restore(args, output=stdout)
        return
    if args.command == "setup":

        def setup_input(prompt: str) -> str:
            print(prompt, end="", file=stdout, flush=True)
            value = stdin.readline()
            if not isinstance(value, str) or value == "":
                raise setup.SetupError("setup input ended before all required values were supplied")
            return value.rstrip("\n")

        setup.run_setup(
            env_file=args.env_file,
            workspace=args.workspace,
            cloudflare_token=args.cloudflare_token_file,
            apply=args.apply,
            input_reader=setup_input,
            output=stdout,
        )
        return
    command_started = time.monotonic()
    config = _load_config(args)
    deadline = command_started + config.policy.limits.process_seconds
    with _database(args, deadline=deadline) as connection:
        if args.command == "status":
            _status_command(connection, config, output=stdout)
            return
        if args.command == "infra" and args.infra_command == "list":
            _infra_list(connection, config, output=stdout)
            return
        if args.command == "app" and args.app_command in {"list", "show"}:
            _app_read(args, connection, config, output=stdout)
            return
        if args.command == "storage" and args.storage_command in {"list", "show"}:
            _storage_read(args, connection, config, output=stdout)
            return

        if args.command == "backup":
            _backup(args, connection, config, output=stdout)
        elif args.command == "infra":
            if args.infra_command == "image":
                if args.image_command == "list":
                    _infra_image_list(config, output=stdout)
                elif args.image_command == "set":
                    _infra_image_set(args, connection, config, output=stdout)
                else:
                    _infra_prune(args, connection, config, input_stream=stdin, output=stdout)
            elif args.infra_command in {"start", "stop", "reboot"}:
                _infra_power(args, connection, config, input_stream=stdin, output=stdout)
            elif args.infra_command == "logs":
                print(
                    openstack.host_logs(config.platform, args.host, lines=args.lines),
                    end="",
                    file=stdout,
                )
            else:
                _infra_replace(
                    args,
                    connection,
                    config,
                    input_stream=stdin,
                    output=stdout,
                )
        elif args.command == "app":
            if args.app_command == "env":
                if args.env_command == "list":
                    result = services.EnvironmentService(
                        connection,
                        config,
                        args.state_directory,
                    ).list(args.slug)
                    _table(("KEY",), tuple((item,) for item in result.key_names), output=stdout)
                else:
                    _environment_mutation(args, connection, config, stdin=stdin, output=stdout)
            elif args.app_command == "logs":
                _app_logs(args, connection, config, output=stdout)
            elif args.app_command == "create":
                _app_create(args, connection, config, output=stdout)
            elif args.app_command == "deploy":
                _app_deploy(args, connection, config, output=stdout)
            else:
                _app_remove(args, connection, config, output=stdout)
        elif args.command == "storage":
            _storage_mutation(args, connection, config, output=stdout)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv)
        dispatch(args)
        return EXIT_OK
    except (ValidationError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except (
        runtime.LockBusy,
        db.UnfinishedOperationError,
        openstack.DriftError,
        openstack.RecoveryRequired,
    ) as error:
        print(f"conflict: {error}", file=sys.stderr)
        return EXIT_CONFLICT
    except (DependencyUnavailable, remote.DependencyUnavailable) as error:
        print(f"unavailable: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except (
        CliError,
        restore.RestoreError,
        app.ApplicationError,
        openstack.OpenStackError,
        storage.StorageOperationError,
        services.ServiceDeadlineError,
        setup.SetupError,
        remote.HelperError,
        remote.ProtocolError,
        runtime.RuntimeFailure,
        db.DatabaseError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:
        correlation_id = str(uuid_module.uuid4())
        if args is not None:
            try:
                runtime.ensure_private_directory(args.state_directory)
                runtime.write_private_stack_diagnostic(
                    args.state_directory / "diagnostics",
                    error,
                    correlation_id=correlation_id,
                )
            except Exception:
                pass
        print(
            f"error: unexpected management failure; correlation ID {correlation_id}",
            file=sys.stderr,
        )
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
