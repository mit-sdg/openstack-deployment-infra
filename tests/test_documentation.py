from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "nix/README.md",
    *sorted((ROOT / "docs").glob("*.md")),
)
CURRENT_PRODUCT_DOCUMENTS = (
    ROOT / "README.md",
    *(ROOT / "docs" / name for name in (
        "ACCEPTANCE_CHECKLIST.md",
        "ARCHITECTURE.md",
        "CONFIGURATION.md",
        "CONTROL_PLANE_CONTRACT.md",
        "GETTING_STARTED.md",
        "OPERATIONS.md",
        "PUBLIC_INGRESS.md",
        "README.md",
        "SETUP.md",
        "TROUBLESHOOTING.md",
        "TUTORIAL.md",
    )),
)
LINK_RE = re.compile(r"\[[^]]*\]\(([^)]+)\)")
ROUTE_RE = re.compile(r'\("(GET|POST|PUT|PATCH|DELETE)", "(/v1/[^\"]+)", self\.')


class DocumentationTests(unittest.TestCase):
    def test_repository_is_domain_generic_and_has_no_milestone_identifiers(self) -> None:
        forbidden_domain = bytes.fromhex("6d69742d7364672e646576").decode("ascii")
        retired_milestone = bytes.fromhex("6d31").decode("ascii")
        milestone_pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(retired_milestone)}(?=[A-Za-z_]|[^0-9])",
            re.IGNORECASE,
        )
        roots = (
            ROOT / "openstack_platform",
            ROOT / "deploy",
            ROOT / "infra",
            ROOT / "nix",
            ROOT / "docs",
            ROOT / "config",
            ROOT / "tests",
        )
        paths = [ROOT / "README.md", ROOT / "flake.nix", ROOT / "pyproject.toml"]
        paths.extend(path for directory in roots for path in directory.rglob("*") if path.is_file())
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn(forbidden_domain, text)
                self.assertIsNone(milestone_pattern.search(text))

    def test_python_package_names_component_boundaries(self) -> None:
        self.assertTrue((ROOT / "openstack_platform" / "operator.py").is_file())
        controller = ROOT / "openstack_platform" / "controller"
        self.assertTrue((controller / "api.py").is_file())
        self.assertTrue((controller / "application_models.py").is_file())
        self.assertTrue((controller / "application_runtime.py").is_file())
        self.assertTrue((controller / "nomad_jobs.py").is_file())
        self.assertTrue((controller / "database.py").is_file())
        self.assertFalse((controller / "app.py").exists())
        self.assertFalse((controller / "db.py").exists())
        self.assertTrue((ROOT / "openstack_platform" / "helper" / "main.py").is_file())
        retired_package = "platform" + "_cli"
        self.assertFalse((ROOT / retired_package).exists())

    def test_relative_links_resolve(self) -> None:
        for document in DOCUMENTS:
            for target in LINK_RE.findall(document.read_text()):
                if "://" in target or target.startswith(("#", "mailto:")):
                    continue
                path = target.split("#", 1)[0]
                if path:
                    with self.subTest(document=document.relative_to(ROOT), target=target):
                        self.assertTrue((document.parent / path).resolve().exists())

    def test_current_operator_and_pre_management_boundary_is_documented(self) -> None:
        readme = (ROOT / "README.md").read_text()
        architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
        contract = (ROOT / "docs" / "CONTROL_PLANE_CONTRACT.md").read_text()
        tutorial = (ROOT / "docs" / "TUTORIAL.md").read_text()

        normalized_readme = " ".join(readme.split())
        self.assertIn("There is not yet a supported end-user application workflow", normalized_readme)
        self.assertIn("sync-engine management application", normalized_readme)
        self.assertIn("authentication application do not exist", normalized_readme)
        self.assertIn("no product commands", architecture)
        self.assertIn("admin NixOS role runs that executable", architecture)
        self.assertIn(
            "setup`, `status`, `backup`, `restore`, and `infra`",
            " ".join(tutorial.split()),
        )
        self.assertIn("The CLI has no application, deployment, environment", contract)
        self.assertIn("implemented, packaged, and run by the admin", contract)
        self.assertIn("does not authenticate HTTP requests", contract)
        self.assertIn("socket is mode `0660`", contract)
        self.assertIn("Idempotency-Key", contract)

    def test_retired_product_cli_and_repository_manifest_are_not_current_instructions(self) -> None:
        retired_commands = (
            "openstack-platform app ",
            "openstack-platform storage ",
            "$PLATFORM_CLI app ",
            "$PLATFORM_CLI storage ",
        )
        retired_manifest = "platform" + ".yaml"
        for document in CURRENT_PRODUCT_DOCUMENTS:
            text = document.read_text()
            with self.subTest(document=document.relative_to(ROOT)):
                for command in retired_commands:
                    self.assertNotIn(command, text)
                self.assertNotIn(retired_manifest, text)

    def test_documented_controller_routes_match_implementation(self) -> None:
        implementation = (ROOT / "openstack_platform" / "controller" / "api.py").read_text()
        contract = (ROOT / "docs" / "CONTROL_PLANE_CONTRACT.md").read_text()
        routes = set(ROUTE_RE.findall(implementation))
        self.assertEqual(len(routes), 29)
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.assertIn(f"`{method} {path}`", contract)

    def test_automated_setup_contract_is_documented(self) -> None:
        readme = (ROOT / "README.md").read_text()
        setup = (ROOT / "docs" / "SETUP.md").read_text()
        contract = (ROOT / "docs" / "CONTROL_PLANE_CONTRACT.md").read_text()
        example = (ROOT / "config" / "platform.example.json").read_text()

        self.assertIn("automated setup", readme.lower())
        self.assertIn("PLATFORM_INGRESS_ADDRESS", setup)
        self.assertIn("PLATFORM_DATA_GIB", setup)
        self.assertIn("PLATFORM_BACKUP_GIB", setup)
        self.assertIn("Cloudflare account", setup)
        self.assertIn("--env-file PATH", contract)
        self.assertIn('"sizeGiB": 500', example)
        self.assertIn('"sizeGiB": 200', example)

    def test_repaired_operator_path_is_traceable(self) -> None:
        operations = (ROOT / "docs" / "OPERATIONS.md").read_text()
        publishing = (ROOT / "docs" / "IMAGE_PUBLISHING.md").read_text()
        contract = (ROOT / "docs" / "CONTROL_PLANE_CONTRACT.md").read_text()
        checklist = (ROOT / "docs" / "ACCEPTANCE_CHECKLIST.md").read_text()
        svg = (ROOT / "docs" / "architecture-overview.svg").read_text()

        self.assertIn("setup_operator_bridge.py", operations)
        self.assertIn("operator-bridge=verified", operations)
        self.assertIn("EMIT_SCRIPT=", operations)
        self.assertIn("--age-identity", contract)
        self.assertIn("SOURCE_COMMIT", publishing)
        self.assertIn("scope-record entry", checklist)
        self.assertNotIn("rollback", svg.lower())
        self.assertIn("infra replace", operations)
        self.assertIn("uv run python -m unittest discover", operations)

    def test_retired_repository_name_is_absent(self) -> None:
        retired_name = "".join(("app-platform", "-infra"))
        for document in DOCUMENTS:
            with self.subTest(document=document.relative_to(ROOT)):
                self.assertNotIn(retired_name, document.read_text())

    def test_retired_managed_storage_entrypoints_are_not_documented_or_packaged(self) -> None:
        retired_scripts = (
            "infra/services/" + "provision_project.py",
            "infra/services/" + "collect_usage.py",
        )
        for script in retired_scripts:
            self.assertFalse((ROOT / script).exists())
            for document in DOCUMENTS:
                with self.subTest(document=document.relative_to(ROOT), script=script):
                    self.assertNotIn(Path(script).name, document.read_text())
        admin_role = (ROOT / "nix/roles/admin.nix").read_text()
        self.assertNotIn("managed-usage", admin_role)
        self.assertNotIn("collect_usage", admin_role)


if __name__ == "__main__":
    unittest.main()
