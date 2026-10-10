"""Project sizing routes, builder defaults and overrides, and build snapshots."""

from __future__ import annotations

import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from openstack_platform import openstack
from openstack_platform.controller import builder_settings, deployment_service
from openstack_platform.controller import database as db
from openstack_platform.controller.deployment_config import parse_configuration
from openstack_platform.controller.http import HttpError
from tests import test_controller_api as api_fixtures

SMALL = openstack.Flavor("50", "builder-small", 1, 1024, 20)
LARGE = openstack.Flavor("200", "worker-large", 4, 16384, 64)
TINY = openstack.Flavor("10", "tiny", 1, 575, 0)


class PortalSizingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = api_fixtures.ControllerAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.api, self.connection = self.fixture.api, self.fixture.connection
        self.project, self.admin = self.api.router("project"), self.api.router("privileged")
        self.app_id = self.fixture.create_application().body["applicationId"]
        for patch in (
            mock.patch.object(openstack, "observe_flavors", return_value=(LARGE, TINY, SMALL)),
            mock.patch.object(openstack, "observe_flavor_capacity", side_effect=self.flavor),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def flavor(_platform, reference, **_bounds):
        return next(
            item for item in (SMALL, LARGE, TINY) if reference in {item.name, item.flavor_id}
        )

    def put(self, path, body, router=None):
        key = str(uuid.uuid4())
        response = (router or self.project).dispatch("PUT", path, {"Idempotency-Key": key}, body)
        self.api.wait_for_operations()
        return response, db.get_operation(self.connection, key)

    def test_project_lists_only_sizes_that_fit_the_service_reserve(self) -> None:
        value = self.project.dispatch("GET", "/v1/flavors", {}, None).body
        self.assertEqual(
            value,
            {
                "items": [
                    dict(flavor_id="200", name="worker-large", vcpus=4, ram_mib=16384, disk_gib=64),
                    dict(flavor_id="50", name="builder-small", vcpus=1, ram_mib=1024, disk_gib=20),
                ]
            },
        )
        for query, body in (("?extra=1", None), ("", {})):
            with self.assertRaises(HttpError):
                self.project.dispatch("GET", "/v1/flavors" + query, {}, body)

    def test_provider_failures_withhold_details(self) -> None:
        with (
            mock.patch.object(
                openstack,
                "observe_flavors",
                side_effect=openstack.OpenStackError("PROVIDER_SECRET_SENTINEL"),
            ),
            self.assertRaises(HttpError) as raised,
        ):
            self.project.dispatch("GET", "/v1/flavors", {}, None)
        self.assertEqual(raised.exception.code, "EXTERNAL_OPERATION_FAILED")
        self.assertNotIn("SENTINEL", raised.exception.summary)

    def test_default_uses_inventory_then_changes_through_both_sockets(self) -> None:
        path = "/v1/settings/default-builder-size"
        self.assertEqual(
            self.project.dispatch("GET", path, {}, None).body["flavor"]["name"], SMALL.name
        )
        response, operation = self.put(
            path, {"flavor": LARGE.flavor_id, "expectedFlavor": SMALL.name}
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(operation.scope, "infrastructure")
        self.assertEqual(db.get_default_builder_flavor(self.connection), LARGE.name)
        _response, operation = self.put(
            "/v1/admin/settings/default-builder-size",
            {"flavor": SMALL.flavor_id, "expectedFlavor": LARGE.name},
            self.admin,
        )
        self.assertEqual(operation.status, "succeeded")
        self.assertEqual(db.get_default_builder_flavor(self.connection), SMALL.name)

    def test_default_requires_one_gib_and_rejects_changed_selection(self) -> None:
        path = "/v1/settings/default-builder-size"
        for body in (
            {"flavor": TINY.flavor_id, "expectedFlavor": SMALL.name},
            {"flavor": LARGE.flavor_id, "expectedFlavor": LARGE.name},
        ):
            _response, operation = self.put(path, body)
            self.assertEqual(operation.status, "failed")
            self.assertIsNone(db.get_default_builder_flavor(self.connection))
        with self.assertRaises(HttpError):
            self.project.dispatch(
                "PUT",
                path,
                {"Idempotency-Key": str(uuid.uuid4())},
                {"flavor": None, "expectedFlavor": SMALL.name},
            )

        path = f"/v1/applications/{self.app_id}/builder-size"
        for body in (
            {"flavor": TINY.flavor_id, "expectedFlavor": None},
            {"flavor": LARGE.flavor_id, "expectedFlavor": SMALL.name},
        ):
            _response, operation = self.put(path, body)
            self.assertEqual(operation.status, "failed")
            self.assertIsNone(db.get_application_builder_flavor(self.connection, self.app_id))
        for query, body in (("?extra=1", None), ("", {})):
            with self.assertRaises(HttpError):
                self.project.dispatch("GET", path + query, {}, body)

    def test_override_is_independent_of_worker_and_reset_follows_default(self) -> None:
        path = f"/v1/applications/{self.app_id}/builder-size"
        before = db.get_application(self.connection, self.app_id)
        initial = self.project.dispatch("GET", path, {}, None).body
        self.assertTrue(initial["useDefault"])
        self.assertEqual(initial["flavor"]["name"], SMALL.name)
        _response, operation = self.put(path, {"flavor": LARGE.flavor_id, "expectedFlavor": None})
        self.assertEqual(operation.scope, f"app-{self.app_id}")
        self.assertEqual(operation.status, "succeeded")
        db.put_default_builder_flavor(self.connection, SMALL.name)
        self.assertEqual(
            builder_settings.effective_flavor(self.connection, self.fixture.config, self.app_id),
            LARGE.name,
        )
        current = db.get_application(self.connection, self.app_id)
        self.assertEqual(
            (current.worker_flavor, current.worker_server_id, current.desired_running),
            (before.worker_flavor, before.worker_server_id, before.desired_running),
        )
        self.assertEqual(self.fixture.helper_calls, [])
        _response, operation = self.put(path, {"flavor": None, "expectedFlavor": LARGE.name})
        self.assertEqual(operation.status, "succeeded")
        self.assertIsNone(db.get_application_builder_flavor(self.connection, self.app_id))
        db.put_default_builder_flavor(self.connection, LARGE.name)
        self.assertEqual(
            builder_settings.effective_flavor(self.connection, self.fixture.config, self.app_id),
            LARGE.name,
        )

    def test_builder_selection_recovery_reconciles_an_already_written_setting(self) -> None:
        path = f"/v1/applications/{self.app_id}/builder-size"
        key = str(uuid.uuid4())
        body = {"flavor": LARGE.flavor_id, "expectedFlavor": None}
        with mock.patch.object(
            db, "mark_succeeded", side_effect=RuntimeError("interrupted after writing")
        ):
            self.project.dispatch("PUT", path, {"Idempotency-Key": key}, body)
            self.api.wait_for_operations()
        self.assertEqual(db.get_operation(self.connection, key).status, "recovery_required")
        self.project.dispatch("PUT", path, {"Idempotency-Key": key}, body)
        self.api.wait_for_operations()
        self.assertEqual(db.get_operation(self.connection, key).status, "succeeded")
        self.assertEqual(
            db.get_application_builder_flavor(self.connection, self.app_id), LARGE.name
        )

    def test_build_pins_app_effective_flavor_and_recovery_keeps_it(self) -> None:
        db.put_application_builder_flavor(self.connection, self.app_id, LARGE.name)
        key = str(uuid.uuid4())
        operation = db.begin_operation(
            self.connection,
            operation_id=key,
            kind="app.deploy",
            scope=f"app-{self.app_id}",
            phase="validated",
            deadline_at="2030-01-01T00:00:00Z",
            refs={"builder_image_id": str(uuid.uuid4())},
        )
        configuration = parse_configuration(
            {
                "schemaVersion": 1,
                "build": {
                    "runtime": "node",
                    "packages": ["."],
                    "buildScript": None,
                    "startScript": "start",
                },
                "runtime": {"port": 3000, "healthPath": "/health"},
                "storageBindings": [],
            }
        )
        captured = []

        def helper(_config, _action, args, **_bounds):
            captured.append(args["builderFlavor"])
            raise RuntimeError("stop at build boundary")

        for _attempt in range(2):
            with (
                mock.patch.object(db, "checkpoint_deployment_attempt"),
                self.assertRaisesRegex(RuntimeError, "build boundary"),
            ):
                deployment_service._prepare_deployment_build(
                    self.connection,
                    self.fixture.config,
                    helper_caller=helper,
                    state_directory=self.fixture.root,
                    operation=operation,
                    application_id=self.app_id,
                    application_slug="demo-app",
                    repository="https://github.com/example/app",
                    requested_ref="main",
                    source_commit="a" * 40,
                    configuration_revision=1,
                    configuration=configuration,
                    manifest=configuration.manifest({}),
                    deadline=time.monotonic() + 30,
                )
            db.put_application_builder_flavor(self.connection, self.app_id, None)
            db.put_default_builder_flavor(self.connection, SMALL.name)
            operation = db.get_operation(self.connection, key)
        self.assertEqual(captured, [LARGE.name, LARGE.name])


class SizingMigrationTests(unittest.TestCase):
    def test_v6_preserves_apps_and_seeds_neither_selection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            connection = db.connect(Path(folder) / "platform.sqlite3")
            self.addCleanup(connection.close)
            db.migrate(connection, target_version=5)
            identifier = str(uuid.uuid4())
            db.put_application(
                connection,
                application_id=identifier,
                application_slug="existing",
                worker_flavor="worker-small",
                scheduler_cpu_mhz=500,
                scheduler_memory_mib=512,
            )
            before = db.get_application(connection, identifier)
            db.migrate(connection, target_version=6)
            self.assertEqual(db.schema_version(connection), 6)
            self.assertEqual(db.get_application(connection, identifier), before)
            self.assertIsNone(db.get_default_builder_flavor(connection))
            self.assertIsNone(db.get_application_builder_flavor(connection, identifier))
            db.put_default_builder_flavor(connection, SMALL.name)
            db.put_application_builder_flavor(connection, identifier, LARGE.name)
            db.migrate(connection, target_version=6)
            self.assertEqual(db.get_default_builder_flavor(connection), SMALL.name)
            self.assertEqual(db.get_application_builder_flavor(connection, identifier), LARGE.name)
