"""Fixed bounds and live grant checks for the public metadata view."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit

ABSOLUTE_SECONDS = 3600
IDLE_SECONDS = 600
GRANT_SECONDS = 90 * 86400
MEMBER_LIMIT = 100
AUDIT_SECONDS = 30 * 86400
AUDIT_ROWS = 2_000_000
RESPONSE_BYTES = 256 * 1024


def current_grant(
    db: sqlite3.Connection, user: Mapping[str, Any], issuer: str, now: float
) -> sqlite3.Row | None:
    if not user["enabled"] or user["issuer"] != issuer:
        return None
    return cast(
        sqlite3.Row | None,
        db.execute(
            "SELECT generation FROM staff_grants WHERE user_id=? AND enabled=1 AND valid_until>?",
            (user["id"], now),
        ).fetchone(),
    )


def public_url(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
        ):
            return None
    except ValueError:
        return None
    return value
