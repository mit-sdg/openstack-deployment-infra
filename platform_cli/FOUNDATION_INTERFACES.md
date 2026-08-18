# Frozen foundation interfaces

Status: frozen branch-point contract for infrastructure, application, storage,
read/status, packaging, and CLI integration owners.

These are concrete helpers, not extension points. Feature files import them
directly. Changes to this file or the listed signatures are integrator-owned.

## Package and ownership locations

The Python import root is the repository-root package `platform_cli`; do not add
a second package root or a `src/` mirror.

| Owner | Files/imports |
| --- | --- |
| Integrator | `platform_cli/cli.py`, `platform_cli/__init__.py`, helper action registration |
| Infrastructure | `platform_cli/openstack.py` |
| Application | `platform_cli/app.py`, `platform_cli/helper/app.py` |
| Storage | `platform_cli/storage.py`, `platform_cli/helper/storage.py` |
| Read/status | `platform_cli/status.py` |
| Packaging | project metadata and launchers for `platform_cli.cli:main` and `platform_cli.helper.main:main`; no feature behavior |
| Foundation (frozen here) | `config.py`, `validation.py`, `db.py`, `runtime.py`, `remote.py`, `helper/main.py`, `helper/nomad.py` |

Management-host code may import all foundation modules. Helper feature code may
import `validation`, `runtime`, `remote` envelope types, and `helper.nomad`; it
must not import management SQLite or CLI presentation. Feature owners add
ordinary named command functions in their owned modules. They do not add a
provider interface, repository class, command bus, generic workflow, or a
second dispatch layer.

The accepted runtime locations remain:

- management release: `/srv/openstack-platform/platform-cli/releases/<full-source-commit>/`;
- management launcher: `/srv/openstack-platform/bin/openstack-platform`;
- persistent non-secret management configuration: direct mode-`0600`
  `/srv/openstack-platform/config/platform.json`;
- private state: `/srv/openstack-platform/state` (`0700`), including
  `platform.sqlite3` and private policy/log files (`0600`);
- helper release:
  `<paths.adminState>/controller/platform-cli/releases/<full-source-commit>/`;
- helper launcher: `<paths.root>/bin/openstack-platform-helper`;
- helper inventory: `/etc/<namespace>/platform.json`, either a direct readable
  regular file or a NixOS symlink resolving to a direct root-owned,
  non-group/world-writable, `agentops`-readable regular file under `/nix/store`.
  Helper deployment verifies that its project, project ID, namespace, and
  complete paths match the management inventory before upload and installation.

`/srv/openstack-platform/config/platform.json` must contain the real stable `projectId` before
an installed command uses it. Install it from the operator's private
repository, never from the immutable Git archive. Do not substitute the example
UUID or infer it from the project name.

## Configuration and validation

Frozen configuration signatures:

```python
config.load(
    platform_path: str | Path,
    policy_path: str | Path,
    *,
    require_private_policy: bool = True,
) -> config.Config
config.load_platform(path: str | Path) -> config.PlatformConfig
config.load_policy(
    path: str | Path,
    *,
    require_private: bool = True,
) -> config.Policy
config.platform_config_identity(platform: config.PlatformConfig) -> str
```

`Config`, `PlatformConfig`, `StandardProfile`, `RuntimeImages`, `Limits`, and
`Policy` are the frozen, immutable records returned by these calls. Callers use
snake-case attributes, not the source JSON spelling. `platform_config_identity`
hashes the stable deployment inventory used to bind management state; image/flavor,
version, checksum, and container selections are excluded. Inventory
escape-hatch lookups use `PlatformConfig.get(dotted_name: str) -> Any`.

Frozen shared validators:

```python
validation.slug(value: object) -> str
validation.uuid(value: object, *, field: str = "UUID") -> str
validation.commit(value: object) -> str
validation.sha256_hex(value: object, *, field: str = "SHA-256") -> str
validation.env_key(value: object) -> str
validation.script_name(value: object) -> str
validation.repository_url(value: object) -> str
validation.relative_path(
    value: object, *, field: str = "path", allow_dot: bool = True
) -> str
validation.resolve_inside(
    root: Path, value: object, *, field: str = "path"
) -> Path
validation.health_path(value: object) -> str
validation.oci_digest_pin(
    value: object, *, field: str = "runtime image"
) -> str
validation.age_recipient(value: object) -> str
validation.bounded_text(
    value: object, *, field: str, maximum: int
) -> str
validation.load_strict_yaml(
    text: str | bytes, *, maximum_bytes: int = 65_536
) -> Any
```

