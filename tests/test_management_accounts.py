"""Local credentials, one-time enrollment, role boundaries and admin account actions."""

from __future__ import annotations

import base64
import dataclasses
import io
import os
import sqlite3
import time
from contextlib import closing, redirect_stderr
from unittest.mock import patch
from urllib.parse import urlsplit

from openstack_platform.management.backup import restore_database
from openstack_platform.management.broker import bootstrap, local_security
from openstack_platform.management.broker.accounts import security_change
from openstack_platform.management.broker.database import MIGRATION_2, SCHEMA_V1, Database
from openstack_platform.management.common import canonical, digest
from tests.test_management import ManagementCase


class SecurityTests(ManagementCase):
    def test_scrypt_unique_salts_constant_verification_and_rehash(self) -> None:
        value = "a private password 89347"
        first, second = local_security.hash_password(value), local_security.hash_password(value)
        self.assertNotEqual(first.split("$")[4], second.split("$")[4])
        self.assertEqual(len(bytes.fromhex(first.split("$")[4])), 16)
        self.assertEqual(local_security.verify_password(value, first), (True, False))
        self.assertEqual(local_security.verify_password("wrong", first), (False, False))
        old = local_security.hash_password(value, n=16384, p=1)
        self.assertEqual(local_security.verify_password(value, old), (True, True))
        self.assertEqual(
            local_security.verify_password(value, first.replace("$32768$", "$1073741824$")),
            (False, False),
        )

    def test_password_policy_byte_bounds_and_username(self) -> None:
        for value in ("short", "ALICE and many extra characters", "x" * 1025, "😀" * 300):
            with self.assertRaises(ValueError):
                local_security.password(value, "alice")
        self.assertEqual(local_security.password("😀" * 12, "alice"), "😀" * 12)

    def test_rfc6238_sha1_window_and_replay(self) -> None:
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(local_security.totp_code(secret, 1), "287082")
        for timestamp, expected in (
            (1111111109, "081804"),
            (1111111111, "050471"),
            (1234567890, "005924"),
            (2000000000, "279037"),
            (20000000000, "353130"),
        ):
            self.assertEqual(local_security.totp_code(secret, timestamp // 30), expected)
        now = 1000.0
        counter = int(now // 30)
        for offset in (-1, 0, 1):
            code = local_security.totp_code(secret, counter + offset)
            accepted = local_security.verify_totp(secret, code, now, -1)
            self.assertEqual(accepted, counter + offset)
            self.assertIsNone(local_security.verify_totp(secret, code, now, accepted))
        self.assertIsNone(
            local_security.verify_totp(
                secret, local_security.totp_code(secret, counter + 2), now, -1
            )
        )
        self.assertIsNone(local_security.verify_totp(secret, "1234567", now, -1))


class AccountsTests(ManagementCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()
        self.broker.auth.clock = lambda: self.now
        self.folder = bootstrap.enrollment_file(self.config).parent
        self.folder.mkdir(parents=True, mode=0o2750)
        self.folder.chmod(0o2750)

    def anonymous(self) -> tuple[str, dict[str, str]]:
        options = self.call("GET", "/v1/auth/options").body
        return options["data"]["csrfToken"], {
            "cookie": self.config.login_cookie + "=" + options["browser"]["cookies"][0]["value"]
        }

    def begin(
        self,
        token: str,
        *,
        username: str | None = None,
        password: str = "private secure phrase 48219",
        totp: str = "",
        selected: bool = False,
    ) -> tuple[dict, str, dict[str, str]]:
        csrf, headers = self.anonymous()
        body = {
            "csrfToken": csrf,
            "token": token,
            "password": password,
            "totp": totp,
            "totpEnabled": selected,
            "role": "admin",
        }
        if username:
            body["username"] = username
        started = self.call("POST", "/v1/auth/enroll", body, headers=headers).body["data"]
        return started, csrf, headers

    def finish(
        self, started: dict, csrf: str, headers: dict[str, str], *, alias: str = "admin"
    ) -> dict:
        code = (
            local_security.totp_code(started["totpSecret"], int(self.now // 30))
            if started["totpSecret"]
            else ""
        )
        result = self.call(
            "POST",
            "/v1/auth/enroll/finish",
            {"csrfToken": csrf, "enrollmentToken": started["enrollmentToken"], "totp": code},
            headers=headers,
        ).body
        if "browser" in result:
            self.tokens[alias] = result["browser"]["cookies"][1]["value"]
            session = self.call("GET", "/v1/session", owner=alias).body["data"]
            self.csrf[alias] = session["csrfToken"]
            return session
        return result["data"]

    def admin(self) -> dict:
        url = bootstrap.issue(self.config, now=self.now)
        self.bootstrap_token = urlsplit(url).fragment
        started, csrf, headers = self.begin(self.bootstrap_token, username="rootadmin")
        self.admin_secret = started["totpSecret"]
        return self.finish(started, csrf, headers)

    def invite(self, role: str = "staff", name: str = "localstaff") -> dict:
        return self.call(
            "POST",
            "/v1/accounts",
            {"username": name, "displayName": "Local Account", "role": role},
            "admin",
        ).body["data"]

    def local_login(
        self,
        username: str,
        *,
        password: str = "private secure phrase 48219",
        code: str = "",
        claimed_role: str = "admin",
    ) -> dict:
        csrf, headers = self.anonymous()
        return self.call(
            "POST",
            "/v1/auth/login",
            {
                "csrfToken": csrf,
                "username": username,
                "password": password,
                "method": "local",
                "totp": code,
                "role": claimed_role,
            },
            headers=headers,
        ).body

    def test_bootstrap_hash_only_atomic_mode_fragment_single_use_and_required_totp(self) -> None:
        session = self.admin()
        self.assertEqual(session["role"], "admin")
        content = bootstrap.enrollment_file(self.config).read_text()
        self.assertNotIn(self.bootstrap_token.split(".")[1], content)
        self.assertEqual(bootstrap.enrollment_file(self.config).stat().st_mode & 0o777, 0o640)
        self.assert_error(
            "TOKEN_INVALID", lambda: self.begin(self.bootstrap_token, username="secondadmin")
        )
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM used_tokens").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT role,issuer,status FROM users").fetchone()[:],
                ("admin", "local", "active"),
            )
            self.assertEqual(db.execute("SELECT generation FROM users").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT totp_confirmed FROM local_accounts").fetchone()[0], 1
            )
        self.assertNotIn("private secure phrase", content)

    def test_bootstrap_expiry_malformed_unsafe_file_origin_csrf_and_no_token_logs(self) -> None:
        token = urlsplit(bootstrap.issue(self.config, now=self.now)).fragment
        csrf, headers = self.anonymous()
        body = {
            "csrfToken": csrf,
            "token": token,
            "password": "private secure phrase 48219",
            "username": "rootadmin",
        }
        with redirect_stderr(io.StringIO()) as output:
            self.assert_error(
                "ORIGIN_REJECTED",
                lambda: self.call(
                    "POST", "/v1/auth/enroll", body, headers={**headers, "origin": "null"}
                ),
            )
            self.assert_error(
                "CSRF_REJECTED", lambda: self.call("POST", "/v1/auth/enroll", body, headers={})
            )
            self.assert_error(
                "TOKEN_INVALID", lambda: self.begin("malformed", username="rootadmin")
            )
            self.now += 86400
            self.assert_error("TOKEN_INVALID", lambda: self.begin(token, username="rootadmin"))
        self.assertNotIn(token, output.getvalue())

    def test_every_admin_route_denies_owner_and_staff_and_commons_admin_is_impossible(self) -> None:
        alice = self.login()
        for role in ("owner", "staff"):
            with self.broker.database.connect(write=True) as db:
                db.execute("UPDATE users SET role=? WHERE id=?", (role, alice))
                security_change(db, alice)
            self.login()
            for method, path, body in (
                ("GET", "/v1/accounts", None),
                ("POST", "/v1/accounts", {}),
                ("PATCH", f"/v1/accounts/{alice}", {}),
                ("PUT", f"/v1/accounts/{alice}/quotas", {}),
                ("GET", "/v1/account-audit", None),
                ("POST", "/v1/reauthenticate", {}),
            ):
                self.assert_error(
                    "ACCESS_DENIED", lambda m=method, p=path, b=body: self.call(m, p, b, "alice")
                )
        self.admin()
        self.assert_error(
            "INVALID_ROLE",
            lambda: self.call(
                "PATCH", f"/v1/accounts/{alice}", {"action": "role", "value": "admin"}, "admin"
            ),
        )
        with self.broker.database.connect(write=True) as db:
            import sqlite3

            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE users SET role='admin' WHERE id=?", (alice,))

    def test_invite_expiry_replay_claimed_role_ignored_and_local_staff_own_scope(self) -> None:
        self.admin()
        invitation = self.invite()
        token = urlsplit(invitation["setupUrl"]).fragment
        started, csrf, headers = self.begin(token)
        session = self.finish(started, csrf, headers, alias="localstaff")
        self.assertEqual(session["role"], "staff")
        self.assertEqual(self.call("GET", "/v1/staff/owners", owner="localstaff").status, 200)
        self.assert_error(
            "ACCESS_DENIED", lambda: self.call("GET", "/v1/accounts", owner="localstaff")
        )
        own = self.create("localstaff", "local-project")
        self.login()
        foreign = self.create(slug="other-project")
        self.assert_error(
            "NOT_FOUND", lambda: self.call("GET", f"/v1/apps/{foreign}", owner="localstaff")
        )
        self.assertEqual(self.call("GET", f"/v1/apps/{own}", owner="localstaff").status, 200)
        self.assert_error("TOKEN_INVALID", lambda: self.begin(token))
        next_invitation = self.invite("owner", "laterowner")
        self.now += 72 * 3600
        self.assert_error(
            "TOKEN_INVALID", lambda: self.begin(urlsplit(next_invitation["setupUrl"]).fragment)
        )

    def test_step_up_role_disable_reset_revoke_all_generations_and_quota_audit(self) -> None:
        self.admin()
        alice = self.login()
        self.now += 301
        self.assert_error(
            "STEP_UP_REQUIRED",
            lambda: self.call(
                "PATCH", f"/v1/accounts/{alice}", {"action": "role", "value": "staff"}, "admin"
            ),
        )
        fresh = local_security.totp_code(self.admin_secret, int(self.now // 30))
        self.call(
            "POST",
            "/v1/reauthenticate",
            {"password": "private secure phrase 48219", "totp": fresh},
            "admin",
        )
        old = self.tokens["alice"]
        self.call("PATCH", f"/v1/accounts/{alice}", {"action": "role", "value": "staff"}, "admin")
        self.assert_error(
            "SESSION_EXPIRED",
            lambda: self.call(
                "GET", "/v1/session", headers={"cookie": self.config.session_cookie + "=" + old}
            ),
        )
        self.login()
        self.assertEqual(
            self.call("GET", "/v1/session", owner="alice").body["data"]["role"], "staff"
        )
        self.call(
            "PUT", f"/v1/accounts/{alice}/quotas", {"apps": 7, "concurrentOperations": 3}, "admin"
        )
        self.assertEqual(
            self.call("GET", f"/v1/staff/owners/{alice}", owner="alice").body["data"]["quota"][
                "apps"
            ]["limit"],
            7,
        )
        self.call("PATCH", f"/v1/accounts/{alice}", {"action": "enabled", "value": False}, "admin")
        self.assert_error("SESSION_EXPIRED", lambda: self.call("GET", "/v1/session", owner="alice"))
        self.call("PATCH", f"/v1/accounts/{alice}", {"action": "enabled", "value": True}, "admin")
        self.login()
        self.call(
            "PATCH", f"/v1/accounts/{alice}", {"action": "revoke-sessions", "value": None}, "admin"
        )
        self.assert_error("SESSION_EXPIRED", lambda: self.call("GET", "/v1/session", owner="alice"))
        actions = self.call("GET", "/v1/account-audit", owner="admin").body["data"]["items"]
        self.assertTrue(
            {"role", "enabled", "quotas", "revoke-sessions"} <= {row["action"] for row in actions}
        )
        self.assertNotIn(self.admin_secret, canonical(actions))

    def test_admin_local_login_mfa_replay_backoff_and_rehash(self) -> None:
        self.admin()
        oldcode = local_security.totp_code(self.admin_secret, int(self.now // 30))
        self.assert_error(
            "INVALID_CREDENTIALS", lambda: self.local_login("rootadmin", code=oldcode)
        )
        self.now += 31
        self.assert_error(
            "INVALID_CREDENTIALS",
            lambda: self.local_login(
                "rootadmin",
                password="wrong",
                code=local_security.totp_code(self.admin_secret, int(self.now // 30)),
            ),
        )
        self.assert_error(
            "INVALID_CREDENTIALS",
            lambda: self.local_login(
                "rootadmin", code=local_security.totp_code(self.admin_secret, int(self.now // 30))
            ),
        )
        self.now += 10
        weak = local_security.hash_password("private secure phrase 48219", n=16384, p=1)
        with self.broker.database.connect(write=True) as db:
            db.execute("UPDATE local_accounts SET password_hash=?", (weak,))
        result = self.local_login(
            "rootadmin", code=local_security.totp_code(self.admin_secret, int(self.now // 30))
        )
        self.assertEqual(result["data"]["returnPath"], "/admin/accounts")
        with self.broker.database.connect() as db:
            self.assertNotEqual(
                db.execute("SELECT password_hash FROM local_accounts").fetchone()[0], weak
            )

    def test_bootstrap_operator_identity_directory_mode_and_overwrite(self) -> None:
        first = urlsplit(bootstrap.issue(self.config, now=self.now)).fragment
        second = urlsplit(bootstrap.issue(self.config, now=self.now)).fragment
        self.assert_error("TOKEN_INVALID", lambda: self.begin(first, username="rootadmin"))
        with patch(
            "openstack_platform.management.broker.bootstrap.management_peer",
            return_value=(os.geteuid() + 1, os.getegid()),
        ):
            with self.assertRaises(ValueError):
                bootstrap.issue(dataclasses.replace(self.config, development=False))
        self.assertNotEqual(first, second)

    def test_password_and_totp_resets_are_single_use_revoke_sessions_and_preserve_roles(
        self,
    ) -> None:
        self.admin()
        invitation = self.invite("owner", "resetowner")
        started, csrf, headers = self.begin(urlsplit(invitation["setupUrl"]).fragment)
        self.finish(started, csrf, headers, alias="resetowner")
        before = self.tokens["resetowner"]
        reset = self.call(
            "PATCH",
            f"/v1/accounts/{invitation['userId']}",
            {"action": "password-reset", "value": None},
            "admin",
        ).body["data"]
        self.assert_error(
            "SESSION_EXPIRED",
            lambda: self.call(
                "GET", "/v1/session", headers={"cookie": self.config.session_cookie + "=" + before}
            ),
        )
        token = urlsplit(reset["setupUrl"]).fragment
        stage, csrf, headers = self.begin(token, password="a replacement phrase 49218")
        session = self.finish(stage, csrf, headers, alias="resetowner")
        self.assertEqual(session["role"], "owner")
        self.assert_error("TOKEN_INVALID", lambda: self.begin(token))
        reset = self.call(
            "PATCH",
            f"/v1/accounts/{invitation['userId']}",
            {"action": "totp-reset", "value": None},
            "admin",
        ).body["data"]
        stage, csrf, headers = self.begin(urlsplit(reset["setupUrl"]).fragment)
        secret = stage["totpSecret"]
        self.assertIsNotNone(secret)
        self.assertEqual(self.finish(stage, csrf, headers)["returnPath"], "/sign-in")
        self.now += 31
        result = self.local_login(
            "resetowner",
            password="a replacement phrase 49218",
            code=local_security.totp_code(secret, int(self.now // 30)),
        )
        self.assertEqual(result["data"]["returnPath"], "/apps")

    def test_restore_preserves_admin_and_staff_roles_but_invalidates_all_token_authority(
        self,
    ) -> None:
        import sqlite3
        from contextlib import closing

        from openstack_platform.management.backup import restore_database

        self.admin()
        invited = self.invite()
        snapshot = self.root / "accounts-snapshot.sqlite3"
        with self.broker.database.connect() as db, closing(sqlite3.connect(snapshot)) as copied:
            db.backup(copied)
        snapshot.chmod(0o600)
        destination = self.root / "accounts-restored/management.sqlite3"
        restore_database(snapshot, destination)
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(
                {row[0] for row in db.execute("SELECT role FROM users")}, {"admin", "staff"}
            )
            for table in ("sessions", "csrf_tokens", "account_tokens", "enrollments"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM used_tokens").fetchone()[0], 1)
            self.assertEqual(
                db.execute(
                    "SELECT totp_confirmed FROM local_accounts WHERE totp_secret IS NOT NULL"
                ).fetchone()[0],
                1,
            )
            fence = db.execute("SELECT valid_after FROM token_policy").fetchone()[0]
        with self.assertRaises(ValueError):
            bootstrap.read(self.config, self.bootstrap_token, time.time(), fence)
        self.assertNotIn(
            invited["setupUrl"], snapshot.read_bytes().decode("utf-8", errors="ignore")
        )

    def test_pending_account_admin_promotion_and_stepup_expiry_after_slow_enrollment(self) -> None:
        self.admin()
        invited = self.invite("owner", "futureadmin")
        promoted = self.call(
            "PATCH",
            f"/v1/accounts/{invited['userId']}",
            {"action": "role", "value": "admin"},
            "admin",
        ).body["data"]
        self.assertIn("setupUrl", promoted)
        stage, csrf, headers = self.begin(urlsplit(promoted["setupUrl"]).fragment)
        self.assertIsNotNone(stage["totpSecret"])
        self.now += 301
        session = self.finish(stage, csrf, headers, alias="futureadmin")
        self.assertEqual(session["role"], "admin")
        self.assert_error(
            "STEP_UP_REQUIRED",
            lambda: self.call(
                "POST",
                "/v1/accounts",
                {"username": "anotheradmin", "displayName": "Another", "role": "admin"},
                "futureadmin",
            ),
        )

    def test_legacy_restore_upgrade_fences_still_valid_consumed_bootstrap_file(self) -> None:
        identity = digest(self.config.portal_origin + "\n" + self.config.issuer)
        for version in (1, 2):
            with self.subTest(version=version):
                snapshot = self.root / f"legacy-{version}.sqlite3"
                with closing(sqlite3.connect(snapshot)) as db:
                    db.executescript(SCHEMA_V1 + (MIGRATION_2 if version == 2 else ""))
                    db.execute("INSERT INTO metadata VALUES(?,?)", (version, identity))
                    db.execute(
                        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT)"
                    )
                    db.execute("INSERT INTO schema_migrations VALUES(1,?)", (digest(SCHEMA_V1),))
                    if version == 2:
                        db.execute(
                            "INSERT INTO schema_migrations VALUES(2,?)", (digest(MIGRATION_2),)
                        )
                    db.commit()
                snapshot.chmod(0o600)
                if version == 1:
                    self.admin()
                consumed = self.bootstrap_token
                restore_database(snapshot, self.broker.database.path)
                self.now += 2
                with patch(
                    "openstack_platform.management.broker.database.time.time", return_value=self.now
                ):
                    self.broker.database = Database(self.config)
                self.broker.auth.database = self.broker.database
                with self.broker.database.connect() as db:
                    self.assertEqual(
                        db.execute("SELECT COUNT(*) FROM used_tokens").fetchone()[0], 0
                    )
                    self.assertEqual(
                        db.execute("SELECT valid_after FROM token_policy").fetchone()[0], self.now
                    )
                self.now += 1
                self.assert_error(
                    "TOKEN_INVALID",
                    lambda consumed=consumed: self.begin(consumed, username="secondadmin"),
                )
                self.admin()  # A newly issued operator file still enrolls successfully.
                Database(self.config)  # Restart must not move the fence again.
                with self.broker.database.connect() as db:
                    self.assertEqual(
                        db.execute("SELECT valid_after FROM token_policy").fetchone()[0],
                        self.now - 1,
                    )

    def test_enrollment_code_attempts_are_bounded(self) -> None:
        token = urlsplit(bootstrap.issue(self.config, now=self.now)).fragment
        stage, csrf, headers = self.begin(token, username="attemptadmin")
        correct = local_security.totp_code(stage["totpSecret"], int(self.now // 30))
        wrong = "000000" if correct != "000000" else "111111"
        for _ in range(5):
            result = self.call(
                "POST",
                "/v1/auth/enroll/finish",
                {"csrfToken": csrf, "enrollmentToken": stage["enrollmentToken"], "totp": wrong},
                headers=headers,
            )
            self.assertEqual(result.status, 401)
        self.assert_error(
            "TOKEN_INVALID",
            lambda: self.call(
                "POST",
                "/v1/auth/enroll/finish",
                {"csrfToken": csrf, "enrollmentToken": stage["enrollmentToken"], "totp": correct},
                headers=headers,
            ),
        )
