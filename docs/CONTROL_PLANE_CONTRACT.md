# Operator CLI and local controller API reference

This page defines the two implemented control surfaces:

- `openstack-platform`, the supported operator-only CLI for setup,
  infrastructure, backup, and recovery; and
- `openstack-platform-controller`, the local Unix-socket product API intended
  for the future management application.

The CLI has no application, deployment, environment, or managed-storage
commands. The controller has no public listener, browser authentication, or
project authorization. Automated setup installs the policy and helper release;
the admin role starts and locally exposes the controller service. There is no
management application or supported end-user product workflow yet. See
[MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md) for the
future UI and authentication integration.

## Operator CLI

### Global syntax

```text
openstack-platform [--platform-config PATH] [--state-directory PATH]
  [--policy PATH] COMMAND
```

Except for `setup`, the defaults are:

```text
--platform-config  $PLATFORM_CONFIG, or config/platform.json
--state-directory  /srv/openstack-platform/state
--policy           <state-directory>/policy.json
```

The installed launcher pins the installed inventory rather than relying on the
source-tree default. Global options precede the command. Run as the
unprivileged `/srv/openstack-platform` owner, never as root.

The command database starts empty and is initialized or migrated on first use.
Its identity is bound to the configured project UUID, namespace, stable
resource inventory, and state paths. A database copied from another deployment
is rejected. Accepted records are not imported from provider resources.

### Greenfield setup

```text
openstack-platform setup check --env-file PATH [--cloudflare-token-file PATH] [--json]
openstack-platform setup --env-file PATH [--workspace PATH]
  [--cloudflare-token-file PATH] --apply
```

`setup check` (also available by omitting both `check` and `--apply`) authenticates and prints a resolved human or JSON plan. It checks project identity, quota deltas, provider choices, fixed-address availability, reserved-name collisions, host tooling, ingress, and release/image sources using provider reads only. It writes nothing and generates no credentials. With `--apply`, setup verifies the authenticated OpenStack
project, generates private deployment material, reserves fixed ports, builds
and tests all five role images, creates the persistent roles and volumes,
installs the policy and operator/helper releases, verifies the hosted controller
service/socket/API boundary, selects images, and initializes backups.

The environment file must be a direct current-user-owned mode-`0600` file.
Setup parses literal dotenv/OpenRC assignments without executing them. Missing
non-secret choices may prompt; password input uses a hidden terminal prompt.
The default workspace is `/srv/openstack-platform/setup`. Fresh volume defaults
are 32 GiB for admin state, 500 GiB for managed data, and 600 GiB for backups. Setup rejects a backup volume smaller than managed data.

Cloudflare account and DNS administration remain external. A direct mode-`0600`
token file enables the reference tunnel during ingress bootstrap. Without one,
setup completes the OpenStack deployment and reports public ingress as pending.
See [SETUP.md](SETUP.md) for inputs, ordered mutations, verification, and retry
boundaries.

### Status and infrastructure reads

```text
openstack-platform status
openstack-platform infra list
openstack-platform infra image list
openstack-platform infra logs admin|ingress|storage [--lines COUNT]
```

`status` combines accepted counts with bounded live observations. Its columns
are:

```text
STATE INFRA APPS STORAGE LIVE UNAVAILABLE UNHEALTHY
```

`APPS` and `STORAGE` are aggregate management-state counts only. The CLI
cannot list or mutate those records. An unavailable observation does not erase
accepted state. On a fresh database before image selection, the three
persistent-role observations are unavailable.

`infra list` shows accepted role image metadata and bounded live state.
`infra image list` lists candidate images with safe metadata. Host logs are
bounded to 1–2,000 lines; the default is 200.

### Select and prune images

```text
openstack-platform infra image set admin|ingress|storage|worker|builder IMAGE
openstack-platform infra image prune
openstack-platform infra image prune --apply --yes
```

Image selection resolves the provider image and records its exact UUID, full
source commit, and compatibility hash. Incomplete or incompatible metadata is
rejected.

Pruning is plan-first. The plan protects selected images, images referenced by
unfinished operations or servers, and the configured newest complete images
per role. Incomplete metadata-bearing images are review-only. Apply re-reads
UUIDs, fingerprints, server references, and protections under the
infrastructure lock and refuses drift. Deletion is checkpointed per UUID;
ambiguous or interrupted results become recovery-required.

### Operate persistent hosts

