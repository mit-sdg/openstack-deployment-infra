# Internals

This reference describes processes, state ownership, trust boundaries, and failure handling for contributors and operators. Read [How it works](../how-it-works.md) for an introduction.

## Overview

Five VM roles run in one OpenStack project: persistent admin, ingress, and storage hosts, plus disposable workers and builders. The operator uses a separate unprivileged account on the operator host.

```text
                        browsers
                           |  HTTPS
              HTTPS provider (for example Cloudflare)
                           |
+---------------------- ingress host ---------------------+
| Traefik   <domain>        -> admin:8080, management-web |
|           <app>.<domain>  -> that app's worker          |
|           s3.<domain>     -> storage host, Garage S3    |
+---------------------------------------------------------+

+----------------------- admin host ----------------------+
| management-web                                          |
|   | broker.sock                                         |
| management-broker --> identity.sock                     |
|   |                   management-identity --------------+--> Commons (HTTPS)
|   | project.sock                                        |
| controller (runs as platform-controller)                |
|   ^ privileged.sock <-----------------------------------+--- operator host, over SSH
|   | starts                                              |    (openstack-platform CLI,
| helper --> OpenStack, Nomad, registry, storage admin    |     operator dashboard)
| Nomad server, backups, health checks                    |
+---------------------------------------------------------+

+- worker (1 per app) -+  +--- storage host ----+  +- builder (1 per build) -+
| Nomad client         |  | PostgreSQL, MongoDB |  | rootless BuildKit,      |
| the app's container  |  | Garage S3, registry |  | created by the helper   |
+----------------------+  +---------------------+  | and deleted after use   |
                                                   +-------------------------+
```

| Component | Runs on | Runs as | Purpose |
| --- | --- | --- | --- |
| `openstack-platform` CLI | Operator host | The operator account | Setup, status, images, persistent hosts, operator-state backup ([reference](operator-cli.md)) |
| Operator dashboard | Operator host | The operator account | Read-only status page on a private Unix socket |
| Controller (`<namespace>-controller.service`) | Admin | `platform-controller` | Owns app, deployment, environment-name, and storage records; orchestrates their lifecycle ([API](controller-api.md)) |
| Helper (`openstack-platform-helper`) | Admin | Started by its caller | Performs allowlisted actions against OpenStack, Nomad, the registry, and storage services |
| Nomad server | Admin | `nomad` | Schedules each app's container on its worker |
| `management-web` | Admin | `management-web` | Serves the owner portal to ingress on port 8080; holds no authoritative state |
| `management-broker` | Admin | `management-broker` | Owns portal accounts, sessions, ownership, quotas, and audit; the controller's only project peer |
| `management-identity` | Admin | `management-identity` | Redeems Commons sign-in codes; the only portal service that makes outbound connections |
| Traefik | Ingress | | Routes public hostnames |
| PostgreSQL, MongoDB, Garage, registry | Storage | | Managed storage and app images |
| App container | Worker | Nomad allocation | One app |
| Rootless BuildKit | Builder | A user service of the unprivileged `agentops` account | Builds one app image and pushes it to the registry |

## Shared configuration and contract

The inventory describes the deployment; the policy sets operator defaults and limits; the code contract fixes interfaces across languages.

`platform.json` holds project identity, resource names, addresses, images, versions, volumes, paths, ingress, and internal naming. It contains no secrets but reveals deployment layout, so it stays private. Source tools use `PLATFORM_CONFIG`; the installed launcher pins `/srv/openstack-platform/config/platform.json`; guests use the read-only `/etc/<namespace>/platform.json`. See [Configuration](configuration.md).

The mode-`0600` operator policy holds worker and storage profiles, digest-pinned Node.js and Bun defaults (`runtimeImages`), limits, and the public age key `backupAgeRecipient`. Credentials, private PKI, age identities, Nomad tokens, storage administrator credentials, and SSH private keys belong in neither file.

[`infra/lib/platform_contract.json`](../../infra/lib/platform_contract.json) defines roles, ports, accounts and IDs, executables, paths, protocol constants, and required inventory keys. Python uses `openstack_platform.contracts`, standalone scripts use `infra/lib/platform_contract.py`, Nix uses `nix/lib/constants.nix`, and shell receives an allowlisted projection from `infra/lib/platform_config.py`. Contract changes require a coordinated release.

Each controller database binds to the project UUID, namespace, and stable inventory: network, addresses, resource names, volumes, paths, and PKI naming. Image, flavor, version, checksum, and container selections are excluded, preserving identity across upgrades. A different deployment identity is refused.

## Roles and state ownership

Durable state lives on volumes that survive server replacement. Worker and builder disks are disposable; apps must store persistent data in managed storage.

| Role | Lifetime | Main responsibility | Durable state |
| --- | --- | --- | --- |
| `admin` | Persistent | Nomad control plane, controller, helper, owner portal, monitoring, backups | Admin-state volume and backup volume |
| `ingress` | Persistent | Public routing to the portal, apps, and public S3 | None |
| `storage` | Persistent | PostgreSQL, MongoDB, Garage S3, and the private OCI registry | Managed-data volume |
| `worker` | Replaceable, one per app | One app's Nomad allocation | None |
| `builder` | Single use | One rootless BuildKit build from one source snapshot | None |

The admin-state volume holds controller SQLite, Nomad state and Variables, helper and portal releases, the portal database, and diagnostics. The managed-data volume holds storage and registry data; the backup volume holds encrypted sets and verification evidence.

