"""Bounded HTTP helpers for control-plane health and backup scripts."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, cast

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 120


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _request: object,
        _response: object,
        _code: int,
        _message: str,
        _headers: object,
        _new_url: str,
    ) -> None:
        return None


def bounded_request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 10,
    response_limit: int = 65_536,
    ssl_context: ssl.SSLContext | None = None,
) -> bytes:
    """Read one response with a deadline, byte ceiling, and no redirects."""
    if (
        not isinstance(url, str)
        or not url
        or "\x00" in url
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
        or isinstance(response_limit, bool)
        or not isinstance(response_limit, int)
        or not 1 <= response_limit <= _MAX_RESPONSE_BYTES
    ):
        raise ValueError("bounded HTTP request arguments are invalid")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=dict(headers or {}),
    )
    handlers: list[Any] = [_NoRedirect()]
    if ssl_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = cast(bytes, response.read(response_limit + 1))
    except urllib.error.HTTPError as error:
        error.read(min(response_limit, 65_536))
        raise RuntimeError(f"HTTP request failed with status {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"HTTP request failed: {error.__class__.__name__}") from None
    if len(body) > response_limit:
        raise RuntimeError("HTTP response exceeded its configured size limit")
    return body


def bounded_json(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 10,
    response_limit: int = 65_536,
    ssl_context: ssl.SSLContext | None = None,
) -> Any:
    """Decode one bounded response without following a redirect."""
    raw = bounded_request(
        url,
        method=method,
        data=data,
        headers=headers,
        timeout_seconds=timeout_seconds,
        response_limit=response_limit,
        ssl_context=ssl_context,
    )

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("HTTP response was malformed JSON") from error
