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
LINK_RE = re.compile(r"\[[^]]*\]\(([^)]+)\)")


class DocumentationTests(unittest.TestCase):
    def test_relative_links_resolve(self) -> None:
        for document in DOCUMENTS:
            for target in LINK_RE.findall(document.read_text()):
                if "://" in target or target.startswith(("#", "mailto:")):
                    continue
                path = target.split("#", 1)[0]
                if path:
                    with self.subTest(document=document.relative_to(ROOT), target=target):
                        self.assertTrue((document.parent / path).resolve().exists())

    def test_current_rollback_and_private_diagnostic_claims_match_m1(self) -> None:
        readme = (ROOT / "README.md").read_text()
        architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
        contract = (ROOT / "docs" / "CONTROL_PLANE_CONTRACT.md").read_text()
        admin_role = (ROOT / "nix" / "roles" / "admin.nix").read_text()

        self.assertNotIn("rollback to earlier versions", readme)
        self.assertIn("failed deployment candidates", readme)
        self.assertIn("no general operator command", architecture)
        self.assertIn("A failed candidate is removed", contract)
        self.assertIn("`Dockerfile*` files may coexist as inert source files", contract)
        self.assertIn("`NODE_ENV=production`", contract)
        self.assertIn("Staff `set`, `import`, and `unset` refuse", contract)
        self.assertIn("ModifyIndex compare-and-set", contract)
        self.assertIn("or provisioning requests", contract)
        self.assertIn("Bindings neither create nor remove storage", contract)
        self.assertIn("helper-diagnostics/<correlation-id>.trace", contract)
        self.assertIn("source file/line locations", contract)
        self.assertIn("controller/helper-diagnostics 0700 agentops agentops", admin_role)

    def test_automated_setup_contract_is_documented(self) -> None:
        readme = (ROOT / "README.md").read_text()
        setup = (ROOT / "docs" / "SETUP.md").read_text()
        contract = (ROOT / "docs" / "CONTROL_PLANE_CONTRACT.md").read_text()
        example = (ROOT / "config" / "platform.example.json").read_text()

        self.assertIn("Automated setup", readme)
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

        self.assertIn("setup_management_bridge.py", operations)
        self.assertIn("management-bridge=verified", operations)
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
