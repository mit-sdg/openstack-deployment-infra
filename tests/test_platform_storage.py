from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from platform_cli import db, remote, status
from platform_cli.config import load
from platform_cli.helper import production
from platform_cli.helper.main import HelperActionError
from platform_cli.helper.nomad import SecretItems, VariableSnapshot
from platform_cli.helper.storage import (
    S3_KEYS,
    ProviderCredential,
    RotationEvidence,
    handlers,
    mongo_create,
    mongo_environment,
    mongo_observe_handler,
    mongo_remove,
    mongo_rotate,
    mongo_verify,
    mongo_verify_handler,
    postgres_create,
    postgres_environment,
    postgres_observe_handler,
    postgres_rotate,
    postgres_verify,
    postgres_verify_handler,
    s3_create_handler,
    s3_environment,
    s3_observe_handler,
    s3_remove_handler,
    s3_rotate_handler,
    s3_verify,
    s3_verify_handler,
)
from platform_cli.storage import (
    StorageOperationError,
    create,
    remove,
    rotate,
    verify,
)
from platform_cli.storage_contract import (
    ENVIRONMENT_KEYS,
    FIXED_PLATFORM_ENVIRONMENT,
    PLATFORM_ENVIRONMENT_KEYS,
    platform_environment_values,
)
from platform_cli.validation import ValidationError

APP_ID = "11111111-1111-4111-8111-111111111111"
SENTINEL = "sentinel-storage-secret"


def config_fixture(directory: Path):
    platform = {
        "project": "example-project",
        "projectId": "00000000-0000-4000-8000-000000000000",
        "prefix": "example",
        "namespace": "app-platform",
        "domain": "apps.example.com",
        "datacenter": "example-dc",
        "region": "global",
        "network": "example-network",
        "hosts": {"admin": "example-admin"},
        "ports": {"admin": "example-admin-port"},
        "paths": {"root": "/srv/app-platform"},
    }
    policy = {
        "standard": {
            "workerFlavor": "example.1c2g",
            "cpuMHz": 1000,
            "memoryMiB": 2048,
            "postgresConnections": 10,
            "postgresMeasuredBytes": 2147483648,
            "mongoMeasuredBytes": 2147483648,
            "s3Bytes": 5368709120,
            "s3Objects": 100000,
        },
        "runtimeImages": {
            "bun": "registry.example/bun@sha256:" + "a" * 64,
            "node": "registry.example/node@sha256:" + "b" * 64,
        },
        "backupAgeRecipient": "age1" + "q" * 58,
    }
    platform_path = directory / "platform.json"
    policy_path = directory / "policy.json"
    platform_path.write_text(json.dumps(platform))
    platform_path.chmod(0o644)
    policy_path.write_text(json.dumps(policy))
    policy_path.chmod(0o600)
    return load(platform_path, policy_path)


