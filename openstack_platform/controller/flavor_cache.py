"""Bounded, single-flight, stale-while-revalidate flavor display observations."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial

from .. import openstack
from ..config import PlatformConfig
from ..validation import flavor_reference

LIFETIME_SECONDS = 600
REFRESH_SECONDS = 540
BACKGROUND_CAPACITY = 32


class _Background:
    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="flavor-observation")
        self.slots = threading.BoundedSemaphore(BACKGROUND_CAPACITY)
        self.lock = threading.Lock()
        self.closed = False

    def submit(self, observe: Callable[[], None]) -> bool:
        with self.lock:
            if self.closed or not self.slots.acquire(blocking=False):
                return False
            try:
                task = self.pool.submit(observe)
            except BaseException:
                self.slots.release()
                raise
            task.add_done_callback(lambda _task: self.slots.release())
            return True

    def close(self) -> None:
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)


@dataclass
class _Entry[T]:
    observe: Callable[[], T]
    value: T | None = None
    result: Future[T] | None = None
    pending: bool = False
    refresh_at: float = 0
    expires: float = 0
    retry_at: float = 0
    failures: int = 0


class _Cache[T]:
    def __init__(self, clock: Callable[[], float], capacity: int, background: _Background) -> None:
        self.clock, self.capacity, self.background = clock, capacity, background
        self.lock = threading.Lock()
        self.entries: OrderedDict[str, _Entry[T]] = OrderedDict()

    def _entry(self, key: str, observe: Callable[[], T]) -> _Entry[T]:
        entry = self.entries.get(key)
        if entry is None:
            if len(self.entries) >= self.capacity:
                oldest = next(
                    (key for key, entry in self.entries.items() if not entry.pending), None
                )
                if oldest is None:
                    raise TimeoutError("flavor observation capacity exhausted")
                del self.entries[oldest]
            entry = self.entries[key] = _Entry(observe)
        self.entries.move_to_end(key)
        return entry

    def _start(self, entry: _Entry[T], *, background: bool) -> bool:
        if entry.pending or self.clock() < max(entry.refresh_at, entry.retry_at):
            return False
        previous = entry.result
        result: Future[T] = Future()
        entry.result, entry.pending = result, True
        if background and not self.background.submit(lambda: self._observe(entry, result)):
            entry.result, entry.pending = previous, False
            return False
        return True

    def _observe(self, entry: _Entry[T], result: Future[T]) -> None:
        try:
            value = entry.observe()
        except BaseException as error:
            with self.lock:
                entry.failures += 1
                entry.retry_at = self.clock() + min(300, 5 * 2 ** min(entry.failures - 1, 6))
                entry.pending = False
                result.set_exception(error)
        else:
            with self.lock:
                entry.value = value
                entry.expires = self.clock() + LIFETIME_SECONDS
                entry.refresh_at = entry.expires - (LIFETIME_SECONDS - REFRESH_SECONDS)
                entry.retry_at, entry.failures, entry.pending = 0, 0, False
                result.set_result(value)

    def get(self, key: str, observe: Callable[[], T]) -> T:
        with self.lock:
            entry = self._entry(key, observe)
            if entry.value is not None:
                # Even an expired display value returns immediately. Validation
                # never enters this cache and always observes the provider itself.
                self._start(entry, background=True)
                return entry.value
            leader = self._start(entry, background=False)
            result = entry.result
            assert result is not None
        if leader:
            self._observe(entry, result)
        # Cold followers share the same lookup, with a bounded wait.
        return result.result(timeout=20)

    def prewarm(self, key: str, observe: Callable[[], T]) -> None:
        with self.lock:
            self._start(self._entry(key, observe), background=True)

    def refresh(self) -> None:
        with self.lock:
            for entry in self.entries.values():
                self._start(entry, background=True)


class FlavorCache:
    def __init__(
        self, platform: PlatformConfig, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.platform = platform
        self.background = _Background()
        self.observations: _Cache[openstack.Flavor] = _Cache(clock, 128, self.background)
        self.catalog: _Cache[tuple[openstack.Flavor, ...]] = _Cache(clock, 1, self.background)
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def flavor(self, reference: str) -> openstack.Flavor:
        reference = flavor_reference(reference)
        return self.observations.get(
            reference, lambda: openstack.observe_flavor_capacity(self.platform, reference)
        )

    def flavors(self) -> tuple[openstack.Flavor, ...]:
        return self.catalog.get("flavors", lambda: openstack.observe_flavors(self.platform))

    def maintain(self, references: Iterable[str]) -> None:
        """Prewarm the catalog and in-use flavors; refresh retained entries ahead of expiry."""
        self.catalog.prewarm("flavors", lambda: openstack.observe_flavors(self.platform))
        for reference in references:
            reference = flavor_reference(reference)
            try:
                self.observations.prewarm(
                    reference,
                    partial(openstack.observe_flavor_capacity, self.platform, reference),
                )
            except TimeoutError:
                break
        self.catalog.refresh()
        self.observations.refresh()

    def start(self, references: Callable[[], Iterable[str]]) -> None:
        def run() -> None:
            while not self.stopping.is_set():
                try:
                    self.maintain(references())
                except Exception:
                    # Snapshot failures wait for the next cycle; provider failures
                    # retain values and use each entry's exponential retry schedule.
                    pass
                self.stopping.wait(5)

        self.thread = threading.Thread(target=run, name="flavor-cache-refresh", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join()
        self.background.close()
