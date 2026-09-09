from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deploy.releases import install_release as installer
from openstack_platform import image_pipeline as pipeline
from openstack_platform import release_manifest as release
from openstack_platform import setup
from openstack_platform.config import load_platform
from openstack_platform.controller import seed_images
from openstack_platform.openstack import publisher_metadata
from tests.repository_fixtures import clean_repository

ROOT = Path(__file__).resolve().parents[1]


class ImagePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = tempfile.TemporaryDirectory()
        cls.repository, cls.commit = clean_repository(ROOT, Path(cls.source.name) / "repository")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.source.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "retained"
        self.platform = self.directory / "platform.json"
        document = json.loads((self.repository / "config/platform.example.json").read_text())
        document["images"] = pipeline._image_names(document["images"], self.commit)
        pipeline._json(self.platform, document)
        self.values = {
            **os.environ,
            "GITHUB_SHA": self.commit,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "example/platform",
            "GITHUB_RUN_ID": "100",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_EVENT_NAME": "push",
            "PLATFORM_ALLOW_UNSIGNED_PRODUCTION": release.UNSIGNED_PRODUCTION_ACKNOWLEDGEMENT,
            "PLATFORM_ENVIRONMENT": "production",
        }
        self.context = pipeline.publication_context(self.values)
        inventory = load_platform(self.platform)
        self.evidence_context = {
            **self.context,
            "inventorySha256": pipeline._inventory_sha256(inventory),
        }
        for index, role in enumerate(release.ROLES):
            directory = self.root / role
            directory.mkdir(parents=True)
            output = f"/nix/store/{index + 1:032x}-{role}-image"
            (directory / f"{role}.qcow2").write_bytes(b"QFI\xfb" + role.encode() * 10)
            (directory / "path-info.json").write_text(
                json.dumps(
                    {
                        output: {
                            "narHash": "sha256-"
                            + base64.b64encode(hashlib.sha256(role.encode()).digest()).decode(),
                            "narSize": 1000,
                            "references": [],
                        }
                    }
                )
            )
            (directory / "build.log").write_text("Nix build succeeded\n")
            (directory / "qemu.log").write_text(
                f"qcow-config-drive-smoke=passed role={role} accelerator=kvm\n"
            )
            (directory / "qemu-serial.log").write_text(f"platform-{role}-qcow-smoke-passed\n")
            pipeline._json(
                directory / "record.json",
                {
                    "role": role,
                    "context": self.context,
                    "inventorySha256": pipeline._inventory_sha256(inventory),
                    "outputStorePath": output,
                    "publicationMetadata": dict(publisher_metadata(inventory, role, self.commit)),
                    "qemu": "passed",
                    "sha256": {
                        path.name: release._artifact_sha256(path) for path in directory.iterdir()
                    },
                },
            )
        self.gates = self.directory / "gates.json"
        pipeline._json(self.gates, dict.fromkeys(pipeline.GATES, "success"))

    def prepare(self) -> None:
        pipeline.prepare(self.repository, self.root, self.platform, self.gates, self.values)

    def test_unsigned_production_retains_and_publishes_exact_bytes_without_rebuilding(self) -> None:
        before = {role: (self.root / role / f"{role}.qcow2").read_bytes() for role in release.ROLES}
        self.prepare()
        receipt = release._load(self.root / "publication.json")
        self.assertEqual(receipt["ci"], dict.fromkeys(pipeline.GATES, "success"))
        self.assertEqual(receipt["context"], self.context)
        self.assertEqual(len(receipt["files"]), 36)  # six per role plus six evidence files
        component = release._load(self.root / "evidence/release-manifest.json")
        self.assertEqual(component["releaseChannel"], "production")
        self.assertEqual(component["trust"]["mode"], "production-unsigned")
        self.assertFalse(list(self.root.rglob("*.sig")))
        self.assertFalse(list(self.root.rglob("*.pem")))
        # Simulate GitHub upload/download: no Nix store or runner-local paths are needed.
        downloaded = self.directory / "downloaded"
        shutil.copytree(self.root, downloaded)
        shutil.rmtree(self.root)
        self.root = downloaded
        real_run = subprocess.run
        published = []

        def run(arguments, **kwargs):
            if str(arguments[0]).endswith("publish_nixos_image.sh"):
                role, qcow = arguments[1:3]
                self.assertEqual(Path(qcow).read_bytes(), before[role])
                self.assertEqual(
                    kwargs["env"]["PLATFORM_ARTIFACT_QCOW2_SHA256"],
                    hashlib.sha256(before[role]).hexdigest(),
                )
                published.append(role)
                return subprocess.CompletedProcess(arguments, 0)
            self.assertEqual(arguments[0], "git")  # no build, QEMU, fetch, or signer
            return real_run(arguments, **kwargs)

        with mock.patch.object(pipeline.subprocess, "run", side_effect=run):
            # A retried publisher consumes the earlier complete build, not new bytes.
            pipeline.publish(
                self.repository,
                self.root,
                self.platform,
                {**self.values, "GITHUB_RUN_ATTEMPT": "2"},
            )
        self.assertEqual(published, list(release.ROLES))

    def test_build_publication_and_hosted_seed_use_identical_inventory_in_both_modes(self) -> None:
        values = {
            **self.values,
            "OPENSTACK_PUBLISH_ENABLED": "true",
            "OPENSTACK_UNSIGNED_PRODUCTION": release.UNSIGNED_PRODUCTION_ACKNOWLEDGEMENT,
            "PLATFORM_CONFIG_JSON": self.platform.read_text(),
            "OS_PROJECT_ID": load_platform(self.platform).project_id.replace("-", ""),
        }
        build, unsigned, signed = (
            self.directory / name for name in ("build.json", "unsigned.json", "signed.json")
        )
        pipeline.write_inventory(self.repository, build, values)
        pipeline.write_inventory(self.repository, unsigned, values)
        pipeline.write_inventory(
            self.repository,
            signed,
            {**values, "OPENSTACK_UNSIGNED_PRODUCTION": ""},
        )
        self.assertEqual(build.read_bytes(), signed.read_bytes())
        self.assertEqual(build.read_bytes(), unsigned.read_bytes())
        self.assertEqual(build.stat().st_mode & 0o777, 0o600)
        for role in release.ROLES:
            self.assertEqual(
                json.loads(unsigned.read_text())["images"][role],
                json.loads(signed.read_text())["images"][role],
            )
            self.assertEqual(
                publisher_metadata(load_platform(unsigned), role, self.commit),
                publisher_metadata(load_platform(signed), role, self.commit),
            )
        self.assertEqual(
            pipeline._inputs(self.root, signed, self.context),
            pipeline._inputs(self.root, unsigned, self.context),
        )
        # Setup builds these seed records from its image inventory. Use the
        # exact build inventory as the immutable /etc/.../platform.json input.
        seed_path = self.directory / "image-selections.json"
        guest = load_platform(build)
        document = {
            "schemaVersion": 1,
            "projectId": guest.project_id,
            "namespace": guest.namespace,
            "images": {
                role: {
                    "imageId": f"00000000-0000-4000-8000-{index:012d}",
                    "displayName": load_platform(unsigned).get(f"images.{role}"),
                    "sourceCommit": self.commit,
                    "compatibilityHash": publisher_metadata(guest, role, self.commit)[
                        f"{guest.namespace.replace('-', '_')}_compatibility_sha256"
                    ],
                }
                for index, role in enumerate(release.ROLES, 1)
            },
        }
        pipeline._json(seed_path, document)
        seed_path.chmod(0o600)
        seed_images.seed(
            platform_config=build, state_directory=self.directory / "controller", manifest=seed_path
        )
        seed_images.seed(
            platform_config=signed,
            state_directory=self.directory / "controller",
            manifest=seed_path,
        )
        document["images"]["worker"]["displayName"] += "-unsigned"
        pipeline._json(seed_path, document)
        with self.assertRaisesRegex(seed_images.SeedFailure, "unproven"):
            seed_images.seed(
                platform_config=build,
                state_directory=self.directory / "controller",
                manifest=seed_path,
            )
        with self.assertRaisesRegex(seed_images.SeedFailure, "name does not match"):
            seed_images.seed(
                platform_config=build,
                state_directory=self.directory / "fresh-controller",
                manifest=seed_path,
            )
        changed = json.loads(unsigned.read_text())
        changed["images"]["worker"] += "-unsigned"
        pipeline._json(unsigned, changed)
        with self.assertRaisesRegex(release.ReleaseVerificationError, "role evidence"):
            pipeline._inputs(self.root, unsigned, self.context)

    def test_setup_reuses_all_published_sha512_roles_without_rebuilding_images(self) -> None:
        self.prepare()
        artifact_path = self.root / "evidence/artifacts/role-artifacts.json"
        artifact = release._load(artifact_path)
        inventory = load_platform(self.platform)
        prefix = inventory.namespace.replace("-", "_")
        ids = {
            role: f"00000000-0000-4000-8000-{index:012d}"
            for index, role in enumerate(release.ROLES, 1)
        }
        paths = mock.Mock(
            repository=self.repository,
            workspace=self.directory,
            platform=self.platform,
            openstack_wrapper=Path("fake-openstack"),
        )

        def provider(arguments, **_kwargs):
            if arguments[1:3] == ("image", "list"):
                name = arguments[arguments.index("--name") + 1]
                role = next(
                    role for role in release.ROLES if inventory.get(f"images.{role}") == name
                )
                return [{"Name": name, "ID": ids[role]}]
            self.assertEqual(arguments[1:3], ("image", "show"))
            role = next(role for role, image_id in ids.items() if image_id == arguments[3])
            record = artifact["roleArtifacts"][role]
            return {
                "status": "active",
                "os_hash_algo": "sha512",
                "os_hash_value": "a" * 128,
                "properties": {
                    **record["publicationMetadata"],
                    f"{prefix}_artifact_manifest_sha256": release._sha256_file(artifact_path),
                    f"{prefix}_qcow2_sha256": record["qcow2Sha256"],
                    f"{prefix}_nix_closure_sha256": record["nixClosureSha256"],
                    f"{prefix}_nix_output": record["nixOutput"],
                },
            }

        def download(arguments, **_kwargs):
            self.assertEqual(arguments[1:3], ("image", "save"))
            role = next(role for role, image_id in ids.items() if image_id == arguments[-1])
            Path(arguments[arguments.index("--file") + 1]).write_bytes(
                (self.root / role / f"{role}.qcow2").read_bytes()
            )
            return ""

        with (
            mock.patch.object(setup, "_json_command", side_effect=provider),
            mock.patch.object(setup, "_command", side_effect=download),
            mock.patch.object(
                setup, "_build_nix_output", return_value=Path("/nix/store/smoke")
            ) as build,
        ):
            self.assertEqual(
                setup._build_and_publish_images(
                    paths,
                    inventory.document,
                    {},
                    Path("/nix/store/python"),
                    self.commit,
                    artifact,
                    artifact_path,
                ),
                ids,
            )
        build.assert_called_once_with(self.repository, {}, "imageSmoke")

    def test_offline_signing_inputs_validate_github_main_run_and_all_successful_source_jobs(
        self,
    ) -> None:
        run = {
            "id": 100,
            "head_sha": self.commit,
            "head_branch": "main",
            "event": "push",
            "head_repository": {"full_name": "example/platform"},
            "path": ".github/workflows/ci.yml",
            "run_attempt": 2,
            "conclusion": "failure",
        }  # Awaiting external evidence is expected.
        names = [
            "Static checks",
            "Build, start, and health-check generated Bun and Node recipes",
            "Nix evaluation and formatting",
            "Test packaged binaries",
            *(f"Test {role} VM" for role in release.ROLES),
            *(f"Build production {role} image" for role in release.ROLES),
        ]
        jobs = {
            "total_count": len(names),
            "jobs": [
                {"name": name, "head_sha": self.commit, "conclusion": "success"} for name in names
            ],
        }
        output = self.directory / "signing-inputs.json"
        real_run = subprocess.run

        def api(arguments, **kwargs):
            if arguments[:2] == ["gh", "api"]:
                document = jobs if "/jobs?" in arguments[2] else run
                return subprocess.CompletedProcess(arguments, 0, json.dumps(document))
            self.assertEqual(arguments[0], "git")
            return real_run(arguments, **kwargs)

        with (
            mock.patch.object(pipeline.subprocess, "run", side_effect=api),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            pipeline.signing_inputs(
                self.repository,
                self.root,
                self.repository / "config/platform.example.json",
                github_repository="example/platform",
                run_id="100",
                output=output,
            )
            self.assertEqual(
                json.loads(output.with_suffix(".context.json").read_text()), self.evidence_context
            )
            self.assertEqual(
                json.loads(output.read_text()),
                pipeline._inputs(self.root, self.platform, self.context),
            )
            output.unlink()
            output.with_suffix(".context.json").unlink()
            for key, value in (
                ("head_branch", "feature"),
                ("head_sha", "a" * 40),
                ("event", "pull_request"),
                ("head_repository", {"full_name": "fork/platform"}),
            ):
                original = run[key]
                run[key] = value
                with self.assertRaises(release.ReleaseVerificationError):
                    pipeline.signing_inputs(
                        self.repository,
                        self.root,
                        self.platform,
                        github_repository="example/platform",
                        run_id="100",
                        output=output,
                    )
                run[key] = original
            for job in jobs["jobs"]:
                job["conclusion"] = "skipped"
                with self.assertRaises(release.ReleaseVerificationError):
                    pipeline.signing_inputs(
                        self.repository,
                        self.root,
                        self.platform,
                        github_repository="example/platform",
                        run_id="100",
                        output=output,
                    )
                job["conclusion"] = "success"
            self.assertFalse(output.exists())

    def test_build_role_retains_smoke_and_closure_logs_only_after_success(self) -> None:
        role = "admin"
        shutil.rmtree(self.root / role)
        output = self.directory / "nix-output"
        output.mkdir()
        (output / "image.qcow2").write_bytes(b"exact-build-bytes")
        real_run = subprocess.run

        def run(arguments, **kwargs):
            if arguments[:2] == ["nix", "build"]:
                Path(arguments[arguments.index("--out-link") + 1]).symlink_to(output)
                kwargs["stdout"].write(b"nix build passed\n")
            elif arguments[:2] == ["nix", "path-info"]:
                kwargs["stdout"].write(b'{"closure": "recorded"}\n')
            elif str(arguments[0]).endswith("smoke_openstack_image.sh"):
                self.assertEqual(Path(arguments[2]).read_bytes(), b"exact-build-bytes")
                kwargs["stdout"].write(
                    b"qcow-config-drive-smoke=passed role=admin accelerator=kvm\n"
                )
                Path(kwargs["env"]["PLATFORM_QEMU_SERIAL_LOG"]).write_bytes(b"full serial evidence")
            else:
                return real_run(arguments, **kwargs)
            return subprocess.CompletedProcess(arguments, 0)

        with mock.patch.object(pipeline.subprocess, "run", side_effect=run):
            pipeline.build_role(self.repository, self.root, self.platform, role, self.values)
        record = release._load(self.root / role / "record.json")
        self.assertEqual(
            record["sha256"]["admin.qcow2"], hashlib.sha256(b"exact-build-bytes").hexdigest()
        )
        self.assertIn("qemu-serial.log", record["sha256"])
        self.assertFalse((self.root / role / "result").exists())

        shutil.rmtree(self.root / role)

        def fail_smoke(arguments, **kwargs):
            if str(arguments[0]).endswith("smoke_openstack_image.sh"):
                raise subprocess.CalledProcessError(1, arguments)
            return run(arguments, **kwargs)

        with mock.patch.object(pipeline.subprocess, "run", side_effect=fail_smoke):
            with self.assertRaises(subprocess.CalledProcessError):
                pipeline.build_role(self.repository, self.root, self.platform, role, self.values)
        self.assertTrue((self.root / role / "qemu.log").exists())
        self.assertFalse((self.root / role / "record.json").exists())

    def test_component_artifact_install_and_setup_gates_require_same_explicit_mode(self) -> None:
        self.prepare()
        child = pipeline._trust(self.root, self.values)
        component = Path(child["PLATFORM_RELEASE_MANIFEST"])
        for verifier in (
            lambda values: release.verify_from_environment(self.repository, self.commit, values),
            lambda values: release.verify_artifact_from_environment(component, values),
        ):
            self.assertEqual(verifier(child)["releaseChannel"], "production")
            with self.assertRaises(release.ReleaseVerificationError):
                verifier({**child, "PLATFORM_ALLOW_UNSIGNED_PRODUCTION": ""})
            with self.assertRaises(release.ReleaseVerificationError):
                verifier({**child, "PLATFORM_ALLOW_UNSIGNED_PRODUCTION": "true"})
        arguments = setup._release_evidence_arguments(child)
        self.assertIn("--allow-unsigned-production", arguments)
        self.assertNotIn("--allow-unsigned-development", arguments)
        for production, development, success in (
            (False, False, False),
            (False, True, False),
            (True, True, False),
            (True, False, True),
        ):
            with self.subTest(production=production, development=development):
                kwargs = dict(
                    commit=self.commit,
                    manifest=component,
                    signature=None,
                    trust_root=None,
                    allow_unsigned_development=development,
                    allow_unsigned_production=production,
                )
                if success:
                    installer._verify_release_gate(self.repository, **kwargs)
                else:
                    with self.assertRaises(installer.InstallFailure):
                        installer._verify_release_gate(self.repository, **kwargs)
        with self.assertRaises(release.ReleaseVerificationError):
            release.generate(
                self.repository,
                self.commit,
                self.directory / "mixed",
                signing_key=None,
                unsigned=True,
                unsigned_production=True,
            )

    def test_unsigned_production_cli_role_gate_rejects_wrong_commit_and_missing_size(self) -> None:
        self.prepare()
        component = self.root / "evidence/release-manifest.json"
        artifact = self.root / "evidence/artifacts/role-artifacts.json"
        record = release._load(self.root / "admin/record.json")
        arguments = [
            "verify-role",
            "--component-manifest",
            str(component),
            "--manifest",
            str(artifact),
            "--allow-unsigned-production",
            "--role",
            "admin",
            "--qcow2",
            str(self.root / "admin/admin.qcow2"),
            "--path-info",
            str(self.root / "admin/path-info.json"),
            "--output-store-path",
            record["outputStorePath"],
            "--platform",
            str(self.platform),
            "--commit",
            self.commit,
        ]
        with mock.patch("builtins.print"):
            self.assertEqual(release.main(arguments), 0)
        with self.assertRaisesRegex(release.ReleaseVerificationError, "commit differs"):
            release.main([*arguments[:-1], "a" * 40])
        document = release._load(artifact)
        document["roleArtifacts"]["admin"].pop("qcow2SizeBytes")
        pipeline._json(artifact, document)
        with self.assertRaisesRegex(release.ReleaseVerificationError, "identity.*malformed"):
            release.main(arguments)

    def test_ci_gate_failure_missing_or_skipped_never_seals(self) -> None:
        for gate in pipeline.GATES:
            for status in ("failure", "skipped", "cancelled", None):
                with self.subTest(gate=gate, status=status):
                    results = dict.fromkeys(pipeline.GATES, "success")
                    if status is None:
                        results.pop(gate)
                    else:
                        results[gate] = status
                    pipeline._json(self.gates, results)
                    with self.assertRaisesRegex(release.ReleaseVerificationError, "CI gates"):
                        self.prepare()
                    self.assertFalse((self.root / "evidence").exists())

    def test_missing_mixed_run_and_failed_qemu_records_are_rejected(self) -> None:
        path = self.root / "builder/record.json"
        original = path.read_bytes()
        for key, value in (
            ("context", {**self.context, "runId": "101"}),
            ("qemu", "failed"),
            ("role", "worker"),
        ):
            record = json.loads(original)
            record[key] = value
            pipeline._json(path, record)
            with self.assertRaisesRegex(release.ReleaseVerificationError, "role evidence"):
                self.prepare()
        path.unlink()
        with self.assertRaises(release.ReleaseVerificationError):
            self.prepare()

    def test_every_retained_input_is_rechecked_before_any_provider_call(self) -> None:
        self.prepare()
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            original = path.read_bytes()
            path.write_bytes(original + b"tamper")
            with (
                self.subTest(path=path.relative_to(self.root)),
                mock.patch.object(pipeline, "_verify") as verified,
            ):
                with self.assertRaises(release.ReleaseVerificationError):
                    pipeline.publish(self.repository, self.root, self.platform, self.values)
                verified.assert_not_called()
            path.write_bytes(original)

    def test_unsigned_default_rollback_and_development_separation(self) -> None:
        self.prepare()
        for changes in (
            {"PLATFORM_ALLOW_UNSIGNED_PRODUCTION": ""},
            {"PLATFORM_ALLOW_UNSIGNED_PRODUCTION": "true"},
            {"PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT": release.UNSIGNED_ACKNOWLEDGEMENT},
            {"PLATFORM_RELEASE_TRUST_ROOT": "/unused/public.pem"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(release.ReleaseVerificationError):
                    pipeline.publish(
                        self.repository, self.root, self.platform, {**self.values, **changes}
                    )

    def test_no_automatic_fallback_from_signed_default_to_unsigned(self) -> None:
        self.values.pop("PLATFORM_ALLOW_UNSIGNED_PRODUCTION")
        with self.assertRaisesRegex(release.ReleaseVerificationError, "external trust roots"):
            self.prepare()
        self.assertFalse((self.root / "evidence").exists())

    def test_signed_external_evidence_uses_same_bytes_and_never_needs_private_key(self) -> None:
        self.values.pop("PLATFORM_ALLOW_UNSIGNED_PRODUCTION")
        key, public = self.directory / "private.pem", self.directory / "public.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", key], check=True)
        subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", public], check=True)
        evidence = self.root / "evidence"
        component = release.generate(
            self.repository, self.commit, evidence, signing_key=key, unsigned=False
        )
        inputs = self.directory / "inputs.json"
        pipeline._json(inputs, pipeline._inputs(self.root, self.platform, self.context))
        release.generate_artifact_manifest(
            component,
            inputs,
            evidence / "artifacts",
            signing_key=key,
            unsigned=False,
            build_context=self.evidence_context,
        )
        self.values.update(
            {
                "PLATFORM_RELEASE_TRUST_ROOT": str(public),
                "PLATFORM_ARTIFACT_TRUST_ROOT": str(public),
            }
        )
        for key_name, value in (
            ("runId", "101"),
            ("repository", "fork/platform"),
            ("runAttempt", "2"),
            ("inventorySha256", "b" * 64),
        ):
            release.generate_artifact_manifest(
                component,
                inputs,
                evidence / "artifacts",
                signing_key=key,
                unsigned=False,
                build_context={**self.evidence_context, key_name: value},
            )
            with self.assertRaisesRegex(release.ReleaseVerificationError, "exact CI build context"):
                pipeline._verify(self.repository, self.root, self.platform, self.values)
        release.generate_artifact_manifest(
            component,
            inputs,
            evidence / "artifacts",
            signing_key=key,
            unsigned=False,
            build_context=self.evidence_context,
        )
        key.unlink()
        self.prepare()
        child, artifact = pipeline._verify(self.repository, self.root, self.platform, self.values)
        self.assertEqual(artifact["trust"]["mode"], "production-ed25519")
        self.assertNotIn("PLATFORM_ALLOW_UNSIGNED_PRODUCTION", child)
        signature = evidence / "artifacts/role-artifacts.sig"
        signature.write_bytes(b"broken")
        with self.assertRaises(release.ReleaseVerificationError):
            pipeline.publish(self.repository, self.root, self.platform, self.values)

    def test_pr_other_branch_and_wrong_run_cannot_publish(self) -> None:
        self.prepare()
        for key, value in (
            ("GITHUB_EVENT_NAME", "pull_request"),
            ("GITHUB_REF", "refs/heads/dev"),
            ("GITHUB_SHA", "a" * 40),
            ("GITHUB_RUN_ID", "101"),
            ("GITHUB_RUN_ATTEMPT", "0"),
            ("GITHUB_REPOSITORY", "fork/platform"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(release.ReleaseVerificationError):
                    pipeline.publish(
                        self.repository, self.root, self.platform, {**self.values, key: value}
                    )

    def test_push_path_selection_excludes_docs_and_includes_all_image_inputs(self) -> None:
        repository, commit = clean_repository(self.repository, self.directory / "push-repository")

        def commit_change(path: str) -> str:
            file = repository / path
            file.write_text(file.read_text() + "\n# test change\n" if file.exists() else "test\n")
            subprocess.run(["git", "add", path], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "change",
                ],
                cwd=repository,
                check=True,
            )
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True
            ).strip()

        docs = commit_change("nix/README.md")
        self.assertFalse(pipeline.relevant_push(repository, commit, docs))
        code = commit_change("nix/roles/admin.nix")
        self.assertTrue(pipeline.relevant_push(repository, docs, code))
        self.assertTrue(pipeline.relevant_push(repository, "0" * 40, code))
        with self.assertRaises(release.ReleaseVerificationError):
            pipeline.relevant_push(repository, "a" * 40, code)


if __name__ == "__main__":
    unittest.main()
