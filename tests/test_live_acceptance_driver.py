from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid5

from openstack_platform import acceptance
from openstack_platform.acceptance_live_driver import (
    DriverConfig,
    LiveDriverError,
    RepositoryLiveDriver,
    SubprocessTransport,
    SupportedInterfaces,
)

DEPLOYMENT = "12345678-1234-4234-9234-123456789abc"
PROJECT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NAMESPACE = "p07-live-12345678"


def config(root: Path, executable: Path) -> DriverConfig:
    driver_path = root / "driver.json"
    driver_path.write_text("{}\n")
    driver_path.chmod(0o600)
    private = root / "input.env"
    private.write_text(f"OS_PROJECT_ID='{PROJECT}'\nPLATFORM_NAMESPACE='{NAMESPACE}'\n")
    private.chmod(0o600)
    ssh_config = root / "ssh-config"
    ssh_config.write_text("Host none\n")
    ssh_config.chmod(0o600)
    identity = root / "identity"
    identity.write_text("not-used-by-plan\n")
    identity.chmod(0o600)
    app: Mapping[str, object] = {
        "slug": "acceptance-12345678",
        "repository": "https://github.com/example/acceptance",
        "commit": "1" * 40,
        "requestedRef": "main",
        "verificationPath": "/acceptance/storage",
        "resetPath": "/acceptance/storage/reset",
        "contentProof": {
            "postgres": {"value": "12345678"},
            "mongo": {"value": "12345678"},
            "s3": {"value": "12345678"},
        },
        "emptyProof": {"postgres": None, "mongo": None, "s3": None},
        "configuration": {
            "schemaVersion": 1,
            "build": {
                "runtime": "node",
                "packages": ["."],
                "buildScript": None,
                "startScript": "start",
            },
            "runtime": {"port": 3000, "healthPath": "/health"},
            "storageBindings": [],
        },
    }
    return DriverConfig(
        driver_path,
        DEPLOYMENT,
        PROJECT,
        NAMESPACE,
        str(executable),
        str(root / "platform.json"),
        str(root / "policy.json"),
        str(root / "state"),
        str(private),
        str(root / "workspace"),
        str(executable),
        str(executable),
        str(executable),
        str(ssh_config),
        "admin",
        "recovery",
        "/run/p07/project.sock",
        "/run/p07/privileged.sock",
        str(executable),
        str(executable),
        str(identity),
        str(root / "offline"),
        str(root / "staging"),
        "/srv/backups",
        "/srv/platform",
        "/srv/state",
        str(root / "transcript.json"),
        "destroy-after-verified-restore",
        app,
        {
            "ingress": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "admin": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        },
    )


class FakeCommandTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], bool, bytes]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout: int = 1800,
        mutating: bool = False,
    ) -> bytes:
        encoded = tuple(argv)
        self.calls.append((encoded, mutating, stdin))
        if "token" in encoded:
            return json.dumps({"project_id": PROJECT}).encode()
        if "-F" in encoded and "recovery" in encoded:
            return b'{"applicationId":"ok"}\n201'
        if "status" in encoded:
            return b"STATE INFRA APPS STORAGE LIVE UNAVAILABLE UNHEALTHY\nhealthy 5 0 0 5 0 0\n"
        if "list" in encoded:
            return b"[]"
        return b"setup plan"


