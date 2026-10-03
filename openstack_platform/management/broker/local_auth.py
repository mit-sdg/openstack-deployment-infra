"""Local credential admission, source-scoped backoff and reserved live-admin capacity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...controller.http import HttpError
from ..common import digest
from .local_security import (
    DUMMY_HASH,
    HashCapacityError,
    hash_password,
    hashing_slot,
    verify_password,
    verify_totp,
)

if TYPE_CHECKING:
    from .auth import Auth

BACKOFF_ROWS = 4096
MAX_BACKOFF_SECONDS = 5


def invalid() -> HttpError:
    return HttpError(
        401, "INVALID_CREDENTIALS", "Username, password or authentication code is incorrect."
    )


def authenticate(
    auth: Auth, username: str, password: str, code: object, address: str, *, step_up: bool = False
) -> dict[str, Any]:
    now = auth.clock()
    limits = auth.step_up_limits if step_up else auth.local_limits
    failures = auth.step_up_failures if step_up else auth.failures
    # No DB allocation and no scrypt slot before validated-address admission.
    limits.check_bucket(address, "start", now)
    name = digest("local:" + username)
    if not step_up:
        with auth.database.connect() as db:
            exists = db.execute("SELECT 1 FROM login_backoff WHERE name_hash=?", (name,)).fetchone()
        if not exists:
            limits.check_bucket(address, "options", now)  # New-name allocation budget.
    window = failures.reserve("local:" + username, address, now)
    reserved = succeeded = failed = False
    try:
        # Keep one independent slot for proof from a live local-admin session.
        with hashing_slot(step_up=step_up):
            with auth.database.connect(write=True) as db:
                tracked = False
                if not step_up:
                    db.execute(
                        "DELETE FROM login_backoff WHERE name_hash IN (SELECT name_hash FROM login_backoff WHERE updated<? AND attempt_until<=? LIMIT 100)",
                        (now - 3600, now),
                    )
                    backoff = db.execute(
                        "SELECT * FROM login_backoff WHERE name_hash=?", (name,)
                    ).fetchone()
                    if backoff is not None and (
                        backoff["attempt_until"] > now
                        or backoff["blocked_until"] > now
                        and backoff["blocked_address"] == address
                    ):
                        raise invalid()
                    if (
                        backoff is None
                        and db.execute("SELECT COUNT(*) FROM login_backoff").fetchone()[0]
                        >= BACKOFF_ROWS
                    ):
                        db.execute(
                            "DELETE FROM login_backoff WHERE name_hash IN (SELECT name_hash FROM login_backoff WHERE blocked_until<=? AND attempt_until<=? ORDER BY updated,name_hash LIMIT 1)",
                            (now, now),
                        )
                    if (
                        backoff is not None
                        or db.execute("SELECT COUNT(*) FROM login_backoff").fetchone()[0]
                        < BACKOFF_ROWS
                    ):
                        db.execute(
                            "INSERT INTO login_backoff(name_hash,failures,blocked_until,attempt_until,updated,blocked_address) VALUES(?,0,0,?,?,?) ON CONFLICT(name_hash) DO UPDATE SET attempt_until=excluded.attempt_until,updated=excluded.updated",
                            (name, now + 30, now, address),
                        )
                        reserved = tracked = True
                    # All rows protected: authenticate without allocating. Existing
                    # blocked windows stay protected; capacity never locks out a new name.
                row = db.execute(
                    "SELECT users.*,local_accounts.password_hash,local_accounts.totp_secret,local_accounts.totp_confirmed,local_accounts.last_counter FROM users JOIN local_accounts ON user_id=users.id WHERE issuer='local' AND username=?",
                    (username,),
                ).fetchone()
                user = dict(row) if row is not None else None
            encoded = (
                user["password_hash"] if user is not None and user["password_hash"] else DUMMY_HASH
            )
            valid, upgrade = verify_password(password, encoded, step_up=step_up)
            replacement = hash_password(password, step_up=step_up) if valid and upgrade else None
            with auth.database.connect(write=True) as db:
                latest = db.execute(
                    "SELECT users.*,local_accounts.password_hash,local_accounts.totp_secret,local_accounts.totp_confirmed,local_accounts.last_counter FROM users JOIN local_accounts ON user_id=users.id WHERE issuer='local' AND username=?",
                    (username,),
                ).fetchone()
                eligible = (
                    valid
                    and user is not None
                    and latest is not None
                    and latest["generation"] == user["generation"]
                    and latest["password_hash"] == encoded
                    and latest["enabled"]
                    and latest["status"] == "active"
                )
                counter = None
                if eligible and latest is not None:
                    if latest["totp_confirmed"] and latest["totp_secret"]:
                        counter = verify_totp(
                            latest["totp_secret"], code, auth.clock(), latest["last_counter"]
                        )
                        eligible = counter is not None
                    elif latest["role"] == "admin":
                        eligible = False
                succeeded = bool(eligible)
                failed = not succeeded
                if succeeded and latest is not None:
                    if counter is not None:
                        db.execute(
                            "UPDATE local_accounts SET last_counter=? WHERE user_id=?",
                            (counter, latest["id"]),
                        )
                    if replacement is not None:
                        db.execute(
                            "UPDATE local_accounts SET password_hash=? WHERE user_id=?",
                            (replacement, latest["id"]),
                        )
                    if tracked:
                        db.execute(
                            "UPDATE login_backoff SET failures=0,blocked_until=0,attempt_until=0,updated=?,blocked_address='' WHERE name_hash=?",
                            (auth.clock(), name),
                        )
                    result = dict(latest)
                elif tracked:
                    count = (
                        db.execute(
                            "SELECT failures FROM login_backoff WHERE name_hash=?", (name,)
                        ).fetchone()[0]
                        + 1
                    )
                    db.execute(
                        "UPDATE login_backoff SET failures=?,blocked_until=?,attempt_until=0,updated=?,blocked_address=? WHERE name_hash=?",
                        (
                            min(count, 32),
                            auth.clock() + min(MAX_BACKOFF_SECONDS, 2 ** min(count - 1, 3)),
                            auth.clock(),
                            address,
                            name,
                        ),
                    )
            reserved = False
            if not succeeded:
                raise invalid()
            return result
    except HashCapacityError:
        raise HttpError(
            503, "AUTH_UNAVAILABLE", "Sign-in is temporarily unavailable.", retryable=True
        ) from None
    finally:
        if reserved:
            with auth.database.connect(write=True) as db:
                db.execute("UPDATE login_backoff SET attempt_until=0 WHERE name_hash=?", (name,))
        failures.finish(window, failed=failed, succeeded=succeeded)
