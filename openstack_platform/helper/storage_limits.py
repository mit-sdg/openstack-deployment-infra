"""Fixed provider assignments and logical usage; credentials stay at this boundary."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..controller.storage_contract import validate_quotas
from . import storage as s
from .main import HelperActionError

MONGO_BLOCKED_ROLE = "platform_size_blocked"
# Delete/drop operations let an application recover without an operator.
MONGO_BLOCKED_ACTIONS = (
    "find",
    "remove",
    "dropCollection",
    "dropIndex",
    "listCollections",
    "listIndexes",
    "collStats",
    "dbStats",
    "killCursors",
)


def mongo_role(database: Any, username: str, database_name: str) -> str:
    users = s._mongo_users(database)
    user = next((item for item in users if item.get("user") == username), None)
    if user is None or user.get("roles") not in (
        [{"role": "readWrite", "db": database_name}],
        [{"role": MONGO_BLOCKED_ROLE, "db": database_name}],
    ):
        raise HelperActionError("IDENTITY_MISMATCH", "MongoDB application role is invalid")
    return str(user["roles"][0]["role"])


def mongo_reconcile(
    database: Any, username: str, database_name: str, used: int, limit: int
) -> bool:
    current = mongo_role(database, username, database_name)
    blocked = used > limit or (current == MONGO_BLOCKED_ROLE and used * 100 > limit * 95)
    target = MONGO_BLOCKED_ROLE if blocked else "readWrite"
    if target == MONGO_BLOCKED_ROLE:
        roles = database.command("rolesInfo", MONGO_BLOCKED_ROLE)
        privileges = [
            {
                "resource": {"db": database_name, "collection": ""},
                "actions": list(MONGO_BLOCKED_ACTIONS),
            }
        ]
        if not isinstance(roles, Mapping) or not isinstance(roles.get("roles"), list):
            raise HelperActionError(
                "PROVIDER_RESPONSE_INVALID", "MongoDB role inventory is invalid"
            )
        if roles["roles"]:
            database.command("updateRole", MONGO_BLOCKED_ROLE, privileges=privileges, roles=[])
        else:
            database.command("createRole", MONGO_BLOCKED_ROLE, privileges=privileges, roles=[])
    if current != target:
        # One atomic assignment: never revoke before granting its replacement.
        database.command("updateUser", username, roles=[{"role": target, "db": database_name}])
    if mongo_role(database, username, database_name) != target:
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "MongoDB role assignment was not confirmed"
        )
    return blocked


def _count(value: Any) -> int:
    # dbStats BSON numeric values can be floats, but byte units must be exact.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or int(value) != value
    ):
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "storage usage count is invalid")
    return int(value)


def resource_action(
    args: Mapping[str, Any],
    *,
    resource_type: str,
    mutate: bool,
    admin: Any,
    nomad: Any,
    host: str,
    endpoint: str,
) -> Mapping[str, Any]:
    keys = {"applicationId", "applicationSlug", "providerId", "providerName"}
    action = f"storage.{resource_type}.{'limits' if mutate else 'usage'}"
    if mutate:
        s._operation_args(args, keys | {"quotas"}, action)
        quotas = validate_quotas(resource_type, args["quotas"])
    else:
        s._exact(args, keys, action)
        quotas = {}
    application_id, application_slug = s._common(args, resource_type)
    environment = s._owned_environment(nomad, application_slug, resource_type)
    if resource_type in {"postgres", "mongo"}:
        name = s._require_fixed_provider(args, application_id, resource_type)
    else:
        provider_id, name = s._require_s3_provider(args, environment)
    credential = s._credential_from_environment(
        resource_type, str(args["providerId"]), name, environment
    )
    objects = connections = None
    blocked = False
    if resource_type == "postgres":
        s._require_postgres_identity(credential, application_id=application_id, host=host)
        if mutate:
            s.postgres_role_settings(admin, credential.credential_name, quotas["connections"])
            s._pg_execute(
                admin,
                f"ALTER DATABASE {s._quote_identifier(name)} CONNECTION LIMIT {quotas['connections']}",
            )
        row = s._pg_execute(
            admin,
            "SELECT pg_database_size(datname), numbackends FROM pg_stat_database WHERE datname=%s",
            (name,),
        ).fetchone()
        if row is None:
            raise HelperActionError("PROVIDER_RESPONSE_INVALID", "PostgreSQL usage is unavailable")
        used, connections = _count(row[0]), _count(row[1])
    elif resource_type == "mongo":
        s._require_mongo_identity(credential, application_id=application_id, host=host)
        database = admin[name]
        if not s._mongo_owner_matches(
            s._mongo_users(database),
            application_id=application_id,
            credential_name=credential.credential_name,
        ):
            raise HelperActionError("IDENTITY_MISMATCH", "MongoDB ownership marker is invalid")
        stats = database.command("dbStats", scale=1)
        used = _count(stats["dataSize"]) + _count(stats["indexSize"])
        if mutate:
            blocked = mongo_reconcile(
                database, credential.credential_name, name, used, quotas["sizeBytes"]
            )
        else:
            blocked = mongo_role(database, credential.credential_name, name) == MONGO_BLOCKED_ROLE
        if mutate and s.mongo_port() != 27017:
            import urllib.parse

            parsed = urllib.parse.urlsplit(environment["MONGODB_URI"])
            s._MONGO_POOL.set(min(10, quotas["connections"]))
            normalized = s.mongo_environment(
                host, name, credential.credential_name, urllib.parse.unquote(parsed.password or "")
            )
            if normalized["MONGODB_URI"] != environment["MONGODB_URI"]:
                s._publish(nomad, application_slug, "mongo", normalized)
    else:
        s._require_s3_endpoint(environment, endpoint)
        s._require_s3_live_identity(
            admin,
            provider_id=provider_id,
            provider_name=name,
            access_key_id=credential.credential_name,
        )
        if mutate:
            admin.request(
                "/UpdateBucket",
                {"quotas": {"maxSize": quotas["s3Bytes"], "maxObjects": quotas["s3Objects"]}},
                query={"id": provider_id},
            )
        info = admin.request("/GetBucketInfo", query={"id": provider_id})
        if mutate and info.get("quotas") != {
            "maxSize": quotas["s3Bytes"],
            "maxObjects": quotas["s3Objects"],
        }:
            raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage quotas were not confirmed")
        used, objects = _count(info["bytes"]), _count(info["objects"])
    return {
        "applied": mutate,
        "usage": {
            "usedBytes": used,
            "objectCount": objects,
            "currentConnections": connections,
            "instanceMemoryBytes": None,
            "cpuTimeMilliseconds": None,
            "measuredAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
        "writeBlocked": blocked,
    }


def host_projection(raw: Any, names: set[str]) -> dict[str, Any]:
    """Project nested host responses before any value can enter the journal."""
    import math

    def count(value: Any, *, positive: bool = False) -> int:
        number = _count(value)
        if positive and number == 0:
            raise HelperActionError("PROVIDER_RESPONSE_INVALID", "host capacity is invalid")
        return number

    if not isinstance(raw, Mapping):
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "host metrics are invalid")
    loads = raw.get("loadAverage")
    if (
        not isinstance(loads, list)
        or len(loads) != 3
        or any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or number < 0
            for number in loads
        )
    ):
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "host load is invalid")
    memory, volume, containers = raw.get("memory"), raw.get("dataVolume"), raw.get("containers")
    if (
        not isinstance(memory, Mapping)
        or not isinstance(volume, Mapping)
        or not isinstance(containers, list)
    ):
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "host metrics are invalid")
    projected = []
    for item in containers:
        if not isinstance(item, Mapping) or item.get("name") not in names:
            raise HelperActionError(
                "PROVIDER_RESPONSE_INVALID", "host container identity is invalid"
            )
        projected.append(
            {
                "name": item["name"],
                "usedBytes": count(item.get("usedBytes")),
                "limitBytes": count(item.get("limitBytes"), positive=True),
                "available": item.get("available", True) is True,
            }
        )
    if len(projected) != len(names) or {item["name"] for item in projected} != names:
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "host container inventory is incomplete"
        )
    return {
        "cpuCount": count(raw.get("cpuCount"), positive=True),
        "loadAverage": loads,
        "memory": {
            "totalBytes": count(memory.get("totalBytes"), positive=True),
            "availableBytes": count(memory.get("availableBytes")),
        },
        "dataVolume": {
            "totalBytes": count(volume.get("totalBytes"), positive=True),
            "usedBytes": count(volume.get("usedBytes")),
        },
        "containers": projected,
    }
