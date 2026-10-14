"""Page-sized controller transport capacity and a bounded background share."""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from openstack_platform.controller.http import PeerPolicy
from openstack_platform.controller.main import build_parser
from openstack_platform.management.broker.client import (
    BACKGROUND_CONNECTION_CAPACITY,
    PROJECT_CONNECTION_CAPACITY,
    ControllerUnavailable,
    ProjectClient,
)


class ProjectClientCapacityTests(unittest.TestCase):
    def response(self) -> Mock:
        response = Mock(status=200)
        response.getheader.side_effect = {
            "Content-Length": "2",
            "Content-Type": "application/json",
        }.get
        response.read.return_value = b"{}"
        return response

    def test_page_bursts_fill_24_connections_and_excess_waits_then_times_out(self) -> None:
        client = ProjectClient(Path("/unused.sock"), timeout=0.3)
        release = threading.Event()
        started = threading.Condition()
        count = 0

        def reply() -> Mock:
            nonlocal count
            with started:
                count += 1
                started.notify_all()
            if not release.wait(5):
                raise AssertionError("held controller requests were not released")
            return self.response()

        def connection(*args: Any) -> Mock:
            return Mock(getresponse=Mock(side_effect=reply))

        with patch(
            "openstack_platform.management.broker.client.UnixConnection", side_effect=connection
        ):
            with ThreadPoolExecutor(max_workers=PROJECT_CONNECTION_CAPACITY + 1) as pool:
                try:
                    held = [
                        pool.submit(client.request, "GET", "/v1/held")
                        for _ in range(PROJECT_CONNECTION_CAPACITY)
                    ]
                    with started:
                        self.assertTrue(
                            started.wait_for(lambda: count == PROJECT_CONNECTION_CAPACITY, 3)
                        )
                    excess = pool.submit(client.request, "GET", "/v1/excess")
                    with self.assertRaisesRegex(ControllerUnavailable, "local connection capacity"):
                        excess.result(timeout=1)
                    self.assertEqual(count, PROJECT_CONNECTION_CAPACITY)
                finally:
                    release.set()
                self.assertEqual([read.result(timeout=1) for read in held], [(200, {})] * 24)
            self.assertEqual(client.request("GET", "/v1/after"), (200, {}))

    def test_background_cannot_take_foreground_share(self) -> None:
        client = ProjectClient(Path("/unused.sock"), timeout=0.3)
        release = threading.Event()
        started = threading.Condition()
        background = foreground = 0

        def reply() -> Mock:
            nonlocal background, foreground
            with started:
                if client.background.get():
                    background += 1
                else:
                    foreground += 1
                started.notify_all()
            if not release.wait(5):
                raise AssertionError("held controller requests were not released")
            return self.response()

        def read_background() -> tuple[int, dict[str, Any]]:
            with client.background_reads():
                return client.request("GET", "/v1/background")

        with patch("openstack_platform.management.broker.client.UnixConnection") as connection:
            connection.side_effect = lambda *args: Mock(getresponse=Mock(side_effect=reply))
            with ThreadPoolExecutor(max_workers=25) as pool:
                try:
                    held = [
                        pool.submit(read_background) for _ in range(BACKGROUND_CONNECTION_CAPACITY)
                    ]
                    with started:
                        self.assertTrue(started.wait_for(lambda: background == 4, 3))
                    excess = pool.submit(read_background)
                    held += [
                        pool.submit(client.request, "GET", "/v1/foreground") for _ in range(20)
                    ]
                    with started:
                        self.assertTrue(started.wait_for(lambda: foreground == 20, 3))
                    with self.assertRaises(ControllerUnavailable):
                        excess.result(timeout=1)
                    self.assertEqual(background, 4)
                finally:
                    release.set()
                self.assertTrue(all(read.result(timeout=1) == (200, {}) for read in held))

    def test_failure_releases_both_slots_and_background_context(self) -> None:
        client = ProjectClient(Path("/unused.sock"), timeout=0.1, capacity=2)
        with patch("openstack_platform.management.broker.client.UnixConnection") as connection:
            connection.return_value.getresponse.side_effect = OSError("lost response")
            with self.assertRaises(ControllerUnavailable), client.background_reads():
                client.request("GET", "/v1/health")
            self.assertFalse(client.background.get())
            connection.return_value.getresponse.side_effect = None
            connection.return_value.getresponse.return_value = self.response()
            with client.background_reads():
                self.assertEqual(client.request("GET", "/v1/health"), (200, {}))
            self.assertEqual(client.request("GET", "/v1/health"), (200, {}))

    def test_controller_default_and_cli_have_headroom_above_client_capacity(self) -> None:
        policy = PeerPolicy(frozenset({(100, 100)}))
        self.assertEqual(policy.max_connections_per_peer, 32)
        self.assertEqual(build_parser().parse_args([]).max_connections_per_peer, 32)
        self.assertGreater(policy.max_connections_per_peer, PROJECT_CONNECTION_CAPACITY)
        admin_role = (Path(__file__).resolve().parents[1] / "nix/roles/admin.nix").read_text()
        self.assertIn(f'"--max-connections-per-peer {policy.max_connections_per_peer}"', admin_role)
