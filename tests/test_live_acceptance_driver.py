from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
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
                        "status": "accepted",
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

    def list_items(self, path: str) -> list[Mapping[str, object]]:
        self.calls.append(("controller-list", path, False))
        if "/deployments" in path:
            return list(self.deployments)
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
            "candidateVerified": True,
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
