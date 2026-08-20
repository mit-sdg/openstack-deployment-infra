"""Managed-storage provider operations executed on the trusted admin host.

Application credentials exist only in this process, provider APIs, and the
application's Nomad Variable. Handler arguments and results contain provider
identities, fixed policy inputs, key names, and verification evidence, never
secret values.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn, cast

from ..storage_contract import ENVIRONMENT_KEYS
from ..validation import ValidationError, slug, uuid
from .main import Handler, HelperActionError
from .nomad import (
    SecretItems,
    VariableClient,
    VariableSnapshot,
    VariableUpdate,
    update_owned_items,
    variable_path,
)

POSTGRES_KEYS = ENVIRONMENT_KEYS["postgres"]
MONGO_KEYS = ENVIRONMENT_KEYS["mongo"]
S3_KEYS = ENVIRONMENT_KEYS["s3"]
RESOURCE_KEYS = ENVIRONMENT_KEYS
_OWNER = {"postgres": "postgres", "mongo": "mongo", "s3": "s3"}
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{1,62}")
APPLICATION_CA_PATH = "/platform-ca/internal-ca.crt"


@dataclass(frozen=True, slots=True)
class ProviderCredential:
    """A scoped credential whose representation cannot disclose its values."""

    provider_id: str
    provider_name: str
    credential_name: str
    environment: SecretItems = field(repr=False)


@dataclass(frozen=True, slots=True)
class RotationEvidence:
    allocation_healthy: bool
    observed_modify_index: int
    public_healthy: bool = True


EvidenceObserver = Callable[[str, str, str, int], RotationEvidence]
PasswordFactory = Callable[[], str]


def _password() -> str:
    return secrets.token_urlsafe(32)


def _operation_generation(operation_id: str, action: str) -> str:
    identifier = uuid(operation_id, field="operationId")
    return hashlib.sha256(f"{identifier}:{action}".encode()).hexdigest()[:8]


def _operation_args(
    args: Mapping[str, Any], expected: set[str], action: str
) -> tuple[str, bool, str]:
    """Require the sole greenfield mutation envelope.

    The pre-operation protocol had no durable identity and cannot safely
    distinguish a retry from a second mutation. It is intentionally not
    accepted by the M1 helper.
    """
    operation_keys = {"operationId", "recover"}
    if args.keys() != expected | operation_keys:
        raise HelperActionError("INVALID_ARGS", f"{action} arguments are invalid")
    operation_id = uuid(args["operationId"], field="operationId")
    recovering = args["recover"]
    if not isinstance(recovering, bool):
        raise HelperActionError("INVALID_ARGS", f"{action} recovery flag is invalid")
    return operation_id, recovering, _operation_generation(operation_id, action)


def _identity(application_id: object) -> tuple[str, str, str]:
    identifier = uuid(application_id, field="applicationId")
    base = identifier.replace("-", "")[:20]
    return identifier, f"p_{base}", f"o_{base}"


def _credential_name(application_id: object, generation: str) -> str:
    identifier, _, _ = _identity(application_id)
    if not re.fullmatch(r"[a-f0-9]{8}", generation):
        raise ValidationError("credential generation is malformed")
    return f"u_{identifier.replace('-', '')[:20]}_{generation}"


def _positive(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{field_name} must be a positive integer")
    return value


def _exact(args: Mapping[str, Any], expected: set[str], action: str) -> None:
    if args.keys() != expected:
        raise HelperActionError("INVALID_ARGS", f"{action} arguments are invalid")


def _common(args: Mapping[str, Any]) -> tuple[str, str]:
    return uuid(args["applicationId"], field="applicationId"), slug(args["applicationSlug"])


def _require_fixed_provider(
    args: Mapping[str, Any], application_id: str, resource_type: str
) -> str:
    _, expected_name, _ = _identity(application_id)
    if args.get("providerId") != expected_name or args.get("providerName") != expected_name:
        raise HelperActionError(
            "IDENTITY_MISMATCH", f"{resource_type} provider identity does not match"
        )
    return expected_name


def _require_s3_provider(
    args: Mapping[str, Any], environment: Mapping[str, str]
) -> tuple[str, str]:
    provider_id = args.get("providerId")
    provider_name = args.get("providerName")
    if (
        not isinstance(provider_id, str)
        or not provider_id
        or "\x00" in provider_id
        or not isinstance(provider_name, str)
        or not provider_name
        or provider_name != environment["S3_BUCKET"]
    ):
        raise HelperActionError("IDENTITY_MISMATCH", "S3 provider identity does not match")
    return provider_id, provider_name


def _require_s3_endpoint(environment: Mapping[str, str], expected_endpoint: str) -> str:
    """Validate the endpoint before a scoped client receives any secret."""
    endpoint = environment.get("AWS_ENDPOINT_URL_S3")
    if (
        not isinstance(expected_endpoint, str)
        or not expected_endpoint
        or "\x00" in expected_endpoint
        or not isinstance(endpoint, str)
        or not endpoint
        or endpoint != environment.get("S3_ENDPOINT")
        or endpoint != expected_endpoint
        or "\x00" in endpoint
        or not endpoint.startswith("https://")
    ):
        raise HelperActionError("IDENTITY_MISMATCH", "S3 endpoint identity does not match")
    return endpoint


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValidationError("generated provider identifier is malformed")
    return '"' + value + '"'


def _pg_execute(connection: Any, statement: str, parameters: Sequence[Any] = ()) -> Any:
    return connection.execute(statement, tuple(parameters))


def _pg_exists(connection: Any, query: str, name: str) -> bool:
    return _pg_execute(connection, query, (name,)).fetchone() is not None


def postgres_environment(
    host: str,
    database: str,
    username: str,
    password: str,
    *,
    ca_path: str = APPLICATION_CA_PATH,
) -> SecretItems:
    user = urllib.parse.quote(username, safe="")
    secret = urllib.parse.quote(password, safe="")
    ca = urllib.parse.quote(ca_path, safe="")
    return SecretItems(
        {
            "DATABASE_URL": (
                f"postgresql://{user}:{secret}@{host}:5432/{database}"
                f"?sslmode=verify-full&sslrootcert={ca}"
            ),
            "PGHOST": host,
            "PGPORT": "5432",
            "PGDATABASE": database,
            "PGUSER": username,
            "PGPASSWORD": password,
            "PGSSLMODE": "verify-full",
            "PGSSLROOTCERT": ca_path,
        }
    )


_POSTGRES_MARKER_PREFIX = "m1-platform-create"


def _postgres_marker(operation_id: str, generation: str, role: str) -> str:
    identifier = uuid(operation_id, field="operationId")
    if not re.fullmatch(r"[a-f0-9]{8}", generation) or role not in {
        "owner",
        "credential",
        "database",
    }:
        raise ValidationError("PostgreSQL creation marker is malformed")
    return f"{_POSTGRES_MARKER_PREFIX}:{identifier}:{generation}:{role}"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _postgres_creation_evidence(
    admin: Any,
    *,
    application_id: str,
    operation_id: str,
    generation: str,
) -> tuple[str, str, int]:
    """Return exact created identities only when all durable markers agree."""
    _, database, owner = _identity(application_id)
    credential_name = _credential_name(application_id, generation)
    owner_row = _pg_execute(
        admin,
        "SELECT rolname, obj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname=%s",
        (owner,),
    ).fetchone()
    credential_row = _pg_execute(
        admin,
        "SELECT rolname, obj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname=%s",
        (credential_name,),
    ).fetchone()
    database_row = _pg_execute(
        admin,
        "SELECT datname, pg_get_userbyid(datdba), obj_description(oid, 'pg_database') "
        "FROM pg_database WHERE datname=%s",
        (database,),
    ).fetchone()
    expected_owner = _postgres_marker(operation_id, generation, "owner")
    expected_credential = _postgres_marker(operation_id, generation, "credential")
    prefix = f"{_POSTGRES_MARKER_PREFIX}:{uuid(operation_id, field='operationId')}:{generation}:database:size="
    if (
        owner_row is None
        or credential_row is None
        or database_row is None
        or len(owner_row) < 2
        or len(credential_row) < 2
        or len(database_row) < 3
        or owner_row[0] != owner
        or owner_row[1] != expected_owner
        or credential_row[0] != credential_name
        or credential_row[1] != expected_credential
        or database_row[0] != database
        or database_row[1] != owner
        or not isinstance(database_row[2], str)
        or not database_row[2].startswith(prefix)
    ):
        raise _recovery_required(
            "postgres", "create", "inspect the operation-owned role and database markers"
        )
    try:
        baseline = int(database_row[2].removeprefix(prefix))
    except (TypeError, ValueError):
        raise _recovery_required(
            "postgres", "create", "inspect the operation-owned database size marker"
        ) from None
    if baseline < 0:
        raise _recovery_required(
            "postgres", "create", "inspect the operation-owned database size marker"
        )
    return database, owner, baseline


def _postgres_creation_empty(
    admin: Any,
    *,
    database: str,
    baseline_size: int,
) -> bool:
    """Use an independent provider size read before an irreversible drop."""
    row = _pg_execute(admin, "SELECT pg_database_size(%s)", (database,)).fetchone()
    if row is None or len(row) < 1 or isinstance(row[0], bool):
        return False
    try:
        size = int(row[0])
    except (TypeError, ValueError):
        return False
    return size == baseline_size


def _postgres_created_absent(
    admin: Any, *, database: str, owner: str, credential_name: str
) -> bool:
    return (
        not _pg_exists(admin, "SELECT 1 FROM pg_database WHERE datname=%s", database)
        and not _pg_exists(admin, "SELECT 1 FROM pg_roles WHERE rolname=%s", owner)
        and not _pg_exists(admin, "SELECT 1 FROM pg_roles WHERE rolname=%s", credential_name)
    )


def _postgres_remove_created(
    admin: Any,
    *,
    application_id: str,
    operation_id: str,
    generation: str,
) -> None:
    database, owner, baseline = _postgres_creation_evidence(
        admin,
        application_id=application_id,
        operation_id=operation_id,
        generation=generation,
    )
    credential_name = _credential_name(application_id, generation)
    if not _postgres_creation_empty(admin, database=database, baseline_size=baseline):
        raise _recovery_required(
            "postgres", "create", "inspect the operation-owned database for application data"
        )
    _pg_execute(admin, f"DROP DATABASE {_quote_identifier(database)} WITH (FORCE)")
    if _pg_exists(admin, "SELECT 1 FROM pg_database WHERE datname=%s", database):
        raise _recovery_required("postgres", "create", "confirm operation-owned database absence")
    _pg_execute(admin, f"DROP ROLE {_quote_identifier(credential_name)}")
    _pg_execute(admin, f"DROP ROLE {_quote_identifier(owner)}")
    if not _postgres_created_absent(
        admin, database=database, owner=owner, credential_name=credential_name
    ):
        raise _recovery_required(
            "postgres", "create", "confirm operation-owned roles and database absence"
        )


def postgres_create(
    admin: Any,
    *,
    application_id: str,
    host: str,
    connections: int,
    measured_target_bytes: int,
    password_factory: PasswordFactory = _password,
    generation: str,
    operation_id: str | None = None,
) -> ProviderCredential:
    """Create one fixed-quota database and independently rotatable login."""
    connections = _positive(connections, "postgresConnections")
    _positive(measured_target_bytes, "measuredTargetBytes")
    _, database, owner = _identity(application_id)
    username = _credential_name(application_id, generation)
    password = password_factory()
    if not password or "\x00" in password or len(password.encode()) > 1_024:
        raise ValidationError("generated PostgreSQL credential is malformed")
    if operation_id is None:
        raise ValidationError("PostgreSQL creation requires an operation ID")
    operation_id = uuid(operation_id, field="operationId")
    if (
        _pg_exists(admin, "SELECT 1 FROM pg_database WHERE datname=%s", database)
        or _pg_exists(admin, "SELECT 1 FROM pg_roles WHERE rolname=%s", owner)
        or _pg_exists(admin, "SELECT 1 FROM pg_roles WHERE rolname=%s", username)
    ):
        raise HelperActionError(
            "RESOURCE_EXISTS", "PostgreSQL deterministic provider identity exists"
        )
    try:
        _pg_execute(admin, f"CREATE ROLE {_quote_identifier(owner)} NOLOGIN")
        password_literal = "'" + password.replace("'", "''") + "'"
        _pg_execute(
            admin,
            f"CREATE ROLE {_quote_identifier(username)} LOGIN PASSWORD {password_literal} CONNECTION LIMIT {connections}",
        )
        _pg_execute(
            admin,
            f"COMMENT ON ROLE {_quote_identifier(owner)} IS "
            f"{_sql_literal(_postgres_marker(operation_id, generation, 'owner'))}",
        )
        _pg_execute(
            admin,
            f"COMMENT ON ROLE {_quote_identifier(username)} IS "
            f"{_sql_literal(_postgres_marker(operation_id, generation, 'credential'))}",
        )
        _pg_execute(admin, f"GRANT {_quote_identifier(owner)} TO {_quote_identifier(username)}")
        _pg_execute(admin, f"ALTER ROLE {_quote_identifier(username)} SET statement_timeout='30s'")
        _pg_execute(
            admin,
            f"ALTER ROLE {_quote_identifier(username)} SET idle_in_transaction_session_timeout='60s'",
        )
        _pg_execute(admin, f"ALTER ROLE {_quote_identifier(username)} SET lock_timeout='5s'")
        _pg_execute(admin, f"ALTER ROLE {_quote_identifier(username)} SET temp_file_limit='256MB'")
        _pg_execute(
            admin,
            f"CREATE DATABASE {_quote_identifier(database)} OWNER {_quote_identifier(owner)} CONNECTION LIMIT {connections}",
        )
        # A new database inherits template1's ACL, which grants CONNECT to
        # PUBLIC. Every other application role could then open this database
        # and read its catalogues: schema, table, and column names. Table data
        # stays unreadable without a grant, but the shape of one project should
        # not be visible to the rest. The owner keeps its own CONNECT, and the
        # application role holds the owner role, so this revoke costs the
        # application nothing.
        _pg_execute(
            admin,
            f"REVOKE CONNECT ON DATABASE {_quote_identifier(database)} FROM PUBLIC",
        )
        size_row = _pg_execute(admin, "SELECT pg_database_size(%s)", (database,)).fetchone()
        if size_row is None or len(size_row) < 1 or isinstance(size_row[0], bool):
            raise _recovery_required(
                "postgres", "create", "inspect the operation-owned database size"
            )
        baseline_size = int(size_row[0])
        if baseline_size < 0:
            raise _recovery_required(
                "postgres", "create", "inspect the operation-owned database size"
            )
        database_marker = (
            f"{_postgres_marker(operation_id, generation, 'database')}:size={baseline_size}"
        )
        _pg_execute(
            admin,
            f"COMMENT ON DATABASE {_quote_identifier(database)} IS {_sql_literal(database_marker)}",
        )
    except Exception:
        try:
            _postgres_remove_created(
                admin,
                application_id=application_id,
                operation_id=operation_id,
                generation=generation,
            )
        except HelperActionError:
            raise _recovery_required(
                "postgres",
                "create",
                "inspect the operation-owned roles and database before cleanup",
            ) from None
        except Exception:
            raise _recovery_required(
                "postgres", "create", "confirm operation-owned roles and database cleanup"
            ) from None
        raise HelperActionError(
            "CREATE_ROLLED_BACK", "storage creation failed and rollback was confirmed"
        ) from None
    credential = ProviderCredential(
        database, database, username, postgres_environment(host, database, username, password)
    )
    _require_postgres_identity(credential, application_id=application_id, host=host)
    return credential


def _require_postgres_identity(
    credential: ProviderCredential,
    *,
    application_id: str | None = None,
    host: str,
) -> None:
    """Validate every non-secret projection of a PostgreSQL credential."""
    try:
        expected_database = (
            _identity(application_id)[1] if application_id is not None else credential.provider_name
        )
        environment = credential.environment
        if (
            credential.provider_id != expected_database
            or credential.provider_name != expected_database
            or environment["PGDATABASE"] != expected_database
            or environment["PGPORT"] != "5432"
            or environment["PGUSER"] != credential.credential_name
            or not isinstance(host, str)
            or not host
            or "\x00" in host
            or environment["PGHOST"] != host
            or environment["PGSSLMODE"] != "verify-full"
        ):
            raise ValueError
        parsed = urllib.parse.urlsplit(environment["DATABASE_URL"])
        if (
            parsed.scheme != "postgresql"
            or parsed.hostname != environment["PGHOST"]
            or parsed.port != 5432
            or urllib.parse.unquote(parsed.path.removeprefix("/")) != expected_database
            or urllib.parse.unquote(parsed.username or "") != credential.credential_name
            or urllib.parse.unquote(parsed.password or "") != environment["PGPASSWORD"]
        ):
            raise ValueError
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if query.get("sslmode") != ["verify-full"] or query.get("sslrootcert") != [
            environment["PGSSLROOTCERT"]
        ]:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HelperActionError(
            "IDENTITY_MISMATCH", "PostgreSQL credential identity does not match"
        ) from None


def postgres_verify(
    scoped_connect: Callable[..., Any],
    credential: ProviderCredential,
    *,
    host: str,
) -> None:
    _require_postgres_identity(credential, host=host)
    values = credential.environment
    connection = scoped_connect(
        host=values["PGHOST"],
        port=int(values["PGPORT"]),
        dbname=values["PGDATABASE"],
        user=values["PGUSER"],
        password=values["PGPASSWORD"],
        sslmode="verify-full",
    )
    created = False
    try:
        _pg_execute(connection, "CREATE TEMP TABLE platform_access_check (value integer)")
        created = True
        _pg_execute(connection, "INSERT INTO platform_access_check (value) VALUES (1)")
        row = _pg_execute(connection, "SELECT value FROM platform_access_check").fetchone()
        if row is None or row[0] != 1:
            raise RuntimeError("PostgreSQL scoped verification failed")
    finally:
        try:
            if created:
                _pg_execute(connection, "DROP TABLE IF EXISTS platform_access_check")
        finally:
            connection.close()


def postgres_rotate(
    admin: Any,
    *,
    application_id: str,
    host: str,
    connections: int,
    old_environment: Mapping[str, str],
    password_factory: PasswordFactory = _password,
    generation: str,
) -> ProviderCredential:
    """Create a second login; the old login remains valid until evidence passes."""
    connections = _positive(connections, "postgresConnections")
    application_id, database, owner = _identity(application_id)
    old_name = old_environment.get("PGUSER")
    if not isinstance(old_name, str):
        raise HelperActionError("IDENTITY_MISMATCH", "PostgreSQL provider identity does not match")
    _require_postgres_identity(
        ProviderCredential(database, database, old_name, SecretItems(dict(old_environment))),
        application_id=application_id,
        host=host,
    )
    username = _credential_name(application_id, generation)
    password = password_factory()
    if not password or "\x00" in password or len(password.encode()) > 1_024:
        raise ValidationError("generated PostgreSQL credential is malformed")
    password_literal = "'" + password.replace("'", "''") + "'"
    _pg_execute(
        admin,
        f"CREATE ROLE {_quote_identifier(username)} LOGIN PASSWORD {password_literal} CONNECTION LIMIT {connections}",
    )
    try:
        _pg_execute(admin, f"GRANT {_quote_identifier(owner)} TO {_quote_identifier(username)}")
    except Exception:
        _pg_execute(admin, f"DROP ROLE {_quote_identifier(username)}")
        raise
    credential = ProviderCredential(
        database, database, username, postgres_environment(host, database, username, password)
    )
    _require_postgres_identity(credential, application_id=application_id, host=host)
    return credential


def postgres_retire(admin: Any, credential_name: str) -> None:
    if not re.fullmatch(r"u_[a-f0-9]{20}_[a-f0-9]{8}", credential_name):
        raise HelperActionError(
            "IDENTITY_MISMATCH", "PostgreSQL credential identity does not match"
        )
    _pg_execute(admin, f"DROP ROLE {_quote_identifier(credential_name)}")


def postgres_remove(admin: Any, *, application_id: str) -> None:
    _, database, owner = _identity(application_id)
    _pg_execute(admin, f"DROP DATABASE IF EXISTS {_quote_identifier(database)} WITH (FORCE)")
    rows = _pg_execute(
        admin,
        "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
        (f"u_{application_id.replace('-', '')[:20]}_%",),
    ).fetchall()
    for row in rows:
        _pg_execute(admin, f"DROP ROLE {_quote_identifier(row[0])}")
    _pg_execute(admin, f"DROP ROLE IF EXISTS {_quote_identifier(owner)}")


def postgres_absent(admin: Any, *, application_id: str) -> bool:
    _, database, owner = _identity(application_id)
    users = _pg_execute(
        admin,
        "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
        (f"u_{application_id.replace('-', '')[:20]}_%",),
    ).fetchall()
    return (
        not _pg_exists(admin, "SELECT 1 FROM pg_database WHERE datname=%s", database)
        and not _pg_exists(admin, "SELECT 1 FROM pg_roles WHERE rolname=%s", owner)
        and not users
    )


def mongo_environment(
    host: str,
    database: str,
    username: str,
    password: str,
    *,
    ca_path: str = APPLICATION_CA_PATH,
) -> SecretItems:
    user = urllib.parse.quote(username, safe="")
    secret = urllib.parse.quote(password, safe="")
    ca = urllib.parse.quote(ca_path, safe="")
    return SecretItems(
        {
            "MONGODB_URI": (
                f"mongodb://{user}:{secret}@{host}:27017/{database}"
                f"?authSource={database}&tls=true&tlsCAFile={ca}"
            )
        }
    )


_MONGO_OWNER_FIELD = "m1PlatformOwner"
_MONGO_OPERATION_FIELD = "m1OperationId"
_MONGO_GENERATION_FIELD = "m1CredentialGeneration"


def _mongo_users(database_client: Any) -> list[Mapping[str, Any]]:
    response = database_client.command("usersInfo")
    if not isinstance(response, Mapping) or not isinstance(response.get("users"), list):
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "MongoDB user inventory is invalid")
    users = response["users"]
    if any(not isinstance(item, Mapping) for item in users):
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "MongoDB user inventory is invalid")
    return [cast(Mapping[str, Any], item) for item in users]


def _mongo_collections(database_client: Any) -> list[str]:
    listing = getattr(database_client, "list_collection_names", None)
    if not callable(listing):
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "MongoDB collection inventory is unavailable"
        )
    names = listing()
    if (
        not isinstance(names, Sequence)
        or isinstance(names, (str, bytes))
        or any(not isinstance(name, str) or not name for name in names)
    ):
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "MongoDB collection inventory is invalid"
        )
    return list(names)


def _mongo_owner_matches(
    users: Sequence[Mapping[str, Any]],
    *,
    application_id: str,
    credential_name: str | None = None,
    operation_id: str | None = None,
    generation: str | None = None,
) -> bool:
    matches = []
    for user in users:
        if credential_name is not None and user.get("user") != credential_name:
            continue
        custom = user.get("customData")
        if not isinstance(custom, Mapping):
            continue
        if custom.get(_MONGO_OWNER_FIELD) != application_id:
            continue
        if operation_id is not None and custom.get(_MONGO_OPERATION_FIELD) != operation_id:
            continue
        if generation is not None and custom.get(_MONGO_GENERATION_FIELD) != generation:
            continue
        matches.append(user)
    return len(matches) == 1


def _mongo_creation_evidence(
    users: Sequence[Mapping[str, Any]],
    *,
    application_id: str,
    operation_id: str,
    generation: str,
) -> str:
    """Return the sole operation-owned login, rejecting foreign users."""
    identifier = uuid(application_id, field="applicationId")
    operation_id = uuid(operation_id, field="operationId")
    username = _credential_name(identifier, generation)
    matches = [
        user
        for user in users
        if user.get("user") == username
        and isinstance(user.get("customData"), Mapping)
        and user["customData"].get(_MONGO_OWNER_FIELD) == identifier
        and user["customData"].get(_MONGO_OPERATION_FIELD) == operation_id
        and user["customData"].get(_MONGO_GENERATION_FIELD) == generation
    ]
    if len(matches) != 1 or len(users) != 1:
        raise _recovery_required(
            "mongo", "create", "inspect the operation-owned user marker and database users"
        )
    return username


def _mongo_remove_created(
    admin: Any,
    *,
    application_id: str,
    operation_id: str,
    generation: str,
) -> None:
    identifier, database, _ = _identity(application_id)
    database_client = admin[database]
    users = _mongo_users(database_client)
    _mongo_creation_evidence(
        users,
        application_id=identifier,
        operation_id=operation_id,
        generation=generation,
    )
    collections = _mongo_collections(database_client)
    if collections:
        raise _recovery_required(
            "mongo", "create", "inspect operation-owned database data before cleanup"
        )
    username = _credential_name(identifier, generation)
    database_client.command("dropUser", username)
    admin.drop_database(database)
    if not mongo_absent(admin, application_id=identifier):
        raise _recovery_required("mongo", "create", "confirm operation-owned database absence")


def mongo_create(
    admin: Any,
    *,
    application_id: str,
    host: str,
    measured_target_bytes: int,
    password_factory: PasswordFactory = _password,
    generation: str,
    operation_id: str | None = None,
) -> ProviderCredential:
    _positive(measured_target_bytes, "measuredTargetBytes")
    application_id, database, _ = _identity(application_id)
    if operation_id is not None:
        operation_id = uuid(operation_id, field="operationId")
    username = _credential_name(application_id, generation)
    password = password_factory()
    if not password or "\x00" in password or len(password.encode()) > 1_024:
        raise ValidationError("generated MongoDB credential is malformed")
    database_client = admin[database]
    # A deterministic database is greenfield-owned only when the provider
    # proves it had no database entry, collections, data, or users before this
    # operation.  In particular, never turn an existing database into an M1
    # resource merely because its usersInfo list is empty.
    try:
        database_names = admin.list_database_names()
    except AttributeError:
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "MongoDB database inventory is unavailable"
        ) from None
    if (
        not isinstance(database_names, Sequence)
        or isinstance(database_names, (str, bytes))
        or any(not isinstance(name, str) for name in database_names)
    ):
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "MongoDB database inventory is invalid"
        )
    users = _mongo_users(database_client)
    collections = _mongo_collections(database_client)
    if database in database_names or users or collections:
        raise HelperActionError(
            "RESOURCE_EXISTS", "deterministic MongoDB database already contains provider state"
        )
    custom_data = {_MONGO_OWNER_FIELD: application_id, _MONGO_GENERATION_FIELD: generation}
    if operation_id is not None:
        custom_data[_MONGO_OPERATION_FIELD] = operation_id
    database_client.command(
        "createUser",
        username,
        pwd=password,
        roles=[{"role": "readWrite", "db": database}],
        customData=custom_data,
    )
    # A concurrent writer between the preflight and createUser is not owned by
    # this operation.  Leave the provider marker for explicit recovery rather
    # than dropping a database whose data or foreign user appeared during the race.
    users_after = _mongo_users(database_client)
    _mongo_creation_evidence(
        users_after,
        application_id=application_id,
        operation_id=operation_id or "00000000-0000-4000-8000-000000000000",
        generation=generation,
    )
    if _mongo_collections(database_client):
        raise _recovery_required("mongo", "create", "inspect concurrent collections before cleanup")
    return ProviderCredential(
        database, database, username, mongo_environment(host, database, username, password)
    )


def _require_mongo_identity(
    credential: ProviderCredential,
    *,
    application_id: str | None = None,
    host: str,
) -> None:
    """Validate the canonical Mongo URI before it can be used or retired."""
    try:
        expected_database = (
            _identity(application_id)[1] if application_id is not None else credential.provider_name
        )
        environment = credential.environment
        if (
            credential.provider_id != expected_database
            or credential.provider_name != expected_database
        ):
            raise ValueError
        parsed = urllib.parse.urlsplit(environment["MONGODB_URI"])
        if parsed.scheme != "mongodb":
            raise ValueError
        if not isinstance(host, str) or not host or "\x00" in host or parsed.hostname != host:
            raise ValueError
        if parsed.port != 27017:
            raise ValueError
        if urllib.parse.unquote(parsed.path.removeprefix("/")) != expected_database:
            raise ValueError
        if urllib.parse.unquote(parsed.username or "") != credential.credential_name:
            raise ValueError
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if query.get("authSource") != [expected_database] or query.get("tls") != ["true"]:
            raise ValueError
        if query.get("tlsCAFile") != [APPLICATION_CA_PATH]:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HelperActionError(
            "IDENTITY_MISMATCH", "MongoDB credential identity does not match"
        ) from None


def mongo_verify(
    scoped_connect: Callable[..., Any],
    credential: ProviderCredential,
    *,
    host: str,
) -> None:
    _require_mongo_identity(credential, host=host)
    values = credential.environment
    client = scoped_connect(uri=values["MONGODB_URI"])
    collection = client[credential.provider_name][f"platform_access_check_{secrets.token_hex(8)}"]
    try:
        inserted = collection.insert_one({"value": 1})
        document = collection.find_one({"_id": inserted.inserted_id})
        if not document or document.get("value") != 1:
            raise RuntimeError("MongoDB scoped verification failed")
    finally:
        try:
            collection.drop()
        finally:
            client.close()


def _mongo_old_username(uri: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(uri)
        if (
            parsed.scheme != "mongodb"
            or parsed.username is None
            or parsed.hostname is None
            or parsed.port != 27017
            or not parsed.path.startswith("/")
        ):
            raise ValueError
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if query.get("authSource") != [urllib.parse.unquote(parsed.path.removeprefix("/"))]:
            raise ValueError
        return urllib.parse.unquote(parsed.username)
    except (TypeError, ValueError):
        raise HelperActionError(
            "VARIABLE_MALFORMED", "MongoDB Variable value is malformed"
        ) from None


def mongo_rotate(
    admin: Any,
    *,
    application_id: str,
    host: str,
    old_environment: Mapping[str, str],
    password_factory: PasswordFactory = _password,
    generation: str,
    operation_id: str | None = None,
) -> ProviderCredential:
    application_id, database, _ = _identity(application_id)
    old_name = _mongo_old_username(old_environment["MONGODB_URI"])
    _require_mongo_identity(
        ProviderCredential(database, database, old_name, SecretItems(dict(old_environment))),
        application_id=application_id,
        host=host,
    )
    if operation_id is not None:
        operation_id = uuid(operation_id, field="operationId")
    username = _credential_name(application_id, generation)
    password = password_factory()
    if not password or "\x00" in password or len(password.encode()) > 1_024:
        raise ValidationError("generated MongoDB credential is malformed")
    custom_data = {_MONGO_OWNER_FIELD: application_id, _MONGO_GENERATION_FIELD: generation}
    if operation_id is not None:
        custom_data[_MONGO_OPERATION_FIELD] = operation_id
    admin[database].command(
        "createUser",
        username,
        pwd=password,
        roles=[{"role": "readWrite", "db": database}],
        customData=custom_data,
    )
    credential = ProviderCredential(
        database, database, username, mongo_environment(host, database, username, password)
    )
    _require_mongo_identity(credential, application_id=application_id, host=host)
    return credential


def mongo_retire(admin: Any, database: str, credential_name: str) -> None:
    if not re.fullmatch(r"u_[a-f0-9]{20}_[a-f0-9]{8}", credential_name):
        raise HelperActionError("IDENTITY_MISMATCH", "MongoDB credential identity does not match")
    admin[database].command("dropUser", credential_name)


def mongo_remove(
    admin: Any,
    *,
    application_id: str,
    credential_name: str | None = None,
    operation_id: str | None = None,
    generation: str | None = None,
) -> None:
    """Drop only a database bearing this platform's ownership marker."""
    application_id, database, _ = _identity(application_id)
    if operation_id is not None:
        operation_id = uuid(operation_id, field="operationId")
    database_client = admin[database]
    users = _mongo_users(database_client)
    if not _mongo_owner_matches(
        users,
        application_id=application_id,
        credential_name=credential_name,
        operation_id=operation_id,
        generation=generation,
    ):
        raise _recovery_required(
            "mongo", "remove", "inspect the deterministic database ownership marker before deletion"
        )
    admin.drop_database(database)


