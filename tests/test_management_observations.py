"""Cached portal reads, bounded background refresh and mutation invalidation."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from openstack_platform.management.broker.client import ControllerUnavailable
from tests.test_management import ManagementCase


class ObservationTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.login()
        self.app = self.create()
        self.path = f"/v1/apps/{self.app}"
        self.fixture.delay = 0

    def age(self, seconds: float) -> None:
        with self.broker.database.connect(write=True) as db:
            db.execute(
                "UPDATE observations SET updated=? WHERE app_id=?",
                (time.time() - seconds, self.app),
            )

    def read(self):
        return self.call("GET", self.path, owner="alice").body["data"]

    def updated(self):
        with self.broker.database.connect() as db:
            return db.execute(
                "SELECT updated FROM observations WHERE app_id=?", (self.app,)
            ).fetchone()[0]

    def test_personal_list_returns_cached_rows_during_slow_single_flight_refresh(self) -> None:
        self.age(40)
        entered, release = threading.Event(), threading.Event()
        original = self.broker.client.request
        reads = []

        def slow(*args, **kwargs):
            self.assertTrue(self.broker.client.background.get())
            reads.append(args[1])
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with (
            patch.object(self.broker.client, "request", side_effect=slow),
            ThreadPoolExecutor() as caller,
        ):
            try:
                for _ in range(2):
                    page = (
                        caller.submit(self.call, "GET", "/v1/apps", owner="alice")
                        .result(1)
                        .body["data"]
                    )
                    self.assertEqual(page["items"][0]["applicationId"], self.app)
                    self.assertTrue(page["items"][0]["refreshing"])
                    self.assertGreaterEqual(page["items"][0]["observationAgeSeconds"], 40)
                self.assertTrue(entered.wait(1))
                self.assertEqual(reads, [f"/v1/applications/{self.app}"])
            finally:
                release.set()
                self.broker.app_management.close()
        self.assertGreater(self.updated(), time.time() - 5)

    def test_missing_list_observation_never_waits_for_controller(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.execute("DELETE FROM observations WHERE app_id=?", (self.app,))
        with (
            patch.object(self.broker.app_management.refresh_pool, "submit") as submit,
            patch.object(self.broker.client, "request") as read,
        ):
            row = self.call("GET", "/v1/apps", owner="alice").body["data"]["items"][0]
        read.assert_not_called()
        self.assertEqual(submit.call_count, 1)
        self.assertTrue(row["stale"])
        self.assertTrue(row["refreshing"])
        self.assertIsNone(row["observedAt"])

    def test_queue_has_one_admission_per_app_and_a_broker_wide_bound(self) -> None:
        rows = [
            {"id": str(uuid.uuid4()), "lifecycle": "ready", "observation_updated": None}
            for _ in range(105)
        ]
        manager = self.broker.app_management
        with patch.object(manager.refresh_pool, "submit") as submit:
            self.assertEqual(len(manager.refresh_observations(rows)), 100)
            self.assertEqual(len(manager.refresh_observations(rows, cache_seconds=2)), 100)
            self.assertEqual(submit.call_count, 100)
        manager.close()
        with patch.object(manager.refresh_pool, "submit") as submit:
            manager.refresh_observations(rows)
            submit.assert_not_called()

    def test_refresh_pool_runs_at_most_four_checks_with_background_share(self) -> None:
        manager = self.broker.app_management
        rows = [
            {"id": str(uuid.uuid4()), "lifecycle": "ready", "observation_updated": None}
            for _ in range(8)
        ]
        release, entered = threading.Event(), threading.Event()
        lock = threading.Lock()
        active = []

        def slow(row, **_kwargs):
            self.assertTrue(self.broker.client.background.get())
            with lock:
                active.append(row["id"])
                if len(active) == 4:
                    entered.set()
            self.assertTrue(release.wait(5))

        with patch.object(self.broker, "app_model", side_effect=slow):
            try:
                self.assertEqual(len(manager.refresh_observations(rows)), 8)
                self.assertTrue(entered.wait(1))
                self.assertEqual(len(active), 4)
            finally:
                release.set()
                manager.close()

    def test_recent_detail_uses_cache_without_any_controller_call(self) -> None:
        with patch.object(self.broker.client, "request") as read:
            app = self.read()
        read.assert_not_called()
        self.assertFalse(app["stale"])
        self.assertFalse(app["refreshing"])
        self.assertLess(app["observationAgeSeconds"], 2)
        self.assertNotIn("sizing", app)
        self.assertNotIn("_metadata", app)

    def test_detail_returns_recent_cache_then_refreshes_in_background(self) -> None:
        self.age(10)
        entered, release = threading.Event(), threading.Event()
        original = self.broker.client.request

        def slow(*args, **kwargs):
            self.assertTrue(self.broker.client.background.get())
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with (
            patch.object(self.broker.client, "request", side_effect=slow),
            ThreadPoolExecutor() as caller,
        ):
            try:
                app = caller.submit(self.read).result(1)
                self.assertTrue(app["stale"])
                self.assertTrue(app["refreshing"])
                self.assertGreaterEqual(app["observationAgeSeconds"], 10)
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
                self.broker.app_management.close()
        self.assertFalse(self.read()["stale"])

    def test_old_or_missing_detail_cache_uses_synchronous_probe(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.age(31)
                if missing:
                    with self.broker.database.connect(write=True) as db:
                        db.execute("DELETE FROM observations WHERE app_id=?", (self.app,))
                original = self.broker.client.request
                with patch.object(self.broker.client, "request", wraps=original) as read:
                    self.assertFalse(self.read()["stale"])
                self.assertEqual(
                    read.call_args_list[0].args[:2], ("GET", f"/v1/applications/{self.app}")
                )

    def deploy(self):
        self.save(self.app)
        response = self.call(
            "POST",
            self.path + "/deployments",
            {"configurationRevision": 1, "commit": "a" * 40},
            "alice",
        )
        intent = response.body["data"]
        self.broker.journal.dispatch(intent["intentId"])
        return intent

    def test_deploy_start_stop_restart_invalidate_admission_progress_and_completion(self) -> None:
        deployed = self.deploy()
        self.assertEqual(self.updated(), 0)
        self.assertEqual(self.read()["activeDeploymentId"], deployed["operationId"])
        for suffix, body in (
            ("state", {"desiredRunning": False}),
            ("state", {"desiredRunning": True}),
            ("restart", {}),
        ):
            with self.subTest(suffix=suffix, body=body):
                intent = self.call("POST", self.path + "/" + suffix, body, "alice").body["data"]
                self.assertEqual(self.updated(), 0)
                original = self.broker.client.request
                # In-progress polling bypasses even a newly written display cache.
                with patch.object(self.broker.client, "request", wraps=original) as read:
                    self.read()
                    self.read()
                self.assertEqual(
                    sum(
                        call.args[1] == f"/v1/applications/{self.app}"
                        for call in read.call_args_list
                    ),
                    2,
                )
                self.broker.journal.dispatch(intent["intentId"])
                self.assertEqual(self.updated(), 0)
                app = self.read()
                self.assertFalse(app["stale"])
                if suffix == "state":
                    self.assertEqual(app["desiredRunning"], body["desiredRunning"])

    def test_probe_started_before_mutation_cannot_restore_invalidated_cache(self) -> None:
        self.age(10)
        entered, release = threading.Event(), threading.Event()
        original = self.broker.client.request

        def slow(*args, **kwargs):
            response = original(*args, **kwargs)
            if args[1] == f"/v1/applications/{self.app}":
                entered.set()
                self.assertTrue(release.wait(5))
            return response

        with patch.object(self.broker.client, "request", side_effect=slow):
            try:
                self.read()
                self.assertTrue(entered.wait(1))
                self.save(self.app)
                self.assertEqual(self.updated(), 0)
            finally:
                release.set()
                self.broker.app_management.close()
        self.assertEqual(self.updated(), 0)
        self.assertFalse(self.read()["stale"])

    def test_failed_background_probe_preserves_last_observation(self) -> None:
        self.age(40)
        before = self.updated()
        with patch.object(
            self.broker.client, "request", side_effect=ControllerUnavailable("fixture")
        ):
            self.call("GET", "/v1/apps", owner="alice")
            self.broker.app_management.close()
        self.assertEqual(self.updated(), before)

    def test_personal_list_keeps_owner_member_rules_even_for_staff(self) -> None:
        bob = self.login("bob")
        taylor = self.login("taylor")
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE users SET role='staff' WHERE id=?", (taylor,))
        self.login("taylor")
        self.assertEqual(self.call("GET", "/v1/apps", owner="bob").body["data"]["items"], [])
        self.assertEqual(self.call("GET", "/v1/apps", owner="taylor").body["data"]["items"], [])
        self.call("POST", self.path + "/members", {"username": "bob"}, "alice")
        row = self.call("GET", "/v1/apps", owner="bob").body["data"]["items"][0]
        self.assertEqual(
            (row["applicationId"], row["access"], row["ownerDisplayName"]),
            (self.app, "member", "Alice Student"),
        )
        self.call("DELETE", self.path + "/members/" + bob, owner="alice")
        self.assertEqual(self.call("GET", "/v1/apps", owner="bob").body["data"]["items"], [])
