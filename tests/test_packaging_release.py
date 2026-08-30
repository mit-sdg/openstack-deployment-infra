from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from openstack_platform import release_manifest
from openstack_platform.helper import production

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/releases/install_release.py"
SMOKE = ROOT / "deploy/releases/release_smoke.py"
CONFIG_INSTALLER = ROOT / "deploy/releases/install_operator_config.py"
HELPER_DEPLOY = ROOT / "deploy/releases/deploy_helper_release.sh"
UNITS = ROOT / "deploy/releases/systemd"
UV = shutil.which("uv")


def _platform_identity_sha256(document: dict[str, object]) -> str:
    identity = {
        "namespace": document["namespace"],
        "project": document["project"],
        "projectId": document["projectId"],
        "paths": document["paths"],
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_INSTALLER_SPEC = importlib.util.spec_from_file_location("release_installer_test", INSTALLER)
assert _INSTALLER_SPEC is not None and _INSTALLER_SPEC.loader is not None
INSTALLER_MODULE = importlib.util.module_from_spec(_INSTALLER_SPEC)
sys.modules[_INSTALLER_SPEC.name] = INSTALLER_MODULE
_INSTALLER_SPEC.loader.exec_module(INSTALLER_MODULE)


class ReleaseArchiveVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        (self.repository / "payload.txt").write_text("release payload\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", self.repository], check=True)
        subprocess.run(["git", "-C", self.repository, "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", self.repository, "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", self.repository, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", self.repository, "commit", "-qm", "archive fixture"], check=True
        )
        self.commit = subprocess.run(
            ["git", "-C", self.repository, "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.archive = self.root / "release.tar"
        subprocess.run(
            [
                "git",
                "-C",
                self.repository,
                "archive",
                "--format=tar",
                f"--output={self.archive}",
                self.commit,
            ],
            check=True,
        )
        self.archive_sha256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _verify(self, archive: Path, commit: str, checksum: str) -> None:
        INSTALLER_MODULE._verify_archive_identity(
            archive, expected_commit=commit, expected_sha256=checksum
        )

    def test_real_git_archive_verifies_without_any_path_executables(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": ""}):
            self._verify(self.archive, self.commit, self.archive_sha256)

    def test_payload_tampering_is_rejected_by_trusted_archive_checksum(self) -> None:
        with tarfile.open(self.archive, mode="r:") as bundle:
            member = next(item for item in bundle.getmembers() if item.isfile() and item.size)
        with self.archive.open("r+b") as stream:
            stream.seek(member.offset_data)
            original = stream.read(1)
            stream.seek(member.offset_data)
            stream.write(bytes((original[0] ^ 1,)))

        with self.assertRaisesRegex(INSTALLER_MODULE.InstallFailure, "SHA-256 does not match"):
            self._verify(self.archive, self.commit, self.archive_sha256)

    def test_plain_tar_with_matching_checksum_is_not_commit_addressed(self) -> None:
        plain = self.root / "plain.tar"
        with tarfile.open(plain, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
            bundle.add(self.repository / "payload.txt", arcname="payload.txt")
        checksum = hashlib.sha256(plain.read_bytes()).hexdigest()

        with self.assertRaisesRegex(
            INSTALLER_MODULE.InstallFailure, "not a commit-addressed git archive"
        ):
            self._verify(plain, self.commit, checksum)

    def test_fabricated_non_git_pax_comment_is_rejected(self) -> None:
        fabricated = self.root / "fabricated.tar"
        with tarfile.open(
            fabricated,
            mode="w",
            format=tarfile.PAX_FORMAT,
            pax_headers={"comment": self.commit},
        ) as bundle:
            bundle.add(self.repository / "payload.txt", arcname="payload.txt")
        checksum = hashlib.sha256(fabricated.read_bytes()).hexdigest()

        with self.assertRaisesRegex(
            INSTALLER_MODULE.InstallFailure, "not a commit-addressed git archive"
        ):
            self._verify(fabricated, self.commit, checksum)

    def test_noncanonical_git_pax_commit_is_rejected(self) -> None:
        content = self.archive.read_bytes().replace(
            self.commit.encode("ascii"), self.commit.upper().encode("ascii"), 1
        )
        self.archive.write_bytes(content)
        checksum = hashlib.sha256(content).hexdigest()

        with self.assertRaisesRegex(
            INSTALLER_MODULE.InstallFailure, "no canonical full source commit"
        ):
            self._verify(self.archive, self.commit, checksum)

    def test_real_git_archive_commit_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(INSTALLER_MODULE.InstallFailure, "commit does not match"):
            self._verify(self.archive, "0" * 40, self.archive_sha256)


class HelperRuntimePathTests(unittest.TestCase):
    def test_namespace_paths_and_diagnostics_come_from_live_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            platform_path = Path(temporary) / "platform.json"
            document = json.loads((ROOT / "config/platform.example.json").read_text())
            document["namespace"] = "test-platform"
            document["paths"] = {
                "root": "/srv/test-platform",
                "adminState": "/srv/test-platform-state",
                "backups": "/srv/test-platform-backups",
                "data": "/srv/test-platform-data",
            }
            platform_path.write_text(json.dumps(document), encoding="utf-8")
            with mock.patch.dict(os.environ, {"PLATFORM_CONFIG": str(platform_path)}):
                runtime = production.helper_runtime()

        self.assertEqual(runtime.platform.namespace, "test-platform")
        self.assertEqual(runtime.root, Path("/srv/test-platform"))
        self.assertEqual(runtime.admin_state, Path("/srv/test-platform-state"))
        self.assertEqual(runtime.backups, Path("/srv/test-platform-backups"))
        self.assertEqual(runtime.data, Path("/srv/test-platform-data"))
        self.assertEqual(
            runtime.diagnostic_directory,
            Path("/srv/test-platform-state/controller/helper-diagnostics"),
        )
        for relative in (
            "openstack_platform/helper/production.py",
            "openstack_platform/helper/main.py",
            "openstack_platform/helper/application_actions.py",
            "deploy/releases/install_release.py",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("/srv/app-platform", source)
            self.assertNotIn("/etc/app-platform", source)

    def test_active_build_log_is_private_tail_readable_and_offset_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = mock.Mock(admin_state=Path(temporary))
            log_path, state_path = production._build_log_paths(
                runtime, "demo-app", "00000000-0000-4000-8000-000000000099"
            )
            log_path.write_text("first\nsecond\nthird\n", encoding="utf-8")
            log_path.chmod(0o600)
            production._write_build_log_state(state_path, "running")
            with mock.patch.object(production, "helper_runtime", return_value=runtime):
                tail = production._read_build_log(
                    {
                        "buildId": "00000000-0000-4000-8000-000000000099",
                        "slug": "demo-app",
                        "lines": 2,
                        "offset": None,
                    }
                )
                following = production._read_build_log(
                    {
                        "buildId": "00000000-0000-4000-8000-000000000099",
                        "slug": "demo-app",
                        "lines": 2,
                        "offset": 6,
                    }
                )
        self.assertEqual(tail["text"], "second\nthird\n")
        self.assertEqual(tail["state"], "running")
        self.assertEqual(following["text"], "second\nthird\n")

    @unittest.skipUnless(sys.version_info[:2] == (3, 14), "release smoke tests require Python 3.14")
    def test_helper_release_smoke_uses_only_sanitized_inventory(self) -> None:
        environment = os.environ.copy()
        environment["PLATFORM_CONFIG"] = "/private/inventory/must-not-be-read.json"
        result = subprocess.run(
            [sys.executable, SMOKE, "helper", "--source", ROOT],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "release-smoke=helper:ok\n")


class HelperPlatformConfigurationVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = self.root / "nix/store"
        self.store.mkdir(parents=True)
        self.stable = self.root / "etc/test-platform/platform.json"
        self.stable.parent.mkdir(parents=True)
        self.document = json.loads((ROOT / "config/platform.example.json").read_text())
        self.document["namespace"] = "test-platform"
        self.document["paths"] = {
            "root": "/srv/test-platform",
            "adminState": "/srv/test-platform-state",
            "backups": "/srv/test-platform-backups",
            "data": "/srv/test-platform-data",
        }
        self.identity_sha256 = _platform_identity_sha256(self.document)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store_target(
        self,
        name: str = "0123456789abcdfghijklmnpqrsvwxyz-openstack-platform.json",
    ) -> Path:
        target = self.store / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.document), encoding="utf-8")
        target.chmod(0o444)
        return target

    def _verify(self) -> None:
        INSTALLER_MODULE._verify_helper_platform_configuration(
            self.stable,
            expected_namespace="test-platform",
            expected_identity_sha256=self.identity_sha256,
            nix_store=self.store,
            helper_config_root=self.root / "etc",
        )

    def test_accepts_stable_symlink_to_immutable_root_owned_nix_store_file(self) -> None:
        target = self._store_target()
        self.stable.symlink_to(target)

        # The fixture owner stands in for uid 0; all path, symlink, mode,
        # readability, file-type, and JSON checks still use the real filesystem.
        with mock.patch.object(INSTALLER_MODULE, "_ROOT_UID", os.geteuid()):
            self._verify()

    def test_accepts_nixos_style_symlink_chain_only_when_realpath_ends_in_store(self) -> None:
        self._store_target("1bcdefghijklmnpqrsvwxyz012345678-etc/etc/test-platform/platform.json")
        static = self.root / "etc/static"
        static.symlink_to(self.store / "1bcdefghijklmnpqrsvwxyz012345678-etc/etc")
        self.stable.symlink_to(static / "test-platform/platform.json")

        with mock.patch.object(INSTALLER_MODULE, "_ROOT_UID", os.geteuid()):
            self._verify()

    def test_rejects_store_symlink_at_an_arbitrary_mutable_location(self) -> None:
        target = self._store_target()
        arbitrary = self.root / "home/agentops/platform.json"
        arbitrary.parent.mkdir(parents=True)
        arbitrary.symlink_to(target)

        with (
            mock.patch.object(INSTALLER_MODULE, "_ROOT_UID", os.geteuid()),
            self.assertRaisesRegex(INSTALLER_MODULE.InstallFailure, "allowed only at /etc"),
        ):
            INSTALLER_MODULE._verify_helper_platform_configuration(
                arbitrary,
                expected_namespace="test-platform",
                expected_identity_sha256=self.identity_sha256,
                nix_store=self.store,
                helper_config_root=self.root / "etc",
            )

    def test_rejects_mutable_and_symlink_chain_targets_outside_nix_store(self) -> None:
        mutable = self.root / "mutable/platform.json"
        mutable.parent.mkdir()
        mutable.write_text(json.dumps(self.document), encoding="utf-8")
        intermediary = self.root / "etc/static-platform.json"
        intermediary.symlink_to(mutable)
        self.stable.symlink_to(intermediary)

        with mock.patch.object(INSTALLER_MODULE, "_ROOT_UID", os.geteuid()):
            with self.assertRaisesRegex(
                INSTALLER_MODULE.InstallFailure, "target must be under /nix/store"
            ):
                self._verify()

    def test_rejects_non_root_owned_or_writable_nix_store_target(self) -> None:
        target = self._store_target()
        self.stable.symlink_to(target)

        with mock.patch.object(INSTALLER_MODULE, "_ROOT_UID", os.geteuid() + 1):
            with self.assertRaisesRegex(INSTALLER_MODULE.InstallFailure, "owned by root"):
                self._verify()

        target.chmod(0o664)
        with mock.patch.object(INSTALLER_MODULE, "_ROOT_UID", os.geteuid()):
            with self.assertRaisesRegex(INSTALLER_MODULE.InstallFailure, "group or world writable"):
                self._verify()

    def test_rejects_nix_store_target_not_readable_by_agentops(self) -> None:
        target = self._store_target()
        self.stable.symlink_to(target)

        with (
            mock.patch.object(INSTALLER_MODULE, "_ROOT_UID", os.geteuid()),
            mock.patch.object(INSTALLER_MODULE.os, "access", return_value=False),
            self.assertRaisesRegex(
                INSTALLER_MODULE.InstallFailure, "readable by the operator account"
            ),
        ):
            self._verify()

    def test_rejects_store_document_with_mismatched_project_namespace_or_paths(self) -> None:
        target = self._store_target()
        changed = dict(self.document)
        changed["paths"] = dict(self.document["paths"], backups="/srv/attacker-backups")
        target.chmod(0o644)
        target.write_text(json.dumps(changed), encoding="utf-8")
        target.chmod(0o444)
        self.stable.symlink_to(target)

        with mock.patch.object(INSTALLER_MODULE, "_ROOT_UID", os.geteuid()):
            with self.assertRaisesRegex(
                INSTALLER_MODULE.InstallFailure,
                "does not match the expected project, namespace, and paths",
            ):
                self._verify()


class ReleaseInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        if sys.version_info[:2] != (3, 14):
            self.skipTest("release smoke tests require Python 3.14")
        if UV is None:
            self.skipTest("release smoke tests require uv")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.openstack_command = self.root / "protected-openstack"
        self.openstack_command.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        self.openstack_command.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, repository: Path, name: str, content: str) -> None:
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def _repository(self, *, partial_helper: bool = False) -> tuple[Path, str]:
        repository = self.root / f"repository-{len(list(self.root.glob('repository-*')))}"
        repository.mkdir()
        self._write(
            repository,
            "pyproject.toml",
            """
            [project]
            name = "release-fixture"
            version = "1.0.0"
            requires-python = ">=3.14,<3.15"
            dependencies = []
            """,
        )
        self._write(repository, "openstack_platform/__init__.py", "")
        shutil.copy2(
            ROOT / "openstack_platform/release_manifest.py",
            repository / "openstack_platform/release_manifest.py",
        )
        self._write(
            repository,
            "openstack_platform/controller/database.py",
            "MIGRATIONS = (Migration(1, ()),)\n",
        )
        self._write(repository, "openstack_platform/controller/api.py", "API_VERSION = 1\n")
        self._write(
            repository,
            "openstack_platform/restore.py",
            """
            import argparse
            import shutil

            def main():
                parser = argparse.ArgumentParser()
                parser.add_argument("backup")
                parser.add_argument("--destination", required=True)
                parser.add_argument("--platform-config", required=True)
                parser.add_argument("--yes", action="store_true")
                args = parser.parse_args()
                assert args.yes
                shutil.copyfile(args.backup, args.destination)
                print("restore=verified schema-version=2 integrity=ok")
                return 0

            if __name__ == "__main__":
                raise SystemExit(main())
            """,
        )
        self._write(
            repository,
            "openstack_platform/operator.py",
            """
            import argparse
            import json
            import stat

            def main():
                parser = argparse.ArgumentParser()
                parser.add_argument("--platform-config", required=True)
                parser.add_argument("--state-directory", required=True)
                parser.add_argument("--policy", required=True)
                commands = parser.add_subparsers(dest="command", required=True)
                commands.add_parser("status")
                args = parser.parse_args()
                with open(args.platform_config, encoding="utf-8") as stream:
                    platform = json.load(stream)
                with open(args.policy, encoding="utf-8") as stream:
                    policy = json.load(stream)
                assert platform["projectId"] and policy["backupAgeRecipient"]
                assert stat.S_IMODE(__import__("os").stat(args.policy).st_mode) == 0o600
                print("operator-fixture=ok")
                return 0

            if __name__ == "__main__":
                raise SystemExit(main())
            """,
        )
        self._write(repository, "openstack_platform/helper/__init__.py", "")
        actions = (
            ["backup.accept"]
            if partial_helper
            else [
                "app.observe",
                "backup.accept",
                "storage.observe",
            ]
        )
        rendered_actions = ", ".join(f'"{action}": lambda _args: {{}}' for action in actions)
        self._write(
            repository,
            "openstack_platform/helper/main.py",
            f"""
            import json
            import sys

            def serve_once(_input, output, handlers):
                if output is not None:
                    json.dump({{
                        "version": 1,
                        "requestId": "00000000-0000-0000-0000-000000000000",
                        "ok": False,
                        "error": {{
                            "code": "INVALID_REQUEST",
                            "message": "helper request is invalid",
                        }},
                    }}, output)
                return 0

            def main():
                return serve_once(sys.stdin, sys.stdout, {{{rendered_actions}}})

            if __name__ == "__main__":
                raise SystemExit(main())
            """,
        )
        self._write(
            repository,
            "openstack_platform/helper/actions-v1.txt",
            "\n".join(actions) + "\n",
        )
        smoke_target = repository / "deploy/releases/release_smoke.py"
        smoke_target.parent.mkdir(parents=True)
        shutil.copy2(SMOKE, smoke_target)
        installer_target = repository / "deploy/releases/install_release.py"
        shutil.copy2(INSTALLER, installer_target)
        fixture_store = self.root / "fixture-nix/store"
        installer_target.write_text(
            installer_target.read_text(encoding="utf-8")
            .replace("/nix/store", str(fixture_store))
            .replace("_ROOT_UID = 0", f"_ROOT_UID = {os.geteuid()}")
            .replace(
                '_HELPER_CONFIG_ROOT = Path("/etc")',
                f"_HELPER_CONFIG_ROOT = Path({str(self.root / 'empty-admin/etc')!r})",
            )
            .replace(
                'test "$(stat -c %u -- "$resolved_platform_config")" = 0',
                f'test "$(stat -c %u -- "$resolved_platform_config")" = {os.geteuid()}',
            ),
            encoding="utf-8",
        )
        shutil.copy2(CONFIG_INSTALLER, repository / "deploy/releases/install_operator_config.py")
        contract_target = repository / "infra/lib/platform_contract.json"
        contract_target.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "infra/lib/platform_contract.json", contract_target)
        helper_deploy = repository / "deploy/releases/deploy_helper_release.sh"
        shutil.copy2(HELPER_DEPLOY, helper_deploy)
        helper_deploy.write_text(
            helper_deploy.read_text(encoding="utf-8")
            .replace(
                "ssh_config=$operator_root/.secrets/ssh/config",
                f"ssh_config={self.root / 'fixture-ssh-config'}",
            )
            .replace("/run/current-system/sw/bin/python3.14", sys.executable)
            .replace("/nix/store", str(fixture_store))
            .replace("target_metadata.st_uid != 0", "target_metadata.st_uid != os.geteuid()")
            .replace("or metadata.st_uid != 0", "or metadata.st_uid != os.geteuid()")
            .replace(
                "readonly helper_config_root=/etc",
                "helper_config_root=${PLATFORM_HELPER_CONFIG_ROOT:-/etc}",
            ),
            encoding="utf-8",
        )
        helper_deploy.chmod(0o755)
        shutil.copytree(UNITS, repository / "deploy/releases/systemd")
        self._write(repository, "flake.nix", "{ outputs = _: {}; }\n")
        self._write(repository, "flake.lock", "{}\n")
        for role in release_manifest.ROLES:
            self._write(repository, f"nix/roles/{role}.nix", "{ ... }: {}\n")

        config = repository / "config"
        config.mkdir()
        shutil.copy2(ROOT / "config/platform.example.json", config / "platform.example.json")
        shutil.copy2(
            ROOT / "config/platform-policy.example.json",
            config / "platform-policy.example.json",
        )

        subprocess.run(["git", "init", "-q", repository], check=True)
        subprocess.run(["git", "-C", repository, "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", repository, "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            [UV, "lock", "--directory", repository, "--python", sys.executable], check=True
        )
        subprocess.run(["git", "-C", repository, "add", "."], check=True)
        subprocess.run(["git", "-C", repository, "commit", "-qm", "fixture"], check=True)
        commit = subprocess.run(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        return repository, commit

    def _evidence(self, repository: Path, commit: str) -> Path:
        output = self.root / f"release-evidence-{commit}"
        if not output.exists():
            release_manifest.generate(repository, commit, output, signing_key=None, unsigned=True)
        return output

    def _install(
        self,
        repository: Path,
        commit: str,
        mode: str,
        *,
        check: bool = True,
        install_units: bool = False,
        prepare_config: bool = True,
        evidence_commit: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        installation_root = self.root / f"{mode}-install"
        release_root = installation_root / (
            "helper-releases" if mode == "helper" else "operator-releases"
        )
        bin_root = installation_root / "bin"
        state_root = installation_root / "state"
        unit_root = installation_root / "units"
        config_root = installation_root / "config"
        helper_platform = installation_root / "etc/test-platform/platform.json"
        if mode == "operator" and prepare_config and not (config_root / "platform.json").exists():
            config_root.mkdir(parents=True, mode=0o700)
            state_root.mkdir(parents=True, mode=0o700)
            shutil.copy2(ROOT / "config/platform.example.json", config_root / "platform.json")
            shutil.copy2(ROOT / "config/platform-policy.example.json", state_root / "policy.json")
            (config_root / "platform.json").chmod(0o600)
            (state_root / "policy.json").chmod(0o600)
        if mode == "helper":
            helper_platform.parent.mkdir(parents=True, exist_ok=True)
            document = json.loads((ROOT / "config/platform.example.json").read_text())
            document["namespace"] = "test-platform"
            document["paths"] = {
                "root": "/srv/test-platform",
                "adminState": "/srv/test-platform-state",
                "backups": "/srv/test-platform-backups",
                "data": "/srv/test-platform-data",
            }
            helper_platform.write_text(json.dumps(document), encoding="utf-8")
        argv = [
            sys.executable,
            INSTALLER,
            "--mode",
            mode,
            "--source",
            repository,
            "--commit",
            commit,
            "--release-manifest",
            self._evidence(repository, evidence_commit or commit) / "release-manifest.json",
            "--allow-unsigned-development",
            "--python",
            sys.executable,
            "--uv",
            UV,
            "--release-root",
            release_root,
            "--bin-root",
            bin_root,
            "--state-root",
            state_root,
            "--config-root",
            config_root,
            "--platform-config",
            helper_platform,
            "--openstack-command",
            self.openstack_command,
        ]
        if mode == "helper":
            argv.extend(
                (
                    "--expected-platform-namespace",
                    str(document["namespace"]),
                    "--expected-platform-identity-sha256",
                    _platform_identity_sha256(document),
                )
            )
        if install_units:
            argv.extend(("--install-user-units", "--user-unit-dir", unit_root))
        return subprocess.run(
            argv,
            check=check,
            capture_output=True,
            text=True,
        )

    def test_operator_install_is_commit_addressed_and_idempotent(self) -> None:
        repository, commit = self._repository()
        first = self._install(repository, commit, "operator", install_units=True)
        second = self._install(repository, commit, "operator", install_units=True)

        release_root = self.root / "operator-install/operator-releases"
        release = release_root / "releases" / commit
        launcher = self.root / "operator-install/bin/openstack-platform"
        self.assertIn(f"commit={commit}", first.stdout)
        self.assertIn(f"commit={commit}", second.stdout)
        self.assertEqual((release / ".complete").read_text(), f"{commit}\n")
        self.assertEqual((release_root / "current").resolve(), release)
        retained = release / "evidence"
        self.assertTrue((retained / "release-manifest.json").is_file())
        self.assertTrue((retained / "release.sbom.json").is_file())
        self.assertTrue((retained / "release.provenance.json").is_file())
        self.assertEqual(launcher.resolve(), release / "bin/openstack-platform")
        restore_launcher = self.root / "operator-install/bin/openstack-platform-restore"
        self.assertEqual(restore_launcher.resolve(), release / "bin/openstack-platform-restore")
        restore_text = restore_launcher.resolve().read_text()
        self.assertIn("openstack_platform.restore", restore_text)
        self.assertIn(str(self.root / "operator-install/state/platform.sqlite3"), restore_text)
        self.assertIn("--replacement-state-directory", restore_text)
        self.assertIn("operator replacement destination must be absent", restore_text)
        replacement_state = self.root / "replacement-operator-state"
        replacement_state.mkdir(mode=0o700)
        replacement_backup = self.root / "replacement-backup.sqlite3"
        replacement_backup.write_bytes(b"replacement state")
        replacement_backup.chmod(0o600)
        restored = subprocess.run(
            (
                restore_launcher,
                "--replacement-state-directory",
                replacement_state,
                replacement_backup,
                "--yes",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("restore=verified", restored.stdout)
        self.assertEqual(
            (replacement_state / "platform.sqlite3").read_bytes(), b"replacement state"
        )
        self.assertFalse((self.root / "operator-install/state/platform.sqlite3").exists())
        repeated = subprocess.run(
            (
                restore_launcher,
                "--replacement-state-directory",
                replacement_state,
                replacement_backup,
                "--yes",
            ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(repeated.returncode, 78)
        self.assertIn("must be absent", repeated.stderr)
        launcher_text = launcher.resolve().read_text()
        self.assertIn(str(self.root / "operator-install/config/platform.json"), launcher_text)
        self.assertIn(f"openstack_command={self.openstack_command}", launcher_text)
        self.assertIn('export PLATFORM_OPENSTACK_COMMAND="$openstack_command"', launcher_text)
        self.assertNotIn("$release/source/config/platform.json", launcher_text)
        self.assertFalse((release / "source/config/platform.json").exists())
        config_installer = self.root / "operator-install/bin/openstack-platform-install-config"
        self.assertEqual(
            config_installer.resolve(), release / "bin/openstack-platform-install-config"
        )
        launched = subprocess.run(
            [launcher, "status"], check=True, stdout=subprocess.PIPE, text=True
        ).stdout
        self.assertEqual(launched, "operator-fixture=ok\n")
        policy = self.root / "operator-install/state/policy.json"
        policy.chmod(0o640)
        rejected = subprocess.run([launcher, "status"], capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 78)
        self.assertIn("ownership or mode is invalid", rejected.stderr)
        policy.chmod(0o600)
        self.assertFalse((release / ".candidate").exists())
        self.assertEqual(stat.S_IMODE((self.root / "operator-install/state").stat().st_mode), 0o700)
        replacements = {
            "@BIN_ROOT@": str(self.root / "operator-install/bin"),
            "@CONFIG_ROOT@": str(self.root / "operator-install/config"),
            "@STATE_ROOT@": str(self.root / "operator-install/state"),
        }
        for name in ("openstack-platform-backup.service", "openstack-platform-backup.timer"):
            installed = self.root / "operator-install/units" / name
            expected = (UNITS / name).read_text()
            for placeholder, value in replacements.items():
                expected = expected.replace(placeholder, value)
            self.assertEqual(installed.read_text(), expected)
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o600)

    def test_installed_operator_launcher_rejects_fixed_path_overrides(self) -> None:
        repository, commit = self._repository()
        self._install(repository, commit, "operator")
        launcher = self.root / "operator-install/bin/openstack-platform"
        fixed = {
            "--platform-config": self.root / "operator-install/config/platform.json",
            "--state-directory": self.root / "operator-install/state",
            "--policy": self.root / "operator-install/state/policy.json",
        }
        for option in fixed:
            with self.subTest(option=option):
                result = subprocess.run(
                    [launcher, option, str(self.root / "attacker"), "status"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 78)
                self.assertIn("fixed-path override", result.stderr)

    def test_operator_install_requires_persistent_owned_private_configuration(self) -> None:
        repository, commit = self._repository()
        result = self._install(repository, commit, "operator", check=False, prepare_config=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install current operator configuration", result.stderr)
        self.assertFalse((self.root / "operator-install/operator-releases/current").exists())

    def test_operator_install_requires_protected_explicit_openstack_wrapper(self) -> None:
        repository, commit = self._repository()
        self.openstack_command.chmod(0o755)

        result = self._install(repository, commit, "operator", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protected OpenStack command", result.stderr)
        self.assertFalse((self.root / "operator-install/operator-releases/current").exists())

    def test_openstack_default_honors_launcher_selected_command(self) -> None:
        environment = os.environ.copy()
        environment["PLATFORM_OPENSTACK_COMMAND"] = str(self.openstack_command)
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import inspect,sys; from openstack_platform import openstack; "
                    "assert inspect.signature(openstack.verify_project).parameters"
                    "['executable'].default == sys.argv[1]"
                ),
                self.openstack_command,
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )

    def test_operator_install_rejects_configuration_symlinks_and_bad_modes(self) -> None:
        repository, commit = self._repository()
        config_root = self.root / "operator-install/config"
        state_root = self.root / "operator-install/state"
        config_root.mkdir(parents=True, mode=0o700)
        state_root.mkdir(parents=True)
        source = self.root / "platform-source.json"
        shutil.copy2(ROOT / "config/platform.example.json", source)
        (config_root / "platform.json").symlink_to(source)
        shutil.copy2(ROOT / "config/platform-policy.example.json", state_root / "policy.json")
        (state_root / "policy.json").chmod(0o644)

        result = self._install(repository, commit, "operator", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("direct regular file", result.stderr)

    def test_helper_install_selects_only_a_complete_action_map(self) -> None:
        repository, commit = self._repository()
        self._install(repository, commit, "helper")

        release = self.root / "helper-install/helper-releases/releases" / commit
        launcher = self.root / "helper-install/bin/openstack-platform-helper"
        self.assertEqual((self.root / "helper-install/helper-releases/current").resolve(), release)
        self.assertEqual(launcher.resolve(), release / "bin/openstack-platform-helper")
        result = subprocess.run([launcher], check=True)
        self.assertEqual(result.returncode, 0)
        launcher_text = launcher.resolve().read_text()
        self.assertIn(
            f"platform_config={self.root / 'helper-install/etc/test-platform/platform.json'}",
            launcher_text,
        )
        self.assertIn('export PLATFORM_CONFIG="$platform_config"', launcher_text)
        self.assertIn('if test -L "$platform_config"', launcher_text)
        self.assertIn("/nix/store/*", launcher_text)
        self.assertNotIn('test ! -L "$platform_config"', launcher_text)
        self.assertNotIn("/srv/app-platform", launcher_text)
        self.assertNotIn("/etc/app-platform", launcher_text)

        platform_config = self.root / "helper-install/etc/test-platform/platform.json"
        mutable = self.root / "mutable-platform.json"
        shutil.copy2(platform_config, mutable)
        platform_config.unlink()
        platform_config.symlink_to(mutable)
        rejected = subprocess.run([launcher], capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 78)
        self.assertIn("invalid symlink target", rejected.stderr)

    def test_partial_helper_action_map_is_never_selected(self) -> None:
        repository, commit = self._repository(partial_helper=True)
        result = self._install(repository, commit, "helper", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("omits the app family", result.stderr)
        self.assertFalse((self.root / "helper-install/helper-releases/current").exists())
        self.assertFalse((self.root / "helper-install/bin/openstack-platform-helper").exists())
        releases = self.root / "helper-install/helper-releases/releases"
        self.assertEqual(list(releases.iterdir()), [])

    def test_manifest_bound_tamper_leaves_current_release_unchanged(self) -> None:
        repository, commit = self._repository()
        self._install(repository, commit, "operator")
        current = self.root / "operator-install/operator-releases/current"
        selected = current.resolve()
        actions = repository / "openstack_platform/helper/actions-v1.txt"
        actions.write_text(actions.read_text() + "tampered.action\n")

        result = self._install(repository, commit, "operator", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wheel inputs do not match", result.stderr)
        self.assertEqual(current.resolve(), selected)
        self.assertEqual((selected / ".complete").read_text(), f"{commit}\n")

    def test_commit_mismatch_does_not_create_a_release(self) -> None:
        repository, actual_commit = self._repository()
        wrong_commit = "0" * 40
        result = self._install(
            repository,
            wrong_commit,
            "operator",
            check=False,
            evidence_commit=actual_commit,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("component set does not match", result.stderr)
        releases = self.root / "operator-install/operator-releases/releases"
        self.assertFalse(releases.exists())

    def test_tracked_changes_must_be_committed(self) -> None:
        repository, commit = self._repository()
        self._evidence(repository, commit)
        with (repository / "openstack_platform/operator.py").open("a", encoding="utf-8") as output:
            output.write("# dirty\n")
        result = self._install(repository, commit, "operator", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wheel inputs do not match", result.stderr)

    def test_backup_unit_uses_installer_owned_path_placeholders(self) -> None:
        service = (UNITS / "openstack-platform-backup.service").read_text()
        for placeholder in ("@BIN_ROOT@", "@CONFIG_ROOT@", "@STATE_ROOT@"):
            self.assertIn(placeholder, service)
        self.assertNotIn("/srv/openstack-platform", service)

    def test_helper_deployment_bootstraps_an_empty_configured_admin(self) -> None:
        repository, commit = self._repository()
        ssh_config = self.root / "fixture-ssh-config"
        ssh_config.write_text("fixture\n", encoding="utf-8")
        ssh_config.chmod(0o600)
        admin = self.root / "empty-admin"
        admin_root = admin / "configured-root"
        admin_state = admin / "configured-state"
        platform = self.root / "operator-platform.json"
        document = json.loads((ROOT / "config/platform.example.json").read_text())
        document["paths"]["root"] = str(admin_root)
        document["paths"]["adminState"] = str(admin_state)
        platform.write_text(json.dumps(document), encoding="utf-8")
        platform.chmod(0o600)
        helper_config_root = admin / "etc"
        live_platform = helper_config_root / document["namespace"] / "platform.json"
        live_platform.parent.mkdir(parents=True)
        store_platform = (
            self.root / "fixture-nix/store/0123456789abcdfghijklmnpqrsvwxyz-platform.json"
        )
        store_platform.parent.mkdir(parents=True)
        store_platform.write_text(json.dumps(document), encoding="utf-8")
        store_platform.chmod(0o444)
        live_platform.symlink_to(store_platform)

        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        self._write(
            fake_bin,
            "ssh",
            f"""
            #!{sys.executable}
            import os
            import subprocess
            import sys

            arguments = sys.argv[1:]
            assert arguments[:2] == ["-F", {str(ssh_config)!r}]
            assert arguments[2:4] == ["platform-admin", "--"]
            command = arguments[4:]
            if command == ["id", "-u"]:
                print(os.geteuid())
                raise SystemExit(0)
            environment = None
            if len(command) > 1 and command[0] == {sys.executable!r} and command[1].endswith("install_release.py"):
                environment = os.environ.copy()
                environment["PATH"] = ""
            raise SystemExit(subprocess.run(command, env=environment).returncode)
            """,
        )
        self._write(
            fake_bin,
            "scp",
            f"""
            #!{sys.executable}
            import shutil
            import sys

            arguments = sys.argv[1:]
            assert arguments[:2] == ["-F", {str(ssh_config)!r}]
            assert arguments[2] == "--"
            source, target = arguments[3:]
            prefix = "platform-admin:"
            assert target.startswith(prefix)
            shutil.copy2(source, target[len(prefix):])
            """,
        )
        for command in (fake_bin / "ssh", fake_bin / "scp"):
            command.chmod(0o755)

        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        environment["PLATFORM_CONFIG"] = str(platform)
        environment["PLATFORM_HELPER_CONFIG_ROOT"] = str(helper_config_root)
        environment["PLATFORM_RELEASE_MANIFEST"] = str(
            self._evidence(repository, commit) / "release-manifest.json"
        )
        environment["PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT"] = (
            release_manifest.UNSIGNED_ACKNOWLEDGEMENT
        )
        result = subprocess.run(
            [repository / "deploy/releases/deploy_helper_release.sh", commit],
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

        release = admin_state / "operator/helper-releases/releases" / commit
        launcher = admin_root / "bin/openstack-platform-helper"
        self.assertEqual(result.stdout.splitlines()[-1], f"helper-release={commit}:verified")
        self.assertEqual((admin_state / "operator/helper-releases/current").resolve(), release)
        self.assertEqual(launcher.resolve(), release / "bin/openstack-platform-helper")
        launcher_text = launcher.resolve().read_text(encoding="utf-8")
        self.assertIn(f"platform_config={live_platform}", launcher_text)
        self.assertEqual((release / ".complete").read_text(), f"{commit}\n")
        self.assertEqual(list((admin_state / "operator/helper-releases/incoming").iterdir()), [])

    def test_helper_deployment_refuses_mismatched_live_config_before_upload(self) -> None:
        repository, commit = self._repository()
        ssh_config = self.root / "fixture-ssh-config"
        ssh_config.write_text("fixture\n", encoding="utf-8")
        ssh_config.chmod(0o600)
        platform = self.root / "operator-platform.json"
        document = json.loads((ROOT / "config/platform.example.json").read_text())
        document["namespace"] = "test-platform"
        document["paths"]["root"] = str(self.root / "admin-root")
        document["paths"]["adminState"] = str(self.root / "admin-state")
        platform.write_text(json.dumps(document), encoding="utf-8")
        platform.chmod(0o600)
        helper_config_root = self.root / "admin-etc"
        live_platform = helper_config_root / "test-platform/platform.json"
        live_platform.parent.mkdir(parents=True)
        mismatched = dict(document)
        mismatched["project"] = "different-project"
        live_platform.write_text(json.dumps(mismatched), encoding="utf-8")

        upload_marker = self.root / "scp-was-called"
        fake_bin = self.root / "mismatch-fake-bin"
        fake_bin.mkdir()
        self._write(
            fake_bin,
            "ssh",
            f"""
            #!{sys.executable}
            import subprocess
            import sys

            arguments = sys.argv[1:]
            assert arguments[:2] == ["-F", {str(ssh_config)!r}]
            assert arguments[2:4] == ["platform-admin", "--"]
            raise SystemExit(subprocess.run(arguments[4:]).returncode)
            """,
        )
        self._write(
            fake_bin,
            "scp",
            f"""
            #!{sys.executable}
            from pathlib import Path
            Path({str(upload_marker)!r}).touch()
            raise SystemExit(99)
            """,
        )
        for command in (fake_bin / "ssh", fake_bin / "scp"):
            command.chmod(0o755)

        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
        environment["PLATFORM_CONFIG"] = str(platform)
        environment["PLATFORM_HELPER_CONFIG_ROOT"] = str(helper_config_root)
        environment["PLATFORM_RELEASE_MANIFEST"] = str(
            self._evidence(repository, commit) / "release-manifest.json"
        )
        environment["PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT"] = (
            release_manifest.UNSIGNED_ACKNOWLEDGEMENT
        )
        result = subprocess.run(
            [repository / "deploy/releases/deploy_helper_release.sh", commit],
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match operator project, namespace, and paths", result.stderr)
        self.assertFalse(upload_marker.exists())
        self.assertFalse((self.root / "admin-state").exists())

    def test_helper_deployment_uses_pinned_ssh_and_configured_admin_paths(self) -> None:
        script = HELPER_DEPLOY.read_text()
        self.assertIn("ssh_config=$operator_root/.secrets/ssh/config", script)
        self.assertIn("platform_contract.json", script)
        self.assertNotIn("PLATFORM_SSH_CONFIG", script)
        self.assertIn('paths["root"]', script)
        self.assertIn('paths["adminState"]', script)
        self.assertIn("/run/current-system/sw/bin/python3.14", script)
        self.assertIn('--archive-sha256 "$archive_sha256"', script)
        self.assertNotIn("/srv/" + "test-platform", script)


class OperatorConfigInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("configuration installer deliberately refuses root")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "private").mkdir()
        self.platform = self.root / "private/platform.json"
        self.policy = self.root / "private/platform-policy.json"
        shutil.copy2(ROOT / "config/platform.example.json", self.platform)
        shutil.copy2(ROOT / "config/platform-policy.example.json", self.policy)
        self.policy.chmod(0o600)
        self.destination = self.root / "srv/openstack-platform/config"
        self.state = self.root / "srv/openstack-platform/state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        return subprocess.run(
            [
                sys.executable,
                "-S",
                CONFIG_INSTALLER,
                "--platform",
                self.platform,
                "--policy",
                self.policy,
                "--config-root",
                self.destination,
                "--state-root",
                self.state,
            ],
            env=environment,
            check=check,
            capture_output=True,
            text=True,
        )

    def test_standalone_help_imports_without_pythonpath_outside_the_checkout(self) -> None:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-S", CONFIG_INSTALLER, "--help"],
            cwd=self.root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--platform", result.stdout)

    def test_installs_and_updates_nonsecret_configuration_without_values_in_output(self) -> None:
        first = self._run()
        installed_platform = self.destination / "platform.json"
        installed_policy = self.state / "policy.json"
        before = installed_platform.stat().st_ino
        policy_before = installed_policy.stat().st_ino
        platform = json.loads(self.platform.read_text())
        platform["displayName"] = "Updated Platform"
        self.platform.write_text(json.dumps(platform))
        policy = json.loads(self.policy.read_text())
        policy["standard"]["memoryMiB"] = 4096
        self.policy.write_text(json.dumps(policy))
        second = self._run()

        self.assertEqual(first.stdout, "operator-config=installed\n")
        self.assertEqual(second.stdout, "operator-config=installed\n")
        self.assertNotIn("Updated Platform", second.stdout + second.stderr)
        self.assertNotEqual(before, installed_platform.stat().st_ino)
        self.assertNotEqual(policy_before, installed_policy.stat().st_ino)
        self.assertEqual(
            json.loads(installed_platform.read_text())["displayName"], "Updated Platform"
        )
        self.assertEqual(json.loads(installed_policy.read_text())["standard"]["memoryMiB"], 4096)
        self.assertFalse((self.destination / "projects").exists())
        self.assertEqual(stat.S_IMODE(installed_platform.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(installed_policy.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_install_creates_only_clean_operator_configuration_paths(self) -> None:
        result = self._run()

        self.assertEqual(result.stdout, "operator-config=installed\n")
        self.assertTrue((self.destination / "platform.json").is_file())
        self.assertTrue((self.state / "policy.json").is_file())
        self.assertFalse((self.destination / "projects").exists())

    def test_rejects_symlink_input_without_replacing_installed_configuration(self) -> None:
        self._run()
        installed = (self.destination / "platform.json").read_bytes()
        self.platform.unlink()
        self.platform.symlink_to(ROOT / "config/platform.example.json")

        result = self._run(check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("direct directories and regular files", result.stderr)
        self.assertEqual((self.destination / "platform.json").read_bytes(), installed)


if __name__ == "__main__":
    unittest.main()