def mongo_absent(admin: Any, *, application_id: str) -> bool:
    _, database, _ = _identity(application_id)
    names = admin.list_database_names()
    if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "MongoDB database inventory is invalid"
        )
    database_client = admin[database]
    return (
        database not in names
        and not _mongo_users(database_client)
        and not _mongo_collections(database_client)
    )


def s3_environment(endpoint: str, bucket: str, access_key: str, secret_key: str) -> SecretItems:
    return SecretItems(
        {
            "AWS_ENDPOINT_URL_S3": endpoint,
            "AWS_REGION": "garage",
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
            "AWS_CA_BUNDLE": APPLICATION_CA_PATH,
            "S3_ENDPOINT": endpoint,
            "S3_BUCKET": bucket,
            "S3_FORCE_PATH_STYLE": "true",
        }
    )


def _s3_bucket_name(application_id: str, application_slug: str, prefix: str) -> str:
    identifier = uuid(application_id, field="applicationId")
    checked_slug = slug(application_slug)
    suffix = identifier.replace("-", "")[:8]
    slug_limit = 63 - len(prefix) - len(suffix) - 2
    if slug_limit < 3:
        raise ValidationError("platform prefix is too long for an S3 bucket name")
    return f"{prefix}-{checked_slug[:slug_limit].rstrip('-')}-{suffix}"


