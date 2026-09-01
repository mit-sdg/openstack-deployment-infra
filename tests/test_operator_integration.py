from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from openstack_platform import openstack, operator, remote
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller import database as db
from openstack_platform.controller import deployment_service, status
from openstack_platform.controller.deployment_config import parse_configuration
from openstack_platform.controller.storage_contract import PLATFORM_ENVIRONMENT_KEYS
from openstack_platform.helper.main import default_handlers
from openstack_platform.helper.production import ACTION_MANIFEST
from tests.product_fixtures import accept_deployment

APP_ID = "00000000-0000-4000-8000-000000000001"
IMAGE_ID = "00000000-0000-4000-8000-000000000002"
DIGEST = "registry.example/runtime/image@sha256:" + "a" * 64
RECIPIENT = "age1" + "q" * 58


class OperatorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.platform = self.root / "platform.json"
        self.policy = self.root / "policy.json"
        platform_document = json.loads(
            (Path(__file__).resolve().parents[1] / "config/platform.example.json").read_text()
        )
        platform_document.update(
            {
                "projectId": APP_ID,
                "datacenter": "dc1",
                "network": "private",
                "hosts": {"admin": "admin", "ingress": "ingress", "storage": "storage"},
                "ports": {
                    "admin": "admin-port",
                    "ingress": "ingress-port",
                    "storage": "storage-port",
                },
                "addresses": {
                    "admin": "192.0.2.11",
                    "ingress": "192.0.2.12",
                    "storage": "storage.internal",
                },
                "flavors": {
                    **platform_document["flavors"],
                    "builder": "builder-standard",
                },
                "images": {
                    "admin": "admin",
                    "ingress": "ingress",
                    "storage": "storage",
                    "worker": "worker",
                    "builder": "builder",
                },
            }
        )
        self.platform.write_text(json.dumps(platform_document))
        self.policy.write_text(
            json.dumps(
                {
                    "standard": {
                        "workerFlavor": "one-vcpu",
                        "cpuMHz": 1000,
                        "memoryMiB": 2048,
                        "postgresConnections": 10,
                        "postgresMeasuredBytes": 2147483648,
                        "mongoMeasuredBytes": 2147483648,
                        "s3Bytes": 5368709120,
                        "s3Objects": 100000,
                    },
                    "runtimeImages": {"bun": DIGEST, "node": DIGEST},
                    "backupAgeRecipient": RECIPIENT,
                }
            )
        )
        self.policy.chmod(0o600)
        self.verify_project = mock.patch.object(
            operator.openstack,
            "verify_project",
            return_value=openstack.ProjectIdentity(APP_ID, "example-project"),
        )
        self.verify_project.start()
        self.addCleanup(self.verify_project.stop)
        self.observe_flavor = mock.patch.object(
            operator.openstack,
            "observe_flavor",
            return_value="one-vcpu",
        )
        self.observe_flavor_mock = self.observe_flavor.start()
        self.addCleanup(self.observe_flavor.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def argv(self, *command: str) -> list[str]:
        return [
            "--state-directory",
            str(self.state),
            "--platform-config",
            str(self.platform),
            "--policy",
            str(self.policy),
            *command,
        ]

    def test_unexpected_operator_failure_writes_private_correlation_diagnostic(self) -> None:
        sentinel = "sentinel-management-exception"
        stderr = StringIO()
        with (
            mock.patch.object(operator, "dispatch", side_effect=RuntimeError(sentinel)),
            mock.patch("sys.stderr", new=stderr),
        ):
            result = operator.main(
                [
                    "--state-directory",
                    str(self.state),
                    "status",
                ]
            )
        self.assertEqual(result, operator.EXIT_ERROR)
        self.assertIn("correlation ID", stderr.getvalue())
        self.assertNotIn(sentinel, stderr.getvalue())
        diagnostics = list((self.state / "diagnostics").glob("*.trace"))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].stat().st_mode & 0o777, 0o600)
        payload = diagnostics[0].read_text()
        self.assertIn("openstack_platform/operator.py:", payload)
        self.assertNotIn(sentinel, payload)

    def test_remote_dependency_unavailable_exits_four(self) -> None:
        with (
            mock.patch.object(
                operator,
                "dispatch",
                side_effect=remote.DependencyUnavailable("helper unavailable"),
            ),
            mock.patch("sys.stderr", new=StringIO()) as stderr,
        ):
            self.assertEqual(operator.main(["status"]), operator.EXIT_UNAVAILABLE)
        self.assertIn("unavailable:", stderr.getvalue())

    def test_operator_command_tree_excludes_controller_product_commands(self) -> None:
        commands = (
            ("setup", "--env-file", str(self.root / "setup.env")),
            (
                "setup",
                "--env-file",
                str(self.root / "setup.env"),
                "--cloudflare-token-file",
                str(self.root / "cloudflare.token"),
                "--apply",
            ),
            ("status",),
            ("backup",),
            ("infra", "list"),
            ("infra", "image", "list"),
            ("infra", "image", "set", "worker", IMAGE_ID),
            ("infra", "image", "prune"),
            ("infra", "image", "prune", "--apply", "--yes"),
            ("infra", "start", "admin"),
            ("infra", "stop", "storage", "--yes"),
            ("infra", "reboot", "ingress", "--yes"),
            ("infra", "replace", "admin", "--yes"),
            ("infra", "logs", "admin"),
        )
        parser = operator.build_parser()
        for command in commands:
            with self.subTest(command=command):
                parser.parse_args(self.argv(*command))
        for command in (("app", "list"), ("storage", "list")):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(self.argv(*command))

    def test_status_initializes_private_database_and_renders_table(self) -> None:
        output = StringIO()
        operator.dispatch(operator.build_parser().parse_args(self.argv("status")), stdout=output)
        self.assertIn("STATE", output.getvalue())
        database = self.state / "platform.sqlite3"
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        connection = db.connect(database)
        try:
            self.assertEqual(db.schema_version(connection), db.MIGRATIONS[-1].version)
        finally:
            connection.close()

    def test_fresh_status_counts_only_expected_persistent_dependencies(self) -> None:
        args = operator.build_parser().parse_args(self.argv("status"))
        with operator._database(args) as connection:
            model = status.status_show(
                connection,
                observe_infrastructure=lambda role: status.InfrastructureObservation(
                    role,
                    "active",
                    "healthy",
                    "2026-01-01T00:00:00Z",
                ),
            )
        self.assertEqual(model["state"], "healthy")
        self.assertEqual(model["observations"], {"available": 3, "unavailable": 0, "unhealthy": 0})

    def test_image_selection_uses_lock_operation_and_accepted_uuid(self) -> None:
        selected = openstack.ImageSelection("worker", IMAGE_ID, "worker-image", "b" * 40, "c" * 64)
        args = operator.build_parser().parse_args(
            self.argv("infra", "image", "set", "worker", IMAGE_ID)
        )
        output = StringIO()
        with mock.patch.object(operator.openstack, "select_image", return_value=selected):
            operator.dispatch(args, stdout=output)
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            accepted = db.get_image_selection(connection, "worker")
            self.assertIsNotNone(accepted)
            assert accepted is not None
            self.assertEqual(accepted.image_id, IMAGE_ID)
            operation = connection.execute(
                "SELECT status FROM operations WHERE kind='infra.image.set'"
            ).fetchone()
            self.assertEqual(operation["status"], "succeeded")
        finally:
            connection.close()

    def test_image_selection_recovers_observed_phase_without_provider_resubmit(self) -> None:
        args = operator.build_parser().parse_args(
            self.argv("infra", "image", "set", "worker", IMAGE_ID)
        )
        configured = operator._load_config(args)
        operation_id = "00000000-0000-4000-8000-000000000099"
        with operator._database(args) as connection:
            db.begin_operation(
                connection,
                operation_id=operation_id,
                kind="infra.image.set",
                scope="infrastructure",
                phase="selection_observed",
                deadline_at=operator._deadline(configured),
                refs={
                    "role": "worker",
                    "requested_image": IMAGE_ID,
                    "image_id": IMAGE_ID,
                    "display_name": "worker-image",
                    "source_commit": "b" * 40,
                    "compatibility_hash": "c" * 64,
                },
            )
        with mock.patch.object(operator.openstack, "select_image") as select:
            operator.dispatch(args, stdout=StringIO())
        select.assert_not_called()
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            self.assertEqual(db.get_operation(connection, operation_id).status, "succeeded")  # type: ignore[union-attr]
            self.assertEqual(db.get_image_selection(connection, "worker").image_id, IMAGE_ID)  # type: ignore[union-attr]
        finally:
            connection.close()

    def test_prune_apply_recovers_exact_checkpoint_without_self_protecting_candidates(self) -> None:
        args = operator.build_parser().parse_args(
            self.argv("infra", "image", "prune", "--apply", "--yes")
        )
        configured = operator._load_config(args)
        operation_id = "00000000-0000-4000-8000-000000000098"
        plan = openstack.PrunePlan(
            (IMAGE_ID,),
            (),
            (),
            "d" * 64,
            "2026-01-01T00:00:00Z",
            "2099-01-01T00:15:00Z",
            2,
            ((IMAGE_ID, "e" * 64),),
            (),
            (),
            (),
            "f" * 64,
        )
        refs = {**plan.operation_refs(), "deleted_image_ids": [], "pending_image_id": IMAGE_ID}
        with operator._database(args) as connection:
            db.begin_operation(
                connection,
                operation_id=operation_id,
                kind="infra.image.prune.apply",
                scope="infrastructure",
                phase="image_deleting",
                deadline_at=operator._deadline(configured),
                refs=refs,
            )
            db.mark_recovery_required(connection, operation_id, "injected interruption")
        recovered = openstack.PruneResult((IMAGE_ID,), "d" * 64)
        with mock.patch.object(
            operator.openstack, "recover_image_prune", return_value=recovered
        ) as recover:
            operator.dispatch(args, stdout=StringIO())
        self.assertEqual(recover.call_args.kwargs["operation_image_ids"], ())
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            self.assertEqual(db.get_operation(connection, operation_id).status, "succeeded")  # type: ignore[union-attr]
        finally:
            connection.close()

    def test_host_log_line_bounds(self) -> None:
        parser = operator.build_parser()
        for command in (
            ("infra", "logs", "admin", "--lines", "0"),
            ("infra", "logs", "admin", "--lines", "2001"),
        ):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(self.argv(*command))

    def test_platform_ownership_transfer_preserves_unrelated_staff_keys(self) -> None:
        args = operator.build_parser().parse_args(self.argv("status"))
        with operator._database(args) as connection:
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
            db.set_environment_keys(
                connection,
                application_id=APP_ID,
                owner="staff",
                keys=["NODE_ENV", "STAFF_SENTINEL"],
            )
            deployment_service._record_platform_environment_ownership(
                connection,
                APP_ID,
                sorted(PLATFORM_ENVIRONMENT_KEYS),
            )
            ownership = {
                item.key_name: item.owner
                for item in db.list_environment_keys(connection, application_id=APP_ID)
            }
        self.assertEqual(ownership["NODE_ENV"], "platform")
        self.assertEqual(ownership["STAFF_SENTINEL"], "staff")

    def _select_deployment_images(self) -> None:
        args = operator.build_parser().parse_args(self.argv("status"))
        with operator._database(args) as connection:
            for role, suffix in (("builder", "2"), ("worker", "3")):
                db.put_image_selection(
                    connection,
                    role=role,
                    image_id=f"00000000-0000-4000-8000-00000000000{suffix}",
                    display_name=f"{role}-image",
                    source_commit="b" * 40,
                    compatibility_hash="c" * 64,
                )

    def _deploy(self, application: str, helper_caller) -> deployment_service.DeploymentOutcome:
        args = operator.build_parser().parse_args(self.argv("status"))
        configured = operator._load_config(args)
        configuration = parse_configuration(
            {
                "schemaVersion": 1,
                "build": {
                    "runtime": "node",
                    "packages": ["."],
                    "buildScript": None,
                    "startScript": "serve",
                },
                "runtime": {"port": 8080, "healthPath": "/ready"},
                "storageBindings": [],
            }
        )
        with operator._database(args) as connection:
            return deployment_service.DeploymentService(
                connection,
                configured,
                self.state,
                helper_caller=helper_caller,
            ).deploy(
                deployment_service.DeploymentRequest(
                    application=application,
                    repository="https://github.com/o/r",
                    requested_ref="main",
                    source_commit="a" * 40,
                    configuration_revision=1,
                    configuration=configuration,
                ),
                deadline=operator._command_deadline(configured),
            )

    def test_deploy_recovers_builder_phase(self) -> None:
        self._select_deployment_images()
        deploy_args = operator.build_parser().parse_args(self.argv("status"))
        manifest = app.Manifest("node", (".",), None, "serve", 8080, "/ready")
        recipe = app.generate_recipe(
            manifest,
            operator._load_config(deploy_args).policy.runtime_images,
        )
        image = "storage.internal:5000/projects/demo-app/app@sha256:" + "d" * 64
        actions: list[str] = []
        call_values: dict[str, list[object]] = {}
        self.observe_flavor_mock.side_effect = lambda *_args, **_kwargs: (
            actions.append("flavor.verify") or "one-vcpu"
        )
        fail_build = [True]

        def helper(
            _config: object, action: str, values: object, **_kwargs: object
        ) -> dict[str, object]:
            actions.append(action)
            call_values.setdefault(action, []).append(values)
            if action == "app.build":
                checkpoint = db.connect(self.state / "platform.sqlite3")
                try:
                    current = checkpoint.execute(
                        "SELECT refs_json FROM operations WHERE kind='app.deploy' AND status IN ('running','recovery_required')"
                    ).fetchone()
                    self.assertEqual(
                        json.loads(current["refs_json"])["builder_image_id"],
                        "00000000-0000-4000-8000-000000000002",
                    )
                finally:
                    checkpoint.close()
                if fail_build[0]:
                    fail_build[0] = False
                    raise app.ApplicationError("bounded build interruption")
                return {
                    "image": image,
                    "recipeHash": recipe.sha256,
                    "manifest": {
                        "runtime": "node",
                        "packages": ["."],
                        "buildScript": None,
                        "startScript": "serve",
                        "port": 8080,
                        "healthPath": "/ready",
                    },
                    "log": "safe build log\n",
                    "logTruncated": False,
                    "builderAbsent": True,
                }
            if action == "app.builder.delete":
                return {"absent": True}
            if action == "app.env.set":
                return {
                    "keys": [
                        "NODE_ENV",
                        "PLATFORM_ENV",
                        "PLATFORM_PROJECT_ID",
                        "PLATFORM_PROJECT_SLUG",
                        "PORT",
                    ],
                    "modifyIndex": 2,
                    "restarted": False,
                    "schedulerHealthy": False,
                    "publicHealthy": False,
                }
            if action == "app.worker.observe":
                return {"absent": True}
            if action == "app.worker.create":
                checkpoint = db.connect(self.state / "platform.sqlite3")
                try:
                    current = checkpoint.execute(
                        "SELECT refs_json FROM operations WHERE kind='app.deploy' AND status IN ('running','recovery_required')"
                    ).fetchone()
                    self.assertEqual(
                        json.loads(current["refs_json"])["worker_image_id"],
                        "00000000-0000-4000-8000-000000000003",
                    )
                finally:
                    checkpoint.close()
                return {
                    "ready": True,
                    "serverId": "00000000-0000-4000-8000-000000000004",
                    "serverName": "example-worker-000000000001",
                    "portId": "00000000-0000-4000-8000-000000000005",
                    "portName": "example-worker-000000000001-v4",
                }
            if action == "app.deploy":
                candidate = app.nomad_candidate_identity(values["job"])  # type: ignore[index]
                assert candidate is not None
                return {
                    "jobId": app.nomad_job_id(values["job"], "demo-app"),  # type: ignore[index]
                    "nomadVersion": 4,
                    "candidateJobSha256": candidate[0],
                    "candidateImage": candidate[1],
                }
            if action == "app.health":
                deployed = call_values["app.deploy"][-1]
                candidate = app.nomad_candidate_identity(deployed["job"])  # type: ignore[index]
                assert candidate is not None
                return {
                    "healthy": True,
                    "terminal": False,
                    "version": 4,
                    "currentVersion": 4,
                    "candidateJobSha256": values.get(  # type: ignore[union-attr]
                        "candidateJobSha256", candidate[0]
                    ),
                    "candidateImage": values.get(  # type: ignore[union-attr]
                        "candidateImage", candidate[1]
                    ),
                    "allocations": 1,
                }
            if action == "app.remove":
                return {"jobAbsent": True, "variableAbsent": True}
            if action == "app.worker.delete":
                return {"absent": True}
            if action == "app.manifest.delete":
                return {"absent": True}
            if action == "app.manifest.retain":
                return {"protected": [image], "deleted": []}
            raise AssertionError((action, values))

        with mock.patch.object(operator.app, "check_public_health", return_value=True):
            with self.assertRaises(app.ApplicationError):
                self._deploy("demo-app", helper)
            result = self._deploy("demo-app", helper)
            self.assertEqual(result.nomad_version, 4)
            build_request = call_values["app.build"][-1]
            self.assertEqual(build_request["requestedRef"], "main")  # type: ignore[index]
            self.assertEqual(build_request["configurationRevision"], 1)  # type: ignore[index]
            self.assertNotIn("configPath", build_request)  # type: ignore[operator]
            environment_call = call_values["app.env.set"][-1]
            self.assertEqual(
                environment_call["updates"],  # type: ignore[index]
                {
                    "NODE_ENV": "production",
                    "PLATFORM_ENV": "production",
                    "PLATFORM_PROJECT_ID": environment_call["updates"][  # type: ignore[index]
                        "PLATFORM_PROJECT_ID"
                    ],
                    "PLATFORM_PROJECT_SLUG": "demo-app",
                    "PORT": "8080",
                },
            )
            connection = db.connect(self.state / "platform.sqlite3")
            try:
                application = db.get_application(connection, "demo-app")
                assert application is not None
                owners = {
                    item.key_name: item.owner
                    for item in db.list_environment_keys(
                        connection, application_id=application.application_id
                    )
                }
                self.assertEqual(
                    owners,
                    {name: "platform" for name in PLATFORM_ENVIRONMENT_KEYS},
                )
                attempts = db.list_deployment_attempts(connection, application.application_id)
                self.assertEqual(
                    (attempts[0].snapshot_kind, attempts[0].status),
                    ("strict", "succeeded"),
                )
                self.assertEqual(attempts[0].configuration_revision, 1)
            finally:
                connection.close()
            self.observe_flavor_mock.assert_called()
            self.assertLess(actions.index("flavor.verify"), actions.index("app.worker.create"))

        self.assertIn("app.builder.delete", actions)
        self.assertIn("app.manifest.retain", actions)
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            operation = connection.execute(
                "SELECT status FROM operations WHERE kind='app.deploy'"
            ).fetchone()
            self.assertEqual(operation["status"], "succeeded")
        finally:
            connection.close()

    def test_platform_environment_interruption_restores_exact_nonsecret_prior_values(self) -> None:
        args = operator.build_parser().parse_args(self.argv("status"))
        configured = operator._load_config(args)
        candidate = "storage.internal:5000/projects/demo-app/app@sha256:" + "e" * 64
        prior_image = "storage.internal:5000/projects/demo-app/app@sha256:" + "d" * 64
        prior = {
            "NODE_ENV": "production",
            "PLATFORM_ENV": "production",
            "PLATFORM_PROJECT_ID": APP_ID,
            "PLATFORM_PROJECT_SLUG": "demo-app",
            "PORT": "3000",
        }
        desired = {**prior, "PORT": "8080"}
        calls: list[tuple[str, object]] = []
        with operator._database(args) as connection:
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
            accept_deployment(
                connection,
                application_id=APP_ID,
                source_commit="a" * 40,
                recipe_hash="b" * 64,
                image_digest=prior_image,
                nomad_job='{"ID":"demo-app"}',
                nomad_job_sha256="c" * 64,
                nomad_version=7,
                health_path="/old-health",
                application_port=3000,
                build_log_path="logs/old.log",
            )
            operation_id = "00000000-0000-4000-8000-000000000099"
            db.begin_operation(
                connection,
                operation_id=operation_id,
                kind="app.deploy",
                scope=f"app-{APP_ID}",
                phase="platform_environment_mutating",
                deadline_at="2026-08-18T01:00:00Z",
                refs={
                    "application_id": APP_ID,
                    "slug": "demo-app",
                    "repository": "https://github.com/o/r",
                    "source_commit": "f" * 40,
                    "requested_ref": "main",
                    "configuration_revision": 0,
                    "configuration_sha256": "0" * 64,
                    "platform_key_names": sorted(PLATFORM_ENVIRONMENT_KEYS),
                    "desired_platform_values": desired,
                    "prior_platform_values": prior,
                },
            )
            db.checkpoint_operation(
                connection,
                operation_id,
                phase="platform_environment_mutating",
                candidate_digest=candidate,
            )
            operation = db.get_operation(connection, operation_id)
            assert operation is not None
            spec = app.DeploymentSpec(
                APP_ID,
                "demo-app",
                "https://github.com/o/r",
                "f" * 40,
                "main",
                0,
                "0" * 64,
                "one-vcpu",
                1000,
                2048,
            )

            def helper(
                _config: object, action: str, values: object, **_kwargs: object
            ) -> dict[str, object]:
                calls.append((action, values))
                if action == "app.env.set":
                    self.assertEqual(values["updates"], prior)  # type: ignore[index]
                    return {
                        "keys": sorted(prior),
                        "restarted": True,
                        "schedulerHealthy": True,
                        "publicHealthy": True,
                    }
                if action == "app.builder.delete":
                    return {"absent": True}
                raise AssertionError(action)

            service = deployment_service.DeploymentService(
                connection,
                configured,
                self.state,
                helper_caller=helper,
            )
            recovered = service.recover_operation(
                spec,
                operation,
                deadline=operator._command_deadline(configured),
            )
            self.assertIsNotNone(recovered.operation)
            self.assertIsNone(recovered.completed)
            owners = {
                item.key_name: item.owner
                for item in db.list_environment_keys(connection, application_id=APP_ID)
            }
        self.assertEqual(owners, {name: "platform" for name in prior})
        self.assertEqual(
            [action for action, _values in calls], ["app.env.set", "app.builder.delete"]
        )

    def test_update_worker_uses_inactive_bounded_slot_while_accepted_worker_remains(self) -> None:
        args = operator.build_parser().parse_args(self.argv("status"))
        configured = operator._load_config(args)
        operation_id = "00000000-0000-4000-8000-000000000099"
        connection = db.connect(self.state / "platform.sqlite3")
        db.migrate(connection)
        db.put_application(
            connection,
            application_id=APP_ID,
            application_slug="demo-app",
            worker_server_id="00000000-0000-4000-8000-000000000004",
            worker_server_name="example-worker-stable",
            worker_port_id="00000000-0000-4000-8000-000000000005",
            worker_port_name="example-worker-stable-v4",
            worker_flavor="one-vcpu",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=2048,
        )
        accept_deployment(
            connection,
            application_id=APP_ID,
            source_commit="a" * 40,
            recipe_hash="b" * 64,
            image_digest=DIGEST,
            nomad_job='job "demo-app" {\n}',
            nomad_version=3,
            build_log_path="logs/stable.log",
        )
        db.begin_operation(
            connection,
            operation_id=operation_id,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="validated",
            deadline_at="2026-08-18T01:00:00Z",
        )
        calls: list[tuple[str, object]] = []

        def helper(_config: object, action: str, values: object, **_kwargs: object):
            calls.append((action, values))
            return {
                "ready": True,
                "serverId": "00000000-0000-4000-8000-000000000006",
                "serverName": "example-worker-candidate",
                "portId": "00000000-0000-4000-8000-000000000007",
                "portName": "example-worker-candidate-v4",
            }

        worker = deployment_service._prepare_deployment_worker(
            connection,
            configured,
            helper_caller=helper,
            state_directory=self.state,
            operation_id=operation_id,
            application_id=APP_ID,
            application_slug="demo-app",
            worker_flavor="one-vcpu",
            candidate=DIGEST,
            refs={},
            deadline=operator._command_deadline(configured),
        )
        connection.close()
        self.assertEqual(worker.server_name, "example-worker-candidate")
        self.assertEqual(
            calls,
            [
                (
                    "app.worker.observe",
                    {
                        "applicationId": app.deployment_worker_ids(APP_ID)[1],
                        "slug": "demo-app",
                    },
                )
            ],
        )

    def test_confirmed_candidate_removal_is_terminal_and_candidate_is_cleaned(self) -> None:
        self._select_deployment_images()
        args = operator.build_parser().parse_args(self.argv("status"))
        manifest = app.Manifest("node", (".",), None, "serve", 8080, "/ready")
        recipe = app.generate_recipe(manifest, operator._load_config(args).policy.runtime_images)
        image = "storage.internal:5000/projects/failed-app/app@sha256:" + "e" * 64
        actions: list[str] = []
        fail_candidate_cleanup = [True]

        def helper(
            _config: object, action: str, _values: object, **_kwargs: object
        ) -> dict[str, object]:
            actions.append(action)
            if action == "app.build":
                return {
                    "image": image,
                    "recipeHash": recipe.sha256,
                    "manifest": {
                        "runtime": "node",
                        "packages": ["."],
                        "buildScript": None,
                        "startScript": "serve",
                        "port": 8080,
                        "healthPath": "/ready",
                    },
                    "log": "bounded\n",
                    "builderAbsent": True,
                }
            if action == "app.env.set":
                return {
                    "keys": sorted(PLATFORM_ENVIRONMENT_KEYS),
                    "modifyIndex": 2,
                    "restarted": False,
                    "schedulerHealthy": False,
                    "publicHealthy": False,
                }
            if action == "app.worker.observe":
                return {"absent": True}
            if action == "app.worker.create":
                return {
                    "ready": True,
                    "serverId": "00000000-0000-4000-8000-000000000004",
                    "serverName": "example-worker-failed",
                    "portId": "00000000-0000-4000-8000-000000000005",
                    "portName": "example-worker-failed-v4",
                }
            if action == "app.manifest.delete":
                if fail_candidate_cleanup[0]:
                    fail_candidate_cleanup[0] = False
                    raise app.ApplicationError("injected manifest cleanup interruption")
                return {"absent": True}
            raise AssertionError(action)

        with (
            mock.patch.object(
                operator.app,
                "deploy_and_cleanup",
                side_effect=app.DeploymentFailed(
                    "candidate unhealthy; candidate removal completed", cleanup_succeeded=True
                ),
            ) as deploy,
        ):
            with self.assertRaises(app.ApplicationError):
                self._deploy("failed-app", helper)
            recovered = self._deploy("failed-app", helper)
            self.assertEqual(recovered.recovered, "candidate-removed")
        self.assertEqual(actions.count("app.build"), 1)
        self.assertEqual(actions.count("app.manifest.delete"), 2)
        deploy.assert_called_once()
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            operation = connection.execute(
                "SELECT status, phase, cleanup_state FROM operations WHERE kind='app.deploy'"
            ).fetchone()
            self.assertEqual(tuple(operation), ("failed", "candidate_removed", "confirmed"))
        finally:
            connection.close()

    def test_status_power_and_replace_use_completion_apis(self) -> None:
        status_args = operator.build_parser().parse_args(self.argv("status"))
        model = {
            "state": "healthy",
            "accepted": {
                "infrastructureRoles": 0,
                "applications": 0,
                "storageResources": 0,
            },
            "observations": {"available": 0, "unavailable": 0, "unhealthy": 0},
        }
        with mock.patch.object(operator.status, "status_show_live", return_value=model) as live:
            operator.dispatch(status_args, stdout=StringIO())
        live.assert_called_once()

        power_args = operator.build_parser().parse_args(self.argv("infra", "start", "admin"))
        powered = openstack.PowerResult(
            "admin",
            "00000000-0000-4000-8000-000000000006",
            "ACTIVE",
        )
        with mock.patch.object(operator.openstack, "power_host", return_value=powered) as power:
            operator.dispatch(power_args, stdout=StringIO())
        power.assert_called_once()
        self.assertTrue(callable(power.call_args.kwargs["health_check"]))
        self.assertTrue(callable(power.call_args.kwargs["checkpoint"]))

        replace_args = operator.build_parser().parse_args(
            self.argv("infra", "replace", "ingress", "--yes")
        )
        with operator._database(replace_args) as connection:
            db.put_image_selection(
                connection,
                role="ingress",
                image_id="00000000-0000-4000-8000-000000000007",
                display_name="ingress-image",
                source_commit="b" * 40,
                compatibility_hash="c" * 64,
            )
        replaced = openstack.ReplacementResult(
            "ingress",
            True,
            "00000000-0000-4000-8000-000000000008",
            "00000000-0000-4000-8000-000000000007",
            "confirmed",
        )

        def replace(*_args: object, **kwargs: object) -> openstack.ReplacementResult:
            self.assertIsNone(kwargs["user_data_path"])
            checkpoint = kwargs["checkpoint"]
            refs = {
                "role": "ingress",
                "old_server_id": "00000000-0000-4000-8000-000000000006",
                "replacement_server_id": replaced.active_server_id,
                "selected_image_id": replaced.selected_image_id,
                "lifecycle_observations": {
                    "old_host_retained_until_ready": True,
                    "exact_identity_verified": True,
                },
                "cleanup_state": "confirmed",
            }
            checkpoint("accepted", refs)  # type: ignore[operator]
            checkpoint("complete", refs)  # type: ignore[operator]
            return replaced

        output = StringIO()
        with mock.patch.object(
            operator.openstack, "replace_host", side_effect=replace
        ) as replace_call:
            operator.dispatch(replace_args, stdout=output)
        replace_call.assert_called_once()
        evidence = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(
            evidence["observations"],
            {"oldHostRetainedUntilReady": True, "exactIdentityVerified": True},
        )
        self.assertNotIn("dataRetained", evidence["observations"])
        self.assertTrue(callable(replace_call.call_args.kwargs["health_check"]))

    def test_replacement_lifecycle_evidence_rejects_each_falsified_observation(self) -> None:
        refs = {
            "role": "ingress",
            "old_server_id": "00000000-0000-4000-8000-000000000006",
            "replacement_server_id": "00000000-0000-4000-8000-000000000008",
            "selected_image_id": "00000000-0000-4000-8000-000000000007",
            "lifecycle_observations": {
                "old_host_retained_until_ready": True,
                "exact_identity_verified": True,
            },
        }
        for field in ("old_host_retained_until_ready", "exact_identity_verified"):
            falsified = json.loads(json.dumps(refs))
            falsified["lifecycle_observations"][field] = False
            operation = db.Operation(
                "00000000-0000-4000-8000-000000000099",
                "infra.replace",
                "infrastructure",
                "succeeded",
                "complete",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:01:00Z",
                "2026-01-01T01:00:00Z",
                falsified,
                None,
                None,
                "confirmed",
            )
            with self.subTest(field=field), self.assertRaises(openstack.RecoveryRequired):
                operator._replacement_observation(operation, "ingress")

    def test_interrupted_reboot_recovers_by_observation_without_replaying_action(self) -> None:
        args = operator.build_parser().parse_args(self.argv("infra", "reboot", "ingress", "--yes"))
        server_id = "00000000-0000-4000-8000-000000000006"

        def interrupted(*_args: object, **kwargs: object) -> openstack.PowerResult:
            checkpoint = kwargs["checkpoint"]
            checkpoint(  # type: ignore[operator]
                "power_requested",
                {
                    "role": "ingress",
                    "action": "reboot",
                    "server_id": server_id,
                    "baseline_ready_markers": 1,
                    "baseline_failure_markers": 0,
                },
            )
            raise openstack.OpenStackError("fixed interrupted reboot")

        with mock.patch.object(operator.openstack, "power_host", side_effect=interrupted):
            with self.assertRaises(openstack.OpenStackError):
                operator.dispatch(args, stdout=StringIO())

        recovered = openstack.PowerResult(
            "ingress",
            server_id,
            "ACTIVE",
        )
        with (
            mock.patch.object(operator.openstack, "power_host") as replay,
            mock.patch.object(
                operator.openstack, "recover_power_host", return_value=recovered
            ) as observe,
        ):
            operator.dispatch(args, stdout=StringIO())
        replay.assert_not_called()
        observe.assert_called_once()
        self.assertEqual(observe.call_args.kwargs["refs"]["server_id"], server_id)


class HelperIntegrationTests(unittest.TestCase):
    def test_manifest_exactly_matches_lazy_production_handlers(self) -> None:
        manifest = tuple(
            line
            for line in (Path(__file__).parents[1] / "openstack_platform/helper/actions-v1.txt")
            .read_text()
            .splitlines()
            if line and not line.startswith("#")
        )
        handlers = default_handlers()
        self.assertEqual(manifest, ACTION_MANIFEST)
        self.assertEqual(tuple(sorted(handlers)), manifest)
        self.assertTrue(all(callable(handler) for handler in handlers.values()))


if __name__ == "__main__":
    unittest.main()
