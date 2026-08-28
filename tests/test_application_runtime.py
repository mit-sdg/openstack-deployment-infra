from __future__ import annotations

import json
import tarfile
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openstack_platform.controller.application_runtime as app_module
from openstack_platform.controller import database as db
from openstack_platform.controller.application_runtime import (
    ApplicationError,
    BuilderObservation,
    DeploymentAcceptance,
    DeploymentFailed,
    Manifest,
    Recipe,
    StorageBinding,
    accept_healthy_deployment,
    acquire_github_commit,
    apply_registry_retention,
    build_with_disposable_builder,
    create_build_archive,
    create_builder,
    create_worker,
    deploy_and_cleanup,
    deployment_recovery_action,
    execute_builder_build,
    generate_recipe,
    nomad_candidate_identity,
    parse_build_metadata,
    parse_builder_observation,
    parse_dotenv,
    parse_worker_observation,
    render_nomad_job,
    set_environment,
)
from openstack_platform.config import PlatformConfig, RuntimeImages
from openstack_platform.controller.deployment_config import parse_configuration
from openstack_platform.helper import production
from openstack_platform.runtime import HttpResult
from openstack_platform.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
DIGEST = "b" * 64
BUN_IMAGE = f"docker.io/oven/bun@sha256:{'1' * 64}"
NODE_IMAGE = f"docker.io/library/node@sha256:{'2' * 64}"
APP_ID = "12345678-1234-4234-8234-123456789abc"
SENTINEL = "provider-secret-that-must-not-escape"


