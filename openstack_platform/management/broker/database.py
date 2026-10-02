"""Private SQLite management state; no credential values are persisted."""

from __future__ import annotations

import fcntl
import os
import sqlite3
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
SCHEMA_VERSION = 2


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
            if row is None or row["version"] not in (1, 2) or row["identity"] != identity:
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
