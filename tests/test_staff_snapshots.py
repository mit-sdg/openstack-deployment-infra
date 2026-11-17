"""Staff database snapshots: helper files and storage, controller operations and links."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from openstack_platform import remote
from openstack_platform.controller import database as db
from openstack_platform.controller import storage_exports
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.async_operations import AsyncOperationExecutor
from openstack_platform.controller.http import HttpError
from openstack_platform.helper import production, staff_exports
from openstack_platform.helper.main import HelperActionError
from tests import test_controller_api as controller_fixtures

APP_ID = "11111111-1111-4111-8111-111111111111"
RESOURCE_ID = "22222222-2222-4222-8222-222222222222"
INSTANCE_ID = "33333333-3333-4333-8333-333333333333"
DATABASE = "p_11111111111141118111"
TAKEN = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
KEY_ID = "GK" + "a" * 24
SECRET = "b" * 64


def export_args(**changes: Any) -> dict[str, Any]:
    return {
        "applicationId": APP_ID,
        "applicationSlug": "demo-app",
        "resourceId": RESOURCE_ID,
        "resourceName": "default",
        "displayLabel": "Main data",
        "providerName": DATABASE,
        "instanceId": INSTANCE_ID,
        "deadlineAt": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        **changes,
    }


def script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o700)
    return path


class FakeCollection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def find(self, _filter: dict[str, Any], **_options: Any) -> list[dict[str, Any]]:
        return list(self.documents)


class FakeDatabase:
    def __init__(self, collections: dict[str, list[dict[str, Any]]], views=()) -> None:
        self.collections = collections
        self.views = list(views)

    def list_collections(self) -> list[dict[str, Any]]:
        return [{"name": name, "type": "collection"} for name in self.collections] + [
            {"name": name, "type": "view"} for name in self.views
        ]

    def __getitem__(self, name: str) -> FakeCollection:
        return FakeCollection(self.collections[name])


class FakeGarage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, Any]] = []
        self.buckets: dict[str, str] = {}
        self.keys: dict[str, str] = {}

    def request(self, path: str, body: Any = None, query: Any = None) -> Any:
        self.calls.append((path, body, query))
        if path == "/ListBuckets":
            return [{"id": ident, "globalAliases": [name]} for name, ident in self.buckets.items()]
        if path == "/CreateBucket":
            self.buckets[body["globalAlias"]] = "e" * 64
            return {"id": "e" * 64}
        if path == "/ListKeys":
            return [{"accessKeyId": key, "name": name} for key, name in self.keys.items()]
        if path == "/CreateKey":
            key = f"GK{len(self.keys):024x}"
            self.keys[key] = body["name"]
            return {"accessKeyId": key, "secretAccessKey": SECRET}
        if path == "/DeleteKey":
            self.keys.pop(query["id"], None)
        return None

    def paths(self) -> list[str]:
        return [call[0] for call in self.calls]


class MissingObject(Exception):
    response = {"Error": {"Code": "404"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.presigned: list[dict[str, Any]] = []

    def put_object(self, **values: Any) -> None:
        body = values.pop("Body").read()
        self.objects[values["Key"]] = {
            **values,
            "body": body,
            "LastModified": datetime.now(UTC),
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise MissingObject()
        item = self.objects[Key]
        return {"ContentLength": len(item["body"]), "LastModified": item["LastModified"]}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, _name: str) -> Any:
        objects = self.objects

        class Pages:
            def paginate(self, **_values: Any) -> list[dict[str, Any]]:
                return [
                    {
                        "Contents": [
                            {"Key": key, "LastModified": item["LastModified"]}
                            for key, item in objects.items()
                        ]
                    }
                ]

        return Pages()

    def generate_presigned_url(self, operation: str, **values: Any) -> str:
        self.presigned.append({"operation": operation, **values})
        return "https://s3.example.test/staff-exports-test/" + values["Params"]["Key"] + "?sig"


class HelperExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_request_needs_exact_args_and_the_database_of_the_named_app(self) -> None:
        request = staff_exports.export_request(export_args(), "mongo")
        self.assertEqual(request.database, DATABASE)
        self.assertEqual(request.application_slug, "demo-app")
        with self.assertRaises(HelperActionError) as extra:
            staff_exports.export_request({**export_args(), "path": "/"}, "mongo")
        self.assertEqual(extra.exception.code, "INVALID_ARGS")
        with self.assertRaises(HelperActionError) as foreign:
            staff_exports.export_request(export_args(providerName="p_" + "f" * 20), "postgres")
        self.assertEqual(foreign.exception.code, "IDENTITY_MISMATCH")
        # A named resource has its own database, never the default one.
        with self.assertRaises(HelperActionError) as named:
            staff_exports.export_request(export_args(resourceName="second"), "mongo")
        self.assertEqual(named.exception.code, "IDENTITY_MISMATCH")
        with self.assertRaises(HelperActionError):
            staff_exports.export_request(export_args(), "s3")

    def test_bucket_stays_outside_the_prefix_that_backups_copy(self) -> None:
        self.assertEqual(staff_exports.bucket_name("61040", "sdg"), "staff-exports-61040")
        with self.assertRaises(HelperActionError):
            staff_exports.bucket_name("61040", "staff")

    def test_collections_become_relaxed_json_lines_with_a_manifest_entry(self) -> None:
        from bson import ObjectId

        created = ObjectId("65a0000000000000000000aa")
        database = FakeDatabase(
            {
                "users": [{"_id": created, "at": datetime(2026, 11, 1, tzinfo=UTC), "n": 5}],
                "Users": [{"_id": 1}],
                "odd name$": [],
                "system.profile": [{"_id": 2}],
            },
            views=["report"],
        )
        files = staff_exports.write_collections(
            database, self.root, staff_exports.Budget(), time.monotonic() + 60
        )
        by_collection = {item["collection"]: item for item in files}
        self.assertEqual(set(by_collection), {"users", "Users", "odd name$"})
        line = (self.root / by_collection["users"]["name"]).read_text().strip()
        self.assertEqual(
            json.loads(line),
            {"_id": {"$oid": str(created)}, "at": {"$date": "2026-11-01T00:00:00Z"}, "n": 5},
        )
        self.assertEqual(by_collection["users"]["documents"], 1)
        names = [item["name"] for item in files]
        self.assertEqual(len({name.lower() for name in names}), 3)
        self.assertTrue(by_collection["odd name$"]["name"].startswith("collections/collection-"))
        mode = stat.S_IMODE((self.root / by_collection["users"]["name"]).stat().st_mode)
        self.assertEqual(mode, 0o600)
        (self.root / "second").mkdir()
        with self.assertRaises(HelperActionError) as large:
            staff_exports.write_collections(
                FakeDatabase({"big": [{"_id": i} for i in range(100)]}),
                self.root / "second",
                staff_exports.Budget(50),
                time.monotonic() + 60,
            )
        self.assertEqual(large.exception.code, "EXPORT_TOO_LARGE")

    def test_dump_output_streams_to_a_private_file_within_its_budget(self) -> None:
        self.root.joinpath("tools").mkdir()
        tool = script(self.root / "tools", "dump", 'printf "%s" "$SNAPSHOT_TEXT"\n')
        path = self.root / "out.sql"
        staff_exports.run_dump(
            [str(tool)],
            env={"SNAPSHOT_TEXT": "create table x;", "PATH": "/usr/bin:/bin"},
            path=path,
            budget=staff_exports.Budget(),
            deadline=time.monotonic() + 30,
        )
        self.assertEqual(path.read_text(), "create table x;")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        failing = script(self.root / "tools", "fails", "exit 3\n")
        with self.assertRaises(HelperActionError) as failed:
            staff_exports.run_dump(
                [str(failing)],
                env={},
                path=self.root / "failed",
                budget=staff_exports.Budget(),
                deadline=time.monotonic() + 30,
            )
        self.assertEqual(failed.exception.code, "EXPORT_FAILED")
        endless = script(self.root / "tools", "endless", "exec yes\n")
        started = time.monotonic()
        with self.assertRaises(HelperActionError) as large:
            staff_exports.run_dump(
                [str(endless)],
                env={"PATH": "/usr/bin:/bin"},
                path=self.root / "large",
                budget=staff_exports.Budget(4 * 1024**2),
                deadline=time.monotonic() + 30,
            )
        self.assertEqual(large.exception.code, "EXPORT_TOO_LARGE")
        slow = script(self.root / "tools", "slow", "exec sleep 30\n")
        with self.assertRaises(HelperActionError) as late:
            staff_exports.run_dump(
                [str(slow)],
                env={"PATH": "/usr/bin:/bin"},
                path=self.root / "slow",
                budget=staff_exports.Budget(),
                deadline=time.monotonic() + 0.5,
            )
        self.assertEqual(late.exception.code, "DEADLINE_EXPIRED")
        self.assertLess(time.monotonic() - started, 20)

    def test_archive_holds_the_files_and_a_manifest_with_their_checksums(self) -> None:
        request = staff_exports.export_request(export_args(), "mongo")
        (self.root / "collections").mkdir()
        (self.root / "collections/users.jsonl").write_text('{"_id": 1}\n')
        (self.root / "mongodump.archive.gz").write_bytes(b"\x1f\x8b archive")
        files = [
            staff_exports.describe(
                self.root, "collections/users.jsonl", collection="users", documents=1
            ),
            staff_exports.describe(self.root, "mongodump.archive.gz"),
        ]
        summary = staff_exports.manifest(request, TAKEN, files)
        archive = self.root / "snapshot.zip"
        staff_exports.build_archive(self.root, files, summary, archive)
        with zipfile.ZipFile(archive) as opened:
            self.assertEqual(
                sorted(opened.namelist()),
                ["MANIFEST.json", "collections/users.jsonl", "mongodump.archive.gz"],
            )
            self.assertEqual(
                opened.getinfo("mongodump.archive.gz").compress_type, zipfile.ZIP_STORED
            )
            stored = json.loads(opened.read("MANIFEST.json"))
            self.assertEqual(opened.read("collections/users.jsonl"), b'{"_id": 1}\n')
        self.assertEqual(stored["app"], "demo-app")
        self.assertEqual(stored["resourceId"], RESOURCE_ID)
        self.assertEqual(stored["databaseType"], "mongo")
        self.assertEqual(stored["takenAt"], "2026-11-17T12:00:00Z")
        self.assertEqual(stored["files"][0]["documents"], 1)
        self.assertEqual(len(stored["files"][1]["sha256"]), 64)
        self.assertFalse((self.root / "mongodump.archive.gz").exists())
        self.assertEqual(
            staff_exports.download_name(request, TAKEN), "demo-app-main-data-20261117T120000Z.zip"
        )
        key = staff_exports.object_key(RESOURCE_ID, TAKEN)
        self.assertEqual(staff_exports.checked_object_key(key), key)
        for bad in ("exports/../x.zip", f"exports/{RESOURCE_ID}/x.zip", None):
            with self.assertRaises(HelperActionError):
                staff_exports.checked_object_key(bad)

    def test_store_is_created_once_with_a_private_key_scoped_to_its_bucket(self) -> None:
        garage = FakeGarage()
        garage.keys["GK" + "0" * 22 + "99"] = "platform-staff-exports-test"
        key_file = self.root / "key.env"
        arguments = dict(
            bucket="staff-exports-test",
            key_name="platform-staff-exports-test",
            key_file=key_file,
            lock_file=self.root / "lock",
        )
        first = staff_exports.ensure_store(garage, **arguments)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(staff_exports.read_key(key_file), first)
        # A key whose secret was never stored is replaced.
        self.assertNotIn("GK" + "0" * 22 + "99", garage.keys)
        grants = [body for path, body, _query in garage.calls if path == "/AllowBucketKey"]
        self.assertEqual(grants[-1]["permissions"], {"read": True, "write": True, "owner": False})
        quotas = [body for path, body, _query in garage.calls if path == "/UpdateBucket"]
        self.assertEqual(quotas[-1]["quotas"]["maxSize"], staff_exports.BUCKET_QUOTA_BYTES)
        garage.calls.clear()
        self.assertEqual(staff_exports.ensure_store(garage, **arguments), first)
        self.assertNotIn("/CreateKey", garage.paths())
        self.assertNotIn("/CreateBucket", garage.paths())
        key_file.chmod(0o644)
        with self.assertRaises(HelperActionError):
            staff_exports.read_key(key_file)

    def test_sweep_and_links_honour_the_one_day_lifetime(self) -> None:
        client = FakeS3()
        fresh = staff_exports.object_key(RESOURCE_ID, TAKEN)
        old = staff_exports.object_key(RESOURCE_ID, TAKEN - timedelta(days=2))
        client.objects[fresh] = {"body": b"zip", "LastModified": datetime.now(UTC)}
        client.objects[old] = {
            "body": b"zip",
            "LastModified": datetime.now(UTC) - timedelta(hours=25),
        }
        self.assertEqual(staff_exports.sweep(client, "bucket", datetime.now(UTC)), 1)
        self.assertEqual(client.deleted, [old])
        public = FakeS3()
        now = datetime.now(UTC)
        result = staff_exports.link(client, public, "bucket", fresh, now)
        self.assertEqual(
            public.presigned,
            [
                {
                    "operation": "get_object",
                    "Params": {"Bucket": "bucket", "Key": fresh},
                    "ExpiresIn": 300,
                }
            ],
        )
        self.assertEqual(result["expiresAt"], staff_exports.timestamp(now + timedelta(minutes=5)))
        for key, when in ((old, now), (fresh, now + timedelta(hours=25))):
            with self.assertRaises(HelperActionError) as gone:
                staff_exports.link(client, public, "bucket", key, when)
            self.assertEqual(gone.exception.code, "EXPORT_NOT_FOUND")


class ProductionExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        tools = self.root / "tools"
        tools.mkdir()
        self.argv_log = self.root / "argv.log"
        # The fake tools record their arguments and the password they were given.
        script(
            tools,
            "pg_dump",
            f'echo "$@ PGPASSWORD=$PGPASSWORD" > {self.argv_log}\nprintf "create table x;"\n',
        )
        script(
            tools,
            "mongodump",
            f'echo "$@" > {self.argv_log}\nprintf "archive"\n',
        )
        self.enterContext(mock.patch.dict(os.environ, {"PLATFORM_DATABASE_TOOLS": str(tools)}))
        self.runtime = SimpleNamespace(
            root=self.root,
            admin_state=self.state,
            platform=SimpleNamespace(
                get=lambda name: {"internalNames.storage": "storage.internal"}[name],
                namespace="test",
                prefix="example",
                domain="example.test",
            ),
        )
        self.s3 = FakeS3()
        self.instances = mock.Mock()
        self.instances.call.side_effect = lambda action, _identifier: (
            {"type": self.kind}
            if action == "observe"
            else {"port": 30001, "adminPassword": "instance-secret"}
        )
        self.enterContext(
            mock.patch.object(production, "helper_runtime", return_value=self.runtime)
        )
        self.enterContext(mock.patch.object(production, "_nomad_secrets", return_value=self.root))
        self.enterContext(
            mock.patch.object(production, "_instance_client", return_value=self.instances)
        )
        self.enterContext(
            mock.patch.object(
                production,
                "_staff_export_clients",
                return_value=("staff-exports-test", self.s3, FakeS3()),
            )
        )

    def run_export(self, kind: str) -> dict[str, Any]:
        self.kind = kind
        handler = production.production_handlers()[f"storage.{kind}.export"]
        return dict(handler(export_args()))

    def test_postgres_snapshot_uploads_plain_sql_and_returns_metadata_only(self) -> None:
        result = self.run_export("postgres")
        self.assertEqual(set(result), {"objectKey", "filename", "bytes", "sha256", "takenAt"})
        stored = self.s3.objects[result["objectKey"]]
        self.assertEqual(stored["Bucket"], "staff-exports-test")
        self.assertEqual(
            stored["ContentDisposition"], f'attachment; filename="{result["filename"]}"'
        )
        self.assertEqual(stored["CacheControl"], "private, no-store")
        self.assertEqual(len(stored["body"]), result["bytes"])
        with zipfile.ZipFile(io.BytesIO(stored["body"])) as opened:
            self.assertEqual(opened.read("database.sql"), b"create table x;")
            summary = json.loads(opened.read("MANIFEST.json"))
        self.assertEqual(summary["database"], DATABASE)
        argv = self.argv_log.read_text()
        for flag in ("--no-owner", "--no-privileges", "--format=plain", DATABASE):
            self.assertIn(flag, argv)
        self.assertIn("PGPASSWORD=instance-secret", argv)
        self.assertNotIn("instance-secret", json.dumps(result))
        self.assertEqual(list((self.state / "controller/staff-exports/work").iterdir()), [])

    def test_mongo_snapshot_has_json_lines_and_an_archive_without_users(self) -> None:
        from bson import ObjectId

        database = FakeDatabase({"tests": [{"_id": ObjectId(), "who": "participant"}]})

        class Client:
            def __init__(self, *_args: Any, **options: Any) -> None:
                assert options["tls"] is True and options["authSource"] == "admin"

            def __getitem__(self, name: str) -> FakeDatabase:
                assert name == DATABASE
                return database

            def close(self) -> None:
                pass

        with mock.patch.dict(sys.modules, {"pymongo": SimpleNamespace(MongoClient=Client)}):
            result = self.run_export("mongo")
        with zipfile.ZipFile(io.BytesIO(self.s3.objects[result["objectKey"]]["body"])) as opened:
            self.assertEqual(
                sorted(opened.namelist()),
                ["MANIFEST.json", "collections/tests.jsonl", "mongodump.archive.gz"],
            )
            self.assertIn(b"participant", opened.read("collections/tests.jsonl"))
        argv = self.argv_log.read_text()
        self.assertIn(f"--db {DATABASE} --gzip --archive", argv)
        self.assertNotIn("--dumpDbUsersAndRoles", argv)
        self.assertNotIn("instance-secret", argv)

    def test_wrong_instance_type_or_missing_tools_stop_before_any_dump(self) -> None:
        self.kind = "mongo"
        handler = production.production_handlers()["storage.postgres.export"]
        with self.assertRaises(HelperActionError) as wrong:
            handler(export_args())
        self.assertEqual(wrong.exception.code, "IDENTITY_MISMATCH")
        with mock.patch.dict(os.environ, {"PLATFORM_DATABASE_TOOLS": str(self.root / "none")}):
            with self.assertRaises(HelperActionError) as missing:
                handler(export_args())
        self.assertEqual(missing.exception.code, "DEPENDENCY_UNAVAILABLE")
        self.assertFalse(self.argv_log.exists())
        self.assertEqual(self.s3.objects, {})

    def test_link_and_sweep_without_a_store_do_nothing(self) -> None:
        handlers = production.production_handlers()
        with mock.patch.object(production, "_staff_export_clients", return_value=None):
            self.assertEqual(handlers["storage.export.sweep"]({}), {"deleted": 0})
            with self.assertRaises(HelperActionError) as gone:
                handlers["storage.export.link"](
                    {"objectKey": staff_exports.object_key(RESOURCE_ID, TAKEN)}
                )
        self.assertEqual(gone.exception.code, "EXPORT_NOT_FOUND")
        with self.assertRaises(HelperActionError):
            handlers["storage.export.link"]({"objectKey": "exports/x", "extra": 1})


class ControllerExportTests(unittest.TestCase):
    # The controller API fixture, without also collecting its tests here.
    setUp_base = controller_fixtures.ControllerAPITests.setUp
    tearDown = controller_fixtures.ControllerAPITests.tearDown
    dispatch = controller_fixtures.ControllerAPITests.dispatch
    headers = staticmethod(controller_fixtures.ControllerAPITests.headers)
    create_application = controller_fixtures.ControllerAPITests.create_application

    def setUp(self) -> None:
        self.setUp_base()  # type: ignore[call-arg]
        self.api.close()
        self.release = threading.Event()
        self.release.set()
        self.helper_calls = []
        self.helper_result: Any = None

        def helper(_config: Any, action: str, values: Any, *, deadline: Any = None) -> Any:
            self.helper_calls.append((action, dict(values)))
            if action.endswith(".export"):
                self.release.wait(10)
                if isinstance(self.helper_result, Exception):
                    raise self.helper_result
                return self.helper_result or {
                    "objectKey": staff_exports_key(values["resourceId"]),
                    "filename": "demo-app-main-20261117T120000Z.zip",
                    "bytes": 4096,
                    "sha256": "c" * 64,
                    "takenAt": datetime.now(UTC).isoformat(),
                }
            if action == "storage.export.link":
                return {
                    "url": "https://s3.example.test/staff-exports-test/" + values["objectKey"],
                    "expiresAt": "2026-11-17T12:05:00Z",
                }
            raise AssertionError(f"unexpected helper action {action}")

        self.api = ControllerAPI(self.connection, self.config, self.root, helper_caller=helper)
        self.router = self.api.router("project")
        self.application = self.create_application().body["applicationId"]

    def resource(self, kind: str = "mongo", *, instance: bool = True, state: str = "active") -> str:
        created = db.put_managed_resource(
            self.connection,
            application_id=self.application,
            resource_type=kind,
            resource_name="default" if kind != "s3" else "files",
            provider_name="p_" + uuid.uuid4().hex[:20],
            lifecycle_state=state,
            display_label="Main",
        )
        if instance:
            db.set_storage_instance(self.connection, created.resource_id, str(uuid.uuid4()), 30001)
        return created.resource_id

    def export(self, resource: str, key: str | None = None) -> Any:
        return self.dispatch(
            "POST", f"/v1/storage/{resource}/exports", None, self.headers(key or str(uuid.uuid4()))
        )

    def status(self, resource: str, operation: str) -> dict[str, Any]:
        return self.dispatch("GET", f"/v1/storage/{resource}/exports/{operation}").body

    def test_export_runs_outside_the_app_scope_and_records_only_metadata(self) -> None:
        resource = self.resource()
        key = str(uuid.uuid4())
        self.release.clear()
        admitted = self.export(resource, key)
        self.assertEqual(admitted.status, 202)
        self.assertEqual(admitted.body["operationId"], key)
        self.assertEqual(self.status(resource, key)["status"], "running")
        # A deploy of the same app is not held up by the snapshot.
        db.begin_operation(
            self.connection,
            operation_id=str(uuid.uuid4()),
            kind="app.deploy",
            scope=f"app-{self.application}",
            phase="build",
            deadline_at=db.utc_now(),
        )
        with self.assertRaises(HttpError) as busy:
            self.export(self.resource("postgres"))
        self.assertEqual(busy.exception.code, "EXPORT_BUSY")
        self.release.set()
        self.api.wait_for_operations()
        self.assertEqual(self.export(resource, key).body["operationId"], key)
        action, values = self.helper_calls[0]
        self.assertEqual(action, "storage.mongo.export")
        self.assertEqual(
            set(values),
            {
                "applicationId",
                "applicationSlug",
                "resourceId",
                "resourceName",
                "displayLabel",
                "providerName",
                "instanceId",
                "deadlineAt",
            },
        )
        self.assertEqual(values["applicationSlug"], "demo-app")
        self.assertEqual(len(self.helper_calls), 1)
        status = self.status(resource, key)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(
            set(status["export"]), {"filename", "bytes", "sha256", "takenAt", "expiresAt"}
        )
        self.assertNotIn("objectKey", json.dumps(status))
        operation = self.dispatch("GET", f"/v1/operations/{key}").body
        self.assertEqual(operation["scope"], f"export-{resource}")
        self.assertNotIn("objectKey", json.dumps(operation))

    def test_link_is_fresh_public_and_tied_to_its_resource(self) -> None:
        resource = self.resource()
        key = str(uuid.uuid4())
        self.release.clear()
        self.export(resource, key)
        with self.assertRaises(HttpError) as early:
            self.dispatch("POST", f"/v1/storage/{resource}/exports/{key}/link")
        self.assertEqual(early.exception.code, "EXPORT_NOT_READY")
        self.release.set()
        self.api.wait_for_operations()
        link = self.dispatch("POST", f"/v1/storage/{resource}/exports/{key}/link").body
        self.assertTrue(link["url"].startswith("https://s3.example.test/"))
        self.assertEqual(link["sha256"], "c" * 64)
        self.assertEqual(self.helper_calls[-1][1], {"objectKey": staff_exports_key(resource)})
        other = self.resource("postgres")
        for method, path in (
            ("GET", f"/v1/storage/{other}/exports/{key}"),
            ("POST", f"/v1/storage/{other}/exports/{key}/link"),
        ):
            with self.assertRaises(HttpError) as foreign:
                self.dispatch(method, path)
            self.assertEqual(foreign.exception.code, "EXPORT_NOT_FOUND")
        with self.assertRaises(HttpError) as deploy:
            self.dispatch("GET", f"/v1/storage/{resource}/exports/{self.application}")
        self.assertEqual(deploy.exception.code, "EXPORT_NOT_FOUND")
        later = datetime.now(UTC) + timedelta(hours=25)
        with self.api._snapshot() as connection:
            self.assertEqual(
                storage_exports.status_model(connection, resource, key, now=later)["status"],
                "expired",
            )

        def outside(*_args: Any, **_kwargs: Any) -> Any:
            return {"url": "https://evil.example/x", "expiresAt": "2026-11-17T12:05:00Z"}

        with self.assertRaises(remote.ProtocolError):
            storage_exports.link(
                self.api._snapshot, self.config, resource, key, helper_caller=outside
            )

        def deleted(*_args: Any, **_kwargs: Any) -> Any:
            raise remote.HelperError("EXPORT_NOT_FOUND", "gone")

        with self.assertRaises(HttpError) as gone:
            storage_exports.link(
                self.api._snapshot, self.config, resource, key, helper_caller=deleted
            )
        self.assertEqual(gone.exception.code, "EXPORT_EXPIRED")

    def test_only_active_databases_on_their_own_instance_can_be_exported(self) -> None:
        for resource, code in (
            (self.resource("s3"), "EXPORT_UNSUPPORTED"),
            (self.resource("postgres", instance=False), "EXPORT_UNAVAILABLE"),
        ):
            with self.subTest(code=code), self.assertRaises(HttpError) as refused:
                self.export(resource)
            self.assertEqual(refused.exception.code, code)
        self.assertEqual(self.helper_calls, [])

    def test_failures_end_the_operation_with_an_actionable_code(self) -> None:
        resource = self.resource()
        for failure, code in (
            (remote.HelperError("EXPORT_TOO_LARGE", "big"), "EXPORT_TOO_LARGE"),
            (remote.HelperError("ACTION_FAILED", "details"), "EXPORT_FAILED"),
            ({"objectKey": staff_exports_key(str(uuid.uuid4()))}, "EXPORT_FAILED"),
        ):
            with self.subTest(code=code):
                self.helper_result = failure
                key = str(uuid.uuid4())
                self.export(resource, key)
                self.api.wait_for_operations()
                status = self.status(resource, key)
                self.assertEqual((status["status"], status["errorCode"]), ("failed", code))
                self.assertIsNone(status["export"])

    def stopped_export(self, resource: str, *, recovery: bool) -> str:
        """An export the controller stopped mid-way, as recorded before a restart."""
        identifier = str(uuid.uuid4())
        db.claim_idempotency_request(
            self.connection, request_id=identifier, request_fingerprint="0" * 64
        )
        db.enqueue_operation_dispatch(
            self.connection,
            operation_id=identifier,
            kind=storage_exports.EXPORT_KIND,
            scope=storage_exports.scope(resource),
        )
        db.begin_operation(
            self.connection,
            operation_id=identifier,
            kind=storage_exports.EXPORT_KIND,
            scope=storage_exports.scope(resource),
            phase="exporting",
            deadline_at=db.utc_now(),
        )
        if recovery:
            db.mark_recovery_required(self.connection, identifier, "stopped")
            db.set_operation_dispatch_status(self.connection, identifier, "recovery_required")
        return identifier

    def test_interrupted_exports_end_instead_of_waiting_for_recovery(self) -> None:
        resource = self.resource()
        stopped = self.stopped_export(resource, recovery=True)
        retry = str(uuid.uuid4())
        self.assertEqual(self.export(resource, retry).status, 202)
        self.api.wait_for_operations()
        self.assertEqual(self.status(resource, stopped)["status"], "failed")
        self.assertEqual(self.status(resource, retry)["status"], "succeeded")
        # A controller restart ends a running export the same way.
        running = self.stopped_export(resource, recovery=False)
        AsyncOperationExecutor(self.root / "platform.sqlite3", self.connection).close()
        operation = db.get_operation(self.connection, running)
        dispatch = db.get_operation_dispatch(self.connection, running)
        assert operation is not None and dispatch is not None
        self.assertEqual((operation.status, dispatch.status), ("failed", "finished"))

    def test_hourly_sweep_asks_the_helper_and_tolerates_failure(self) -> None:
        calls: list[str] = []

        def helper(_config: Any, action: str, _values: Any, **_bounds: Any) -> Any:
            calls.append(action)
            raise remote.DependencyUnavailable("down")

        with self.assertLogs("openstack_platform.controller.storage_exports", "WARNING"):
            storage_exports.sweep(self.config, helper)
        self.assertEqual(calls, ["storage.export.sweep"])


def staff_exports_key(resource: str) -> str:
    return f"exports/{resource}/20261117T120000Z-{'d' * 16}.zip"


if __name__ == "__main__":
    unittest.main()