class ManagementStorageTests(unittest.TestCase):
    def test_storage_type_aliases_are_not_accepted(self) -> None:
        from platform_cli import storage as management_storage

        with self.assertRaises(ValidationError):
            management_storage._selected(["mongodb"])
        with self.assertRaises(ValidationError):
            management_storage._selected(["object-storage"])

    def test_canonical_platform_environment_has_exact_node_runtime_mode(self) -> None:
        values = platform_environment_values(APP_ID, "demo-app", 8080)
        self.assertEqual(values["NODE_ENV"], "production")
        self.assertEqual(values["PLATFORM_ENV"], "production")
        self.assertEqual(values["PORT"], "8080")
        self.assertEqual(set(values), PLATFORM_ENVIRONMENT_KEYS)
        self.assertEqual(FIXED_PLATFORM_ENVIRONMENT["NODE_ENV"], "production")
        with self.assertRaises(ValidationError):
            platform_environment_values(APP_ID, "demo-app", 0)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config = config_fixture(self.directory)
        self.database_path = self.directory / "platform.sqlite3"
        self.connection = db.connect(self.database_path)
        db.migrate(self.connection)
        db.put_application(
            self.connection,
            application_id=APP_ID,
            application_slug="demo-app",
            worker_flavor="example.1c2g",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=2048,
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def add_resource(self, resource_type: str) -> None:
        values = {
            "postgres_connections": 10 if resource_type == "postgres" else None,
            "measured_target_bytes": 2147483648 if resource_type in {"postgres", "mongo"} else None,
            "s3_bytes": 5368709120 if resource_type == "s3" else None,
            "s3_objects": 100000 if resource_type == "s3" else None,
        }
        db.put_managed_resource(
            self.connection,
            application_id=APP_ID,
            resource_type=resource_type,
            provider_id=f"{resource_type}-id",
            provider_name=f"{resource_type}-name",
            lifecycle_state="active",
            **values,
        )
        db.set_environment_keys(
            self.connection,
            application_id=APP_ID,
            owner=resource_type,
            keys={"postgres": ["DATABASE_URL"], "mongo": ["MONGODB_URI"], "s3": ["S3_BUCKET"]}[
                resource_type
            ],
        )

    def test_create_partial_failure_records_truth_without_secret_material(self) -> None:
        actions: list[str] = []

        def caller(action, args, **bounds):
            actions.append(action)
            self.assertNotIn("class", args)
            if action == "storage.postgres.create":
                self.assertEqual(args["postgresConnections"], 10)
                return {
                    "providerId": "p_11111111111141118111",
                    "providerName": "p_11111111111141118111",
                    "verified": True,
                    "evidenceAccepted": True,
                }
            raise RuntimeError(SENTINEL)

        with self.assertRaisesRegex(
            StorageOperationError, "mongo creation was not confirmed"
        ) as raised:
            create(
                self.connection, self.config, APP_ID, ["postgres", "mongo"], helper_caller=caller
            )
        self.assertNotIn(SENTINEL, str(raised.exception))
        resources = {
            item.resource_type: item
            for item in db.list_managed_resources(self.connection, application_id=APP_ID)
        }
        self.assertEqual(resources["postgres"].lifecycle_state, "active")
        self.assertEqual(resources["mongo"].lifecycle_state, "recovery_required")
        self.assertEqual(actions, ["storage.postgres.create", "storage.mongo.create"])
        self.assertNotIn(SENTINEL.encode(), self.database_path.read_bytes())
        owners = {
            (item.key_name, item.owner)
            for item in db.list_environment_keys(self.connection, application_id=APP_ID)
        }
        self.assertIn(("PGPASSWORD", "postgres"), owners)
        operation = db.get_unfinished_operation(self.connection, f"app-{APP_ID}")
        self.assertEqual(operation.status, "recovery_required")  # type: ignore[union-attr]

    def test_same_create_invocation_recovers_automatically_with_same_operation(self) -> None:
        calls: list[tuple[str, bool]] = []

        def caller(action, args, **bounds):
            calls.append((args["operationId"], args["recover"]))
            if not args["recover"]:
                raise RuntimeError(SENTINEL)
            return {
                "providerId": "p_11111111111141118111",
                "providerName": "p_11111111111141118111",
                "verified": True,
                "evidenceAccepted": True,
                "modifyIndex": 12,
            }

        with self.assertRaisesRegex(
            StorageOperationError, "exact action: run openstack-platform storage create"
        ):
            create(self.connection, self.config, APP_ID, ["postgres"], helper_caller=caller)
        result = create(self.connection, self.config, APP_ID, ["postgres"], helper_caller=caller)
        self.assertEqual(result.completed, ("postgres",))
        self.assertEqual(calls, [(calls[0][0], False), (calls[0][0], True)])
        operation = db.get_operation(self.connection, calls[0][0])
        self.assertEqual(operation.status, "succeeded")  # type: ignore[union-attr]
        self.assertNotIn(SENTINEL.encode(), self.database_path.read_bytes())

    def test_different_storage_kind_refuses_unfinished_create(self) -> None:
        with self.assertRaises(StorageOperationError):
            create(
                self.connection,
                self.config,
                APP_ID,
                ["mongo"],
                helper_caller=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()),
            )
        calls = 0

        def caller(*args, **kwargs):
            nonlocal calls
            calls += 1
            return {}

        with self.assertRaises(db.UnfinishedOperationError):
            rotate(self.connection, self.config, APP_ID, ["mongo"], helper_caller=caller)
        self.assertEqual(calls, 0)

    def test_verify_and_remove_retry_their_recorded_phase(self) -> None:
        self.add_resource("postgres")
        verify_calls: list[bool] = []

        def verify_caller(action, args, **bounds):
            verify_calls.append(args["recover"])
            if not args["recover"]:
                raise RuntimeError(SENTINEL)
            return {"verified": True, "modifyIndex": 14}

        with self.assertRaises(StorageOperationError):
            verify(self.connection, self.config, APP_ID, ["postgres"], helper_caller=verify_caller)
        verified = verify(
            self.connection, self.config, APP_ID, ["postgres"], helper_caller=verify_caller
        )
        self.assertEqual(verified.completed, ("postgres",))
        self.assertEqual(verify_calls, [False, True])

        remove_calls: list[bool] = []

        def remove_caller(action, args, **bounds):
            remove_calls.append(args["recover"])
            if args["preflight"]:
                return {"preflightAccepted": True}
            if not args["recover"]:
                raise RuntimeError(SENTINEL)
            return {"confirmedAbsent": True, "environmentRemoved": True, "modifyIndex": 15}

        with self.assertRaises(StorageOperationError):
            remove(
                self.connection,
                self.config,
                APP_ID,
                ["postgres"],
                confirm_slug="demo-app",
                confirm_destructive=True,
                helper_caller=remove_caller,
            )
        removed = remove(
            self.connection,
            self.config,
            APP_ID,
            ["postgres"],
            confirm_slug="demo-app",
            confirm_destructive=True,
            helper_caller=remove_caller,
        )
        self.assertEqual(removed.completed, ("postgres",))
        self.assertEqual(remove_calls, [False, False, True, True])
        self.assertEqual(db.list_managed_resources(self.connection, application_id=APP_ID), [])

    def test_rotation_calls_only_selected_type_and_reports_rejected_evidence(self) -> None:
        self.add_resource("postgres")
        self.add_resource("mongo")
        actions: list[str] = []

        def caller(action, args, **bounds):
            actions.append(action)
            return {
                "providerId": "mongo-id",
                "providerName": "mongo-name",
                "credentialName": "u_candidate",
                "verified": True,
                "evidenceAccepted": False,
                "retired": False,
                "rolledBack": True,
            }

        result = rotate(self.connection, self.config, APP_ID, ["mongo"], helper_caller=caller)
        self.assertEqual(actions, ["storage.mongo.rotate"])
        self.assertEqual(result.completed, ())
        self.assertEqual(result.evidence_rejected, ("mongo",))
        operation = db.get_operation(self.connection, result.operation_id)  # type: ignore[arg-type]
        self.assertEqual(operation.status, "failed")  # type: ignore[union-attr]

    def test_removal_refuses_before_mutation_and_retains_keys_without_absence(self) -> None:
        self.add_resource("postgres")
        calls = 0

        def caller(action, args, **bounds):
            nonlocal calls
            calls += 1
            if args["preflight"]:
                return {"preflightAccepted": True}
            return {"confirmedAbsent": False, "environmentRemoved": False}

        with self.assertRaises(ValidationError):
            remove(
                self.connection,
                self.config,
                APP_ID,
                ["postgres"],
                confirm_slug="wrong-app",
                confirm_destructive=True,
                helper_caller=caller,
            )
        self.assertEqual(calls, 0)
        self.assertEqual(_resource(self.connection, "postgres").lifecycle_state, "active")

        with self.assertRaises(StorageOperationError):
            remove(
                self.connection,
                self.config,
                APP_ID,
                ["postgres"],
                confirm_slug="demo-app",
                confirm_destructive=True,
                helper_caller=caller,
            )
        self.assertEqual(calls, 2)
        self.assertEqual(
            _resource(self.connection, "postgres").lifecycle_state, "recovery_required"
        )
        keys = db.list_environment_keys(self.connection, application_id=APP_ID)
        self.assertEqual(
            [(item.key_name, item.owner) for item in keys], [("DATABASE_URL", "postgres")]
        )

    def test_policy_quotas_survive_verify_rotation_and_failure(self) -> None:
        self.add_resource("postgres")
        db.put_managed_resource(
            self.connection,
            application_id=APP_ID,
            resource_type="postgres",
            provider_id="postgres-id",
            provider_name="postgres-name",
            lifecycle_state="active",
            postgres_connections=17,
            measured_target_bytes=3_456_789_012,
            last_verified_at="2026-01-01T00:00:00Z",
        )
        verify(
            self.connection,
            self.config,
            APP_ID,
            ["postgres"],
            helper_caller=lambda *args, **kwargs: {"verified": True, "modifyIndex": 20},
        )
        seen_connections: list[int] = []

        def rejected_rotation(action, args, **bounds):
            seen_connections.append(args["postgresConnections"])
            return {
                "providerId": "postgres-id",
                "providerName": "postgres-name",
                "credentialName": "u_candidate",
                "verified": True,
                "evidenceAccepted": False,
                "retired": False,
                "rolledBack": True,
            }

        rotate(
            self.connection,
            self.config,
            APP_ID,
            ["postgres"],
            helper_caller=rejected_rotation,
        )
        resource = _resource(self.connection, "postgres")
        self.assertEqual(seen_connections, [10])
        self.assertEqual(resource.postgres_connections, 10)
        self.assertEqual(resource.measured_target_bytes, 2_147_483_648)
        self.assertIsNotNone(resource.last_verified_at)

    def test_status_calls_only_non_mutating_observation_action(self) -> None:
        self.add_resource("s3")
        actions: list[str] = []

        def caller(action, args, **bounds):
            actions.append(action)
            return {
                "observed": True,
                "modifyIndex": 8,
                "keyNames": list(ENVIRONMENT_KEYS["s3"]),
            }

        observed = status.storage_observer(
            self.connection,
            self.config,
            helper_caller=caller,
        )(APP_ID, "s3")
        self.assertEqual(observed.health, "healthy")
        model = status.storage_show(self.connection, APP_ID, "s3")
        assert model is not None
        self.assertEqual(model["providerId"], "s3-id")
        self.assertEqual(model["providerName"], "s3-name")
        self.assertEqual(actions, ["storage.s3.observe"])

    def test_status_rejects_noncanonical_or_secret_bearing_observation(self) -> None:
        self.add_resource("s3")

        def caller(action, args, **bounds):
            return {
                "observed": True,
                "modifyIndex": 8,
                "keyNames": list(ENVIRONMENT_KEYS["s3"]),
                "credential": SENTINEL,
            }

        model = status.storage_show(
            self.connection,
            APP_ID,
            "s3",
            observe=status.storage_observer(
                self.connection,
                self.config,
                helper_caller=caller,
            ),
        )
        assert model is not None
        self.assertFalse(model["live"]["available"])
        self.assertNotIn(SENTINEL, repr(model))

    def test_remove_preflights_all_types_before_first_delete(self) -> None:
        self.add_resource("postgres")
        self.add_resource("mongo")
        events: list[str] = []

        def caller(action, args, **bounds):
            resource_type = action.split(".")[1]
            events.append(f"{resource_type}-{'preflight' if args['preflight'] else 'delete'}")
            if args["preflight"]:
                return {"preflightAccepted": True}
            return {"confirmedAbsent": True, "environmentRemoved": True, "modifyIndex": 30}

        remove(
            self.connection,
            self.config,
            APP_ID,
            ["postgres", "mongo"],
            confirm_slug="demo-app",
            confirm_destructive=True,
            helper_caller=caller,
        )
        self.assertEqual(
            events,
            [
                "postgres-preflight",
                "mongo-preflight",
                "postgres-delete",
                "mongo-preflight",
                "mongo-delete",
            ],
        )

    def test_interrupted_remove_repreflights_newly_nonempty_s3_before_next_delete(self) -> None:
        for resource_type in ("postgres", "mongo", "s3"):
            self.add_resource(resource_type)
        events: list[str] = []
        interrupt_mongo = [True]
        s3_nonempty = [False]

        def caller(action, args, **bounds):
            resource_type = action.split(".")[1]
            event = f"{resource_type}-{'preflight' if args['preflight'] else 'delete'}"
            events.append(event)
            if args["preflight"]:
                if resource_type == "s3" and s3_nonempty[0]:
                    return {"preflightAccepted": False}
                return {"preflightAccepted": True}
            if resource_type == "mongo" and interrupt_mongo[0]:
                interrupt_mongo[0] = False
                raise RuntimeError(SENTINEL)
            return {"confirmedAbsent": True, "environmentRemoved": True, "modifyIndex": 31}

        with self.assertRaises(StorageOperationError):
            remove(
                self.connection,
                self.config,
                APP_ID,
                ["postgres", "mongo", "s3"],
                confirm_slug="demo-app",
                confirm_destructive=True,
                helper_caller=caller,
            )
        self.assertNotIn(
            "postgres",
            {
                item.resource_type
                for item in db.list_managed_resources(self.connection, application_id=APP_ID)
            },
        )

        s3_nonempty[0] = True
        retry_start = len(events)
        with self.assertRaisesRegex(StorageOperationError, "before another deletion"):
            remove(
                self.connection,
                self.config,
                APP_ID,
                ["postgres", "mongo", "s3"],
                confirm_slug="demo-app",
                confirm_destructive=True,
                helper_caller=caller,
            )
        retry_events = events[retry_start:]
        self.assertEqual(retry_events, ["mongo-preflight", "s3-preflight"])
        self.assertNotIn("mongo-delete", retry_events)
        operation = db.get_unfinished_operation(self.connection, f"app-{APP_ID}")
        assert operation is not None
        self.assertEqual(operation.status, "recovery_required")
        self.assertEqual(operation.refs["completed"], ["postgres"])
        self.assertNotIn(SENTINEL.encode(), self.database_path.read_bytes())

    def test_multi_type_calls_share_one_absolute_deadline(self) -> None:
        self.add_resource("postgres")
        self.add_resource("mongo")
        timeouts: list[float] = []

        def caller(action, args, **bounds):
            timeouts.append(bounds["timeout_seconds"])
            time.sleep(0.02)
            resource_type = action.split(".")[1]
            return {
                "providerId": f"{resource_type}-id",
                "providerName": f"{resource_type}-name",
                "credentialName": "u_candidate",
                "verified": True,
                "evidenceAccepted": True,
                "retired": True,
                "rolledBack": False,
            }

        deadline = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
        rotate(
            self.connection,
            self.config,
            APP_ID,
            ["postgres", "mongo"],
            helper_caller=caller,
            deadline_at=deadline,
            process_deadline=time.monotonic() + 2,
        )
        self.assertEqual(len(timeouts), 2)
        self.assertLess(timeouts[1], timeouts[0])
        self.assertLessEqual(timeouts[0], 2)

    def test_expired_recovery_uses_fresh_bounded_attempt_deadline_and_preserves_evidence(
        self,
    ) -> None:
        self.add_resource("postgres")
        operation_id = "66666666-6666-4666-8666-666666666666"
        original_deadline = "2000-01-01T00:00:00Z"
        db.begin_operation(
            self.connection,
            operation_id=operation_id,
            kind="storage.verify",
            scope=f"app-{APP_ID}",
            phase="verify_postgres",
            deadline_at=original_deadline,
            refs={
                "selected": ["postgres"],
                "completed": [],
                "current": "postgres",
                "original_modify_index": 41,
            },
        )
        calls: list[tuple[bool, float]] = []

        def caller(action, args, **bounds):
            calls.append((args["recover"], bounds["timeout_seconds"]))
            return {"verified": True, "modifyIndex": 42}

        result = verify(
            self.connection,
            self.config,
            APP_ID,
            ["postgres"],
            helper_caller=caller,
        )
        self.assertEqual(result.operation_id, operation_id)
        self.assertEqual(calls[0][0], True)
        self.assertGreater(calls[0][1], 0)
        self.assertLessEqual(calls[0][1], self.config.policy.limits.helper_seconds)
        operation = db.get_operation(self.connection, operation_id)
        assert operation is not None
        self.assertEqual(operation.deadline_at, original_deadline)
        self.assertEqual(operation.refs["original_modify_index"], 41)

    def test_confirmed_create_rollback_does_not_leave_recovery_metadata(self) -> None:
        def caller(*args, **kwargs):
            raise remote.HelperError("CREATE_ROLLED_BACK", "safe rollback")

        with self.assertRaisesRegex(StorageOperationError, "rollback was confirmed"):
            create(self.connection, self.config, APP_ID, ["s3"], helper_caller=caller)
        self.assertEqual(db.list_managed_resources(self.connection, application_id=APP_ID), [])
        operation = self.connection.execute(
            "SELECT status, cleanup_state FROM operations WHERE kind='storage.create'"
        ).fetchone()
        self.assertEqual((operation["status"], operation["cleanup_state"]), ("failed", "confirmed"))

    def test_management_protocol_and_lazy_production_compose_for_create(self) -> None:
        events: list[str] = []
        garage = Garage(events)
        nomad = MemoryNomad({"PORT": "3000"})
        action_map = handlers(
            postgres_admin=object(),
            postgres_connect=lambda **kwargs: None,
            mongo_admin=object(),
            mongo_connect=lambda **kwargs: None,
            garage_admin=garage,
            s3_connect=lambda access, secret: S3Client(events),
            nomad=nomad,
            storage_host="storage.internal",
            s3_endpoint="https://storage.internal:9000",
            prefix="example",
            observe_evidence=lambda app, slug, kind, index: RotationEvidence(True, index, True),
        )

        def protocol(action, args, **bounds):
            return production.production_handlers()[action](args)

        with mock.patch.object(
            production,
            "_storage_handlers",
            side_effect=lambda action: (action_map, ()),
        ):
            result = create(
                self.connection,
                self.config,
                APP_ID,
                ["s3"],
                helper_caller=protocol,
            )
        self.assertEqual(result.completed, ("s3",))
        self.assertEqual(_resource(self.connection, "s3").lifecycle_state, "active")
        self.assertIn("AWS_ACCESS_KEY_ID", nomad.items)