class FakeInterfaces:
    def __init__(self, value: DriverConfig) -> None:
        self.c = value
        self.calls: list[tuple[object, ...]] = []
        self.storage: list[dict[str, object]] = []
        self.deployments: list[dict[str, object]] = []
        self.deleted = False
        self.enabled = True
        self.app_id = str(uuid5(UUID(DEPLOYMENT), "application"))

    def setup_plan(self) -> None:
        self.calls.append(("setup-plan", False))

    def baseline(self) -> str:
        self.calls.append(("inventory", False))
        return "b" * 64

    def inventory(self) -> list[dict[str, object]]:
        return []

    def setup_apply(self) -> dict[str, bool]:
        self.calls.append(("setup", True))
        return {name: True for name in dict(acceptance.ACTION_CHECKS)["greenfield_setup"]}

    def mutate_controller(
        self,
        method: str,
        path: str,
        body: object,
        key: str,
        *,
        wait: bool = True,
        privileged: bool = False,
    ) -> object:
        self.calls.append(("controller", method, path, key, wait, privileged))
        if path == "/v1/applications":
            return {"applicationId": self.app_id, "enabled": False}
        if path.endswith("/storage") and isinstance(body, dict):
            kind = body["type"]
            if not any(item["type"] == kind for item in self.storage):
                self.storage.append(
                    {
                        "resourceId": str(uuid5(UUID(DEPLOYMENT), str(kind))),
                        "applicationId": self.app_id,
                        "type": kind,
                        "name": "acceptance",
                        "lifecycleState": "active",
                        "lastVerifiedAt": "2026-01-01T00:00:00Z",
                    }
                )
        if path.endswith("/deployments") and isinstance(body, dict):
            if not any(item["deploymentId"] == key for item in self.deployments):
                self.deployments.append(
                    {
                        "deploymentId": key,
                        "repositoryCommit": body["commit"],
                        "status": "succeeded",
                        "imageDigest": "registry.example/app@sha256:" + "a" * 64,
                        "nomadVersion": 1,
                        "acceptedAt": "2026-01-01T00:00:00Z",
                        "lastHealthyAt": "2026-01-01T00:00:01Z",
                    }
                )
        if path.endswith("/disable"):
            self.enabled = False
        if path.endswith("/enable"):
            self.enabled = True
        if path.endswith("/delete"):
            if not privileged:
                raise AssertionError("delete did not use the privileged socket")
            self.deleted = True
            self.storage.clear()
        return {"status": "succeeded"}

    def list_items(self, path: str, *, privileged: bool = False) -> list[Mapping[str, object]]:
        self.calls.append(("controller-list", path, privileged))
        if "/deployments" in path:
            return list(self.deployments)
        if self.deleted and "/applications/" in path:
            raise AssertionError("tombstoned application storage route was queried")
        if path.startswith("/v1/admin/") and not privileged:
            raise AssertionError("admin inventory did not use the privileged socket")
        return list(self.storage)

    def controller(
        self, method: str, path: str, body: object | None = None, **_options: object
    ) -> tuple[int, object]:
        self.calls.append(("controller-read", method, path, False))
        if self.deleted:
            return 404, {"error": "not_found"}
        return 200, {
            "applicationId": self.app_id,
            "enabled": self.enabled,
            "url": "https://acceptance.example.test",
        }

    def interrupt_controller(self, operation_key: str) -> dict[str, bool]:
        self.calls.append(("interrupt-controller", operation_key, True))
        return {"durableOperationStarted": True, "interruptionInjected": True}

    def verify_public_storage(self, url: str, expected: Mapping[str, object]) -> dict[str, bool]:
        self.calls.append(("public-storage", url, expected, False))
        return {
            "publicRouteHealthy": True,
            "storageBound": True,
            "postgresWriteReadVerified": True,
            "mongoWriteReadVerified": True,
            "s3WriteReadVerified": True,
        }

    def operator_backup_restore(self) -> dict[str, bool]:
        self.calls.append(("operator-backup-restore", True))
        return {name: True for name in dict(acceptance.ACTION_CHECKS)["operator_sqlite_restore"]}

    def hosted_restore(self) -> dict[str, bool]:
        self.calls.append(("hosted-backup-restore", True))
        return {name: True for name in dict(acceptance.ACTION_CHECKS)["hosted_sqlite_restore"]}

    def managed_restore(self, url: str) -> dict[str, bool]:
        self.calls.append(("managed-backup-restore", url, True))
        return {name: True for name in dict(acceptance.ACTION_CHECKS)["managed_data_restore"]}

    def replace(self, role: str) -> dict[str, bool]:
        self.calls.append(("replace", role, True))
        action = "persistent_host_replacement" if role == "ingress" else "admin_recovery"
        return {name: True for name in dict(acceptance.ACTION_CHECKS)[action]}

    def teardown(self, baseline: str) -> dict[str, bool]:
        if baseline != "b" * 64 or not self.deleted or self.storage:
            raise AssertionError("cleanup did not receive converged scoped state")
        self.calls.append(("teardown", True))
        return {name: True for name in dict(acceptance.ACTION_CHECKS)["cleanup_verify"]}


