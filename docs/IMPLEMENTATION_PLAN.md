# Self-hosted management platform implementation plan

## Status and goal

This is the implementation target for replacing the current operator-driven
application CLI with a self-hosted management application and local controller
API. It is not implemented yet. Until a phase is completed and its contract is
updated, the current CLI, `platform.yaml`, setup, and recovery documentation
remain authoritative.

The target lets a signed-in user manage a small web application without cloud,
SSH, Nomad, registry, or storage-administrator credentials. The management
application and controller run on the persistent `admin` host. Enabled projects
serve at `https://<slug>.mit-sdg.dev`.

## Decisions

| Area | Decision |
| --- | --- |
| Product configuration | The management UI is the only editable source. `platform.yaml` is retired. |
| Source | Public credential-free GitHub repositories only. The management backend resolves `refs/heads/<preferred-branch>` with bounded `git ls-remote --refs`, defaulting an omitted preference to `main`, and submits the exact lowercase 40-character commit. The controller fetches and verifies that commit. |
| Build input | Typed Node/Bun configuration: package paths, optional build-script name, required start-script name, port, health path, and storage bindings. No shell commands, Dockerfiles, build arguments, provider IDs, host paths, or user-selected infrastructure. |
| Deployments | One active deployment per project; every attempt is retained. One attempt may be in progress per project. No initial cancellation. |
| Failure behavior | A failed candidate must leave the previous active deployment and route serving. |
| Configuration history | The management database stores editable desired configuration. The controller stores an immutable snapshot per deployment attempt as execution evidence, not as a second editable source. |
| Project lifecycle | New projects start disabled. Projects can be enabled, disabled, deployed, or deleted. Deploying a disabled project reserves quota/capacity and a successful deployment enables it. Disable frees compute but preserves the accepted deployment, environment, storage, and history. Enable does not rebuild. Delete cascades through all attached storage and runtime resources. |
| Environment | Values are write-only and live only in Nomad Variables. Databases and output contain names, ownership, revisions, and timestamps, never values. |
| Storage | PostgreSQL, MongoDB, and S3 resources belong to one project. Machine names are immutable; UI display labels are mutable. Project deletion removes all attached storage. |
| Domains | `domain = mit-sdg.dev` produces `https://<slug>.mit-sdg.dev`; ingress must cover the base and wildcard hostnames. |
| Authentication | A controlled external application provides one-time login codes and Ed25519-signed JWTs. Full OIDC is not required. The sync-engine management application is implemented in this repository. |
| Authorization | The authentication application supplies stable subject and global `user`/`admin` role. The management database owns project authorization and per-user quotas. |
| Hosting | Management web listens on `admin:8080` behind ingress. The controller uses a restricted Unix socket and is not publicly reachable. |
| CLI | The CLI becomes a setup, infrastructure, backup, restore, diagnostic, and recovery surface. It no longer deploys or manages application product state after cutover. |
| Admin replacement | An external recovery host replaces or recovers `admin`; self-replacement is not required. |
| Retention | Deployment records and bounded build logs are retained indefinitely. Runtime logs remain bounded live logs. OCI artifacts remain bounded. |
| Initial scope | No private repositories, webhooks, custom domains, arbitrary builds, previews, scaling, persistent runtime-log archive, teams, interactive shells, instant rollback, or user cancellation. |

## Terms

- **OpenStack project**: the cloud tenant containing one platform installation.
- **Application project**: one user-managed app, slug, deployment history,
  environment, worker, and storage.
- **Deployment attempt**: one immutable request for an exact commit and UI
  configuration revision, including failed attempts.
- **Accepted deployment**: an attempt that passed build, scheduler, application,
  and route checks.
- **Active deployment**: the accepted attempt serving the stable project URL.
- **Controller**: the trusted service that validates requests, owns execution
  state, journals recovery, and invokes provider/helper boundaries.
- **Management application**: the separate sync-engine web application that
  owns users, project intent, desired configuration, authorization, and quotas.
- **External recovery host**: the off-platform bootstrap and admin-recovery host,
  not the routine management plane.

## Architecture and trust boundaries

```text
browser
  | HTTPS
  v
external DNS/TLS provider
  | HTTP with original Host
  v
ingress / Traefik
  | admin:8080 (security-group restricted to ingress)
  v
management web application (unprivileged account)
  | versioned bounded JSON over restricted Unix socket
  v
controller (trusted account)
  |-- controller SQLite, locks, diagnostics, bounded build logs
  |-- local constrained helper / Nomad / storage / registry
  `-- protected OpenStack provider adapter
