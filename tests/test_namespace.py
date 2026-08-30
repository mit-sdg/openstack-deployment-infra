from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openstack_platform import contracts

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
        document = self.load_document(self.example())
        values = platform_config.shell_values(document)
        self.assertEqual(values["PLATFORM_DISPLAY_NAME"], "Example Platform")
        self.assertEqual(values["PLATFORM_ORGANIZATION"], "Example Organization")
        self.assertEqual(values["PLATFORM_NAMESPACE"], "app-platform")
        self.assertEqual(values["PLATFORM_METADATA_PREFIX"], "app_platform")
        self.assertEqual(values["PLATFORM_INTERNAL_CA_FILE"], "internal-ca.pem")

    def test_invalid_display_name_is_rejected(self) -> None:
        document = self.example()
        document["displayName"] = "Invalid/Platform"
        with self.assertRaisesRegex(ValueError, "platform displayName"):
            self.load_document(document)

    def test_invalid_organization_is_rejected(self) -> None:
        document = self.example()
        document["organization"] = "Invalid/Organization"
        with self.assertRaisesRegex(ValueError, "platform organization"):
            self.load_document(document)

    def test_invalid_namespace_is_rejected(self) -> None:
        document = self.example()
        document["namespace"] = "Invalid Namespace"
        with self.assertRaisesRegex(ValueError, "platform namespace"):
            self.load_document(document)

    def test_internal_ca_must_be_a_plain_file_name(self) -> None:
        document = self.example()
        document["pki"] = {"internalCaFile": "../internal-ca.pem"}
        with self.assertRaisesRegex(ValueError, "plain .pem file name"):
            self.load_document(document)

    def test_nul_transport_preserves_shell_metacharacters_without_execution(self) -> None:
        document = self.example()
        marker = Path(tempfile.gettempdir()) / "platform-config-eval-regression"
        marker.unlink(missing_ok=True)
        adversarial = f"example.invalid;$(touch {marker})\nquoted='value'"
        document["domain"] = adversarial
        with tempfile.TemporaryDirectory() as directory:
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

    def test_nul_transport_rejects_embedded_nul_and_is_bounded(self) -> None:
        document = self.example()
        document["domain"] = "invalid\x00value"
        with self.assertRaisesRegex(ValueError, "contains NUL"):
            platform_config.nul_transport(document)
        document["domain"] = "x" * platform_config.MAXIMUM_TRANSPORT_BYTES
        with self.assertRaisesRegex(ValueError, "size limit"):
            platform_config.nul_transport(document)

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

    def test_production_shell_has_no_eval(self) -> None:
        for path in (ROOT / "infra").rglob("*.sh"):
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotRegex(path.read_text(), r"(?m)(?:^|[;&|\s])eval\s")


class PlatformRoleNamespaceTests(unittest.TestCase):
    def test_deployment_display_name_is_not_hardcoded(self) -> None:
        deployment_name = ".".join(("6", "1040"))
        for directory in (ROOT / "infra", ROOT / "nix"):
            for path in directory.rglob("*"):
                if path.suffix not in {".nix", ".py", ".sh", ".yaml"}:
                    continue
                with self.subTest(path=path.relative_to(ROOT)):
                    self.assertNotIn(deployment_name, path.read_text())

    def test_config_drive_placeholders_have_renderer_substitutions(self) -> None:
        pairs = {
            "infra/cloud-init-nixos/admin.yaml": "openstack_platform/host_user_data.py",
            "infra/cloud-init-nixos/builder.yaml": "infra/openstack/builder_lifecycle.sh",
            "infra/cloud-init-nixos/ingress.yaml": "openstack_platform/host_user_data.py",
            "infra/cloud-init-nixos/storage.yaml": "openstack_platform/host_user_data.py",
            "infra/cloud-init-nixos/worker.yaml": "infra/openstack/worker_lifecycle.sh",
        }
        pattern = re.compile(r"__[A-Z0-9_]+__")
        for template, renderer in pairs.items():
            with self.subTest(template=template):
                required = set(pattern.findall((ROOT / template).read_text()))
                provided = set(pattern.findall((ROOT / renderer).read_text()))
                self.assertEqual(required - provided, set())


class PlatformConfigValidationTests(unittest.TestCase):
    def example(self) -> dict[str, object]:
        return json.loads((ROOT / "config" / "platform.example.json").read_text())

    def load_document(self, document: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.json"
            path.write_text(json.dumps(document))
            with patch.dict(os.environ, {"PLATFORM_CONFIG": str(path)}):
                return platform_config.load()

    def test_tracked_example_satisfies_every_dereferenced_field(self) -> None:
        platform_config.validate(self.example())

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

    def test_required_paths_are_shared_and_complete(self) -> None:
        document = self.example()
        self.assertEqual(platform_config.REQUIRED_PATHS, contracts.INVENTORY_REQUIRED_PATHS)
        for dotted in platform_config.REQUIRED_PATHS:
            platform_config.get(document, dotted)
        self.assertEqual(
            len(set(platform_config.REQUIRED_PATHS)), len(platform_config.REQUIRED_PATHS)
        )

    def test_nix_and_python_load_the_same_contract_file(self) -> None:
        constants = (ROOT / "nix/lib/constants.nix").read_text(encoding="utf-8")
        self.assertIn("../../infra/lib/platform_contract.json", constants)
        self.assertNotRegex(constants, r"\b(?:8080|4646|997|998)\b")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"infra/lib/platform_contract.json"', pyproject)
        self.assertIn('"openstack_platform/platform_contract.json"', pyproject)


if __name__ == "__main__":
    unittest.main()
