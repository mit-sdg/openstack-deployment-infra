#!/usr/bin/env python3
"""Load the shared, non-secret platform configuration."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import uuid as uuid_module
from pathlib import Path
from typing import Any, cast

LIB_DIRECTORY = Path(__file__).resolve().parent
_CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "platform_implementation_contract", LIB_DIRECTORY / "platform_contract.py"
)
if _CONTRACT_SPEC is None or _CONTRACT_SPEC.loader is None:
    raise RuntimeError("platform contract loader is unavailable")
_CONTRACT_MODULE = importlib.util.module_from_spec(_CONTRACT_SPEC)
_CONTRACT_SPEC.loader.exec_module(_CONTRACT_MODULE)
CONTRACT = cast(dict[str, Any], _CONTRACT_MODULE.CONTRACT)

NAMESPACE_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
DISPLAY_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,38}[A-Za-z0-9._-])?")
PROJECT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,126}[A-Za-z0-9._-])?")
ORGANIZATION_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,62}[A-Za-z0-9._-])?")

REPOSITORY_CONFIG = Path(__file__).resolve().parents[2] / "config" / "platform.json"
DEFAULT_PATHS = (REPOSITORY_CONFIG,)
MAXIMUM_TRANSPORT_BYTES = 65_536


INVENTORY_CONTRACT = cast(dict[str, Any], CONTRACT["inventory"])
ALLOWED_TOP_LEVEL = frozenset(cast(list[str], INVENTORY_CONTRACT["allowedTopLevel"]))
REQUIRED_TOP_LEVEL = frozenset(cast(list[str], INVENTORY_CONTRACT["requiredTopLevel"]))
REQUIRED_PATHS = tuple(cast(list[str], INVENTORY_CONTRACT["requiredPaths"]))


def config_path() -> Path:
    override = os.environ.get("PLATFORM_CONFIG")
    if override:
        return Path(override)
    for path in DEFAULT_PATHS:
        if path.is_file():
            return path
    raise FileNotFoundError("platform.json was not found; set PLATFORM_CONFIG")


def load() -> dict[str, Any]:
    path = config_path()
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("platform config must be a JSON object with string keys")
    document = cast(dict[str, Any], value)
    unknown = document.keys() - ALLOWED_TOP_LEVEL
    missing = REQUIRED_TOP_LEVEL - document.keys()
    if unknown:
        raise ValueError(f"platform config has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"platform config is missing keys: {', '.join(sorted(missing))}")

    validate(document)
    ingress = document.get("publicIngress")
    if not isinstance(ingress, dict) or set(ingress) != {"mode", "providerCidrs"}:
        raise ValueError("publicIngress must contain exactly mode and providerCidrs")
    mode = ingress.get("mode")
    cidrs = ingress.get("providerCidrs")
    if mode not in {"tunnel", "direct"}:
        raise ValueError("publicIngress.mode must be tunnel or direct")
    if not isinstance(cidrs, list) or any(not isinstance(item, str) for item in cidrs):
        raise ValueError("publicIngress.providerCidrs must be a string array")
    canonical: list[str] = []
    for item in cidrs:
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as error:
            raise ValueError("publicIngress.providerCidrs contains a malformed CIDR") from error
        if network.version != 4 or network.prefixlen == 0:
            raise ValueError(
                "publicIngress.providerCidrs must contain exact non-default IPv4 CIDRs"
            )
        canonical.append(str(network))
    if len(canonical) != len(set(canonical)):
        raise ValueError("publicIngress.providerCidrs contains duplicate CIDRs")
    if mode == "tunnel" and canonical:
        raise ValueError("tunnel ingress must not configure provider CIDRs")
    if mode == "direct" and not canonical:
        raise ValueError("direct ingress requires at least one provider CIDR")

    project = document["project"]
    if (
        not isinstance(project, str)
        or not PROJECT_RE.fullmatch(project)
        or "\x00" in project
        or "\n" in project
        or "\r" in project
    ):
        raise ValueError("platform project must be bounded non-empty text")
    project_id = document["projectId"]
    try:
        parsed_project_id = uuid_module.UUID(project_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("platform projectId must be a canonical lowercase UUID") from error
    if str(parsed_project_id) != project_id:
        raise ValueError("platform projectId must be a canonical lowercase UUID")

    display_name = document["displayName"]
    if not isinstance(display_name, str) or not DISPLAY_NAME_RE.fullmatch(display_name):
        raise ValueError("platform displayName must be 1-40 safe display characters")
    organization = document["organization"]
    if not isinstance(organization, str) or not ORGANIZATION_RE.fullmatch(organization):
        raise ValueError("platform organization must be 1-64 safe display characters")

    namespace = document["namespace"]
    if not isinstance(namespace, str) or not NAMESPACE_RE.fullmatch(namespace):
        raise ValueError("platform namespace must be 3-32 lowercase letters, numbers, or hyphens")
    internal_ca_file = document["pki"].get("internalCaFile")
    if (
        not isinstance(internal_ca_file, str)
        or not FILE_NAME_RE.fullmatch(internal_ca_file)
        or not internal_ca_file.endswith(".pem")
    ):
        raise ValueError("pki.internalCaFile must be a plain .pem file name")
    return document


def get(document: dict[str, Any], dotted: str) -> Any:
    value: Any = document
    for component in dotted.split("."):
        value = value[int(component)] if isinstance(value, list) else value[component]
    return value


def validate(document: dict[str, Any]) -> None:
    """Check every nested field the deployment scripts dereference.

    load() only checks top-level keys, so a configuration that renames or drops
    a nested field (a flavor, an image, a volume) stays silent here and fails
    much later inside an image build. Report every missing path at once.
    """
    missing: list[str] = []
    for dotted in REQUIRED_PATHS:
        try:
            get(document, dotted)
        except (KeyError, IndexError, TypeError):
            missing.append(dotted)
    if missing:
        raise ValueError(
            "platform config is missing or malformed at: " + ", ".join(sorted(missing))
        )


def shell_values(document: dict[str, Any]) -> dict[str, str | int]:
    contract_ports = cast(dict[str, int], CONTRACT["ports"])
    contract_accounts = cast(dict[str, dict[str, str | int]], CONTRACT["accounts"])
    operator_account = contract_accounts["operator"]
    controller_account = contract_accounts["controller"]
    return {
        "PLATFORM_PROJECT": document["project"],
        "PLATFORM_PROJECT_ID": document["projectId"],
        "PLATFORM_DISPLAY_NAME": document["displayName"],
        "PLATFORM_ORGANIZATION": document["organization"],
        "PLATFORM_PREFIX": document["prefix"],
        "PLATFORM_NAMESPACE": document["namespace"],
        "PLATFORM_METADATA_PREFIX": document["namespace"].replace("-", "_"),
        "PLATFORM_INTERNAL_CA_FILE": document["pki"]["internalCaFile"],
        "PLATFORM_DOMAIN": document["domain"],
        "PLATFORM_RECOVERY_DOMAIN_1": document["recoveryDomains"][0],
        "PLATFORM_RECOVERY_DOMAIN_2": document["recoveryDomains"][1],
        "PLATFORM_DATACENTER": document["datacenter"],
        "PLATFORM_REGION": document["region"],
        "PLATFORM_NETWORK": document["network"],
        "PLATFORM_STORAGE_INTERNAL_NAME": document["internalNames"]["storage"],
        "PLATFORM_OBJECT_STORAGE_INTERNAL_NAME": document["internalNames"]["objectStorage"],
        "PLATFORM_OPERATOR_CIDR": document["operatorCidr"],
        "PLATFORM_INGRESS_MODE": document["publicIngress"]["mode"],
        "PLATFORM_PROVIDER_CIDRS": ",".join(document["publicIngress"]["providerCidrs"]),
        "PLATFORM_METADATA_ADDRESS": document["metadataAddress"],
        "PLATFORM_ADMIN_IP": document["addresses"]["admin"],
        "PLATFORM_INGRESS_IP": document["addresses"]["ingress"],
        "PLATFORM_STORAGE_IP": document["addresses"]["storage"],
        "PLATFORM_ADMIN_HOST": document["hosts"]["admin"],
        "PLATFORM_INGRESS_HOST": document["hosts"]["ingress"],
        "PLATFORM_STORAGE_HOST": document["hosts"]["storage"],
        "PLATFORM_ADMIN_PORT": document["ports"]["admin"],
        "PLATFORM_INGRESS_PORT": document["ports"]["ingress"],
        "PLATFORM_STORAGE_PORT": document["ports"]["storage"],
        "PLATFORM_ADMIN_VOLUME": document["volumes"]["adminState"]["name"],
        "PLATFORM_ADMIN_VOLUME_SIZE": document["volumes"]["adminState"]["sizeGiB"],
        "PLATFORM_BACKUP_VOLUME": document["volumes"]["backup"]["name"],
        "PLATFORM_BACKUP_VOLUME_SIZE": document["volumes"]["backup"]["sizeGiB"],
        "PLATFORM_DATA_VOLUME": document["volumes"]["data"]["name"],
        "PLATFORM_DATA_VOLUME_SIZE": document["volumes"]["data"]["sizeGiB"],
        "PLATFORM_VOLUME_TYPE": document["volumes"]["data"]["type"],
        "PLATFORM_ADMIN_IMAGE": document["images"]["admin"],
        "PLATFORM_INGRESS_IMAGE": document["images"]["ingress"],
        "PLATFORM_STORAGE_IMAGE": document["images"]["storage"],
        "PLATFORM_WORKER_IMAGE": document["images"]["worker"],
        "PLATFORM_BUILDER_IMAGE": document["images"]["builder"],
        "PLATFORM_ADMIN_FLAVOR": document["flavors"]["admin"],
        "PLATFORM_INGRESS_FLAVOR": document["flavors"]["ingress"],
        "PLATFORM_STORAGE_FLAVOR": document["flavors"]["storage"],
        "PLATFORM_WORKER_FLAVOR": document["flavors"]["worker"],
        "PLATFORM_BUILDER_FLAVOR": document["flavors"]["builder"],
        "PLATFORM_POSTGRES_IMAGE": document["containers"]["postgres"],
        "PLATFORM_MONGODB_IMAGE": document["containers"]["mongodb"],
        "PLATFORM_REGISTRY_IMAGE": document["containers"]["registry"],
        "PLATFORM_ROOT": document["paths"]["root"],
        "PLATFORM_ADMIN_STATE": document["paths"]["adminState"],
        "PLATFORM_BACKUPS": document["paths"]["backups"],
        "PLATFORM_DATA": document["paths"]["data"],
        "PLATFORM_APPLICATION_PORT": contract_ports["application"],
        "PLATFORM_NOMAD_HTTP_PORT": contract_ports["nomadHttp"],
        "PLATFORM_NOMAD_RPC_PORT": contract_ports["nomadRpc"],
        "PLATFORM_NOMAD_SERF_PORT": contract_ports["nomadSerf"],
        "PLATFORM_REGISTRY_PORT": contract_ports["registry"],
        "PLATFORM_POSTGRES_PORT": contract_ports["postgres"],
        "PLATFORM_MONGODB_PORT": contract_ports["mongodb"],
        "PLATFORM_GARAGE_RPC_PORT": contract_ports["garageRpc"],
        "PLATFORM_GARAGE_S3_PORT": contract_ports["garageS3"],
        "PLATFORM_OPERATOR_USER": operator_account["name"],
        "PLATFORM_OPERATOR_UID": operator_account["uid"],
        "PLATFORM_CONTROLLER_USER": controller_account["name"],
    }


def nul_transport(document: dict[str, Any]) -> bytes:
    """Return a bounded, non-executable NAME/NUL/VALUE/NUL stream."""
    parts: list[bytes] = []
    for name, value in shell_values(document).items():
        encoded_name = name.encode("ascii")
        encoded_value = str(value).encode("utf-8")
        if b"\x00" in encoded_value:
            raise ValueError(f"platform config value for {name} contains NUL")
        parts.extend((encoded_name, b"\x00", encoded_value, b"\x00"))
    payload = b"".join(parts)
    if len(payload) > MAXIMUM_TRANSPORT_BYTES:
        raise ValueError("platform config transport exceeds the size limit")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("key")
    subparsers.add_parser("shell0")
    subparsers.add_parser("validate")
    args = parser.parse_args()
    document = load()
    if args.command == "validate":
        validate(document)
        print("platform-config=valid")
    elif args.command == "get":
        value = get(document, args.key)
        print(json.dumps(value) if isinstance(value, (dict, list)) else value)
    else:
        validate(document)
        os.write(1, nul_transport(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
