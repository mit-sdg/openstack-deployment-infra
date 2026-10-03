"""Account-bound, generation-fenced browser recognition; never a sign-in credential."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Any

from ...controller.http import Request

LIFETIME = 90 * 86400
TOKEN = re.compile(
    r"1\.[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}\.[0-9]{1,19}\.[0-9]{1,12}\.[A-Za-z0-9_-]{43}"
)


def signature(key: bytes, payload: str) -> str:
    separated = hmac.digest(key, b"owner-portal/known-device/v1", hashlib.sha256)
    return (
        base64.urlsafe_b64encode(hmac.digest(separated, payload.encode("ascii"), hashlib.sha256))
        .decode()
        .rstrip("=")
    )


def issue(key: bytes, user: dict[str, Any], now: float) -> str:
    payload = f"1.{user['id']}.{user['generation']}.{int(now) + LIFETIME}"
    return payload + "." + signature(key, payload)


def valid(key: bytes, token: object, user: dict[str, Any] | None, now: float) -> bool:
    if (
        not isinstance(token, str)
        or TOKEN.fullmatch(token) is None
        or user is None
        or user["issuer"] != "local"
    ):
        return False
    _version, identifier, generation, expires, mac = token.split(".")
    payload = token.rsplit(".", 1)[0]
    return (
        hmac.compare_digest(signature(key, payload), mac)
        and identifier == user["id"]
        and int(generation) == user["generation"]
        and now < int(expires) <= now + LIFETIME
    )


def read(request: Request, name: str) -> str | None:
    # Invalid, duplicate and malformed device cookies are unrecognized devices.
    # They must not create a distinct authentication error or bypass a budget.
    values = []
    for pair in request.headers.get("cookie", "").split(";"):
        key, separator, value = pair.strip().partition("=")
        if key == name and separator:
            values.append(value)
    return values[0] if len(values) == 1 and TOKEN.fullmatch(values[0]) else None
