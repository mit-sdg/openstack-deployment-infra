# Controller API

The controller on the admin host speaks a small JSON HTTP API over two local Unix sockets. This page describes that transport and lists every route. It is for contributors and for operators who call the controller directly, for example to [deploy apps from the command line](../guides/deploy-apps-from-the-command-line.md).

Most people never call this API. The owner portal calls it on behalf of app owners, and the `openstack-platform` tool and [operator dashboard](../guides/run-the-platform.md#open-the-operator-dashboard) read from it. The routes are defined in [`openstack_platform/controller/api.py`](../../openstack_platform/controller/api.py), and the transport in [`openstack_platform/controller/http.py`](../../openstack_platform/controller/http.py).

## Sockets and peers

The controller has no network listener. It serves two Unix sockets, and each one exposes a different set of routes:

| Socket | File owner and mode | Admitted peer (exact UID:GID) | Routes |
| --- | --- | --- | --- |
| `/run/<namespace>-controller/project.sock` | `platform-controller:controller-api`, `0660` | `management-broker` (996:983) | Every route that is not privileged |
| `/run/<namespace>-controller/privileged.sock` | `platform-controller:platform-admin`, `0660` | The operator account `agentops` (1000:1000) | Every `/v1/admin/...` route, plus `POST /v1/applications/{id}/delete` |

`<namespace>` is the deployment namespace from the inventory. The fixed account names and IDs come from [`infra/lib/platform_contract.json`](../../infra/lib/platform_contract.json).

Group membership only lets a process open the socket file. The server then reads the connecting process's credentials with Linux `SO_PEERCRED` before it parses any HTTP, and admits the connection only if the UID:GID pair is on that socket's allowlist. Nothing in a request can choose or upgrade a socket's capability: a route that is not in a socket's set answers `404 NOT_FOUND`. The operator account belongs to both groups, but it is admitted only on the privileged socket; setup checks that the operator is refused on the project socket.

This check authenticates the local host process, not a browser user. Ownership, quotas, sessions, and CSRF protection belong to the owner portal's broker, which is the only project peer.

### Call the privileged socket

The operator reaches the privileged socket through the generated `platform-admin` SSH alias and `curl` on the admin host. Set the namespace first:

```bash
NAMESPACE=<NAMESPACE>   # the namespace from your inventory
ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- \
  curl --fail-with-body --silent --show-error \
  --unix-socket "/run/$NAMESPACE-controller/privileged.sock" \
  http://localhost/v1/admin/capabilities
```

You should see `{"apiVersion":1,"features":["maintenance-after-build-v1","reuse-worker-v1"]}`. The host part of the URL is ignored; `localhost` is conventional.

Calling the project socket requires running as `management-broker` on the admin host. [Deploy apps from the command line](../guides/deploy-apps-from-the-command-line.md#set-up-your-shell) shows the approved way to do that.

## HTTP framing

| Rule | Value |
| --- | --- |
| Protocol | HTTP/1.1, JSON request and response bodies |
| Path | Must start with `/v1/`; anything else is `404 NOT_FOUND` |
| Request body | `Content-Type: application/json` with `Content-Length`; at most 1 MiB; chunked bodies are refused |
| JSON | Strict: duplicate keys, `NaN`, and `Infinity` are refused; a route refuses any body field it does not define |
| Repeated headers | A repeated `Content-Length` or `Idempotency-Key` is refused |
| Query | At most 32 fields; a route refuses query fields it does not define |
| Response body | JSON, at most 1 MiB |
| Response headers | `Content-Type: application/json`, `Cache-Control: no-store`, `X-Correlation-ID`, and `Location` on `201` and `202` |
| Connections | At most 64 in total and 16 per peer; the installed service lowers this to 8 per peer |
| Deadlines | 5 s to send headers, 30 s to send the body, 5 s to write a response, 15 s idle between requests |
| Requests per connection | At most 100 |

A connection that fails the peer check or the connection limit gets `503` with `Retry-After: 1` and one of the codes `PEER_CREDENTIALS_UNAVAILABLE`, `PEER_IDENTITY_REJECTED`, or `CONNECTION_LIMIT`, and is then closed. After any framing error the server closes the connection instead of reading further, so leftover bytes of a request (which may hold a secret) are never parsed as a new request.

Application, deployment, operation, and storage IDs are canonical lowercase UUIDs. OpenStack flavor IDs are opaque strings (for example `4200`), not UUIDs.

## Idempotency keys

Every route that creates a record or starts an operation requires an `Idempotency-Key` header. The route tables below mark these routes. The key must be a canonical lowercase UUID:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
```

The controller records each key with a fingerprint of the request’s method, path, and parsed JSON body:

- **Same key, same request:** the controller replays the recorded result. You get the same application or the same operation back, without starting a new operation. A `recovery_required` operation resumes as described below.
- **Same key, different request:** `409 IDEMPOTENCY_CONFLICT`.
- **The key becomes the ID.** For `POST /v1/applications` the key is the new application ID. For every route that starts an operation, the key is the operation ID. For a deployment, the deployment ID is the same as its operation ID.

Save each key until its operation finishes. If an operation ends in `recovery_required`, repeat the identical request with the same key to resume it. The dispatch journal does not store request bodies, so a request that carried an environment value must be sent again with that value. A new key does not get around unfinished foreground work: the controller answers `409 OPERATION_CONFLICT` with the blocking `operationId`. Accepted finishing work uses the narrower admission rules below.

## Results and operations

Long-running mutations return an operation URL before external work begins. Poll that URL for completion.

| Status | Meaning |
| --- | --- |
| `200` | A read, a plan, or a synchronous change (storage label, deploy key) |
| `201` | `POST /v1/applications` created or replayed an application record |
| `202` | The controller accepted an operation and will carry it out in the background |

A `202` response looks like this, with the same URL in the `Location` header:

```json
{
  "operationId": "3f0c2a8e-6b1d-4c39-9a57-1d2e4f6a7b80",
  "statusUrl": "/v1/operations/3f0c2a8e-6b1d-4c39-9a57-1d2e4f6a7b80",
  "result": {"kind": "operation", "id": "3f0c2a8e-6b1d-4c39-9a57-1d2e4f6a7b80"}
}
```

Poll the `statusUrl` until `status` is no longer `running`. Operations started on the project socket are polled with `GET /v1/operations/{id}`; operations started on the privileged socket (including cascade deletion) are polled with `GET /v1/admin/operations/{id}`.

| Field | Meaning |
| --- | --- |
| `operationId` | The operation ID (the request's idempotency key) |
| `kind` | For example `app.deploy`, `app.enable`, `app.env.set`, `storage.create`, `infra.image.set` |
| `scope` | `app-<application ID>` or `infrastructure`; one foreground operation and one accepted finishing operation can be unfinished per app |
| `status` | `running`, `succeeded`, `failed`, or `recovery_required` |
| `phase` | The last recorded step; `queued`, `executing`, `startup_interrupted`, or `finishing` before the domain service records its own |
| `startedAt`, `updatedAt`, `deadlineAt` | Timestamps |
| `safeError` | A bounded, secret-free summary, or `null` |
| `errorCode` | `PLATFORM_BUSY` or `RUNTIME_UNAVAILABLE` for failures a caller can act on, otherwise `null` |
| `cleanupState` | Latest recorded cleanup evidence; a migrated checkpoint can still describe builder cleanup. Read it with `status`, `finishing`, and `nextRetryAt` |
| `finishing` | `true` when the operation has durable acceptance and only finishing work remains; retained on completed records |
| `finishingRetryAttempts` | Number of automatic finishing attempts started, from 0 to 5; manual replay does not reset this count |
| `nextRetryAt` | UTC timestamp for the next automatic attempt, or `null` while executing, after success, or after exhaustion |

Four worker threads run accepted operations, and at most 32 can be admitted (queued or running) at once. When that capacity is full the controller answers `503 OPERATION_QUEUE_FULL`, which is safe to retry later with the same key.

After deployment acceptance, address handover, exact predecessor job/worker removal, and registry retention run as finishing work. An enabled app's address handover also uses this path. Failures keep the operation `running` while the controller retries, with waits of 1, 2, 4, 8, and 10 minutes (25 minutes of backoff, plus execution time). Each automatic attempt has at most 120 seconds, bounded further by the configured operation deadline. After five automatic attempts fail, the operation becomes `recovery_required` and `nextRetryAt` is `null`. The journal preserves the schedule and attempt count across restarts; an interrupted automatic attempt consumes its attempt. Replay the original method, path, body, and key to try again manually.

Finishing retries and manual replay claim the same dispatch and take the same app lock as foreground mutations. They reuse the recorded intent; they do not rebuild, resubmit the accepted job, overwrite environment values, or accept the deployment again. Before handover or predecessor cleanup, a running app's exact accepted job and public route are reobserved. A missing or ambiguous observation prevents destructive work. A stopped app stays stopped when cleanup completes.

While finishing is pending, including after automatic retries are exhausted, environment set/delete/import, storage verify/rotate, and restart are admitted. Enable is admitted as a no-op after address handover. Disable requires confirmed handover and predecessor job absence, so stopping the accepted job cannot expose the old route; remaining worker cleanup must target a separate placement. Enable can return an already-running app unchanged; creating a worker for a stopped app waits until finishing completes. Deploy, resize, rollback, app deletion, storage create/remove, and address reservation changes return `409 POST_ACCEPTANCE_CONFLICT` with the blocking operation ID. Accepted foreground work still serializes through the app lock; admission does not authorize concurrent helper mutations.

Most synchronous handlers share one lock. Operation polling, health, application reads, environment-name reads, storage reads, runtime logs, deploy-key reads, and the GitHub source checks skip it and read from their own snapshot instead, so a slow locked request (such as `GET /v1/admin/status`) does not block them. Concurrent `GET /v1/applications/{id}` requests that see the same accepted state share one live probe, and its result is reused for two seconds.

## Errors

Errors carry a code, summary, correlation ID, and retryability flag:

```json
{
  "error": {
    "code": "OPERATION_CONFLICT",
    "summary": "another operation is unfinished for this resource scope",
    "correlationId": "8d1f6b0a-2c4e-4f7a-9b3d-5e6f7a8b9c0d",
    "retryable": false,
    "operationId": "3f0c2a8e-6b1d-4c39-9a57-1d2e4f6a7b80"
  }
}
```

`operationId` appears only when an operation blocks the request. Errors never include provider payloads, stack traces, secret values, or private operation references.

| Code | HTTP | Meaning |
| --- | --- | --- |
| `INVALID_REQUEST`, `INVALID_BODY`, `INVALID_QUERY`, `INVALID_JSON` | 400 | The request does not match the route's strict schema |
| `INVALID_IDEMPOTENCY_KEY` | 400 | The header is missing or not a canonical UUID |
| `NOT_FOUND` | 404 | No such route on this socket |
| `APPLICATION_NOT_FOUND`, `DEPLOYMENT_NOT_FOUND`, `STORAGE_NOT_FOUND`, `OPERATION_NOT_FOUND` | 404 | No such record |
| `METHOD_NOT_ALLOWED` | 405 | The path exists, but not with this method |
| `REQUEST_TIMEOUT` | 408 | The body did not arrive within its deadline |
| `IDEMPOTENCY_CONFLICT` | 409 | The key was already used for a different request |
| `OPERATION_CONFLICT` | 409 | Another foreground operation is unfinished for the same scope |
| `POST_ACCEPTANCE_CONFLICT` | 409 | The requested change can interfere with an accepted operation's pending finishing work |
| `STATE_CONFLICT` | 409 | Current controller state prevents the request |
| `REQUEST_TOO_LARGE` | 413 | The body exceeds 1 MiB |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | The body is not `application/json` |
| `INTERNAL_ERROR`, `RESPONSE_TOO_LARGE`, `INVALID_RESPONSE` | 500 | Unexpected controller failure; quote the correlation ID |
| `EXTERNAL_OPERATION_FAILED` | 502 | A provider, Nomad, or storage call did not complete safely |
| A helper error code | 502 | The helper refused or failed a synchronous action |
| `DEPENDENCY_UNAVAILABLE`, `OPERATION_QUEUE_FULL`, `SOURCE_UNAVAILABLE` | 503 | Retryable: a dependency or capacity is unavailable |
| `DEADLINE_EXCEEDED` | 504 | Retryable: the request ran out of time |

## Lists and logs

List routes accept `limit` (1 to 100, default 50) and `cursor` (the ID of the last item on the previous page). They return:

```json
{"items": [], "nextCursor": null, "truncated": false}
```

Build and runtime log routes return bounded JSON responses, not streams. They accept `lines` (1 to 1000, default 200). The build log also accepts `offset`, a byte offset bounded by the configured build-log size, and returns `nextOffset`. The runtime log accepts `stream=stdout` (the default) or `stream=stderr`.

## Routes

Columns: **Socket** is where the route is installed. **Key** shows whether the route requires an `Idempotency-Key`. `{id}` is a UUID unless stated otherwise.

### Health and capabilities

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `GET /v1/health` | project | no | Readiness check; returns `{"status":"ok","capability":"project"}`. The `<namespace>-controller-readiness` unit calls it. |
| `GET /v1/admin/capabilities` | privileged | no | API version (`1`) and features: `maintenance-after-build-v1`, `reuse-worker-v1` |

### Applications

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `POST /v1/applications` | project | yes | Create an application record from `{"slug": "..."}`; returns `201`. Starts no worker. |
| `GET /v1/applications/{id}` | project | no | Accepted state (`activeDeploymentId`, sizing, URL), a `requiresMaintenance` flag for a retained primary IPv4, and a `live` health observation |
| `POST /v1/applications/{id}/enable` | project | yes | Start the accepted deployment again |
| `POST /v1/applications/{id}/disable` | project | yes | Stop the accepted job and its worker; data and history are kept |
| `POST /v1/applications/{id}/restart` | project | yes | Restart the exact accepted running task in place |
| `POST /v1/applications/{id}/delete` | privileged | yes | Cascade deletion; body `{"confirmation": "<slug>"}` |
| `GET /v1/admin/applications` | privileged | no | Paged list of every application, including deleted ones, with sizing |

### Deployments and logs

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `POST /v1/applications/{id}/deployments` | project | yes | Build and deploy one exact commit (see [deployment request](#deployment-request)); optional `maintenance` and `plan` |
| `POST /v1/admin/applications/{id}/deployments` | privileged | yes | Operator deployment: replacement with a reviewed sizing `plan`, or worker reuse (see [operator deployment modes](#operator-deployment-modes)) |
| `GET /v1/applications/{id}/deployments` | project | no | Paged deployment history for one app |
| `GET /v1/admin/deployments` | privileged | no | Paged list of every deployment attempt, newest first |
| `GET /v1/deployments/{id}` | project | no | One deployment attempt, with its configuration, source repository, image digest, and runtime |
| `GET /v1/deployments/{id}/build-log` | project | no | Retained build output; `lines`, `offset` |
| `GET /v1/deployments/{id}/startup-log` | project | no | Startup record of a candidate that never became healthy; `captured: false` when there is none |
| `GET /v1/applications/{id}/runtime-log` | project | no | Recent output of the running app; `lines`, `stream=stdout` or `stderr` |

### Source and deploy keys

These routes let the portal work with private GitHub repositories through the app's deploy key. The helper keeps the private key; responses carry only the public half and fingerprint.

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `GET /v1/applications/{id}/source-key` | project | no | Read the deploy key: `present`, and when present `publicKey`, `fingerprint`, `createdAt` |
| `POST /v1/applications/{id}/source-key` | project | no | Create the key if absent; `{"replace": true}` replaces it |
| `DELETE /v1/applications/{id}/source-key` | project | no | Remove the key; an app without one gets the same `present: false` answer |
| `POST /v1/applications/{id}/source-key/check` | project | no | Check `{repository, branch}` with the key; returns the branch head or a problem: `key-refused`, `not-found`, `branch-missing`, `unavailable` |
| `POST /v1/applications/{id}/source/commits` | project | no | Up to five recent commits of `{repository, branch}`, read with the key |
| `POST /v1/applications/{id}/source/check` | project | no | Check `{repository, commit, configuration}` against the build's checkout rules (package.json, lockfiles, scripts, runtime version) |

The three `POST` routes that read GitHub answer `{"keyPresent": false}` when the app has no key, and `503 SOURCE_UNAVAILABLE` when GitHub cannot be read in time.

### Environment variables

Values go to the app's Nomad Variable and are never returned. Each change restarts and health-checks a running app.

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `GET /v1/applications/{id}/environment` | project | no | Names, owners, revision, and `updatedAt`; never values |
| `PUT /v1/applications/{id}/environment/{key}` | project | yes | Add or replace one value: `{"value": "..."}`; `{key}` is the variable name |
| `DELETE /v1/applications/{id}/environment/{key}` | project | yes | Remove one variable |
| `POST /v1/applications/{id}/environment/import` | project | yes | Import a strict dotenv document: `{"dotenv": "..."}` |

### Managed storage

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `POST /v1/applications/{id}/storage` | project | yes | Create a resource: `{"type": "postgres" \| "mongo" \| "s3", "name": "..."}`; `name` defaults to `default` |
| `GET /v1/applications/{id}/storage` | project | no | Paged list of the app's resources |
| `GET /v1/storage/{id}` | project | no | One resource: type, name, label, lifecycle state, authoritative quotas, cached usage, write blocking, timestamps |
| `PUT /v1/storage/{id}/limits` | project | yes | Admin-only at the broker; complete `quotas` and `expectedQuotas` objects; async `storage.limits.set` |
| `PATCH /v1/storage/{id}/label` | project | yes | Change the display label: `{"displayLabel": "..."}`; returns `200` |
| `POST /v1/storage/{id}/verify` | project | yes | Verify provider identity and health |
| `POST /v1/storage/{id}/rotate` | project | yes | Issue new scoped credentials, check the app's health with them, then retire the old ones |
| `DELETE /v1/storage/{id}` | project | yes | Delete the resource: `{"confirmation": "<resource name>", "purge": false}`; `purge: true` empties an S3 bucket first; without it a non-empty bucket is refused. The portal allows this only for a portal admin with a fresh step-up. |
| `GET /v1/admin/storage` | privileged | no | Paged list of every resource, including provider identifiers and safe usage errors |
| `POST /v1/admin/storage/migrate-instances` | privileged | yes | Quiesce, copy, verify, publish isolated endpoints and refresh accepted jobs; never delete shared source data |
| `POST /v1/admin/storage/repair-postgres` | privileged | yes | Reapply settings and resource connection limits to every current PostgreSQL login; empty body; async `storage.postgres.repair` |

Resource rows hold authoritative quotas; policy supplies the size defaults at creation.
Postgres and Mongo quota objects contain exactly `sizeBytes`, `connections`,
`memoryBytes`, and `cpuMillicores`. Defaults are 2 GiB size (policy), 10 app connections,
512 MiB memory and 500 millicores. Size bounds are 1–500 GiB; connections 1–100;
memory 512 MiB–8 GiB in whole MiB; CPU 100–4000 millicores. S3 quota objects contain
`s3Bytes` (1 MiB–500 GiB) and `s3Objects` (1–100,000,000).

Limits requests require complete `quotas` and `expectedQuotas`. The latter is the
accepted integer snapshot, including historical values outside today's new-resource
bounds. A stale snapshot fails async validation; re-read and use a new key. Owner/staff
account authorization is enforced by the broker's admin check, as for default builder
size. The limits route uses the project socket; the broker has no privileged access.
Creates/raises can reject with `MEMORY_BUDGET_EXCEEDED`, `CONNECTION_BUDGET_EXCEEDED`
or `DISK_BUDGET_EXCEEDED`. Reservations include unfinished assignments; a lost response
does not release capacity. SQLite writer transactions serialize controller admission;
the host manager independently checks durable instance reservations and disk geometry.

Database resources include `isolation` (`shared` or `instance`) and `hardQuotaBytes`
(null before migration). An instance's XFS hard quota is 125% of size, rounded up to
MiB. Each resource has its own container, cgroup, port, credentials, data directory and
worker allowlist. Legacy shared databases permit soft-size changes and PostgreSQL
connection reductions; other compute/connection edits require migration and return
`409 INSTANCE_MIGRATION_REQUIRED`.

`usage` always contains nullable `usedBytes`, `objectCount`, `currentConnections`,
`instanceMemoryBytes`, `cpuTimeMilliseconds`, and `measuredAt`, plus `stale`.
CPU time is cumulative and resets when the cgroup restarts. Collection runs every
300 seconds; samples older than 900 seconds are stale. Failed collection keeps the
last successful values. Reads use SQLite. PostgreSQL reports database bytes and
backend count; MongoDB reports logical data+index bytes and no per-user connection
count; S3 reports bytes and objects. Instance counters are null for shared resources.

`writeBlock` contains `blocked`, `reason` (`size_limit_exceeded` or null), and `since`
(ISO timestamp or null). MongoDB removes insert/update/create permissions above its
soft limit and retains find/delete/drop/list/stats. It restores readWrite at or below
95% of the limit. A limits change reconciles the role promptly. Rotation preserves the
role and PostgreSQL timeouts. The hard filesystem quota also bounds growth between
collector samples and when physical allocation exceeds logical size.

Admin storage models additionally expose `instanceId`, `instancePort`,
`migrationState`, provider identity and safe usage errors. Owner models omit these.
`GET /v1/admin/status` adds cached `storageHost`: measuredAt/stale, CPU count/load
averages, memory total/available, volume total/used, per-container memory/availability,
Postgres/Mongo connection totals, instance connection/availability projections and
writeBlockedResources. The host metrics channel is authenticated TLS and never runs
provider observations on resource-read requests.

`POST /v1/admin/storage/migrate-instances` accepts an empty body and Idempotency-Key,
returns `202` with admin operation polling, and journals `storage.instances.migrate`.
Run `openstack-platform-storage-migrate` as agentops on the admin VM. Replay its
printed UUID with `--request-id`; `--timeout` defaults to 900 seconds (1–7200).
Per-app child journals retain quiescence, verified copies, endpoint publication and
accepted-job refresh. Old shared data is never deleted by migration. See the
[migration runbook](../guides/migrate-storage-instances.md).

`openstack-platform-storage-repair` re-applies current PostgreSQL login timeouts and
resource-row connection limits through the local privileged socket. It prints a UUID
and waits; `--request-id` resumes, `--timeout` defaults to 300 seconds (1–3600).
The Nix wrappers pin the socket; raw packaged commands require `--socket PATH`.

### Operations

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `GET /v1/operations/{id}` | project | no | Poll an operation started on the project socket |
| `GET /v1/admin/operations/{id}` | privileged | no | Poll an operation started on the privileged socket |
| `GET /v1/admin/operations` | privileged | no | Paged list of every operation, newest first, including queued ones |

### Sizing and rollback

Operator resize and rollback are plan-first. Read a plan, review it, then apply exactly that plan with the app's slug as confirmation. Both reuse a retained image, so neither rebuilds. The portal can also use a worker sizing plan on a new deployment, which builds the requested commit.

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `GET /v1/flavors` | project | no | Available worker sizes as `{"items": [{"flavor_id", "name", "vcpus", "ram_mib", "disk_gib"}]}`; no body or query. Sizes must leave at least 64 MiB after the greater of 512 MiB or 10% OS/service reserve. No provider metadata is exposed. |
| `GET /v1/applications/{id}/resize-plan` | project | no | Same exact fingerprinted plan as the privileged route; requires one `flavor` query field, no body. The broker permits only staff and admins. |
| `GET /v1/admin/applications/{id}/resize-plan` | privileged | no | Sizing plan for one target; requires exactly one `flavor` query field |
| `POST /v1/admin/applications/{id}/resize` | privileged | yes | Apply `{plan, confirmation}`; reuses the accepted image on a new worker of the planned size |
| `GET /v1/admin/applications/{id}/rollback-plan` | privileged | no | Plan to redeploy an earlier successful attempt; requires `deploymentId`, optional `reuseWorker=true` or `false` (default `false`) |
| `POST /v1/admin/applications/{id}/rollback` | privileged | yes | Apply `{plan, confirmation}` through the normal health checks and acceptance |

### Builder sizes

A builder is a temporary machine used only to build an app's image. These settings affect builds that start afterwards, leave workers unchanged, and require no resize plan or maintenance. The broker admits only admins to default-setting routes, and staff or admins to app builder routes. The project socket still authenticates only the broker process.

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `GET /v1/settings/default-builder-size` | project | no | Effective platform default as `{"flavor": <size>}`; initially inventory `flavors.builder`. No body or query. |
| `PUT /v1/settings/default-builder-size` | project | yes | Set the default from `{flavor, expectedFlavor}`; `expectedFlavor` is the current effective name. Validates an existing size with at least 1 vCPU and 1024 MiB RAM. Returns `202`; operation kind `infra.builder-size.set`, scope `infrastructure`. |
| `GET /v1/admin/settings/default-builder-size` | privileged | no | Same default read for operators. |
| `PUT /v1/admin/settings/default-builder-size` | privileged | yes | Same audited default change for operators; poll the privileged operation route. |
| `GET /v1/applications/{id}/builder-size` | project | no | `{flavor, defaultFlavor, useDefault}`: effective app size, effective platform default, and whether the app uses the default. No body or query. |
| `PUT /v1/applications/{id}/builder-size` | project | yes | Set `{flavor, expectedFlavor}`. `flavor` is a name or ID, or `null` to reset to the platform default. `expectedFlavor` is the current override's name, or `null` when using the default. Same 1 vCPU/1024 MiB minimum. Returns `202`; kind `app.builder-size.set`, scope `app-<id>`. |

Each size has `flavor_id`, `name`, `vcpus`, `ram_mib`, and `disk_gib`. Flavor references accept names or opaque IDs. Selections store the observed name so builder creation can check both the selected image and flavor. A changed expected selection fails without changing the setting; read it again and use a new request key. A recorded selection interrupted after writing can be resumed with the original body and key.

### Retained addresses

These routes manage optional, app-owned network addresses. [Deploy apps from the command line](../guides/deploy-apps-from-the-command-line.md) explains when to use them.

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `GET /v1/admin/applications/{id}/fixed-ip` | privileged | no | Retained primary IPv4 reservation, with fresh attachment evidence when reserved |
| `POST /v1/admin/applications/{id}/fixed-ip/plan` | privileged | no | Read-only plan for `{networkId, subnetId, address}`; availability is proven only by reserving |
| `POST /v1/admin/applications/{id}/fixed-ip` | privileged | yes | `{"action": "reserve", networkId, subnetId, address}` or `{"action": "release"}` |
| `GET /v1/admin/applications/{id}/public-ip` | privileged | no | Recorded outbound floating IPv4 reservation |
| `POST /v1/admin/applications/{id}/public-ip/plan` | privileged | no | Read-only quota and routed-network check for `{externalNetworkId}` |
| `POST /v1/admin/applications/{id}/public-ip` | privileged | yes | `allocate` or `attach` (with `externalNetworkId`, and `floatingIpId` for `attach`), `release`, or `reconcile` |

### Platform status and images

| Route | Socket | Key | Purpose |
| --- | --- | --- | --- |
| `GET /v1/admin/status` | privileged | no | Aggregate accepted counts and live observations; `openstack-platform status` reads this. It holds the API lock while it probes, so prefer database reads for polling. |
| `GET /v1/admin/hosts` | privileged | no | Live observations of the persistent hosts; also holds the API lock |
| `GET /v1/admin/images` | privileged | no | The controller's selected image for each of the five roles |
| `POST /v1/admin/images/{role}/selection` | privileged | yes | Compare-and-swap one role's image: `{"imageId": "...", "expectedImageId": "..."}`; `{role}` is `admin`, `ingress`, `storage`, `worker`, or `builder`. Only `worker` and `builder` change what the controller provisions. |

## Request bodies

### Deployment request

`POST /v1/applications/{id}/deployments` requires the first five fields below; `maintenance` and `plan` are optional:

| Field | Meaning |
| --- | --- |
| `repository` | Canonical GitHub HTTPS URL |
| `commit` | Full 40-character commit SHA |
| `requestedRef` | The branch or ref the caller resolved the commit from |
| `configurationRevision` | The caller's revision number for this configuration |
| `configuration` | The build and runtime configuration below |
| `maintenance` | Optional boolean. `true` keeps the old version serving during the build, then stops it before starting the new one (no concurrent processes, but not zero downtime). |
| `plan` | Optional reviewed sizing plan. Without it, the accepted sizing is kept. |

```json
{
  "schemaVersion": 1,
  "build": {
    "runtime": "node",
    "packages": ["."],
    "buildScript": "build",
    "startScript": "start"
  },
  "runtime": {"port": 3000, "healthPath": "/healthz"},
  "storageBindings": [
    {
      "resourceId": "5b7e1c2d-3a4f-4e6b-8c9d-0e1f2a3b4c5d",
      "outputs": {"url": "DATABASE_URL"}
    }
  ]
}
```

`runtime` is `node` or `bun`. `packages` lists 1 to 32 package directories, each with a supported lockfile. `buildScript` may be `null`. Script names are package.json script names, not shell commands. `storageBindings` maps a resource's outputs to environment variable names (at most 32 bindings); it never creates or deletes storage. Reserved platform variable names cannot be targets.

### Operator deployment modes

`POST /v1/admin/applications/{id}/deployments` requires the same five deployment fields, plus one of these modes:

- **Replacement:** include a reviewed sizing `plan`. Add `"maintenance": true` to stop the old version only after the new one is built.
- **Worker reuse:** send `"maintenance": true` and `"reuseWorker": true`, with no `plan`. The controller stops the accepted app and starts the new image on the same worker, keeping its size, port, and IP address.

An app with a retained primary IPv4 (`requiresMaintenance: true`) must be deployed with `"maintenance": true`. A changed mode requires a new idempotency key. A failed cutover after the maintenance stop keeps the accepted pointer but can leave the app stopped.

## Related

- [Deploy apps from the command line](../guides/deploy-apps-from-the-command-line.md)
- [Internals](internals.md#processes-and-trust-boundaries)
- [Operator CLI reference](operator-cli.md)
- [Security](../security.md)
