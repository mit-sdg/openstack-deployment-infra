"""The CI upload boundary excludes credentials and unbounded browser artifacts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.collect_owner_portal_artifacts import collect, sanitized_diagnostic


class ArtifactTests(unittest.TestCase):
    def test_diagnostic_drops_credential_fields_and_refuses_query_strings(self) -> None:
        row = {
            "method": "GET",
            "path": "/api/v1/apps",
            "status": 500,
            "headers": {"cookie": "fixture-only"},
            "error": {"code": "INTERNAL_ERROR", "summary": "fixture-only"},
        }
        payload = sanitized_diagnostic(json.dumps({"version": 1, "failures": [row]}).encode())
        self.assertNotIn(b"fixture-only", payload)
        row["path"] += "?token=fixture-only"
        with self.assertRaises(ValueError):
            sanitized_diagnostic(json.dumps({"version": 1, "failures": [row]}).encode())

    def test_upload_only_allows_named_direct_files_with_individual_and_total_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            screenshots = root / "screenshots"
            screenshots.mkdir()
            payload = b"\x89PNG\r\n\x1a\n" + b"fixture" * 3
            (screenshots / "desktop-light-dashboard.png").write_bytes(payload)
            (screenshots / "desktop-dark-dashboard.png").write_bytes(payload)
            (screenshots / "mobile-light-dashboard.png").symlink_to("desktop-light-dashboard.png")
            (screenshots / "mobile-dark-dashboard.png").write_bytes(b"x" * 100)
            (screenshots / "trace.zip").write_bytes(b"fixture-only")
            with (
                patch("tests.collect_owner_portal_artifacts.MAXIMUM_TOTAL", len(payload)),
                patch("tests.collect_owner_portal_artifacts.MAXIMUM_FILE", 64),
            ):
                count, size = collect(root)
            self.assertEqual((count, size), (1, len(payload)))
            self.assertEqual(len(list((root / "upload").iterdir())), 1)