```text
openstack-platform infra start  admin|ingress|storage
openstack-platform infra stop   admin|ingress|storage --yes
openstack-platform infra reboot admin|ingress|storage --yes
openstack-platform infra replace admin|ingress|storage --yes
```

Stop, reboot, and replace require confirmation. Replacement retains the old
server and required volumes until the candidate passes role readiness and the
provider re-confirms the selected image UUID, retained flavor UUID, configured
name, and operation provenance. On readiness failure, the prior host is
restored. This is the only supported persistent-host replacement path.

### Back up operator state

```text
openstack-platform backup
```

The command uses SQLite's online backup API, encrypts the private copy to the
policy `backupAgeRecipient`, and stages it through the pinned admin alias at:

```text
<paths.backups>/controller/.staging/<name>
```

Helper acceptance verifies the age-v1 header and ciphertext SHA-256, then
publishes the ciphertext, checksum, and manifest under
`<paths.backups>/controller/`. The manifest is the commit marker. Retention counts only
complete evidence trios. This backup contains only the external operator CLI
SQLite state. It does not contain the hosted controller database, managed
PostgreSQL, MongoDB, Garage data, registry blobs, or an age identity. The
hosted-controller backup and restore procedure is in [Operations](OPERATIONS.md#hosted-controller-database-on-admin).

### Restore operator state offline

```text
openstack-platform --state-directory PATH restore BACKUP
  [--age-identity IDENTITY] --yes
```

The installed fixed-destination launcher is preferred for live state:

```text
/srv/openstack-platform/bin/openstack-platform-restore BACKUP
  [--age-identity IDENTITY] --yes
```

The launcher targets `/srv/openstack-platform/state/platform.sqlite3` by
default. For a disposable recovery drill, the supported leading
`--replacement-state-directory PATH` option targets the absent
`PATH/platform.sqlite3`; `PATH` must already be a private mode-`0700` direct
directory. Restore contacts no provider,
helper, SSH, Nomad, or network service. It requires private direct files and a
private destination directory; verifies deployment identity, known schema,
SQLite integrity, foreign keys, and unfinished operations; then atomically
replaces the destination. A failed verification leaves the existing database
unchanged.

After restore, use `status` and `infra list` to compare aggregate accepted state
with live observations. If `APPS` or `STORAGE` is nonzero, preserve the database
for controlled management integration or recovery; the operator CLI intentionally
has no product-state reconciliation commands.

### CLI recovery and exits

Mutations journal checkpoints and serialize infrastructure work. Lock waits and
external calls share bounded deadlines. For an interrupted operation:

1. do not edit SQLite or manually rename, detach, delete, or recreate the
   referenced provider resource;
2. restore the dependency named by the safe error; and
3. rerun the same operator command with the same role, image, and confirmation.

Exit codes are `0` success, `1` safe operation failure, `2` usage or validation
failure, `3` conflict/recovery-required, `4` unavailable dependency, and `130`
interrupt. Unexpected failures print a correlation ID and write a private
bounded diagnostic below the state directory.

## Local controller API

### Availability and trust boundary

`openstack-platform-controller` is implemented, packaged, and run by the admin
NixOS role under the dedicated `platform-controller` account. It starts after
the retained state mount, Nomad, controller policy, and operator-owned helper
release are available. The controller exposes separate mode-`0660` Unix sockets. The project socket is
grouped to `controller-api` and accepts only the configured
`management-broker` UID/GID. The privileged socket is grouped to `platform-admin` and accepts only
the configured operator UID/GID. Both sockets authenticate every accepted
connection with Linux `SO_PEERCRED` and limit each allowed peer to eight active
connections in the Nix service. Browser login, project authorization, quota,
and the management application remain unimplemented. The exact host and route
capability contract is in
[MANAGEMENT_CONTROLLER_BOUNDARY.md](MANAGEMENT_CONTROLLER_BOUNDARY.md).

The executable syntax is:

```text
openstack-platform-controller
  [--platform-config PATH]
  [--state-directory PATH]
  [--policy PATH]
  [--socket PATH]
  [--socket-group GROUP]
  [--project-peer UID:GID]...
  [--privileged-socket PATH]
  [--privileged-socket-group GROUP]
  [--privileged-peer UID:GID]...
  [--max-connections-per-peer COUNT]
```

Its defaults are:

```text
--platform-config  $PLATFORM_CONFIG, or /etc/openstack-platform/platform.json
--state-directory  /srv/openstack-platform/state/controller
--policy           <state-directory>/policy.json
--socket                    /run/openstack-platform/controller.sock
--privileged-socket         <socket-directory>/privileged.sock
--project-peer              controller effective UID:GID when omitted
--privileged-peer           controller effective UID:GID when omitted
--max-connections-per-peer  8
```

The admin service supplies deployment-specific values:
`/etc/<namespace>/platform.json`, `<adminState>/controller/state`, and
`/run/<namespace>-controller/project.sock` and
`/run/<namespace>-controller/privileged.sock`, with groups `controller-api` and
`platform-admin`, respectively.

The socket path must be absolute. Its parent must already be owned by the
controller process and grant no permissions to the `other` class. The server
creates the socket as mode `0660` and optionally assigns `--socket-group`. It removes only an owned inactive Unix
socket; it refuses a regular file, foreign socket, or active listener.

The transport authenticates the host process, not the browser user. The future
management backend must still enforce browser authentication, project
ownership, and quota before calling project routes. Administrator reads and
cascade-delete routes are absent from the project socket; HTTP input cannot
select or upgrade a socket capability.

### HTTP transport

The API uses HTTP/1.1 JSON over the Unix stream socket. Paths are under `/v1/`.
Request and response bodies are each limited to 1 MiB. Chunked request bodies,
duplicate JSON keys, repeated `Content-Length` or `Idempotency-Key` headers,
non-finite JSON numbers, unknown mutation fields, and unsupported content types
are rejected.

The transport admits at most 64 connections globally and 16 per Unix peer UID,
identified with `SO_PEERCRED`. Excess connections receive a retryable JSON `503
CONNECTION_LIMIT`, `Retry-After: 1`, and are closed without creating a worker.
Headers have a 5-second absolute deadline, bodies a 30-second absolute deadline,
response writes a 5-second deadline, and an idle keep-alive closes after 15
seconds. A connection serves at most 100 requests. Shutdown closes admitted
sockets and does not wait for stalled clients.

Every mutation requires `Idempotency-Key` containing a canonical lowercase
UUID. A repeated key with identical input replays the recorded result; the same
key with different method, path, or body returns `409 IDEMPOTENCY_CONFLICT`.
Database-only application creation returns `201`. External mutations return
`202`, `Location: /v1/operations/{id}`, and:

```json
{
  "operationId": "00000000-0000-4000-8000-000000000001",
  "statusUrl": "/v1/operations/00000000-0000-4000-8000-000000000001",
  "result": {"kind": "operation", "id": "00000000-0000-4000-8000-000000000001"}
}
```

The controller durably records the idempotency result and reserves the
application scope before returning `202`; external provider, helper, build,
health-check, and cleanup work runs after acceptance. The current executor runs
at most four operations concurrently and admits at most 32 running or queued
operations. When that bound is full, a request that has not been accepted
returns retryable `503 OPERATION_QUEUE_FULL` and creates no dispatch record.

Only one external mutation for an application scope can be queued, running, or
recovery-required. A different idempotency key for that scope returns `409
OPERATION_CONFLICT` with the blocking operation ID. Repeating the accepted key
and identical input returns the original `202` response and does not enqueue a
second execution. Reads and operation polling use separate short SQLite
transactions and remain available while external work runs. Callers determine
completion from the operation resource, not request duration.

A newly accepted operation can initially report `status: "running"` and
`phase: "queued"` or `phase: "executing"`. The dispatch journal deliberately
contains no request body, because environment bodies can contain secrets. On
startup, the controller does not guess or replay interrupted work. A dispatch
that never left the durable queue is marked failed with cleanup not required;
a caller can retry that request with a new idempotency key. A dispatch that had
started is changed to `recovery_required` with phase `startup_interrupted`,
preserving its scope and idempotency result. To resume it, repeat the identical
method, path, body, and `Idempotency-Key`; the controller verifies the stored
fingerprint and re-dispatches recovery using the newly supplied body without
persisting that body. This is required for environment mutations because their
secret values are intentionally absent from the dispatch journal. A changed
request with the same key returns `409 IDEMPOTENCY_CONFLICT`, and a different
key for the application returns `409 OPERATION_CONFLICT` until the original
operation becomes terminal.

### Product routes

All `{id}` values are canonical UUIDs.

| Method and route | JSON body or query |
| --- | --- |
| `GET /v1/health` | none; project-socket readiness only |
| `POST /v1/applications` | `{"slug": string}` |
| `GET /v1/applications/{id}` | none |
| `POST /v1/applications/{id}/enable` | absent or `{}` |
| `POST /v1/applications/{id}/disable` | absent or `{}` |
| `POST /v1/applications/{id}/delete` | `{"confirmation": exactSlug}` |
| `POST /v1/applications/{id}/deployments` | repository, exact commit, requested ref, configuration revision, and typed configuration; all required |
| `GET /v1/applications/{id}/deployments` | `limit` and `cursor` optional |
| `GET /v1/deployments/{id}` | none |
| `GET /v1/deployments/{id}/build-log` | `lines` and `offset` optional |
| `GET /v1/applications/{id}/runtime-log` | `lines` optional |
| `GET /v1/applications/{id}/environment` | none; values are never returned |
| `PUT /v1/applications/{id}/environment/{key}` | `{"value": string}` |
| `DELETE /v1/applications/{id}/environment/{key}` | absent or `{}` |
| `POST /v1/applications/{id}/environment/import` | `{"dotenv": string}` |
| `POST /v1/applications/{id}/storage` | `{"type": "postgres"|"mongo"|"s3", "name"?: string}` |
| `GET /v1/applications/{id}/storage` | `limit` and `cursor` optional |
| `GET /v1/storage/{id}` | none |
| `PATCH /v1/storage/{id}/label` | `{"displayLabel": string}` |
| `POST /v1/storage/{id}/verify` | absent or `{}` |
| `POST /v1/storage/{id}/rotate` | absent or `{}` |
| `DELETE /v1/storage/{id}` | `{"confirmation": exactMachineName, "purge"?: boolean}` |
| `GET /v1/operations/{id}` | none |

The deployment body has exactly these fields:

```json
{
  "repository": "https://github.com/OWNER/REPOSITORY",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "requestedRef": "main",
  "configurationRevision": 1,
  "configuration": {
    "schemaVersion": 1,
    "build": {
      "runtime": "node",
      "packages": ["."],
      "buildScript": "build",
      "startScript": "start"
    },
    "runtime": {"port": 3000, "healthPath": "/health"},
    "storageBindings": []
  }
}
```

Configuration is supplied by the caller as an immutable typed snapshot. The
source repository supplies code and supported lockfiles, not platform
configuration. Build and start values are package-script names, not shell
commands. The schema rejects Dockerfile selection, command text, build
arguments, build-time environment, provider IDs, host paths, resource
provisioning, and unknown fields.

A storage binding identifies an existing active resource by controller UUID and
maps typed outputs to unique runtime environment keys. Bindings neither create
nor remove storage. Environment values remain in Nomad Variables; controller
SQLite and API reads contain names, ownership, revisions, and timestamps only.

### Administrator reads

| Route | Pagination |
| --- | --- |
| `GET /v1/admin/status` | none |
| `GET /v1/admin/hosts` | none |
| `GET /v1/admin/images` | none |
| `GET /v1/admin/applications` | optional `limit`, `cursor` |
| `GET /v1/admin/deployments` | optional `limit`, `cursor` |
| `GET /v1/admin/storage` | optional `limit`, `cursor` |
| `GET /v1/admin/operations` | optional `limit`, `cursor` |

These routes exist only on the privileged socket. The same socket contains
application cascade delete and storage delete. The browser-facing
management identities cannot open that socket; privileged workflows require
a separately reviewed operator-side client.

Paginated lists default to 50 and accept `limit` from 1 through 100. Log reads
default to 200 lines and accept 1 through 1,000. Build offsets are additionally
bounded by the configured build-log byte limit.

### Responses and errors

Every transport response includes `Content-Type: application/json`,
`Cache-Control: no-store`, and `X-Correlation-ID`. Error bodies are:

```json
{
  "error": {
    "code": "ERROR_CODE",
    "summary": "bounded safe summary",
    "correlationId": "canonical UUID",
    "retryable": false,
    "operationId": "optional conflicting operation UUID"
  }
}
```

Implemented result classes include invalid request/body/query/JSON/media,
not-found, method-not-allowed, idempotency conflict, unfinished-operation
conflict, operation-queue-full, state conflict, deadline exceeded, dependency
unavailable, helper or external-operation failure, and bounded internal
failure. Provider payloads, stack traces, secret values, and operation refs are
not returned.
