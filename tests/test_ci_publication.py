from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


class PublicationTriggerTests(unittest.TestCase):
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

    def test_embedded_infra_is_a_publication_input(self) -> None:
        workflow = WORKFLOW.read_text()

        self.assertIn(
            "publish_paths=(flake.nix flake.lock nix infra ':(exclude)*.md')",
            workflow,
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
        # nix/README.md lives inside an image-input directory but changes no
        # image. Without the exclusion a README edit rebuilds and republishes
        # every role image.
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

        self.assertEqual(workflow.count("tests/install_ci_apt_packages.sh"), 2)
        self.assertIn("for attempt in 1 2", installer)
        self.assertIn("timeout --foreground --kill-after=30s 5m sudo apt-get update", installer)
        self.assertIn("timeout --foreground --kill-after=30s 10m sudo apt-get install", installer)


if __name__ == "__main__":
    unittest.main()
