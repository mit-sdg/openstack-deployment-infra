"""Distributed-guessing, recognized-device and reserved-capacity regressions."""

from __future__ import annotations

import dataclasses
import time
from unittest.mock import patch

from openstack_platform.management.broker import bootstrap, known_device, local_auth, local_security
from openstack_platform.management.broker.accounts import security_change
from openstack_platform.management.web.server import WebServer
from tests import test_management_accounts as accounts
from tests.test_management import ManagementCase


class LocalAdmissionTests(ManagementCase):
    anonymous = accounts.AccountsTests.anonymous
    begin = accounts.AccountsTests.begin
    finish = accounts.AccountsTests.finish
    admin = accounts.AccountsTests.admin

    def setUp(self) -> None:
        super().setUp()
        delay_patch = patch.object(local_auth, "sleep")
        self.delay = delay_patch.start()
        self.addCleanup(delay_patch.stop)
        self.now = time.time()
        self.broker.auth.clock = lambda: self.now
        folder = bootstrap.enrollment_file(self.config).parent
        folder.mkdir(parents=True, mode=0o2750)
        folder.chmod(0o2750)
        self.user_id = self.admin()["user"]["id"]
        self.now += 31
        with self.broker.database.connect() as db:
            self.user = dict(
                db.execute("SELECT * FROM users WHERE id=?", (self.user_id,)).fetchone()
            )
        self.device = known_device.issue(self.broker.auth.anonymous.key, self.user, self.now)

    def login(
        self,
        *,
        name="rootadmin",
        address="203.0.113.20",
        password="private secure phrase 48219",
        code=None,
        device=None,
        step_up=False,
    ):
        return local_auth.authenticate(
            self.broker.auth,
            name,
            password,
            local_security.totp_code(self.admin_secret, int(self.now // 30))
            if code is None
            else code,
            address,
            step_up=step_up,
            device=device,
        )

    def wrong_code(self):
        valid = {
            local_security.totp_code(self.admin_secret, int(self.now // 30) + d) for d in (-1, 0, 1)
        }
        return next(f"{n:06d}" for n in range(10) if f"{n:06d}" not in valid)

    def event_count(self, kind):
        with self.broker.database.connect() as db:
            return local_auth.count(db, self.user_id, kind, self.now)

    def exhaust_passwords(self):
        with patch.object(local_auth, "verify_password", return_value=(False, False)):
            for i in range(20):
                self.assert_error(
                    "INVALID_CREDENTIALS",
                    lambda i=i: self.login(address=f"198.51.100.{i + 1}", password="wrong"),
                )
        self.assertEqual(self.event_count("password"), 20)

    def step(
        self, code, *, password="private secure phrase 48219", device=None, address="203.0.113.90"
    ):
        headers = {"x-portal-client-address": address}
        if device:
            headers["cookie"] = (
                self.config.session_cookie
                + "="
                + self.tokens["admin"]
                + "; "
                + self.config.device_cookie
                + "="
                + device
            )
        return self.call(
            "POST",
            "/v1/reauthenticate",
            {"password": password, "totp": code},
            "admin",
            headers=headers,
        )

    def test_distributed_password_guesses_hit_account_budget_and_dummy_path(self) -> None:
        self.exhaust_passwords()
        with (
            patch.object(local_auth, "verify_password", return_value=(True, False)) as verification,
            patch.object(local_auth, "verify_totp") as factor,
        ):
            self.assert_error("INVALID_CREDENTIALS", lambda: self.login(address="192.0.2.250"))
            self.assertEqual(verification.call_args.args[1], local_security.DUMMY_HASH)
            factor.assert_not_called()
        self.assertEqual(self.event_count("password"), 20)

    def test_owner_and_staff_without_mfa_have_the_same_distributed_password_budget(self) -> None:
        for role in ("owner", "staff"):
            with self.subTest(role=role), self.broker.database.connect(write=True) as db:
                db.execute("UPDATE users SET role=? WHERE id=?", (role, self.user_id))
                db.execute(
                    "UPDATE local_accounts SET totp_confirmed=0,totp_secret=NULL WHERE user_id=?",
                    (self.user_id,),
                )
                db.execute("DELETE FROM authentication_failures")
            self.now += 61
            self.exhaust_passwords()
            self.assert_error("INVALID_CREDENTIALS", lambda: self.login(address="192.0.2.250"))

    def test_known_device_succeeds_while_password_budget_is_exhausted(self) -> None:
        self.exhaust_passwords()
        self.assertEqual(self.login(device=self.device)["role"], "admin")
        self.assertEqual(self.event_count("password"), 20)
        self.assertEqual(self.event_count("totp"), 0)

    def test_correct_password_totp_backoff_is_shared_across_addresses_and_paths(self) -> None:
        self.assert_error("INVALID_CREDENTIALS", lambda: self.login(code=self.wrong_code()))
        self.assertEqual(self.event_count("totp"), 1)
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT totp_streak,totp_blocked_until FROM local_accounts WHERE user_id=?",
                    (self.user_id,),
                ).fetchone()[:],
                (1, self.now + 30),
            )
        with patch.object(local_auth, "verify_totp") as factor:
            self.assert_error(
                "INVALID_CREDENTIALS", lambda: self.step(self.wrong_code(), device=self.device)
            )
            factor.assert_not_called()
        self.assertEqual(self.event_count("totp"), 1)
        self.now += 30
        self.assert_error(
            "INVALID_CREDENTIALS", lambda: self.step(self.wrong_code(), device=self.device)
        )
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT totp_streak,totp_blocked_until FROM local_accounts WHERE user_id=?",
                    (self.user_id,),
                ).fetchone()[:],
                (2, self.now + 60),
            )
        self.assert_error(
            "INVALID_CREDENTIALS", lambda: self.login(device=self.device, address="192.0.2.1")
        )
        self.now += 60
        self.assertEqual(self.login(device=self.device)["role"], "admin")
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT totp_streak,totp_blocked_until FROM local_accounts WHERE user_id=?",
                    (self.user_id,),
                ).fetchone()[:],
                (0, 0),
            )
        self.assertEqual(self.event_count("totp"), 2)  # Success cannot reopen the hourly window.

    def test_totp_hard_rolling_window_and_one_hour_cap(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.executemany(
                "INSERT INTO authentication_failures(user_id,kind,created) VALUES(?,'totp',?)",
                [(self.user_id, self.now - 1)] * 9,
            )
            db.execute("UPDATE local_accounts SET totp_streak=20 WHERE user_id=?", (self.user_id,))
        self.assert_error("INVALID_CREDENTIALS", lambda: self.login(code=self.wrong_code()))
        self.assertEqual(self.event_count("totp"), 10)
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT totp_blocked_until FROM local_accounts WHERE user_id=?", (self.user_id,)
                ).fetchone()[0],
                self.now + 3600,
            )
        with patch.object(local_auth, "verify_totp") as factor:
            self.assert_error(
                "INVALID_CREDENTIALS", lambda: self.login(device=self.device, address="192.0.2.1")
            )
            factor.assert_not_called()
        self.now += 3600
        self.assertEqual(self.login(device=self.device)["role"], "admin")

    def test_wrong_passwords_never_charge_or_trigger_totp_backoff(self) -> None:
        self.assert_error("INVALID_CREDENTIALS", lambda: self.login(password="wrong"))
        self.assertEqual(self.event_count("password"), 1)
        self.assertEqual(self.event_count("totp"), 0)
        self.assertEqual(self.login(address="198.51.100.4")["role"], "admin")

    def test_reauthentication_shares_password_budget_and_needs_device_exemption(self) -> None:
        with patch.object(local_auth, "verify_password", return_value=(False, False)):
            for i in range(20):
                self.assert_error(
                    "INVALID_CREDENTIALS",
                    lambda i=i: self.step(
                        self.wrong_code(), password="wrong", address=f"198.51.100.{i + 1}"
                    ),
                )
        self.assertEqual(self.event_count("password"), 20)
        self.assert_error(
            "INVALID_CREDENTIALS",
            lambda: self.step(local_security.totp_code(self.admin_secret, int(self.now // 30))),
        )
        self.assertEqual(
            self.step(
                local_security.totp_code(self.admin_secret, int(self.now // 30)), device=self.device
            ).status,
            200,
        )

    def test_forged_expired_wrong_account_and_old_generation_cookies_are_not_exempt(self) -> None:
        self.exhaust_passwords()
        forged = self.device[:-1] + ("A" if self.device[-1] != "A" else "B")
        expired = known_device.issue(
            self.broker.auth.anonymous.key, self.user, self.now - known_device.LIFETIME - 1
        )
        other = known_device.issue(
            self.broker.auth.anonymous.key,
            {**self.user, "id": "00000000-0000-4000-8000-000000000090"},
            self.now,
        )
        old = known_device.issue(
            self.broker.auth.anonymous.key,
            {**self.user, "generation": self.user["generation"] - 1},
            self.now,
        )
        for token in (forged, expired, other, old, "malformed"):
            with (
                self.subTest(token=token),
                patch.object(
                    local_auth, "verify_password", return_value=(True, False)
                ) as verification,
            ):
                self.assert_error(
                    "INVALID_CREDENTIALS", lambda token=token: self.login(device=token)
                )
                self.assertEqual(verification.call_args.args[1], local_security.DUMMY_HASH)

    def test_generation_change_invalidates_device_and_clears_stale_account_budgets(self) -> None:
        self.exhaust_passwords()
        with self.broker.database.connect(write=True) as db:
            security_change(db, self.user_id)
            user = dict(db.execute("SELECT * FROM users WHERE id=?", (self.user_id,)).fetchone())
        self.assertFalse(
            known_device.valid(self.broker.auth.anonymous.key, self.device, user, self.now)
        )
        self.assertEqual(self.event_count("password"), 0)

    def test_anonymous_flood_cannot_occupy_known_device_or_live_session_lane(self) -> None:
        self.assertTrue(local_security.HASH_SLOTS.acquire(blocking=False))
        try:
            self.assert_error("AUTH_UNAVAILABLE", lambda: self.login(name="unknown"))
            for _ in range(11):
                self.broker.auth.local_limits.check_bucket("203.0.113.20", "start", self.now)
            for _ in range(11):
                self.broker.auth.local_limits.check_bucket("127.0.0.1", "start", self.now)
            for _ in range(5):
                attempt = self.broker.auth.failures.reserve(
                    "local:rootadmin", "203.0.113.20", self.now
                )
                self.broker.auth.failures.finish(attempt, failed=True, succeeded=False)
            self.assertEqual(self.login(device=self.device)["role"], "admin")
            self.now += 31
            self.assertEqual(
                self.step(
                    local_security.totp_code(self.admin_secret, int(self.now // 30)),
                    address="127.0.0.1",
                ).status,
                200,
            )
            with self.broker.database.connect() as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM authentication_failures").fetchone()[0], 0
                )
        finally:
            local_security.HASH_SLOTS.release()

    def test_successful_local_login_issues_private_strict_cookie_and_logout_retains_it(
        self,
    ) -> None:
        csrf, headers = self.anonymous()
        response = self.call(
            "POST",
            "/v1/auth/login",
            {
                "csrfToken": csrf,
                "method": "local",
                "username": "rootadmin",
                "password": "private secure phrase 48219",
                "totp": local_security.totp_code(self.admin_secret, int(self.now // 30)),
            },
            headers=headers,
        ).body
        directive = next(c for c in response["browser"]["cookies"] if c["name"] == "device")
        self.assertTrue(
            known_device.valid(
                self.broker.auth.anonymous.key, directive["value"], self.user, self.now
            )
        )
        self.assertEqual(directive["maxAge"], 90 * 86400)
        web = WebServer(
            ("127.0.0.1", 0),
            dataclasses.replace(self.config, portal_origin="https://portal.example.test"),
            self.root,
        )
        self.addCleanup(web.server_close)
        rendered = web.cookie(directive)
        for part in (
            "__Host-portal-device=",
            "HttpOnly",
            "Secure",
            "SameSite=Strict",
            "Path=/",
            "Max-Age=7776000",
        ):
            self.assertIn(part, rendered)
        self.assertNotIn("Domain=", rendered)
        logout = self.call("POST", "/v1/logout", {}, "admin").body
        self.assertNotIn("device", [c["name"] for c in logout["browser"]["cookies"]])

    def test_unknown_and_exhausted_accounts_use_the_same_dummy_and_generic_error(self) -> None:
        self.exhaust_passwords()
        with patch.object(
            local_auth, "verify_password", return_value=(False, False)
        ) as verification:
            for name, address in (("rootadmin", "192.0.2.1"), ("unknown", "192.0.2.2")):
                self.assert_error(
                    "INVALID_CREDENTIALS",
                    lambda name=name, address=address: self.login(name=name, address=address),
                )
                self.assertEqual(verification.call_args.args[1], local_security.DUMMY_HASH)
        self.assertEqual(self.event_count("password"), 20)

    def test_address_attempt_budget_still_precedes_hash_capacity(self) -> None:
        with patch.object(
            local_auth, "verify_password", return_value=(False, False)
        ) as verification:
            for i in range(12):
                self.assert_error("INVALID_CREDENTIALS", lambda i=i: self.login(name=f"unknown{i}"))
            self.assert_error("RATE_LIMITED", lambda: self.login(name="unknownextra"))
            self.assertEqual(verification.call_count, 12)

    def test_known_browser_options_and_http_login_survive_anonymous_address_saturation(
        self,
    ) -> None:
        self.broker.auth.address_limits.limits = {"options": 1, "start": 1}
        # Bootstrap already consumed both anonymous buckets at the loopback IP.
        self.assert_error("RATE_LIMITED", lambda: self.call("GET", "/v1/auth/options"))
        options = self.call(
            "GET",
            "/v1/auth/options",
            headers={"cookie": self.config.device_cookie + "=" + self.device},
        ).body
        binder = options["browser"]["cookies"][0]["value"]
        response = self.call(
            "POST",
            "/v1/auth/login",
            {
                "csrfToken": options["data"]["csrfToken"],
                "method": "local",
                "username": "rootadmin",
                "password": "private secure phrase 48219",
                "totp": local_security.totp_code(self.admin_secret, int(self.now // 30)),
            },
            headers={
                "cookie": self.config.device_cookie
                + "="
                + self.device
                + "; "
                + self.config.login_cookie
                + "="
                + binder
            },
        )
        self.assertEqual(response.status, 200)

    def test_password_failures_and_budget_denials_have_the_same_failure_latency_floor(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.executemany(
                "INSERT INTO authentication_failures(user_id,kind,created) VALUES(?,'password',?)",
                [(self.user_id, self.now)] * 20,
            )
        for device in (None, self.device):
            self.delay.reset_mock()
            with (
                patch.object(local_auth, "verify_password", return_value=(False, False)),
                patch.object(local_auth, "monotonic", side_effect=[10.0, 10.25]),
            ):
                self.assert_error(
                    "INVALID_CREDENTIALS", lambda device=device: self.login(device=device)
                )
            self.delay.assert_called_once_with(0.75)

    def test_step_up_rejects_unvalidated_address_before_hashing(self) -> None:
        with patch.object(local_auth, "verify_password") as verification:
            self.assert_error("INVALID_REQUEST", lambda: self.step("000000", address="untrusted"))
            verification.assert_not_called()
