from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from openstack_platform import acceptance

DEPLOYMENT_ID = "12345678-1234-4234-9234-123456789abc"
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NAMESPACE = "p07-release-12345678"


class FakeDriver:
    def __init__(self, *, fail_once: str | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_once = fail_once
        self.failed = False
        self.owned_resources: set[str] = set()
        self.unrelated_resources = {"sentinel-server": "ACTIVE", "sentinel-volume": "available"}

    def request(
        self, document: acceptance.Mapping[str, object], *, timeout: int
    ) -> acceptance.Mapping[str, object]:
        request = dict(document)
        self.calls.append(request)
        scope = request["scope"]
        assert isinstance(scope, dict)
        action = request["action"]
        if request["mode"] == "plan":
            return {
                "schemaVersion": 1,
                "ok": True,
                **scope,
                "capabilities": list(acceptance.ACTION_NAMES),
                "baselineFingerprint": "b" * 64,
                "ownedResources": [],
            }
        assert isinstance(action, str)
        if action == self.fail_once and not self.failed:
            self.failed = True
            raise acceptance.AcceptanceError("injected safe failure")
        if action == "greenfield_setup":
            self.owned_resources.update({"admin", "ingress", "storage"})
        elif action == "application_create":
            self.owned_resources.add("application")
        elif action in {"postgres_lifecycle", "mongo_lifecycle", "s3_lifecycle"}:
            self.owned_resources.add(action.removesuffix("_lifecycle"))
        elif action == "application_delete":
            self.owned_resources.difference_update({"application", "postgres", "mongo", "s3"})
        elif action == "cleanup_verify":
            self.owned_resources.clear()
        checks = dict(acceptance.ACTION_CHECKS)[action]
        return {
            "schemaVersion": 1,
            "ok": True,
            **scope,
            "action": action,
            "checks": {check: True for check in checks},
        }


def make_plan(driver: FakeDriver) -> acceptance.Plan:
    return acceptance.create_plan(
        driver,
        deployment_id=DEPLOYMENT_ID,
        project_id=PROJECT_ID,
        namespace=NAMESPACE,
        driver_sha256="d" * 64,
        max_minutes=60,
        step_timeout_seconds=60,
    )


class LiveAcceptanceTests(unittest.TestCase):
    def private_directory(self, root: str, name: str) -> Path:
        path = Path(root) / name
        path.mkdir(mode=0o700)
        return path

    def test_plan_is_non_mutating_exact_and_deployment_scoped(self) -> None:
        driver = FakeDriver()
        plan = make_plan(driver)
        self.assertEqual(len(driver.calls), 1)
        call = driver.calls[0]
        self.assertEqual(call["mode"], "plan")
        self.assertEqual(call["requiredActions"], list(acceptance.ACTION_NAMES))
        self.assertEqual(
            call["scope"],
            {
                "deploymentId": DEPLOYMENT_ID,
                "projectId": PROJECT_ID,
                "namespace": NAMESPACE,
            },
        )
        self.assertEqual(plan.baseline_fingerprint, "b" * 64)
        self.assertEqual(
            [item["name"] for item in plan.document()["actions"]], list(acceptance.ACTION_NAMES)
        )  # type: ignore[index]

    def test_refuses_non_disposable_namespace_and_preexisting_owned_resource(self) -> None:
        driver = FakeDriver()
        with self.assertRaisesRegex(acceptance.AcceptanceError, "namespace must be disposable"):
            acceptance.create_plan(
                driver,
                deployment_id=DEPLOYMENT_ID,
                project_id=PROJECT_ID,
                namespace="production",
                driver_sha256="d" * 64,
                max_minutes=60,
                step_timeout_seconds=60,
            )

        class DirtyDriver(FakeDriver):
            def request(
                self, document: acceptance.Mapping[str, object], *, timeout: int
            ) -> acceptance.Mapping[str, object]:
                response = dict(super().request(document, timeout=timeout))
                response["ownedResources"] = ["already-there"]
                return response

        with self.assertRaisesRegex(acceptance.AcceptanceError, "already contains"):
            make_plan(DirtyDriver())

    def test_interruption_is_checkpointed_and_next_run_resumes_without_replay(self) -> None:
        driver = FakeDriver(fail_once="postgres_lifecycle")
        plan = make_plan(driver)
        with tempfile.TemporaryDirectory() as temporary:
            state = self.private_directory(temporary, "state")
            with self.assertRaisesRegex(acceptance.AcceptanceError, "injected safe failure"):
                acceptance.run_plan(
                    driver,
                    plan,
                    plan_sha256="a" * 64,
                    state_directory=state,
                    signing_key=b"k" * 32,
                )
            checkpoint = json.loads((state / "checkpoint.json").read_text())
            self.assertEqual(
                [event["action"] for event in checkpoint["events"]],
                [
                    "greenfield_setup",
                    "interrupted_resume_injection",
                    "interrupted_resume",
                    "application_create",
                ],
            )
            acceptance.run_plan(
                driver,
                plan,
                plan_sha256="a" * 64,
                state_directory=state,
                signing_key=b"k" * 32,
            )
            execution_actions = [
                str(call["action"]) for call in driver.calls if call["mode"] == "execute"
            ]
            self.assertEqual(execution_actions.count("greenfield_setup"), 1)
            self.assertEqual(execution_actions.count("postgres_lifecycle"), 2)
            self.assertEqual(execution_actions[-1], "cleanup_verify")
            self.assertEqual(driver.owned_resources, set())
            self.assertEqual(
                driver.unrelated_resources,
                {"sentinel-server": "ACTIVE", "sentinel-volume": "available"},
            )

    def test_tampered_checkpoint_cannot_skip_a_live_action(self) -> None:
        driver = FakeDriver(fail_once="postgres_lifecycle")
        plan = make_plan(driver)
        with tempfile.TemporaryDirectory() as temporary:
            state = self.private_directory(temporary, "state")
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance.run_plan(
                    driver,
                    plan,
                    plan_sha256="a" * 64,
                    state_directory=state,
                    signing_key=b"k" * 32,
                )
            checkpoint_path = state / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["events"][0]["checks"]["platformHealthy"] = False
            checkpoint_path.write_text(json.dumps(checkpoint))
            with self.assertRaisesRegex(acceptance.AcceptanceError, "checks are invalid"):
                acceptance.run_plan(
                    driver,
                    plan,
                    plan_sha256="a" * 64,
                    state_directory=state,
                    signing_key=b"k" * 32,
                )

    def test_evidence_is_fixed_sanitized_chained_checksummed_and_signed(self) -> None:
        driver = FakeDriver()
        plan = make_plan(driver)
        with tempfile.TemporaryDirectory() as temporary:
            state = self.private_directory(temporary, "state")
            evidence_path = acceptance.run_plan(
                driver,
                plan,
                plan_sha256="a" * 64,
                state_directory=state,
                signing_key=b"private-signing-material" * 2,
            )
            acceptance.verify_evidence(state, b"private-signing-material" * 2)
            evidence = json.loads(evidence_path.read_text())
            self.assertEqual(evidence["result"], "passed")
            self.assertEqual(len(evidence["events"]), len(acceptance.ACTION_NAMES))
            self.assertNotIn("private-signing-material", evidence_path.read_text())
            self.assertNotIn("provider", evidence_path.read_text().lower())
            previous = "0" * 64
            for event in evidence["events"]:
                self.assertEqual(event["previousSha256"], previous)
                without_hash = {key: value for key, value in event.items() if key != "eventSha256"}
                self.assertEqual(
                    event["eventSha256"], acceptance._sha256(acceptance._canonical(without_hash))
                )
                previous = event["eventSha256"]
            evidence_path.write_bytes(evidence_path.read_bytes() + b" ")
            with self.assertRaisesRegex(acceptance.AcceptanceError, "checksum"):
                acceptance.verify_evidence(state, b"private-signing-material" * 2)

    def test_wrong_check_scope_plan_or_key_is_rejected(self) -> None:
        class BadDriver(FakeDriver):
            def request(
                self, document: acceptance.Mapping[str, object], *, timeout: int
            ) -> acceptance.Mapping[str, object]:
                response = dict(super().request(document, timeout=timeout))
                if document["mode"] == "execute":
                    response["projectId"] = str(UUID(int=0))
                return response

        plan = make_plan(BadDriver())
        with tempfile.TemporaryDirectory() as temporary:
            state = self.private_directory(temporary, "state")
            with self.assertRaisesRegex(acceptance.AcceptanceError, "exact scope"):
                acceptance.run_plan(
                    BadDriver(),
                    plan,
                    plan_sha256="a" * 64,
                    state_directory=state,
                    signing_key=b"k" * 32,
                )

        with tempfile.TemporaryDirectory() as temporary:
            state = self.private_directory(temporary, "state")
            good = FakeDriver()
            plan = make_plan(good)
            acceptance.run_plan(
                good,
                plan,
                plan_sha256="a" * 64,
                state_directory=state,
                signing_key=b"k" * 32,
            )
            with self.assertRaisesRegex(acceptance.AcceptanceError, "signature"):
                acceptance.verify_evidence(state, b"x" * 32)


if __name__ == "__main__":
    unittest.main()
