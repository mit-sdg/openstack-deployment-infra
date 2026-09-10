from __future__ import annotations

import json
import unittest
from unittest import mock

from openstack_platform.controller import timing

OPERATION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class DeploymentTimingTests(unittest.TestCase):
    def messages(self, captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    def test_reports_operation_and_helper_spans_without_error_contents(self):
        with self.assertLogs("openstack_platform.timings", level="INFO") as captured:
            with self.assertRaises(RuntimeError), timing.operation(OPERATION):
                with timing.helper("app.build"):
                    raise RuntimeError("private-key=password-do-not-log")
        rows = self.messages(captured)
        self.assertEqual(
            [r["event"] for r in rows],
            ["operation.started", "helper.started", "helper.finished", "operation.finished"],
        )
        self.assertTrue(all(r["operationId"] == OPERATION for r in rows))
        self.assertFalse(rows[-1]["returned"])
        self.assertFalse(rows[-2]["returned"])
        self.assertGreaterEqual(rows[-1]["elapsedSeconds"], 0)
        self.assertNotIn("private-key", json.dumps(rows))
        self.assertNotIn("password", json.dumps(rows))

    def test_nested_context_is_restored_and_unscoped_or_invalid_labels_are_not_logged(self):
        with self.assertLogs("openstack_platform.timings", level="INFO") as captured:
            with timing.operation(OPERATION):
                with timing.operation(OTHER), timing.helper("app.worker.capacity"):
                    pass
                with timing.helper("app.deploy"):
                    pass
                with timing.helper("private-input=not-a-fixed-action"):
                    pass
            with timing.helper("app.remove"):
                pass
        rows = self.messages(captured)
        helper_rows = [r for r in rows if r["event"] == "helper.started"]
        self.assertEqual(
            [(r["operationId"], r["action"]) for r in helper_rows],
            [(OTHER, "app.worker.capacity"), (OPERATION, "app.deploy")],
        )
        self.assertNotIn("private-input", json.dumps(rows))

    def test_diagnostic_failure_does_not_change_execution(self):
        ran = False
        with mock.patch.object(timing._LOG, "info", side_effect=OSError("journal unavailable")):
            with timing.operation(OPERATION), timing.helper("app.build"):
                ran = True
        self.assertTrue(ran)
        with self.assertNoLogs("openstack_platform.timings", level="INFO"):
            with timing.helper("app.build"):
                pass