def _retire_owned_s3_key(admin: Any, *, key_id: str, expected_name: str) -> None:
    record = _s3_key_record(admin, key_id)
    if record is not None and record.get("name") != expected_name:
        raise _recovery_required("s3", "cleanup", "inspect the operation-owned access-key name")
    admin.request("/DeleteKey", {}, {"id": key_id})
    if _s3_key_present(admin, key_id):
        raise _recovery_required("s3", "cleanup", "confirm operation-owned access-key absence")


def s3_create(
    admin: Any,
    *,
    application_id: str,
    application_slug: str,
    prefix: str,
    endpoint: str,
    bytes_quota: int,
    objects_quota: int,
    generation: str,
    scoped_client: Callable[..., Any] | None = None,
) -> ProviderCredential:
    _positive(bytes_quota, "s3Bytes")
    _positive(objects_quota, "s3Objects")
    identifier = uuid(application_id, field="applicationId")
    application_slug = slug(application_slug)
    bucket = _s3_bucket_name(identifier, application_slug, prefix)
    bucket_info = key_info = None
    key_name: str | None = None
    try:
        if _s3_bucket_id(admin, bucket) is not None:
            raise HelperActionError("RESOURCE_EXISTS", "Garage bucket already exists")
        bucket_info = admin.request("/CreateBucket", {"globalAlias": bucket})
        if (
            not isinstance(bucket_info, Mapping)
            or not isinstance(bucket_info.get("id"), str)
            or not bucket_info["id"]
        ):
            raise HelperActionError(
                "PROVIDER_RESPONSE_INVALID", "Garage bucket identity is invalid"
            )
        if not re.fullmatch(r"[a-f0-9]{8}", generation):
            raise ValidationError("credential generation is malformed")
        key_name = f"application-{application_slug}-{generation}"
        if _s3_key_by_name(admin, key_name) is not None:
            raise HelperActionError("RESOURCE_EXISTS", "Garage operation key already exists")
        key_info = admin.request("/CreateKey", {"name": key_name, "neverExpires": True})
        if (
            not isinstance(key_info, Mapping)
            or not isinstance(key_info.get("accessKeyId"), str)
            or not isinstance(key_info.get("secretAccessKey"), str)
        ):
            # A partial CreateKey response is not proof that a returned or
            # same-named key belongs to this attempt.  Leave it for explicit
            # recovery rather than deleting by deterministic name.
            raise _recovery_required(
                "s3", "create", "inspect the exact operation-owned access-key response"
            )
        admin.request(
            "/UpdateBucket",
            {"quotas": {"maxSize": bytes_quota, "maxObjects": objects_quota}},
            {"id": bucket_info["id"]},
        )
        admin.request(
            "/AllowBucketKey",
            {
                "bucketId": bucket_info["id"],
                "accessKeyId": key_info["accessKeyId"],
                "permissions": {"read": True, "write": True, "owner": False},
            },
        )
        info = _s3_bucket_info(admin, provider_id=bucket_info["id"])
        if info is None:
            raise HelperActionError(
                "ABSENCE_UNCONFIRMED", "Garage bucket disappeared after creation"
            )
        _require_s3_bucket_identity(
            info,
            provider_id=bucket_info["id"],
            provider_name=bucket,
            access_key_id=key_info["accessKeyId"],
        )
    except Exception as original_error:
        if (
            isinstance(original_error, HelperActionError)
            and original_error.code == "RESOURCE_EXISTS"
        ):
            raise
        candidate_bucket_id = bucket_info.get("id") if isinstance(bucket_info, Mapping) else None
        candidate_id = key_info.get("accessKeyId") if isinstance(key_info, Mapping) else None
        exact_response = (
            isinstance(candidate_bucket_id, str)
            and isinstance(candidate_id, str)
            and isinstance(key_info, Mapping)
            and isinstance(key_info.get("secretAccessKey"), str)
            and key_name is not None
            and scoped_client is not None
        )
        if exact_response:
            assert isinstance(candidate_bucket_id, str)
            assert isinstance(candidate_id, str)
            assert isinstance(key_info, Mapping)
            assert isinstance(key_info.get("secretAccessKey"), str)
            assert key_name is not None
            assert scoped_client is not None
            credential = ProviderCredential(
                candidate_bucket_id,
                bucket,
                candidate_id,
                s3_environment(endpoint, bucket, candidate_id, key_info["secretAccessKey"]),
            )
            try:
                _s3_remove_created(
                    admin,
                    scoped_client,
                    credential=credential,
                    expected_key_name=key_name,
                    expected_endpoint=endpoint,
                )
            except Exception:
                raise _recovery_required(
                    "s3", "create", "confirm operation-owned bucket, data, and access-key cleanup"
                ) from None
            raise HelperActionError(
                "CREATE_ROLLED_BACK", "storage creation failed and rollback was confirmed"
            ) from None
        # A lost or malformed provider response is not ownership evidence.  In
        # particular, never rediscover a bucket/key by deterministic name and
        # delete it after an ambiguous CreateBucket/CreateKey attempt.
        raise _recovery_required(
            "s3", "create", "inspect the exact operation-owned bucket and access-key response"
        ) from None
    return ProviderCredential(
        bucket_info["id"],
        bucket,
        key_info["accessKeyId"],
        s3_environment(endpoint, bucket, key_info["accessKeyId"], key_info["secretAccessKey"]),
    )


