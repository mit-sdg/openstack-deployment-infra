"""Account-wide guessing budgets, recognized browsers and bounded hash admission."""

from __future__ import annotations

import sqlite3
from time import monotonic, sleep
from typing import TYPE_CHECKING, Any

from ...controller.http import HttpError
from . import known_device
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

PASSWORD_FAILURES = 20
TOTP_FAILURES = 10
WINDOW_SECONDS = 3600
TOTP_BACKOFF_SECONDS = 30
MAX_TOTP_BACKOFF_SECONDS = 3600
FAILURE_FLOOR_SECONDS = 1.0


def invalid() -> HttpError:
    return HttpError(
        401, "INVALID_CREDENTIALS", "Username, password or authentication code is incorrect."
    )


def account(db: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT users.*,local_accounts.password_hash,local_accounts.totp_secret,local_accounts.totp_confirmed,local_accounts.last_counter,local_accounts.totp_streak,local_accounts.totp_blocked_until FROM users JOIN local_accounts ON user_id=users.id WHERE issuer='local' AND username=?",
        (username,),
    ).fetchone()
    return dict(row) if row else None


def count(db: sqlite3.Connection, user: str, kind: str, now: float) -> int:
    return int(
        db.execute(
            "SELECT COUNT(*) FROM authentication_failures WHERE user_id=? AND kind=? AND created>?",
            (user, kind, now - WINDOW_SECONDS),
        ).fetchone()[0]
    )


def blocked(
    db: sqlite3.Connection, user: dict[str, Any] | None, recognized: bool, now: float
) -> bool:
    return (
        user is None
        or not user["enabled"]
        or user["status"] != "active"
        or not user["password_hash"]
        or user["totp_blocked_until"] > now
        or count(db, user["id"], "totp", now) >= TOTP_FAILURES
        or not recognized
        and count(db, user["id"], "password", now) >= PASSWORD_FAILURES
    )


def failure(db: sqlite3.Connection, user: str, kind: str, now: float) -> None:
    # At most 20 password + 10 TOTP events per live account/hour. Unknown names
    # allocate no rows, and there is no global table cap that can lock out users.
    db.execute(
        "DELETE FROM authentication_failures WHERE user_id=? AND created<=?",
        (user, now - WINDOW_SECONDS),
    )
    maximum = PASSWORD_FAILURES if kind == "password" else TOTP_FAILURES
    if count(db, user, kind, now) < maximum:
        db.execute(
            "INSERT INTO authentication_failures(user_id,kind,created) VALUES(?,?,?)",
            (user, kind, now),
        )


def authenticate(
    auth: Auth,
    username: str,
    password: str,
    code: object,
    address: str,
    *,
    step_up: bool = False,
    device: object = None,
) -> dict[str, Any]:
    started = monotonic()
    hashed = False
    now = auth.clock()
    with auth.database.connect() as db:
        user = account(db, username)
        recognized = known_device.valid(auth.anonymous.key, device, user, now)
        denied = blocked(db, user, recognized, now)
    trusted = recognized or step_up
    limits = auth.step_up_limits if trusted else auth.local_limits
    failures = auth.step_up_failures if trusted else auth.failures
    # Separate recognized/session budgets keep an anonymous flood at the same
    # address from consuming the returning user's admission or hashing capacity.
    limits.check_bucket(address, "start", now)
    window = failures.reserve("local:" + username, address, now)
    succeeded = failed = False
    try:
        with hashing_slot(step_up=trusted):
            # Exhausted budgets, unavailable accounts and unknown names all do
            # the standard dummy verification and return the same generic error.
            encoded = DUMMY_HASH if denied else user["password_hash"] if user else DUMMY_HASH
            hashed = True
            valid, upgrade = verify_password(password, encoded, step_up=trusted)
            with auth.database.connect(write=True) as db:
                now = auth.clock()
                latest = account(db, username)
                recognized_now = known_device.valid(auth.anonymous.key, device, latest, now)
                eligible = (
                    not denied
                    and latest is not None
                    and user is not None
                    and latest["generation"] == user["generation"]
                    and latest["password_hash"] == encoded
                    and not blocked(db, latest, recognized_now, now)
                )
                if eligible and latest is not None:
                    if not valid:
                        failure(db, latest["id"], "password", now)
                        eligible = False
                    elif latest["totp_confirmed"] and latest["totp_secret"]:
                        counter = verify_totp(
                            latest["totp_secret"], code, now, latest["last_counter"]
                        )
                        if counter is None:
                            failure(db, latest["id"], "totp", now)
                            streak = min(latest["totp_streak"] + 1, 32)
                            delay = min(
                                MAX_TOTP_BACKOFF_SECONDS,
                                TOTP_BACKOFF_SECONDS * 2 ** min(streak - 1, 7),
                            )
                            db.execute(
                                "UPDATE local_accounts SET totp_streak=?,totp_blocked_until=? WHERE user_id=?",
                                (streak, now + delay, latest["id"]),
                            )
                            eligible = False
                        else:
                            db.execute(
                                "UPDATE local_accounts SET last_counter=?,totp_streak=0,totp_blocked_until=0 WHERE user_id=?",
                                (counter, latest["id"]),
                            )
                    elif latest["role"] == "admin":
                        eligible = False
                succeeded = bool(eligible)
                failed = not succeeded
                if succeeded and latest is not None:
                    # Preserve rolling failure windows on success; resetting only
                    # the exponential streak cannot reopen an hourly guessing budget.
                    db.execute(
                        "UPDATE local_accounts SET totp_streak=0,totp_blocked_until=0 WHERE user_id=?",
                        (latest["id"],),
                    )
                    result = dict(latest)
            if not succeeded:
                raise invalid()
            # Rehash outside writer locks, then recheck the exact credential and
            # generation. It shares the already acquired lane, with no nested queue.
            if upgrade:
                replacement = hash_password(password, step_up=trusted)
                with auth.database.connect(write=True) as db:
                    current = account(db, username)
                    if (
                        current is None
                        or current["generation"] != result["generation"]
                        or current["password_hash"] != encoded
                        or not current["enabled"]
                        or current["status"] != "active"
                    ):
                        raise invalid()
                    db.execute(
                        "UPDATE local_accounts SET password_hash=? WHERE user_id=?",
                        (replacement, result["id"]),
                    )
            return result
    except HashCapacityError:
        raise HttpError(
            503, "AUTH_UNAVAILABLE", "Sign-in is temporarily unavailable.", retryable=True
        ) from None
    finally:
        failures.finish(window, failed=failed, succeeded=succeeded)
        if hashed and not succeeded:
            # Equalize credential failures, including legacy hashes, exhausted
            # budgets and unknown names. Never sleep while holding a hash slot.
            remaining = FAILURE_FLOOR_SECONDS - (monotonic() - started)
            if remaining > 0:
                sleep(remaining)
