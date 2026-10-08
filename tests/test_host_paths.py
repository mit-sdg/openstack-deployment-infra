"""Privileged preparation rejects links and changes only verified open handles."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openstack_platform import host_paths

ROOT = Path(__file__).resolve().parents[1]


class HostPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="host-paths-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.source = self.root / "source"
        self.source.write_bytes(b"public fixture policy\n")
        self.source.chmod(0o600)
        self.folder = self.root / "service"
        self.folder.mkdir(mode=0o700)
        self.destination = self.folder / "policy.json"

    def copy(self) -> None:
        host_paths.copy(self.source, self.destination, self.uid, self.uid, self.gid)

    def test_regular_copy_and_repeated_handle_normalization(self) -> None:
        self.copy()
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.destination.stat().st_mode & 0o7777, 0o600)
        for _ in range(2):
            host_paths.normalize(self.source, self.uid, {self.gid}, {0o600, 0o640}, self.gid, 0o640)
            host_paths.check(self.source, self.uid, {self.gid}, {0o640})
        self.assertEqual(self.source.stat().st_mode & 0o7777, 0o640)
        self.assertEqual(list(self.folder.glob(".prepare-*")), [])

    def test_linked_parent_source_destination_and_hardlink_are_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir(mode=0o755)
        protected = outside / "protected"
        protected.write_bytes(b"protected fixture\n")
        protected.chmod(0o644)
        original = protected.read_bytes(), protected.stat().st_mode, outside.stat().st_mode
        self.destination.symlink_to(protected)
        self.copy()
        self.assertFalse(self.destination.is_symlink())
        self.destination.unlink()
        self.folder.rmdir()
        self.folder.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.copy()
        self.folder.unlink()
        self.folder.mkdir(mode=0o700)
        self.source.unlink()
        self.source.symlink_to(protected)
        with self.assertRaises(ValueError):
            self.copy()
        self.source.unlink()
        os.link(protected, self.source)
        with self.assertRaises(ValueError):
            host_paths.normalize(self.source, self.uid, {self.gid}, {0o644}, self.gid, 0o600)
        self.assertEqual(
            (protected.read_bytes(), protected.stat().st_mode, outside.stat().st_mode), original
        )

    def test_replacement_after_check_cannot_redirect_metadata_changes(self) -> None:
        outside = self.root / "outside"
        outside.write_bytes(b"unchanged\n")
        outside.chmod(0o644)
        previous = self.root / "previous"
        chown = host_paths.os.fchown

        def swap(fd, uid, gid):
            self.source.rename(previous)
            self.source.symlink_to(outside)
            chown(fd, uid, gid)

        with patch.object(host_paths.os, "fchown", side_effect=swap):
            host_paths.normalize(self.source, self.uid, {self.gid}, {0o600}, self.gid, 0o640)
        self.assertEqual(previous.stat().st_mode & 0o7777, 0o640)
        self.assertEqual(outside.stat().st_mode & 0o7777, 0o644)
        self.assertEqual(outside.read_bytes(), b"unchanged\n")

    def test_wrong_owner_or_mode_never_reaches_mutation(self) -> None:
        with patch.object(
            host_paths.os, "fchown", side_effect=AssertionError("unexpected mutation")
        ):
            for uid, modes in ((self.uid + 1, {0o600}), (self.uid, {0o644})):
                with self.assertRaises(ValueError):
                    host_paths.normalize(self.source, uid, {self.gid}, modes, self.gid, 0o640)

    def test_fifo_source_is_rejected_without_waiting_for_a_writer(self) -> None:
        self.source.unlink()
        os.mkfifo(self.source, 0o600)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                f"import sys; sys.path.insert(0,{str(ROOT)!r}); from openstack_platform.host_paths import check; from pathlib import Path; check(Path({str(self.source)!r}),{self.uid},{{{self.gid}}},{{0o600}})",
            ],
            capture_output=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"object type", result.stderr)

    def test_preflight_reports_all_refusals_and_leaves_bytes_modes_and_times_identical(
        self,
    ) -> None:
        public = self.root / "public.pem"
        public.write_bytes(b"do not capture contents")
        public.chmod(0o600)
        self.source.chmod(0o640)
        plan = {
            "version": 1,
            "operations": [
                {
                    "kind": "copy",
                    "source": str(self.source),
                    "destination": str(self.destination),
                    "sourceUid": self.uid,
                    "uid": self.uid,
                    "gid": self.gid,
                },
                {"kind": "check", "path": str(public), "uid": self.uid, "allowedModes": [0o644]},
            ],
        }
        before = [
            (p.stat().st_mode, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
            for p in (self.source, public)
        ]
        with patch.object(host_paths.os, "fchown", side_effect=AssertionError("preflight mutated")):
            errors = host_paths.preflight(plan)
        self.assertEqual(len(errors), 2)
        for path in (self.source, public):
            self.assertTrue(any(str(path) in error for error in errors))
        self.assertTrue(
            all("expected" in error and "uid:gid:mode:nlink=" in error for error in errors)
        )
        self.assertNotIn("do not capture contents", "\n".join(errors))
        self.assertEqual(
            before,
            [
                (p.stat().st_mode, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                for p in (self.source, public)
            ],
        )
        self.assertFalse(self.destination.exists())