Feature modules should not duplicate these checks. Feature-specific schemas
remain in the owning feature module.

## SQLite

The connection and migration boundary is:

```python
db.connect(
    path: str | Path, *, create: bool = True,
    identity: db.DeploymentIdentity | None = None,
) -> sqlite3.Connection
db.migrate(
    connection: sqlite3.Connection, *, target_version: int | None = None,
    identity: db.DeploymentIdentity | None = None,
) -> None
db.schema_version(connection: sqlite3.Connection) -> int
db.backup_database(
    connection: sqlite3.Connection, destination: str | Path
) -> Path
```

Startup takes the deadline-aware
`runtime.lock(state_directory, "database-maintenance", wait=True, deadline=...)`,
builds the current deployment identity from the installed inventory, opens the
connection, migrates, and releases that lock before taking a feature lock. The
identity is checked before SQLite WAL sidecars are enabled. No caller uses
`db.transaction` around a database helper or external work.

Frozen operation signatures:

```python
db.get_operation(connection, operation_id: str) -> db.Operation | None
db.get_unfinished_operation(connection, scope: str) -> db.Operation | None
db.begin_operation(
    connection,
    *,
    operation_id: str,
    kind: str,
    scope: str,
    phase: str,
    deadline_at: str,
    refs: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> db.Operation
db.checkpoint_operation(
    connection,
    operation_id: str,
    *,
    phase: str,
    refs: Mapping[str, Any] | None = None,
    candidate_digest: str | None = None,
    cleanup_state: str | None = None,
    now: str | None = None,
) -> db.Operation
db.mark_recovery_required(
    connection,
    operation_id: str,
    error: BaseException | str,
    *,
    phase: str | None = None,
    now: str | None = None,
) -> db.Operation
db.mark_succeeded(
    connection,
    operation_id: str,
    *,
    cleanup_state: str = "confirmed",
    now: str | None = None,
) -> db.Operation
db.mark_failed(
    connection,
    operation_id: str,
    error: BaseException | str,
    *,
    cleanup_state: str,
    now: str | None = None,
) -> db.Operation
```

Frozen accepted-state signatures:

```python
db.put_image_selection(
    connection,
    *,
    role: str,
    image_id: str,
    display_name: str,
    source_commit: str,
    compatibility_hash: str,
    selected_at: str | None = None,
) -> None
db.get_image_selection(connection, role: str) -> db.ImageSelection | None
db.list_image_selections(connection) -> list[db.ImageSelection]

db.put_application(
    connection,
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
) -> None
db.get_application(connection, identifier: str) -> db.Application | None
db.list_applications(connection) -> list[db.Application]
db.delete_application(connection, application_id: str) -> None

db.accept_deployment(
    connection,
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
) -> None
db.get_deployment(connection, application_id: str) -> db.Deployment | None

db.put_managed_resource(
    connection,
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
) -> None
db.list_managed_resources(
    connection, *, application_id: str | None = None
) -> list[db.ManagedResource]
db.delete_managed_resource(
    connection, *, application_id: str, resource_type: str
) -> None

db.set_environment_keys(
    connection, *, application_id: str, owner: str, keys: Sequence[str]
) -> None
db.list_environment_keys(
    connection, *, application_id: str | None = None
) -> list[db.EnvironmentKey]
```

Every write helper above opens and closes its own short transaction. SQL rows
leave `db.py` only as frozen `Operation`, `ImageSelection`, `Application`,
`Deployment`, `ManagedResource`, or `EnvironmentKey` records.

Operation `refs` are compact, command-owned, non-secret recovery identities.
Never pass credentials, environment values, provider responses, source content,
user data, or cloud-init to a database, result, error, or log helper.

## Runtime

Frozen process and locking signatures:

