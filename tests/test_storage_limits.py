from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from infra.lib.storage_health import alarms
from infra.monitor.storage_host import STATUS_PATH, handler
from openstack_platform.contracts import MONGO_OWNER_FIELD
from openstack_platform.controller import database as db
from openstack_platform.controller import storage_limits as limits
from openstack_platform.controller import storage_repair
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.http import HttpError
from openstack_platform.controller.storage_contract import canonicalize_environment
from openstack_platform.helper import storage as providers
from openstack_platform.helper.storage_limits import (
    MONGO_BLOCKED_ROLE,
    host_projection,
    mongo_reconcile,
    resource_action,
)
from openstack_platform.validation import ValidationError
from tests.test_platform_storage import APP_ID, MemoryNomad, config_fixture

DATABASE = "p_11111111111141118111"
LOGIN = "u_11111111111141118111_abcdef12"
MIB = 1024**2
GIB = 1024**3


def db_quotas(size=GIB, connections=10, memory=512 * MIB, cpu=500):
    return {
        "sizeBytes": size,
        "connections": connections,
        "memoryBytes": memory,
        "cpuMillicores": cpu,
    }


def now():
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Postgres:
    def __init__(self):
        self.statements = []
        self.info = SimpleNamespace(get_parameters=lambda: {}, password="root-private")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, statement, parameters=()):
        self.statements.append(statement)
        return self

    def fetchone(self):
        statement = self.statements[-1]
        if statement.startswith("SELECT 1 FROM"):
            return None
        if "numbackends" in statement:
            return (123456, 7)
        if statement.startswith("SELECT pg_database_size"):
            return (0,)
        return None

    def fetchall(self):
        return [(LOGIN,)]


class Mongo:
    def __init__(self):
        self.users = {
            LOGIN: {
                "user": LOGIN,
                "roles": [{"role": "readWrite", "db": DATABASE}],
                "customData": {MONGO_OWNER_FIELD: APP_ID},
            }
        }
        self.used = 2 * GIB
        self.actions = []
        self.role_exists = False
        self.fail_switch = False
        self.commands = []

    def __getitem__(self, name):
        return self

    def command(self, name, *args, **values):
        self.commands.append(name)
        if name == "usersInfo":
            return {"users": list(self.users.values())}
        if name == "dbStats":
            return {"dataSize": self.used - 100, "indexSize": 100, "storageSize": 50 * MIB}
        if name == "rolesInfo":
            return {"roles": [{"role": MONGO_BLOCKED_ROLE}] if self.role_exists else []}
        if name in {"createRole", "updateRole"}:
            self.role_exists = True
            self.actions = values["privileges"][0]["actions"]
        if name == "updateUser":
            if self.fail_switch:
                raise RuntimeError("lost role-switch response")
            self.users[args[0]]["roles"] = values["roles"]
        if name == "createUser":
            self.users[args[0]] = {
                "user": args[0],
                "roles": values["roles"],
                "customData": values["customData"],
            }
        return {"ok": 1}


