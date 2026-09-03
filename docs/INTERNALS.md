# Platform internals

This document is for maintainers building or integrating platform components.
It describes ownership, process boundaries, state, internal interfaces, and the
future management application. Deployment operators should start with [Deploy
the platform](DEPLOYMENT.md); exact backup and recovery commands are in
[Operations](OPERATIONS.md).

## Shared configuration and contract

One private deployment inventory, `config/platform.json`, supplies project
identity, resource names, addresses, images, versions, volumes, paths, ingress,
and internal naming. Source-tree tools select it through `PLATFORM_CONFIG`; the
installed operator launcher pins
`/srv/openstack-platform/config/platform.json`; guests use the immutable
NixOS-provided `/etc/<namespace>/platform.json`.

The inventory is non-secret but private because it exposes deployment topology.
The separate mode-`0600` operator policy contains the standard worker/storage
profile, digest-pinned Node and Bun runtime images, and the public age recipient
for the two SQLite backup classes. Credentials, private PKI, age identities,
Nomad tokens, storage administrator credentials, and SSH private keys do not
belong in either file.

`infra/lib/platform_contract.json` is the canonical cross-language
implementation contract. It defines role sets, ports, service accounts,
executables, paths, protocols, and required inventory keys. Python consumes the
packaged projection through `openstack_platform.contracts`; standalone
infrastructure scripts use `infra/lib/platform_contract.py`; Nix imports it
through `nix/lib/constants.nix`; shell receives an allowlisted projection from
`infra/lib/platform_config.py`. Changing the contract is a coordinated release,
not a deployment edit.

The controller database identity hashes the OpenStack project UUID, namespace,
and stable inventory fields: network/address identity, resource names, volumes,
paths, and PKI naming. Image, flavor, version, checksum, and container changes
are excluded so ordinary upgrades do not strand state. A copied database from a
different stable identity is rejected.

## Roles and state ownership

![Platform architecture](architecture-overview.svg)

| Role | Lifetime | Main responsibility | Durable state |
| --- | --- | --- | --- |
| `admin` | Persistent | Nomad control, controller, constrained helper, monitoring, backup staging | Controller/Nomad state and backup volume |
| `ingress` | Persistent | Public platform and healthy Nomad routing | None |
| `storage` | Persistent | PostgreSQL, MongoDB, Garage S3, and OCI registry | Managed-data volume |
| `worker` | Replaceable | One application's Nomad allocation | None |
| `builder` | Single use | Rootless BuildKit build from one source snapshot | None |

The admin-state volume contains hosted-controller SQLite, Nomad state,
operator-helper releases, and diagnostics. The storage volume contains managed
services and registry data. The backup volume contains encrypted logical
backups and committed verification evidence.

Persistent host replacement keeps the fixed port and retained volumes. It
creates a candidate host from the selected exact image UUID, verifies identity
and role readiness, and removes the old host only after acceptance. A failed
candidate returns to the old host; an ambiguous provider result is journaled as
recovery-required.

## Processes and trust boundaries

### Operator host

`openstack-platform` is the unprivileged infrastructure CLI. It owns greenfield
setup, infrastructure status, image selection/pruning, persistent-host
lifecycle, external operator-state backup, and offline restore. It has no
application, deployment, environment, or managed-storage mutation commands.
Its SQLite database is separate from the hosted controller database.

The operator reaches admin only through the generated `platform-admin` SSH
alias. That alias pins the address, `agentops` account, ED25519 host key,
identity file, strict checking, and forwarding restrictions. Provider calls use
the separately protected `platform-openstack` executable; releases do not carry
cloud credentials.

### Admin host

`openstack-platform-controller` runs as `platform-controller`. It owns product
records and lifecycle orchestration. It calls a fixed local helper rather than
holding broad OpenStack, Nomad, registry, or storage administrator interfaces in
the HTTP process.

`openstack-platform-helper` accepts one strict protocol-v1 JSON request. The
release-bound `openstack_platform/helper/actions-v1.txt` allowlist must exactly
match production handlers. Helper code performs application/Nomad operations,
managed-storage provider operations, and backup evidence acceptance. Request
input cannot select an executable, remote host, provider credential, or general
shell command.

### Controller sockets

The admin role exposes two mode-`0660` Unix sockets:

| Socket | Peer | Capability |
| --- | --- | --- |
| `/run/<namespace>-controller/project.sock` | `management-broker` UID/GID | Health and non-destructive product routes |
| `/run/<namespace>-controller/privileged.sock` | Operator UID/GID | Administrator reads, cascade application deletion, and storage deletion |

The server authenticates every connection with Linux `SO_PEERCRED` before
parsing HTTP. HTTP input cannot select or upgrade a socket capability. The
transport authenticates the local host process, not a browser user; ownership,
quota, session, and CSRF decisions belong to the future management broker.

