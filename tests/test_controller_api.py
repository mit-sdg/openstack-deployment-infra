from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from platform_cli import db
from platform_cli.config import (
    Config,
    Limits,
    PlatformConfig,
    Policy,
    RuntimeImages,
    StandardProfile,
)
from platform_cli.controller_api import ControllerAPI
from platform_cli.controller_http import HttpError


class ControllerAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = db.connect(self.root / "platform.sqlite3")
        db.migrate(self.connection)
        platform = PlatformConfig(
            "project",
            "00000000-0000-4000-8000-000000000099",
            "test",
            "test-platform",
            "example.test",
            "dc1",
            "region1",
            "network",
            {"paths": {"root": "/srv/openstack-platform"}},
        )
        policy = Policy(
            StandardProfile(  # type: ignore[arg-type]
                "worker-small", 500, 512, 20, 1_000_000, 1_000_000, 1_000_000, 100
            ),
            RuntimeImages(
                "registry.example/bun@sha256:" + "a" * 64,
                "registry.example/node@sha256:" + "b" * 64,
            ),
            "age1" + "q" * 58,
            Limits(
                1_000_000,
                262_144,
                65_536,
                262_144,
                1_000_000,
                1_000_000,
                1_000_000,
                10,
                10,
                30,
                30,
                1,
            ),
        )
        self.config = Config(platform, policy)
        self.helper_calls: list[tuple[str, dict[str, object]]] = []

        def helper(_config, action, values, *, deadline=None):
            self.helper_calls.append((action, dict(values)))
            if action == "app.env.set":
                return {
                    "keys": sorted(values["updates"]),
                    "modifyIndex": 1,
                    "restarted": False,
                    "schedulerHealthy": False,
                    "publicHealthy": False,
                }
            if action == "app.remove":
                return {"jobAbsent": True, "variableAbsent": True}
            if action == "app.worker.delete":
                return {"absent": True}
            raise AssertionError(f"unexpected helper action {action}")

        self.api = ControllerAPI(
            self.connection,
            self.config,
            self.root,
            helper_caller=helper,
        )
        self.router = self.api.router()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    @staticmethod
    def headers(key: str) -> dict[str, str]:
        return {"Idempotency-Key": key}

    def dispatch(self, method, path, body=None, headers=None):
        return self.router.dispatch(method, path, headers or {}, body)

    def create_application(self, key="00000000-0000-4000-8000-000000000001"):
        return self.dispatch(
            "POST",
            "/v1/applications",
            {"slug": "demo-app"},
            self.headers(key),
        )

    def test_database_create_replays_and_changed_input_conflicts(self) -> None:
        first = self.create_application()
        replay = self.create_application()
        self.assertEqual(first.status, 201)
        self.assertEqual(first.body, replay.body)
        self.assertEqual(
            first.body["applicationId"],
            "00000000-0000-4000-8000-000000000001",
        )
        self.assertFalse(first.body["enabled"])
        self.assertEqual(len(db.list_applications(self.connection)), 1)

        with self.assertRaises(HttpError) as raised:
            self.dispatch(
                "POST",
                "/v1/applications",
                {"slug": "other-app"},
                self.headers("00000000-0000-4000-8000-000000000001"),
            )
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_unknown_fields_and_noncanonical_keys_are_rejected(self) -> None:
        with self.assertRaises(HttpError) as raised:
            self.dispatch(
                "POST",
                "/v1/applications",
                {"slug": "demo-app", "providerId": "forbidden"},
                self.headers("00000000-0000-4000-8000-000000000002"),
            )
        self.assertEqual(raised.exception.code, "INVALID_BODY")
        with self.assertRaises(HttpError) as raised:
            self.dispatch(
                "POST",
                "/v1/applications",
                {"slug": "demo-app"},
                self.headers("00000000-0000-4000-8000-0000000000AA"),
            )
        self.assertEqual(raised.exception.code, "INVALID_IDEMPOTENCY_KEY")

    @mock.patch("platform_cli.application_service.openstack.verify_project")
    def test_lost_environment_response_replays_without_value_persistence(
        self, verify_project
    ) -> None:
        verify_project.return_value = None
        application_id = self.create_application().body["applicationId"]
        secret = "sentinel-value-never-persisted"
        request_key = "00000000-0000-4000-8000-000000000003"
        path = f"/v1/applications/{application_id}/environment/API_TOKEN"
        first = self.dispatch(
            "PUT", path, {"value": secret}, self.headers(request_key)
        )
        replay = self.dispatch(
            "PUT", path, {"value": secret}, self.headers(request_key)
        )
        self.assertEqual(first.status, 202)
        self.assertEqual(first.body, replay.body)
        self.assertEqual(len(self.helper_calls), 1)
        operation = db.get_operation(self.connection, request_key)
        self.assertIsNotNone(operation)
        self.assertEqual(operation.status, "succeeded")
        revision = db.get_environment_revision(self.connection, application_id)
        self.assertEqual(revision.revision, 1)
        rendered = "\n".join(self.connection.iterdump())
        self.assertNotIn(secret, rendered)
        for path in self.root.glob("platform.sqlite3*"):
            self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertNotIn(secret, str(first.body))
        self.assertNotIn(secret, repr(operation.refs))

        environment = self.dispatch(
            "GET", f"/v1/applications/{application_id}/environment"
        )
        self.assertEqual(environment.body["revision"], 1)
        self.assertEqual(
            environment.body["keys"], [{"name": "API_TOKEN", "owner": "staff"}]
        )
        self.assertNotIn("value", str(environment.body).lower())

    @mock.patch("platform_cli.application_service.openstack.verify_project")
    def test_cascade_delete_replays_after_application_is_tombstoned(
        self, verify_project
    ) -> None:
        verify_project.return_value = None
        application_id = self.create_application().body["applicationId"]
        request_key = "00000000-0000-4000-8000-000000000004"
        path = f"/v1/applications/{application_id}/delete"
        first = self.dispatch(
            "POST",
            path,
            {"confirmation": "demo-app"},
            self.headers(request_key),
        )
        self.assertIsNone(db.get_application(self.connection, application_id))
        replay = self.dispatch(
            "POST",
            path,
            {"confirmation": "demo-app"},
            self.headers(request_key),
        )
        self.assertEqual(first.body, replay.body)
        self.assertEqual(db.get_operation(self.connection, request_key).status, "succeeded")
        self.assertIsNotNone(db.get_slug_tombstone(self.connection, "demo-app"))

    def test_operation_read_omits_refs_and_admin_pagination_is_bounded(self) -> None:
        self.create_application()
        for index, slug in enumerate(("alpha-app", "bravo-app"), 10):
            self.dispatch(
                "POST",
                "/v1/applications",
                {"slug": slug},
                self.headers(f"00000000-0000-4000-8000-{index:012d}"),
            )
        operation_id = "00000000-0000-4000-8000-000000000020"
        db.begin_operation(
            self.connection,
            operation_id=operation_id,
            kind="test.operation",
            scope="test-scope",
            phase="started",
            deadline_at=db.utc_now(),
            refs={"internal": "not-an-api-field"},
        )
        operation = self.dispatch("GET", f"/v1/operations/{operation_id}")
        self.assertNotIn("refs", operation.body)
        page = self.dispatch("GET", "/v1/admin/applications?limit=1")
        self.assertEqual(len(page.body["items"]), 1)
        self.assertTrue(page.body["truncated"])
        cursor = page.body["nextCursor"]
        second = self.dispatch(
            "GET", f"/v1/admin/applications?limit=1&cursor={cursor}"
        )
        self.assertNotEqual(
            page.body["items"][0]["applicationId"],
            second.body["items"][0]["applicationId"],
        )


if __name__ == "__main__":
    unittest.main()
