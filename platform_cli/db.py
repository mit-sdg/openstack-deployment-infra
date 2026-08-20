"""SQLite schema and short, direct state operations for M1."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid as uuid_module
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PlatformConfig, platform_config_identity
from .runtime import ensure_private_directory, safe_summary
from .validation import (
    ValidationError,
    commit,
    env_key,
    oci_digest_pin,
    sha256_hex,
    slug,
    uuid,
)
from .validation import (
    health_path as validate_health_path,
)

BUSY_TIMEOUT_MS = 5_000
_MAX_REFS_BYTES = 16_384
_OPERATION_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_CLEANUP_STATE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NOMAD_JOB_SHA: re.Pattern[str] = re.compile(r'm1_candidate_job_sha256\s*=\s*"([0-9a-f]{64})"')
_ENVIRONMENT_OWNERS = {"platform", "staff", "postgres", "mongo", "s3"}
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|credential|private_key|user_data|cloud_init|env_value|source_contents?)(?:$|_)",
    re.IGNORECASE,
)


class DatabaseError(RuntimeError):
    """Base class for safe database failures."""


class MigrationError(DatabaseError):
    pass


class UnfinishedOperationError(DatabaseError):
    def __init__(self, scope: str, operation_id: str, kind: str) -> None:
        self.scope = scope
        self.operation_id = operation_id
        self.kind = kind
        super().__init__(
            f"scope {scope!r} already has unfinished {kind!r} operation {operation_id}"
        )


@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    """Stable identity that is cryptographically bound into the M1 marker."""

    project_id: str
    namespace: str
    config_identity: str


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        canonical = "\n-- statement --\n".join(statement.strip() for statement in self.statements)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    kind: str
    scope: str
    status: str
    phase: str
    started_at: str
    updated_at: str
    deadline_at: str
    refs: Mapping[str, Any]
    candidate_digest: str | None
    safe_error: str | None
    cleanup_state: str


@dataclass(frozen=True, slots=True)
class ImageSelection:
    role: str
    image_id: str
    display_name: str
    source_commit: str
    compatibility_hash: str
    selected_at: str


@dataclass(frozen=True, slots=True)
class Application:
    application_id: str
    slug: str
    repository_url: str | None
    config_path: str | None
    desired_running: bool
    url: str | None
    worker_server_id: str | None
    worker_server_name: str | None
    worker_port_id: str | None
    worker_port_name: str | None
    worker_flavor: str
    scheduler_cpu_mhz: int
    scheduler_memory_mib: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Deployment:
    application_id: str
    source_commit: str
    recipe_hash: str
    image_digest: str
    nomad_job: str
    nomad_job_sha256: str
    nomad_version: int
    health_path: str
    application_port: int
    build_log_path: str
    accepted_at: str
    last_healthy_at: str


@dataclass(frozen=True, slots=True)
class ManagedResource:
    application_id: str
    resource_type: str
    provider_id: str | None
    provider_name: str
    lifecycle_state: str
    postgres_connections: int | None
    measured_target_bytes: int | None
    s3_bytes: int | None
    s3_objects: int | None
    last_verified_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class EnvironmentKey:
    application_id: str
    key_name: str
    owner: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        (
            """
            CREATE TABLE image_selections (
                role TEXT PRIMARY KEY CHECK (role IN ('admin','ingress','storage','worker','builder')),
                image_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                source_commit TEXT NOT NULL,
                compatibility_hash TEXT NOT NULL,
                selected_at TEXT NOT NULL
            ) STRICT
            """,
            """
            CREATE TABLE applications (
                application_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                repository_url TEXT,
                config_path TEXT,
                desired_running INTEGER NOT NULL CHECK (desired_running IN (0,1)),
                url TEXT,
                worker_server_id TEXT,
                worker_server_name TEXT,
                worker_port_id TEXT,
                worker_port_name TEXT,
                worker_flavor TEXT NOT NULL,
                scheduler_cpu_mhz INTEGER NOT NULL CHECK (scheduler_cpu_mhz > 0),
                scheduler_memory_mib INTEGER NOT NULL CHECK (scheduler_memory_mib > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT
            """,
        ),
    ),
    Migration(
        2,
        (
            """
            CREATE TABLE deployments (
                application_id TEXT PRIMARY KEY REFERENCES applications(application_id) ON DELETE CASCADE,
                source_commit TEXT NOT NULL,
                recipe_hash TEXT NOT NULL,
                image_digest TEXT NOT NULL,
                nomad_job TEXT NOT NULL,
                nomad_version INTEGER NOT NULL CHECK (nomad_version >= 0),
                build_log_path TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                last_healthy_at TEXT NOT NULL,
                nomad_job_sha256 TEXT NOT NULL,
                health_path TEXT NOT NULL,
                application_port INTEGER NOT NULL CHECK (application_port BETWEEN 1 AND 65535)
            ) STRICT
            """,
        ),
    ),
    Migration(
        3,
        (
            """
            CREATE TABLE managed_resources (
                application_id TEXT NOT NULL REFERENCES applications(application_id) ON DELETE CASCADE,
                resource_type TEXT NOT NULL CHECK (resource_type IN ('postgres','mongo','s3')),
                provider_id TEXT,
                provider_name TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('creating','active','removing','recovery_required')),
                postgres_connections INTEGER CHECK (postgres_connections > 0),
                measured_target_bytes INTEGER CHECK (measured_target_bytes > 0),
                s3_bytes INTEGER CHECK (s3_bytes > 0),
                s3_objects INTEGER CHECK (s3_objects > 0),
                last_verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (application_id, resource_type),
                CHECK (resource_type = 'postgres' OR postgres_connections IS NULL),
                CHECK (resource_type IN ('postgres','mongo') OR measured_target_bytes IS NULL),
                CHECK (resource_type = 's3' OR (s3_bytes IS NULL AND s3_objects IS NULL))
            ) STRICT
            """,
            """
            CREATE TABLE environment_keys (
                application_id TEXT NOT NULL REFERENCES applications(application_id) ON DELETE CASCADE,
                key_name TEXT NOT NULL,
                owner TEXT NOT NULL CHECK (owner IN ('platform','staff','postgres','mongo','s3')),
                PRIMARY KEY (application_id, key_name)
            ) STRICT
            """,
        ),
    ),
    Migration(
        4,
        (
            """
            CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','recovery_required')),
                phase TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                refs_json TEXT NOT NULL CHECK (json_valid(refs_json) AND json_type(refs_json) = 'object'),
                candidate_digest TEXT,
                safe_error TEXT,
                cleanup_state TEXT NOT NULL,
                CHECK (length(refs_json) <= 16384),
                CHECK (safe_error IS NULL OR length(safe_error) <= 1024)
            ) STRICT
            """,
            """
            CREATE UNIQUE INDEX one_unfinished_operation_per_scope
            ON operations(scope)
            WHERE status IN ('running','recovery_required')
            """,
            "CREATE INDEX operations_updated_at ON operations(updated_at)",
        ),
    ),
)

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT
"""

