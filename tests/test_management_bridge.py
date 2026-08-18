from __future__ import annotations

import importlib.util
import io
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "setup_management_bridge", ROOT / "deploy/platform-cli/setup_management_bridge.py"
)
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class ManagementBridgeTests(unittest.TestCase):
    def test_atomic_outputs_are_private_and_replace_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            directory = root / "ssh"
            bridge._private_directory(directory)
            destination = directory / "config"
            target = root / "outside"
            target.write_text("keep")
            destination.symlink_to(target)
            bridge._atomic_write(destination, b"Host platform-admin\n", mode=0o600)
            self.assertEqual(destination.read_bytes(), b"Host platform-admin\n")
            self.assertEqual(target.read_text(), "keep")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_provider_wrapper_requires_a_protected_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = root / "platform-openstack"
            wrapper.write_text("#!/bin/sh\nexit 0\n")
            wrapper.chmod(0o700)
            bridge._verify_executable(wrapper, label="wrapper")

            outside = root / "outside"
            outside.write_text("#!/bin/sh\nexit 0\n")
            outside.chmod(0o700)
            linked = root / "linked-wrapper"
            linked.symlink_to(outside)
            with self.assertRaisesRegex(bridge.BridgeError, "Nix-store"):
                bridge._verify_executable(linked, label="wrapper")

    def test_preflight_automates_local_bridge_prerequisites_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / "ssh"
            ssh.mkdir(mode=0o700)
            identity = ssh / "identity"
            identity.write_bytes(b"private-key-fixture")
            identity.chmod(0o600)
            wrapper = root / "platform-openstack"
            wrapper.write_text("#!/bin/sh\nexit 0\n")
            wrapper.chmod(0o700)
            output = io.StringIO()
            with redirect_stdout(output):
                bridge.preflight(
                    bridge.parser().parse_args(
                        [
                            "--preflight",
                            "--ssh-identity",
                            str(identity),
                            "--ssh-config",
                            str(ssh / "config"),
                            "--known-hosts",
                            str(ssh / "known_hosts"),
                            "--provider-command",
                            str(wrapper),
                        ]
                    )
                )
            self.assertEqual(output.getvalue(), "management-bridge=prerequisites-ready\n")

    def test_paths_and_console_fingerprint_are_strict(self) -> None:
        with self.assertRaises(bridge.BridgeError):
            bridge._safe_path(Path("relative/config"), label="config")
        with self.assertRaises(bridge.BridgeError):
            bridge._safe_path(Path("/tmp/has space"), label="config")
        self.assertEqual(
            bridge._console_fingerprint(
                b"noise\nED25519 SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
            ),
            "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        with self.assertRaisesRegex(bridge.BridgeError, "ED25519"):
            bridge._console_fingerprint(b"RSA SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")


if __name__ == "__main__":
    unittest.main()
