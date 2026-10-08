from __future__ import annotations

import copy
import io
import json
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from backup import emit_garage_backup as emit  # noqa: E402
from backup import garage_catalog as catalog  # noqa: E402
from backup import restore_garage_backup as restore  # noqa: E402

NAME = "test-demo-11111111"
OLD_ID, NEW_ID = "a" * 64, "b" * 64
BACKUP_KEY, APP_KEY = "GK" + "1" * 24, "GK" + "2" * 24
CREDS = {"GARAGE_BACKUP_ACCESS_KEY": BACKUP_KEY, "GARAGE_BACKUP_SECRET_KEY": "3" * 64}
APP_CREDENTIAL = {
    "accessKeyId": APP_KEY,
    "secretAccessKey": "4" * 64,
    "name": "application-" + NAME + "-12345678",
}


class Admin:
    def __init__(self, *, replacement=False):
        self.calls = []
        self.buckets = (
            {}
            if replacement
            else {
                OLD_ID: {
                    "id": OLD_ID,
                    "name": NAME,
                    "aliases": [NAME],
                    "quotas": {"maxSize": 1000, "maxObjects": 100},
                    "keys": [],
                }
            }
        )
        self.keys = (
            {}
            if replacement
            else {
                APP_KEY: APP_CREDENTIAL,
                BACKUP_KEY: {"accessKeyId": BACKUP_KEY, "name": "platform-backup"},
            }
        )
        self.permissions = (
            {}
            if replacement
            else {(OLD_ID, APP_KEY): {"read": True, "write": True, "owner": False}}
        )
        self.fail_revoke = False

    def request(self, action, body=None, **query):
        self.calls.append((action, copy.deepcopy(body), query))
        if action == "ListBuckets":
            return [
                {"id": identifier, "globalAliases": bucket["aliases"]}
                for identifier, bucket in self.buckets.items()
            ]
        if action == "GetBucketInfo":
            bucket = self.buckets[query["id"]]
            return {
                "id": query["id"],
                "quotas": bucket.get("quotas", {"maxSize": None, "maxObjects": None}),
                "keys": [
                    {"accessKeyId": key, "permissions": perms}
                    for (identifier, key), perms in self.permissions.items()
                    if identifier == query["id"]
                ],
            }
        if action in {"AllowBucketKey", "DenyBucketKey"}:
            pair = (body["bucketId"], body["accessKeyId"])
            if action == "DenyBucketKey" and body["permissions"]["write"] and self.fail_revoke:
                raise RuntimeError("revocation failed")
            perms = self.permissions.setdefault(
                pair, {"read": False, "write": False, "owner": False}
            )
            for name, value in body["permissions"].items():
                if value:
                    perms[name] = action == "AllowBucketKey"
            return None
        if action == "ListKeys":
            return [
                {"id": identifier, "name": key["name"]} for identifier, key in self.keys.items()
            ]
        if action == "GetKeyInfo":
            return self.keys[query["id"]]
        if action == "ImportKey":
            self.keys[body["accessKeyId"]] = body
            return body
        if action == "CreateBucket":
            self.buckets[NEW_ID] = {
                "id": NEW_ID,
                "name": body["globalAlias"],
                "aliases": [body["globalAlias"]],
            }
            return {"id": NEW_ID}
        if action == "UpdateBucket":
            self.buckets[query["id"]]["quotas"] = body["quotas"]
            return None
        if action == "UpdateKey":
            return None
        raise AssertionError(action)


class S3:
    def __init__(self, admin):
        self.admin = admin
        self.fail_read = False
        self.fail_put = False
        self.data = {(NAME, "notes/hello.txt"): b"hello"}
        self.closed_bodies = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, *, Bucket):
        if self.fail_read:
            raise RuntimeError("access denied")
        assert any(
            self.admin.permissions.get((identifier, BACKUP_KEY), {}).get("read")
            for identifier in self.admin.buckets
        )
        return [
            {
                "Contents": [
                    {"Key": key, "Size": len(data), "ETag": '"etag"'}
                    for (bucket, key), data in self.data.items()
                    if bucket == Bucket
                ]
            }
        ]

    def get_object(self, *, Bucket, Key, IfMatch):
        assert IfMatch == '"etag"'
        body = io.BytesIO(self.data[(Bucket, Key)])
        self.closed_bodies.append(body)
        return {"Body": body, "ContentLength": len(body.getvalue())}

    def put_object(self, *, Bucket, Key, Body, ContentLength):
        if self.fail_put:
            raise RuntimeError("write failed")
        assert self.admin.permissions[(NEW_ID, BACKUP_KEY)] == {
            "read": True,
            "write": True,
            "owner": False,
        }
        self.data[(Bucket, Key)] = Body.read()
        assert len(self.data[(Bucket, Key)]) == ContentLength

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": len(self.data[(Bucket, Key)])}


