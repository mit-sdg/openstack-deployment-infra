from __future__ import annotations

import time
import unittest
import uuid
from typing import Any
from unittest.mock import patch

from openstack_platform.management.broker import runtime_logs
from tests.test_management import ManagementCase


class RuntimeLogTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = 1000.0
        self.broker.runtime_logs.clock = lambda: self.now
        self.login()
        self.app = self.create()
        self.save(self.app)
        self.fixture.delay = 0
        self.sent: list[str] = []
        request = self.broker.client.request

        def spy(method: str, path: str, *args: Any, **kwargs: Any) -> Any:
            self.sent.append(path)
            return request(method, path, *args, **kwargs)

        self.broker.client.request = spy  # type: ignore[method-assign]

    def logs(self, query: str = "", owner: str = "alice") -> Any:
        return self.call("GET", f"/v1/apps/{self.app}/logs{query}", owner=owner).body["data"]

    def reads(self) -> list[str]:
        return [path for path in self.sent if "/runtime-log" in path]

    def deploy(self) -> None:
        intent = self.call(
            "POST",
            f"/v1/apps/{self.app}/deployments",
            {"commit": "1" * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        self.broker.journal.dispatch(intent["intentId"])

    def test_undeployed_app_has_no_output_and_skips_the_controller(self) -> None:
        result = self.logs()
        self.assertEqual(
            {key: result[key] for key in ("stream", "running", "text", "truncated", "lines")},
            {"stream": "stdout", "running": False, "text": "", "truncated": False, "lines": 500},
        )
        self.assertEqual(self.reads(), [])

    def test_owner_reads_output_and_errors_shared_for_a_few_seconds(self) -> None:
        self.deploy()
        output = self.logs()
        self.assertTrue(output["running"])
        self.assertIn("Listening on port 3000", output["text"])
        errors = self.logs("?stream=stderr")
        self.assertEqual(
            (errors["stream"], errors["text"]), ("stderr", "Warning: SESSION_SECRET is short\n")
        )
        self.assertEqual(self.logs("?stream=stdout"), output)
        self.assertEqual(
            self.reads(),
            [
                f"/v1/applications/{self.app}/runtime-log?lines=500",
                f"/v1/applications/{self.app}/runtime-log?lines=500&stream=stderr",
            ],
        )
        self.now += runtime_logs.SHARE_SECONDS
        self.logs()
        self.assertEqual(len(self.reads()), 3)

    def test_queries_and_other_owners_are_refused(self) -> None:
        for query in ("?stream=both", "?stream=stdout&stream=stderr", "?lines=10"):
            with self.subTest(query=query):
                self.assert_error("INVALID_REQUEST", lambda query=query: self.logs(query))
        self.login("bob")
        self.assert_error("NOT_FOUND", lambda: self.logs(owner="bob"))
        self.assertEqual(self.reads(), [])

    def test_stopped_app_and_controller_answers(self) -> None:
        self.deploy()
        self.fixture.apps[self.app]["desiredRunning"] = False
        self.assertFalse(self.logs()["running"])
        running = {"lifecycleState": "ready", "desiredRunning": True, "activeDeploymentId": "x"}
        answers: list[tuple[int, dict[str, Any], str]] = [
            (502, {"error": {"code": "ALLOCATION_NOT_FOUND"}}, ""),
            (400, {"error": {"code": "INVALID_QUERY"}}, "LOG_STREAM_UNAVAILABLE"),
            (502, {"error": {"code": "NOMAD_RESPONSE_INVALID"}}, "STATE_UNAVAILABLE"),
            (
                200,
                {"applicationId": self.app, "stream": "stdout", "text": "x"},
                "STATE_UNAVAILABLE",
            ),
        ]
        for status, body, code in answers:
            self.now += runtime_logs.SHARE_SECONDS
            with (
                self.subTest(status=status, body=body),
                patch.object(self.broker, "app_model", return_value=running),
                patch.object(self.broker.client, "request", return_value=(status, body)),
            ):
                if code:
                    self.assert_error(code, lambda: self.logs("?stream=stderr"))
                else:
                    self.assertFalse(self.logs("?stream=stderr")["running"])

    def test_one_controller_read_at_a_time(self) -> None:
        self.assertTrue(self.broker.runtime_logs.reading.acquire(timeout=1))
        try:
            with patch.object(runtime_logs, "WAIT_SECONDS", 0.01):
                self.assert_error("LOGS_BUSY", lambda: self.logs())
        finally:
            self.broker.runtime_logs.reading.release()

    def test_tail_keeps_whole_lines_within_the_limit(self) -> None:
        self.assertEqual(runtime_logs.tail("a\nb\n", 10), ("a\nb\n", False))
        self.assertEqual(runtime_logs.tail("first\nsecond\nthird\n", 12), ("third\n", True))
        text, cut = runtime_logs.tail("é" * 10, 5)
        self.assertTrue(cut)
        self.assertLessEqual(len(text.encode()), 6)
        started = time.monotonic()
        text, cut = runtime_logs.tail("line\n" * 200_000)
        self.assertTrue(cut and text.startswith("line\n"))
        self.assertLessEqual(len(text.encode()), runtime_logs.TEXT_BYTES)
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()


class StartupLogTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.login()
        self.app = self.create()
        self.save(self.app)
        self.fixture.delay = 0

    def deploy(self, *, fail: bool) -> str:
        self.fixture.failed_next = fail
        intent = self.call(
            "POST",
            f"/v1/apps/{self.app}/deployments",
            {"commit": ("2" if fail else "1") * 40, "configurationRevision": 1},
            "alice",
        ).body["data"]
        self.broker.journal.dispatch(intent["intentId"])
        return str(
            self.call("GET", f"/v1/intents/{intent['intentId']}", owner="alice").body["data"][
                "operationId"
            ]
        )

    def startup(self, deployment: str, owner: str = "alice") -> Any:
        return self.call(
            "GET", f"/v1/apps/{self.app}/deployments/{deployment}/startup-log", owner=owner
        ).body["data"]

    def test_owner_reads_why_a_failed_deployment_stopped(self) -> None:
        healthy = self.deploy(fail=False)
        self.assertEqual(self.startup(healthy), {"captured": False})
        failed = self.deploy(fail=True)
        record = self.startup(failed)
        self.assertTrue(record["captured"] and record["found"])
        self.assertEqual(record["restarts"], 3)
        self.assertIn("Cannot find module 'express'", record["stderr"])
        self.assertEqual(
            [(event["type"], event["exitCode"]) for event in record["events"]][1],
            ("Terminated", 1),
        )
        self.assertNotIn("taskState", record)
        self.login("bob")
        self.assert_error("NOT_FOUND", lambda: self.startup(failed, owner="bob"))
        self.assert_error(
            "NOT_FOUND",
            lambda: self.call(
                "GET",
                f"/v1/apps/{self.app}/deployments/{uuid.uuid4()}/startup-log",
                owner="alice",
            ),
        )

    def test_controllers_without_startup_records_report_none(self) -> None:
        failed = self.deploy(fail=True)
        request = self.broker.client.request

        def older(method: str, path: str, *args: Any, **kwargs: Any) -> Any:
            if path.endswith("/startup-log"):
                return 404, {"error": {"code": "NOT_FOUND"}}
            return request(method, path, *args, **kwargs)

        with patch.object(self.broker.client, "request", older):
            self.assertEqual(self.startup(failed), {"captured": False})
