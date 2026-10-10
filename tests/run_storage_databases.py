"""Opt-in real PostgreSQL 17/MongoDB 8 check: uv run python tests/run_storage_databases.py.

Rootless Podman, idle loopback ports 5432/27017/30000/30001, and cached official
images are required. Containers and their anonymous volumes are removed in finally.
Only TLS transport, host lifecycle/quotas and Nomad/OpenStack are adapted locally;
all SQL, Mongo commands, native dump/restore tools and migration verification run
against real authenticated databases. CI's storage VM additionally tests TLS,
systemd, cgroups, XFS and nftables.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack, nullcontext
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from pymongo import MongoClient
from pymongo.errors import OperationFailure

from openstack_platform.database_backups import Native
from openstack_platform.database_backups import command as backup_command
from openstack_platform.helper import instance_migration, storage
from openstack_platform.helper.instances import connect_ready
from openstack_platform.helper.storage_limits import mongo_reconcile, resource_action
from openstack_platform.storage_instances import _COPY_CANCEL, http_handler, run
from openstack_platform.storage_migration import copy_instance
from tests.test_platform_storage import APP_ID, MemoryNomad, config_fixture

HOST = "127.0.0.1"
QUOTAS = {"sizeBytes": 2 * 1024**3, "memoryBytes": 1024**3, "connections": 10, "cpuMillicores": 500}
PODMAN = ("podman", "--cgroup-manager=cgroupfs")
PG_CONNECT = psycopg.connect


def pg_connect(**values):
    values.pop("sslrootcert", None)
    values["sslmode"] = "disable"
    return PG_CONNECT(**values)


def plain_uri(uri):
    parsed = urllib.parse.urlsplit(uri)
    query = [
        (k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if not k.lower().startswith("tls")
    ]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def mongo_connect(*args, **values):
    values = {k: v for k, v in values.items() if not k.lower().startswith("tls")}
    if args and args[0].startswith("mongodb://"):
        args = (plain_uri(args[0]), *args[1:])
    values.setdefault("serverSelectionTimeoutMS", 2000)
    values.setdefault("connectTimeoutMS", 2000)
    return MongoClient(*args, **values)


def mongo_app(*, uri):
    return mongo_connect(uri)


class LocalNative(Native):
    def pg_options(self, entry, password):
        return {**super().pg_options(entry, password), "sslmode": "disable"}

    def uri(self, entry, password):
        return plain_uri(super().uri(entry, password))


class LocalHost:
    """Host lifecycle boundary; copy and its HTTP cancellation are production code."""

    namespace = "storage-real"
    platform = {"internalNames": {"storage": HOST}}

    def __init__(self, root, containers):
        self.data = root
        self.configs = {}
        self.interrupt = False
        wrappers = root / "bin"
        wrappers.mkdir()
        for tool in ("pg_dump", "pg_restore", "psql", "mongodump", "mongorestore"):
            container = containers[0 if tool.startswith("pg_") or tool == "psql" else 2]
            program = wrappers / tool
            program.write_text(
                "#!/bin/sh\nexec "
                + shlex.join(
                    (
                        *PODMAN,
                        "exec",
                        "--user=0:0",
                        "--env",
                        "PGPASSWORD",
                        "--env",
                        "PGSSLMODE",
                        "--env",
                        "PGOPTIONS",
                        "--env",
                        "PGAPPNAME",
                        container,
                        tool,
                    )
                )
                + ' "$@"\n'
            )
            program.chmod(0o700)
        self.environment = {
            **os.environ,
            "PATH": str(wrappers) + ":" + os.environ["PATH"],
            "PGSSLMODE": "disable",
        }

    def directory(self, identifier):
        return self.data / identifier

    def locked(self):
        return nullcontext()

    def save(self, config):
        self.configs[config["instanceId"]] = config
        self.directory(config["instanceId"]).mkdir(exist_ok=True)
        (self.directory(config["instanceId"]) / "admin-password").write_text("target-password")

    def capacity(self, identifier, limits, **options):
        pass

    def apply(self, config, **options):
        pass

    def command(self, argv, **options):
        environment = {**options.pop("env", {}), **self.environment}
        if argv[0] in {"mongodump", "mongorestore"}:
            configuration = Path(argv[argv.index("--config") + 1])
            value = json.loads(configuration.read_text())
            value["uri"] = plain_uri(value["uri"])
            configuration.write_text(json.dumps(value))
        if "stdout" in options:
            return backup_command(argv, env=environment, **options)
        return run(argv, env=environment, **options)

    def dispatch(self, args, *, disconnected):
        action = args["action"]
        identifier = args["instanceId"]
        config = self.configs.get(identifier)
        if action == "create":
            if config is None:
                config = {
                    "instanceId": identifier,
                    "applicationId": APP_ID,
                    "type": args["type"],
                    "port": 30000 if args["type"] == "postgres" else 30001,
                    "migrationState": None,
                    "quotas": args["quotas"],
                    "acceptedQuotas": args["quotas"],
                    "reservedQuotas": args["quotas"],
                }
            self.save(config)
            return config
        if config is None:
            return {"absent": True}
        if action == "credentials":
            return {"port": config["port"], "adminPassword": "target-password"}
        if action == "copy":
            if self.interrupt:
                raise RuntimeError("injected interruption before copy")
            marker = _COPY_CANCEL.set(disconnected)
            try:
                copy_instance(self, config, args)
            finally:
                _COPY_CANCEL.reset(marker)
        elif action == "freeze":
            config["sourceRole"] = args["sourceRole"]
        elif action == "seal":
            assert config["migrationState"] == "verified"
            config["migrationState"] = "switched"
        elif action == "abort-copy":
            config["abortedOperationId"] = args["operationId"]
            config["migrationState"] = "aborted"
        elif action == "allow":
            config["allowIps"] = args["addresses"]
        return config


class Client:
    def __init__(self, host):
        self.server = ThreadingHTTPServer((HOST, 0), http_handler(host, "test-token"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def call(self, action, identifier, **values):
        request = urllib.request.Request(
            f"http://{HOST}:{self.server.server_port}/platform/instances",
            data=json.dumps({"action": action, "instanceId": identifier, **values}).encode(),
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=150) as response:
            return json.load(response)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def exercise(root, containers):
    host = LocalHost(root, containers)
    native = LocalNative(HOST, "unused-local-ca", execute=host.command)
    source_pg = connect_ready(
        lambda: pg_connect(
            host=HOST,
            port=5432,
            dbname="platform",
            user="platform_admin",
            password="source-password",
            autocommit=True,
        )
    )
    source_mongo = None
    for port, password in ((27017, "source-password"), (30001, "target-password")):
        anonymous = mongo_connect(HOST, port)
        connect_ready(lambda peer=anonymous: peer.admin.command("ping"))
        anonymous.admin.command(
            "createUser", "platform_admin", pwd=password, roles=[{"role": "root", "db": "admin"}]
        )
        anonymous.close()
        authenticated = mongo_connect(
            HOST, port, username="platform_admin", password=password, authSource="admin"
        )
        authenticated.admin.command("ping")
        if port == 27017:
            source_mongo = authenticated
        else:
            authenticated.close()
    assert source_mongo is not None
    print(
        "servers:",
        source_pg.execute("SHOW server_version").fetchone()[0],
        source_mongo.admin.command("buildInfo")["version"],
        flush=True,
    )
    storage._PORT_CONTEXT.set((5432, 27017))
    postgres = storage.postgres_create(
        source_pg,
        application_id=APP_ID,
        host=HOST,
        connections=10,
        measured_target_bytes=QUOTAS["sizeBytes"],
        generation="abcdef12",
        operation_id=str(uuid.uuid4()),
        password_factory=lambda: "app-'@:password",
    )
    mongo = storage.mongo_create(
        source_mongo,
        application_id=APP_ID,
        host=HOST,
        measured_target_bytes=QUOTAS["sizeBytes"],
        generation="abcdef12",
        operation_id=str(uuid.uuid4()),
        password_factory=lambda: "app-'@:password",
    )
    assert native.databases("mongo", 27017, "source-password") == [mongo.provider_name]
    empty = {"type": "mongo", "port": 27017, "databases": [mongo.provider_name]}
    native.dump(empty, "source-password", root / "empty.dump")
    native.restore(empty, "source-password", root / "empty.dump")
    storage.postgres_verify(pg_connect, postgres, host=HOST)
    storage.mongo_verify(mongo_app, mongo, host=HOST)
    name = postgres.provider_name
    with pg_connect(
        host=HOST,
        port=5432,
        dbname=name,
        user="platform_admin",
        password="source-password",
        autocommit=True,
    ) as admin:
        admin.execute("COMMENT ON EXTENSION plpgsql IS 'admin-owned comment'")
    with pg_connect(
        host=HOST,
        port=5432,
        dbname=name,
        user=postgres.credential_name,
        password=postgres.environment["PGPASSWORD"],
        autocommit=True,
    ) as app:
        # Model old resources whose tables were created as the login itself.
        app.execute("RESET ROLE")
        app.execute(
            "CREATE FUNCTION public.restore_guard() RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$ BEGIN IF session_user='platform_admin' THEN RAISE EXCEPTION 'admin restore session'; END IF; RETURN true; END $$"
        )
        app.execute("CREATE TABLE public.guarded(value integer CHECK(public.restore_guard()))")
        app.execute("INSERT INTO public.guarded VALUES (42)")
        app.execute("CREATE SCHEMA pgdata")
        app.execute("CREATE TABLE pgdata.items(value integer)")
        app.execute("INSERT INTO pgdata.items VALUES (84)")
    source_mongo[name].items.insert_one({"value": 42})
    source_mongo[name].items.insert_one({"value": 99})
    source_mongo[name].items.create_index("value")
    source_mongo[name].items.create_index("spare")
    source_mongo[name].deletable.insert_one({"value": 0})
    assert mongo_reconcile(source_mongo[name], mongo.credential_name, name, used=2, limit=1)
    postgres_new = storage.postgres_rotate(
        source_pg,
        application_id=APP_ID,
        host=HOST,
        connections=10,
        old_environment=postgres.environment,
        generation="1234abcd",
    )
    mongo_new = storage.mongo_rotate(
        source_mongo,
        application_id=APP_ID,
        host=HOST,
        old_environment=mongo.environment,
        generation="1234abcd",
        operation_id=str(uuid.uuid4()),
    )
    storage.postgres_retire(source_pg, postgres.credential_name)
    storage.mongo_retire(source_mongo, name, mongo.credential_name)
    postgres, mongo = postgres_new, mongo_new
    with pg_connect(
        host=HOST,
        port=5432,
        dbname=name,
        user=postgres.credential_name,
        password=postgres.environment["PGPASSWORD"],
    ) as app:
        assert [
            app.execute("SHOW " + setting).fetchone()[0]
            for setting in (
                "statement_timeout",
                "idle_in_transaction_session_timeout",
                "lock_timeout",
                "temp_file_limit",
            )
        ] == ["30s", "1min", "5s", "256MB"]
    storage.mongo_verify(mongo_app, mongo, host=HOST, write_blocked=True)
    with mongo_app(uri=mongo.environment["MONGODB_URI"]) as app:
        for denied in (
            lambda: app[name].items.insert_one({"value": 100}),
            lambda: app[name].items.update_one({"value": 42}, {"$set": {"value": 100}}),
            lambda: app[name].items.create_index("forbidden"),
            lambda: app[name].create_collection("forbidden"),
        ):
            try:
                denied()
            except OperationFailure as error:
                assert error.code == 13
            else:
                raise AssertionError("rotation lost Mongo write block")
        assert app[name].items.find_one()["value"] == 42
        assert app[name].items.delete_one({"value": 99}).deleted_count == 1
        app[name].items.drop_index("spare_1")
        app[name].deletable.drop()
        assert "value_1" in app[name].items.index_information()
        assert app[name].command("collStats", "items")["count"] == 1
    from openstack_platform.controller.storage_contract import canonicalize_environment

    records = {}
    for kind, credential, port in (("postgres", postgres, 5432), ("mongo", mongo, 27017)):
        record = {
            "name": "default",
            "providerName": name,
            "bindings": dict(canonicalize_environment(kind, "default", credential.environment)),
            "writeBlock": {"blocked": kind == "mongo"},
        }
        records[kind] = record
        entry = {"type": kind, "port": port, "databases": [name], "resources": [record]}
        native.dump(entry, "source-password", root / (kind + ".dump"))
        entry["port"] = 30000 if kind == "postgres" else 30001
        entry["quotas"] = {**QUOTAS, "connections": 20}
        native.restore(entry, "target-password", root / (kind + ".dump"))
    with pg_connect(
        host=HOST,
        port=30000,
        dbname=name,
        user=postgres.credential_name,
        password=postgres.environment["PGPASSWORD"],
        autocommit=True,
    ) as app:
        assert app.execute("SELECT value FROM guarded").fetchone()[0] == 42
        assert (
            app.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname=session_user").fetchone()[
                0
            ]
            == 20
        )
        app.execute("CREATE TABLE pgdata.stale(value integer)")
    target_mongo = mongo_connect(
        HOST, 30001, username="platform_admin", password="target-password", authSource="admin"
    )
    target_mongo[name].stale.insert_one({"value": 0})
    assert not mongo_reconcile(
        target_mongo[name], mongo.credential_name, name, used=0, limit=QUOTAS["sizeBytes"]
    )
    print(
        "native backup/restore, empty Mongo, ownership, blocked role and rotation: passed",
        flush=True,
    )
    source_pg.close()
    source_mongo.close()
    target_mongo.close()
    secrets = root / "secrets"
    secrets.mkdir()
    (secrets / "storage-bootstrap.env").write_text(
        "POSTGRES_PASSWORD=source-password\nMONGO_PASSWORD=source-password\n"
    )
    runtime = SimpleNamespace(root=root, platform=SimpleNamespace(get=lambda field: HOST))
    variables = MemoryNomad({**records["postgres"]["bindings"], **records["mongo"]["bindings"]})
    client = Client(host)
    original_read = Path.read_text

    def read(path, *args, **options):
        return (
            "source-password"
            if str(path)
            in {
                "/etc/storage-real/secrets/postgres-password",
                "/etc/storage-real/secrets/mongodb-password",
            }
            else original_read(path, *args, **options)
        )

    try:
        with ExitStack() as stack:
            stack.enter_context(mock.patch("psycopg.connect", side_effect=pg_connect))
            for module in (
                "openstack_platform.database_backups",
                "openstack_platform.helper.instance_migration",
                "openstack_platform.storage_migration",
            ):
                stack.enter_context(mock.patch(module + ".MongoClient", side_effect=mongo_connect))
            stack.enter_context(mock.patch.object(Path, "read_text", read))
            for kind, credential in (("postgres", postgres), ("mongo", mongo)):
                identifier = str(uuid.uuid4())
                args = {
                    "applicationId": APP_ID,
                    "applicationSlug": "local-app",
                    "resourceName": "default",
                    "type": kind,
                    "providerId": name,
                    "providerName": name,
                    "instanceId": identifier,
                    "quotas": QUOTAS,
                    "operationId": str(uuid.uuid4()),
                    "recover": False,
                    "copySeconds": 120,
                    "workerIds": [],
                    "retainedWorker": None,
                    "reservations": {"databaseBytes": 6 * 1024**3, "garageBytes": 0},
                }
                host.interrupt = True
                try:
                    instance_migration.migrate(
                        args, client=client, runtime=runtime, nomad=variables
                    )
                except urllib.error.HTTPError as error:
                    assert error.code == 503
                else:
                    raise AssertionError("injected copy failure was ignored")
                abort_args = {
                    key: args[key]
                    for key in (
                        "applicationId",
                        "applicationSlug",
                        "resourceName",
                        "type",
                        "providerId",
                        "providerName",
                        "instanceId",
                        "operationId",
                    )
                }
                abort_args.update(
                    mode="apply", sourceRole=host.configs[identifier].get("sourceRole", "readWrite")
                )
                assert instance_migration.abort(
                    abort_args, client=client, runtime=runtime, nomad=variables
                )["unfrozen"]
                if kind == "postgres":
                    with pg_connect(
                        host=HOST,
                        port=5432,
                        dbname=name,
                        user=postgres.credential_name,
                        password=postgres.environment["PGPASSWORD"],
                        autocommit=True,
                    ) as resumed:
                        resumed.execute("INSERT INTO public.guarded VALUES(43)")
                else:
                    with mongo_app(uri=mongo.environment["MONGODB_URI"]) as resumed:
                        # The originally blocked role is preserved by abort.
                        resumed[name].items.find_one()
                host.interrupt = False
                args["operationId"] = str(uuid.uuid4())
                result = instance_migration.migrate(
                    args, client=client, runtime=runtime, nomad=variables
                )
                assert result["published"] and result["verified"]
                assert instance_migration.migrate(
                    args, client=client, runtime=runtime, nomad=variables
                )["published"]
                assert host.configs[identifier]["migrationState"] == "switched"
                storage._PORT_CONTEXT.set((30000, 30001))
                admin = (
                    pg_connect(
                        host=HOST,
                        port=30000,
                        dbname="platform",
                        user="platform_admin",
                        password="target-password",
                        autocommit=True,
                    )
                    if kind == "postgres"
                    else mongo_connect(
                        HOST,
                        30001,
                        username="platform_admin",
                        password="target-password",
                        authSource="admin",
                    )
                )
                limit_args = {
                    "applicationId": APP_ID,
                    "applicationSlug": "local-app",
                    "resourceName": "default",
                    "providerId": name,
                    "providerName": name,
                    "operationId": str(uuid.uuid4()),
                    "recover": False,
                    "quotas": {**QUOTAS, "connections": 30},
                }
                result = resource_action(
                    limit_args,
                    resource_type=kind,
                    mutate=True,
                    admin=admin,
                    nomad=variables,
                    host=HOST,
                    endpoint="unused",
                )
                assert result["applied"] and result["usage"]["usedBytes"] > 0
                if kind == "postgres":
                    assert (
                        admin.execute(
                            "SELECT datconnlimit FROM pg_database WHERE datname=%s", (name,)
                        ).fetchone()[0]
                        == 30
                    )
                    assert (
                        admin.execute(
                            "SELECT rolconnlimit FROM pg_roles WHERE rolname=%s",
                            (credential.credential_name,),
                        ).fetchone()[0]
                        == 30
                    )
                else:
                    assert "stale" not in admin[name].list_collection_names()
                    assert admin[name].items.find_one()["value"] == 42
                admin.close()
            # Run the actual collector and privileged repair service over the
            # same real provider boundary; SQLite and journal state are real.
            from openstack_platform.controller import database as db
            from openstack_platform.controller.storage_limits import StorageLimitsService

            connection = db.connect(root / "controller.sqlite3")
            db.migrate(connection)
            config = config_fixture(root)
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="local-app",
                worker_flavor="small",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=1024,
            )
            for kind, instance in ((item["type"], item) for item in host.configs.values()):
                row = db.put_managed_resource(
                    connection,
                    resource_id=instance["instanceId"],
                    application_id=APP_ID,
                    resource_type=kind,
                    provider_id=name,
                    provider_name=name,
                    lifecycle_state="active",
                    measured_target_bytes=QUOTAS["sizeBytes"],
                    postgres_connections=10 if kind == "postgres" else None,
                    mongo_connections=10 if kind == "mongo" else None,
                )
                db.set_storage_instance(
                    connection,
                    row.resource_id,
                    row.resource_id,
                    instance["port"],
                    migration_state="switched",
                )

            def helper(_config, action, args, **bounds):
                if action == "storage.host.observe":
                    raise RuntimeError("host metrics are outside this local database check")
                kind = action.split(".")[1]
                # Production's helper establishes these per request, including
                # on the collector's independent worker threads.
                storage._PORT_CONTEXT.set((30000, 30001))
                admin = (
                    pg_connect(
                        host=HOST,
                        port=30000,
                        dbname="platform",
                        user="platform_admin",
                        password="target-password",
                        autocommit=True,
                    )
                    if kind == "postgres"
                    else mongo_connect(
                        HOST,
                        30001,
                        username="platform_admin",
                        password="target-password",
                        authSource="admin",
                    )
                )
                allowed = {
                    "applicationId",
                    "applicationSlug",
                    "resourceName",
                    "providerId",
                    "providerName",
                    "quotas",
                    "operationId",
                    "recover",
                }
                try:
                    return resource_action(
                        {key: value for key, value in args.items() if key in allowed},
                        resource_type=kind,
                        mutate=action.endswith(".limits"),
                        admin=admin,
                        nomad=variables,
                        host=HOST,
                        endpoint="unused",
                    )
                finally:
                    admin.close()

            service = StorageLimitsService(
                connection, config, root / "controller", helper_caller=helper
            )
            storage._PORT_CONTEXT.set((30000, 30001))
            service.collect()
            for row in db.list_managed_resources(connection):
                service.collect(_resource_id=row.resource_id)
            for row in db.list_managed_resources(connection):
                assert row.usage_error is None
                assert row.usage["usedBytes"] > 0
            request_id = str(uuid.uuid4())
            service.repair_postgres(request_id=request_id)
            service.repair_postgres(request_id=str(uuid.uuid4()))
            assert db.get_operation(connection, request_id).status == "succeeded"
            with pg_connect(
                host=HOST,
                port=30000,
                dbname=name,
                user=postgres.credential_name,
                password=postgres.environment["PGPASSWORD"],
            ) as app:
                assert app.execute("SHOW statement_timeout").fetchone()[0] == "30s"
                assert (
                    app.execute(
                        "SELECT rolconnlimit FROM pg_roles WHERE rolname=session_user"
                    ).fetchone()[0]
                    == 10
                )
                assert app.execute("SELECT current_user").fetchone()[0] == "o_" + name[2:]
            with ExitStack() as sessions:
                for _ in range(10):
                    sessions.enter_context(
                        pg_connect(
                            host=HOST,
                            port=30000,
                            dbname=name,
                            user=postgres.credential_name,
                            password=postgres.environment["PGPASSWORD"],
                        )
                    )
                try:
                    extra = pg_connect(
                        host=HOST,
                        port=30000,
                        dbname=name,
                        user=postgres.credential_name,
                        password=postgres.environment["PGPASSWORD"],
                    )
                except psycopg.OperationalError:
                    pass
                else:
                    extra.close()
                    raise AssertionError("PostgreSQL app exceeded its connection cap")
            connection.close()
            cleanup = mongo_connect(
                HOST,
                30001,
                username="platform_admin",
                password="target-password",
                authSource="admin",
            )
            try:
                storage.mongo_remove(
                    cleanup, application_id=APP_ID, credential_name=mongo.credential_name
                )
                assert storage.mongo_absent(cleanup, application_id=APP_ID)
            finally:
                cleanup.close()
        print(
            "helper migration/replay, limits, controller collector/repair and deletion: passed",
            flush=True,
        )
    finally:
        client.close()


def main():
    for port in (5432, 27017, 30000, 30001):
        with socket.socket() as check:
            check.bind((HOST, port))
    containers = [
        f"storage-check-{os.getpid()}-{label}"
        for label in ("pg-source", "pg-target", "mongo-source", "mongo-target")
    ]
    with tempfile.TemporaryDirectory(prefix="storage-real-", dir="/dev/shm") as temporary:
        root = Path(temporary)
        try:
            for index, container in enumerate(containers):
                command = [
                    *PODMAN,
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    container,
                    "--network=host",
                    "--volume",
                    str(root) + ":" + str(root),
                ]
                if index < 2:
                    password = "source-password" if index == 0 else "target-password"
                    command += [
                        "-e",
                        "POSTGRES_USER=platform_admin",
                        "-e",
                        "POSTGRES_PASSWORD=" + password,
                        "-e",
                        "POSTGRES_DB=platform",
                        "docker.io/library/postgres:17-alpine",
                        "postgres",
                        "-c",
                        "port=" + str(5432 if index == 0 else 30000),
                    ]
                else:
                    command += [
                        "--user=999:999",
                        "--entrypoint=mongod",
                        "docker.io/library/mongo:8.0",
                        "--auth",
                        "--bind_ip",
                        HOST,
                        "--port",
                        str(27017 if index == 2 else 30001),
                        "--wiredTigerCacheSizeGB",
                        "0.25",
                        "--maxConns",
                        "100",
                    ]
                subprocess.run(
                    command, check=True, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL
                )
            exercise(root, containers)
        finally:
            for container in containers:
                subprocess.run(
                    (*PODMAN, "rm", "-f", "-v", container),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