def _resource(connection, resource_type):
    return next(
        item
        for item in db.list_managed_resources(connection, application_id=APP_ID)
        if item.resource_type == resource_type
    )


class MemoryNomad:
    def __init__(self, items: dict[str, str]) -> None:
        self.items = SecretItems(items)
        self.index = 8
        self.writes: list[SecretItems] = []

    def read_variable(self, path: str) -> VariableSnapshot:
        return VariableSnapshot(path, self.index, self.items)

    def compare_and_set(self, path: str, expected_index: int, items) -> int:
        if expected_index != self.index:
            raise AssertionError("bad CAS")
        self.items = SecretItems(dict(items))
        self.writes.append(self.items)
        self.index += 1
        return self.index


class Body:
    def read(self):
        return b"ok"

    def close(self):
        pass


class S3Client:
    def __init__(self, events: list[str], objects: list[dict] | None = None) -> None:
        self.events = events
        self.objects = objects or []

    def put_object(self, **kwargs):
        self.events.append("verify-put")

    def get_object(self, **kwargs):
        return {"Body": Body()}

    def delete_object(self, **kwargs):
        self.events.append("verify-delete")

    def head_bucket(self, **kwargs):
        self.events.append("observe-head")

    def close(self):
        self.events.append("client-close")

    def get_paginator(self, name):
        client = self

        class Paginator:
            def paginate(self, **kwargs):
                return iter([{"Contents": client.objects}])

        return Paginator()

    def delete_objects(self, **kwargs):
        self.events.append("purge")


