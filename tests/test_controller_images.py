from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from openstack_platform import openstack, runtime
from openstack_platform.config import load
from openstack_platform.controller import database as db
from openstack_platform.controller import deployment_service, image_service
from openstack_platform.controller.api import ControllerAPI
from openstack_platform.controller.deployment_config import parse_configuration
from openstack_platform.controller.http import HttpError
from tests.test_platform_openstack import FakeCloud, canonical_image, result

ROOT = Path(__file__).resolve().parents[1]
OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
APP = "44444444-4444-4444-8444-444444444444"


class HostedImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = load(
            ROOT / "config/platform.example.json",
            ROOT / "config/platform-policy.example.json",
            require_private_policy=False,
        )
        self.connection = db.connect(self.root / "platform.sqlite3", check_same_thread=False)
        db.migrate(self.connection)
        for role in ("builder", "worker"):
            db.put_image_selection(
                self.connection,
                role=role,
                image_id=OLD,
                display_name=f"old-{role}",
                source_commit="b" * 40,
                compatibility_hash="c" * 64,
            )
        self.helper = mock.Mock(side_effect=AssertionError("image selection cannot call helper"))
        self.api = ControllerAPI(self.connection, self.config, self.root, helper_caller=self.helper)
        self.router = self.api.router("privileged")
        self.cloud = FakeCloud(
            self.config.platform,
            [
                canonical_image(self.config.platform, NEW, role="worker"),
                canonical_image(self.config.platform, OTHER, role="builder"),
            ],
        )
        real_select = openstack.select_image
        self.real_select = real_select
        self.select_patch = mock.patch.object(
            openstack,
            "select_image",
            side_effect=lambda *args, **kwargs: real_select(
                *args, **kwargs, command_runner=self.cloud
            ),
        )
        self.selector = self.select_patch.start()
        self.counter = 0

    def tearDown(self):
        self.api.close()
        self.select_patch.stop()
        self.connection.close()
        self.temporary.cleanup()

    def request(self, *, role="worker", image=NEW, expected=OLD, key=None):
        self.counter += 1
        key = key or f"00000000-0000-4000-8000-{self.counter:012d}"
        response = self.router.dispatch(
            "POST",
            f"/v1/admin/images/{role}/selection",
            {"Idempotency-Key": key},
            {"imageId": image, "expectedImageId": expected},
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(response.headers["Location"], response.body["statusUrl"])
        return response

    def poll(self, response):
        self.api.wait_for_operations()
        return self.router.dispatch("GET", response.body["statusUrl"], {}, None).body

    def test_exact_cas_selection_replay_and_admin_polling(self):
        first = self.request()
        operation = self.poll(first)
        self.assertEqual(operation["status"], "succeeded")
        selected = db.get_image_selection(self.connection, "worker")
        self.assertEqual(selected.image_id, NEW)
        self.assertEqual(
            selected.compatibility_hash, openstack.image_compatibility_hash(self.config.platform)
        )
        self.assertEqual(db.get_image_selection(self.connection, "builder").image_id, OLD)
        self.assertEqual(self.poll(self.request(key=first.body["operationId"])), operation)
        self.assertEqual(self.selector.call_count, 1)
        with self.assertRaises(HttpError) as error:
            self.request(image=OTHER, key=first.body["operationId"])
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        stale = self.poll(self.request())
        self.assertEqual(stale["status"], "failed")
        self.assertEqual(self.selector.call_count, 1)
        self.assertEqual(db.get_image_selection(self.connection, "worker"), selected)
        self.helper.assert_not_called()
        self.assertTrue(
            all(
                call[1:3] in {("token", "issue"), ("image", "list"), ("image", "show")}
                for call in self.cloud.calls
            )
        )
        with self.assertRaises(HttpError):
            self.api.router("project").dispatch("GET", first.body["statusUrl"], {}, None)

    def test_strict_uuid_role_fields_and_capability(self):
        for role, image, expected in (
            ("admin", NEW, OLD),
            ("worker", "image-name", OLD),
            ("builder", NEW, "bad"),
        ):
            with self.assertRaises(HttpError) as error:
                self.request(role=role, image=image, expected=expected)
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(HttpError) as error:
            self.api.router("project").dispatch("POST", "/v1/admin/images/worker/selection", {}, {})
        self.assertEqual(error.exception.status, 404)
        self.selector.assert_not_called()

    def test_provider_role_compatibility_owner_and_project_are_verified(self):
        image = self.cloud.images[NEW]
        original = json.loads(json.dumps(image))
        variants = [
            canonical_image(self.config.platform, NEW, role="builder"),
            {**original, "properties": {}},
            {**original, "status": "queued"},
            {**original, "owner": OTHER},
        ]
        for invalid in variants:
            self.cloud.images[NEW] = invalid
            self.assertEqual(self.poll(self.request())["status"], "failed")
            self.assertEqual(db.get_image_selection(self.connection, "worker").image_id, OLD)
        self.cloud.images[NEW] = original
        real_select = self.real_select

        def wrong_project(argv, **kwargs):
            return result(tuple(argv), {"project_id": OTHER})

        with mock.patch.object(
            openstack,
            "select_image",
            side_effect=lambda *args, **kwargs: real_select(
                *args, **kwargs, command_runner=wrong_project
            ),
        ):
            self.assertEqual(self.poll(self.request())["status"], "failed")
        self.assertEqual(db.get_image_selection(self.connection, "worker").image_id, OLD)

    def test_crash_before_and_after_selection_write_reconciles_same_intent(self):
        real_put = db.put_image_selection
        for after_write in (False, True):
            real_put(
                self.connection,
                role="worker",
                image_id=OLD,
                display_name="old-worker",
                source_commit="b" * 40,
                compatibility_hash="c" * 64,
            )

            def interrupted(*args, after_write=after_write, **kwargs):
                if after_write:
                    real_put(*args, **kwargs)
                raise OSError("simulated database interruption")

            with mock.patch.object(db, "put_image_selection", side_effect=interrupted):
                response = self.request()
                self.assertEqual(self.poll(response)["status"], "recovery_required")
            self.assertEqual(
                db.get_image_selection(self.connection, "worker").image_id,
                NEW if after_write else OLD,
            )
            selected_at = db.get_image_selection(self.connection, "worker").selected_at
            with self.assertRaises(HttpError) as conflict:
                self.request()
            self.assertEqual(conflict.exception.code, "OPERATION_CONFLICT")
            self.api.close()
            self.api = ControllerAPI(
                self.connection, self.config, self.root, helper_caller=self.helper
            )
            self.router = self.api.router("privileged")
            self.assertEqual(
                self.poll(self.request(key=response.body["operationId"]))["status"], "succeeded"
            )
            if after_write:
                self.assertEqual(
                    db.get_image_selection(self.connection, "worker").selected_at, selected_at
                )

    def test_recovery_does_not_overwrite_provider_or_selection_drift(self):
        with mock.patch.object(db, "put_image_selection", side_effect=OSError("before commit")):
            response = self.request()
            self.assertEqual(self.poll(response)["status"], "recovery_required")
        self.cloud.images[NEW]["properties"] = {}
        self.assertEqual(
            self.poll(self.request(key=response.body["operationId"]))["status"], "recovery_required"
        )
        self.assertEqual(db.get_image_selection(self.connection, "worker").image_id, OLD)
        db.put_image_selection(
            self.connection,
            role="worker",
            image_id=OTHER,
            display_name="drift",
            source_commit="d" * 40,
            compatibility_hash="e" * 64,
        )
        self.assertEqual(
            self.poll(self.request(key=response.body["operationId"]))["status"], "recovery_required"
        )
        self.assertEqual(db.get_image_selection(self.connection, "worker").image_id, OTHER)

    def test_selection_requires_the_hosted_infrastructure_lock(self):
        with runtime.lock(self.root, "infrastructure"):
            response = self.request()
            self.assertEqual(self.poll(response)["status"], "failed")
        self.selector.assert_not_called()
        self.assertEqual(db.get_image_selection(self.connection, "worker").image_id, OLD)

    def test_deployment_records_both_images_and_recovery_keeps_them(self):
        operation = db.begin_operation(
            self.connection,
            operation_id=APP,
            kind="app.deploy",
            scope=f"app-{APP}",
            phase="validated",
            deadline_at="2030-01-01T00:00:00Z",
        )
        pinned = image_service.pin_deployment_images(
            self.connection,
            self.root,
            operation,
            ("builder", "worker"),
            deadline=time.monotonic() + 30,
        )
        self.assertEqual(pinned.refs, {"builder_image_id": OLD, "worker_image_id": OLD})
        self.assertEqual(self.poll(self.request())["status"], "succeeded")
        self.assertEqual(
            self.poll(self.request(role="builder", image=OTHER))["status"], "succeeded"
        )
        recovered = image_service.pin_deployment_images(
            self.connection,
            self.root,
            pinned,
            ("builder", "worker"),
            deadline=time.monotonic() + 30,
        )
        self.assertEqual(recovered.refs, pinned.refs)
        self.assertEqual(set(db.unfinished_operation_image_ids(self.connection)), {OLD})
        configuration = parse_configuration(
            {
                "schemaVersion": 1,
                "build": {
                    "runtime": "node",
                    "packages": ["."],
                    "buildScript": None,
                    "startScript": "start",
                },
                "runtime": {"port": 8080, "healthPath": "/"},
                "storageBindings": [],
            }
        )

        def build(_config, action, values, **kwargs):
            self.assertEqual(action, "app.build")
            self.assertEqual(values["builderImageId"], OLD)
            raise RuntimeError("stop after observing selected build input")

        with (
            mock.patch.object(db, "checkpoint_deployment_attempt"),
            self.assertRaisesRegex(RuntimeError, "selected build input"),
        ):
            deployment_service._prepare_deployment_build(
                self.connection,
                self.config,
                helper_caller=build,
                state_directory=self.root,
                operation=recovered,
                application_id=APP,
                application_slug="demo-app",
                repository="https://github.com/example/app",
                requested_ref="main",
                source_commit="a" * 40,
                configuration_revision=1,
                configuration=configuration,
                manifest=configuration.manifest({}),
                deadline=time.monotonic() + 30,
            )

        def worker(_config, action, values, **kwargs):
            if action == "app.worker.observe":
                return {"absent": True}
            self.assertEqual(action, "app.worker.create")
            self.assertEqual(values["workerImageId"], OLD)
            return {
                "ready": True,
                "serverId": NEW,
                "serverName": "candidate",
                "portId": OTHER,
                "portName": "candidate-port",
            }

        with mock.patch.object(openstack, "observe_flavor", return_value="worker-small"):
            deployment_service._prepare_deployment_worker(
                self.connection,
                self.config,
                helper_caller=worker,
                state_directory=self.root,
                operation_id=APP,
                application_id=APP,
                application_slug="demo-app",
                worker_flavor="worker-small",
                candidate="registry.example/app@sha256:" + "a" * 64,
                refs=dict(recovered.refs),
                deadline=time.monotonic() + 30,
            )
        new_operation = db.begin_operation(
            self.connection,
            operation_id=OTHER,
            kind="app.deploy",
            scope=f"app-{OTHER}",
            phase="validated",
            deadline_at="2030-01-01T00:00:00Z",
        )
        fresh = image_service.pin_deployment_images(
            self.connection,
            self.root,
            new_operation,
            ("builder", "worker"),
            deadline=time.monotonic() + 30,
        )
        self.assertEqual(fresh.refs, {"builder_image_id": OTHER, "worker_image_id": NEW})


if __name__ == "__main__":
    unittest.main()
