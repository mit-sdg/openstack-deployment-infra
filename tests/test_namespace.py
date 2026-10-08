from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "infra" / "lib" / "platform_config.py"
SPEC = importlib.util.spec_from_file_location("platform_config", HELPER_PATH)
assert SPEC and SPEC.loader
platform_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(platform_config)


class PlatformConfigNamespaceTests(unittest.TestCase):
    def load_document(self, document: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.json"
            path.write_text(json.dumps(document))
            with patch.dict(os.environ, {"PLATFORM_CONFIG": str(path)}):
                return platform_config.load()

    def example(self) -> dict[str, object]:
        return json.loads((ROOT / "config" / "platform.example.json").read_text())

    def test_example_namespace_exports(self) -> None:
        for field, invalid in (
            ("displayName", "Invalid/Platform"),
            ("organization", "Invalid/Organization"),
            ("namespace", "Invalid Namespace"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load_document({**self.example(), field: invalid})
        document = self.load_document(self.example())
        values = platform_config.shell_values(document)
        self.assertEqual(values["PLATFORM_DISPLAY_NAME"], "Example Platform")
        self.assertEqual(values["PLATFORM_ORGANIZATION"], "Example Organization")
        self.assertEqual(values["PLATFORM_NAMESPACE"], "app-platform")
        self.assertEqual(values["PLATFORM_METADATA_PREFIX"], "app_platform")
        self.assertEqual(values["PLATFORM_INTERNAL_CA_FILE"], "internal-ca.pem")

        document = self.example()
        document["domain"] = "invalid\x00value"
        with self.assertRaisesRegex(ValueError, "contains NUL"):
            platform_config.nul_transport(document)
        document["domain"] = "x" * platform_config.MAXIMUM_TRANSPORT_BYTES
        with self.assertRaisesRegex(ValueError, "size limit"):
            platform_config.nul_transport(document)

    def test_internal_ca_must_be_a_plain_file_name(self) -> None:
        document = self.example()
        document["pki"] = {"internalCaFile": "../internal-ca.pem"}
        with self.assertRaisesRegex(ValueError, "plain .pem file name"):
            self.load_document(document)

    def test_nul_transport_preserves_shell_metacharacters_without_execution(self) -> None:
        document = self.example()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "platform-config-eval-regression"
            adversarial = f"example.invalid;$(touch {marker})\nquoted='value'"
            document["domain"] = adversarial
            path = Path(directory) / "platform.json"
            path.write_text(json.dumps(document))
            command = (
                f"source {ROOT / 'infra/lib/platform-config.sh'}; "
                "load_platform_config; printf '%s' \"$PLATFORM_DOMAIN\""
            )
            completed = subprocess.run(
                ["/bin/bash", "-c", command],
                env={**os.environ, "PLATFORM_CONFIG": str(path)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stdout.decode(), adversarial)
            self.assertFalse(marker.exists())

    def test_shell_transport_rejects_unknown_duplicate_and_incomplete_records(self) -> None:
        cases = {
            "unknown": b"ATTACKER_KEY\0value\0",
            "duplicate": b"PLATFORM_PROJECT\0one\0PLATFORM_PROJECT\0two\0",
            "incomplete": b"PLATFORM_PROJECT\0unterminated",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "platform-config.sh").write_text(
                (ROOT / "infra/lib/platform-config.sh").read_text()
            )
            (root / "platform_config.py").write_text("")
            fake_python = root / "python3"
            fake_python.write_text(
                "#!/usr/bin/python3\n"
                "import os, sys\n"
                "sys.stdout.buffer.write(bytes.fromhex(os.environ['TRANSPORT_HEX']))\n"
            )
            fake_python.chmod(0o700)
            for name, payload in cases.items():
                with self.subTest(name=name):
                    completed = subprocess.run(
                        [
                            "/bin/bash",
                            "-c",
                            f"source {root / 'platform-config.sh'}; load_platform_config",
                        ],
                        env={
                            **os.environ,
                            "PATH": f"{root}:{os.environ['PATH']}",
                            "TRANSPORT_HEX": payload.hex(),
                        },
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertNotEqual(completed.returncode, 0)


class PlatformConfigValidationTests(unittest.TestCase):
    def example(self) -> dict[str, object]:
        return json.loads((ROOT / "config" / "platform.example.json").read_text())

    def load_document(self, document: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.json"
            path.write_text(json.dumps(document))
            with patch.dict(os.environ, {"PLATFORM_CONFIG": str(path)}):
                return platform_config.load()

    def test_load_rejects_a_missing_nested_field_before_use(self) -> None:
        document = self.example()
        flavors = dict(document["flavors"])  # type: ignore[arg-type]
        del flavors["worker"]
        document["flavors"] = flavors

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.json"
            path.write_text(json.dumps(document))
            with patch.dict(os.environ, {"PLATFORM_CONFIG": str(path)}):
                with self.assertRaisesRegex(ValueError, "flavors.worker"):
                    platform_config.load()

    def test_standalone_loader_rejects_unsafe_direct_ingress(self) -> None:
        for ingress in (
            {"mode": "direct", "providerCidrs": []},
            {"mode": "direct", "providerCidrs": ["0.0.0.0/0"]},
            {"mode": "direct", "providerCidrs": ["not-a-cidr"]},
        ):
            document = self.example()
            document["publicIngress"] = ingress
            with self.subTest(ingress=ingress), self.assertRaises(ValueError):
                self.load_document(document)

    def test_validate_reports_every_missing_path_at_once(self) -> None:
        document = self.example()
        del document["images"]
        del document["paths"]

        with self.assertRaises(ValueError) as caught:
            platform_config.validate(document)
        message = str(caught.exception)
        self.assertIn("images.admin", message)
        self.assertIn("paths.root", message)


if __name__ == "__main__":
    unittest.main()