## Greenfield setup flow

`openstack_platform.setup` resolves setup input and checkpoints the operation.
At a high level it:

1. validates the clean full commit, signed component/artifact evidence, local
   tools, protected files, authenticated project, quota, names, and addresses;
2. generates private inventory, policy, SSH identities, service credentials,
   backup identity, and internal PKI;
3. reconciles security groups and reserves persistent fixed ports;
4. evaluates and builds each NixOS role, QEMU-boots the exact QCOW2 with a
   config drive, verifies signed artifact identity, and publishes to Glance;
5. creates admin, establishes host-key and SSH/provider bridges, and bootstraps
   Nomad ACLs;
6. creates storage and ingress, verifying exact serial readiness markers;
7. installs the matching operator and helper releases;
8. seeds all five accepted image UUIDs, then starts and verifies the hosted
   controller service, readiness unit, socket ownership/mode, peer restrictions,
   and hosted-controller backup timer;
9. records the same image UUIDs in operator state and initializes backup
   schedules; and
10. requires healthy aggregate status before reporting completion.

Setup is resumable because generated private material and provider mutations are
checkpointed and re-observed. Existing resources are reused only when their
full expected projection matches. There is no import or name-only adoption
phase.

## Application lifecycle

The implemented controller supports a future management caller, but there is no
supported browser caller today. For a deployment request the controller:

1. validates the application UUID/slug, public credential-free GitHub URL,
   requested ref, exact commit, configuration revision, and closed typed
   configuration;
2. records an immutable deployment attempt and accepts the external operation
   under an idempotency key;
3. asks the helper to create a single-use builder;
4. acquires the exact source snapshot and transfers a generated recipe;
5. runs rootless BuildKit and records the pushed immutable OCI digest;
6. deletes and verifies the builder and its fixed port;
7. creates or verifies the application's dedicated worker;
8. submits the generated Nomad job and candidate route;
9. accepts only after scheduler, application, and public-route health pass; and
10. removes a failed candidate with bounded cleanup while preserving the prior
    accepted route.

The repository supplies source and supported lockfiles only. Deployment
configuration is a caller-owned immutable snapshot. Node/Bun package paths and
script names are data, not shell command strings. Storage bindings map typed
resource outputs to runtime environment keys; they do not create or delete
storage.

Environment values and managed-service credentials live in owner-scoped Nomad
Variables. Controller SQLite records key names, owners, revisions, and
timestamps, never values. API reads never return values.

## Operator CLI reference

Global syntax:

```text
openstack-platform [--platform-config PATH] [--state-directory PATH]
  [--policy PATH] COMMAND
```

Implemented commands:

```text
openstack-platform setup check --env-file PATH [--cloudflare-token-file PATH] [--json]
openstack-platform setup --env-file PATH [--workspace PATH]
  [--cloudflare-token-file PATH] --apply
openstack-platform status
openstack-platform backup
openstack-platform restore BACKUP [--age-identity IDENTITY] --yes
openstack-platform infra list
openstack-platform infra image list
openstack-platform infra image set admin|ingress|storage|worker|builder IMAGE
openstack-platform infra image prune [--apply --yes]
openstack-platform infra logs admin|ingress|storage [--lines COUNT]
openstack-platform infra start admin|ingress|storage
openstack-platform infra stop admin|ingress|storage --yes
openstack-platform infra reboot admin|ingress|storage --yes
openstack-platform infra replace admin|ingress|storage --yes
```

`status` combines accepted state with bounded live observations. `APPS` and
`STORAGE` are aggregate controller counts; the CLI cannot list or mutate those
records. Image selection records exact provider UUID, source commit, and
compatibility identity. Pruning is plan-first and protects selected,
server-referenced, unfinished-operation, and retained-history images.

The installed restore launcher targets external operator state at
`/srv/openstack-platform/state/platform.sqlite3`. Its dedicated
`--replacement-state-directory` option supports an absent database in a
private drill directory. Restore contacts no provider or network service and
validates deployment identity, schema, SQLite integrity, foreign keys,
sidecars, and unfinished operations before atomic replacement.

CLI exits are `0` success, `1` safe operation failure, `2` usage/validation,
`3` conflict or recovery-required, `4` unavailable dependency, and `130`
interrupt. Unexpected failures expose a correlation ID and write a private,
bounded diagnostic.

## Controller HTTP transport

The controller speaks HTTP/1.1 JSON over Unix streams. Requests and responses
are limited to 1 MiB. The parser rejects chunked request bodies, duplicate JSON
keys, repeated length/idempotency headers, non-finite numbers, unknown mutation
fields, and unsupported media types.

