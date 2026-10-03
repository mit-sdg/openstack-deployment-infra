"""Password/TOTP verification with bounded work and persistent per-name backoff."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...controller.http import HttpError
from ..common import digest
from .local_security import (
    DUMMY_HASH,
    HashCapacityError,
    hash_password,
    verify_password,
    verify_totp,
)

if TYPE_CHECKING:
    from .auth import Auth


def invalid() -> HttpError:
    return HttpError(
        401, "INVALID_CREDENTIALS", "Username, password or authentication code is incorrect."
    )


def authenticate(
    auth: Auth, username: str, password: str, code: object, address: str
) -> dict[str, Any]:
    now = auth.clock()
    window = auth.failures.reserve("local:" + username, address, now)
    name = digest("local:" + username)
    reserved = succeeded = failed = False
    try:
        with auth.database.connect(write=True) as db:
            db.execute(
                "DELETE FROM login_backoff WHERE name_hash IN (SELECT name_hash FROM login_backoff WHERE updated<? AND attempt_until<? LIMIT 100)",
                (now - 3600, now),
            )
            backoff = db.execute(
                "SELECT * FROM login_backoff WHERE name_hash=?", (name,)
            ).fetchone()
            if backoff is not None and (
                backoff["blocked_until"] > now or backoff["attempt_until"] > now
            ):
                raise invalid()
            if (
                backoff is None
                and db.execute("SELECT COUNT(*) FROM login_backoff").fetchone()[0] >= 4096
            ):
                raise invalid()
            db.execute(
                "INSERT INTO login_backoff VALUES(?,0,0,?,?) ON CONFLICT(name_hash) DO UPDATE SET attempt_until=excluded.attempt_until,updated=excluded.updated",
                (name, now + 30, now),
            )
            reserved = True
            row = db.execute(
                "SELECT users.*,local_accounts.password_hash,local_accounts.totp_secret,local_accounts.totp_confirmed,local_accounts.last_counter FROM users JOIN local_accounts ON user_id=users.id WHERE issuer='local' AND username=?",
                (username,),
            ).fetchone()
            user = dict(row) if row is not None else None
        encoded = (
            user["password_hash"] if user is not None and user["password_hash"] else DUMMY_HASH
        )
        valid, upgrade = verify_password(password, encoded)
        replacement = hash_password(password) if valid and upgrade else None
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
                db.execute(
                    "UPDATE login_backoff SET failures=0,blocked_until=0,attempt_until=0,updated=? WHERE name_hash=?",
                    (auth.clock(), name),
                )
                result = dict(latest)
            else:
                count = (
                    db.execute(
                        "SELECT failures FROM login_backoff WHERE name_hash=?", (name,)
                    ).fetchone()[0]
                    + 1
                )
                db.execute(
                    "UPDATE login_backoff SET failures=?,blocked_until=?,attempt_until=0,updated=? WHERE name_hash=?",
                    (
                        min(count, 32),
                        auth.clock() + min(900, 2 ** min(count - 1, 10)),
                        auth.clock(),
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
        auth.failures.finish(window, failed=failed, succeeded=succeeded)