def s3_verify(
    scoped_client: Callable[..., Any],
    credential: ProviderCredential,
    *,
    endpoint: str,
) -> None:
    values = credential.environment
    _require_s3_endpoint(values, endpoint)
    if (
        not credential.provider_id
        or credential.provider_name != values.get("S3_BUCKET")
        or values.get("AWS_ACCESS_KEY_ID") != credential.credential_name
        or values.get("AWS_REGION") != "garage"
        or values.get("S3_FORCE_PATH_STYLE") != "true"
        or values.get("AWS_CA_BUNDLE") != APPLICATION_CA_PATH
    ):
        raise HelperActionError("IDENTITY_MISMATCH", "S3 credential identity does not match")
    client = scoped_client(values["AWS_ACCESS_KEY_ID"], values["AWS_SECRET_ACCESS_KEY"])
    key = f".platform-access-check-{secrets.token_hex(8)}"
    body: Any = None
    try:
        client.put_object(Bucket=credential.provider_name, Key=key, Body=b"ok")
        body = client.get_object(Bucket=credential.provider_name, Key=key)["Body"]
        if body.read() != b"ok":
            raise RuntimeError("S3 scoped verification failed")
    finally:
        try:
            if body is not None:
                body.close()
        finally:
            try:
                client.delete_object(Bucket=credential.provider_name, Key=key)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()


def s3_rotate(
    admin: Any,
    *,
    provider_id: str,
    provider_name: str,
    endpoint: str,
    generation: str,
) -> ProviderCredential:
    if not re.fullmatch(r"[a-f0-9]{8}", generation):
        raise ValidationError("credential generation is malformed")
    key_name = f"application-{provider_name}-{generation}"
    try:
        key = admin.request("/CreateKey", {"name": key_name, "neverExpires": True})
    except Exception:
        # A lost CreateKey response does not prove that a same-named key is
        # operation-owned; never delete by name on that ambiguity.
        raise _recovery_required(
            "s3", "rotate", "inspect the candidate access key after creation"
        ) from None
    if (
        not isinstance(key, Mapping)
        or not isinstance(key.get("accessKeyId"), str)
        or not isinstance(key.get("secretAccessKey"), str)
    ):
        candidate_id = key.get("accessKeyId", key.get("id")) if isinstance(key, Mapping) else None
        if not isinstance(candidate_id, str):
            candidate_id = _s3_key_by_name(admin, key_name)
        if not isinstance(candidate_id, str):
            raise _recovery_required(
                "s3", "rotate", "inspect the candidate access key after creation"
            )
        _retire_owned_s3_key(admin, key_id=candidate_id, expected_name=key_name)
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "Garage access-key identity is invalid"
        )
    try:
        admin.request(
            "/AllowBucketKey",
            {
                "bucketId": provider_id,
                "accessKeyId": key["accessKeyId"],
                "permissions": {"read": True, "write": True, "owner": False},
            },
        )
        info = _s3_bucket_info(admin, provider_id=provider_id)
        if info is None:
            raise _recovery_required(
                "s3", "rotate", "inspect the bucket after candidate key creation"
            )
        _require_s3_bucket_identity(
            info,
            provider_id=provider_id,
            provider_name=provider_name,
            access_key_id=key["accessKeyId"],
        )
    except Exception:
        try:
            admin.request("/DeleteKey", {}, {"id": key["accessKeyId"]})
            if _s3_key_present(admin, key["accessKeyId"]):
                raise _recovery_required(
                    "s3", "rotate", "remove the operation-owned candidate access key"
                )
        except Exception as cleanup_error:
            if isinstance(cleanup_error, HelperActionError):
                raise
            raise _recovery_required(
                "s3", "rotate", "remove the operation-owned candidate access key"
            ) from None
        raise
    return ProviderCredential(
        provider_id,
        provider_name,
        key["accessKeyId"],
        s3_environment(endpoint, provider_name, key["accessKeyId"], key["secretAccessKey"]),
    )


def s3_retire(admin: Any, credential_name: str) -> None:
    admin.request("/DeleteKey", {}, {"id": credential_name})


def s3_remove(
    admin: Any,
    scoped_client: Callable[..., Any],
    *,
    provider_id: str,
    provider_name: str,
    environment: Mapping[str, str],
    purge: bool,
) -> None:
    info = _s3_bucket_info(admin, provider_id=provider_id)
    if info is None:
        raise _recovery_required("s3", "remove", "inspect the provider bucket before deletion")
    _require_s3_bucket_identity(
        info,
        provider_id=provider_id,
        provider_name=provider_name,
        access_key_id=environment["AWS_ACCESS_KEY_ID"],
    )
    client = scoped_client(environment["AWS_ACCESS_KEY_ID"], environment["AWS_SECRET_ACCESS_KEY"])
    try:
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=provider_name)
        pending: list[Mapping[str, Any]] = []
        for page in pages:
            if not isinstance(page, Mapping):
                raise HelperActionError(
                    "PROVIDER_RESPONSE_INVALID", "S3 object inventory is invalid"
                )
            objects = page.get("Contents", [])
            if (
                not isinstance(objects, Sequence)
                or isinstance(objects, (str, bytes))
                or any(
                    not isinstance(item, Mapping)
                    or not isinstance(item.get("Key"), str)
                    or not item["Key"]
                    for item in objects
                )
            ):
                raise HelperActionError(
                    "PROVIDER_RESPONSE_INVALID", "S3 object inventory is invalid"
                )
            pending.extend(objects)
        if pending and not purge:
            raise HelperActionError(
                "BUCKET_NOT_EMPTY", "S3 bucket is not empty; explicit purge is required"
            )
        if purge:
            for offset in range(0, len(pending), 1000):
                client.delete_objects(
                    Bucket=provider_name,
                    Delete={
                        "Objects": [
                            {"Key": item["Key"]} for item in pending[offset : offset + 1000]
                        ],
                        "Quiet": True,
                    },
                )
        admin.request("/DeleteBucket", {}, {"id": provider_id})
        admin.request("/DeleteKey", {}, {"id": environment["AWS_ACCESS_KEY_ID"]})
        if _s3_key_present(admin, environment["AWS_ACCESS_KEY_ID"]):
            raise HelperActionError(
                "ABSENCE_UNCONFIRMED", "S3 access-key absence was not confirmed"
            )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _s3_bucket_info(admin: Any, *, provider_id: str) -> Mapping[str, Any] | None:
    try:
        response = admin.request("/GetBucketInfo", None, {"id": provider_id})
    except Exception as error:
        status = getattr(error, "code", None)
        if status == 404 or error.__class__.__name__ in {"NotFound", "NoSuchBucket"}:
            return None
        raise
    if not isinstance(response, Mapping) or response.get("id") != provider_id:
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage bucket identity is invalid")
    return response


def s3_absent(admin: Any, *, provider_id: str) -> bool:
    return _s3_bucket_info(admin, provider_id=provider_id) is None


def _require_s3_bucket_identity(
    info: Mapping[str, Any],
    *,
    provider_id: str,
    provider_name: str,
    access_key_id: str,
) -> None:
    aliases = info.get("globalAliases")
    if (
        info.get("id") != provider_id
        or not isinstance(aliases, Sequence)
        or isinstance(aliases, (str, bytes))
        or any(not isinstance(alias, str) or not alias for alias in aliases)
        or provider_name not in aliases
    ):
        raise HelperActionError(
            "IDENTITY_MISMATCH", "Garage bucket provider ID and alias do not match"
        )
    keys = info.get("keys")
    if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes)):
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "Garage bucket project key inventory is invalid"
        )
    if any(not isinstance(item, Mapping) for item in keys):
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "Garage bucket project key inventory is invalid"
        )
    matches = [item for item in keys if item.get("accessKeyId", item.get("id")) == access_key_id]
    if len(matches) != 1:
        raise HelperActionError(
            "IDENTITY_MISMATCH", "Garage bucket is not attached to the expected project key"
        )
    permissions = matches[0].get("permissions")
    if (
        not isinstance(permissions, Mapping)
        or permissions.get("read") is not True
        or permissions.get("write") is not True
        or permissions.get("owner") is not False
    ):
        raise HelperActionError("IDENTITY_MISMATCH", "Garage project key permissions do not match")


def _require_s3_live_identity(
    admin: Any,
    *,
    provider_id: str,
    provider_name: str,
    access_key_id: str,
) -> Mapping[str, Any]:
    info = _s3_bucket_info(admin, provider_id=provider_id)
    if info is None:
        raise _recovery_required("s3", "observe", "inspect the missing provider bucket identity")
    try:
        _require_s3_bucket_identity(
            info,
            provider_id=provider_id,
            provider_name=provider_name,
            access_key_id=access_key_id,
        )
    except HelperActionError:
        raise HelperActionError(
            "RECOVERY_REQUIRED", "S3 identity does not match; recovery is required"
        ) from None
    return info


def _s3_bucket_has_objects(
    scoped_client: Callable[..., Any],
    provider_name: str,
    environment: Mapping[str, str],
) -> bool:
    client = scoped_client(environment["AWS_ACCESS_KEY_ID"], environment["AWS_SECRET_ACCESS_KEY"])
    try:
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=provider_name)
        for page in pages:
            if not isinstance(page, Mapping):
                raise HelperActionError(
                    "PROVIDER_RESPONSE_INVALID", "S3 object inventory is invalid"
                )
            objects = page.get("Contents", [])
            if not isinstance(objects, Sequence) or isinstance(objects, (str, bytes)):
                raise HelperActionError(
                    "PROVIDER_RESPONSE_INVALID", "S3 object inventory is invalid"
                )
            if objects:
                return True
        return False
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _require_s3_key_identity(admin: Any, *, key_id: str, expected_name: str) -> None:
    record = _s3_key_record(admin, key_id)
    if record is None or record.get("name") != expected_name:
        raise _recovery_required("s3", "create", "inspect the operation-owned access-key name")


def _s3_remove_created(
    admin: Any,
    scoped_client: Callable[..., Any],
    *,
    credential: ProviderCredential,
    expected_key_name: str,
    expected_endpoint: str,
) -> None:
    """Delete a create candidate only after marker, empty-state, and absence checks."""
    info = _s3_bucket_info(admin, provider_id=credential.provider_id)
    if info is not None:
        _require_s3_bucket_identity(
            info,
            provider_id=credential.provider_id,
            provider_name=credential.provider_name,
            access_key_id=credential.credential_name,
        )
        _require_s3_endpoint(credential.environment, expected_endpoint)
        if _s3_bucket_has_objects(scoped_client, credential.provider_name, credential.environment):
            raise _recovery_required(
                "s3", "create", "inspect operation-owned bucket data before cleanup"
            )
        admin.request("/DeleteBucket", {}, {"id": credential.provider_id})
        if not s3_absent(admin, provider_id=credential.provider_id):
            raise _recovery_required("s3", "create", "confirm operation-owned bucket absence")
    _require_s3_key_identity(
        admin, key_id=credential.credential_name, expected_name=expected_key_name
    )
    admin.request("/DeleteKey", {}, {"id": credential.credential_name})
    if _s3_key_present(admin, credential.credential_name):
        raise _recovery_required("s3", "create", "confirm operation-owned access-key absence")


def _s3_bucket_id(admin: Any, provider_name: str) -> str | None:
    response = admin.request("/ListBuckets")
    buckets: object = response
    if isinstance(response, Mapping):
        buckets = response.get("buckets", response.get("items", []))
    if not isinstance(buckets, Sequence) or isinstance(buckets, (str, bytes)):
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage bucket inventory is invalid")
    matches: list[str] = []
    for item in buckets:
        if not isinstance(item, Mapping):
            continue
        aliases = item.get("globalAliases", [])
        names: list[object] = []
        if (
            not isinstance(aliases, Sequence)
            or isinstance(aliases, (str, bytes))
            or any(not isinstance(alias, str) or not alias for alias in aliases)
        ):
            raise HelperActionError(
                "PROVIDER_RESPONSE_INVALID", "Garage bucket alias inventory is invalid"
            )
        names.extend(aliases)
        if provider_name not in names:
            continue
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier:
            matches.append(identifier)
    if len(matches) > 1:
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage bucket identity is ambiguous")
    return matches[0] if matches else None


def _s3_expected_bucket_id(
    admin: Any, application_id: str, application_slug: str, prefix: str
) -> str | None:
    return _s3_bucket_id(admin, _s3_bucket_name(application_id, application_slug, prefix))


def _s3_key_inventory(admin: Any) -> Sequence[object]:
    response = admin.request("/ListKeys")
    keys: object = response
    if isinstance(response, Mapping):
        keys = response.get("keys", response.get("items", []))
    if (
        not isinstance(keys, Sequence)
        or isinstance(keys, (str, bytes))
        or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("accessKeyId", item.get("id")), str)
            or not item.get("accessKeyId", item.get("id"))
            for item in keys
        )
    ):
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage key inventory is invalid")
    return keys


def _s3_key_present(admin: Any, key_id: str) -> bool:
    for item in _s3_key_inventory(admin):
        if isinstance(item, Mapping) and item.get("accessKeyId", item.get("id")) == key_id:
            return True
    return False


