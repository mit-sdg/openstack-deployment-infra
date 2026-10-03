"""Stdlib local credentials, RFC 6238 counters, and hashed one-time links."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 3
SCRYPT_MEMORY = 48 * 1024**2
HASH_SLOTS = threading.BoundedSemaphore(1)
KNOWN_DEVICE_HASH_SLOTS = threading.BoundedSemaphore(1)
STEP_UP_HASH_SLOTS = threading.BoundedSemaphore(1)
HASH_LEASE: ContextVar[Literal["anonymous", "known", "step_up"] | None] = ContextVar(
    "password_hash_lease", default=None
)


class HashCapacityError(RuntimeError):
    """The bounded password worker pool is occupied."""


def username(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{2,31}", value):
        raise ValueError(
            "local username must be 3–32 lowercase letters, digits, dots, underscores or hyphens"
        )
    return value


def password(value: object, account: str) -> str:
    if not isinstance(value, str) or not len(value) >= 12 or len(value.encode("utf-8")) > 1024:
        raise ValueError(
            "password must contain at least 12 characters and at most 1024 UTF-8 bytes"
        )
    if account.casefold() in value.casefold():
        raise ValueError("password cannot contain the username")
    return value


@contextmanager
def hashing_slot(*, step_up: bool = False, known: bool = False) -> Iterator[None]:
    lane: Literal["anonymous", "known", "step_up"] = (
        "step_up" if step_up else "known" if known else "anonymous"
    )
    if HASH_LEASE.get() == lane:
        yield
        return
    pool = STEP_UP_HASH_SLOTS if step_up else KNOWN_DEVICE_HASH_SLOTS if known else HASH_SLOTS
    if not pool.acquire(blocking=False):
        raise HashCapacityError("local authentication capacity unavailable")
    lease = HASH_LEASE.set(lane)
    try:
        yield
    finally:
        HASH_LEASE.reset(lease)
        pool.release()


def hash_password(
    value: str, *, n: int = SCRYPT_N, p: int = SCRYPT_P, step_up: bool = False, known: bool = False
) -> str:
    if n not in {8192, 16384, 32768} or not 1 <= p <= 10:
        raise ValueError("unsupported password cost")
    salt = secrets.token_bytes(16)
    with hashing_slot(step_up=step_up, known=known):
        key = hashlib.scrypt(
            value.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=p, maxmem=SCRYPT_MEMORY, dklen=64
        )
    return f"scrypt${n}${SCRYPT_R}${p}${salt.hex()}${key.hex()}"


def verify_password(
    value: str, encoded: str, *, step_up: bool = False, known: bool = False
) -> tuple[bool, bool]:
    """Return validity and upgrade requirement; reject unbounded/corrupt costs."""
    if len(value.encode("utf-8")) > 1024:
        return False, False
    try:
        kind, ns, rs, ps, salt_hex, key_hex = encoded.split("$")
        n, r, p = int(ns), int(rs), int(ps)
        if (
            kind != "scrypt"
            or n not in {8192, 16384, 32768}
            or r != 8
            or not 1 <= p <= 10
            or len(salt_hex) != 32
            or len(key_hex) != 128
        ):
            return False, False
        salt, stored = bytes.fromhex(salt_hex), bytes.fromhex(key_hex)
    except (ValueError, TypeError):
        return False, False
    with hashing_slot(step_up=step_up, known=known):
        key = hashlib.scrypt(
            value.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MEMORY, dklen=64
        )
    valid = hmac.compare_digest(key, stored)
    return valid, valid and (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


def totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp_code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret, casefold=False)
    digest = hmac.digest(key, struct.pack(">Q", counter), "sha1")
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def verify_totp(secret: str, code: object, now: float, last_counter: int) -> int | None:
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        return None
    counter = int(now // 30)
    matched = None
    try:
        for candidate in (counter - 1, counter, counter + 1):
            if candidate >= 0:
                valid = hmac.compare_digest(totp_code(secret, candidate), code)
                if valid and candidate > last_counter:
                    matched = candidate
    except (ValueError, TypeError, struct.error):
        return None
    return matched


def token_hash(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ValueError("invalid one-time token")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


DUMMY_HASH = "scrypt$32768$8$3$00000000000000000000000000000000$595935967a4e93405d5e11de777ea198e4bf73d0cad8e70d865b357de14acaea801ce37252f3c1347134a105f4f1e6dea3116216ee0a848bf4b07203b2f97af7"
