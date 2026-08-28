"""Bounded application runtime and build log reads."""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..validation import ValidationError, uuid
from . import application_runtime as app
from . import database as db
from .service_support import HelperCaller, operation_deadline


@dataclass(frozen=True, slots=True)
class BuildAttempt:
    build_id: str
    status: str
    phase: str
    started_at: str
    source_commit: str | None


@dataclass(frozen=True, slots=True)
class LogChunk:
    text: str
    state: str
    next_offset: int
    truncated: bool


def tail_file(path: Path, *, lines: int, maximum_bytes: int) -> str:
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
        return "\n".join(
            b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-lines:]
        )
    finally:
        os.close(descriptor)


class LogService:
    """Return bounded runtime and build logs without presentation concerns."""

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

    def _application(self, identifier: str) -> db.Application:
        application = db.get_application(self.connection, identifier)
        if application is None:
            raise ValidationError("application does not exist")
        return application

    def runtime(
        self,
        application_identifier: str,
        *,
        lines: int,
    ) -> LogChunk:
        application = self._application(application_identifier)
        result = app.application_logs(
            application.slug,
            lines=lines,
            follow=False,
            timeout_seconds=self.config.policy.limits.helper_seconds,
            helper_caller=lambda action, values, **_bounds: self.helper_caller(
                self.config,
                action,
                values,
                deadline=operation_deadline(self.config),
            ),
        )
        text = result.get("text")
        if not isinstance(text, str):
            raise app.ApplicationError("helper returned invalid runtime log evidence")
        return LogChunk(text, "running", len(text.encode()), bool(result.get("truncated")))

    def build(
        self,
        application_identifier: str,
        *,
        build_id: str,
        lines: int,
        offset: int | None = None,
    ) -> tuple[BuildAttempt, LogChunk]:
        application = self._application(application_identifier)
        operations = db.list_application_deploy_operations(
            self.connection,
            application.application_id,
        )
        if not operations:
            raise ValidationError("application has no build attempts")
        selected_id = uuid(build_id, field="build ID")
        operation = next(
            (item for item in operations if item.operation_id == selected_id),
            None,
        )
        if operation is None:
            raise ValidationError("build ID does not belong to the application")
        attempt = BuildAttempt(
            operation.operation_id,
            operation.status,
            operation.phase,
            operation.started_at,
            operation.refs.get("source_commit")
            if isinstance(operation.refs.get("source_commit"), str)
            else None,
        )
        result = self.helper_caller(
            self.config,
            "app.build.logs",
            {
                "slug": application.slug,
                "buildId": operation.operation_id,
                "lines": lines,
                "offset": offset,
            },
            deadline=operation_deadline(self.config),
        )
        if result.get("exists") is True:
            text = result.get("text")
            next_offset = result.get("nextOffset")
            state = result.get("state")
            if (
                not isinstance(text, str)
                or isinstance(next_offset, bool)
                or not isinstance(next_offset, int)
                or state not in {"running", "complete", "failed", "unknown"}
            ):
                raise app.ApplicationError("helper returned invalid build log evidence")
            return attempt, LogChunk(
                text,
                state,
                next_offset,
                bool(result.get("truncated")),
            )
        if offset is not None:
            raise ValidationError("live build log is unavailable for this historical attempt")
        stored = operation.refs.get("build_log_path")
        if not isinstance(stored, str):
            raise ValidationError("build attempt has no captured output")
        root = self.state_directory.resolve(strict=True)
        path = (self.state_directory / stored).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValidationError("build log path is invalid")
        text = tail_file(
            path,
            lines=lines,
            maximum_bytes=self.config.policy.limits.build_log_bytes,
        )
        return attempt, LogChunk(
            text + ("\n" if text else ""),
            "complete",
            path.stat().st_size,
            False,
        )
