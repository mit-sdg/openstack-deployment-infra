from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_persistent_host", ROOT / "infra/openstack/verify_persistent_host.py"
)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class PersistentHostProjectionTests(unittest.TestCase):
    def test_flavor_name_accepts_mapping_and_openstack_cli_string_projections(self) -> None:
        identifier = "00000000-0000-4000-8000-000000000001"

        self.assertEqual(
            verifier.resource_name({"id": identifier, "original_name": "example.2c4g"}, "flavor"),
            "example.2c4g",
        )
        self.assertEqual(
            verifier.resource_name(f"example.2c4g ({identifier})", "flavor"),
            "example.2c4g",
        )

    def test_flavor_name_rejects_ambiguous_string_projection(self) -> None:
        with self.assertRaisesRegex(SystemExit, "malformed"):
            verifier.resource_name("example.2c4g", "flavor")


if __name__ == "__main__":
    unittest.main()
