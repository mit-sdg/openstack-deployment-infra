"""Storage-host copy/verify stage; old shared data is never deleted here."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from pymongo import MongoClient

from . import durable
from .instance_contract import MIB
from .validation import ValidationError


@contextmanager
def driver_guard(
    deadline: float,
    *,
    postgres: tuple[Any, ...] = (),
    mongo: tuple[Any, ...] = (),
    comment: str = "",
) -> Iterator[None]:
    """Interrupt native verification queries when the authenticated caller leaves."""
    from .storage_instances import _COPY_CANCEL

    cancel = _COPY_CANCEL.get()
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(0.1):
            if time.monotonic() < deadline and not (cancel is not None and cancel()):
                continue
            for connection in postgres:
                try:
                    connection.cancel_safe(timeout=1)
                except Exception:
                    pass
            for client in mongo:
                try:
                    for operation in client.admin.command(
                        "currentOp", {"command.comment": comment}, maxTimeMS=1000
                    ).get("inprog", []):
                        client.admin.command("killOp", op=operation["opid"], maxTimeMS=1000)
                except Exception:
                    pass
            return

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        if time.monotonic() >= deadline or (cancel is not None and cancel()):
            raise TimeoutError("instance copy caller disconnected or deadline expired")
        yield
    finally:
        finished.set()
        thread.join(timeout=3)


def postgres_fingerprint(
    connection: Any, checksummed: set[str] | None = None
) -> dict[str, tuple[int, str | None]]:
    tables = connection.execute(
        "SELECT schemaname, tablename FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2"
    ).fetchall()
    result = {}
    for schema, name in tables:
        identifier = sql.Identifier(schema, name)
        count = connection.execute(
            sql.SQL("SELECT pg_catalog.count(*) FROM {}").format(identifier)
        ).fetchone()[0]
        size = (
            connection.execute(
                "SELECT pg_catalog.pg_total_relation_size(%s)",
                (
                    f'"{schema.replace(chr(34), chr(34) * 2)}"."{name.replace(chr(34), chr(34) * 2)}"',
                ),
            ).fetchone()[0]
            if checksummed is None
            else None
        )
        checksum = None
        if (size is not None and size <= 8 * MIB) or (
            checksummed is not None and json.dumps([schema, name]) in checksummed
        ):
            checksum = connection.execute(
                sql.SQL(
                    "SELECT pg_catalog.md5(pg_catalog.string_agg(pg_catalog.md5(pg_catalog.row_to_json(t)::pg_catalog.text), '' ORDER BY pg_catalog.md5(pg_catalog.row_to_json(t)::pg_catalog.text))) FROM {} t"
                ).format(identifier)
            ).fetchone()[0]
        result[json.dumps([schema, name])] = (count, checksum)
    return result


def mongo_fingerprint(
    database: Any,
    checksum: bool | None = None,
    *,
    deadline: float | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    def options() -> dict[str, Any]:
        if deadline is None:
            return {}
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError("instance verification deadline expired")
        return {"maxTimeMS": remaining, "comment": comment}

    names = sorted(database.list_collection_names(**options()))
    counts = {name: database[name].count_documents({}, **options()) for name in names}
    if checksum is None:
        checksum = database.command("dbStats", scale=1, **options())["dataSize"] <= 8 * MIB
    hashes = database.command("dbHash", **options())["collections"] if checksum else None
    return {"counts": counts, "hashes": hashes}


def _copy_instance(manager: Any, config: dict[str, Any], args: Mapping[str, Any]) -> None:
    if (
        set(args)
        != {
            "action",
            "instanceId",
            "database",
            "seconds",
            "operationId",
            "applicationLogin",
            "applicationPassword",
        }
        or not isinstance(args["seconds"], int)
        or isinstance(args["seconds"], bool)
        or not 120 <= args["seconds"] <= 7200
        or not isinstance(args["database"], str)
        or not re.fullmatch(r"p_[a-f0-9]{20}", args["database"])
    ):
        raise ValidationError("migration source database is invalid")
    expected_login = "u_" + config["applicationId"].replace("-", "")[:20] + "_"
    if (
        not isinstance(args["applicationLogin"], str)
        or not re.fullmatch(re.escape(expected_login) + r"[a-f0-9]{8}", args["applicationLogin"])
        or not isinstance(args["applicationPassword"], str)
        or not 1 <= len(args["applicationPassword"].encode()) <= 1024
        or "\x00" in args["applicationPassword"]
    ):
        raise ValidationError("migration application credential is invalid")
    if config.get("abortedOperationId") == args["operationId"]:
        raise ValidationError("migration was aborted; start a fresh intent")
    if config.get("migrationState") in {"verified", "switched"}:
        shutil.rmtree(
            manager.data / "instance-migrations" / config["instanceId"], ignore_errors=True
        )
        return  # Publication may have started; never drop/re-copy this instance.
    deadline = time.monotonic() + args["seconds"]
    config["migrationState"] = "copying"
    manager.save(config)
    directory = manager.data / "instance-migrations" / config["instanceId"]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    host = manager.platform["internalNames"]["storage"]
    namespace = manager.namespace
    ca = f"/etc/{namespace}/pki/internal-ca.pem"
    name = args["database"]
    new_password = (manager.directory(config["instanceId"]) / "admin-password").read_text()
    if config["type"] == "postgres":
        old_password = Path(f"/etc/{namespace}/secrets/postgres-password").read_text().strip()
        archive = directory / "migration.dump"
        base = {
            **os.environ,
            "PGSSLMODE": "verify-full",
            "PGSSLROOTCERT": ca,
            "PGAPPNAME": "platform-copy:" + config["instanceId"],
            "PGOPTIONS": "-c client_connection_check_interval=1000",
        }
        manager.command(
            (
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--host",
                host,
                "--port",
                "5432",
                "--username",
                "platform_admin",
                "--dbname",
                name,
                "--file",
                str(archive),
            ),
            env={**base, "PGPASSWORD": old_password},
            timeout=max(1, deadline - time.monotonic()),
        )
        values = dict(
            host=host,
            dbname=name,
            user="platform_admin",
            sslmode="verify-full",
            sslrootcert=ca,
            connect_timeout=10,
            options=f"-c statement_timeout={max(1, int((deadline - time.monotonic()) * 1000))} -c search_path=pg_catalog",
        )
        restore = [
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--single-transaction",
            "--exit-on-error",
            "--no-comments",
            "--host",
            host,
            "--port",
            str(config["port"]),
            "--username",
            "platform_admin",
            "--dbname",
            name,
        ]
        with psycopg.connect(**values, port=config["port"], password=new_password) as target:
            with driver_guard(deadline, postgres=(target,)):
                # Discard only unpublished target objects on replay; preserve
                # database/credential markers and all shared source data.
                target.execute(
                    "SELECT pg_catalog.pg_terminate_backend(pid) FROM pg_catalog.pg_stat_activity WHERE datname=%s AND pid<>pg_catalog.pg_backend_pid()",
                    (name,),
                )
                schemas = target.execute(
                    "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema'"
                ).fetchall()
                for (schema,) in schemas:
                    target.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                target.execute(
                    "SELECT pg_catalog.lo_unlink(oid) FROM pg_catalog.pg_largeobject_metadata"
                )
                target.execute(
                    sql.SQL("CREATE SCHEMA public AUTHORIZATION {}").format(
                        sql.Identifier("o_" + name[2:])
                    )
                )
        listed = manager.command(
            ("pg_restore", "--list", str(archive)), timeout=max(1, deadline - time.monotonic())
        ).stdout
        if len(listed) > 16 * MIB:
            raise ValidationError("migration object inventory exceeded its bound")
        groups: dict[str, list[bytes]] = {"schemas": [], "extensions": [], "objects": []}
        for line in listed.splitlines(keepends=True):
            if re.match(rb"^[0-9]+; [0-9]+ [0-9]+ SCHEMA ", line):
                groups["schemas"].append(line)
            elif re.match(rb"^[0-9]+; [0-9]+ [0-9]+ EXTENSION ", line):
                groups["extensions"].append(line)
            else:
                groups["objects"].append(line)
        lists = {}
        for label, lines in groups.items():
            path = directory / (label + ".list")
            durable.atomic_write(path, b"".join(lines), mode=0o600, maximum_bytes=16 * MIB)
            lists[label] = path
        application_restore = [part for part in restore]
        application_restore[application_restore.index("--username") + 1] = args["applicationLogin"]
        app_env = {**base, "PGPASSWORD": args["applicationPassword"]}
        # Authenticate as the real app login, rather than connecting as root and
        # SET ROLE: user-defined defaults/functions cannot regain root authority.
        manager.command(
            [
                *application_restore,
                "--use-list",
                str(lists["schemas"]),
                "--role",
                "o_" + name[2:],
                str(archive),
            ],
            env=app_env,
            timeout=max(1, deadline - time.monotonic()),
        )
        # Only fixed image extension scripts run as admin, before app functions
        # exist. Suppress ownership-sensitive comments such as plpgsql's.
        manager.command(
            [*restore, "--use-list", str(lists["extensions"]), str(archive)],
            env={**base, "PGPASSWORD": new_password},
            timeout=max(1, deadline - time.monotonic()),
        )
        with psycopg.connect(**values, port=config["port"], password=new_password) as target:
            with driver_guard(deadline, postgres=(target,)):
                # Extension configuration tables can require data import rights;
                # keep their ownership with admin while granting the app owner.
                for (schema,) in target.execute(
                    "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema'"
                ).fetchall():
                    target.execute(
                        sql.SQL(
                            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}"
                        ).format(sql.Identifier(schema), sql.Identifier("o_" + name[2:]))
                    )
        for section in ("pre-data", "data", "post-data"):
            manager.command(
                [
                    *application_restore,
                    "--use-list",
                    str(lists["objects"]),
                    "--section=" + section,
                    "--role",
                    "o_" + name[2:],
                    str(archive),
                ],
                env=app_env,
                timeout=max(1, deadline - time.monotonic()),
            )
        with (
            psycopg.connect(**values, port=5432, password=old_password) as source,
            psycopg.connect(**values, port=config["port"], password=new_password) as target,
        ):
            with driver_guard(deadline, postgres=(source, target)):
                before = postgres_fingerprint(source)
                after = postgres_fingerprint(
                    target, {name for name, (_, digest) in before.items() if digest is not None}
                )
    else:
        old_password = Path(f"/etc/{namespace}/secrets/mongodb-password").read_text().strip()

        def uri(port: int, password: str) -> str:
            return f"mongodb://platform_admin:{urllib.parse.quote(password, safe='')}@{host}:{port}/?authSource=admin&tls=true&tlsCAFile={urllib.parse.quote(ca, safe='')}&serverSelectionTimeoutMS=10000"

        source_uri, target_uri = uri(27017, old_password), uri(config["port"], new_password)
        archive = directory / "migration.archive"
        files = []
        for label, value in (("source", source_uri), ("target", target_uri)):
            path = directory / f"{label}-migration.json"
            durable.atomic_write(
                path, json.dumps({"uri": value}).encode(), mode=0o600, maximum_bytes=65536
            )
            files.append(path)
        try:
            manager.command(
                (
                    "mongodump",
                    "--config",
                    str(files[0]),
                    "--db",
                    name,
                    "--gzip",
                    "--archive=" + str(archive),
                ),
                timeout=max(1, deadline - time.monotonic()),
            )
            manager.command(
                (
                    "mongorestore",
                    "--config",
                    str(files[1]),
                    "--numParallelCollections=1",
                    "--numInsertionWorkersPerCollection=1",
                    "--nsInclude",
                    name + ".*",
                    "--drop",
                    "--gzip",
                    "--archive=" + str(archive),
                ),
                timeout=max(1, deadline - time.monotonic()),
            )
            source_mongo: MongoClient[dict[str, Any]] = MongoClient(source_uri)
            target_mongo: MongoClient[dict[str, Any]] = MongoClient(target_uri)
            try:
                comment = "platform-copy:" + config["instanceId"]
                with driver_guard(deadline, mongo=(source_mongo, target_mongo), comment=comment):
                    before = mongo_fingerprint(
                        source_mongo[name], deadline=deadline, comment=comment
                    )
                    after = mongo_fingerprint(
                        target_mongo[name],
                        before["hashes"] is not None,
                        deadline=deadline,
                        comment=comment,
                    )
            finally:
                source_mongo.close()
                target_mongo.close()
        finally:
            for secret_file in files:
                secret_file.unlink(missing_ok=True)
    if time.monotonic() >= deadline:
        raise TimeoutError("instance verification deadline expired")
    if before != after:
        raise ValidationError("instance copy verification failed; old database remains intact")
    config["verificationHash"] = hashlib.sha256(
        json.dumps(before, sort_keys=True).encode()
    ).hexdigest()
    config["migrationState"] = "verified"
    manager.save(config)
    # The durable verification checkpoint permits reclaiming temporary archives;
    # source databases remain intact until a separate explicit cleanup.
    shutil.rmtree(directory)


def copy_instance(manager: Any, config: dict[str, Any], args: Mapping[str, Any]) -> None:
    boosted = config["type"] == "mongo" and config["quotas"]["memoryBytes"] < 1024**3
    if boosted:
        with manager.locked():
            target = {**config["quotas"], "memoryBytes": 1024**3}
            manager.capacity(config["instanceId"], target, maintenance=True)
            config["restoreMemoryBytes"] = target["memoryBytes"]
            config["reservedQuotas"] = target
            manager.save(config)
            manager.apply(config, restart=False)
    try:
        _copy_instance(manager, config, args)
    finally:
        if boosted:
            with manager.locked():
                config.pop("restoreMemoryBytes", None)
                config["reservedQuotas"] = config["acceptedQuotas"]
                manager.save(config)
                manager.apply(config, restart=False)
