from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from infra.lib import health_alert_config, health_alerts
from infra.lib.platform_config import load as load_inventory
from infra.monitor import send_health_alert
from openstack_platform.config import load_platform
from openstack_platform.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]


class HealthAlertsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = {
            "namespace": "app-platform",
            "paths": {"adminState": str(self.directory)},
            "healthAlerts": {"enabled": True},
        }
        self.snapshot = {
            "healthy": False,
            "checks": {"openstack": {}},
            "error": "secret password sentinel https://example.invalid/token",
        }
        self.settings = health_alert_config.validate({"enabled": True})
        self.state = {"failures": 0, "incident": False, "last_attempt": 0.0}

    def notify(self, healthy=False):
        with contextlib.redirect_stdout(io.StringIO()):
            health_alerts.notify(self.config, {**self.snapshot, "healthy": healthy})

    def test_threshold_repeat_and_recovery_across_runs(self):
        with mock.patch.object(health_alerts, "send", return_value=True) as sender:
            self.notify()
            sender.assert_not_called()
            self.notify()
            self.assertEqual(sender.call_count, 1)
            self.notify()
            self.assertEqual(sender.call_count, 1)
            saved = self.directory / "operator/status/health-alerts.json"
            state = json.loads(saved.read_text())
            self.assertEqual(state["failures"], 3)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            state["last_attempt"] -= self.settings["repeatSeconds"]
            saved.write_text(json.dumps(state))
            self.notify()
            self.assertEqual(sender.call_count, 2)
            self.notify(healthy=True)
            self.assertEqual(sender.call_count, 3)
            self.assertIn("recovered", sender.call_args.args[1]["text"])
            self.notify(healthy=True)
            self.assertEqual(sender.call_count, 3)
            self.notify()
            self.assertEqual(sender.call_count, 3)
            self.notify()
            self.assertEqual(sender.call_count, 4)

    def test_failed_delivery_is_throttled_and_recovery_is_retried(self):
        self.config["healthAlerts"]["failureThreshold"] = 1
        with mock.patch.object(health_alerts, "send", return_value=False) as sender:
            self.notify()
            self.notify()
            self.assertEqual(sender.call_count, 1)
            self.notify(healthy=True)
            self.assertEqual(sender.call_count, 2)
            self.notify(healthy=True)
            self.assertEqual(sender.call_count, 2)
            saved = self.directory / "operator/status/health-alerts.json"
            state = json.loads(saved.read_text())
            state["last_attempt"] -= 300
            saved.write_text(json.dumps(state))
            sender.return_value = True
            self.notify(healthy=True)
            self.assertEqual(sender.call_count, 3)
            self.notify(healthy=True)
            self.assertEqual(sender.call_count, 3)

    def test_attempt_is_persisted_before_delivery(self):
        self.config["healthAlerts"]["failureThreshold"] = 1

        def observe(*_args):
            state = json.loads((self.directory / "operator/status/health-alerts.json").read_text())
            self.assertTrue(state["incident"])
            self.assertGreater(state["last_attempt"], 0)
            return False

        with mock.patch.object(health_alerts, "send", side_effect=observe):
            self.notify()

    def test_disabled_does_not_read_secret_or_write_state(self):
        self.config.pop("healthAlerts")
        with mock.patch.object(health_alerts, "send") as sender:
            self.notify()
            sender.assert_not_called()
        self.assertFalse((self.directory / "operator").exists())

    def test_messages_only_include_fixed_check_names(self):
        for event in ("failing", "recovered", "test"):
            for format_name in ("slack", "json"):
                with self.subTest(event=event, format=format_name):
                    message = health_alerts.payload(
                        "app-platform", event, self.snapshot, format_name
                    )
                    encoded = json.dumps(message)
                    self.assertNotIn("sentinel", encoded)
                    self.assertNotIn("https://", encoded)
                    self.assertIn("app-platform", message["text"])
                    self.assertIn(datetime.now(UTC).date().isoformat(), message["text"])
                    if format_name == "slack":
                        self.assertEqual(set(message), {"text"})
                    else:
                        self.assertEqual(message["event"], event)
                        self.assertEqual(
                            message["failed_checks"],
                            ["public_ingress"] if event == "failing" else [],
                        )

    def test_configuration_validation_in_both_loaders(self):
        original = json.loads((ROOT / "config/platform.example.json").read_text())
        cases = [
            None,
            {"url": "https://example.invalid/secret"},
            {"enabled": 1},
            {"format": "xml"},
            {"failureThreshold": 0},
            {"failureThreshold": True},
            {"repeatSeconds": 3599},
            {"repeatSeconds": 604801},
        ]
        for settings in cases:
            path = self.directory / "platform.json"
            path.write_text(json.dumps({**original, "healthAlerts": settings}))
            with self.subTest(settings=settings), self.assertRaises(ValidationError):
                load_platform(path)
            with (
                mock.patch.dict(os.environ, {"PLATFORM_CONFIG": str(path)}),
                self.assertRaises(ValueError),
            ):
                load_inventory()
        for settings in (
            {},
            {"enabled": True, "format": "json", "failureThreshold": 3, "repeatSeconds": 3600},
        ):
            health_alert_config.validate(settings)

    def test_https_validation_does_not_expose_url(self):
        for url in (
            "http://example.invalid/token",
            "file:///token",
            "https://user:token@example.invalid",
            "https://example.invalid/#token",
            "https:///token",
            "https://example.invalid:bad/token",
            "https://example.invalid/\ntoken",
            "https://example.invalid/\x7ftoken",
            "https://example.invalid/\x80token",
            "https://example.invalid:0",
            "https://[bad/token",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                health_alert_config.validate_webhook(url)
            self.assertNotIn("token", str(caught.exception))
        self.assertEqual(
            health_alert_config.validate_webhook("https://example.invalid/token?key=secret"),
            "https://example.invalid/token?key=secret",
        )

    def test_protected_secret_file(self):
        path = self.directory / "webhook"
        path.write_text("https://example.invalid/token\n")
        path.chmod(0o600)
        self.assertEqual(health_alerts.read_webhook(path), "https://example.invalid/token")
        link = self.directory / "link"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            health_alerts.read_webhook(link)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            health_alerts.read_webhook(path)

    def test_sender_hard_deadline_and_no_secret_in_arguments_or_output(self):
        path = self.directory / "webhook"
        path.write_text("https://example.invalid/sentinel-token")
        path.chmod(0o600)
        with mock.patch.object(
            health_alerts.subprocess, "run", side_effect=subprocess.TimeoutExpired("sender", 10)
        ) as run:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertFalse(health_alerts.send(path, {"text": "test"}))
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(run.call_args.kwargs["timeout"], 10)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("sentinel", str(run.call_args.args))
            self.assertIn(b"sentinel-token", run.call_args.kwargs["input"])

    def test_http_post_timeout_redirect_policy_and_status(self):
        body = json.dumps(
            {"url": "https://example.invalid/token", "payload": {"text": "test"}}
        ).encode()
        for status, expected in ((200, 0), (204, 0), (302, 1), (500, 1)):
            opener = mock.MagicMock()
            opener.open.return_value.__enter__.return_value.status = status
            with (
                mock.patch.object(
                    send_health_alert.sys, "stdin", mock.Mock(buffer=io.BytesIO(body))
                ),
                mock.patch.object(
                    send_health_alert.urllib.request, "build_opener", return_value=opener
                ) as build,
            ):
                self.assertEqual(send_health_alert.main(), expected)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(json.loads(request.data), {"text": "test"})
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 8)
                self.assertEqual(build.call_args.args[0].proxies, {})
                self.assertIsNone(
                    build.call_args.args[1].redirect_request(
                        None, None, 302, "", None, "http://example.invalid"
                    )
                )
        redirect_error = urllib.error.HTTPError(
            "https://example.invalid/token", 302, "redirect", {}, None
        )
        self.addCleanup(redirect_error.close)
        opener.open.side_effect = redirect_error
        with (
            mock.patch.object(send_health_alert.sys, "stdin", mock.Mock(buffer=io.BytesIO(body))),
            mock.patch.object(
                send_health_alert.urllib.request, "build_opener", return_value=opener
            ),
        ):
            self.assertEqual(send_health_alert.main(), 1)

    def test_webhook_failure_preserves_health_exit_status(self):
        with mock.patch.dict(
            os.environ, {"PLATFORM_CONFIG": str(ROOT / "config/platform.example.json")}
        ):
            checker = importlib.import_module("infra.monitor.check_platform")
        backups = self.directory / "backups"
        latest = backups / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        latest.mkdir(parents=True)
        for name in ("postgres.age", "mongodb.age", "garage.age", "SHA256SUMS"):
            (latest / name).touch()
        (latest / "MANIFEST").write_text("format_version=3\n")
        config = {**checker.CONFIG, **self.config}
        config["healthAlerts"]["failureThreshold"] = 1

        def command(*args, **_kwargs):
            if args[1:3] == ("server", "list"):
                return json.dumps(
                    [{"Name": name, "Status": "ACTIVE"} for name in config["hosts"].values()]
                )
            if args[1:3] == ("node", "status"):
                return "[]"
            if args[1] == "status":
                return '{"configured":true,"verified":true}'
            return ""

        with (
            mock.patch.object(checker, "CONFIG", config),
            mock.patch.object(checker, "STATUS", self.directory / "snapshot.json"),
            mock.patch.object(checker, "BACKUPS", backups),
            mock.patch.object(checker, "command", side_effect=command),
            mock.patch.object(checker, "bounded_request", return_value=b"OK"),
            mock.patch("lib.health_alerts.send", return_value=False),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checker.main(), 0)
            with (
                mock.patch.object(checker, "command", side_effect=RuntimeError("sentinel")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(checker.main(), 1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checker.main(), 0)


if __name__ == "__main__":
    unittest.main()
