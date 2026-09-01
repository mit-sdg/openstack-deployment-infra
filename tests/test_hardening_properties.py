from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from infra.lib.platform_contract import ContractError, load_contract
from openstack_platform import config, durable, restore
from openstack_platform.controller import database as db
from openstack_platform.controller.http import HttpError, Response, Router


class DurableReplacementProperties(unittest.TestCase):
    def test_every_interruption_stage_has_a_deterministic_retry_state(self) -> None:
        stages = (
            "before_write",
            "after_write",
            "after_file_fsync",
            "after_rename",
            "after_directory_fsync",
        )
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                destination = root / "state"
                durable.atomic_write(destination, b"old", mode=0o600, maximum_bytes=16)

                def interrupt(observed: str, selected: str = stage) -> None:
                    if observed == selected:
                        raise OSError("injected interruption")

                with self.assertRaises(durable.DurableReplaceError):
                    durable.atomic_write(
                        destination,
                        b"new",
                        mode=0o600,
                        maximum_bytes=16,
                        fault=interrupt,
                    )
                self.assertIn(destination.read_bytes(), {b"old", b"new"})
                durable.atomic_write(destination, b"new", mode=0o600, maximum_bytes=16)
                self.assertEqual(destination.read_bytes(), b"new")
                self.assertFalse((root / ".state.tmp").exists())

    def test_stale_symlink_and_unexpected_destination_type_are_never_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            victim = root / "victim"
            victim.write_bytes(b"secret")
            stale = root / ".state.tmp"
            stale.symlink_to(victim)
            with self.assertRaises(durable.DurableReplaceError):
                durable.atomic_write(root / "state", b"new", mode=0o600, maximum_bytes=16)
            self.assertTrue(stale.is_symlink())
            self.assertEqual(victim.read_bytes(), b"secret")

            destination = root / "directory"
            destination.mkdir()
            with self.assertRaises(durable.DurableReplaceError):
                durable.atomic_write(destination, b"new", mode=0o600, maximum_bytes=16)
            self.assertTrue(destination.is_dir())


class ParserProperties(unittest.TestCase):
    def test_seeded_contract_mutations_are_always_rejected(self) -> None:
        source = json.loads(Path("infra/lib/platform_contract.json").read_text())
        randomizer = random.Random(0xC0A7)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            for _ in range(64):
                candidate = json.loads(json.dumps(source))
                mutation = randomizer.randrange(4)
                if mutation == 0:
                    candidate["version"] = randomizer.randrange(2, 10_000)
                elif mutation == 1:
                    candidate["ports"][randomizer.choice(sorted(candidate["ports"]))] = (
                        randomizer.choice([0, 65536, True, "443"])
                    )
                elif mutation == 2:
                    candidate["roles"]["all"].append(candidate["roles"]["all"][0])
                else:
                    candidate[f"unknown{randomizer.randrange(1000)}"] = None
                path.write_text(json.dumps(candidate))
                with self.assertRaises(ContractError):
                    load_contract(path)

    def test_seeded_config_byte_corruption_never_returns_partial_inventory(self) -> None:
        raw = Path("config/platform.example.json").read_bytes()
        randomizer = random.Random(0xC0F1)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "platform.json"
            for _ in range(64):
                candidate = bytearray(raw)
                offset = randomizer.randrange(len(candidate))
                candidate[offset] = 0
                path.write_bytes(candidate)
                with self.assertRaises((config.ValidationError, UnicodeDecodeError)):
                    config.load_platform(path)


class StateBoundaryProperties(unittest.TestCase):
    def test_invalid_router_targets_never_call_mutation_handler(self) -> None:
        calls = 0

        def mutate(_request):
            nonlocal calls
            calls += 1
            return Response(202, {})

        router = Router()
        router.add("POST", "/v1/applications", mutate)
        randomizer = random.Random(0xF12A)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789%?#;\\\x00"
        for _ in range(256):
            target = "".join(
                randomizer.choice(alphabet) for _ in range(randomizer.randrange(0, 48))
            )
            if target == "/v1/applications":
                continue
            try:
                router.dispatch("POST", target, {}, {})
            except (HttpError, ValueError):
                pass
        self.assertEqual(calls, 0)

    def test_unsupported_database_is_detected_before_migration_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE legacy_controller_state(value TEXT)")
            connection.commit()
            connection.close()
            path.chmod(0o600)
            before = path.read_bytes()
            with self.assertRaisesRegex(db.UnsupportedPriorStateError, "UNSUPPORTED_PRIOR_STATE"):
                db.connect(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_name(path.name + "-wal").exists())

    def test_future_schema_is_rejected_before_wal_or_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            connection = db.connect(path)
            db.migrate(connection)
            connection.close()
            raw = sqlite3.connect(path)
            raw.execute(
                "INSERT INTO schema_migrations VALUES (99, 'future', '2099-01-01T00:00:00Z')"
            )
            raw.commit()
            raw.close()
            before = path.read_bytes()
            with self.assertRaisesRegex(db.UnsupportedPriorStateError, "UNSUPPORTED_PRIOR_STATE"):
                db.connect(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_name(path.name + "-wal").exists())
            self.assertFalse(path.with_name(path.name + "-shm").exists())

    def test_restore_reports_stable_unsupported_state_without_replacing_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            state = root / "state"
            state.mkdir(mode=0o700)
            backup = root / "legacy.sqlite3"
            legacy = sqlite3.connect(backup)
            legacy.execute("CREATE TABLE legacy_controller_state(value TEXT)")
            legacy.commit()
            legacy.close()
            backup.chmod(0o600)
            destination = state / "platform.sqlite3"
            current = db.connect(destination)
            db.migrate(current)
            current.close()
            before = destination.read_bytes()
            with self.assertRaisesRegex(restore.RestoreError, "UNSUPPORTED_PRIOR_STATE"):
                restore.restore_database(backup, destination)
            self.assertEqual(destination.read_bytes(), before)


