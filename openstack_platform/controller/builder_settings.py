"""Controller-held builder size and audited, plan-free selection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import openstack, runtime
from ..config import Config
from ..validation import ValidationError, flavor_reference
from . import database as db
from .service_support import operation_deadline, remaining_seconds, wall_deadline

DEFAULT_BUILDER_SIZE_KIND = "infra.builder-size.set"
APP_BUILDER_SIZE_KIND = "app.builder-size.set"


def default_flavor(connection: sqlite3.Connection, config: Config) -> str:
    selected = db.get_default_builder_flavor(connection)
    return (
        selected
        if selected is not None
        else flavor_reference(config.platform.get("flavors.builder"))
    )


def effective_flavor(connection: sqlite3.Connection, config: Config, application_id: str) -> str:
    selected = db.get_application_builder_flavor(connection, application_id)
    return selected if selected is not None else default_flavor(connection, config)


def validate_flavor(flavor: openstack.Flavor) -> None:
    if flavor.vcpus < 1 or flavor.ram_mib < 1024:
        raise ValidationError("builder size requires at least 1 vCPU and 1024 MiB RAM")


class BuilderSettingsService:
    def __init__(self, connection: sqlite3.Connection, config: Config, state_directory: Path):
        self.connection = connection
        self.config = config
        self.state_directory = state_directory

    def select(
        self,
        reference: str | None,
        expected: str | None,
        *,
        request_id: str,
        application_id: str | None = None,
    ) -> None:
        if application_id is None and (reference is None or expected is None):
            raise ValidationError("default builder size requires a flavor and expected flavor")
        reference = None if reference is None else flavor_reference(reference)
        expected = None if expected is None else flavor_reference(expected)
        scope = "infrastructure" if application_id is None else f"app-{application_id}"
        kind = DEFAULT_BUILDER_SIZE_KIND if application_id is None else APP_BUILDER_SIZE_KIND
        deadline = operation_deadline(self.config)
        intent = {"flavor": reference, "expected_flavor": expected}
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            operation = db.get_unfinished_operation(self.connection, scope)
            if operation is not None:
                if (
                    operation.operation_id != request_id
                    or operation.kind != kind
                    or any(operation.refs.get(key) != value for key, value in intent.items())
                ):
                    raise db.UnfinishedOperationError(scope, operation.operation_id, operation.kind)
                operation = db.renew_operation_deadline(
                    self.connection, request_id, wall_deadline(deadline)
                )
            else:
                operation = db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind=kind,
                    scope=scope,
                    phase="validated",
                    deadline_at=wall_deadline(deadline),
                    refs=intent,
                )
            try:
                current = (
                    default_flavor(self.connection, self.config)
                    if application_id is None
                    else db.get_application_builder_flavor(self.connection, application_id)
                )
                if operation.phase not in {"validated", "selection_observed"}:
                    raise db.DatabaseError("builder selection has an unknown recovery phase")
                already_written = (
                    operation.phase == "selection_observed"
                    and current == operation.refs.get("selected_flavor")
                )
                if current != expected and not already_written:
                    raise ValidationError(
                        "builder size changed; read current size and submit a new request"
                    )
                flavor = openstack.observe_flavor_capacity(
                    self.config.platform,
                    reference
                    if reference is not None
                    else default_flavor(self.connection, self.config),
                    timeout_seconds=remaining_seconds(
                        deadline, self.config.policy.limits.process_seconds
                    ),
                )
                validate_flavor(flavor)
                observed = {
                    **intent,
                    "selected_flavor": flavor.name if reference is not None else None,
                }
                if operation.phase == "selection_observed" and operation.refs != observed:
                    raise openstack.DriftError("recorded builder flavor identity changed")
                db.checkpoint_operation(
                    self.connection, request_id, phase="selection_observed", refs=observed
                )
                if not already_written:
                    if application_id is None:
                        db.put_default_builder_flavor(self.connection, flavor.name)
                    else:
                        db.put_application_builder_flavor(
                            self.connection,
                            application_id,
                            flavor.name if reference is not None else None,
                        )
                db.mark_succeeded(self.connection, request_id, cleanup_state="not_required")
            except Exception:
                recorded = db.get_operation(self.connection, request_id)
                if recorded is not None and recorded.phase == "validated":
                    db.mark_failed(
                        self.connection,
                        request_id,
                        "builder size validation failed; selection unchanged",
                        cleanup_state="not_required",
                    )
                else:
                    db.mark_recovery_required(
                        self.connection,
                        request_id,
                        "builder size selection requires reconciliation",
                    )
                raise
