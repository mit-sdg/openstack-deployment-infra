"""Team apps: owners add members who work on the app; quota stays the owner's."""

from __future__ import annotations

import time
import uuid
from typing import Any
from unittest.mock import patch

from openstack_platform.controller.http import HttpError
from openstack_platform.management.broker import bootstrap
from openstack_platform.management.broker.members import MAXIMUM_MEMBERS
from openstack_platform.management.common import strict_json
from tests import test_management_accounts as account_fixtures
from tests.test_management import ManagementCase


class TeamTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.alice = self.login("alice")
        self.bob = self.login("bob")
        self.taylor = self.login("taylor")
        self.app = self.create()
        self.save(self.app)
        self.fixture.delay = 0

    def members(self, owner: str = "alice") -> Any:
        return self.call("GET", f"/v1/apps/{self.app}/members", owner=owner).body["data"]

    def add(self, username: str, owner: str = "alice") -> Any:
        return self.call(
            "POST", f"/v1/apps/{self.app}/members", {"username": username}, owner
        ).body["data"]

    def remove(self, user: str, owner: str = "alice") -> Any:
        return self.call("DELETE", f"/v1/apps/{self.app}/members/{user}", None, owner).body["data"]

    def test_members_work_on_the_app_and_quota_stays_the_owners(self) -> None:
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/apps/{self.app}", owner="bob")
        )
        people = self.add("bob")["items"]
        self.assertEqual(
            [(person["username"], person["role"]) for person in people],
            [("alice", "owner"), ("bob", "member")],
        )
        self.assertEqual(self.add("bob")["items"], people)
        listed = self.call("GET", "/v1/apps", owner="bob").body["data"]
        self.assertEqual(
            [
                (item["applicationId"], item["access"], item["ownerDisplayName"])
                for item in listed["items"]
            ],
            [(self.app, "member", "Alice Student")],
        )
        self.assertEqual(listed["quota"]["apps"]["used"] + listed["quota"]["apps"]["reserved"], 0)
        detail = self.call("GET", f"/v1/apps/{self.app}", owner="bob").body["data"]
        self.assertEqual(
            (detail["access"], detail["ownerDisplayName"]), ("member", "Alice Student")
        )
        self.assertEqual(
            self.call("GET", f"/v1/apps/{self.app}", owner="alice").body["data"]["access"], "owner"
        )
        self.save(self.app, owner="bob", revision=1)
        intent = self.call(
            "POST",
            f"/v1/apps/{self.app}/deployments",
            {"commit": "1" * 40, "configurationRevision": 2},
            "bob",
        ).body["data"]
        self.broker.journal.dispatch(intent["intentId"])
        seen = self.call("GET", f"/v1/intents/{intent['intentId']}", owner="alice").body["data"]
        self.assertEqual((seen["state"], seen["appSlug"]), ("succeeded", "student-app"))
        activity = self.call("GET", f"/v1/apps/{self.app}/activity", owner="alice").body["data"]
        self.assertEqual(
            [
                (item["kind"], item["actor"]["displayName"], item["actor"]["you"])
                for item in activity["items"][:2]
            ],
            [("deploy", "Bob Student", False), ("save_configuration", "Bob Student", False)],
        )
        self.assertEqual(self.call("GET", f"/v1/apps/{self.app}/logs", owner="bob").status, 200)
        key = f"/v1/apps/{self.app}/source-key"
        self.assertTrue(self.call("POST", key, {}, "bob").body["data"]["present"])
        self.assertEqual(self.call("DELETE", key, None, "bob").body["data"], {"present": False})
        self.assert_error(
            "INVALID_REQUEST",
            lambda: self.call("GET", f"/v1/apps/{self.app}/activity?limit=500", owner="alice"),
        )

    def test_only_the_owner_manages_people_and_members_can_leave(self) -> None:
        self.add("bob")
        self.assert_error("OWNER_ONLY", lambda: self.add("taylor", owner="bob"))
        self.assert_error("OWNER_ONLY", lambda: self.remove(self.alice, owner="bob"))
        with self.assertRaises(HttpError) as error:
            self.add("nobody")
        self.assertEqual(error.exception.code, "ACCOUNT_NOT_REGISTERED")
        self.assertEqual(
            error.exception.summary,
            "nobody isn't registered yet. Ask them to sign in to the portal once, then add them.",
        )
        self.assert_error("ALREADY_OWNER", lambda: self.add("alice"))
        for user in (self.taylor, str(uuid.uuid4()), "not-a-uuid"):
            with self.subTest(user=user):
                self.assert_error("NOT_FOUND", lambda user=user: self.remove(user))
        for method, path in (
            ("GET", ""),
            ("GET", "/members"),
            ("GET", "/activity"),
            ("GET", "/logs"),
            ("GET", "/source-key"),
            ("DELETE", "/source-key"),
        ):
            with self.subTest(method=method, path=path):
                self.assert_error(
                    "NOT_FOUND",
                    lambda method=method, path=path: self.call(
                        method, f"/v1/apps/{self.app}{path}", owner="taylor"
                    ),
                )
        self.assertEqual(self.remove(self.bob, owner="bob"), {"items": [], "left": True})
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/apps/{self.app}", owner="bob")
        )
        self.add("taylor")
        self.remove(self.taylor)
        self.assertEqual([person["role"] for person in self.members()["items"]], ["owner"])
        with self.broker.database.connect() as db:
            actions = [
                row[0]
                for row in db.execute(
                    "SELECT action FROM audit WHERE app_id=? AND intent_id IS NULL ORDER BY sequence",
                    (self.app,),
                )
            ]
        self.assertEqual(actions, ["member_add", "member_remove", "member_add", "member_remove"])

    def test_a_team_has_a_limit(self) -> None:
        with self.broker.database.connect(write=True) as db:
            for index in range(MAXIMUM_MEMBERS):
                user = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO users(id,issuer,subject,username,display_name,created,last_login) VALUES(?,?,?,?,?,?,?)",
                    (user, "fixture", user, f"member{index}", "Member", time.time(), time.time()),
                )
                db.execute(
                    "INSERT INTO app_members(app_id,user_id,created) VALUES(?,?,?)",
                    (self.app, user, time.time()),
                )
        self.assert_error("TEAM_FULL", lambda: self.add("bob"))

    def test_removed_member_loses_authority_before_the_write(self) -> None:
        self.add("bob")
        original = self.broker.own

        def removed(request: Any, **kwargs: Any) -> Any:
            result = original(request, **kwargs)
            with self.broker.database.connect(write=True) as db:
                db.execute("DELETE FROM app_members WHERE user_id=?", (self.bob,))
            return result

        with patch.object(self.broker, "own", removed):
            self.assert_error("NOT_FOUND", lambda: self.save(self.app, owner="bob", revision=1))


