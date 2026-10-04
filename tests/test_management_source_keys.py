from __future__ import annotations

from typing import Any
from unittest.mock import patch

from openstack_platform.management.broker import source_keys
from tests.test_management import ManagementCase


class SourceKeyTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = 1000.0
        self.broker.source_keys.clock = lambda: self.now
        self.login()
        self.app = self.create()

    def key(self, method: str = "GET", body: Any = None, owner: str = "alice") -> Any:
        return self.call(method, f"/v1/apps/{self.app}/source-key", body, owner).body["data"]

    def check(self, owner: str = "alice") -> Any:
        return self.call("POST", f"/v1/apps/{self.app}/source-key/check", {}, owner).body["data"]

    def test_owner_creates_reads_and_replaces_a_deploy_key(self) -> None:
        self.assertEqual(self.key(), {"present": False})
        created = self.key("POST", {})
        self.assertTrue(created["present"])
        self.assertTrue(created["publicKey"].startswith("ssh-ed25519 "))
        self.assertTrue(created["fingerprint"].startswith("SHA256:"))
        self.assertEqual(self.key("POST", {}), created)
        self.assertEqual(self.key(), created)
        replaced = self.key("POST", {"replace": True})
        self.assertNotEqual(replaced["publicKey"], created["publicKey"])
        with self.broker.database.connect() as db:
            actions = [
                row[0]
                for row in db.execute(
                    "SELECT action FROM audit WHERE app_id=? AND intent_id IS NULL ORDER BY sequence",
                    (self.app,),
                )
            ]
        self.assertEqual(actions, ["source_key", "source_key", "source_key_replace"])
        for body in ({"replace": "yes"}, {"other": True}):
            with self.subTest(body=body):
                self.assert_error(
                    "INVALID_FIELD" if "replace" in body else "INVALID_REQUEST",
                    lambda body=body: self.key("POST", body),
                )
        self.login("bob")
        self.assert_error("NOT_FOUND", lambda: self.key(owner="bob"))
        self.assert_error("NOT_FOUND", lambda: self.key("POST", {}, owner="bob"))

    def test_access_check_uses_saved_settings_and_is_shared_briefly(self) -> None:
        self.assert_error("SETTINGS_REQUIRED", self.check)
        self.save(self.app)
        self.assertEqual(self.check(), {"keyPresent": False})
        self.key("POST", {})
        self.now += source_keys.SHARE_SECONDS
        self.fixture.source_problem = "key-refused"
        refused = self.check()
        self.assertEqual(
            refused,
            {
                "keyPresent": True,
                "reachable": False,
                "head": None,
                "branch": "main",
                "problem": "key-refused",
            },
        )
        self.fixture.source_problem = None
        self.assertEqual(self.check(), refused)
        self.now += source_keys.SHARE_SECONDS
        self.assertEqual(self.check()["head"], "0123456789abcdef0123456789abcdef01234567")
        calls = [path for _m, path, _k in self.fixture.calls if path.endswith("/check")]
        self.assertEqual(len(calls), 3)
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.call(
                "POST", f"/v1/apps/{self.app}/source-key/check", {"branch": "x"}, "alice"
            ),
        )

    def test_controllers_without_deploy_keys_say_so(self) -> None:
        request = self.broker.client.request

        def older(method: str, path: str, *args: Any, **kwargs: Any) -> Any:
            if "/source-key" in path:
                return 404, {"error": {"code": "NOT_FOUND"}}
            return request(method, path, *args, **kwargs)

        with patch.object(self.broker.client, "request", older):
            self.assert_error("SOURCE_KEYS_UNAVAILABLE", self.key)