def _s3_key_record(admin: Any, key_id: str) -> Mapping[str, Any] | None:
    matches = [
        item
        for item in _s3_key_inventory(admin)
        if isinstance(item, Mapping) and item.get("accessKeyId", item.get("id")) == key_id
    ]
    if len(matches) > 1:
        raise HelperActionError(
            "PROVIDER_RESPONSE_INVALID", "Garage access-key identity is ambiguous"
        )
    return matches[0] if matches else None


def _require_s3_orphan_key_identity(
    admin: Any, *, key_id: str, application_slug: str, provider_name: str
) -> None:
    record = _s3_key_record(admin, key_id)
    if record is None:
        return
    name = record.get("name")
    if (
        not isinstance(name, str)
        or not (
            name.startswith(f"application-{application_slug}-")
            or name.startswith(f"application-{provider_name}-")
        )
        or re.fullmatch(r"application-[A-Za-z0-9._-]+-[a-f0-9]{8}", name) is None
    ):
        raise _recovery_required(
            "s3", "remove", "inspect the accepted access-key name after bucket loss"
        )


def _s3_key_by_name(admin: Any, expected_name: str) -> str | None:
    matches: list[str] = []
    for item in _s3_key_inventory(admin):
        if not isinstance(item, Mapping) or item.get("name") != expected_name:
            continue
        key_id = item.get("accessKeyId", item.get("id"))
        if isinstance(key_id, str) and key_id:
            matches.append(key_id)
    if len(matches) > 1:
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage candidate key is ambiguous")
    return matches[0] if matches else None


def _s3_rotation_keys(
    admin: Any,
    provider_name: str,
    candidate_display_name: str,
    application_slug: str | None = None,
) -> tuple[str | None, list[str]]:
    keys = _s3_key_inventory(admin)
    candidate: list[str] = []
    old: list[str] = []
    for item in keys:
        if not isinstance(item, Mapping):
            raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage key inventory is invalid")
        name = item.get("name")
        key_id = item.get("accessKeyId", item.get("id"))
        if not isinstance(name, str) or not isinstance(key_id, str) or not key_id:
            raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage key inventory is invalid")
        if name == candidate_display_name:
            candidate.append(key_id)
        elif (
            any(
                name.startswith(f"application-{prefix}-")
                for prefix in {provider_name, application_slug}
                if prefix is not None
            )
            and re.fullmatch(r"[a-f0-9]{8}", name.rsplit("-", 1)[-1]) is not None
        ):
            old.append(key_id)
    if len(candidate) > 1:
        raise HelperActionError("PROVIDER_RESPONSE_INVALID", "Garage candidate key is ambiguous")
    return (candidate[0] if candidate else None), old


def _ensure_owned_absent(nomad: VariableClient, application_slug: str, resource_type: str) -> None:
    keys = RESOURCE_KEYS[resource_type]
    snapshot = nomad.read_variable(variable_path(application_slug))
    if any(key in snapshot.items for key in keys):
        raise HelperActionError(
            "RESOURCE_EXISTS", f"{resource_type} environment keys already exist"
        )


def _owned_environment(
    nomad: VariableClient, application_slug: str, resource_type: str
) -> SecretItems:
    keys = RESOURCE_KEYS[resource_type]
    snapshot = nomad.read_variable(variable_path(application_slug))
    missing = [key for key in keys if key not in snapshot.items]
    if missing:
        raise HelperActionError(
            "VARIABLE_INCOMPLETE", f"{resource_type} environment keys are incomplete"
        )
    return SecretItems({key: snapshot.items[key] for key in keys})


def _owned_observation(
    nomad: VariableClient, application_slug: str, resource_type: str
) -> tuple[VariableSnapshot, str, SecretItems | None]:
    """Observe key completeness and ModifyIndex without returning any value."""
    snapshot = nomad.read_variable(variable_path(application_slug))
    present = [key for key in RESOURCE_KEYS[resource_type] if key in snapshot.items]
    if not present:
        return snapshot, "absent", None
    if len(present) != len(RESOURCE_KEYS[resource_type]):
        return snapshot, "partial", None
    environment = SecretItems({key: snapshot.items[key] for key in RESOURCE_KEYS[resource_type]})
    return snapshot, "complete", environment


def _environment_credential_name(resource_type: str, environment: Mapping[str, str]) -> str:
    if resource_type == "postgres":
        return environment["PGUSER"]
    if resource_type == "mongo":
        return _mongo_old_username(environment["MONGODB_URI"])
    return environment["AWS_ACCESS_KEY_ID"]


def _recovery_required(resource_type: str, operation: str, action: str) -> HelperActionError:
    return HelperActionError(
        "RECOVERY_REQUIRED",
        f"{resource_type} {operation} is ambiguous; exact action: {action}",
    )


def _publish(
    nomad: VariableClient,
    application_slug: str,
    resource_type: str,
    environment: Mapping[str, str],
) -> VariableUpdate:
    keys = RESOURCE_KEYS[resource_type]
    return update_owned_items(
        nomad,
        variable_path(application_slug),
        {key: _OWNER[resource_type] for key in keys},
        owner=_OWNER[resource_type],
        updates=environment,
    )


def _remove_environment(
    nomad: VariableClient, application_slug: str, resource_type: str
) -> VariableUpdate:
    keys = RESOURCE_KEYS[resource_type]
    return update_owned_items(
        nomad,
        variable_path(application_slug),
        {key: _OWNER[resource_type] for key in keys},
        owner=_OWNER[resource_type],
        removals=keys,
    )


def _raise_create_failure(
    nomad: VariableClient,
    application_slug: str,
    resource_type: str,
    credential: ProviderCredential,
    *,
    remove_provider: Callable[[], None],
    provider_absent: Callable[[], bool],
) -> NoReturn:
    """Confirm rollback of only the operation-owned Variable keys/provider."""
    rolled_back = True
    try:
        _snapshot, key_state, environment = _owned_observation(
            nomad, application_slug, resource_type
        )
        if key_state == "complete" and environment is not None:
            if (
                _environment_credential_name(resource_type, environment)
                != credential.credential_name
            ):
                rolled_back = False
            else:
                _remove_environment(nomad, application_slug, resource_type)
        elif key_state != "absent":
            rolled_back = False
        _snapshot, key_state, _environment = _owned_observation(
            nomad, application_slug, resource_type
        )
        rolled_back = rolled_back and key_state == "absent"
    except Exception:
        rolled_back = False
    try:
        if not provider_absent():
            remove_provider()
        rolled_back = rolled_back and provider_absent()
    except Exception:
        rolled_back = False
    if rolled_back:
        raise HelperActionError(
            "CREATE_ROLLED_BACK", "storage creation failed and rollback was confirmed"
        ) from None
    raise _recovery_required(
        resource_type,
        "create",
        "inspect the operation-owned provider resource and Nomad owned keys",
    )


def _result(
    credential: ProviderCredential,
    update: Any,
    *,
    verified: bool = True,
    evidence_accepted: bool | None = None,
) -> dict[str, Any]:
    result = {
        "providerId": credential.provider_id,
        "providerName": credential.provider_name,
        "credentialName": credential.credential_name,
        "verified": verified,
        "keyNames": sorted(credential.environment),
        "modifyIndex": update.modify_index,
    }
    if evidence_accepted is not None:
        result["evidenceAccepted"] = evidence_accepted
    return result


def _rotation_result(
    credential: ProviderCredential,
    update: Any,
    *,
    evidence: bool,
    retired: bool,
    rolled_back: bool,
) -> dict[str, Any]:
    result = _result(credential, update)
    result.update({"evidenceAccepted": evidence, "retired": retired, "rolledBack": rolled_back})
    return result


def _observe_evidence(
    observer: EvidenceObserver,
    application_id: str,
    application_slug: str,
    resource_type: str,
    modify_index: int,
) -> bool:
    evidence = observer(application_id, application_slug, resource_type, modify_index)
    return (
        evidence.allocation_healthy
        and evidence.public_healthy
        and evidence.observed_modify_index == modify_index
    )


def postgres_create_handler(
    args: Mapping[str, Any],
    *,
    admin: Any,
    scoped_connect: Callable[..., Any],
    nomad: VariableClient,
    host: str,
    observe_evidence: EvidenceObserver,
    password_factory: PasswordFactory = _password,
) -> Mapping[str, Any]:
    _operation_id, recovering, generation = _operation_args(
        args,
        {"applicationId", "applicationSlug", "postgresConnections", "measuredTargetBytes"},
        "storage.postgres.create",
    )
    application_id, application_slug = _common(args)
    database = _identity(application_id)[1]
    if recovering:
        snapshot, key_state, environment = _owned_observation(nomad, application_slug, "postgres")
        candidate_name = _credential_name(application_id, generation)
        provider_present = _pg_exists(admin, "SELECT 1 FROM pg_database WHERE datname=%s", database)
        candidate_present = _pg_exists(
            admin, "SELECT 1 FROM pg_roles WHERE rolname=%s", candidate_name
        )
        if provider_present and candidate_present:
            _postgres_creation_evidence(
                admin,
                application_id=application_id,
                operation_id=_operation_id,
                generation=generation,
            )
        if key_state == "complete" and environment is not None:
            if (
                provider_present
                and candidate_present
                and _environment_credential_name("postgres", environment) == candidate_name
            ):
                credential = _credential_from_environment(
                    "postgres", database, database, environment
                )
                _require_postgres_identity(credential, application_id=application_id, host=host)
                try:
                    postgres_verify(scoped_connect, credential, host=host)
                    if not _observe_evidence(
                        observe_evidence,
                        application_id,
                        application_slug,
                        "postgres",
                        snapshot.modify_index,
                    ):
                        raise RuntimeError("create health evidence was rejected")
                except Exception:
                    _raise_create_failure(
                        nomad,
                        application_slug,
                        "postgres",
                        credential,
                        remove_provider=lambda: _postgres_remove_created(
                            admin,
                            application_id=application_id,
                            operation_id=_operation_id,
                            generation=generation,
                        ),
                        provider_absent=lambda: _postgres_created_absent(
                            admin,
                            database=database,
                            owner=_identity(application_id)[2],
                            credential_name=candidate_name,
                        ),
                    )
                return _result(credential, snapshot, evidence_accepted=True)
            raise _recovery_required(
                "postgres", "create", "inspect the fixed database and Nomad owned keys"
            )
        if key_state == "partial":
            raise _recovery_required("postgres", "create", "repair the partial Nomad owned-key set")
        if candidate_present:
            _postgres_remove_created(
                admin,
                application_id=application_id,
                operation_id=_operation_id,
                generation=generation,
            )
            provider_present = False
        if provider_present:
            raise _recovery_required(
                "postgres", "create", "inspect the fixed database before retrying"
            )
    _ensure_owned_absent(nomad, application_slug, "postgres")
    credential = postgres_create(
        admin,
        application_id=application_id,
        host=host,
        connections=_positive(args["postgresConnections"], "postgresConnections"),
        measured_target_bytes=_positive(args["measuredTargetBytes"], "measuredTargetBytes"),
        password_factory=password_factory,
        generation=generation,
        operation_id=_operation_id,
    )
    try:
        postgres_verify(scoped_connect, credential, host=host)
        update = _publish(nomad, application_slug, "postgres", credential.environment)
        if not _observe_evidence(
            observe_evidence,
            application_id,
            application_slug,
            "postgres",
            update.modify_index,
        ):
            raise RuntimeError("create health evidence was rejected")
    except Exception:
        _raise_create_failure(
            nomad,
            application_slug,
            "postgres",
            credential,
            remove_provider=lambda: _postgres_remove_created(
                admin,
                application_id=application_id,
                operation_id=_operation_id,
                generation=generation,
            ),
            provider_absent=lambda: _postgres_created_absent(
                admin,
                database=database,
                owner=_identity(application_id)[2],
                credential_name=credential.credential_name,
            ),
        )
    return _result(credential, update, evidence_accepted=True)


