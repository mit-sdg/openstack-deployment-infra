"""Typed access to the canonical cross-component implementation contract.

The source of truth is ``infra/lib/platform_contract.json``. Packaging places
that same file in this package, while source-tree execution uses the repository
copy. Nix and standalone infrastructure scripts consume the source directly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    """The packaged implementation contract is missing or malformed."""


def _contract_bytes() -> bytes:
    packaged = files("openstack_platform").joinpath("platform_contract.json")
    if packaged.is_file():
        return packaged.read_bytes()
    source = Path(__file__).resolve().parents[1] / "infra/lib/platform_contract.json"
    try:
        return source.read_bytes()
    except OSError as error:
        raise ContractError("platform implementation contract is unavailable") from error


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"platform contract contains duplicate key {key!r}")
        result[key] = value
    return result


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ContractError(f"platform contract {field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"platform contract {field} must be non-empty text")
    return value


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractError(f"platform contract {field} must be a string array")
    result = tuple(_string(item, field) for item in value)
    if not result or len(result) != len(set(result)):
        raise ContractError(f"platform contract {field} must be non-empty and unique")
    return result


def _port(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ContractError(f"platform contract {field} must be a valid TCP port")
    return value


def _positive_id(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"platform contract {field} must be a positive integer")
    return value


def _ascii(value: object, field: str) -> bytes:
    text = _string(value, field)
    try:
        return text.encode("ascii")
    except UnicodeEncodeError as error:
        raise ContractError(f"platform contract {field} must be ASCII") from error


try:
    _CONTRACT = json.loads(_contract_bytes(), object_pairs_hook=_reject_duplicates)
except (UnicodeDecodeError, json.JSONDecodeError) as error:
    raise ContractError("platform implementation contract is invalid JSON") from error
_CONTRACT = _object(_CONTRACT, "root")
_EXPECTED_SECTIONS = {
    "version",
    "roles",
    "ports",
    "accounts",
    "executables",
    "directories",
    "installation",
    "protocol",
    "inventory",
}
if set(_CONTRACT) != _EXPECTED_SECTIONS:
    raise ContractError("platform implementation contract has an unexpected shape")
if _CONTRACT.get("version") != 1:
    raise ContractError("platform implementation contract version is unsupported")

_ROLES = _object(_CONTRACT.get("roles"), "roles")
_PORTS = _object(_CONTRACT.get("ports"), "ports")
_ACCOUNTS = _object(_CONTRACT.get("accounts"), "accounts")
_EXECUTABLES = _object(_CONTRACT.get("executables"), "executables")
_DIRECTORIES = _object(_CONTRACT.get("directories"), "directories")
_INSTALLATION = _object(_CONTRACT.get("installation"), "installation")
_PROTOCOL = _object(_CONTRACT.get("protocol"), "protocol")
_INVENTORY = _object(_CONTRACT.get("inventory"), "inventory")
_OPERATOR_ACCOUNT = _object(_ACCOUNTS.get("operator"), "accounts.operator")

IMAGE_ROLES = _strings(_ROLES.get("all"), "roles.all")
PERSISTENT_ROLES = _strings(_ROLES.get("persistent"), "roles.persistent")
if not set(PERSISTENT_ROLES) < set(IMAGE_ROLES):
    raise ContractError("persistent roles must be a proper subset of all roles")

APPLICATION_HOST_PORT = _port(_PORTS.get("application"), "ports.application")
NOMAD_HTTP_PORT = _port(_PORTS.get("nomadHttp"), "ports.nomadHttp")
GARAGE_RPC_PORT = _port(_PORTS.get("garageRpc"), "ports.garageRpc")
GARAGE_S3_PORT = _port(_PORTS.get("garageS3"), "ports.garageS3")
REGISTRY_PORT = _port(_PORTS.get("registry"), "ports.registry")
POSTGRES_PORT = _port(_PORTS.get("postgres"), "ports.postgres")
MONGODB_PORT = _port(_PORTS.get("mongodb"), "ports.mongodb")

BUILDER_EXECUTABLE_NAME = _string(_EXECUTABLES.get("builder"), "executables.builder")

CONTROLLER_BACKUP_DIRECTORY = _string(
    _DIRECTORIES.get("controllerBackup"), "directories.controllerBackup"
)

INSTALLATION_OPERATOR_ROOT = _string(
    _INSTALLATION.get("operatorRoot"), "installation.operatorRoot"
)
INSTALLATION_SYSTEM_CONFIG_ROOT = _string(
    _INSTALLATION.get("systemConfigRoot"), "installation.systemConfigRoot"
)
INSTALLATION_SYSTEM_RUNTIME_ROOT = _string(
    _INSTALLATION.get("systemRuntimeRoot"), "installation.systemRuntimeRoot"
)
INVENTORY_FILENAME = _string(
    _INSTALLATION.get("inventoryFilename"), "installation.inventoryFilename"
)
POLICY_FILENAME = _string(_INSTALLATION.get("policyFilename"), "installation.policyFilename")
DATABASE_FILENAME = _string(
    _INSTALLATION.get("databaseFilename"), "installation.databaseFilename"
)
OPERATOR_SSH_ALIAS = _string(_INSTALLATION.get("sshAlias"), "installation.sshAlias")
OPERATOR_ACCOUNT_NAME = _string(_OPERATOR_ACCOUNT.get("name"), "accounts.operator.name")
OPERATOR_ACCOUNT_UID = _positive_id(
    _OPERATOR_ACCOUNT.get("uid"), "accounts.operator.uid"
)

DEPLOYMENT_ROUTE_HEADER = _string(
    _PROTOCOL.get("deploymentRouteHeader"), "protocol.deploymentRouteHeader"
)
DEPLOYMENT_ROUTE_HEADER_LOWER = DEPLOYMENT_ROUTE_HEADER.lower()

NOMAD_CANDIDATE_JOB_SHA_KEY = _string(
    _PROTOCOL.get("nomadCandidateJobShaKey"), "protocol.nomadCandidateJobShaKey"
)
NOMAD_CANDIDATE_IMAGE_KEY = _string(
    _PROTOCOL.get("nomadCandidateImageKey"), "protocol.nomadCandidateImageKey"
)
NOMAD_ROUTE_MARKER_KEY = _string(
    _PROTOCOL.get("nomadRouteMarkerKey"), "protocol.nomadRouteMarkerKey"
)
NOMAD_SOURCE_COMMIT_KEY = _string(
    _PROTOCOL.get("nomadSourceCommitKey"), "protocol.nomadSourceCommitKey"
)
NOMAD_RECIPE_SHA_KEY = _string(_PROTOCOL.get("nomadRecipeShaKey"), "protocol.nomadRecipeShaKey")

DATABASE_GREENFIELD_MARKER = _ascii(
    _PROTOCOL.get("databaseGreenfieldMarker"), "protocol.databaseGreenfieldMarker"
)
DATABASE_DEPLOYMENT_MARKER_PREFIX = _ascii(
    _PROTOCOL.get("databaseDeploymentMarkerPrefix"),
    "protocol.databaseDeploymentMarkerPrefix",
)
POSTGRES_OWNERSHIP_MARKER_PREFIX = _string(
    _PROTOCOL.get("postgresOwnershipMarkerPrefix"),
    "protocol.postgresOwnershipMarkerPrefix",
)
MONGO_OWNER_FIELD = _string(_PROTOCOL.get("mongoOwnerField"), "protocol.mongoOwnerField")
MONGO_OPERATION_FIELD = _string(
    _PROTOCOL.get("mongoOperationField"), "protocol.mongoOperationField"
)
MONGO_GENERATION_FIELD = _string(
    _PROTOCOL.get("mongoGenerationField"), "protocol.mongoGenerationField"
)

INVENTORY_ALLOWED_TOP_LEVEL = frozenset(
    _strings(_INVENTORY.get("allowedTopLevel"), "inventory.allowedTopLevel")
)
INVENTORY_REQUIRED_TOP_LEVEL = frozenset(
    _strings(_INVENTORY.get("requiredTopLevel"), "inventory.requiredTopLevel")
)
INVENTORY_REQUIRED_PATHS = _strings(_INVENTORY.get("requiredPaths"), "inventory.requiredPaths")
if not INVENTORY_REQUIRED_TOP_LEVEL <= INVENTORY_ALLOWED_TOP_LEVEL:
    raise ContractError("required inventory keys must be allowed")
