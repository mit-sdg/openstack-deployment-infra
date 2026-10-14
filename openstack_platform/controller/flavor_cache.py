"""Bounded, single-flight provider observations for flavor display reads only."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass

from .. import openstack
from ..config import PlatformConfig
from ..validation import flavor_reference


@dataclass
class _Entry[T]:
    result: Future[T]
    expires: float = float("inf")


class _Cache[T]:
    def __init__(self, clock: Callable[[], float], capacity: int) -> None:
        self.clock, self.capacity = clock, capacity
        self.lock = threading.Lock()
        self.entries: OrderedDict[str, _Entry[T]] = OrderedDict()

    def get(self, key: str, observe: Callable[[], T]) -> T:
        with self.lock:
            now = self.clock()
            for stale in [key for key, entry in self.entries.items() if entry.expires <= now]:
                del self.entries[stale]
            entry = self.entries.get(key)
            leader = entry is None
            if entry is None:
                if len(self.entries) >= self.capacity:
                    oldest = next(
                        (key for key, entry in self.entries.items() if entry.result.done()), None
                    )
                    if oldest is None:
                        raise TimeoutError("flavor observation capacity exhausted")
                    del self.entries[oldest]
                entry = self.entries[key] = _Entry(Future())
            self.entries.move_to_end(key)
        if leader:
            try:
                value = observe()
            except BaseException as error:
                with self.lock:
                    entry.result.set_exception(error)
                    entry.expires = self.clock() + 5
                raise
            else:
                with self.lock:
                    entry.result.set_result(value)
                    entry.expires = self.clock() + 600
        # Provider calls keep their existing 30s deadline; followers are bounded.
        return entry.result.result(timeout=20)


class FlavorCache:
    def __init__(
        self, platform: PlatformConfig, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.platform = platform
        self.observations: _Cache[openstack.Flavor] = _Cache(clock, 128)
        self.catalog: _Cache[tuple[openstack.Flavor, ...]] = _Cache(clock, 1)

    def flavor(self, reference: str) -> openstack.Flavor:
        reference = flavor_reference(reference)
        return self.observations.get(
            reference, lambda: openstack.observe_flavor_capacity(self.platform, reference)
        )

    def flavors(self) -> tuple[openstack.Flavor, ...]:
        return self.catalog.get("flavors", lambda: openstack.observe_flavors(self.platform))
