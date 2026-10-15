"""Closed provider protocol, fixed actions, and safe error vocabulary."""

from __future__ import annotations

import math
import time
from typing import Any
from urllib.parse import urlsplit

MAX_RESPONSE = 1_048_576
MAX_ROWS = 500
SAFE_ERROR = "OpenStack command failed; provider details were withheld"


class ProviderError(RuntimeError):
    """Only fixed messages cross the provider boundary."""

    def __init__(self, code: str = "unavailable", status: int = 0) -> None:
        self.code = code
        self.status = status
        super().__init__(
            "OpenStack command output exceeded its safety limit" if code == "size" else SAFE_ERROR
        )


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise ProviderError("timeout")
    return value


def timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(float(value))
        or not 0 < value <= 7200
    ):
        raise ProviderError("invalid")
    return float(value)


def endpoint_url(value: str, *, allow_http: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError("configuration")
    return value.rstrip("/")


# service type, versioned path, response envelope
RESOURCES = {
    "server": ("compute", "servers", "servers", "server"),
    "flavor": ("compute", "flavors", "flavors", "flavor"),
    "image": ("image", "images", "images", None),
    "port": ("network", "ports", "ports", "port"),
    "network": ("network", "networks", "networks", "network"),
    "subnet": ("network", "subnets", "subnets", "subnet"),
    "router": ("network", "routers", "routers", "router"),
    "floating_ip": ("network", "floatingips", "floatingips", "floatingip"),
    "security_group": ("network", "security-groups", "security_groups", "security_group"),
    "security_group_rule": (
        "network",
        "security-group-rules",
        "security_group_rules",
        "security_group_rule",
    ),
    "volume": ("volumev3", "volumes", "volumes", "volume"),
    "volume_type": ("volumev3", "types", "volume_types", "volume_type"),
    "keypair": ("compute", "os-keypairs", "keypairs", "keypair"),
}
FILTERS = {
    "server": {"name"},
    "image": {"name", "visibility"},
    "port": {"name", "device_id", "network_id", "project_id", "fixed_ips"},
    "network": {"name"},
    "subnet": {"name", "network_id"},
    "router": {"name", "project_id"},
    "floating_ip": {"project_id"},
    "security_group": {"name", "project_id"},
    "security_group_rule": {"security_group_id"},
    "volume": {"name"},
}
ACTIONS = frozenset(
    [f"{kind}.{verb}" for kind in RESOURCES for verb in ("show", "list")]
    + ["identity", "console", "attachments", "quota", "limits", "image.usage", "image.catalog"]
)


def text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 256 or "\x00" in value:
        raise ProviderError("invalid")
    return value


def validate(action: object, params: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(action, str) or action not in ACTIONS or not isinstance(params, dict):
        raise ProviderError("invalid")
    if action in {"identity", "limits", "image.usage", "image.catalog"}:
        allowed: set[str] = set()
    elif action == "quota":
        allowed = {"network", "usage"}
        if any(type(value) is not bool for value in params.values()):
            raise ProviderError("invalid")
    elif action == "console":
        allowed = {"reference", "lines"}
        if "lines" not in params or (
            params["lines"] is not None
            and (type(params["lines"]) is not int or not 1 <= params["lines"] <= 2000)
        ):
            raise ProviderError("invalid")
    elif action == "attachments":
        allowed = {"reference"}
    else:
        kind, verb = action.split(".")
        allowed = {"reference"} if verb == "show" else FILTERS.get(kind, set())
    if set(params) - allowed:
        raise ProviderError("invalid")
    if action.endswith(".show") or action in {"console", "attachments"}:
        text(params.get("reference"))
    for key, value in params.items():
        if key not in {"network", "usage", "lines"}:
            text(value)
    return action, params