class NotFound(Exception):
    code = 404


class Garage:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.deleted: list[str] = []
        self.created_names: list[str] = []
        self.keys = {"old-key": "application-demo-app-00000000"}
        self.alias = "demo-bucket"
        self.absent = False

    def request(self, path, body=None, query=None):
        self.events.append(path)
        if path == "/CreateBucket":
            self.alias = body["globalAlias"]
            self.absent = False
            return {"id": "bucket-id"}
        if path == "/CreateKey":
            self.created_names.append(body["name"])
            self.keys["new-key"] = body["name"]
            return {"accessKeyId": "new-key", "secretAccessKey": SENTINEL}
        if path == "/ListKeys":
            return [{"accessKeyId": key_id, "name": name} for key_id, name in self.keys.items()]
        if path == "/DeleteKey":
            self.deleted.append(query["id"])
            self.keys.pop(query["id"], None)
            return None
        if path == "/DeleteBucket":
            self.absent = True
            return None
        if path == "/ListBuckets":
            return [] if self.absent else [{"id": "bucket-id", "globalAliases": [self.alias]}]
        if path == "/GetBucketInfo":
            if self.absent:
                raise NotFound()
            return {
                "id": "bucket-id",
                "globalAliases": [self.alias],
                "keys": [
                    {
                        "accessKeyId": key_id,
                        "permissions": {"read": True, "write": True, "owner": False},
                    }
                    for key_id in self.keys
                ],
            }
        return None


