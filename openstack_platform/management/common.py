"""Strict, secret-safe management parsing and opaque identifiers."""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import math
import secrets
from collections.abc import Mapping
from typing import Any

from ..controller.http import HttpError

# Shared by both release payloads, which need no identity-client import in web.
MANAGEMENT_REQUESTS = 128


def strict_json(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def opaque() -> str:
    return secrets.token_urlsafe(32)


def utc(value: object) -> str | None:
    if isinstance(value, str):
        if len(value) > 40:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (
                parsed.astimezone(datetime.UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
                if parsed.tzinfo is not None
                else None
            )
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return None
    try:
        return (
            datetime.datetime.fromtimestamp(value, datetime.UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except (ValueError, OverflowError, OSError):
        return None


def base64url(value: str) -> bytes:
    if not value or any(
        c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in value
    ):
        raise ValueError("invalid base64url")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        raise ValueError("noncanonical base64url")
    return decoded


def object_body(value: object, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HttpError(400, "INVALID_REQUEST", "Request fields are invalid.")
    return value


def text(value: object, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValueError("invalid text")
    return value