# Version zero is a deliberately non-migration marker.  It records that the
# file was created by this greenfield control plane rather than merely being a
# SQLite file that happens to contain tables with familiar names.  Keeping the
# marker in schema_migrations avoids adding another mutable metadata table and
# leaves the append-only migration numbering available for future releases.
_GREENFIELD_MARKER_VERSION = 0
_GREENFIELD_MARKER_CHECKSUM = hashlib.sha256(b"openstack-platform:m1:greenfield").hexdigest()
_DEPLOYMENT_MARKER_PREFIX = b"openstack-platform:m1:deployment:"
_NAMESPACE = re.compile(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]")
_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEMA_OBJECT_RE = re.compile(
    r"\bCREATE\s+(?:(UNIQUE)\s+)?(TABLE|INDEX|VIEW|TRIGGER)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _schema_objects(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT name, type
          FROM sqlite_master
         WHERE name NOT LIKE 'sqlite_%'
           AND type IN ('table', 'index', 'view', 'trigger')
        """
    ).fetchall()
    objects: dict[str, str] = {}
    for row in rows:
        name = row["name"]
        kind = row["type"]
        if not isinstance(name, str) or not isinstance(kind, str):
            raise MigrationError("SQLite schema metadata is malformed")
        objects[name] = kind
    return objects


def _expected_schema_objects(target_version: int | None = None) -> dict[str, str]:
    expected = {"schema_migrations": "table"}
    for migration in MIGRATIONS:
        if target_version is not None and migration.version > target_version:
            break
        for statement in migration.statements:
            for match in _SCHEMA_OBJECT_RE.finditer(statement):
                _unique, kind, name = match.groups()
                if not _SCHEMA_NAME.fullmatch(name):
                    raise MigrationError("migration schema object name is malformed")
                normalized_kind = kind.lower()
                prior = expected.get(name)
                if prior is not None and prior != normalized_kind:
                    raise MigrationError("migration schema object kind changed")
                expected[name] = normalized_kind
    return expected


def _validate_schema_objects(
    connection: sqlite3.Connection,
    *,
    target_version: int | None = None,
    require_complete: bool = True,
) -> None:
    objects = _schema_objects(connection)
    expected = _expected_schema_objects(target_version)
    unknown = sorted(set(objects) - set(expected))
    if unknown:
        raise MigrationError(
            "SQLite database contains unknown or retained external schema: " + ", ".join(unknown)
        )
    wrong_kind = sorted(name for name, kind in objects.items() if expected.get(name) != kind)
    if wrong_kind:
        raise MigrationError("SQLite database contains an unexpected schema object kind")
    if require_complete:
        missing = sorted(set(expected) - set(objects))
        if missing:
            raise MigrationError(
                "SQLite database is missing expected schema objects: " + ", ".join(missing)
            )


def _validated_deployment_identity(identity: DeploymentIdentity) -> DeploymentIdentity:
    if not isinstance(identity, DeploymentIdentity):
        raise ValidationError("SQLite deployment identity is malformed")
    if not isinstance(identity.namespace, str) or not _NAMESPACE.fullmatch(identity.namespace):
        raise ValidationError("deployment namespace is malformed")
    return DeploymentIdentity(
        project_id=uuid(identity.project_id, field="deployment project UUID"),
        namespace=identity.namespace,
        config_identity=sha256_hex(identity.config_identity, field="deployment config identity"),
    )


def deployment_identity(platform: PlatformConfig) -> DeploymentIdentity:
    """Build the marker identity from authenticated-config inputs.

    ``project_id`` is the value subsequently checked against the authenticated
    OpenStack token by provider mutations.  The remaining projection is stable
    across image/flavor and release upgrades but changes when the deployment's
    namespace, resource inventory, or state paths change.
    """
    return _validated_deployment_identity(
        DeploymentIdentity(
            project_id=platform.project_id,
            namespace=platform.namespace,
            config_identity=platform_config_identity(platform),
        )
    )


def _deployment_marker_checksum(identity: DeploymentIdentity) -> str:
    checked = _validated_deployment_identity(identity)
    encoded = json.dumps(
        {
            "projectId": checked.project_id,
            "namespace": checked.namespace,
            "configIdentity": checked.config_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(_DEPLOYMENT_MARKER_PREFIX + encoded).hexdigest()


def _validate_greenfield_marker(
    connection: sqlite3.Connection, *, identity: DeploymentIdentity | None = None
) -> None:
    objects = _schema_objects(connection)
    if not objects:
        return
    if objects.get("schema_migrations") != "table":
        raise MigrationError("SQLite database is not an explicitly marked greenfield database")
    try:
        rows = connection.execute(
            "SELECT version, checksum FROM schema_migrations WHERE version = ?",
            (_GREENFIELD_MARKER_VERSION,),
        ).fetchall()
    except sqlite3.DatabaseError:
        raise MigrationError(
            "SQLite database is not an explicitly marked greenfield database"
        ) from None
    if len(rows) != 1 or not isinstance(rows[0]["checksum"], str):
        raise MigrationError("SQLite database is not an explicitly marked greenfield database")
    observed = rows[0]["checksum"]
    if not re.fullmatch(r"[0-9a-f]{64}", observed):
        raise MigrationError("SQLite deployment identity marker is malformed")
    if identity is not None:
        expected = _deployment_marker_checksum(identity)
        if observed != expected:
            raise MigrationError("SQLite database belongs to a different deployment identity")
    # Low-level callers that do not have the platform inventory can still
    # inspect a bound database. The CLI and restore paths always pass an
    # identity and perform the cross-deployment check above.
    _validate_schema_objects(connection, require_complete=False)


def _initialize_greenfield_schema(
    connection: sqlite3.Connection, *, identity: DeploymentIdentity | None = None
) -> None:
    marker = (
        _GREENFIELD_MARKER_CHECKSUM if identity is None else _deployment_marker_checksum(identity)
    )
    with transaction(connection):
        connection.execute(_BOOTSTRAP)
        connection.execute(
            "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
            (_GREENFIELD_MARKER_VERSION, marker, utc_now()),
        )


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _nomad_job_sha256(nomad_job: str) -> str:
    markers: list[str] = _NOMAD_JOB_SHA.findall(nomad_job)
    if len(markers) == 1:
        return markers[0]
    return hashlib.sha256(nomad_job.encode()).hexdigest()


def _validate_private_file(path: Path) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DatabaseError("database must be a direct regular file")
    if metadata.st_uid != os.geteuid():
        raise DatabaseError("database must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DatabaseError("database permissions must be 0600")


def connect(
    path: str | Path,
    *,
    create: bool = True,
    identity: DeploymentIdentity | None = None,
) -> sqlite3.Connection:
    """Open a private SQLite database with the required connection settings.

    The caller applies migrations separately, normally while holding the
    ``database-maintenance`` file lock. Production callers pass the current
    deployment identity so copied state is rejected before WAL sidecars exist.
    """
    database_path = Path(path)
    ensure_private_directory(database_path.parent, create=create)
    existed = os.path.lexists(database_path)
    if existed:
        _validate_private_file(database_path)
    elif not create:
        raise FileNotFoundError(database_path)
    else:
        descriptor = os.open(
            database_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)

    try:
        connection = sqlite3.connect(
            database_path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
    except BaseException:
        if not existed:
            database_path.unlink(missing_ok=True)
        raise
    connection.row_factory = sqlite3.Row
    _validate_private_file(database_path)
    try:
        # A fresh file has no schema objects and is initialized by migrate().
        # Any non-empty file must already carry the explicit greenfield marker;
        # otherwise connect() would silently retain or adopt an external schema.
        # Validate before enabling WAL so rejecting an external file does not
        # leave SQLite sidecar state behind.
        _validate_greenfield_marker(connection, identity=identity)
    except BaseException:
        connection.close()
        raise
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


@contextmanager
def transaction(
    connection: sqlite3.Connection, *, immediate: bool = True
) -> Iterator[sqlite3.Connection]:
    """Run a short local transaction; nested transactions are refused."""
    if connection.in_transaction:
        raise DatabaseError("nested or long-lived database transactions are not supported")
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def migrate(
    connection: sqlite3.Connection,
    *,
    target_version: int | None = None,
    identity: DeploymentIdentity | None = None,
) -> None:
    """Apply append-only numbered migrations and verify prior checksums."""
    latest = MIGRATIONS[-1].version
    target = latest if target_version is None else target_version
    if target < 0 or target > latest:
        raise MigrationError(f"unsupported migration target {target}")

    if not _schema_objects(connection):
        _initialize_greenfield_schema(connection, identity=identity)
    else:
        _validate_greenfield_marker(connection, identity=identity)

    applied_rows = connection.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    known = {migration.version: migration for migration in MIGRATIONS}
    marker_seen = False
    migration_versions: list[int] = []
    for row in applied_rows:
        version = row["version"]
        if version == _GREENFIELD_MARKER_VERSION:
            if marker_seen:
                raise MigrationError("greenfield database marker is invalid")
            if identity is not None and row["checksum"] != _deployment_marker_checksum(identity):
                raise MigrationError("greenfield database marker is invalid")
            if identity is None and (
                not isinstance(row["checksum"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["checksum"])
            ):
                raise MigrationError("greenfield database marker is invalid")
            marker_seen = True
            continue
        migration = known.get(version)
        if migration is None:
            raise MigrationError(f"database has unknown future migration {version}")
        if row["checksum"] != migration.checksum:
            raise MigrationError(f"migration {version} checksum does not match")
        migration_versions.append(version)
    if not marker_seen:
        raise MigrationError("SQLite database is not an explicitly marked greenfield database")
    if migration_versions != list(range(1, max(migration_versions, default=0) + 1)):
        raise MigrationError("SQLite migration history is not contiguous")
    current_version = max(migration_versions, default=0)
    if target < current_version:
        raise MigrationError("migration target would downgrade the database")

    applied = set(migration_versions)
    for migration in MIGRATIONS:
        if migration.version > target or migration.version in applied:
            continue
        with transaction(connection):
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.checksum, utc_now()),
            )
        _validate_schema_objects(
            connection,
            target_version=migration.version,
            require_complete=True,
        )

    _validate_schema_objects(connection, target_version=target, require_complete=True)


def validate_complete_schema(
    connection: sqlite3.Connection, *, identity: DeploymentIdentity | None = None
) -> None:
    """Require the fully migrated, explicitly marked M1 schema.

    A migration row is not sufficient evidence on its own: a damaged backup
    can retain the row while losing one of the tables or indexes created by
    that migration.  Restore validation calls this after applying migrations,
    before the candidate can replace the live database.
    """
    _validate_greenfield_marker(connection, identity=identity)
    latest = MIGRATIONS[-1].version
    applied_rows = connection.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    expected_versions = [_GREENFIELD_MARKER_VERSION, *range(1, latest + 1)]
    observed_versions = [int(row["version"]) for row in applied_rows]
    if observed_versions != expected_versions:
        raise MigrationError("SQLite migration history is not the complete M1 history")
    for row in applied_rows:
        if row["version"] == _GREENFIELD_MARKER_VERSION:
            continue
        migration = MIGRATIONS[row["version"] - 1]
        if row["checksum"] != migration.checksum:
            raise MigrationError(f"migration {row['version']} checksum does not match")
    _validate_schema_objects(connection, target_version=latest, require_complete=True)


def schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("SELECT max(version) AS version FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["version"] or 0)


def _walk_refs(value: Any, *, key: str | None = None) -> None:
    if key is not None and _SECRET_KEY.search(key):
        raise ValidationError(f"operation refs key {key!r} may contain secret material")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _walk_refs(item)
        return
    if isinstance(value, dict):
        for nested_key, item in value.items():
            if not isinstance(nested_key, str):
                raise ValidationError("operation refs keys must be strings")
            _walk_refs(item, key=nested_key)
        return
    raise ValidationError("operation refs must contain only JSON values")


def _refs_json(refs: Mapping[str, Any]) -> str:
    plain = dict(refs)
    _walk_refs(plain)
    encoded = json.dumps(plain, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode()) > _MAX_REFS_BYTES:
        raise ValidationError("operation refs exceed their 16384-byte limit")
    return encoded


def _operation_token(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _OPERATION_TOKEN.fullmatch(value):
        raise ValidationError(f"operation {field} is malformed")
    return value


def _cleanup_state(value: str) -> str:
    if not isinstance(value, str) or not _CLEANUP_STATE.fullmatch(value):
        raise ValidationError("operation cleanup state is malformed")
    return value


def _operation(row: sqlite3.Row | None) -> Operation | None:
    if row is None:
        return None
    return Operation(
        operation_id=row["operation_id"],
        kind=row["kind"],
        scope=row["scope"],
        status=row["status"],
        phase=row["phase"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        deadline_at=row["deadline_at"],
        refs=json.loads(row["refs_json"]),
        candidate_digest=row["candidate_digest"],
        safe_error=row["safe_error"],
        cleanup_state=row["cleanup_state"],
    )


def get_operation(connection: sqlite3.Connection, operation_id: str) -> Operation | None:
    return _operation(
        connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
    )


def get_unfinished_operation(connection: sqlite3.Connection, scope: str) -> Operation | None:
    return _operation(
        connection.execute(
            "SELECT * FROM operations WHERE scope = ? AND status IN ('running','recovery_required')",
            (scope,),
        ).fetchone()
    )


def begin_operation(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    kind: str,
    scope: str,
    phase: str,
    deadline_at: str,
    refs: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> Operation:
    """Record mutation intent in one short transaction."""
    operation_id = uuid(operation_id, field="operation_id")
    kind = _operation_token(kind, field="kind")
    scope = _operation_token(scope, field="scope")
    phase = _operation_token(phase, field="phase")
    if not isinstance(deadline_at, str) or len(deadline_at) > 64:
        raise ValidationError("operation deadline is malformed")
    timestamp = now or utc_now()
    with transaction(connection):
        existing = get_unfinished_operation(connection, scope)
        if existing is not None:
            raise UnfinishedOperationError(scope, existing.operation_id, existing.kind)
        connection.execute(
            """
            INSERT INTO operations(
                operation_id, kind, scope, status, phase, started_at, updated_at,
                deadline_at, refs_json, candidate_digest, safe_error, cleanup_state
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, NULL, NULL, 'pending')
            """,
            (
                operation_id,
                kind,
                scope,
                phase,
                timestamp,
                timestamp,
                deadline_at,
                _refs_json(refs or {}),
            ),
        )
    result = get_operation(connection, operation_id)
    assert result is not None
    return result


def renew_operation_deadline(
    connection: sqlite3.Connection,
    operation_id: str,
    deadline_at: str,
    *,
    now: str | None = None,
) -> Operation:
    """Give a resumed operation the deadline of the attempt that resumes it.

    The whole-operation deadline bounds one attempt, so that a build cannot
    outlive the record it is written against. A resumed operation kept the
    deadline of the attempt that stranded it, which is normally already spent:
    every recovery then failed immediately, and the scope stayed locked with no
    command able to release it. Recovery is a new attempt and takes a new
    deadline, still recorded before any work so the durable record and the
    helper continue to agree.
    """
    uuid(operation_id, field="operation_id")
    if not isinstance(deadline_at, str) or len(deadline_at) > 64:
        raise ValidationError("operation deadline is malformed")
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE operations SET deadline_at = ?, updated_at = ? "
            "WHERE operation_id = ? AND status IN ('running','recovery_required')",
            (deadline_at, now or utc_now(), operation_id),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("operation is missing or already terminal")
    result = get_operation(connection, operation_id)
    assert result is not None
    return result


def checkpoint_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    phase: str,
    refs: Mapping[str, Any] | None = None,
    merge_refs: bool = False,
    candidate_digest: str | None = None,
    cleanup_state: str | None = None,
    now: str | None = None,
) -> Operation:
    """Checkpoint non-secret external identities after one visible mutation.

    ``merge_refs`` is used when a provider exception contains only the identity
    of its most recent mutation.  It preserves the earlier rollback snapshot
    instead of replacing it with that deliberately small local projection.
    """
    uuid(operation_id, field="operation_id")
    phase = _operation_token(phase, field="phase")
    if candidate_digest is not None:
        oci_digest_pin(candidate_digest, field="candidate_digest")
    if refs is not None:
        _refs_json(refs)
    if cleanup_state is not None:
        _cleanup_state(cleanup_state)
    with transaction(connection):
        encoded_refs: str | None = None
        if refs is not None:
            merged_refs = dict(refs)
            if merge_refs:
                row = connection.execute(
                    "SELECT refs_json FROM operations WHERE operation_id = ? AND status IN ('running','recovery_required')",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseError("operation is missing or already terminal")
                merged_refs = {**json.loads(row["refs_json"]), **merged_refs}
            encoded_refs = _refs_json(merged_refs)
        assignments = ["phase = ?", "updated_at = ?"]
        values: list[Any] = [phase, now or utc_now()]
        if encoded_refs is not None:
            assignments.append("refs_json = ?")
            values.append(encoded_refs)
        if candidate_digest is not None:
            assignments.append("candidate_digest = ?")
            values.append(candidate_digest)
        if cleanup_state is not None:
            assignments.append("cleanup_state = ?")
            values.append(cleanup_state)
        values.append(operation_id)
        cursor = connection.execute(
            f"UPDATE operations SET {', '.join(assignments)} WHERE operation_id = ? AND status IN ('running','recovery_required')",
            values,
        )
        if cursor.rowcount != 1:
            raise DatabaseError("operation is missing or already terminal")
    result = get_operation(connection, operation_id)
    assert result is not None
    return result


def unfinished_operation_image_ids(
    connection: sqlite3.Connection, *, exclude_operation_id: str | None = None
) -> tuple[str, ...]:
    """Return every image UUID referenced by an unfinished operation."""
    if exclude_operation_id is not None:
        uuid(exclude_operation_id, field="operation_id")
    rows = connection.execute(
        "SELECT operation_id, refs_json FROM operations WHERE status IN ('running','recovery_required')"
    )
    found: set[str] = set()

    def collect(value: Any, key: str | None = None) -> None:
        image_key = key is not None and (
            key == "image_id" or key.endswith("_image_id") or key.endswith("_image_ids")
        )
        if image_key and isinstance(value, str):
            found.add(uuid(value, field="operation image UUID"))
            return
        if image_key and isinstance(value, list):
            for item in value:
                found.add(uuid(item, field="operation image UUID"))
            return
        if isinstance(value, dict):
            for nested_key, item in value.items():
                collect(item, nested_key)
        elif isinstance(value, list):
            for item in value:
                collect(item, key)

    for row in rows:
        if row["operation_id"] != exclude_operation_id:
            collect(json.loads(row["refs_json"]))
    return tuple(sorted(found))


def mark_recovery_required(
    connection: sqlite3.Connection,
    operation_id: str,
    error: BaseException | str,
    *,
    phase: str | None = None,
    now: str | None = None,
) -> Operation:
    return _finish_operation(
        connection,
        operation_id,
        status="recovery_required",
        error=error,
        phase=phase,
        cleanup_state=None,
        now=now,
    )


def mark_succeeded(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    cleanup_state: str = "confirmed",
    now: str | None = None,
) -> Operation:
    return _finish_operation(
        connection,
        operation_id,
        status="succeeded",
        error=None,
        phase=None,
        cleanup_state=cleanup_state,
        now=now,
    )


def mark_failed(
    connection: sqlite3.Connection,
    operation_id: str,
    error: BaseException | str,
    *,
    cleanup_state: str,
    now: str | None = None,
) -> Operation:
    if cleanup_state not in {"confirmed", "not_required"}:
        raise ValidationError("failed operations require confirmed or not_required cleanup")
    return _finish_operation(
        connection,
        operation_id,
        status="failed",
        error=error,
        phase=None,
        cleanup_state=cleanup_state,
        now=now,
    )


def _finish_operation(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    status: str,
    error: BaseException | str | None,
    phase: str | None,
    cleanup_state: str | None,
    now: str | None,
) -> Operation:
    uuid(operation_id, field="operation_id")
    assignments = ["status = ?", "updated_at = ?", "safe_error = ?"]
    values: list[Any] = [status, now or utc_now(), None if error is None else safe_summary(error)]
    if phase is not None:
        assignments.append("phase = ?")
        values.append(_operation_token(phase, field="phase"))
    if cleanup_state is not None:
        assignments.append("cleanup_state = ?")
        values.append(_cleanup_state(cleanup_state))
    values.append(operation_id)
    with transaction(connection):
        cursor = connection.execute(
            f"UPDATE operations SET {', '.join(assignments)} WHERE operation_id = ? AND status IN ('running','recovery_required')",
            values,
        )
        if cursor.rowcount != 1:
            raise DatabaseError("operation is missing or already terminal")
    result = get_operation(connection, operation_id)
    assert result is not None
    return result


def put_image_selection(
    connection: sqlite3.Connection,
    *,
    role: str,
    image_id: str,
    display_name: str,
    source_commit: str,
    compatibility_hash: str,
    selected_at: str | None = None,
) -> None:
    uuid(image_id, field="image_id")
    commit(source_commit)
    sha256_hex(compatibility_hash, field="compatibility_hash")
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO image_selections VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(role) DO UPDATE SET
              image_id=excluded.image_id, display_name=excluded.display_name,
              source_commit=excluded.source_commit, compatibility_hash=excluded.compatibility_hash,
              selected_at=excluded.selected_at
            """,
            (
                role,
                image_id,
                display_name,
                source_commit,
                compatibility_hash,
                selected_at or utc_now(),
            ),
        )