The transport admits at most 64 connections globally and 16 per peer UID. The
Nix service further configures eight active connections for each accepted peer.
Excess connections receive retryable `503 CONNECTION_LIMIT` with
`Retry-After: 1`. Header, body, write, and idle deadlines are 5, 30, 5, and 15
seconds; one connection serves at most 100 requests. Shutdown closes admitted
sockets rather than waiting for stalled clients.

Every mutation requires a canonical lowercase UUID `Idempotency-Key`. Repeating
an identical request replays the recorded result. Reusing the key with changed
method, path, or body returns `409 IDEMPOTENCY_CONFLICT`.

Database-only application creation returns `201`. External mutations durably
reserve application scope and return `202` with an operation resource before
external work. Four workers execute at most 32 admitted running/queued
operations, serialized per application. Reads and polling use short independent
SQLite transactions.

Started work interrupted by controller restart becomes `recovery_required`.
The caller resumes it by repeating the identical request and key. Request bodies
are not retained in the dispatch journal, so secret-bearing environment bodies
must be supplied again. A new key cannot bypass a recovery-required operation
on the same application.

### Project and privileged routes

All IDs are canonical UUIDs. The project socket exposes these non-destructive
routes; the delete routes shown below are installed only on the privileged
socket.

| Method and route | Purpose |
| --- | --- |
| `GET /v1/health` | Project-socket readiness |
| `POST /v1/applications` | Create a controller application from a slug |
| `GET /v1/applications/{id}` | Read one application |
| `POST /v1/applications/{id}/enable` | Enable an accepted application |
| `POST /v1/applications/{id}/disable` | Disable an application |
| `POST /v1/applications/{id}/delete` | Privileged cascade deletion with slug confirmation |
| `POST /v1/applications/{id}/deployments` | Start a typed exact-commit deployment |
| `GET /v1/applications/{id}/deployments` | List bounded deployment history |
| `GET /v1/deployments/{id}` | Read one deployment attempt |
| `GET /v1/deployments/{id}/build-log` | Read bounded retained build output |
| `GET /v1/applications/{id}/runtime-log` | Read bounded current runtime output |
| `GET /v1/applications/{id}/environment` | List environment names and metadata, never values |
| `PUT /v1/applications/{id}/environment/{key}` | Add or replace one value |
| `DELETE /v1/applications/{id}/environment/{key}` | Remove one caller-owned value |
| `POST /v1/applications/{id}/environment/import` | Strict dotenv import |
| `POST /v1/applications/{id}/storage` | Create PostgreSQL, MongoDB, or S3 storage |
| `GET /v1/applications/{id}/storage` | List bounded application storage |
| `GET /v1/storage/{id}` | Read one storage resource |
| `PATCH /v1/storage/{id}/label` | Change its display label |
| `POST /v1/storage/{id}/verify` | Verify provider identity and health |
| `POST /v1/storage/{id}/rotate` | Rotate scoped credentials |
| `DELETE /v1/storage/{id}` | Privileged deletion with machine-name confirmation |
| `GET /v1/operations/{id}` | Poll an accepted mutation |

The privileged socket also exposes bounded administrator views:

| Method and route | Purpose |
| --- | --- |
| `GET /v1/admin/status` | Aggregate accepted and live status |
| `GET /v1/admin/hosts` | Persistent-host observations |
| `GET /v1/admin/images` | Selected/candidate image observations |
| `GET /v1/admin/applications` | Paginated global application list |
| `GET /v1/admin/deployments` | Paginated global deployment list |
| `GET /v1/admin/storage` | Paginated global storage list |
| `GET /v1/admin/operations` | Paginated global operation list |

List pages default to 50 and accept 1–100. Runtime/build logs default to 200
lines; runtime accepts at most 1,000 and build reads are additionally bounded by
the configured byte limit.

Responses include JSON content type, `Cache-Control: no-store`, and a
correlation ID. Errors expose only a code, bounded safe summary, correlation ID,
retryability, and optional blocking operation ID. Provider payloads, stack
traces, secret values, and private operation references are not returned.

## SQLite and operation state

`openstack_platform/controller/database.py` owns schema creation, forward
migrations, deployment binding, applications, immutable deployment attempts,
active deployment pointers, environment metadata, managed resources, image
selection, slug tombstones, idempotency requests, external-operation journals,
and dispatch state.

Database calls are intentionally short. External work is coordinated in domain
services and uses operation checkpoints rather than a database transaction held
across provider calls. Per-application admission prevents two external
mutations from changing the same scope concurrently.

Provider observations are evidence, not accepted state. Reconciliation never
creates product records merely because a matching server, job, database, or
bucket exists. Unsupported prior schemas fail before migration or provider
mutation.

## Backup and recovery model

The deployment has three independent backup classes:

