"""Staff operator command tree and integration-owned orchestration.

The CLI keeps presentation and confirmation here while feature modules retain
provider behavior. It never renders secret values or unrestricted provider
responses.
"""

from __future__ import annotations

import argparse
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
from tempfile import mkstemp
from typing import Any, cast

from . import openstack, remote, restore, runtime, setup
from .config import Config, load, load_platform
from .contracts import CONTROLLER_BACKUP_DIRECTORY
from .controller import application_runtime as app
from .controller import database as db
from .controller import status, storage
from .installation import DEFAULT_OPERATOR_INVENTORY, OPERATOR_ROOT, OPERATOR_STATE
from .validation import ValidationError, bounded_text, commit, sha256_hex, uuid

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_UNAVAILABLE = 4

_MAX_LOG_LINES = 2_000
_DEFAULT_STATE = OPERATOR_STATE
_DEFAULT_PLATFORM = Path(os.environ.get("PLATFORM_CONFIG", str(DEFAULT_OPERATOR_INVENTORY)))
_ACTIVE_COMMAND_DEADLINE: ContextVar[float | None] = ContextVar(
    "operator_command_deadline", default=None
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
    setup_command.add_argument(
        "setup_action",
        nargs="?",
        choices=("check",),
        default=None,
        help="resolve a non-mutating deployment plan",
    )
    setup_command.add_argument("--env-file", type=Path, required=True)
    setup_command.add_argument("--workspace", type=Path, default=OPERATOR_ROOT / "setup")
    setup_command.add_argument("--cloudflare-token-file", type=Path)
    setup_command.add_argument("--json", action="store_true", help="emit the setup check as JSON")
    setup_command.add_argument(
        "--apply", action="store_true", help="build images and create the deployment"
    )

    commands.add_parser("status", help="show accepted state and bounded live availability")
    commands.add_parser("backup", help="back up and encrypt controller SQLite state")
    restore_command = commands.add_parser(
        "restore", help="verify and atomically replace controller SQLite state offline"
    )
    restore_command.add_argument("backup", type=Path)
    restore_command.add_argument("--age-identity", type=Path)
    restore_command.add_argument(
        "--yes", action="store_true", help="confirm replacement of the controller database"
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
    staging = root / CONTROLLER_BACKUP_DIRECTORY / ".staging" / name
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
    identity = db.deployment_identity(load_platform(args.platform_config))
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
        if args.apply and args.setup_action == "check":
            raise setup.SetupError("setup check cannot be combined with --apply")

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
            json_output=args.json,
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
            f"error: unexpected operator failure; correlation ID {correlation_id}",
            file=sys.stderr,
        )
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
