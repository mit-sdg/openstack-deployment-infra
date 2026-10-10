"""Copy an exact shared resource, then publish its isolated endpoint once verified."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any

import psycopg
from pymongo import MongoClient

from ..contracts import MONGODB_PORT, POSTGRES_PORT
from ..validation import uuid
from . import storage as s
from .instances import connect_ready
from .storage_limits import MONGO_BLOCKED_ROLE, mongo_reconcile, mongo_role


def migrate(args: Mapping[str, Any], *, client: Any, runtime: Any, nomad: Any) -> Mapping[str, Any]:
    s._exact(
        args,
        {
            "applicationId",
            "applicationSlug",
            "providerId",
            "providerName",
            "instanceId",
            "quotas",
            "operationId",
            "recover",
            "copySeconds",
            "type",
            "workerIds",
            "reservations",
            "retainedWorker",
        },
        "storage.instances.migrate",
    )
    kind = args["type"]
    if kind not in {"postgres", "mongo"}:
        raise ValueError("only database resources can be migrated")
    identifier = uuid(args["instanceId"], field="instance ID")
    application_id, app_slug = s._common(args, kind)
    database = s._require_fixed_provider(args, application_id, kind)
    s._TRUSTED_HOSTS.set((runtime.platform.get("addresses.storage"),))
    host = runtime.platform.get("internalNames.storage")
    ca = str(runtime.root / "secrets/nomad-cli/internal-ca.pem")
    from .production import _provider_app, _read_environment

    bootstrap = _read_environment(runtime.root / "secrets/storage-bootstrap.env")
    addresses = []
    for worker in args["workerIds"]:
        if args["retainedWorker"] is not None and worker == args["retainedWorker"]["slotId"]:
            addresses.append(args["retainedWorker"]["address"])
            continue
        observed = _provider_app("app.worker.observe", {"applicationId": worker, "slug": app_slug})
        if observed.get("address"):
            addresses.append(observed["address"])
    existing = client.call("credentials", identifier)
    if existing.get("absent") is not True:
        staged = client.call("observe", identifier)
        if staged["migrationState"] == "aborted" and staged["quotas"] != args["quotas"]:
            client.call(
                "limits", identifier, quotas=args["quotas"], reservations=args["reservations"]
            )
    client.call(
        "create",
        identifier,
        applicationId=application_id,
        type=kind,
        quotas=args["quotas"],
        allowIps=[],
        reservations=args["reservations"],
    )
    target_info = client.call("credentials", identifier)
    observed = client.call("observe", identifier)
    environment = s._owned_environment(nomad, app_slug, kind)
    already_published = (
        int(environment["PGPORT"]) == target_info["port"]
        if kind == "postgres"
        else urllib.parse.urlsplit(environment["MONGODB_URI"]).port == target_info["port"]
    )
    if already_published:
        if observed["migrationState"] != "switched":
            raise ValueError("instance publication has no durable seal")
        client.call(
            "allow", identifier, applicationId=application_id, addresses=addresses, mode="replace"
        )
        return {"instancePort": target_info["port"], "verified": True, "published": True}
    s._PORT_CONTEXT.set((POSTGRES_PORT, MONGODB_PORT))
    credential = s._credential_from_environment(kind, database, database, environment)
    if kind == "postgres":
        s._require_postgres_identity(credential, application_id=application_id, host=host)
        values = dict(
            host=host,
            dbname="platform",
            user="platform_admin",
            sslmode="verify-full",
            sslrootcert=ca,
            connect_timeout=10,
            autocommit=True,
        )
        source = psycopg.connect(
            **values, port=POSTGRES_PORT, password=bootstrap["POSTGRES_PASSWORD"]
        )
        target = connect_ready(
            lambda: psycopg.connect(
                **values, port=target_info["port"], password=target_info["adminPassword"]
            )
        )
        try:
            # Deny reconnection and finish every old session before export.
            s._pg_execute(
                source, f"ALTER ROLE {s._quote_identifier(credential.credential_name)} NOLOGIN"
            )
            s._pg_execute(
                source,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                (database,),
            )
            s._PORT_CONTEXT.set((target_info["port"], MONGODB_PORT))
            if not s._pg_exists(target, "SELECT 1 FROM pg_database WHERE datname=%s", database):
                s.postgres_create(
                    target,
                    application_id=application_id,
                    host=host,
                    connections=args["quotas"]["connections"],
                    measured_target_bytes=args["quotas"]["sizeBytes"],
                    generation=credential.credential_name.rsplit("_", 1)[1],
                    operation_id=args["operationId"],
                    password_factory=lambda: environment["PGPASSWORD"],
                )
            new_environment = s.postgres_environment(
                host, database, credential.credential_name, environment["PGPASSWORD"]
            )
        finally:
            source.close()
            target.close()
    else:
        s._require_mongo_identity(credential, application_id=application_id, host=host)
        source_mongo: MongoClient[dict[str, Any]] = MongoClient(
            host,
            MONGODB_PORT,
            username="platform_admin",
            password=bootstrap["MONGO_PASSWORD"],
            authSource="admin",
            tls=True,
            tlsCAFile=ca,
            serverSelectionTimeoutMS=10000,
        )
        target_mongo: MongoClient[dict[str, Any]] = MongoClient(
            host,
            target_info["port"],
            username="platform_admin",
            password=target_info["adminPassword"],
            authSource="admin",
            tls=True,
            tlsCAFile=ca,
            serverSelectionTimeoutMS=10000,
        )
        connect_ready(lambda: target_mongo.admin.command("ping"))
        try:
            current = observed.get("sourceRole")
            if current is None:
                current = mongo_role(source_mongo[database], credential.credential_name, database)
                client.call("freeze", identifier, sourceRole=current)
            source_mongo[database].command(
                "updateUser", credential.credential_name, roles=[{"role": "read", "db": database}]
            )
            s._PORT_CONTEXT.set((POSTGRES_PORT, target_info["port"]))
            users = s._mongo_users(target_mongo[database])
            if not any(item.get("user") == credential.credential_name for item in users):
                parsed = urllib.parse.urlsplit(environment["MONGODB_URI"])
                s.mongo_create(
                    target_mongo,
                    application_id=application_id,
                    host=host,
                    measured_target_bytes=args["quotas"]["sizeBytes"],
                    generation=credential.credential_name.rsplit("_", 1)[1],
                    operation_id=args["operationId"],
                    password_factory=lambda: urllib.parse.unquote(parsed.password or ""),
                )
            if current == MONGO_BLOCKED_ROLE:
                mongo_reconcile(
                    target_mongo[database],
                    credential.credential_name,
                    database,
                    args["quotas"]["sizeBytes"] + 1,
                    args["quotas"]["sizeBytes"],
                )
            parsed = urllib.parse.urlsplit(environment["MONGODB_URI"])
            new_environment = s.mongo_environment(
                host,
                database,
                credential.credential_name,
                urllib.parse.unquote(parsed.password or ""),
            )
        finally:
            source_mongo.close()
            target_mongo.close()
    if observed["migrationState"] not in {"verified", "switched"}:
        client.call(
            "copy",
            identifier,
            database=database,
            seconds=args["copySeconds"],
            operationId=args["operationId"],
            applicationLogin=credential.credential_name,
            applicationPassword=environment["PGPASSWORD"]
            if kind == "postgres"
            else urllib.parse.unquote(
                urllib.parse.urlsplit(environment["MONGODB_URI"]).password or ""
            ),
        )
    if kind == "mongo":
        target_mongo = MongoClient(
            host,
            target_info["port"],
            username="platform_admin",
            password=target_info["adminPassword"],
            authSource="admin",
            tls=True,
            tlsCAFile=ca,
            serverSelectionTimeoutMS=10000,
        )
        try:
            stats = target_mongo[database].command("dbStats", scale=1)
            mongo_reconcile(
                target_mongo[database],
                credential.credential_name,
                database,
                int(stats["dataSize"] + stats["indexSize"]),
                args["quotas"]["sizeBytes"],
            )
        finally:
            target_mongo.close()
    # Seal BEFORE publication; no retry may erase a database an app can see.
    client.call("seal", identifier, operationId=args["operationId"])
    client.call(
        "allow", identifier, applicationId=application_id, addresses=addresses, mode="replace"
    )
    update = s._publish(nomad, app_slug, kind, new_environment)
    return {
        "instancePort": target_info["port"],
        "verified": True,
        "published": True,
        "modifyIndex": update.modify_index,
    }


def abort(args: Mapping[str, Any], *, client: Any, runtime: Any, nomad: Any) -> Mapping[str, Any]:
    s._exact(
        args,
        {
            "applicationId",
            "applicationSlug",
            "resourceName",
            "type",
            "providerId",
            "providerName",
            "instanceId",
            "operationId",
            "sourceRole",
            "mode",
        },
        "storage.instances.abort",
    )
    kind = args["type"]
    if kind not in {"postgres", "mongo"} or args["mode"] not in {"check", "apply"}:
        raise ValueError("abort request is invalid")
    application_id, app_slug = s._common(args, kind)
    database = s._require_fixed_provider(args, application_id, kind)
    environment = s._owned_environment(nomad, app_slug, kind)
    port = (
        int(environment["PGPORT"])
        if kind == "postgres"
        else urllib.parse.urlsplit(environment["MONGODB_URI"]).port
    )
    if port != (POSTGRES_PORT if kind == "postgres" else MONGODB_PORT):
        from .main import HelperActionError

        raise HelperActionError(
            "MIGRATION_ALREADY_PUBLISHED",
            "endpoint is published; replay migration instead of aborting",
        )
    s._PORT_CONTEXT.set((POSTGRES_PORT, MONGODB_PORT))
    host = runtime.platform.get("internalNames.storage")
    s._TRUSTED_HOSTS.set((runtime.platform.get("addresses.storage"),))
    credential = s._credential_from_environment(kind, database, database, environment)
    if kind == "postgres":
        s._require_postgres_identity(credential, application_id=application_id, host=host)
    else:
        s._require_mongo_identity(credential, application_id=application_id, host=host)
    if args["mode"] == "check":
        return {"unpublished": True}
    info = client.call("credentials", args["instanceId"])
    if info.get("absent") is not True:
        client.call("cancel-copy", args["instanceId"])
        client.call("abort-copy", args["instanceId"], operationId=args["operationId"])
    from .production import _read_environment

    bootstrap = _read_environment(runtime.root / "secrets/storage-bootstrap.env")
    ca = str(runtime.root / "secrets/nomad-cli/internal-ca.pem")
    if kind == "postgres":
        with psycopg.connect(
            host=host,
            port=POSTGRES_PORT,
            dbname="platform",
            user="platform_admin",
            password=bootstrap["POSTGRES_PASSWORD"],
            sslmode="verify-full",
            sslrootcert=ca,
            connect_timeout=10,
            autocommit=True,
        ) as connection:
            s._pg_execute(
                connection, f"ALTER ROLE {s._quote_identifier(credential.credential_name)} LOGIN"
            )
    else:
        role = args["sourceRole"]
        if role not in {"readWrite", MONGO_BLOCKED_ROLE}:
            raise ValueError("checkpointed source role is invalid")
        connection_mongo: MongoClient[dict[str, Any]] = MongoClient(
            host,
            MONGODB_PORT,
            username="platform_admin",
            password=bootstrap["MONGO_PASSWORD"],
            authSource="admin",
            tls=True,
            tlsCAFile=ca,
            serverSelectionTimeoutMS=10000,
        )
        try:
            connection_mongo[database].command(
                "updateUser", credential.credential_name, roles=[{"role": role, "db": database}]
            )
        finally:
            connection_mongo.close()
    return {"unfrozen": True, "unpublished": True}
