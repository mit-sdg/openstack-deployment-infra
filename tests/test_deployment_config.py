from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from platform_cli.deployment_config import (
    branch_name,
    parse_configuration,
    resolve_github_branch,
)
from platform_cli.validation import ValidationError

RESOURCE_ID = "11111111-1111-4111-8111-111111111111"
COMMIT = "a" * 40


def configuration() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "build": {
            "runtime": "node",
            "packages": ["."],
            "buildScript": "build",
            "startScript": "start",
        },
        "runtime": {"port": 3000, "healthPath": "/health"},
        "storageBindings": [
            {"resourceId": RESOURCE_ID, "outputs": {"url": "DATABASE_URL"}}
        ],
    }


class DeploymentConfigurationTests(unittest.TestCase):
    def test_configuration_is_strict_typed_and_canonical(self) -> None:
        parsed = parse_configuration(configuration())
        self.assertEqual(parsed.runtime, "node")
        self.assertEqual(parsed.packages, (".",))
        self.assertEqual(parsed.build_script, "build")
        self.assertEqual(parsed.start_script, "start")
        self.assertEqual(parsed.port, 3000)
        self.assertEqual(parsed.health_path, "/health")
        self.assertEqual(json.loads(parsed.canonical_json()), configuration())
        self.assertEqual(parse_configuration(parsed.canonical_json()), parsed)

    def test_unknown_duplicate_and_command_shaped_fields_are_rejected(self) -> None:
        unknown = configuration() | {"command": "rm -rf /"}
        with self.assertRaisesRegex(ValidationError, "fields"):
            parse_configuration(unknown)
        duplicate = json.dumps(configuration()).replace(
            '"schemaVersion": 1,',
            '"schemaVersion": 1, "schemaVersion": 1,',
        )
        with self.assertRaisesRegex(ValidationError, "strict JSON"):
            parse_configuration(duplicate)
        command = configuration()
        command["build"] = {
            "runtime": "node",
            "packages": ["."],
            "buildScript": None,
            "startScript": "start; cat /etc/passwd",
        }
        with self.assertRaises(ValidationError):
            parse_configuration(command)

    def test_paths_runtime_health_and_targets_are_bounded(self) -> None:
        invalid_values = (
            (
                "build",
                {
                    "runtime": "python",
                    "packages": ["."],
                    "buildScript": None,
                    "startScript": "start",
                },
            ),
            (
                "build",
                {
                    "runtime": "node",
                    "packages": ["../escape"],
                    "buildScript": None,
                    "startScript": "start",
                },
            ),
            ("runtime", {"port": 0, "healthPath": "/health"}),
            ("runtime", {"port": 3000, "healthPath": "relative"}),
            (
                "storageBindings",
                [{"resourceId": RESOURCE_ID, "outputs": {"url": "STORAGE__SECRET"}}],
            ),
        )
        for key, value in invalid_values:
            with self.subTest(key=key, value=value):
                document = configuration()
                document[key] = value
                with self.assertRaises(ValidationError):
                    parse_configuration(document)

    def test_storage_resources_are_resolved_to_typed_machine_names(self) -> None:
        parsed = parse_configuration(configuration())
        manifest = parsed.manifest({RESOURCE_ID: ("postgres", "primary")})
        self.assertEqual(manifest.storage_bindings[0].name, "primary")
        self.assertEqual(manifest.storage_bindings[0].resource_type, "postgres")
        with self.assertRaisesRegex(ValidationError, "not supported"):
            parsed.manifest({RESOURCE_ID: ("mongo", "primary")})
        with self.assertRaisesRegex(ValidationError, "missing or inactive"):
            parsed.manifest({})

    def test_binding_targets_and_resources_are_unique(self) -> None:
        document = configuration()
        document["storageBindings"] = [
            {"resourceId": RESOURCE_ID, "outputs": {"url": "DATABASE_URL"}},
            {"resourceId": RESOURCE_ID, "outputs": {"host": "DATABASE_HOST"}},
        ]
        with self.assertRaisesRegex(ValidationError, "resource.*unique"):
            parse_configuration(document)
        document["storageBindings"] = [
            {"resourceId": RESOURCE_ID, "outputs": {"url": "DATABASE_URL", "host": "DATABASE_URL"}}
        ]
        with self.assertRaisesRegex(ValidationError, "conflicts"):
            parse_configuration(document)


class BranchResolutionTests(unittest.TestCase):
    def test_branch_is_resolved_with_fixed_credential_free_git_argv(self) -> None:
        observed: dict[str, object] = {}

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            observed.update(argv=argv, kwargs=kwargs)
            return SimpleNamespace(stdout=f"{COMMIT}\trefs/heads/main\n".encode())

        resolved = resolve_github_branch(
            "https://github.com/example/project",
            command_runner=runner,
        )
        self.assertEqual(resolved, COMMIT)
        self.assertEqual(
            observed["argv"],
            (
                "git",
                "ls-remote",
                "--refs",
                "--exit-code",
                "https://github.com/example/project",
                "refs/heads/main",
            ),
        )
        kwargs = observed["kwargs"]
        assert isinstance(kwargs, dict)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_branch_and_response_are_exact(self) -> None:
        for value in ("", "../main", "main..next", "main.lock", "main//next", "@{bad"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                branch_name(value)
        with self.assertRaisesRegex(ValidationError, "one exact commit"):
            resolve_github_branch(
                "https://github.com/example/project",
                command_runner=lambda *_args, **_kwargs: SimpleNamespace(
                    stdout=f"{COMMIT}\trefs/heads/other\n".encode()
                ),
            )


if __name__ == "__main__":
    unittest.main()
