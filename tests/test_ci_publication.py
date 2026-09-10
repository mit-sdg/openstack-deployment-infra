from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
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
            "for recipe_case in bun node bun-slim node-slim",
            "runtime_files=",
            "test ! -e /app/build-only.txt",
            "test ! -e /app/node_modules",
            "cat /app/nested/assets/message.txt",
            "cat /app/nested/config/message.txt",
            "timeout 240 podman build",
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
        self.assertEqual(WORKFLOW.read_text().count("tests/install_ci_apt_packages.sh"), 6)
        self.assertNotIn("sudo apt-get", WORKFLOW.read_text())
        for value in (
            "ubuntu_sources=/etc/apt/sources.list.d/ubuntu.sources",
            "for attempt in 1 2 3",
            'timeout --foreground --kill-after=30s 5m sudo apt-get "${apt_options[@]}" update',
            'timeout --foreground --kill-after=30s 10m sudo apt-get "${apt_options[@]}" install',
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


class CIAptIsolationTests(unittest.TestCase):
    """Exercise the real shell with process doubles; never run host APT/sudo."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources = self.root / "ubuntu.sources"
        self.sources.write_text(
            "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: noble\n"
            "Components: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
        )
        self.script = self.root / "install.sh"
        source = (ROOT / "tests/install_ci_apt_packages.sh").read_text()
        # Remap only the fixed file path into the fixture; production has no
        # caller-controlled source override or host configuration mutation.
        self.script.write_text(
            source.replace(
                "ubuntu_sources=/etc/apt/sources.list.d/ubuntu.sources",
                "ubuntu_sources=" + shlex.quote(str(self.sources)),
            )
        )
        self.log = self.root / "calls.jsonl"
        runner = f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json, os, sys
            from pathlib import Path
            name = Path(sys.argv[0]).name
            args = sys.argv[1:]
            log = Path(os.environ["FAKE_APT_LOG"])
            previous = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            with log.open("a") as stream:
                stream.write(json.dumps([name, *args]) + "\\n")
            if name == "timeout":
                os.execvp(args[3], args[3:])
            if name == "sudo":
                os.execvp(args[0], args)
            if name == "apt-get":
                action = "update" if "update" in args else "install"
                count = sum(row[0] == "apt-get" and action in row for row in previous)
                if action == os.environ.get("FAIL_ACTION") and count < int(os.environ.get("FAIL_COUNT", "0")):
                    sys.exit(int(os.environ.get("FAIL_CODE", "100")))
            """)
        for name in ("timeout", "sudo", "apt-get", "dpkg", "rm", "sleep"):
            executable = self.root / name
            executable.write_text(runner)
            executable.chmod(0o755)

    def run_installer(self, *packages, **environment):
        return subprocess.run(
            ["/bin/bash", str(self.script), *packages],
            env={
                "PATH": str(self.root),
                "GITHUB_ACTIONS": "true",
                "FAKE_APT_LOG": str(self.log),
                **environment,
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def calls(self, name):
        rows = (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )
        return [row[1:] for row in rows if row[0] == name]

    def test_both_commands_use_only_ubuntu_sources_without_relaxing_integrity(self):
        result = self.run_installer("shellcheck", "podman")
        self.assertEqual(result.returncode, 0, result.stderr)
        options = [
            "-o",
            f"Dir::Etc::sourcelist={self.sources}",
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "Dir::Cache::pkgcache=",
            "-o",
            "Dir::Cache::srcpkgcache=",
            "-o",
            "APT::Get::List-Cleanup=0",
            "-o",
            "APT::Update::Error-Mode=any",
        ]
        self.assertEqual(
            self.calls("apt-get"),
            [options + ["update"], options + ["install", "--yes", "shellcheck", "podman"]],
        )
        self.assertEqual(
            [call[:3] for call in self.calls("timeout")],
            [
                ["--foreground", "--kill-after=30s", "5m"],
                ["--foreground", "--kill-after=30s", "10m"],
            ],
        )
        self.assertEqual(self.calls("rm"), [])
        self.assertTrue(self.sources.exists())

    def test_update_failure_retries_boundedly_and_never_installs_from_stale_lists(self):
        result = self.run_installer(
            "shellcheck", FAIL_ACTION="update", FAIL_COUNT="3", FAIL_CODE="124"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed after 3 attempts", result.stderr)
        self.assertEqual(len(self.calls("apt-get")), 3)
        self.assertTrue(all(call[-1] == "update" for call in self.calls("apt-get")))
        self.assertEqual(len(self.calls("dpkg")), 2)
        self.assertEqual(self.calls("sleep"), [["15"], ["15"]])

    def test_install_errors_are_not_suppressed_and_transient_update_can_recover(self):
        result = self.run_installer("podman", FAIL_ACTION="install", FAIL_COUNT="3")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sum("install" in call for call in self.calls("apt-get")), 3)
        self.log.unlink()
        result = self.run_installer("podman", FAIL_ACTION="update", FAIL_COUNT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum("update" in call for call in self.calls("apt-get")), 2)
        self.assertEqual(sum("install" in call for call in self.calls("apt-get")), 1)

    def test_missing_or_symlink_source_non_ci_and_option_injection_fail_before_sudo(self):
        for packages, environment in (
            (("shellcheck",), {"GITHUB_ACTIONS": "false"}),
            (("--allow-unauthenticated",), {}),
        ):
            with self.subTest(packages=packages, environment=environment):
                self.assertEqual(self.run_installer(*packages, **environment).returncode, 2)
                self.assertEqual(self.calls("sudo"), [])
        self.sources.unlink()
        self.assertEqual(self.run_installer("shellcheck").returncode, 2)
        other = self.root / "other.sources"
        other.write_text("unrelated source")
        self.sources.symlink_to(other)
        self.assertEqual(self.run_installer("shellcheck").returncode, 2)
        self.assertEqual(self.calls("sudo"), [])


if __name__ == "__main__":
    unittest.main()
