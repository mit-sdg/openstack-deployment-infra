"""Keep copy/paste deployment examples syntactically valid and schema-checked."""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

from openstack_platform.controller.deployment_config import parse_configuration

ROOT = Path(__file__).resolve().parents[1]


class ApplicationDeploymentDocumentationTests(unittest.TestCase):
    def test_bash_examples_parse(self):
        document = (ROOT / "docs/APPLICATION_DEPLOYMENTS.md").read_text()
        blocks = re.findall(r"```bash\n(.*?)\n```", document, re.S)
        self.assertGreaterEqual(len(blocks), 6)
        for index, block in enumerate(blocks):
            with self.subTest(block=index):
                subprocess.run(
                    ["bash", "-n"], input=block, text=True, check=True, capture_output=True
                )

    def test_example_configuration_uses_the_real_parser(self):
        document = (ROOT / "docs/APPLICATION_DEPLOYMENTS.md").read_text()
        blocks = re.findall(r"```json\n(.*?)\n```", document, re.S)
        self.assertEqual(len(blocks), 2)
        configurations = [parse_configuration(json.loads(block)) for block in blocks]
        self.assertEqual(
            [configuration.runtime for configuration in configurations], ["bun", "node"]
        )
        self.assertIsNone(configurations[0].runtime_files)
        self.assertEqual(configurations[1].runtime_files, ("dist", "node_modules", "package.json"))
        for configuration in configurations:
            self.assertEqual(configuration.port, 3000)
            self.assertEqual(configuration.health_path, "/health")
