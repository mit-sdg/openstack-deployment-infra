from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from platform_cli import app, cli, db, openstack, remote, status
from platform_cli.helper.main import default_handlers
from platform_cli.helper.production import ACTION_MANIFEST
from platform_cli.validation import ValidationError

APP_ID = "00000000-0000-4000-8000-000000000001"
IMAGE_ID = "00000000-0000-4000-8000-000000000002"
DIGEST = "registry.example/runtime/image@sha256:" + "a" * 64
RECIPIENT = "age1" + "q" * 58


class CliIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.platform = self.root / "platform.json"
        self.policy = self.root / "policy.json"
        self.platform.write_text(
            json.dumps(
                {
                    "project": "example-project",
                    "projectId": APP_ID,
                    "prefix": "example",
                    "namespace": "app-platform",
                    "domain": "apps.example.com",
                    "datacenter": "dc1",
                    "region": "global",
                    "network": "private",
                    "hosts": {"admin": "admin", "ingress": "ingress", "storage": "storage"},
                    "ports": {
                        "admin": "admin-port",
                        "ingress": "ingress-port",
                        "storage": "storage-port",
                    },
                    "paths": {"root": "/srv/app-platform"},
                    "addresses": {"storage": "storage.internal"},
                    "flavors": {"builder": "builder-standard"},
                    "images": {
                        "admin": "admin",
                        "ingress": "ingress",
                        "storage": "storage",
                        "worker": "worker",
                        "builder": "builder",
                    },
                    "pki": {"internalCaFile": "internal-ca.pem"},
                }
            )
        )
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
            cli.openstack,
            "verify_project",
            return_value=openstack.ProjectIdentity(APP_ID, "example-project"),
        )
        self.verify_project_mock = self.verify_project.start()
        self.addCleanup(self.verify_project.stop)
        self.observe_flavor = mock.patch.object(
            cli.openstack,
            "observe_flavor",
            return_value=openstack.Flavor(IMAGE_ID, "one-vcpu", 1, 2048),
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

    def test_unexpected_management_failure_writes_private_correlation_diagnostic(self) -> None:
        sentinel = "sentinel-management-exception"
        stderr = StringIO()
        with (
            mock.patch.object(cli, "dispatch", side_effect=RuntimeError(sentinel)),
            mock.patch("sys.stderr", new=stderr),
        ):
            result = cli.main(
                [
                    "--state-directory",
                    str(self.state),
                    "status",
                ]
            )
        self.assertEqual(result, cli.EXIT_ERROR)
        self.assertIn("correlation ID", stderr.getvalue())
        self.assertNotIn(sentinel, stderr.getvalue())
        diagnostics = list((self.state / "diagnostics").glob("*.trace"))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].stat().st_mode & 0o777, 0o600)
        payload = diagnostics[0].read_text()
        self.assertIn("platform_cli/cli.py:", payload)
        self.assertNotIn(sentinel, payload)

    def test_remote_dependency_unavailable_exits_four(self) -> None:
        with (
            mock.patch.object(
                cli,
                "dispatch",
                side_effect=remote.DependencyUnavailable("helper unavailable"),
            ),
            mock.patch("sys.stderr", new=StringIO()) as stderr,
        ):
            self.assertEqual(cli.main(["status"]), cli.EXIT_UNAVAILABLE)
        self.assertIn("unavailable:", stderr.getvalue())

    def test_complete_command_tree_parses(self) -> None:
        commands = (
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
            ("app", "create", "demo-app"),
            ("app", "deploy", "demo-app", "--repo", "https://github.com/o/r", "--commit", "a" * 40),
            ("app", "remove", "demo-app", "--confirm", "demo-app"),
            ("app", "list"),
            ("app", "show", "demo-app"),
            ("app", "logs", "demo-app", "--runtime"),
            ("app", "env", "set", "demo-app", "MODE"),
            ("app", "env", "unset", "demo-app", "MODE"),
            ("app", "env", "import", "demo-app", "--file", "-"),
            ("app", "env", "list", "demo-app"),
            ("storage", "list"),
            ("storage", "show", "demo-app", "postgres"),
            ("storage", "create", "demo-app", "postgres", "s3"),
            ("storage", "verify", "demo-app"),
            ("storage", "rotate", "demo-app", "mongo"),
            ("storage", "remove", "demo-app", "s3", "--confirm", "demo-app"),
        )
        parser = cli.build_parser()
        for command in commands:
            with self.subTest(command=command):
                parser.parse_args(self.argv(*command))

    def test_status_initializes_private_database_and_renders_table(self) -> None:
        output = StringIO()
        cli.dispatch(cli.build_parser().parse_args(self.argv("status")), stdout=output)
        self.assertIn("STATE", output.getvalue())
        database = self.state / "platform.sqlite3"
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        connection = db.connect(database)
        try:
            self.assertEqual(db.schema_version(connection), 4)
        finally:
            connection.close()

    def test_fresh_status_counts_only_expected_persistent_dependencies(self) -> None:
        args = cli.build_parser().parse_args(self.argv("status"))
        with cli._database(args) as connection:
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
        args = cli.build_parser().parse_args(self.argv("infra", "image", "set", "worker", IMAGE_ID))
        output = StringIO()
        with mock.patch.object(cli.openstack, "select_image", return_value=selected):
            cli.dispatch(args, stdout=output)
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
        args = cli.build_parser().parse_args(self.argv("infra", "image", "set", "worker", IMAGE_ID))
        configured = cli._load_config(args)
        operation_id = "00000000-0000-4000-8000-000000000099"
        with cli._database(args) as connection:
            db.begin_operation(
                connection,
                operation_id=operation_id,
                kind="infra.image.set",
                scope="infrastructure",
                phase="selection_observed",
                deadline_at=cli._deadline(configured),
                refs={
                    "role": "worker",
                    "requested_image": IMAGE_ID,
                    "image_id": IMAGE_ID,
                    "display_name": "worker-image",
                    "source_commit": "b" * 40,
                    "compatibility_hash": "c" * 64,
                },
            )
        with mock.patch.object(cli.openstack, "select_image") as select:
            cli.dispatch(args, stdout=StringIO())
        select.assert_not_called()
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            self.assertEqual(db.get_operation(connection, operation_id).status, "succeeded")  # type: ignore[union-attr]
            self.assertEqual(db.get_image_selection(connection, "worker").image_id, IMAGE_ID)  # type: ignore[union-attr]
        finally:
            connection.close()

    def test_prune_apply_recovers_exact_checkpoint_without_self_protecting_candidates(self) -> None:
        args = cli.build_parser().parse_args(
            self.argv("infra", "image", "prune", "--apply", "--yes")
        )
        configured = cli._load_config(args)
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
        with cli._database(args) as connection:
            db.begin_operation(
                connection,
                operation_id=operation_id,
                kind="infra.image.prune.apply",
                scope="infrastructure",
                phase="image_deleting",
                deadline_at=cli._deadline(configured),
                refs=refs,
            )
            db.mark_recovery_required(connection, operation_id, "injected interruption")
        recovered = openstack.PruneRecoveryResult(
            "continue", (IMAGE_ID,), (IMAGE_ID,), (), "d" * 64
        )
        with mock.patch.object(
            cli.openstack, "recover_image_prune", return_value=recovered
        ) as recover:
            cli.dispatch(args, stdout=StringIO())
        self.assertEqual(recover.call_args.kwargs["operation_image_ids"], ())
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            self.assertEqual(db.get_operation(connection, operation_id).status, "succeeded")  # type: ignore[union-attr]
        finally:
            connection.close()

    def test_created_application_is_inert_and_lets_storage_precede_deployment(self) -> None:
        """An application that reads a database at startup needs one first.

        Only a deployment used to create an application row, and managed
        storage needs that row to exist, so such an application could never
        pass the health check its first deployment demanded.
        """
        output = StringIO()
        cli.dispatch(
            cli.build_parser().parse_args(self.argv("app", "create", "demo-app")), stdout=output
        )
        self.assertIn("slug=demo-app", output.getvalue())

        connection = db.connect(self.state / "platform.sqlite3")
        try:
            application = db.get_application(connection, "demo-app")
            self.assertIsNotNone(application)
            assert application is not None
            # Declared, not running: nothing is deployed yet, so the platform
            # must not report the application as unavailable.
            self.assertFalse(application.desired_running)
            self.assertIsNone(application.repository_url)
            self.assertIsNone(application.worker_server_id)
            self.assertEqual(application.url, "https://demo-app.apps.example.com")
        finally:
            connection.close()

        # The slug now resolves, which is all managed storage and staff
        # environment values require.
        duplicate = cli.build_parser().parse_args(self.argv("app", "create", "demo-app"))
        with self.assertRaisesRegex(ValidationError, "already exists"):
            cli.dispatch(duplicate, stdout=StringIO())

    def test_environment_value_never_enters_output_or_sqlite(self) -> None:
        args = cli.build_parser().parse_args(self.argv("app", "env", "set", "demo-app", "MODE"))
        with cli._database(args) as connection:
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
        sentinel = "sentinel-secret-environment-value"
        output = StringIO()
        with mock.patch.object(
            cli.app,
            "set_environment",
            return_value={"keys": ["MODE"], "modifyIndex": 4},
        ) as invoked:
            cli.dispatch(args, stdin=StringIO(sentinel + "\n"), stdout=output)
        self.assertEqual(invoked.call_args.args[1], {"MODE": sentinel})
        self.assertNotIn(sentinel, output.getvalue())
        self.assertNotIn(sentinel.encode(), (self.state / "platform.sqlite3").read_bytes())
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            keys = db.list_environment_keys(connection, application_id=APP_ID)
            self.assertEqual([(item.key_name, item.owner) for item in keys], [("MODE", "staff")])
        finally:
            connection.close()

    def test_environment_retry_observes_and_closes_interrupted_phase(self) -> None:
        args = cli.build_parser().parse_args(self.argv("app", "env", "set", "demo-app", "MODE"))
        configured = cli._load_config(args)
        with cli._database(args) as connection:
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
            interrupted = cli._begin(
                connection,
                configured,
                kind="app.env.set",
                scope=f"app-{APP_ID}",
                phase="intent_recorded",
                refs={"key_names": ["MODE"], "mutation": "set"},
            )
            db.mark_recovery_required(connection, interrupted, "injected interruption")
        with (
            mock.patch.object(
                cli.app,
                "list_environment",
                return_value={"keys": ["MODE"], "modifyIndex": 3},
            ) as observed,
            mock.patch.object(
                cli.app,
                "set_environment",
                return_value={"keys": ["MODE"], "modifyIndex": 4},
            ),
        ):
            cli.dispatch(args, stdin=StringIO("new-value\n"), stdout=StringIO())
        observed.assert_called_once()
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            rows = list(
                connection.execute(
                    "SELECT status FROM operations WHERE kind='app.env.set' ORDER BY rowid"
                )
            )
            self.assertEqual([row["status"] for row in rows], ["failed", "succeeded"])
        finally:
            connection.close()

    def test_bounded_file_reader_rejects_symlinks_and_oversized_inputs(self) -> None:
        source = self.root / "source.env"
        source.write_bytes(b"12345")
        self.assertEqual(
            cli._read_bounded_file(source, maximum=5, field="environment file"),
            b"12345",
        )
        link = self.root / "linked.env"
        link.symlink_to(source)
        with self.assertRaisesRegex(ValidationError, "direct regular file"):
            cli._read_bounded_file(link, maximum=5, field="environment file")
        with self.assertRaisesRegex(ValidationError, "bounded"):
            cli._read_bounded_file(source, maximum=4, field="environment file")

    def test_log_line_bounds_are_shared_by_host_and_application_logs(self) -> None:
        parser = cli.build_parser()
        for command in (
            ("infra", "logs", "admin", "--lines", "0"),
            ("infra", "logs", "admin", "--lines", "2001"),
            ("app", "logs", "demo-app", "--build", "--lines", "-1"),
        ):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(self.argv(*command))

    def test_wrong_project_refuses_environment_before_secret_or_helper_mutation(self) -> None:
        args = cli.build_parser().parse_args(self.argv("app", "env", "set", "demo-app", "MODE"))
        with cli._database(args) as connection:
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
        secret = StringIO("must-not-be-read\n")
        with (
            mock.patch.object(
                cli.openstack,
                "verify_project",
                side_effect=openstack.OpenStackError("wrong project"),
            ),
            mock.patch.object(cli.app, "set_environment") as mutate,
            self.assertRaisesRegex(openstack.OpenStackError, "wrong project"),
        ):
            cli.dispatch(args, stdin=secret)
        mutate.assert_not_called()
        self.assertEqual(secret.tell(), 0)

    def test_platform_ownership_transfer_preserves_unrelated_staff_keys(self) -> None:
        args = cli.build_parser().parse_args(self.argv("status"))
        with cli._database(args) as connection:
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
            cli._record_platform_environment_ownership(
                connection,
                APP_ID,
                sorted(cli._PLATFORM_ENVIRONMENT),
            )
            ownership = {
                item.key_name: item.owner
                for item in db.list_environment_keys(connection, application_id=APP_ID)
            }
        self.assertEqual(ownership["NODE_ENV"], "platform")
        self.assertEqual(ownership["STAFF_SENTINEL"], "staff")

    def test_staff_environment_commands_cannot_modify_node_env(self) -> None:
        status_args = cli.build_parser().parse_args(self.argv("status"))
        with cli._database(status_args) as connection:
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
        commands = (
            (("app", "env", "set", "demo-app", "NODE_ENV"), "sentinel-development\n"),
            (("app", "env", "unset", "demo-app", "NODE_ENV"), ""),
            (("app", "env", "import", "demo-app", "--file", "-"), "NODE_ENV=staging\n"),
        )
        for command, payload in commands:
            args = cli.build_parser().parse_args(self.argv(*command))
            with (
                self.subTest(command=command),
                mock.patch.object(cli.app, "set_environment") as set_values,
                mock.patch.object(cli.app, "remove_environment") as remove_values,
                self.assertRaisesRegex(ValidationError, "reserved") as raised,
            ):
                cli.dispatch(args, stdin=StringIO(payload))
            set_values.assert_not_called()
            remove_values.assert_not_called()
            self.assertNotIn("sentinel-development", str(raised.exception))

    def test_app_remove_deletes_current_and_prior_manifest_history(self) -> None:
        prior = "registry.example/runtime/image@sha256:" + "b" * 64
        args = cli.build_parser().parse_args(
            self.argv("app", "remove", "demo-app", "--confirm", "demo-app")
        )
        with cli._database(args) as connection:
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
            db.accept_deployment(
                connection,
                application_id=APP_ID,
                source_commit="a" * 40,
                recipe_hash="b" * 64,
                image_digest=DIGEST,
                nomad_job="job",
                nomad_version=4,
                build_log_path="logs/build.log",
            )
            operation_id = "00000000-0000-4000-8000-000000000099"
            db.begin_operation(
                connection,
                operation_id=operation_id,
                kind="app.deploy",
                scope=f"app-{APP_ID}",
                phase="accepted",
                deadline_at="2026-08-18T01:00:00Z",
            )
            db.checkpoint_operation(
                connection,
                operation_id,
                phase="accepted",
                candidate_digest=prior,
            )
            db.mark_succeeded(connection, operation_id)
        deleted: list[str] = []

        def helper(
            _config: object, action: str, values: dict[str, object], **_kwargs: object
        ) -> dict[str, object]:
            if action == "app.remove":
                return {"jobAbsent": True, "variableAbsent": True}
            if action == "app.worker.delete":
                return {"absent": True}
            if action == "app.manifest.delete":
                deleted.append(str(values["image"]))
                return {"absent": True}
            raise AssertionError(action)

        with mock.patch.object(cli, "_helper", side_effect=helper):
            cli.dispatch(args, stdout=StringIO())
        self.assertEqual(set(deleted), {DIGEST, prior})

    def _select_deployment_images(self) -> None:
        args = cli.build_parser().parse_args(self.argv("status"))
        with cli._database(args) as connection:
            for role, suffix in (("builder", "2"), ("worker", "3")):
                db.put_image_selection(
                    connection,
                    role=role,
                    image_id=f"00000000-0000-4000-8000-00000000000{suffix}",
                    display_name=f"{role}-image",
                    source_commit="b" * 40,
                    compatibility_hash="c" * 64,
                )

    def test_deploy_recovers_builder_phase_and_remove_recovers_worker_phase(self) -> None:
        self._select_deployment_images()
        deploy_args = cli.build_parser().parse_args(
            self.argv(
                "app",
                "deploy",
                "demo-app",
                "--repo",
                "https://github.com/o/r",
                "--commit",
                "a" * 40,
            )
        )
        manifest = app.Manifest("node", (".",), None, "serve", 8080, "/ready")
        recipe = app.generate_recipe(
            manifest,
            cli._load_config(deploy_args).policy.runtime_images,
        )
        image = "storage.internal:5000/projects/demo-app/app@sha256:" + "d" * 64
        actions: list[str] = []
        call_values: dict[str, list[object]] = {}
        self.observe_flavor_mock.side_effect = lambda *_args, **_kwargs: (
            actions.append("flavor.verify") or openstack.Flavor(IMAGE_ID, "one-vcpu", 1, 2048)
        )
        fail_build = [True]
        fail_worker_remove = [True]

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
                if fail_worker_remove[0]:
                    fail_worker_remove[0] = False
                    raise app.ApplicationError("bounded worker interruption")
                return {"absent": True}
            if action == "app.manifest.delete":
                return {"absent": True}
            if action == "app.manifest.retain":
                return {"protected": [image], "deleted": []}
            raise AssertionError((action, values))

        with (
            mock.patch.object(cli, "_helper", side_effect=helper),
            mock.patch.object(cli.app, "check_public_health", return_value=True),
        ):
            with self.assertRaises(app.ApplicationError):
                cli.dispatch(deploy_args)
            output = StringIO()
            cli.dispatch(deploy_args, stdout=output)
            self.assertIn("nomad-version=4", output.getvalue())
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
                    {name: "platform" for name in cli._PLATFORM_ENVIRONMENT},
                )
            finally:
                connection.close()
            self.observe_flavor_mock.assert_called()
            self.assertLess(actions.index("flavor.verify"), actions.index("app.worker.create"))

            remove_args = cli.build_parser().parse_args(
                self.argv("app", "remove", "demo-app", "--confirm", "demo-app")
            )
            with self.assertRaises(app.ApplicationError):
                cli.dispatch(remove_args)
            cli.dispatch(remove_args, stdout=output)

        self.assertIn("app.builder.delete", actions)
        self.assertIn("app.manifest.retain", actions)
        self.assertGreaterEqual(actions.count("app.remove"), 2)
        connection = db.connect(self.state / "platform.sqlite3")
        try:
            self.assertIsNone(db.get_application(connection, "demo-app"))
            statuses = {
                row["kind"]: row["status"]
                for row in connection.execute("SELECT kind, status FROM operations")
            }
            self.assertEqual(statuses["app.deploy"], "succeeded")
            self.assertEqual(statuses["app.remove"], "succeeded")
        finally:
            connection.close()

    def test_platform_environment_interruption_restores_exact_nonsecret_prior_values(self) -> None:
        args = cli.build_parser().parse_args(self.argv("status"))
        configured = cli._load_config(args)
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
        with cli._database(args) as connection:
            db.put_application(
                connection,
                application_id=APP_ID,
                application_slug="demo-app",
                worker_flavor="one-vcpu",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )
            db.accept_deployment(
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
                    "config_path": "platform.yaml",
                    "platform_key_names": sorted(cli._PLATFORM_ENVIRONMENT),
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
                "platform.yaml",
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

            with mock.patch.object(cli, "_helper", side_effect=helper):
                recovered = cli._recover_app_deployment(
                    connection,
                    configured,
                    spec,
                    operation,
                    deadline=cli._command_deadline(configured),
                    output=StringIO(),
                )
            self.assertIsNotNone(recovered)
            owners = {
                item.key_name: item.owner
                for item in db.list_environment_keys(connection, application_id=APP_ID)
            }
        self.assertEqual(owners, {name: "platform" for name in prior})
        self.assertEqual(
            [action for action, _values in calls], ["app.env.set", "app.builder.delete"]
        )

    def test_confirmed_candidate_removal_is_terminal_and_candidate_is_cleaned(self) -> None:
        self._select_deployment_images()
        args = cli.build_parser().parse_args(
            self.argv(
                "app",
                "deploy",
                "failed-app",
                "--repo",
                "https://github.com/o/r",
                "--commit",
                "a" * 40,
            )
        )
        manifest = app.Manifest("node", (".",), None, "serve", 8080, "/ready")
        recipe = app.generate_recipe(manifest, cli._load_config(args).policy.runtime_images)
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
                    "keys": sorted(cli._PLATFORM_ENVIRONMENT),
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
            mock.patch.object(cli, "_helper", side_effect=helper),
            mock.patch.object(
                cli.app,
                "deploy_and_cleanup",
                side_effect=app.DeploymentFailed(
                    "candidate unhealthy; candidate removal completed", cleanup_succeeded=True
                ),
            ) as deploy,
        ):
            with self.assertRaises(app.ApplicationError):
                cli.dispatch(args)
            cli.dispatch(args, stdout=StringIO())
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
        status_args = cli.build_parser().parse_args(self.argv("status"))
        model = {
            "state": "healthy",
            "accepted": {
                "infrastructureRoles": 0,
                "applications": 0,
                "storageResources": 0,
            },
            "observations": {"available": 0, "unavailable": 0, "unhealthy": 0},
        }
        with mock.patch.object(cli.status, "status_show_live", return_value=model) as live:
            cli.dispatch(status_args, stdout=StringIO())
        live.assert_called_once()

        power_args = cli.build_parser().parse_args(self.argv("infra", "start", "admin"))
        powered = openstack.PowerResult(
            "admin",
            "00000000-0000-4000-8000-000000000006",
            "start",
            "ACTIVE",
            ("guest:admin-services-ready",),
        )
        with mock.patch.object(cli.openstack, "power_host", return_value=powered) as power:
            cli.dispatch(power_args, stdout=StringIO())
        power.assert_called_once()
        self.assertTrue(callable(power.call_args.kwargs["health_check"]))
        self.assertTrue(callable(power.call_args.kwargs["checkpoint"]))

        replace_args = cli.build_parser().parse_args(
            self.argv("infra", "replace", "ingress", "--yes")
        )
        with cli._database(replace_args) as connection:
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
            "00000000-0000-4000-8000-000000000009",
            "confirmed",
            ("guest:ingress-services-ready",),
        )

        def replace(*_args: object, **kwargs: object) -> openstack.ReplacementResult:
            self.assertIsNone(kwargs["user_data_path"])
            checkpoint = kwargs["checkpoint"]
            checkpoint("observed", {"role": "ingress"})  # type: ignore[operator]
            return replaced

        with mock.patch.object(cli.openstack, "replace_host", side_effect=replace) as replace_call:
            cli.dispatch(replace_args, stdout=StringIO())
        replace_call.assert_called_once()
        self.assertTrue(callable(replace_call.call_args.kwargs["health_check"]))

    def test_interrupted_reboot_recovers_by_observation_without_replaying_action(self) -> None:
        args = cli.build_parser().parse_args(self.argv("infra", "reboot", "ingress", "--yes"))
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

        with mock.patch.object(cli.openstack, "power_host", side_effect=interrupted):
            with self.assertRaises(openstack.OpenStackError):
                cli.dispatch(args, stdout=StringIO())

        recovered = openstack.PowerResult(
            "ingress",
            server_id,
            "reboot",
            "ACTIVE",
            ("nova:active", "guest:ingress-services-ready"),
        )
        with (
            mock.patch.object(cli.openstack, "power_host") as replay,
            mock.patch.object(
                cli.openstack, "recover_power_host", return_value=recovered
            ) as observe,
        ):
            cli.dispatch(args, stdout=StringIO())
        replay.assert_not_called()
        observe.assert_called_once()
        self.assertEqual(observe.call_args.kwargs["refs"]["server_id"], server_id)


class HelperIntegrationTests(unittest.TestCase):
    def test_manifest_exactly_matches_lazy_production_handlers(self) -> None:
        manifest = tuple(
            line
            for line in (Path(__file__).parents[1] / "platform_cli/helper/actions-v1.txt")
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