class ManifestAndRecipeTests(unittest.TestCase):
    def test_recipe_is_digest_pinned_deterministic_and_uses_exec_forms(self) -> None:
        manifest = Manifest("bun", (".",), "build", "start", 3000, "/health")
        images = RuntimeImages(bun=BUN_IMAGE, node=NODE_IMAGE)
        first = generate_recipe(manifest, images)
        second = generate_recipe(manifest, images)
        text = first.dockerfile.decode()
        self.assertEqual(first, second)
        self.assertIn(f"FROM {BUN_IMAGE}", text)
        self.assertIn('RUN ["bun","run","build"]', text)
        self.assertIn('CMD ["bun","run","start"]', text)
        self.assertNotIn("sh -c", text)
        self.assertEqual(
            [line for line in text.splitlines() if line.startswith(("ARG ", "ENV "))],
            ["ENV NODE_ENV=production"],
        )
        self.assertIn("USER 65532:65532", text)
        self.assertEqual(len(first.sha256), 64)

    def test_node_recipe_uses_frozen_npm_install_and_asserted_script_command(self) -> None:
        manifest = Manifest("node", (".",), None, "serve", 8080, "/ready")
        recipe = generate_recipe(
            manifest,
            RuntimeImages(bun=BUN_IMAGE, node=NODE_IMAGE),
        ).dockerfile.decode()
        self.assertIn(f"FROM {NODE_IMAGE}", recipe)
        self.assertIn('RUN ["npm","ci"]', recipe)
        self.assertIn('CMD ["npm","run","serve"]', recipe)
        self.assertNotIn('RUN ["npm","run"', recipe)
        self.assertNotIn("bun", recipe.lower())
        self.assertEqual(
            [line for line in recipe.splitlines() if line.startswith(("ARG ", "ENV "))],
            ["ENV NODE_ENV=production"],
        )

    def test_dotenv_is_strict_and_non_executable(self) -> None:
        self.assertEqual(
            parse_dotenv("MODE=production\nLABEL='safe value'\n"),
            {"MODE": "production", "LABEL": "safe value"},
        )
        for payload in (
            "MODE=one\nMODE=two\n",
            "export MODE=one\n",
            "MODE=${OTHER}\n",
            "MODE=$(id)\n",
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                parse_dotenv(payload)


class SourceTests(unittest.TestCase):
    def test_exact_public_commit_is_checked_and_git_metadata_removed(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            calls.append(tuple(argv))
            destination = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path(argv[-1])
            if "init" in argv:
                (destination / ".git").mkdir()
                (destination / "README.md").write_text("exact checkout\n")
            stdout = (COMMIT + "\n").encode() if "rev-parse" in argv else b""
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(kwargs["env"]["GIT_LFS_SKIP_SMUDGE"], "1")
            self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
            return SimpleNamespace(stdout=stdout)

        with tempfile.TemporaryDirectory() as directory:
            source = acquire_github_commit(
                "https://github.com/example/public-app",
                COMMIT,
                Path(directory) / "source",
                command_runner=runner,
            )
            self.assertTrue((source / "README.md").is_file())
            self.assertFalse((source / ".git").exists())
        fetch = next(call for call in calls if "fetch" in call)
        self.assertIn("https://github.com/example/public-app", fetch)
        self.assertIn(COMMIT, fetch)
        self.assertNotIn("shell", " ".join(fetch))

    def test_source_phases_share_one_absolute_deadline(self) -> None:
        timeouts: list[float] = []

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            timeouts.append(float(kwargs["timeout_seconds"]))
            root = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path(argv[-1])
            if "init" in argv:
                (root / ".git").mkdir()
                (root / "README.md").write_text("exact checkout\n")
            time.sleep(0.01)
            stdout = (COMMIT + "\n").encode() if "rev-parse" in argv else b""
            return SimpleNamespace(stdout=stdout)

        with tempfile.TemporaryDirectory() as directory:
            acquire_github_commit(
                "https://github.com/example/public-app",
                COMMIT,
                Path(directory) / "source",
                timeout_seconds=1,
                deadline=time.monotonic() + 1,
                command_runner=runner,
            )
        self.assertGreater(len(timeouts), 1)
        self.assertLess(timeouts[-1], timeouts[0])

    def test_gitmodules_are_rejected_and_redirects_are_disabled(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(argv: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
            calls.append(tuple(argv))
            root = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path(argv[-1])
            if "init" in argv:
                (root / ".git").mkdir()
                (root / ".gitmodules").write_text("malicious submodule control\n")
            if "ls-files" in argv:
                return SimpleNamespace(stdout=b"")
            return SimpleNamespace(stdout=(COMMIT + "\n").encode())

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            with self.assertRaisesRegex(ValidationError, "gitmodules"):
                acquire_github_commit(
                    "https://github.com/example/public-app",
                    COMMIT,
                    destination,
                    command_runner=runner,
                )
            self.assertFalse(destination.exists())
        fetch = next(call for call in calls if "fetch" in call)
        joined = " ".join(fetch)
        self.assertIn("http.followRedirects=false", joined)
        self.assertIn("filter.lfs.smudge=", joined)
        self.assertIn("filter.lfs.process=", joined)

    def test_repository_dockerfiles_may_coexist_as_inert_source_files(self) -> None:
        for malicious_name in (
            "Dockerfile",
            "nested/Dockerfile",
            "containers/Dockerfile.release",
        ):
            with self.subTest(name=malicious_name), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "source"

                def runner(
                    argv: tuple[str, ...],
                    selected: str = malicious_name,
                    **_kwargs: object,
                ) -> SimpleNamespace:
                    root = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path(argv[-1])
                    if "init" in argv:
                        (root / ".git").mkdir()
                        (root / "bun.lock").write_text("")
                        path = root / selected
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("RUN malicious-build-instruction\n")
                    if "ls-files" in argv:
                        index = b"100644 " + b"1" * 40 + b" 0\t" + selected.encode() + b"\0"
                        return SimpleNamespace(stdout=index)
                    return SimpleNamespace(stdout=(COMMIT + "\n").encode())

                source = acquire_github_commit(
                    "https://github.com/example/public-app",
                    COMMIT,
                    destination,
                    command_runner=runner,
                )
                self.assertEqual(
                    (source / malicious_name).read_text(),
                    "RUN malicious-build-instruction\n",
                )
                recipe = generate_recipe(
                    Manifest("bun", (".",), "build", "start", 3000, "/health"),
                    RuntimeImages(bun=BUN_IMAGE, node=NODE_IMAGE),
                ).dockerfile
                self.assertNotIn(b"malicious-build-instruction", recipe)
                self.assertFalse((source / ".git").exists())

    def test_gitlink_index_mode_is_rejected_even_without_gitmodules(self) -> None:
        def runner(argv: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
            destination = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path(argv[-1])
            if "init" in argv:
                (destination / ".git").mkdir()
            if "ls-files" in argv:
                return SimpleNamespace(
                    stdout=(b"160000 " + b"1" * 40 + b" 0\tvendored-dependency\0")
                )
            return SimpleNamespace(stdout=(COMMIT + "\n").encode())

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            with self.assertRaisesRegex(ValidationError, "gitlinks"):
                acquire_github_commit(
                    "https://github.com/example/public-app",
                    COMMIT,
                    destination,
                    command_runner=runner,
                )
            self.assertFalse(destination.exists())

    def test_commit_mismatch_removes_partial_source(self) -> None:
        def runner(argv: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
            destination = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path(argv[-1])
            if "init" in argv:
                (destination / ".git").mkdir()
            return SimpleNamespace(stdout=(("c" * 40) + "\n").encode())

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            with self.assertRaises(ApplicationError):
                acquire_github_commit(
                    "https://github.com/example/public-app",
                    COMMIT,
                    destination,
                    command_runner=runner,
                )
            self.assertFalse(destination.exists())


class BuilderTests(unittest.TestCase):
    def test_ambiguous_metadata_is_rejected(self) -> None:
        with self.assertRaises(ApplicationError):
            parse_build_metadata(
                json.dumps(
                    {
                        "containerimage.digest": f"sha256:{DIGEST}",
                        "other": f"sha256:{'d' * 64}",
                    }
                )
            )

        # BuildKit's image exporter always reports the reference it pushed, so
        # rejecting it would fail every otherwise successful build.
        self.assertEqual(
            parse_build_metadata(
                json.dumps(
                    {
                        "containerimage.digest": f"sha256:{DIGEST}",
                        "image.name": "registry.example:5000/apps/demo-app:build-0",
                    }
                )
            ),
            f"sha256:{DIGEST}",
        )


class ProviderCommandTests(unittest.TestCase):
    IMAGE_ID = "00000000-0000-4000-8000-000000000099"
    SERVER_ID = "00000000-0000-4000-8000-000000000088"
    PORT_ID = "00000000-0000-4000-8000-000000000077"

    @staticmethod
    def result(payload: object = b"") -> SimpleNamespace:
        stdout = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        return SimpleNamespace(
            stdout=stdout,
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    def test_builder_provider_uses_selected_uuid_and_configured_flavor_as_data(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        server_name = "example-builder-123456781234"

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            calls.append((tuple(argv), dict(kwargs)))
            if "show" not in argv:
                return self.result()
            return self.result(
                {
                    "buildId": APP_ID,
                    "server": {
                        "id": self.SERVER_ID,
                        "name": server_name,
                        "status": "ACTIVE",
                        "imageId": self.IMAGE_ID,
                        "flavorName": "builder-standard",
                        "managedBy": "platform",
                        "buildId": APP_ID,
                    },
                    "port": {
                        "id": self.PORT_ID,
                        "name": f"{server_name}-v4",
                        "deviceId": self.SERVER_ID,
                        "address": "192.0.2.40",
                        "description": f"managed-by=platform;build-id={APP_ID}",
                    },
                    "ready": True,
                }
            )

        observed = create_builder(
            APP_ID,
            prefix="example",
            selected_image_id=self.IMAGE_ID,
            flavor_name="builder-standard",
            timeout_seconds=30,
            command_runner=runner,
            builder_command=("fixed-builder",),
        )
        self.assertTrue(observed.ready)
        self.assertEqual(calls[0][0], ("fixed-builder", "create", APP_ID))
        self.assertEqual(
            calls[0][1]["env"],
            {"IMAGE_NAME": self.IMAGE_ID, "FLAVOR_NAME": "builder-standard"},
        )
        self.assertNotIn(self.IMAGE_ID, " ".join(calls[0][0]))

    def test_worker_provider_uses_selected_uuid_and_only_standard_flavor(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        server_name = "example-worker-123456781234"

        created = False

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            nonlocal created
            calls.append((tuple(argv), dict(kwargs)))
            if "create" in argv:
                created = True
                return self.result()
            if not created:
                return self.result(
                    {
                        "applicationId": APP_ID,
                        "slug": "demo-app",
                        "server": None,
                        "port": None,
                        "ready": False,
                    }
                )
            return self.result(
                {
                    "applicationId": APP_ID,
                    "slug": "demo-app",
                    "server": {
                        "id": self.SERVER_ID,
                        "name": server_name,
                        "status": "ACTIVE",
                        "imageId": self.IMAGE_ID,
                        "flavorName": "worker-standard",
                        "managedBy": "platform",
                        "applicationId": APP_ID,
                        "applicationSlug": "demo-app",
                    },
                    "port": {
                        "id": self.PORT_ID,
                        "name": f"{server_name}-v4",
                        "deviceId": self.SERVER_ID,
                        "address": "192.0.2.41",
                        "description": (
                            f"managed-by=platform;application-id={APP_ID};application-slug=demo-app"
                        ),
                    },
                    "ready": True,
                }
            )

        observed = create_worker(
            APP_ID,
            "demo-app",
            prefix="example",
            selected_image_id=self.IMAGE_ID,
            standard_flavor="worker-standard",
            nomad_command="fixed-nomad-wrapper",
            timeout_seconds=30,
            command_runner=runner,
            worker_command=("fixed-worker",),
        )
        self.assertTrue(observed.ready)
        self.assertEqual(calls[1][0], ("fixed-worker", "create", APP_ID, "demo-app"))
        self.assertEqual(
            calls[1][1]["env"],
            {
                "IMAGE_NAME": self.IMAGE_ID,
                "FLAVOR_NAME": "worker-standard",
                "NOMAD": "fixed-nomad-wrapper",
            },
        )

    def test_worker_delete_expands_only_the_two_bounded_deployment_slots(self) -> None:
        platform = SimpleNamespace(prefix="example", project_name="project", project_id=APP_ID)
        observed: list[str] = []

        def delete_worker(identity: str, slug: str, **_kwargs: object):
            observed.append(identity)
            return app_module.WorkerObservation(
                identity,
                slug,
                None,
                "absent",
                None,
                "absent-v4",
                None,
                None,
                False,
            )

        with (
            mock.patch.object(production, "helper_runtime", return_value=SimpleNamespace(platform=platform)),
            mock.patch.object(production.application, "provider_command", return_value=("worker",)),
            mock.patch.object(production.application, "delete_worker", side_effect=delete_worker),
        ):
            result = production._provider_app(
                "app.worker.delete", {"applicationId": APP_ID, "slug": "demo-app"}
            )
        self.assertTrue(result["absent"])
        self.assertEqual(observed, [APP_ID, *app_module.deployment_worker_ids(APP_ID)])
        observed.clear()
        with (
            mock.patch.object(production, "helper_runtime", return_value=SimpleNamespace(platform=platform)),
            mock.patch.object(production.application, "provider_command", return_value=("worker",)),
            mock.patch.object(production.application, "delete_worker", side_effect=delete_worker),
        ):
            production._provider_app(
                "app.worker.delete",
                {"applicationId": APP_ID, "slug": "demo-app", "single": True},
            )
        self.assertEqual(observed, [APP_ID])

    def test_existing_worker_uuid_and_image_are_authoritative_over_new_selection(self) -> None:
        server_name = "example-worker-123456781234"
        old_image = "00000000-0000-4000-8000-000000000066"
        payload = {
            "applicationId": APP_ID,
            "slug": "demo-app",
            "server": {
                "id": self.SERVER_ID,
                "name": server_name,
                "status": "ACTIVE",
                "imageId": old_image,
                "flavorName": "older-worker-flavor",
                "managedBy": "platform",
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
            },
            "port": {
                "id": self.PORT_ID,
                "name": f"{server_name}-v4",
                "deviceId": self.SERVER_ID,
                "address": "192.0.2.41",
                "description": (
                    f"managed-by=platform;application-id={APP_ID};application-slug=demo-app"
                ),
            },
            "ready": True,
        }
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            calls.append((argv, dict(kwargs)))
            return self.result(payload if "show" in argv else b"")

        observed = create_worker(
            APP_ID,
            "demo-app",
            prefix="example",
            selected_image_id=None,
            standard_flavor=None,
            nomad_command="fixed-nomad-wrapper",
            timeout_seconds=30,
            command_runner=runner,
            worker_command=("fixed-worker",),
        )
        self.assertEqual(observed.server_id, self.SERVER_ID)
        self.assertEqual(observed.image_id, old_image)
        create_call = next(call for call in calls if "create" in call[0])
        self.assertEqual(create_call[1]["env"], {"NOMAD": "fixed-nomad-wrapper"})

    def test_observations_reject_name_collisions_with_wrong_metadata_or_attachment(self) -> None:
        builder = {
            "buildId": APP_ID,
            "server": {
                "id": self.SERVER_ID,
                "name": "example-builder-123456781234",
                "status": "ACTIVE",
                "imageId": self.IMAGE_ID,
                "flavorName": "builder-standard",
                "managedBy": "someone-else",
                "buildId": APP_ID,
            },
            "port": {
                "id": self.PORT_ID,
                "name": "example-builder-123456781234-v4",
                "deviceId": self.SERVER_ID,
                "address": "192.0.2.40",
                "description": f"managed-by=platform;build-id={APP_ID}",
            },
            "ready": True,
        }
        with self.assertRaisesRegex(ApplicationError, "identity"):
            parse_builder_observation(json.dumps(builder), build_id=APP_ID, prefix="example")
        worker = {
            "applicationId": APP_ID,
            "slug": "demo-app",
            "server": {
                "id": self.SERVER_ID,
                "name": "example-worker-123456781234",
                "status": "ACTIVE",
                "imageId": self.IMAGE_ID,
                "flavorName": "worker-standard",
                "managedBy": "platform",
                "applicationId": APP_ID,
                "applicationSlug": "demo-app",
            },
            "port": {
                "id": self.PORT_ID,
                "name": "example-worker-123456781234-v4",
                "deviceId": "00000000-0000-4000-8000-000000000055",
                "address": "192.0.2.41",
                "description": (
                    f"managed-by=platform;application-id={APP_ID};application-slug=demo-app"
                ),
            },
            "ready": True,
        }
        with self.assertRaisesRegex(ApplicationError, "attached"):
            parse_worker_observation(
                json.dumps(worker),
                application_id=APP_ID,
                application_slug="demo-app",
                prefix="example",
            )

    def test_build_archive_is_deterministic_bounded_and_separates_recipe(self) -> None:
        generated = b"FROM pinned\nENV NODE_ENV=production\n"
        malicious = b"FROM attacker.example/image\nRUN steal-build-credentials\n"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "package-lock.json").write_text("{}")
            (source / "index.js").write_text("console.log('safe')\n")
            (source / "Dockerfile").write_bytes(malicious)
            first = create_build_archive(source, Recipe(generated, "0" * 64))
            second = create_build_archive(source, Recipe(generated, "0" * 64))
        self.assertEqual(first, second)
        with tarfile.open(fileobj=BytesIO(first[0]), mode="r:") as archive:
            names = set(archive.getnames())
            source_dockerfile = archive.extractfile("source/Dockerfile")
            selected_recipe = archive.extractfile("recipe/Dockerfile")
            assert source_dockerfile is not None
            assert selected_recipe is not None
            self.assertEqual(source_dockerfile.read(), malicious)
            self.assertEqual(selected_recipe.read(), generated)
        self.assertIn("source/index.js", names)
        self.assertIn("source/Dockerfile", names)
        self.assertIn("recipe/Dockerfile", names)
        self.assertEqual(
            [name for name in names if name.startswith("recipe/")],
            ["recipe/Dockerfile"],
        )
        self.assertEqual(first[1], app_module.hashlib.sha256(first[0]).hexdigest())

    def test_archive_transfer_and_rootless_build_use_fixed_remote_argv(self) -> None:
        observation = BuilderObservation(
            APP_ID,
            self.SERVER_ID,
            "example-builder-123456781234",
            self.PORT_ID,
            "example-builder-123456781234-v4",
            "192.0.2.40",
            self.IMAGE_ID,
            "builder-standard",
            True,
        )
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            calls.append((tuple(argv), dict(kwargs)))
            time.sleep(0.01)
            if "receive" in argv:
                digest = argv[-2]
                return self.result({"buildId": APP_ID, "sha256": digest})
            return SimpleNamespace(
                stdout=json.dumps({"containerimage.digest": f"sha256:{DIGEST}"}).encode(),
                stderr=b"bounded build output\n",
                stdout_truncated=False,
                stderr_truncated=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package-lock.json").write_text("{}")
            known_hosts = root / "known_hosts"
            known_hosts.write_text("192.0.2.40 ssh-ed25519 AAAA\n")
            known_hosts.chmod(0o600)
            identity = root / "builder_operator_ed25519"
            identity.write_text("PRIVATE KEY\n")
            identity.chmod(0o600)
            execution = execute_builder_build(
                observation,
                root,
                Recipe(b"FROM pinned\n", "0" * 64),
                "registry.example/projects/demo-app/app",
                known_hosts,
                identity,
                source_limit=1_048_576,
                build_log_limit=4096,
                timeout_seconds=30,
                connect_timeout_seconds=5,
                deadline=time.monotonic() + 2,
                command_runner=runner,
                ssh_command=("fixed-ssh",),
            )
        self.assertEqual(execution.log, b"bounded build output\n")
        receive_argv, receive_kwargs = calls[0]
        self.assertEqual(receive_argv[0], "fixed-ssh")
        self.assertIn("StrictHostKeyChecking=yes", receive_argv)
        # Both hops must offer the configured key rather than whichever default
        # identity happens to exist in the admin's home directory.
        for argv in (receive_argv, calls[1][0]):
            self.assertIn("IdentitiesOnly=yes", argv)
            self.assertEqual(argv[argv.index("-i") + 1], str(identity))
        self.assertEqual(receive_argv[-4], "receive")
        self.assertIsInstance(receive_kwargs["stdin"], bytes)
        self.assertLess(
            float(calls[1][1]["timeout_seconds"]), float(receive_kwargs["timeout_seconds"])
        )
        self.assertTrue(any("T" in value and value.endswith("Z") for value in receive_argv))
        build_argv = calls[1][0]
        self.assertEqual(
            build_argv[-3:],
            ("build", APP_ID, "registry.example/projects/demo-app/app"),
        )
        self.assertNotIn("FROM pinned", " ".join((*receive_argv, *build_argv)))

    def test_interruption_still_attempts_provider_server_and_port_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(app_module, "create_builder", side_effect=KeyboardInterrupt),
                mock.patch.object(app_module, "delete_builder") as deleted,
                self.assertRaises(KeyboardInterrupt),
            ):
                build_with_disposable_builder(
                    build_id=APP_ID,
                    source_directory=directory,
                    recipe=Recipe(b"FROM pinned\n", "0" * 64),
                    image_name="registry.example/projects/demo-app/app",
                    prefix="example",
                    selected_builder_image_id=self.IMAGE_ID,
                    builder_flavor="builder-standard",
                    known_hosts_directory=Path(directory) / "known-hosts",
                    identity_path=Path(directory) / "builder_operator_ed25519",
                )
        deleted.assert_called_once()

    def test_registry_retention_never_deletes_current_accepted_or_rollback_references(self) -> None:
        images = [
            f"registry.example/projects/demo-app/app@sha256:{character * 64}"
            for character in "1234567"
        ]
        calls: list[tuple[str, ...]] = []

        def runner(argv: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            digest = argv[-1]
            return self.result(
                {"repository": "projects/demo-app/app", "digest": digest, "absent": True}
            )

        retained_old_reference = images[-1]
        result = apply_registry_retention(
            "demo-app",
            images,
            referenced_images=(images[0], retained_old_reference),
            prior_successful_to_keep=2,
            timeout_seconds=30,
            command_runner=runner,
            registry_command=("fixed-registry",),
        )
        self.assertEqual(result.deleted, tuple(images[3:6]))
        deleted_digests = {argv[-1] for argv in calls}
        self.assertNotIn(images[0].rsplit("@", 1)[-1], deleted_digests)
        self.assertNotIn(retained_old_reference.rsplit("@", 1)[-1], deleted_digests)

    def test_every_recorded_phase_has_an_explicit_recovery_action(self) -> None:
        expected = {
            "validated": "acquire_source",
            "source_acquired": "acquire_source",
            "builder_created": "cleanup_builder_then_rebuild",
            "image_pushed": "cleanup_builder_then_reconcile_worker",
            "builder_cleaned": "reconcile_worker",
            "worker_ready": "submit_job",
            "job_submitted": "observe_health_or_remove_candidate",
            "deployment_healthy": "accept_deployment",
            "accepted": "complete",
        }
        for phase, action in expected.items():
            with self.subTest(phase=phase):
                candidate = (
                    f"registry.example/projects/demo-app/app@sha256:{DIGEST}"
                    if phase == "image_pushed"
                    else None
                )
                self.assertEqual(
                    deployment_recovery_action(phase, candidate_digest=candidate), action
                )


class WorkerPrimitiveTests(unittest.TestCase):
    def test_worker_and_job_primitives_have_no_class_profile_state(self) -> None:
        paths = (
            ROOT / "infra" / "openstack" / "worker_lifecycle.sh",
            ROOT / "infra" / "cloud-init-nixos" / "worker.yaml",
            ROOT / "openstack_platform" / "controller" / "application_runtime.py",
            ROOT / "openstack_platform" / "contracts.py",
            ROOT / "infra" / "lib" / "platform_contract.json",
        )
        combined = "\n".join(path.read_text().lower() for path in paths)
        for retired in ("project_class", "personal", "team"):
            self.assertNotIn(retired, combined)
        self.assertIn("application_id", combined)
        self.assertIn("platform_candidate_job_sha256", combined)

    def test_lifecycle_mutations_use_resolved_uuids_and_repeat_project_identity_checks(
        self,
    ) -> None:
        for name in ("worker_lifecycle.sh", "builder_lifecycle.sh"):
            text = (ROOT / "infra" / "openstack" / name).read_text()
            self.assertIn("EXPECTED_PROJECT_NAME", text)
            self.assertIn("EXPECTED_PROJECT_ID", text)
            self.assertIn("token_project_id", text)
            self.assertIn("resolve_named_id", text)
            self.assertIn('server delete --wait "$server_id"', text)
            self.assertIn('port delete "$port_id"', text)
            self.assertNotIn('server delete --wait "$server_name"', text)
            self.assertNotIn('port delete "$port_name"', text)
        worker = (ROOT / "infra" / "openstack" / "worker_lifecycle.sh").read_text()
        self.assertIn('flavor show "$flavor"', worker)
        self.assertIn("vcpus != 1", worker)


class DeploymentTests(unittest.TestCase):
    def platform(self) -> PlatformConfig:
        return PlatformConfig(
            project_name="project",
            project_id=APP_ID,
            prefix="example",
            namespace="app-platform",
            domain="apps.example.com",
            datacenter="dc1",
            region="global",
            network="private",
            document={},
        )

    def candidate_job(self) -> tuple[str, tuple[str, str]]:
        job = render_nomad_job(
            application_id=APP_ID,
            application_slug="demo-app",
            image=f"registry.example/apps/demo-app@sha256:{DIGEST}",
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=self.platform(),
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
        )
        candidate = nomad_candidate_identity(job)
        assert candidate is not None
        return job, candidate

    @staticmethod
    def health(
        version: int,
        candidate: tuple[str, str],
        *,
        healthy: bool,
        terminal: bool,
    ) -> dict[str, object]:
        return {
            "version": version,
            "currentVersion": version,
            "candidateJobSha256": candidate[0],
            "candidateImage": candidate[1],
            "allocations": 1,
            "healthy": healthy,
            "terminal": terminal,
        }

    def test_job_maps_named_storage_without_optional_template_functions(self) -> None:
        manifest = Manifest(
            "node",
            (".",),
            None,
            "start",
            3000,
            "/health",
            (StorageBinding("default", "mongo", (("uri", "MONGODB_URI"),)),),
        )
        job = render_nomad_job(
            application_id=APP_ID,
            application_slug="demo-app",
            image=f"registry.example/apps/demo-app@sha256:{DIGEST}",
            manifest=manifest,
            platform=self.platform(),
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
        )
        self.assertIn('(ne $key "STORAGE__MONGO__DEFAULT__URI")', job)
        self.assertIn('{{ $value := index . "STORAGE__MONGO__DEFAULT__URI" }}', job)
        self.assertIn("MONGODB_URI={{ $value | toJSON }}", job)
        self.assertNotIn("hasPrefix", job)
        self.assertNotIn(r"\n{{ end }}", job)

    def test_job_has_only_application_placement_and_explicit_standard_resources(self) -> None:
        job = render_nomad_job(
            application_id=APP_ID,
            application_slug="demo-app",
            image=f"registry.example/apps/demo-app@sha256:{DIGEST}",
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=self.platform(),
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
        )
        self.assertIn("${meta.application_id}", job)
        self.assertIn("cpu    = 1000", job)
        self.assertIn("memory = 2048", job)
        self.assertIn("readonly_rootfs = true", job)
        self.assertIn('source    = "app-platform-internal-ca"', job)
        self.assertIn('destination = "/platform-ca"', job)
        self.assertIn("read_only   = true", job)
        self.assertNotIn("personal", job)
        self.assertNotIn("team", job)
        self.assertNotIn("project_class", job)
        candidate = nomad_candidate_identity(job)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate[1], f"registry.example/apps/demo-app@sha256:{DIGEST}")
        with self.assertRaisesRegex(ValidationError, "exact job"):
            nomad_candidate_identity(job.replace("memory = 2048", "memory = 2047"))

        candidate_job = render_nomad_job(
            application_id=APP_ID,
            application_slug="demo-app",
            image=f"registry.example/apps/demo-app@sha256:{DIGEST}",
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=self.platform(),
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
            candidate=True,
            placement_id="00000000-0000-4000-8000-000000000099",
            staged=True,
            route_marker="00000000-0000-4000-8000-000000000099",
        )
        self.assertIn('job "demo-app-candidate"', candidate_job)
        self.assertIn('value     = "00000000-0000-4000-8000-000000000099"', candidate_job)
        self.assertIn("Host(`demo-app-preview.apps.example.com`)", candidate_job)
        self.assertNotIn("Host(`demo-app.apps.example.com`)", candidate_job)
        marker = "00000000-0000-4000-8000-000000000099"
        self.assertIn(f"X-Platform-Deployment={marker}", candidate_job)
        promoted = render_nomad_job(
            application_id=APP_ID,
            application_slug="demo-app",
            image=f"registry.example/apps/demo-app@sha256:{DIGEST}",
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=self.platform(),
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
            candidate=True,
            placement_id=marker,
            staged=True,
            promoted=True,
            route_marker=marker,
            route_priority=200,
        )
        self.assertIn("Host(`demo-app-preview.apps.example.com`)", promoted)
        self.assertIn("Host(`demo-app.apps.example.com`)", promoted)
        self.assertIn("priority=200", promoted)

        for observed, expected in (({}, False), ({"x-platform-deployment": marker}, True)):
            with self.subTest(headers=observed):
                self.assertEqual(
                    app_module.check_public_health(
                        "demo-app",
                        self.platform(),
                        "/health",
                        timeout_seconds=5,
                        expected_marker=marker,
                        http_caller=lambda *_args, **_kwargs: HttpResult(200, observed, b"OK"),
                    ),
                    expected,
                )

    def test_interrupted_submission_accepts_only_exact_candidate_evidence(self) -> None:
        job = render_nomad_job(
            application_id=APP_ID,
            application_slug="demo-app",
            image=f"registry.example/apps/demo-app@sha256:{DIGEST}",
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=self.platform(),
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
        )
        candidate = nomad_candidate_identity(job)
        assert candidate is not None
        calls: list[str] = []

        def helper(action: str, _args: object, **_kwargs: object) -> dict[str, object]:
            calls.append(action)
            if action == "app.deploy":
                return {
                    "jobId": "demo-app",
                    "nomadVersion": 4,
                    "candidateJobSha256": candidate[0],
                    "candidateImage": candidate[1],
                    "submitted": False,
                }
            return self.health(4, candidate, healthy=True, terminal=False)

        result = deploy_and_cleanup(
            "demo-app",
            job,
            helper_caller=helper,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result.nomad_version, 4)
        self.assertEqual(calls, ["app.deploy", "app.health"])

        def wrong_helper(action: str, _args: object, **_kwargs: object) -> dict[str, object]:
            if action == "app.deploy":
                return {
                    "jobId": "demo-app",
                    "nomadVersion": 4,
                    "candidateJobSha256": "0" * 64,
                    "candidateImage": candidate[1],
                }
            raise AssertionError(action)

        with self.assertRaisesRegex(ApplicationError, "exact deployment candidate"):
            deploy_and_cleanup(
                "demo-app",
                job,
                helper_caller=wrong_helper,
            )

    def test_healthy_deploy_returns_version_without_candidate_cleanup(self) -> None:
        calls: list[str] = []
        job, candidate = self.candidate_job()

        def helper(action: str, args: object, **_kwargs: object) -> dict[str, object]:
            calls.append(action)
            if action == "app.deploy":
                return {
                    "jobId": "demo-app",
                    "nomadVersion": 4,
                    "candidateJobSha256": candidate[0],
                    "candidateImage": candidate[1],
                }
            return self.health(4, candidate, healthy=True, terminal=False)

        result = deploy_and_cleanup(
            "demo-app",
            job,
            helper_caller=helper,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result.nomad_version, 4)
        self.assertEqual(calls, ["app.deploy", "app.health"])

    def test_pending_zero_allocation_observation_is_polled_without_cleanup(self) -> None:
        job, candidate = self.candidate_job()
        observations = 0
        calls: list[str] = []

        def helper(action: str, _args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal observations
            calls.append(action)
            if action == "app.deploy":
                return {
                    "jobId": "demo-app",
                    "nomadVersion": 4,
                    "candidateJobSha256": candidate[0],
                    "candidateImage": candidate[1],
                }
            observations += 1
            if observations == 1:
                return self.health(4, candidate, healthy=False, terminal=False) | {"allocations": 0}
            return self.health(4, candidate, healthy=True, terminal=False)

        result = deploy_and_cleanup(
            "demo-app",
            job,
            attempts=2,
            helper_caller=helper,
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result.observations, 2)
        self.assertEqual(calls, ["app.deploy", "app.health", "app.health"])

    def test_public_health_must_pass_after_scheduler_health(self) -> None:
        public = iter((False, True))
        job, candidate = self.candidate_job()

        def helper(action: str, _args: object, **_kwargs: object) -> dict[str, object]:
            if action == "app.deploy":
                return {
                    "jobId": "demo-app",
                    "nomadVersion": 4,
                    "candidateJobSha256": candidate[0],
                    "candidateImage": candidate[1],
                }
            return self.health(4, candidate, healthy=True, terminal=False)

        result = deploy_and_cleanup(
            "demo-app",
            job,
            attempts=2,
            helper_caller=helper,
            public_health_check=lambda: next(public),
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result.observations, 2)

    def test_promoted_route_identity_failure_removes_only_promoted_slot(self) -> None:
        marker = "00000000-0000-4000-8000-000000000099"
        job = render_nomad_job(
            application_id=APP_ID,
            application_slug="demo-app",
            image=f"registry.example/apps/demo-app@sha256:{DIGEST}",
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=self.platform(),
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
            candidate=True,
            placement_id=marker,
            staged=True,
            promoted=True,
            route_marker=marker,
            route_priority=200,
        )
        identity = nomad_candidate_identity(job)
        calls: list[tuple[str, object]] = []

        def helper(action: str, args: object, **_kwargs: object) -> dict[str, object]:
            calls.append((action, args))
            if action == "app.deploy":
                return {
                    "jobId": "demo-app-candidate",
                    "nomadVersion": 9,
                    "candidateJobSha256": identity[0],
                    "candidateImage": identity[1],
                }
            if action == "app.health":
                return self.health(9, identity, healthy=True, terminal=False)
            return {"jobAbsent": True}

        with self.assertRaises(DeploymentFailed):
            deploy_and_cleanup(
                "demo-app",
                job,
                attempts=1,
                helper_caller=helper,
                public_health_check=lambda: False,
            )
        self.assertIn(
            (
                "app.remove",
                {
                    "slug": "demo-app",
                    "jobId": "demo-app-candidate",
                    "candidateJobSha256": identity[0],
                    "candidateImage": identity[1],
                },
            ),
            calls,
        )

    def test_terminal_deploy_removes_only_the_current_candidate(self) -> None:
        calls: list[tuple[str, object]] = []
        job, candidate = self.candidate_job()

        def helper(action: str, args: object, **_kwargs: object) -> dict[str, object]:
            calls.append((action, args))
            if action == "app.deploy":
                return {
                    "jobId": "demo-app",
                    "nomadVersion": 4,
                    "candidateJobSha256": candidate[0],
                    "candidateImage": candidate[1],
                }
            if action == "app.health":
                return self.health(4, candidate, healthy=False, terminal=True)
            return {"jobAbsent": True}

        with self.assertRaises(DeploymentFailed) as raised:
            deploy_and_cleanup("demo-app", job, helper_caller=helper, sleep=lambda _seconds: None)
        self.assertTrue(raised.exception.cleanup_succeeded)
        self.assertIn(
            (
                "app.remove",
                {
                    "slug": "demo-app",
                    "jobId": "demo-app",
                    "candidateJobSha256": candidate[0],
                    "candidateImage": candidate[1],
                },
            ),
            calls,
        )
        self.assertEqual(raised.exception.cleanup_evidence["action"], "remove-candidate")

    def test_shared_acceptance_reobserves_exact_job_and_public_route_before_database_write(
        self,
    ) -> None:
        job, candidate = self.candidate_job()
        acceptance = DeploymentAcceptance(
            application_id=APP_ID,
            application_slug="demo-app",
            repository="https://github.com/example/demo-app",
            deployment_id="00000000-0000-4000-8000-000000000099",
            worker_server_id="00000000-0000-4000-8000-000000000004",
            worker_server_name="example-worker-demo",
            worker_port_id="00000000-0000-4000-8000-000000000005",
            worker_port_name="example-worker-demo-v4",
            worker_flavor="one-vcpu",
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
            image=candidate[1],
            nomad_job=job,
            nomad_job_sha256=candidate[0],
            nomad_version=4,
            health_path="/health",
            application_port=3000,
            build_log_path="logs/demo.log",
            public_url="https://demo-app.apps.example.com",
        )

        def helper(_action: str, args: object, **_kwargs: object) -> dict[str, object]:
            return self.health(4, candidate, healthy=True, terminal=False) | {"requested": args}

        with tempfile.TemporaryDirectory() as directory:
            connection = db.connect(Path(directory) / "state.sqlite3")
            db.migrate(connection)
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
            configuration = parse_configuration(
                {
                    "schemaVersion": 1,
                    "build": {
                        "runtime": "bun",
                        "packages": ["."],
                        "buildScript": "build",
                        "startScript": "start",
                    },
                    "runtime": {"port": 3000, "healthPath": "/health"},
                    "storageBindings": [],
                }
            )
            db.claim_idempotency_request(
                connection,
                request_id=acceptance.deployment_id,
                request_fingerprint="d" * 64,
            )
            db.create_deployment_attempt(
                connection,
                deployment_id=acceptance.deployment_id,
                application_id=APP_ID,
                source_commit=COMMIT,
                requested_ref="main",
                configuration_revision=1,
                configuration=configuration,
                environment_revision=0,
                idempotency_request_id=acceptance.deployment_id,
            )
            with self.assertRaisesRegex(ApplicationError, "public deployment route"):
                accept_healthy_deployment(
                    connection,
                    acceptance,
                    helper_timeout_seconds=30,
                    helper_caller=helper,
                    public_health_check=lambda: False,
                )
            self.assertIsNone(db.get_deployment(connection, APP_ID))
            accept_healthy_deployment(
                connection,
                acceptance,
                helper_timeout_seconds=30,
                helper_caller=helper,
                public_health_check=lambda: True,
            )
            persisted = db.get_deployment(connection, APP_ID)
            connection.close()
        assert persisted is not None
        self.assertEqual(persisted.nomad_job_sha256, candidate[0])
        self.assertEqual(persisted.health_path, "/health")

    def test_environment_value_is_only_protocol_data_and_never_an_argv(self) -> None:
        sentinel = "secret value with spaces"
        observed: dict[str, object] = {}

        def helper(action: str, args: object, **_kwargs: object) -> dict[str, object]:
            observed.update({"action": action, "args": args})
            return {"keys": ["API_KEY"], "modifyIndex": 2}

        result = set_environment(
            "demo-app",
            {"API_KEY": sentinel},
            {},
            timeout_seconds=10,
            helper_caller=helper,
        )
        self.assertEqual(result["keys"], ["API_KEY"])
        self.assertEqual(observed["action"], "app.env.set")
        self.assertEqual(
            observed["args"],
            {"slug": "demo-app", "updates": {"API_KEY": sentinel}, "ownership": {}},
        )


if __name__ == "__main__":
    unittest.main()
