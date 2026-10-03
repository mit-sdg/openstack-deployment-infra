"""Typed, identity-verified offline enrollment; no network or service-control rights."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from ... import durable
from ...validation import uuid as checked_uuid
from ..common import canonical, digest
from ..config import Config, management_peer
from .database import validate_database
from .staff_policy import GRANT_SECONDS, MEMBER_LIMIT


def change_grant(
    db: sqlite3.Connection,
    config: Config,
    *,
    action: str,
    user_id: str,
    issuer: str,
    subject: str,
    review: str,
    days: int = 90,
    now: float | None = None,
) -> int:
    """Caller holds BEGIN IMMEDIATE; all changes, audit and revocation commit together."""
    checked_uuid(user_id)
    checked_uuid(subject)
    if action not in {"grant", "renew", "revoke"} or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", review
    ):
        raise ValueError("invalid grant action or review reference")
    if type(days) is not int or not 1 <= days <= GRANT_SECONDS // 86400:
        raise ValueError("grant days must be 1–90")
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if (
        user is None
        or user["issuer"] != issuer
        or issuer != config.issuer
        or user["subject"] != subject
    ):
        raise ValueError("independently verified identity does not match broker record")
    old = db.execute("SELECT * FROM staff_grants WHERE user_id=?", (user_id,)).fetchone()
    if action == "renew" and old is None:
        raise ValueError("renew requires an existing grant")
    if action != "revoke" and not user["enabled"]:
        raise ValueError("disabled account cannot receive a grant")
    if action != "revoke" and (old is None or not old["enabled"]):
        count = db.execute("SELECT COUNT(*) FROM staff_grants WHERE enabled=1").fetchone()[0]
        if count >= MEMBER_LIMIT:
            raise ValueError("staff membership limit reached")
    generation = 1 if old is None else old["generation"] + 1
    timestamp = time.time() if now is None else now
    db.execute(
        "INSERT INTO staff_grants VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,generation=excluded.generation,valid_until=excluded.valid_until,updated=excluded.updated",
        (
            user_id,
            int(action != "revoke"),
            generation,
            timestamp + days * 86400,
            timestamp,
            timestamp,
        ),
    )
    db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    db.execute(
        "INSERT INTO staff_grant_audit(user_id,action,generation,actor_uid,review,created) VALUES(?,?,?,?,?,?)",
        (user_id, action, generation, os.geteuid(), review, timestamp),
    )
    return generation


def run(config: Config, arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(
        description="Offline staff grants after independent identity verification"
    )
    parser.add_argument("action", choices=("list", "inspect", "grant", "renew", "revoke"))
    parser.add_argument("--user-id")
    parser.add_argument("--issuer")
    parser.add_argument("--subject")
    parser.add_argument("--review")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args(arguments)
    peer = (os.geteuid(), os.getegid())
    if not config.development and peer != management_peer("managementBroker"):
        raise ValueError("staff recovery requires the broker identity")
    if os.path.lexists(config.broker_socket):
        raise ValueError("stop portal admissions and broker before staff recovery")
    path = config.state_directory / "management.sqlite3"
    durable.validate_file(path, mode=0o600, maximum_bytes=1024**3)
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
        if validate_database(db, digest(config.portal_origin + "\n" + config.issuer))[0] != 3:
            raise ValueError("staff recovery requires schema 3")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        if args.action == "list":
            rows = db.execute(
                "SELECT user_id,enabled,generation,valid_until FROM staff_grants ORDER BY enabled DESC,updated DESC,user_id LIMIT 101"
            ).fetchall()
            print(
                canonical(
                    {"items": [dict(row) for row in rows[:100]], "truncated": len(rows) > 100}
                )
            )
        elif args.action == "inspect":
            identifier = checked_uuid(args.user_id)
            row = db.execute(
                "SELECT id,issuer,subject,enabled FROM users WHERE id=?", (identifier,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown broker user")
            print(canonical(dict(row)))
        else:
            if not all((args.user_id, args.issuer, args.subject, args.review)):
                raise ValueError("grant changes require user-id, issuer, subject and review")
            generation = change_grant(
                db,
                config,
                action=args.action,
                user_id=args.user_id,
                issuer=args.issuer,
                subject=args.subject,
                review=args.review,
                days=args.days,
            )
            print(
                canonical(
                    {"action": args.action, "generation": generation, "sessions": "invalidated"}
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args, remaining = parser.parse_known_args()
    run(Config.load(args.config), remaining)


if __name__ == "__main__":
    main()
