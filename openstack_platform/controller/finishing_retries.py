"""Durable, bounded retries of accepted operations' idempotent finishing work."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from ..runtime import safe_summary
from . import database as db

# Five automatic attempts after the initial failure, with 25 minutes of backoff.
RETRY_DELAYS = (60, 120, 240, 480, 600)


def schedule(connection: sqlite3.Connection, operation_id: str, error: BaseException | str) -> bool:
    """Retain the original intent; only exhausted retries need human recovery."""
    with db.transaction(connection):
        operation = db.get_operation(connection, operation_id)
        if (
            operation is None
            or not operation.finishing
            or operation.status in {"succeeded", "failed"}
        ):
            return False
        retry = dict(operation.refs.get("finishing_retry", {}))
        attempts = int(retry.get("attempts", 0))
        next_at = None
        if attempts < len(RETRY_DELAYS):
            next_at = (
                (datetime.now(UTC) + timedelta(seconds=RETRY_DELAYS[attempts]))
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
        retry.update(attempts=attempts, next_at=next_at)
        connection.execute(
            "UPDATE operations SET status = ?, refs_json = ?, safe_error = ?, updated_at = ? WHERE operation_id = ?",
            (
                "running" if next_at else "recovery_required",
                db._refs_json({**operation.refs, "finishing_retry": retry}),
                safe_summary(error),
                db.utc_now(),
                operation_id,
            ),
        )
        return next_at is not None


def due(connection: sqlite3.Connection) -> tuple[db.Operation, ...]:
    rows = connection.execute(
        "SELECT o.operation_id FROM operations o JOIN operation_dispatches d ON d.operation_id = o.operation_id "
        "WHERE o.finishing = 1 AND o.status = 'running' AND d.status = 'recovery_required' "
        "AND json_extract(o.refs_json, '$.finishing_retry.next_at') <= ? "
        "ORDER BY json_extract(o.refs_json, '$.finishing_retry.next_at') LIMIT 32",
        (db.utc_now(),),
    ).fetchall()
    return tuple(
        operation for row in rows if (operation := db.get_operation(connection, row[0])) is not None
    )
