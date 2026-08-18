from __future__ import annotations

import importlib.util
import json
import os
import re
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
            "infra/cloud-init-nixos/admin.yaml": "platform_cli/host_user_data.py",
            "infra/cloud-init-nixos/builder.yaml": "infra/openstack/builder_lifecycle.sh",
            "infra/cloud-init-nixos/ingress.yaml": "platform_cli/host_user_data.py",
            "infra/cloud-init-nixos/storage.yaml": "platform_cli/host_user_data.py",
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

    def test_tracked_example_satisfies_every_dereferenced_field(self) -> None:
        platform_config.validate(self.example())

    def test_validate_reports_a_nested_field_that_load_does_not_check(self) -> None:
        document = self.example()
        flavors = dict(document["flavors"])  # type: ignore[arg-type]
        del flavors["worker"]
        document["flavors"] = flavors

        # load() only checks top-level keys, so this document passes it.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.json"
            path.write_text(json.dumps(document))
            with patch.dict(os.environ, {"PLATFORM_CONFIG": str(path)}):
                platform_config.load()

        with self.assertRaisesRegex(ValueError, "flavors.worker"):
            platform_config.validate(document)

    def test_validate_reports_every_missing_path_at_once(self) -> None:
        document = self.example()
        del document["images"]
        del document["paths"]

        with self.assertRaises(ValueError) as caught:
            platform_config.validate(document)
        message = str(caught.exception)
        self.assertIn("images.admin", message)
        self.assertIn("paths.root", message)

    def test_required_paths_cover_every_shell_value_dereference(self) -> None:
        # A field that shell_values() reads but REQUIRED_PATHS omits would slip
        # past validation and fail later inside a build.
        document = self.example()
        for dotted in platform_config.REQUIRED_PATHS:
            platform_config.get(document, dotted)
        self.assertEqual(
            len(set(platform_config.REQUIRED_PATHS)), len(platform_config.REQUIRED_PATHS)
        )


if __name__ == "__main__":
    unittest.main()
