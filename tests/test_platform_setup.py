from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openstack_platform import operator, setup


class SetupEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self, content: str) -> Path:
        path = self.root / "setup.env"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_literal_openrc_assignments_are_read_without_shell_execution(self) -> None:
        path = self.environment(
            """\
#!/bin/bash
export OS_AUTH_URL=https://identity.example/v3
export OS_PROJECT_NAME="demo project"
read -sr OS_PASSWORD_INPUT
export OS_PASSWORD=$OS_PASSWORD_INPUT
PLATFORM_DOMAIN='apps.example.test'
"""
        )

        values = setup.load_environment_file(path)

        self.assertEqual(values["OS_AUTH_URL"], "https://identity.example/v3")
        self.assertEqual(values["OS_PROJECT_NAME"], "demo project")
        self.assertEqual(values["PLATFORM_DOMAIN"], "apps.example.test")
        self.assertNotIn("OS_PASSWORD", values)

    def test_environment_file_must_be_private(self) -> None:
        path = self.environment("OS_PROJECT_NAME=demo\n")
        path.chmod(0o644)
        with self.assertRaisesRegex(setup.SetupError, "mode-0600"):
            setup.load_environment_file(path)

    def test_duplicate_assignment_is_rejected(self) -> None:
        path = self.environment("OS_PROJECT_NAME=one\nOS_PROJECT_NAME=two\n")
        with self.assertRaisesRegex(setup.SetupError, "duplicate"):
            setup.load_environment_file(path)

    def test_project_identity_rejects_a_conflicting_configured_uuid(self) -> None:
        token_project = "00000000-0000-4000-8000-000000000001"
        environment = {
            "OS_PROJECT_NAME": "demo",
            "OS_PROJECT_ID": "00000000-0000-4000-8000-000000000002",
        }
        with (
            mock.patch.object(setup, "_command", return_value=token_project),
            self.assertRaisesRegex(setup.SetupError, "does not match"),
        ):
            setup._project_identity(Path("/nix/store/openstack"), environment)

    def test_apply_rejects_missing_release_evidence_before_workspace_mutation(self) -> None:
        path = self.environment(
            "OS_AUTH_URL=https://identity.example/v3\n"
            "OS_PROJECT_NAME=demo\n"
            "OS_USERNAME=operator\n"
            "OS_PASSWORD=secret\n"
            f"PLATFORM_SOURCE_COMMIT={'a' * 40}\n"
        )
        with (
            mock.patch.object(setup, "_repository_root", return_value=Path(__file__).parents[1]),
            mock.patch.object(setup, "_source_commit", return_value="a" * 40) as source_commit,
            mock.patch.object(setup, "_private_directory") as private_directory,
            self.assertRaisesRegex(setup.SetupError, "PLATFORM_RELEASE_MANIFEST"),
        ):
            setup.run_setup(
                env_file=path,
                workspace=self.root / "workspace",
                cloudflare_token=None,
                apply=True,
                output=io.StringIO(),
            )
        private_directory.assert_not_called()
        self.assertEqual(source_commit.call_args.kwargs["supplied"], "a" * 40)
        self.assertFalse((self.root / "workspace").exists())


class SetupPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / "setup.env"
        self.component_manifest = self.root / "release-manifest.json"
        self.artifact_manifest = self.root / "role-artifacts.json"
        self.component_manifest.write_text("{}")
        self.artifact_manifest.write_text("{}")
        self.env_file.write_text(
            "OS_AUTH_URL=https://identity.test/v3\n"
            "OS_PROJECT_NAME=demo\nOS_USERNAME=user\nOS_PASSWORD=secret\n"
            f"PLATFORM_RELEASE_MANIFEST={self.component_manifest}\n"
            f"PLATFORM_ARTIFACT_MANIFEST={self.artifact_manifest}\n",
            encoding="utf-8",
        )
        self.env_file.chmod(0o600)
        self.project_id = "00000000-0000-4000-8000-000000000001"
        self.network_id = "00000000-0000-4000-8000-000000000002"
        repository = Path(__file__).resolve().parents[1]
        values = {
            "PLATFORM_PREFIX": "demo",
            "PLATFORM_NAMESPACE": "demo-platform",
            "PLATFORM_DISPLAY_NAME": "Demo",
            "PLATFORM_ORGANIZATION": "Demo",
            "PLATFORM_DOMAIN": "apps.example.test",
            "PLATFORM_NETWORK": "public",
            "PLATFORM_OPERATOR_CIDR": "192.0.2.10/32",
            "PLATFORM_ADMIN_ADDRESS": "192.0.2.11",
            "PLATFORM_INGRESS_ADDRESS": "192.0.2.12",
            "PLATFORM_STORAGE_ADDRESS": "192.0.2.13",
            "PLATFORM_ADMIN_FLAVOR": "admin-flavor",
            "PLATFORM_INGRESS_FLAVOR": "ingress-flavor",
            "PLATFORM_STORAGE_FLAVOR": "storage-flavor",
            "PLATFORM_WORKER_FLAVOR": "worker-flavor",
            "PLATFORM_BUILDER_FLAVOR": "builder-flavor",
            "PLATFORM_VOLUME_TYPE": "production",
        }
        with (
            mock.patch.object(setup, "_network_default"),
            mock.patch.object(setup, "_flavor_inventory", return_value=[]),
            mock.patch.object(setup, "_volume_type_default"),
        ):
            document = setup._platform_document(
                repository,
                values,
                setup.ProjectIdentity(self.project_id, "demo"),
                "a" * 40,
                Path("/usr/bin/openstack"),
                {},
                lambda prompt: self.fail(prompt),
            )
        self.resolved = setup.ResolvedSetup(
            repository,
            values,
            {},
            "a" * 40,
            setup.ProjectIdentity(self.project_id, "demo"),
            document,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def verified_release() -> tuple[dict[str, object], dict[str, object]]:
        artifacts = {
            role: {
                "qcow2Sha256": "a" * 64,
                "qcow2SizeBytes": (index + 1) * 1024**3,
                "nixClosureSha256": "b" * 64,
            }
            for index, role in enumerate(setup.IMAGE_ROLES)
        }
        return {"releaseChannel": "production"}, {"roleArtifacts": artifacts}

    def provider_json(self, argv: object, **_kwargs: object) -> object:
        command = tuple(str(item) for item in argv)  # type: ignore[arg-type]
        if command[1:3] == ("network", "list"):
            return [{"ID": self.network_id, "Name": "public"}]
        if command[1:3] == ("catalog", "show"):
            return {
                "name": "glance",
                "type": "image",
                "endpoints": [
                    {
                        "interface": "public",
                        "region": "RegionOne",
                        "url": "https://images.example.test/v2",
                    }
                ],
            }
        if command[1:3] == ("flavor", "list"):
            sizes = {
                "admin-flavor": (2, 4096),
                "ingress-flavor": (2, 2048),
                "storage-flavor": (4, 8192),
                "worker-flavor": (1, 4096),
                "builder-flavor": (4, 8192),
            }
            return [
                {
                    "ID": f"00000000-0000-4000-8000-{index:012d}",
                    "Name": name,
                    "VCPUs": cpu,
                    "RAM": ram,
                }
                for index, (name, (cpu, ram)) in enumerate(sizes.items(), 10)
            ]
        if command[1:4] == ("volume", "type", "list"):
            return [{"ID": "type-1", "Name": "production"}]
        if command[1:3] == ("subnet", "list"):
            return [{"ID": "subnet-1", "CIDR": "192.0.2.0/24"}]
        if command[1:3] == ("quota", "show"):
            return {
                name: {"in_use": 0, "limit": 1000000}
                for name in (
                    "instances",
                    "cores",
                    "ram",
                    "volumes",
                    "gigabytes",
                    "ports",
                    "security_groups",
                    "security_group_rules",
                    "key_pairs",
                )
            }
        if "list" in command:
            return []
        self.fail(f"unexpected provider command: {command}")

    def test_provider_check_uses_only_read_operations_and_never_generates_credentials(self) -> None:
        commands: list[tuple[str, ...]] = []

        def spy(argv: object, **kwargs: object) -> object:
            command = tuple(str(item) for item in argv)  # type: ignore[arg-type]
            commands.append(command)
            return self.provider_json(argv, **kwargs)

        def identity_read(argv: object, **_kwargs: object) -> str:
            command = tuple(str(item) for item in argv)  # type: ignore[arg-type]
            commands.append(command)
            if command[1:3] == ("token", "issue"):
                if "id" in command:
                    return "opaque-token"
                return self.project_id
            if command[1:3] == ("project", "show"):
                return "demo"
            if Path(command[0]).name == "openstack":
                self.fail(f"unexpected identity command: {command}")
            return json.dumps(
                {
                    "usage": {"image_count": 1, "image_size": 1024**3},
                    "limits": {"image_count": 100, "image_size": 100 * 1024**3},
                }
            )

        forbidden = (
            "_atomic_private_write",
            "_private_directory",
            "_ensure_key",
            "_ensure_secret_files",
            "_ensure_age_identity",
            "_build_nix_output",
            "_apply_foundation",
            "_build_and_publish_images",
            "_bootstrap_roles",
        )
        with (
            mock.patch.multiple(
                setup, **{name: mock.DEFAULT for name in forbidden}
            ) as mutation_spies,
            mock.patch.object(
                setup,
                "_local_toolchain",
                return_value={
                    "host": "x86_64-linux",
                    "requiredHost": "x86_64-linux",
                    "commands": {},
                    "missing": [],
                    "ready": True,
                },
            ),
            mock.patch.object(setup.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch.object(setup, "_source_commit", return_value="a" * 40),
            mock.patch.object(setup, "_platform_document", return_value=self.resolved.document),
            mock.patch.object(
                setup, "verify_from_environment", return_value=self.verified_release()[0]
            ),
            mock.patch.object(
                setup, "verify_artifact_from_environment", return_value=self.verified_release()[1]
            ),
            mock.patch.object(setup, "_command", side_effect=identity_read),
            mock.patch.object(setup, "_json_command", side_effect=spy),
        ):
            for name, spy_function in mutation_spies.items():
                spy_function.side_effect = AssertionError(name)
            plan = setup._setup_check(
                env_file=self.env_file,
                cloudflare_token=None,
                input_reader=lambda prompt: self.fail(prompt),
                secret_reader=lambda prompt: self.fail(prompt),
            )

        self.assertTrue(plan["ready"])
        self.assertEqual(plan["project"]["id"], self.project_id)
        self.assertFalse((self.root / "workspace").exists())
        mutating = {"create", "delete", "set", "unset", "update", "add", "remove", "rebuild"}
        self.assertFalse(any(mutating.intersection(command[1:]) for command in commands), commands)

    def test_quota_falls_back_when_detail_policy_is_denied(self) -> None:
        flavors = {
            role: row
            for role, row in zip(
                ("admin", "ingress", "storage", "worker", "builder"),
                self.provider_json(("openstack", "flavor", "list")),
                strict=True,
            )
        }

        def provider(argv: object, **_kwargs: object) -> object:
            command = tuple(str(item) for item in argv)  # type: ignore[arg-type]
            if command[1:3] == ("quota", "show") and "--usage" in command:
                raise setup.SetupError("quota detail forbidden")
            if command[1:3] == ("limits", "show"):
                return [
                    {"Name": "instances_used", "Value": 5},
                    {"Name": "total_cores_used", "Value": 25},
                    {"Name": "total_ram_used", "Value": 51200},
                    {"Name": "total_volumes_used", "Value": 5},
                    {"Name": "total_gigabytes_used", "Value": 812},
                ]
            if command[1:3] == ("quota", "show"):
                return [
                    {"Resource": name, "Limit": limit}
                    for name, limit in (
                        ("instances", 128),
                        ("cores", 128),
                        ("ram", 262144),
                        ("volumes", 10),
                        ("gigabytes", 3000),
                        ("ports", -1),
                        ("security_groups", -1),
                        ("security_group_rules", -1),
                        ("key_pairs", 100),
                    )
                ]
            if command[1:3] == ("keypair", "list"):
                return [{"Name": "existing"}]
            self.fail(f"unexpected fallback quota command: {command}")

        with mock.patch.object(setup, "_json_command", side_effect=provider):
            quotas = setup._quota_deltas(Path("openstack"), self.resolved, flavors)

        self.assertTrue(setup._quotas_ready(quotas))
        self.assertEqual(quotas["ports"]["available"], "unlimited")
        self.assertEqual(quotas["key_pairs"]["inUse"], 1)

    def test_address_occupancy_is_an_adversarial_failure(self) -> None:
        def occupied(argv: object, **kwargs: object) -> object:
            command = tuple(str(item) for item in argv)  # type: ignore[arg-type]
            if command[1:3] == ("port", "list") and "ip-address=192.0.2.12" in command:
                return [{"ID": "hostile-port", "Name": "unrelated"}]
            return self.provider_json(argv, **kwargs)

        with (
            mock.patch.object(
                setup,
                "_local_toolchain",
                return_value={
                    "host": "x86_64-linux",
                    "requiredHost": "x86_64-linux",
                    "commands": {},
                    "missing": [],
                    "ready": True,
                },
            ),
            mock.patch.object(setup.shutil, "which", return_value="/usr/bin/openstack"),
            mock.patch.object(setup, "_resolve_setup_inputs", return_value=self.resolved),
            mock.patch.object(
                setup, "verify_from_environment", return_value=self.verified_release()[0]
            ),
            mock.patch.object(
                setup, "verify_artifact_from_environment", return_value=self.verified_release()[1]
            ),
            mock.patch.object(setup, "_json_command", side_effect=occupied),
            mock.patch.object(
                setup,
                "_glance_usage",
                return_value={
                    "usage": {"image_count": 1, "image_size": 1024**3},
                    "limits": {"image_count": 100, "image_size": 100 * 1024**3},
                },
            ),
        ):
            plan = setup._setup_check(
                env_file=self.env_file,
                cloudflare_token=None,
                input_reader=lambda prompt: self.fail(prompt),
                secret_reader=lambda prompt: self.fail(prompt),
            )
        self.assertFalse(plan["ready"])
        self.assertFalse(plan["resolved"]["fixedAddresses"]["ingress"]["available"])

    def test_glance_quota_refuses_non_https_endpoint_before_token_use(self) -> None:
        with (
            mock.patch.object(
                setup,
                "_json_command",
                return_value={
                    "endpoints": [
                        {
                            "interface": "public",
                            "region": "RegionOne",
                            "url": "http://images.example.test/v2",
                        }
                    ]
                },
            ),
            mock.patch.object(setup, "_command") as command,
            self.assertRaisesRegex(setup.SetupError, "uses HTTP"),
        ):
            setup._glance_usage(Path("openstack"), self.resolved)
        command.assert_not_called()

    def test_glance_quota_allows_http_only_after_exact_acknowledgement(self) -> None:
        self.resolved.values["PLATFORM_ALLOW_HTTP_GLANCE"] = (
            "I_UNDERSTAND_GLANCE_CREDENTIALS_USE_HTTP"
        )
        try:
            with (
                mock.patch.object(
                    setup,
                    "_json_command",
                    return_value={
                        "endpoints": [
                            {
                                "interface": "public",
                                "region": "RegionOne",
                                "url": "http://images.example.test/v2",
                            },
                            {
                                "interface": "public",
                                "region": "RegionOne",
                                "url": "http://images.example.test/v2",
                            },
                        ]
                    },
                ),
                mock.patch.object(
                    setup,
                    "_command",
                    side_effect=(
                        "opaque-token",
                        json.dumps({"limits": {}, "usage": {}}),
                    ),
                ) as command,
                mock.patch.object(setup.shutil, "which", return_value="/usr/bin/curl"),
            ):
                result = setup._glance_usage(Path("openstack"), self.resolved)
        finally:
            self.resolved.values.pop("PLATFORM_ALLOW_HTTP_GLANCE")
        self.assertEqual(result, {"limits": {}, "usage": {}})
        self.assertEqual(command.call_count, 2)

    def test_legacy_glance_quota_404_requires_a_separate_exact_acknowledgement(self) -> None:
        self.resolved.values["PLATFORM_ALLOW_HTTP_GLANCE"] = (
            "I_UNDERSTAND_GLANCE_CREDENTIALS_USE_HTTP"
        )
        catalog = {
            "endpoints": [
                {
                    "interface": "public",
                    "region": "RegionOne",
                    "url": "http://images.example.test/v2",
                }
            ]
        }
        failure = setup.SetupError(
            "setup command failed (curl): curl: (22) The requested URL returned error: 404"
        )
        try:
            with (
                mock.patch.object(setup, "_json_command", return_value=catalog),
                mock.patch.object(setup, "_command", side_effect=("opaque-token", failure)),
                mock.patch.object(setup.shutil, "which", return_value="/usr/bin/curl"),
                self.assertRaisesRegex(setup.SetupError, "quota usage endpoint"),
            ):
                setup._glance_usage(Path("openstack"), self.resolved)

            self.resolved.values["PLATFORM_ALLOW_UNAVAILABLE_GLANCE_QUOTA"] = (
                "I_UNDERSTAND_GLANCE_QUOTA_IS_UNVERIFIED"
            )
            with (
                mock.patch.object(setup, "_json_command", return_value=catalog),
                mock.patch.object(setup, "_command", side_effect=("opaque-token", failure)),
                mock.patch.object(setup.shutil, "which", return_value="/usr/bin/curl"),
            ):
                usage = setup._glance_usage(Path("openstack"), self.resolved)
            self.assertEqual(usage, {"_platform_glance_quota_unavailable": True})
            with mock.patch.object(setup, "_glance_usage", return_value=usage):
                quotas = setup._glance_quota_deltas(
                    Path("openstack"), self.resolved, self.verified_release()[1]
                )
            self.assertTrue(setup._quotas_ready(quotas))
            self.assertEqual(quotas["image_count"]["verification"], "unverified-legacy-provider")
        finally:
            self.resolved.values.pop("PLATFORM_ALLOW_HTTP_GLANCE", None)
            self.resolved.values.pop("PLATFORM_ALLOW_UNAVAILABLE_GLANCE_QUOTA", None)

    def test_glance_unknown_and_shortfall_are_not_ready(self) -> None:
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures/openstack/glance_quota_formatter_outputs.json"
            ).read_text()
        )
        manifest = self.verified_release()[1]
        with mock.patch.object(setup, "_glance_usage", return_value=fixture["unknown"]):
            unknown = setup._glance_quota_deltas(Path("openstack"), self.resolved, manifest)
        self.assertIsNone(unknown["image_count"]["shortfall"])
        self.assertIsNone(unknown["image_storage_bytes"]["shortfall"])
        self.assertFalse(setup._quotas_ready(unknown))

        with mock.patch.object(setup, "_glance_usage", return_value=fixture["shortfall"]):
            short = setup._glance_quota_deltas(Path("openstack"), self.resolved, manifest)
        self.assertGreater(short["image_count"]["shortfall"], 0)
        self.assertGreater(short["image_storage_bytes"]["shortfall"], 0)
        self.assertFalse(setup._quotas_ready(short))


class SetupInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Path(__file__).resolve().parents[1]
        self.values = {
            "PLATFORM_PREFIX": "demo",
            "PLATFORM_NAMESPACE": "demo-platform",
            "PLATFORM_DISPLAY_NAME": "Demo Platform",
            "PLATFORM_ORGANIZATION": "Demo Organization",
            "PLATFORM_DOMAIN": "apps.example.test",
            "PLATFORM_NETWORK": "public",
            "PLATFORM_OPERATOR_CIDR": "192.0.2.10/32",
            "PLATFORM_ADMIN_ADDRESS": "192.0.2.11",
            "PLATFORM_INGRESS_ADDRESS": "192.0.2.12",
            "PLATFORM_STORAGE_ADDRESS": "192.0.2.13",
            "PLATFORM_ADMIN_FLAVOR": "admin-flavor",
            "PLATFORM_INGRESS_FLAVOR": "ingress-flavor",
            "PLATFORM_STORAGE_FLAVOR": "storage-flavor",
            "PLATFORM_WORKER_FLAVOR": "worker-flavor",
            "PLATFORM_BUILDER_FLAVOR": "builder-flavor",
            "PLATFORM_VOLUME_TYPE": "production",
        }

    def test_backup_volume_cannot_be_smaller_than_managed_data(self) -> None:
        self.values.update({"PLATFORM_DATA_GIB": "500", "PLATFORM_BACKUP_GIB": "499"})
        with (
            mock.patch.object(setup, "_network_default", return_value="ignored"),
            mock.patch.object(setup, "_flavor_inventory", return_value=[]),
            mock.patch.object(setup, "_volume_type_default", return_value="ignored"),
            self.assertRaisesRegex(setup.SetupError, "backup volume"),
        ):
            setup._platform_document(
                self.repository,
                self.values,
                setup.ProjectIdentity("00000000-0000-4000-8000-000000000001", "demo-project"),
                "a" * 40,
                Path("/nix/store/openstack"),
                {},
                lambda _prompt: self.fail("complete environment must not prompt"),
            )


class SetupControllerVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = setup.SetupPaths(
            repository=root,
            workspace=root / "workspace",
            platform=root / "platform.json",
            policy=root / "policy.json",
            bootstrap=root / "bootstrap",
            pki=root / "pki",
            openstack_environment=root / "openstack.env",
            openstack_wrapper=root / "platform-openstack",
            ssh_directory=root / "ssh",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_verification_checks_service_socket_api_and_operator_denial(self) -> None:
        with mock.patch.object(
            setup,
            "_command",
            return_value="controller-boundary=verified namespace=demo\n",
        ) as command:
            setup._verify_controller_boundary(self.paths, {"namespace": "demo"}, {})

        argv = command.call_args.args[0]
        self.assertEqual(
            argv,
            (
                "ssh",
                "-F",
                self.paths.ssh_directory / "config",
                "platform-admin",
                "--",
                "bash",
                "-s",
                "--",
                "demo",
            ),
        )
        script = command.call_args.kwargs["stdin"].decode("utf-8")
        self.assertIn('systemctl is-active --quiet "$controller"', script)
        self.assertIn('systemctl is-active --quiet "$readiness"', script)
        self.assertIn('systemctl is-enabled --quiet "$hosted_backup_timer"', script)
        self.assertIn("socket|platform-controller|controller-api|660", script)
        self.assertIn("socket|platform-controller|platform-admin|660", script)
        self.assertIn('--unix-socket "$project_socket"', script)
        self.assertIn('--unix-socket "$privileged_socket"', script)
        self.assertIn("operator account unexpectedly crossed the project", script)
        self.assertTrue(command.call_args.kwargs["capture"])

    def test_verification_rejects_unexpected_remote_evidence(self) -> None:
        with (
            mock.patch.object(setup, "_command", return_value=""),
            self.assertRaisesRegex(setup.SetupError, "unexpected verification evidence"),
        ):
            setup._verify_controller_boundary(self.paths, {"namespace": "demo"}, {})


class SetupCliTests(unittest.TestCase):
    def test_setup_dispatch_does_not_load_an_existing_platform_configuration(self) -> None:
        parser = operator.build_parser()
        args = parser.parse_args(["setup", "--env-file", "/private/setup.env"])
        output = io.StringIO()
        with (
            mock.patch.object(operator, "_load_config") as load_config,
            mock.patch.object(setup, "run_setup", return_value=None) as run_setup,
        ):
            operator.dispatch(args, stdin=io.StringIO(), stdout=output)
        load_config.assert_not_called()
        run_setup.assert_called_once()
        self.assertFalse(run_setup.call_args.kwargs["apply"])


if __name__ == "__main__":
    unittest.main()
