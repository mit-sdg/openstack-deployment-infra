"""Stateless anonymous challenges and bounded per-address admission limits."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import stat
import threading
from collections import OrderedDict, deque
from pathlib import Path

from ...controller.http import HttpError, Request
from ..common import base64url, opaque


def load_key(directory: Path) -> bytes:
    """Keep the explicitly authorized anonymous HMAC key private and crash durable."""
    path = directory / "anonymous.key"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != 32
            ):
                raise ValueError("anonymous key ownership or mode is invalid")
            key = os.read(descriptor, 33)
            if len(key) != 32:
                raise ValueError("anonymous key length is invalid")
            return key
        finally:
            os.close(descriptor)
    try:
        key = secrets.token_bytes(32)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        return key
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class AnonymousChallenge:
    def __init__(self, directory: Path, realm: str) -> None:
        self.key = load_key(directory)
        self.realm = realm

    def mac(self, purpose: str, value: str) -> str:
        message = (purpose + "\0" + self.realm + "\0" + value).encode()
        return (
            base64.urlsafe_b64encode(hmac.digest(self.key, message, hashlib.sha256))
            .decode()
            .rstrip("=")
        )

    def issue(self, now: float, purpose: str = "login-binder") -> str:
        message = opaque() + "." + str(int(now) + 600)
        return message + "." + self.mac(purpose, message)

    def valid(self, binder: str, now: float, purpose: str = "login-binder") -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}\.[0-9]{1,12}\.[A-Za-z0-9_-]{43}", binder):
            return False
        nonce, expires, signature = binder.split(".")
        try:
            if len(base64url(nonce)) != 32 or len(base64url(signature)) != 32:
                return False
        except ValueError:
            return False
        return now < int(expires) <= now + 600 and hmac.compare_digest(
            signature, self.mac(purpose, nonce + "." + expires)
        )

    def csrf(self, binder: str) -> str:
        return self.mac("anonymous-csrf", binder)


def client_address_bucket(request: Request) -> str:
    """Use only web's validated address, grouping IPv6 clients by /64."""
    try:
        ip = ipaddress.ip_address(request.headers["x-portal-client-address"])
        return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)
    except (KeyError, ValueError):
        raise HttpError(400, "INVALID_REQUEST", "A trusted client address is required.") from None


class AddressLimits:
    def __init__(self, options_per_minute: int = 600, starts_per_minute: int = 400) -> None:
        self.lock = threading.Lock()
        self.visits: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self.limits = {"options": options_per_minute, "start": starts_per_minute}

    def check(self, request: Request, action: str, now: float) -> None:
        self.check_bucket(client_address_bucket(request), action, now)

    def check_bucket(self, address: str, action: str, now: float) -> None:
        """Admit a bucket already validated/grouped at the trusted web boundary."""
        key = action, address
        with self.lock:
            stale = [
                key for key, values in self.visits.items() if not values or values[-1] <= now - 60
            ]
            for stale_key in stale:
                del self.visits[stale_key]
            if key not in self.visits and len(self.visits) >= 4096:
                self.visits.popitem(last=False)
            visits = self.visits.setdefault(key, deque())
            while visits and visits[0] <= now - 60:
                visits.popleft()
            if len(visits) >= self.limits[action]:
                raise HttpError(
                    429, "RATE_LIMITED", "Please wait a minute before trying sign-in again."
                )
            visits.append(now)
            self.visits.move_to_end(key)
