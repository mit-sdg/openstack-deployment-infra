from __future__ import annotations

import copy
import json
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from openstack_platform import openstack, operator, remote, runtime
from openstack_platform.config import Config, load_platform, load_policy
from tests.test_platform_openstack import FLAVOR, OLD_IMAGE, SERVER, FakeCloud

ROOT = Path(__file__).resolve().parents[1]


def configuration() -> Config:
    return Config(
        load_platform(ROOT / "config/platform.example.json"),
        load_policy(ROOT / "config/platform-policy.example.json", require_private=False),
    )


class IngressOperatorHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.platform = configuration().platform
        self.cloud = FakeCloud(self.platform, role="ingress")
        self.host = openstack.PersistentHost(
            "ingress",
            self.platform.get("hosts.ingress"),
            SERVER,
            "ACTIVE",
            OLD_IMAGE,
            FLAVOR,
            "example.2c2g",
            (),
        )
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def check(self, http, seconds=6):
        openstack.check_role_health(
            self.platform,
            "ingress",
            self.host,
            seconds,
            provider_runner=self.cloud,
            http_get=http,
            sleep=self.sleep,
            clock=lambda: self.now,
        )

    def test_tunnel_health_never_requires_a_public_origin_listener(self) -> None:
        self.assertEqual(self.platform.get("publicIngress.mode"), "tunnel")
        http = mock.Mock(return_value=runtime.HttpResult(200, {}, b"OK\n"))
        self.check(http)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args, (f"https://{self.platform.domain}/healthz",))
        self.assertFalse(http.call_args.kwargs["allow_redirects"])
        self.assertEqual(http.call_args.kwargs["response_limit"], 64)
        self.assertEqual(self.now, 0)

    def test_connector_warmup_retries_without_weakening_public_health(self) -> None:
        http = mock.Mock(
            side_effect=[
                OSError("private edge diagnostic"),
                runtime.HttpResult(503, {}, b"not ready"),
                runtime.HttpResult(200, {}, b"OK"),
            ]
        )
        self.check(http)
        self.assertEqual(http.call_count, 3)
        self.assertEqual(self.now, 4)
        self.assertTrue(all(call.kwargs["timeout_seconds"] <= 2 for call in http.call_args_list))

    def test_unhealthy_public_route_fails_at_original_deadline_without_body_output(self) -> None:
        http = mock.Mock(return_value=runtime.HttpResult(200, {}, b"private wrong response"))
        with self.assertRaises(openstack.OpenStackError) as error:
            self.check(http, seconds=4)
        self.assertIn("deadline", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.now, 4)
        self.assertEqual(http.call_count, 2)


class HostedOperatorStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = configuration()
        self.model = {
            "state": "healthy",
            "accepted": {"infrastructureRoles": 5, "applications": 1, "storageResources": 1},
            "observations": {"available": 5, "unavailable": 0, "unhealthy": 0},
            "operations": {"incomplete": 0, "builders": 0, "items": []},
        }

    def reply(self, model=None):
        return runtime.CommandResult(
            argv=("ssh",),
            returncode=0,
            stdout=json.dumps(self.model if model is None else model).encode(),
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    def test_status_uses_pinned_hosted_api_not_external_shadow_products(self) -> None:
        output = StringIO()
        with (
            mock.patch.object(runtime, "run", return_value=self.reply()) as run,
            mock.patch.object(operator.db, "list_image_selections", return_value=[object()] * 5),
            mock.patch.object(operator.status, "incomplete_operations", return_value=[]),
            mock.patch.object(
                operator.status, "status_show_live", side_effect=AssertionError("shadow read")
            ),
        ):
            operator._status_command(mock.Mock(), self.config, output=output)
        self.assertIn("healthy", output.getvalue())
        args = run.call_args.args[0]
        self.assertEqual(
            args[:5], ("ssh", "-F", remote.DEFAULT_SSH_CONFIG.as_posix(), "platform-admin", "--")
        )
        self.assertIn("/privileged.sock", args[-1])
        self.assertIn("http://localhost/v1/admin/status", args[-1])
        self.assertLessEqual(run.call_args.kwargs["stdout_limit"], 65536)

    def test_unfinished_external_infrastructure_still_degrades_status(self) -> None:
        output = StringIO()
        with (
            mock.patch.object(operator, "_hosted_status", return_value=copy.deepcopy(self.model)),
            mock.patch.object(operator.db, "list_image_selections", return_value=[object()] * 5),
            mock.patch.object(operator.status, "incomplete_operations", return_value=[object()]),
        ):
            operator._status_command(mock.Mock(), self.config, output=output)
        self.assertIn("degraded", output.getvalue())

    def test_bad_or_truncated_counts_are_rejected_without_exposing_body(self) -> None:
        bad = copy.deepcopy(self.model)
        bad["accepted"]["applications"] = True
        for reply in (
            self.reply(bad),
            runtime.CommandResult(
                argv=("ssh",),
                returncode=0,
                stdout=b"private-body",
                stderr=b"",
                stdout_truncated=True,
                stderr_truncated=False,
            ),
        ):
            with mock.patch.object(runtime, "run", return_value=reply):
                with self.assertRaises(remote.ProtocolError) as error:
                    operator._hosted_status(self.config)
                self.assertNotIn("private-body", str(error.exception))

    def test_hosted_outage_does_not_fall_back_to_stale_external_state(self) -> None:
        with mock.patch.object(
            runtime, "run", side_effect=runtime.CommandFailure("secret=private")
        ):
            with self.assertRaises(remote.DependencyUnavailable) as error:
                operator._hosted_status(self.config)
        self.assertNotIn("private", str(error.exception))


if __name__ == "__main__":
    unittest.main()