1. **Hosted controller:** the live controller SQLite database under admin state,
   encrypted on admin to `<paths.backups>/hosted-controller` with its private
   identity held off-platform.
2. **External operator state:** the operator CLI SQLite database, backed up
   locally with SQLite's online API and encrypted to
   `<paths.backups>/controller`.
3. **Managed data:** encrypted PostgreSQL, MongoDB, Garage catalog/data, and
   retained OCI manifests/blobs under timestamped namespace directories.

Each accepted set uses ciphertext/data, checksums, and a final manifest as its
commit marker. Managed restore verification uses disposable PostgreSQL and
MongoDB containers and validates Garage/OCI archives before writing
`RESTORE-MANIFEST`.

Off-site export chooses only committed sets, verifies every copy, writes an
append-only canonical manifest, and updates a credential-free health receipt.
The destination must be a distinct mounted filesystem and provider retention is
operator-owned. Full-loss recovery restores both SQLite databases and managed
data into explicit replacement targets; it does not depend on GitHub or the
original registry because retained OCI artifacts are included.

## Network and workload isolation

Tunnel ingress binds Traefik to loopback and opens no public origin port. Direct
ingress applies one canonical provider IPv4 set to Neutron, the NixOS firewall,
and trusted forwarded headers. In both modes the external provider terminates
browser HTTPS and preserves `Host`.

Builders deny cloud metadata, use a dedicated unprivileged SSH identity from
admin, run rootless BuildKit, have registry push access only for their task, and
expire. Workers have no SSH service, deny metadata, accept application traffic
only from ingress, and run generated Nomad jobs with no privileged container or
host-volume option. Runtime credentials are scoped to one application and
injected through Nomad Variables.

The internal CA authenticates retained role/service relationships. Its private
material and deployment credentials are supplied after image build and are not
stored in public source or the Nix store.

## Future management application

> **TODO:** implement the sync-engine management application and its external
> authentication application. The host identities and controller project
> boundary exist; browser login, user/project ownership, quota, sessions, audit,
> and the UI do not.

The intended process chain is:

```text
browser -> HTTPS ingress -> management-web renderer
                            -> typed Unix request -> management-broker
                                                     -> controller project.sock

operator-side privileged client --------------------> controller privileged.sock
```

`management-web` is an untrusted renderer. It owns no authoritative session or
project state, cannot open a controller socket, and can reach only the broker's
Unix socket. `management-broker` owns sessions, users, project ownership, quota,
desired state, audit records, idempotency keys, and reconciliation. It is the
exact peer accepted by `project.sock`, has no TCP/IP access, and cannot open the
privileged socket. Neither process can read controller SQLite, cloud/Nomad
credentials, storage administrator credentials, builder/backup keys, age
identities, helper diagnostics, or build logs directly.

The authentication integration will exchange a short-lived, single-use browser
code and accept only Ed25519 JWTs with configured key ID, issuer, audience,
subject, role, issue time, and expiry. The browser receives an opaque
server-side session cookie using the `__Host-` prefix, `Secure`, `HttpOnly`,
`SameSite=Lax`, and no `Domain`. Cookie-authenticated mutations require exact
origin and session-bound CSRF verification.

The broker and controller databases do not share a transaction. The broker
records intent and a canonical UUID idempotency key before calling the
controller. Timeout or disconnect is an unknown result: reconciliation repeats
the identical request/key or polls the known operation, never allocates a new
key for the same intent.

The planned UI includes:

- a project dashboard with quota and observed operation state;
- immutable slug/stable URL, public GitHub repository, and preferred branch;
- typed Node/Bun package, package-script, port, health, and storage-binding
  configuration;
- separate save and exact-commit deploy actions with bounded build history;
- write-only environment add/remove/import with no reveal or export;
- PostgreSQL, MongoDB, and S3 create/label/verify/rotate without credential
  display; and
- enable and disable actions.

Cascade application deletion, provider-backed storage deletion, and global
administrator reads remain absent from the browser UI. They require a separate
reviewed privileged operator client. Secret submissions and authentication
JWTs must bypass any framework event/action persistence that would record their
values.

## Failure invariants

Maintain these invariants when changing the implementation:

- A build or upload creates a candidate; role-specific live checks create
  acceptance.
- A provider name match is never enough to adopt, mutate, or delete a resource.
- An unavailable observation does not erase accepted state.
- Builder cleanup is required after success and failure.
- Candidate deployment failure preserves the prior accepted route.
- Worker deletion does not delete managed data or deployment history.
- Persistent replacement retains the prior server and volumes through candidate
  readiness.
- Unknown external results remain journaled and resumable under the same intent.
- Environment and managed-service values do not enter controller SQLite or API
  reads.
- Product mutation does not move into the operator CLI.
- Executable rollback, database restore, and provider-state recovery are
  separate operations.
