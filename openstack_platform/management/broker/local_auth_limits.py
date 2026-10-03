"""Process-local admission and device failure windows; no durable state changes."""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from ...controller.http import HttpError
from .local_security import HashCapacityError

CAPACITY = 4096
ACCOUNT_ATTEMPTS = 12
ACCOUNT_WINDOW = 60
DEVICE_FAILURES = 20
DEVICE_WINDOW = 3600
AccountKey = tuple[str, int]
DeviceKey = tuple[str, int, str]


@dataclass
class AccountWindow:
    starts: deque[float] = field(default_factory=deque)
    active: bool = False


class KnownAccountLimits:
    def __init__(self, *, capacity: int = CAPACITY) -> None:
        self.lock = threading.Lock()
        self.capacity = capacity
        self.accounts: OrderedDict[AccountKey, AccountWindow] = OrderedDict()

    @contextmanager
    def reserve(self, key: AccountKey, now: float) -> Iterator[None]:
        with self.lock:
            for identifier, existing in list(self.accounts.items()):
                while existing.starts and existing.starts[0] <= now - ACCOUNT_WINDOW:
                    existing.starts.popleft()
                if not existing.active and not existing.starts:
                    del self.accounts[identifier]
            row = self.accounts.get(key)
            if row is None:
                if len(self.accounts) >= self.capacity:
                    raise HashCapacityError("known-account admission capacity unavailable")
                row = AccountWindow()
                self.accounts[key] = row
            if row.active:
                raise HashCapacityError("known-account request already in flight")
            if len(row.starts) >= ACCOUNT_ATTEMPTS:
                raise HttpError(429, "RATE_LIMITED", "Please wait before trying sign-in again.")
            row.active = True
            row.starts.append(now)
            self.accounts.move_to_end(key)
        try:
            yield
        finally:
            with self.lock:
                row.active = False


@dataclass
class DeviceWindow:
    failures: deque[float] = field(default_factory=deque)
    pending: int = 0


@dataclass
class DeviceReservation:
    row: DeviceWindow | None = None
    denied: bool = False

    @property
    def exempt(self) -> bool:
        return self.row is not None and not self.denied


class DeviceFailureLimits:
    def __init__(self, *, capacity: int = CAPACITY) -> None:
        self.lock = threading.Lock()
        self.capacity = capacity
        self.devices: OrderedDict[DeviceKey, DeviceWindow] = OrderedDict()

    def reserve(self, key: DeviceKey, now: float) -> DeviceReservation:
        with self.lock:
            for identifier, existing in list(self.devices.items()):
                while existing.failures and existing.failures[0] <= now - DEVICE_WINDOW:
                    existing.failures.popleft()
                if not existing.pending and not existing.failures:
                    del self.devices[identifier]
            row = self.devices.get(key)
            if row is None:
                if len(self.devices) >= self.capacity:
                    # Never evict a live failure window to reopen its exemption.
                    # Untracked devices use the durable account password budget.
                    return DeviceReservation()
                row = DeviceWindow()
                self.devices[key] = row
            if len(row.failures) + row.pending >= DEVICE_FAILURES:
                return DeviceReservation(denied=True)
            row.pending += 1
            self.devices.move_to_end(key)
            return DeviceReservation(row=row)

    def finish(self, reservation: DeviceReservation, *, failed: bool, now: float) -> None:
        with self.lock:
            if reservation.row is not None:
                reservation.row.pending -= 1
                if failed:
                    reservation.row.failures.append(now)
