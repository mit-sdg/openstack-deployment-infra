from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from openstack_platform.config import load_platform
from openstack_platform.controller import database as db

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/releases/migrate_legacy_controller.py"
SPEC = importlib.util.spec_from_file_location("migrate_legacy_controller", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class LegacyControllerMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_state = self.root / "source-state"
        self.source_state.mkdir(mode=0o700)
        self.source = self.root / "legacy.sqlite3"
        self.mapping = self.root / "mapping.json"
        self.platform = ROOT / "config/platform.example.json"
        self.identity = db.deployment_identity(load_platform(self.platform))
        self.application_id = "10000000-0000-4000-8000-000000000001"
        self.deployment_id = "20000000-0000-4000-8000-000000000002"
        self.server_id = "30000000-0000-4000-8000-000000000003"
        self.port_id = "40000000-0000-4000-8000-000000000004"
        self.image_id = "50000000-0000-4000-8000-000000000005"
        self._legacy_database()
        self._mapping()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _legacy_database(self) -> None:
        connection = sqlite3.connect(self.source)
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER, checksum TEXT, applied_at TEXT);
            CREATE TABLE applications(
              application_id TEXT, slug TEXT, repository_url TEXT, config_path TEXT,
              desired_running INTEGER, url TEXT, worker_server_id TEXT,
              worker_server_name TEXT, worker_port_id TEXT, worker_port_name TEXT,
              worker_flavor TEXT, scheduler_cpu_mhz INTEGER, scheduler_memory_mib INTEGER,
              created_at TEXT, updated_at TEXT);
            CREATE TABLE deployments(
              application_id TEXT, source_commit TEXT, recipe_hash TEXT, image_digest TEXT,
              nomad_job TEXT, nomad_version INTEGER, build_log_path TEXT, accepted_at TEXT,
              last_healthy_at TEXT, nomad_job_sha256 TEXT, health_path TEXT,
              application_port INTEGER);
            CREATE TABLE environment_keys(application_id TEXT, key_name TEXT, owner TEXT);
            CREATE TABLE image_selections(
              role TEXT, image_id TEXT, display_name TEXT, source_commit TEXT,
              compatibility_hash TEXT, selected_at TEXT);
            CREATE TABLE managed_resources(
              application_id TEXT, resource_type TEXT, resource_name TEXT, provider_id TEXT,
              provider_name TEXT, lifecycle_state TEXT, postgres_connections INTEGER,
              measured_target_bytes INTEGER, s3_bytes INTEGER, s3_objects INTEGER,
              last_verified_at TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE operations(
              operation_id TEXT, kind TEXT, scope TEXT, status TEXT, phase TEXT,
              started_at TEXT, updated_at TEXT, deadline_at TEXT, refs_json TEXT,
              candidate_digest TEXT, safe_error TEXT, cleanup_state TEXT);
            """
        )
        marker = migration._legacy_marker(self.identity)
        rows = [(0, marker, "2026-01-01T00:00:00Z")]
        rows.extend(
            (version, checksum, "2026-01-01T00:00:00Z")
            for version, checksum in migration._LEGACY_MIGRATIONS.items()
        )
        connection.executemany("INSERT INTO schema_migrations VALUES (?, ?, ?)", rows)
        connection.execute(
            "INSERT INTO applications VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.application_id,
                "demo-app",
                "https://github.com/example/demo",
                "platform.yaml",
                "https://demo-app.apps.example.com",
                self.server_id,
                "example-worker",
                self.port_id,
                "example-worker-v4",
                "standard.2c4g",
                1000,
                2048,
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
            ),
        )
        digest = "registry.example/projects/demo-app/app@sha256:" + "a" * 64
        log = Path("build-logs") / self.application_id / f"{'b' * 40}-{self.deployment_id}.log"
        log_path = self.source_state / log
        log_path.parent.mkdir(mode=0o700, parents=True)
        log_path.write_text("build complete\n", encoding="utf-8")
        log_path.chmod(0o600)
        connection.execute(
            "INSERT INTO deployments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.application_id,
                "b" * 40,
                "c" * 64,
                digest,
                '{"ID":"demo-app"}',
                7,
                log.as_posix(),
                "2026-01-02T00:00:00Z",
                "2026-01-02T00:00:00Z",
                "d" * 64,
                "/health",
                3000,
            ),
        )
        connection.execute(
            "INSERT INTO environment_keys VALUES (?, ?, ?)",
            (self.application_id, "MONGODB_URI", "storage.mongo.default"),
        )
        connection.execute(
            "INSERT INTO image_selections VALUES (?, ?, ?, ?, ?, ?)",
            (
                "worker",
                self.image_id,
                "example-worker-image",
                "e" * 40,
                "f" * 64,
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO managed_resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.application_id,
                "mongo",
                "default",
                "provider-id",
                "provider-name",
                "active",
                None,
                1024,
                None,
                None,
                "2026-01-02T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO operations VALUES (?, 'app.deploy', ?, 'succeeded', 'accepted', ?, ?, ?, '{}', ?, NULL, 'confirmed')",
            (
                self.deployment_id,
                f"app-{self.application_id}",
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
                "2026-01-02T01:00:00Z",
                digest,
            ),
        )
        connection.commit()
        connection.close()
        self.source.chmod(0o600)

    def _mapping(self) -> None:
        value = {
            "demo-app": {
                "requestedRef": "main",
                "configuration": {
                    "schemaVersion": 1,
                    "build": {
                        "runtime": "bun",
                        "packages": ["."],
                        "buildScript": "build",
                        "startScript": "start",
                    },
                    "runtime": {"port": 3000, "healthPath": "/health"},
                    "storageBindings": [
                        {
                            "resourceType": "mongo",
                            "resourceName": "default",
                            "outputs": {"uri": "MONGODB_URI"},
                        }
                    ],
                },
            }
        }
        self.mapping.write_text(json.dumps(value), encoding="utf-8")
        self.mapping.chmod(0o600)

    def test_imports_complete_legacy_product_state(self) -> None:
        destination_state = self.root / "imported"
        destination = migration.import_legacy(
            source_database=self.source,
            source_state=self.source_state,
            destination_state=destination_state,
            platform_path=self.platform,
            mapping_path=self.mapping,
        )

        connection = db.connect(destination, identity=self.identity)
        db.validate_complete_schema(connection, identity=self.identity)
        self.assertEqual(len(db.list_applications(connection)), 1)
        self.assertEqual(len(db.list_managed_resources(connection)), 1)
        self.assertEqual(len(db.list_image_selections(connection)), 1)
        deployment = db.get_deployment(connection, self.application_id)
        self.assertIsNotNone(deployment)
        assert deployment is not None
        self.assertEqual(deployment.source_commit, "b" * 40)
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM operations WHERE status IN ('running', 'recovery_required')"
            ).fetchone()
        )
        connection.close()
        receipt = json.loads((destination_state / "LEGACY-IMPORT-RECEIPT.json").read_text())
        self.assertEqual(receipt["applications"], 1)
        self.assertEqual(receipt["storageResources"], 1)
        self.assertEqual(
            receipt["sourceSha256"], hashlib.sha256(self.source.read_bytes()).hexdigest()
        )

    def test_rejects_marker_for_another_deployment(self) -> None:
        connection = sqlite3.connect(self.source)
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 0", ("0" * 64,)
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(migration.ImportFailure, "marker or migration"):
            migration.import_legacy(
                source_database=self.source,
                source_state=self.source_state,
                destination_state=self.root / "rejected",
                platform_path=self.platform,
                mapping_path=self.mapping,
            )


if __name__ == "__main__":
    unittest.main()
