from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import struct
import tempfile
import unittest
from pathlib import Path

from openstack_platform import host_keys, runtime
from openstack_platform.runtime import CommandResult

ADDRESS = "192.0.2.11"


def ed25519_blob(value: bytes) -> bytes:
    raw = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + value
    return base64.b64encode(raw)


def console_fingerprint(encoded_key: bytes) -> bytes:
    raw = base64.b64decode(encoded_key)
    fingerprint = base64.b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
    return b"host key fingerprint SHA256:" + fingerprint + b" operator (ED25519)\n"


class ConfigAndKeyscanRunner:
    def __init__(self, encoded_key: bytes) -> None:
        self.encoded_key = encoded_key
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: object, **kwargs: object) -> CommandResult:
        assert isinstance(argv, tuple)
        self.calls.append(argv)
        if argv[0] == "ssh":
            return runtime.run(argv, **kwargs)  # type: ignore[arg-type]
        if argv[0] == "ssh-keyscan":
            assert argv[-1] == ADDRESS
            assert argv[-3:-1] == ("-t", "ed25519")
            return CommandResult(
                argv,
                0,
                ADDRESS.encode() + b" ssh-ed25519 " + self.encoded_key + b"\n",
                b"",
                False,
                False,
            )
        raise AssertionError("unexpected host-key command")


class HostKeyPinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.known_hosts = self.directory / "exact-known-hosts"
        self.decoy_known_hosts = self.directory / "decoy-known-hosts"
        self.config = self.directory / "admin-ssh-config"
        self.old_key = ed25519_blob(b"O" * 32)
        self.new_key = ed25519_blob(b"N" * 32)
        self.known_hosts.write_bytes(
            b"198.51.100.8 ssh-ed25519 "
            + ed25519_blob(b"U" * 32)
            + b"\n"
            + ADDRESS.encode()
            + b" ssh-ed25519 "
            + self.old_key
            + b"\n"
        )
        self.decoy_known_hosts.write_bytes(b"decoy unchanged\n")
        self.known_hosts.chmod(0o600)
        self.decoy_known_hosts.chmod(0o600)
        self.config.write_text(
            "Host platform-admin\n"
            f"  HostName {ADDRESS}\n"
            "  Port 22\n"
            "  StrictHostKeyChecking yes\n"
            "  CheckHostIP no\n"
            f"  UserKnownHostsFile {self.known_hosts}\n"
        )

    def test_real_ssh_config_resolution_updates_only_exact_pin_atomically(self) -> None:
        runner = ConfigAndKeyscanRunner(self.new_key)
        before_inode = self.known_hosts.stat().st_ino
        unrelated = self.known_hosts.read_bytes().splitlines()[0]
        output = io.StringIO()

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            host_keys.pin_verified_admin_host_key(
                ADDRESS,
                console_fingerprint(self.new_key),
                ssh_config_path=self.config,
                command_runner=runner,
            )

        records = self.known_hosts.read_bytes().splitlines()
        self.assertEqual(records[0], unrelated)
        self.assertEqual(records[1], ADDRESS.encode() + b" ssh-ed25519 " + self.new_key)
        self.assertNotEqual(self.known_hosts.stat().st_ino, before_inode)
        self.assertEqual(self.known_hosts.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.decoy_known_hosts.read_bytes(), b"decoy unchanged\n")
        self.assertEqual([call[0] for call in runner.calls], ["ssh", "ssh-keyscan", "ssh"])
        self.assertEqual(output.getvalue(), "")

    def test_unverified_scan_is_rejected_without_update_or_fingerprint_output(self) -> None:
        runner = ConfigAndKeyscanRunner(self.new_key)
        original = self.known_hosts.read_bytes()
        before = self.known_hosts.stat()
        output = io.StringIO()

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(host_keys.HostKeyError) as caught:
                host_keys.pin_verified_admin_host_key(
                    ADDRESS,
                    console_fingerprint(self.old_key),
                    ssh_config_path=self.config,
                    command_runner=runner,
                )

        after = self.known_hosts.stat()
        self.assertEqual(self.known_hosts.read_bytes(), original)
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertNotIn("SHA256", str(caught.exception))
        self.assertNotIn(self.new_key.decode(), str(caught.exception))
        self.assertEqual(output.getvalue(), "")

    def test_matching_wildcard_old_key_is_removed_before_new_pin(self) -> None:
        self.known_hosts.write_bytes(b"192.0.2.* ssh-ed25519 " + self.old_key + b"\n")
        runner = ConfigAndKeyscanRunner(self.new_key)

        host_keys.pin_verified_admin_host_key(
            ADDRESS,
            console_fingerprint(self.new_key),
            ssh_config_path=self.config,
            command_runner=runner,
        )

        self.assertEqual(
            self.known_hosts.read_bytes(),
            ADDRESS.encode() + b" ssh-ed25519 " + self.new_key + b"\n",
        )

    def test_config_with_multiple_known_hosts_files_fails_closed(self) -> None:
        self.config.write_text(
            "Host platform-admin\n"
            f"  HostName {ADDRESS}\n"
            "  StrictHostKeyChecking yes\n"
            f"  UserKnownHostsFile {self.known_hosts} {self.decoy_known_hosts}\n"
        )
        original = self.known_hosts.read_bytes()

        with self.assertRaises(host_keys.HostKeyError):
            host_keys.pin_verified_admin_host_key(
                ADDRESS,
                console_fingerprint(self.new_key),
                ssh_config_path=self.config,
                command_runner=ConfigAndKeyscanRunner(self.new_key),
            )

        self.assertEqual(self.known_hosts.read_bytes(), original)
        self.assertEqual(self.decoy_known_hosts.read_bytes(), b"decoy unchanged\n")


if __name__ == "__main__":
    unittest.main()
