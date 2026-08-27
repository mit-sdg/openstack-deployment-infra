from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from platform_cli import app, db, services, storage
from platform_cli.config import load
from platform_cli.deployment_config import parse_configuration
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

    def test_deleted_slug_cannot_be_redeclared(self) -> None:
        application = self.declare()
        self.connection.execute(
            "INSERT INTO application_slug_tombstones VALUES (?, ?, ?)",
            ("retired-app", application.application_id, "2026-08-27T00:00:00Z"),
        )
        with self.assertRaisesRegex(ValidationError, "permanently reserved"):
            services.ApplicationService(
                self.connection,
                self.config,
                self.root / "service-state",
            ).declare("retired-app")

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

    def accepted_application(self, *, running: bool = True) -> tuple[str, str, str, str]:
        created = self.declare()
        server_id = "00000000-0000-4000-8000-000000000011"
        port_id = "00000000-0000-4000-8000-000000000012"
        marker = "00000000-0000-4000-8000-000000000013"
        image = "registry.example/projects/demo/app@sha256:" + "d" * 64
        job = app.render_nomad_job(
            application_id=created.application_id,
            application_slug=created.slug,
            image=image,
            manifest=app.Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=self.config.platform,
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit="a" * 40,
            recipe_hash="b" * 64,
            route_marker=marker,
        )
        db.accept_deployment(
            self.connection,
            application_id=created.application_id,
            source_commit="a" * 40,
            recipe_hash="b" * 64,
            image_digest=image,
            nomad_job=job,
            nomad_job_sha256=app.nomad_candidate_identity(job)[0],
            nomad_version=4,
            health_path="/health",
            application_port=3000,
            build_log_path="logs/build.log",
        )
        db.set_environment_keys(
            self.connection,
            application_id=created.application_id,
            owner="staff",
            keys=("API_TOKEN",),
        )
        db.set_application_runtime(
            self.connection,
            created.application_id,
            running=running,
            worker_server_id=server_id if running else None,
            worker_server_name="worker-demo" if running else None,
            worker_port_id=port_id if running else None,
            worker_port_name="worker-demo-port" if running else None,
        )
        return created.application_id, server_id, port_id, image

    def test_disable_stops_exact_runtime_and_preserves_accepted_state(self) -> None:
        application_id, server_id, port_id, image = self.accepted_application()
        calls: list[tuple[str, dict[str, object]]] = []

        def helper(_config, action, values, **_bounds):
            calls.append((action, dict(values)))
            if action == "app.remove":
                return {"jobAbsent": True, "variableAbsent": False}
            if action == "app.worker.observe":
                return {
                    "ready": True,
                    "absent": False,
                    "serverId": server_id,
                    "serverName": "worker-demo",
                    "portId": port_id,
                    "portName": "worker-demo-port",
                }
            if action == "app.worker.delete":
                return {"absent": True}
            raise AssertionError(action)

        with mock.patch.object(services.openstack, "verify_project"):
            result = services.ApplicationService(
                self.connection,
                self.config,
                self.root / "service-state",
                helper_caller=helper,
            ).disable("demo-app")

        self.assertEqual(result.state, "disabled")
        persisted = db.get_application(self.connection, application_id)
        assert persisted is not None
        self.assertFalse(persisted.desired_running)
        self.assertIsNone(persisted.worker_server_id)
        self.assertEqual(db.get_deployment(self.connection, application_id).image_digest, image)  # type: ignore[union-attr]
        self.assertEqual(
            [item.key_name for item in db.list_environment_keys(
                self.connection, application_id=application_id
            )],
            ["API_TOKEN"],
        )
        removal = next(values for action, values in calls if action == "app.remove")
        self.assertEqual(
            set(removal),
            {"slug", "jobId", "candidateJobSha256", "candidateImage"},
        )
        deletion = next(values for action, values in calls if action == "app.worker.delete")
        self.assertEqual(deletion["applicationId"], application_id)
        self.assertTrue(deletion["single"])

    def test_enable_reuses_accepted_job_and_current_variable_without_build(self) -> None:
        application_id, server_id, port_id, _image = self.accepted_application(running=False)
        worker_image = "00000000-0000-4000-8000-000000000021"
        db.put_image_selection(
            self.connection,
            role="worker",
            image_id=worker_image,
            display_name="worker",
            source_commit="c" * 40,
            compatibility_hash="e" * 64,
        )
        deployment = db.get_deployment(self.connection, application_id)
        assert deployment is not None
        identity = app.nomad_candidate_identity(deployment.nomad_job)
        calls: list[str] = []

        def helper(_config, action, values, **_bounds):
            calls.append(action)
            if action == "app.worker.observe":
                return {"absent": True}
            if action == "app.worker.create":
                self.assertEqual(values["workerImageId"], worker_image)
                return {
                    "ready": True,
                    "absent": False,
                    "serverId": server_id,
                    "serverName": "worker-demo",
                    "portId": port_id,
                    "portName": "worker-demo-port",
                }
            if action == "app.deploy":
                self.assertEqual(values["job"], deployment.nomad_job)
                return {
                    "jobId": "demo-app",
                    "nomadVersion": 9,
                    "candidateJobSha256": identity[0],
                    "candidateImage": identity[1],
                }
            if action == "app.health":
                return {
                    "version": 9,
                    "currentVersion": 9,
                    "candidateJobSha256": identity[0],
                    "candidateImage": identity[1],
                    "allocations": 1,
                    "healthy": True,
                    "terminal": False,
                }
            raise AssertionError(action)

        with (
            mock.patch.object(services.openstack, "verify_project"),
            mock.patch.object(services.openstack, "observe_flavor", return_value="example.1c2g"),
            mock.patch.object(services.app, "check_public_health", return_value=True),
        ):
            result = services.ApplicationService(
                self.connection,
                self.config,
                self.root / "service-state",
                helper_caller=helper,
            ).enable("demo-app")

        self.assertEqual(result.state, "enabled")
        persisted = db.get_application(self.connection, application_id)
        assert persisted is not None
        self.assertTrue(persisted.desired_running)
        self.assertEqual((persisted.worker_server_id, persisted.worker_port_id), (server_id, port_id))
        self.assertEqual(db.get_deployment(self.connection, application_id).nomad_version, 9)  # type: ignore[union-attr]
        self.assertNotIn("app.build", calls)
        self.assertEqual(
            [item.key_name for item in db.list_environment_keys(
                self.connection, application_id=application_id
            )],
            ["API_TOKEN"],
        )

    def test_delete_resumes_storage_purge_and_preserves_tombstoned_history(self) -> None:
        application_id, _server_id, _port_id, _image = self.accepted_application()
        resource = db.put_managed_resource(
            self.connection,
            application_id=application_id,
            resource_type="s3",
            resource_name="assets",
            provider_id="bucket-id",
            provider_name="bucket-name",
            lifecycle_state="active",
            s3_bytes=100,
            s3_objects=10,
        )
        db.set_environment_keys(
            self.connection,
            application_id=application_id,
            owner=storage.storage_owner("s3", "assets"),
            keys=("STORAGE__S3__ASSETS__URL",),
        )
        fail_once = True
        calls: list[tuple[str, dict[str, object]]] = []

        def helper(_config, action, values, **_bounds):
            nonlocal fail_once
            calls.append((action, dict(values)))
            if action == "storage.s3.remove":
                self.assertTrue(values["purge"])
                if values["preflight"]:
                    return {"preflightAccepted": True}
                if fail_once:
                    fail_once = False
                    raise RuntimeError("interrupted")
                return {"confirmedAbsent": True, "environmentRemoved": True}
            if action == "app.remove":
                return {"jobAbsent": True, "variableAbsent": True}
            if action == "app.worker.delete":
                return {"absent": True}
            if action == "app.manifest.delete":
                return {"absent": True}
            raise AssertionError(action)

        service = services.ApplicationService(
            self.connection,
            self.config,
            self.root / "service-state",
            helper_caller=helper,
        )
        with mock.patch.object(services.openstack, "verify_project"):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                service.delete("demo-app", confirmation="demo-app")
            unfinished = db.get_unfinished_operation(self.connection, f"app-{application_id}")
            assert unfinished is not None
            self.assertEqual((unfinished.kind, unfinished.phase), ("app.delete", "storage_removing"))
            self.assertEqual(db.get_managed_resource(self.connection, resource.resource_id).lifecycle_state, "recovery_required")  # type: ignore[union-attr]
            with self.assertRaises(db.UnfinishedOperationError):
                services.EnvironmentService(
                    self.connection, self.config, self.root / "service-state"
                ).mutate(
                    services.EnvironmentMutationRequest(
                        action="set", application="demo-app", updates={"OTHER": "secret"}
                    )
                )
            service.delete("demo-app", confirmation="demo-app")

        self.assertIsNone(db.get_application(self.connection, application_id))
        self.assertEqual(db.get_slug_tombstone(self.connection, "demo-app").application_id, application_id)  # type: ignore[union-attr]
        self.assertEqual(len(db.list_deployment_attempts(self.connection, application_id)), 1)
        self.assertEqual(db.list_managed_resources(self.connection, application_id=application_id), [])
        self.assertEqual(db.list_environment_keys(self.connection, application_id=application_id), [])
        operation = db.get_operation(self.connection, unfinished.operation_id)
        self.assertEqual((operation.status, operation.phase), ("succeeded", "tombstoned"))  # type: ignore[union-attr]
        destructive_s3 = [
            values for action, values in calls
            if action == "storage.s3.remove" and values["preflight"] is False
        ]
        self.assertEqual([item["recover"] for item in destructive_s3], [False, True])
        first_storage = next(
            index for index, (action, _values) in enumerate(calls)
            if action == "storage.s3.remove"
        )
        self.assertEqual(calls[0][0], "app.remove")
        self.assertIn("jobId", calls[0][1])
        self.assertGreater(first_storage, 0)

    def test_storage_remove_refuses_active_configuration_reference(self) -> None:
        created = self.declare()
        resource = db.put_managed_resource(
            self.connection,
            application_id=created.application_id,
            resource_type="postgres",
            resource_name="primary",
            provider_name="p_demo",
            lifecycle_state="active",
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
                "storageBindings": [
                    {"resourceId": resource.resource_id, "outputs": {"url": "DATABASE_URL"}}
                ],
            }
        )
        request_id = "00000000-0000-4000-8000-000000000031"
        deployment_id = "00000000-0000-4000-8000-000000000032"
        db.claim_idempotency_request(
            self.connection,
            request_id=request_id,
            request_fingerprint=db.request_fingerprint({"request": request_id}),
        )
        db.create_deployment_attempt(
            self.connection,
            deployment_id=deployment_id,
            application_id=created.application_id,
            source_commit="a" * 40,
            requested_ref="refs/heads/main",
            configuration_revision=1,
            configuration=configuration,
            environment_revision=0,
            idempotency_request_id=request_id,
        )
        db.checkpoint_deployment_attempt(
            self.connection,
            deployment_id,
            status="succeeded",
            recipe_hash="b" * 64,
            image_digest="registry.example/projects/demo/app@sha256:" + "d" * 64,
            nomad_job="stored-job",
            nomad_job_sha256="e" * 64,
            nomad_version=1,
            build_log_path="logs/build.log",
        )
        with self.assertRaisesRegex(ValidationError, "active deployment references"):
            storage.remove(
                self.connection,
                self.config,
                created.application_id,
                ("postgres",),
                resource_name="primary",
                confirm_name="primary",
                confirm_destructive=True,
            )


if __name__ == "__main__":
    unittest.main()
