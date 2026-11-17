"""Staff database snapshots in the portal: who may ask, the audit trail and limits."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from openstack_platform.management.broker import bootstrap
from openstack_platform.management.common import strict_json
from openstack_platform.management.web.server import WebServer
from tests import test_management_accounts as account_fixtures
from tests import test_management_admin_apps as admin_fixtures
from tests.test_management import ManagementCase


class SnapshotTests(ManagementCase):
    anonymous = account_fixtures.AccountsTests.anonymous
    begin = account_fixtures.AccountsTests.begin
    finish = account_fixtures.AccountsTests.finish
    admin = account_fixtures.AccountsTests.admin
    staff = admin_fixtures.AdminApplicationTests.staff
    complete = admin_fixtures.AdminApplicationTests.complete

    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()
        self.broker.auth.clock = lambda: self.now
        folder = bootstrap.enrollment_file(self.config).parent
        folder.mkdir(parents=True, mode=0o2750)
        folder.chmod(0o2750)
        self.admin()
        self.owner = self.login("alice")
        self.app_id = self.create()
        self.save(self.app_id)
        self.fixture.delay = 0
        self.taylor = self.staff()
        self.database = self.add("mongo")
        self.base = f"/v1/apps/{self.app_id}/storage/{self.database}/snapshots"

    def add(self, kind: str) -> str:
        response = self.call("POST", f"/v1/apps/{self.app_id}/storage", {"type": kind}, "alice")
        self.assertEqual(self.complete(response, "alice")["state"], "succeeded")
        items = self.call("GET", f"/v1/apps/{self.app_id}/storage", owner="alice").body["data"]
        return next(item["resourceId"] for item in items["items"] if item["type"] == kind)

    def audit(self, action: str) -> list[dict[str, Any]]:
        with self.broker.database.connect() as db:
            return [
                {**dict(row), "details": strict_json(row["details"].encode())}
                for row in db.execute(
                    "SELECT * FROM admin_audit WHERE action=? ORDER BY sequence", (action,)
                )
            ]

    def request(self, actor: str = "taylor", key: str | None = None) -> Any:
        return self.call("POST", self.base, {}, actor, key=key)

    def test_staff_request_progress_and_download_are_audited(self) -> None:
        key = str(uuid.uuid4())
        requested = self.request(key=key)
        self.assertEqual(requested.status, 202)
        snapshot = requested.body["data"]["snapshotId"]
        self.assertEqual(requested.body["data"]["state"], "preparing")
        # A retry with the same key is the same snapshot and one audit row.
        self.assertEqual(self.request(key=key).body["data"]["snapshotId"], snapshot)
        rows = self.audit("storage_snapshot_requested")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["actor_id"], rows[0]["target_id"]), (self.taylor, self.owner))
        self.assertEqual(
            rows[0]["details"],
            {
                "applicationId": self.app_id,
                "appSlug": "student-app",
                "resourceId": self.database,
                "resourceType": "mongo",
                "snapshotId": snapshot,
            },
        )
        status = self.call("GET", f"{self.base}/{snapshot}", owner="taylor").body["data"]
        self.assertEqual(status["state"], "ready")
        self.assertTrue(status["filename"].startswith("student-app-"))
        self.assertIsNone(status["error"])
        link = self.call("POST", f"{self.base}/{snapshot}/link", {}, "admin").body["data"]
        self.assertTrue(link["url"].startswith("https://"))
        self.assertEqual(link["sha256"], status["sha256"])
        issued = self.audit("storage_snapshot_link_issued")
        self.assertEqual(len(issued), 1)
        self.assertEqual(issued[0]["details"]["sha256"], status["sha256"])
        self.assertEqual(issued[0]["details"]["bytes"], status["bytes"])
        self.assertEqual(issued[0]["details"]["appSlug"], "student-app")
        self.assertNotIn(link["url"], json.dumps(issued))
        with self.broker.database.connect() as db:
            reads = db.execute(
                "SELECT route FROM staff_read_audit WHERE actor_id=?", (self.taylor,)
            ).fetchall()
        self.assertIn("/v1/apps/{id}/storage/{id}/snapshots/{id}", {row["route"] for row in reads})
        history = self.call("GET", "/v1/audit", owner="admin").body["data"]["items"]
        self.assertLessEqual(
            {"storage_snapshot_requested", "storage_snapshot_link_issued"},
            {item["action"] for item in history},
        )

    def test_owners_and_teammates_cannot_take_or_download_snapshots(self) -> None:
        snapshot = self.request().body["data"]["snapshotId"]
        bob = self.login("bob")
        self.call("POST", f"/v1/apps/{self.app_id}/members", {"username": "bob"}, "alice")
        calls = len(self.fixture.calls)
        for actor in ("alice", "bob"):
            for method, path in (
                ("POST", self.base),
                ("GET", f"{self.base}/{snapshot}"),
                ("POST", f"{self.base}/{snapshot}/link"),
            ):
                with self.subTest(actor=actor, path=path):
                    self.assert_error(
                        "ACCESS_DENIED",
                        lambda method=method, path=path, actor=actor: self.call(
                            method, path, {} if method == "POST" else None, actor
                        ),
                    )
        self.assertEqual(len(self.fixture.calls), calls)
        self.assertTrue(bob)
        self.assertEqual(len(self.audit("storage_snapshot_link_issued")), 0)

    def test_mutations_need_the_session_csrf_token(self) -> None:
        self.assert_error(
            "CSRF_REJECTED",
            lambda: self.call("POST", self.base, {}, "taylor", headers={"x-csrf-token": "x"}),
        )
        self.assertEqual(self.audit("storage_snapshot_requested"), [])

    def test_three_snapshots_per_database_an_hour_for_each_staff_member(self) -> None:
        for _ in range(3):
            self.assertEqual(self.request().status, 202)
        self.assert_error("RATE_LIMITED", self.request)
        self.assertEqual(len(self.audit("storage_snapshot_requested")), 3)
        # Another database, and another staff member, have their own allowance.
        other = self.add("postgres")
        self.assertEqual(
            self.call(
                "POST", f"/v1/apps/{self.app_id}/storage/{other}/snapshots", {}, "taylor"
            ).status,
            202,
        )
        self.assertEqual(self.request("admin").status, 202)
        # Requests older than an hour no longer count.
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE admin_audit SET created=created-3601")
        self.assertEqual(self.request().status, 202)

    def test_refusals_explain_themselves_and_leave_no_request_row(self) -> None:
        bucket = self.add("s3")
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "POST", f"/v1/apps/{self.app_id}/storage/{bucket}/snapshots", {}, "taylor"
            ),
        )
        foreign = self.create(slug="other-app")
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "POST", f"/v1/apps/{foreign}/storage/{self.database}/snapshots", {}, "taylor"
            ),
        )
        original = self.broker.client.request
        for code, mapped in (
            ("EXPORT_BUSY", "SNAPSHOT_BUSY"),
            ("OPERATION_CONFLICT", "SNAPSHOT_IN_PROGRESS"),
            ("EXPORT_UNAVAILABLE", "SNAPSHOT_UNAVAILABLE"),
        ):
            with self.subTest(code=code):

                def refuse(method: str, path: str, *args: Any, code: str = code, **kwargs: Any):
                    if method == "POST" and path.endswith("/exports"):
                        return 409, {"error": {"code": code, "message": "no"}}
                    return original(method, path, *args, **kwargs)

                with patch.object(self.broker.client, "request", side_effect=refuse):
                    self.assert_error(mapped, self.request)
        self.assertEqual(self.audit("storage_snapshot_requested"), [])

    def test_failed_or_unfinished_snapshots_have_no_download(self) -> None:
        self.fixture.export_fails = True
        snapshot = self.request().body["data"]["snapshotId"]
        status = self.call("GET", f"{self.base}/{snapshot}", owner="taylor").body["data"]
        self.assertEqual(status["state"], "failed")
        self.assertIn("2 GiB", status["error"])
        self.assert_error(
            "SNAPSHOT_NOT_READY",
            lambda: self.call("POST", f"{self.base}/{snapshot}/link", {}, "taylor"),
        )
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call("GET", f"{self.base}/{uuid.uuid4()}", owner="taylor"),
        )
        self.assertEqual(self.audit("storage_snapshot_link_issued"), [])

    def test_web_pins_snapshot_methods_and_waits_longer_for_links(self) -> None:
        assets = self.root / "assets"
        assets.mkdir()
        web = WebServer(("127.0.0.1", 0), self.config, assets)
        self.addCleanup(web.server_close)
        base = f"/api/v1/apps/{self.app_id}/storage/{self.database}/snapshots"
        snapshot = str(uuid.uuid4())
        with patch.object(web.broker, "request", return_value=(200, {"data": {}})) as upstream:
            for method, path in (
                ("GET", base),
                ("DELETE", base),
                ("POST", f"{base}/{snapshot}"),
                ("GET", f"{base}/{snapshot}/link"),
                ("POST", f"{base}/{snapshot}/other"),
            ):
                with self.subTest(method=method, path=path):
                    self.assertEqual(web.forward(method, path, "", {}, b"").status, 404)
            upstream.assert_not_called()
            headers = {"host": urlsplit(self.config.portal_origin).netloc}
            web.forward("POST", f"{base}/{snapshot}/link", "", headers, b"")
            self.assertEqual(upstream.call_args.kwargs["timeout_seconds"], 35)
            web.forward("GET", f"{base}/{snapshot}", "", headers, b"")
            self.assertIsNone(upstream.call_args.kwargs["timeout_seconds"])
