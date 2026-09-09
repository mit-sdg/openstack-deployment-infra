from __future__ import annotations

import importlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform.config import load_platform
from openstack_platform.controller import application_runtime as app
from openstack_platform.helper import production
from openstack_platform.helper.main import HelperActionError
from openstack_platform.runtime import CommandFailure, CommandResult
from openstack_platform.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]
BUILD = "11111111-1111-4111-8111-111111111111"
IMAGE = "22222222-2222-4222-8222-222222222222"


class BuildFailureCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.observation = app.BuilderObservation(
            BUILD, IMAGE, "builder", IMAGE, "port", "192.0.2.1", IMAGE, "builder", True
        )
        self.platform = load_platform(ROOT / "config/platform.example.json")
        self.runtime = SimpleNamespace(
            platform=self.platform, root=self.root, admin_state=self.root
        )

    def test_only_completed_remote_failure_is_classified_as_rejection(self):
        known_hosts = self.root / "known-hosts"
        known_hosts.write_text("fixture")
        known_hosts.chmod(0o600)
        for code in (1, 2, 255, -9, None):
            failure = app.ApplicationError("withheld")
            failure.__cause__ = CommandFailure(
                "withheld",
                None
                if code is None
                else CommandResult(("fixed",), code, b"", b"private-failure-output", False, False),
            )
            with (
                mock.patch.object(
                    app, "_builder_identity", return_value=str(self.root / "identity")
                ),
                mock.patch.object(app, "create_build_archive", return_value=(b"archive", "a" * 64)),
                mock.patch.object(
                    app,
                    "_provider_result",
                    side_effect=[
                        SimpleNamespace(
                            stdout=json.dumps({"buildId": BUILD, "sha256": "a" * 64}).encode()
                        ),
                        failure,
                    ],
                ),
            ):
                with self.assertRaises(app.ApplicationError) as caught:
                    app.execute_builder_build(
                        self.observation,
                        self.root,
                        mock.Mock(),
                        "registry.example/projects/demo-app/app",
                        known_hosts,
                        self.root / "identity",
                        source_limit=1024,
                        build_log_limit=1024,
                        timeout_seconds=30,
                        connect_timeout_seconds=5,
                    )
            self.assertEqual(isinstance(caught.exception, app.BuildRejected), code in (1, 2))
            self.assertNotIn("private-failure-output", str(caught.exception))

    def test_rejection_intent_survives_uncertain_builder_cleanup(self):
        for cleanup_error in (None, app.ApplicationError("cleanup unavailable")):
            with (
                mock.patch.object(app, "create_builder", return_value=self.observation),
                mock.patch.object(app, "pin_builder_host_key"),
                mock.patch.object(
                    app,
                    "execute_builder_build",
                    side_effect=app.BuildRejected("fixed rejected build"),
                ),
                mock.patch.object(app, "delete_builder", side_effect=cleanup_error) as cleanup,
            ):
                with self.assertRaises(app.BuildRejected):
                    app.build_with_disposable_builder(
                        build_id=BUILD,
                        source_directory=self.root,
                        recipe=mock.Mock(),
                        image_name="registry.example/projects/demo-app/app",
                        prefix="example",
                        selected_builder_image_id=IMAGE,
                        builder_flavor="builder",
                        known_hosts_directory=self.root / "known",
                        identity_path=self.root / "identity",
                    )
            cleanup.assert_called_once()

    def test_source_validation_failure_has_a_safe_explicit_rejection_code(self):
        arguments = {
            "buildId": BUILD,
            "slug": "demo-app",
            "repository": "https://github.com/example/app",
            "requestedRef": "main",
            "commit": "a" * 40,
            "configurationRevision": 1,
            "configuration": {
                "schemaVersion": 1,
                "build": {
                    "runtime": "node",
                    "packages": ["."],
                    "buildScript": None,
                    "startScript": "start",
                },
                "runtime": {"port": 8080, "healthPath": "/"},
                "storageBindings": [],
            },
            "builderImageId": IMAGE,
            "runtimeImages": {
                "node": "registry.example/node@sha256:" + "a" * 64,
                "bun": "registry.example/bun@sha256:" + "b" * 64,
            },
            "sourceLimit": 1024,
            "buildLogLimit": 1024,
            "connectSeconds": 5,
            "deadlineAt": "2030-01-01T00:00:00Z",
        }
        with (
            mock.patch.object(production, "helper_runtime", return_value=self.runtime),
            mock.patch.object(app, "acquire_github_commit"),
            mock.patch.object(
                production,
                "validate_checkout",
                side_effect=ValidationError("private source details"),
            ),
            mock.patch.object(app, "build_with_disposable_builder") as builder,
        ):
            with self.assertRaises(HelperActionError) as caught:
                production._build_application(arguments)
        self.assertEqual(caught.exception.code, "BUILD_REJECTED")
        self.assertNotIn("private source details", str(caught.exception))
        builder.assert_not_called()

    def test_cleanup_confirms_both_builder_and_build_artifact(self):
        absent = app.BuilderObservation(
            BUILD, None, "builder", None, "port", None, None, None, False
        )
        for observation in (self.observation, absent):
            with (
                mock.patch.object(production, "helper_runtime", return_value=self.runtime),
                mock.patch.object(app, "delete_builder", return_value=observation),
                mock.patch.object(app, "confirm_build_manifest_absent") as registry,
            ):
                if observation.absent:
                    response = production._provider_app(
                        "app.build.cleanup", {"buildId": BUILD, "slug": "demo-app"}
                    )
                    self.assertEqual(
                        response,
                        {
                            "buildId": BUILD,
                            "slug": "demo-app",
                            "builderAbsent": True,
                            "artifactAbsent": True,
                        },
                    )
                    registry.assert_called_once()
                else:
                    with self.assertRaises(HelperActionError):
                        production._provider_app(
                            "app.build.cleanup", {"buildId": BUILD, "slug": "demo-app"}
                        )
                    registry.assert_not_called()
        with (
            mock.patch.object(production, "helper_runtime", return_value=self.runtime),
            mock.patch.object(app, "delete_builder", return_value=absent),
            mock.patch.object(
                app,
                "confirm_build_manifest_absent",
                side_effect=app.ApplicationError("artifact remains"),
            ),
        ):
            with self.assertRaises(app.ApplicationError):
                production._provider_app(
                    "app.build.cleanup", {"buildId": BUILD, "slug": "demo-app"}
                )

    def test_registry_absence_requires_fixed_origin_404_not_presence_redirect_or_outage(self):
        with mock.patch.dict(
            os.environ, {"PLATFORM_CONFIG": str(ROOT / "config/platform.example.json")}
        ):
            registry = importlib.import_module("infra.registry.delete_manifest")
        for code in (404, 200, 302, 401, 500):
            opener = mock.Mock()
            if code == 200:
                opener.open.return_value = mock.MagicMock()
            else:
                opener.open.side_effect = urllib.error.HTTPError(
                    "https://registry.invalid", code, "fixed", {}, None
                )
            output = io.StringIO()
            with (
                mock.patch.object(
                    registry,
                    "env",
                    return_value={"REGISTRY_BUILDER_PASSWORD": "private-registry-password"},
                ),
                mock.patch.object(registry, "internal_ca_context"),
                mock.patch.object(registry.urllib.request, "build_opener", return_value=opener),
                redirect_stdout(output),
            ):
                if code == 404:
                    registry.confirm_build_absent("projects/demo-app/app", BUILD)
                    self.assertEqual(
                        json.loads(output.getvalue()),
                        {"repository": "projects/demo-app/app", "buildId": BUILD, "absent": True},
                    )
                else:
                    with self.assertRaises(RuntimeError):
                        registry.confirm_build_absent("projects/demo-app/app", BUILD)
                    self.assertEqual(output.getvalue(), "")
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "HEAD")
            self.assertTrue(
                request.full_url.endswith(
                    "/v2/projects/demo-app/app/manifests/build-" + BUILD.replace("-", "")
                )
            )
            self.assertNotIn("private-registry-password", output.getvalue())
        self.assertIsNone(
            registry._NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.invalid"
            )
        )

    def test_registry_wrapper_rejects_wrong_identity_and_inexact_absence(self):
        for evidence in (
            {"repository": "projects/demo-app/app", "buildId": IMAGE, "absent": True},
            {"repository": "projects/demo-app/app", "buildId": BUILD, "absent": 1},
        ):
            runner = mock.Mock(
                return_value=SimpleNamespace(
                    stdout=json.dumps(evidence).encode(),
                    stdout_truncated=False,
                    stderr_truncated=False,
                )
            )
            with self.assertRaises(app.ApplicationError):
                app.confirm_build_manifest_absent(
                    "demo-app",
                    BUILD,
                    registry_command=("fixed-registry",),
                    timeout_seconds=10,
                    command_runner=runner,
                )
            self.assertEqual(
                runner.call_args.args[0],
                ("fixed-registry", "build-absent", "projects/demo-app/app", BUILD),
            )
