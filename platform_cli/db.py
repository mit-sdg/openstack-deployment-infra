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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deployment_config import DeploymentConfiguration

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
    resource_name as validate_resource_name,
)

BUSY_TIMEOUT_MS = 5_000
_MAX_REFS_BYTES = 16_384
_OPERATION_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_CLEANUP_STATE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_REQUESTED_REF = re.compile(r"[^\x00-\x20\x7f]{1,256}")
_DEPLOYMENT_STATUSES = {
    "queued",
    "building",
    "deploying",
    "succeeded",
    "failed",
    "recovery_required",
}
_NOMAD_JOB_SHA: re.Pattern[str] = re.compile(r'm1_candidate_job_sha256\s*=\s*"([0-9a-f]{64})"')
_ENVIRONMENT_OWNER = re.compile(
    r"(?:platform|staff|storage\.(?:postgres|mongo|s3)\.[a-z][a-z0-9-]{0,39})"
)
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|credential|private_key|user_data|cloud_init|env_value|source_contents?)(?:$|_)",
    re.IGNORECASE,
)


class DatabaseError(RuntimeError):
    """Base class for safe database failures."""


class MigrationError(DatabaseError):
    pass


class IdempotencyConflictError(DatabaseError):
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
class DeploymentAttempt:
    deployment_id: str
    application_id: str
    status: str
    snapshot_kind: str
    source_commit: str
    requested_ref: str | None
    configuration_revision: int | None
    configuration: DeploymentConfiguration | None
    configuration_sha256: str | None
    environment_revision: int | None
    idempotency_request_id: str | None
    recipe_hash: str | None
    image_digest: str | None
    nomad_job: str | None
    nomad_job_sha256: str | None
    nomad_version: int | None
    health_path: str | None
    application_port: int | None
    build_log_path: str | None
    safe_error: str | None
    cleanup_state: str | None
    requested_at: str
    updated_at: str
    accepted_at: str | None
    last_healthy_at: str | None


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
class ActiveDeployment:
    application_id: str
    deployment_id: str
    lifecycle_state: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class IdempotencyRequest:
    request_id: str
    request_fingerprint: str
    result_kind: str | None
    result_id: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class EnvironmentRevision:
    application_id: str
    revision: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class SlugTombstone:
    slug: str
    application_id: str
    deleted_at: str


