"""The browser's pre-deploy check (owner-portal utils/preflight.ts) and the
build's validate_checkout must agree on the shared cases."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from openstack_platform.controller.deployment_config import (
    checkout_checks,
    parse_configuration,
    validate_checkout,
)

CASES = Path(__file__).resolve().parents[1] / "frontend/owner-portal/src/utils/preflight-cases.json"


class CheckoutPreflightParityTests(unittest.TestCase):
    def test_build_rules_match_the_browser_preflight_cases(self) -> None:
        cases = json.loads(CASES.read_text())
        self.assertGreater(len(cases), 10)
        for case in cases:
            with self.subTest(case["name"]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name, content in case["files"].items():
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                for name, size in case.get("sizes", {}).items():
                    os.truncate(root / name, size)
                for name, target in case.get("links", {}).items():
                    (root / name).symlink_to(target)
                configuration = parse_configuration(
                    {
                        "schemaVersion": 1,
                        "build": {
                            "runtime": case["configuration"]["runtime"],
                            "packages": case["configuration"]["packages"],
                            "buildScript": case["configuration"]["buildScript"],
                            "startScript": case["configuration"]["startScript"],
                        },
                        "runtime": {"port": 3000, "healthPath": "/health"},
                        "storageBindings": [],
                    }
                )
                named = checkout_checks(configuration, root)
                self.assertEqual(
                    sorted(check["id"] for check in named if check["state"] == "problem"),
                    sorted(case["problems"]),
                )
                if case["problems"]:
                    with self.assertRaises((ValueError, OSError)):
                        validate_checkout(configuration, root)
                else:
                    validate_checkout(configuration, root)


if __name__ == "__main__":
    unittest.main()