```

The repository already opens admin port 8080 and routes platform-level hosts
from ingress to it. The management web account must be separate from the
controller account and unable to read OpenStack, Nomad, storage, builder,
backup, or age credentials.

The helper remains a strict action boundary even though it becomes local. The
local transport must preserve the fixed action allowlist, bounded JSON envelope,
deadlines, safe errors, and prohibition on secret or raw provider output.
Runtime requests cannot select the helper transport or executable path.

### Component ownership

| Component | Owns |
| --- | --- |
| Authentication application | Login, one-time codes, JWT signing, stable subject, global role |
| Management application/database | Sessions, project ownership, display name, repository, preferred branch, editable configuration and revision, per-user quota, confirmations, product audit, controller IDs and idempotency keys |
| Controller/database | Application UUID and slug, lifecycle state, deployment attempts/snapshots, active deployment, workers, environment metadata, storage lifecycle, image selections, idempotency, technical operations, safe failures |
| Nomad Variables/providers | Environment and storage credential values |
| Helper | Fixed Nomad, worker, builder, registry, backup, and storage actions |
| External recovery host | Initial bootstrap, planned/unplanned admin replacement, offline restore |

The management and controller databases do not share a transaction. Every
mutation therefore uses a durable idempotency key supplied in the
`Idempotency-Key` request header:

1. management records intent and the key;
2. controller stores the canonical request fingerprint and result identity;
3. repeating the same key/request returns the original result;
4. repeating the key with different input returns conflict; and
5. after a timeout, management queries/repeats by key rather than creating a
   replacement resource.

A lost response is an unknown result, not failure evidence.

## Authentication and sessions

The minimal login protocol is:

1. the user signs in to the existing authentication application;
2. it creates a short-lived, single-use code and redirects to the management
   callback with the code;
3. the management backend exchanges the code over a backend request;
4. the authentication application returns an Ed25519-signed JWT containing
   `sub`, `role`, `iss`, `aud`, `iat`, `exp`, and `kid`; and
5. management verifies a fixed algorithm, signature, issuer, audience, and time
   bounds, then creates a server-side session.

The JWT must never appear in the redirect URL, local storage, analytics, or
access logs. Signing-key rotation must allow an overlap between current and
next public keys.

The management cookie must use the `__Host-` prefix, `Secure`, `HttpOnly`,
`Path=/`, and no `Domain` attribute. This prevents project subdomains from
receiving it. Cookie-authenticated mutations also require CSRF and origin
checks.

The controller trusts only the local management service through Unix-socket
permissions. It does not query the authentication database or implement
user-to-project authorization again.

## Project and configuration model

### Project identity

The management record contains its own project ID, controller application UUID,
owner subject, mutable display name, immutable slug, canonical GitHub URL,
preferred branch, desired configuration revision, desired enabled state, quota
reservation, observed controller state, idempotency/operation IDs, and
timestamps.

The controller record contains immutable application UUID and slug, stable URL,
lifecycle state, active deployment UUID, active worker identity, environment
revision, and timestamps.

Slugs keep the current rules: 3–40 lowercase letters, numbers, or hyphens;
start with a letter; end with a letter or number; no consecutive hyphens. Add a
reserved set such as `admin`, `api`, `auth`, `status`, and `www`. Slugs are not
renamed and remain tombstoned after deletion to prevent URL takeover.

### UI deployment configuration

A deployment snapshot has this shape:

```json
{
  "repository": "https://github.com/example/project",
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "requestedRef": "main",
  "configurationRevision": 7,
  "build": {
    "runtime": "node",
    "packages": ["."],
    "buildScript": "build",
    "startScript": "start"
  },
  "runtime": {
    "port": 3000,
    "healthPath": "/health"
  },
  "storageBindings": [
    {
      "resourceId": "75c5b85e-34e1-4bf1-b369-03874f634e8e",
      "outputs": {"url": "DATABASE_URL"}
    }
  ]
}
```

Validation must enforce:

- `node` or `bun` runtime;
- bounded unique package paths contained in the checkout;
- supported lockfiles;
- package-script names rather than command lines;
- valid port and absolute health path;
- unique valid environment targets not reserved by the platform/storage;
- typed output names for active storage owned by the application; and
- bounded, versioned, canonical JSON with unknown mutation fields rejected.

The UI may inspect `package.json` and lockfiles to suggest values, but a user
must save explicit UI configuration before deployment. Saving configuration
does not change the running app; creating a deployment captures the current
revision.

Repositories may contain Dockerfiles, but they remain inert. The platform
continues generating the recipe from typed configuration and policy-pinned
runtime images.

## Deployment history and state

Add first-class deployment-attempt records instead of using technical
`operations` as product history. Each attempt records:

- deployment/application UUIDs and idempotency key;
- exact repository, commit, and optional requested ref;
- configuration schema/revision and immutable snapshot;
- selected environment revision;
- state, phase, technical operation UUID, and timestamps;
- builder and candidate-worker identities when present;
- recipe hash, immutable image digest, scheduler identity/version;
- bounded build-log path and truncation state;
- safe error code/summary and cleanup state; and
- accepted, active, superseded, failed, or recovery-required status.

Recommended states are:

```text
queued → validating → acquiring_source → building
       → candidate_provisioning → candidate_deploying → verifying
       → promoting → active → superseded
