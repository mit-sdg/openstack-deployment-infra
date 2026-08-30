"""Bounded in-process execution for durably accepted controller mutations."""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from . import database as db

OperationWork = Callable[[sqlite3.Connection], object]


class AsyncOperationExecutor:
    """Execute accepted work off request threads with bounded memory use.

    The dispatch journal contains no request payloads (environment mutations can
    contain secrets).  Consequently startup never guesses or replays work: an
    interrupted dispatch is made recovery-required before the socket is served.
    """

    def __init__(
        self,
        database_path: Path,
        startup_connection: sqlite3.Connection,
        *,
        workers: int = 4,
        capacity: int = 32,
    ) -> None:
        if workers < 1 or capacity < workers:
            raise ValueError("async operation bounds are invalid")
        self.database_path = database_path
        self._slots = threading.BoundedSemaphore(capacity)
        self._queue: queue.Queue[tuple[str, OperationWork] | None] = queue.Queue()
        self._closed = False
        self._state_lock = threading.Lock()
        self._recover_startup(startup_connection)
        self._threads = [
            threading.Thread(
                target=self._run,
                name=f"controller-operation-{index + 1}",
                daemon=False,
            )
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def _recover_startup(self, connection: sqlite3.Connection) -> None:
        for dispatch in db.list_operation_dispatches(connection):
            if dispatch.status not in {"pending", "running"}:
                continue
            operation = db.get_operation(connection, dispatch.operation_id)
            if operation is not None and operation.status in {"succeeded", "failed"}:
                db.set_operation_dispatch_status(connection, dispatch.operation_id, "finished")
                continue
            if operation is None:
                try:
                    operation = db.begin_operation(
                        connection,
                        operation_id=dispatch.operation_id,
                        kind=dispatch.kind,
                        scope=dispatch.scope,
                        phase="startup_interrupted",
                        deadline_at=db.utc_now(),
                    )
                except db.UnfinishedOperationError:
                    operation = None
            if operation is not None and operation.status == "running":
                if dispatch.status == "pending":
                    db.mark_failed(
                        connection,
                        operation.operation_id,
                        "queued operation was not started before controller restart",
                        cleanup_state="not_required",
                    )
                    db.set_operation_dispatch_status(connection, dispatch.operation_id, "finished")
                    continue
                db.mark_recovery_required(
                    connection,
                    operation.operation_id,
                    "controller stopped before asynchronous work completed",
                    phase="startup_interrupted",
                )
            db.set_operation_dispatch_status(
                connection,
                dispatch.operation_id,
                "recovery_required",
                error="controller stopped before asynchronous work completed",
            )

    def submit(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        kind: str,
        scope: str,
        work: OperationWork,
    ) -> db.OperationDispatch:
        with self._state_lock:
            if self._closed:
                raise db.DispatchQueueFullError("operation executor is stopping")
        if not self._slots.acquire(blocking=False):
            raise db.DispatchQueueFullError("operation executor is at capacity")
        try:
            dispatch, created = db.enqueue_operation_dispatch(
                connection,
                operation_id=operation_id,
                kind=kind,
                scope=scope,
            )
            if not created:
                self._slots.release()
                return dispatch
            self._queue.put_nowait((operation_id, work))
            return dispatch
        except BaseException:
            self._slots.release()
            raise

    def resubmit_recovery(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        kind: str,
        scope: str,
        work: OperationWork,
    ) -> db.OperationDispatch:
        """Re-dispatch recovery using only the identical caller-supplied body."""
        with self._state_lock:
            if self._closed:
                raise db.DispatchQueueFullError("operation executor is stopping")
        if not self._slots.acquire(blocking=False):
            raise db.DispatchQueueFullError("operation executor is at capacity")
        try:
            dispatch = db.requeue_recovery_dispatch(
                connection,
                operation_id=operation_id,
                kind=kind,
                scope=scope,
            )
            self._queue.put_nowait((operation_id, work))
            return dispatch
        except BaseException:
            self._slots.release()
            raise

    def _run(self) -> None:
        connection = db.connect(self.database_path, create=False)
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    self._queue.task_done()
                    return
                operation_id, work = item
                try:
                    db.set_operation_dispatch_status(connection, operation_id, "running")
                    work(connection)
                    operation = db.get_operation(connection, operation_id)
                    if operation is None:
                        dispatch = db.get_operation_dispatch(connection, operation_id)
                        assert dispatch is not None
                        db.begin_operation(
                            connection,
                            operation_id=operation_id,
                            kind="api.noop",
                            scope=dispatch.scope,
                            phase="accepted",
                            deadline_at=db.utc_now(),
                        )
                        db.mark_succeeded(connection, operation_id, cleanup_state="not_required")
                except BaseException as error:
                    operation = db.get_operation(connection, operation_id)
                    if operation is None:
                        dispatch = db.get_operation_dispatch(connection, operation_id)
                        assert dispatch is not None
                        try:
                            db.begin_operation(
                                connection,
                                operation_id=operation_id,
                                kind=dispatch.kind,
                                scope=dispatch.scope,
                                phase="rejected",
                                deadline_at=db.utc_now(),
                            )
                            db.mark_failed(
                                connection,
                                operation_id,
                                error,
                                cleanup_state="not_required",
                            )
                        except db.UnfinishedOperationError:
                            pass
                    elif operation.status == "running":
                        db.mark_recovery_required(connection, operation_id, error)
                finally:
                    try:
                        operation = db.get_operation(connection, operation_id)
                        dispatch_status = (
                            "recovery_required"
                            if operation is not None and operation.status == "recovery_required"
                            else "finished"
                        )
                        db.set_operation_dispatch_status(connection, operation_id, dispatch_status)
                    finally:
                        self._slots.release()
                        self._queue.task_done()
        finally:
            connection.close()

    def wait(self) -> None:
        self._queue.join()

    def close(self, *, wait: bool = True) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if wait:
            self._queue.join()
        for _thread in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join()
