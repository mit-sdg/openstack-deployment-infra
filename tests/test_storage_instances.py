from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform.controller import database as db
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.http import HttpError
from openstack_platform.controller.storage_capacity import check
from openstack_platform.controller.storage_instances import InstanceMigrationService
from openstack_platform.helper.nomad import SecretItems, VariableSnapshot, WorkloadVariables
from openstack_platform.instance_contract import GIB, MIB, CapacityError
from openstack_platform.storage_instances import Manager, database_command, http_handler
from openstack_platform.storage_migration import mongo_fingerprint, postgres_fingerprint
from openstack_platform.validation import ValidationError
from tests.product_fixtures import accept_deployment
from tests.test_platform_storage import APP_ID, config_fixture


def quotas(size=2 * GIB, memory=512 * MIB, connections=10, cpu=500):
    return {
        "sizeBytes": size,
        "memoryBytes": memory,
        "connections": connections,
        "cpuMillicores": cpu,
    }


class InstanceManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.break_assignment = False

        def command(argv, **kwargs):
            self.calls.append((tuple(argv), kwargs))
            if self.break_assignment and argv[:2] == ("systemctl", "set-property"):
                raise RuntimeError("interrupted cgroup assignment")
            if argv[0] == "rm":
                shutil.rmtree(argv[-1])
            return subprocess.CompletedProcess(argv, 0, b"0\n" if argv[0] == "du" else b"", b"")

        self.manager = Manager(
            {
                "namespace": "example",
                "paths": {"data": str(self.root)},
                "addresses": {"admin": "10.0.0.2", "storage": "10.0.0.4"},
                "internalNames": {"storage": "storage.example.internal"},
                "containers": {
                    "postgres": "postgres@sha256:" + "a" * 64,
                    "mongodb": "mongo@sha256:" + "b" * 64,
                },
            },
            command=command,
            units=self.root / "units",
            memory_budget=50 * GIB,
        )
        self.geometry = mock.patch(
            "openstack_platform.storage_instances.os.statvfs",
            return_value=SimpleNamespace(f_blocks=1024**2, f_frsize=1024**2),
        )
        self.geometry.start()
        self.addCleanup(self.geometry.stop)

    def create(self, kind="postgres", limits=None):
        identifier = str(uuid.uuid4())
        result = self.manager.dispatch(
            {
                "action": "create",
                "instanceId": identifier,
                "applicationId": APP_ID,
                "type": kind,
                "quotas": limits or quotas(),
                "allowIps": ["10.0.0.8"],
                "reservations": {"databaseBytes": 5 * GIB, "garageBytes": 0},
            }
        )
        return identifier, result

    def test_lifecycle_applies_cgroups_xfs_network_and_preserves_data_until_delete(self):
        pg, p = self.create()
        mongo, m = self.create("mongo")
        self.assertNotEqual(p["port"], m["port"])
        self.assertNotEqual(
            self.manager.read(pg)["projectId"], self.manager.read(mongo)["projectId"]
        )
        assignment = next(
            argv for argv, _ in self.calls if argv[:2] == ("systemctl", "set-property")
        )
        self.assertTrue(
            {
                "MemoryMax=536870912",
                "MemorySwapMax=0",
                "CPUQuota=50%",
                "CPUWeight=100",
                "IOWeight=100",
                "TasksMax=256",
            }
            <= set(assignment)
        )
        quota_calls = [
            argv[3] for argv, _ in self.calls if argv[0] == "xfs_quota" and "ihard=" in argv[3]
        ]
        self.assertTrue(
            any(
                "bhard=3145728k" in statement and "ihard=65536" in statement
                for statement in quota_calls
            )
        )
        firewall = next(
            values["input"].decode() for argv, values in self.calls if argv == ("nft", "-f", "-")
        )
        self.assertIn("10.0.0.8", firewall)
        self.assertIn(f"tcp dport {{ {p['port']} }} drop", firewall)
        self.assertNotIn("30000-60000", firewall)
        self.assertNotIn("established", firewall)
        pg_command = database_command(self.manager.read(pg), "example")
        self.assertTrue(
            {
                "max_connections=15",
                "superuser_reserved_connections=5",
                "shared_buffers=128MB",
                "maintenance_work_mem=32MB",
                "autovacuum_max_workers=1",
            }
            <= set(pg_command)
        )
        mongo_command = database_command(self.manager.read(mongo), "example")
        self.assertEqual(mongo_command[mongo_command.index("--maxConns") + 1], "20")
        self.assertEqual(mongo_command[mongo_command.index("--wiredTigerCacheSizeGB") + 1], "0.25")
        self.manager.dispatch({"action": "stop", "instanceId": mongo})
        self.assertTrue((self.manager.directory(mongo) / "data").exists())
        self.calls.clear()
        self.manager.restore()
        self.assertFalse(
            any(
                argv[:2] == ("systemctl", "start") and argv[-1] == self.manager.unit(mongo)
                for argv, _ in self.calls
            )
        )
        self.manager.dispatch(
            {
                "action": "limits",
                "instanceId": pg,
                "quotas": quotas(memory=GIB, connections=20, cpu=1000),
                "reservations": {"databaseBytes": 5 * GIB, "garageBytes": 0},
            }
        )
        self.assertTrue(
            any(
                argv[:2] == ("systemctl", "restart") and argv[-1] == self.manager.unit(pg)
                for argv, _ in self.calls
            )
        )
        with self.assertRaises(ValidationError):
            self.manager.dispatch({"action": "remove", "instanceId": pg, "deleteData": False})
        self.manager.dispatch({"action": "remove", "instanceId": pg, "deleteData": True})
        self.assertFalse(self.manager.directory(pg).exists())
        self.assertTrue(self.manager.directory(mongo).exists())
        self.assertTrue(
            self.manager.dispatch({"action": "remove", "instanceId": pg, "deleteData": True})[
                "confirmedAbsent"
            ]
        )

    def test_mongo_bootstrap_uses_allocated_port_and_restart_inherits_unit_caps(self):
        identifier, result = self.create("mongo")
        config = self.manager.read(identifier)
        self.assertTrue(config["adminInitialized"])
        bootstrap = next((argv, kw) for argv, kw in self.calls if argv[:2] == ("podman", "exec"))
        self.assertEqual(bootstrap[0][bootstrap[0].index("--user") + 1], "0:0")
        self.assertIn("HOME=/tmp", bootstrap[0])
        self.assertIn("--norc", bootstrap[0])
        self.assertEqual(bootstrap[0][bootstrap[0].index("--port") + 1], str(result["port"]))
        password = (self.manager.directory(identifier) / "admin-password").read_text()
        self.assertNotIn(password, " ".join(bootstrap[0]))
        self.assertIn(password.encode(), bootstrap[1]["input"])
        with mock.patch(
            "openstack_platform.storage_instances.os.execvp", side_effect=SystemExit
        ) as execute:
            with self.assertRaises(SystemExit):
                self.manager.launch(identifier)
        argv = execute.call_args.args[1]
        self.assertIn("--cgroups=disabled", argv)
        self.assertEqual(argv[argv.index("--entrypoint") + 1], "mongod")
        self.assertIn("--tmpfs=/var/run/postgresql:rw,size=16m,mode=1777,nosuid,nodev,noexec", argv)
        self.assertFalse(
            any(value.startswith(("MONGO_INITDB", "--memory", "--cgroup-parent")) for value in argv)
        )
        self.calls.clear()
        self.manager.dispatch(
            {
                "action": "create",
                "instanceId": identifier,
                "applicationId": APP_ID,
                "type": "mongo",
                "quotas": quotas(),
                "allowIps": [],
                "reservations": {"databaseBytes": 5 * GIB, "garageBytes": 0},
            }
        )
        self.assertFalse(any(argv[:2] == ("podman", "exec") for argv, _ in self.calls))
        self.assertFalse(
            any(argv[0] == "xfs_quota" and "project -s" in argv[3] for argv, _ in self.calls)
        )

    def test_interrupted_data_removal_keeps_port_project_reserved_until_replay(self):
        identifier, original = self.create()
        actual = self.manager.command
        failed = False

        def interrupted(argv, **kwargs):
            nonlocal failed
            if argv[0] == "rm" and not failed:
                failed = True
                Path(argv[-1], "config.json").unlink()
                raise RuntimeError("recursive deletion interrupted after config removal")
            return actual(argv, **kwargs)

        self.manager.command = interrupted
        with self.assertRaises(RuntimeError):
            self.manager.dispatch(
                {"action": "remove", "instanceId": identifier, "deleteData": True}
            )
        self.assertTrue(self.manager.deleting_record(identifier).exists())
        _, following = self.create()
        self.assertNotEqual(original["port"], following["port"])
        self.assertTrue(
            self.manager.dispatch(
                {"action": "remove", "instanceId": identifier, "deleteData": True}
            )["confirmedAbsent"]
        )
        self.assertFalse(self.manager.deleting_record(identifier).exists())
        _, reused = self.create()
        self.assertEqual(reused["port"], original["port"])

    def test_quota_raise_applies_even_when_instance_is_crash_looping(self):
        identifier, _ = self.create("mongo")
        self.calls.clear()
        self.manager.dispatch(
            {
                "action": "limits",
                "instanceId": identifier,
                "quotas": quotas(size=3 * GIB),
                "reservations": {"databaseBytes": 6 * GIB, "garageBytes": 0},
            }
        )
        self.assertTrue(
            any(argv[0] == "xfs_quota" and "bhard=4718592k" in argv[3] for argv, _ in self.calls)
        )
        self.assertTrue(any(argv[:2] == ("systemctl", "start") for argv, _ in self.calls))
        self.assertEqual(self.manager.read(identifier)["acceptedQuotas"]["sizeBytes"], 3 * GIB)

    def test_rejected_quota_reduction_does_not_stop_the_instance(self):
        identifier, _ = self.create()
        original = self.manager.command

        def command(argv, **options):
            if argv[0] == "du":
                return subprocess.CompletedProcess(argv, 0, str(2 * GIB).encode(), b"")
            return original(argv, **options)

        self.manager.command = command
        self.calls.clear()
        with self.assertRaises(CapacityError) as denied:
            self.manager.dispatch(
                {
                    "action": "limits",
                    "instanceId": identifier,
                    "quotas": quotas(size=GIB),
                    "reservations": {"databaseBytes": 3 * GIB, "garageBytes": 0},
                }
            )
        self.assertEqual(denied.exception.code, "SIZE_BELOW_USAGE")
        self.assertFalse(any(argv[0] == "systemctl" for argv, _ in self.calls))
        self.assertEqual(self.manager.read(identifier)["quotas"], quotas())

    def test_dns_urls_and_saved_jobs_keep_certificate_name_and_exact_identity(self):
        import hashlib
        import urllib.parse

        from openstack_platform.controller.application_models import Manifest
        from openstack_platform.controller.nomad_jobs import (
            nomad_candidate_identity,
            render_nomad_job,
            storage_hosts_job,
        )
        from openstack_platform.helper import storage

        host = "storage.example.internal"
        for port in (5432, 30005):
            marker = storage._PORT_CONTEXT.set((port, 27017 if port == 5432 else 30006))
            try:
                pg = storage.postgres_environment(
                    host, "p_" + "a" * 20, "u_" + "a" * 20 + "_abcdef12", "secret"
                )
                mongo = storage.mongo_environment(
                    host, "p_" + "a" * 20, "u_" + "a" * 20 + "_abcdef12", "secret"
                )
                self.assertEqual(urllib.parse.urlsplit(pg["DATABASE_URL"]).hostname, host)
                self.assertEqual(urllib.parse.urlsplit(mongo["MONGODB_URI"]).hostname, host)
                self.assertEqual(pg["PGHOST"], host)
            finally:
                storage._PORT_CONTEXT.reset(marker)
        (self.root / "dns").mkdir()
        config = config_fixture(self.root / "dns")
        job = render_nomad_job(
            application_id=APP_ID,
            application_slug="demo-app",
            image="registry/app@sha256:" + "b" * 64,
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=config.platform,
            cpu_mhz=1000,
            memory_mib=1024,
            source_commit="a" * 40,
            recipe_hash="c" * 64,
        )
        self.assertIn("storage.example.internal:192.0.2.13", job)
        original_identity = nomad_candidate_identity(job)[0]
        marker_start = job.index("  meta {")
        marker_end = job.index("  }\n\n", marker_start) + len("  }\n\n")
        metadata = job[marker_start:marker_end]
        raw = job[:marker_start] + job[marker_end:]
        raw = "\n".join(line for line in raw.split("\n") if "extra_hosts" not in line)
        metadata = metadata.replace(original_identity, hashlib.sha256(raw.encode()).hexdigest())
        old_job = raw[:marker_start] + metadata + raw[marker_start:]
        upgraded = storage_hosts_job(old_job, config.platform)
        self.assertEqual(nomad_candidate_identity(upgraded), nomad_candidate_identity(job))
        self.assertEqual(storage_hosts_job(upgraded, config.platform), upgraded)

    def test_pending_assignments_keep_capacity_reserved_until_confirmed(self):
        for _ in range(6):
            self.create(limits=quotas(memory=8 * GIB))
        identifier, _ = self.create(limits=quotas(memory=GIB))
        self.break_assignment = True
        with self.assertRaises(RuntimeError):
            self.manager.dispatch(
                {
                    "action": "limits",
                    "instanceId": identifier,
                    "quotas": quotas(memory=2 * GIB),
                    "reservations": {"databaseBytes": 20 * GIB, "garageBytes": 0},
                }
            )
        with self.assertRaises(RuntimeError):
            self.manager.dispatch(
                {
                    "action": "limits",
                    "instanceId": identifier,
                    "quotas": quotas(),
                    "reservations": {"databaseBytes": 20 * GIB, "garageBytes": 0},
                }
            )
        with self.assertRaises(CapacityError) as denied:
            self.create()
        self.assertEqual(denied.exception.code, "MEMORY_BUDGET_EXCEEDED")
        self.break_assignment = False
        self.manager.dispatch(
            {
                "action": "limits",
                "instanceId": identifier,
                "quotas": quotas(),
                "reservations": {"databaseBytes": 20 * GIB, "garageBytes": 0},
            }
        )
        self.create()

    def test_credentials_accept_only_the_allocated_instance_port(self):
        from openstack_platform.helper import storage

        marker = storage._PORT_CONTEXT.set((30000, 30001))
        try:
            database = "p_11111111111141118111"
            user = "u_11111111111141118111_abcdef12"
            environment = storage.postgres_environment("storage", database, user, "password")
            credential = storage.ProviderCredential(database, database, user, environment)
            storage._require_postgres_identity(credential, application_id=APP_ID, host="storage")
            self.assertEqual(environment["PGPORT"], "30000")
            legacy = storage._PORT_CONTEXT.set((5432, 27017))
            try:
                with self.assertRaises(storage.HelperActionError):
                    storage._require_postgres_identity(
                        credential, application_id=APP_ID, host="storage"
                    )
            finally:
                storage._PORT_CONTEXT.reset(legacy)
        finally:
            storage._PORT_CONTEXT.reset(marker)

    def test_instance_http_rejections_keep_safe_capacity_codes(self):
        from openstack_platform.helper.instances import InstanceClient
        from openstack_platform.helper.main import HelperActionError
        from openstack_platform.runtime import HttpStatusFailure

        client = InstanceClient("https://storage:3903", "secret-token", mock.Mock())
        with mock.patch(
            "openstack_platform.helper.instances.bounded_http",
            side_effect=HttpStatusFailure(
                400,
                b'{"error":{"code":"MEMORY_BUDGET_EXCEEDED","summary":"secret-provider-detail"}}',
            ),
        ):
            with self.assertRaises(HelperActionError) as raised:
                client.call("create", str(uuid.uuid4()))
        self.assertEqual(raised.exception.code, "MEMORY_BUDGET_EXCEEDED")
        self.assertNotIn("secret", str(raised.exception))

    def test_authenticated_channel_rejects_before_parsing_or_mutating(self):
        request = mock.Mock(
            path="/platform/instances",
            headers={"Authorization": "Bearer wrong", "Content-Length": "999999"},
        )
        with mock.patch.object(self.manager, "dispatch") as dispatch:
            http_handler(self.manager, "admin-token").do_POST(request)
        request.send_error.assert_called_once_with(403)
        request.rfile.read.assert_not_called()
        dispatch.assert_not_called()

    def test_http_errors_log_safe_codes_without_commands_or_credentials(self):
        for error, expected_status, code in (
            (
                CapacityError("MEMORY_BUDGET_EXCEEDED", "private-provider-detail"),
                400,
                "MEMORY_BUDGET_EXCEEDED",
            ),
            (
                subprocess.CalledProcessError(
                    125,
                    ["podman", "private-provider-detail"],
                    stderr=b"read-only file system: private-provider-detail",
                ),
                503,
                "INSTANCE_OPERATION_FAILED",
            ),
        ):
            with self.subTest(code=code):
                payload = json.dumps(
                    {"action": "create", "secret": "private-provider-detail"}
                ).encode()
                request = mock.Mock(
                    path="/platform/instances",
                    headers={
                        "Authorization": "Bearer admin-token",
                        "Content-Length": str(len(payload)),
                    },
                    rfile=io.BytesIO(payload),
                    wfile=io.BytesIO(),
                )
                with (
                    mock.patch.object(self.manager, "dispatch", side_effect=error),
                    self.assertLogs(level="WARNING") as logs,
                ):
                    http_handler(self.manager, "admin-token").do_POST(request)
                request.send_response.assert_called_once_with(expected_status)
                self.assertEqual(json.loads(request.wfile.getvalue())["error"]["code"], code)
                self.assertIn(code, " ".join(logs.output))
                self.assertNotIn("private-provider-detail", " ".join(logs.output))
                if expected_status == 503:
                    self.assertIn("read_only_filesystem", " ".join(logs.output))

    def test_copy_imports_app_sql_with_app_authentication_and_extensions_with_admin(self):
        identifier, _ = self.create()
        original_command = self.manager.command
        calls = []

        def command(argv, **options):
            if argv[0] == "pg_dump":
                Path(argv[argv.index("--file") + 1]).write_bytes(b"native archive")
            if argv[:2] == ("pg_restore", "--list"):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    b"1; 0 0 SCHEMA - public owner\n2; 0 0 EXTENSION - plpgsql platform_admin\n3; 0 0 TABLE public probe owner\n",
                    b"",
                )
            if argv[0] == "pg_restore":
                calls.append((list(argv), options))
            return original_command(argv, **options)

        self.manager.command = command

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, query, *args):
                text = query if isinstance(query, str) else query.as_string()
                self.values = [("public",)] if "pg_namespace" in text else []
                return self

            def fetchall(self):
                return self.values

        original_read = Path.read_text

        def read(path, *args, **options):
            return (
                "shared-private"
                if str(path) == "/etc/example/secrets/postgres-password"
                else original_read(path, *args, **options)
            )

        login = "u_11111111111141118111_abcdef12"
        with (
            mock.patch(
                "openstack_platform.storage_migration.psycopg.connect",
                side_effect=lambda **kwargs: Connection(),
            ),
            mock.patch(
                "openstack_platform.storage_migration.postgres_fingerprint",
                return_value={"probe": (1, "checksum")},
            ),
            mock.patch.object(Path, "read_text", read),
        ):
            self.manager.dispatch(
                {
                    "action": "copy",
                    "instanceId": identifier,
                    "database": "p_11111111111141118111",
                    "seconds": 120,
                    "operationId": str(uuid.uuid4()),
                    "applicationLogin": login,
                    "applicationPassword": "app-private",
                }
            )
        self.assertEqual(self.manager.read(identifier)["migrationState"], "verified")
        self.assertEqual(len(calls), 5)
        for argv, options in calls:
            extension = Path(argv[argv.index("--use-list") + 1]).name == "extensions.list"
            self.assertEqual(
                argv[argv.index("--username") + 1], "platform_admin" if extension else login
            )
            self.assertEqual(
                options["env"]["PGPASSWORD"],
                (self.manager.directory(identifier) / "admin-password").read_text()
                if extension
                else "app-private",
            )
            self.assertIn("--no-comments", argv)
            self.assertNotIn("app-private", " ".join(argv))
            self.assertNotIn("shared-private", " ".join(argv))
            if not extension:
                for setting in (
                    "statement_timeout=0",
                    "lock_timeout=0",
                    "idle_in_transaction_session_timeout=0",
                ):
                    self.assertIn(setting, options["env"]["PGOPTIONS"])

    def test_failed_postgres_import_restores_the_login_temp_limit(self):
        from openstack_platform.storage_migration import postgres_import_settings

        admin = mock.MagicMock()
        admin.__enter__.return_value = admin
        with mock.patch("openstack_platform.storage_migration.psycopg.connect", return_value=admin):
            with self.assertRaisesRegex(RuntimeError, "import failed"):
                with postgres_import_settings({}, "app_login"):
                    raise RuntimeError("import failed")
        statements = [call.args[0].as_string() for call in admin.execute.call_args_list]
        self.assertEqual(
            statements,
            [
                "ALTER ROLE \"app_login\" SET temp_file_limit = '-1'",
                "ALTER ROLE \"app_login\" SET temp_file_limit = '256MB'",
            ],
        )

    def test_sealed_migration_cannot_recopy_or_delete_source_data(self):
        identifier, _ = self.create()
        config = self.manager.read(identifier)
        config["migrationState"] = "verified"
        self.manager.save(config)
        self.manager.dispatch({"action": "seal", "instanceId": identifier})
        self.calls.clear()
        self.manager.dispatch(
            {
                "action": "copy",
                "instanceId": identifier,
                "database": "p_11111111111141118111",
                "seconds": 1800,
                "applicationLogin": "u_11111111111141118111_abcdef12",
                "applicationPassword": "password",
                "operationId": str(uuid.uuid4()),
            }
        )
        self.assertFalse(any(argv[0] in {"pg_dump", "pg_restore", "rm"} for argv, _ in self.calls))
        self.assertEqual(self.manager.read(identifier)["migrationState"], "switched")


class InstanceControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config_fixture(self.root)
        self.connection = db.connect(self.root / "platform.sqlite3")
        self.addCleanup(self.connection.close)
        db.migrate(self.connection)
        db.put_storage_host_usage(
            self.connection,
            {"dataVolume": {"totalBytes": 1024 * GIB}, "instanceMemoryBudgetBytes": 50 * GIB},
        )

    def add(self, identifier, kind, name="default", connections=10):
        if db.get_application(self.connection, identifier) is None:
            db.put_application(
                self.connection,
                application_id=identifier,
                application_slug="app-" + identifier[:8],
                worker_flavor="small",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=1024,
            )
        return db.put_managed_resource(
            self.connection,
            application_id=identifier,
            resource_type=kind,
            resource_name=name,
            provider_id="p_" + identifier.replace("-", "")[:20],
            provider_name="p_" + identifier.replace("-", "")[:20],
            lifecycle_state="active",
            postgres_connections=connections if kind == "postgres" else None,
            mongo_connections=connections if kind == "mongo" else None,
            measured_target_bytes=2 * GIB,
        )

    def test_fifty_default_pairs_fit_and_pending_raises_cannot_overcommit(self):
        resources = []
        for _ in range(50):
            identifier = str(uuid.uuid4())
            resources.extend([self.add(identifier, "postgres"), self.add(identifier, "mongo")])
        with self.assertRaises(CapacityError) as memory:
            check(self.connection, resources[0].resource_id, quotas(memory=GIB))
        self.assertEqual(memory.exception.code, "MEMORY_BUDGET_EXCEEDED")
        api = ControllerAPI(
            self.connection,
            self.config,
            self.root,
            helper_caller=mock.Mock(
                side_effect=AssertionError("admission must precede provider calls")
            ),
        )
        try:
            with self.assertRaises(HttpError) as denied:
                api.router("project").dispatch(
                    "PUT",
                    f"/v1/storage/{resources[0].resource_id}/limits",
                    {"Idempotency-Key": str(uuid.uuid4())},
                    {"quotas": quotas(memory=GIB), "expectedQuotas": quotas()},
                )
            self.assertEqual(
                (denied.exception.status, denied.exception.code), (400, "MEMORY_BUDGET_EXCEEDED")
            )
        finally:
            api.close()

        for resource in resources[:22:2]:
            with db.transaction(self.connection):
                self.connection.execute(
                    "UPDATE managed_resources SET postgres_connections=100 WHERE resource_id=?",
                    (resource.resource_id,),
                )
        key = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=key,
            kind="storage.limits.set",
            scope="app-" + resources[22].application_id,
            phase="applying",
            deadline_at=db.utc_now(),
            refs={"resource_id": resources[22].resource_id, "quotas": quotas(connections=20)},
        )
        with self.assertRaises(CapacityError) as connections:
            check(self.connection, resources[24].resource_id, quotas(connections=11))
        self.assertEqual(connections.exception.code, "CONNECTION_BUDGET_EXCEEDED")
        with self.assertRaises(CapacityError) as disk:
            check(self.connection, resources[24].resource_id, quotas(size=500 * GIB))
        self.assertEqual(disk.exception.code, "DISK_BUDGET_EXCEEDED")

    def test_first_limits_edit_stops_before_binding_update_and_replays_one_redeploy(self):
        from dataclasses import replace

        from openstack_platform.controller.application_models import Manifest
        from openstack_platform.controller.nomad_jobs import render_nomad_job, storage_hosts_job
        from openstack_platform.controller.storage_limits import StorageLimitsService

        resource = self.add(APP_ID, "mongo")
        application = db.get_application(self.connection, APP_ID)
        old_platform = replace(
            self.config.platform,
            document={
                **self.config.platform.document,
                "internalNames": {"storage": "legacy.example.internal"},
            },
        )
        job = render_nomad_job(
            application_id=APP_ID,
            application_slug=application.slug,
            image="registry/app@sha256:" + "b" * 64,
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=old_platform,
            cpu_mhz=1000,
            memory_mib=1024,
            source_commit="a" * 40,
            recipe_hash="c" * 64,
        )
        accept_deployment(
            self.connection,
            application_id=APP_ID,
            source_commit="a" * 40,
            recipe_hash="c" * 64,
            image_digest="registry/app@sha256:" + "b" * 64,
            nomad_job=job,
            nomad_version=1,
            build_log_path="logs/build.log",
        )
        db.set_application_runtime(
            self.connection,
            APP_ID,
            running=True,
            worker_server_id=str(uuid.uuid4()),
            worker_server_name="worker",
            worker_port_id=str(uuid.uuid4()),
            worker_port_name="worker-v4",
            nomad_version=1,
        )
        calls = []
        interrupted = True

        def helper(config, action, args, **bounds):
            calls.append(action)
            if action == "app.stop":
                return {"jobStopped": True}
            if action == "storage.mongo.limits":
                self.assertEqual(calls[-2], "app.stop")
                return {
                    "applied": True,
                    "writeBlocked": False,
                    "usage": {
                        "usedBytes": 0,
                        "objectCount": None,
                        "currentConnections": None,
                        "instanceMemoryBytes": None,
                        "cpuTimeMilliseconds": None,
                        "measuredAt": db.utc_now(),
                    },
                }
            if action == "app.deploy":
                self.assertEqual(args["job"], storage_hosts_job(job, self.config.platform))
                if interrupted:
                    raise RuntimeError("lost deploy response")
                return {"nomadVersion": 2}
            raise AssertionError(action)

        service = StorageLimitsService(
            self.connection, self.config, self.root, helper_caller=helper
        )
        key = str(uuid.uuid4())
        with self.assertRaises(RuntimeError):
            service.select(resource.resource_id, quotas(size=3 * GIB), quotas(), request_id=key)
        self.assertEqual(calls, ["app.stop", "storage.mongo.limits", "app.deploy"])
        interrupted = False
        service.select(resource.resource_id, quotas(size=3 * GIB), quotas(), request_id=key)
        self.assertEqual(calls, ["app.stop", "storage.mongo.limits", "app.deploy", "app.deploy"])
        self.assertEqual(
            db.get_deployment(self.connection, APP_ID).nomad_job,
            storage_hosts_job(job, self.config.platform),
        )
        self.assertEqual(db.get_operation(self.connection, key).status, "succeeded")

    def test_migration_replays_only_unswitched_resources_and_refreshes_accepted_job(self):
        first = self.add(APP_ID, "postgres")
        second = self.add(APP_ID, "mongo")
        application = db.get_application(self.connection, APP_ID)
        # The production renderer supplies scoped Variables and restart semantics.
        # Use a real generated job because immutable job identity is a boundary.
        from openstack_platform.controller.application_models import Manifest
        from openstack_platform.controller.nomad_jobs import render_nomad_job

        job = render_nomad_job(
            application_id=APP_ID,
            application_slug=application.slug,
            image="registry/app@sha256:" + "b" * 64,
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=self.config.platform,
            cpu_mhz=1000,
            memory_mib=1024,
            source_commit="a" * 40,
            recipe_hash="c" * 64,
        )
        accept_deployment(
            self.connection,
            application_id=APP_ID,
            source_commit="a" * 40,
            recipe_hash="c" * 64,
            image_digest="registry/app@sha256:" + "b" * 64,
            nomad_job=job,
            nomad_version=1,
            build_log_path="logs/build.log",
        )
        db.set_application_runtime(
            self.connection,
            APP_ID,
            running=True,
            worker_server_id=str(uuid.uuid4()),
            worker_server_name="worker",
            worker_port_id=str(uuid.uuid4()),
            worker_port_name="worker-v4",
            nomad_version=1,
        )
        calls = []
        interrupted = True

        def helper(config, action, args, **bounds):
            calls.append((action, args))
            if action.endswith(".usage"):
                return {"usage": {"usedBytes": 0}}
            if action == "storage.backup.ensure":
                return {
                    "verified": True,
                    "shared": True,
                    "type": args["type"],
                    "database": args["database"],
                    "backedUpAt": db.utc_now(),
                }
            if action == "app.stop":
                return {"jobStopped": True}
            if action == "storage.instances.migrate":
                if args["type"] == "mongo" and interrupted:
                    raise RuntimeError("lost copy response")
                return {
                    "verified": True,
                    "published": True,
                    "instancePort": 30000 if args["type"] == "postgres" else 30001,
                }
            if action == "app.deploy":
                return {"nomadVersion": 2}
            if action == "app.health":
                return {"healthy": True}
            raise AssertionError(action)

        service = InstanceMigrationService(
            self.connection, self.config, self.root, helper_caller=helper
        )
        key = str(uuid.uuid4())
        self.connection.execute(
            "UPDATE managed_resources SET postgres_connections=101 WHERE resource_id=?",
            (first.resource_id,),
        )
        with self.assertRaises(ValidationError):
            service.migrate(request_id=key)
        self.assertEqual(calls, [])
        self.assertIsNone(db.get_unfinished_operation(self.connection, "app-" + APP_ID))
        self.connection.execute(
            "UPDATE managed_resources SET postgres_connections=10 WHERE resource_id=?",
            (first.resource_id,),
        )
        with self.assertRaises(RuntimeError):
            service.migrate(request_id=key)
        self.assertIsNotNone(
            db.get_managed_resource(self.connection, first.resource_id).instance_id
        )
        self.assertIsNone(db.get_managed_resource(self.connection, second.resource_id).instance_id)
        child = db.get_unfinished_operation(self.connection, "app-" + APP_ID)
        self.assertEqual(child.status, "recovery_required")
        with self.assertRaises(CapacityError) as published:
            service.abort(request_id=str(uuid.uuid4()), application_ids=(APP_ID,))
        self.assertEqual(published.exception.code, "MIGRATION_ALREADY_PUBLISHED")
        interrupted = False
        service.migrate(request_id=key)
        self.assertEqual(
            [args["type"] for action, args in calls if action == "storage.instances.migrate"],
            ["postgres", "mongo", "mongo"],
        )
        self.assertEqual(sum(action == "app.stop" for action, args in calls), 1)
        self.assertEqual(db.get_deployment(self.connection, APP_ID).nomad_version, 2)
        self.assertEqual(db.get_operation(self.connection, key).status, "succeeded")
        self.assertFalse(any(action.endswith("remove") for action, args in calls))

    def test_resource_backup_has_its_own_deadline_before_the_app_copy_budget(self):
        resource = self.add(APP_ID, "postgres")
        now = [1000.0]

        def helper(config, action, args, **bounds):
            if action.endswith(".usage"):
                return {"usage": {"usedBytes": 0}}
            if action == "storage.backup.ensure":
                self.assertEqual(bounds["deadline"], now[0] + 3600)
                now[0] += 1900  # Longer than the default app budget; no sleeping.
                return {
                    "verified": True,
                    "shared": True,
                    "type": "postgres",
                    "database": resource.provider_name,
                    "backedUpAt": db.utc_now(),
                }
            if action == "storage.instances.migrate":
                self.assertEqual(bounds["deadline"], now[0] + 1800)
                self.assertEqual(args["copySeconds"], 1800)
                return {"verified": True, "published": True, "instancePort": 30000}
            raise AssertionError(action)

        key = str(uuid.uuid4())
        service = InstanceMigrationService(
            self.connection, self.config, self.root, helper_caller=helper
        )
        with mock.patch(
            "openstack_platform.controller.storage_instances.time.monotonic",
            side_effect=lambda: now[0],
        ):
            service.migrate(request_id=key, application_ids=(APP_ID,))
        self.assertEqual(db.get_operation(self.connection, key).status, "succeeded")

    def test_migration_backup_gate_and_abort_unfreeze_are_resumable(self):
        resource = self.add(APP_ID, "mongo")
        other = str(uuid.uuid4())
        self.add(other, "postgres")
        calls = []
        verified = False

        def helper(config, action, args, **bounds):
            calls.append((action, args))
            if action.endswith(".usage"):
                return {"usage": {"usedBytes": 0}, "writeBlocked": True}
            if action == "storage.backup.ensure":
                return {
                    "verified": verified,
                    "shared": True,
                    "type": args["type"],
                    "database": args["database"],
                    "backedUpAt": db.utc_now(),
                }
            if action == "storage.instances.migrate":
                raise RuntimeError("copy failed")
            if action == "storage.instances.abort":
                self.assertEqual(args["sourceRole"], "platform_size_blocked")
                return {"unpublished": True, "unfrozen": args["mode"] == "apply"}
            raise AssertionError(action)

        service = InstanceMigrationService(
            self.connection, self.config, self.root, helper_caller=helper
        )
        master = str(uuid.uuid4())
        with self.assertRaises(ValidationError):
            service.migrate(request_id=master, application_ids=(APP_ID,))
        self.assertFalse(any(action == "storage.instances.migrate" for action, _ in calls))
        verified = True
        with self.assertRaises(RuntimeError):
            service.migrate(request_id=master, application_ids=(APP_ID,))
        with self.assertRaises(ValidationError):
            service.migrate(request_id=master, application_ids=(other,))
        abort = str(uuid.uuid4())
        service.abort(request_id=abort, application_ids=(APP_ID,))
        service.abort(request_id=abort, application_ids=(APP_ID,))
        self.assertEqual(
            sum(
                action == "storage.instances.abort" and args["mode"] == "apply"
                for action, args in calls
            ),
            1,
        )
        self.assertIsNone(db.get_unfinished_operation(self.connection, "app-" + APP_ID))
        self.assertEqual(db.get_operation(self.connection, master).status, "failed")
        self.assertIsNone(
            db.get_managed_resource(self.connection, resource.resource_id).instance_id
        )

    def test_manager_outage_is_retryable_before_deployment_admission(self):
        from openstack_platform.remote import HelperError

        resource = self.add(APP_ID, "postgres")
        db.set_storage_instance(self.connection, resource.resource_id, resource.resource_id, 30000)
        helper = mock.Mock(side_effect=HelperError("INSTANCE_MANAGER_UNAVAILABLE", "retry"))
        api = ControllerAPI(self.connection, self.config, self.root, helper_caller=helper)
        try:
            key = str(uuid.uuid4())
            with self.assertRaises(HttpError) as rejected:
                api.router("project").dispatch(
                    "POST", f"/v1/applications/{APP_ID}/enable", {"Idempotency-Key": key}, {}
                )
            self.assertEqual(
                (rejected.exception.status, rejected.exception.code, rejected.exception.retryable),
                (503, "INSTANCE_MANAGER_UNAVAILABLE", True),
            )
            self.assertIsNone(db.get_operation(self.connection, key))
            self.assertIsNone(db.get_operation_dispatch(self.connection, key))
        finally:
            api.close()

    def test_s3_metadata_and_object_budget_include_unfinished_raises(self):
        self.add(APP_ID, "postgres")
        first = db.put_managed_resource(
            self.connection,
            application_id=APP_ID,
            resource_type="s3",
            provider_name="bucket",
            lifecycle_state="active",
            s3_bytes=MIB,
            s3_objects=100000,
        )
        second_app = str(uuid.uuid4())
        self.add(second_app, "postgres")
        second = db.put_managed_resource(
            self.connection,
            application_id=second_app,
            resource_type="s3",
            provider_name="bucket-two",
            lifecycle_state="active",
            s3_bytes=MIB,
            s3_objects=100000,
        )
        from openstack_platform.controller.storage_capacity import disk_reservations

        self.assertGreaterEqual(
            disk_reservations(self.connection)["garageBytes"], 2 * 100000 * 4096
        )
        db.begin_operation(
            self.connection,
            operation_id=str(uuid.uuid4()),
            kind="storage.limits.set",
            scope="app-" + APP_ID,
            phase="applying",
            deadline_at=db.utc_now(),
            refs={
                "resource_id": first.resource_id,
                "quotas": {"s3Bytes": MIB, "s3Objects": 4900000},
            },
        )
        with self.assertRaises(CapacityError) as rejected:
            check(self.connection, second.resource_id, {"s3Bytes": MIB, "s3Objects": 100001})
        self.assertEqual(rejected.exception.code, "OBJECT_BUDGET_EXCEEDED")

    def test_failed_create_retains_identity_and_capacity_until_explicit_cleanup(self):
        from openstack_platform.controller import storage
        from openstack_platform.remote import HelperError

        self.add(APP_ID, "mongo")  # declare the application, then remove the fixture resource
        self.connection.execute("DELETE FROM managed_resources")

        def rejected(action, args, **bounds):
            raise HelperError("CREATE_ROLLED_BACK", "provider credentials rolled back")

        with self.assertRaises(storage.StorageOperationError):
            storage.create(
                self.connection, self.config, APP_ID, ["postgres"], helper_caller=rejected
            )
        row = db.list_managed_resources(self.connection)[0]
        self.assertEqual(row.lifecycle_state, "recovery_required")
        self.assertEqual(row.instance_id, row.resource_id)
        self.assertEqual(row.memory_bytes, 512 * MIB)
        self.assertEqual(
            db.get_unfinished_operation(self.connection, "app-" + APP_ID).status,
            "recovery_required",
        )

    def test_worker_delete_replay_replaces_allowlist_before_provider_deletion(self):
        from openstack_platform.controller.application_runtime import deployment_worker_ids
        from openstack_platform.controller.fixed_ip_service import worker_helper

        resource = self.add(APP_ID, "postgres")
        db.set_storage_instance(
            self.connection, resource.resource_id, resource.resource_id, 30000, migration_state=None
        )
        slots = (APP_ID, *deployment_worker_ids(APP_ID))
        addresses = {slots[0]: "10.0.0.8", slots[1]: "10.0.0.9"}
        calls = []
        interrupted = True

        def helper(config, action, args, **bounds):
            nonlocal interrupted
            calls.append((action, args))
            if action == "app.worker.observe":
                address = addresses.get(args["applicationId"])
                return {"absent": address is None, "address": address}
            if action == "storage.instances.network":
                return {"applied": True}
            if action == "app.worker.delete":
                addresses.pop(args["applicationId"], None)
                if interrupted:
                    interrupted = False
                    raise RuntimeError("lost delete reply")
                return {"absent": True}
            raise AssertionError(action)

        wrapped = worker_helper(self.connection, helper)
        args = {"applicationId": APP_ID, "slug": "app-" + APP_ID[:8], "single": True}
        with self.assertRaises(RuntimeError):
            wrapped(self.config, "app.worker.delete", args, deadline=1234)
        wrapped(self.config, "app.worker.delete", args, deadline=1234)
        network = [values for action, values in calls if action == "storage.instances.network"]
        self.assertEqual([value["addresses"] for value in network], [["10.0.0.9"], ["10.0.0.9"]])
        self.assertTrue(all(value["mode"] == "replace" for value in network))
        self.assertLess(
            next(i for i, (action, _) in enumerate(calls) if action == "storage.instances.network"),
            next(i for i, (action, _) in enumerate(calls) if action == "app.worker.delete"),
        )

    def test_workload_variable_mirror_replays_a_lost_scoped_write(self):
        canonical = "nomad/jobs/app"
        workload = "nomad/jobs/app-candidate"
        values = {canonical: {"BOUND": "old"}, workload: {"BOUND": "old"}}
        failure = True

        class Variables:
            def read_variable(self, path):
                return VariableSnapshot(path, 1, SecretItems(values[path]))

            def compare_and_set(self, path, index, items):
                if path == workload and failure:
                    raise RuntimeError("lost workload write")
                values[path] = dict(items)
                return 2

        client = WorkloadVariables(Variables(), "app", "app-candidate")
        with self.assertRaises(RuntimeError):
            client.compare_and_set(canonical, 1, {"BOUND": "new"})
        self.assertEqual(values[canonical]["BOUND"], "new")
        self.assertEqual(values[workload]["BOUND"], "old")
        failure = False
        client.compare_and_set(canonical, 1, {"BOUND": "new"})
        self.assertEqual(values[workload], values[canonical])