```

Failures terminate as `failed` or `recovery_required`. Once a deployment ID is
returned, it remains queryable indefinitely. Another attempt for the project is
rejected while one is unfinished; it is not silently queued or substituted.

## Deployment execution

### Build

The controller must record the attempt and technical operation before external
work, then:

1. verify the app is not deleting and no incompatible operation is unfinished;
2. validate idempotency, exact commit, typed configuration, bindings, and
   selected builder/worker images;
3. fetch and verify exactly that commit without credentials;
4. validate configured paths, scripts, and lockfiles against the checkout;
5. generate a deterministic recipe;
6. create a single-use builder with exact image/flavor identity;
7. transfer bounded source and generated recipe separately;
8. push one immutable project image digest and retain a bounded build log; and
9. delete and verify the builder and fixed port on every terminal path.

Malformed configuration fails before provider mutation. Ambiguous provider
results become recovery-required rather than being guessed.

### Failure-preserving candidate

The current implementation does not meet the target: it uses no Nomad canary,
disables auto-revert, and failed cleanup can purge the shared job. Replace it
with a mechanism that proves these properties:

1. the accepted allocation and worker remain active;
2. a distinct candidate worker is created for the deployment attempt;
3. a non-auto-promoted candidate allocation runs on that worker;
4. health evidence identifies the candidate, not merely the old stable app;
5. promotion occurs only after scheduler, application, and route checks;
6. active deployment/worker pointers are committed before predecessor cleanup;
7. the old allocation/worker is removed only after candidate acceptance; and
8. any pre-promotion failure removes only candidate job/allocation, worker,
   port, and unreferenced image.

Nomad canary deployment on a candidate worker is the preferred starting point.
If canary/static-port/routing behavior cannot prove the invariant, use distinct
active and candidate jobs plus a constrained route-promotion action.

The exact candidate route identity remains an implementation gate. A health
request that may have reached the old deployment is insufficient. Use a
candidate-specific route through the same ingress or route/response evidence
that identifies the deployment, and prove it in live Nomad/Traefik tests.

Promotion spans Nomad, routing, SQLite, and provider state. Journal every phase.
If predecessor cleanup fails after acceptance, report the new deployment active
and cleanup recovery-required rather than calling the deployment failed.

Registry retention protects the active image, incomplete-operation references,
and a configured number of prior accepted images. Failed candidates are deleted
only after absence and lack of references are confirmed.

## Environment variables

The UI supports add/overwrite, remove, strict bounded dotenv import, and bounded
batch change. Values are never revealed. Management must not persist them in
its database, logs, traces, browser storage, events, or analytics. Controller
request bodies containing values must not be logged.

Controller SQLite stores key names, owners, timestamps, and a monotonically
increasing environment revision. It stores neither values nor value hashes. A
deployment records the revision initially consumed.

Ownership remains separated between platform keys, user keys, and storage
outputs. Users cannot mutate fixed keys such as project identity, slug, port,
production mode, canonical storage keys, or the reserved `STORAGE__` prefix.

For an enabled app, a mutation must lock the app, record intent without values,
update Nomad Variables with compare-and-set, restart, verify a new healthy
allocation/public route, and then accept metadata/revision. On health failure,
restore prior in-memory values with compare-and-set and verify the rollback. An
unconfirmed rollback becomes recovery-required.

For a disabled app, values change without restart and are consumed on enable.
Project deletion removes them as part of the cascade.

## Managed storage

The application record is created before storage so a database can exist before
the first deployment. Provisioning and binding remain separate internally even
if one UI flow performs both:

- provisioning creates an application-scoped provider resource and credentials;
- a deployment snapshot maps selected typed outputs to runtime keys.

Credentials remain in providers and Nomad Variables and are never returned by
the API. Rotation affects one resource, restarts the app when needed, and
requires provider/application health or recovery-required state.

Each resource has immutable UUID, owner application, type, immutable machine
name, mutable display label, provider identity, lifecycle, limits, verification
time, and timestamps. Renaming a label does not rename provider identity or
canonical keys.

Normal removal is refused while the active deployment references the resource.
The safe flow is: remove the desired binding, successfully deploy, then remove
the unreferenced resource.

Project deletion is one resumable controller operation, not browser-side calls:

1. mark deleting and reject new mutations;
2. withdraw/stop the workload;
3. remove every PostgreSQL, MongoDB, and S3 resource with absence evidence,
   purging S3 under the project-delete confirmation;
4. remove storage/user environment values and Nomad Variables;
5. remove jobs, workers, and ports;
6. remove tracked manifests according to deletion policy;
7. preserve deployment and safe operation history as tombstoned records; and
8. tombstone the slug.

Interruption resumes from the exact resource and phase. The management project
remains `deleting` or `recovery required` until completion is confirmed.

## Enable, disable, quota, and deletion

- **Disable:** block new mutations, remove job/worker/port with absence evidence,
  and preserve accepted deployment/image, environment, storage, and history.
- **Enable:** require an accepted deployment, recreate a worker, submit the
  stored accepted job/configuration using current environment/storage, verify
  scheduler/public health, and do not rebuild.
- **Delete:** require explicit typed-slug confirmation and execute the storage
  and runtime cascade above. There is no initial undo.
- **Quota:** the management database transactionally reserves capacity for both
  `enabling` and `enabled` projects. Unknown controller results keep the
  reservation pending until reconciliation. The controller may enforce global
  infrastructure capacity but does not own per-user counters.

A disabled project URL is unavailable initially; a custom suspended page is not
required. Deployment of a disabled project performs the same capacity
reservation as enable. The route remains unavailable while the candidate is
being verified, and successful promotion changes the project to enabled.

## Controller API

Use versioned bounded JSON over an owner/group-restricted Unix socket. Unknown
mutation fields and duplicate JSON keys are rejected. Responses use canonical
UUIDs and UTC timestamps. Errors contain only stable safe code, bounded summary,
correlation ID, established retryability, and relevant conflicting operation ID.
They never contain provider payloads, secrets, stack traces, or raw stderr.

A database-only create returns `201 Created` with the resource. Mutations that
start provider, scheduler, helper, or restart work return `202 Accepted` with an
operation ID and status URL. Database-only updates return their completed
resource directly.

Initial capabilities:

```text
POST   /v1/applications
GET    /v1/applications/{id}
POST   /v1/applications/{id}/enable
POST   /v1/applications/{id}/disable
POST   /v1/applications/{id}/delete