class HelperStorageTests(unittest.TestCase):
    def test_mongo_create_rejects_existing_database_and_remove_requires_owner_marker(self) -> None:
        database = "p_11111111111141118111"

        class MongoDatabase:
            def __init__(self, users=None, collections=None) -> None:
                self.users = list(users or [])
                self.collections = list(collections or [])
                self.commands: list[str] = []

            def command(self, name, *args, **kwargs):
                self.commands.append(name)
                if name == "usersInfo":
                    return {"users": self.users}
                return {}

            def list_collection_names(self):
                return self.collections

        class MongoAdmin:
            def __init__(self, names, database) -> None:
                self.names = list(names)
                self.database = database
                self.dropped = False

            def __getitem__(self, name):
                return self.database

            def list_database_names(self):
                return list(self.names)

            def drop_database(self, name):
                self.dropped = True

        existing = MongoAdmin([database], MongoDatabase(collections=["records"]))
        with self.assertRaisesRegex(HelperActionError, "already contains"):
            mongo_create(
                existing,
                application_id=APP_ID,
                host="storage.internal",
                measured_target_bytes=1,
                generation="abcdef12",
                operation_id="44444444-4444-4444-8444-444444444444",
            )
        self.assertFalse(existing.dropped)
        self.assertEqual(existing.database.commands, ["usersInfo"])

        foreign = MongoAdmin(
            [database],
            MongoDatabase(users=[{"user": "u_foreign", "customData": {"owner": "other"}}]),
        )
        with self.assertRaisesRegex(HelperActionError, "ownership marker"):
            mongo_remove(
                foreign,
                application_id=APP_ID,
                credential_name="u_foreign",
            )
        self.assertFalse(foreign.dropped)

    def test_rotation_rejects_uri_host_drift_before_provider_mutation(self) -> None:
        database = "p_11111111111141118111"
        old_user = "u_11111111111141118111_abcdef12"
        old_mongo = dict(mongo_environment("wrong.storage", database, old_user, SENTINEL))
        with self.assertRaisesRegex(HelperActionError, "identity"):
            mongo_rotate(
                object(),
                application_id=APP_ID,
                host="storage.internal",
                old_environment=old_mongo,
                generation="12345678",
                operation_id="44444444-4444-4444-8444-444444444444",
            )
        old_postgres = dict(postgres_environment("wrong.storage", database, old_user, SENTINEL))
        with self.assertRaisesRegex(HelperActionError, "identity"):
            postgres_rotate(
                object(),
                application_id=APP_ID,
                host="storage.internal",
                connections=1,
                old_environment=old_postgres,
                generation="12345678",
            )

    def initial(self):
        environment = dict(
            s3_environment("https://storage:9000", "demo-bucket", "old-key", "old-secret")
        )
        return {"PORT": "3000", "STAFF_SENTINEL": "preserve-me", **environment}

    def test_storage_key_contract_is_canonical_across_management_and_helper(self) -> None:
        from platform_cli import storage as management_storage
        from platform_cli.helper import storage as helper_storage

        self.assertIs(management_storage.ENVIRONMENT_KEYS, ENVIRONMENT_KEYS)
        self.assertIs(helper_storage.RESOURCE_KEYS, ENVIRONMENT_KEYS)
        self.assertEqual(helper_storage.S3_KEYS, ENVIRONMENT_KEYS["s3"])

    def test_verify_and_observe_reject_untrusted_storage_endpoints_before_credentials(self) -> None:
        database = "p_11111111111141118111"
        postgres_user = "u_11111111111141118111_abcdef12"
        postgres_nomad = MemoryNomad(
            dict(postgres_environment("attacker.storage", database, postgres_user, SENTINEL))
        )
        postgres_args = {
            "applicationId": APP_ID,
            "applicationSlug": "demo-app",
            "providerId": database,
            "providerName": database,
        }
        postgres_calls: list[object] = []
        with self.assertRaisesRegex(HelperActionError, "identity"):
            postgres_observe_handler(
                postgres_args,
                scoped_connect=lambda **kwargs: postgres_calls.append(kwargs),
                nomad=postgres_nomad,
                host="trusted.storage",
            )
        with self.assertRaisesRegex(HelperActionError, "identity"):
            postgres_verify_handler(
                {
                    **postgres_args,
                    "operationId": "44444444-4444-4444-8444-444444444444",
                    "recover": False,
                },
                scoped_connect=lambda **kwargs: postgres_calls.append(kwargs),
                nomad=postgres_nomad,
                host="trusted.storage",
            )
        self.assertEqual(postgres_calls, [])

        mongo_nomad = MemoryNomad(
            dict(mongo_environment("attacker.storage", database, postgres_user, SENTINEL))
        )
        mongo_args = {**postgres_args}
        mongo_calls: list[object] = []
        with self.assertRaisesRegex(HelperActionError, "identity"):
            mongo_observe_handler(
                mongo_args,
                scoped_connect=lambda **kwargs: mongo_calls.append(kwargs),
                nomad=mongo_nomad,
                host="trusted.storage",
            )
        with self.assertRaisesRegex(HelperActionError, "identity"):
            mongo_verify_handler(
                {
                    **mongo_args,
                    "operationId": "44444444-4444-4444-8444-444444444444",
                    "recover": False,
                },
                scoped_connect=lambda **kwargs: mongo_calls.append(kwargs),
                nomad=mongo_nomad,
                host="trusted.storage",
            )
        self.assertEqual(mongo_calls, [])

        s3_nomad = MemoryNomad(self.initial())
        s3_args = {
            "applicationId": APP_ID,
            "applicationSlug": "demo-app",
            "providerId": "bucket-id",
            "providerName": "demo-bucket",
        }
        s3_calls: list[object] = []
        garage = Garage([])
        with self.assertRaisesRegex(HelperActionError, "endpoint identity"):
            s3_observe_handler(
                s3_args,
                scoped_client=lambda access, secret: s3_calls.append((access, secret)),
                nomad=s3_nomad,
                admin=garage,
                endpoint="https://trusted.storage:9000",
            )
        with self.assertRaisesRegex(HelperActionError, "endpoint identity"):
            s3_verify_handler(
                {
                    **s3_args,
                    "operationId": "44444444-4444-4444-8444-444444444444",
                    "recover": False,
                },
                scoped_client=lambda access, secret: s3_calls.append((access, secret)),
                nomad=s3_nomad,
                admin=garage,
                endpoint="https://trusted.storage:9000",
            )
        self.assertEqual(s3_calls, [])

    def test_create_requires_public_restart_evidence_and_confirms_rollback(self) -> None:
        events: list[str] = []
        garage = Garage(events)
        nomad = MemoryNomad({"PORT": "3000"})
        observed: list[int] = []

        def evidence(app, slug, kind, index):
            observed.append(index)
            return RotationEvidence(True, index, public_healthy=False)

        with self.assertRaisesRegex(HelperActionError, "rollback was confirmed") as raised:
            s3_create_handler(
                {
                    "applicationId": APP_ID,
                    "applicationSlug": "demo-app",
                    "s3Bytes": 1000,
                    "s3Objects": 100,
                    "operationId": "44444444-4444-4444-8444-444444444444",
                    "recover": False,
                },
                admin=garage,
                scoped_client=lambda access, secret: S3Client(events),
                nomad=nomad,
                prefix="example",
                endpoint="https://storage:9000",
                observe_evidence=evidence,
            )
        self.assertEqual(raised.exception.code, "CREATE_ROLLED_BACK")
        self.assertEqual(len(observed), 1)
        self.assertTrue(garage.absent)
        self.assertEqual(dict(nomad.items), {"PORT": "3000"})
        self.assertLess(events.index("verify-delete"), events.index("/DeleteBucket"))

    def test_interrupted_create_never_cleans_ambiguous_or_nonempty_candidates(self) -> None:
        operation_id = "44444444-4444-4444-8444-444444444444"
        generation = hashlib.sha256(f"{operation_id}:storage.s3.create".encode()).hexdigest()[:8]
        events: list[str] = []
        garage = Garage(events)
        from platform_cli.helper import storage as helper_storage

        expected_bucket = helper_storage._s3_bucket_name(APP_ID, "demo-app", "example")
        garage.alias = expected_bucket
        garage.keys["candidate-key"] = f"application-demo-app-{generation}"
        with self.assertRaisesRegex(HelperActionError, "exact operation-owned"):
            s3_create_handler(
                {
                    "applicationId": APP_ID,
                    "applicationSlug": "demo-app",
                    "s3Bytes": 1000,
                    "s3Objects": 100,
                    "operationId": operation_id,
                    "recover": True,
                },
                admin=garage,
                scoped_client=lambda access, secret: S3Client(events),
                nomad=MemoryNomad({"PORT": "3000"}),
                prefix="example",
                endpoint="https://storage:9000",
                observe_evidence=lambda app, slug, kind, index: RotationEvidence(True, index),
            )
        self.assertEqual(garage.deleted, [])
        self.assertNotIn("/DeleteBucket", events)
        self.assertNotIn("/DeleteKey", events)

        database = "p_11111111111141118111"
        candidate = "u_11111111111141118111_abcdef12"

        class MongoDatabase:
            def command(self, name, *args, **kwargs):
                if name == "usersInfo":
                    return {
                        "users": [
                            {
                                "user": candidate,
                                "customData": {
                                    "m1PlatformOwner": APP_ID,
                                    "m1OperationId": operation_id,
                                    "m1CredentialGeneration": "abcdef12",
                                },
                            }
                        ]
                    }
                return {}

            def list_collection_names(self):
                return ["application-data"]

        class MongoAdmin:
            def __init__(self) -> None:
                self.dropped = False

            def __getitem__(self, name):
                return MongoDatabase()

            def list_database_names(self):
                return [database]

            def drop_database(self, name):
                self.dropped = True

        mongo_admin = MongoAdmin()
        with self.assertRaisesRegex(HelperActionError, "data before cleanup"):
            helper_storage._mongo_remove_created(
                mongo_admin,
                application_id=APP_ID,
                operation_id=operation_id,
                generation="abcdef12",
            )
        self.assertFalse(mongo_admin.dropped)

        class AmbiguousPostgres:
            def __init__(self) -> None:
                self.statements: list[str] = []

            def execute(self, statement, parameters=()):
                self.statements.append(statement)
                if statement.startswith("CREATE ROLE"):
                    raise RuntimeError("lost create response")
                return self

            def fetchone(self):
                return None

        postgres_admin = AmbiguousPostgres()
        with self.assertRaisesRegex(HelperActionError, "operation-owned roles"):
            postgres_create(
                postgres_admin,
                application_id=APP_ID,
                host="storage.internal",
                connections=1,
                measured_target_bytes=1,
                generation="abcdef12",
                operation_id=operation_id,
            )
        self.assertFalse(
            any(statement.startswith("DROP ") for statement in postgres_admin.statements)
        )

    def test_status_observation_is_read_only_while_verify_probe_cleans_up(self) -> None:
        events: list[str] = []
        garage = Garage(events)
        nomad = MemoryNomad(self.initial())
        result = s3_observe_handler(
            {
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
                "providerId": "bucket-id",
                "providerName": "demo-bucket",
            },
            scoped_client=lambda access, secret: S3Client(events),
            nomad=nomad,
            admin=garage,
            endpoint="https://storage:9000",
        )
        self.assertTrue(result["observed"])
        self.assertEqual(events, ["/GetBucketInfo", "observe-head", "client-close"])
        verified = s3_verify_handler(
            {
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
                "providerId": "bucket-id",
                "providerName": "demo-bucket",
                "operationId": "44444444-4444-4444-8444-444444444444",
                "recover": False,
            },
            scoped_client=lambda access, secret: S3Client(events),
            nomad=nomad,
            admin=garage,
            endpoint="https://storage:9000",
        )
        self.assertTrue(verified["verified"])
        self.assertEqual(events[-2:], ["verify-delete", "client-close"])
        self.assertEqual(nomad.writes, [])

    def test_s3_remove_refuses_provider_id_with_wrong_alias_before_delete(self) -> None:
        events: list[str] = []
        garage = Garage(events)
        garage.alias = "another-project"
        with self.assertRaisesRegex(HelperActionError, "identity does not match"):
            s3_remove_handler(
                {
                    "applicationId": APP_ID,
                    "applicationSlug": "demo-app",
                    "providerId": "bucket-id",
                    "providerName": "demo-bucket",
                    "confirmSlug": "demo-app",
                    "purge": False,
                    "preflight": True,
                    "operationId": "44444444-4444-4444-8444-444444444444",
                    "recover": False,
                },
                admin=garage,
                scoped_client=lambda access, secret: S3Client(events),
                nomad=MemoryNomad(self.initial()),
            )
        self.assertIn("/GetBucketInfo", events)
        self.assertNotIn("/DeleteBucket", events)

    def test_s3_rotation_rolls_back_on_unmatched_health_evidence_and_preserves_other_owners(
        self,
    ) -> None:
        events: list[str] = []
        garage = Garage(events)
        nomad = MemoryNomad(self.initial())
        result = s3_rotate_handler(
            {
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
                "providerId": "bucket-id",
                "providerName": "demo-bucket",
                "operationId": "44444444-4444-4444-8444-444444444444",
                "recover": False,
            },
            admin=garage,
            scoped_client=lambda access, secret: S3Client(events),
            nomad=nomad,
            endpoint="https://storage:9000",
            observe_evidence=lambda app, slug, kind, index: RotationEvidence(True, index - 1),
        )
        self.assertFalse(result["evidenceAccepted"])
        self.assertTrue(result["rolledBack"])
        self.assertEqual(nomad.items["AWS_ACCESS_KEY_ID"], "old-key")
        self.assertEqual(nomad.items["STAFF_SENTINEL"], "preserve-me")
        self.assertEqual(garage.deleted, ["new-key"])
        self.assertNotIn(SENTINEL, repr(result))
        self.assertNotIn(SENTINEL, repr(nomad.writes[-2]))

    def test_operation_id_makes_rotation_generation_deterministic_without_returning_secret(
        self,
    ) -> None:
        names: list[str] = []
        for _attempt in range(2):
            events: list[str] = []
            garage = Garage(events)
            result = s3_rotate_handler(
                {
                    "applicationId": APP_ID,
                    "applicationSlug": "demo-app",
                    "providerId": "bucket-id",
                    "providerName": "demo-bucket",
                    "operationId": "22222222-2222-4222-8222-222222222222",
                    "recover": False,
                },
                admin=garage,
                scoped_client=lambda access, secret, events=events: S3Client(events),
                nomad=MemoryNomad(self.initial()),
                endpoint="https://storage:9000",
                observe_evidence=lambda app, slug, kind, index: RotationEvidence(True, index),
            )
            names.extend(garage.created_names)
            self.assertNotIn(SENTINEL, repr(result))
        self.assertEqual(names[0], names[1])
        self.assertRegex(names[0], r"^application-demo-bucket-[a-f0-9]{8}$")

    def test_s3_rotation_retires_old_key_only_after_matching_health_evidence(self) -> None:
        events: list[str] = []
        garage = Garage(events)
        nomad = MemoryNomad(self.initial())

        def evidence(app, slug, kind, index):
            events.append("healthy-evidence")
            return RotationEvidence(True, index)

        result = s3_rotate_handler(
            {
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
                "providerId": "bucket-id",
                "providerName": "demo-bucket",
                "operationId": "44444444-4444-4444-8444-444444444444",
                "recover": False,
            },
            admin=garage,
            scoped_client=lambda access, secret: S3Client(events),
            nomad=nomad,
            endpoint="https://storage:9000",
            observe_evidence=evidence,
        )
        self.assertTrue(result["evidenceAccepted"])
        self.assertTrue(result["retired"])
        self.assertEqual(garage.deleted, ["old-key"])
        self.assertEqual(nomad.items["AWS_ACCESS_KEY_ID"], "new-key")
        self.assertLess(events.index("healthy-evidence"), events.index("/DeleteKey"))

    def test_s3_remove_checks_confirmation_and_absence_before_owned_key_removal(self) -> None:
        events: list[str] = []
        garage = Garage(events)
        nomad = MemoryNomad(self.initial())
        args = {
            "applicationId": APP_ID,
            "applicationSlug": "demo-app",
            "providerId": "bucket-id",
            "providerName": "demo-bucket",
            "confirmSlug": "wrong-app",
            "purge": False,
            "preflight": False,
            "operationId": "44444444-4444-4444-8444-444444444444",
            "recover": False,
        }
        with self.assertRaisesRegex(Exception, "confirmation"):
            s3_remove_handler(
                args, admin=garage, scoped_client=lambda a, s: S3Client(events), nomad=nomad
            )
        self.assertEqual(events, [])
        self.assertEqual(len(nomad.writes), 0)

        args["confirmSlug"] = "demo-app"
        result = s3_remove_handler(
            args,
            admin=garage,
            scoped_client=lambda a, s: S3Client(events),
            nomad=nomad,
        )
        self.assertTrue(result["confirmedAbsent"])
        self.assertTrue(result["environmentRemoved"])
        self.assertEqual(nomad.items["PORT"], "3000")
        self.assertEqual(nomad.items["STAFF_SENTINEL"], "preserve-me")
        for key in S3_KEYS:
            self.assertNotIn(key, nomad.items)
        self.assertLess(events.index("/GetBucketInfo"), len(events))
        self.assertEqual(len(nomad.writes), 1)

    def test_remove_recovery_cleans_accepted_key_after_bucket_disappeared(self) -> None:
        events: list[str] = []
        garage = Garage(events)
        garage.absent = True
        nomad = MemoryNomad(self.initial())
        result = s3_remove_handler(
            {
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
                "providerId": "bucket-id",
                "providerName": "demo-bucket",
                "confirmSlug": "demo-app",
                "purge": False,
                "preflight": False,
                "operationId": "33333333-3333-4333-8333-333333333333",
                "recover": True,
            },
            admin=garage,
            scoped_client=lambda access, secret: S3Client(events),
            nomad=nomad,
        )
        self.assertTrue(result["confirmedAbsent"])
        self.assertEqual(garage.deleted, ["old-key"])
        self.assertNotIn("old-key", repr(result))
        for key in S3_KEYS:
            self.assertNotIn(key, nomad.items)

    def test_scoped_verification_failures_cleanup_every_fixture(self) -> None:
        class Cursor:
            def __init__(self, row=None) -> None:
                self.row = row

            def fetchone(self):
                return self.row

        class PostgresConnection:
            def __init__(self) -> None:
                self.statements: list[str] = []
                self.closed = False

            def execute(self, statement, parameters=()):
                self.statements.append(statement)
                return Cursor((2,)) if statement.startswith("SELECT value") else Cursor()

            def close(self):
                self.closed = True

        postgres_connection = PostgresConnection()
        postgres_credential = ProviderCredential(
            "database",
            "database",
            "scoped-user",
            postgres_environment("storage", "database", "scoped-user", SENTINEL),
        )
        with self.assertRaisesRegex(RuntimeError, "PostgreSQL scoped verification failed"):
            postgres_verify(
                lambda **kwargs: postgres_connection,
                postgres_credential,
                host="storage",
            )
        self.assertIn("DROP TABLE IF EXISTS platform_access_check", postgres_connection.statements)
        self.assertTrue(postgres_connection.closed)

        class MongoCollection:
            def __init__(self) -> None:
                self.dropped = False

            def insert_one(self, document):
                return mock.Mock(inserted_id="fixture-id")

            def find_one(self, query):
                raise RuntimeError("bounded read failed")

            def drop(self):
                self.dropped = True

        collection = MongoCollection()
        mongo_client = mock.MagicMock()
        mongo_client.__getitem__.return_value.__getitem__.return_value = collection
        mongo_credential = ProviderCredential(
            "database",
            "database",
            "scoped-user",
            mongo_environment("storage", "database", "scoped-user", SENTINEL),
        )
        with self.assertRaisesRegex(RuntimeError, "bounded read failed"):
            mongo_verify(lambda **kwargs: mongo_client, mongo_credential, host="storage")
        self.assertTrue(collection.dropped)
        mongo_client.close.assert_called_once()

        events: list[str] = []

        class WrongBody(Body):
            def read(self):
                return b"wrong"

            def close(self):
                events.append("body-close")

        client = S3Client(events)
        client.get_object = lambda **kwargs: {"Body": WrongBody()}
        s3_credential = ProviderCredential(
            "bucket-id",
            "demo-bucket",
            "scoped-key",
            s3_environment("https://storage:9000", "demo-bucket", "scoped-key", SENTINEL),
        )
        with self.assertRaisesRegex(RuntimeError, "S3 scoped verification failed"):
            s3_verify(
                lambda access, secret: client,
                s3_credential,
                endpoint="https://storage:9000",
            )
        self.assertIn("verify-delete", events)
        self.assertIn("body-close", events)
        self.assertIn("client-close", events)

    def test_secret_carriers_are_redacted(self) -> None:
        credential = ProviderCredential(
            "provider-id",
            "provider-name",
            "credential-name",
            SecretItems({"PASSWORD": SENTINEL}),
        )
        self.assertNotIn(SENTINEL, repr(credential))
        self.assertNotIn(SENTINEL, repr(credential.environment))

    def test_production_create_observer_requires_fresh_scheduler_and_public_health(self) -> None:
        platform = mock.Mock(prefix="example")
        platform.get.return_value = "storage.internal"
        events: list[str] = []
        garage = Garage(events)
        nomad = MemoryNomad({"PORT": "3000"})

        def allocation(identifier: str) -> dict[str, object]:
            return {
                "ID": identifier,
                "ModifyIndex": 1,
                "ModifyTime": 1,
                "JobVersion": 7,
                "DesiredStatus": "run",
                "ClientStatus": "running",
                "DeploymentStatus": {"Healthy": True},
                "TaskStates": {"app": {"Restarts": 0, "StartedAt": identifier}},
            }

        boto3 = mock.Mock()
        botocore_config = mock.Mock()
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "boto3": boto3,
                    "botocore": mock.Mock(),
                    "botocore.config": botocore_config,
                },
            ),
            mock.patch.object(
                production,
                "helper_runtime",
                return_value=mock.Mock(platform=platform, root=Path("/srv/app-platform")),
            ),
            mock.patch.object(
                production,
                "_read_environment",
                return_value={
                    "POSTGRES_PASSWORD": "secret",
                    "MONGO_PASSWORD": "secret",
                    "GARAGE_ADMIN_TOKEN": "secret",
                },
            ),
            mock.patch.object(production, "_nomad_client", return_value=nomad),
            mock.patch.object(production, "_GarageAdmin", return_value=garage),
            mock.patch.object(production.ssl, "create_default_context", return_value=mock.Mock()),
            mock.patch.object(
                boto3, "client", side_effect=lambda *args, **kwargs: S3Client(events)
            ),
            mock.patch.object(
                production.app_actions,
                "_allocations",
                side_effect=[[allocation("old")], [allocation("new")]],
            ),
            mock.patch.object(production.app_actions, "_status", return_value={"Version": 7}),
            mock.patch.object(
                production.app_actions, "_public_health_from_job", return_value=True
            ) as public_health,
            mock.patch.object(production, "run", return_value=mock.Mock()) as runner,
        ):
            action_map, clients = production._storage_handlers("storage.s3.create")
            result = action_map["storage.s3.create"](
                {
                    "applicationId": APP_ID,
                    "applicationSlug": "demo-app",
                    "s3Bytes": 1000,
                    "s3Objects": 100,
                    "operationId": "55555555-5555-4555-8555-555555555555",
                    "recover": False,
                }
            )
        self.assertEqual(clients, ())
        self.assertTrue(result["evidenceAccepted"])
        self.assertTrue(any("restart" in call.args[0] for call in runner.call_args_list))
        public_health.assert_called_once()

    def test_production_initializes_only_required_backend_and_closes_partial_client(self) -> None:
        platform = mock.Mock(prefix="example")
        platform.get.return_value = "storage.internal"
        postgres = mock.Mock()
        psycopg = mock.Mock()
        with (
            mock.patch.dict(sys.modules, {"psycopg": psycopg}),
            mock.patch.object(
                production,
                "helper_runtime",
                return_value=mock.Mock(platform=platform, root=Path("/srv/app-platform")),
            ),
            mock.patch.object(
                production,
                "_read_environment",
                return_value={
                    "POSTGRES_PASSWORD": "secret",
                    "MONGO_PASSWORD": "secret",
                    "GARAGE_ADMIN_TOKEN": "secret",
                },
            ),
            mock.patch.object(production, "_nomad_client", return_value=MemoryNomad({})),
            mock.patch.object(psycopg, "connect", return_value=postgres) as connect,
        ):
            _handlers, clients = production._storage_handlers("storage.postgres.observe")
            self.assertEqual(clients, ())
            connect.assert_not_called()
            with mock.patch.object(
                production.storage_actions, "handlers", side_effect=RuntimeError("compose failed")
            ):
                with self.assertRaisesRegex(RuntimeError, "compose failed"):
                    production._storage_handlers("storage.postgres.create")
        postgres.close.assert_called_once()

    def test_fixed_handler_map_has_all_per_type_storage_actions(self) -> None:
        action_map = handlers(
            postgres_admin=object(),
            postgres_connect=lambda **kwargs: None,
            mongo_admin=object(),
            mongo_connect=lambda **kwargs: None,
            garage_admin=object(),
            s3_connect=lambda access, secret: None,
            nomad=MemoryNomad({}),
            storage_host="storage.internal",
            s3_endpoint="https://storage.internal:9000",
            prefix="example",
            observe_evidence=lambda app, slug, kind, index: RotationEvidence(True, index),
        )
        self.assertEqual(
            set(action_map),
            {
                f"storage.{resource_type}.{operation}"
                for resource_type in ("postgres", "mongo", "s3")
                for operation in ("create", "observe", "verify", "rotate", "remove")
            },
        )


if __name__ == "__main__":
    unittest.main()
