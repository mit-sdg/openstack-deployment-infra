"""Garage backup-key wiring and cleanup through the production HTTP client."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform import runtime
from openstack_platform.helper import production, storage
from openstack_platform.helper.main import HelperActionError
from tests.test_platform_storage import (
    APP_ID,
    Garage,
    MemoryNomad,
    NotFound,
    S3Client,
    config_fixture,
)


class GarageProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = config_fixture(self.root)
        (self.root / "secrets").mkdir()
        (self.root / "secrets/storage-bootstrap.env").write_text(
            "GARAGE_ADMIN_TOKEN=fixture-token\n"
        )
        self.backup_file = self.root / "secrets/garage-backup.env"
        self.events: list[str] = []
        self.garage = Garage(self.events)
        self.garage.absent = True
        self.grants: list[tuple[str, dict]] = []

    def create(self, admin, backup_key="backup-key"):
        return storage.s3_create(
            admin,
            application_id=APP_ID,
            application_slug="demo-app",
            prefix="example",
            endpoint="https://storage:9000",
            bytes_quota=1000,
            objects_quota=100,
            generation="abcdef12",
            backup_access_key=backup_key,
            scoped_client=lambda *_: S3Client(self.events),
        )

    def wire(self):
        modules = {
            "boto3": SimpleNamespace(client=lambda *_args, **_kwargs: S3Client(self.events)),
            "botocore.config": SimpleNamespace(Config=lambda **_kwargs: None),
        }
        with (
            mock.patch.object(
                production,
                "helper_runtime",
                return_value=SimpleNamespace(root=self.root, platform=self.config.platform),
            ),
            mock.patch.object(production, "_nomad_secrets", return_value=self.root),
            mock.patch.object(
                production, "_nomad_client", return_value=MemoryNomad({"PORT": "3000"})
            ),
            mock.patch.object(production, "_GarageAdmin", return_value=self.garage),
            mock.patch.object(production.ssl, "create_default_context"),
            mock.patch.dict(sys.modules, modules),
        ):
            return production._storage_handlers("storage.s3.create", application_slug="demo-app")[0]

    def test_persisted_id_grants_only_the_real_backup_key_despite_duplicate_names(self) -> None:
        self.backup_file.write_text(
            "GARAGE_BACKUP_ACCESS_KEY=backup-key\nGARAGE_BACKUP_SECRET_KEY=unused\n"
        )
        self.garage.keys["orphan-backup"] = "platform-backup"
        original = self.garage.request

        def request(path, body=None, query=None):
            if path in {"/AllowBucketKey", "/DenyBucketKey"}:
                self.grants.append((path, dict(body)))
            return original(path, body, query)

        self.garage.request = request
        result = self.wire()["storage.s3.create"](
            {
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
                "resourceName": "default",
                "s3Bytes": 1000,
                "s3Objects": 100,
                "operationId": "44444444-4444-4444-8444-444444444444",
                "recover": False,
            }
        )
        self.assertEqual(result["providerId"], "bucket-id")
        self.assertNotIn("orphan-backup", [body["accessKeyId"] for _, body in self.grants])
        self.assertIn(
            (
                "/AllowBucketKey",
                {
                    "bucketId": "bucket-id",
                    "accessKeyId": "backup-key",
                    "permissions": {"read": True, "write": False, "owner": False},
                },
            ),
            self.grants,
        )
        self.assertIn(
            (
                "/DenyBucketKey",
                {
                    "bucketId": "bucket-id",
                    "accessKeyId": "backup-key",
                    "permissions": {"read": False, "write": True, "owner": True},
                },
            ),
            self.grants,
        )

    def test_missing_file_or_access_key_field_reports_backup_key_missing(self) -> None:
        for contents in (None, "GARAGE_BACKUP_SECRET_KEY=unused\n", "GARAGE_BACKUP_ACCESS_KEY=\n"):
            with self.subTest(contents=contents):
                if contents is not None:
                    self.backup_file.write_text(contents)
                with self.assertRaises(HelperActionError) as raised:
                    self.wire()
                self.assertEqual(raised.exception.code, "BACKUP_KEY_MISSING")
                self.assertEqual(self.events, [])

    def test_missing_exact_provider_key_fails_before_creation_without_name_fallback(self) -> None:
        self.backup_file.write_text("GARAGE_BACKUP_ACCESS_KEY=missing-key\n")
        self.garage.keys["orphan-backup"] = "platform-backup"
        handlers = self.wire()
        with self.assertRaises(HelperActionError) as raised:
            handlers["storage.s3.create"](
                {
                    "applicationId": APP_ID,
                    "applicationSlug": "demo-app",
                    "resourceName": "default",
                    "s3Bytes": 1000,
                    "s3Objects": 100,
                    "operationId": "44444444-4444-4444-8444-444444444444",
                    "recover": False,
                }
            )
        self.assertEqual(raised.exception.code, "BACKUP_KEY_MISSING")
        self.assertNotIn("/CreateBucket", self.events)
        self.assertNotIn("/CreateKey", self.events)

    def http(self, url, *, method, data, **_kwargs):
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.removeprefix("/v2")
        query = dict(urllib.parse.parse_qsl(parsed.query)) or None
        body = None if data is None else json.loads(data)
        if path == "/DenyBucketKey":
            raise runtime.HttpStatusFailure(503, b"fixture backup grant failure")
        try:
            response = self.garage.request(path, body, query)
        except NotFound:
            raise runtime.HttpStatusFailure(404, b'{"code":"NoSuchBucket"}') from None
        return runtime.HttpResult(200, {}, json.dumps(response).encode())

    def test_grant_failure_rolls_back_bucket_and_key_through_production_http(self) -> None:
        admin = production._GarageAdmin("https://storage:3903/v2", "fixture-token", None)
        with mock.patch.object(production, "bounded_http", side_effect=self.http):
            with self.assertRaises(HelperActionError) as raised:
                self.create(admin)
        self.assertEqual(raised.exception.code, "CREATE_ROLLED_BACK")
        self.assertTrue(self.garage.absent)
        self.assertNotIn("new-key", self.garage.keys)
        self.assertIn("backup-key", self.garage.keys)
        self.assertLess(self.events.index("/DeleteBucket"), self.events.index("/DeleteKey"))

    def test_http_authorization_failure_is_not_proof_of_bucket_absence(self) -> None:
        admin = production._GarageAdmin("https://storage:3903/v2", "fixture-token", None)
        with mock.patch.object(
            production, "bounded_http", side_effect=runtime.HttpStatusFailure(403, b"private")
        ):
            with self.assertRaises(runtime.HttpStatusFailure) as raised:
                storage.s3_absent(admin, provider_id="bucket-id")
        self.assertEqual(raised.exception.status, 403)
        self.assertNotIn("private", str(raised.exception))

    def test_unconfirmed_key_deletion_reports_the_remaining_key(self) -> None:
        original = self.garage.request

        def request(path, body=None, query=None):
            if path == "/DenyBucketKey":
                raise RuntimeError("fixture grant failure")
            if path == "/DeleteKey":
                return None  # Simulate a successful reply without confirmed deletion.
            return original(path, body, query)

        self.garage.request = request
        with self.assertRaises(HelperActionError) as raised:
            self.create(self.garage)
        self.assertEqual(raised.exception.code, "RECOVERY_REQUIRED")
        self.assertIn("access-key absence", str(raised.exception))
        self.assertTrue(self.garage.absent)
        self.assertIn("new-key", self.garage.keys)