class TeardownTransport(FakeCommandTransport):
    def __init__(self, name: str, identifier: str) -> None:
        super().__init__()
        self.name = name
        self.identifier = identifier
        self.live = True
        self.fail_delete_once = False

    @property
    def projection(self) -> dict[str, object]:
        return {"id": self.identifier, "name": self.name, "projectId": PROJECT}

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout: int = 1800,
        mutating: bool = False,
    ) -> bytes:
        encoded = tuple(argv)
        self.calls.append((encoded, mutating, stdin))
        if "token" in encoded:
            return json.dumps({"project_id": PROJECT}).encode()
        if "show" in encoded:
            return json.dumps(
                {"id": self.identifier, "name": self.name, "project_id": PROJECT}
            ).encode()
        if "delete" in encoded:
            if self.fail_delete_once:
                self.fail_delete_once = False
                raise RuntimeError("fault after intent")
            self.live = False
            return b""
        if "list" in encoded:
            if "volume" in encoded and self.live:
                return json.dumps([{"id": self.identifier, "name": self.name}]).encode()
            return b"[]"
        raise AssertionError(encoded)


class StaticOutputTransport(FakeCommandTransport):
    def __init__(self, output: Mapping[str, object]) -> None:
        super().__init__()
        self.output = output

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout: int = 1800,
        mutating: bool = False,
    ) -> bytes:
        self.calls.append((tuple(argv), mutating, stdin))
        return json.dumps(self.output).encode()


