"""Write-only application environment orchestration."""

from __future__ import annotations

import sqlite3
import uuid as uuid_module
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .. import openstack, runtime
from . import application_runtime as app
from . import database as db
from ..config import Config
from .service_support import (
    HelperCaller,
    operation_deadline,
    reject_deleting,
    remaining_seconds,
    wall_deadline,
)
from .storage_contract import PLATFORM_ENVIRONMENT_KEYS, RESERVED_ENVIRONMENT_PREFIX
from ..validation import ValidationError, env_key, uuid

EnvironmentAction = Literal["set", "unset", "import"]


@dataclass(frozen=True, slots=True)
class EnvironmentMutationRequest:
    action: EnvironmentAction
    application: str
    updates: Mapping[str, str] = field(default_factory=dict, repr=False)
    removals: tuple[str, ...] = ()
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentMutationResult:
    key_names: tuple[str, ...]
    modify_index: int | None


class EnvironmentService:
    """Own write-only environment intent, recovery, CAS evidence, and metadata."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
        *,
        helper_caller: HelperCaller,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory
        self.helper_caller = helper_caller

    def _helper(
        self,
        action: str,
        values: Mapping[str, object],
        *,
        deadline: float,
    ) -> Mapping[str, object]:
        return self.helper_caller(self.config, action, values, deadline=deadline)

    def _ownership(self, application_id: str) -> dict[str, str]:
        return {
            item.key_name: item.owner
            for item in db.list_environment_keys(
                self.connection,
                application_id=application_id,
            )
        }

    def _recover_interrupted(
        self,
        application: db.Application,
        ownership: Mapping[str, str],
        operation: db.Operation,
        *,
        deadline: float,
    ) -> None:
        intended = operation.refs.get("key_names")
        mutation = operation.refs.get("mutation")
        if (
            not isinstance(intended, list)
            or any(not isinstance(item, str) for item in intended)
            or mutation not in {"set", "unset"}
        ):
            raise app.ApplicationError("interrupted environment intent is malformed")
        intended_names = {env_key(item) for item in intended}
        if intended_names & PLATFORM_ENVIRONMENT_KEYS or any(
            name.startswith(RESERVED_ENVIRONMENT_PREFIX) for name in intended_names
        ):
            raise app.ApplicationError("interrupted staff environment intent used a reserved key")
        observed = app.list_environment(
            application.slug,
            timeout_seconds=remaining_seconds(deadline, self.config.policy.limits.helper_seconds),
            helper_caller=lambda action, values, **_bounds: self._helper(
                action, values, deadline=deadline
            ),
        )
        names = observed.get("keys")
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise app.ApplicationError("helper returned invalid interrupted environment evidence")
        present = {env_key(item) for item in names}
        recovered = {
            name for name, owner in ownership.items() if owner == "staff" and name in present
        }
        if mutation == "set":
            recovered.update(
                name
                for name in intended_names & present
                if ownership.get(name, "staff") == "staff"
            )
        db.set_environment_keys(
            self.connection,
            application_id=application.application_id,
            owner="staff",
            keys=sorted(recovered),
        )
        db.mark_failed(
            self.connection,
            operation.operation_id,
            "interrupted environment state was observed before explicit retry",
            cleanup_state="not_required",
        )

    def mutate(self, request: EnvironmentMutationRequest) -> EnvironmentMutationResult:
        application = db.get_application(self.connection, request.application)
        if application is None:
            raise ValidationError("application does not exist")
        reject_deleting(self.connection, application)
        if request.action not in {"set", "unset", "import"}:
            raise ValidationError("environment action is invalid")
        updates = {env_key(key): value for key, value in request.updates.items()}
        removals = tuple(env_key(key) for key in request.removals)
        overlap = set(updates) & set(removals)
        if overlap:
            raise ValidationError("environment keys cannot be both set and removed")
        if request.action == "unset" and not removals:
            raise ValidationError("select at least one environment key to remove")
        if request.action != "unset" and removals:
            raise ValidationError("environment removals require the unset action")
        if request.action == "unset" and updates:
            raise ValidationError("environment updates require set or import")

        deadline = operation_deadline(self.config)
        scope = f"app-{application.application_id}"
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            openstack.verify_project(
                self.config.platform,
                timeout_seconds=remaining_seconds(
                    deadline,
                    self.config.policy.limits.process_seconds,
                ),
            )
            refreshed = db.get_application(self.connection, request.application)
            if refreshed is None or refreshed.application_id != application.application_id:
                raise ValidationError("application does not exist")
            ownership = self._ownership(refreshed.application_id)
            environment_revision = db.get_environment_revision(
                self.connection, refreshed.application_id
            )
            if environment_revision is None:
                raise db.DatabaseError("application environment revision is missing")
            unfinished = db.get_unfinished_operation(self.connection, scope)
            if unfinished is not None:
                if not unfinished.kind.startswith("app.env."):
                    raise db.UnfinishedOperationError(
                        scope,
                        unfinished.operation_id,
                        unfinished.kind,
                    )
                self._recover_interrupted(
                    refreshed,
                    ownership,
                    unfinished,
                    deadline=deadline,
                )
                ownership = self._ownership(refreshed.application_id)

            intended_names = sorted(set(updates) | set(removals))
            protected = {
                name
                for name in intended_names
                if name in PLATFORM_ENVIRONMENT_KEYS
                or name.startswith(RESERVED_ENVIRONMENT_PREFIX)
                or (name in ownership and ownership[name] != "staff")
            }
            if protected:
                first = sorted(protected)[0]
                raise ValidationError(
                    f"environment key {first!r} is reserved for the platform or storage"
                )
            mutation = "unset" if removals else "set"
            operation_id = (
                str(uuid_module.uuid4())
                if request.request_id is None
                else uuid(request.request_id, field="environment operation ID")
            )
            db.begin_operation(
                self.connection,
                operation_id=operation_id,
                kind=f"app.env.{request.action}",
                scope=scope,
                phase="intent_recorded",
                deadline_at=wall_deadline(deadline),
                refs={"key_names": intended_names, "mutation": mutation},
            )
            try:
                helper_caller = lambda action, values, **_bounds: self._helper(
                    action, values, deadline=deadline
                )
                if removals:
                    result = app.remove_environment(
                        refreshed.slug,
                        removals,
                        ownership,
                        timeout_seconds=remaining_seconds(
                            deadline,
                            self.config.policy.limits.helper_seconds,
                        ),
                        helper_caller=helper_caller,
                    )
                else:
                    result = app.set_environment(
                        refreshed.slug,
                        updates,
                        ownership,
                        timeout_seconds=remaining_seconds(
                            deadline,
                            self.config.policy.limits.helper_seconds,
                        ),
                        helper_caller=helper_caller,
                    )
                names = result.get("keys")
                if not isinstance(names, list) or any(
                    not isinstance(item, str) for item in names
                ):
                    raise app.ApplicationError("helper returned invalid environment key evidence")
                if (
                    refreshed.desired_running
                    and db.get_deployment(self.connection, refreshed.application_id) is not None
                    and (
                        result.get("restarted") is not True
                        or result.get("schedulerHealthy") is not True
                        or result.get("publicHealthy") is not True
                    )
                ):
                    raise app.ApplicationError(
                        "environment restart and health evidence was not confirmed"
                    )
                present = {env_key(item) for item in names}
                prior_staff = {name for name, owner in ownership.items() if owner == "staff"}
                db.set_environment_keys(
                    self.connection,
                    application_id=refreshed.application_id,
                    owner="staff",
                    keys=sorted((prior_staff | set(updates)) & present),
                )
                db.advance_environment_revision(
                    self.connection,
                    application_id=refreshed.application_id,
                    expected_revision=environment_revision.revision,
                )
                db.checkpoint_operation(
                    self.connection,
                    operation_id,
                    phase="accepted",
                    refs={"key_names": intended_names, "mutation": mutation},
                )
                db.mark_succeeded(
                    self.connection,
                    operation_id,
                    cleanup_state="not_required",
                )
            except Exception as error:
                db.mark_recovery_required(self.connection, operation_id, error)
                raise
        modify_index = result.get("modifyIndex")
        if modify_index is not None and (
            isinstance(modify_index, bool) or not isinstance(modify_index, int)
        ):
            raise app.ApplicationError("helper returned invalid environment revision evidence")
        return EnvironmentMutationResult(tuple(sorted(present)), modify_index)