class GarageBackupTests(unittest.TestCase):
    def archive(self):
        admin = Admin()
        s3 = S3(admin)
        stream = io.BytesIO()
        emit.export_archive(admin, s3, "test", BACKUP_KEY, stream)
        stream.seek(0)
        return stream, admin, s3

    def database(self, root):
        path = root / "platform.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE managed_resources(resource_id TEXT, resource_type TEXT, provider_id TEXT, provider_name TEXT, last_verified_at TEXT)"
        )
        connection.execute(
            "INSERT INTO managed_resources VALUES ('app-storage','s3',?,?, 'yesterday')",
            (OLD_ID, NAME),
        )
        connection.commit()
        connection.close()
        path.chmod(0o600)
        return path

    def test_export_backfills_read_only_and_contains_objects_and_recovery_grants(self):
        stream, admin, s3 = self.archive()
        manifest = catalog.verify_archive(stream, admin=admin, prefix="test")
        self.assertEqual(manifest["buckets"], [NAME])
        self.assertEqual(manifest["objects"][0]["size"], 5)
        self.assertEqual(
            manifest["bucket_metadata"][0]["keys"],
            [{**APP_CREDENTIAL, "permissions": {"read": True, "write": True, "owner": False}}],
        )
        self.assertEqual(admin.permissions[(OLD_ID, BACKUP_KEY)], catalog.READ_ONLY)
        self.assertTrue(all(body.closed for body in s3.closed_bodies))
        self.assertFalse(
            any(
                body and body.get("permissions", {}).get("write")
                for action, body, _ in admin.calls
                if action == "AllowBucketKey"
            )
        )
        # A repeated backup repairs accidentally broad grants rather than merging
        # read onto write. Garage's false fields do not revoke permissions.
        admin.permissions[(OLD_ID, BACKUP_KEY)]["write"] = True
        emit.export_archive(admin, s3, "test", BACKUP_KEY, io.BytesIO())
        self.assertEqual(admin.permissions[(OLD_ID, BACKUP_KEY)], catalog.READ_ONLY)

    def test_unreadable_bucket_and_truncated_inventory_fail_backup(self):
        admin = Admin()
        s3 = S3(admin)
        s3.fail_read = True
        with self.assertRaises(RuntimeError):
            emit.export_archive(admin, s3, "test", BACKUP_KEY, io.BytesIO())
        with mock.patch.object(admin, "request", return_value=[{}] * 10000):
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                catalog.app_buckets(admin, "test")

    @staticmethod
    def make_archive(manifest, *, payload=b"", entry=None):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            data = json.dumps(manifest).encode()
            member = tarfile.TarInfo("manifest.json")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            if entry is not None:
                member = tarfile.TarInfo(entry)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        stream.seek(0)
        return stream

    def test_restore_recreates_app_key_and_grant_then_remaps_offline_database(self):
        stream, _, _ = self.archive()
        admin = Admin(replacement=True)
        s3 = S3(admin)
        s3.data = {}
        with tempfile.TemporaryDirectory() as temp:
            path = self.database(Path(temp))
            self.assertEqual(
                restore.restore_archive(admin, s3, "test", CREDS, stream, controller_database=path),
                (1, 1),
            )
            connection = sqlite3.connect(path)
            self.assertEqual(
                connection.execute(
                    "SELECT provider_id, last_verified_at FROM managed_resources"
                ).fetchone(),
                (NEW_ID, None),
            )
            connection.close()
        self.assertEqual(s3.data[(NAME, "notes/hello.txt")], b"hello")
        self.assertEqual(admin.keys[APP_KEY], APP_CREDENTIAL)
        self.assertEqual(
            admin.permissions[(NEW_ID, APP_KEY)], {"read": True, "write": True, "owner": False}
        )
        self.assertEqual(admin.permissions[(NEW_ID, BACKUP_KEY)], catalog.READ_ONLY)

    def test_failed_restore_still_revokes_backup_write_and_revoke_failure_is_fatal(self):
        for fail_revoke in (False, True):
            stream, _, _ = self.archive()
            admin = Admin(replacement=True)
            admin.fail_revoke = fail_revoke
            s3 = S3(admin)
            s3.fail_put = True
            with tempfile.TemporaryDirectory() as temp:
                path = self.database(Path(temp))
                with self.assertRaises(RuntimeError):
                    restore.restore_archive(
                        admin, s3, "test", CREDS, stream, controller_database=path
                    )
                connection = sqlite3.connect(path)
                self.assertEqual(
                    connection.execute("SELECT provider_id FROM managed_resources").fetchone()[0],
                    OLD_ID,
                )
                connection.close()
            if not fail_revoke:
                self.assertEqual(admin.permissions[(NEW_ID, BACKUP_KEY)], catalog.READ_ONLY)
            else:
                self.assertTrue(
                    any(
                        action == "DenyBucketKey" and body["permissions"]["write"]
                        for action, body, _ in admin.calls
                    )
                )

    def test_archive_order_sizes_duplicates_and_foreign_keys_are_rejected(self):
        original, _, _ = self.archive()
        manifest = catalog.verify_archive(original)
        for mutate, entry, payload in (
            (lambda m: m["objects"][0].update(size=6), "objects/000000000000.bin", b"hello"),
            (lambda m: None, "../escape", b"hello"),
            (lambda m: m["objects"].append(m["objects"][0]), None, b""),
            (
                lambda m: m["bucket_metadata"][0]["keys"][0].update(name="platform-backup"),
                None,
                b"",
            ),
        ):
            with self.subTest(entry=entry):
                changed = copy.deepcopy(manifest)
                mutate(changed)
                with self.assertRaises(RuntimeError):
                    catalog.verify_archive(self.make_archive(changed, entry=entry, payload=payload))

    def test_remap_ownership_conflict_and_symlink_never_change_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.database(root)
            with self.assertRaises(RuntimeError):
                catalog.remap_controller(
                    path, [{"name": "other", "old_id": OLD_ID, "new_id": NEW_ID}]
                )
            link = root / "link.sqlite3"
            link.symlink_to(path)
            with self.assertRaises(RuntimeError):
                catalog.remap_controller(link, [])

    def test_secret_input_rejects_links_and_world_readable_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "secrets"
            path.write_text("KEY=hidden\n")
            path.chmod(0o644)
            with self.assertRaises(RuntimeError):
                catalog.read_env(path)
            path.chmod(0o600)
            self.assertEqual(catalog.read_env(path), {"KEY": "hidden"})
            link = path.with_name("link")
            link.symlink_to(path)
            with self.assertRaises(RuntimeError):
                catalog.read_env(link)
