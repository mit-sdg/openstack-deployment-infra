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
        self.app_id = str(uuid5(UUID(DEPLOYMENT), "application"))

    def setup_plan(self) -> None:
        self.calls.append(("setup-plan", False))

    def baseline(self) -> str:
        self.calls.append(("inventory", False))
        return "b" * 64

    def inventory(self) -> list[dict[str, object]]:
        return []

    def setup_apply(self) -> None:
        self.calls.append(("setup", True))

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
                    }
                )
        if path.endswith("/deployments") and isinstance(body, dict):
            if not any(item["deploymentId"] == key for item in self.deployments):
                self.deployments.append(
                    {
                        "deploymentId": key,
                        "repositoryCommit": body["commit"],
                    }
                )
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
        return 200, {
            "applicationId": self.app_id,
            "enabled": True,
            "url": "https://acceptance.example.test",
        }

    def interrupt_controller(self, operation_key: str) -> None:
        self.calls.append(("interrupt-controller", operation_key, True))

    def verify_public_storage(self, url: str, expected_commit: str) -> None:
        self.calls.append(("public-storage", url, expected_commit, False))

    def operator_backup_restore(self) -> None:
        self.calls.append(("operator-backup-restore", True))

    def hosted_restore(self) -> None:
        self.calls.append(("hosted-backup-restore", True))

    def managed_restore(self) -> None:
        self.calls.append(("managed-backup-restore", True))

    def replace(self, role: str) -> None:
        self.calls.append(("replace", role, True))

    def teardown(self, baseline: str) -> None:
        if baseline != "b" * 64 or not self.deleted or self.storage:
            raise AssertionError("cleanup did not receive converged scoped state")
        self.calls.append(("teardown", True))


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