def get_image_selection(connection: sqlite3.Connection, role: str) -> ImageSelection | None:
    row = connection.execute("SELECT * FROM image_selections WHERE role = ?", (role,)).fetchone()
    if row is None:
        return None
    return ImageSelection(
        role=row["role"],
        image_id=row["image_id"],
        display_name=row["display_name"],
        source_commit=row["source_commit"],
        compatibility_hash=row["compatibility_hash"],
        selected_at=row["selected_at"],
    )


def list_image_selections(connection: sqlite3.Connection) -> list[ImageSelection]:
    selections: list[ImageSelection] = []
    for row in connection.execute("SELECT role FROM image_selections ORDER BY role"):
        selection = get_image_selection(connection, row["role"])
        assert selection is not None
        selections.append(selection)
    return selections


def put_application(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    application_slug: str,
    worker_flavor: str,
    scheduler_cpu_mhz: int,
    scheduler_memory_mib: int,
    repository_url: str | None = None,
    config_path: str | None = None,
    desired_running: bool = True,
    url: str | None = None,
    worker_server_id: str | None = None,
    worker_server_name: str | None = None,
    worker_port_id: str | None = None,
    worker_port_name: str | None = None,
    now: str | None = None,
) -> None:
    application_id = uuid(application_id, field="application_id")
    application_slug = slug(application_slug)
    if worker_server_id is not None:
        uuid(worker_server_id, field="worker_server_id")
    if worker_port_id is not None:
        uuid(worker_port_id, field="worker_port_id")
    if scheduler_cpu_mhz <= 0 or scheduler_memory_mib <= 0 or not worker_flavor:
        raise ValidationError("application sizing must be positive and have a worker flavor")
    timestamp = now or utc_now()
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
              slug=excluded.slug, repository_url=excluded.repository_url,
              config_path=excluded.config_path, desired_running=excluded.desired_running,
              url=excluded.url, worker_server_id=excluded.worker_server_id,
              worker_server_name=excluded.worker_server_name, worker_port_id=excluded.worker_port_id,
              worker_port_name=excluded.worker_port_name, worker_flavor=excluded.worker_flavor,
              scheduler_cpu_mhz=excluded.scheduler_cpu_mhz,
              scheduler_memory_mib=excluded.scheduler_memory_mib, updated_at=excluded.updated_at
            """,
            (
                application_id,
                application_slug,
                repository_url,
                config_path,
                int(desired_running),
                url,
                worker_server_id,
                worker_server_name,
                worker_port_id,
                worker_port_name,
                worker_flavor,
                scheduler_cpu_mhz,
                scheduler_memory_mib,
                timestamp,
                timestamp,
            ),
        )


def _application(row: sqlite3.Row | None) -> Application | None:
    if row is None:
        return None
    return Application(
        application_id=row["application_id"],
        slug=row["slug"],
        repository_url=row["repository_url"],
        config_path=row["config_path"],
        desired_running=bool(row["desired_running"]),
        url=row["url"],
        worker_server_id=row["worker_server_id"],
        worker_server_name=row["worker_server_name"],
        worker_port_id=row["worker_port_id"],
        worker_port_name=row["worker_port_name"],
        worker_flavor=row["worker_flavor"],
        scheduler_cpu_mhz=row["scheduler_cpu_mhz"],
        scheduler_memory_mib=row["scheduler_memory_mib"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_application(connection: sqlite3.Connection, identifier: str) -> Application | None:
    return _application(
        connection.execute(
            "SELECT * FROM applications WHERE application_id = ? OR slug = ?",
            (identifier, identifier),
        ).fetchone()
    )


def list_applications(connection: sqlite3.Connection) -> list[Application]:
    applications: list[Application] = []
    for row in connection.execute("SELECT * FROM applications ORDER BY slug"):
        application = _application(row)
        assert application is not None
        applications.append(application)
    return applications


def get_deployment(connection: sqlite3.Connection, application_id: str) -> Deployment | None:
    row = connection.execute(
        "SELECT * FROM deployments WHERE application_id = ?", (application_id,)
    ).fetchone()
    if row is None:
        return None
    return Deployment(
        application_id=row["application_id"],
        source_commit=row["source_commit"],
        recipe_hash=row["recipe_hash"],
        image_digest=row["image_digest"],
        nomad_job=row["nomad_job"],
        nomad_job_sha256=row["nomad_job_sha256"],
        nomad_version=row["nomad_version"],
        health_path=row["health_path"],
        application_port=row["application_port"],
        build_log_path=row["build_log_path"],
        accepted_at=row["accepted_at"],
        last_healthy_at=row["last_healthy_at"],
    )


def list_application_manifest_images(
    connection: sqlite3.Connection,
    application_id: str,
) -> tuple[str, ...]:
    """Expose every registry manifest recorded for an application.

    The accepted deployment and all deploy-operation candidates are included so
    retention and removal never infer references from registry age or a single
    current row.
    """
    identifier = uuid(application_id, field="application_id")
    images: list[str] = []
    deployment = get_deployment(connection, identifier)
    if deployment is not None:
        images.append(oci_digest_pin(deployment.image_digest, field="deployment image"))
    rows = connection.execute(
        """
        SELECT candidate_digest
          FROM operations
         WHERE scope = ? AND kind = 'app.deploy' AND candidate_digest IS NOT NULL
         ORDER BY updated_at DESC, operation_id DESC
        """,
        (f"app-{identifier}",),
    )
    for row in rows:
        image = oci_digest_pin(row["candidate_digest"], field="operation candidate image")
        if image not in images:
            images.append(image)
    return tuple(images)


def list_application_successful_manifest_history(
    connection: sqlite3.Connection,
    application_id: str,
) -> tuple[str, ...]:
    """Return current then previously accepted manifests in recency order."""
    identifier = uuid(application_id, field="application_id")
    images: list[str] = []
    deployment = get_deployment(connection, identifier)
    if deployment is not None:
        images.append(oci_digest_pin(deployment.image_digest, field="deployment image"))
    rows = connection.execute(
        """
        SELECT candidate_digest
          FROM operations
         WHERE scope = ? AND kind = 'app.deploy' AND status = 'succeeded'
           AND candidate_digest IS NOT NULL
         ORDER BY updated_at DESC, operation_id DESC
        """,
        (f"app-{identifier}",),
    )
    for row in rows:
        image = oci_digest_pin(row["candidate_digest"], field="successful candidate image")
        if image not in images:
            images.append(image)
    return tuple(images)


def list_active_application_manifest_references(
    connection: sqlite3.Connection,
    application_id: str,
) -> tuple[str, ...]:
    """Return manifests with a current or unfinished runtime reference."""
    identifier = uuid(application_id, field="application_id")
    images: list[str] = []
    deployment = get_deployment(connection, identifier)
    if deployment is not None:
        images.append(oci_digest_pin(deployment.image_digest, field="deployment image"))
    rows = connection.execute(
        """
        SELECT candidate_digest
          FROM operations
         WHERE scope = ? AND kind = 'app.deploy'
           AND status IN ('running','recovery_required')
           AND candidate_digest IS NOT NULL
         ORDER BY updated_at DESC, operation_id DESC
        """,
        (f"app-{identifier}",),
    )
    for row in rows:
        image = oci_digest_pin(row["candidate_digest"], field="active candidate image")
        if image not in images:
            images.append(image)
    return tuple(images)


def accept_deployment(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    source_commit: str,
    recipe_hash: str,
    image_digest: str,
    nomad_job: str,
    nomad_version: int,
    build_log_path: str,
    nomad_job_sha256: str | None = None,
    health_path: str = "/",
    application_port: int = 8080,
    accepted_at: str | None = None,
    last_healthy_at: str | None = None,
) -> None:
    """Accept a complete M1 deployment with source and recipe evidence."""
    _accept_deployment(
        connection,
        application_id=application_id,
        source_commit=commit(source_commit),
        recipe_hash=sha256_hex(recipe_hash, field="recipe_hash"),
        image_digest=image_digest,
        nomad_job=nomad_job,
        nomad_version=nomad_version,
        build_log_path=build_log_path,
        nomad_job_sha256=nomad_job_sha256,
        health_path=health_path,
        application_port=application_port,
        accepted_at=accepted_at,
        last_healthy_at=last_healthy_at,
    )


def _accept_deployment(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    source_commit: str,
    recipe_hash: str,
    image_digest: str,
    nomad_job: str,
    nomad_version: int,
    build_log_path: str,
    nomad_job_sha256: str | None,
    health_path: str,
    application_port: int,
    accepted_at: str | None,
    last_healthy_at: str | None,
) -> None:
    application_id = uuid(application_id, field="application_id")
    oci_digest_pin(image_digest, field="image_digest")
    checked_job_sha256 = sha256_hex(
        nomad_job_sha256 or _nomad_job_sha256(nomad_job),
        field="nomad_job_sha256",
    )
    checked_health_path = validate_health_path(health_path)
    if (
        isinstance(application_port, bool)
        or not isinstance(application_port, int)
        or not 1 <= application_port <= 65_535
    ):
        raise ValidationError("application_port must be from 1 through 65535")
    timestamp = accepted_at or utc_now()
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO deployments(
              application_id, source_commit, recipe_hash,
              image_digest, nomad_job, nomad_version, build_log_path,
              accepted_at, last_healthy_at, nomad_job_sha256, health_path,
              application_port
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
              source_commit=excluded.source_commit, recipe_hash=excluded.recipe_hash,
              image_digest=excluded.image_digest, nomad_job=excluded.nomad_job,
              nomad_job_sha256=excluded.nomad_job_sha256,
              nomad_version=excluded.nomad_version, health_path=excluded.health_path,
              application_port=excluded.application_port,
              build_log_path=excluded.build_log_path,
              accepted_at=excluded.accepted_at, last_healthy_at=excluded.last_healthy_at
            """,
            (
                application_id,
                source_commit,
                recipe_hash,
                image_digest,
                nomad_job,
                nomad_version,
                build_log_path,
                timestamp,
                last_healthy_at or timestamp,
                checked_job_sha256,
                checked_health_path,
                application_port,
            ),
        )


