from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace

from platform_cli.app import Manifest, nomad_candidate_identity, render_nomad_job
from platform_cli.config import PlatformConfig
from platform_cli.helper.app import _public_health_from_job, _status_or_absent, handlers
from platform_cli.helper.main import HelperActionError
from platform_cli.helper.nomad import SecretItems, VariableSnapshot
from platform_cli.runtime import CommandResult, CommandTimedOut
from platform_cli.validation import ValidationError

SENTINEL = "do-not-render-this-secret"
CANDIDATE_SHA = "a" * 64
COMMIT = "c" * 40
CANDIDATE_IMAGE = "registry.example/projects/demo-app/app@sha256:" + "b" * 64


class FakeVariableClient:
    def __init__(self) -> None:
        self.index = 2
        self.items = SecretItems({"DATABASE_URL": "preserved"})
        self.written: SecretItems | None = None

    def read_variable(self, path: str) -> VariableSnapshot:
        return VariableSnapshot(path, self.index, self.items)

    def compare_and_set(self, path: str, expected_index: int, items: object) -> int:
        if expected_index != self.index:
            raise AssertionError("wrong CAS index")
        self.written = SecretItems(dict(items))  # type: ignore[arg-type]
        self.items = self.written
        self.index += 1
        return self.index


class FakeNomad:
    def __init__(self, variables: FakeVariableClient) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.variables = variables
        self.job_absent = False
        self.status = {"ID": "demo-app", "Version": 7, "Status": "running"}
        self.inspection = {
            "ID": "demo-app",
            "Version": 7,
            "Meta": {
                "m1_candidate_job_sha256": CANDIDATE_SHA,
                "m1_candidate_image": CANDIDATE_IMAGE,
            },
            "TaskGroups": [
                {
                    "Name": "app",
                    "Tasks": [{"Name": "app", "Config": {"image": CANDIDATE_IMAGE}}],
                }
            ],
        }
        self.allocations = [
            {
                "ID": "alloc-123",
                "JobVersion": 7,
                "ClientStatus": "running",
                "DesiredStatus": "run",
                "DeploymentStatus": {"Healthy": True},
                "CreateIndex": 20,
            }
        ]

    def __call__(self, argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        self.calls.append((tuple(argv), dict(kwargs)))
        returncode = 0
        if "stop" in argv:
            self.job_absent = True
            output = b""
        elif "purge" in argv:
            self.variables.index = 0
            self.variables.items = SecretItems({})
            output = b""
        elif "status" in argv:
            output = json.dumps(self.status).encode()
            returncode = 1 if self.job_absent else 0
        elif "restart" in argv:
            self.allocations[0]["ModifyIndex"] = self.allocations[0].get("ModifyIndex", 0) + 1
            output = b""
        elif "allocs" in argv:
            output = json.dumps(self.allocations).encode()
        elif "inspect" in argv:
            output = json.dumps(self.inspection).encode()
        elif "logs" in argv:
            output = b"bounded application output\n"
        else:
            output = b""
        stderr = b'No job(s) with prefix or ID "demo-app" found\n' if returncode == 1 else b""
        return SimpleNamespace(
            stdout=output,
            stderr=stderr,
            stdout_truncated=False,
            returncode=returncode,
        )


class ApplicationHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.variables = FakeVariableClient()
        self.nomad = FakeNomad(self.variables)
        self.actions = handlers(
            self.variables,
            command_runner=self.nomad,
            nomad_command=("fixed-nomad-wrapper",),
            timeout_seconds=12,
            response_limit=4096,
            environment_health_attempts=1,
            environment_poll_interval_seconds=0,
            public_health_check=lambda _slug: True,
            sleep=lambda _seconds: None,
        )

    def test_action_surface_is_complete_and_small(self) -> None:
        self.assertEqual(
            set(self.actions),
            {
                "app.deploy",
                "app.health",
                "app.logs",
                "app.remove",
                "app.env.set",
                "app.env.remove",
                "app.env.list",
            },
        )

    def test_deploy_rejects_unmarked_pre_m1_job_before_provider_calls(self) -> None:
        job = 'job "demo-app" { # sentinel job data\n}'
        with self.assertRaisesRegex(ValidationError, "M1 candidate marker"):
            self.actions["app.deploy"]({"slug": "demo-app", "job": job})
        self.assertEqual(self.nomad.calls, [])

    def test_inspect_dependency_failure_blocks_submit_and_remove(self) -> None:
        unmarked = 'job "demo-app" {\n\n}\n'
        identity = hashlib.sha256(unmarked.encode()).hexdigest()
        job = unmarked.replace(
            'job "demo-app" {\n',
            'job "demo-app" {\n'
            "  meta {\n"
            f'    m1_candidate_job_sha256 = "{identity}"\n'
            f'    m1_candidate_image      = "{CANDIDATE_IMAGE}"\n'
            f'    m1_source_commit        = "{COMMIT}"\n'
            f'    m1_recipe_sha256        = "{"d" * 64}"\n'
            "  }\n\n",
            1,
        )
        calls: list[tuple[str, ...]] = []

        def unavailable(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            calls.append(argv)
            if "inspect" in argv:
                return SimpleNamespace(
                    stdout=b"",
                    stderr=b"permission denied\n",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    returncode=1,
                )
            return SimpleNamespace(
                stdout=b"",
                stderr=b"",
                stdout_truncated=False,
                stderr_truncated=False,
                returncode=0,
            )

        actions = handlers(
            self.variables,
            command_runner=unavailable,
            nomad_command=("fixed-nomad-wrapper",),
        )
        with self.assertRaises(HelperActionError):
            actions["app.deploy"]({"slug": "demo-app", "job": job})
        with self.assertRaises(HelperActionError):
            actions["app.remove"]({"slug": "demo-app"})
        self.assertFalse(any("run" in argv or "stop" in argv for argv in calls))

    def test_interrupted_post_submission_recovery_observes_exact_candidate_without_resubmit(
        self,
    ) -> None:
        platform = PlatformConfig(
            project_name="project",
            project_id="12345678-1234-4234-8234-123456789abc",
            prefix="example",
            namespace="app-platform",
            domain="apps.example.com",
            datacenter="dc1",
            region="global",
            network="private",
            document={},
        )
        image = "registry.example/projects/demo-app/app@sha256:" + "a" * 64
        job = render_nomad_job(
            application_id="12345678-1234-4234-8234-123456789abc",
            application_slug="demo-app",
            image=image,
            manifest=Manifest("node", (".",), None, "start", 3000, "/health"),
            platform=platform,
            cpu_mhz=1000,
            memory_mib=2048,
            source_commit=COMMIT,
            recipe_hash="c" * 64,
        )
        candidate = nomad_candidate_identity(job)
        assert candidate is not None
        submitted = 0
        current: dict[str, object] | None = None
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            nonlocal submitted, current
            calls.append((argv, kwargs))
            returncode = 0
            output = b""
            if "inspect" in argv:
                if current is None:
                    returncode = 1
                else:
                    output = json.dumps(current).encode()
            elif "run" in argv:
                submitted += 1
                current = {
                    "ID": "demo-app",
                    "Version": 8,
                    "Meta": {
                        "m1_candidate_job_sha256": candidate[0],
                        "m1_candidate_image": candidate[1],
                    },
                    "TaskGroups": [
                        {
                            "Name": "app",
                            "Tasks": [{"Name": "app", "Config": {"image": candidate[1]}}],
                        }
                    ],
                }
            return SimpleNamespace(
                stdout=output,
                stderr=(
                    b'No job(s) with prefix or ID "demo-app" found\n' if returncode == 1 else b""
                ),
                stdout_truncated=False,
                stderr_truncated=False,
                returncode=returncode,
            )

        actions = handlers(
            self.variables,
            command_runner=runner,
            nomad_command=("fixed-nomad-wrapper",),
        )
        first = actions["app.deploy"]({"slug": "demo-app", "job": job})
        recovered = actions["app.deploy"]({"slug": "demo-app", "job": job})
        self.assertTrue(first["submitted"])
        self.assertFalse(recovered["submitted"])
        self.assertEqual(recovered["candidateJobSha256"], candidate[0])
        self.assertEqual(recovered["candidateImage"], candidate[1])
        self.assertEqual(recovered["nomadVersion"], 8)
        self.assertEqual(submitted, 1)
        validation_calls = [call for call in calls if "validate" in call[0]]
        self.assertEqual(len(validation_calls), 2)
        self.assertTrue(
            all(
                call[0] == ("fixed-nomad-wrapper", "job", "validate", "-")
                for call in validation_calls
            )
        )
        self.assertTrue(all(call[1]["stdin"] == job.encode() for call in validation_calls))

    def test_public_health_rejects_a_nomad_route_outside_the_trusted_domain(self) -> None:
        inspection = {
            "TaskGroups": [
                {
                    "Name": "app",
                    "Services": [
                        {
                            "Name": "app-demo-app",
                            "Tags": [
                                "traefik.http.routers.demo-app.rule=Host(`demo-app.attacker.example`)"
                            ],
                            "Checks": [{"Type": "http", "Path": "/health"}],
                        }
                    ],
                }
            ]
        }
        calls: list[str] = []

        def runner(argv, **kwargs):
            calls.append(" ".join(argv))
            return SimpleNamespace(stdout=json.dumps(inspection).encode())

        with self.assertRaisesRegex(HelperActionError, "exact public health evidence"):
            _public_health_from_job(
                "demo-app",
                trusted_domain="apps.example.com",
                command_runner=runner,
                nomad_command=("fixed-nomad-wrapper",),
                timeout_seconds=5,
                response_limit=4096,
            )
        self.assertEqual(calls, ["fixed-nomad-wrapper job inspect -json demo-app"])

    def test_health_uses_current_version_allocations_without_raw_provider_data(self) -> None:
        args = {
            "slug": "demo-app",
            "version": 7,
            "candidateJobSha256": CANDIDATE_SHA,
            "candidateImage": CANDIDATE_IMAGE,
        }
        result = self.actions["app.health"](args)
        self.assertEqual(result["currentVersion"], 7)
        self.assertEqual(result["candidateJobSha256"], CANDIDATE_SHA)
        self.assertEqual(result["candidateImage"], CANDIDATE_IMAGE)
        self.assertEqual(result["allocations"], 1)
        self.assertTrue(result["healthy"])
        self.assertFalse(result["terminal"])
        self.nomad.allocations[0]["ClientStatus"] = "failed"
        failed = self.actions["app.health"](args)
        self.assertFalse(failed["healthy"])
        self.assertTrue(failed["terminal"])

    def test_health_accepts_nomad_two_status_projection_with_inspected_version(self) -> None:
        self.nomad.status = [
            {
                "Allocations": [],
                "Evaluations": [],
                "LatestDeployment": None,
                "Summary": {},
            }
        ]
        result = self.actions["app.health"](
            {
                "slug": "demo-app",
                "version": 7,
                "candidateJobSha256": CANDIDATE_SHA,
                "candidateImage": CANDIDATE_IMAGE,
            }
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["currentVersion"], 7)

    def test_health_rejects_stale_version_duplicate_allocations_and_candidate_metadata_drift(
        self,
    ) -> None:
        args = {
            "slug": "demo-app",
            "version": 7,
            "candidateJobSha256": CANDIDATE_SHA,
            "candidateImage": CANDIDATE_IMAGE,
        }
        duplicate = dict(self.nomad.allocations[0])
        duplicate["ID"] = "alloc-duplicate"
        self.nomad.allocations.append(duplicate)
        result = self.actions["app.health"](args)
        self.assertFalse(result["healthy"])
        self.assertTrue(result["terminal"])
        self.assertEqual(result["allocations"], 2)

        self.nomad.allocations.pop()
        self.nomad.inspection["Version"] = 8
        stale = self.actions["app.health"](args)
        self.assertFalse(stale["healthy"])
        self.assertTrue(stale["terminal"])

        self.nomad.inspection["Version"] = 7
        self.nomad.inspection["Meta"]["m1_candidate_job_sha256"] = "c" * 64
        drifted = self.actions["app.health"](args)
        self.assertFalse(drifted["healthy"])
        self.assertTrue(drifted["terminal"])

    def test_environment_updates_use_cas_preserve_other_owner_and_hide_values(self) -> None:
        result = self.actions["app.env.set"](
            {
                "slug": "demo-app",
                "updates": {"API_KEY": SENTINEL},
                "ownership": {"DATABASE_URL": "storage.postgres.default"},
            }
        )
        assert self.variables.written is not None
        self.assertEqual(self.variables.written["DATABASE_URL"], "preserved")
        self.assertEqual(self.variables.written["API_KEY"], SENTINEL)
        self.assertEqual(result["modifyIndex"], 3)
        self.assertEqual(result["keys"], ["API_KEY", "DATABASE_URL"])
        self.assertTrue(result["restarted"])
        self.assertTrue(result["schedulerHealthy"])
        self.assertTrue(result["publicHealthy"])
        self.assertIn(
            ("fixed-nomad-wrapper", "job", "restart", "-yes", "demo-app"),
            [argv for argv, _kwargs in self.nomad.calls],
        )
        self.assertNotIn(SENTINEL, repr(result))
        self.assertNotIn(SENTINEL, repr(self.variables.written))

    def test_platform_reconciliation_corrects_stale_node_env_and_preserves_sentinel(self) -> None:
        self.variables.items = SecretItems({"NODE_ENV": "development", "STAFF_SENTINEL": SENTINEL})
        result = self.actions["app.env.set"](
            {
                "slug": "demo-app",
                "updates": {"NODE_ENV": "production"},
                "ownership": {"NODE_ENV": "staff", "STAFF_SENTINEL": "staff"},
            }
        )
        self.assertEqual(self.variables.items["NODE_ENV"], "production")
        self.assertEqual(self.variables.items["STAFF_SENTINEL"], SENTINEL)
        self.assertTrue(result["restarted"])
        self.assertTrue(result["schedulerHealthy"])
        self.assertTrue(result["publicHealthy"])
        self.assertNotIn(SENTINEL, repr(result))
        self.assertNotIn(SENTINEL, repr(self.variables.items))

    def test_environment_health_failure_rolls_back_prior_values_in_memory(self) -> None:
        public = iter((False, True))
        actions = handlers(
            self.variables,
            command_runner=self.nomad,
            nomad_command=("fixed-nomad-wrapper",),
            timeout_seconds=12,
            response_limit=4096,
            environment_health_attempts=1,
            environment_poll_interval_seconds=0,
            public_health_check=lambda _slug: next(public),
            sleep=lambda _seconds: None,
        )
        with self.assertRaisesRegex(Exception, "prior values were restored"):
            actions["app.env.set"](
                {"slug": "demo-app", "updates": {"API_KEY": SENTINEL}, "ownership": {}}
            )
        self.assertEqual(dict(self.variables.items), {"DATABASE_URL": "preserved"})
        self.assertEqual(self.variables.index, 4)

    def test_environment_interruption_preserves_observable_current_state(self) -> None:
        def interrupting(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            if "restart" in argv:
                raise KeyboardInterrupt
            return self.nomad(argv, **kwargs)

        actions = handlers(
            self.variables,
            command_runner=interrupting,
            nomad_command=("fixed-nomad-wrapper",),
            environment_health_attempts=1,
            environment_poll_interval_seconds=0,
            public_health_check=lambda _slug: True,
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(KeyboardInterrupt):
            actions["app.env.set"](
                {"slug": "demo-app", "updates": {"API_KEY": SENTINEL}, "ownership": {}}
            )
        self.assertEqual(set(self.variables.items), {"DATABASE_URL", "API_KEY"})
        recovery = actions["app.env.list"]({"slug": "demo-app"})
        self.assertEqual(recovery["keys"], ["API_KEY", "DATABASE_URL"])
        self.assertIn("repeat set", recovery["interruptionRecovery"])

    def test_environment_for_absent_job_mutates_without_a_false_health_claim(self) -> None:
        self.nomad.job_absent = True
        result = self.actions["app.env.set"](
            {"slug": "demo-app", "updates": {"API_KEY": SENTINEL}, "ownership": {}}
        )
        self.assertFalse(result["restarted"])
        self.assertFalse(result["schedulerHealthy"])
        self.assertFalse(result["publicHealthy"])
        self.assertNotIn(
            ("fixed-nomad-wrapper", "job", "restart", "-yes", "demo-app"),
            [argv for argv, _kwargs in self.nomad.calls],
        )

    def test_removing_absent_keys_from_absent_variable_is_a_noop(self) -> None:
        self.variables.index = 0
        self.variables.items = SecretItems({})
        self.variables.written = None
        self.nomad.job_absent = True

        result = self.actions["app.env.remove"](
            {"slug": "demo-app", "keys": ["NODE_ENV"], "ownership": {"NODE_ENV": "staff"}}
        )

        self.assertEqual(result["modifyIndex"], 0)
        self.assertEqual(result["keys"], [])
        self.assertIsNone(self.variables.written)
        self.assertFalse(result["restarted"])
        self.assertFalse(result["schedulerHealthy"])
        self.assertFalse(result["publicHealthy"])

    def test_environment_cannot_change_storage_owned_key(self) -> None:
        with self.assertRaisesRegex(ValidationError, "owned by storage.postgres.default"):
            self.actions["app.env.set"](
                {
                    "slug": "demo-app",
                    "updates": {"DATABASE_URL": SENTINEL},
                    "ownership": {"DATABASE_URL": "storage.postgres.default"},
                }
            )

    def test_logs_and_remove_use_fixed_validated_argv(self) -> None:
        logs = self.actions["app.logs"]({"slug": "demo-app", "stderr": True, "lines": 20})
        self.assertEqual(logs["text"], "bounded application output\n")
        self.assertFalse(logs["followed"])
        log_argv = self.nomad.calls[-1][0]
        self.assertEqual(
            log_argv,
            (
                "fixed-nomad-wrapper",
                "alloc",
                "logs",
                "-tail",
                "-n",
                "20",
                "-stderr",
                "alloc-123",
                "app",
            ),
        )
        removed = self.actions["app.remove"]({"slug": "demo-app"})
        self.assertTrue(removed["removed"])
        self.assertTrue(removed["jobAbsent"])
        self.assertTrue(removed["variableAbsent"])
        removal_argvs = [call[0] for call in self.nomad.calls[-3:]]
        self.assertEqual(
            removal_argvs,
            [
                ("fixed-nomad-wrapper", "job", "stop", "-purge", "-yes", "demo-app"),
                ("fixed-nomad-wrapper", "job", "status", "-json", "demo-app"),
                (
                    "fixed-nomad-wrapper",
                    "var",
                    "purge",
                    "-force",
                    "nomad/jobs/demo-app",
                ),
            ],
        )

    def test_nomad_two_stopped_allocation_status_is_dead(self) -> None:
        payload = json.dumps(
            [
                {
                    "JobID": "demo-app",
                    "ClientStatus": "complete",
                    "DesiredStatus": "stop",
                }
            ]
        ).encode()

        def runner(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            return CommandResult(argv, 0, payload, b"", False, False, 0.1)

        self.assertEqual(
            _status_or_absent(
                "demo-app",
                command_runner=runner,
                nomad_command=("fixed-nomad-wrapper",),
                timeout_seconds=20,
                response_limit=65_536,
            ),
            {"ID": "demo-app", "Status": "dead"},
        )

    def test_follow_logs_preserves_partial_output_at_deadline(self) -> None:
        partial = CommandResult(
            argv=("fixed-nomad-wrapper",),
            returncode=-15,
            stdout=b"partial bounded output\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
            elapsed_seconds=12,
        )

        def timed_runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            if "logs" in argv:
                raise CommandTimedOut("deadline", partial)
            return self.nomad(argv, **kwargs)

        actions = handlers(
            self.variables,
            command_runner=timed_runner,
            nomad_command=("fixed-nomad-wrapper",),
            timeout_seconds=12,
            response_limit=4096,
        )
        logs = actions["app.logs"](
            {"slug": "demo-app", "stderr": False, "lines": 5, "follow": True}
        )
        self.assertEqual(logs["text"], "partial bounded output\n")
        self.assertTrue(logs["deadlineReached"])

    def test_follow_logs_is_explicit_and_bounded_by_the_fixed_runner_deadline(self) -> None:
        logs = self.actions["app.logs"](
            {"slug": "demo-app", "stderr": False, "lines": 5, "follow": True}
        )
        self.assertTrue(logs["followed"])
        self.assertFalse(logs["deadlineReached"])
        argv, kwargs = self.nomad.calls[-1]
        self.assertEqual(
            argv,
            (
                "fixed-nomad-wrapper",
                "alloc",
                "logs",
                "-tail",
                "-n",
                "5",
                "-f",
                "alloc-123",
                "app",
            ),
        )
        self.assertEqual(kwargs["timeout_seconds"], 12)
        self.assertEqual(kwargs["stdout_limit"], 4096)

    def test_unknown_arguments_and_unbounded_values_are_rejected_before_commands(self) -> None:
        count = len(self.nomad.calls)
        with self.assertRaisesRegex(Exception, "arguments"):
            self.actions["app.remove"]({"slug": "demo-app", "extra": True})
        with self.assertRaises(ValidationError):
            self.actions["app.env.set"](
                {
                    "slug": "demo-app",
                    "updates": {"bad-key": "value"},
                    "ownership": {},
                }
            )
        self.assertEqual(len(self.nomad.calls), count)


if __name__ == "__main__":
    unittest.main()