class AdminTeamTests(ManagementCase):
    anonymous = account_fixtures.AccountsTests.anonymous
    begin = account_fixtures.AccountsTests.begin
    finish = account_fixtures.AccountsTests.finish
    admin = account_fixtures.AccountsTests.admin

    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()
        self.broker.auth.clock = lambda: self.now
        folder = bootstrap.enrollment_file(self.config).parent
        folder.mkdir(parents=True, mode=0o2750)
        folder.chmod(0o2750)
        self.admin_user = self.admin()["user"]["id"]
        self.owner = self.login("alice")
        self.bob = self.login("bob")
        self.app = self.create()

    def test_admin_manages_any_team_and_reassigning_to_a_member_drops_their_membership(
        self,
    ) -> None:
        prefix = f"/v1/apps/{self.app}"
        added = self.call("POST", prefix + "/members", {"username": "bob"}, "admin").body["data"]
        self.assertEqual([person["role"] for person in added["items"]], ["owner", "member"])
        with self.broker.database.connect() as db:
            row = db.execute(
                "SELECT details FROM admin_audit WHERE action='app_member_add'"
            ).fetchone()
        self.assertEqual(strict_json(row[0].encode())["memberId"], self.bob)
        body = {"ownerId": self.bob, "expectedOwnerId": self.owner}
        self.call("PUT", prefix + "/owner", body, "admin")
        people = self.call("GET", prefix + "/members", owner="admin").body["data"]["items"]
        self.assertEqual(
            [(person["userId"], person["role"]) for person in people], [(self.bob, "owner")]
        )
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/apps/{self.app}", owner="alice")
        )
