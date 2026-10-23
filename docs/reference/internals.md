# Internals

This reference explains state ownership, process boundaries, and failure handling for contributors and operators. Read [How it works](../how-it-works.md) for the platform tour; terms are defined in the [glossary](../README.md#glossary).

## Contents

- [Controller](#controller)
- [Broker and portal](#broker-and-portal)
- [Helper](#helper)
- [Workers and Nomad](#workers-and-nomad)
- [Builders](#builders)
- [Ingress](#ingress)
- [Storage instances](#storage-instances)
- [S3 and Garage](#s3-and-garage)
- [Backups](#backup-and-recovery-model)
- [Images and CI](#images-and-ci)
- [Operator host and setup](#operator-host)
- [Health alerts](#platform-health-alerts)

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

Persistent host replacement moves the fixed port and volumes to a candidate built from the selected image UUID. Identity and readiness checks precede old-server deletion. A confirmed failure triggers rollback to the retained server. Ambiguous provider results require recovery. See [Hosts and images](../guides/hosts-and-images.md#replace-a-persistent-host).

## Processes and trust boundaries

The operating system separates the controller, broker, web server, identity service, and storage manager. Unix socket peer credentials identify local callers; browser roles and ownership are checked by the broker. The following sections describe each subsystem’s authority and state.

## Controller

The controller owns app records and lifecycle decisions. Its fixed helper performs external actions against OpenStack, Nomad, the registry, and storage.

The `openstack-platform-controller` process runs as `platform-controller`. Its systemd unit grants no capabilities, makes the system read-only, and limits writes to controller state, build logs, helper diagnostics, deploy keys, and hosted-controller backups. The unit allows the address families needed by its helper subprocesses; it has no HTTP network listener.

Implementation: [`api.py`](../../openstack_platform/controller/api.py), [`application_service.py`](../../openstack_platform/controller/application_service.py), [`deployment_service.py`](../../openstack_platform/controller/deployment_service.py), and [`admin.nix`](../../nix/roles/admin.nix).

### Controller sockets

The controller has no network listener.

The admin role exposes two mode-`0660` Unix sockets in `/run/<namespace>-controller/` (a mode-`0750` runtime directory):

| Socket | Group | Admitted peer | Capability |
| --- | --- | --- | --- |
| `project.sock` | `controller-api` | `management-broker` UID:GID | Health and product routes, including confirmed storage and app deletion |
| `privileged.sock` | `platform-admin` | The operator account's UID:GID | Administrator reads and operations, and cascade app deletion |

The controller admits at most 32 connections per peer on each socket. The broker permits 24 concurrent controller requests with a five-second acquire timeout. Background observation refreshes and journal recovery share at most four of those slots, leaving 20 available for foreground requests. Background work acquires its share before competing for a controller slot; queued refreshes hold no controller capacity. The controller limit leaves headroom for readiness checks and connections still closing.

Before parsing HTTP, the server reads Linux `SO_PEERCRED` and admits only the socket’s configured UID:GID pairs. Requests cannot upgrade capabilities. This identifies the local process; `management-broker` handles browser ownership, quotas, sessions, and CSRF. See [Controller API](controller-api.md).

`<namespace>-controller-readiness` runs as `management-broker` and waits for `GET /v1/health` on the project socket. The broker requires the readiness unit and restarts with it.

### Application lifecycle

A deployment builds one exact commit. Its candidate becomes the accepted deployment only after health checks pass; the accepted pointer records the version the platform recognizes as live. Ordinary replacement keeps the accepted version serving on candidate failure. Maintenance deployments stop it before cutover and can leave the app stopped.

#### Deployment steps

For project and operator deployment requests, the controller:

1. Validates the app, the canonical GitHub URL, the requested ref, the exact commit, the configuration revision, and the closed, typed configuration.
2. Records an immutable deployment attempt and accepts an operation under the request's `Idempotency-Key` (which becomes the deployment ID).
3. Asks the helper to build the exact source snapshot.
4. The helper fetches the source, resolves the requested Node.js or Bun version, and generates a build recipe.
5. Creates a single-use builder, runs rootless BuildKit, and records the pushed image’s immutable digest.
6. Deletes the builder and its port, and verifies they are gone.
7. Creates the app's worker, or, for an operator worker-reuse deployment, verifies the exact accepted worker.
8. Submits the generated Nomad job and a candidate route.
9. Accepts the candidate only after Nomad, the app’s health path, and the public route pass. The public response must carry the deployment’s `X-Platform-Deployment` marker.
10. After durable acceptance, verifies any address handover, removes the exact predecessor job and worker, and applies registry retention. Finishing failures use the durable retry schedule described below.
11. On candidate failure, records startup evidence when available and attempts bounded candidate cleanup. Ordinary replacement keeps the accepted route; maintenance can leave the app stopped. Unconfirmed cleanup requires recovery.

Repositories supply source and supported lockfiles. Package paths and script names are data, never shell commands. Storage bindings map outputs to environment names without creating or deleting storage. Secret values live in app-scoped Nomad Variables; SQLite keeps environment names, owners, revisions, and timestamps, and API reads omit values.

Before removing an unhealthy candidate, `app.startup` reads the newest allocation's status, restart count, last 12 task events, and last 200 stdout and stderr lines (64 KiB each). It gets at most 30 seconds or one third of the remaining deadline; failure never blocks removal. The mode-`0600` record is `startup-logs/<application>/<deployment>.json` under controller state and appears on the portal's failed deployment page.

With `maintenance: true`, the old version serves during build and artifact/storage checks. Under the app lock, the controller journals its job, image, placement, server, and port, then stops it before starting the candidate. Acceptance alone changes the accepted pointer. Retries use the stop checkpoint. The operator, staff, or a portal admin can request this through their respective interfaces. Cutover interrupts service and prevents concurrent app processes.

### Authoritative reads and retained rollback

App reads take `activeDeploymentId` from the accepted pointer. Deployment reads include `configuration`, `configurationSha256`, `sourceRepository`, commit, and image digest. Source evidence comes from journaled intent, not current app settings. Missing or inconsistent evidence makes `sourceRepository` null and prevents rollback to that attempt.

Rollback redeploys a different complete, successful attempt of the same enabled app without rebuilding. Its plan binds accepted deployment, environment revision, sizing, storage identities, historical image, and configuration. Apply rechecks that projection under the app lock, storage outputs, and registry image availability (`app.manifest.verify` uses manifest/blob `GET` and `HEAD` reads).

Rollback keeps the current worker flavor and secrets; a replacement worker gets a newly measured allocation, while worker reuse keeps the accepted allocation. Derived values such as `PORT` follow candidate configuration. It restores no environment snapshot, database, bucket objects, or worker filesystem. Ordinary candidate failure keeps the predecessor. Acceptance transfers and verifies any floating IP before predecessor cleanup. Resize follows the same path with the accepted image and new sizing. See [Deploy apps from the command line](../guides/deploy-apps-from-the-command-line.md).

### SQLite and operation state

SQLite stores controller decisions and operation checkpoints. After restart, callers resume interrupted foreground work with the same request and key. Accepted finishing work resumes automatically from its recorded intent; uncertain provider outcomes remain unresolved until checked.

[`openstack_platform/controller/database.py`](../../openstack_platform/controller/database.py) owns schema creation, forward migrations, the deployment binding, and durable records. These include apps, deployment attempts and accepted pointers, environment metadata, managed resources, images, slug tombstones, idempotency requests, operations and dispatches, address reservations, and builder settings. Unsupported prior schemas fail before any migration or provider call.

Transactions do not span provider calls. Domain-service checkpoints coordinate external work. Provider observations are evidence, not accepted state; reconciliation never creates app or storage records from matching external resources.

Requests that create records or operations require `Idempotency-Key`; synchronous deploy-key changes are repeatable without one. The SHA-256 fingerprint covers method, path, and parsed body. Identical requests replay their result; different input yields `409 IDEMPOTENCY_CONFLICT`. The key becomes the operation ID, or the application ID for creation. Strict deployment configurations count as public metadata, so output names such as `password` and `secret_access_key` remain valid binding names in fingerprints.

App creation is database-only (`201`). Background operations reserve `app-<id>` or `infrastructure` before external work (`202`), with one unfinished foreground operation per scope. A separate accepted finishing reservation can coexist with compatible foreground work; both use the same app lock. Four threads handle at most 32 admitted operations. Deploy-key and storage-label changes are synchronous exceptions; see [route contracts](controller-api.md#routes).

The dispatch journal omits request bodies. Before sockets open after restart, interrupted foreground operations with domain intent become `recovery_required`, retaining checkpoints; dispatches without domain intent fail terminally with nothing to clean up.

Repeat the identical request and key to resume, supplying environment values again. New keys cannot bypass unfinished work. A renewed deadline sets `running` and clears the safe error. Build rejection becomes terminal only after builder and build-tag absence is proven; uncertain cleanup retries without rebuilding.

Operations and dispatches have a `finishing` flag and separate uniqueness constraints for foreground and finishing admission. Deployment acceptance commits the flag with the active deployment pointer and successful attempt. Enable commits it with accepted runtime presence. Migration recognizes a post-acceptance checkpoint only when the successful attempt, active pointer, operation ID, and candidate image agree. An unaccepted `deployment_healthy` checkpoint is not finishing work.

The controller journals automatic retry count and next retry time in `refs.finishing_retry`, without storing a request body. Five retries wait 60, 120, 240, 480, and 600 seconds; each attempt renews its deadline for at most 120 seconds. The operation stays `running` between attempts and becomes `recovery_required` only after exhaustion.

Restart preserves waiting schedules and counts interrupted attempts. Automatic and manual replay use the same dispatch claim, so they cannot execute the same operation twice concurrently. Manual replay keeps the original intent and key and does not reset the automatic budget.

The finishing path reobserves the accepted job and public route before handover or predecessor removal. It never rewrites acceptance, environment values, or desired-running state. Handover reobserves exact provider associations; unknown outcomes do not authorize a repeated mutation. Worker deletion requires exact predecessor job absence first. The journaled `predecessor_cleanup` phase means handover is confirmed; `accepted` means only registry retention remains.

Compatible environment changes, storage verify/rotate, and restart retain foreground admission. Enable can be a no-op after handover. Disable also requires journaled predecessor job absence and a separate cleanup placement; worker creation for a stopped app, another deploy, resize, rollback, deletion, storage create/remove, and address changes remain blocked with `POST_ACCEPTANCE_CONFLICT`.

Helper failures write a bounded controller journal line with the fixed action, error class, and an allowlisted error code. Unknown codes are redacted. Exception messages, request arguments, environment values, credentials, stderr, response bodies, and tracebacks are omitted; `safeError` remains a separate user-safe summary.

Mutating handlers and operation admission share the API lock, including synchronous deploy-key and storage-label writes. Read-only handlers skip it: app and deployment records, logs, environment names, storage, flavor and builder settings, sizing and rollback plans, address reads and plans, and all administrator reads. Database inputs come from short transactions on private query-only connections. The snapshot closes before provider, helper, network, or file probes; app-scoped read plans keep their existing app locks during validation. No read handler uses the shared writer. Identical app and deployment rows share a live app probe and reuse it for two seconds.

`GET /v1/admin/status` captures accepted state, unfinished operations, cached storage usage, and storage-host metrics in one snapshot, then runs live infrastructure and app probes outside the API lock. `GET /v1/admin/hosts` follows the same capture/probe boundary. Status observers and projections perform no database writes; the usage collector owns updates to cached metrics.

Live app health combines the helper’s `app.health` Nomad allocation evidence with a public request to the accepted deployment’s health path. The public response must carry its `X-Platform-Deployment` route marker. Disabled apps report `stopped` without probes; missing evidence or transport failures report `unknown`.

### Whole-app deletion

`POST /v1/applications/{id}/delete` is available on both controller sockets. The controller journals the cascade, removes the app’s jobs, managed database instances and data, S3 bucket and objects, Nomad Variable, deploy key, workers, address reservations, and registry manifests, then records a slug tombstone. Provider absence must be confirmed before deletion advances. Unknown outcomes require same-key recovery.

The portal adds admin-only authorization, recent re-authentication, and exact-slug confirmation. Deletion cannot be undone through the API. Recovery requires platform backups and any separately escrowed app secrets.

## Broker and portal

The portal is the web interface for 6.1040 Apps. The broker owns browser authorization and portal state; it calls the controller as the sole project peer.

Implementation: [`management/broker/`](../../openstack_platform/management/broker/), [`management/web/server.py`](../../openstack_platform/management/web/server.py), [`management/identity/client.py`](../../openstack_platform/management/identity/client.py), and [`admin.nix`](../../nix/roles/admin.nix).

### Owner portal processes

Each portal service has a separate systemd unit and unprivileged account:

| Service | Network | Can reach | Cannot reach |
| --- | --- | --- | --- |
| `management-web` | Listens on TCP 8080, accepting only the ingress host's address | `broker.sock` | Controller sockets, controller and operator state, broker state, platform PKI, operator secrets |
| `management-broker` | None (Unix sockets only) | `project.sock`, `identity.sock` | `privileged.sock`, operator state, platform PKI, operator secrets |
| `management-identity` | Outbound only to the inventory's `ownerPortal.identityEgressCidrs` (Cloudflare's published ranges by default) and the local DNS resolver | Commons | Persistent state directories, controller sockets, platform PKI, operator secrets |

`broker.sock` (in `/run/<namespace>-management-broker/`) admits only the `management-web` peer. `identity.sock` (in `/run/<namespace>-management-identity/`) admits only the `management-broker` peer. Both reuse the controller's transport, with the same `SO_PEERCRED` check.

### Owner portal internals

The broker checks browser sessions, ownership, and quotas before calling the controller’s project socket. Staff and admins can manage apps across owners under the authority rules below.

```text
browser -> HTTPS provider -> ingress -> management-web -> broker.sock -> management-broker
                                                                            | -> project.sock -> controller
                                                                            | -> identity.sock -> management-identity -> Commons (HTTPS)
```

`management-web` serves the React app as static files under a strict content security policy and forwards only an allowlisted set of paths: `/api/v1/...` to the broker's `/v1/...`, and `/auth/...` to `/v1/auth/...`. It owns no authoritative state. The broker owns users, sessions, ownership, teams, quotas, the intent journal, and the audit log, and is the only controller project peer. The identity service admits only the broker and talks only to the one configured Commons origin.

#### Sign in with Commons

Commons Connect uses an authorization-code flow with PKCE (RFC 7636, `S256`); the portal never receives a Commons password. Commons must accept the portal origin (`https://<domain>`) as an app.

1. `GET /auth/commons/start` checks per-address admission and accepts a same-origin or typed navigation. It sets `__Host-portal-commons` (HttpOnly, Secure, SameSite=Lax, ten minutes) and redirects to `<commonsOrigin>/connect?app=<portal origin>&state=<state>&code_challenge=<challenge>&code_challenge_method=S256`.
2. Commons asks for approval and redirects to `/auth/commons/callback` with `code` and `state`, or `error=access_denied` after Cancel.
3. The callback clears the binder cookie, checks admission and binder validity, and compares the state in constant time. Only then does it ask the identity service to redeem the code.
4. The identity service posts `{code, app, code_verifier}` to Commons `/api/connect/redeem`. It uses verified system TLS, no redirects or proxies, strict JSON, and bounded requests. It returns only the stable subject UUID, username, and display name.
5. The broker maps Commons origin and stable subject to an account. First sign-in creates an `owner`; disabled or pending accounts are refused. Successful sign-in creates a session.

The cookie holds an HMAC-signed binder. Separate HMACs derive the state and PKCE verifier from it, so no per-sign-in server record is needed. The challenge is the verifier’s SHA-256 in unpadded base64url. The verifier leaves the broker only for redemption. Reading a callback code alone does not supply the verifier required to redeem it.

Both navigations answer only with `303` redirects: start leaves only for the exact Commons approval URL, and the callback lands only on an allowlisted portal page or `/sign-in?error=CODE`. The sign-in page explains `COMMONS_CANCELLED`, `SIGN_IN_EXPIRED`, `IDENTITY_UNAVAILABLE`, `ACCOUNT_DISABLED`, and `RATE_LIMITED`. Codes, states, and verifiers are never stored, logged, or echoed. If the identity service is down, new Commons sign-ins fail but existing sessions keep working.

#### Sessions and CSRF

The `__Host-portal-session` cookie contains an opaque token (HttpOnly, Secure, SameSite=Lax, `Path=/`, no `Domain`). The broker stores its digest with the server-side session record. Every request rechecks the account's current enabled state, role, and security generation; a security change increments the generation and ends all of that account's sessions.

| Session | Absolute lifetime | Idle timeout |
| --- | --- | --- |
| Owner | 8 hours (default) | 30 minutes (default) |
| Staff | 1 hour | 10 minutes |
| Portal admin | 1 hour | 15 minutes |

Mutations require the exact portal `Origin` and the session's CSRF token; admin and staff reads also require the CSRF token and same-origin metadata. Anonymous forms (local login, enrollment) use an HMAC CSRF token bound to a Strict `__Host-portal-anonymous` cookie. Logout, local revocation, expiry, and restore end sessions. Password changes, archiving, or withdrawn approval in Commons do not end portal sessions that already exist.

`GET /v1/session` returns the same CSRF token for the session's lifetime. The broker derives it with HMAC-SHA-256 using its private `anonymous.key`, the separate `session-csrf` purpose, portal origin, and session digest. Tokens are unguessable and compared in constant time after validating the session. Repeated page loads, focus changes, and reads from other tabs cannot evict a token. Session rotation changes the token; logout, idle/absolute expiry, and security-generation revocation invalidate it with the session. Broker schema 4 removes the unused `csrf_tokens` table without discarding sessions.

On `403 CSRF_REJECTED`, the portal shares a session refresh across concurrent failures and retries each request once. Queries keep their cancellation signal; mutations keep the same body and idempotency key. Broker CSRF validation precedes mutation writes and controller dispatch. A second rejection or failed refresh surfaces the error, and a credential change prevents replay into a different session.

#### Local accounts, roles, and admin enrollment

Local accounts use issuer `local`, an immutable UUID subject, and a unique lowercase username. They store salted password hashes and private TOTP state.

- **Roles:** `owner`, `staff`, and `admin`, stored in the broker database and copied into each session. Only local accounts can be admins, and admins must have a confirmed TOTP factor. Client-supplied role claims are ignored.
- **Passwords:** Passwords use `hashlib.scrypt` (N=32768, r=8, p=3, 64-byte key, random 16-byte salt), must have at least 12 characters and at most 1024 UTF-8 bytes, and may not contain the username. Failed checks are rate-limited per account, address, and browser, and take at least one second.
- **TOTP:** TOTP follows RFC 6238: SHA-1, six digits, 30-second steps, a window of one step either side, and strictly increasing accepted counters. The secret is shown once, as text and an `otpauth` URI.
- **Step-up:** Sensitive account actions need the password plus a fresh TOTP code within the last five minutes.
- **Known devices:** A successful local sign-in sets `__Host-portal-device` (HttpOnly, Secure, SameSite=Strict, 90 days), an HMAC-signed browser marker that relaxes some failure budgets but grants no access by itself.
- **Invitations and resets:** Invitations and password resets are single-use, hashed tokens valid for 72 hours. Setup links carry secrets only in the URL fragment.
- **First admin:** The operator runs the portal's bootstrap command on the admin host, which writes a hash-only enrollment file and prints a single-use setup URL valid for 24 hours. [Open the owner portal](../guides/open-the-portal.md#5-create-the-first-portal-admin) describes the procedure.

#### Staff and admin authority

Staff and portal admins use the shared `/api/v1/apps/{app}` endpoints for every app, without ownership or team membership. These requests pass through the app-management authority context: reads require CSRF and same-origin metadata, enter the private read audit, and suppress results if authority changes during the read.

Mutations recheck the live role in the write transaction, carry the server-only journal marker and enter the app action audit. Owners and teammates retain their app-scoped authority. The `/api/v1/apps` and `/api/v1/intents` lists remain personal for every role.

The portal routes are `/apps`, `/all-apps`, `/people`, `/people/:id`, `/activity`, `/audit`, and admin-only `/platform-settings`. App pages live under `/apps/:id`.

| Action | Owner (own and team apps) | Staff | Portal admin |
| --- | --- | --- | --- |
| Settings, deploys, environment, storage create, verify, rotate, logs, deploy keys, stop, start, restart | Yes | Any app | Any app |
| Resume blocked changes | Own and team apps; admin-created changes require staff authority | Any app | Any app |
| App and concurrency limits | Quota (default 2 apps, 1 concurrent change) | None | None |
| Accounts, roles, quotas, account audit | No | No | Yes |
| Create an app for another owner | No | Yes | Yes |
| Adopt an operator-created app, reassign an owner, delete storage | No | Yes | Yes |
| Maintenance outage, worker size, or app build machine | No | Yes | Yes |
| Default build machine | No | No | Yes |
| Delete an entire app (with step-up) | No | No | Yes |

The app-owner picker reads only IDs, names, roles, enabled flags, and account status from `/api/v1/people/eligible-owners`, with bounded name search and pagination. It grants no account-management access.

Shared app-administration actions and staff/admin intent resumption require a staff or admin session, with no step-up. Whole-app deletion and its resumption are exceptions: they require an active admin session with re-authentication in the last five minutes, checked again in the admission/resume transaction. Account-management step-up is unchanged. Storage deletion still requires typed confirmation and refuses saved bindings. For apps with `requiresMaintenance: true`, the broker sets `maintenance: true` on app-administration deploys. Mutation bodies accept only their declared fields.

Each app-administration mutation is journaled with a server-only marker and written to the admin action audit, so admins see staff changes. Foreground app mutations share one busy scope per app (`409 APP_BUSY`). Once the broker observes a controller operation with confirmed `finishing: true`, that intent stops holding the app and concurrency quota; the controller enforces finishing conflicts. Without that evidence, the broker keeps the busy scope.

Teams keep one owner plus members in an `app_members` table; members pass the same app checks as the owner, and only the owner, staff, or an admin adds or removes people. An app counts only against its owner's quota.

When the operator deletes an app through the privileged socket, the broker learns of it only from a definitive `404 APPLICATION_NOT_FOUND` on the project read. It then marks its record deleted, stops counting it toward quota, writes one `app_deleted_by_administrator` audit event, and answers further mutations with `410 APPLICATION_DELETED`. Outages never trigger this. A successful portal deletion also retires the broker record immediately after confirmed controller completion.

Both paths remove team memberships, saved configuration revisions, observations, source-check caches, and runtime-log caches. App environment values live in Nomad Variables; private deploy keys live in the helper’s source-key directory. Controller deletion removes both before recording its tombstone.

Broker sessions are account-scoped and contain no app references, so deleting an app does not sign its users out. Intent and audit history are retained. In-flight read results cannot repopulate caches after retirement.

`GET /api/v1/apps/{app}/activity` includes the original actor. `attention=1` selects blocked or unknown intents and cached recovery-required operations without a recent-activity limit, so an older blocked deploy remains visible.

Resume rechecks the caller's live session and role inside the journal update transaction.

Already accepted work continues to be observed when its original account is disabled; this does not authorize a disabled account to deliver a prepared or unknown request. It preserves the original actor, body, fingerprint, method, path, client key, and controller key.

The `audit` table records `resume` under the caller; staff and admin resumes also record `app_resume` in `admin_audit`, with the app and intent IDs. For admin-created requests, dispatch checks the latest resumer's active staff/admin authority, or the original actor's authority when no resume has been recorded.

Whole-app deletion requires an active admin actor at dispatch; staff cannot authorize its replay. Its admission and each manual resume require fresh step-up, while automatic same-key reconciliation does not require keeping a browser session alive.

Environment edits require resubmission by their original actor with the original key and value; generic resume cannot replay them because the journal stores no values.

An environment change restarts the app and checks its health. If the app is unhealthy, the helper restores the previous values and checks the app again. When that recovery is confirmed, the change fails with `ENVIRONMENT_UNHEALTHY` and the app stays free for a corrected edit; only an unconfirmed rollback needs resubmission. `nomad job restart` only restarts running allocations, so when Nomad has stopped restarting a crashing app, the helper reschedules the job with `nomad job eval -force-reschedule` instead.

#### Cached app reads

My apps (`GET /api/v1/apps`) returns the last observation from broker SQLite without controller calls. Ownership and team membership still select the rows for every role. Missing observations and those at least 30 seconds old enter the same bounded refresh pool as All apps, using the broker client's background connection share. Last known health remains usable for ten minutes; older or invalidated health is stale. `observedAt` records the last check, `observationAgeSeconds` gives its age (null when missing or invalidated), and `refreshing` marks queued or running work. The portal polls every 1.5 seconds while any row is refreshing, otherwise every five seconds. Missing or stale health shows Checking while its refresh is pending.

App details (`GET /api/v1/apps/{app}`) use observations up to 30 seconds old and queue a refresh after two seconds. This 30-second bound matches list refresh admission and limits the age of the initial health display while the background check runs. Missing, invalidated, or older observations retain synchronous checks. Staff metadata, worker capacity enrichment, and the accepted deployment's settings revision are cached with health, so a cache hit makes no controller call. Owner/member responses omit sizing and maintenance metadata. The app header polls every 1.5 seconds while refreshing, otherwise every five seconds.

Every app mutation invalidates observations in its admission transaction; journal dispatch invalidates them again when recording admission, progress, or completion. Detail reads bypass the display cache while an app intent is prepared, unknown, or accepted, including accepted finishing work. Cache writes compare the app's settings revision and the journal's count/latest update with their values before the probe; an overlapping mutation prevents an older probe from restoring a valid cache. External deletion is detected by background app observations and the journal scan; mutations retain their synchronous deletion check.

#### Class records

The All apps response reads observations from SQLite without waiting for controller reads. It queues observations missing or at least 30 seconds old for only the returned page, reusing `app_model` validation and cache writes. A broker-wide pool runs at most four checks at once, with at most 100 queued or running apps and single-flight admission per app.

Failed checks preserve the last observation. Definitive app-not-found results reconcile external deletion through the existing journal; its round-robin background scan also continues. Shutdown cancels queued work and joins running checks. The portal polls every 15 seconds, or every three seconds while a row is Unknown or refreshing. Attention items appear beneath the app state only when present.

| Route | Authorization and result |
| --- | --- |
| `GET /api/v1/all-apps` | Staff/admin; every managed app, owner names, cached app status and blocked/unknown changes. Filters: `q` (up to 64 characters), `ownerId`, `status` (including `attention`). Filtering precedes pagination. Last known app state is used for up to ten minutes; missing or older observations show Unknown. `observedAt` gives the cache timestamp; `refreshing` marks queued or running checks. Environment retry keys are exposed only to the original actor. |
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
| `POST /api/v1/apps/{app}/delete` | Admin only with step-up; body `{"confirmation":"<exact slug>"}` and Idempotency-Key; returns `202` with an `app_delete` intent. Wrong confirmation returns `400 CONFIRMATION_REQUIRED`; stale step-up returns `403 STEP_UP_REQUIRED`. Same-key replay returns the original intent even after deletion; changed input returns `409 IDEMPOTENCY_CONFLICT`. |

Class lists default to 25 records and cap at 50. People and Activity project bounded, allowlisted metadata. Shared app pages use the same app projections for all authorized users, including settings, environment names, storage and logs; secret values remain write-only. App enumeration starts from the broker database; controller global lists remain privileged. People and Activity reads use token buckets refilled at one request/s per user and two/s per address, with bursts of ten and twenty. Each read limiter allows 12 concurrent reads per user and 64 overall, accommodating a page’s 6–10 parallel reads and several staff loading pages together. Elevated app reads use a separate token bucket with bursts of 40 per person and 80 per address, refilling at four and eight requests/s respectively. These token budgets still limit sustained abuse. Owners have no equivalent in-flight read limiter; their controller reads share the same bounded client. The private read audit retains 30 days with a two-million-row cap; exhausted capacity or failed authority checks refuse the read.

Portal API queries retry transient failures at most three times: retryable `429 RATE_LIMITED`, `502`, `503`, `504`, and network failures. Backoff is 250 ms, 750 ms, then two seconds, increased to respect `Retry-After` (seconds or HTTP date) or the JSON `retryAfterSeconds` hint. Other HTTP failures, including `400`, `401`, `403`, `404`, and `409`, are not retried. Mutations have no query-client retries. The browser queues elevated reads above 12 concurrent requests. Failed refetches keep loaded data; page and section load errors appear only when no data has loaded.

#### Portal data and releases

- **Resource projections:** The broker returns environment names and revisions, and storage resource type, label, status, and binding defaults. It never stores or returns values, connection strings, or provider IDs. Environment request fingerprints are HMAC-SHA256 with a key derived from the broker's private `anonymous.key`, so a copy of the broker database cannot be used to guess values offline.
- **Intents:** The broker reserves ownership and quota and records an intent plus a controller idempotency key before calling the controller. Unknown outcomes repeat the exact key and request; accepted operations are polled. Only confirmed cleanup settles an intent. An environment intent whose outcome is lost needs the owner to resubmit the value.
- **Restore:** Restoring the broker database invalidates every session and token, keeps roles, and removes `anonymous.key` (a new one is generated at start).
- **Releases:** Broker and web archives bind the exact source commit, built web assets, dependency locks, SBOM, provenance, and schema and protocol compatibility, signed under the same trust policy as the helper release. Installation activates only a matching broker and web pair. [Releases and upgrades](../guides/releases-and-upgrades.md) covers this.

## Helper

The helper executes a closed action protocol. It runs as the caller and uses fixed service clients and provider commands. The root-owned storage manager and narrowly allowed systemd actions supply the privileged storage operations.

Implementation: [`remote.py`](../../openstack_platform/remote.py), [`helper/main.py`](../../openstack_platform/helper/main.py), [`helper/production.py`](../../openstack_platform/helper/production.py), and the release-bound action list below.

The helper (`<paths.root>/bin/openstack-platform-helper`) reads exactly one protocol-v1 JSON request from standard input and writes one JSON response to standard output:

```json
{"version": 1, "requestId": "<uuid>", "action": "app.build", "args": {}}
```

For a valid request, the response echoes `version` and `requestId`. Malformed requests use a valid extracted request ID when possible, otherwise the zero UUID. Success adds `"ok": true` and `result`; failure adds `"ok": false` and `error: {code, message}`. Requests and responses are limited to 1 MiB. Request input cannot choose an executable, a remote host, a provider credential, or a shell command.

Implemented actions must exactly match the release-bound [`actions-v1.txt`](../../openstack_platform/helper/actions-v1.txt); release smoke tests reject a mismatch. Unknown actions return `UNKNOWN_ACTION`.

| Area | Actions |
| --- | --- |
| Builds | `app.build`, `app.build.cleanup`, `app.build.logs`, `app.builder.delete` |
| Workers | `app.worker.create`, `app.worker.observe`, `app.worker.capacity`, `app.worker.delete` |
| Nomad jobs | `app.deploy`, `app.promote`, `app.health`, `app.restart`, `app.stop`, `app.remove`, `app.startup`, `app.logs` |
| Environment | `app.env.list`, `app.env.set`, `app.env.remove` |
| Registry | `app.manifest.retain`, `app.manifest.verify`, `app.manifest.delete` |
| Source | `app.source.key`, `app.source.check`, `app.source.commits`, `app.source.preflight` |
| Managed storage | `storage.<type>.create`, `.observe`, `.usage`, `.limits`, `.verify`, `.rotate`, `.remove` for `postgres`, `mongo`, and `s3`; `storage.host.observe` reads authenticated host metrics |
| Instance migration and networking | `storage.instances.available`, `.migrate`, `.abort`, `.network` |
| Backups | `backup.accept` (operator-state upload), `storage.backup.ensure` (resource checkpoint) |

### Deploy keys for private repositories

The helper creates a per-app ed25519 pair with `ssh-keygen` at `<adminState>/controller/source-keys/<slug>/` (files `0600`). Key responses return only the public half. The helper never returns the private key through the protocol or copies it to a builder. Fetch first uses credential-free HTTPS, then, if a key exists, `ssh://git@ssh.github.com:443/OWNER/REPO.git` with that key and GitHub’s pinned ed25519 host key. Both require the exact commit; failure yields `SOURCE_REJECTED`.

Key removal renames the directory before deletion, exposing the whole pair or nothing to builds and backups. Cascade deletion removes it after the job and Variable. Keys accompany hosted-controller backups ([backup model](#backup-and-recovery-model)).

## Workers and Nomad

Each app has one dedicated worker VM. Replacement deployments, resizing, and OS upgrades discard its local disk. Apps must put persistent data in managed storage. Operator worker reuse is the explicit exception described below.

Workers have no SSH service, block cloud metadata, and accept app traffic only from ingress. Generated Nomad jobs use neither privileged containers nor host volumes. App credentials come from app-scoped Nomad Variables.

Implementation: [`worker.nix`](../../nix/roles/worker.nix), [`nomad_jobs.py`](../../openstack_platform/controller/nomad_jobs.py), [`worker_capacity.py`](../../openstack_platform/helper/worker_capacity.py), and [`sizing.py`](../../openstack_platform/controller/sizing.py).

### Health deadlines

Nomad's healthy deadline is 10 minutes from placement, including image download; its progress deadline is 12 minutes. Crash loops fail sooner through restart policy. Controller polling lasts up to 12 minutes, reserving time for startup evidence and job, worker, and artifact cleanup. Enable resubmits the accepted job unchanged, including older deadlines.

### Worker sizes

The project peer can list closed OpenStack flavor capacity projections and fetch the same fingerprinted worker sizing plan as the operator. The portal groups worker choices by flavor family and shows a plain before/after review. Staff and admins edit the app's build machine in its own Settings card; Deploy shows it read-only. Sizes that cannot leave 64 MiB after the OS/service memory reserve are excluded. The broker admits only staff and admins, validates the plan against the current app observation, and forwards it unchanged on deploy. A fresh worker’s allocation comes from its measured Nomad capacity:

1. Subtract the larger of Nomad’s configured reserve and the platform reserve. The platform reserve is at least 200 MHz CPU and 512 MiB memory, or 10% of each total rounded up, whichever is larger.
2. With a sizing plan, clamp the memory budget to the selected flavor’s RAM after the platform reserve.
3. Set the hard memory limit to `(budgetMiB - 32)` rounded down to 128 MiB, with a 64 MiB minimum.
4. Set the CPU share to half the CPU budget rounded down to 100 MHz, with a 100 MHz minimum.

Nomad’s CPU allocation is a soft share: the app can use all worker vCPUs. Memory is a hard limit. Enable and worker reuse validate the accepted allocation against measured capacity; resume keeps the checkpointed allocation. A selected worker size replaces the VM. A retained primary address requires maintenance cutover after the build.

Staff/admin app details enrich `sizing` with optional `vcpus` and `ram_mib`, matched by worker flavor name or ID from `/v1/flavors`. The broker shares the expensive flavor read across requests with a ten-minute, single-flight cache; failures omit enrichment and share a 30-second retry cooldown. The worker size choices use the same cache.

The controller caches OpenStack flavor observations for display reads: `/v1/flavors`, the default builder size, and app builder sizes (on either socket). Startup asynchronously prewarms the catalog, default builder, per-app builders, and worker flavors in use, captured through a private database snapshot. A five-second maintenance cycle discovers changed selections and refreshes retained entries after nine minutes, one minute before their ten-minute lifetime ends. Successful lifetimes begin at provider completion. Reads return the last successful value immediately even after expiry while one refresh per reference runs; a cold entry retains synchronous lookup and a 20-second follower wait. Validation paths observe OpenStack directly: builder selection, sizing-plan creation/validation, deployment, and provider resource validation bypass the display cache.

Flavor background work uses two threads with at most 32 queued or running tasks shared by catalog and reference reads. The cache retains at most 128 reference entries and one catalog entry, evicting entries with no pending lookup by least recent use. Failures keep the last successful value and retry after 5, 10, 20, 40, 80, 160, then 300 seconds; repeated failures remain capped at 300 seconds and success resets the schedule. Cold failures share the same backoff. Provider calls retain their 30-second deadline. Controller shutdown stops maintenance, cancels queued work, and joins running calls. These are display observations, so a removed or changed flavor may remain visible during an outage; fresh validation still governs whether it can be selected or deployed.

Overview’s Machines section separates total machine RAM from the app's hard memory limit and states that the app can use all worker vCPUs. Nomad CPU is a soft share, with no CPU hard limit. Without enrichment, the portal shows the flavor and memory limit. Deploy's size review shows an upper bound for memory until the worker's capacity is measured.

The build machine uses the app builder-size read. Owners and teammates cannot read sizing or see Machines.

### Worker reuse

Operator-only `reuseWorker: true` requires `maintenance: true`. The controller pins the accepted worker and its size, rechecking `ACTIVE` state, ownership, attachment, Nomad and Docker health, and capacity. It stops the old job and starts the new image on that worker; no server or IP changes. Failed cutover keeps the accepted pointer but leaves the app stopped with its worker retained. OS or flavor changes require replacement.

### Retained worker primary ports

An app can retain a fixed primary IPv4 across worker replacements.

The controller journals the Neutron reservation separately from worker slots. Server metadata belongs to a generation; port name, UUID, network, subnet, security group, and address belong to the reservation.

Worker actions accept only the controller’s closed `retainedPort` identity. Nova receives the existing port explicitly; ordinary cleanup never deletes it. After predecessor removal, the controller records the port’s worker slot before reuse, preventing cleanup from confusing old and new attachments.

Reservations and lifecycle changes share the app lock. Allocation and worker-creation intent precede provider calls; unknown outcomes never authorize another creation or release. Release requires a proven unbound, owned port; app deletion releases it after worker cleanup. Fixed and floating reservations are mutually exclusive. Retained-port deploy, resize, and rollback require maintenance because a port attaches to only one worker.

The admin image ships `nix/pkgs/openstacksdk-security-group-project-alias.patch`: security-group `project_id` falls back to Neutron’s legacy `tenant_id`. This preserves ownership evidence without relaxing validation.

## Builders

The helper creates a single-use builder for one exact source snapshot. Admin drives the builder over restricted SSH using a dedicated unprivileged identity. The builder blocks cloud metadata, runs rootless BuildKit as `agentops`, and receives registry access for the build. Cleanup verifies deletion of both the builder server and its port before worker deployment.

Implementation: [`builder_lifecycle.sh`](../../infra/openstack/builder_lifecycle.sh), [`builder_execute.py`](../../infra/openstack/builder_execute.py), [`builder.nix`](../../nix/roles/builder.nix), and [`builder_settings.py`](../../openstack_platform/controller/builder_settings.py).

### Builder sizes

The nullable `applications.builder_flavor` and singleton `builder_settings` table store builder selections. An app override takes precedence over the platform default. A null override uses the current default; an unset platform default uses inventory `flavors.builder`. Admins change the default; staff and admins set or reset an app's override. Worker upserts preserve app builder overrides.

Builder choices require at least 1 vCPU and 1024 MiB RAM. Default changes serialize through the infrastructure lock and journal `infra.builder-size.set`; app changes use the app lock and journal `app.builder-size.set`. Both compare the expected current selection and record the observed flavor name before writing. The broker uses its durable intent journal and audit log; staff cannot manage or resume default changes.

Immediately before starting a build, the controller records the app's effective builder flavor under the infrastructure lock, alongside its pinned builder image. Recovery keeps that flavor. The `app.build` helper request requires `builderFlavor` next to `builderImageId`; the helper validates it with `flavor_reference` and uses that recorded flavor. Builder creation still checks that the created server has the selected image and flavor. Changing a builder size affects later builds, needs no plan or maintenance, and leaves workers alone.

### Runtime versions

The build resolves the requested Node.js or Bun version to an official `-slim` image pinned by SHA-256 digest. [`runtime_versions.py`](../../openstack_platform/runtime_versions.py) reads:

- **Node.js:** `engines.node` in `package.json`, then a root `.nvmrc` or `.node-version`.
- **Bun:** `"packageManager": "bun@x.y.z"`, then `engines.bun`, then `.bun-version`. A `packageManager` that names another manager is ignored.

Ranges use a subset of npm semver: exact and partial versions, x-ranges, `^`, `~`, comparators, hyphen ranges, a space for AND, and `||` for OR. A request that matches nothing, or only lines older than the built-in floors (Node.js 20, Bun 1.1), is refused before any lookup.

`app.build` resolves versions through [`runtime_images.py`](../../openstack_platform/helper/runtime_images.py). Node.js releases come from `https://nodejs.org/dist/index.json`, Bun from Docker Hub `oven/bun` tags. It selects the newest matching release above the floor, preferring Node.js LTS when allowed (`>=20` selects LTS over Current). It resolves the `-slim` tag to a digest, trying the next two releases if unpublished. HTTPS lookups follow no redirects and use a 10-second request timeout within a 60-second total budget. Docker Hub uses an anonymous, short-lived pull token; no deployment credentials are sent. Without a version request, the policy’s `runtimeImages` pin remains unchanged.

The helper returns `runtime: {runtime, version, image, source}`. For `default`, the controller requires the policy pin; otherwise it validates the official digest-pinned image against the request, regenerates the recipe, and checks its hash. The operation retains the choice; deployment reads expose `runtime`, and rollback/resize copy it. Rejected requests yield `BUILD_REJECTED` with a build-log reason; lookup failures yield `RUNTIME_UNAVAILABLE` before builder creation. The controller unit permits IPv4/IPv6, and admin default egress allows `nodejs.org`, `auth.docker.io`, and `registry-1.docker.io`.

### Log timestamps

Generated images use [`log_timestamps.cjs`](../../openstack_platform/controller/log_timestamps.cjs) as their entrypoint, executed by the image's own Node.js or Bun runtime. The generated Dockerfile embeds the wrapper in a root-owned, mode-`0444` file at `/platform-log-timestamps.cjs`; its `CMD` still invokes `npm run <startScript>` or `bun run <startScript>`. The command inherits the image’s environment, `/app` working directory, and user `65532:65532`.

The wrapper timestamps each stdout and stderr record independently with RFC 3339 UTC milliseconds (`2026-10-07T18:36:05.123Z `), using the time the segment's first bytes arrive. Pending partial lines flush after 100 ms of idle time or stream closure. Lines longer than 8 KiB split into timestamped records, preserving UTF-8 boundaries when possible. Bytes pass through without decoding, including invalid UTF-8; API readers replace invalid bytes for display. Stream backpressure bounds queued output.

SIGTERM and SIGINT go to the package command’s process group, including its shell and app. The wrapper drains output and preserves the command's exit code or terminating signal; Nomad's kill timeout remains unchanged.

The admin helper records build lines through [`TimestampedBuildLog`](../../openstack_platform/log_timestamps.py) as builder output arrives over SSH. Platform build notes use the same UTC prefix. Partial build lines are visible when flushed, long lines split at 8 KiB, and the existing file byte limit includes timestamp prefixes. These times describe reception on the admin, rather than execution on the builder. Process output collection reads available chunks instead of waiting for a full buffer.

The wrapper's SHA-256 digest is part of recipe identity, alongside generator version 3 and the selected runtime image pin. Controller regeneration therefore checks the exact wrapper used by the helper. The wrapper is part of each newly generated image. Restart and retained rollback use the entrypoint already in that image. Portal runtime, build, and startup viewers parse valid leading UTC millisecond timestamps into a muted local-time column with UTC tooltips; legacy lines remain readable. Hiding the column does not remove timestamps from copy or download exports.

## Ingress

Traefik routes public hostnames to the portal, app workers, and Garage. The HTTPS provider terminates browser TLS and preserves `Host`.

| Mode | Origin listener | Allowed source |
| --- | --- | --- |
| Tunnel | Loopback; no public origin port | Local tunnel process |
| Direct | Origin listener | The same configured provider IPv4 ranges in Neutron, the host firewall, and trusted forwarded-header settings |

| Public route | Destination |
| --- | --- |
| `<domain>` | Admin `management-web` on port 8080 |
| `<app>.<domain>` | The app’s healthy Nomad worker service |
| `s3.<domain>` | Garage on storage, over HTTPS verified against the internal CA |
| `/healthz` | Traefik health check |

The S3 proxy preserves `Host` so presigned URLs remain valid. Admin’s firewall and `management-web` each restrict port 8080 to the ingress address. The internal CA authenticates platform services. Private keys and deployment credentials arrive after image construction and stay out of source and the Nix store.

Implementation: [`ingress.nix`](../../nix/roles/ingress.nix), [`admin.nix`](../../nix/roles/admin.nix), and [`nomad_jobs.py`](../../openstack_platform/controller/nomad_jobs.py).

## Storage instances

Every new PostgreSQL or MongoDB resource runs its own database server. Each server is a Podman container under `<namespace>-database@<id>.service` on storage, with its own data directory, TCP port, cgroup limits, XFS project quota, and worker allowlist. Database images are pinned by inventory.

All live app databases were migrated to instances as of 2026-10-10. The shared PostgreSQL and MongoDB servers still hold sealed fallback copies; their removal is separate work. The [migration runbook](../guides/migrate-storage-instances.md) covers that one-time migration.

Implementation: [`instance_contract.py`](../../openstack_platform/instance_contract.py), [`storage_instances.py`](../../openstack_platform/storage_instances.py), [`storage_capacity.py`](../../openstack_platform/controller/storage_capacity.py), and [`storage.nix`](../../nix/roles/storage.nix).

### Storage isolation and recovery

The helper reaches a loopback instance manager through the storage host’s TLS administration listener, using the Garage admin bearer. Instance credentials and configuration live on the managed-data volume. Failed creation or migration keeps the instance identity and reservations. Only explicit resource deletion removes its data. A migration abort retains the target; later resource deletion removes both the target and shared source.

Systemd controls Podman and conmon with Podman cgroups disabled. Each instance has `MemoryMax`, `MemorySwapMax=0`, and `CPUQuota`. Its container root filesystem is read-only; writable tmpfs mounts count toward memory. A crash or out-of-memory kill restarts that instance. PostgreSQL stops with SIGINT; MongoDB uses SIGTERM. Both use mixed kill mode and a 180-second grace period.

CPU and disk edits apply live. Memory or connection changes restart the database instance. Shared resources can change prospective CPU and memory caps before migration. Raising shared connection caps requires migration; any shared MongoDB connection change requires migration.

### Database limits

| Setting | Default or enforced value |
| --- | --- |
| Instance resources | 512 MiB memory, 500 millicores CPU, 10 app connections; example policy size 2 GiB |
| Accepted memory | 512 MiB–8 GiB, whole MiB |
| Accepted CPU | 100–4000 millicores |
| Accepted app connections | 1–100 |
| Accepted size | 1–500 GiB |
| Scheduling | `CPUWeight=100`, `IOWeight=100`, `TasksMax=256` |
| PostgreSQL connections | `max_connections=connections+5`; five superuser slots |
| PostgreSQL memory | `shared_buffers=memory/4`, `work_mem=2MB`, maintenance memory/16 capped at 64 MiB; one autovacuum worker |
| PostgreSQL WAL | `max_wal_size=256MB`, `min_wal_size=80MB`; targets, not hard WAL caps |
| PostgreSQL login settings | Statement timeout 30 s, idle transaction timeout 60 s, lock timeout 5 s, temp file limit 256 MB |
| MongoDB connections | `maxConns=connections+10`; URI `maxPoolSize=connections`, `maxIdleTimeMS=60000` |
| MongoDB cache | Memory/4 with 256 MiB minimum; `slowms=100` |
| MongoDB restore | Temporary minimum 1 GiB memory, charged against a separate 2 GiB maintenance pool; one collection and insertion worker |
| Writable tmpfs | 64 MiB `/tmp` and a 16 MiB PostgreSQL socket tmpfs |
| XFS hard byte quota | 150% of the size limit, rounded up to MiB |
| XFS inode quota | `max(4096, sizeBytes/32768)` with integer division |
| Logs | Per-instance 200 messages/30 s; host journal 1 GiB persistent and 256 MiB runtime |

MongoDB’s standalone image cannot set the `defaultMaxTimeMS` cluster parameter. Instance CPU and memory caps contain query resource use.

When MongoDB logical data plus indexes exceed `sizeBytes`, the collector assigns a restricted role that blocks inserts, updates, and collection creation. Reads, deletes, drops, lists, and statistics remain available. Writes resume at or below 95% of the limit. Role replacement is atomic; credential rotation preserves the effective role. Logical usage and physical XFS allocation differ. Hitting the hard disk quota can require an admin size increase before database access recovers.

PostgreSQL requires TLS and SCRAM for remote connections. MongoDB requires TLS. Connection strings name the certificate’s DNS host; inventory-derived host mappings resolve it on hosts and in Nomad Docker jobs.

### Worker access

An nftables input chain restricts each allocated database port to verified app worker IPs plus admin, storage, and loopback. Revocation also affects established connections. Ports 30000–30999 are reserved from client ephemeral allocation.

Allowlists are durable. Unchanged worker observations avoid repeated provider queries. Apps without database instances do not depend on instance-manager network administration.

### Host admission and shared-service caps

Resource rows are authoritative. Admission includes creating resources and unfinished limit changes so retries cannot oversubscribe the host.

| Budget or service | Current bound |
| --- | --- |
| Normal instance memory | Storage reports its budget; transition default 36 GiB |
| Maintenance memory | Additional 2 GiB |
| Database slice | 12 CPU cores, 38 GiB, no swap |
| App connection reservations | 2000 across PostgreSQL and MongoDB |
| Shared PostgreSQL source | 8 GiB, four cores, 100 connections, 2 GiB buffers |
| Shared MongoDB source | 8 GiB, four cores, 800 connections, 2 GiB cache |
| Garage | 4 GiB, two cores, no swap |
| Registry | 2 GiB, one core, no swap |
| Instance manager | 512 MiB, one core, 128 tasks, no swap |

The Python contract permits a normal memory budget up to 50 GiB. The deployed Nix role still sets the transition budget and 38 GiB database slice; removing shared sources does not raise these limits automatically.

Disk admission reserves each database’s hard quota and each bucket’s Garage allowance. Fixed disk allowances are 100 GiB for the registry, 32 GiB for each shared source, 16 GiB for migration archives, 16 GiB for Garage overhead, and 1 GiB for metadata. An increase is refused when reservations exceed 85% of the data volume. If no host sample exists, controller admission uses a 500 GiB volume estimate. The manager also checks physical allocation before reductions.

With fifty apps each reserving two 2 GiB databases and a 5 GiB bucket, logical quotas total 450 GiB. Physical reservations total 809.5 GiB including the fixed allowances. A 1 TiB volume has an 870.4 GiB admission boundary at 85%; a 500 GiB volume cannot admit those defaults. The registry’s 100 GiB allowance bounds growth; it does not guarantee space for arbitrary image histories.

### Usage and alarms

Usage API reads use SQLite samples. Up to sixteen concurrent probes collect every five minutes, or every minute when usage reaches 80% of the size limit, writes are blocked, or the last collection failed. Collector errors retain the last sample and expose a fixed admin-only `usageError`. Collection does not reserve an app mutation scope.

[`storage_health.py`](../../infra/lib/storage_health.py) raises alarms at connections ≥80%, data-volume use ≥85%, available memory ≤10%, or container memory use ≥90%. Unavailable instances and any MongoDB write block also raise alarms.

### Migration gates

Before each resource cutover, the controller requires a verified source backup no older than `migrationBackupMaxAgeMinutes` (default 60). The helper starts `<namespace>-resource-backup@<type>-<db>.service` if the checkpoint is missing or stale. The per-app migration budget is `migrationAppSeconds` (default 1800 seconds).

Migration freezes source logins, copies into the target, checks counts and content fingerprints, and seals the target before publishing its endpoint. PostgreSQL extension scripts import separately as admin. App SQL imports under the real app login and owner role, with extension comments suppressed. Interrupted native tools and verification support cancellation; replay terminates stale target sessions. Source data stays available for recovery.

See the [migration runbook](../guides/migrate-storage-instances.md) for selection, replay, abort, capacity prerequisites, and release order.

## S3 and Garage

One shared Garage service runs on storage. Each S3 resource owns a bucket and an access key limited to that bucket. Byte and object quotas apply per bucket. New keys, quotas, grants, and deletion are verified against the recorded bucket identity. S3 creation reads `GARAGE_BACKUP_ACCESS_KEY` from `<paths.root>/secrets/garage-backup-key-id.env` on admin and grants read-only backup access to that exact key ID, after verifying it exists. The backup secret in `garage-backup.env` stays private to the operator; `init_garage_backup_key.py` publishes the ID alone, mode 0640, so the controller can read it through its operator group. Key display names can repeat. A missing file, access-key field, or provider key fails with `BACKUP_KEY_MISSING` before bucket or app-key creation.

Failed S3 creation verifies the exact candidate bucket, alias, app key, endpoint, and empty contents before cleanup. The production Garage client preserves HTTP status so a deleted bucket's 404 proves absence and cleanup proceeds to the app key. Key ownership is rechecked immediately before deletion. Unconfirmed cleanup reports which bucket or key still needs verification and remains recoverable.

| Capacity or fairness limit | Value |
| --- | --- |
| Object reservations | Five million across managed buckets |
| Bucket disk reservation | `max(125% of byte quota, 4096 bytes × object quota)`, rounded up to MiB |
| Garage overhead | Additional 16 GiB; its disk project never shrinks while blocks may await collection |
| Per-bucket requests | 200 requests/s, burst 1000, 200 connections |
| Internal peer requests | Additional 100 requests/s, burst 200, 50 connections |
| Public peers | Share bucket limits without the additional internal peer limit |
| Admin backup traffic | Exempt from these fairness limits |

Public S3 uses `s3.<domain>`. Apps receive both internal and public endpoint outputs through the storage binding contract. The ingress proxy preserves the signed host and verifies storage TLS.

The private OCI registry shares the storage data volume. Its images are rebuildable from Git and are excluded from managed-data backups. On 2026-10-10 it used approximately 34 GB of the volume’s 45 GB used space; that is a live observation, not a capacity setting.

Implementation: [`helper/storage.py`](../../openstack_platform/helper/storage.py), [`storage_contract.py`](../../openstack_platform/controller/storage_contract.py), and [`storage.nix`](../../nix/roles/storage.nix).

## Backup and recovery model

Each state class has an independently encrypted backup set. Controller, portal, and operator-state decryption identities stay off the admin host; the managed-data identity also lives on admin.

| Backup set | Contents | Written by | Location | Schedule (UTC; random delay) |
| --- | --- | --- | --- | --- |
| Hosted controller | Controller SQLite and a paired deploy-key archive | `<namespace>-hosted-controller-backup` (as `platform-controller`) | `<paths.backups>/hosted-controller` | Daily, 02:15; up to 30 min |
| Portal | Broker SQLite | `<namespace>-management-broker-backup` (as `management-broker`) | `<paths.backups>/management-broker` | Daily, 02:30; up to 15 min |
| Operator state | Operator CLI SQLite | `openstack-platform backup` (operator user timer) | `<paths.backups>/controller` | Daily, 02:45; up to 30 min |
| Managed data | PostgreSQL, MongoDB, and Garage catalog and objects | `<namespace>-platform-backup` (as the operator) | `<paths.backups>/<namespace>/<timestamp>` | Daily, 03:15; up to 30 min |
| Off-site export | The newest committed set of each class | `<namespace>-offsite-export` | An operator-mounted off-site filesystem | Daily, 05:00; up to 30 min |

The off-site destination must be mounted again after every admin replacement. As of 2026-10-10 no production off-site export is configured, so the `offsite_recovery` health check fails there. An enabled timer alone does not establish an export destination.

**Key custody.**

| Identity | Encrypts | Where the private half lives |
| --- | --- | --- |
| Backup age identity from setup | Hosted controller and operator-state sets (the policy's `backupAgeRecipient`), and the portal set | Off-platform: setup writes it to `/srv/openstack-platform/.secrets/setup/backup-age-identity.txt`; escrow it and never copy it to admin. Admin holds only the public recipient, in the policy and, for the portal set, in `<adminState>/management-broker-releases/config/backup-recipient.txt`. |
| Managed-data identity | Managed-data sets | On admin at `<paths.root>/persistent/secrets/backup-age-key.txt`, plus an operator escrow copy |

Restores take the private identity explicitly: for example, `openstack-platform restore BACKUP --age-identity IDENTITY --yes` for operator state. Hosted controller restore decrypts on the operator side, stages plaintext on admin, and runs `openstack-platform-hosted-controller-restore` as root from the recovery console.

Each set commits by writing its manifest after ciphertext and checksums. Off-site export copies only committed sets, verifies copies, appends a manifest, and updates a credential-free receipt. Its destination must be a separate mounted filesystem; the operator manages provider retention.

Hosted-controller and portal backups prune after commit ([`backup_retention.py`](../../openstack_platform/backup_retention.py)). Sets named within 14 days and the newest three complete sets stay. Pruning removes manifests first, leaving uncommitted debris if interrupted. Pruning failure fails the unit but preserves the new backup.

Garage backup inventories buckets through the admin API, grants `platform-backup` read access, and fails on unreadable buckets. Garage archive format 3 includes original bucket IDs, quotas, app keys, and grants. Restore recreates buckets, imports app keys, restores objects/grants, and remaps IDs into an offline replacement controller database. Temporary write grants are revoked even on failure. Object records and a completion digest are streamed to bound memory.

Managed-data restore checks use disposable PostgreSQL/MongoDB containers and validate the Garage archive before writing `RESTORE-MANIFEST`. Offline SQLite restore checks deployment identity, schema, integrity, foreign keys, sidecars, and unfinished operations before atomic replacement, without network calls.

Managed-data format 4 contains native per-database dumps for shared and isolated endpoints, role settings/users, and encrypted managed-storage bindings. Its plaintext coverage catalogs contain no secrets. A failed payload aborts the set. Files and metadata are synced before the completed directory is published. Sets are age-encrypted; the live managed-data sets totaled about 20 MB on 2026-10-10 (about 16 MB S3 and 4 MB MongoDB).

Managed-data pruning runs only after a successful set. It removes sets older than `RETENTION_DAYS` (default 14, by filesystem age) but never the newest `RETENTION_MINIMUM_KEPT` (default 3), matching controller and portal retention. Operator-state upload retention uses its configured count. See [`run_platform_backup.sh`](../../infra/backup/run_platform_backup.sh), [`backup_retention.py`](../../openstack_platform/backup_retention.py), and [`helper/main.py`](../../openstack_platform/helper/main.py).

Restore creates missing isolated instances, restores managed bindings and an offline controller’s endpoints and limits, then reconciles current worker allowlists before opening access. Tracked shared-resource backups also restore into isolated instances. Retained source payloads stay in the archive instead of replacing current instance data. The full-loss drill requires both database engines; CI exercises fresh-instance restore.

App images and user-supplied Nomad environment values are excluded. After full loss, restore those values from escrow and rebuild accepted GitHub commits. See [Backups and recovery](../guides/backups-and-recovery.md).

## Images and CI

The inventory names five VM roles: admin, ingress, storage, worker, and builder. Operator state selects images for persistent-host replacement; controller state selects the worker and builder images used by app operations. A build records its selected image and flavor so recovery uses the same inputs.

Production CI on `main` retains exact QCOW2 bytes, Nix closure evidence, and source-bound release/artifact manifests. Publication verifies every role and the retained CI receipt before any upload. It then uploads all five roles concurrently, without rebuilding. Failure waits for all uploads and reports the failed roles. Development publication uses its separate serialized matrix.

The publisher checks image UUID, owner, active status, metadata, MD5 checksum, and exact byte size. When Glance supplies a SHA-256 or SHA-512 multihash, that must also match. Without a provider multihash, it reports `content_verification=provider-md5-size`. It never downloads the uploaded image to verify content. This cloud used that MD5-and-size path on 2026-10-10; the observed publish job took about 8.6 minutes.

Verification uses the configured release trust policy. CI supports an explicitly acknowledged production-signing opt-out when its signing gate is disabled; integrity and source-binding checks still run. Do not infer signature verification solely from a successful upload.

A new image does not replace a running VM. Persistent replacement moves the fixed port and attached volumes to a candidate and checks identity and readiness before deleting the old server. Worker and builder selections affect later provisioning.

For rollout order and commands, use [Releases and upgrades](../guides/releases-and-upgrades.md). Implementation: [`ci.yml`](../../.github/workflows/ci.yml), [`image_pipeline.py`](../../openstack_platform/image_pipeline.py), [`publish_nixos_image.sh`](../../infra/openstack/publish_nixos_image.sh), and [`image_service.py`](../../openstack_platform/controller/image_service.py).

## Operator host

`openstack-platform` owns setup, infrastructure status, image selection and pruning, persistent-host lifecycle, operator-state backup, and offline restore. It has no app, deployment, environment, or managed-storage mutation commands. The dedicated `openstack-platform-storage-repair` command on the admin VM reapplies PostgreSQL login settings through the privileged controller socket. The operator CLI’s SQLite state is separate from the hosted controller.

The generated `platform-admin` alias in `/srv/openstack-platform/.secrets/ssh/config` pins the admin address, `agentops`, the identity (`IdentitiesOnly yes`), and an ed25519 host key in a private known-hosts file with strict checking. Agent and port forwarding are disabled. Provider reads use the persistent session and peer-checked Unix socket described in [OpenStack provider reads](openstack-provider.md). The protected `platform-openstack` launcher sends shell reads to that socket; mutations retain the protected credential wrapper. Releases carry no cloud credentials.

`openstack-platform status` combines accepted state with live observations and reports available, unavailable, and unhealthy counts. Its app health comes from the hosted controller’s Nomad and public-route checks.

The operator dashboard (`openstack-platform dashboard`) runs as the operator, serves a mode-`0600` Unix socket (default `STATE_DIRECTORY/run/dashboard.sock`), and admits only that UID and a loopback `Host`. It reads the privileged controller API separately from the portal. Refreshes run every 30 to 900 seconds (default 60) and gather:

- One SSH session calling the controller's privileged socket (`/v1/admin/capabilities`, `/v1/admin/images`, `/v1/admin/applications`, `/v1/admin/deployments` for the two newest 100-item pages, `/v1/admin/storage`, `/v1/admin/status`, and `/v1/admin/operations` for the three newest pages), reading the platform-health timer's snapshot, and checking systemd unit status.
- The `infra list` projection from operator state, with a bounded provider lookup.
- Credential-free HTTPS probes of public ingress and of each enabled app's health path, verifying each deployment's `X-Platform-Deployment` marker (or app ID).

The reader includes `/v1/admin/status`, which captures database inputs in a private snapshot and runs live observations after closing it, outside the API lock. It does not call `/v1/admin/hosts`. Failed sources keep their last good records with age labels. It serves static assets and `GET /api/snapshot`; `POST /api/refresh` wakes a refresh at most every 10 seconds and requires a same-origin custom header. It changes no platform state.

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

## Platform health alerts

The admin health timer runs `infra/monitor/check_platform.py` every five minutes. After writing its snapshot, the checker calls `infra/lib/health_alerts.py`. With `healthAlerts.enabled` false or absent, that call performs no secret reads, state writes, or network requests. With alerts enabled, it locks the persistent operator status file, updates the consecutive-failure count, and records any notification attempt before network I/O. The nonblocking lock prevents overlapping runs from waiting on a sender. State changes use an atomic replacement of a mode-`0600` file; attempts survive reboot on the admin state volume.

The webhook URL is a protected runtime input under the operator secrets directory. Token-bearing URLs stay out of inventory and Nix derivations. Operators copy the file through the existing pinned SSH alias. The health service runs as the operator and reads only a direct, owner-owned mode-`0600` URL file; the persistent parent directory is mode `0700`.

`infra/monitor/send_health_alert.py` receives the URL and payload through stdin in a child process. The parent discards child output and kills the child after ten seconds. The child uses verified HTTPS with an eight-second socket timeout, no redirects, and no environment proxies; it neither reads nor logs response bodies.

Delivery and state errors are contained by the notification call, preserving the checker's original health exit status. No exactly-once delivery guarantee is possible when the receiver accepts a request but the reply is lost. The persisted attempt suppresses immediate duplicate failure alerts; recovery retries can duplicate a message after such an uncertain result.

The health unit calls OpenStack and public HTTPS health endpoints and has no `IPAddressDeny` or address-family sandbox. The foundation preserves Neutron's default egress rules, and the common NixOS firewall restricts inbound traffic. Additional operator/provider egress restrictions must permit the configured webhook destination. Both health and the test unit depend on the admin state mount so persistent files cannot silently be written under an unmounted volume. The test unit adds read-only filesystem protection and runs as the same unprivileged operator.

The checker stops after its first failed check. Alerts therefore describe the first missing check from the ordered check list and do not send exception details. It does not query app-operation states. A process killed before notification or a timer that stops running produces no webhook alert; independent host monitoring is required for those failures.

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
