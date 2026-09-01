from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


class PublicationTriggerTests(unittest.TestCase):
    def test_every_github_action_is_pinned_to_an_immutable_sha(self) -> None:
        workflow = WORKFLOW.read_text()
        uses = re.findall(r"uses:\s+([^\s#]+)", workflow)
        self.assertTrue(uses)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_generated_recipes_have_an_explicit_rootless_live_smoke_job(self) -> None:
        workflow = WORKFLOW.read_text()
        smoke = (ROOT / "tests" / "smoke_generated_recipes.sh").read_text()

        self.assertIn("generated-recipes:", workflow)
        self.assertIn("tests/smoke_generated_recipes.sh", workflow)
        self.assertIn("podman", workflow)
        self.assertIn("Host.Security.Rootless", smoke)
        self.assertIn("generate_recipe", smoke)
        self.assertIn("for runtime in bun node", smoke)
        self.assertIn("podman image inspect", smoke)
        self.assertIn("NODE_ENV=production", smoke)
        self.assertIn("health=passed", smoke)
        self.assertNotIn("--privileged", smoke)
        self.assertNotIn("sudo podman", smoke)

    def test_every_embedded_image_source_is_a_publication_input(self) -> None:
        workflow = WORKFLOW.read_text()
        declaration = re.search(r"publish_paths=\(\s*(.*?)\s*\)", workflow, re.DOTALL)
        self.assertIsNotNone(declaration)
        inputs = set(declaration.group(1).split())
        self.assertTrue(
            {
                "flake.nix",
                "flake.lock",
                "nix",
                "infra",
                "openstack_platform",
                "deploy",
                "pyproject.toml",
                "uv.lock",
                "LICENSE",
            }
            <= inputs
        )
        self.assertIn(
            'git ls-tree -r --name-only "$COMMIT_SHA" -- "${publish_paths[@]}"',
            workflow,
        )
        self.assertIn(
            'git diff --quiet "$BEFORE_SHA" "$COMMIT_SHA" -- "${publish_paths[@]}"',
            workflow,
        )

    def test_documentation_under_an_image_input_does_not_publish(self) -> None:
        # A Markdown file can live inside an image-input directory without
        # changing an image. Without the exclusion, such an edit would rebuild
        # and republish every role image.
        workflow = WORKFLOW.read_text()
        self.assertIn("':(exclude)*.md'", workflow)

    def test_publication_inventory_uses_the_authenticated_project_uuid(self) -> None:
        workflow = WORKFLOW.read_text()

        self.assertIn("OS_PROJECT_ID: ${{ secrets.OS_PROJECT_ID }}", workflow)
        self.assertIn('project_id = str(UUID(os.environ["OS_PROJECT_ID"]))', workflow)
        self.assertIn('document["projectId"] = project_id', workflow)

    def test_qemu_tool_installation_retries_bounded_apt_setup(self) -> None:
        workflow = WORKFLOW.read_text()
        installer = (ROOT / "tests" / "install_ci_apt_packages.sh").read_text()

        self.assertEqual(workflow.count("tests/install_ci_apt_packages.sh"), 3)
        self.assertIn("for attempt in 1 2", installer)
        self.assertIn("timeout --foreground --kill-after=30s 5m sudo apt-get update", installer)
        self.assertIn("timeout --foreground --kill-after=30s 10m sudo apt-get install", installer)

    def test_development_publication_is_manual_protected_and_explicitly_unsigned(self) -> None:
        workflow = WORKFLOW.read_text()

        self.assertIn("development-role-evidence:", workflow)
        self.assertIn("development-evidence:", workflow)
        self.assertIn("development-publish:", workflow)
        self.assertIn("github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("inputs.development_publish == true", workflow)
        self.assertIn("github.ref_name != 'main'", workflow)
        self.assertIn("Require an exact open same-repository PR head", workflow)
        self.assertIn("needs: development-role-evidence", workflow)
        self.assertIn("needs: development-evidence", workflow)
        self.assertIn("Upload compact role evidence", workflow)
        self.assertIn("Upload exact role QCOW2", workflow)
        self.assertIn("compression-level: 0", workflow)
        self.assertIn("Download exact role QCOW2", workflow)
        self.assertIn("merge-multiple: true", workflow)
        self.assertIn("role:\n          - admin", workflow)
        self.assertIn("environment: openstack-images", workflow)
        self.assertIn("DEVELOPMENT_PLATFORM_CONFIG_JSON", workflow)
        self.assertIn("I_UNDERSTAND_THIS_IS_NOT_PRODUCTION", workflow)
        self.assertIn("development-role-evidence-${{ github.sha }}", workflow)


if __name__ == "__main__":
    unittest.main()