def mongo_create_handler(
    args: Mapping[str, Any],
    *,
    admin: Any,
    scoped_connect: Callable[..., Any],
    nomad: VariableClient,
    host: str,
    observe_evidence: EvidenceObserver,
    password_factory: PasswordFactory = _password,
) -> Mapping[str, Any]:
    _operation_id, recovering, generation = _operation_args(
        args,
        {"applicationId", "applicationSlug", "measuredTargetBytes"},
        "storage.mongo.create",
    )
    application_id, application_slug = _common(args)
    if recovering:
        snapshot, key_state, environment = _owned_observation(nomad, application_slug, "mongo")
        candidate_name = _credential_name(application_id, generation)
        database = _identity(application_id)[1]
        users = _mongo_users(admin[database])
        names = {item.get("user") for item in users if isinstance(item.get("user"), str)}
        candidate_present = candidate_name in names
        candidate_owned = _mongo_owner_matches(
            users,
            application_id=application_id,
            credential_name=candidate_name,
            operation_id=_operation_id,
            generation=generation,
        )
        if candidate_present:
            _mongo_creation_evidence(
                users,
                application_id=application_id,
                operation_id=_operation_id,
                generation=generation,
            )
        if key_state == "complete" and environment is not None:
            if candidate_present and not candidate_owned:
                raise _recovery_required(
                    "mongo", "create", "inspect the candidate user ownership marker"
                )
            if (
                candidate_owned
                and _environment_credential_name("mongo", environment) == candidate_name
            ):
                credential = _credential_from_environment("mongo", database, database, environment)
                _require_mongo_identity(credential, application_id=application_id, host=host)
                try:
                    mongo_verify(scoped_connect, credential, host=host)
                    if not _observe_evidence(
                        observe_evidence,
                        application_id,
                        application_slug,
                        "mongo",
                        snapshot.modify_index,
                    ):
                        raise RuntimeError("create health evidence was rejected")
                except Exception:
                    _raise_create_failure(
                        nomad,
                        application_slug,
                        "mongo",
                        credential,
                        remove_provider=lambda: _mongo_remove_created(
                            admin,
                            application_id=application_id,
                            operation_id=_operation_id,
                            generation=generation,
                        ),
                        provider_absent=lambda: mongo_absent(admin, application_id=application_id),
                    )
                return _result(credential, snapshot, evidence_accepted=True)
            raise _recovery_required(
                "mongo", "create", "inspect the fixed database users and Nomad owned keys"
            )
        if key_state == "partial":
            raise _recovery_required("mongo", "create", "repair the partial Nomad owned-key set")
        if candidate_present:
            if not candidate_owned:
                raise _recovery_required(
                    "mongo", "create", "inspect the candidate user ownership marker"
                )
            _mongo_remove_created(
                admin,
                application_id=application_id,
                operation_id=_operation_id,
                generation=generation,
            )
        elif names:
            raise _recovery_required(
                "mongo", "create", "inspect the fixed database users before retrying"
            )
    _ensure_owned_absent(nomad, application_slug, "mongo")
    credential = mongo_create(
        admin,
        application_id=application_id,
        host=host,
        measured_target_bytes=_positive(args["measuredTargetBytes"], "measuredTargetBytes"),
        password_factory=password_factory,
        generation=generation,
        operation_id=_operation_id,
    )
    _require_mongo_identity(credential, application_id=application_id, host=host)
    try:
        mongo_verify(scoped_connect, credential, host=host)
        update = _publish(nomad, application_slug, "mongo", credential.environment)
        if not _observe_evidence(
            observe_evidence,
            application_id,
            application_slug,
            "mongo",
            update.modify_index,
        ):
            raise RuntimeError("create health evidence was rejected")
    except Exception:
        _raise_create_failure(
            nomad,
            application_slug,
            "mongo",
            credential,
            remove_provider=lambda: _mongo_remove_created(
                admin,
                application_id=application_id,
                operation_id=_operation_id,
                generation=generation,
            ),
            provider_absent=lambda: mongo_absent(admin, application_id=application_id),
        )
    return _result(credential, update, evidence_accepted=True)


def s3_create_handler(
    args: Mapping[str, Any],
    *,
    admin: Any,
    scoped_client: Callable[..., Any],
    nomad: VariableClient,
    prefix: str,
    endpoint: str,
    observe_evidence: EvidenceObserver,
) -> Mapping[str, Any]:
    _operation_id, recovering, generation = _operation_args(
        args,
        {"applicationId", "applicationSlug", "s3Bytes", "s3Objects"},
        "storage.s3.create",
    )
    application_id, application_slug = _common(args)
    if recovering:
        snapshot, key_state, environment = _owned_observation(nomad, application_slug, "s3")
        if key_state == "complete" and environment is not None:
            provider_name = environment["S3_BUCKET"]
            if provider_name.endswith(application_id.replace("-", "")[:8]):
                provider_id = _s3_bucket_id(admin, provider_name)
                if provider_id is not None:
                    credential = _credential_from_environment(
                        "s3", provider_id, provider_name, environment
                    )
                    info = _s3_bucket_info(admin, provider_id=provider_id)
                    if info is None:
                        raise _recovery_required(
                            "s3", "create", "inspect the deterministic bucket identity"
                        )
                    _require_s3_bucket_identity(
                        info,
                        provider_id=provider_id,
                        provider_name=provider_name,
                        access_key_id=environment["AWS_ACCESS_KEY_ID"],
                    )
                    _require_s3_endpoint(environment, endpoint)
                    try:
                        s3_verify(scoped_client, credential, endpoint=endpoint)
                        if not _observe_evidence(
                            observe_evidence,
                            application_id,
                            application_slug,
                            "s3",
                            snapshot.modify_index,
                        ):
                            raise RuntimeError("create health evidence was rejected")
                    except Exception:
                        _raise_create_failure(
                            nomad,
                            application_slug,
                            "s3",
                            credential,
                            remove_provider=lambda: _s3_remove_created(
                                admin,
                                scoped_client,
                                credential=credential,
                                expected_key_name=f"application-{application_slug}-{generation}",
                                expected_endpoint=endpoint,
                            ),
                            provider_absent=lambda: (
                                s3_absent(admin, provider_id=credential.provider_id)
                                and not _s3_key_present(admin, credential.credential_name)
                            ),
                        )
                    return _result(credential, snapshot, evidence_accepted=True)
            raise _recovery_required(
                "s3", "create", "inspect the deterministic bucket and Nomad owned keys"
            )
        if key_state != "absent":
            raise _recovery_required("s3", "create", "repair the partial Nomad owned-key set")
        bucket_id = _s3_expected_bucket_id(admin, application_id, application_slug, prefix)
        candidate_key = _s3_key_by_name(admin, f"application-{application_slug}-{generation}")
        if candidate_key is not None or bucket_id is not None:
            # A retry has no durable CreateBucket/CreateKey response.  A
            # deterministic alias or name is not ownership evidence: it may
            # describe a foreign resource or a concurrent writer.  Do not
            # delete either candidate; require explicit operator inspection.
            raise _recovery_required(
                "s3", "create", "inspect exact operation-owned bucket/key creation evidence"
            )
    _ensure_owned_absent(nomad, application_slug, "s3")
    credential = s3_create(
        admin,
        application_id=application_id,
        application_slug=application_slug,
        prefix=prefix,
        endpoint=endpoint,
        bytes_quota=_positive(args["s3Bytes"], "s3Bytes"),
        objects_quota=_positive(args["s3Objects"], "s3Objects"),
        generation=generation,
        scoped_client=scoped_client,
    )
    try:
        s3_verify(scoped_client, credential, endpoint=endpoint)
        update = _publish(nomad, application_slug, "s3", credential.environment)
        if not _observe_evidence(
            observe_evidence,
            application_id,
            application_slug,
            "s3",
            update.modify_index,
        ):
            raise RuntimeError("create health evidence was rejected")
    except Exception:
        _raise_create_failure(
            nomad,
            application_slug,
            "s3",
            credential,
            remove_provider=lambda: _s3_remove_created(
                admin,
                scoped_client,
                credential=credential,
                expected_key_name=f"application-{application_slug}-{generation}",
                expected_endpoint=endpoint,
            ),
            provider_absent=lambda: (
                s3_absent(admin, provider_id=credential.provider_id)
                and not _s3_key_present(admin, credential.credential_name)
            ),
        )
    return _result(credential, update, evidence_accepted=True)


def _credential_from_environment(
    resource_type: str, provider_id: str, provider_name: str, environment: SecretItems
) -> ProviderCredential:
    if resource_type == "postgres":
        credential_name = environment["PGUSER"]
    elif resource_type == "mongo":
        credential_name = _mongo_old_username(environment["MONGODB_URI"])
    else:
        credential_name = environment["AWS_ACCESS_KEY_ID"]
    return ProviderCredential(provider_id, provider_name, credential_name, environment)


def postgres_observe_handler(
    args: Mapping[str, Any],
    *,
    scoped_connect: Callable[..., Any],
    nomad: VariableClient,
    host: str,
) -> Mapping[str, Any]:
    _exact(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName"},
        "storage.postgres.observe",
    )
    application_id, application_slug = _common(args)
    _require_fixed_provider(args, application_id, "PostgreSQL")
    snapshot, key_state, environment = _owned_observation(nomad, application_slug, "postgres")
    if key_state != "complete" or environment is None:
        raise HelperActionError("VARIABLE_INCOMPLETE", "postgres environment keys are incomplete")
    if environment["PGDATABASE"] != args["providerName"]:
        raise HelperActionError("IDENTITY_MISMATCH", "PostgreSQL provider identity does not match")
    credential = _credential_from_environment(
        "postgres", str(args["providerId"]), str(args["providerName"]), environment
    )
    _require_postgres_identity(credential, application_id=application_id, host=host)
    connection = scoped_connect(
        host=environment["PGHOST"],
        port=int(environment["PGPORT"]),
        dbname=environment["PGDATABASE"],
        user=environment["PGUSER"],
        password=environment["PGPASSWORD"],
        sslmode="verify-full",
    )
    try:
        row = _pg_execute(connection, "SELECT 1").fetchone()
        if row is None or row[0] != 1:
            raise RuntimeError("PostgreSQL safe observation failed")
    finally:
        connection.close()
    return {"observed": True, "keyNames": list(POSTGRES_KEYS), "modifyIndex": snapshot.modify_index}


def mongo_observe_handler(
    args: Mapping[str, Any],
    *,
    scoped_connect: Callable[..., Any],
    nomad: VariableClient,
    host: str,
) -> Mapping[str, Any]:
    _exact(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName"},
        "storage.mongo.observe",
    )
    application_id, application_slug = _common(args)
    provider_name = _require_fixed_provider(args, application_id, "MongoDB")
    snapshot, key_state, environment = _owned_observation(nomad, application_slug, "mongo")
    if key_state != "complete" or environment is None:
        raise HelperActionError("VARIABLE_INCOMPLETE", "mongo environment keys are incomplete")
    credential = _credential_from_environment("mongo", provider_name, provider_name, environment)
    _require_mongo_identity(credential, application_id=application_id, host=host)
    client = scoped_connect(uri=environment["MONGODB_URI"])
    try:
        response = client[provider_name].command("ping")
        if not isinstance(response, Mapping) or response.get("ok") != 1:
            raise RuntimeError("MongoDB safe observation failed")
    finally:
        client.close()
    return {"observed": True, "keyNames": list(MONGO_KEYS), "modifyIndex": snapshot.modify_index}


def s3_observe_handler(
    args: Mapping[str, Any],
    *,
    scoped_client: Callable[..., Any],
    nomad: VariableClient,
    admin: Any,
    endpoint: str,
) -> Mapping[str, Any]:
    _exact(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName"},
        "storage.s3.observe",
    )
    _, application_slug = _common(args)
    snapshot, key_state, environment = _owned_observation(nomad, application_slug, "s3")
    if key_state != "complete" or environment is None:
        raise HelperActionError("VARIABLE_INCOMPLETE", "s3 environment keys are incomplete")
    provider_id, provider_name = _require_s3_provider(args, environment)
    _require_s3_endpoint(environment, endpoint)
    _require_s3_live_identity(
        admin,
        provider_id=provider_id,
        provider_name=provider_name,
        access_key_id=environment["AWS_ACCESS_KEY_ID"],
    )
    client = scoped_client(environment["AWS_ACCESS_KEY_ID"], environment["AWS_SECRET_ACCESS_KEY"])
    try:
        client.head_bucket(Bucket=provider_name)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return {"observed": True, "keyNames": list(S3_KEYS), "modifyIndex": snapshot.modify_index}


def postgres_verify_handler(
    args: Mapping[str, Any],
    *,
    scoped_connect: Callable[..., Any],
    nomad: VariableClient,
    host: str,
) -> Mapping[str, Any]:
    _operation_args(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName"},
        "storage.postgres.verify",
    )
    application_id, application_slug = _common(args)
    _require_fixed_provider(args, application_id, "PostgreSQL")
    snapshot, key_state, environment = _owned_observation(nomad, application_slug, "postgres")
    if key_state != "complete" or environment is None:
        raise HelperActionError("VARIABLE_INCOMPLETE", "postgres environment keys are incomplete")
    credential = _credential_from_environment(
        "postgres", str(args["providerId"]), str(args["providerName"]), environment
    )
    if credential.provider_name != environment["PGDATABASE"]:
        raise HelperActionError("IDENTITY_MISMATCH", "PostgreSQL provider identity does not match")
    postgres_verify(scoped_connect, credential, host=host)
    return {
        "verified": True,
        "keyNames": list(POSTGRES_KEYS),
        "modifyIndex": snapshot.modify_index,
    }


def mongo_verify_handler(
    args: Mapping[str, Any],
    *,
    scoped_connect: Callable[..., Any],
    nomad: VariableClient,
    host: str,
) -> Mapping[str, Any]:
    _operation_args(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName"},
        "storage.mongo.verify",
    )
    application_id, application_slug = _common(args)
    _require_fixed_provider(args, application_id, "MongoDB")
    snapshot, key_state, environment = _owned_observation(nomad, application_slug, "mongo")
    if key_state != "complete" or environment is None:
        raise HelperActionError("VARIABLE_INCOMPLETE", "mongo environment keys are incomplete")
    credential = _credential_from_environment(
        "mongo", str(args["providerId"]), str(args["providerName"]), environment
    )
    mongo_verify(scoped_connect, credential, host=host)
    return {
        "verified": True,
        "keyNames": list(MONGO_KEYS),
        "modifyIndex": snapshot.modify_index,
    }


