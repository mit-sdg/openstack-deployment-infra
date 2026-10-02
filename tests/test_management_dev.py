"""Long-checkout harness sockets, development confinement and startup cleanup."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openstack_platform.controller.http import ControllerServer, Router
from openstack_platform.management.config import Config
from openstack_platform.management.dev.__main__ import main
from openstack_platform.management.identity.client import IdentityConfig

ROOT = Path(__file__).resolve().parents[1]


class ManagementDevelopmentTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.state = self.enterContext(tempfile.TemporaryDirectory(dir=ROOT / ".tmp"))
        self.root = Path(self.state)
        self.socket_root = Path(
            self.enterContext(
                tempfile.TemporaryDirectory(
                    prefix=f"owner-portal-sockets-{os.geteuid()}-", dir="/tmp"
                )
            )
        )

    def load(self, **overrides: object) -> Config:
        value = {
            "portalOrigin": "http://127.0.0.1:9443",
            "commonsOrigin": "https://localhost:9444",
            "stateDirectory": str(self.root / "broker"),
            "brokerSocket": str(self.socket_root / "broker.sock"),
            "controllerSocket": str(self.socket_root / "project.sock"),
            "identitySocket": str(self.socket_root / "identity.sock"),
            "development": True,
            **overrides,
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(value))
        return Config.load(path)

    def identity(self, socket: Path) -> IdentityConfig:
        path = self.root / "identity.json"
        path.write_text(
            json.dumps(
                {
                    "commonsOrigin": "https://localhost:9444",
                    "socket": str(socket),
                    "development": True,
                }
            )
        )
        return IdentityConfig.load(path)

    def test_all_sockets_bind_in_private_directory_from_long_checkout(self) -> None:
        checkout = Path("/tmp") / ("deep-checkout-" + "x" * 100)
        with patch("pathlib.Path.cwd", return_value=checkout):
            config = self.load(stateDirectory=str(checkout / ".tmp/state"))
            self.identity(config.identity_socket)
        metadata = self.socket_root.lstat()
        self.assertEqual(metadata.st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)
        for path in (config.identity_socket, config.broker_socket, config.controller_socket):
            with self.subTest(path=path):
                self.assertLessEqual(len(os.fsencode(path)), 107)
                server = ControllerServer(str(path), Router())
                server.server_close()
                self.assertFalse(path.exists())

    def test_development_rejects_shared_foreign_and_symlink_socket_directories(self) -> None:
        socket = self.socket_root / "identity.sock"
        for mode in (0o755, 0o710, 0o770):
            self.socket_root.chmod(mode)
            with self.assertRaisesRegex(ValueError, "private harness directory"):
                self.load()
            with self.assertRaisesRegex(ValueError, "private harness directory"):
                self.identity(socket)
        self.socket_root.chmod(0o700)
        with patch(
            "openstack_platform.management.config.os.geteuid", return_value=os.geteuid() + 1
        ):
            with self.assertRaisesRegex(ValueError, "private harness directory"):
                self.load()
        link = self.socket_root.with_name(self.socket_root.name + "-link")
        link.symlink_to(self.socket_root, target_is_directory=True)
        try:
            with self.assertRaisesRegex(ValueError, "private harness directory"):
                self.load(brokerSocket=str(link / "broker.sock"))
            with self.assertRaisesRegex(ValueError, "private harness directory"):
                self.identity(link / "identity.sock")
        finally:
            link.unlink()
        with self.assertRaisesRegex(ValueError, "private harness directory"):
            self.load(brokerSocket="/tmp/broker.sock")
        with self.assertRaisesRegex(ValueError, "development state"):
            self.load(stateDirectory=str(self.socket_root / "broker"))

    def test_existing_worktree_socket_configuration_remains_supported(self) -> None:
        config = self.load(
            brokerSocket=str(self.root / "b.sock"),
            controllerSocket=str(self.root / "c.sock"),
            identitySocket=str(self.root / "i.sock"),
        )
        self.assertEqual(self.identity(config.identity_socket).socket, config.identity_socket)

    def test_socket_length_errors_identify_field_and_byte_limit(self) -> None:
        path = self.socket_root / ("x" * 108)
        for name in ("brokerSocket", "controllerSocket", "identitySocket"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, name + ".*107"):
                self.load(**{name: str(path)})
        with self.assertRaisesRegex(ValueError, "identity.*107"):
            self.identity(path)
        with self.assertRaisesRegex(ValueError, "Unix socket path.*107"):
            ControllerServer(str(path), Router())
        unicode_path = self.socket_root / ("é" * 50)
        with self.assertRaisesRegex(ValueError, "brokerSocket.*107"):
            self.load(brokerSocket=str(unicode_path))

    def test_partial_startup_failure_closes_sockets_and_removes_private_directory(self) -> None:
        is_file = Path.is_file
        assets = ROOT / "frontend/owner-portal/dist/index.html"
        with (
            patch("sys.argv", ["portal-dev", "--http", "--state", str(self.root)]),
            patch(
                "openstack_platform.management.dev.__main__.HarnessWeb",
                side_effect=RuntimeError("fixture startup failure"),
            ),
            patch("pathlib.Path.is_file", new=lambda path: path == assets or is_file(path)),
            self.assertRaisesRegex(RuntimeError, "fixture startup failure"),
        ):
            main()
        value = json.loads((self.root / "config.json").read_text())
        sockets = [
            Path(value[name]) for name in ("brokerSocket", "controllerSocket", "identitySocket")
        ]
        self.assertEqual(len({path.parent for path in sockets}), 1)
        self.assertFalse(sockets[0].parent.exists())


if __name__ == "__main__":
    unittest.main()
