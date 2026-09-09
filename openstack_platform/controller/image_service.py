"""Hosted role-image metadata selection and durable provisioning snapshots."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from .. import openstack, runtime
from ..config import Config
from ..contracts import IMAGE_ROLES
from ..validation import ValidationError, uuid
from . import database as db
from .service_support import operation_deadline, remaining_seconds, wall_deadline

HOSTED_IMAGE_ROLES = IMAGE_ROLES
IMAGE_SELECTION_KIND = "infra.image.set"


def hosted_role(value: object) -> str:
    if not isinstance(value, str) or value not in HOSTED_IMAGE_ROLES:
        raise ValidationError("hosted image selection role must be a platform image role")
    return value


def pin_deployment_images(
    connection: sqlite3.Connection,
    state_directory: Path,
    operation: db.Operation,
    roles: Sequence[str],
    *,
    deadline: float,
) -> db.Operation:
    """Record first-use selections together; recovery never replaces recorded IDs."""
    with runtime.lock(state_directory, "infrastructure", deadline=deadline):
        current = db.get_operation(connection, operation.operation_id)
        if current is None:
            raise db.DatabaseError("deployment operation is missing")
        refs: dict[str, object] = {}
        for role in roles:
            if role not in {"worker", "builder"}:
                raise ValidationError("deployment provisioning can pin only worker or builder")
            key = f"{role}_image_id"
            if key in current.refs:
                refs[key] = uuid(current.refs[key], field=f"recorded {role} image UUID")
            else:
                selected = db.get_image_selection(connection, role)
                if selected is None:
                    raise ValidationError(f"select a hosted {role} image before deployment")
                refs[key] = selected.image_id
        return db.checkpoint_operation(
            connection, current.operation_id, phase=current.phase, refs=refs, merge_refs=True
        )


class ImageSelectionService:
    """CAS selection in hosted state; never mutate provider resources or running VMs."""

    def __init__(self, connection: sqlite3.Connection, config: Config, state_directory: Path):
        self.connection = connection
        self.config = config
        self.state_directory = state_directory

    def select(self, role: str, image_id: str, expected_image_id: str, *, request_id: str) -> None:
        role = hosted_role(role)
        image_id = uuid(image_id, field="image UUID")
        expected_image_id = uuid(expected_image_id, field="expected current image UUID")
        request_id = uuid(request_id, field="image selection request UUID")
        deadline = operation_deadline(self.config)
        intent = {"role": role, "image_id": image_id, "expected_image_id": expected_image_id}
        with runtime.lock(self.state_directory, "infrastructure", deadline=deadline):
            operation = db.get_unfinished_operation(self.connection, "infrastructure")
            if operation is not None:
                if (
                    operation.operation_id != request_id
                    or operation.kind != IMAGE_SELECTION_KIND
                    or any(operation.refs.get(key) != value for key, value in intent.items())
                ):
                    raise db.UnfinishedOperationError(
                        "infrastructure", operation.operation_id, operation.kind
                    )
                operation = db.renew_operation_deadline(
                    self.connection, request_id, wall_deadline(deadline)
                )
            else:
                operation = db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind=IMAGE_SELECTION_KIND,
                    scope="infrastructure",
                    phase="validated",
                    deadline_at=wall_deadline(deadline),
                    refs=intent,
                )
            try:
                current = db.get_image_selection(self.connection, role)
                if current is None:
                    raise ValidationError(
                        "hosted image selection is missing; initialize controller images first"
                    )
                if operation.phase not in {"validated", "selection_observed"}:
                    raise db.DatabaseError("image selection has an unknown recovery phase")
                # Only the observed phase can have committed a selection before a
                # crash. It must still match the exact saved provider projection.
                already_written = operation.phase == "selection_observed" and (
                    current.image_id == image_id
                    and current.display_name == operation.refs.get("display_name")
                    and current.source_commit == operation.refs.get("source_commit")
                    and current.compatibility_hash == operation.refs.get("compatibility_hash")
                )
                if current.image_id != expected_image_id and not already_written:
                    raise ValidationError(
                        "hosted image selection changed; read current selection and submit a new request"
                    )
                selected = openstack.select_image(
                    self.config.platform,
                    role,
                    image_id,
                    timeout_seconds=remaining_seconds(
                        deadline, self.config.policy.limits.process_seconds
                    ),
                )
                observed = {
                    **intent,
                    "display_name": selected.display_name,
                    "source_commit": selected.source_commit,
                    "compatibility_hash": selected.compatibility_hash,
                }
                if operation.phase == "selection_observed" and operation.refs != observed:
                    raise openstack.DriftError("recorded hosted image provenance changed")
                db.checkpoint_operation(
                    self.connection, request_id, phase="selection_observed", refs=observed
                )
                if not already_written:
                    db.put_image_selection(
                        self.connection,
                        role=role,
                        image_id=selected.image_id,
                        display_name=selected.display_name,
                        source_commit=selected.source_commit,
                        compatibility_hash=selected.compatibility_hash,
                    )
                db.mark_succeeded(self.connection, request_id, cleanup_state="not_required")
            except Exception:
                current_operation = db.get_operation(self.connection, request_id)
                if current_operation is not None and current_operation.phase == "validated":
                    db.mark_failed(
                        self.connection,
                        request_id,
                        "hosted image selection validation failed; selection unchanged",
                        cleanup_state="not_required",
                    )
                else:
                    db.mark_recovery_required(
                        self.connection,
                        request_id,
                        "hosted image selection requires reconciliation",
                    )
                raise
