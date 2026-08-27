# Management application specification

## Purpose and completion boundary

This document is the implementation brief for the sync-engine management
application. The application runs on the persistent admin host and gives signed-in
users a browser UI for projects, deployments, environment metadata, managed
storage, and operation progress. It calls the local controller API; it never
invokes the operator CLI, SSH, Nomad, OpenStack, registry, or storage providers.

The application is complete when a user can perform the full project lifecycle
without shell or infrastructure credentials, administrators can inspect safe
platform state, and the security and reconciliation scenarios below pass.

Private repositories, repository webhooks, custom domains, preview products,
teams, scaling, scheduled jobs, cancellation, shell access, database consoles,
visible credentials, and persistent runtime-log archives are outside this build.

## Process and trust boundary

```text
browser -> HTTPS ingress -> management web :8080
                            |
                            `-> HTTP over /run/openstack-platform/controller.sock
                                -> trusted controller and local constrained helper
```

The management process runs as the dedicated `management-web` account. It can:

- listen on port 8080;
- read its non-secret configuration and authentication public keys;
- write only its own durable application/session state; and
- connect to the controller socket through the restricted API group.

It cannot read controller SQLite, OpenStack credentials, Nomad tokens, storage
administrator credentials, builder keys, backup keys, age identities, helper
diagnostics, or controller build logs directly. All product reads and mutations
use the API models below.

## Authoritative ownership

| Owner | Durable facts |
| --- | --- |
| Authentication application | Login interaction, one-time codes, Ed25519 signing keys, stable subject, global `user` or `admin` role |
| Management application | User profile, server session, project ownership, display name, repository, preferred branch, desired typed configuration and revision, desired enabled state, per-user quota reservation, confirmations, product audit, controller IDs, idempotency keys, and observed operation state |
| Controller | Application UUID/slug, lifecycle execution, immutable deployment attempts and snapshots, active deployment and worker, environment key metadata/revision, storage lifecycle, provider-safe evidence, operation journals, and slug tombstones |
| Nomad Variables and providers | Environment and storage credential values |

The management and controller databases do not share a transaction. Management
records intent before each controller call and stores the request's canonical
UUID idempotency key. A timeout or disconnected response is an unknown result:
management repeats the same request and key or reads the known operation; it
never creates a replacement key for the same intent.

## Authentication application contract

The deployment supplies these required settings:

- `AUTH_AUTHORIZE_URL`: HTTPS browser authorization endpoint;
- `AUTH_EXCHANGE_URL`: HTTPS backend code-exchange endpoint;
- `AUTH_ISSUER`: exact JWT `iss`;
- `AUTH_AUDIENCE`: exact JWT `aud` for this management application;
- `AUTH_PUBLIC_KEYS_FILE`: direct non-secret JSON file mapping accepted `kid`
  values to Ed25519 public keys; and
- `PUBLIC_ORIGIN`: exact management origin used for callbacks and origin checks.

### Login

1. `GET /auth/login` creates a short-lived server-side login transaction with a
   random state value and exact callback URI.
2. The backend redirects to `AUTH_AUTHORIZE_URL` with only `state` and
   `redirect_uri` query fields.
3. The authentication application redirects to
   `${PUBLIC_ORIGIN}/auth/callback?code=...&state=...`.
4. The backend consumes the state once and sends a backend JSON `POST` to
   `AUTH_EXCHANGE_URL` with exactly `code` and `redirectUri`.
5. A successful exchange returns exactly `{"token":"<JWT>"}`. The code is
   short-lived and single-use; replay is rejected.
6. Management accepts only JWT `alg=EdDSA` with a configured `kid`, valid
   Ed25519 signature, exact issuer/audience, stable non-empty `sub`, role
   `user` or `admin`, bounded `iat`, and unexpired bounded `exp`.
7. Management creates a fresh server-side session and redirects to `/`.

The JWT never enters a URL, cookie, browser storage, analytics, application
event, access log, or long-lived management record. Public-key rotation keeps
current and next keys accepted during a configured overlap. Unknown keys fail
closed; the application does not fetch keys selected by token input.

### Session and request security

The browser receives only an opaque random session identifier in
`__Host-platform_session` with `Secure`, `HttpOnly`, `Path=/`, `SameSite=Lax`,
and no `Domain`. Session creation rotates any prior identifier. Logout and
expiry invalidate server state.

Every cookie-authenticated mutation requires:

- an exact `Origin` equal to `PUBLIC_ORIGIN`;
- a session-bound unpredictable CSRF value supplied outside the cookie;
- a supported content type and bounded body; and
- authorization rechecked from current management records.

Project subdomains must never receive the management cookie. Pages use a
restrictive CSP and do not load third-party scripts, analytics, or remote
assets.

## Management records

### User

- stable authentication subject, primary key;
- global role;
- quota limit and reserved enabled-project count;
- first/last sign-in timestamps; and
- disabled/session-revocation state if required by administration.

### Project

- management project UUID;
- controller application UUID;
- owner subject;
- mutable display name;
- immutable slug and stable URL;
- canonical public GitHub HTTPS repository;
- preferred branch, default `main`;
- desired deployment configuration and monotonically increasing revision;
- desired enabled state;
- quota reservation state;
- current controller operation/idempotency IDs;
- last observed controller state; and
- created/updated timestamps.

Slugs are 3–40 lowercase letters, numbers, or hyphens; start with a letter; end
with a letter or number; contain no consecutive hyphens; and reject at least
`admin`, `api`, `auth`, `status`, and `www`. Slugs never change.

A new project starts disabled. A deployment of a disabled project reserves the
same quota as enable. Successful deployment enables it. Disable releases the
reservation only after the controller confirms the disabled state. An unknown
controller result keeps the reservation pending.

### Desired deployment configuration

Management stores and edits this strict JSON shape:

```json
{
  "schemaVersion": 1,
  "build": {
    "runtime": "node",
    "packages": ["."],
    "buildScript": "build",
    "startScript": "start"
  },
  "runtime": {"port": 3000, "healthPath": "/health"},
  "storageBindings": [
    {
      "resourceId": "11111111-1111-4111-8111-111111111111",
      "outputs": {"url": "DATABASE_URL"}
    }
  ]
}
```

The UI edits typed fields only. It cannot submit shell commands, Dockerfiles,
build arguments, environment values, provider IDs, host paths, resource
selection, or unknown fields. Saving increments the configuration revision but
does not deploy.

The backend resolves the preferred branch with fixed credential-free
`git ls-remote --refs <canonical-repository> refs/heads/<branch>`, bounded time
and output, no shell, no redirects selected by user input, and
`GIT_TERMINAL_PROMPT=0`. Deployment sends the resulting exact lowercase
40-character commit. An omitted branch means literal `main`, not GitHub API
default-branch discovery.

## Controller transport

Management uses HTTP/1.1 JSON over the fixed Unix socket. Requests and responses
are bounded. Mutation JSON rejects duplicate and unknown fields. Every mutation
uses a fresh canonical UUID in `Idempotency-Key`; a retried intent reuses it.
Environment-value request bodies are never logged, traced, recorded as
sync-engine events, or persisted by management.

A database-only create returns `201`. External mutations return `202` with:

```json
{
  "operationId": "00000000-0000-4000-8000-000000000001",
  "statusUrl": "/v1/operations/00000000-0000-4000-8000-000000000001",
  "result": {"kind": "operation", "id": "00000000-0000-4000-8000-000000000001"}
}
```

The controller may finish work before returning `202`; management still reads
the operation and does not infer completion from request duration.

Errors contain only `code`, bounded `summary`, `correlationId`, `retryable`, and
an optional conflicting `operationId`. Management displays the safe summary and
correlation ID without provider payloads or stack traces.

## Controller route contract

All `{id}` values are canonical UUIDs. List responses are
`{"items": [...], "nextCursor": UUID|null, "truncated": boolean}` and accept
bounded `limit` and `cursor` query fields. Log responses accept bounded `lines`;
build logs also accept `offset`.

| Route | Request body |
| --- | --- |
| `POST /v1/applications` | `{"slug": string}` |
| `GET /v1/applications/{id}` | none |
| `POST /v1/applications/{id}/enable` | absent or `{}` |
| `POST /v1/applications/{id}/disable` | absent or `{}` |
| `POST /v1/applications/{id}/delete` | `{"confirmation": exactSlug}` |
| `POST /v1/applications/{id}/deployments` | `{"repository": URL, "commit": SHA, "requestedRef": branch, "configurationRevision": integer, "configuration": object}` |
| `GET /v1/applications/{id}/deployments` | none |
| `GET /v1/deployments/{id}` | none |
| `GET /v1/deployments/{id}/build-log` | none; query controls bounded chunk |
| `GET /v1/applications/{id}/runtime-log` | none; query controls bounded tail |
| `GET /v1/applications/{id}/environment` | none; returns names, owners, revision, timestamps—never values |
| `PUT /v1/applications/{id}/environment/{key}` | `{"value": string}` |
| `DELETE /v1/applications/{id}/environment/{key}` | absent or `{}` |
| `POST /v1/applications/{id}/environment/import` | `{"dotenv": strictBoundedText}` |
| `POST /v1/applications/{id}/storage` | `{"type": "postgres"|"mongo"|"s3", "name"?: machineName}` |
| `GET /v1/applications/{id}/storage` | none |
| `GET /v1/storage/{id}` | none |
| `PATCH /v1/storage/{id}/label` | `{"displayLabel": string}` |
| `POST /v1/storage/{id}/verify` | absent or `{}` |
| `POST /v1/storage/{id}/rotate` | absent or `{}` |
| `DELETE /v1/storage/{id}` | `{"confirmation": exactMachineName, "purge"?: boolean}` |
| `GET /v1/operations/{id}` | none |

Administrator-only UI reads use `/v1/admin/status`, `/v1/admin/hosts`,
`/v1/admin/images`, `/v1/admin/applications`, `/v1/admin/deployments`,
`/v1/admin/storage`, and `/v1/admin/operations`. The management backend—not the
controller—enforces the global admin role before making these calls.

## User interface

### Dashboard

List only management-authorized projects. Show display name, slug/URL, desired
state, observed state, active deployment commit, current operation, and safe
recovery state. Include create-project flow and quota availability.

### Project overview and lifecycle

Show stable URL, repository/branch, enabled state, active deployment, storage
summary, environment-key count, and current operation. Enable, disable, and
delete are explicit actions. Delete requires typing the exact slug and states
that all attached storage, including S3 objects, is irreversibly removed.

### Configuration and deployment

Provide controls for runtime, package paths, optional build script, required
start script, port, health path, and typed storage output bindings. Save and
deploy are separate. Deploy shows the exact resolved commit and configuration
revision before confirmation. Display every attempt, phase/status, timestamps,
safe error, cleanup state, and bounded build log.

### Environment

List key names and owners only. Support add/overwrite, remove, and strict dotenv
import. Inputs use password-style controls and are cleared after submission.
Never offer reveal, copy, export, or browser persistence. Reserved platform and
storage keys are visibly non-editable.

The secret submission path is not a normal persisted sync-engine action input.
If the framework records action arguments or events, use a dedicated host
endpoint that forwards the value directly to the controller and emits only key
name, revision, operation ID, and success/failure metadata. Apply the same rule
to dotenv payloads and authentication JWTs.

### Storage

List type, immutable machine name, mutable display label, lifecycle, limits,
and last verification. Never display credentials. Provide create, label,
verify, rotate, and remove. Removal is disabled while the active deployment
references the resource; the UI directs the user to remove the desired binding,
deploy successfully, then remove storage. S3 removal requires explicit purge
confirmation when applicable.

### Administration

Global admins can view safe platform status, hosts, selected images,
applications including tombstones, deployment attempts, managed storage with
provider identities, and incomplete/recovery-required operations. Initial UI
has no low-level infrastructure mutation controls.

## Reconciliation rules

- Store intent and idempotency key before calling the controller.
- On `201`, bind the returned controller application UUID transactionally to the
  management project.
- On `202`, store the operation ID and read it until terminal.
- On timeout/disconnect, repeat the identical request and key.
- A key/input conflict is a product error; never silently allocate another key.
- Reconcile desired and observed states on process restart and periodically.
- Keep quota reserved during enabling/enabled and while an enable/deploy result
  is unknown.
- Do not mark deletion complete until the controller operation and tombstoned
  application observation confirm it.
- Saving desired configuration never changes active deployment state.

## Required evidence

The application test suite must prove:

- JWT algorithm/signature/issuer/audience/time/key checks, code/state replay,
  key overlap, session fixation prevention, logout, and expiry;
- host-only secure cookie behavior, exact-origin and CSRF rejection;
- cross-user project isolation and admin-only reads;
- transactional quota races and unknown-result reservation handling;
- idempotent lost responses for create and every external mutation;
- strict configuration, source URL/branch resolution, and unknown-field
  rejection;
- project create, deploy, failure/history/log, disable, enable-without-build,
  and typed-slug cascade delete flows;
- environment add/remove/import without values in management DB, framework
  events, logs, traces, browser storage, or analytics;
- storage binding/removal guard, rotation, labels, and safe errors;
- operation reconciliation after management process restart; and
- accessible keyboard navigation, labels, focus/error handling, and status
  announcements for all primary flows.

Controller, live candidate routing, provider, backup, and admin-replacement
evidence remains in the platform acceptance suite rather than being mocked as a
management-application guarantee.
