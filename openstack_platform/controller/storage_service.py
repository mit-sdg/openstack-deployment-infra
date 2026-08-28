"""Managed-storage mutation orchestration."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import openstack, runtime
from . import database as db
from . import storage
from ..config import Config
from .service_support import (
    HelperCaller,
    operation_deadline,
    reject_deleting,
    remaining_seconds,
    wall_deadline,
)
from ..validation import ValidationError, resource_name, uuid

StorageAction = Literal["create", "verify", "rotate", "remove"]


@dataclass(frozen=True, slots=True)
class StorageMutationRequest:
    action: StorageAction
    application: str
    resource_types: tuple[str, ...]
    resource_name: str = "default"
    confirm_name: str | None = None
    purge_s3: bool = False
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class StorageMutationResult:
    requested: tuple[str, ...]
    completed: tuple[str, ...]


class StorageService:
    """Own locking, project verification, and managed-storage dispatch."""

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

    def mutate(self, request: StorageMutationRequest) -> StorageMutationResult:
        application = db.get_application(self.connection, request.application)
        if application is None:
            raise ValidationError("application does not exist")
        reject_deleting(self.connection, application)
        checked_name = resource_name(request.resource_name)
        if request.action not in {"create", "verify", "rotate", "remove"}:
            raise ValidationError("storage action is invalid")
        if not isinstance(request.purge_s3, bool):
            raise ValidationError("purge_s3 must be boolean")
        selected = request.resource_types
        if not selected:
            raise ValidationError("select at least one storage type")

        deadline = operation_deadline(self.config)
        with runtime.lock(
            self.state_directory,
            f"app-{application.application_id}",
            deadline=deadline,
        ):
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
            durable_deadline = wall_deadline(deadline)
            operation_id = (
                None
                if request.request_id is None
                else uuid(request.request_id, field="storage operation ID")
            )

            def call_helper(
                action: str, values: Mapping[str, object], **_bounds: object
            ) -> Mapping[str, object]:
                return self.helper_caller(
                    self.config, action, values, deadline=deadline
                )

            if request.action == "create":
                result = storage.create(
                    self.connection,
                    self.config,
                    refreshed.application_id,
                    selected,
                    resource_name=checked_name,
                    helper_caller=call_helper,
                    operation_id=operation_id,
                    deadline_at=durable_deadline,
                    process_deadline=deadline,
                )
            elif request.action == "verify":
                result = storage.verify(
                    self.connection,
                    self.config,
                    refreshed.application_id,
                    selected,
                    resource_name=checked_name,
                    helper_caller=call_helper,
                    operation_id=operation_id,
                    deadline_at=durable_deadline,
                    process_deadline=deadline,
                )
            elif request.action == "rotate":
                result = storage.rotate(
                    self.connection,
                    self.config,
                    refreshed.application_id,
                    selected,
                    resource_name=checked_name,
                    helper_caller=call_helper,
                    operation_id=operation_id,
                    deadline_at=durable_deadline,
                    process_deadline=deadline,
                )
            else:
                result = storage.remove(
                    self.connection,
                    self.config,
                    refreshed.application_id,
                    selected,
                    resource_name=checked_name,
                    confirm_name=request.confirm_name,
                    confirm_destructive=True,
                    purge_s3=request.purge_s3,
                    helper_caller=call_helper,
                    operation_id=operation_id,
                    deadline_at=durable_deadline,
                    process_deadline=deadline,
                )
        return StorageMutationResult(result.requested, result.completed)