def s3_verify_handler(
    args: Mapping[str, Any],
    *,
    scoped_client: Callable[..., Any],
    nomad: VariableClient,
    admin: Any,
    endpoint: str,
) -> Mapping[str, Any]:
    _operation_args(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName"},
        "storage.s3.verify",
    )
    _, application_slug = _common(args)
    snapshot, key_state, environment = _owned_observation(nomad, application_slug, "s3")
    if key_state != "complete" or environment is None:
        raise HelperActionError("VARIABLE_INCOMPLETE", "s3 environment keys are incomplete")
    provider_id, provider_name = _require_s3_provider(args, environment)
    _require_s3_endpoint(environment, endpoint)
    _require_s3_live_identity(
        admin,
        provider_id=provider_id,
        provider_name=provider_name,
        access_key_id=environment["AWS_ACCESS_KEY_ID"],
    )
    credential = _credential_from_environment("s3", provider_id, provider_name, environment)
    if credential.provider_name != environment["S3_BUCKET"]:
        raise HelperActionError("IDENTITY_MISMATCH", "S3 provider identity does not match")
    s3_verify(scoped_client, credential, endpoint=endpoint)
    return {
        "verified": True,
        "keyNames": list(S3_KEYS),
        "modifyIndex": snapshot.modify_index,
    }


def postgres_rotate_handler(
    args: Mapping[str, Any],
    *,
    admin: Any,
    scoped_connect: Callable[..., Any],
    nomad: VariableClient,
    host: str,
    observe_evidence: EvidenceObserver,
    password_factory: PasswordFactory = _password,
) -> Mapping[str, Any]:
    _operation_id, recovering, generation = _operation_args(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName", "postgresConnections"},
        "storage.postgres.rotate",
    )
    application_id, application_slug = _common(args)
    provider_name = _require_fixed_provider(args, application_id, "PostgreSQL")
    old = _owned_environment(nomad, application_slug, "postgres")
    candidate_name = _credential_name(application_id, generation)
    if recovering and _environment_credential_name("postgres", old) == candidate_name:
        candidate = _credential_from_environment("postgres", provider_name, provider_name, old)
        _require_postgres_identity(candidate, application_id=application_id, host=host)
        postgres_verify(scoped_connect, candidate, host=host)
        snapshot = nomad.read_variable(variable_path(application_slug))
        if not _observe_evidence(
            observe_evidence, application_id, application_slug, "postgres", snapshot.modify_index
        ):
            return _rotation_result(
                candidate, snapshot, evidence=False, retired=False, rolled_back=False
            )
        rows = _pg_execute(
            admin,
            "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
            (f"u_{application_id.replace('-', '')[:20]}_%",),
        ).fetchall()
        old_names = [row[0] for row in rows if row[0] != candidate_name]
        if len(old_names) > 1:
            raise _recovery_required("postgres", "rotate", "inspect multiple old application roles")
        if old_names:
            postgres_retire(admin, old_names[0])
            if _pg_exists(admin, "SELECT 1 FROM pg_roles WHERE rolname=%s", old_names[0]):
                raise _recovery_required("postgres", "rotate", "confirm old role retirement")
        return _rotation_result(candidate, snapshot, evidence=True, retired=True, rolled_back=False)
    if recovering and _pg_exists(admin, "SELECT 1 FROM pg_roles WHERE rolname=%s", candidate_name):
        # The candidate is operation-owned and was never published. It is the
        # only credential recovery may clean up before retrying.
        postgres_retire(admin, candidate_name)
    old_credential = _credential_from_environment(
        "postgres", str(args["providerId"]), str(args["providerName"]), old
    )
    _require_postgres_identity(old_credential, application_id=application_id, host=host)
    candidate = postgres_rotate(
        admin,
        application_id=application_id,
        host=host,
        connections=_positive(args["postgresConnections"], "postgresConnections"),
        old_environment=old,
        password_factory=password_factory,
        generation=generation,
    )
    _require_postgres_identity(candidate, application_id=application_id, host=host)
    try:
        postgres_verify(scoped_connect, candidate, host=host)
        update = _publish(nomad, application_slug, "postgres", candidate.environment)
    except Exception:
        postgres_retire(admin, candidate.credential_name)
        raise
    if not _observe_evidence(
        observe_evidence, application_id, application_slug, "postgres", update.modify_index
    ):
        rollback = _publish(nomad, application_slug, "postgres", old)
        postgres_retire(admin, candidate.credential_name)
        return _rotation_result(
            candidate, rollback, evidence=False, retired=False, rolled_back=True
        )
    try:
        published = _owned_environment(nomad, application_slug, "postgres")
        published_credential = _credential_from_environment(
            "postgres", provider_name, provider_name, published
        )
        _require_postgres_identity(published_credential, application_id=application_id, host=host)
        if published_credential.credential_name != candidate.credential_name:
            raise _recovery_required(
                "postgres",
                "rotate",
                "inspect the published credential before retiring the old role",
            )
        postgres_retire(admin, old_credential.credential_name)
        if _pg_exists(
            admin,
            "SELECT 1 FROM pg_roles WHERE rolname=%s",
            old_credential.credential_name,
        ):
            raise _recovery_required("postgres", "rotate", "confirm old role retirement")
    except HelperActionError:
        raise
    except Exception:
        return _rotation_result(candidate, update, evidence=True, retired=False, rolled_back=False)
    return _rotation_result(candidate, update, evidence=True, retired=True, rolled_back=False)


def mongo_rotate_handler(
    args: Mapping[str, Any],
    *,
    admin: Any,
    scoped_connect: Callable[..., Any],
    nomad: VariableClient,
    host: str,
    observe_evidence: EvidenceObserver,
    password_factory: PasswordFactory = _password,
) -> Mapping[str, Any]:
    _operation_id, recovering, generation = _operation_args(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName"},
        "storage.mongo.rotate",
    )
    application_id, application_slug = _common(args)
    provider_name = _require_fixed_provider(args, application_id, "MongoDB")
    old = _owned_environment(nomad, application_slug, "mongo")
    candidate_name = _credential_name(application_id, generation)
    users = _mongo_users(admin[provider_name])
    names = [name for item in users if isinstance((name := item.get("user")), str)]
    old_name = _environment_credential_name("mongo", old)
    old_credential = _credential_from_environment(
        "mongo", str(args["providerId"]), str(args["providerName"]), old
    )
    _require_mongo_identity(old_credential, application_id=application_id, host=host)
    if recovering and old_name == candidate_name:
        if not _mongo_owner_matches(
            users,
            application_id=application_id,
            credential_name=candidate_name,
            operation_id=_operation_id,
            generation=generation,
        ):
            raise _recovery_required(
                "mongo", "rotate", "inspect the candidate user ownership marker"
            )
        candidate = _credential_from_environment("mongo", provider_name, provider_name, old)
        _require_mongo_identity(candidate, application_id=application_id, host=host)
        mongo_verify(scoped_connect, candidate, host=host)
        snapshot = nomad.read_variable(variable_path(application_slug))
        if not _observe_evidence(
            observe_evidence, application_id, application_slug, "mongo", snapshot.modify_index
        ):
            return _rotation_result(
                candidate, snapshot, evidence=False, retired=False, rolled_back=False
            )
        old_names = [
            name
            for name in names
            if name != candidate_name
            and _mongo_owner_matches(
                users,
                application_id=application_id,
                credential_name=name,
            )
        ]
        if len(old_names) > 1:
            raise _recovery_required("mongo", "rotate", "inspect multiple old application users")
        if len(old_names) != len([name for name in names if name != candidate_name]):
            raise _recovery_required("mongo", "rotate", "inspect foreign application users")
        if old_names:
            mongo_retire(admin, provider_name, old_names[0])
            remaining_users = _mongo_users(admin[provider_name])
            if any(item.get("user") == old_names[0] for item in remaining_users):
                raise _recovery_required("mongo", "rotate", "confirm old user retirement")
        return _rotation_result(candidate, snapshot, evidence=True, retired=True, rolled_back=False)
    if recovering and candidate_name in names:
        if not _mongo_owner_matches(
            users,
            application_id=application_id,
            credential_name=candidate_name,
            operation_id=_operation_id,
            generation=generation,
        ):
            raise _recovery_required(
                "mongo", "rotate", "inspect the candidate user ownership marker"
            )
        mongo_retire(admin, provider_name, candidate_name)
    candidate = mongo_rotate(
        admin,
        application_id=application_id,
        host=host,
        old_environment=old,
        password_factory=password_factory,
        generation=generation,
        operation_id=_operation_id,
    )
    _require_mongo_identity(candidate, application_id=application_id, host=host)
    try:
        mongo_verify(scoped_connect, candidate, host=host)
        update = _publish(nomad, application_slug, "mongo", candidate.environment)
    except Exception:
        mongo_retire(admin, candidate.provider_name, candidate.credential_name)
        raise
    if not _observe_evidence(
        observe_evidence, application_id, application_slug, "mongo", update.modify_index
    ):
        rollback = _publish(nomad, application_slug, "mongo", old)
        mongo_retire(admin, candidate.provider_name, candidate.credential_name)
        return _rotation_result(
            candidate, rollback, evidence=False, retired=False, rolled_back=True
        )
    try:
        published = _owned_environment(nomad, application_slug, "mongo")
        published_credential = _credential_from_environment(
            "mongo", provider_name, provider_name, published
        )
        _require_mongo_identity(published_credential, application_id=application_id, host=host)
        if published_credential.credential_name != candidate.credential_name:
            raise _recovery_required(
                "mongo", "rotate", "inspect the published credential before retiring the old user"
            )
        users = _mongo_users(admin[provider_name])
        if not _mongo_owner_matches(
            users,
            application_id=application_id,
            credential_name=old_credential.credential_name,
        ):
            raise _recovery_required("mongo", "rotate", "inspect the old user ownership marker")
        mongo_retire(admin, old_credential.provider_name, old_credential.credential_name)
        remaining_users = _mongo_users(admin[old_credential.provider_name])
        if any(item.get("user") == old_credential.credential_name for item in remaining_users):
            raise _recovery_required("mongo", "rotate", "confirm old user retirement")
    except HelperActionError:
        raise
    except Exception:
        return _rotation_result(candidate, update, evidence=True, retired=False, rolled_back=False)
    return _rotation_result(candidate, update, evidence=True, retired=True, rolled_back=False)