class MigrationFingerprintTests(unittest.TestCase):
    def test_target_checksum_selection_uses_source_even_after_compaction(self):
        class PostgreSQL:
            def __init__(self, size):
                self.size = size
                self.value = None

            def execute(self, query, values=None):
                rendered = str(query)
                if "pg_tables" in rendered:
                    self.value = [("public", "items")]
                elif "pg_total_relation_size" in rendered:
                    self.value = (self.size,)
                elif "count(*)" in rendered:
                    self.value = (3,)
                else:
                    self.value = ("checksum",)
                return self

            def fetchall(self):
                return self.value

            def fetchone(self):
                return self.value

        before = postgres_fingerprint(PostgreSQL(16 * MIB))
        after = postgres_fingerprint(
            PostgreSQL(MIB), {name for name, (_, digest) in before.items() if digest is not None}
        )
        self.assertEqual(before, after)

        class MongoDB:
            def __init__(self, size):
                self.size = size

            def list_collection_names(self):
                return ["items"]

            def __getitem__(self, name):
                return self

            def count_documents(self, query):
                return 3

            def command(self, name, **kwargs):
                return (
                    {"dataSize": self.size}
                    if name == "dbStats"
                    else {"collections": {"items": "checksum"}}
                )

        before = mongo_fingerprint(MongoDB(16 * MIB))
        after = mongo_fingerprint(MongoDB(MIB), before["hashes"] is not None)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
