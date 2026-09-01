from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")))
CURRENT_PRODUCT_DOCUMENTS = (
    ROOT / "README.md",
    *(ROOT / "docs" / name for name in ("DEPLOYMENT.md", "INTERNALS.md", "OPERATIONS.md")),
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

    def test_tracked_file_guide_covers_every_existing_git_path(self) -> None:
        guide = (ROOT / "docs" / "REPOSITORY_GUIDE.md").read_text()
        entries = re.findall(r"^- `([^`]+)` —", guide, re.MULTILINE)
        listed = set(entries)
        git_paths = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        tracked = {path for path in git_paths if (ROOT / path).is_file()}
        self.assertEqual(len(entries), len(listed), "tracked-file guide has duplicate paths")
        self.assertFalse(tracked - listed, f"unlisted tracked paths: {sorted(tracked - listed)}")
        stale = {path for path in listed - tracked if not (ROOT / path).is_file()}
        self.assertFalse(stale, f"tracked-file guide has missing paths: {sorted(stale)}")

    def test_documentation_is_consolidated_into_six_reader_documents(self) -> None:
        names = {path.name for path in (ROOT / "docs").glob("*.md")}
        self.assertEqual(
            names,
            {
                "DEPLOYMENT.md",
                "DEVELOPMENT.md",
                "INTERNALS.md",
                "MAINTENANCE.md",
                "OPERATIONS.md",
                "REPOSITORY_GUIDE.md",
            },
        )
        self.assertFalse((ROOT / "REPOSITORY_FINDINGS.md").exists())
        self.assertFalse((ROOT / "nix" / "README.md").exists())

    def test_deployment_guide_is_human_facing_and_states_current_limits(self) -> None:
        deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
        normalized = " ".join(deployment.split())

        self.assertIn(
            "browser management UI and its authentication service do not exist", normalized
        )
        self.assertIn("What appears in OpenStack", deployment)
        self.assertIn("Why the deployment boundary is safer", deployment)
        self.assertIn("Future management application", (ROOT / "docs" / "INTERNALS.md").read_text())
        self.assertIn("TODO", deployment)
        self.assertNotIn("`GET /v1/", deployment)
        self.assertNotIn("`POST /v1/", deployment)

    def test_current_operator_and_pre_management_boundary_is_documented(self) -> None:
        readme = " ".join((ROOT / "README.md").read_text().split())
        internals = " ".join((ROOT / "docs" / "INTERNALS.md").read_text().split())

        self.assertIn("there is not yet a supported workflow for application owners", readme)
        self.assertIn("has no application, deployment, environment", internals)
        self.assertIn("runs as `platform-controller`", internals)
        self.assertIn("authenticates the local host process, not a browser user", internals)
        self.assertIn("two mode-`0660` Unix sockets", internals)
        self.assertIn("Idempotency-Key", internals)

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
        internals = (ROOT / "docs" / "INTERNALS.md").read_text()
        routes = set(ROUTE_RE.findall(implementation))
        self.assertEqual(len(routes), 30)
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.assertIn(f"`{method} {path}`", internals)

    def test_automated_setup_contract_is_documented(self) -> None:
        deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
        internals = (ROOT / "docs" / "INTERNALS.md").read_text()
        example = (ROOT / "config" / "platform.example.json").read_text()

        self.assertIn("PLATFORM_INGRESS_ADDRESS", deployment)
        self.assertIn("PLATFORM_DATA_GIB", deployment)
        self.assertIn("PLATFORM_BACKUP_GIB", deployment)
        self.assertIn("Cloudflare", deployment)
        self.assertIn("controller service, readiness unit, socket", internals)
        self.assertIn("--env-file PATH", internals)
        self.assertIn('"sizeGiB": 500', example)
        self.assertIn('"sizeGiB": 600', example)

    def test_release_operator_and_recovery_paths_remain_traceable(self) -> None:
        operations = (ROOT / "docs" / "OPERATIONS.md").read_text()
        maintenance = (ROOT / "docs" / "MAINTENANCE.md").read_text()
        internals = (ROOT / "docs" / "INTERNALS.md").read_text()
        svg = (ROOT / "docs" / "architecture-overview.svg").read_text()

        self.assertIn("setup_operator_bridge.py", maintenance)
        self.assertIn("operator-bridge=verified", maintenance)
        self.assertIn("EMIT_SCRIPT=", operations)
        self.assertIn("--age-identity", internals)
        self.assertIn("SOURCE_COMMIT", maintenance)
        self.assertIn("baseline", maintenance)
        self.assertNotIn("rollback", svg.lower())
        self.assertIn("infra replace", operations)
        development = (ROOT / "docs" / "DEVELOPMENT.md").read_text()
        self.assertIn("uv run python -m unittest discover", development)

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