class ProviderLimitsTests(unittest.TestCase):
    def test_ownership_normalization_rejects_other_apps_before_reassigning(self):
        admin = mock.Mock()
        admin.info.get_parameters.return_value = {"dbname": "platform"}
        admin.info.password = "root-private"
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchall.return_value = [("u_" + "2" * 20 + "_abcdef12",)]
        with mock.patch("psycopg.connect", return_value=connection):
            with self.assertRaises(providers.HelperActionError) as rejected:
                providers.postgres_normalize_ownership(
                    admin, DATABASE, "o_" + DATABASE[2:], login=LOGIN
                )
        self.assertEqual(rejected.exception.code, "IDENTITY_MISMATCH")
        self.assertEqual(connection.execute.call_count, 1)

    @mock.patch("psycopg.connect")
    def test_create_rotate_and_repair_share_postgres_role_settings(self, connect):
        admin = Postgres()
        connect.return_value = admin
        created = providers.postgres_create(
            admin,
            application_id=APP_ID,
            host="storage",
            connections=17,
            measured_target_bytes=GIB,
            generation="abcdef12",
            operation_id=str(uuid.uuid4()),
        )
        create_settings = [
            s.replace(created.credential_name, "LOGIN")
            for s in admin.statements
            if s.startswith("ALTER ROLE")
        ]
        admin.statements.clear()
        rotated = providers.postgres_rotate(
            admin,
            application_id=APP_ID,
            host="storage",
            connections=17,
            old_environment=created.environment,
            generation="12345678",
        )
        self.assertEqual(
            create_settings,
            [
                s.replace(rotated.credential_name, "LOGIN")
                for s in admin.statements
                if s.startswith("ALTER ROLE")
            ],
        )
        self.assertEqual(len(create_settings), 6)
        for setting in (
            "statement_timeout='30s'",
            "idle_in_transaction_session_timeout='60s'",
            "lock_timeout='5s'",
            "temp_file_limit='256MB'",
        ):
            self.assertTrue(any(setting in statement for statement in create_settings))
        admin.statements.clear()
        nomad = MemoryNomad(
            canonicalize_environment("postgres", "default", dict(created.environment))
        )
        args = {
            "applicationId": APP_ID,
            "applicationSlug": "demo-app",
            "resourceName": "default",
            "providerId": DATABASE,
            "providerName": DATABASE,
            "quotas": db_quotas(connections=17),
            "operationId": str(uuid.uuid4()),
            "recover": False,
        }
        for _ in range(2):
            result = resource_action(
                args,
                resource_type="postgres",
                mutate=True,
                admin=admin,
                nomad=nomad,
                host="storage",
                endpoint="unused",
            )
        self.assertEqual(result["usage"]["usedBytes"], 123456)
        self.assertEqual(result["usage"]["currentConnections"], 7)
        self.assertEqual(
            sum(
                s.startswith("ALTER DATABASE") and "CONNECTION LIMIT 17" in s
                for s in admin.statements
            ),
            2,
        )
        self.assertEqual(
            create_settings,
            [
                s.replace(created.credential_name, "LOGIN")
                for s in admin.statements
                if s.startswith("ALTER ROLE")
            ][:6],
        )

    def test_mongo_block_hysteresis_unblock_failure_and_rotation(self):
        admin = Mongo()
        self.assertTrue(mongo_reconcile(admin, LOGIN, DATABASE, 2 * MIB, MIB))
        self.assertTrue(
            {
                "find",
                "remove",
                "dropCollection",
                "dropIndex",
                "listCollections",
                "listIndexes",
                "dbStats",
                "collStats",
            }
            <= set(admin.actions)
        )
        self.assertFalse(
            {"insert", "update", "createCollection", "createIndex"} & set(admin.actions)
        )
        self.assertTrue(mongo_reconcile(admin, LOGIN, DATABASE, MIB, MIB))
        rotated = providers.mongo_rotate(
            admin,
            application_id=APP_ID,
            host="storage",
            generation="12345678",
            old_environment=providers.mongo_environment("storage", DATABASE, LOGIN, "password"),
        )
        self.assertEqual(
            admin.users[rotated.credential_name]["roles"],
            [{"role": MONGO_BLOCKED_ROLE, "db": DATABASE}],
        )
        client = mock.MagicMock()
        providers.mongo_verify(lambda **kwargs: client, rotated, host="storage", write_blocked=True)
        client[DATABASE].command.assert_called_once_with("dbStats", scale=1)
        client[DATABASE].__getitem__.assert_not_called()
        admin.fail_switch = True
        with self.assertRaises(RuntimeError):
            mongo_reconcile(admin, LOGIN, DATABASE, MIB // 2, MIB)
        self.assertEqual(
            admin.users[LOGIN]["roles"], [{"role": MONGO_BLOCKED_ROLE, "db": DATABASE}]
        )
        admin.fail_switch = False
        self.assertFalse(mongo_reconcile(admin, LOGIN, DATABASE, MIB // 2, MIB))
        self.assertEqual(admin.users[LOGIN]["roles"], [{"role": "readWrite", "db": DATABASE}])

    def test_garage_assigns_both_quotas_and_reads_bucket_counters(self):
        endpoint = "https://storage:9000"
        environment = providers.s3_environment(endpoint, "bucket", "key", "secret")
        nomad = MemoryNomad(canonicalize_environment("s3", "default", dict(environment)))
        admin = mock.Mock()
        info = {
            "id": "bucket-id",
            "globalAliases": ["bucket"],
            "keys": [
                {"accessKeyId": "key", "permissions": {"read": True, "write": True, "owner": False}}
            ],
            "bytes": 987654,
            "objects": 42,
            "quotas": {"maxSize": MIB, "maxObjects": 100},
        }
        admin.request.side_effect = lambda path, *args, **kwargs: info
        result = resource_action(
            {
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
                "resourceName": "default",
                "providerId": "bucket-id",
                "providerName": "bucket",
                "quotas": {"s3Bytes": MIB, "s3Objects": 100},
                "operationId": str(uuid.uuid4()),
                "recover": True,
            },
            resource_type="s3",
            mutate=True,
            admin=admin,
            nomad=nomad,
            host="storage",
            endpoint=endpoint,
        )
        admin.request.assert_any_call(
            "/UpdateBucket",
            {"quotas": {"maxSize": MIB, "maxObjects": 100}},
            query={"id": "bucket-id"},
        )
        self.assertEqual(
            (result["usage"]["usedBytes"], result["usage"]["objectCount"]), (987654, 42)
        )


class ControllerLimitsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = config_fixture(self.root)
        self.connection = db.connect(self.root / "platform.sqlite3")
        db.migrate(self.connection)
        db.put_application(
            self.connection,
            application_id=APP_ID,
            application_slug="demo-app",
            worker_flavor="small",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=1024,
        )
        self.resource = db.put_managed_resource(
            self.connection,
            application_id=APP_ID,
            resource_type="mongo",
            provider_id=DATABASE,
            provider_name=DATABASE,
            lifecycle_state="active",
            measured_target_bytes=GIB,
        )
        self.mongo = Mongo()
        self.nomad = MemoryNomad(
            canonicalize_environment(
                "mongo",
                "default",
                dict(providers.mongo_environment("storage", DATABASE, LOGIN, "password")),
            )
        )
        self.calls = []
        self.lose_response = False

        def helper(config, action, args, **bounds):
            self.calls.append((action, args))
            if action == "storage.host.observe":
                return {"measuredAt": now()}
            provider_args = {
                key: value
                for key, value in args.items()
                if key
                not in {
                    "instanceId",
                    "instanceQuotas",
                    "workerIds",
                    "resourceId",
                    "reservations",
                    "retainedWorker",
                    "workloadJobId",
                    "stagedInstanceId",
                }
            }
            result = resource_action(
                provider_args,
                resource_type="mongo",
                mutate=action.endswith("limits"),
                admin=self.mongo,
                nomad=self.nomad,
                host="storage",
                endpoint="unused",
            )
            if self.lose_response:
                raise RuntimeError("response lost")
            return result

        self.service = limits.StorageLimitsService(
            self.connection, self.config, self.root, helper_caller=helper
        )
        self.helper = helper

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_periodic_block_prompt_limit_raise_and_stale_cache_on_failure(self):
        self.service.collect()
        resource = db.get_managed_resource(self.connection, self.resource.resource_id)
        self.assertTrue(resource.write_blocked)
        self.assertEqual(resource.usage["usedBytes"], 2 * GIB)
        since = resource.blocked_since
        self.service.collect()
        self.assertEqual(
            db.get_managed_resource(self.connection, resource.resource_id).blocked_since, since
        )
        self.service.select(
            resource.resource_id,
            db_quotas(3 * GIB),
            db_quotas(),
            request_id=str(uuid.uuid4()),
        )
        resource = db.get_managed_resource(self.connection, resource.resource_id)
        self.assertFalse(resource.write_blocked)
        self.assertIsNone(resource.blocked_since)
        self.assertEqual(resource.measured_target_bytes, 3 * GIB)
        usage = dict(resource.usage)
        usage["measuredAt"] = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        db.put_storage_usage(self.connection, resource.resource_id, usage)
        self.lose_response = True
        self.service.collect()
        failed = db.get_managed_resource(self.connection, resource.resource_id)
        self.assertEqual(failed.usage, usage)
        self.assertEqual(failed.usage_error, "collection_failed")
        self.assertTrue(limits.usage_model(failed)["stale"])

    def test_failed_collector_releases_scope_and_admin_can_recover_crash_loop(self):
        self.service.collect()
        sample = db.get_managed_resource(self.connection, self.resource.resource_id).usage
        self.lose_response = True
        self.service.collect()
        self.assertIsNone(db.get_unfinished_operation(self.connection, "app-" + APP_ID))
        self.assertEqual(
            db.get_managed_resource(self.connection, self.resource.resource_id).usage, sample
        )
        deployment = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=deployment,
            kind="app.deploy",
            scope="app-" + APP_ID,
            phase="validated",
            deadline_at=db.utc_now(),
        )
        db.mark_failed(self.connection, deployment, "fixture", cleanup_state="not_required")
        self.lose_response = False
        self.service.select(
            self.resource.resource_id, db_quotas(3 * GIB), db_quotas(), request_id=str(uuid.uuid4())
        )
        self.assertEqual(
            db.get_managed_resource(
                self.connection, self.resource.resource_id
            ).measured_target_bytes,
            3 * GIB,
        )

    def test_size_reduction_requires_fresh_usage_and_margin_before_any_assignment(self):
        self.service.collect()
        self.connection.execute(
            "UPDATE managed_resources SET measured_target_bytes=? WHERE resource_id=?",
            (3 * GIB, self.resource.resource_id),
        )
        with self.assertRaises(ValidationError) as denied:
            self.service.select(
                self.resource.resource_id,
                db_quotas(GIB),
                db_quotas(3 * GIB),
                request_id=str(uuid.uuid4()),
            )
        self.assertEqual(denied.exception.code, "SIZE_BELOW_USAGE")
        usage = dict(db.get_managed_resource(self.connection, self.resource.resource_id).usage)
        usage["measuredAt"] = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        db.put_storage_usage(self.connection, self.resource.resource_id, usage)
        with self.assertRaises(ValidationError) as stale:
            self.service.select(
                self.resource.resource_id,
                db_quotas(GIB),
                db_quotas(3 * GIB),
                request_id=str(uuid.uuid4()),
            )
        self.assertEqual(stale.exception.code, "USAGE_NOT_FRESH")

    def test_near_limit_collection_runs_every_minute(self):
        with mock.patch(
            "openstack_platform.controller.storage_limits.time.monotonic", return_value=1000
        ):
            self.service.collect(scheduled=True)
        self.assertEqual(self.service._next_due[self.resource.resource_id], 1060)

    def test_lost_provider_response_replays_intent_without_accepting_limits_early(self):
        key = str(uuid.uuid4())
        self.lose_response = True
        target, expected = db_quotas(3 * GIB), db_quotas()
        with self.assertRaises(RuntimeError):
            self.service.select(self.resource.resource_id, target, expected, request_id=key)
        self.assertEqual(
            db.get_managed_resource(
                self.connection, self.resource.resource_id
            ).measured_target_bytes,
            GIB,
        )
        self.assertEqual(db.get_operation(self.connection, key).status, "recovery_required")
        with self.assertRaises(db.UnfinishedOperationError):
            self.service.select(
                self.resource.resource_id,
                db_quotas(4 * GIB),
                expected,
                request_id=key,
            )
        self.lose_response = False
        self.service.select(self.resource.resource_id, target, expected, request_id=key)
        self.assertTrue(self.calls[-1][1]["recover"])
        self.assertEqual(db.get_operation(self.connection, key).status, "succeeded")
        self.assertEqual(
            db.get_managed_resource(
                self.connection, self.resource.resource_id
            ).measured_target_bytes,
            3 * GIB,
        )
        with self.assertRaises(ValidationError):
            self.service.select(
                self.resource.resource_id, target, expected, request_id=str(uuid.uuid4())
            )

    def test_project_route_idempotency_bound_validation_and_private_read_projection(self):
        api = ControllerAPI(self.connection, self.config, self.root, helper_caller=self.helper)
        try:
            project, privileged = api.router("project"), api.router("privileged")
            path = f"/v1/storage/{self.resource.resource_id}/limits"
            for router, route in (
                (privileged, path),
                (project, "/v1/admin/storage/repair-postgres"),
            ):
                with self.assertRaises(HttpError) as denied:
                    router.dispatch("PUT" if route == path else "POST", route, {}, {})
                self.assertEqual(denied.exception.status, 404)
            for value in (True, MIB - 1, 500 * 1024**3 + 1, None):
                with self.assertRaises(HttpError) as invalid:
                    project.dispatch(
                        "PUT",
                        path,
                        {"Idempotency-Key": str(uuid.uuid4())},
                        {
                            "quotas": db_quotas(value),
                            "expectedQuotas": db_quotas(),
                        },
                    )
                self.assertEqual(invalid.exception.status, 400)
            key = str(uuid.uuid4())
            body = {
                "quotas": db_quotas(3 * GIB),
                "expectedQuotas": db_quotas(),
            }
            accepted = project.dispatch("PUT", path, {"Idempotency-Key": key}, body)
            self.assertEqual(accepted.status, 202)
            self.assertEqual(accepted.body["statusUrl"], f"/v1/operations/{key}")
            api.wait_for_operations()
            self.assertEqual(
                project.dispatch("PUT", path, {"Idempotency-Key": key}, body).body, accepted.body
            )
            with self.assertRaises(HttpError) as conflict:
                project.dispatch(
                    "PUT",
                    path,
                    {"Idempotency-Key": key},
                    {**body, "quotas": db_quotas(4 * GIB)},
                )
            self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
            calls = len(self.calls)
            model = project.dispatch(
                "GET", f"/v1/storage/{self.resource.resource_id}", {}, None
            ).body
            self.assertEqual(len(self.calls), calls)
            self.assertEqual(model["quotas"], body["quotas"])
            self.assertFalse(model["usage"]["stale"])
            self.assertFalse({"providerId", "providerName", "usageError"} & model.keys())
            admin = privileged.dispatch("GET", "/v1/admin/storage", {}, None).body["items"][0]
            self.assertEqual(admin["providerId"], DATABASE)
            self.assertIn("usageError", admin)
        finally:
            api.close()

    def test_migration_preserves_authoritative_limits_and_starts_with_unknown_usage(self):
        prior = db.connect(self.root / "previous.sqlite3")
        try:
            db.migrate(prior, target_version=6)
            db.put_application(
                prior,
                application_id=APP_ID,
                application_slug="existing",
                worker_flavor="small",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=1024,
            )
            identifier = str(uuid.uuid4())
            # Seed the previous schema directly; the current reader requires v7.
            with db.transaction(prior):
                prior.execute(
                    "INSERT INTO managed_resources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        APP_ID,
                        "postgres",
                        "default",
                        "existing",
                        DATABASE,
                        DATABASE,
                        "active",
                        37,
                        7 * MIB,
                        None,
                        None,
                        None,
                        now(),
                        now(),
                    ),
                )
            db.migrate(prior)
            db.migrate(prior)
            resource = db.get_managed_resource(prior, identifier)
            self.assertEqual(limits.quotas(resource), db_quotas(7 * MIB, connections=37))
            self.assertTrue(limits.usage_model(resource)["stale"])
            self.assertIsNone(limits.usage_model(resource)["measuredAt"])
            self.assertFalse(resource.write_blocked)
            self.assertIsNone(db.get_storage_host_usage(prior))
        finally:
            prior.close()

    def test_repair_resumes_completed_resources_and_uses_row_quotas(self):
        # Two independent resources demonstrate checkpointed whole-platform repair.
        for name in ("first", "second"):
            db.put_managed_resource(
                self.connection,
                application_id=APP_ID,
                resource_type="postgres",
                resource_name=name,
                provider_id=name,
                provider_name=name,
                lifecycle_state="active",
                postgres_connections=23,
                measured_target_bytes=2 * MIB,
            )
        calls = []
        fail = True

        def helper(config, action, args, **bounds):
            calls.append(args)
            if args["resourceName"] == "second" and fail:
                raise RuntimeError("response lost")
            return {"applied": True}

        service = limits.StorageLimitsService(
            self.connection, self.config, self.root, helper_caller=helper
        )
        key = str(uuid.uuid4())
        with self.assertRaises(RuntimeError):
            service.repair_postgres(request_id=key)
        self.assertEqual(len(db.get_operation(self.connection, key).refs["completed"]), 1)
        fail = False
        service.repair_postgres(request_id=key)
        self.assertEqual([item["resourceName"] for item in calls], ["first", "second", "second"])
        self.assertTrue(all(item["quotas"]["connections"] == 23 for item in calls))
        self.assertEqual(db.get_operation(self.connection, key).status, "succeeded")


class HostLimitsTests(unittest.TestCase):
    def test_host_authentication_precedes_any_observation_and_only_fixed_route_is_served(self):
        request = mock.Mock()
        request.path = STATUS_PATH
        request.headers = {"Authorization": "Bearer wrong"}
        with mock.patch("infra.monitor.storage_host.snapshot") as observe:
            handler("admin-token", Path("/data"), ["postgres"]).do_GET(request)
        request.send_error.assert_called_once_with(403)
        observe.assert_not_called()
        request.reset_mock()
        request.path = "/anything"
        handler("admin-token", Path("/data"), ["postgres"]).do_GET(request)
        request.send_error.assert_called_once_with(404)

    def test_repeated_host_samples_do_not_duplicate_instance_inventory(self):
        from infra.monitor.storage_host import snapshot

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identifier = str(uuid.uuid4())
            config = root / "instances" / identifier / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"quotas": {"memoryBytes": 512 * MIB}}))
            names = ["example-" + kind for kind in ("postgres", "mongodb", "garage", "registry")]
            with mock.patch(
                "infra.monitor.storage_host.subprocess.run",
                return_value=SimpleNamespace(stdout=b"[]"),
            ):
                first = snapshot(root, names)
                second = snapshot(root, names)
            self.assertEqual(len(names), 4)
            self.assertEqual(first["containers"], second["containers"])
            self.assertEqual(len(second["containers"]), 5)

    def test_global_capacity_memory_disk_and_write_block_alarm_thresholds(self):
        host = {
            "postgresConnections": {"current": 320, "limit": 400},
            "mongoConnections": {"current": 800, "limit": 1000},
            "memory": {"totalBytes": 100, "availableBytes": 10},
            "dataVolume": {"totalBytes": 100, "usedBytes": 85},
            "containers": [{"usedBytes": 90, "limitBytes": 100}],
            "writeBlockedResources": 1,
        }
        self.assertEqual(
            set(alarms(host)),
            {
                "postgres_connections_high",
                "mongo_connections_high",
                "storage_volume_high",
                "storage_memory_high",
                "storage_container_memory_high",
                "storage_write_blocked",
            },
        )
        host.update(
            postgresConnections={"current": 319, "limit": 400},
            mongoConnections={"current": 799, "limit": 1000},
            memory={"totalBytes": 100, "availableBytes": 11},
            dataVolume={"totalBytes": 100, "usedBytes": 84},
            containers=[],
            writeBlockedResources=0,
        )
        self.assertEqual(alarms(host), [])
        host.update(
            cpuCount=4,
            loadAverage=[0.1, 0.2, 0.3],
            containers=[
                {
                    "name": "postgres",
                    "usedBytes": 10,
                    "limitBytes": 20,
                    "unexpected": "provider-secret",
                }
            ],
        )
        host["memory"]["unexpected"] = "provider-secret"
        host["unexpected"] = "provider-secret"
        projected = host_projection(host, {"postgres"})
        self.assertNotIn("provider-secret", repr(projected))
        with self.assertRaises(providers.HelperActionError):
            host_projection(host, {"mongo"})

    def test_repair_cli_polls_the_same_privileged_operation(self):
        key = str(uuid.uuid4())
        connection = mock.Mock()
        connection.getresponse.side_effect = [
            mock.Mock(status=202, read=lambda n: ('{"operationId":"' + key + '"}').encode()),
            mock.Mock(status=200, read=lambda n: b'{"status":"succeeded"}'),
        ]
        with mock.patch.object(
            storage_repair, "UnixConnection", return_value=connection
        ) as connect:
            storage_repair.repair(
                Path("/run/example-controller/privileged.sock"), key, seconds=30, output=mock.Mock()
            )
        self.assertEqual(connect.call_args.args[0], Path("/run/example-controller/privileged.sock"))
        self.assertEqual(
            connection.request.call_args_list,
            [
                mock.call(
                    "POST", "/v1/admin/storage/repair-postgres", headers={"Idempotency-Key": key}
                ),
                mock.call("GET", f"/v1/admin/operations/{key}", headers={}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
