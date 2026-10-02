"""Dependency-free policy for the public Commons credential-checking inventory."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

LIMITS = {
    "appLimit": (2, 1, 1000),
    "concurrencyLimit": (1, 1, 16),
    "absoluteSeconds": (28800, 60, 86400),
    "idleSeconds": (1800, 60, 86400),
    "anonymousOptionsPerMinute": (600, 1, 100000),
    "anonymousStartsPerMinute": (400, 1, 100000),
}


def validate(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or type(value.get("enabled")) is not bool
        or set(value)
        - {"enabled", "commonsOrigin", "identityEgressCidrs", "classLabel"}
        - set(LIMITS)
    ):
        raise ValueError("ownerPortal has invalid or unknown fields")
    result = dict(value)
    if not result["enabled"] and set(result) == {"enabled"}:
        return result
    url = result.get("commonsOrigin")
    if not isinstance(url, str) or len(url) > 256:
        raise ValueError("ownerPortal requires a bounded Commons HTTPS origin")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.hostname == "localhost"
        or parsed.hostname.endswith(".localhost")
        or "." not in parsed.hostname
    ):
        raise ValueError(
            "ownerPortal requires a public Commons HTTPS origin without path or userinfo"
        )
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("ownerPortal cannot use a development origin")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("ownerPortal port is invalid")
    cidrs = result.get("identityEgressCidrs", [])
    if (
        not isinstance(cidrs, (tuple, list))
        or len(cidrs) > 64
        or any(not isinstance(item, str) for item in cidrs)
    ):
        raise ValueError("ownerPortal identityEgressCidrs must be bounded CIDRs")
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr, strict=True)
        if network.prefixlen == 0:
            raise ValueError("ownerPortal identity egress cannot admit all addresses")
    if len(set(cidrs)) != len(cidrs):
        raise ValueError("ownerPortal identity egress contains duplicates")
    result["identityEgressCidrs"] = list(cidrs)
    label = result.get("classLabel", "class account")
    if (
        not isinstance(label, str)
        or not 1 <= len(label) <= 80
        or any(ord(char) < 32 for char in label)
    ):
        raise ValueError("ownerPortal classLabel is invalid")
    result["classLabel"] = label
    for name, (default, low, high) in LIMITS.items():
        limit = result.get(name, default)
        if type(limit) is not int or not low <= limit <= high:
            raise ValueError("ownerPortal limit is invalid")
        result[name] = limit
    if result["idleSeconds"] > result["absoluteSeconds"]:
        raise ValueError("ownerPortal session lifetimes are invalid")
    return result