POST   /v1/applications/{id}/deployments
GET    /v1/applications/{id}/deployments
GET    /v1/deployments/{id}
GET    /v1/deployments/{id}/build-log
GET    /v1/applications/{id}/runtime-log

GET    /v1/applications/{id}/environment
PUT    /v1/applications/{id}/environment/{key}
DELETE /v1/applications/{id}/environment/{key}
POST   /v1/applications/{id}/environment/import

POST   /v1/applications/{id}/storage
GET    /v1/applications/{id}/storage
GET    /v1/storage/{id}
PATCH  /v1/storage/{id}/label
POST   /v1/storage/{id}/verify
POST   /v1/storage/{id}/rotate
DELETE /v1/storage/{id}

GET    /v1/operations/{id}
```

List and log endpoints require deterministic ordering, bounded pagination or
line/byte limits, and explicit truncation evidence.

Administrator reads cover platform status, hosts, selected images,
applications, deployments, storage, and incomplete/recovery-required
operations. Build on the allowlisted models in `platform_cli/status.py`; do not
return raw OpenStack/Nomad documents. Provider IDs may be displayed when useful
for admin diagnosis but cannot become arbitrary mutation input.

Low-level infrastructure mutations remain CLI-only initially.

## CLI end state

Retain:

```text
openstack-platform setup
openstack-platform status
openstack-platform infra list
openstack-platform infra image list|set|prune
openstack-platform infra start|stop|reboot|replace|logs
openstack-platform backup
openstack-platform restore
```

Retire public `openstack-platform app ...` and `openstack-platform storage ...`
commands after API parity and migration. A short compatibility window may route
old commands through the same extracted services, but no independent deployment
implementation may remain. Low-level managed-data backup/restore checks and
release installers remain operator tools.

The management application never invokes or parses CLI output.

## State placement, backup, and admin recovery

Controller releases, SQLite, locks, diagnostics, and build logs move from the
external management host to a private controller directory on the persistent
admin-state volume. The admin NixOS role must add:

- hardened controller and separate management-web service accounts;
- controller and web systemd services ordered after the state mount;
- restricted controller socket;
- management web on port 8080;
- local helper/provider transport; and
- readiness checks for database, helper, controller, and web health.

Moving SQLite changes the failure boundary. Continue encrypted controller
backups on the admin backup volume, but establish an approved independent copy
available to the external recovery host when admin cannot boot. The off-host
transport remains an implementation gate: it must preserve encryption,
checksums, manifests, private ownership, and retention without placing an age
identity on the platform.

The controller cannot replace its own host. For planned admin replacement:

1. block new mutations and finish active operations;
2. create/verify a controller backup;
3. stop web/controller cleanly;
4. run external replacement with a bounded external journal;
5. retain/restore the old host if replacement readiness fails;
6. attach retained state/backup volumes only through the verified path;
7. start controller from retained state and reconcile all dependencies; and
8. record an external replacement audit event.

Unplanned recovery must reconcile server, port, volume, image, flavor, and
provenance before mutation. It must not use delete-first or manual volume
detachment. This procedure must be proven before retiring routine management
from the external host.

## Database and cutover migration

Append-only controller migrations add deployment attempts/snapshots, active
pointers, lifecycle state, idempotency, environment revisions, immutable
storage IDs/labels, candidate/active worker identity, and slug tombstones. Keep
deployment-bound identity, private-file checks, unknown-schema rejection,
foreign keys, integrity checks, and the technical operation journal.

Existing accepted deployments become read-only legacy accepted attempts using
only commit, image, job, health, recipe, and log evidence already present.
Unknown UI configuration is marked legacy/unknown, never guessed. A legacy
project needs explicit desired UI configuration before its next deployment.

Move state through an offline cutover:

1. stop external backup timer and mutations;
2. create and verify an online SQLite backup, identity, integrity, foreign keys,
   schema, and unfinished operations;
3. install matching controller/helper releases on admin;
4. restore and validate into private staging;
5. start controller while web remains unavailable;
6. reconcile state and incomplete operations;
7. enable web after readiness; and
8. retain the external copy until rollback/backup evidence is accepted.

Code rollback and database rollback are separate. Never overwrite a migrated
database with an older release's copy.

## Implementation sequence

| Phase | Work | Exit condition |
| --- | --- | --- |
| 0. Baseline | Freeze this target; inventory affected CLI/helper/state/recovery paths; add a test demonstrating current failed-candidate risk. | Current tests pass and the new preservation test fails for the expected current behavior. |
| 1. Service extraction | Move app/deploy/env/storage/read orchestration out of `argparse` and table rendering into typed services; keep temporary CLI parity. | CLI behavior passes through one service implementation with existing locks, deadlines, checkpoints, and safe errors. |
| 2. History and UI config | Add schema, immutable attempts/snapshots, idempotency, strict UI config, source validation without `platform.yaml`, recipe generation, and legacy migration. | UI-style service deployment works; all attempts are queryable; malformed input mutates nothing. |
| 3. Safe candidates | Add candidate worker, canary/explicit candidate job, exact route identity, promotion, predecessor cleanup, registry references, and phase recovery. | Build/worker/scheduler/health/route failures leave the previous deployment serving; restart at every phase converges. |
| 4. Lifecycle and data | Add enable/disable, environment revisions/batches, storage IDs/labels/removal guard, cascade delete, and slug tombstones. | Disable/enable preserves state correctly; env rollback and interrupted resource deletion are proven. |
| 5. Controller API | Package versioned Unix-socket API, errors, idempotency, pagination, logs, app/deployment/env/storage/operation endpoints, and admin reads. | Contract/security tests pass; lost responses do not duplicate resources; secrets never enter logs. |
| 6. Admin hosting | Add NixOS accounts/services/socket, local helper transport, persistent controller state, web port 8080, ingress verification, backups, and hardening. | Base/project domains work, credentials are unreadable by web, and routine calls do not SSH to self. |
| 7. Management integration | Implement login-code/JWT/session flow, ownership, quota, UI flows, operation reconciliation, and admin dashboard in the sync-engine app. | Cross-user access, token, cookie, CSRF, quota-race, and safe-error tests pass. |
| 8. Recovery and cutover | Migrate state, establish independent backup access, rehearse planned/unplanned admin replacement, and pilot rollout. | Admin can be recovered externally and controller/apps reconcile from retained state. |
| 9. Retirement/docs | Remove product CLI commands and `platform.yaml` support; rewrite setup, tutorial, operations, API, troubleshooting, recovery, and acceptance docs. | No supported docs/code path deploys by CLI or repository manifest; full user lifecycle needs no shell. |

Roll out through CI, a private reference app, admin-only pilot, limited-user
pilot, failure/restart drills, backup/restore and admin-replacement drill, then
broader use. The existing live application remains available during early
implementation and is cut over fresh, with its managed storage explicitly
linked, after replacement-path tests pass. A later announced downtime window is
acceptable; no product-command or `platform.yaml` backward compatibility is
required. Do not combine the first state migration with the first recovery
rehearsal or user rollout.

## Verification requirements

The updated acceptance suite must cover:

- strict config/source/storage/env validation and unknown-field rejection;
- append-only schema and legacy migration;
- idempotency, lost responses, concurrency, lock scopes, deadlines, and every
  durable recovery phase;
- active deployment availability during build and every candidate failure;
- exact candidate route identity and promotion;
- builder/candidate/predecessor cleanup and registry protection;
- enable without rebuild and disable without data/history loss;
- environment compare-and-set, restart, health, and rollback;
- storage reference guards, credential rotation, and interrupted cascade delete;
- bounded API requests, responses, logs, pagination, socket ownership, and safe
  diagnostics;
- separate web/controller accounts and credential denial;
- JWT algorithm/signature/issuer/audience/time checks, code replay, key rotation,
  session fixation, CSRF/origin, project ownership, and host-only cookies;
- base/wildcard DNS, TLS, Host preservation, and security-group isolation;
- controller backup/restore, reboot recovery, state cutover, and external admin
  replacement; and
- secret absence from databases, output, access logs, traces, events, browser
  storage, and analytics.

Live tests, not mocks alone, must continuously probe the stable project URL
while inducing candidate failures and must exercise Nomad, Traefik, OpenStack,
backup restore, and admin replacement.

## Initial non-goals

The first release does not include private repositories, GitHub webhooks,
repository manifests, Dockerfile execution, arbitrary build commands, custom
project domains, preview deployments, multiple replicas, autoscaling,
user-selected resources, scheduled jobs, persistent runtime-log aggregation,
interactive shells, database consoles, teams/collaborators, visible storage
credentials, storage machine-name renames/data migration, user cancellation,
instant artifact rollback, suspended-project pages, or admin self-replacement.

These features must not be approximated by exposing raw helper/provider access.

## Definition of done

The goal is complete when:

- login uses the one-time-code and signed-JWT flow with correctly scoped
  sessions;
- users can access only management-authorized projects and quota is race-safe;
- UI-owned typed configuration replaces every supported `platform.yaml` path;
- every attempt and bounded build log remains queryable;
- failed candidates leave the prior deployment serving;
- enabled apps serve at `https://<slug>.mit-sdg.dev`;
- environment and storage credential values never enter databases or output;
- disable frees compute, enable avoids rebuild, and delete resumably removes all
  attached storage/runtime resources while tombstoning the slug;
- management runs on admin behind ingress and reaches a local restricted
  controller API;
- state survives reboot and moves with the retained admin-state volume;
- the external host can recover/replace admin without a running controller;
- the public CLI contains only setup, infrastructure, backup, restore,
  diagnostics, and recovery; and
- unit, integration, NixOS, live-failure, security, backup/restore, migration,
  and external-recovery acceptance evidence passes.
