"""Logical database bundles: shared and isolated endpoints, credentials and recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import psycopg
from pymongo import MongoClient

from . import durable
from .controller.storage_contract import canonical_secret_keys, storage_owner
from .helper.instances import InstanceClient
from .helper.nomad import update_owned_items, variable_path
from .validation import ValidationError, resource_name, slug, uuid

FORMAT = "platform-database-logical-v1"
MAX_METADATA = 16 * 1024**2
MAX_PAYLOAD = 1024**4
DATABASE = re.compile(r"p_[a-f0-9]{20}")
ROOT_ROLE = re.compile(
    rb'^(?:DROP ROLE IF EXISTS|CREATE ROLE|ALTER ROLE) "?platform_admin"?(?:[ ;])'
)


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024**2):
            result.update(chunk)
    return result.hexdigest()


def command(argv: Sequence[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        tuple(argv), check=True, stderr=subprocess.DEVNULL, timeout=3600, **options
    )


class Native:
    def __init__(self, host: str, ca: str, *, execute: Callable[..., Any] = command):
        self.host, self.ca, self.execute = host, ca, execute

    def pg_options(self, entry: Mapping[str, Any], password: str) -> dict[str, Any]:
        return dict(
            host=self.host,
            port=entry["port"],
            user="platform_admin",
            password=password,
            dbname="postgres",
            sslmode="verify-full",
            sslrootcert=self.ca,
            connect_timeout=10,
        )

    def uri(self, entry: Mapping[str, Any], password: str) -> str:
        return f"mongodb://platform_admin:{urllib.parse.quote(password, safe='')}@{self.host}:{entry['port']}/?authSource=admin&tls=true&tlsCAFile={urllib.parse.quote(self.ca, safe='')}&serverSelectionTimeoutMS=10000"

    def databases(self, kind: str, port: int, password: str) -> list[str]:
        if kind == "postgres":
            with psycopg.connect(**self.pg_options({"port": port}, password)) as connection:
                return [
                    row[0]
                    for row in connection.execute(
                        "SELECT datname FROM pg_database WHERE datname ~ '^p_[a-f0-9]{20}$' ORDER BY datname"
                    )
                ]
        client: MongoClient[dict[str, Any]] = MongoClient(self.uri({"port": port}, password))
        try:
            return sorted(name for name in client.list_database_names() if DATABASE.fullmatch(name))
        finally:
            client.close()

    def tools(
        self, entry: Mapping[str, Any], password: str, directory: Path
    ) -> tuple[list[str], dict[str, str]]:
        if entry["type"] == "postgres":
            return [
                "--host",
                self.host,
                "--port",
                str(entry["port"]),
                "--username",
                "platform_admin",
            ], {
                **os.environ,
                "PGPASSWORD": password,
                "PGSSLMODE": "verify-full",
                "PGSSLROOTCERT": self.ca,
            }
        configuration = directory / "mongo-tools.json"
        durable.atomic_write(
            configuration,
            json.dumps({"uri": self.uri(entry, password)}).encode(),
            mode=0o600,
            maximum_bytes=65536,
        )
        return ["--config", str(configuration)], dict(os.environ)

    def dump(self, entry: Mapping[str, Any], password: str, path: Path) -> None:
        args, environment = self.tools(entry, password, path.parent)
        with path.open("wb") as output:
            os.chmod(path, 0o600)
            if entry["type"] == "postgres":
                from psycopg import sql

                # Back up one database with all its generated roles, hashes,
                # role settings, comments and memberships. No root role changes.
                database = entry["databases"][0]
                suffix = database[2:]
                with psycopg.connect(**self.pg_options(entry, password)) as connection:
                    roles = connection.execute(
                        "SELECT rolname, rolcanlogin, rolconnlimit, rolpassword, rolconfig, shobj_description(oid,'pg_authid') FROM pg_authid WHERE rolname=%s OR oid IN (SELECT member FROM pg_auth_members WHERE roleid=(SELECT oid FROM pg_roles WHERE rolname=%s)) ORDER BY rolname",
                        ("o_" + suffix, "o_" + suffix),
                    ).fetchall()
                    from .controller.storage_contract import provider_environment

                    live_logins = {
                        provider_environment("postgres", row["name"], row["bindings"])["PGUSER"]
                        for row in entry.get("resources", [])
                    }
                    for name, login, limit, secret, settings, comment in roles:
                        # A quiesced migration source has NOLOGIN. Its backup
                        # restores the published app login, never retired users.
                        login = login or name in live_logins
                        # An existing target role is updated atomically; restore replay
                        # never drops a role still owning a live database.
                        ident = sql.Identifier(name)
                        create = sql.SQL("CREATE ROLE {};").format(ident).as_string(connection)
                        output.write(
                            sql.SQL(
                                "DO $platform_restore$ BEGIN {}; EXCEPTION WHEN duplicate_object THEN NULL; END $platform_restore$;\n"
                            )
                            .format(sql.SQL(create.rstrip(";")))
                            .as_string(connection)
                            .encode()
                        )
                        output.write(
                            sql.SQL("ALTER ROLE {} {} CONNECTION LIMIT {} PASSWORD {};\n")
                            .format(
                                ident,
                                sql.SQL("LOGIN" if login else "NOLOGIN"),
                                sql.Literal(limit),
                                sql.Literal(secret),
                            )
                            .as_string(connection)
                            .encode()
                        )
                        for setting in settings or []:
                            key, value = setting.split("=", 1)
                            output.write(
                                sql.SQL("ALTER ROLE {} SET {} TO {};\n")
                                .format(ident, sql.Identifier(key), sql.Literal(value))
                                .as_string(connection)
                                .encode()
                            )
                        if comment is not None:
                            output.write(
                                sql.SQL("COMMENT ON ROLE {} IS {};\n")
                                .format(ident, sql.Literal(comment))
                                .as_string(connection)
                                .encode()
                            )
                    memberships = connection.execute(
                        "SELECT r.rolname,m.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid=a.roleid JOIN pg_roles m ON m.oid=a.member WHERE r.rolname=%s",
                        ("o_" + suffix,),
                    ).fetchall()
                    for role, member in memberships:
                        output.write(
                            sql.SQL("GRANT {} TO {};\n")
                            .format(sql.Identifier(role), sql.Identifier(member))
                            .as_string(connection)
                            .encode()
                        )
                output.flush()
                self.execute(
                    [
                        "pg_dump",
                        "--create",
                        "--clean",
                        "--if-exists",
                        "--use-set-session-authorization",
                        *args,
                        "--dbname",
                        database,
                    ],
                    env=environment,
                    stdout=output,
                )
            else:
                self.execute(
                    [
                        "mongodump",
                        *args,
                        "--db",
                        entry["databases"][0],
                        "--dumpDbUsersAndRoles",
                        "--gzip",
                        "--archive",
                    ],
                    env=environment,
                    stdout=output,
                )

    def restore(self, entry: Mapping[str, Any], password: str, path: Path) -> None:
        args, environment = self.tools(entry, password, path.parent)
        if entry["type"] == "postgres":
            cleaned = path.parent / "restore.sql"
            filter_postgres(path, cleaned)
            self.execute(
                [
                    "psql",
                    "-X",
                    "--set=ON_ERROR_STOP=1",
                    *args,
                    "--dbname=postgres",
                    "--file",
                    str(cleaned),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
            )
        else:
            self.execute(
                [
                    "mongorestore",
                    *args,
                    "--db",
                    entry["databases"][0],
                    "--restoreDbUsersAndRoles",
                    "--numParallelCollections=1",
                    "--numInsertionWorkersPerCollection=1",
                    "--drop",
                    "--gzip",
                    "--archive=" + str(path),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
            )
        if entry["type"] == "mongo" and entry.get("resources"):
            from .controller.storage_contract import provider_environment

            client: MongoClient[dict[str, Any]] = MongoClient(self.uri(entry, password))
            try:
                for row in entry["resources"]:
                    raw = provider_environment("mongo", row["name"], row["bindings"])
                    name = urllib.parse.unquote(
                        urllib.parse.urlsplit(raw["MONGODB_URI"]).username or ""
                    )
                    role = (
                        "platform_size_blocked"
                        if row.get("writeBlock", {}).get("blocked")
                        else "readWrite"
                    )
                    client[row["providerName"]].command(
                        "updateUser", name, roles=[{"role": role, "db": row["providerName"]}]
                    )
            finally:
                client.close()
        if not set(entry["databases"]) <= set(
            self.databases(entry["type"], entry["port"], password)
        ):
            raise ValidationError("restored database inventory did not match")


def filter_postgres_stream(incoming: BinaryIO, outgoing: BinaryIO) -> None:
    """Preserve the replacement root; copy data without buffering giant rows."""
    import shutil

    for line in incoming:
        if line.startswith(b"\\connect "):
            outgoing.write(line)
            shutil.copyfileobj(incoming, outgoing, 1024**2)
            return
        if not ROOT_ROLE.match(line):
            outgoing.write(line)


def filter_postgres(source: Path, target: Path) -> None:
    with source.open("rb") as incoming, target.open("wb") as outgoing:
        os.chmod(target, 0o600)
        filter_postgres_stream(incoming, outgoing)


class Context:
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        manager: Any,
        provider: Any,
        nomad: Any,
        resources: Callable[[], list[dict[str, Any]]],
        platform: Any = None,
    ):
        self.platform = platform
        self.restore_ports: set[int] = set()
        self.config, self.manager, self.provider, self.nomad, self.resources = (
            config,
            manager,
            provider,
            nomad,
            resources,
        )

    def inventory(self, kind: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
        from .helper.production import _read_environment

        bootstrap = _read_environment(
            Path(
                os.environ.get(
                    "SECRETS_FILE",
                    str(Path(self.config["paths"]["root"]) / "secrets/storage-bootstrap.env"),
                )
            )
        )
        password = bootstrap["POSTGRES_PASSWORD" if kind == "postgres" else "MONGO_PASSWORD"]
        rows = [row for row in self.resources() if row["type"] == kind]
        instances = (
            {item["instanceId"]: item for item in self.manager.call("backup-inventory")["items"]}
            if any(row["instanceId"] is not None for row in rows)
            else {}
        )
        shared_names = self.provider.databases(
            kind, 5432 if kind == "postgres" else 27017, password
        )
        entries: list[dict[str, Any]] = []
        passwords = {"shared": password}
        entries.extend(
            dict(
                id="shared." + name,
                instanceId=None,
                type=kind,
                port=5432 if kind == "postgres" else 27017,
                databases=[name],
                resources=[],
            )
            for name in shared_names
        )
        for row in rows:
            if row["lifecycleState"] != "active" or row.get("backupEligible") is False:
                raise ValidationError(
                    "finish storage lifecycle recovery before committing a backup"
                )
            identifier = row["instanceId"]
            keys = canonical_secret_keys(kind, row["name"])
            values = self.nomad.read_variable(variable_path(row["applicationSlug"])).items
            if any(key not in values for key in keys):
                raise ValidationError("resource binding credentials are incomplete")
            record = {
                name: row[name]
                for name in (
                    "resourceId",
                    "applicationId",
                    "applicationSlug",
                    "name",
                    "providerName",
                    "quotas",
                )
            }
            record["bindings"] = {key: values[key] for key in keys}
            record["writeBlock"] = row.get(
                "writeBlock", {"blocked": False, "reason": None, "since": None}
            )
            if identifier is None:
                name = row["providerName"]
                selected = next((entry for entry in entries if name in entry["databases"]), None)
                if selected is None:
                    raise ValidationError("shared resource was missing from backup inventory")
                selected["resources"].append(record)
            else:
                instance = instances.get(identifier)
                if (
                    instance is None
                    or instance["applicationId"] != row["applicationId"]
                    or instance["quotas"] != row["quotas"]
                ):
                    raise ValidationError("instance backup identity was not confirmed")
                credential = self.manager.call("credentials", identifier)
                names = self.provider.databases(
                    kind, credential["port"], credential["adminPassword"]
                )
                if names != [row["providerName"]]:
                    raise ValidationError("isolated resource database inventory did not match")
                entries.append(
                    dict(
                        id=identifier,
                        instanceId=identifier,
                        type=kind,
                        port=credential["port"],
                        databases=names,
                        resources=[record],
                        applicationId=row["applicationId"],
                        quotas=instance["quotas"],
                        migrationState=instance["migrationState"],
                    )
                )
                passwords[identifier] = credential["adminPassword"]
        return entries, passwords

    def reserve_restore_ports(self, manifest: Mapping[str, Any]) -> None:
        self.restore_ports.update(
            entry["port"] for entry in manifest["entries"] if entry["instanceId"] is not None
        )

    def restore_entry(
        self,
        entry: dict[str, Any],
        path: Path,
        *,
        reservations: dict[str, int],
        controller_database: Path | None = None,
    ) -> bool | None:
        if not entry["resources"]:
            # Retained source copies remain in the encrypted archive, but no
            # longer describe an active managed resource after publication.
            return None
        if controller_database is not None:
            from .controller import database as db

            connection = db.connect(controller_database)
            try:
                for resource in entry["resources"]:
                    current = db.get_managed_resource(connection, resource["resourceId"])
                    if (
                        current is None
                        or current.application_id != resource["applicationId"]
                        or current.provider_name != resource["providerName"]
                    ):
                        raise ValidationError(
                            "replacement controller resource identity did not match"
                        )
                    if entry["instanceId"] is None and current.instance_id is not None:
                        from .controller.storage_limits import quotas

                        entry = {
                            **entry,
                            "instanceId": current.instance_id,
                            "id": current.instance_id,
                            "port": current.instance_port,
                            "applicationId": current.application_id,
                            "quotas": quotas(current),
                            "migrationState": current.migration_state,
                        }
            finally:
                connection.close()
        if entry["instanceId"] is None:
            if len(entry["resources"]) != 1:
                raise ValidationError("shared resource archive must contain one database")
            resource = entry["resources"][0]
            occupied = self.restore_ports | {
                item["port"] for item in self.manager.call("list")["items"]
            }
            existing = self.manager.call("credentials", resource["resourceId"])
            port = (
                existing["port"]
                if existing.get("absent") is not True
                else next((port for port in range(30000, 31000) if port not in occupied), None)
            )
            if port is None:
                raise ValidationError("restore instance ports are exhausted")
            self.restore_ports.add(port)
            entry = {
                **entry,
                "instanceId": resource["resourceId"],
                "id": resource["resourceId"],
                "applicationId": resource["applicationId"],
                "port": port,
                "quotas": resource["quotas"],
                "migrationState": "switched",
            }
        identifier = entry["instanceId"]
        created = self.manager.call(
            "restore-create",
            identifier,
            applicationId=entry["applicationId"],
            type=entry["type"],
            quotas=entry["quotas"],
            port=entry["port"],
            allowIps=[],
            reservations=reservations,
        )
        entry["quotas"] = created["quotas"]
        checkpoint = self.manager.call("restore-begin", identifier, backupSha256=digest(path))
        if checkpoint.get("alreadyRestored") is True:
            if controller_database is not None:
                from .controller import database as db

                connection = db.connect(controller_database)
                try:
                    db.set_storage_instance(
                        connection,
                        entry["resources"][0]["resourceId"],
                        identifier,
                        entry["port"],
                        migration_state=entry["migrationState"],
                    )
                finally:
                    connection.close()
            return True
        password = self.manager.call("credentials", identifier)["adminPassword"]
        self.provider.restore(entry, password, path)
        for resource in entry["resources"]:
            from .controller.storage_contract import canonicalize_environment, provider_environment
            from .helper import storage as storage_provider

            raw: Mapping[str, str] = provider_environment(
                entry["type"], resource["name"], resource["bindings"]
            )
            host = self.config["internalNames"]["storage"]
            marker = storage_provider._PORT_CONTEXT.set(
                (
                    entry["port"] if entry["type"] == "postgres" else 5432,
                    entry["port"] if entry["type"] == "mongo" else 27017,
                )
            )
            try:
                if entry["type"] == "postgres":
                    raw = storage_provider.postgres_environment(
                        host, resource["providerName"], raw["PGUSER"], raw["PGPASSWORD"]
                    )
                else:
                    uri = urllib.parse.urlsplit(raw["MONGODB_URI"])
                    raw = storage_provider.mongo_environment(
                        host,
                        resource["providerName"],
                        urllib.parse.unquote(uri.username or ""),
                        urllib.parse.unquote(uri.password or ""),
                    )
                bindings = canonicalize_environment(entry["type"], resource["name"], raw)
            finally:
                storage_provider._PORT_CONTEXT.reset(marker)
            owner = storage_owner(entry["type"], resource["name"])
            for job_id in (resource["applicationSlug"], resource["applicationSlug"] + "-candidate"):
                if (
                    job_id.endswith("-candidate")
                    and self.nomad.read_variable(variable_path(job_id)).modify_index == 0
                ):
                    continue
                update_owned_items(
                    self.nomad,
                    variable_path(job_id),
                    {key: owner for key in bindings},
                    owner=owner,
                    updates=bindings,
                )
            if identifier is not None and controller_database is not None:
                from .controller import database as db

                connection = db.connect(controller_database)
                try:
                    current = db.get_managed_resource(connection, resource["resourceId"])
                    if current is None or current.application_id != entry["applicationId"]:
                        raise ValidationError(
                            "replacement controller resource identity did not match"
                        )
                    with db.transaction(connection):
                        connection.execute(
                            "UPDATE managed_resources SET postgres_connections=?,mongo_connections=?,measured_target_bytes=?,memory_bytes=?,cpu_millicores=?,usage_json='{}',usage_error=NULL,write_blocked=?,blocked_since=? WHERE resource_id=?",
                            (
                                entry["quotas"]["connections"]
                                if entry["type"] == "postgres"
                                else None,
                                entry["quotas"]["connections"]
                                if entry["type"] == "mongo"
                                else None,
                                entry["quotas"]["sizeBytes"],
                                entry["quotas"]["memoryBytes"],
                                entry["quotas"]["cpuMillicores"],
                                int(resource.get("writeBlock", {}).get("blocked") is True),
                                resource.get("writeBlock", {}).get("since"),
                                current.resource_id,
                            ),
                        )
                    if self.platform is not None:
                        from .controller.nomad_jobs import storage_hosts_job

                        deployment = db.get_deployment(connection, current.application_id)
                        if deployment is not None:
                            job = storage_hosts_job(deployment.nomad_job, self.platform)
                            with db.transaction(connection):
                                connection.execute(
                                    "UPDATE deployment_attempts SET nomad_job=?,nomad_job_sha256=? WHERE deployment_id=?",
                                    (
                                        job,
                                        hashlib.sha256(job.encode()).hexdigest(),
                                        deployment.deployment_id,
                                    ),
                                )
                    db.set_storage_instance(
                        connection,
                        current.resource_id,
                        identifier,
                        entry["port"],
                        migration_state=entry["migrationState"],
                    )
                finally:
                    connection.close()
        if identifier is not None:
            if controller_database is not None:
                from .controller import database as db
                from .controller.fixed_ip_service import get as fixed_port
                from .controller.fixed_ip_service import helper_identity
                from .controller.nomad_jobs import deployment_worker_ids
                from .helper.production import _provider_app

                connection = db.connect(controller_database)
                try:
                    retained = fixed_port(connection, entry["applicationId"])
                finally:
                    connection.close()
                addresses = []
                for worker in (
                    entry["applicationId"],
                    *deployment_worker_ids(entry["applicationId"]),
                ):
                    worker_args: dict[str, Any] = {
                        "applicationId": worker,
                        "slug": entry["resources"][0]["applicationSlug"],
                    }
                    if retained is not None and retained["worker_slot_id"] == worker:
                        worker_args["retainedPort"] = helper_identity(retained)
                    observed = _provider_app("app.worker.observe", worker_args)
                    if observed.get("address") is not None:
                        addresses.append(observed["address"])
                    elif observed.get("absent") is not True:
                        raise ValidationError("restore worker inventory was not confirmed")
                self.manager.call(
                    "allow",
                    identifier,
                    applicationId=entry["applicationId"],
                    addresses=addresses,
                    mode="replace",
                )
            self.manager.call("restore-finish", identifier, migrationState=entry["migrationState"])
        return identifier is not None


def add_bytes(archive: tarfile.TarFile, name: str, value: Any) -> None:
    import io

    payload = json.dumps(value, sort_keys=True).encode()
    if len(payload) > MAX_METADATA:
        raise ValidationError("database backup metadata exceeded its bound")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(payload))


def emit(
    context: Context, kind: str, stream: BinaryIO, *, catalog: Path | None = None
) -> dict[str, Any]:
    entries, passwords = context.inventory(kind)
    manifest = {
        "format": FORMAT,
        "type": kind,
        "createdAt": timestamp(),
        "namespace": context.config["namespace"],
        "projectId": context.config["projectId"],
        "entries": entries,
    }
    with tarfile.open(fileobj=stream, mode="w|") as archive:
        add_bytes(archive, "MANIFEST.json", manifest)
        for entry in entries:
            with tempfile.TemporaryDirectory(
                prefix="database-backup-", dir=os.environ.get("DATABASE_BACKUP_TMPDIR")
            ) as directory:
                path = Path(directory) / "payload"
                context.provider.dump(
                    entry,
                    passwords[entry["id"]] if entry["instanceId"] else passwords["shared"],
                    path,
                )
                if not 0 < path.stat().st_size <= MAX_PAYLOAD:
                    raise ValidationError("database backup payload exceeded its bound")
                add_bytes(
                    archive,
                    entry["id"] + ".json",
                    {"bytes": path.stat().st_size, "sha256": digest(path)},
                )
                archive.add(path, arcname=entry["id"] + ".dump", recursive=False)
        final, _ = context.inventory(kind)
        if entries != final:
            raise ValidationError("database inventory or bindings changed during backup; retry")
    public = {
        **manifest,
        "entries": [
            {
                **entry,
                "resources": [
                    {key: value for key, value in row.items() if key != "bindings"}
                    for row in entry["resources"]
                ],
            }
            for entry in entries
        ],
    }
    if catalog is not None:
        durable.atomic_write(
            catalog,
            json.dumps(public, sort_keys=True).encode(),
            mode=0o600,
            maximum_bytes=MAX_METADATA,
        )
    return public


def read_json(archive: tarfile.TarFile, name: str) -> Any:
    member = archive.next()
    if (
        member is None
        or member.name != name
        or not member.isfile()
        or not 0 < member.size <= MAX_METADATA
    ):
        raise ValidationError("database archive metadata is invalid")
    source = archive.extractfile(member)
    assert source is not None
    return json.loads(source.read(MAX_METADATA + 1))


def validate_manifest(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("format") != FORMAT
        or value.get("type") not in {"postgres", "mongo"}
        or not isinstance(value.get("entries"), list)
        or len(value["entries"]) > 1000
    ):
        raise ValidationError("database archive manifest is invalid")
    seen = set()
    for entry in value["entries"]:
        identifier = entry.get("instanceId")
        if identifier is not None:
            uuid(identifier, field="instance ID")
            uuid(entry["applicationId"], field="application ID")
            if len(entry["resources"]) != 1:
                raise ValidationError("isolated archive must contain one resource")
            from .instance_contract import validate_limits

            validate_limits(entry["quotas"])
            if (
                entry["id"] != identifier
                or isinstance(entry["port"], bool)
                or not isinstance(entry["port"], int)
                or not 30000 <= entry["port"] <= 30999
            ):
                raise ValidationError("database archive instance endpoint is invalid")
        elif entry["port"] != (5432 if value["type"] == "postgres" else 27017):
            raise ValidationError("shared archive port is invalid")
        elif not re.fullmatch(r"shared(?:\.p_[a-f0-9]{20})?", entry["id"]):
            raise ValidationError("database archive shared identity is invalid")
        if (
            entry["id"] in seen
            or entry["type"] != value["type"]
            or not isinstance(entry["databases"], list)
            or any(not DATABASE.fullmatch(name) for name in entry["databases"])
        ):
            raise ValidationError("database archive identity is invalid")
        seen.add(entry["id"])
        for row in entry["resources"]:
            if row["providerName"] not in entry["databases"] or (
                identifier is not None and row["applicationId"] != entry["applicationId"]
            ):
                raise ValidationError("archive resource identity is inconsistent")
            uuid(row["resourceId"], field="resource ID")
            uuid(row["applicationId"], field="application ID")
            slug(row["applicationSlug"])
            resource_name(row["name"])
            if set(row["bindings"]) != set(
                canonical_secret_keys(value["type"], row["name"])
            ) or any(not isinstance(text, str) for text in row["bindings"].values()):
                raise ValidationError("database archive binding keys are invalid")
    return value


def consume(
    stream: BinaryIO,
    apply: Callable[[dict[str, Any], Path], None],
    *,
    config: Mapping[str, Any] | None = None,
    manifest_observer: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    counts = {"shared": 0, "isolated": 0}
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        manifest = validate_manifest(read_json(archive, "MANIFEST.json"))
        if config is not None and any(
            manifest[key] != config[key] for key in ("namespace", "projectId")
        ):
            raise ValidationError("database backup belongs to another deployment")
        if manifest_observer is not None:
            manifest_observer(manifest)
        for entry in manifest["entries"]:
            metadata = read_json(archive, entry["id"] + ".json")
            member = archive.next()
            if (
                member is None
                or member.name != entry["id"] + ".dump"
                or not member.isfile()
                or not 0 < member.size <= MAX_PAYLOAD
                or metadata["bytes"] != member.size
            ):
                raise ValidationError("database archive payload is invalid")
            source = archive.extractfile(member)
            assert source is not None
            with tempfile.TemporaryDirectory(
                prefix="database-restore-", dir=os.environ.get("DATABASE_BACKUP_TMPDIR")
            ) as directory:
                path = Path(directory) / "payload"
                with path.open("wb") as target:
                    os.chmod(path, 0o600)
                    while chunk := source.read(1024**2):
                        target.write(chunk)
                if digest(path) != metadata["sha256"]:
                    raise ValidationError("database archive checksum failed")
                apply(entry, path)
            counts["isolated" if entry["instanceId"] else "shared"] += 1
        if archive.next() is not None:
            raise ValidationError("database archive has unexpected entries")
    return {"type": manifest["type"], **counts}


def controller_resources(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    from .management.broker.client import UnixConnection

    def items(path: str) -> list[dict[str, Any]]:
        result = []
        cursor = None
        while True:
            client = UnixConnection(
                Path(f"/run/{config['namespace']}-controller/privileged.sock"), 30
            )
            try:
                client.request(
                    "GET",
                    path
                    + "?limit=100"
                    + ("&cursor=" + urllib.parse.quote(cursor, safe="") if cursor else ""),
                )
                response = client.getresponse()
                body = response.read(1048577)
                if response.status != 200 or len(body) > 1048576:
                    raise ValidationError("backup controller inventory is unavailable")
                page = json.loads(body)
                result.extend(page["items"])
                cursor = page["nextCursor"]
                if not cursor:
                    return result
            finally:
                client.close()

    apps = {item["applicationId"]: item["slug"] for item in items("/v1/admin/applications")}
    pending = {
        item["scope"]
        for item in items("/v1/admin/operations")
        if item["status"] in {"running", "recovery_required"}
        and (
            item["kind"]
            in {
                "storage.create",
                "storage.rotate",
                "storage.remove",
                "storage.limits.set",
                "storage.postgres.repair",
                "app.delete",
            }
        )
    }
    return [
        {
            **row,
            "applicationSlug": apps[row["applicationId"]],
            "backupEligible": "infrastructure" not in pending
            and "app-" + row["applicationId"] not in pending,
        }
        for row in items("/v1/admin/storage")
    ]


def live_context(config: Mapping[str, Any]) -> Context:
    from .helper.production import _nomad_client, _read_environment, helper_runtime

    runtime = helper_runtime()
    ca = str(Path(config["paths"]["root"]) / "secrets/nomad-cli/internal-ca.pem")
    creds = _read_environment(
        Path(os.environ.get("SECRETS_FILE", str(runtime.root / "secrets/storage-bootstrap.env")))
    )
    return Context(
        config,
        manager=InstanceClient(
            f"https://{config['addresses']['storage']}:3903",
            creds["GARAGE_ADMIN_TOKEN"],
            ssl.create_default_context(cafile=ca),
        ),
        provider=Native(config["internalNames"]["storage"], ca),
        nomad=_nomad_client(runtime),
        resources=lambda: controller_resources(config),
        platform=runtime.platform,
    )


def fresh_backup(
    root: Path, kind: str, database: str, minutes: int, *, config: Mapping[str, Any]
) -> dict[str, Any] | None:
    for directory in sorted(root.glob("20??????T??????Z"), reverse=True):
        try:
            values = dict(
                line.split("=", 1) for line in (directory / "MANIFEST").read_text().splitlines()
            )
            if values.get("format_version") != "4":
                continue
            name = "postgres" if kind == "postgres" else "mongodb"
            catalog = directory / (name + "-catalog.json")
            sums = dict(
                (name, checksum)
                for checksum, name in (
                    line.split("  ", 1)
                    for line in (directory / "SHA256SUMS").read_text().splitlines()
                )
            )
            if digest(catalog) != sums[catalog.name] or not (directory / (name + ".age")).is_file():
                continue
            value = json.loads(catalog.read_text())
            if (
                value.get("format") != FORMAT
                or value.get("type") != kind
                or any(value.get(key) != config[key] for key in ("namespace", "projectId"))
            ):
                continue
            ciphertext = directory / (name + ".age")
            if digest(ciphertext) != sums.get(ciphertext.name):
                continue
            age = (datetime.now(UTC) - datetime.fromisoformat(value["createdAt"])).total_seconds()
            if not 0 <= age <= minutes * 60:
                continue
            if any(
                entry["instanceId"] is None and database in entry["databases"]
                for entry in value["entries"]
            ):
                return {
                    "verified": True,
                    "shared": True,
                    "type": kind,
                    "database": database,
                    "backedUpAt": value["createdAt"],
                    "backup": directory.name,
                }
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def ensure_backup(
    config: Mapping[str, Any],
    kind: str,
    database: str,
    minutes: int,
    *,
    execute: Callable[..., Any] = command,
) -> dict[str, Any]:
    if (
        kind not in {"postgres", "mongo"}
        or not DATABASE.fullmatch(database)
        or not isinstance(minutes, int)
        or isinstance(minutes, bool)
        or not 1 <= minutes <= 1440
    ):
        raise ValidationError("migration backup requirement is invalid")
    root = Path(config["paths"]["backups"]) / config["namespace"]
    result = fresh_backup(root, kind, database, minutes, config=config)
    if result is None:
        execute(
            ["/run/current-system/sw/bin/openstack-platform-managed-backup"],
            stdout=subprocess.DEVNULL,
        )
        result = fresh_backup(root, kind, database, minutes, config=config)
    if result is None:
        raise ValidationError("fresh verified shared-resource backup is unavailable")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["emit", "restore", "verify", "filter-sql"])
    parser.add_argument("--type", choices=["postgres", "mongo"], required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument(
        "--restore-container", help="Disposable verification container; imports every payload"
    )
    parser.add_argument("--controller-database", type=Path)
    parser.add_argument("--resource", action="append")
    args = parser.parse_args()
    try:
        config = json.loads(Path(os.environ["PLATFORM_CONFIG"]).read_text())
        if args.action == "filter-sql":
            filter_postgres_stream(sys.stdin.buffer, sys.stdout.buffer)
        elif args.action == "emit":
            context = live_context(config)
            emit(context, args.type, sys.stdout.buffer, catalog=args.catalog)
        elif args.action == "restore":
            context = live_context(config)
            from .instance_contract import garage_reservation, hard_quota

            # Inventory the offline accepted rows for complete disk reservations.
            if args.controller_database is None:
                raise ValidationError(
                    "restore requires the offline replacement controller database"
                )
            metadata = args.controller_database.lstat()
            import stat

            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or any(
                    Path(str(args.controller_database) + suffix).exists()
                    for suffix in ("-wal", "-shm")
                )
            ):
                raise ValidationError(
                    "restore requires a private offline controller database without SQLite sidecars"
                )
            connection = sqlite3.connect(f"file:{args.controller_database}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT measured_target_bytes,s3_bytes,s3_objects,instance_port FROM managed_resources"
                ).fetchall()
                context.restore_ports.update(row[3] for row in rows if row[3] is not None)
                if args.catalog is not None:
                    other = json.loads(args.catalog.read_text())
                    if other.get("format") != FORMAT or any(
                        other.get(key) != config[key] for key in ("namespace", "projectId")
                    ):
                        raise ValidationError("restore catalog belongs to another deployment")
                    for entry in other["entries"]:
                        if entry["instanceId"] is not None:
                            if (
                                not isinstance(entry["port"], int)
                                or not 30000 <= entry["port"] <= 30999
                            ):
                                raise ValidationError("restore catalog port is invalid")
                            context.restore_ports.add(entry["port"])
                reservations = {
                    "databaseBytes": sum(hard_quota(row[0] or 0) for row in rows),
                    "garageBytes": sum(
                        garage_reservation(row[1] or 0, row[2] or 0) for row in rows
                    ),
                }
            finally:
                connection.close()
            selected = (
                None
                if args.resource is None
                else {uuid(value, field="resource ID") for value in args.resource}
            )

            restored_counts = {"shared": 0, "isolated": 0}
            found: set[str] = set()

            def restore(entry: dict[str, Any], path: Path) -> None:
                if entry["resources"] and (
                    selected is None
                    or any(row["resourceId"] in selected for row in entry["resources"])
                ):
                    found.update(row["resourceId"] for row in entry["resources"])
                    isolated = context.restore_entry(
                        entry,
                        path,
                        reservations=reservations,
                        controller_database=args.controller_database,
                    )
                    if isolated is not None:
                        restored_counts["isolated" if isolated else "shared"] += 1

            result = consume(
                sys.stdin.buffer,
                restore,
                config=config,
                manifest_observer=context.reserve_restore_ports,
            )
            if selected is not None and not selected <= found:
                raise ValidationError("selected resources are absent from this archive")
            result.update(restored_counts)
            print(
                f"database-restore=verified kind={result['type']} shared={result['shared']} isolated={result['isolated']}"
            )
        else:

            def verify(entry: dict[str, Any], path: Path) -> None:
                if args.restore_container is None:
                    return
                if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", args.restore_container):
                    raise ValidationError("restore test container name is invalid")
                if entry["type"] == "postgres":
                    cleaned = path.parent / "restore.sql"
                    filter_postgres(path, cleaned)
                    with cleaned.open("rb") as payload:
                        command(
                            [
                                "podman",
                                "exec",
                                "-i",
                                args.restore_container,
                                "psql",
                                "-X",
                                "-v",
                                "ON_ERROR_STOP=1",
                                "-U",
                                "platform_admin",
                                "-d",
                                "postgres",
                            ],
                            stdin=payload,
                            stdout=subprocess.DEVNULL,
                        )
                    for name in entry["databases"]:
                        command(
                            [
                                "podman",
                                "exec",
                                args.restore_container,
                                "psql",
                                "-At",
                                "-U",
                                "platform_admin",
                                "-d",
                                name,
                                "-c",
                                "SELECT 1",
                            ],
                            stdout=subprocess.DEVNULL,
                        )
                else:
                    with path.open("rb") as payload:
                        command(
                            [
                                "podman",
                                "exec",
                                "-i",
                                args.restore_container,
                                "mongorestore",
                                "--db",
                                entry["databases"][0],
                                "--restoreDbUsersAndRoles",
                                "--drop",
                                "--gzip",
                                "--archive",
                                "--numParallelCollections=1",
                                "--numInsertionWorkersPerCollection=1",
                            ],
                            stdin=payload,
                            stdout=subprocess.DEVNULL,
                        )

            result = consume(sys.stdin.buffer, verify, config=config)
            print(
                f"database-archive=verified kind={result['type']} shared={result['shared']} isolated={result['isolated']}"
            )
    except Exception:
        print("database backup/restore failed; encrypted set was not accepted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
