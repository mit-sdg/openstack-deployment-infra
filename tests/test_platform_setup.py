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
        with mock.patch.object(setup, "_command", return_value=project_id) as command:
            identity = setup._project_identity(Path("/nix/store/openstack"), environment)

        self.assertEqual(identity, setup.ProjectIdentity(project_id, "demo"))
        self.assertEqual(command.call_args.args[0][1:3], ("token", "issue"))

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

    def test_plan_is_non_mutating_and_names_every_major_phase(self) -> None:
        path = self.environment("OS_PROJECT_NAME=demo\n")
        output = io.StringIO()

        result = setup.run_setup(
            env_file=path,
            workspace=self.root / "workspace",
            cloudflare_token=None,
            apply=False,
            output=output,
        )

        self.assertIsNone(result)
        self.assertFalse((self.root / "workspace").exists())
        rendered = output.getvalue()
        self.assertIn("five commit-addressed NixOS role images", rendered)
        self.assertIn("three persistent VMs", rendered)
        self.assertIn("rerun with --apply", rendered)


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
