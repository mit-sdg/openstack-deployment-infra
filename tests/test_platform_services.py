from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from platform_cli import db, services, storage
from platform_cli.config import load
from platform_cli.validation import ValidationError


def config_fixture(directory: Path):
    platform = {
        "project": "example-project",
        "projectId": "00000000-0000-4000-8000-000000000000",
        "prefix": "example",
        "namespace": "app-platform",
        "domain": "apps.example.com",
        "datacenter": "example-dc",
        "region": "global",
        "network": "example-network",
        "hosts": {"admin": "example-admin"},
        "ports": {"admin": "example-admin-port"},
        "paths": {"root": "/srv/app-platform"},
    }
    policy = {
        "standard": {
            "workerFlavor": "example.1c2g",
            "cpuMHz": 1000,
            "memoryMiB": 2048,
            "postgresConnections": 10,
            "postgresMeasuredBytes": 2147483648,
            "mongoMeasuredBytes": 2147483648,
            "s3Bytes": 5368709120,
            "s3Objects": 100000,
        },
        "runtimeImages": {
            "bun": "registry.example/bun@sha256:" + "a" * 64,
            "node": "registry.example/node@sha256:" + "b" * 64,
        },
        "backupAgeRecipient": "age1" + "q" * 58,
    }
    platform_path = directory / "platform.json"
    policy_path = directory / "policy.json"
    platform_path.write_text(json.dumps(platform))
    policy_path.write_text(json.dumps(policy))
    policy_path.chmod(0o600)
    return load(platform_path, policy_path)


class ProductServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = config_fixture(self.root)
        self.connection = db.connect(self.root / "state.sqlite3")
        db.migrate(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def declare(self) -> services.ApplicationCreated:
        return services.ApplicationService(
            self.connection,
            self.config,
            self.root / "service-state",
        ).declare("demo-app")

    def test_application_declaration_is_typed_inert_and_independent_of_cli(self) -> None:
        created = self.declare()

        persisted = db.get_application(self.connection, created.application_id)
        assert persisted is not None
        self.assertEqual(created.slug, "demo-app")
        self.assertEqual(created.url, "https://demo-app.apps.example.com")
        self.assertFalse(created.enabled)
        self.assertFalse(persisted.desired_running)
        self.assertIsNone(db.get_deployment(self.connection, created.application_id))
        with self.assertRaisesRegex(ValidationError, "already exists"):
            self.declare()

    def test_environment_service_hides_values_and_records_only_accepted_key_names(self) -> None:
        created = self.declare()
        request = services.EnvironmentMutationRequest(
            action="set",
            application="demo-app",
            updates={"API_TOKEN": "secret-that-must-not-render"},
        )
        self.assertNotIn("secret-that-must-not-render", repr(request))

        with (
            mock.patch.object(services.openstack, "verify_project"),
            mock.patch.object(
                services.app,
                "set_environment",
                return_value={"keys": ["API_TOKEN"], "modifyIndex": 4},
            ) as update,
        ):
            result = services.EnvironmentService(
                self.connection,
                self.config,
                self.root / "service-state",
            ).mutate(request)

        self.assertEqual(result, services.EnvironmentMutationResult(("API_TOKEN",), 4))
        self.assertEqual(
            [
                (item.key_name, item.owner)
                for item in db.list_environment_keys(
                    self.connection,
                    application_id=created.application_id,
                )
            ],
            [("API_TOKEN", "staff")],
        )
        update.assert_called_once()
        operation = db.get_unfinished_operation(
            self.connection,
            f"app-{created.application_id}",
        )
        self.assertIsNone(operation)

    def test_storage_service_owns_lock_project_check_and_typed_dispatch(self) -> None:
        created = self.declare()
        expected = storage.StorageResult(("mongo",), ("mongo",))

        with (
            mock.patch.object(services.openstack, "verify_project") as verify_project,
            mock.patch.object(services.storage, "create", return_value=expected) as create,
        ):
            result = services.StorageService(
                self.connection,
                self.config,
                self.root / "service-state",
            ).mutate(
                services.StorageMutationRequest(
                    action="create",
                    application="demo-app",
                    resource_types=("mongo",),
                    resource_name="primary",
                )
            )

        self.assertEqual(result, services.StorageMutationResult(("mongo",), ("mongo",)))
        verify_project.assert_called_once()
        call = create.call_args
        self.assertEqual(call.args[:3], (self.connection, self.config, created.application_id))
        self.assertEqual(call.args[3], ("mongo",))
        self.assertEqual(call.kwargs["resource_name"], "primary")
        self.assertGreater(call.kwargs["process_deadline"], 0)
        self.assertTrue(call.kwargs["deadline_at"].endswith("Z"))

    def test_storage_verify_without_type_is_the_only_empty_selection(self) -> None:
        self.declare()
        service = services.StorageService(self.connection, self.config, self.root / "service-state")

        with self.assertRaisesRegex(ValidationError, "select at least one"):
            service.mutate(
                services.StorageMutationRequest(
                    action="create",
                    application="demo-app",
                    resource_types=(),
                )
            )
        with (
            mock.patch.object(services.openstack, "verify_project"),
            mock.patch.object(
                services.storage,
                "verify",
                return_value=storage.StorageResult(("postgres",), ("postgres",)),
            ) as verify,
        ):
            service.mutate(
                services.StorageMutationRequest(
                    action="verify",
                    application="demo-app",
                    resource_types=(),
                )
            )
        self.assertIsNone(verify.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