def s3_rotate_handler(
    args: Mapping[str, Any],
    *,
    admin: Any,
    scoped_client: Callable[..., Any],
    nomad: VariableClient,
    endpoint: str,
    observe_evidence: EvidenceObserver,
) -> Mapping[str, Any]:
    _operation_id, recovering, generation = _operation_args(
        args,
        {"applicationId", "applicationSlug", "providerId", "providerName"},
        "storage.s3.rotate",
    )
    application_id, application_slug = _common(args)
    old = _owned_environment(nomad, application_slug, "s3")
    provider_id, provider_name = _require_s3_provider(args, old)
    _require_s3_live_identity(
        admin,
        provider_id=provider_id,
        provider_name=provider_name,
        access_key_id=old["AWS_ACCESS_KEY_ID"],
    )
    candidate_display_name = f"application-{provider_name}-{generation}"
    if recovering:
        candidate_key_id, old_key_ids = _s3_rotation_keys(
            admin, provider_name, candidate_display_name, application_slug
        )
        if candidate_key_id == _environment_credential_name("s3", old):
            _require_s3_live_identity(
                admin,
                provider_id=provider_id,
                provider_name=provider_name,
                access_key_id=candidate_key_id,
            )
            candidate = _credential_from_environment("s3", provider_id, provider_name, old)
            s3_verify(scoped_client, candidate, endpoint=endpoint)
            snapshot = nomad.read_variable(variable_path(application_slug))
            if not _observe_evidence(
                observe_evidence, application_id, application_slug, "s3", snapshot.modify_index
            ):
                return _rotation_result(
                    candidate, snapshot, evidence=False, retired=False, rolled_back=False
                )
            if len(old_key_ids) != 1:
                raise _recovery_required(
                    "s3", "rotate", "inspect application keys before retiring old access"
                )
            _require_s3_live_identity(
                admin,
                provider_id=provider_id,
                provider_name=provider_name,
                access_key_id=old_key_ids[0],
            )
            s3_retire(admin, old_key_ids[0])
            if _s3_key_present(admin, old_key_ids[0]):
                raise _recovery_required("s3", "rotate", "confirm old access-key retirement")
            return _rotation_result(
                candidate, snapshot, evidence=True, retired=True, rolled_back=False
            )
        if candidate_key_id is not None:
            record = _s3_key_record(admin, candidate_key_id)
            if record is None or record.get("name") != candidate_display_name:
                raise _recovery_required(
                    "s3", "rotate", "inspect the operation-owned candidate access key"
                )
            s3_retire(admin, candidate_key_id)
            if _s3_key_present(admin, candidate_key_id):
                raise _recovery_required(
                    "s3", "rotate", "confirm candidate access-key absence before retrying"
                )
    old_credential = _credential_from_environment("s3", provider_id, provider_name, old)
    candidate = s3_rotate(
        admin,
        provider_id=old_credential.provider_id,
        provider_name=old_credential.provider_name,
        endpoint=endpoint,
        generation=generation,
    )
    try:
        _require_s3_live_identity(
            admin,
            provider_id=provider_id,
            provider_name=provider_name,
            access_key_id=candidate.credential_name,
        )
        s3_verify(scoped_client, candidate, endpoint=endpoint)
        update = _publish(nomad, application_slug, "s3", candidate.environment)
    except Exception:
        s3_retire(admin, candidate.credential_name)
        raise
    if not _observe_evidence(
        observe_evidence, application_id, application_slug, "s3", update.modify_index
    ):
        rollback = _publish(nomad, application_slug, "s3", old)
        s3_retire(admin, candidate.credential_name)
        return _rotation_result(
            candidate, rollback, evidence=False, retired=False, rolled_back=True
        )
    try:
        published = _owned_environment(nomad, application_slug, "s3")
        published_provider_id, published_provider_name = _require_s3_provider(args, published)
        if (
            published_provider_id != candidate.provider_id
            or published_provider_name != candidate.provider_name
            or published["AWS_ACCESS_KEY_ID"] != candidate.credential_name
        ):
            raise _recovery_required(
                "s3", "rotate", "inspect the published bucket identity before retiring the old key"
            )
        _require_s3_live_identity(
            admin,
            provider_id=candidate.provider_id,
            provider_name=candidate.provider_name,
            access_key_id=candidate.credential_name,
        )
        _require_s3_live_identity(
            admin,
            provider_id=old_credential.provider_id,
            provider_name=old_credential.provider_name,
            access_key_id=old_credential.credential_name,
        )
        s3_retire(admin, old_credential.credential_name)
        if _s3_key_present(admin, old_credential.credential_name):
            raise _recovery_required("s3", "rotate", "confirm old access-key retirement")
    except HelperActionError:
        raise
    except Exception:
        return _rotation_result(candidate, update, evidence=True, retired=False, rolled_back=False)
    return _rotation_result(candidate, update, evidence=True, retired=True, rolled_back=False)


def _remove_result(resource_type: str, update: Any) -> dict[str, Any]:
    return {
        "confirmedAbsent": True,
        "environmentRemoved": True,
        "removedKeyNames": list(RESOURCE_KEYS[resource_type]),
        "remainingKeyNames": list(update.key_names),
        "modifyIndex": update.modify_index,
    }


def postgres_remove_handler(
    args: Mapping[str, Any], *, admin: Any, nomad: VariableClient
) -> Mapping[str, Any]:
    _operation_args(
        args,
        {
            "applicationId",
            "applicationSlug",
            "providerId",
            "providerName",
            "confirmSlug",
            "preflight",
        },
        "storage.postgres.remove",
    )
    application_id, application_slug = _common(args)
    if args["confirmSlug"] != application_slug or not isinstance(args["preflight"], bool):
        raise HelperActionError("CONFIRMATION_REQUIRED", "PostgreSQL deletion slug does not match")
    try:
        _require_fixed_provider(args, application_id, "PostgreSQL")
    except HelperActionError:
        raise HelperActionError(
            "CONFIRMATION_REQUIRED", "PostgreSQL deletion identity does not match"
        ) from None
    snapshot, key_state, _environment = _owned_observation(nomad, application_slug, "postgres")
    absent = postgres_absent(admin, application_id=application_id)
    if key_state != "complete" and not (key_state == "absent" and absent):
        raise _recovery_required(
            "postgres", "remove", "inspect provider absence and the Nomad owned-key set"
        )
    if args["preflight"]:
        return {"preflightAccepted": True}
    if not absent:
        postgres_remove(admin, application_id=application_id)
    if not postgres_absent(admin, application_id=application_id):
        raise HelperActionError("ABSENCE_UNCONFIRMED", "PostgreSQL absence could not be confirmed")
    update = (
        snapshot
        if key_state == "absent"
        else _remove_environment(nomad, application_slug, "postgres")
    )
    return _remove_result("postgres", update)


def mongo_remove_handler(
    args: Mapping[str, Any], *, admin: Any, nomad: VariableClient
) -> Mapping[str, Any]:
    _operation_args(
        args,
        {
            "applicationId",
            "applicationSlug",
            "providerId",
            "providerName",
            "confirmSlug",
            "preflight",
        },
        "storage.mongo.remove",
    )
    application_id, application_slug = _common(args)
    if args["confirmSlug"] != application_slug or not isinstance(args["preflight"], bool):
        raise HelperActionError("CONFIRMATION_REQUIRED", "MongoDB deletion slug does not match")
    try:
        _require_fixed_provider(args, application_id, "MongoDB")
    except HelperActionError:
        raise HelperActionError(
            "CONFIRMATION_REQUIRED", "MongoDB deletion identity does not match"
        ) from None
    snapshot, key_state, environment = _owned_observation(nomad, application_slug, "mongo")
    absent = mongo_absent(admin, application_id=application_id)
    if key_state != "complete" and not (key_state == "absent" and absent):
        raise _recovery_required(
            "mongo", "remove", "inspect provider absence and the Nomad owned-key set"
        )
    if args["preflight"]:
        return {"preflightAccepted": True}
    if not absent:
        assert environment is not None
        mongo_remove(
            admin,
            application_id=application_id,
            credential_name=_environment_credential_name("mongo", environment),
        )
    if not mongo_absent(admin, application_id=application_id):
        raise HelperActionError("ABSENCE_UNCONFIRMED", "MongoDB absence could not be confirmed")
    update = (
        snapshot if key_state == "absent" else _remove_environment(nomad, application_slug, "mongo")
    )
    return _remove_result("mongo", update)


def s3_remove_handler(
    args: Mapping[str, Any],
    *,
    admin: Any,
    scoped_client: Callable[..., Any],
    nomad: VariableClient,
) -> Mapping[str, Any]:
    _operation_args(
        args,
        {
            "applicationId",
            "applicationSlug",
            "providerId",
            "providerName",
            "confirmSlug",
            "purge",
            "preflight",
        },
        "storage.s3.remove",
    )
    application_id, application_slug = _common(args)
    if (
        args["confirmSlug"] != application_slug
        or not isinstance(args["purge"], bool)
        or not isinstance(args["preflight"], bool)
    ):
        raise HelperActionError("CONFIRMATION_REQUIRED", "S3 deletion confirmation is invalid")
    snapshot, key_state, environment = _owned_observation(nomad, application_slug, "s3")
    if not isinstance(args.get("providerId"), str) or not isinstance(args.get("providerName"), str):
        raise HelperActionError("RECOVERY_REQUIRED", "S3 deletion identity is malformed")
    provider_id = args["providerId"]
    provider_name = args["providerName"]
    info = _s3_bucket_info(admin, provider_id=provider_id)
    absent = info is None
    if key_state == "complete" and environment is not None:
        try:
            provider_id, provider_name = _require_s3_provider(args, environment)
            if info is not None:
                _require_s3_bucket_identity(
                    info,
                    provider_id=provider_id,
                    provider_name=provider_name,
                    access_key_id=environment["AWS_ACCESS_KEY_ID"],
                )
            else:
                _require_s3_orphan_key_identity(
                    admin,
                    key_id=environment["AWS_ACCESS_KEY_ID"],
                    application_slug=application_slug,
                    provider_name=provider_name,
                )
        except HelperActionError:
            raise HelperActionError(
                "RECOVERY_REQUIRED", "S3 deletion identity does not match; recovery is required"
            ) from None
        if (
            args["preflight"]
            and not absent
            and not args["purge"]
            and _s3_bucket_has_objects(scoped_client, provider_name, environment)
        ):
            raise HelperActionError(
                "BUCKET_NOT_EMPTY", "S3 bucket is not empty; explicit purge is required"
            )
        if args["preflight"]:
            return {"preflightAccepted": True}
        if not absent:
            s3_remove(
                admin,
                scoped_client,
                provider_id=provider_id,
                provider_name=provider_name,
                environment=environment,
                purge=args["purge"],
            )
        elif _s3_key_present(admin, environment["AWS_ACCESS_KEY_ID"]):
            # The accepted key may survive a lost bucket-delete response, but
            # it is retired only after its application-owned name was checked.
            s3_retire(admin, environment["AWS_ACCESS_KEY_ID"])
            if _s3_key_present(admin, environment["AWS_ACCESS_KEY_ID"]):
                raise HelperActionError("ABSENCE_UNCONFIRMED", "S3 key absence was not confirmed")
    elif not (key_state == "absent" and absent):
        raise _recovery_required(
            "s3", "remove", "inspect provider absence and the Nomad owned-key set"
        )
    elif args["preflight"]:
        return {"preflightAccepted": True}
    if not s3_absent(admin, provider_id=provider_id):
        raise HelperActionError("ABSENCE_UNCONFIRMED", "S3 bucket absence could not be confirmed")
    if (
        key_state == "complete"
        and environment is not None
        and _s3_key_present(admin, environment["AWS_ACCESS_KEY_ID"])
    ):
        raise HelperActionError("ABSENCE_UNCONFIRMED", "S3 access-key absence was not confirmed")
    update = (
        snapshot if key_state == "absent" else _remove_environment(nomad, application_slug, "s3")
    )
    return _remove_result("s3", update)


def handlers(
    *,
    postgres_admin: Any,
    postgres_connect: Callable[..., Any],
    mongo_admin: Any,
    mongo_connect: Callable[..., Any],
    garage_admin: Any,
    s3_connect: Callable[..., Any],
    nomad: VariableClient,
    storage_host: str,
    s3_endpoint: str,
    prefix: str,
    observe_evidence: EvidenceObserver,
) -> dict[str, Handler]:
    """Bind trusted clients into the fifteen fixed storage protocol actions."""
    return {
        "storage.postgres.create": lambda args: postgres_create_handler(
            args,
            admin=postgres_admin,
            scoped_connect=postgres_connect,
            nomad=nomad,
            host=storage_host,
            observe_evidence=observe_evidence,
        ),
        "storage.postgres.observe": lambda args: postgres_observe_handler(
            args, scoped_connect=postgres_connect, nomad=nomad, host=storage_host
        ),
        "storage.postgres.verify": lambda args: postgres_verify_handler(
            args, scoped_connect=postgres_connect, nomad=nomad, host=storage_host
        ),
        "storage.postgres.rotate": lambda args: postgres_rotate_handler(
            args,
            admin=postgres_admin,
            scoped_connect=postgres_connect,
            nomad=nomad,
            host=storage_host,
            observe_evidence=observe_evidence,
        ),
        "storage.postgres.remove": lambda args: postgres_remove_handler(
            args, admin=postgres_admin, nomad=nomad
        ),
        "storage.mongo.create": lambda args: mongo_create_handler(
            args,
            admin=mongo_admin,
            scoped_connect=mongo_connect,
            nomad=nomad,
            host=storage_host,
            observe_evidence=observe_evidence,
        ),
        "storage.mongo.observe": lambda args: mongo_observe_handler(
            args, scoped_connect=mongo_connect, nomad=nomad, host=storage_host
        ),
        "storage.mongo.verify": lambda args: mongo_verify_handler(
            args, scoped_connect=mongo_connect, nomad=nomad, host=storage_host
        ),
        "storage.mongo.rotate": lambda args: mongo_rotate_handler(
            args,
            admin=mongo_admin,
            scoped_connect=mongo_connect,
            nomad=nomad,
            host=storage_host,
            observe_evidence=observe_evidence,
        ),
        "storage.mongo.remove": lambda args: mongo_remove_handler(
            args, admin=mongo_admin, nomad=nomad
        ),
        "storage.s3.create": lambda args: s3_create_handler(
            args,
            admin=garage_admin,
            scoped_client=s3_connect,
            nomad=nomad,
            prefix=prefix,
            endpoint=s3_endpoint,
            observe_evidence=observe_evidence,
        ),
        "storage.s3.observe": lambda args: s3_observe_handler(
            args,
            scoped_client=s3_connect,
            nomad=nomad,
            admin=garage_admin,
            endpoint=s3_endpoint,
        ),
        "storage.s3.verify": lambda args: s3_verify_handler(
            args,
            scoped_client=s3_connect,
            nomad=nomad,
            admin=garage_admin,
            endpoint=s3_endpoint,
        ),
        "storage.s3.rotate": lambda args: s3_rotate_handler(
            args,
            admin=garage_admin,
            scoped_client=s3_connect,
            nomad=nomad,
            endpoint=s3_endpoint,
            observe_evidence=observe_evidence,
        ),
        "storage.s3.remove": lambda args: s3_remove_handler(
            args, admin=garage_admin, scoped_client=s3_connect, nomad=nomad
        ),
    }