| State | Owner | Location | Contents |
| --- | --- | --- | --- |
| Operator state | `openstack-platform` | `/srv/openstack-platform/state/platform.sqlite3` on the operator host | Image selections for persistent-host replacement, infrastructure operations |
| Controller state | Controller | `<adminState>/controller/state/platform.sqlite3` | Apps, deployment attempts, accepted deployments, environment names, managed resources, the controller's image selections, operations |
| Deploy keys | Helper | `<adminState>/controller/source-keys/<slug>/` | One ed25519 key pair per app with a private repository |
| Startup and build logs | Controller | `<adminState>/controller/` | Bounded logs of builds and failed candidates |
| App secrets | Nomad | Nomad Variables on the admin-state volume | Environment values and managed-storage credentials, scoped to one app |
| Portal state | `management-broker` | `<adminState>/management-broker/management.sqlite3` | Users, sessions, ownership, teams, quotas, intents, audit |
| Managed data | Storage services | Managed-data volume | Databases, buckets, app images |

Operator and hosted-controller SQLite state are separate. The CLI asks the controller for app counts; neither database stores app secret values.

Persistent host replacement moves the fixed port and volumes to a candidate built from the selected image UUID. Identity and readiness checks precede old-server deletion. Failure restores the old server; ambiguous provider results require recovery. See [Hosts and images](../guides/hosts-and-images.md#replace-a-persistent-host).

## Processes and trust boundaries

The operating system enforces process boundaries independently of request data.

### Operator host

`openstack-platform` owns setup, infrastructure status, image selection and pruning, persistent-host lifecycle, operator-state backup, and offline restore. It has no app, deployment, environment, or managed-storage mutation commands. Its SQLite state is separate from the hosted controller.

The generated `platform-admin` alias in `/srv/openstack-platform/.secrets/ssh/config` pins the admin address, `agentops`, the identity (`IdentitiesOnly yes`), and an ed25519 host key in a private known-hosts file with strict checking. Agent and port forwarding are disabled. Provider calls use the protected `platform-openstack` wrapper; releases carry no cloud credentials.

The operator dashboard (`openstack-platform dashboard`) runs as the operator, serves a mode-`0600` Unix socket (default `STATE_DIRECTORY/run/dashboard.sock`), and admits only that UID and a loopback `Host`. It reads the privileged controller API separately from the portal. Refreshes run every 30 to 900 seconds (default 60) and gather:

- One SSH session calling the controller's privileged socket (`/v1/admin/capabilities`, `/v1/admin/images`, `/v1/admin/applications`, `/v1/admin/deployments` for the two newest 100-item pages, `/v1/admin/storage`, and `/v1/admin/operations` for the three newest pages), reading the platform-health timer's snapshot, and checking systemd unit status.
- The `infra list` projection from operator state, with a bounded provider lookup.
- Credential-free HTTPS probes of public ingress and of each enabled app's health path, verifying each deployment's `X-Platform-Deployment` marker (or app ID).

The dashboard avoids `/v1/admin/status` and `/v1/admin/hosts`, which hold the API lock during provider probes. Failed sources keep their last good records with age labels. It serves static assets and `GET /api/snapshot`; `POST /api/refresh` wakes a refresh at most every 10 seconds and requires a same-origin custom header. It changes no platform state.

### Admin host: controller and helper

The controller owns app records and lifecycle orchestration. A fixed local helper performs external actions; the HTTP process has no broad OpenStack, Nomad, registry, or storage-administrator interfaces.

The controller (`openstack-platform-controller`) runs as `platform-controller` under a hardened systemd unit: no capabilities, a read-only system, and write access restricted to its state, build-log, diagnostics, and deploy-key directories and the operator-state backup root.

The helper (`<paths.root>/bin/openstack-platform-helper`) reads exactly one protocol-v1 JSON request from standard input and writes one JSON response to standard output:

```json
{"version": 1, "requestId": "<uuid>", "action": "app.build", "args": {}}
```

The response is `{"version": 1, "requestId": ..., "ok": true, "result": {...}}` or `{"ok": false, "error": {"code": ..., "message": ...}}`. Requests and responses are limited to 1 MiB. Request input cannot choose an executable, a remote host, a provider credential, or a shell command.

Implemented actions must exactly match the release-bound [`actions-v1.txt`](../../openstack_platform/helper/actions-v1.txt); release smoke tests reject a mismatch. Unknown actions return `UNKNOWN_ACTION`.

| Area | Actions |
| --- | --- |
| Builds | `app.build`, `app.build.cleanup`, `app.build.logs`, `app.builder.delete` |
| Workers | `app.worker.create`, `app.worker.observe`, `app.worker.capacity`, `app.worker.delete` |
| Nomad jobs | `app.deploy`, `app.promote`, `app.health`, `app.restart`, `app.stop`, `app.remove`, `app.startup`, `app.logs` |
| Environment | `app.env.list`, `app.env.set`, `app.env.remove` |
| Registry | `app.manifest.retain`, `app.manifest.verify`, `app.manifest.delete` |
| Source | `app.source.key`, `app.source.check`, `app.source.commits`, `app.source.preflight` |
| Managed storage | `storage.<type>.create`, `.observe`, `.verify`, `.rotate`, `.remove` for `postgres`, `mongo`, and `s3` |
| Backups | `backup.accept` (called by `openstack-platform backup` over SSH) |

### Controller sockets

The controller has no network listener.

The admin role exposes two mode-`0660` Unix sockets in `/run/<namespace>-controller/` (a mode-`0750` runtime directory):

| Socket | Group | Admitted peer | Capability |
| --- | --- | --- | --- |
| `project.sock` | `controller-api` | `management-broker` UID:GID | Health and product routes, including confirmed storage deletion |
| `privileged.sock` | `platform-admin` | The operator account's UID:GID | Administrator reads and operations, and cascade app deletion |

Before parsing HTTP, the server reads Linux `SO_PEERCRED` and admits only the socket’s configured UID:GID pairs. Requests cannot upgrade capabilities. This identifies the local process; `management-broker` handles browser ownership, quotas, sessions, and CSRF. See [Controller API](controller-api.md).

`<namespace>-controller-readiness` runs as `management-broker` and waits for `GET /v1/health` on the project socket. The broker requires the readiness unit and restarts with it.

### Owner portal processes

Each portal service has a separate systemd unit and unprivileged account:

| Service | Network | Can reach | Cannot reach |
| --- | --- | --- | --- |
| `management-web` | Listens on TCP 8080, accepting only the ingress host's address | `broker.sock` | Controller sockets, controller and operator state, broker state, PKI, secrets |
| `management-broker` | None (Unix sockets only) | `project.sock`, `identity.sock` | `privileged.sock`, operator state, PKI, secrets |
| `management-identity` | Outbound only to the inventory's `ownerPortal.identityEgressCidrs` (Cloudflare's published ranges by default) and the local DNS resolver | Commons | Every state directory, controller sockets, PKI, secrets |

