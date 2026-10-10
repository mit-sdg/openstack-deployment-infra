"""Storage-host copy/verify stage; old shared data is never deleted here."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from pymongo import MongoClient

from . import durable
from .instance_contract import MIB
from .validation import ValidationError


def postgres_fingerprint(
    connection: Any, checksummed: set[str] | None = None
) -> dict[str, tuple[int, str | None]]:
    tables = connection.execute(
        "SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2"
    ).fetchall()
    result = {}
    for schema, name in tables:
        identifier = sql.Identifier(schema, name)
        count = connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(identifier)
        ).fetchone()[0]
        size = (
            connection.execute(
                "SELECT pg_total_relation_size(%s)",
                (
                    f'"{schema.replace(chr(34), chr(34) * 2)}"."{name.replace(chr(34), chr(34) * 2)}"',
                ),
            ).fetchone()[0]
            if checksummed is None
            else None
        )
        checksum = None
        if (size is not None and size <= 8 * MIB) or (
            checksummed is not None and f"{schema}.{name}" in checksummed
        ):
            checksum = connection.execute(
                sql.SQL(
                    "SELECT md5(string_agg(md5(row_to_json(t)::text), '' ORDER BY md5(row_to_json(t)::text))) FROM {} t"
                ).format(identifier)
            ).fetchone()[0]
        result[f"{schema}.{name}"] = (count, checksum)
    return result


def mongo_fingerprint(database: Any, checksum: bool | None = None) -> dict[str, Any]:
    names = sorted(database.list_collection_names())
    counts = {name: database[name].count_documents({}) for name in names}
    if checksum is None:
        checksum = database.command("dbStats", scale=1)["dataSize"] <= 8 * MIB
    hashes = database.command("dbHash")["collections"] if checksum else None
    return {"counts": counts, "hashes": hashes}


def copy_instance(manager: Any, config: dict[str, Any], args: Mapping[str, Any]) -> None:
    if (
        set(args) != {"action", "instanceId", "database"}
        or not isinstance(args["database"], str)
        or not re.fullmatch(r"p_[a-f0-9]{20}", args["database"])
    ):
        raise ValidationError("migration source database is invalid")
    if config.get("migrationState") in {"verified", "switched"}:
        shutil.rmtree(
            manager.data / "instance-migrations" / config["instanceId"], ignore_errors=True
        )
        return  # Publication may have started; never drop/re-copy this instance.
    deadline = time.monotonic() + 700
    config["migrationState"] = "copying"
    manager.save(config)
    directory = manager.data / "instance-migrations" / config["instanceId"]
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    host = manager.platform["addresses"]["storage"]
    namespace = manager.namespace
    ca = f"/etc/{namespace}/pki/internal-ca.pem"
    name = args["database"]
    new_password = (manager.directory(config["instanceId"]) / "admin-password").read_text()
    if config["type"] == "postgres":
        old_password = Path(f"/etc/{namespace}/secrets/postgres-password").read_text().strip()
        archive = directory / "migration.dump"
        base = {**os.environ, "PGSSLMODE": "verify-full", "PGSSLROOTCERT": ca}
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
        manager.command(
            (
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-acl",
                "--single-transaction",
                "--exit-on-error",
                "--role",
                "o_" + name[2:],
                "--host",
                host,
                "--port",
                str(config["port"]),
                "--username",
                "platform_admin",
                "--dbname",
                name,
                str(archive),
            ),
            env={**base, "PGPASSWORD": new_password},
            timeout=max(1, deadline - time.monotonic()),
        )
        values = dict(
            host=host,
            dbname=name,
            user="platform_admin",
            sslmode="verify-full",
            sslrootcert=ca,
            connect_timeout=10,
        )
        with (
            psycopg.connect(**values, port=5432, password=old_password) as source,
            psycopg.connect(**values, port=config["port"], password=new_password) as target,
        ):
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
            before = mongo_fingerprint(source_mongo[name])
            after = mongo_fingerprint(target_mongo[name], before["hashes"] is not None)
        finally:
            source_mongo.close()
            target_mongo.close()
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
