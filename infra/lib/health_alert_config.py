"""Non-secret inventory policy for optional platform-health alerts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


def validate(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {
        "enabled",
        "format",
        "failureThreshold",
        "repeatSeconds",
    }:
        raise ValueError("healthAlerts has invalid or unknown fields")
    result = {"enabled": False, "format": "slack", "failureThreshold": 2, "repeatSeconds": 14400}
    result.update(value)
    if type(result["enabled"]) is not bool:
        raise ValueError("healthAlerts.enabled must be boolean")
    if result["format"] not in ("slack", "json"):
        raise ValueError("healthAlerts.format must be slack or json")
    for name, minimum, maximum in (("failureThreshold", 1, 100), ("repeatSeconds", 3600, 604800)):
        number = result[name]
        if type(number) is not int or not minimum <= number <= maximum:
            raise ValueError(
                f"healthAlerts.{name} must be an integer from {minimum} through {maximum}"
            )
    return result


def validate_webhook(value: str) -> str:
    message = "health alert webhook must be a bounded HTTPS URL without userinfo or fragment"
    if (
        not value
        or len(value) > 4096
        or any(
            character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
            for character in value
        )
    ):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    return value