class IdempotencyAndTransitionProperties(unittest.TestCase):
    def test_seeded_replay_conflicts_and_invalid_transitions_do_not_mutate(self) -> None:
        randomizer = random.Random(0x1DE0)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            connection = db.connect(path)
            db.migrate(connection)
            for index in range(32):
                request_id = f"00000000-0000-4000-8000-{index + 1:012d}"
                items = [("slug", f"app-{index}"), ("port", 3000 + index)]
                randomizer.shuffle(items)
                fingerprint = db.request_fingerprint(dict(items))
                claimed = db.claim_idempotency_request(
                    connection,
                    request_id=request_id,
                    request_fingerprint=fingerprint,
                    now="2026-01-01T00:00:00Z",
                )
                self.assertEqual(
                    db.claim_idempotency_request(
                        connection,
                        request_id=request_id,
                        request_fingerprint=db.request_fingerprint(dict(reversed(items))),
                    ),
                    claimed,
                )
                rows_before = connection.total_changes
                with self.assertRaises(db.IdempotencyConflictError):
                    db.claim_idempotency_request(
                        connection,
                        request_id=request_id,
                        request_fingerprint="f" * 64,
                    )
                self.assertEqual(connection.total_changes, rows_before)

            operation_id = "10000000-0000-4000-8000-000000000001"
            db.begin_operation(
                connection,
                operation_id=operation_id,
                kind="app.deploy",
                scope="app-property",
                phase="validated",
                deadline_at="2099-01-01T00:00:00Z",
            )
            operation_before = db.get_operation(connection, operation_id)
            with self.assertRaises(config.ValidationError):
                db.checkpoint_operation(
                    connection,
                    operation_id,
                    phase="invalid phase",
                    refs={"provider_token": "must-not-persist"},
                )
            self.assertEqual(db.get_operation(connection, operation_id), operation_before)
            self.assertNotIn(b"must-not-persist", path.read_bytes())
            connection.close()


class SecretDiagnosticProperty(unittest.TestCase):
    def test_durable_errors_do_not_include_payload_or_paths(self) -> None:
        secret = b"diagnostic-secret-value"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "state"
            destination.mkdir()
            with self.assertRaises(durable.DurableReplaceError) as captured:
                durable.atomic_write(destination, secret, mode=0o600, maximum_bytes=1024)
            rendered = str(captured.exception)
            self.assertNotIn(secret.decode(), rendered)
            self.assertNotIn(str(destination), rendered)


class ImageFirstBootProperties(unittest.TestCase):
    def test_production_cloud_init_runs_only_with_config_drive(self) -> None:
        common = Path("nix/modules/common.nix").read_text()
        datasource_block = common.split("datasource_list = [", 1)[1].split("];", 1)[0]

        self.assertIn('"ConfigDrive"', datasource_block)
        self.assertNotIn('"OpenStack"', datasource_block)
        self.assertNotIn('"None"', datasource_block)
        self.assertIn("preserve_hostname = true;", common)
        self.assertIn("cloud-init-image-state-reset", common)
        self.assertIn("systemd.services.cloud-init-local.serviceConfig.ExecStartPre", common)
        self.assertIn("! -e ${configRoot}/.provisioned", common)
        self.assertIn("rm -rf /var/lib/cloud", common)
        self.assertIn(
            'systemd.services.cloud-init.requires = [ "cloud-init-local.service" ];', common
        )
        for role in ("admin", "ingress", "storage", "worker", "builder"):
            template = Path(f"infra/cloud-init-nixos/{role}.yaml").read_text()
            self.assertIn("/etc/__PLATFORM_NAMESPACE__/.provisioned", template)
            self.assertIn("config-drive-v1", template)


if __name__ == "__main__":
    unittest.main()
