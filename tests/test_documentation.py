from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DOCUMENTS = (ROOT / "README.md", *sorted(DOCS.rglob("*.md")), ROOT / "frontend" / "DESIGN.md")
LINK_RE = re.compile(r"\[[^]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^#{1,6} +(.+?) *#*$", re.MULTILINE)
FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
ROUTE_RE = re.compile(r'\("(GET|POST|PUT|PATCH|DELETE)", "(/v1/[^\"]+)", self\.')

READER_DOCUMENTS = {
    "README.md",
    "how-it-works.md",
    "security.md",
    "development.md",
    "guides/plan-a-deployment.md",
    "guides/deploy-the-platform.md",
    "guides/open-the-portal.md",
    "guides/run-the-platform.md",
    "guides/manage-apps-and-people.md",
    "guides/deploy-apps-from-the-command-line.md",
    "guides/backups-and-recovery.md",
    "guides/hosts-and-images.md",
    "guides/releases-and-upgrades.md",
    "guides/troubleshooting.md",
    "guides/for-app-owners.md",
    "reference/configuration.md",
    "reference/operator-cli.md",
    "reference/controller-api.md",
    "reference/internals.md",
    "reference/repository-map.md",
}


def read(relative: str) -> str:
    return (DOCS / relative).read_text()


def anchors(document: Path) -> set[str]:
    """GitHub-style heading anchors, with -1, -2 suffixes for repeats."""
    text = FENCE_RE.sub("", document.read_text())
    seen: dict[str, int] = {}
    result = set()
    for heading in HEADING_RE.findall(text):
        plain = re.sub(r"`|\*\*|\*|\[([^]]*)\]\([^)]*\)", r"\1", heading).strip().lower()
        slug = re.sub(r"[^\w\- ]", "", plain).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else f"{slug}-{count}")
    return result


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

    def test_documentation_has_the_reader_documents(self) -> None:
        names = {path.relative_to(DOCS).as_posix() for path in DOCS.rglob("*.md")}
        self.assertEqual(names, READER_DOCUMENTS)
        self.assertFalse((ROOT / "REPOSITORY_FINDINGS.md").exists())
        self.assertFalse((ROOT / "nix" / "README.md").exists())

    def test_links_and_anchors_resolve(self) -> None:
        for document in DOCUMENTS:
            text = FENCE_RE.sub("", document.read_text())
            for target in LINK_RE.findall(text):
                if "://" in target or target.startswith("mailto:"):
                    continue
                path, _, anchor = target.partition("#")
                linked = (document.parent / path).resolve() if path else document
                with self.subTest(document=document.relative_to(ROOT), target=target):
                    self.assertTrue(linked.exists(), "linked file is missing")
                    if anchor and linked.suffix == ".md":
                        self.assertIn(anchor, anchors(linked), "linked section is missing")

    def test_images_exist_and_architecture_svg_is_current(self) -> None:
        image = re.compile(r"!\[[^]]*\]\(([^)\s]+)\)")
        for document in DOCUMENTS:
            for target in image.findall(document.read_text()):
                with self.subTest(document=document.relative_to(ROOT), image=target):
                    self.assertTrue((document.parent / target).resolve().is_file())
        svg = (DOCS / "images" / "architecture.svg").read_text()
        self.assertNotIn("rollback", svg.lower())
        self.assertFalse((DOCS / "architecture-overview.svg").exists())

    def test_repository_map_covers_every_tracked_file(self) -> None:
        guide = read("reference/repository-map.md")
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
        self.assertEqual(len(entries), len(listed), "repository map has duplicate paths")
        self.assertFalse(tracked - listed, f"unlisted tracked paths: {sorted(tracked - listed)}")
        stale = {path for path in listed - tracked if not (ROOT / path).is_file()}
        self.assertFalse(stale, f"repository map has missing paths: {sorted(stale)}")

    def test_every_controller_route_is_documented(self) -> None:
        implementation = (ROOT / "openstack_platform" / "controller" / "api.py").read_text()
        reference = read("reference/controller-api.md")
        routes = set(ROUTE_RE.findall(implementation))
        self.assertEqual(len(routes), 60)
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.assertIn(f"`{method} {path}`", reference)

    def test_trust_boundaries_are_documented(self) -> None:
        internals = " ".join(read("reference/internals.md").split())
        for phrase in (
            "platform-controller",
            "0660",
            "SO_PEERCRED",
            "Idempotency-Key",
            "--age-identity",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, internals)

    def test_setup_contract_is_documented(self) -> None:
        configuration = read("reference/configuration.md")
        example = (ROOT / "config" / "platform.example.json").read_text()
        for variable in ("PLATFORM_INGRESS_ADDRESS", "PLATFORM_DATA_GIB", "PLATFORM_BACKUP_GIB"):
            with self.subTest(variable=variable):
                self.assertIn(variable, configuration)
        self.assertIn("Cloudflare", read("guides/plan-a-deployment.md"))
        self.assertIn('"sizeGiB": 500', example)
        self.assertIn('"sizeGiB": 600', example)

    def test_release_and_recovery_procedures_remain_traceable(self) -> None:
        releases = read("guides/releases-and-upgrades.md")
        self.assertIn("setup_operator_bridge.py", releases)
        self.assertIn("operator-bridge=verified", releases)
        self.assertIn("SOURCE_COMMIT", releases)
        self.assertIn("EMIT_SCRIPT=", read("guides/backups-and-recovery.md"))
        self.assertIn("infra replace", read("guides/hosts-and-images.md"))
        self.assertIn("uv run python -m unittest discover", read("development.md"))

    def test_retired_commands_and_manifest_are_not_instructions(self) -> None:
        retired_commands = (
            "openstack-platform app ",
            "openstack-platform storage ",
            "$PLATFORM_CLI app ",
            "$PLATFORM_CLI storage ",
        )
        retired_manifest = "platform" + ".yaml"
        retired_name = "".join(("app-platform", "-infra"))
        for document in DOCUMENTS:
            text = document.read_text()
            with self.subTest(document=document.relative_to(ROOT)):
                for command in retired_commands:
                    self.assertNotIn(command, text)
                self.assertNotIn(retired_manifest, text)
                self.assertNotIn(retired_name, text)

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
