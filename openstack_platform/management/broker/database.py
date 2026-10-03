"""Private broker identities, local credential hashes/TOTP state, ownership and audit."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

from ..common import digest
from ..config import Config

SCHEMA_V1 = """
CREATE TABLE metadata (version INTEGER NOT NULL, identity TEXT NOT NULL);
CREATE TABLE users (id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
 username TEXT NOT NULL, display_name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 created REAL NOT NULL, last_login REAL NOT NULL, UNIQUE(issuer,subject));
CREATE TABLE flows (binder TEXT PRIMARY KEY, csrf TEXT NOT NULL, state TEXT UNIQUE, nonce TEXT,
 status TEXT NOT NULL, expires REAL NOT NULL, return_path TEXT NOT NULL DEFAULT '/apps',
 claims TEXT, validated REAL, assertion_expires REAL);
CREATE TABLE replays (issuer TEXT NOT NULL, jti TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(issuer,jti));
CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 created REAL NOT NULL, last_used REAL NOT NULL, expires REAL NOT NULL, kid TEXT NOT NULL);
CREATE TABLE csrf_tokens (token TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(token) ON DELETE CASCADE,
 expires REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE quotas (user_id TEXT PRIMARY KEY REFERENCES users(id), apps INTEGER NOT NULL, concurrent INTEGER NOT NULL);
CREATE TABLE intents (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), app_id TEXT,
 kind TEXT NOT NULL, client_key TEXT NOT NULL, controller_key TEXT NOT NULL UNIQUE,
 fingerprint TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL,
 state TEXT NOT NULL, operation_id TEXT, operation TEXT, safe_error TEXT,
 created REAL NOT NULL, updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_retry REAL NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0,
 UNIQUE(user_id,client_key));
CREATE TABLE apps (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), slug TEXT NOT NULL UNIQUE,
 lifecycle TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
 create_intent TEXT NOT NULL REFERENCES intents(id));
CREATE TABLE configurations (app_id TEXT NOT NULL REFERENCES apps(id), revision INTEGER NOT NULL,
 repository TEXT NOT NULL, branch TEXT NOT NULL, configuration TEXT NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY(app_id,revision));
CREATE TABLE audit (sequence INTEGER PRIMARY KEY, user_id TEXT REFERENCES users(id), app_id TEXT,
 intent_id TEXT, action TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE observations (app_id TEXT PRIMARY KEY REFERENCES apps(id), body TEXT NOT NULL, updated REAL NOT NULL);
CREATE INDEX intent_owner_state ON intents(user_id,state);
CREATE INDEX intent_recovery ON intents(state,next_retry,lease);
"""

MIGRATION_2 = """
DELETE FROM csrf_tokens;
DELETE FROM sessions;
DROP TABLE flows;
DROP TABLE replays;
ALTER TABLE sessions DROP COLUMN kid;
UPDATE metadata SET version=2;
"""
SCHEMA = SCHEMA_V1
SCHEMA_VERSION = 3

MIGRATION_3 = """
DELETE FROM csrf_tokens;
DELETE FROM sessions;
DROP TABLE csrf_tokens;
DROP TABLE sessions;
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'owner' CHECK(role IN ('owner','staff','admin') AND (role!='admin' OR issuer='local'));
ALTER TABLE users ADD COLUMN generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>0);
ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','pending'));
CREATE UNIQUE INDEX local_username ON users(username COLLATE NOCASE) WHERE issuer='local';
CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 created REAL NOT NULL, last_used REAL NOT NULL, expires REAL NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('owner','staff','admin')),
 generation INTEGER NOT NULL CHECK(generation>0), reauthenticated_at REAL NOT NULL DEFAULT 0);
CREATE TABLE csrf_tokens (token TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(token) ON DELETE CASCADE,
 expires REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE local_accounts (user_id TEXT PRIMARY KEY REFERENCES users(id),
 password_hash TEXT, totp_secret TEXT, totp_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(totp_confirmed IN (0,1)),
 last_counter INTEGER NOT NULL DEFAULT -1);
CREATE TABLE login_backoff (name_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL,
 blocked_until REAL NOT NULL, attempt_until REAL NOT NULL, updated REAL NOT NULL, blocked_address TEXT NOT NULL DEFAULT '');
CREATE TABLE token_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), valid_after REAL NOT NULL);
INSERT INTO token_policy VALUES(1,0);
CREATE TABLE account_tokens (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
 user_id TEXT NOT NULL REFERENCES users(id), generation INTEGER NOT NULL,
 purpose TEXT NOT NULL CHECK(purpose IN ('invite','password-reset','totp-reset')),
 expires REAL NOT NULL, created REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0 CHECK(failures BETWEEN 0 AND 5));
