"""Regression coverage for anonymous backoff saturation and live-admin isolation."""

from __future__ import annotations

import time
from unittest.mock import patch

from openstack_platform.management.broker import bootstrap, local_auth, local_security
from openstack_platform.management.common import digest
from tests import test_management_accounts as accounts
from tests.test_management import ManagementCase


class LocalAdmissionTests(ManagementCase):
    anonymous = accounts.AccountsTests.anonymous
    begin = accounts.AccountsTests.begin
    finish = accounts.AccountsTests.finish
    admin = accounts.AccountsTests.admin

    def setUp(self) -> None:
        super().setUp()
        self.now = time.time()
        self.broker.auth.clock = lambda: self.now
        folder = bootstrap.enrollment_file(self.config).parent
        folder.mkdir(parents=True, mode=0o2750)
        folder.chmod(0o2750)
        self.admin()
        self.now += 31

    def login(
        self, *, name="rootadmin", address="203.0.113.20", password="private secure phrase 48219"
    ):
        return local_auth.authenticate(
            self.broker.auth,
            name,
            password,
            local_security.totp_code(self.admin_secret, int(self.now // 30)),
            address,
        )

    def test_full_backoff_table_evicts_oldest_non_blocked_row(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.executemany(
                "INSERT INTO login_backoff VALUES(?,?,?,?,?,?)",
                [
                    (
                        f"{i:064x}",
                        1,
                        self.now + 5 if i else 0,
                        0,
                        self.now - 100 if i else self.now - 200,
                        "198.51.100.4",
                    )
                    for i in range(4096)
                ],
            )
        self.assertEqual(self.login()["role"], "admin")
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM login_backoff").fetchone()[0], 4096)
            self.assertIsNone(
                db.execute("SELECT 1 FROM login_backoff WHERE name_hash=?", ("0" * 64,)).fetchone()
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM login_backoff WHERE name_hash=?", (digest("local:rootadmin"),)
                ).fetchone()
            )

    def test_all_rows_protected_does_not_lock_out_a_new_username(self) -> None:
        with self.broker.database.connect(write=True) as db:
            db.executemany(
                "INSERT INTO login_backoff VALUES(?,?,?,?,?,?)",
                [
                    (f"{i:064x}", 4, self.now + 5, self.now + 30, self.now, "198.51.100.4")
                    for i in range(4096)
                ],
            )
        self.assertEqual(self.login()["role"], "admin")
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM login_backoff").fetchone()[0], 4096)
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM login_backoff WHERE name_hash=?", (digest("local:rootadmin"),)
                ).fetchone()
            )

    def test_targeted_username_backoff_is_short_and_does_not_block_other_sources(self) -> None:
        attacker = "198.51.100.4"
        self.assert_error(
            "INVALID_CREDENTIALS", lambda: self.login(address=attacker, password="wrong")
        )
        with self.broker.database.connect() as db:
            original = db.execute(
                "SELECT blocked_until FROM login_backoff WHERE name_hash=?",
                (digest("local:rootadmin"),),
            ).fetchone()[0]
        self.assertLessEqual(original - self.now, local_auth.MAX_BACKOFF_SECONDS)
        self.assert_error(
            "INVALID_CREDENTIALS", lambda: self.login(address=attacker, password="wrong")
        )
        with self.broker.database.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT blocked_until FROM login_backoff WHERE name_hash=?",
                    (digest("local:rootadmin"),),
                ).fetchone()[0],
                original,
            )
        self.assertEqual(self.login(address="203.0.113.40")["role"], "admin")
        self.now += 900
        self.assert_error(
            "INVALID_CREDENTIALS", lambda: self.login(address=attacker, password="wrong")
        )
        with self.broker.database.connect() as db:
            self.assertLessEqual(
                db.execute(
                    "SELECT blocked_until FROM login_backoff WHERE name_hash=?",
                    (digest("local:rootadmin"),),
                ).fetchone()[0]
                - self.now,
                local_auth.MAX_BACKOFF_SECONDS,
            )

    def test_address_row_creation_and_attempt_budgets_precede_hashing(self) -> None:
        with patch.object(
            local_auth, "verify_password", return_value=(False, False)
        ) as verification:
            for i in range(6):
                self.assert_error("INVALID_CREDENTIALS", lambda i=i: self.login(name=f"unknown{i}"))
            self.assert_error("RATE_LIMITED", lambda: self.login(name="unknownextra"))
            self.assertEqual(verification.call_count, 6)
        with self.broker.database.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM login_backoff").fetchone()[0], 6)
        # Existing usernames also have an aggregate address budget, not merely
        # per-name limits which can be escaped by cycling the name.
        self.now += 61
        with patch.object(
            local_auth, "verify_password", return_value=(False, False)
        ) as verification:
            for i in range(12):
                self.assert_error(
                    "INVALID_CREDENTIALS", lambda i=i: self.login(name=f"unknown{i % 6}")
                )
                self.now += 1
            self.assert_error("RATE_LIMITED", lambda: self.login(name="rootadmin"))
            self.assertLessEqual(verification.call_count, 12)

    def test_anonymous_slot_exhaustion_allocates_no_row_and_cannot_shed_step_up(self) -> None:
        self.assertTrue(local_security.HASH_SLOTS.acquire(blocking=False))
        try:
            self.assert_error("AUTH_UNAVAILABLE", lambda: self.login(name="newunknown"))
            with self.broker.database.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM login_backoff").fetchone()[0], 0)
                # Even a pre-existing targeted name block cannot stop step-up.
            with self.broker.database.connect(write=True) as db:
                db.execute(
                    "INSERT INTO login_backoff VALUES(?,32,?,0,?,?)",
                    (digest("local:rootadmin"), self.now + 900, self.now, "127.0.0.1"),
                )
            # Saturate the anonymous address budget at the same address.
            for _ in range(12):
                self.broker.auth.local_limits.check_bucket("127.0.0.1", "start", self.now)
            result = self.call(
                "POST",
                "/v1/reauthenticate",
                {
                    "password": "private secure phrase 48219",
                    "totp": local_security.totp_code(self.admin_secret, int(self.now // 30)),
                },
                "admin",
            )
            self.assertEqual(result.status, 200)
        finally:
            local_security.HASH_SLOTS.release()

    def test_step_up_rejects_unvalidated_address_before_hashing(self) -> None:
        with patch.object(local_auth, "verify_password") as verification:
            self.assert_error(
                "INVALID_REQUEST",
                lambda: self.call(
                    "POST",
                    "/v1/reauthenticate",
                    {"password": "private secure phrase 48219", "totp": "000000"},
                    "admin",
                    headers={"x-portal-client-address": "untrusted"},
                ),
            )
            verification.assert_not_called()
