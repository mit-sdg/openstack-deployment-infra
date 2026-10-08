"""Bounded in-process execution for durably accepted controller mutations."""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path

from . import database as db
from . import finishing_retries

OperationWork = Callable[[sqlite3.Connection], object]


class AsyncOperationExecutor:
    """Execute accepted work off request threads with bounded memory use.

    The dispatch journal contains no request payloads (environment mutations can
    contain secrets). Interrupted foreground work needs caller replay. Accepted
    finishing work resumes from its separate durable intent and retry schedule.
    """

    def __init__(
        self,
        database_path: Path,
        startup_connection: sqlite3.Connection,
        *,
        workers: int = 4,
        capacity: int = 32,
        finishing_work: Callable[[sqlite3.Connection, str], object] | None = None,
    ) -> None:
        if workers < 1 or capacity < workers:
            raise ValueError("async operation bounds are invalid")
        self.database_path = database_path
        self._slots = threading.BoundedSemaphore(capacity)
        self._queue: queue.Queue[tuple[str, OperationWork] | None] = queue.Queue()
        self._closed = False
        self._finishing_work = finishing_work
        self._stop_retries = threading.Event()
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
        self._retry_thread = threading.Thread(
            target=self._retry_loop, name="controller-finishing-retries", daemon=False
        )
        self._retry_thread.start()

    def _recover_startup(self, connection: sqlite3.Connection) -> None:
        for dispatch in db.list_operation_dispatches(connection):
            operation = db.get_operation(connection, dispatch.operation_id)
            if (
                operation is not None
                and operation.finishing
                and operation.status not in {"succeeded", "failed"}
            ):
                retry = operation.refs.get("finishing_retry")
                interrupted = dispatch.status in {"pending", "running"}
                if interrupted or retry is None:
                    finishing_retries.schedule(
                        connection,
                        operation.operation_id,
                        "controller stopped before finishing work completed",
                    )
                    db.set_operation_dispatch_status(
                        connection, dispatch.operation_id, "recovery_required"
                    )
                continue
            if dispatch.status not in {"pending", "running"}:
                continue
            no_domain_intent = operation is None
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
                if no_domain_intent:
                    db.mark_failed(
                        connection,
                        operation.operation_id,
                        "operation stopped before recording any domain mutation intent",
                        cleanup_state="not_required",
                    )
                    db.set_operation_dispatch_status(connection, dispatch.operation_id, "finished")
                    continue
                # Keep an existing domain checkpoint: deployment recovery must
                # distinguish a candidate worker from an accepted route. Only
                # the placeholder created above uses startup_interrupted.
                db.mark_recovery_required(
                    connection,
                    operation.operation_id,
                    "controller stopped before asynchronous work completed",
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
        # Admission, durable enqueue, and in-memory enqueue are one shutdown
        # critical section. Otherwise close() can stop every worker after the
        # closed check but before this item reaches the queue, stranding an
        # accepted operation forever.
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
        automatic: bool = False,
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
                    automatic=automatic,
                )
                self._queue.put_nowait((operation_id, work))
                return dispatch
            except BaseException:
                self._slots.release()
                raise

    def _retry_loop(self) -> None:
        connection = db.connect(self.database_path, create=False)
        try:
            while not self._stop_retries.wait(1):
                if self._finishing_work is None:
                    continue
                for operation in finishing_retries.due(connection):
                    try:
                        self.resubmit_recovery(
                            connection,
                            operation_id=operation.operation_id,
                            kind=operation.kind,
                            scope=operation.scope,
                            work=partial(self._finish, operation_id=operation.operation_id),
                            automatic=True,
                        )
                    except db.DatabaseError:
                        # Capacity, shutdown or a concurrent manual same-key claim
                        # leaves the durable next retry available to the next tick.
                        continue
        finally:
            connection.close()

    def _finish(self, connection: sqlite3.Connection, operation_id: str) -> object:
        assert self._finishing_work is not None
        return self._finishing_work(connection, operation_id)

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
                            kind=dispatch.kind,
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
                        if operation.finishing:
                            db.record_finishing_failure(connection, operation_id, error)
                        else:
                            db.mark_recovery_required(connection, operation_id, error)
                finally:
                    try:
                        operation = db.get_operation(connection, operation_id)
                        finishing_pending = (
                            operation is not None
                            and operation.finishing
                            and operation.status in {"running", "recovery_required"}
                        )
                        if finishing_pending:
                            assert operation is not None
                            finishing_retries.schedule(
                                connection,
                                operation_id,
                                operation.safe_error or "accepted finishing work was interrupted",
                            )
                        dispatch_status = (
                            "recovery_required"
                            if finishing_pending
                            or (operation is not None and operation.status == "recovery_required")
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

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._stop_retries.set()
        self._retry_thread.join()
        self._queue.join()
        for _thread in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join()