@dataclass(frozen=True, slots=True)
class ManagedResource:
    resource_id: str
    application_id: str
    resource_type: str
    resource_name: str
    display_label: str
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
            """
            CREATE INDEX operations_updated_at ON operations(updated_at)
            """,
            """
            CREATE TABLE environment_keys (
                            application_id TEXT NOT NULL REFERENCES applications(application_id) ON DELETE CASCADE,
                            key_name TEXT NOT NULL,
                            owner TEXT NOT NULL,
                            PRIMARY KEY (application_id, key_name)
                        ) STRICT
            """,
            """
            CREATE TABLE idempotency_requests (
                            request_id TEXT PRIMARY KEY,
                            request_fingerprint TEXT NOT NULL CHECK (
                                length(request_fingerprint) = 64
                                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
                            ),
                            result_kind TEXT,
                            result_id TEXT,
                            created_at TEXT NOT NULL,
                            completed_at TEXT,
                            CHECK ((result_kind IS NULL) = (result_id IS NULL)),
                            CHECK ((result_id IS NULL) = (completed_at IS NULL))
                        ) STRICT
            """,
            """
            CREATE TABLE deployment_attempts (
                            deployment_id TEXT PRIMARY KEY,
                            application_id TEXT NOT NULL REFERENCES applications(application_id) ON DELETE CASCADE,
                            status TEXT NOT NULL CHECK (status IN (
                                'queued','building','deploying','succeeded','failed','recovery_required'
                            )),
                            snapshot_kind TEXT NOT NULL CHECK (snapshot_kind = 'strict'),
                            source_commit TEXT NOT NULL,
                            requested_ref TEXT,
                            configuration_revision INTEGER CHECK (configuration_revision >= 0),
                            configuration_json TEXT,
                            configuration_sha256 TEXT,
                            environment_revision INTEGER CHECK (environment_revision >= 0),
                            idempotency_request_id TEXT UNIQUE REFERENCES idempotency_requests(request_id),
                            recipe_hash TEXT,
                            image_digest TEXT,
                            nomad_job TEXT,
                            nomad_job_sha256 TEXT,
                            nomad_version INTEGER CHECK (nomad_version >= 0),
                            health_path TEXT,
                            application_port INTEGER CHECK (application_port BETWEEN 1 AND 65535),
                            build_log_path TEXT,
                            safe_error TEXT CHECK (safe_error IS NULL OR length(safe_error) <= 1024),
                            cleanup_state TEXT,
                            requested_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            accepted_at TEXT,
                            last_healthy_at TEXT,
                            UNIQUE (application_id, deployment_id),
                            CHECK (
                                requested_ref IS NOT NULL
                                AND configuration_revision IS NOT NULL
                                AND json_valid(configuration_json)
                                AND json_type(configuration_json) = 'object'
                                AND length(configuration_sha256) = 64
                                AND configuration_sha256 NOT GLOB '*[^0-9a-f]*'
                                AND environment_revision IS NOT NULL
                                AND idempotency_request_id IS NOT NULL
                            ),
                            CHECK (status != 'succeeded' OR (
                                recipe_hash IS NOT NULL AND image_digest IS NOT NULL
                                AND nomad_job IS NOT NULL AND nomad_job_sha256 IS NOT NULL
                                AND nomad_version IS NOT NULL AND build_log_path IS NOT NULL
                                AND accepted_at IS NOT NULL AND last_healthy_at IS NOT NULL
                            )),
                            CHECK (status NOT IN ('failed','recovery_required') OR safe_error IS NOT NULL)
                        ) STRICT
            """,
            """
            CREATE UNIQUE INDEX one_unfinished_deployment_per_application
                        ON deployment_attempts(application_id)
                        WHERE status IN ('queued','building','deploying','recovery_required')
            """,
            """
            CREATE TABLE active_deployments (
                            application_id TEXT PRIMARY KEY REFERENCES applications(application_id) ON DELETE CASCADE,
                            deployment_id TEXT NOT NULL,
                            lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('running','stopped')),
                            updated_at TEXT NOT NULL,
                            FOREIGN KEY (application_id, deployment_id)
                                REFERENCES deployment_attempts(application_id, deployment_id)
                        ) STRICT
            """,
            """
            CREATE TRIGGER deployment_attempt_request_immutable
                        BEFORE UPDATE ON deployment_attempts
                        WHEN OLD.deployment_id IS NOT NEW.deployment_id
                          OR OLD.application_id IS NOT NEW.application_id
                          OR OLD.snapshot_kind IS NOT NEW.snapshot_kind
                          OR OLD.source_commit IS NOT NEW.source_commit
                          OR OLD.requested_ref IS NOT NEW.requested_ref
                          OR OLD.configuration_revision IS NOT NEW.configuration_revision
                          OR OLD.configuration_json IS NOT NEW.configuration_json
                          OR OLD.configuration_sha256 IS NOT NEW.configuration_sha256
                          OR OLD.environment_revision IS NOT NEW.environment_revision
                          OR OLD.idempotency_request_id IS NOT NEW.idempotency_request_id
                          OR OLD.requested_at IS NOT NEW.requested_at
                          OR OLD.health_path IS NOT NEW.health_path
                          OR OLD.application_port IS NOT NEW.application_port
                        BEGIN
                            SELECT RAISE(ABORT, 'deployment attempt request is immutable');
                        END
            """,
            """
            CREATE TABLE managed_resources (
                            resource_id TEXT PRIMARY KEY,
                            application_id TEXT NOT NULL REFERENCES applications(application_id) ON DELETE CASCADE,
                            resource_type TEXT NOT NULL CHECK (resource_type IN ('postgres','mongo','s3')),
                            resource_name TEXT NOT NULL CHECK (
                                length(resource_name) BETWEEN 1 AND 40
                                AND resource_name GLOB '[a-z]*'
                                AND resource_name NOT GLOB '*[^a-z0-9-]*'
                                AND resource_name NOT GLOB '*--*'
                                AND (length(resource_name) = 1 OR substr(resource_name, -1) GLOB '[a-z0-9]')
                            ),
                            display_label TEXT NOT NULL CHECK (length(display_label) BETWEEN 1 AND 100),
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
                            UNIQUE (application_id, resource_type, resource_name),
                            CHECK (resource_type = 'postgres' OR postgres_connections IS NULL),
                            CHECK (resource_type IN ('postgres','mongo') OR measured_target_bytes IS NULL),
                            CHECK (resource_type = 's3' OR (s3_bytes IS NULL AND s3_objects IS NULL))
                        ) STRICT
            """,
            """
            CREATE TABLE environment_revisions (
                            application_id TEXT PRIMARY KEY REFERENCES applications(application_id) ON DELETE CASCADE,
                            revision INTEGER NOT NULL CHECK (revision >= 0),
                            updated_at TEXT NOT NULL
                        ) STRICT
            """,
            """
            CREATE TABLE application_slug_tombstones (
                            slug TEXT PRIMARY KEY,
                            application_id TEXT NOT NULL,
                            deleted_at TEXT NOT NULL
                        ) STRICT
            """,
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
_SCHEMA_DROP_RE = re.compile(
    r"\bDROP\s+(TABLE|INDEX|VIEW|TRIGGER)\s+(?:IF\s+EXISTS\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)",
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
            changes = [
                (match.start(), "create", match)
                for match in _SCHEMA_OBJECT_RE.finditer(statement)
            ] + [
                (match.start(), "drop", match) for match in _SCHEMA_DROP_RE.finditer(statement)
            ]
            for _position, action, match in sorted(changes, key=lambda item: item[0]):
                if action == "create":
                    _unique, kind, name = match.groups()
                    normalized_kind = kind.lower()
                    prior = expected.get(name)
                    if prior is not None and prior != normalized_kind:
                        raise MigrationError("migration schema object kind changed")
                    expected[name] = normalized_kind
                else:
                    kind, name = match.groups()
                    if expected.get(name) == kind.lower():
                        del expected[name]
                if not _SCHEMA_NAME.fullmatch(name):
                    raise MigrationError("migration schema object name is malformed")
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
    # identity and perform the cross-deployment check above. Validate against
    # the schema generation actually present so a migration may retire an old
    # object without making its immediate predecessor look external.
    current = connection.execute(
        "SELECT max(version) AS version FROM schema_migrations WHERE version > 0"
    ).fetchone()["version"]
    _validate_schema_objects(
        connection,
        target_version=0 if current is None else int(current),
        require_complete=False,
    )


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
    check_same_thread: bool = True,
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
            check_same_thread=check_same_thread,
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


def request_fingerprint(request: Mapping[str, Any]) -> str:
    """Hash a bounded canonical request without persisting its contents."""
    plain = dict(request)
    _walk_refs(plain)
    try:
        encoded = json.dumps(
            plain,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError):
        raise ValidationError("idempotency request must be strict JSON") from None
    if len(encoded) > 1_048_576:
        raise ValidationError("idempotency request exceeds its 1048576-byte limit")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_request(row: sqlite3.Row | None) -> IdempotencyRequest | None:
    if row is None:
        return None
    return IdempotencyRequest(
        request_id=row["request_id"],
        request_fingerprint=row["request_fingerprint"],
        result_kind=row["result_kind"],
        result_id=row["result_id"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def get_idempotency_request(
    connection: sqlite3.Connection, request_id: str
) -> IdempotencyRequest | None:
    identifier = uuid(request_id, field="request_id")
    return _idempotency_request(
        connection.execute(
            "SELECT * FROM idempotency_requests WHERE request_id = ?", (identifier,)
        ).fetchone()
    )


def claim_idempotency_request(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    request_fingerprint: str,
    now: str | None = None,
) -> IdempotencyRequest:
    identifier = uuid(request_id, field="request_id")
    fingerprint = sha256_hex(request_fingerprint, field="request fingerprint")
    with transaction(connection):
        existing = get_idempotency_request(connection, identifier)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    "idempotency request was already used for different input"
                )
            return existing
        connection.execute(
            "INSERT INTO idempotency_requests VALUES (?, ?, NULL, NULL, ?, NULL)",
            (identifier, fingerprint, now or utc_now()),
        )
    result = get_idempotency_request(connection, identifier)
    assert result is not None
    return result


def complete_idempotency_request(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    result_kind: str,
    result_id: str,
    now: str | None = None,
) -> IdempotencyRequest:
    identifier = uuid(request_id, field="request_id")
    kind = _operation_token(result_kind, field="result kind")
    result_identifier = uuid(result_id, field="idempotency result ID")
    with transaction(connection):
        existing = get_idempotency_request(connection, identifier)
        if existing is None:
            raise DatabaseError("idempotency request is missing")
        if existing.result_id is not None:
            if (existing.result_kind, existing.result_id) != (kind, result_identifier):
                raise IdempotencyConflictError(
                    "idempotency request already has a different result"
                )
            return existing
        connection.execute(
            "UPDATE idempotency_requests SET result_kind = ?, result_id = ?, completed_at = ? "
            "WHERE request_id = ?",
            (kind, result_identifier, now or utc_now(), identifier),
        )
    result = get_idempotency_request(connection, identifier)
    assert result is not None
    return result


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


def list_application_deploy_operations(
    connection: sqlite3.Connection, application_id: str
) -> tuple[Operation, ...]:
    rows = connection.execute(
        """
        SELECT * FROM operations
         WHERE scope = ? AND kind = 'app.deploy'
         ORDER BY started_at DESC, operation_id DESC
        """,
        (f"app-{application_id}",),
    ).fetchall()
    return tuple(operation for row in rows if (operation := _operation(row)) is not None)


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
        tombstone = connection.execute(
            "SELECT 1 FROM application_slug_tombstones WHERE slug = ?",
            (application_slug,),
        ).fetchone()
        if tombstone is not None:
            raise DatabaseError("application slug is permanently retired")
        existing = connection.execute(
            "SELECT slug FROM applications WHERE application_id = ?", (application_id,)
        ).fetchone()
        if existing is not None and existing["slug"] != application_slug:
            connection.execute(
                "INSERT INTO application_slug_tombstones VALUES (?, ?, ?)",
                (existing["slug"], application_id, timestamp),
            )
        connection.execute(
            """
            INSERT INTO applications(
              application_id, slug, repository_url, desired_running, url,
              worker_server_id, worker_server_name, worker_port_id, worker_port_name,
              worker_flavor, scheduler_cpu_mhz, scheduler_memory_mib, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
              slug=excluded.slug, repository_url=excluded.repository_url,
              desired_running=excluded.desired_running, url=excluded.url,
              worker_server_id=excluded.worker_server_id,
              worker_server_name=excluded.worker_server_name, worker_port_id=excluded.worker_port_id,
              worker_port_name=excluded.worker_port_name, worker_flavor=excluded.worker_flavor,
              scheduler_cpu_mhz=excluded.scheduler_cpu_mhz,
              scheduler_memory_mib=excluded.scheduler_memory_mib, updated_at=excluded.updated_at
            """,
            (
                application_id,
                application_slug,
                repository_url,
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
        connection.execute(
            "INSERT INTO environment_revisions VALUES (?, 0, ?) "
            "ON CONFLICT(application_id) DO NOTHING",
            (application_id, timestamp),
        )
        connection.execute(
            "UPDATE active_deployments SET lifecycle_state = ?, updated_at = ? "
            "WHERE application_id = ?",
            ("running" if desired_running else "stopped", timestamp, application_id),
        )


def _application(row: sqlite3.Row | None) -> Application | None:
    if row is None:
        return None
    return Application(
        application_id=row["application_id"],
        slug=row["slug"],
        repository_url=row["repository_url"],
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
            "SELECT application.* FROM applications AS application "
            "WHERE (application.application_id = ? OR application.slug = ?) "
            "AND NOT EXISTS (SELECT 1 FROM application_slug_tombstones AS tombstone "
            "WHERE tombstone.application_id = application.application_id)",
            (identifier, identifier),
        ).fetchone()
    )


def _get_application_including_tombstone(
    connection: sqlite3.Connection, application_id: str
) -> Application | None:
    return _application(
        connection.execute(
            "SELECT * FROM applications WHERE application_id = ?", (application_id,)
        ).fetchone()
    )


def list_applications(connection: sqlite3.Connection) -> list[Application]:
    applications: list[Application] = []
    for row in connection.execute(
        "SELECT application.* FROM applications AS application "
        "WHERE NOT EXISTS (SELECT 1 FROM application_slug_tombstones AS tombstone "
        "WHERE tombstone.application_id = application.application_id) ORDER BY application.slug"
    ):
        application = _application(row)
        assert application is not None
        applications.append(application)
    return applications


def set_application_runtime(
    connection: sqlite3.Connection,
    application_id: str,
    *,
    running: bool,
    worker_server_id: str | None = None,
    worker_server_name: str | None = None,
    worker_port_id: str | None = None,
    worker_port_name: str | None = None,
    nomad_version: int | None = None,
    now: str | None = None,
) -> None:
    """Atomically record only accepted runtime presence; deployment snapshots stay intact."""
    identifier = uuid(application_id, field="application_id")
    if running and None in (
        worker_server_id,
        worker_server_name,
        worker_port_id,
        worker_port_name,
    ):
        raise ValidationError("running application requires exact worker identity")
    if worker_server_id is not None:
        uuid(worker_server_id, field="worker_server_id")
    if worker_port_id is not None:
        uuid(worker_port_id, field="worker_port_id")
    if nomad_version is not None:
        _revision(nomad_version, field="nomad version")
    timestamp = now or utc_now()
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE applications SET desired_running = ?, worker_server_id = ?, "
            "worker_server_name = ?, worker_port_id = ?, worker_port_name = ?, updated_at = ? "
            "WHERE application_id = ?",
            (
                int(running), worker_server_id, worker_server_name, worker_port_id,
                worker_port_name, timestamp, identifier,
            ),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("application is missing")
        active = connection.execute(
            "SELECT deployment_id FROM active_deployments WHERE application_id = ?",
            (identifier,),
        ).fetchone()
        if active is not None:
            connection.execute(
                "UPDATE active_deployments SET lifecycle_state = ?, updated_at = ? "
                "WHERE application_id = ?",
                ("running" if running else "stopped", timestamp, identifier),
            )
            if nomad_version is not None:
                connection.execute(
                    "UPDATE deployment_attempts SET nomad_version = ?, last_healthy_at = ?, "
                    "updated_at = ? WHERE deployment_id = ? AND application_id = ?",
                    (nomad_version, timestamp, timestamp, active["deployment_id"], identifier),
                )


def _deployment_attempt(row: sqlite3.Row | None) -> DeploymentAttempt | None:
    if row is None:
        return None
    configuration = None
    if row["snapshot_kind"] == "strict":
        from .deployment_config import parse_configuration

        encoded = row["configuration_json"]
        if hashlib.sha256(encoded.encode()).hexdigest() != row["configuration_sha256"]:
            raise DatabaseError("deployment configuration snapshot hash does not match")
        configuration = parse_configuration(encoded)
        if configuration.canonical_json() != encoded:
            raise DatabaseError("deployment configuration snapshot is not canonical")
    return DeploymentAttempt(
        deployment_id=row["deployment_id"],
        application_id=row["application_id"],
        status=row["status"],
        snapshot_kind=row["snapshot_kind"],
        source_commit=row["source_commit"],
        requested_ref=row["requested_ref"],
        configuration_revision=row["configuration_revision"],
        configuration=configuration,
        configuration_sha256=row["configuration_sha256"],
        environment_revision=row["environment_revision"],
        idempotency_request_id=row["idempotency_request_id"],
        recipe_hash=row["recipe_hash"],
        image_digest=row["image_digest"],
        nomad_job=row["nomad_job"],
        nomad_job_sha256=row["nomad_job_sha256"],
        nomad_version=row["nomad_version"],
        health_path=row["health_path"],
        application_port=row["application_port"],
        build_log_path=row["build_log_path"],
        safe_error=row["safe_error"],
        cleanup_state=row["cleanup_state"],
        requested_at=row["requested_at"],
        updated_at=row["updated_at"],
        accepted_at=row["accepted_at"],
        last_healthy_at=row["last_healthy_at"],
    )


def get_deployment_attempt(
    connection: sqlite3.Connection, deployment_id: str
) -> DeploymentAttempt | None:
    identifier = uuid(deployment_id, field="deployment_id")
    return _deployment_attempt(
        connection.execute(
            "SELECT * FROM deployment_attempts WHERE deployment_id = ?", (identifier,)
        ).fetchone()
    )


def get_active_deployment(
    connection: sqlite3.Connection, application_id: str
) -> ActiveDeployment | None:
    row = connection.execute(
        "SELECT * FROM active_deployments WHERE application_id = ?", (application_id,)
    ).fetchone()
    if row is None:
        return None
    return ActiveDeployment(
        application_id=row["application_id"],
        deployment_id=row["deployment_id"],
        lifecycle_state=row["lifecycle_state"],
        updated_at=row["updated_at"],
    )


def get_deployment(
    connection: sqlite3.Connection, application_id: str
) -> Deployment | None:
    row = connection.execute(
        """
        SELECT attempt.*
          FROM active_deployments AS active
          JOIN deployment_attempts AS attempt
            ON attempt.deployment_id = active.deployment_id
           AND attempt.application_id = active.application_id
         WHERE active.application_id = ?
        """,
        (application_id,),
    ).fetchone()
    attempt = _deployment_attempt(row)
    if attempt is None:
        return None
    if (
        attempt.recipe_hash is None
        or attempt.image_digest is None
        or attempt.nomad_job is None
        or attempt.nomad_job_sha256 is None
        or attempt.nomad_version is None
        or attempt.health_path is None
        or attempt.application_port is None
        or attempt.build_log_path is None
        or attempt.accepted_at is None
        or attempt.last_healthy_at is None
    ):
        raise DatabaseError("active deployment evidence is incomplete")
    return Deployment(
        application_id=attempt.application_id,
        source_commit=attempt.source_commit,
        recipe_hash=attempt.recipe_hash,
        image_digest=attempt.image_digest,
        nomad_job=attempt.nomad_job,
        nomad_job_sha256=attempt.nomad_job_sha256,
        nomad_version=attempt.nomad_version,
        health_path=attempt.health_path,
        application_port=attempt.application_port,
        build_log_path=attempt.build_log_path,
        accepted_at=attempt.accepted_at,
        last_healthy_at=attempt.last_healthy_at,
    )


def _revision(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field} must be a non-negative integer")
    return value


def create_deployment_attempt(
    connection: sqlite3.Connection,
    *,
    deployment_id: str,
    application_id: str,
    source_commit: str,
    requested_ref: str,
    configuration_revision: int,
    configuration: DeploymentConfiguration,
    environment_revision: int,
    idempotency_request_id: str,
    now: str | None = None,
) -> DeploymentAttempt:
    from .deployment_config import DeploymentConfiguration

    identifier = uuid(deployment_id, field="deployment_id")
    application = uuid(application_id, field="application_id")
    source = commit(source_commit)
    if not isinstance(requested_ref, str) or not _REQUESTED_REF.fullmatch(requested_ref):
        raise ValidationError("requested source ref is malformed")
    config_revision = _revision(configuration_revision, field="configuration revision")
    environment = _revision(environment_revision, field="environment revision")
    request_id = uuid(idempotency_request_id, field="idempotency request ID")
    if not isinstance(configuration, DeploymentConfiguration):
        raise ValidationError("deployment configuration snapshot is malformed")
    encoded = configuration.canonical_json()
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    timestamp = now or utc_now()
    with transaction(connection):
        request = get_idempotency_request(connection, request_id)
        if request is None:
            raise DatabaseError("idempotency request is missing")
        if request.result_id is not None:
            if (request.result_kind, request.result_id) != ("deployment", identifier):
                raise IdempotencyConflictError("idempotency request already has a different result")
            existing = get_deployment_attempt(connection, identifier)
            if existing is None:
                raise DatabaseError("idempotency result deployment is missing")
            return existing
        current_environment = get_environment_revision(connection, application)
        if current_environment is None or current_environment.revision != environment:
            raise DatabaseError("environment revision is missing or stale")
        unfinished = connection.execute(
            "SELECT deployment_id FROM deployment_attempts WHERE application_id = ? "
            "AND status IN ('queued','building','deploying','recovery_required')",
            (application,),
        ).fetchone()
        if unfinished is not None:
            raise DatabaseError("application already has an unfinished deployment attempt")
        connection.execute(
            """
            INSERT INTO deployment_attempts(
                deployment_id, application_id, status, snapshot_kind, source_commit,
                requested_ref, configuration_revision, configuration_json,
                configuration_sha256, environment_revision, idempotency_request_id,
                health_path, application_port, requested_at, updated_at
            ) VALUES (?, ?, 'queued', 'strict', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                application,
                source,
                requested_ref,
                config_revision,
                encoded,
                fingerprint,
                environment,
                request_id,
                configuration.health_path,
                configuration.port,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE idempotency_requests SET result_kind = 'deployment', result_id = ?, "
            "completed_at = ? WHERE request_id = ?",
            (identifier, timestamp, request_id),
        )
    result = get_deployment_attempt(connection, identifier)
    assert result is not None
    return result


def checkpoint_deployment_attempt(
    connection: sqlite3.Connection,
    deployment_id: str,
    *,
    status: str,
    recipe_hash: str | None = None,
    image_digest: str | None = None,
    nomad_job: str | None = None,
    nomad_job_sha256: str | None = None,
    nomad_version: int | None = None,
    build_log_path: str | None = None,
    error: BaseException | str | None = None,
    cleanup_state: str | None = None,
    now: str | None = None,
) -> DeploymentAttempt:
    identifier = uuid(deployment_id, field="deployment_id")
    if status not in _DEPLOYMENT_STATUSES:
        raise ValidationError("deployment attempt status is malformed")
    evidence: dict[str, Any] = {}
    if recipe_hash is not None:
        evidence["recipe_hash"] = sha256_hex(recipe_hash, field="recipe_hash")
    if image_digest is not None:
        evidence["image_digest"] = oci_digest_pin(image_digest, field="image_digest")
    if nomad_job is not None:
        evidence["nomad_job"] = nomad_job
    if nomad_job_sha256 is not None:
        evidence["nomad_job_sha256"] = sha256_hex(
            nomad_job_sha256, field="nomad_job_sha256"
        )
    if nomad_version is not None:
        evidence["nomad_version"] = _revision(nomad_version, field="nomad version")
    if build_log_path is not None:
        if not build_log_path or len(build_log_path) > 1024:
            raise ValidationError("build log path is malformed")
        evidence["build_log_path"] = build_log_path
    if error is not None:
        evidence["safe_error"] = safe_summary(error)
    if cleanup_state is not None:
        evidence["cleanup_state"] = _cleanup_state(cleanup_state)
    timestamp = now or utc_now()
    with transaction(connection):
        current = get_deployment_attempt(connection, identifier)
        if current is None:
            raise DatabaseError("deployment attempt is missing")
        resulting_error = evidence.get("safe_error", current.safe_error)
        if status in {"failed", "recovery_required"} and resulting_error is None:
            raise DatabaseError("failed deployment attempt requires a safe error")
        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, timestamp]
        for column, value in evidence.items():
            assignments.append(f"{column} = ?")
            values.append(value)
        if status == "succeeded":
            assignments.extend(["accepted_at = ?", "last_healthy_at = ?"])
            values.extend([timestamp, timestamp])
        values.append(identifier)
        try:
            connection.execute(
                f"UPDATE deployment_attempts SET {', '.join(assignments)} "
                "WHERE deployment_id = ?",
                values,
            )
        except sqlite3.IntegrityError as database_error:
            raise DatabaseError("deployment attempt evidence is incomplete") from database_error
        if status == "succeeded":
            connection.execute(
                """
                INSERT INTO active_deployments VALUES (?, ?, 'running', ?)
                ON CONFLICT(application_id) DO UPDATE SET
                  deployment_id=excluded.deployment_id,
                  lifecycle_state=excluded.lifecycle_state,
                  updated_at=excluded.updated_at
                """,
                (current.application_id, identifier, timestamp),
            )
            connection.execute(
                "UPDATE applications SET desired_running = 1, updated_at = ? "
                "WHERE application_id = ?",
                (timestamp, current.application_id),
            )
    result = get_deployment_attempt(connection, identifier)
    assert result is not None
    return result


def list_deployment_attempts(
    connection: sqlite3.Connection,
    application_id: str,
    *,
    status: str | None = None,
) -> tuple[DeploymentAttempt, ...]:
    identifier = uuid(application_id, field="application_id")
    if status is not None and status not in _DEPLOYMENT_STATUSES:
        raise ValidationError("deployment attempt status is malformed")
    query = "SELECT * FROM deployment_attempts WHERE application_id = ?"
    parameters: tuple[str, ...] = (identifier,)
    if status is not None:
        query += " AND status = ?"
        parameters += (status,)
    rows = connection.execute(
        query + " ORDER BY requested_at DESC, deployment_id DESC", parameters
    )
    return tuple(attempt for row in rows if (attempt := _deployment_attempt(row)) is not None)


def active_storage_resource_ids(
    connection: sqlite3.Connection, application_id: str
) -> tuple[str, ...]:
    """Return immutable storage UUIDs referenced by the accepted configuration."""
    active = get_active_deployment(connection, uuid(application_id, field="application_id"))
    if active is None:
        return ()
    attempt = get_deployment_attempt(connection, active.deployment_id)
    if attempt is None or attempt.configuration is None:
        return ()
    return tuple(binding.resource_id for binding in attempt.configuration.storage_bindings)


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
    images = [
        oci_digest_pin(attempt.image_digest, field="deployment image")
        for attempt in list_deployment_attempts(connection, identifier)
        if attempt.image_digest is not None
    ]
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
    images = [
        oci_digest_pin(attempt.image_digest, field="deployment image")
        for attempt in list_deployment_attempts(connection, identifier, status="succeeded")
        if attempt.image_digest is not None
    ]
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


def _display_label(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 100
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ValidationError("storage display label must be 1-100 printable characters")
    return value


def put_managed_resource(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    resource_type: str,
    resource_name: str = "default",
    provider_name: str,
    lifecycle_state: str,
    provider_id: str | None = None,
    resource_id: str | None = None,
    display_label: str | None = None,
    postgres_connections: int | None = None,
    measured_target_bytes: int | None = None,
    s3_bytes: int | None = None,
    s3_objects: int | None = None,
    last_verified_at: str | None = None,
    now: str | None = None,
) -> ManagedResource:
    application_id = uuid(application_id, field="application_id")
    checked_resource_name = validate_resource_name(resource_name)
    checked_label = _display_label(
        checked_resource_name if display_label is None else display_label
    )
    supplied_id = None if resource_id is None else uuid(resource_id, field="resource_id")
    timestamp = now or utc_now()
    with transaction(connection):
        existing = connection.execute(
            "SELECT resource_id FROM managed_resources "
            "WHERE application_id = ? AND resource_type = ? AND resource_name = ?",
            (application_id, resource_type, checked_resource_name),
        ).fetchone()
        if (
            existing is not None
            and supplied_id is not None
            and existing["resource_id"] != supplied_id
        ):
            raise DatabaseError("storage resource UUID is immutable")
        identifier = (
            existing["resource_id"]
            if existing is not None
            else supplied_id or str(uuid_module.uuid4())
        )
        connection.execute(
            """
            INSERT INTO managed_resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(application_id, resource_type, resource_name) DO UPDATE SET
              display_label=excluded.display_label,
              provider_id=excluded.provider_id, provider_name=excluded.provider_name,
              lifecycle_state=excluded.lifecycle_state,
              postgres_connections=excluded.postgres_connections,
              measured_target_bytes=excluded.measured_target_bytes,
              s3_bytes=excluded.s3_bytes, s3_objects=excluded.s3_objects,
              last_verified_at=excluded.last_verified_at, updated_at=excluded.updated_at
            """,
            (
                identifier,
                application_id,
                resource_type,
                checked_resource_name,
                checked_label,
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
    result = get_managed_resource(connection, identifier)
    assert result is not None
    return result


def _managed_resource(row: sqlite3.Row | None) -> ManagedResource | None:
    if row is None:
        return None
    return ManagedResource(
        resource_id=row["resource_id"],
        application_id=row["application_id"],
        resource_type=row["resource_type"],
        resource_name=row["resource_name"],
        display_label=row["display_label"],
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


def get_managed_resource(
    connection: sqlite3.Connection, resource_id: str
) -> ManagedResource | None:
    identifier = uuid(resource_id, field="resource_id")
    return _managed_resource(
        connection.execute(
            "SELECT * FROM managed_resources WHERE resource_id = ?", (identifier,)
        ).fetchone()
    )


def rename_managed_resource(
    connection: sqlite3.Connection,
    resource_id: str,
    display_label: str,
    *,
    now: str | None = None,
) -> ManagedResource:
    identifier = uuid(resource_id, field="resource_id")
    label = _display_label(display_label)
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE managed_resources SET display_label = ?, updated_at = ? WHERE resource_id = ?",
            (label, now or utc_now(), identifier),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("storage resource is missing")
    result = get_managed_resource(connection, identifier)
    assert result is not None
    return result


def list_managed_resources(
    connection: sqlite3.Connection,
    *,
    application_id: str | None = None,
) -> list[ManagedResource]:
    if application_id is None:
        rows = connection.execute(
            "SELECT * FROM managed_resources ORDER BY application_id, resource_type, resource_name"
        )
    else:
        rows = connection.execute(
            "SELECT * FROM managed_resources WHERE application_id = ? ORDER BY resource_type, resource_name",
            (application_id,),
        )
    return [resource for row in rows if (resource := _managed_resource(row)) is not None]


def set_managed_resource_lifecycle(
    connection: sqlite3.Connection,
    resource_id: str,
    lifecycle_state: str,
    *,
    now: str | None = None,
) -> None:
    identifier = uuid(resource_id, field="resource_id")
    if lifecycle_state not in {"creating", "active", "removing", "recovery_required"}:
        raise ValidationError("storage lifecycle state is invalid")
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE managed_resources SET lifecycle_state = ?, updated_at = ? "
            "WHERE resource_id = ?",
            (lifecycle_state, now or utc_now(), identifier),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("storage resource is missing")


def delete_managed_resource(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    resource_type: str,
    resource_name: str = "default",
) -> None:
    uuid(application_id, field="application_id")
    checked_name = validate_resource_name(resource_name)
    with transaction(connection):
        connection.execute(
            "DELETE FROM managed_resources WHERE application_id = ? AND resource_type = ? AND resource_name = ?",
            (application_id, resource_type, checked_name),
        )


def get_environment_revision(
    connection: sqlite3.Connection, application_id: str
) -> EnvironmentRevision | None:
    identifier = uuid(application_id, field="application_id")
    row = connection.execute(
        "SELECT * FROM environment_revisions WHERE application_id = ?", (identifier,)
    ).fetchone()
    if row is None:
        return None
    return EnvironmentRevision(
        application_id=row["application_id"],
        revision=row["revision"],
        updated_at=row["updated_at"],
    )


def advance_environment_revision(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    expected_revision: int,
    now: str | None = None,
) -> EnvironmentRevision:
    identifier = uuid(application_id, field="application_id")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise ValidationError("expected environment revision must be an integer")
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE environment_revisions SET revision = revision + 1, updated_at = ? "
            "WHERE application_id = ? AND revision = ?",
            (now or utc_now(), identifier, expected_revision),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("environment revision is missing or stale")
    result = get_environment_revision(connection, identifier)
    assert result is not None
    return result


def get_slug_tombstone(
    connection: sqlite3.Connection, application_slug: str
) -> SlugTombstone | None:
    checked_slug = slug(application_slug)
    row = connection.execute(
        "SELECT * FROM application_slug_tombstones WHERE slug = ?", (checked_slug,)
    ).fetchone()
    if row is None:
        return None
    return SlugTombstone(
        slug=row["slug"], application_id=row["application_id"], deleted_at=row["deleted_at"]
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
    if not isinstance(owner, str) or not _ENVIRONMENT_OWNER.fullmatch(owner):
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


def complete_application_deletion(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    operation_id: str,
    now: str | None = None,
) -> None:
    """Tombstone a fully purged app while retaining deployment and operation history."""
    identifier = uuid(application_id, field="application_id")
    operation_identifier = uuid(operation_id, field="operation_id")
    timestamp = now or utc_now()
    with transaction(connection):
        operation = get_operation(connection, operation_identifier)
        if (
            operation is None
            or operation.kind != "app.delete"
            or operation.status not in {"running", "recovery_required"}
            or operation.refs.get("application_id") != identifier
            or operation.phase != "manifest_absent"
        ):
            raise DatabaseError("application deletion operation is not ready to complete")
        if connection.execute(
            "SELECT 1 FROM managed_resources WHERE application_id = ? LIMIT 1", (identifier,)
        ).fetchone() is not None:
            raise DatabaseError("application deletion refuses managed resource rows")
        application = _get_application_including_tombstone(connection, identifier)
        if application is None:
            raise DatabaseError("application is missing")
        connection.execute(
            "INSERT INTO application_slug_tombstones VALUES (?, ?, ?) "
            "ON CONFLICT(slug) DO NOTHING",
            (application.slug, identifier, timestamp),
        )
        connection.execute(
            "DELETE FROM environment_keys WHERE application_id = ?", (identifier,)
        )
        connection.execute(
            "UPDATE applications SET desired_running = 0, url = NULL, "
            "worker_server_id = NULL, worker_server_name = NULL, worker_port_id = NULL, "
            "worker_port_name = NULL, updated_at = ? WHERE application_id = ?",
            (timestamp, identifier),
        )
        connection.execute(
            "UPDATE active_deployments SET lifecycle_state = 'stopped', updated_at = ? "
            "WHERE application_id = ?",
            (timestamp, identifier),
        )
        cursor = connection.execute(
            "UPDATE operations SET status = 'succeeded', phase = 'tombstoned', "
            "updated_at = ?, safe_error = NULL, cleanup_state = 'confirmed' "
            "WHERE operation_id = ? AND status IN ('running','recovery_required')",
            (timestamp, operation_identifier),
        )
        if cursor.rowcount != 1:
            raise DatabaseError("application deletion operation could not be completed")


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