```python
runtime.lock(
    state_directory: str | Path,
    scope: str,
    *,
    wait: bool = False,
    deadline: float | None = None,
) -> ContextManager[None]
runtime.run(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    stdin: bytes | None = None,
    stdout_limit: int = 1_048_576,
    stderr_limit: int = 262_144,
    inherit_env: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    secrets: Sequence[str | bytes] = (),
    check: bool = True,
) -> runtime.CommandResult
runtime.bounded_http(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 30,
    response_limit: int = 1_048_576,
    ssl_context: object | None = None,
    allow_redirects: bool = False,
) -> runtime.HttpResult
runtime.append_private_log(
    path: str | Path,
    text: object,
    *,
    maximum_bytes: int,
    secrets: Sequence[str | bytes] = (),
) -> int
runtime.write_private_stack_diagnostic(
    directory: str | Path,
    error: BaseException,
    *,
    correlation_id: str,
    maximum_frames: int = 64,
) -> Path
```

`write_private_stack_diagnostic` creates one mode-`0600` correlation file in a
mode-`0700` directory. It traverses traceback frames directly and records only
trusted package-relative file/line locations, fixed external markers, and the
correlation ID. It does not render exception text, source lines, provider
payloads, locals, or environment values.

`lock` accepts only `infrastructure`, `database-maintenance`, or
`app-<canonical UUID>`. A waiting lock with a monotonic absolute `deadline`
uses bounded non-blocking probes and fails with a safe lock/deadline error; it
never lets a blocking `flock` outlive the operation. `run` always uses an argv
with `shell=False`, an explicit environment allowlist, a whole-call deadline,
and bounded stdout/stderr.

## Helper protocol and Nomad Variables

The management call signature is:

```python
remote.call_helper(
    action: str,
    args: Mapping[str, Any],
    *,
    timeout_seconds: float,
    request_limit: int = 1_048_576,
    response_limit: int = 1_048_576,
    stderr_limit: int = 262_144,
    request_id: str | None = None,
    command_runner: Callable[..., Any] = runtime.run,
    ssh_config_path: str | os.PathLike[str] = remote.DEFAULT_SSH_CONFIG,
) -> Mapping[str, Any]
```

By default it invokes exactly
`ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- openstack-platform-helper`;
user input is JSON on stdin only. The pinned configuration and alias own connect
timeout and strict host-key policy. Action handlers have the one frozen shape:

```python
Handler = Callable[[Mapping[str, Any]], Mapping[str, Any]]
helper.main.serve_once(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    handlers: Mapping[str, Handler],
    *,
    request_limit: int = 1_048_576,
    response_limit: int = 1_048_576,
    diagnostic_directory: str | Path = DEFAULT_DIAGNOSTIC_DIRECTORY,
) -> int
```

Each helper feature owner implements handlers in its owned module. The
integrator alone assembles the complete protocol-v1 action map. Handler results
and deliberate `HelperActionError` messages contain no secrets. A partial map
must not be packaged as the production protocol-v1 helper.

Frozen Nomad Variable signatures:

```python
helper.nomad.variable_path(application_slug: str) -> str
helper.nomad.merge_owned_items(
    current: Mapping[str, str],
    ownership: Mapping[str, str],
    *,
    owner: str,
    updates: Mapping[str, str] | None = None,
    removals: Sequence[str] = (),
    maximum_keys: int = 128,
    maximum_value_bytes: int = 65_536,
) -> helper.nomad.SecretItems
helper.nomad.update_owned_items(
    client: helper.nomad.VariableClient,
    path: str,
    ownership: Mapping[str, str],
    *,
    owner: str,
    updates: Mapping[str, str] | None = None,
    removals: Sequence[str] = (),
    maximum_keys: int = 128,
    maximum_value_bytes: int = 65_536,
    attempts: int = 3,
) -> helper.nomad.VariableUpdate
```

Values stay in `SecretItems`, whose representation is redacted. Only key names
and the new ModifyIndex leave the helper. Storage code uses the same owned-key
merge; it never performs a whole-Variable PUT.

Backup acceptance remains
`helper.main.accept_staged_backup(*, staging_directory, backup_directory, name,
expected_sha256, plaintext_sha256, integrity_checked_at, retention_count=14,
maximum_bytes=1_073_741_824) -> Mapping`. It verifies age-v1 framing and the
encrypted SHA-256, writes mode-0600 ciphertext/checksum evidence, and fsyncs
the accepted directory after each promotion step. The final manifest rename is
the commit marker: retention counts only complete ciphertext/checksum/manifest
trios, retries reconcile an interrupted pre-commit promotion, and malformed
committed evidence is preserved for operator attention. Retention is bounded by
count and never treats an uncommitted partial set as accepted.
