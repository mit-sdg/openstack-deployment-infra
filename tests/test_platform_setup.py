from __future__ import annotations

import io
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

    def test_nova_bootstrap_key_is_rsa_and_private(self) -> None:
        private_key = self.root / "admin_nova_rsa"

        setup._ensure_key(private_key, key_type="rsa")

        self.assertEqual(private_key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(private_key.with_suffix(".pub").stat().st_mode & 0o777, 0o644)
        self.assertTrue(
            private_key.with_suffix(".pub").read_text(encoding="utf-8").startswith("ssh-rsa ")
        )

    def test_project_identity_uses_scoped_token_without_project_list_permission(self) -> None:
        project_id = "00000000-0000-4000-8000-000000000001"
        environment = {"OS_PROJECT_NAME": "demo", "OS_PROJECT_ID": project_id}
        with mock.patch.object(setup, "_command", side_effect=(project_id, "demo")) as command:
            identity = setup._project_identity(Path("/nix/store/openstack"), environment)

        self.assertEqual(identity, setup.ProjectIdentity(project_id, "demo"))
        self.assertEqual(command.call_args_list[0].args[0][1:3], ("token", "issue"))
        self.assertEqual(command.call_args_list[1].args[0][1:3], ("project", "show"))

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

    def test_malformed_or_missing_direct_provider_cidrs_fail_before_mutation(self) -> None:
        cases = (
            "PLATFORM_INGRESS_MODE=direct\n",
            "PLATFORM_INGRESS_MODE=direct\nPLATFORM_PROVIDER_CIDRS=0.0.0.0/0\n",
            "PLATFORM_INGRESS_MODE=direct\nPLATFORM_PROVIDER_CIDRS=203.0.113.7/24\n",
        )
        for index, content in enumerate(cases):
            path = self.environment(content)
            workspace = self.root / f"workspace-{index}"
            with self.subTest(content=content), self.assertRaises(setup.SetupError):
                setup.run_setup(
                    env_file=path,
                    workspace=workspace,
                    cloudflare_token=None,
                    apply=False,
                    output=io.StringIO(),
                )
            self.assertFalse(workspace.exists())

    def test_apply_rejects_missing_release_evidence_before_workspace_mutation(self) -> None:
        path = self.environment(
            "OS_AUTH_URL=https://identity.example/v3\n"
            "OS_PROJECT_NAME=demo\n"
            "OS_USERNAME=operator\n"
            "OS_PASSWORD=secret\n"
        )
        with (
            mock.patch.object(setup, "_repository_root", return_value=Path(__file__).parents[1]),
            mock.patch.object(setup, "_source_commit", return_value="a" * 40),
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
        self.assertFalse((self.root / "workspace").exists())

    def test_check_is_non_mutating_and_renders_resolved_plan(self) -> None:
        path = self.environment("OS_PROJECT_NAME=demo\n")
        output = io.StringIO()
        plan = {
            "ready": True,
            "project": {"name": "demo", "id": "00000000-0000-4000-8000-000000000001"},
            "resolved": {
                "network": {"name": "public", "id": "00000000-0000-4000-8000-000000000002"},
                "flavors": {"admin": {"name": "large", "vcpus": 2, "ramMiB": 4096}},
                "volumeType": {"name": "production"},
                "fixedAddresses": {"admin": {"address": "192.0.2.11", "available": True}},
            },
            "quotaDeltas": {"instances": {"requiredDelta": 5, "available": 10, "shortfall": 0}},
            "nameCollisions": [],
            "toolchain": {"requiredHost": "x86_64-linux", "commands": {}},
            "ingress": {"choice": "external-provider-pending", "domain": "apps.test"},
            "source": {
                "releaseCommit": "a" * 40,
                "roleImages": {},
                "runtimeImages": {},
                "containerImages": {},
            },
        }
        with mock.patch.object(setup, "_setup_check", return_value=plan) as check:
            result = setup.run_setup(
                env_file=path,
                workspace=self.root / "workspace",
                cloudflare_token=None,
                apply=False,
                output=output,
            )

        self.assertIsNone(result)
        self.assertFalse((self.root / "workspace").exists())
        check.assert_called_once()
        rendered = output.getvalue()
        self.assertIn("setup-check=ready", rendered)
        self.assertIn("quota.instances=+5", rendered)
        self.assertIn("no resources or credentials were created", rendered)


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
            role: {"qcow2Sha256": "a" * 64, "nixClosureSha256": "b" * 64}
            for role in setup.IMAGE_ROLES
        }
        return {"releaseChannel": "production"}, {"roleArtifacts": artifacts}

    def provider_json(self, argv: object, **_kwargs: object) -> object:
        command = tuple(str(item) for item in argv)  # type: ignore[arg-type]
        if command[1:3] == ("network", "list"):
            return [{"ID": self.network_id, "Name": "public"}]
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
                return self.project_id
            if command[1:3] == ("project", "show"):
                return "demo"
            self.fail(f"unexpected identity command: {command}")

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
            mock.patch.object(setup.shutil, "which", return_value="/usr/bin/openstack"),
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
        ):
            plan = setup._setup_check(
                env_file=self.env_file,
                cloudflare_token=None,
                input_reader=lambda prompt: self.fail(prompt),
                secret_reader=lambda prompt: self.fail(prompt),
            )
        self.assertFalse(plan["ready"])
        self.assertFalse(plan["resolved"]["fixedAddresses"]["ingress"]["available"])


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

    def test_generated_inventory_uses_large_fresh_volume_defaults(self) -> None:
        with (
            mock.patch.object(setup, "_network_default", return_value="ignored"),
            mock.patch.object(setup, "_flavor_inventory", return_value=[]),
            mock.patch.object(setup, "_volume_type_default", return_value="ignored"),
        ):
            document = setup._platform_document(
                self.repository,
                self.values,
                setup.ProjectIdentity("00000000-0000-4000-8000-000000000001", "demo-project"),
                "a" * 40,
                Path("/nix/store/openstack"),
                {},
                lambda _prompt: self.fail("complete environment must not prompt"),
            )

        self.assertEqual(document["volumes"]["data"]["sizeGiB"], 500)
        self.assertEqual(document["volumes"]["backup"]["sizeGiB"], 200)
        self.assertEqual(document["volumes"]["adminState"]["sizeGiB"], 32)
        labels = {volume["label"] for volume in document["volumes"].values()}
        self.assertEqual(len(labels), 3)
        self.assertTrue(all(len(label.encode()) <= 12 for label in labels))
        self.assertEqual(document["addresses"]["ingress"], "192.0.2.12")
        self.assertEqual(document["images"]["worker"], "demo-nixos-worker-aaaaaaaa")
        self.assertEqual(document["staticIngressRoutes"], {})
        self.assertEqual(document["publicIngress"], {"mode": "tunnel", "providerCidrs": []})

    def test_explicit_volume_sizes_override_fresh_defaults(self) -> None:
        self.values.update(
            {
                "PLATFORM_ADMIN_STATE_GIB": "64",
                "PLATFORM_DATA_GIB": "750",
                "PLATFORM_BACKUP_GIB": "300",
            }
        )
        with (
            mock.patch.object(setup, "_network_default", return_value="ignored"),
            mock.patch.object(setup, "_flavor_inventory", return_value=[]),
            mock.patch.object(setup, "_volume_type_default", return_value="ignored"),
        ):
            document = setup._platform_document(
                self.repository,
                self.values,
                setup.ProjectIdentity("00000000-0000-4000-8000-000000000001", "demo-project"),
                "b" * 40,
                Path("/nix/store/openstack"),
                {},
                lambda _prompt: self.fail("complete environment must not prompt"),
            )
        self.assertEqual(document["volumes"]["adminState"]["sizeGiB"], 64)
        self.assertEqual(document["volumes"]["data"]["sizeGiB"], 750)
        self.assertEqual(document["volumes"]["backup"]["sizeGiB"], 300)


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

    def test_verification_propagates_remote_failure(self) -> None:
        with (
            mock.patch.object(
                setup,
                "_command",
                side_effect=setup.SetupError("setup command failed (ssh): controller not active"),
            ),
            self.assertRaisesRegex(setup.SetupError, "controller not active"),
        ):
            setup._verify_controller_boundary(self.paths, {"namespace": "demo"}, {})

    def test_bootstrap_verifies_controller_after_helper_install(self) -> None:
        source = Path(setup.__file__).read_text(encoding="utf-8")
        bootstrap = source[source.index("def _bootstrap_roles(") : source.index("def _paths(")]
        helper = bootstrap.index("deploy_helper_release.sh")
        verification = bootstrap.index("_verify_controller_boundary(paths, platform, child)")
        final_status = bootstrap.index("status_output = _command", verification)
        self.assertLess(helper, verification)
        self.assertLess(verification, final_status)


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

    def test_setup_error_uses_normal_cli_error_exit(self) -> None:
        with mock.patch.object(setup, "run_setup", side_effect=setup.SetupError("bounded")):
            with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
                result = operator.main(["setup", "--env-file", "/missing"])
        self.assertEqual(result, operator.EXIT_ERROR)
        self.assertIn("error: bounded", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
