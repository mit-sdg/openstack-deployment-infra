#!/usr/bin/env python3
"""Load the canonical implementation contract used by Python, Nix, and scripts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

CONTRACT_PATH = Path(__file__).with_name("platform_contract.json")


class ContractError(RuntimeError):
    """The checked-in implementation contract is malformed."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"platform contract contains duplicate key {key!r}")
        result[key] = value
    return result


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ContractError(f"platform contract {field} must be an object")
    return value


def _strings(value: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ContractError(f"platform contract {field} must be a non-empty string array")
    items = tuple(cast(str, item) for item in value)
    if not items:
        raise ContractError(f"platform contract {field} must not be empty")
    if len(items) != len(set(items)):
        raise ContractError(f"platform contract {field} contains duplicates")
    return items


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ContractError("platform contract is unavailable") from error
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError("platform contract is not valid UTF-8 JSON") from error
    root = _mapping(document, "root")
    if set(root) != {
        "version",
        "roles",
        "ports",
        "accounts",
        "executables",
        "directories",
        "installation",
        "protocol",
        "inventory",
    }:
        raise ContractError("platform contract has an unexpected top-level shape")
    if root["version"] != 1:
        raise ContractError("platform contract version is unsupported")

    roles = _mapping(root["roles"], "roles")
    all_roles = _strings(roles.get("all"), "roles.all")
    persistent_roles = _strings(roles.get("persistent"), "roles.persistent")
    if not set(persistent_roles) < set(all_roles):
        raise ContractError("persistent roles must be a proper subset of all roles")

    ports = _mapping(root["ports"], "ports")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535
        for value in ports.values()
    ):
        raise ContractError("platform contract ports must be valid TCP port numbers")

    accounts = _mapping(root["accounts"], "accounts")
    account_names: list[str] = []
    user_ids: list[int] = []
    group_ids: list[int] = []
    for key, raw_account in accounts.items():
        account = _mapping(raw_account, f"accounts.{key}")
        if not {"name", "gid"} <= set(account) or set(account) - {"name", "uid", "gid"}:
            raise ContractError(f"platform contract account {key} has an invalid shape")
        name = account["name"]
        if not isinstance(name, str) or not name or any(char.isspace() for char in name):
            raise ContractError(f"platform contract account {key} has an invalid name")
        account_names.append(name)
        uid = account.get("uid")
        gid = account["gid"]
        if uid is not None:
            if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
                raise ContractError(f"platform contract account {key} has an invalid UID")
            user_ids.append(uid)
        if isinstance(gid, bool) or not isinstance(gid, int) or gid <= 0:
            raise ContractError(f"platform contract account {key} has an invalid GID")
        group_ids.append(gid)
    if (
        len(account_names) != len(set(account_names))
        or len(user_ids) != len(set(user_ids))
        or len(group_ids) != len(set(group_ids))
    ):
        raise ContractError("platform contract account names, UIDs, and GIDs must be unique")

    installation = _mapping(root["installation"], "installation")
    expected_installation = {
        "operatorRoot",
        "systemConfigRoot",
        "systemRuntimeRoot",
        "inventoryFilename",
        "policyFilename",
        "databaseFilename",
        "sshAlias",
    }
    if set(installation) != expected_installation:
        raise ContractError("platform contract installation has an invalid shape")
    for key in ("operatorRoot", "systemConfigRoot", "systemRuntimeRoot"):
        value = installation[key]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ContractError(f"platform contract installation {key} must be absolute")
    for key in ("inventoryFilename", "policyFilename", "databaseFilename", "sshAlias"):
        value = installation[key]
        if (
            not isinstance(value, str)
            or not value
            or "/" in value
            or any(char.isspace() for char in value)
        ):
            raise ContractError(f"platform contract installation {key} is malformed")

    inventory = _mapping(root["inventory"], "inventory")
    allowed = _strings(inventory.get("allowedTopLevel"), "inventory.allowedTopLevel")
    required = _strings(inventory.get("requiredTopLevel"), "inventory.requiredTopLevel")
    required_paths = _strings(inventory.get("requiredPaths"), "inventory.requiredPaths")
    if not set(required) <= set(allowed):
        raise ContractError("required inventory keys must be allowed")
    if not set(required) <= {path.split(".", 1)[0] for path in required_paths}:
        raise ContractError("required inventory keys must have required paths")

    return dict(root)


CONTRACT = load_contract()
