from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from platform_cli import db
from platform_cli.config import load_platform
from platform_cli.deployment_config import parse_configuration
from platform_cli.validation import ValidationError

APP_ID = "00000000-0000-4000-8000-000000000001"
IMAGE_ID = "00000000-0000-4000-8000-000000000002"
DIGEST = "registry.example/projects/demo/app@sha256:" + "a" * 64
DEPLOYMENT_ID = "00000000-0000-4000-8000-000000000004"
REQUEST_ID = "00000000-0000-4000-8000-000000000005"
SECOND_REQUEST_ID = "00000000-0000-4000-8000-000000000006"


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "platform.sqlite3"
        self.connection = db.connect(self.path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def migrate(self) -> None:
        db.migrate(self.connection)

    def add_application(self) -> None:
        db.put_application(
            self.connection,
            application_id=APP_ID,
            application_slug="demo-app",
            repository_url="https://github.com/example/demo",
            config_path="platform.yaml",
            worker_flavor="example.1c2g",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=2048,
            now="2026-01-01T00:00:00Z",
        )

    def test_fresh_and_incremental_migrations_create_exact_product_schema(self) -> None:
        db.migrate(self.connection, target_version=1)
        self.assertEqual(db.schema_version(self.connection), 1)
        self.assertEqual(
            {
                row[0]
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if not row[0].startswith("sqlite_")
            },
            {"schema_migrations", "image_selections", "applications"},
        )
        db.migrate(self.connection)
        self.assertEqual(db.schema_version(self.connection), 6)
        tables = {
            row[0]
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith("sqlite_")
        }
        self.assertEqual(
            tables,
            {
                "schema_migrations",
                "image_selections",
                "applications",
                "deployment_attempts",
                "active_deployments",
                "managed_resources",
                "environment_keys",
                "operations",
                "idempotency_requests",
                "environment_revisions",
                "application_slug_tombstones",
            },
        )
        schema_sql = "\n".join(
            row[0] or ""
            for row in self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        )
        self.assertNotIn("class", schema_sql.lower())
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_named_resource_migration_preserves_default_provider_identity(self) -> None:
        db.migrate(self.connection, target_version=4)
        self.add_application()
        self.connection.execute(
            "INSERT INTO managed_resources VALUES (?, 'mongo', ?, ?, 'active', NULL, 1024, NULL, NULL, NULL, ?, ?)",
            (APP_ID, "provider-id", "p_existing", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        self.connection.execute(
            "INSERT INTO environment_keys VALUES (?, 'MONGODB_URI', 'mongo')", (APP_ID,)
        )
        db.migrate(self.connection)
        resource = db.list_managed_resources(self.connection, application_id=APP_ID)[0]
        self.assertEqual((resource.resource_type, resource.resource_name), ("mongo", "default"))
        self.assertEqual(
            (resource.provider_id, resource.provider_name), ("provider-id", "p_existing")
        )
        self.assertEqual(resource.display_label, "default")
        self.assertEqual(str(uuid.UUID(resource.resource_id)), resource.resource_id)
        key = db.list_environment_keys(self.connection, application_id=APP_ID)[0]
        self.assertEqual((key.key_name, key.owner), ("MONGODB_URI", "storage.mongo.default"))

    def test_external_schema_is_rejected_before_greenfield_migration(self) -> None:
        self.connection.close()
        raw = sqlite3.connect(self.path)
        raw.execute("CREATE TABLE retained_external_state(value TEXT)")
        raw.commit()
        raw.close()
        self.path.chmod(0o600)
        with self.assertRaises(db.MigrationError):
            db.connect(self.path)

    def test_explicit_greenfield_marker_survives_incremental_upgrade(self) -> None:
        db.migrate(self.connection, target_version=1)
        self.assertEqual(db.schema_version(self.connection), 1)
        db.migrate(self.connection)
        self.assertEqual(db.schema_version(self.connection), 6)
        self.assertEqual(
            self.connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = 0"
            ).fetchone()[0],
            db._GREENFIELD_MARKER_CHECKSUM,
        )

    def test_bound_deployment_marker_rejects_copied_state_and_survives_upgrade(self) -> None:
        platform = load_platform(Path(__file__).parents[1] / "config/platform.example.json")
        identity = db.deployment_identity(platform)
        self.connection.close()
        self.path.unlink()

        connection = db.connect(self.path, identity=identity)
        db.migrate(connection, target_version=1, identity=identity)
        marker = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 0"
        ).fetchone()[0]
        self.assertNotEqual(marker, db._GREENFIELD_MARKER_CHECKSUM)
        connection.close()

        same_deployment = db.connect(self.path, identity=identity)
        db.migrate(same_deployment, identity=identity)
        self.assertEqual(db.schema_version(same_deployment), 6)
        same_deployment.close()

        wrong_project = db.deployment_identity(replace(platform, project_id=IMAGE_ID))
        with self.assertRaisesRegex(db.MigrationError, "different deployment identity"):
            db.connect(self.path, identity=wrong_project)
        wrong_namespace = db.deployment_identity(replace(platform, namespace="other-platform"))
        with self.assertRaisesRegex(db.MigrationError, "different deployment identity"):
            db.connect(self.path, identity=wrong_namespace)

    def test_legacy_accepted_deployment_migration_preserves_exact_job_identity(self) -> None:
        db.migrate(self.connection, target_version=2)
        self.add_application()
        job_identity = "d" * 64
        job = f'job "demo-app" {{\n  meta {{\n    m1_candidate_job_sha256 = "{job_identity}"\n  }}\n}}\n'
        self.connection.execute(
            "INSERT INTO deployments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                APP_ID,
                "a" * 40,
                "b" * 64,
                DIGEST,
                job,
                4,
                "logs/build.log",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                job_identity,
                "/",
                8080,
            ),
        )
        db.migrate(self.connection)
        deployment = db.get_deployment(self.connection, APP_ID)
        assert deployment is not None
        self.assertEqual(deployment.nomad_job_sha256, job_identity)
        self.assertEqual(deployment.health_path, "/")
        self.assertEqual(deployment.application_port, 8080)
        attempt = db.list_deployment_attempts(self.connection, APP_ID)[0]
        self.assertEqual((attempt.status, attempt.snapshot_kind), ("succeeded", "legacy"))
        self.assertIsNone(attempt.configuration)
        self.assertIsNone(attempt.requested_ref)
        self.assertIsNone(attempt.idempotency_request_id)
        self.assertEqual(
            db.get_active_deployment(self.connection, APP_ID).deployment_id,  # type: ignore[union-attr]
            attempt.deployment_id,
        )

    def test_missing_expected_schema_object_is_refused_even_with_its_migration_row(self) -> None:
        self.migrate()
        self.connection.execute("DROP TABLE applications")
        with self.assertRaisesRegex(db.MigrationError, "missing expected schema objects"):
            db.migrate(self.connection)

    def test_changed_or_future_migration_is_refused(self) -> None:
        self.migrate()
        self.connection.execute("UPDATE schema_migrations SET checksum = 'bad' WHERE version = 2")
        with self.assertRaisesRegex(db.MigrationError, "checksum"):
            db.migrate(self.connection)
        self.connection.execute("DELETE FROM schema_migrations WHERE version = 2")
        self.connection.execute(
            "INSERT INTO schema_migrations VALUES (99, 'x', '2026-01-01T00:00:00Z')"
        )
        with self.assertRaisesRegex(db.MigrationError, "future"):
            db.migrate(self.connection)

    def test_only_one_unfinished_operation_per_scope(self) -> None:
        self.migrate()
        first = str(uuid.uuid4())
        operation = db.begin_operation(
            self.connection,
            operation_id=first,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="validate",
            deadline_at="2026-01-01T00:15:00Z",
            refs={"builder_id": IMAGE_ID},
            now="2026-01-01T00:00:00Z",
        )
        self.assertEqual(operation.refs, {"builder_id": IMAGE_ID})
        self.assertFalse(self.connection.in_transaction)
        with self.assertRaises(db.UnfinishedOperationError):
            db.begin_operation(
                self.connection,
                operation_id=str(uuid.uuid4()),
                kind="storage.create",
                scope=f"app-{APP_ID}",
                phase="validate",
                deadline_at="2026-01-01T00:15:00Z",
            )
        db.mark_recovery_required(self.connection, first, "ambiguous result", phase="observe")
        with self.assertRaises(db.UnfinishedOperationError):
            db.begin_operation(
                self.connection,
                operation_id=str(uuid.uuid4()),
                kind="app.remove",
                scope=f"app-{APP_ID}",
                phase="validate",
                deadline_at="2026-01-01T00:15:00Z",
            )
        db.mark_failed(self.connection, first, "rolled back", cleanup_state="confirmed")
        second = db.begin_operation(
            self.connection,
            operation_id=str(uuid.uuid4()),
            kind="app.remove",
            scope=f"app-{APP_ID}",
            phase="validate",
            deadline_at="2026-01-01T00:15:00Z",
        )
        self.assertEqual(second.status, "running")

    def test_resumed_operation_takes_the_deadline_of_the_attempt_resuming_it(self) -> None:
        """A stranded operation must not stay stranded.

        The whole-operation deadline bounds one attempt. Keeping the spent
        deadline of the attempt that stranded an operation made every recovery
        fail on sight, and nothing else could release the scope.
        """
        self.migrate()
        operation_id = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=operation_id,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="validated",
            deadline_at="2026-01-01T00:15:00Z",
            refs={"builder_id": IMAGE_ID},
            now="2026-01-01T00:00:00Z",
        )
        db.mark_recovery_required(self.connection, operation_id, error="stranded")
        renewed = db.renew_operation_deadline(
            self.connection, operation_id, "2026-01-01T09:15:00Z", now="2026-01-01T09:00:00Z"
        )
        self.assertEqual(renewed.deadline_at, "2026-01-01T09:15:00Z")
        self.assertEqual(renewed.status, "recovery_required")
        self.assertFalse(self.connection.in_transaction)
        # The record the helper is checked against moves with it.
        stored = db.get_unfinished_operation(self.connection, f"app-{APP_ID}")
        assert stored is not None
        self.assertEqual(stored.deadline_at, "2026-01-01T09:15:00Z")

    def test_operation_refs_and_errors_exclude_obvious_secrets(self) -> None:
        self.migrate()
        with self.assertRaisesRegex(ValidationError, "secret material"):
            db.begin_operation(
                self.connection,
                operation_id=str(uuid.uuid4()),
                kind="storage.create",
                scope=f"app-{APP_ID}",
                phase="create",
                deadline_at="2026-01-01T00:15:00Z",
                refs={"database_password": "sentinel-secret"},
            )
        operation_id = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=operation_id,
            kind="storage.create",
            scope=f"app-{APP_ID}",
            phase="create",
            deadline_at="2026-01-01T00:15:00Z",
        )
        failed = db.mark_recovery_required(
            self.connection,
            operation_id,
            RuntimeError("provider returned sentinel-secret in an unrestricted response"),
        )
        self.assertNotIn("sentinel-secret", failed.safe_error or "")
        serialized = self.path.read_bytes()
        self.assertNotIn(b"sentinel-secret", serialized)

    def test_application_deployment_resources_and_key_ownership_use_real_constraints(self) -> None:
        self.migrate()
        self.add_application()
        application = db.get_application(self.connection, "demo-app")
        assert application is not None
        self.assertEqual(application.scheduler_cpu_mhz, 1000)
        db.put_image_selection(
            self.connection,
            role="worker",
            image_id=IMAGE_ID,
            display_name="example-worker",
            source_commit="a" * 40,
            compatibility_hash="b" * 64,
        )
        self.assertEqual(db.get_image_selection(self.connection, "worker").image_id, IMAGE_ID)  # type: ignore[union-attr]
        with self.assertRaisesRegex(ValidationError, "compatibility_hash"):
            db.put_image_selection(
                self.connection,
                role="builder",
                image_id=IMAGE_ID,
                display_name="example-builder",
                source_commit="a" * 40,
                compatibility_hash="not-a-hash",
            )
        db.accept_deployment(
            self.connection,
            application_id=APP_ID,
            source_commit="a" * 40,
            recipe_hash="b" * 64,
            image_digest=DIGEST,
            nomad_job="demo-app",
            nomad_version=1,
            build_log_path="logs/build.log",
        )
        self.assertEqual(db.get_deployment(self.connection, APP_ID).nomad_version, 1)  # type: ignore[union-attr]
        with self.assertRaisesRegex(ValidationError, "recipe_hash"):
            db.accept_deployment(
                self.connection,
                application_id=APP_ID,
                source_commit="a" * 40,
                recipe_hash="not-a-hash",
                image_digest=DIGEST,
                nomad_job="demo-app",
                nomad_version=1,
                build_log_path="logs/build.log",
            )
        db.put_managed_resource(
            self.connection,
            application_id=APP_ID,
            resource_type="postgres",
            provider_name="app_demo",
            lifecycle_state="active",
            postgres_connections=10,
            measured_target_bytes=2_147_483_648,
        )
        self.assertEqual(
            db.list_managed_resources(self.connection, application_id=APP_ID)[0].resource_type,
            "postgres",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.put_managed_resource(
                self.connection,
                application_id=APP_ID,
                resource_type="mongo",
                provider_name="app_demo",
                lifecycle_state="active",
                s3_bytes=10,
            )
        db.set_environment_keys(
            self.connection,
            application_id=APP_ID,
            owner="platform",
            keys=["PORT", "PLATFORM_PROJECT_ID"],
        )
        db.set_environment_keys(
            self.connection,
            application_id=APP_ID,
            owner="staff",
            keys=["APP_MODE"],
        )
        db.set_environment_keys(
            self.connection,
            application_id=APP_ID,
            owner="platform",
            keys=["PORT"],
        )
        keys = db.list_environment_keys(self.connection, application_id=APP_ID)
        self.assertEqual(
            [(key.key_name, key.owner) for key in keys],
            [("APP_MODE", "staff"), ("PORT", "platform")],
        )
        self.assertFalse(self.connection.in_transaction)

    def test_checkpoint_can_merge_local_mutation_refs_without_losing_rollback_state(self) -> None:
        self.migrate()
        operation_id = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=operation_id,
            kind="infra.replace",
            scope="infrastructure",
            phase="observed",
            deadline_at="2026-01-01T00:15:00Z",
            refs={"old_server_id": APP_ID, "prior_status": "ACTIVE"},
        )
        operation = db.checkpoint_operation(
            self.connection,
            operation_id,
            phase="ambiguous",
            refs={"replacement_server_id": IMAGE_ID},
            merge_refs=True,
        )
        self.assertEqual(
            operation.refs,
            {
                "old_server_id": APP_ID,
                "prior_status": "ACTIVE",
                "replacement_server_id": IMAGE_ID,
            },
        )

    def test_unfinished_operation_image_references_are_all_protected(self) -> None:
        self.migrate()
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=first,
            kind="infra.replace",
            scope="infrastructure",
            phase="observed",
            deadline_at="2026-01-01T00:15:00Z",
            refs={"selected_image_id": IMAGE_ID},
        )
        other_image = "00000000-0000-4000-8000-000000000003"
        db.begin_operation(
            self.connection,
            operation_id=second,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="worker_creating",
            deadline_at="2026-01-01T00:15:00Z",
            refs={"nested": {"worker_image_id": other_image}},
        )
        self.assertEqual(
            db.unfinished_operation_image_ids(self.connection),
            (IMAGE_ID, other_image),
        )
        self.assertEqual(
            db.unfinished_operation_image_ids(self.connection, exclude_operation_id=first),
            (other_image,),
        )

    def test_application_removal_completion_refuses_a_late_managed_row(self) -> None:
        self.migrate()
        self.add_application()
        operation_id = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=operation_id,
            kind="app.remove",
            scope=f"app-{APP_ID}",
            phase="manifest_absent",
            deadline_at="2026-01-01T00:15:00Z",
            refs={"application_id": APP_ID, "slug": "demo-app"},
        )
        db.put_managed_resource(
            self.connection,
            application_id=APP_ID,
            resource_type="mongo",
            provider_name="app_demo",
            lifecycle_state="active",
        )
        with self.assertRaisesRegex(db.DatabaseError, "managed resource"):
            db.complete_application_removal(
                self.connection,
                application_id=APP_ID,
                operation_id=operation_id,
            )
        self.assertIsNotNone(db.get_application(self.connection, APP_ID))
        self.assertEqual(db.get_operation(self.connection, operation_id).status, "running")  # type: ignore[union-attr]
        db.delete_managed_resource(
            self.connection, application_id=APP_ID, resource_type="mongo"
        )
        db.complete_application_removal(
            self.connection,
            application_id=APP_ID,
            operation_id=operation_id,
            now="2026-01-01T00:01:00Z",
        )
        self.assertIsNone(db.get_application(self.connection, APP_ID))
        self.assertEqual(db.get_slug_tombstone(self.connection, "demo-app").application_id, APP_ID)  # type: ignore[union-attr]

    def test_all_manifest_history_and_only_active_references_are_exposed(self) -> None:
        self.migrate()
        self.add_application()
        current = DIGEST
        prior = "registry.example/projects/demo/app@sha256:" + "b" * 64
        candidate = "registry.example/projects/demo/app@sha256:" + "c" * 64
        db.accept_deployment(
            self.connection,
            application_id=APP_ID,
            source_commit="a" * 40,
            recipe_hash="b" * 64,
            image_digest=current,
            nomad_job="demo-app",
            nomad_version=1,
            build_log_path="logs/build.log",
        )
        old_operation = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=old_operation,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="image_pushed",
            deadline_at="2026-01-01T00:15:00Z",
        )
        db.checkpoint_operation(
            self.connection, old_operation, phase="accepted", candidate_digest=prior
        )
        db.mark_succeeded(self.connection, old_operation)
        active_operation = str(uuid.uuid4())
        db.begin_operation(
            self.connection,
            operation_id=active_operation,
            kind="app.deploy",
            scope=f"app-{APP_ID}",
            phase="image_pushed",
            deadline_at="2026-01-01T00:15:00Z",
        )
        db.checkpoint_operation(
            self.connection,
            active_operation,
            phase="image_pushed",
            candidate_digest=candidate,
        )
        self.assertEqual(
            set(db.list_application_manifest_images(self.connection, APP_ID)),
            {current, prior, candidate},
        )
        self.assertEqual(
            db.list_application_successful_manifest_history(self.connection, APP_ID),
            (current, prior),
        )
        self.assertEqual(
            set(db.list_active_application_manifest_references(self.connection, APP_ID)),
            {current, candidate},
        )

    def test_attempt_request_is_immutable_while_evidence_and_status_evolve(self) -> None:
        self.migrate()
        self.add_application()
        configuration = parse_configuration(
            {
                "schemaVersion": 1,
                "build": {
                    "runtime": "node",
                    "packages": ["."],
                    "buildScript": "build",
                    "startScript": "start",
                },
                "runtime": {"port": 3000, "healthPath": "/health"},
                "storageBindings": [],
            }
        )
        for request_id in (REQUEST_ID, SECOND_REQUEST_ID):
            db.claim_idempotency_request(
                self.connection,
                request_id=request_id,
                request_fingerprint=db.request_fingerprint({"requestId": request_id}),
            )
        first = db.create_deployment_attempt(
            self.connection,
            deployment_id=DEPLOYMENT_ID,
            application_id=APP_ID,
            source_commit="a" * 40,
            requested_ref="refs/heads/main",
            configuration_revision=7,
            configuration=configuration,
            environment_revision=0,
            idempotency_request_id=REQUEST_ID,
            now="2026-01-01T00:00:00Z",
        )
        self.assertEqual((first.status, first.snapshot_kind), ("queued", "strict"))
        self.assertEqual(first.configuration, configuration)
        self.assertIsNone(first.recipe_hash)
        self.assertIsNone(first.image_digest)
        self.assertIsNone(first.safe_error)

        immutable_changes = {
            "application_id": IMAGE_ID,
            "source_commit": "f" * 40,
            "requested_ref": "refs/heads/other",
            "configuration_revision": 8,
            "configuration_json": "{}",
            "environment_revision": 1,
            "idempotency_request_id": SECOND_REQUEST_ID,
        }
        for column, value in immutable_changes.items():
            with self.subTest(column=column), self.assertRaisesRegex(
                sqlite3.IntegrityError, "immutable"
            ):
                self.connection.execute(
                    f"UPDATE deployment_attempts SET {column} = ? WHERE deployment_id = ?",
                    (value, DEPLOYMENT_ID),
                )

        with self.assertRaisesRegex(db.DatabaseError, "unfinished"):
            db.create_deployment_attempt(
                self.connection,
                deployment_id=IMAGE_ID,
                application_id=APP_ID,
                source_commit="c" * 40,
                requested_ref="refs/heads/next",
                configuration_revision=8,
                configuration=configuration,
                environment_revision=0,
                idempotency_request_id=SECOND_REQUEST_ID,
            )
        building = db.checkpoint_deployment_attempt(
            self.connection,
            DEPLOYMENT_ID,
            status="building",
            recipe_hash="b" * 64,
            build_log_path="logs/first.log",
            now="2026-01-01T00:01:00Z",
        )
        self.assertEqual(
            db.list_deployment_attempts(self.connection, APP_ID, status="building"),
            (building,),
        )
        recovery = db.checkpoint_deployment_attempt(
            self.connection,
            DEPLOYMENT_ID,
            status="recovery_required",
            error="builder result was ambiguous",
            now="2026-01-01T00:02:00Z",
        )
        self.assertEqual(recovery.status, "recovery_required")
        self.assertEqual(recovery.recipe_hash, "b" * 64)
        failed = db.checkpoint_deployment_attempt(
            self.connection,
            DEPLOYMENT_ID,
            status="failed",
            cleanup_state="confirmed",
            now="2026-01-01T00:03:00Z",
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(
            db.list_deployment_attempts(self.connection, APP_ID, status="failed"),
            (failed,),
        )

        second = db.create_deployment_attempt(
            self.connection,
            deployment_id=IMAGE_ID,
            application_id=APP_ID,
            source_commit="c" * 40,
            requested_ref="refs/heads/next",
            configuration_revision=8,
            configuration=configuration,
            environment_revision=0,
            idempotency_request_id=SECOND_REQUEST_ID,
            now="2026-01-02T00:00:00Z",
        )
        second = db.checkpoint_deployment_attempt(
            self.connection,
            second.deployment_id,
            status="succeeded",
            recipe_hash="d" * 64,
            image_digest="registry.example/projects/demo/app@sha256:" + "e" * 64,
            nomad_job="second",
            nomad_job_sha256="e" * 64,
            nomad_version=2,
            build_log_path="logs/second.log",
            cleanup_state="not_required",
            now="2026-01-02T00:01:00Z",
        )
        self.assertEqual(db.get_deployment(self.connection, APP_ID).nomad_version, 2)  # type: ignore[union-attr]
        self.assertEqual(
            [item.deployment_id for item in db.list_deployment_attempts(self.connection, APP_ID)],
            [IMAGE_ID, DEPLOYMENT_ID],
        )
        self.assertEqual(second.status, "succeeded")

    def test_idempotency_fingerprint_and_result_are_stable_on_replay(self) -> None:
        self.migrate()
        fingerprint = db.request_fingerprint({"slug": "demo-app", "runtime": {"port": 3000}})
        claimed = db.claim_idempotency_request(
            self.connection,
            request_id=REQUEST_ID,
            request_fingerprint=fingerprint,
            now="2026-01-01T00:00:00Z",
        )
        self.assertIsNone(claimed.result_id)
        self.assertEqual(
            db.claim_idempotency_request(
                self.connection,
                request_id=REQUEST_ID,
                request_fingerprint=fingerprint,
            ),
            claimed,
        )
        completed = db.complete_idempotency_request(
            self.connection,
            request_id=REQUEST_ID,
            result_kind="deployment",
            result_id=DEPLOYMENT_ID,
            now="2026-01-01T00:01:00Z",
        )
        self.assertEqual((completed.result_kind, completed.result_id), ("deployment", DEPLOYMENT_ID))
        self.assertEqual(
            db.complete_idempotency_request(
                self.connection,
                request_id=REQUEST_ID,
                result_kind="deployment",
                result_id=DEPLOYMENT_ID,
            ),
            completed,
        )
        with self.assertRaises(db.IdempotencyConflictError):
            db.claim_idempotency_request(
                self.connection,
                request_id=REQUEST_ID,
                request_fingerprint="f" * 64,
            )
        with self.assertRaisesRegex(ValidationError, "secret material"):
            db.request_fingerprint({"api_token": "must-not-be-persisted"})
        self.assertNotIn(b"must-not-be-persisted", self.path.read_bytes())

    def test_environment_revision_storage_identity_and_slug_tombstone(self) -> None:
        self.migrate()
        self.add_application()
        revision = db.get_environment_revision(self.connection, APP_ID)
        assert revision is not None
        self.assertEqual(revision.revision, 0)
        self.assertEqual(
            db.advance_environment_revision(
                self.connection,
                application_id=APP_ID,
                expected_revision=0,
                now="2026-01-01T00:01:00Z",
            ).revision,
            1,
        )
        with self.assertRaisesRegex(db.DatabaseError, "stale"):
            db.advance_environment_revision(
                self.connection, application_id=APP_ID, expected_revision=0
            )

        resource = db.put_managed_resource(
            self.connection,
            application_id=APP_ID,
            resource_type="postgres",
            resource_name="primary",
            display_label="Primary database",
            provider_name="app_demo",
            lifecycle_state="active",
        )
        renamed = db.rename_managed_resource(
            self.connection, resource.resource_id, "Customer data"
        )
        self.assertEqual(renamed.resource_id, resource.resource_id)
        self.assertEqual(renamed.display_label, "Customer data")
        with self.assertRaisesRegex(db.DatabaseError, "immutable"):
            db.put_managed_resource(
                self.connection,
                application_id=APP_ID,
                resource_id=DEPLOYMENT_ID,
                resource_type="postgres",
                resource_name="primary",
                provider_name="app_demo",
                lifecycle_state="active",
            )

        db.put_application(
            self.connection,
            application_id=APP_ID,
            application_slug="demo-renamed",
            worker_flavor="example.1c2g",
            scheduler_cpu_mhz=1000,
            scheduler_memory_mib=2048,
            now="2026-01-01T00:02:00Z",
        )
        tombstone = db.get_slug_tombstone(self.connection, "demo-app")
        assert tombstone is not None
        self.assertEqual(tombstone.application_id, APP_ID)
        with self.assertRaisesRegex(db.DatabaseError, "retired"):
            db.put_application(
                self.connection,
                application_id=IMAGE_ID,
                application_slug="demo-app",
                worker_flavor="example.1c2g",
                scheduler_cpu_mhz=1000,
                scheduler_memory_mib=2048,
            )

    def test_database_path_must_not_be_a_symlink_even_when_dangling(self) -> None:
        symlink = self.directory / "dangling.sqlite3"
        symlink.symlink_to(self.directory / "missing-target.sqlite3")
        with self.assertRaises(db.DatabaseError):
            db.connect(symlink)
        self.assertFalse((self.directory / "missing-target.sqlite3").exists())

    def test_online_backup_is_consistent_private_and_readable(self) -> None:
        self.migrate()
        self.add_application()
        destination = self.directory / "backups" / "snapshot.sqlite3"
        db.backup_database(self.connection, destination)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        backup = sqlite3.connect(destination)
        try:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                backup.execute("SELECT slug FROM applications").fetchone()[0], "demo-app"
            )
        finally:
            backup.close()
        with self.assertRaises(db.DatabaseError):
            db.backup_database(self.connection, destination)


if __name__ == "__main__":
    unittest.main()