class LiveAcceptanceDriverTests(unittest.TestCase):
    def test_fake_command_transport_proves_guards_precede_supported_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "supported"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            commands = FakeCommandTransport()
            interfaces = SupportedInterfaces(value, commands)
            interfaces.capture_ownership = lambda: None  # type: ignore[method-assign]
            interfaces.setup_apply()
            interfaces.controller(
                "POST",
                "/v1/applications",
                {"slug": "acceptance-12345678"},
                key="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                mutating=True,
            )
            mutating_indexes = [
                index for index, (_argv, mutating, _stdin) in enumerate(commands.calls) if mutating
            ]
            self.assertEqual(len(mutating_indexes), 2)
            for index in mutating_indexes:
                self.assertIn("token", commands.calls[index - 1][0])
            all_arguments = "\n".join(
                argument for argv, _mutating, _stdin in commands.calls for argument in argv
            )
            self.assertNotIn("OS_PASSWORD", all_arguments)
            self.assertNotIn("AGE-SECRET-KEY", all_arguments)
            controller_stdin = commands.calls[mutating_indexes[-1]][2]
            self.assertEqual(json.loads(controller_stdin), {"slug": "acceptance-12345678"})

    def test_every_declared_action_is_composed_and_returns_exact_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            fake = FakeInterfaces(value)
            driver = RepositoryLiveDriver(value, fake)  # type: ignore[arg-type]
            plan = driver.handle(
                {
                    "schemaVersion": 1,
                    "mode": "plan",
                    "action": "full_drill",
                    "scope": {
                        "deploymentId": DEPLOYMENT,
                        "projectId": PROJECT,
                        "namespace": NAMESPACE,
                    },
                    "requiredActions": list(acceptance.ACTION_NAMES),
                    "bounds": {"maxMinutes": 60, "stepTimeoutSeconds": 60},
                }
            )
            self.assertEqual(plan["ownedResources"], [])
            for action, required in acceptance.ACTION_CHECKS:
                response = driver.handle(
                    {
                        "schemaVersion": 1,
                        "mode": "execute",
                        "action": action,
                        "scope": {
                            "deploymentId": DEPLOYMENT,
                            "projectId": PROJECT,
                            "namespace": NAMESPACE,
                        },
                        "planSha256": "a" * 64,
                        "driverConfigurationSha256": hashlib.sha256(
                            value.path.read_bytes()
                        ).hexdigest(),
                        "baselineFingerprint": "b" * 64,
                    }
                )
                self.assertEqual(response["checks"], {name: True for name in required})
            self.assertIn(("replace", "ingress", True), fake.calls)
            self.assertIn(("replace", "admin", True), fake.calls)
            self.assertEqual(fake.calls[-1], ("teardown", True))

    def test_interruption_requires_the_stable_public_content_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            fake = FakeInterfaces(value)
            fake.verify_public_storage = lambda _url, _expected: {  # type: ignore[method-assign]
                "publicRouteHealthy": True,
                "storageBound": False,
                "postgresWriteReadVerified": False,
                "mongoWriteReadVerified": False,
                "s3WriteReadVerified": False,
            }
            driver = RepositoryLiveDriver(value, fake)  # type: ignore[arg-type]
            with self.assertRaisesRegex(LiveDriverError, "did not establish"):
                driver.handle(self._execute_request(value, "interrupted_resume_injection"))

    def test_reenable_requires_a_live_public_route_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            fake = FakeInterfaces(value)
            fake.verify_public_storage = lambda _url, _expected: {  # type: ignore[method-assign]
                "publicRouteHealthy": False,
                "storageBound": True,
                "postgresWriteReadVerified": True,
                "mongoWriteReadVerified": True,
                "s3WriteReadVerified": True,
            }
            driver = RepositoryLiveDriver(value, fake)  # type: ignore[arg-type]
            with self.assertRaisesRegex(LiveDriverError, "did not establish"):
                driver.handle(self._execute_request(value, "application_disable_enable"))

    def test_missing_concrete_observation_fails_instead_of_synthesizing_true(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            fake = FakeInterfaces(value)
            original = fake.setup_apply

            def incomplete() -> dict[str, bool]:
                result = original()
                result["platformHealthy"] = False
                return result

            fake.setup_apply = incomplete  # type: ignore[method-assign]
            driver = RepositoryLiveDriver(value, fake)  # type: ignore[arg-type]
            with self.assertRaisesRegex(LiveDriverError, "did not establish"):
                driver.handle(
                    {
                        "schemaVersion": 1,
                        "mode": "execute",
                        "action": "greenfield_setup",
                        "scope": {
                            "deploymentId": DEPLOYMENT,
                            "projectId": PROJECT,
                            "namespace": NAMESPACE,
                        },
                        "planSha256": "a" * 64,
                        "driverConfigurationSha256": hashlib.sha256(
                            value.path.read_bytes()
                        ).hexdigest(),
                        "baselineFingerprint": "b" * 64,
                    }
                )

    def test_each_exact_content_field_is_independently_falsifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            interfaces = SupportedInterfaces(value, FakeCommandTransport())
            expected = value.application["contentProof"]
            assert isinstance(expected, Mapping)
            check_names = {
                "postgres": "postgresWriteReadVerified",
                "mongo": "mongoWriteReadVerified",
                "s3": "s3WriteReadVerified",
            }
            for field, check in check_names.items():
                observed = dict(expected)
                observed[field] = {"value": "falsified"}
                interfaces.public_json = lambda _url, value=observed: value  # type: ignore[method-assign]
                with self.subTest(field=field):
                    checks = interfaces.verify_public_storage("https://example.test", expected)
                    self.assertIs(checks[check], False)
                    self.assertIs(checks["storageBound"], False)
                    for other in set(check_names.values()) - {check}:
                        self.assertIs(checks[other], True)

    def test_candidate_check_fails_for_each_missing_typed_lifecycle_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            for field in ("imageDigest", "nomadVersion", "acceptedAt", "lastHealthyAt"):
                fake = FakeInterfaces(value)
                original_list = fake.list_items

                def missing_candidate_field(
                    path: str, missing: str = field, listing: object = original_list
                ) -> list[Mapping[str, object]]:
                    assert callable(listing)
                    items = [dict(item) for item in listing(path)]
                    if "/deployments" in path and items:
                        items[0][missing] = None
                    return items

                fake.list_items = missing_candidate_field  # type: ignore[method-assign]
                driver = RepositoryLiveDriver(value, fake)  # type: ignore[arg-type]
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(LiveDriverError, "did not establish"),
                ):
                    driver.handle(self._execute_request(value, "application_deploy"))

    def test_persistent_retention_fails_for_each_exact_content_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            for failed in (
                "postgresWriteReadVerified",
                "mongoWriteReadVerified",
                "s3WriteReadVerified",
            ):
                fake = FakeInterfaces(value)
                original = fake.verify_public_storage

                def falsified(
                    url: str,
                    expected: Mapping[str, object],
                    field: str = failed,
                    verify: object = original,
                ) -> dict[str, bool]:
                    assert callable(verify)
                    result = verify(url, expected)
                    result[field] = False
                    return result

                fake.verify_public_storage = falsified  # type: ignore[method-assign]
                driver = RepositoryLiveDriver(value, fake)  # type: ignore[arg-type]
                with (
                    self.subTest(failed=failed),
                    self.assertRaisesRegex(LiveDriverError, "did not establish"),
                ):
                    driver.handle(self._execute_request(value, "persistent_host_replacement"))

    @staticmethod
    def _execute_request(value: DriverConfig, action: str) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "mode": "execute",
            "action": action,
            "scope": {
                "deploymentId": DEPLOYMENT,
                "projectId": PROJECT,
                "namespace": NAMESPACE,
            },
            "planSha256": "a" * 64,
            "driverConfigurationSha256": hashlib.sha256(value.path.read_bytes()).hexdigest(),
            "baselineFingerprint": "b" * 64,
        }

    def test_managed_restore_false_observation_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            fake = FakeInterfaces(value)
            fake.managed_restore = lambda _url: {  # type: ignore[method-assign]
                "postgresRestored": True,
                "mongoRestored": True,
                "s3Restored": False,
                "restoreManifestVerified": True,
            }
            driver = RepositoryLiveDriver(value, fake)  # type: ignore[arg-type]
            with self.assertRaisesRegex(LiveDriverError, "did not establish"):
                driver.handle(
                    {
                        "schemaVersion": 1,
                        "mode": "execute",
                        "action": "managed_data_restore",
                        "scope": {
                            "deploymentId": DEPLOYMENT,
                            "projectId": PROJECT,
                            "namespace": NAMESPACE,
                        },
                        "planSha256": "a" * 64,
                        "driverConfigurationSha256": hashlib.sha256(
                            value.path.read_bytes()
                        ).hexdigest(),
                        "baselineFingerprint": "b" * 64,
                    }
                )

    def test_substring_ownership_adversary_is_rejected_before_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            resource = {
                "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "name": "wrong-name",
                "project_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "description": f"expected-name {PROJECT}",
                "properties": {
                    f"{NAMESPACE.replace('-', '_')}_managed_by": "platform",
                    f"{NAMESPACE.replace('-', '_')}_namespace": NAMESPACE,
                    f"{NAMESPACE.replace('-', '_')}_project_id": PROJECT,
                },
            }
            transport = StaticOutputTransport(resource)
            interfaces = SupportedInterfaces(value, transport)
            with self.assertRaisesRegex(LiveDriverError, "name differs"):
                interfaces._show_projection("server", str(resource["id"]), "expected-name")
            self.assertFalse(any("delete" in call[0] for call in transport.calls))

    def test_keypair_without_exact_user_projection_is_not_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            transport = StaticOutputTransport(
                {
                    "name": "example-admin",
                    "fingerprint": "aa:bb",
                    "public_key": "ssh-ed25519 AAAA",
                    "type": "ssh",
                    "description": f"user_id={PROJECT}",
                }
            )
            with self.assertRaisesRegex(LiveDriverError, "required field"):
                SupportedInterfaces(value, transport)._show_projection(
                    "keypair", "example-admin", "example-admin"
                )
            self.assertFalse(any("delete" in call[0] for call in transport.calls))

    def _teardown_fixture(
        self, root: Path, executable: Path
    ) -> tuple[SupportedInterfaces, TeardownTransport, str]:
        value = config(root, executable)
        name = "acceptance-backup"
        identifier = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        transport = TeardownTransport(name, identifier)
        interfaces = SupportedInterfaces(value, transport)
        interfaces._owned_names = lambda: {"volume": [name]}  # type: ignore[method-assign]
        ownership = {
            "schemaVersion": 1,
            "deploymentId": DEPLOYMENT,
            "projectId": PROJECT,
            "namespace": NAMESPACE,
            "resources": [
                {
                    "kind": "volume",
                    "name": name,
                    "deleteReference": identifier,
                    "projection": transport.projection,
                }
            ],
        }
        interfaces.ownership_path.write_text(json.dumps(ownership))
        interfaces.ownership_path.chmod(0o600)
        baseline = hashlib.sha256(b"[]").hexdigest()
        return interfaces, transport, baseline

    def test_teardown_faults_are_resumable_at_every_journal_boundary(self) -> None:
        phases = ("before_intent", "after_intent", "after_delete", "after_confirmation")
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                executable = root / "unused"
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o700)
                interfaces, transport, baseline = self._teardown_fixture(root, executable)
                platform = mock.Mock()
                platform.get.return_value = "acceptance-backup"
                original_record = interfaces._record_teardown
                if phase == "before_intent":

                    def fail_before(*_args: object, **_kwargs: object) -> object:
                        raise RuntimeError("fault before intent")

                    interfaces._record_teardown = fail_before  # type: ignore[method-assign]
                elif phase == "after_intent":
                    transport.fail_delete_once = True
                elif phase == "after_delete":

                    def fail_after_delete(
                        entries: object,
                        intent: object,
                        status: str,
                        record: object = original_record,
                    ) -> object:
                        if status == "confirmed":
                            raise RuntimeError("fault after provider deletion")
                        assert callable(record)
                        return record(entries, intent, status)

                    interfaces._record_teardown = fail_after_delete  # type: ignore[method-assign]
                else:

                    def fail_after_confirmation(
                        entries: object,
                        intent: object,
                        status: str,
                        record: object = original_record,
                    ) -> object:
                        assert callable(record)
                        result = record(entries, intent, status)
                        if status == "confirmed":
                            raise RuntimeError("fault after confirmation")
                        return result

                    interfaces._record_teardown = fail_after_confirmation  # type: ignore[method-assign]
                with (
                    mock.patch(
                        "openstack_platform.acceptance_live_driver.load_platform",
                        return_value=platform,
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    interfaces.teardown(baseline)
                journal = json.loads(interfaces.teardown_progress_path.read_text())
                statuses = [entry["status"] for entry in journal["entries"]]
                expected_statuses = {
                    "before_intent": [],
                    "after_intent": ["intended"],
                    "after_delete": ["intended"],
                    "after_confirmation": ["confirmed"],
                }
                self.assertEqual(statuses, expected_statuses[phase])
                self.assertEqual(transport.live, phase in {"before_intent", "after_intent"})
                interfaces._record_teardown = original_record  # type: ignore[method-assign]
                with mock.patch(
                    "openstack_platform.acceptance_live_driver.load_platform",
                    return_value=platform,
                ):
                    result = interfaces.teardown(baseline)
                self.assertTrue(all(result.values()))
                self.assertFalse(transport.live)
                confirmed = json.loads(interfaces.teardown_progress_path.read_text())
                self.assertEqual(confirmed["entries"][0]["status"], "confirmed")

    def test_teardown_refuses_uncheckpointed_disappeared_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            interfaces = SupportedInterfaces(value, FakeCommandTransport())
            ownership = {
                "schemaVersion": 1,
                "deploymentId": DEPLOYMENT,
                "projectId": PROJECT,
                "namespace": NAMESPACE,
                "resources": [
                    {
                        "kind": "keypair",
                        "name": "demo-admin",
                        "deleteReference": "demo-admin",
                        "projection": {
                            "name": "demo-admin",
                            "fingerprint": "aa:bb",
                            "publicKey": "ssh-ed25519 AAAA",
                            "type": "ssh",
                            "userId": "user-1",
                        },
                    }
                ],
            }
            interfaces.ownership_path.write_text(json.dumps(ownership))
            interfaces.ownership_path.chmod(0o600)
            with self.assertRaisesRegex(LiveDriverError, "disappeared"):
                interfaces.teardown("b" * 64)

    def test_teardown_refuses_absent_immutable_ownership_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            transport = FakeCommandTransport()
            with self.assertRaisesRegex(LiveDriverError, "ownership metadata is absent"):
                SupportedInterfaces(value, transport).teardown("b" * 64)
            self.assertFalse(any(mutating for _argv, mutating, _stdin in transport.calls))

    def test_real_plan_transport_transcript_contains_only_non_mutating_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / "fake-supported-interface"
            program.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "a=sys.argv[1:]\n"
                f"print(json.dumps({{'project_id':'{PROJECT}'}}) if 'token' in a else ('[]' if 'list' in a else 'setup plan'))\n"
            )
            program.chmod(0o700)
            value = config(root, program)
            transport = SubprocessTransport(Path(value.transcript))
            driver = RepositoryLiveDriver(value, SupportedInterfaces(value, transport))
            driver.handle(
                {
                    "schemaVersion": 1,
                    "mode": "plan",
                    "action": "full_drill",
                    "scope": {
                        "deploymentId": DEPLOYMENT,
                        "projectId": PROJECT,
                        "namespace": NAMESPACE,
                    },
                    "requiredActions": list(acceptance.ACTION_NAMES),
                    "bounds": {"maxMinutes": 60, "stepTimeoutSeconds": 60},
                }
            )
            transcript = json.loads(Path(value.transcript).read_text())
            self.assertEqual(transcript["mutationCount"], 0)
            self.assertGreaterEqual(len(transcript["commands"]), 10)
            self.assertTrue(all(command["mutating"] is False for command in transcript["commands"]))
            rendered = Path(value.transcript).read_text()
            self.assertNotIn(PROJECT, rendered)
            self.assertNotIn("--apply", rendered)
            self.assertIn("setup", rendered)
            self.assertIn("<absolute>/input.env", rendered)

    def test_scope_mismatch_stops_before_any_interface(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "unused"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            value = config(root, executable)
            fake = FakeInterfaces(value)
            driver = RepositoryLiveDriver(value, fake)  # type: ignore[arg-type]
            with self.assertRaisesRegex(Exception, "scope differs"):
                driver.handle(
                    {
                        "schemaVersion": 1,
                        "mode": "plan",
                        "action": "full_drill",
                        "scope": {
                            "deploymentId": DEPLOYMENT,
                            "projectId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                            "namespace": NAMESPACE,
                        },
                        "requiredActions": list(acceptance.ACTION_NAMES),
                        "bounds": {"maxMinutes": 60, "stepTimeoutSeconds": 60},
                    }
                )
            self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
