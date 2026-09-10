from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from openstack_platform.controller import application_runtime as app
from openstack_platform.helper import production
from openstack_platform.helper.errors import HelperActionError
from openstack_platform.validation import ValidationError
from tests import test_openstack_lifecycle_scripts as fixtures


class WorkerDeleteGuardsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LifecycleScriptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.expected = dict(
            EXPECTED_SERVER_ID=fixtures.SERVER_ID, EXPECTED_PORT_ID=fixtures.PORT_ID
        )

    def delete(self, **env):
        return self.fixture.invoke("worker", "delete", **(env or self.expected))

    def test_fresh_shell_observation_refuses_replacement_and_wrong_expected_pair(self):
        for retained in (False, True):
            for mismatch in ("server", "port"):
                with self.subTest(retained=retained, mismatch=mismatch):
                    self.fixture.seed("worker", retained=retained)
                    env = {**self.expected, f"EXPECTED_{mismatch.upper()}_ID": fixtures.OTHER_ID}
                    result = self.delete(**env)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("expected deletion target", result.stderr)
                    self.assertFalse(self.fixture.mutations())

    def test_name_absence_is_not_uuid_absence_and_inventory_failures_fail_closed(self):
        for renamed in (True, False):
            with self.subTest(renamed=renamed):
                state = self.fixture.seed("worker", existing=False, port_exists=False)
                if renamed:
                    state = self.fixture.seed("worker")
                    state["server"]["name"] = "renamed-worker"
                    state["port"]["name"] = "renamed-port"
                else:
                    # Name lookups succeed, but the UUID absence inventory fails.
                    state["after"] = [
                        dict(command=["port", "list"], patch={"fail": ["server", "list"]})
                    ]
                self.fixture.save_state(state)
                result = self.delete()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.fixture.mutations())
                if renamed:
                    self.assertIn("still exists", result.stderr)

    def test_guarded_cleanup_preserves_retained_primary_and_retries_lost_server_delete(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                state = self.fixture.seed("worker", retained=retained)
                state["after"] = [dict(command=["server", "delete"], exit=1)]
                self.fixture.save_state(state)
                first = self.delete()
                self.assertNotEqual(first.returncode, 0)
                second = self.delete()
                self.assertEqual(second.returncode, 0, second.stderr)
                third = self.delete()
                self.assertEqual(third.returncode, 0, third.stderr)
                self.assertEqual(
                    self.fixture.calls("server", "delete"),
                    [["server", "delete", "--wait", fixtures.SERVER_ID]],
                )
                self.assertEqual(len(self.fixture.calls("port", "delete")), int(not retained))
                final = self.fixture.read_state()
                self.assertIsNone(final["server"])
                if retained:
                    self.assertEqual(final["port"]["id"], fixtures.PORT_ID)
                    self.assertEqual(final["port"]["device_id"], "")
                else:
                    self.assertIsNone(final["port"])

    def test_guarded_port_delete_rechecks_drift_after_server_mutation(self):
        for patch in (
            {"id": fixtures.OTHER_ID},
            {"device_id": fixtures.OTHER_ID},
            {"description": "foreign"},
        ):
            with self.subTest(patch=patch):
                state = self.fixture.seed("worker")
                state["after"] = [dict(command=["server", "delete"], patch={"port": patch})]
                self.fixture.save_state(state)
                result = self.delete()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.fixture.calls("port", "delete"))

    def test_guarded_orphan_cleanup_deletes_only_the_expected_owned_port(self):
        self.fixture.seed("worker", existing=False)
        result = self.delete()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.mutations(), [["port", "delete", fixtures.PORT_ID]])

    def test_shell_expected_pair_is_strict_and_cannot_be_partial(self):
        for env in (
            {"EXPECTED_SERVER_ID": fixtures.SERVER_ID},
            {"EXPECTED_PORT_ID": fixtures.PORT_ID},
            {**self.expected, "EXPECTED_SERVER_ID": ""},
            {**self.expected, "EXPECTED_SERVER_ID": fixtures.SERVER_ID.replace("-", "")},
        ):
            with self.subTest(env=env):
                self.fixture.seed("worker")
                result = self.delete(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.fixture.mutations())

    def test_production_helper_allows_only_paired_single_worker_guards(self):
        platform = SimpleNamespace(
            prefix="example", project_name="project", project_id=fixtures.PROJECT_ID
        )
        absent = app.WorkerObservation(
            fixtures.APP_ID, "demo-app", None, "absent", None, "absent-v4", None, None, False
        )
        guarded = {
            "applicationId": fixtures.APP_ID,
            "slug": "demo-app",
            "single": True,
            "expectedServerId": fixtures.SERVER_ID,
            "expectedPortId": fixtures.PORT_ID,
        }
        with (
            mock.patch.object(
                production, "helper_runtime", return_value=SimpleNamespace(platform=platform)
            ),
            mock.patch.object(app, "provider_command", return_value=("fixed-worker",)),
            mock.patch.object(app, "delete_worker", return_value=absent) as delete,
        ):
            self.assertTrue(production._provider_app("app.worker.delete", guarded)["absent"])
            delete.assert_called_once()
            self.assertEqual(delete.call_args.args, (fixtures.APP_ID, "demo-app"))
            self.assertEqual(delete.call_args.kwargs["expected_server_id"], fixtures.SERVER_ID)
            self.assertEqual(delete.call_args.kwargs["expected_port_id"], fixtures.PORT_ID)
            delete.reset_mock()
            for values in (
                {**guarded, "single": False},
                {**guarded, "single": 1},
                {key: value for key, value in guarded.items() if key != "single"},
                {key: value for key, value in guarded.items() if key != "expectedPortId"},
                {key: value for key, value in guarded.items() if key != "expectedServerId"},
                {**guarded, "expectedPortId": "bad"},
            ):
                with (
                    self.subTest(values=values),
                    self.assertRaises((ValidationError, HelperActionError)),
                ):
                    production._provider_app("app.worker.delete", values)
            delete.assert_not_called()

    def test_runtime_propagates_guards_only_to_delete_and_validates_pair(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "applicationId": fixtures.APP_ID,
                        "slug": "demo-app",
                        "server": None,
                        "port": None,
                        "ready": False,
                    }
                ).encode(),
                stdout_truncated=False,
                stderr_truncated=False,
            )

        options = dict(
            prefix="example",
            timeout_seconds=30,
            command_runner=runner,
            worker_command=("fixed-worker",),
        )
        self.assertTrue(
            app.delete_worker(
                fixtures.APP_ID,
                "demo-app",
                expected_server_id=fixtures.SERVER_ID,
                expected_port_id=fixtures.PORT_ID,
                **options,
            ).absent
        )
        self.assertEqual(calls[0][0], ("fixed-worker", "delete", fixtures.APP_ID, "demo-app"))
        self.assertEqual(calls[0][1]["env"], self.expected)
        self.assertIsNone(calls[1][1]["env"])
        for values in (
            {"expected_server_id": fixtures.SERVER_ID},
            {"expected_port_id": fixtures.PORT_ID},
        ):
            calls.clear()
            with self.assertRaises(ValidationError):
                app.delete_worker(fixtures.APP_ID, "demo-app", **values, **options)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
