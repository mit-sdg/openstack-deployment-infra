from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openstack_platform import durable


class DurableReplacementProperties(unittest.TestCase):
    def test_every_interruption_stage_has_a_deterministic_retry_state(self) -> None:
        stages = (
            "before_write",
            "after_write",
            "after_file_fsync",
            "after_rename",
            "after_directory_fsync",
        )
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                destination = root / "state"
                durable.atomic_write(destination, b"old", mode=0o600, maximum_bytes=16)

                def interrupt(observed: str, selected: str = stage) -> None:
                    if observed == selected:
                        raise OSError("injected interruption")

                with self.assertRaises(durable.DurableReplaceError):
                    durable.atomic_write(
                        destination,
                        b"new",
                        mode=0o600,
                        maximum_bytes=16,
                        fault=interrupt,
                    )
                self.assertIn(destination.read_bytes(), {b"old", b"new"})
                durable.atomic_write(destination, b"new", mode=0o600, maximum_bytes=16)
                self.assertEqual(destination.read_bytes(), b"new")
                self.assertFalse((root / ".state.tmp").exists())

    def test_stale_symlink_and_unexpected_destination_type_are_never_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            victim = root / "victim"
            victim.write_bytes(b"secret")
            stale = root / ".state.tmp"
            stale.symlink_to(victim)
            with self.assertRaises(durable.DurableReplaceError):
                durable.atomic_write(root / "state", b"new", mode=0o600, maximum_bytes=16)
            self.assertTrue(stale.is_symlink())
            self.assertEqual(victim.read_bytes(), b"secret")

            destination = root / "directory"
            destination.mkdir()
            with self.assertRaises(durable.DurableReplaceError):
                durable.atomic_write(destination, b"new", mode=0o600, maximum_bytes=16)
            self.assertTrue(destination.is_dir())


class SecretDiagnosticProperty(unittest.TestCase):
    def test_durable_errors_do_not_include_payload_or_paths(self) -> None:
        secret = b"diagnostic-secret-value"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "state"
            destination.mkdir()
            with self.assertRaises(durable.DurableReplaceError) as captured:
                durable.atomic_write(destination, secret, mode=0o600, maximum_bytes=1024)
            rendered = str(captured.exception)
            self.assertNotIn(secret.decode(), rendered)
            self.assertNotIn(str(destination), rendered)


if __name__ == "__main__":
    unittest.main()
