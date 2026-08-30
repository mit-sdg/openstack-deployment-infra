"""Strict non-secret inventory and private operator-policy loading."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .contracts import (
    INVENTORY_ALLOWED_TOP_LEVEL,
    INVENTORY_REQUIRED_PATHS,
    INVENTORY_REQUIRED_TOP_LEVEL,
)
from .validation import (
    ValidationError,
    age_recipient,
    oci_digest_pin,
    uuid,
)

_JSON_MAX_BYTES = 1_048_576
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}")
_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]")
_PREFIX = re.compile(r"[a-z0-9][a-z0-9-]{0,30}[a-z0-9]")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)

_PLATFORM_KEYS = set(INVENTORY_ALLOWED_TOP_LEVEL)
_REQUIRED_PLATFORM_KEYS = set(INVENTORY_REQUIRED_TOP_LEVEL)
_STANDARD_KEYS = {
    "workerFlavor",
    "cpuMHz",
    "memoryMiB",
    "postgresConnections",
    "postgresMeasuredBytes",
    "mongoMeasuredBytes",
    "s3Bytes",
    "s3Objects",
}
_LIMIT_NAMES = {
    "sourceBytes": (52_428_800, 1_048_576, 1_073_741_824),
    "dotenvBytes": (262_144, 1_024, 4_194_304),
    "environmentValueBytes": (65_536, 1, 1_048_576),
    "stderrBytes": (262_144, 1_024, 4_194_304),
    "buildLogBytes": (10_485_760, 1_024, 104_857_600),
    "helperRequestBytes": (1_048_576, 1_024, 8_388_608),
    "helperResponseBytes": (1_048_576, 1_024, 8_388_608),
    "connectSeconds": (10, 1, 120),
    "httpSeconds": (30, 1, 300),
    "processSeconds": (900, 1, 7_200),
    "helperSeconds": (900, 1, 7_200),
    "pollIntervalSeconds": (2, 1, 30),
}


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    """The stable deployment identity and complete non-secret inventory."""

    project_name: str
    project_id: str
    prefix: str
    namespace: str
    domain: str
    datacenter: str
    region: str
    network: str
    document: Mapping[str, Any]

    def get(self, dotted_name: str) -> Any:
        """Read one required inventory value using an explicit dotted name."""
        value: Any = self.document
        for component in dotted_name.split("."):
            if not isinstance(value, Mapping) or component not in value:
                raise KeyError(f"platform inventory has no {dotted_name!r} value")
            value = value[component]
        return value


@dataclass(frozen=True, slots=True)
class StandardProfile:
    worker_flavor: str
    cpu_mhz: int
    memory_mib: int
    postgres_connections: int
    postgres_measured_bytes: int
    mongo_measured_bytes: int
    s3_bytes: int
    s3_objects: int


@dataclass(frozen=True, slots=True)
class RuntimeImages:
    bun: str
    node: str


@dataclass(frozen=True, slots=True)
class Limits:
    source_bytes: int
    dotenv_bytes: int
    environment_value_bytes: int
    stderr_bytes: int
    build_log_bytes: int
    helper_request_bytes: int
    helper_response_bytes: int
    connect_seconds: int
    http_seconds: int
    process_seconds: int
    helper_seconds: int
    poll_interval_seconds: int


@dataclass(frozen=True, slots=True)
class Policy:
    standard: StandardProfile
    runtime_images: RuntimeImages
    backup_age_recipient: str
    limits: Limits


@dataclass(frozen=True, slots=True)
class Config:
    platform: PlatformConfig
    policy: Policy


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path, *, private: bool) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise FileNotFoundError(f"configuration file was not found: {path}") from None
    except OSError as error:
        raise ValidationError(f"configuration must be a direct regular file: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(f"configuration must be a direct regular file: {path}")
        if private:
            if metadata.st_uid != os.geteuid():
                raise ValidationError("private policy file must be owned by the current user")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValidationError(
                    "private policy file permissions must not allow group or other access"
                )
        if metadata.st_size > _JSON_MAX_BYTES:
            raise ValidationError("configuration exceeds its 1048576-byte limit")
        raw = b""
        while chunk := os.read(descriptor, min(65_536, _JSON_MAX_BYTES + 1 - len(raw))):
            raw += chunk
            if len(raw) > _JSON_MAX_BYTES:
                raise ValidationError("configuration exceeds its 1048576-byte limit")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except UnicodeDecodeError as error:
        raise ValidationError("configuration must be UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise ValidationError("configuration is malformed JSON") from error
    if not isinstance(document, dict):
        raise ValidationError("configuration must be a JSON object")
    return document


def _keys(
    document: Mapping[str, Any], expected: set[str], *, field: str, required: set[str] | None = None
) -> None:
    unknown = document.keys() - expected
    missing = (expected if required is None else required) - document.keys()
    if unknown:
        raise ValidationError(f"{field} has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValidationError(f"{field} is missing keys: {', '.join(sorted(missing))}")


def _required_inventory_paths(document: Mapping[str, Any]) -> None:
    missing: list[str] = []
    for dotted in INVENTORY_REQUIRED_PATHS:
        value: Any = document
        try:
            for component in dotted.split("."):
                if isinstance(value, Mapping):
                    value = value[component]
                elif isinstance(value, list):
                    value = value[int(component)]
                else:
                    raise KeyError(component)
        except (IndexError, KeyError, TypeError, ValueError):
            missing.append(dotted)
    if missing:
        raise ValidationError(
            "platform inventory is missing or malformed at: " + ", ".join(sorted(missing))
        )


def _text(value: object, *, field: str, pattern: re.Pattern[str] = _NAME) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValidationError(f"{field} is malformed")
    return value


def _positive_int(value: object, *, field: str, minimum: int = 1, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"{field} must be an integer from {minimum} through {maximum}")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_public_ingress(document: Mapping[str, Any]) -> None:
    ingress = document.get("publicIngress")
    if not isinstance(ingress, dict):
        raise ValidationError("publicIngress must be a JSON object")
    _keys(ingress, {"mode", "providerCidrs"}, field="publicIngress")
    mode = ingress["mode"]
    cidrs = ingress["providerCidrs"]
    if mode not in {"tunnel", "direct"}:
        raise ValidationError("publicIngress.mode must be tunnel or direct")
    if not isinstance(cidrs, list) or any(not isinstance(item, str) for item in cidrs):
        raise ValidationError("publicIngress.providerCidrs must be a JSON string array")
    canonical: list[str] = []
    for item in cidrs:
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as error:
            raise ValidationError("publicIngress.providerCidrs contains a malformed CIDR") from error
        if network.version != 4 or network.prefixlen == 0:
            raise ValidationError("publicIngress.providerCidrs must contain exact non-default IPv4 CIDRs")
        canonical.append(str(network))
    if len(canonical) != len(set(canonical)):
        raise ValidationError("publicIngress.providerCidrs contains duplicate CIDRs")
    if mode == "tunnel" and canonical:
        raise ValidationError("tunnel ingress must not configure provider CIDRs")
    if mode == "direct" and not canonical:
        raise ValidationError("direct ingress requires at least one provider CIDR")


def load_platform(path: str | Path) -> PlatformConfig:
    """Load the non-secret deployment inventory, including stable project UUID."""
    document = _read_json(Path(path), private=False)
    _keys(document, _PLATFORM_KEYS, field="platform inventory", required=_REQUIRED_PLATFORM_KEYS)
    _required_inventory_paths(document)
    _validate_public_ingress(document)

    project_name = _text(document["project"], field="project")
    project_id = uuid(document["projectId"], field="projectId")
    prefix = _text(document["prefix"], field="prefix", pattern=_PREFIX)
    namespace = _text(document["namespace"], field="namespace", pattern=_NAMESPACE)
    domain = _text(document["domain"], field="domain", pattern=_DOMAIN)
    datacenter = _text(document["datacenter"], field="datacenter")
    region = _text(document["region"], field="region")
    network = _text(document["network"], field="network")
    for key in ("hosts", "ports", "paths"):
        if not isinstance(document[key], dict):
            raise ValidationError(f"{key} must be a JSON object")

    return PlatformConfig(
        project_name=project_name,
        project_id=project_id,
        prefix=prefix,
        namespace=namespace,
        domain=domain,
        datacenter=datacenter,
        region=region,
        network=network,
        document=_freeze(document),
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def platform_config_identity(platform: PlatformConfig) -> str:
    """Hash only the stable deployment inventory used by the controller state marker.

    Image, flavor, version, checksum, and container selections intentionally do
    not participate: changing those values is a normal image or release
    upgrade.  Resource names, paths, network identity, and public deployment
    identity do participate, so a database copied between two deployments (or
    into a differently named deployment) cannot be accepted by the control
    surface.
    """
    keys = (
        "project",
        "projectId",
        "prefix",
        "namespace",
        "domain",
        "recoveryDomains",
        "datacenter",
        "region",
        "network",
        "internalNames",
        "addresses",
        "hosts",
        "ports",
        "volumes",
        "paths",
        "pki",
    )
    try:
        projection = {key: _plain(platform.document.get(key)) for key in keys}
        encoded = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (KeyError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise ValidationError("platform deployment identity is malformed") from error
    return hashlib.sha256(encoded).hexdigest()


def load_policy(path: str | Path, *, require_private: bool = True) -> Policy:
    """Load operator policy; private mode forbids group or other access."""
    document = _read_json(Path(path), private=require_private)
    _keys(
        document,
        {"standard", "runtimeImages", "backupAgeRecipient", "limits"},
        field="policy",
        required={"standard", "runtimeImages", "backupAgeRecipient"},
    )

    standard = document["standard"]
    if not isinstance(standard, dict):
        raise ValidationError("standard must be a JSON object")
    _keys(standard, _STANDARD_KEYS, field="standard")
    worker_flavor = _text(standard["workerFlavor"], field="standard.workerFlavor")
    cpu_mhz = _positive_int(
        standard["cpuMHz"], field="standard.cpuMHz", minimum=100, maximum=1_000_000
    )
    profile = StandardProfile(
        worker_flavor=worker_flavor,
        cpu_mhz=cpu_mhz,
        memory_mib=_positive_int(
            standard["memoryMiB"], field="standard.memoryMiB", maximum=1_048_576
        ),
        postgres_connections=_positive_int(
            standard["postgresConnections"], field="standard.postgresConnections", maximum=10_000
        ),
        postgres_measured_bytes=_positive_int(
            standard["postgresMeasuredBytes"], field="standard.postgresMeasuredBytes"
        ),
        mongo_measured_bytes=_positive_int(
            standard["mongoMeasuredBytes"], field="standard.mongoMeasuredBytes"
        ),
        s3_bytes=_positive_int(standard["s3Bytes"], field="standard.s3Bytes"),
        s3_objects=_positive_int(standard["s3Objects"], field="standard.s3Objects"),
    )

    runtime_images = document["runtimeImages"]
    if not isinstance(runtime_images, dict):
        raise ValidationError("runtimeImages must be a JSON object")
    _keys(runtime_images, {"bun", "node"}, field="runtimeImages")
    images = RuntimeImages(
        bun=oci_digest_pin(runtime_images["bun"], field="runtimeImages.bun"),
        node=oci_digest_pin(runtime_images["node"], field="runtimeImages.node"),
    )

    supplied_limits = document.get("limits", {})
    if not isinstance(supplied_limits, dict):
        raise ValidationError("limits must be a JSON object")
    unknown_limits = supplied_limits.keys() - _LIMIT_NAMES.keys()
    if unknown_limits:
        raise ValidationError(f"limits has unknown keys: {', '.join(sorted(unknown_limits))}")
    limit_values: dict[str, int] = {}
    for name, (default, minimum, maximum) in _LIMIT_NAMES.items():
        limit_values[name] = _positive_int(
            supplied_limits.get(name, default),
            field=f"limits.{name}",
            minimum=minimum,
            maximum=maximum,
        )
    limits = Limits(
        source_bytes=limit_values["sourceBytes"],
        dotenv_bytes=limit_values["dotenvBytes"],
        environment_value_bytes=limit_values["environmentValueBytes"],
        stderr_bytes=limit_values["stderrBytes"],
        build_log_bytes=limit_values["buildLogBytes"],
        helper_request_bytes=limit_values["helperRequestBytes"],
        helper_response_bytes=limit_values["helperResponseBytes"],
        connect_seconds=limit_values["connectSeconds"],
        http_seconds=limit_values["httpSeconds"],
        process_seconds=limit_values["processSeconds"],
        helper_seconds=limit_values["helperSeconds"],
        poll_interval_seconds=limit_values["pollIntervalSeconds"],
    )
    return Policy(
        standard=profile,
        runtime_images=images,
        backup_age_recipient=age_recipient(document["backupAgeRecipient"]),
        limits=limits,
    )


def load(
    platform_path: str | Path, policy_path: str | Path, *, require_private_policy: bool = True
) -> Config:
    """Load the inventory and operator policy as one immutable record."""
    return Config(
        platform=load_platform(platform_path),
        policy=load_policy(policy_path, require_private=require_private_policy),
    )