CREATE TABLE used_tokens (id TEXT PRIMARY KEY, purpose TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE enrollments (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 generation INTEGER NOT NULL, binder_hash TEXT NOT NULL, purpose TEXT NOT NULL,
 secret TEXT, password_hash TEXT, expires REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0);
CREATE TABLE admin_audit (sequence INTEGER PRIMARY KEY, actor_id TEXT REFERENCES users(id),
 target_id TEXT REFERENCES users(id), action TEXT NOT NULL, details TEXT NOT NULL,
 created REAL NOT NULL);
CREATE TABLE staff_read_audit (
 sequence INTEGER PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES users(id),
 correlation TEXT NOT NULL, route TEXT NOT NULL, owner_id TEXT, app_id TEXT,
 deployment_id TEXT, page_limit INTEGER, cursor_used INTEGER NOT NULL,
 result_count INTEGER NOT NULL, outcome TEXT NOT NULL, status INTEGER NOT NULL,
 stale INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE staff_read_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 row_count INTEGER NOT NULL CHECK(row_count>=0), pruned_at REAL NOT NULL);
INSERT INTO staff_read_state VALUES(1,0,0);
CREATE INDEX staff_read_age ON staff_read_audit(created,sequence);
CREATE INDEX staff_read_actor ON staff_read_audit(actor_id,created);
CREATE INDEX admin_audit_age ON admin_audit(created DESC,sequence DESC);
CREATE INDEX account_token_age ON account_tokens(expires);
CREATE INDEX enrollment_age ON enrollments(expires);
CREATE INDEX user_page ON users(created DESC,id DESC);
CREATE INDEX user_name ON users(username,id);
CREATE INDEX app_page ON apps(created DESC,id DESC);
CREATE INDEX app_owner_page ON apps(user_id,created DESC,id DESC);
CREATE INDEX intent_page ON intents(created DESC,id DESC);
CREATE INDEX intent_owner_page ON intents(user_id,created DESC,id DESC);
CREATE INDEX intent_app_page ON intents(app_id,created DESC,id DESC);
UPDATE metadata SET version=3;
"""


class Database:
    def __init__(self, config: Config) -> None:
        self.path = config.state_directory / "management.sqlite3"
        config.state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if config.state_directory.is_symlink() or self.path.is_symlink():
            raise ValueError("management state must not be a symlink")
        os.chmod(config.state_directory, 0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        identity = digest(config.portal_origin + "\n" + config.issuer)
        lock = os.open(
            config.state_directory / "migration.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.initialize(identity)
        finally:
            os.close(lock)
        os.chmod(self.path, 0o600)

    def initialize(self, identity: str) -> None:
        with self.connect(write=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone()
            if not exists:
                db.executescript("BEGIN IMMEDIATE;" + SCHEMA_V1)
                db.execute("INSERT INTO metadata VALUES(1,?)", (identity,))
            row = db.execute("SELECT * FROM metadata").fetchone()
            if row is None or row["version"] not in (1, 2, 3) or row["identity"] != identity:
                raise ValueError("unsupported management schema or deployment identity")
            db.execute(
                "CREATE TABLE IF NOT EXISTS observations (app_id TEXT PRIMARY KEY REFERENCES apps(id), body TEXT NOT NULL, updated REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
            )
            migration = db.execute(
                "SELECT checksum FROM schema_migrations WHERE version=1"
            ).fetchone()
            if migration is None:
                db.execute("INSERT INTO schema_migrations VALUES(1,?)", (digest(SCHEMA_V1),))
            elif migration[0] != digest(SCHEMA_V1):
                raise ValueError("management migration checksum mismatch")
            if row["version"] == 1:
                # No automatic ownership reassignment by mutable usernames.
                # Sessions are invalidated when the authentication model changes.
                for statement in MIGRATION_2.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO schema_migrations VALUES(2,?)", (digest(MIGRATION_2),))
            second = db.execute("SELECT checksum FROM schema_migrations WHERE version=2").fetchone()
            if second is None or second[0] != digest(MIGRATION_2):
                raise ValueError("management migration checksum mismatch")
            if row["version"] < 3:
                for statement in MIGRATION_3.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(
                    "UPDATE token_policy SET valid_after=? WHERE singleton=1", (time.time(),)
                )
                db.execute("INSERT INTO schema_migrations VALUES(3,?)", (digest(MIGRATION_3),))
            third = db.execute("SELECT checksum FROM schema_migrations WHERE version=3").fetchone()
            if third is None or third[0] != digest(MIGRATION_3):
                raise ValueError("management migration checksum mismatch")

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
            os.chmod(self.path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = self.path.with_name(self.path.name + suffix)
                try:
                    os.chmod(sidecar, 0o600)
                except FileNotFoundError:
                    # SQLite removes these when its last connection closes.
                    # Another connection may close between stat and chmod;
                    # disappearance is normal, including after a committed write.
                    continue


def validate_database(
    connection: sqlite3.Connection, identity: str | None = None
) -> tuple[int, str]:
    if [row[0] for row in connection.execute("PRAGMA integrity_check")] != [
        "ok"
    ] or connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("broker SQLite integrity or foreign keys failed")
    rows = connection.execute("SELECT version,identity FROM metadata").fetchall()
    if (
        len(rows) != 1
        or rows[0][0] not in (1, 2, 3)
        or (identity is not None and rows[0][1] != identity)
    ):
        raise ValueError("broker schema or identity mismatch")
    migration = connection.execute(
        "SELECT checksum FROM schema_migrations WHERE version=1"
    ).fetchone()
    if migration is None or migration[0] != digest(SCHEMA_V1):
        raise ValueError("broker migration checksum mismatch")
    if rows[0][0] >= 2:
        second = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version=2"
        ).fetchone()
        if second is None or second[0] != digest(MIGRATION_2):
            raise ValueError("broker migration checksum mismatch")
    if rows[0][0] == 3:
        third = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version=3"
        ).fetchone()
        if third is None or third[0] != digest(MIGRATION_3):
            raise ValueError("broker migration checksum mismatch")
    required = {
        "users",
        "sessions",
        "csrf_tokens",
        "apps",
        "configurations",
        "intents",
        "audit",
        "observations",
        "quotas",
    }
    if rows[0][0] == 3:
        required |= {
            "local_accounts",
            "login_backoff",
            "token_policy",
            "account_tokens",
            "used_tokens",
            "enrollments",
            "admin_audit",
            "staff_read_audit",
            "staff_read_state",
        }
    if not required <= {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        raise ValueError("broker schema is incomplete")
    return int(rows[0][0]), str(rows[0][1])