`broker.sock` (in `/run/<namespace>-management-broker/`) admits only the `management-web` peer. `identity.sock` (in `/run/<namespace>-management-identity/`) admits only the `management-broker` peer. Both reuse the controller's transport, with the same `SO_PEERCRED` check.

## Greenfield setup flow

`openstack-platform setup --env-file PATH --apply` runs from a clean, full-commit checkout as the unprivileged operator. Without `--apply`, or with `setup check`, it performs only read-only preflight. Apply proceeds in this order:

1. **Verify inputs.** Refuse to run as root, read the setup file, and prompt for missing OpenStack credentials. Determine the exact source commit and verify the signed release and artifact evidence before creating anything.
2. **Resolve the plan.** Authenticate to the project and resolve the network, flavors, volume type, and fixed addresses. Write the private inventory into the workspace; an existing workspace with a different inventory is refused.
3. **Generate private material.** Create the operator SSH key, the admin recovery key, the builder key, service secrets, the backup age identity, the operator policy (with that identity's public recipient), the protected `platform-openstack` wrapper, and the internal PKI.
4. **Lay the network foundation.** Reconcile security groups and reserve the persistent hosts' fixed ports.
5. **Build and publish images.** For each of the five roles, reuse a published image only if it matches the signed artifact evidence. Otherwise build it with Nix, verify it against that evidence, boot-test it locally, and publish it to Glance.
6. **Create the admin host.** Create its state and backup volumes and the server, install the operator runtime, and set up the pinned `platform-admin` SSH bridge. Transfer the policy, the image-selection seed, the OpenStack and storage bootstrap credentials, the builder key, and PKI. Bootstrap Nomad ACLs.
7. **Create the storage and ingress hosts.** Ingress gets the Cloudflare Tunnel token if one was given.
8. **Install releases.** Install the operator configuration and release (with its daily backup timer) and the helper release. The helper release's completion marker triggers the controller's activation unit; the preparation unit seeds the controller's image selections from the transferred seed and the controller starts.
9. **Verify the controller boundary.** Check the hosted controller service, readiness unit, socket ownership and mode (`platform-controller:controller-api` and `platform-controller:platform-admin`, both `0660`), that the operator is refused on the project socket and admitted on the privileged one, and that the hosted-controller backup and off-site export timers are enabled.
10. **Record and finish.** Record the same five image UUIDs in operator state, initialize the Garage backup key and the managed-data backup identity on admin, and require `status` to report `healthy`. With a tunnel token, also require the public `/healthz` to return `OK`.

Reruns recheck checkpointed material and provider changes. Resources are reused only when their full expected projection matches; there is no import or name-only adoption. See [Deploy the platform](../guides/deploy-the-platform.md).

## Application lifecycle

A deployment builds one exact commit and accepts it only after health checks pass. Ordinary replacement keeps the accepted version serving on candidate failure. Maintenance deployments stop it before cutover and can leave the app stopped.

### Deployment steps

For project and operator deployment requests, the controller:

1. Validates the app, the canonical GitHub URL, the requested ref, the exact commit, the configuration revision, and the closed, typed configuration.
2. Records an immutable deployment attempt and accepts an operation under the request's `Idempotency-Key` (which becomes the deployment ID).
3. Asks the helper to build the exact source snapshot.
4. The helper fetches the source, resolves the requested Node.js or Bun version, and generates a build recipe.
5. Creates a single-use builder, runs rootless BuildKit, and records the pushed image’s immutable digest.
6. Deletes the builder and its port, and verifies they are gone.
7. Creates the app's worker, or, for an operator worker-reuse deployment, verifies the exact accepted worker.
8. Submits the generated Nomad job and a candidate route.
9. Accepts the candidate only after Nomad, the app's health path, and the public route all pass.
10. After durable acceptance, verifies any address handover, removes the exact predecessor job and worker, and applies registry retention. Finishing failures use the durable retry schedule described below.
11. On candidate failure, records startup evidence when available and attempts bounded candidate cleanup. Ordinary replacement keeps the accepted route; maintenance can leave the app stopped. Unconfirmed cleanup requires recovery.

Repositories supply source and supported lockfiles. Package paths and script names are data, never shell commands. Storage bindings map outputs to environment names without creating or deleting storage. Secret values live in app-scoped Nomad Variables; SQLite keeps environment names, owners, revisions, and timestamps, and API reads omit values.

Nomad's healthy deadline is 10 minutes from placement, including image download; its progress deadline is 12 minutes. Crash loops fail sooner through restart policy. Controller polling lasts up to 12 minutes, reserving time for startup evidence and job, worker, and artifact cleanup. Enable resubmits the accepted job unchanged, including older deadlines.

Before removing an unhealthy candidate, `app.startup` reads the newest allocation's status, restart count, last 12 task events, and last 200 stdout and stderr lines (64 KiB each). It gets at most 30 seconds or one third of the remaining deadline; failure never blocks removal. The mode-`0600` record is `startup-logs/<application>/<deployment>.json` under controller state and appears on the portal's failed deployment page.

With `maintenance: true`, the old version serves during build and artifact/storage checks. Under the app lock, the controller journals its job, image, placement, server, and port, then stops it before starting the candidate. Acceptance alone changes the accepted pointer. Retries use the stop checkpoint. The operator, staff, or a portal admin can request this through their respective interfaces. Cutover interrupts service and prevents concurrent app processes.

Operator-only `reuseWorker: true` requires `maintenance: true`. The controller pins the accepted worker and its size, rechecking `ACTIVE` state, ownership, attachment, Nomad and Docker health, and capacity. It stops the old job and starts the new image on that worker; no server or IP changes. Failed cutover keeps the accepted pointer but leaves the app stopped with its worker retained. OS or flavor changes require replacement.

### Runtime versions

The build resolves the requested Node.js or Bun version to an official `-slim` image pinned by SHA-256 digest. [`runtime_versions.py`](../../openstack_platform/runtime_versions.py) reads:

- **Node.js:** `engines.node` in `package.json`, then a root `.nvmrc` or `.node-version`.
- **Bun:** `"packageManager": "bun@x.y.z"`, then `engines.bun`, then `.bun-version`. A `packageManager` that names another manager is ignored.

Ranges use a subset of npm semver: exact and partial versions, x-ranges, `^`, `~`, comparators, hyphen ranges, a space for AND, and `||` for OR. A request that matches nothing, or only lines older than the built-in floors (Node.js 20, Bun 1.1), is refused before any lookup.

`app.build` resolves versions through [`runtime_images.py`](../../openstack_platform/helper/runtime_images.py). Node.js releases come from `https://nodejs.org/dist/index.json`, Bun from Docker Hub `oven/bun` tags. It selects the newest matching release above the floor, preferring Node.js LTS when allowed (`>=20` selects LTS over Current). It resolves the `-slim` tag to a digest, trying the next two releases if unpublished. HTTPS requests use no redirects or credentials, a 10-second timeout, and a 60-second total lookup. Without a version request, the policy’s `runtimeImages` pin remains unchanged.

The helper returns `runtime: {runtime, version, image, source}`. For `default`, the controller requires the policy pin; otherwise it validates the official digest-pinned image against the request, regenerates the recipe, and checks its hash. The operation retains the choice; deployment reads expose `runtime`, and rollback/resize copy it. Rejected requests yield `BUILD_REJECTED` with a build-log reason; lookup failures yield `RUNTIME_UNAVAILABLE` before builder creation. The controller unit permits IPv4/IPv6, and admin default egress allows `nodejs.org`, `auth.docker.io`, and `registry-1.docker.io`.

### Authoritative reads and retained rollback

App reads take `activeDeploymentId` from the accepted pointer. Deployment reads include `configuration`, `configurationSha256`, `sourceRepository`, commit, and image digest. Source evidence comes from journaled intent, not current app settings. Missing or inconsistent evidence makes `sourceRepository` null and prevents rollback to that attempt.

Rollback redeploys a different complete, successful attempt of the same enabled app without rebuilding. Its plan binds accepted deployment, environment revision, sizing, storage identities, historical image, and configuration. Apply rechecks that projection under the app lock, storage outputs, and registry image availability (`app.manifest.verify` uses manifest/blob `GET` and `HEAD` reads).

Rollback keeps current sizing and secrets; derived values such as `PORT` follow candidate configuration. It restores no environment snapshot, database, bucket objects, or worker filesystem. Ordinary candidate failure keeps the predecessor. Acceptance transfers and verifies any floating IP before predecessor cleanup. Resize follows the same path with the accepted image and new sizing. See [Deploy apps from the command line](../guides/deploy-apps-from-the-command-line.md).

### Retained worker primary ports

An app can retain a fixed primary IPv4 across worker replacements.

Schema migration 4 journals the Neutron reservation separately from worker slots. Server metadata belongs to a generation; port name, UUID, network, subnet, security group, and address belong to the reservation.

Worker actions accept only the controller’s closed `retainedPort` identity. Nova receives the existing port explicitly; ordinary cleanup never deletes it. After predecessor removal, the controller records the port’s worker slot before reuse, preventing cleanup from confusing old and new attachments.

Reservations and lifecycle changes share the app lock. Allocation and worker-creation intent precede provider calls; unknown outcomes never authorize another creation or release. Release requires a proven unbound, owned port; app deletion releases it after worker cleanup. Fixed and floating reservations are mutually exclusive. Retained-port deploy, resize, and rollback require maintenance because a port attaches to only one worker.

The admin image ships `nix/pkgs/openstacksdk-security-group-project-alias.patch`: security-group `project_id` falls back to Neutron’s legacy `tenant_id`. This preserves ownership evidence without relaxing validation.

### Deploy keys for private repositories

The helper creates a per-app ed25519 pair with `ssh-keygen` at `<adminState>/controller/source-keys/<slug>/` (files `0600`). Key responses return only the public half; the private key never reaches controller, broker, or builder. Fetch first uses credential-free HTTPS, then, if a key exists, `ssh://git@ssh.github.com:443/OWNER/REPO.git` with that key and GitHub’s pinned ed25519 host key. Both require the exact commit; failure yields `SOURCE_REJECTED`.

Key removal renames the directory before deletion, exposing the whole pair or nothing to builds and backups. Cascade deletion removes it after the job and Variable. Keys accompany hosted-controller backups ([backup model](#backup-and-recovery-model)).

## SQLite and operation state

SQLite stores controller decisions and operation checkpoints. After restart, callers resume interrupted foreground work with the same request and key. Accepted finishing work resumes automatically from its recorded intent; uncertain provider outcomes remain unresolved until checked.

[`openstack_platform/controller/database.py`](../../openstack_platform/controller/database.py) owns schema creation, forward migrations, the deployment binding, and the tables for apps, immutable deployment attempts, active deployment pointers, environment metadata, managed resources, image selections, slug tombstones, idempotency requests, operations, operation dispatches, and the optional floating-IP and fixed-port reservations. Unsupported prior schemas fail before any migration or provider call.

Transactions do not span provider calls. Domain-service checkpoints coordinate external work. Provider observations are evidence, not accepted state; reconciliation never creates app or storage records from matching external resources.

Requests that create records or operations require `Idempotency-Key`; synchronous deploy-key changes are repeatable without one. The SHA-256 fingerprint covers method, path, and parsed body. Identical requests replay their result; different input yields `409 IDEMPOTENCY_CONFLICT`. The key becomes the operation ID, or the application ID for creation. Strict deployment configurations count as public metadata, so output names such as `password` and `secret_access_key` remain valid binding names in fingerprints.

App creation is database-only (`201`). Background operations reserve `app-<id>` or `infrastructure` before external work (`202`), with one unfinished foreground operation per scope. A separate accepted finishing reservation can coexist with compatible foreground work; both use the same app lock. Four threads handle at most 32 admitted operations. Deploy-key and storage-label changes are synchronous exceptions; see [route contracts](controller-api.md#routes).

The dispatch journal omits request bodies. Before sockets open after restart, interrupted foreground operations with domain intent become `recovery_required`, retaining checkpoints; dispatches without domain intent fail terminally with nothing to clean up. Repeat the identical request and key to resume, supplying environment values again. New keys cannot bypass unfinished work. A renewed deadline sets `running` and clears the safe error. Build rejection becomes terminal only after builder and build-tag absence is proven; uncertain cleanup retries without rebuilding.

Schema migration 5 adds a `finishing` flag to operations and dispatches and separate uniqueness constraints for foreground and finishing admission. Deployment acceptance commits the flag with the active deployment pointer and successful attempt. Enable commits it with accepted runtime presence. The migration recognizes older post-acceptance deployment checkpoints only when the successful attempt, active pointer, operation ID, and candidate image agree; it does not treat an unaccepted `deployment_healthy` checkpoint as finishing.

The controller journals automatic retry count and next retry time in `refs.finishing_retry`, without storing a request body. Five retries wait 60, 120, 240, 480, and 600 seconds; each attempt renews its deadline for at most 120 seconds. The operation stays `running` between attempts and becomes `recovery_required` only after exhaustion. Restart preserves waiting schedules and counts interrupted attempts. Automatic and manual replay use the same dispatch claim, so they cannot execute the same operation twice concurrently. Manual replay keeps the original intent and key and does not reset the automatic budget.

The finishing path reobserves the accepted job and public route before handover or predecessor removal. It never rewrites acceptance, environment values, or desired-running state. Handover reobserves exact provider associations; unknown outcomes do not authorize a repeated mutation. Worker deletion requires exact predecessor job absence first. The journaled `predecessor_cleanup` phase means handover is confirmed; `accepted` means only registry retention remains. Compatible environment changes, storage verify/rotate, and restart retain foreground admission. Enable can be a no-op after handover. Disable also requires journaled predecessor job absence and a separate cleanup placement; worker creation for a stopped app, another deploy, resize, rollback, deletion, storage create/remove, and address changes remain blocked with `POST_ACCEPTANCE_CONFLICT`.

Helper failures write a bounded controller journal line with the fixed action, error class, and an allowlisted error code. Unknown codes are redacted. Exception messages, request arguments, environment values, credentials, stderr, response bodies, and tracebacks are omitted; `safeError` remains a separate user-safe summary.

Most synchronous handlers share the API lock. Polling, health, app reads, environment-name and storage reads, runtime logs, deploy-key reads, and GitHub checks skip it and use private snapshots. Slow locked requests such as `GET /v1/admin/status` do not block them. App reads close their snapshot before helper/public probes; identical app and deployment rows share a probe and reuse it for two seconds.

Health requires the accepted deployment’s health path and route marker. Disabled apps report `stopped` without probes; missing evidence or transport failures report `unknown`.

## Backup and recovery model

Each state class has an independently encrypted backup set. Controller, portal, and operator-state decryption identities stay off the admin host; the managed-data identity also lives on admin.

| Backup set | Contents | Written by | Location | Schedule (UTC) |
| --- | --- | --- | --- | --- |
| Hosted controller | Controller SQLite and a paired deploy-key archive | `<namespace>-hosted-controller-backup` (as `platform-controller`) | `<paths.backups>/hosted-controller` | Daily, 02:15 |
| Portal | Broker SQLite | `<namespace>-management-broker-backup` (as `management-broker`) | `<paths.backups>/management-broker` | Daily, 02:30 |
| Operator state | Operator CLI SQLite | `openstack-platform backup` (operator user timer) | `<paths.backups>/controller` | Daily, 02:45 |
| Managed data | PostgreSQL, MongoDB, and Garage catalog and objects | `<namespace>-platform-backup` (as the operator) | `<paths.backups>/<namespace>/<timestamp>` | Daily, 03:15 |
| Off-site export | The newest committed set of each class | `<namespace>-offsite-export` | An operator-mounted off-site filesystem | Daily, 05:00 |

Each schedule also has a random delay of up to 15 or 30 minutes.

**Key custody.**

| Identity | Encrypts | Where the private half lives |
| --- | --- | --- |
| Backup age identity from setup | Hosted controller and operator-state sets (the policy's `backupAgeRecipient`), and the portal set | Off-platform: setup writes it to `/srv/openstack-platform/.secrets/setup/backup-age-identity.txt`; escrow it and never copy it to admin. Admin holds only the public recipient, in the policy and, for the portal set, in `<adminState>/management-broker-releases/config/backup-recipient.txt`. |
| Managed-data identity | Managed-data sets | On admin at `<paths.root>/persistent/secrets/backup-age-key.txt`, plus an operator escrow copy |

Restores take the private identity explicitly: for example, `openstack-platform restore BACKUP --age-identity IDENTITY --yes` for operator state. Hosted controller restore decrypts on the operator side, stages plaintext on admin, and runs `openstack-platform-hosted-controller-restore` as root from the recovery console.

Each set commits by writing its manifest after ciphertext and checksums. Off-site export copies only committed sets, verifies copies, appends a manifest, and updates a credential-free receipt. Its destination must be a separate mounted filesystem; the operator manages provider retention.

Hosted-controller and portal backups prune after commit ([`backup_retention.py`](../../openstack_platform/backup_retention.py)). Sets named within 14 days and the newest three complete sets stay. Pruning removes manifests first, leaving uncommitted debris if interrupted. Pruning failure fails the unit but preserves the new backup.

Garage backup inventories buckets through the admin API, grants `platform-backup` read access, and fails on unreadable buckets. Encrypted catalog format 2 includes original bucket IDs, quotas, app keys, and grants. Restore recreates buckets, imports app keys, restores objects/grants, and remaps IDs into an offline replacement controller database. Temporary write grants are revoked even on failure.

Managed-data restore checks use disposable PostgreSQL/MongoDB containers and validate the Garage archive before writing `RESTORE-MANIFEST`. Offline SQLite restore checks deployment identity, schema, integrity, foreign keys, sidecars, and unfinished operations before atomic replacement, without network calls.

Managed-data format 3 excludes app images. No set contains Nomad Variable environment values. After full loss, restore secrets from escrow and rebuild accepted GitHub commits. See [Backups and recovery](../guides/backups-and-recovery.md).

## Network and workload isolation

- **Ingress:** In tunnel mode, Traefik binds to loopback and the host opens no public origin port. In direct mode, one exact list of provider IPv4 ranges is applied to Neutron, the NixOS firewall, and trusted forwarded headers. In both modes the provider terminates browser HTTPS and preserves `Host`. Traefik routes `<domain>` to `management-web`, `<app>.<domain>` to the healthy worker, `s3.<domain>` to Garage on the storage host (over HTTPS checked against the internal CA, keeping `Host` so presigned URLs verify), and `/healthz` to its own health check.
- **Admin:** The host firewall admits port 8080 only from the ingress address, and `management-web` additionally restricts its peers to that address.
- **Builders:** Builders block cloud metadata, are driven over SSH from admin with a dedicated unprivileged identity, run rootless BuildKit, get registry push access only for their task, and are deleted.
- **Workers:** Workers have no SSH service, block cloud metadata, accept app traffic only from ingress, and run generated Nomad jobs with no privileged container or host volume. Runtime credentials are scoped to one app and injected through Nomad Variables.
- **PKI:** The internal CA authenticates the platform's own service relationships. Its private material and deployment credentials arrive after the image is built; they are never in public source or the Nix store.

## Owner portal internals

The broker checks browser sessions, ownership, and quotas before calling the controller’s project socket.

```text
browser -> HTTPS provider -> ingress -> management-web -> broker.sock -> management-broker
                                                                            | -> project.sock -> controller
                                                                            | -> identity.sock -> management-identity -> Commons (HTTPS)
```

`management-web` serves the React app as static files under a strict content security policy and forwards only an allowlisted set of paths: `/api/v1/...` to the broker's `/v1/...`, and `/auth/...` to `/v1/auth/...`. It owns no authoritative state. The broker owns users, sessions, ownership, teams, quotas, the intent journal, and the audit log, and is the only controller project peer. The identity service admits only the broker and talks only to the one configured Commons origin.

### Sign in with Commons

Commons Connect uses an authorization-code flow; the portal never receives a Commons password. Commons must accept the portal origin (`https://<domain>`) as an app.

1. `GET /auth/commons/start` checks per-address admission and accepts only a same-origin or typed navigation. It sets a short-lived HMAC binder cookie, `__Host-portal-commons` (HttpOnly, Secure, SameSite=Lax, 10 minutes), and redirects to `<commonsOrigin>/connect?app=<portal origin>&state=<state>`. The state is a separate HMAC of the binder, so only that browser can finish.
2. Commons asks the person to approve, then redirects to `/auth/commons/callback` with `code` and `state`, or with `error=access_denied` after Cancel.
3. The callback always clears the binder cookie and checks admission, the binder, and a constant-time state match. Only then does the identity service redeem the code.
4. The identity service posts `{code, app}` to Commons `/api/connect/redeem` with verified system TLS, no redirects or proxies, strict JSON, and tight time and size bounds. It keeps only the stable subject UUID, username, and display name.
5. The broker maps the person by Commons origin (the issuer) and stable subject, provisions an `owner` account on first sign-in, refuses disabled or pending accounts, and creates a session.

Both navigations answer only with `303` redirects: start leaves only for the exact Commons approval URL, and the callback lands only on an allowlisted portal page or `/sign-in?error=CODE`. The sign-in page explains `COMMONS_CANCELLED`, `SIGN_IN_EXPIRED`, `IDENTITY_UNAVAILABLE`, `ACCOUNT_DISABLED`, and `RATE_LIMITED`. Codes and states are never stored, logged, or echoed. If the identity service is down, new Commons sign-ins fail but existing sessions keep working.

### Sessions and CSRF

The `__Host-portal-session` cookie contains an opaque token (HttpOnly, Secure, SameSite=Lax, `Path=/`, no `Domain`). The broker stores its digest with the server-side session record. Every request rechecks the account's current enabled state, role, and security generation; a security change increments the generation and ends all of that account's sessions.

| Session | Absolute lifetime | Idle timeout |
| --- | --- | --- |
| Owner | 8 hours (default) | 30 minutes (default) |
| Staff | 1 hour | 10 minutes |
| Portal admin | 1 hour | 15 minutes |

Mutations require the exact portal `Origin` and the session's CSRF token; admin and staff reads also require the CSRF token and same-origin metadata. Anonymous forms (local login, enrollment) use an HMAC CSRF token bound to a Strict `__Host-portal-anonymous` cookie. Logout, local revocation, expiry, and restore end sessions. Password changes, archiving, or withdrawn approval in Commons do not end portal sessions that already exist.

### Local accounts, roles, and admin enrollment

Local accounts use issuer `local`, an immutable UUID subject, and a unique lowercase username. They store salted password hashes and private TOTP state.

- **Roles:** `owner`, `staff`, and `admin`, stored in the broker database and copied into each session. Only local accounts can be admins, and admins must have a confirmed TOTP factor. Client-supplied role claims are ignored.
- **Passwords:** Passwords use `hashlib.scrypt` (N=32768, r=8, p=3, 64-byte key, random 16-byte salt), must have at least 12 characters and at most 1024 UTF-8 bytes, and may not contain the username. Failed checks are rate-limited per account, address, and browser, and take at least one second.
- **TOTP:** TOTP follows RFC 6238: SHA-1, six digits, 30-second steps, a window of one step either side, and strictly increasing accepted counters. The secret is shown once, as text and an `otpauth` URI.
- **Step-up:** Sensitive account actions need the password plus a fresh TOTP code within the last five minutes.
- **Known devices:** A successful local sign-in sets `__Host-portal-device` (HttpOnly, Secure, SameSite=Strict, 90 days), an HMAC-signed browser marker that relaxes some failure budgets but grants no access by itself.
- **Invitations and resets:** Invitations and password resets are single-use, hashed tokens valid for 72 hours. Setup links carry secrets only in the URL fragment.
- **First admin:** The operator runs the portal's bootstrap command on the admin host, which writes a hash-only enrollment file and prints a single-use setup URL valid for 24 hours. [Open the owner portal](../guides/open-the-portal.md#5-create-the-first-portal-admin) describes the procedure.

### Staff and admin authority

Staff and portal admins use the shared `/api/v1/apps/{app}` endpoints for every app, without ownership or team membership. These requests pass through the app-management authority context: reads require CSRF and same-origin metadata, enter the private read audit, and suppress results if authority changes during the read. Mutations recheck the live role in the write transaction, carry the server-only journal marker and enter the app action audit. Owners and teammates retain their app-scoped authority. The `/api/v1/apps` and `/api/v1/intents` lists remain personal for every role.

The portal routes are `/apps`, `/all-apps`, `/people`, `/people/:id`, `/activity`, and `/audit`. App pages live under `/apps/:id`. Role-named routes and duplicate app layouts have been removed, with no redirects.

| Action | Owner (own and team apps) | Staff | Portal admin |
| --- | --- | --- | --- |
| Settings, deploys, environment, storage create, verify, rotate, logs, deploy keys, stop, start, restart | Yes | Any app | Any app |
| Resume blocked changes | Own and team apps; admin-created changes require staff authority | Any app | Any app |
| App and concurrency limits | Quota (default 2 apps, 1 concurrent change) | None | None |
| Accounts, roles, quotas, account audit | No | No | Yes |
| Create an app for another owner | No | Yes | Yes |
| Adopt an operator-created app, reassign an owner, delete storage | No | Yes | Yes |
| Maintenance outage or sizing plan on deploy | No | Yes | Yes |

The app-owner picker reads only IDs, names, roles, enabled flags, and account status from `/api/v1/people/eligible-owners`, with bounded name search and pagination. It grants no account-management access.

App-administration actions and staff/admin intent resumption require a staff or admin session, with no step-up. Account-management step-up is unchanged. Storage deletion still requires typed confirmation and refuses saved bindings. For apps with `requiresMaintenance: true`, the broker sets `maintenance: true` on app-administration deploys. Sign-in dependency messages are informational. Mutation bodies accept only their declared fields; the retired `identityProviderConfirmed` field is rejected with `400 INVALID_REQUEST`.

Each app-administration mutation is journaled with a server-only marker and written to the admin action audit, so admins see staff changes. Foreground app mutations share one busy scope per app (`409 APP_BUSY`). Once the broker observes a controller operation with confirmed `finishing: true`, that intent stops holding the app and concurrency quota; the controller enforces finishing conflicts. Without that evidence, the broker keeps the busy scope. Teams keep one owner plus members in an `app_members` table; members pass the same app checks as the owner, and only the owner, staff, or an admin adds or removes people. An app counts only against its owner's quota.

When the operator deletes an app through the privileged socket, the broker learns of it only from a definitive `404 APPLICATION_NOT_FOUND` on the project read. It then marks its record deleted, stops counting it toward quota, writes one `app_deleted_by_administrator` audit event, and answers further mutations with `410 APPLICATION_DELETED`. Outages never trigger this.

`GET /api/v1/apps/{app}/activity` includes the original actor. `attention=1` selects blocked or unknown intents and cached recovery-required operations without a recent-activity limit, so an older blocked deploy remains visible.

Resume rechecks the caller's live session and role inside the journal update transaction. Already accepted work continues to be observed when its original account is disabled; this does not authorize a disabled account to deliver a prepared or unknown request. It preserves the original actor, body, fingerprint, method, path, client key, and controller key. The `audit` table records `resume` under the caller; staff and admin resumes also record `app_resume` in `admin_audit`, with the app and intent IDs. For admin-created requests, dispatch checks the latest resumer's active staff/admin authority, or the original actor's authority when no resume has been recorded. Environment edits still require resubmission by their original actor with the original key and value; generic resume cannot replay them because the journal stores no values.

### Class records

| Route | Authorization and result |
| --- | --- |
| `GET /api/v1/all-apps` | Staff/admin; every managed app, owner names, cached app status and blocked/unknown changes. Filters: `q` (up to 64 characters), `ownerId`, `status` (including `attention`). Filtering precedes pagination. Cached runtime health older than 30 seconds shows Unknown. Environment retry keys are exposed only to the original actor. |
| `POST /api/v1/all-apps` | Staff/admin; create for an active, enabled `ownerId` |
| `POST /api/v1/all-apps/adopt` | Staff/admin; import an accepted app |
| `GET /api/v1/people/eligible-owners` | Staff/admin; bounded owner picker search, without sensitive account fields |
| `GET /api/v1/people` | Staff/admin; every person’s ID, username, display name, enabled flag, role and setup status; filter by `q` |
| `GET /api/v1/people/{owner}` | Staff/admin; person metadata and effective app and concurrency counts |
| `GET /api/v1/activity` | Staff/admin; class activity with app, actor, kind, status and resumability. Filters: `ownerId`, `applicationId`, `attention=1`. Attention has its own pagination so older blocked changes stay visible. |
| `GET /api/v1/people/{user}/account` | Admin only; account details, sign-in method, limits and authenticator status |
| `POST /api/v1/people` | Admin only; create a local account, with step-up |
| `PATCH /api/v1/people/{user}/account` | Admin only; role, enabled state, sessions, invitations and credential resets, with step-up |
| `PUT /api/v1/people/{user}/quotas` | Admin only; limits, without step-up |
| `GET /api/v1/audit` | Admin only; account and app action audit |
| `PUT /api/v1/apps/{app}/owner` | Staff/admin; reassign, with expected owner check |
| `DELETE /api/v1/apps/{app}/storage/{resource}` | Staff/admin; typed confirmation, with saved-binding check |

Class lists default to 25 records and cap at 50. People and Activity project bounded, allowlisted metadata. Shared app pages use the same app projections for all authorized users, including settings, environment names, storage and logs; secret values remain write-only. App enumeration starts from the broker database; controller global lists remain privileged. People and Activity reads retain the class read budgets (60 per minute per user, 120 per address, two concurrent per user) and the private read audit; app reads retain the queued app read budget. Read-log capacity and authority checks remain fail-closed.

### Portal data and releases

- **Resource projections:** The broker returns environment names and revisions, and storage resource type, label, status, and binding defaults. It never stores or returns values, connection strings, or provider IDs. Environment request fingerprints are HMAC-SHA256 with a key derived from the broker's private `anonymous.key`, so a copy of the broker database cannot be used to guess values offline.
- **Intents:** The broker reserves ownership and quota and records an intent plus a controller idempotency key before calling the controller. Unknown outcomes repeat the exact key and request; accepted operations are polled. Only confirmed cleanup settles an intent. An environment intent whose outcome is lost needs the owner to resubmit the value.
- **Restore:** Restoring the broker database invalidates every session and token, keeps roles, and removes `anonymous.key` (a new one is generated at start).
- **Releases:** Broker and web archives bind the exact source commit, built web assets, dependency locks, SBOM, provenance, and schema and protocol compatibility, signed under the same trust policy as the helper release. Installation activates only a matching broker and web pair. [Releases and upgrades](../guides/releases-and-upgrades.md) covers this.

## Failure invariants

Preserve these invariants when changing the code:

- A build or upload creates a candidate; only role-specific live checks create acceptance.
- A matching provider name is never enough to adopt, change, or delete a resource.
- An unavailable observation never erases accepted state.
- Builders are cleaned up after both success and failure.
- A failed ordinary replacement keeps the accepted route; maintenance failure can leave the app stopped.
- Deleting a worker never deletes managed data or deployment history.
- Persistent host replacement keeps the old server and volumes until the candidate is ready.
- Unknown external results stay journaled and resumable under the same intent and key.
- Environment and managed-service values never enter controller SQLite or API reads.
- Product mutations never move into the operator CLI.
- Executable rollback, database restore, and provider-state recovery are separate operations.

## Related

- [How it works](../how-it-works.md)
- [Security](../security.md)
- [Controller API](controller-api.md)
- [Operator CLI reference](operator-cli.md)
- [Configuration](configuration.md)
- [Development](../development.md)

## Platform health alerts

The admin health timer runs `infra/monitor/check_platform.py` every five minutes. After writing its snapshot, the checker calls `infra/lib/health_alerts.py`. With `healthAlerts.enabled` false or absent, that call performs no secret reads, state writes, or network requests. With alerts enabled, it locks the persistent operator status file, updates the consecutive-failure count, and records any notification attempt before network I/O. The nonblocking lock prevents overlapping runs from waiting on a sender. State changes use an atomic replacement of a mode-`0600` file; attempts survive reboot on the admin state volume.

The URL is a protected runtime input under the existing operator secrets directory, rather than a first-boot image input. This matches how admin OpenStack and storage-bootstrap credentials are delivered to persistent operator secrets and keeps token-bearing URLs out of non-secret inventory and Nix derivations. Operators copy the file through the existing pinned SSH alias. The health service runs as the operator and reads only a direct, owner-owned mode-`0600` URL file; the persistent parent directory is mode `0700`.

`infra/monitor/send_health_alert.py` receives the URL and payload through stdin in a child process. The parent discards child output and kills the child after ten seconds. The child uses verified HTTPS with an eight-second socket timeout, no redirects, and no environment proxies; it neither reads nor logs response bodies. Delivery and state errors are contained by the notification call, preserving the checker's original health exit status. No exactly-once delivery guarantee is possible when the receiver accepts a request but the reply is lost. The persisted attempt suppresses immediate duplicate failure alerts; recovery retries can duplicate a message after such an uncertain result.

No health-unit network restriction is relaxed. The existing unit already calls OpenStack and public HTTPS health endpoints and has no `IPAddressDeny` or address-family sandbox. The foundation preserves Neutron's default egress rules, and the common NixOS firewall restricts inbound traffic. Additional operator/provider egress restrictions must permit the configured webhook destination. Both health and the test unit depend on the admin state mount so persistent files cannot silently be written under an unmounted volume. The test unit adds read-only filesystem protection and runs as the same unprivileged operator.

The checker stops after its first failed check. Alerts therefore describe the first missing check from the ordered check list and do not send exception details. It does not query app-operation states. A process killed before notification or a timer that stops running produces no webhook alert; independent host monitoring is required for those failures.
