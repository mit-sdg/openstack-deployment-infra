from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from openstack_platform.image_pipeline import GATES, IMAGE_INPUTS
from openstack_platform.release_manifest import ROLES, UNSIGNED_PRODUCTION_ACKNOWLEDGEMENT

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def job(name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z-]*:|\Z)", WORKFLOW.read_text(), re.M | re.S
    )
    assert match is not None, name
    return match[1]


class PublicationTriggerTests(unittest.TestCase):
    def test_every_github_action_is_pinned_to_an_immutable_sha(self) -> None:
        uses = re.findall(r"uses:\s+([^\s#]+)", WORKFLOW.read_text())
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_generated_recipes_have_an_explicit_rootless_live_smoke_job(self) -> None:
        workflow = WORKFLOW.read_text()
        smoke = (ROOT / "tests/smoke_generated_recipes.sh").read_text()
        for value in ("generated-recipes:", "tests/smoke_generated_recipes.sh", "podman"):
            self.assertIn(value, workflow)
        for value in (
            "Host.Security.Rootless",
            "generate_recipe",
            "for runtime in bun node",
            "podman image inspect",
            "NODE_ENV=production",
            "health=passed",
        ):
            self.assertIn(value, smoke)
        self.assertNotIn("--privileged", smoke)
        self.assertNotIn("sudo podman", smoke)

    def test_main_push_context_uses_one_tested_path_selection_and_exact_native_sha(self) -> None:
        context = job("publication-context")
        self.assertIn("github.ref == 'refs/heads/main'", context)
        self.assertIn("github.event_name == 'push'", context)
        self.assertIn("inputs.publish && !inputs.development_publish", context)
        self.assertIn("fetch-depth: 0", context)
        self.assertIn("image_pipeline context", context)
        self.assertIn('--before "$BEFORE_SHA" >> "$GITHUB_OUTPUT"', context)
        self.assertTrue(
            {
                "flake.nix",
                "flake.lock",
                "config/platform.example.json",
                "nix",
                "infra",
                "openstack_platform",
                "deploy",
                "pyproject.toml",
                "uv.lock",
                "LICENSE",
            }
            <= set(IMAGE_INPUTS)
        )
        self.assertIn(":(exclude)*.md", IMAGE_INPUTS)
        for name in ("production-role-builds", "publish-images"):
            self.assertIn("ref: ${{ needs.publication-context.outputs.commit_sha }}", job(name))
            self.assertNotRegex(job(name), r"GITHUB_(SHA|RUN_ID|RUN_ATTEMPT|REF):")

    def test_five_independent_github_build_runners_retain_exact_images_before_publication(
        self,
    ) -> None:
        builds = job("production-role-builds")
        self.assertIn("needs: publication-context", builds)
        self.assertIn("if: needs.publication-context.outputs.publish == 'true'", builds)
        self.assertIn("runs-on: ubuntu-latest", builds)
        self.assertIn("max-parallel: 5", builds)
        self.assertIn("fail-fast: false", builds)
        roles = re.search(r"        role:\n((?:          - \w+\n)+)", builds)
        self.assertIsNotNone(roles)
        self.assertEqual(tuple(re.findall(r"- (\w+)", roles[1])), ROLES)
        self.assertIn('build-role "${{ matrix.role }}"', builds)
        self.assertNotIn("build-all", builds)
        self.assertNotIn("OS_PASSWORD", builds)
        self.assertNotIn("RELEASE_EVIDENCE_URL", builds)
        self.assertIn("name: production-role-${{ github.run_id }}-${{ matrix.role }}", builds)
        self.assertIn("compression-level: 0", builds)
        self.assertIn("retention-days: 30", builds)
        self.assertNotIn(
            "OPENSTACK_PUBLISH_ENABLED ==", builds
        )  # Builds do not wait for signing/provider enablement.

    def test_all_gates_precede_serialized_no_rebuild_publication(self) -> None:
        publication = job("publish-images")
        for gate in (*GATES, "production-role-builds", "publication-context"):
            self.assertIn(f"      - {gate}", publication)
        self.assertIn("group: openstack-images-provider", publication)
        self.assertIn("cancel-in-progress: false", publication)
        self.assertNotIn("strategy:", publication)
        for forbidden in (
            "nix build",
            "install-nix-action",
            "build-role",
            "smoke_openstack_image.sh",
            "Check configured image",
            "existing_commit",
            "steps.image.outputs.publish",
        ):
            self.assertNotIn(forbidden, publication)
        self.assertIn("pattern: production-role-${{ github.run_id }}-*", publication)
        self.assertIn("merge-multiple: true", publication)
        self.assertIn("NEEDS_JSON: ${{ toJSON(needs) }}", publication)
        self.assertIn("needs[name]['result']", publication)
        self.assertLess(publication.index("prepare --gates"), publication.index("OS_PASSWORD:"))
        self.assertLess(
            publication.index("production-evidence-${{ github.run_id }}-${{ github.run_attempt }}"),
            publication.index("OS_PASSWORD:"),
        )
        self.assertIn("if: env.OPENSTACK_PUBLISH_ENABLED == 'true'", publication)
        self.assertIn('"$PLATFORM_CONFIG" publish', publication)
        self.assertNotIn("build-and-publish-images:", WORKFLOW.read_text())

    def test_artifact_uploads_exclude_inventory_bootstrap_and_provider_credentials(self) -> None:
        builds = job("production-role-builds")
        publication = job("publish-images")
        self.assertIn('--output "$RUNNER_TEMP/production-platform.json"', builds)
        for block in (builds, publication):
            for step in block.split("      - "):
                if "uses: actions/upload-artifact" not in step:
                    continue
                self.assertNotIn("production-platform.json", step)
                self.assertNotIn("config/", step)
                self.assertNotIn("bootstrap", step)
                self.assertNotIn(".pem", step)
                self.assertNotIn("github.workspace", step)
                self.assertNotIn("secrets.", step)
                if "if: always()" in step:
                    self.assertIn("/build.log", step)
                    self.assertIn("/qemu-serial.log", step)
                    self.assertNotIn("path: ${{ runner.temp }}/production-images\n", step)
        self.assertIn(
            'image_pipeline inventory --output "$RUNNER_TEMP/production-platform.json"', publication
        )
        self.assertNotIn("--publication", publication)

    def test_unsigned_variable_is_explicit_and_bypasses_only_external_signing_inputs(self) -> None:
        publication = job("publish-images")
        step = publication.split("      - name: Select explicit production trust policy\n", 1)[
            1
        ].split("      - name:", 1)[0]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        self.assertIn(
            "OPENSTACK_UNSIGNED_PRODUCTION: ${{ vars.OPENSTACK_UNSIGNED_PRODUCTION }}", step
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "python3"
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$RUNNER_TEMP/fetch-call"\n')
            fake.chmod(0o755)
            for mode, inputs, expected in (
                (UNSIGNED_PRODUCTION_ACKNOWLEDGEMENT, False, 0),
                ("", False, 1),
                ("false", False, 1),
                ("true", True, 2),
                ("", True, 0),
                ("false", True, 0),
            ):
                with self.subTest(mode=mode, inputs=inputs):
                    env_file, fetch = root / "environment", root / "fetch-call"
                    env_file.write_text("")
                    fetch.unlink(missing_ok=True)
                    values = {
                        **os.environ,
                        "PATH": f"{root}:{os.environ['PATH']}",
                        "RUNNER_TEMP": str(root),
                        "GITHUB_ENV": str(env_file),
                        "GITHUB_SHA": "a" * 40,
                        "OPENSTACK_UNSIGNED_PRODUCTION": mode,
                        "RELEASE_EVIDENCE_URL": "https://example.invalid/evidence.tar"
                        if inputs
                        else "",
                        "RELEASE_EVIDENCE_SHA256": "b" * 64 if inputs else "",
                        "RELEASE_TRUST_ROOT_PEM": "PUBLIC KEY ONLY" if inputs else "",
                    }
                    result = subprocess.run(
                        ["bash", "-c", script], env=values, capture_output=True, check=False
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    if mode == UNSIGNED_PRODUCTION_ACKNOWLEDGEMENT:
                        self.assertIn("PLATFORM_ALLOW_UNSIGNED_PRODUCTION=", env_file.read_text())
                        self.assertFalse(fetch.exists())
                        self.assertNotIn("TRUST_ROOT", env_file.read_text())
                    elif expected == 0:
                        self.assertIn("bundle-fetch", fetch.read_text())
                        self.assertIn("--commit " + "a" * 40, fetch.read_text())
                        self.assertNotIn("PLATFORM_ALLOW_UNSIGNED_PRODUCTION", env_file.read_text())

    def test_qemu_tool_installation_retries_bounded_apt_setup(self) -> None:
        installer = (ROOT / "tests/install_ci_apt_packages.sh").read_text()
        self.assertEqual(WORKFLOW.read_text().count("tests/install_ci_apt_packages.sh"), 4)
        for value in (
            "for attempt in 1 2",
            "timeout --foreground --kill-after=30s 5m sudo apt-get update",
            "timeout --foreground --kill-after=30s 10m sudo apt-get install",
        ):
            self.assertIn(value, installer)

    def test_development_publication_stays_manual_protected_and_separate(self) -> None:
        workflow = WORKFLOW.read_text()
        for value in (
            "development-role-evidence:",
            "development-evidence:",
            "development-publish:",
            "inputs.development_publish == true",
            "github.ref_name != 'main'",
            "Require an exact open same-repository PR head",
            "DEVELOPMENT_PLATFORM_CONFIG_JSON",
            "I_UNDERSTAND_THIS_IS_NOT_PRODUCTION",
            "environment: openstack-images",
        ):
            self.assertIn(value, workflow)
        self.assertNotIn("OPENSTACK_UNSIGNED_PRODUCTION", job("development-publish"))
        self.assertIn("max-parallel: 1", job("development-publish"))


if __name__ == "__main__":
    unittest.main()