def put_managed_resource(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    resource_type: str,
    provider_name: str,
    lifecycle_state: str,
    provider_id: str | None = None,
    postgres_connections: int | None = None,
    measured_target_bytes: int | None = None,
    s3_bytes: int | None = None,
    s3_objects: int | None = None,
    last_verified_at: str | None = None,
    now: str | None = None,
) -> None:
    application_id = uuid(application_id, field="application_id")
    timestamp = now or utc_now()
    with transaction(connection):
        connection.execute(
            """
            INSERT INTO managed_resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id, resource_type) DO UPDATE SET
              provider_id=excluded.provider_id, provider_name=excluded.provider_name,
              lifecycle_state=excluded.lifecycle_state,
              postgres_connections=excluded.postgres_connections,
              measured_target_bytes=excluded.measured_target_bytes,
              s3_bytes=excluded.s3_bytes, s3_objects=excluded.s3_objects,
              last_verified_at=excluded.last_verified_at, updated_at=excluded.updated_at
            """,
            (
                application_id,
                resource_type,
                provider_id,
                provider_name,
                lifecycle_state,
                postgres_connections,
                measured_target_bytes,
                s3_bytes,
                s3_objects,
                last_verified_at,
                timestamp,
                timestamp,
            ),
        )


def list_managed_resources(
    connection: sqlite3.Connection,
    *,
    application_id: str | None = None,
) -> list[ManagedResource]:
    if application_id is None:
        rows = connection.execute(
            "SELECT * FROM managed_resources ORDER BY application_id, resource_type"
        )
    else:
        rows = connection.execute(
            "SELECT * FROM managed_resources WHERE application_id = ? ORDER BY resource_type",
            (application_id,),
        )
    return [
        ManagedResource(
            application_id=row["application_id"],
            resource_type=row["resource_type"],
            provider_id=row["provider_id"],
            provider_name=row["provider_name"],
            lifecycle_state=row["lifecycle_state"],
            postgres_connections=row["postgres_connections"],
            measured_target_bytes=row["measured_target_bytes"],
            s3_bytes=row["s3_bytes"],
            s3_objects=row["s3_objects"],
            last_verified_at=row["last_verified_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


def delete_managed_resource(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    resource_type: str,
) -> None:
    uuid(application_id, field="application_id")
    with transaction(connection):
        connection.execute(
            "DELETE FROM managed_resources WHERE application_id = ? AND resource_type = ?",
            (application_id, resource_type),
        )


def set_environment_keys(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    owner: str,
    keys: Sequence[str],
) -> None:
    """Replace names for one owner without touching another owner's names."""
    application_id = uuid(application_id, field="application_id")
    if owner not in _ENVIRONMENT_OWNERS:
        raise ValidationError(f"unknown environment key owner {owner!r}")
    validated = sorted({env_key(key) for key in keys})
    with transaction(connection):
        connection.execute(
            "DELETE FROM environment_keys WHERE application_id = ? AND owner = ?",
            (application_id, owner),
        )
        connection.executemany(
            "INSERT INTO environment_keys(application_id, key_name, owner) VALUES (?, ?, ?)",
            ((application_id, key, owner) for key in validated),
        )


def list_environment_keys(
    connection: sqlite3.Connection,
    *,
    application_id: str | None = None,
) -> list[EnvironmentKey]:
    if application_id is None:
        rows = connection.execute(
            "SELECT * FROM environment_keys ORDER BY application_id, key_name"
        )
    else:
        rows = connection.execute(
            "SELECT * FROM environment_keys WHERE application_id = ? ORDER BY key_name",
            (application_id,),
        )
    return [
        EnvironmentKey(
            application_id=row["application_id"],
            key_name=row["key_name"],
            owner=row["owner"],
        )
        for row in rows
    ]


def delete_application(connection: sqlite3.Connection, application_id: str) -> None:
    """Delete accepted app state after feature code has confirmed owned resources absent."""
    uuid(application_id, field="application_id")
    with transaction(connection):
        connection.execute("DELETE FROM applications WHERE application_id = ?", (application_id,))


def complete_application_removal(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    operation_id: str,
    now: str | None = None,
) -> None:
    """Atomically remove accepted app state and finish its verified remove operation."""
    uuid(application_id, field="application_id")
    uuid(operation_id, field="operation_id")
    timestamp = now or utc_now()
    with transaction(connection):
        operation = get_operation(connection, operation_id)
        if (
            operation is None
            or operation.kind != "app.remove"
            or operation.status not in {"running", "recovery_required"}
            or operation.refs.get("application_id") != application_id
            or operation.phase != "manifest_absent"
        ):
            raise DatabaseError("application removal operation is not ready to complete")
        managed = connection.execute(
            "SELECT 1 FROM managed_resources WHERE application_id = ? LIMIT 1",
            (application_id,),
        ).fetchone()
        if managed is not None:
            raise DatabaseError("application removal refuses managed resource rows")
        connection.execute("DELETE FROM applications WHERE application_id = ?", (application_id,))
        cursor = connection.execute(
            """
            UPDATE operations
               SET status = 'succeeded', updated_at = ?, safe_error = NULL,
                   cleanup_state = 'confirmed'
             WHERE operation_id = ? AND status IN ('running','recovery_required')
            """,
            (timestamp, operation_id),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("application removal operation could not be completed")


def backup_database(connection: sqlite3.Connection, destination: str | Path) -> Path:
    """Create an atomic mode-0600 backup using SQLite's online backup API."""
    destination_path = Path(destination)
    ensure_private_directory(destination_path.parent, create=True)
    if destination_path.exists() or destination_path.is_symlink():
        raise DatabaseError("backup destination already exists")
    temporary = destination_path.with_name(
        f".{destination_path.name}.{uuid_module.uuid4().hex}.tmp"
    )
    target: sqlite3.Connection | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)
        target = sqlite3.connect(temporary)
        connection.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise DatabaseError("SQLite backup integrity check failed")
        target.close()
        target = None
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination_path)
        directory_fd = os.open(destination_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if target is not None:
            target.close()
        temporary.unlink(missing_ok=True)
    return destination_path
