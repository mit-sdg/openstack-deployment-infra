from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from openstack_platform import database_backups as backup
from openstack_platform.controller.storage_contract import canonicalize_environment
from openstack_platform.helper import storage
from openstack_platform.helper.nomad import SecretItems, VariableSnapshot
from openstack_platform.instance_contract import GIB, MIB
from openstack_platform.storage_instances import Manager
from openstack_platform.validation import ValidationError


class DatabaseBackupsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "namespace": "example",
            "projectId": str(uuid.uuid4()),
            "paths": {"root": str(self.root), "backups": str(self.root / "backups")},
            "addresses": {"admin": "10.0.0.2", "storage": "10.0.0.4"},
            "internalNames": {"storage": "storage.example.internal"},
        }
        secrets = self.root / "secrets"
        secrets.mkdir()
        (secrets / "storage-bootstrap.env").write_text(
            "POSTGRES_PASSWORD=pg-root\nMONGO_PASSWORD=mongo-root\n"
        )
        self.quotas = {
            "sizeBytes": 2 * GIB,
            "memoryBytes": 512 * MIB,
            "connections": 10,
            "cpuMillicores": 500,
        }
        self.command = lambda argv, **options: subprocess.CompletedProcess(
            argv, 0, b"0\n" if argv[0] == "du" else b"", b""
        )
        self.geometry = mock.patch(
            "openstack_platform.storage_instances.os.statvfs",
            return_value=mock.Mock(f_blocks=1024, f_frsize=GIB),
        )
        self.geometry.start()
        self.addCleanup(self.geometry.stop)

    def manager(self, name):
        return Manager(
            {**self.config, "paths": {**self.config["paths"], "data": str(self.root / name)}},
            command=self.command,
            units=self.root / (name + "-units"),
            mongo_connect=mock.MagicMock(),
        )

    def test_transition_bundle_restores_each_database_into_fresh_instance_and_replays(self):
        for kind in ("postgres", "mongo"):
            with self.subTest(kind=kind):
                self._round_trip(kind)

    def _round_trip(self, kind):
        source_manager = self.manager(kind)
        target_manager = self.manager(kind + "-fresh")
        app_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        resource_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        names = ["p_" + app.replace("-", "")[:20] for app in app_ids]
        result = source_manager.dispatch(
            {
                "action": "create",
                "instanceId": resource_ids[1],
                "applicationId": app_ids[1],
                "type": kind,
                "quotas": self.quotas,
                "allowIps": ["10.0.0.8"],
                "reservations": {"databaseBytes": 6 * GIB, "garageBytes": 0},
            }
        )
        rows = []
        variables = {}
        for index in range(2):
            storage._PORT_CONTEXT.set(
                (result["port"] if index else 5432, result["port"] if index else 27017)
            )
            username = "u_" + names[index][2:] + "_abcdef12"
            environment = (
                storage.postgres_environment(
                    "storage.example.internal", names[index], username, "app-secret"
                )
                if kind == "postgres"
                else storage.mongo_environment(
                    "storage.example.internal", names[index], username, "app-secret"
                )
            )
            variables["nomad/jobs/app-" + str(index)] = dict(
                canonicalize_environment(kind, "default", environment)
            )
            rows.append(
                dict(
                    resourceId=resource_ids[index],
                    applicationId=app_ids[index],
                    applicationSlug="app-" + str(index),
                    type=kind,
                    name="default",
                    providerName=names[index],
                    lifecycleState="active",
                    instanceId=resource_ids[index] if index else None,
                    quotas=self.quotas,
                )
            )
        storage._PORT_CONTEXT.set((5432, 27017))
        restored = {}

        class Provider:
            def databases(self, selected, port, password):
                return [names[1] if port >= 30000 else names[0]]

            def dump(self, entry, password, path):
                path.write_bytes(
                    json.dumps(
                        {
                            "rows": [1, 2, 3],
                            "database": entry["databases"][0],
                            "roles": ["readWrite"],
                        }
                    ).encode()
                )

            def restore(self, entry, password, path):
                restored[entry["id"]] = json.loads(path.read_bytes())

        class Variables:
            def read_variable(self, path):
                return VariableSnapshot(
                    path,
                    1 if path in variables else 0,
                    SecretItems(variables.get(path, {})),
                )

            def compare_and_set(self, path, index, items):
                variables[path] = dict(items)
                return index + 1

        class Client:
            def __init__(self, manager):
                self.manager = manager

            def call(self, action, identifier=None, **values):
                return self.manager.dispatch(
                    {
                        "action": action,
                        **({"instanceId": identifier} if identifier else {}),
                        **values,
                    }
                )

        context = backup.Context(
            self.config,
            manager=Client(source_manager),
            provider=Provider(),
            nomad=Variables(),
            resources=lambda: rows,
        )
        stream = io.BytesIO()
        catalog = self.root / (kind + "-catalog.json")
        backup.emit(context, kind, stream, catalog=catalog)
        public = json.loads(catalog.read_text())
        self.assertEqual(
            [item["instanceId"] is not None for item in public["entries"]], [False, True]
        )
        self.assertNotIn("app-secret", catalog.read_text())
        # The gate must not dump other resources or call the instance manager.
        scoped = io.BytesIO()
        with mock.patch.object(
            context.manager, "call", side_effect=AssertionError("unrelated instance")
        ):
            selected = backup.emit(context, kind, scoped, database=names[0])
        self.assertEqual([entry["databases"] for entry in selected["entries"]], [[names[0]]])
        receipt_dir = backup.receipt_root(self.config)
        receipt_dir.mkdir(exist_ok=True, parents=True)
        encrypted_inputs = []

        def crypto(argv, **options):
            if "--encrypt" in argv:
                encrypted_inputs.append(Path(argv[-1]).read_bytes())
            if "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_bytes(Path(argv[-1]).read_bytes())
            return subprocess.CompletedProcess(argv, 0, b"age1recipient", b"")

        with mock.patch.dict("os.environ", {"AGE_KEY": "operator-only.key"}):
            receipt = backup.checkpoint_backup(
                self.config, kind + "-" + names[0], context=context, execute=crypto
            )
        self.assertTrue(receipt["verified"])
        self.assertEqual(len(encrypted_inputs), 1)
        self.assertEqual(
            backup.consume(io.BytesIO(encrypted_inputs[0]), lambda entry, path: None)["shared"], 1
        )
        self.assertNotIn(
            "app-secret", (receipt_dir / (kind + "-" + names[0] + ".json")).read_text()
        )
        from openstack_platform.controller import database as db

        database_path = self.root / (kind + "-controller.sqlite3")
        connection = db.connect(database_path)
        db.migrate(connection)
        for index, app_id in enumerate(app_ids):
            db.put_application(
                connection,
                application_id=app_id,
                application_slug="app-" + str(index),
                worker_flavor="small",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=1024,
            )
            db.put_managed_resource(
                connection,
                resource_id=resource_ids[index],
                application_id=app_id,
                resource_type=kind,
                provider_name=names[index],
                lifecycle_state="active",
                measured_target_bytes=2 * GIB,
                postgres_connections=10 if kind == "postgres" else None,
                mongo_connections=10 if kind == "mongo" else None,
            )
        db.set_storage_instance(
            connection, resource_ids[1], resource_ids[1], result["port"], migration_state=None
        )
        connection.close()
        target = backup.Context(
            self.config,
            manager=Client(target_manager),
            provider=Provider(),
            nomad=Variables(),
            resources=lambda: rows,
        )

        def restore(entry, path):
            with mock.patch(
                "openstack_platform.helper.production._provider_app", return_value={"absent": True}
            ):
                target.restore_entry(
                    entry,
                    path,
                    reservations={"databaseBytes": 6 * GIB, "garageBytes": 0},
                    controller_database=database_path,
                )

        for _ in range(2):
            stream.seek(0)
            counts = backup.consume(
                stream, restore, config=self.config, manifest_observer=target.reserve_restore_ports
            )
            self.assertEqual((counts["shared"], counts["isolated"]), (1, 1))
        self.assertEqual(len(restored), 2)
        self.assertNotEqual(
            target_manager.read(resource_ids[0])["port"],
            target_manager.read(resource_ids[1])["port"],
        )
        self.assertEqual(restored[resource_ids[1]]["rows"], [1, 2, 3])
        config = target_manager.read(resource_ids[1])
        self.assertEqual(config["port"], result["port"])
        self.assertNotIn("restorePending", config)
        connection = db.connect(database_path)
        try:
            self.assertTrue(
                all(row.instance_id is not None for row in db.list_managed_resources(connection))
            )
        finally:
            connection.close()
        stream.seek(0)
        with self.assertRaises(ValidationError):
            backup.consume(
                stream, mock.Mock(), config={**self.config, "projectId": str(uuid.uuid4())}
            )

    def test_backup_gate_uses_operator_receipts_without_opening_private_payloads(self):
        import os

        root = backup.receipt_root(self.config)
        root.mkdir(parents=True)
        database = "p_" + "a" * 20
        receipt = root / ("postgres-" + database + ".json")
        private = Path(self.config["paths"]["backups"]) / self.config["namespace"]
        private.mkdir(mode=0o700)
        original_read = Path.read_text

        def controller_read(path, *args, **options):
            if path == private or private in path.parents:
                raise PermissionError("operator-only backup payload/key")
            return original_read(path, *args, **options)

        def publish(argv, **options):
            self.assertEqual(
                argv,
                [
                    "/run/current-system/sw/bin/systemctl",
                    "--no-ask-password",
                    "start",
                    "example-resource-backup@postgres-" + database + ".service",
                ],
            )
            receipt.write_text(
                json.dumps(
                    {
                        "namespace": self.config["namespace"],
                        "projectId": self.config["projectId"],
                        "type": "postgres",
                        "database": database,
                        "shared": True,
                        "verified": True,
                        "backedUpAt": backup.timestamp(),
                        "payloadSha256": "a" * 64,
                    }
                )
            )
            receipt.chmod(0o640)

        execute = mock.Mock(side_effect=publish)
        with (
            mock.patch("openstack_platform.contracts.OPERATOR_ACCOUNT_UID", os.getuid()),
            mock.patch.object(Path, "read_text", controller_read),
        ):
            self.assertTrue(
                backup.ensure_backup(self.config, "postgres", database, 60, execute=execute)[
                    "verified"
                ]
            )
            backup.ensure_backup(self.config, "postgres", database, 60, execute=execute)
            self.assertEqual(execute.call_count, 1)
            value = json.loads(receipt.read_text())
            value["backedUpAt"] = (datetime.now(UTC) - timedelta(minutes=61)).isoformat()
            receipt.write_text(json.dumps(value))
            with self.assertRaises(ValidationError):
                backup.ensure_backup(self.config, "postgres", database, 60, execute=mock.Mock())
            with self.assertRaises(RuntimeError):
                backup.ensure_backup(
                    self.config,
                    "postgres",
                    database,
                    60,
                    execute=mock.Mock(side_effect=RuntimeError("export failed")),
                )
            receipt.chmod(0o660)
            self.assertIsNone(
                backup.fresh_backup(root, "postgres", database, 1440, config=self.config)
            )

    def test_root_role_filter_preserves_copy_data_and_application_sql(self):
        source = self.root / "dump.sql"
        source.write_bytes(
            b"ALTER ROLE platform_admin PASSWORD 'old';\nCREATE ROLE app;\n\\connect app\nCOPY records FROM stdin;\nCREATE ROLE platform_admin;\n\\.\n"
        )
        target = self.root / "restore.sql"
        backup.filter_postgres(source, target)
        self.assertEqual(target.read_bytes(), source.read_bytes().split(b"\n", 1)[1])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_native_restore_replaces_target_data_and_reapplies_current_limits(self):
        database = "p_" + "a" * 20
        login = "u_" + "a" * 20 + "_abcdef12"

        def exercise(kind):
            with self.subTest(kind=kind):
                reset = False
                imported = False
                state = {"role": "readWrite", "connections": 10, "databaseConnections": 10}
                admin = mock.MagicMock()
                admin.__enter__.return_value = admin

                def postgres(query, parameters=None):
                    nonlocal reset
                    text = query if isinstance(query, str) else query.as_string()
                    if "pg_terminate_backend" in text:
                        self.assertEqual(parameters, ([database],))
                        reset = True
                    elif "CONNECTION LIMIT" in text:
                        self.assertTrue(imported)
                        field = (
                            "connections"
                            if text.startswith("ALTER ROLE")
                            else "databaseConnections"
                        )
                        state[field] = int(text.rsplit(" ", 1)[-1])
                    return admin

                admin.execute.side_effect = postgres
                mongo = mock.MagicMock()
                mongo.__enter__.return_value = mongo
                target = mongo[database]

                def mongodb(command, *args, **options):
                    nonlocal reset
                    if command == "dropDatabase":
                        reset = True
                    elif command == "updateUser":
                        self.assertTrue(imported)
                        state["role"] = options["roles"][0]["role"]
                    elif command == "usersInfo":
                        return {
                            "users": [
                                {"user": login, "roles": [{"role": state["role"], "db": database}]}
                            ]
                        }
                    elif command == "dbStats":
                        return {"dataSize": 42, "indexSize": 0}
                    return {"ok": 1}

                target.command.side_effect = mongodb

                def restore(argv, **options):
                    nonlocal imported
                    self.assertTrue(reset)
                    imported = True

                environment = (
                    storage.postgres_environment(
                        "storage.example.internal", database, login, "app-private"
                    )
                    if kind == "postgres"
                    else storage.mongo_environment(
                        "storage.example.internal", database, login, "app-private"
                    )
                )
                entry = {
                    "type": kind,
                    "port": 30000,
                    "databases": [database],
                    "quotas": {**self.quotas, "connections": 20},
                    "resources": [
                        {
                            "name": "default",
                            "providerName": database,
                            "bindings": dict(
                                canonicalize_environment(kind, "default", environment)
                            ),
                            "writeBlock": {"blocked": True},
                        }
                    ],
                }
                payload = self.root / (kind + ".dump")
                payload.write_bytes(b"snapshot")
                native = backup.Native("storage.example.internal", "ca.pem", execute=restore)
                native.databases = mock.Mock(return_value=[database])
                with (
                    mock.patch(
                        "openstack_platform.database_backups.psycopg.connect", return_value=admin
                    ),
                    mock.patch(
                        "openstack_platform.database_backups.MongoClient", return_value=mongo
                    ),
                ):
                    native.restore(entry, "root-private", payload)
                self.assertTrue(imported)
                if kind == "postgres":
                    self.assertEqual((state["connections"], state["databaseConnections"]), (20, 20))
                else:
                    self.assertEqual(state["role"], "readWrite")

        for kind in ("postgres", "mongo"):
            exercise(kind)


if __name__ == "__main__":
    unittest.main()
