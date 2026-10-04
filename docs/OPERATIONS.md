# Operate and recover a deployment

Use this guide after automated setup completes. It covers routine health,
backup and restore, off-site recovery evidence, persistent-host replacement,
image pruning, troubleshooting, and teardown boundaries. Greenfield
provisioning belongs in [Deploy the platform](DEPLOYMENT.md); release packaging
belongs in [Release and platform maintenance](MAINTENANCE.md).

Run ordinary commands as the unprivileged owner of
`/srv/openstack-platform`. Use only the generated `platform-admin` SSH alias for
admin-host operations. Root is required only for the explicitly marked offline
hosted-controller restore. Every provider operation is limited to resources
named by the installed inventory. For per-app worker flavor selection and
candidate-based resize through the privileged controller API, see
[Size an application](#size-an-application). For an optional floating IPv4
reservation on a compatible routed network, see
[Reserve a stable outbound IPv4](#reserve-a-stable-outbound-ipv4). This does not enable direct
app ingress or change Cloudflare/TLS.

## Initialize operator variables

```bash
export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
export PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json
export SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config
export PLATFORM_NAMESPACE="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["namespace"])')"
export PLATFORM_ROOT="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["paths"]["root"])')"
export PLATFORM_BACKUPS="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["paths"]["backups"])')"
export PLATFORM_ADMIN_STATE="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["paths"]["adminState"])')"
export PLATFORM_DOMAIN="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["domain"])')"
```

Do not print inventory, credential files, age identities, unrestricted provider
output, or controller operation refs. Safe evidence consists of bounded status,
resource identities already exposed by the CLI, checksums, manifests,
readiness results, and operation/correlation IDs.

## Check routine health

```bash
$PLATFORM_CLI status
$PLATFORM_CLI infra list
ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-controller-readiness.service"
test "$(curl --fail --show-error --silent \
  "https://$PLATFORM_DOMAIN/healthz")" = OK
```

A healthy infrastructure deployment has five accepted image roles, three
available persistent-role observations, active controller/readiness units, and
an exact public `OK` response. `APPS` and `STORAGE` are aggregate controller
counts from the hosted controller API; external shadow application records are
not authoritative. `status` requires the pinned hosted API and reports its
unavailability rather than falling back to shadow state. Local image selection
and unfinished infrastructure operations remain authoritative in the external
operator state. Use `infra list` for foundation inspection during controller
bootstrap/outage; the operator CLI cannot inspect or mutate product records.

An unavailable live observation does not erase accepted state. Diagnose the
named dependency before mutation; do not edit SQLite or provider resources to
make status appear healthy.

## Open the read-only dashboard

The `dashboard` command serves a browser view of the five roles, every hosted
application, recent operations, and the admin platform-health checks. It has no
mutation routes. It runs as the owner of `/srv/openstack-platform`, uses the same
pinned `platform-admin` bridge as `status`, and listens only on a private Unix
socket. You reach it through an SSH session that logs in as that operator.

The React view uses the owner portal's shared visual components while keeping
operator data, its API client and its server separate. Its built assets ship in
the operator CLI release; Node and npm are not needed on the operator host.
Assets are loaded into memory when the dashboard starts. After selecting a new
operator release, restart the dashboard user unit to serve that release's UI.

The socket admits any process running as the operator account, and the page
shows global administrator reads. Do not publish it through a tunnel, reverse
proxy, or shared port: the dashboard has no user authentication of its own.

1. On the management host, start the dashboard as a transient user unit. The
   unit restarts after a failure but does not return after a host reboot; run
   the command again after a reboot. It keeps running after you log out only
   while lingering is enabled for the operator account:
   `loginctl show-user "$USER" --property=Linger` must print `Linger=yes`.

   ```bash
   systemd-run --user --unit=openstack-platform-dashboard --collect \
     --property=Restart=on-failure "$PLATFORM_CLI" dashboard
   systemctl --user is-active openstack-platform-dashboard
   ```

   The default socket is `/srv/openstack-platform/state/run/dashboard.sock`.
   `--interval SECONDS` (30–900, default 60) sets the refresh period. Stop the
   dashboard with `systemctl --user stop openstack-platform-dashboard`.

2. On your workstation, forward a local port to the socket. Replace
   `OPERATOR_HOST` with the SSH destination that logs in as the operator, then
   open `http://localhost:8470`:

   ```bash
   ssh -N -L 127.0.0.1:8470:/srv/openstack-platform/state/run/dashboard.sock OPERATOR_HOST
   ```

   Any free local port works. The dashboard rejects requests whose `Host` is not
   `localhost`, `127.0.0.1`, or `[::1]`.

3. Verify the view. The first refresh can take up to a minute. Afterwards the
   overview reports `Checked` with a time, and the footer lists `Controller`,
   `OpenStack`, `Health timer`, and `Public routes` with recent times and green
   markers. The role and application counts must agree with `$PLATFORM_CLI status`.

Each refresh opens one SSH session to admin. That session sends only `GET`
requests to the database-backed privileged routes (`capabilities`, `images`,
`applications`, the two newest pages of `deployments`, `storage`, and the three
newest pages of `operations`), reads the five-minute platform-health snapshot,
and checks the controller units. If one of those reads fails, the dashboard
keeps that section's last successful records and labels their age. It never calls `/v1/admin/status` or `/v1/admin/hosts`, which
hold the controller's shared API lock while they probe providers and helpers.
Server state comes from the same operator-state and provider reads as
`infra list`. Public routes are probed without credentials or redirects. The
refresh button only wakes the next refresh, at most once every 10 seconds.

Use application search, status filters and sorting to narrow the table, and open
an application row for route evidence, deployment history, sizing, storage and
identifiers. `Needs attention` opens the affected application or highlights its
role; grouped application failures select the attention filter. The theme button
cycles system, light and dark, saving this dashboard's preference independently
of the portal. Relative ages update locally each second. The browser polls the
cached snapshot every 10 seconds, every 2 seconds while collecting or reconnecting,
and every 60 seconds while hidden; conditional ETags avoid reloading unchanged
snapshots. These reads do not change the server's collection interval.

Application statuses mean:

| Status | Evidence |
| --- | --- |
| Serving | The accepted health path returned 2xx with that deployment's `X-Platform-Deployment` marker (jobs rendered without an explicit marker use the application ID) |
| Unverified | A 2xx response lacked the marker or named another deployment |
| Failing | The health path returned another status (including a redirect) or no response |
| Deploying, Starting, Stopping, Updating | An operation is running for the application; route failures are expected during cutover |
| Needs recovery | An operation, deployment attempt, or storage resource is `recovery_required`; resume the operation with its original request and key |
| Not deployed | The application is enabled but has no accepted deployment |
| Unknown | The route could not be checked: the accepted deployment is older than the 200 newest attempts, or no health path is recorded |
| Stopped | The application is disabled, so its route is not probed |

A role is `Unverified` when some of its evidence is missing, for example when
the platform-health timer skipped later checks after an earlier failure. A
value shown as `Last seen …` comes from the last successful read because the
current read failed; the failed source also appears under `Needs attention`.
The health snapshot is stale after 15 minutes. The bars under each health check
show up to 24 checks made by this dashboard process since it started.

If the page does not show what you expect:

- **`Controller API: Unreachable`:** run `$PLATFORM_CLI status`. If it reports
  the hosted controller as unavailable, repair the admin bridge or controller
  before relying on application data; the dashboard keeps the last controller
  records and labels their age.
- **`Platform health — Snapshot is stale` or `Snapshot unavailable`:** run
  `ssh -F "$SSH_CONFIG" platform-admin -- systemctl status "$PLATFORM_NAMESPACE-platform-health.timer"`
  and repair the timer or its last failed run.
- **`OpenStack — Server state unavailable`:** run `$PLATFORM_CLI infra list`.
  `unknown` live states there point to the protected provider wrapper or the
  OpenStack API.
- **`Lost contact with the dashboard service`:** the SSH forward or the
  dashboard process stopped. Check `systemctl --user status
  openstack-platform-dashboard`, then reconnect the forward.

## Roll back an application

Use the privileged controller Unix API to redeploy a retained successful
application artifact and its immutable configuration without rebuilding. Install
matching controller/helper releases with `app.manifest.verify`. The app must be
enabled, and quota must permit both predecessor and candidate workers. Current
sizing, staff secrets, storage credentials and data remain current. **This does
not reverse database migrations or restore data.** Confirm that the historical
application can use the current data before applying.

Run locally on admin as the operator UID/GID admitted to `privileged.sock`.
Use deployment history/read responses to select a historical successful attempt
and inspect its `configuration`, `configurationSha256`, `sourceRepository` and
`imageDigest`. `activeDeploymentId` is authoritative; the latest attempt may have
failed. A missing source snapshot, deleted artifact, or incompatible current
storage dependency prevents rollback. Registry retention can make older
successful attempts unavailable; the controller does not reconstruct them.

```bash
umask 077
NAMESPACE=your-installed-namespace
APP_ID=your-canonical-application-uuid
TARGET_ID=your-historical-deployment-uuid
APP_SLUG=your-exact-application-slug
SOCKET="/run/${NAMESPACE}-controller/privileged.sock"
BASE="http://localhost/v1/admin/applications/${APP_ID}"

curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  "$BASE/rollback-plan?deploymentId=$TARGET_ID" > rollback-plan.json
jq . rollback-plan.json
```

Review both deployment IDs, the historical source/artifact/configuration hash,
current environment revision, sizing and storage identities. The plan is not an
artifact or capacity reservation. A changed accepted deployment, environment
revision, sizing or storage projection requires a fresh review. Apply also
rechecks the actual registry content and current storage output keys.

```bash
KEY="$(python3 -c 'import uuid; print(uuid.uuid4())')"
jq --arg confirmation "$APP_SLUG" \
  '{plan: ., confirmation: $confirmation}' rollback-plan.json > rollback-request.json
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $KEY" \
  --data-binary @rollback-request.json "$BASE/rollback"
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  "http://localhost/v1/admin/operations/$KEY"
```

Poll until terminal or `recovery_required`. Verify `succeeded`, the new accepted
`activeDeploymentId` equal to `$KEY`, and deployment history showing the target
artifact/configuration hash. The historical attempt remains unchanged; rollback
creates a new attempt. Verify application health and any existing optional FIP
association. FIP handover happens after acceptance and before predecessor cleanup;
ordinary applications retain no FIP reservation.

Candidate health failure removes the candidate and keeps the previous accepted
workload and historical artifact. If acceptance, FIP handover or cleanup is
interrupted, retain the request file and `$KEY`, resolve the reported dependency,
and repeat the identical POST. Do not submit a new key or delete the predecessor
manually: acceptance may already have committed, and recovery reobserves that
candidate before completing forward. After completion, remove the two local JSON
files. No secret values are returned by these reads or plans.

## Update hosted role image selections

Use the hosted controller's privileged API to select role-image metadata. Run these commands **locally on the admin host as the permitted
operator account**, after installing a controller release that includes this route.
This is not the external operator database: `infra image set` does not update
hosted selections. The setup-only image seed is not a rollover command. Startup
preparation can replay the original seed without overwriting a journal-proven API
rollover, including a committed selection awaiting crash reconciliation. An
unjournaled difference still blocks preparation rather than being silently adopted.

Publish and verify a compatible image first. The API accepts only exact image
UUIDs for all five roles (`admin`, `ingress`, `storage`, `worker`, `builder`),
reuses provider project/role/provenance checks,
and compares the current selection with `expectedImageId` under the hosted
infrastructure lock. It does not create/delete provider resources, replace running
workers, update persistent hosts, or modify the external operator database.
Only worker/builder selections affect future hosted provisioning. The other three
are selected-image metadata, **not observations of a running host's image**, and
changing them does not schedule or perform persistent-host replacement.

```bash
SOCKET="/run/${PLATFORM_NAMESPACE}-controller/privileged.sock"
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  http://localhost/v1/admin/images
REQUEST_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $REQUEST_ID" \
  --data '{"imageId":"NEW_WORKER_IMAGE_UUID","expectedImageId":"CURRENT_WORKER_IMAGE_UUID"}' \
  http://localhost/v1/admin/images/worker/selection
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  "http://localhost/v1/admin/operations/$REQUEST_ID"
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  http://localhost/v1/admin/images
```

Replace both uppercase UUID placeholders with reviewed values from provider
publication evidence and the initial GET. HTTP 202 means accepted for execution,
not selected: poll until `succeeded`, then verify the role's exact UUID in the
selection list. Repeat for `/v1/admin/images/builder/selection` with its own
current/new UUID and a new request ID. Roles are changed separately, not atomically.

A stale expected UUID or rejected provider image leaves the selection unchanged
and the operation failed. Read the current selection and submit a newly reviewed
request with a new key. A recovery-required operation or unknown HTTP outcome must
be retried with the **identical body and key**; changed input conflicts. Recovery
revalidates the saved provider projection and reconciles a possible committed
write without overwriting unrelated selection drift. Do not edit SQLite to unblock it.

Deployment execution records both selected image UUIDs together before building;
resize records its worker image before provisioning. Recovery retains recorded
UUIDs even after rollover. Queued work that has not recorded selections uses the
current choices when execution reaches that boundary. Enable already pins its
worker image for retry. Keep old images available while recorded operations or
accepted workers still reference them. This API only selects images; it does not
publish, prune, or migrate existing workers.

## Size an application

Use the controller's privileged Unix API to select one application's worker
flavor. This does not change the platform's student defaults. The infrastructure
CLI has no product-mutation commands.

Run these commands **locally on admin**, as the operator UID/GID admitted to
`privileged.sock`. Install matching controller and helper releases first; the
helper must provide `app.worker.capacity`. You need `curl`, `jq`, and Python.
No direct SQLite writes or OpenStack server resize commands are supported.

### Plan and resize an accepted application

The app must have an accepted deployment. For an enabled app, replacement needs
quota for both the old worker and target worker/port simultaneously. For an app
that cannot safely run concurrent processes, use the maintenance procedure below
instead of overlapping workers. Worker disks
are disposable: the new VM does not copy the old VM's filesystem. Managed
PostgreSQL, MongoDB, and S3 resources are unchanged.

Set the installed namespace and the application's UUID (not its slug). Select
an available flavor by name or opaque ID. The Commons target is `xl.4core`, ID
`4200`: 4 vCPUs, 16384 MiB RAM, 64 GiB disk. Availability is checked by the API,
not assumed from this example.

```bash
umask 077
NAMESPACE=your-installed-namespace
APP_ID=your-canonical-application-uuid
SOCKET="/run/${NAMESPACE}-controller/privileged.sock"
BASE="http://localhost/v1/admin/applications/${APP_ID}"

curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  "$BASE/resize-plan?flavor=4200" > resize-plan.json
jq . resize-plan.json
```

Review `applicationId`, `deploymentId`, `current.enabled`, `flavor`, `reserve`,
and `activation`. A successful resize enables the app after healthy acceptance.
The plan is an observation, not a capacity reservation. Apply rechecks the exact
application/deployment and flavor projection under the application lock. A
changed plan or provider projection fails before creating a candidate.

CPU allocation uses the new Nomad node's measured total MHz, not an assumed
MHz-per-vCPU conversion. RAM uses measured node memory, bounded by the reviewed
flavor RAM. For each resource the reserve is the larger of 10% (rounded up) and
200 MHz / 512 MiB respectively, or Nomad's larger reported reserve. Thus a node
reporting 10000 MHz and 16000 MiB offers 9000 MHz and 14400 MiB. Exact MHz and RAM
are known only after the candidate VM registers; the plan does not promise a
clock speed or benchmark throughput.

Prepare one immutable request and idempotency key. Replace `commons` with the
app's exact slug when sizing another app:

```bash
jq -n --slurpfile plan resize-plan.json \
  '{plan: $plan[0], confirmation: "commons"}' > resize-request.json
python3 -c 'import uuid; print(uuid.uuid4())' > resize-key.txt

curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(<resize-key.txt)" \
  --data-binary @resize-request.json "$BASE/resize" > resize-response.json
jq . resize-response.json

STATUS_URL=$(jq -r .statusUrl resize-response.json)
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  "http://localhost${STATUS_URL}" | jq .
```

Poll until `status` is `succeeded`, `failed`, or `recovery_required`. HTTP `202`
means admission, not success. Resize creates an immutable deployment attempt,
reuses the accepted digest/configuration without rebuilding or modifying
environment values, starts an isolated worker/job, checks preview health,
promotes the route, then checks public health and commits sizing with the active
deployment pointer. Only then does it remove the predecessor.

Inspect the accepted size through the administrator list (use pagination for
larger inventories):

```bash
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  'http://localhost/v1/admin/applications?limit=100' | jq .
```

The accepted `sizing.workerFlavor`, `sizing.cpuMHz`, and `sizing.memoryMiB` are
pinned for normal redeploy and disable/enable, rather than reset to student
policy. A new worker with insufficient measured capacity fails closed instead
of silently reducing the allocation. The same plan/apply procedure can shrink
an app; this is replacement, not Nova's in-place resize/confirm/revert protocol.

### Deploy new source without stopping the old app during build

Use the [operator curl deployment runbook](APPLICATION_DEPLOYMENTS.md). The
privileged deployment endpoint accepts explicit `maintenance: true`: it builds,
checks artifact availability and storage bindings, then journals and stops the
exact accepted job/worker before starting the replacement. Build/preflight
failure leaves the old app serving. Failure after the stop requires recovery;
there is no automatic database rollback or zero-downtime promise.

Check `/v1/admin/capabilities` for `maintenance-after-build-v1` first. Keep the app
enabled when obtaining the plan and submitting this request. An older controller
must be upgraded, not worked around by disabling the app before its build.

### Resize without concurrent application processes

Applications whose database integrity depends on a single process must not use
rolling overlap. First disable the application through an authorized project
client (`POST /v1/applications/{id}/disable`, body `{}`, new idempotency key),
and poll that operation until it succeeds. Disable preserves managed data and
configuration while confirming removal of the accepted job and worker.

Then obtain a **fresh** sizing plan through the privileged API. Require
`current.enabled: false`; apply it using the resize procedure above. Changes to
the enabled state invalidate the plan. Before creating a candidate, the controller
reobserves the disabled predecessor's exact worker slot and requires absence.
It reuses the accepted artifact without rebuilding and enables the application
only after the new worker passes health. A failed candidate leaves the app stopped,
its previous size unchanged, and its managed data untouched. Fix the dependency
and submit a fresh reviewed resize request, or explicitly enable the prior size.
An unknown predecessor observation blocks creation and requires same-key recovery.

This maintenance procedure has downtime from disable until healthy acceptance.
It does not restore database contents or make an unsafe multi-process application
safe for rolling deployments. Do not enable the old instance while the maintenance
resize is running. Keep the normal backup and restore-verification prerequisites.

### Select a flavor for the first deployment

Declare the app through `POST /v1/applications` on the project socket, then get a
sizing plan as above. Its `deploymentId` is `null`. Submit the usual exact-commit
[deployment fields](INTERNALS.md#project-and-privileged-routes), plus the complete
`plan` object, to:

```text
POST /v1/admin/applications/{id}/deployments
```

This privileged route also supports a new source deployment combined with a
size change. The project deployment route rejects sizing fields. Omit operator
sizing entirely for students: ordinary declaration/deployment retains the
policy's small flavor, CPU, and memory values, subject to the same capacity
check. Policy defaults are not changed by selecting another app's flavor.

A single-vCPU worker is supported when its measured CPU and RAM fit the pinned
allocation after reserve. In particular, the example policy's 2048-MiB request
cannot fit a VM with only 2048 MiB total RAM plus the service reserve. Choose a
flavor with RAM headroom for that policy (one vCPU with 4 GiB can suffice), or
use a reviewed per-app sizing plan before the first deployment. Existing pinned
allocations are not silently reduced to accommodate a smaller worker.

### Sizing failures and retries

- **Plan drift or invalid confirmation:** no candidate is created. Obtain and
  review a fresh plan, then submit a new request/key.
- **Candidate scheduler, application, or public health failure with confirmed
  cleanup:** the operation is `failed`; the prior accepted size and enabled/stopped
  state remain.
  The reused accepted artifact is not deleted. Fix the app/dependency, review a
  plan, and use a new key for a new attempt.
- **Unknown provider/helper result, interrupted acceptance, or unfinished
  cleanup:** the operation is `recovery_required`. Preserve the request and key;
  after restoring the dependency, repeat the exact POST. Do not edit the plan or
  use a new key to bypass the application's blocked scope. Recovery reobserves
  health before accepting and resumes predecessor cleanup after acceptance.
- **Lost response:** repeat the same POST/key. It returns the existing operation
  instead of allocating another worker. Reusing a key with a changed body is
  `409 IDEMPOTENCY_CONFLICT`.

There is no forced rollback after successful acceptance and predecessor deletion;
use a new reviewed sizing operation. Plans do not reserve quota, and the platform does not copy local
disk state or resize managed-storage quotas. These examples describe the API;
production provider behavior still requires a release acceptance exercise.

## Retry application and storage operations

Poll the `statusUrl` returned with HTTP 202. Privileged application/storage
deletions return `/v1/admin/operations/{id}`, which is readable on the same
privileged socket, including when the original request is replayed after deletion.
Repeat an unknown or recovery-required request with its identical body and
`Idempotency-Key`; do not allocate a new key to bypass unfinished scope.

New storage dispatches use the same `storage.create`, `storage.verify`,
`storage.rotate`, and `storage.remove` kinds as their domain journals. Recovery
requires exact dispatch/domain kind and scope agreement. There is no dual-spelling
compatibility path or data migration: an old unfinished `storage.<type>.<action>`
dispatch paired with a different domain kind remains blocked and unchanged.
Existing data is preserved. Check for unfinished storage operations before rollout.
If a malformed dispatch is found, stop and obtain a separately reviewed, bounded
recovery plan; do not bypass the invariant or edit SQLite as part of this procedure.

A positively reported source/build rejection is recorded before cleanup. The
controller requires both exact builder/server-port absence and an authenticated
404 for that build's unique registry publication tag before marking the attempt
and operation `failed` with confirmed cleanup. That terminal result frees the
application scope for a corrected commit/configuration with a **new** key.
Replaying the failed request returns its existing result without rebuilding.

If cleanup, registry availability, or identity checks are uncertain, the operation
remains `recovery_required`. Identical retries of a recorded rejection perform
cleanup only, including after controller restart. A present build artifact is not
deleted or adopted automatically; it requires separate investigation. Unknown
transport results and malformed success metadata retain the existing deployment
reconciliation path rather than being called deterministic rejection.

Install matching controller and helper releases: `app.build.cleanup` is a new
fixed helper action. The admin image must also contain the updated
`<paths.root>/infra/registry/delete_manifest.py` with `build-absent`; Nix supplies
that source through the baked `infra` link. An older helper or registry script
cannot supply the required absence evidence and leaves recovery blocked.
No runtime worker or managed storage is removed
by failed-build cleanup. This is not an arbitrary cancellation or forced-unlock
endpoint.

## Retain a worker primary fixed IPv4

On provider-routed worker networks, staff can reserve an explicit IPv4 on a
controller-owned Neutron **primary port**, before starting the worker. This is
not a floating IP or post-health outbound-IP handover: the worker boots with this
address and keeps it across disable/enable and maintenance replacement. The
feature creates no router, floating IP, extra NIC, ingress rule, or DNS entry.
It does not guarantee public reachability; cloud routing and policy still apply.
Floating and retained fixed reservations are mutually exclusive per application.

Before upgrading, take a fresh hosted-controller backup. Install matching
controller, helper, and lifecycle scripts through the admin image, and verify
new admin/controller readiness **before reserving**. Migration 4 adds
`application_fixed_ports`; the older schema-3 controller (including `95d57d85`)
cannot operate the migrated database. Do not downgrade that controller against
schema 4 or restore old state while leaving retained provider ports unmanaged.
Worker images do not need rebuilding for this feature.

Run on admin as the operator admitted to `privileged.sock`:

```bash
umask 077
APP_ID=your-canonical-application-uuid
NETWORK_ID=your-canonical-worker-network-uuid
SUBNET_ID=your-canonical-worker-subnet-uuid
ADDRESS=your-requested-ipv4
SOCKET="/run/${PLATFORM_NAMESPACE}-controller/privileged.sock"
BASE="http://localhost/v1/admin/applications/${APP_ID}/fixed-ip"
jq -n --arg network "$NETWORK_ID" --arg subnet "$SUBNET_ID" --arg address "$ADDRESS" \
  '{networkId: $network, subnetId: $subnet, address: $address}' > fixed-ip-plan-request.json
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  -H 'Content-Type: application/json' --data-binary @fixed-ip-plan-request.json "$BASE/plan" | jq .
jq '. + {action: "reserve"}' fixed-ip-plan-request.json > fixed-ip-request.json
python3 -c 'import uuid; print(uuid.uuid4())' > fixed-ip-key.txt
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $(<fixed-ip-key.txt)" \
  --data-binary @fixed-ip-request.json "$BASE" > fixed-ip-response.json
STATUS_URL=$(jq -r .statusUrl fixed-ip-response.json)
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  "http://localhost${STATUS_URL}" | jq .
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" "$BASE" | jq .
```

The plan checks the exact configured network, IPv4 subnet CIDR, usable address,
and worker security-group identity. Explicit addresses outside automatic
allocation pools are allowed; **availability and allocation permission remain
unproven until reservation succeeds**. HTTP `202` means admitted: poll `statusUrl`
until `succeeded` or `recovery_required`. A successful `GET` of a reserved port
rechecks provider identity and reports `attachment: detached|attached`, `portId`,
`address`, and `serverId`. It does not probe application traffic.

Reservation may precede disabling an existing ordinary worker; that worker and
its disposable port are unchanged. For new source, use explicit
[`maintenance: true`](APPLICATION_DEPLOYMENTS.md) so the controller builds first
and disables the predecessor only at cutover. For standalone resize or rollback,
disable the application before obtaining the plan. Replacements require observed
predecessor absence and never overlap workers. For code-only updates, the opt-in
[same-worker deployment and rollback](APPLICATION_DEPLOYMENTS.md#reuse-the-existing-worker-for-code-updates)
keeps the attached server/port/IP and stops only the application, avoiding worker
boot downtime. It requires a ready existing worker and cannot migrate an ordinary
worker to a newly reserved port. Enable also refuses to substitute the port while an old
ordinary worker remains. Existing per-application flavor and scheduler sizing
remain pinned. After acceptance, verify the same `portId`/`address`, application
health at its original HTTPS URL, and connectivity to required dependencies.

Disable and failed-candidate cleanup retain the detached port. To release, submit
`{"action":"release"}` to the same endpoint with a **new** idempotency key;
release accepts only the exact owned, unbound port and loses the address.
Application deletion removes bounded worker slots first, then releases the port.
Do not mutate these resources out of band: Neutron has no attachment/delete CAS.
Unknown create/delete outcomes remain `recovery_required`. Retry the original
request with its unchanged body/key. Lost allocation responses recover only an
exact journaled ownership marker; an empty inventory does not authorize another
allocation. Unresolved worker creation also blocks release. Wrong ownership,
security, extra addresses, trunks, or foreign attachments block mutation; there
is no name-only adoption or forced-release endpoint.

## Reserve a stable outbound IPv4

Staff can optionally reserve one Neutron floating IPv4 across successful worker
replacement, resize, and disable/enable. This does not change the application URL,
Cloudflare, TLS, guest routing, or firewall rules. Direct public app ingress is
unsupported. Candidate/build workers do not use the reservation; handover is not
connection-preserving or zero downtime. Applications must start and pass candidate
health without the reserved source address. A dependency that requires it before
startup/health is incompatible with this candidate-first handover.

By default, fixed IPs belong to disposable worker ports, not applications. They
may already be globally public on provider-routed networks; this floating-IP
feature does not preserve those ports, add another NIC, or provision a tenant
network/router. For that cloud topology, use the separate
[retained primary fixed IPv4](#retain-a-worker-primary-fixed-ipv4) feature. Floating-IP
quota `0` or no matching routed subnet makes this optional feature unavailable,
even when existing public fixed-IP networking works.

### Check cloud capability

Run locally on admin as the operator identity admitted to `privileged.sock`,
using matching controller code and a fresh hosted backup. Schema migration 3 adds
`application_floating_ips`; do not downgrade or restore old state while leaving
its provider associations unmanaged. The external infrastructure CLI database is
not the hosted product database.

The cloud must expose exactly one IPv4 worker subnet and one project-owned router
connecting it to the selected external network with SNAT enabled. The controller
needs quota/network/subnet/router/port/server/security-group/floating-IP reads
and the corresponding floating-IP mutation permissions. The target worker must
retain its exact project-owned worker group, port security, and ingress rules
restricted to the ingress-tier group. No security rule is changed.

```bash
umask 077
APP_ID=your-canonical-application-uuid
EXTERNAL_NETWORK_ID=your-canonical-external-network-uuid
SOCKET="/run/${PLATFORM_NAMESPACE}-controller/privileged.sock"
BASE="http://localhost/v1/admin/applications/${APP_ID}/public-ip"
jq -n --arg network "$EXTERNAL_NETWORK_ID" \
  '{externalNetworkId: $network}' > public-ip-plan-request.json
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  -H 'Content-Type: application/json' \
  --data-binary @public-ip-plan-request.json "$BASE/plan" > public-ip-plan.json
jq . public-ip-plan.json
jq -e '.supported == true' public-ip-plan.json
```

This plan is read-only, needs no idempotency key, and reserves no quota. Review
`reasons`, `floatingIpQuota`, `floatingIpsUsed`, and `routerId`. Quota exhaustion
or missing/ambiguous routed topology blocks allocation before provider mutation;
malformed/failed observations fail closed. Other operators must not modify these
resources out of band: Neutron reassociation has no atomic compare-and-set API.

### Allocate, attach, or release

Select one body. `allocate` creates a platform-owned allocation, deleted on
release/app deletion. `attach` accepts an existing unassociated, project-owned
floating-IP UUID on the selected external network; release detaches but never
deletes that supplied allocation. It must not belong to another app reservation.

```bash
jq -n --arg network "$EXTERNAL_NETWORK_ID" \
  '{action: "allocate", externalNetworkId: $network}' > public-ip-request.json
# Alternatively: {"action":"attach","externalNetworkId":"UUID","floatingIpId":"UUID"}
# To release: {"action":"release"}
python3 -c 'import uuid; print(uuid.uuid4())' > public-ip-key.txt
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(<public-ip-key.txt)" \
  --data-binary @public-ip-request.json "$BASE" > public-ip-response.json
STATUS_URL=$(jq -r .statusUrl public-ip-response.json)
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
  "http://localhost${STATUS_URL}" | jq .
curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" "$BASE" | jq .
```

HTTP `202` means admitted, not completed. Poll `statusUrl` to a terminal or
`recovery_required` state; reuse the exact body/key for retries. A new intent
needs a new key. `GET` reports recorded state, not a fresh probe: check ownership,
address, phase, current/pending port, and allocation marker. `reserved` is
unassociated; `active` records a confirmed router mapping on the accepted port.
Reservations can precede first deployment and survive disable; they may incur
charges while unused. Disable detaches before worker deletion; healthy enable
reassociates the reservation. Releasing a platform allocation loses its address.

Verify actual outbound source IPv4 through a controlled endpoint from the
accepted app—not admin, builder, or preview—and repeat after resize and
disable/enable. Also verify the original HTTPS app URL. Provider association
status and offline OSC fixtures do not prove container SNAT or upstream access.

### Recover handover or release

The old worker retains the address until the candidate passes existing health
and is durably accepted. Then the same floating-IP UUID is reassociated and
verified before predecessor cleanup. Failure before acceptance does not move it;
after acceptance handover is forward-only. A failed/ambiguous handover preserves
the predecessor and leaves the deployment recovery-required. Retry the original
deployment/resize request, not a new public-IP mutation. Recovery checks candidate
health and exact old/pending port ownership; unrelated association drift blocks
mutation. All public-IP and application lifecycle mutations share the app lock.

Allocation journals a unique marker before create. A lost create response can
adopt only one exact unassociated marker match. Zero or multiple matches remain
recovery-required: zero is not proof that create cannot finish later. There is
no automatic repeat-create or force-abandon endpoint. Escalate with the operation,
reservation, and marker identities; do not edit SQLite or delete similar-looking
provider resources. A completed reservation accepts a fresh
`{"action":"reconcile"}` request to verify/converge its recorded association,
not overwrite unrelated drift. Ambiguous release retains its journal for the
same-key retry; app deletion does not delete the worker until release completes.

## Back up all state classes

The deployment has three independent backup classes:

| Backup | Source | Accepted location | Identity custody |
| --- | --- | --- | --- |
| Hosted controller | Admin controller SQLite and deploy keys | `<paths.backups>/hosted-controller` | Operator escrow; not admin |
| External operator state | Operator CLI SQLite | `<paths.backups>/controller` | Operator escrow |
| Managed data | PostgreSQL, MongoDB, Garage, retained OCI artifacts | `<paths.backups>/<namespace>/<timestamp>` | Admin plus separate operator escrow |

One class does not substitute for another.

### Hosted-controller backup

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl start "$PLATFORM_NAMESPACE-hosted-controller-backup.service"
ssh -F "$SSH_CONFIG" platform-admin -- \
  journalctl -u "$PLATFORM_NAMESPACE-hosted-controller-backup.service" -n 5 --no-pager
```

Success reports `hosted-controller-backup=... sha256=...`. A committed set has
ciphertext, checksum, and final manifest. The daily timer runs as the controller
account; the private age identity remains off-platform. New backups also commit
`hosted-controller-source-keys-<timestamp>.tar.age`, its checksum and manifest,
and bind that exact archive in the SQLite manifest's `sourceKeys` field. Verify
both trios. Version-3 off-site bundles copy the paired archive automatically;
versions 1/2 remain readable but cannot restore keys that were never backed up.

### External operator-state backup

```bash
operator_backup="$($PLATFORM_CLI backup)"
printf '%s\n' "$operator_backup"
grep -Eq '^backup=platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age sha256=[0-9a-f]{64}$' \
  <<<"$operator_backup"
```

The command uses SQLite's online backup API, encrypts locally, and transfers the
ciphertext through the pinned alias. Admin accepts only a complete age-v1
ciphertext/checksum/manifest trio. It never copies a live WAL file.

### Managed-data backup and restore check on admin

The admin managed-data identity is
`$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt`. Do not overwrite it
while backups depend on it. Keep an escrow copy outside the deployment.

Run the packaged backup with its fixed dependency paths:

```bash
managed_backup="$(
  ssh -F "$SSH_CONFIG" platform-admin -- env \
    PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
    AGE="$PLATFORM_ROOT/bin/age" \
    AGE_KEYGEN="$PLATFORM_ROOT/bin/age-keygen" \
    AGE_KEY="$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt" \
    EMIT_SCRIPT="$PLATFORM_ROOT/infra/backup/emit_logical_backup.sh" \
    SERVICE_CHECK_PYTHON=python3 \
    GARAGE_EMIT_SCRIPT="$PLATFORM_ROOT/infra/backup/emit_garage_backup.py" \
    REGISTRY_ARTIFACT_SCRIPT="$PLATFORM_ROOT/infra/backup/registry_artifact.py" \
    "$PLATFORM_ROOT/infra/backup/run_platform_backup.sh"
)"
printf '%s\n' "$managed_backup"
grep -Eq '^platform backup complete: .+$' <<<"$managed_backup"
```

The set contains encrypted `postgres.age`, `mongodb.age`, `garage.age`, and
`registry.age`, plus `MANIFEST` and `SHA256SUMS`. OCI blobs stream through
bounded verification and are retained according to controller registry
retention.

Restore-check the newest set without touching live services:

```bash
restore_check="$(
  ssh -F "$SSH_CONFIG" platform-admin -- env \
    PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
    AGE="$PLATFORM_ROOT/bin/age" \
    AGE_KEY="$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt" \
    "$PLATFORM_ROOT/infra/backup/verify_latest_restore.sh"
)"
printf '%s\n' "$restore_check"
grep -Eq '^latest platform restore=verified evidence=.+/RESTORE-MANIFEST$' \
  <<<"$restore_check"
```

The check starts temporary PostgreSQL and MongoDB containers and validates the
Garage and OCI archives. It writes `RESTORE-MANIFEST` only after all checks pass
and removes temporary resources on success or failure.

### Verify backup schedules

```bash
systemctl --user is-enabled openstack-platform-backup.timer
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl is-enabled "$PLATFORM_NAMESPACE-hosted-controller-backup.timer"
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl is-enabled "$PLATFORM_NAMESPACE-platform-backup.timer"
```

## Export encrypted recovery evidence off site

Mount operator-selected off-site storage on admin before installing
`config/offsite-export.example.json`. The mount point must be direct,
`agentops`-owned, mode `0700`, on a different device from
`<paths.backups>`, and protected by provider versioning, object lock, or WORM
retention. The configuration contains no provider credential.

Record the exact source and filesystem type:

```bash
export OFFSITE_MOUNT=/mnt/institutional-recovery
mountpoint -q "$OFFSITE_MOUNT"
findmnt -n -o SOURCE,FSTYPE --target "$OFFSITE_MOUNT"
```

Create a private copy, edit `destination`, `mountSource`, and `filesystemType`
to match, and then install it on admin:

```bash
install -m 0600 config/offsite-export.example.json \
  /private/path/offsite-export.json
${EDITOR:?set EDITOR} /private/path/offsite-export.json
scp -F "$SSH_CONFIG" -- /private/path/offsite-export.json \
  platform-admin:/home/agentops/offsite-export.json
ssh -F "$SSH_CONFIG" platform-admin -- install -m 0600 \
  /home/agentops/offsite-export.json \
  "$PLATFORM_ROOT/persistent/offsite-export.json"
```

Run and verify one export:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- openstack-platform-recovery scheduled-export \
  --platform-config "/etc/$PLATFORM_NAMESPACE/platform.json" \
  --config "$PLATFORM_ROOT/persistent/offsite-export.json" \
  --receipt "$PLATFORM_ROOT/persistent/status/offsite-export.json"
ssh -F "$SSH_CONFIG" platform-admin -- openstack-platform-recovery status \
  --platform-config "/etc/$PLATFORM_NAMESPACE/platform.json" \
  --config "$PLATFORM_ROOT/persistent/offsite-export.json" \
  --receipt "$PLATFORM_ROOT/persistent/status/offsite-export.json"
```

Export selects only the newest committed set from each backup class, verifies
bounded direct files after copying, and then updates the credential-free
receipt. An unmounted, bind-mounted, same-device, changed, or stale destination
fails without replacing the previous receipt. Apply provider retention only
after a newer bundle and its full drill evidence are retained.

## Restore the hosted controller

This operation replaces the live hosted-controller SQLite database. Before
starting, verify the selected manifest/checksum, decrypt the ciphertext on the
operator recovery host, and stage a direct mode-`0600` SQLite file on admin as
`/home/agentops/hosted-controller-restore.sqlite3`. Never copy the age identity
to admin. For a snapshot whose manifest contains `sourceKeys`, also verify and
decrypt that named tar archive on the recovery host. Transfer the plaintext as
`/home/agentops/hosted-controller-source-keys.tar` with mode 0600. Treat it as
private-key material; do not unpack it manually. Legacy snapshots without a key
archive leave existing keys unchanged; after complete loss, replace missing keys
in each app's Settings and update the read-only deploy keys on GitHub.

In an approval-gated root recovery session on the selected admin host:

```bash
sudo systemctl stop \
  "$PLATFORM_NAMESPACE-hosted-controller-backup.timer" \
  "$PLATFORM_NAMESPACE-hosted-controller-backup.service" \
  "$PLATFORM_NAMESPACE-controller.service"
sudo install -m 0600 -o platform-controller -g platform-controller \
  /home/agentops/hosted-controller-restore.sqlite3 \
  "$PLATFORM_ADMIN_STATE/controller/restore-input.sqlite3"
sudo rm -f /home/agentops/hosted-controller-restore.sqlite3
# For snapshots with a paired deploy-key archive:
sudo install -m 0600 -o platform-controller -g platform-controller \
  /home/agentops/hosted-controller-source-keys.tar \
  "$PLATFORM_ADMIN_STATE/controller/restore-source-keys.tar"
sudo rm -f /home/agentops/hosted-controller-source-keys.tar
sudo openstack-platform-hosted-controller-restore --yes
sudo systemctl start \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-hosted-controller-backup.timer"
```

The launcher refuses active controller/backup units and unsafe input. It
validates deployment identity, complete known schema, SQLite integrity, foreign
keys, and unfinished operations before atomic replacement. On refusal, the
current database remains unchanged. With a key archive, every entry is validated
against the replacement database before either state is changed. Keys are restored
as platform-controller-owned 0700 directories and 0600 files; the public key's
mtime is retained. SQLite and key-directory selection are separate filesystem
commits: if interrupted after SQLite replacement, keep the controller stopped,
retain the staged inputs, and repeat restore before starting it.

A persistent admin-image cutover can deadlock when controller preparation
refuses changed image selections while the retained database has one known
`recovery_required` operation. After retaining a fresh hosted backup and
verifying that the replacement snapshot has no unfinished operations, recovery
may explicitly acknowledge that exact operation UUID:

```bash
sudo openstack-platform-hosted-controller-restore --yes \
  --replace-current-recovery-required-operation EXACT_OPERATION_UUID
```

This exception is recovery-console/root gated and succeeds only when the
current database's entire unfinished operation and dispatch state is that one
exact UUID in `recovery_required`; running, pending, additional, absent, or
mismatched state is refused. It does not weaken candidate verification. Do not
use it to avoid replaying a recoverable operation or without retaining its
pre-restore hosted backup.

Verify readiness and create a fresh hosted backup:

```bash
sudo systemctl is-active "$PLATFORM_NAMESPACE-controller-readiness.service"
sudo systemctl start "$PLATFORM_NAMESPACE-hosted-controller-backup.service"
```

Hosted restore does not recreate OpenStack, Nomad, workers, or managed data.

## Restore external operator state offline

Stop the user backup timer and every operator command. Copy an accepted
ciphertext and the escrowed identity to direct current-user-owned mode-`0600`
files on the operator host.

```bash
systemctl --user stop openstack-platform-backup.timer openstack-platform-backup.service
restore_output="$(
  /srv/openstack-platform/bin/openstack-platform-restore \
    /private/path/platform-YYYYMMDDTHHMMSSZ.sqlite3.age \
    --age-identity /private/path/backup-age-identity.txt \
    --yes
)"
printf '%s\n' "$restore_output"
grep -Eq '^restore=verified schema-version=[0-9]+ integrity=ok$' <<<"$restore_output"
systemctl --user start openstack-platform-backup.timer
$PLATFORM_CLI status
$PLATFORM_CLI infra list
```

Restore is offline and contacts no provider, SSH helper, Nomad, or network
service. It validates deployment identity, schema, integrity, foreign keys,
sidecars, locks, and unfinished operations before replacing
`/srv/openstack-platform/state/platform.sqlite3`. Failure leaves the existing
database unchanged.

For a drill that must not touch live operator state, use an absent child in a
private replacement directory:

```bash
install -d -m 0700 /private/path/offline-state
/srv/openstack-platform/bin/openstack-platform-restore \
  --replacement-state-directory /private/path/offline-state \
  /private/path/platform-YYYYMMDDTHHMMSSZ.sqlite3.age \
  --age-identity /private/path/backup-age-identity.txt \
  --yes
```

## Drill complete loss recovery

Full mode is destructive to the services named by its replacement inventory.
Provision empty replacement PostgreSQL, MongoDB, Garage, and registry services.
Do not point the replacement configuration at healthy or nonempty services.
The off-site bundle must contain both SQLite classes, all four managed archives,
an operator image selection, and at least one accepted hosted deployment.

```bash
infra/backup/full_loss_recovery_drill.sh --full \
  /mnt/recovered/$PLATFORM_NAMESPACE-YYYYMMDDTHHMMSSZ \
  /srv/full-loss-drill \
  /escrow/controller-age-identity.txt \
  /escrow/managed-age-identity.txt \
  /private/replacement-platform.json
test -f /srv/full-loss-drill/DRILL-EVIDENCE.json
```

The work path must be absent. The drill imports and verifies the bundle,
restores both SQLite databases to private replacement directories, verifies
restored image/application/accepted-deployment records, and runs destructive
managed replacement restore. `DRILL-EVIDENCE.json` is committed only after all
SQLite and managed restore checks succeed.

For archive and SQLite inspection without service mutation:

```bash
infra/backup/full_loss_recovery_drill.sh --verify-only \
  /mnt/recovered/$PLATFORM_NAMESPACE-YYYYMMDDTHHMMSSZ \
  /srv/full-loss-verification \
  /escrow/controller-age-identity.txt \
  /escrow/managed-age-identity.txt \
  /private/replacement-platform.json
```

Verify-only cannot create `DRILL-EVIDENCE.json` and is not a completed
full-loss drill.

## Replace a persistent host

Replacement stops the old VM but retains it for rollback. The fixed port and
retained volumes move to the candidate. This is a singleton replacement with
service interruption, **not zero downtime**. The old VM is deleted only after
the candidate passes readiness and exact image/flavor/name/provenance checks.
On readiness failure, replacement restores the retained old host. Never delete
the old server or detach a volume manually.

Before replacing storage, require a fresh managed-data `RESTORE-MANIFEST`.
Before replacing admin, require fresh hosted-controller and operator-state
backups. Publish and live-test the replacement role image before selecting its
exact UUID. Use `admin`, `ingress`, or `storage`; the token-file option below is
valid only for ingress.

### Replace admin across a baked image-inventory change

The controller package in an admin image is immutable; installing the external
operator or a helper release does **not** update that controller. The replacement
admin image must contain this startup-seed and all-role selection API support.
Keep the deployment identity and compatibility projection unchanged; this
procedure does not migrate a namespace, project, prefix, or PKI identity.

The actual boot path is:

1. `paths.adminState` mounts the retained volume. `paths.root/persistent` points
   to its operator subtree; the controller database is a separate retained subtree.
2. `nix/roles/admin.nix` runs `<namespace>-controller-prepare.service`, copies
   `<adminState>/operator/image-selections.json` to the controller-owned
   `<adminState>/controller/image-selections.json`, and normalizes credential access.
3. It runs the **baked** `openstack-platform-controller-seed-images` as the
   controller account, with `/etc/<namespace>/platform.json` and
   `<adminState>/controller/state`, before the controller service starts.
4. Initial/missing-role seeding requires the baked image names. Existing records
   matching the protected retained seed are preserved even when the new guest's
   baked names differ. A changed existing selection instead requires exact hosted
   CAS journal evidence. Seed replay never overwrites an existing selection.

Use this order; do not replace the retained seed with the new build's inventory
before the hosted metadata has been reconciled:

1. Pause new application mutations for the cutover. Complete or deliberately
   account for in-flight operations, retain their referenced images, and take the
   required backups. Keep the old operator seed unchanged and keep a protected
   copy of it. Its five records must match the retained hosted selections, or
   already have the supported journal evidence. A pre-existing unexplained mismatch
   is a blocker, not permission to edit the database.
2. On the **external operator host**, select and replace only admin, using reviewed
   exact publication evidence and the existing protected bootstrap inputs:

   ```bash
   export OPERATOR_PUBLIC_KEY=/private/operator.pub
   export ADMIN_SECRETS_FILE=/private/admin-bootstrap.env
   export PKI_DIR=/private/pki
   $PLATFORM_CLI infra image set admin NEW_ADMIN_IMAGE_UUID
   $PLATFORM_CLI infra replace admin --yes
   $PLATFORM_CLI infra list
   ```

   The new guest starts against the unchanged retained seed and database; it does
   not need its API to have been installed on the old guest. Keeping the old seed
   also avoids a *seed-name* failure on automatic pre-acceptance rollback. This is
   not a promise of schema downgrade compatibility: a fallback controller must
   understand any newly applied migrations, including optional-public-IP schema 3.
   Do not force-restore or delete migration rows to make an older binary start.
3. Independently verify the new controller and readiness units on admin. The
   operator's admin replacement gate checks Nomad readiness, not the full controller
   API. Do not resume app mutations merely because `infra replace` returned success.
   Install the matching reviewed helper release when required; retained helper
   releases are not replaced by the image selection API.
4. On **admin**, GET `/v1/admin/images`. Run the exact-UUID CAS/poll sequence in
   [Update hosted role image selections](#update-hosted-role-image-selections) for
   each of `admin`, `ingress`, `storage`, `worker`, and `builder`, using the latest
   all-role build's reviewed UUIDs and each role's current UUID. Require `succeeded`
   for each. Persistent metadata updates do not replace ingress or storage. Every
   partial transition remains restartable against the unchanged old seed because
   completed CAS operations provide the required evidence.
5. Only after those operations succeed, atomically refresh the operator-owned
   seed from the validated API projection, checking its names against the actual
   new guest config. Run locally on admin as the permitted operator account:

   ```bash
   (
     set -euo pipefail
     umask 077
     SOCKET="/run/${PLATFORM_NAMESPACE}-controller/privileged.sock"
     seed="$PLATFORM_ADMIN_STATE/operator/image-selections.json"
     test -f "$seed" && test ! -L "$seed"
     test "$(stat -c '%u:%a' "$seed")" = "$(id -u):600"
     temporary="$(mktemp "${seed}.XXXXXX")"
     trap 'rm -f -- "$temporary"' EXIT
     curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
       http://localhost/v1/admin/images |
       jq --exit-status --slurpfile guest "/etc/${PLATFORM_NAMESPACE}/platform.json" '
         if (.items | map(.role) | sort) != (["admin","ingress","storage","worker","builder"] | sort)
           or any(.items[]; .displayName != $guest[0].images[.role])
         then error("selected roles do not match the baked inventory")
         else {schemaVersion: 1, projectId: $guest[0].projectId, namespace: $guest[0].namespace,
           images: (.items | map({key: .role, value: {imageId, displayName, sourceCommit, compatibilityHash}}) | from_entries)}
         end' > "$temporary"
     sync -f "$temporary"
     mv -T "$temporary" "$seed"
     sync -f "$seed"
   )
   ```

6. With approved service-administration authority, restart and verify the real
   units; do not run the seed executable against SQLite as an ad hoc repair:

   ```bash
   sudo systemctl restart "$PLATFORM_NAMESPACE-controller.service"
   sudo systemctl restart "$PLATFORM_NAMESPACE-controller-readiness.service"
   sudo systemctl show -p Result "$PLATFORM_NAMESPACE-controller-prepare.service"
   sudo systemctl is-active "$PLATFORM_NAMESPACE-controller.service" \
     "$PLATFORM_NAMESPACE-controller-readiness.service"
   ```

   The prepare unit is a non-remaining oneshot: confirm its `Result=success` with
   `systemctl show -p Result` rather than requiring it to remain active. Recheck
   the API selections and the external observed admin UUID; resume app mutations
   only after both checks pass. No external operator DB synchronization is implied.

This preserves the existing controller DB rather than restoring or replacing it.
A premature latest seed with unproven old DB selections still fails safely; keep
or restore the original **metadata file**, not a different SQLite database.

### Supply the ingress token for each fresh replacement

Obtain the current raw Cloudflare connector token through your authorized
credential-custody process. The CLI does not extract guest credentials, prompt
for tokens, or maintain a local credential store. If the only copy is on a
guest without authenticated access, stop and resolve credential custody
separately; do not bypass SSH host-key checking.

The input must be a direct, single-link, operator-owned mode-0600 regular file,
at most 16 KiB, under trusted parent directories. It must contain only the
base64 connector token (at most 8192 characters), optionally followed by one
LF or CRLF newline—not a `TUNNEL_TOKEN=...` environment file or an API token.
The CLI checks file protection and the token's account/tunnel/secret structure
**offline**. This does not prove Cloudflare authentication or domain routing.
Confirm the intended tunnel and domain through authenticated Cloudflare records.
A revoked but structurally valid token can pass local checks; candidate readiness
must still pass before deleting the old VM.

Run as the operator account using the variables from [Initialize operator
variables](#initialize-operator-variables). Substitute your current protected
bootstrap paths and the selected image UUID:

```bash
export OPERATOR_PUBLIC_KEY=/private/operator.pub
export NOMAD_TOKENS_FILE=/private/nomad-tokens.env
export PKI_DIR=/private/pki
$PLATFORM_CLI infra image set ingress NEW_INGRESS_IMAGE_UUID
$PLATFORM_CLI infra replace ingress --yes \
  --cloudflare-tunnel-token-file /private/current-connector-token
$PLATFORM_CLI infra logs ingress --lines 200
test "$(curl --fail --show-error --silent "https://$PLATFORM_DOMAIN/healthz")" = OK
```

Supply a file path, never the token value in arguments, chat, or logs. Each fresh
ingress replacement reads and validates the explicit file before any provider
call, then renders the reviewed template with Cloudflare enabled. Missing or
malformed input fails without stopping the old VM. `ENABLE_CLOUDFLARED=false`,
`CLOUDFLARE_TUNNEL_TOKEN_FILE`, and opaque ingress `--user-data` cannot bypass
this contract. Other roles retain their protected-input contract.

The CLI leaves the original file unchanged. Private temporary token and rendered
user-data files are unlinked on normal completion and handled failure; forced
process termination can leave temporary files requiring protected cleanup.
The token is provisioned to the candidate via provider user-data, not stored in
operator SQLite, refs, logs, or a permanent local credential database. Maintain
your own authorized credential custody. A later fresh replacement or rotation
uses whichever current protected file you supply; no import or reset is needed.
Coordinate Cloudflare rotation so the retained old host can still serve if rollback
is needed; the CLI does not rotate credentials on that host.

### Retry an interrupted replacement

An ambiguous provider result becomes recovery-required. Restore the named
dependency and rerun `infra replace ingress --yes` using the same inventory and
state directory. A recorded pre-acceptance operation rolls back; an accepted
operation rechecks exact candidate provenance, fixed resources, readiness, and
public HTTPS `/healthz` before cleaning up the retained old VM. Tunnel-mode
HTTP is deliberately loopback-only, and direct-mode HTTP is restricted to
provider sources: the external checker does not probe the fixed IP's HTTP port.
It allows up to 120 seconds of public connector warm-up within the remaining
operation deadline, without accepting redirects or a response other than `OK`.
Neither path
reads a token file or re-renders user-data. If supplied on that retry,
`--cloudflare-tunnel-token-file` is ignored with an explicit acknowledgement; it
cannot change the recorded candidate's credential.

Successful rollback ends that operation and does not start another replacement.
Invoke a fresh replacement separately with the current token file. If interruption
occurred in the initial `validated` phase before provider observation, retry starts
a fresh attempt and requires the file. Supplying the current file on every retry
is therefore permitted, but it is consumed only when a fresh attempt starts.

Release updates follow [Install releases outside automated
setup](MAINTENANCE.md#install-releases-outside-automated-setup). Executable
rollback does not restore database or provider state. Before a schema migration,
take and verify backups and follow the [database migration
order](MAINTENANCE.md#database-migration-order).

## Prune images

Image pruning is plan-first:

```bash
$PLATFORM_CLI infra image prune
$PLATFORM_CLI infra image prune --apply --yes
```

The plan protects selected images, server references, unfinished-operation
references, and configured retained history. Apply re-observes every UUID and
fingerprint under the infrastructure lock. Missing or malformed provider image
projections fail closed.

## Teardown boundary

Whole-deployment teardown requires separate human authorization and a reviewed
provider plan scoped to the exact project, prefix, and immutable ownership
evidence. Before provider deletion:

1. confirm through the deployed management boundary that no product resources
   remain; nonzero `APPS` or `STORAGE` is a stop condition;
2. retain and verify all three backup classes, both age identities, off-site
   bundle IDs, and full-loss drill evidence;
3. stop operator, hosted-controller, managed-data, and off-site timers; and
4. record unrelated resources that are explicitly out of scope.

The operator CLI intentionally has no whole-deployment or product teardown
command. Do not use an older binary, substring name matching, or project-wide
delete command.

## Troubleshooting and recovery rules

Start with the symptom and preserve the operation or correlation ID. Do not
clear SQLite rows, bypass the helper, detach provider resources, or print
credentials while diagnosing a failure.

- **Setup preflight is not ready:** rerun `openstack-platform setup check
  --env-file ... --json` and inspect quota deltas, fixed-address availability,
  reserved-name collisions, tooling, ingress, and release evidence. Correct the
  protected input or provider quota; the check has not mutated OpenStack.
- **Setup stopped after mutation:** keep its workspace and rerun the identical
  apply after fixing the named dependency. Setup re-observes each checkpoint.
  Do not edit a partial server, port, volume, keypair, image, database row, or
  generated secret.
- **Project or deployment identity mismatch:** load the intended credential and
  inventory; stop before mutation. Never substitute example IDs or make compact
  and canonical UUID strings match by hand.
- **Unexpected server, port, volume, image, or host key:** reconcile exact
  ownership. Do not rename, detach, adopt, or delete the object merely because
  its name resembles the inventory.
- **A role is `ACTIVE` but not ready:** use `status`, `infra list`, and bounded
  `infra logs ROLE --lines 200`. Compare selected image, flavor, fixed port,
  volumes, metadata, and the role readiness marker. Correct the dependency or
  publish a fixed image, then use the supported replacement path; cloud-init is
  not reapplied to an existing host.
- **The operator bridge is unavailable:** check metadata only for the private
  SSH config and known-hosts files, then require
  `ssh -F "$SSH_CONFIG" platform-admin -- id -un` to return `agentops`.
  Regenerate the bridge through the matching reviewed release. Do not hand-edit
  host keys or choose a remote host from request input.
- **The hosted controller is not ready:** inspect the controller and readiness
  units through the pinned alias. Restore the matching policy/helper release or
  retained-state dependency, then restart those units. The controller has no
  public listener; a missing management UI is unrelated to controller
  readiness.
- **Public health fails:** check the exact hostname, browser-trusted
  certificate, tunnel or provider CIDRs, preserved `Host`, ingress service, and
  exact `/healthz` body in that order. A request to the ingress IP is not an
  equivalent test. Never add `0.0.0.0/0` for diagnosis.
- **Image selection or pruning is refused:** correct incomplete provenance,
  compatibility, provider ownership/status, or server image projection and
  create a new plan. Do not overwrite a tested image name or broaden the delete
  set manually.
- **A backup or restore check fails:** identify which of the four backup
  classes failed, retain its evidence, and correct the named executable,
  identity, mount, checksum, archive, schema, or integrity dependency. Rerun the
  same bounded tool; never publish staged ciphertext manually or overwrite live
  data with an unverified archive.
- **Offline restore is refused:** leave the destination unchanged. Correct the
  reported owner/mode/type, identity, deployment binding, schema, integrity,
  foreign-key, sidecar, unfinished-operation, lock, or size condition in a
  private directory and rerun the installed launcher.
- **An operation is unfinished or recovery-required:** preserve its ID, scope,
  phase, and safe error. Restore the dependency, then repeat the identical
  current command or controller request. Controller recovery requires the same
  method, path, body, and idempotency key.
- **`UNSUPPORTED_PRIOR_STATE`:** preserve and archive the state. Use a new
  namespace and empty state/backup roots; do not edit migration rows, ownership
  markers, or provider metadata to force adoption.

Record only bounded safe evidence in the private operations system: exact
non-secret identities, operation/correlation ID, phase, failed check, checksum,
and readiness result. Keep provider payloads, credentials, secret values, and
age identity contents out of logs and tickets.

## Preflight metadata before an admin image upgrade

Before an admin image upgrade, the operator must arrange the mandatory
[candidate root-path preflight](MAINTENANCE.md#preflight-controller-paths-before-an-admin-image-upgrade)
through the existing approved recovery-console administrator. Use the new
image's reviewed plan on the old host. Root is required only for private path
traversal; the check reads no credential contents and makes no changes. Defer
replacement until every expected/observed metadata refusal is resolved and the
candidate reports `root-path-preflight=ok refusals=0`. Keep the report with the
change review. No new operator sudo access is introduced.

## Storage binding fingerprint controller update

The controller update permits the public storage output names `password` and
`secret_access_key` in validated deployment bindings. It changes controller code
and requires an administrator-approved admin image replacement; merging or
building an image does not authorize deploying it. Deploy the updated image before the broker release that restores every binding
output. The portal can then bind individual PostgreSQL passwords and S3 secret
keys; PostgreSQL's DATABASE_URL already includes its password. This controller
change does not modify broker defaults on its own.

Canonical fingerprint bytes are unchanged. Existing valid accepted or in-flight
operations can be retried with the identical body and key after the controller
restart/recovery procedure. Do not retry with a changed configuration under the
old key. A request rejected before admission has no controller operation to resume;
submit it again after the controller update. In the portal, start a new deployment
request because the broker's earlier rejected intent is terminal. Operation-ref secret filtering remains
strict outside the validated public bindings.

## Owner portal operations

The portal is not deployed yet. These are the supported repository procedures
for a reviewed installation; they do not establish live acceptance. Keep broker
and controller backups before changing releases or authentication realm. Operator
controls remain outside the student UI.

Owners can list, add/replace and delete their own app's environment variables.
Values are write-only: portal reads, errors and audit records contain no values.
Edits restart a running application and take effect after health checks pass;
they do not change the saved configuration revision. The table's timestamp is
for the whole environment revision, because the controller has no per-key
updated timestamp. New environment writes are limited to 30 per owner per
minute. A value can contain at most 65,536 UTF-8 bytes; the controller also
applies the installed policy limits.

Owners can provision one PostgreSQL database, MongoDB database and S3 bucket per
application, watch progress, verify access and rotate credentials. Each output
can bind to an owner-chosen environment name or be left unbound. Targets must be
unique, unreserved and distinct from owner environment names. Save the bindings
and deploy to apply them. Rotation also requires a redeploy to pick up new
credentials. The platform's managed-data backup timer runs nightly; PostgreSQL
and S3 TLS use the platform CA through the default `PGSSLROOTCERT` and
`AWS_CA_BUNDLE` bindings. Renaming these targets requires configuring the client
to use the renamed CA path variable.

The repository root must contain `package.json`, and each selected package
directory must contain its runtime lockfile. Build and start scripts come from
the root `package.json`. Use a small health endpoint such as `/health` that returns
HTTP 2xx with a body of at most 4096 bytes; a full HTML page at `/` can fail health
verification. PostgreSQL's `DATABASE_URL` already includes the password.
Known build and health failures show release-owned guidance in intent/operation
views. Staff and admins also see a bounded internal code; owners do not. Unknown
failures retain a generic message, and controller free text is not used as
guidance.

### Manage applications as a local admin

Sign in with a local admin account and open **Manage applications**. The list
contains every broker-known application; the controller project socket cannot
list operator-created applications. To adopt one, obtain its UUID independently,
leave the owner account ID empty to use your own account (or select an active
account UUID from Accounts), and choose **Adopt application**. Adoption verifies
the controller app and imports its current accepted repository, ref, revision
and validated configuration, including storage bindings. It does not restart,
resize or recreate the app. Apps without a complete accepted snapshot must first
be deployed through the operator. Already owned apps are refused; repeating the
same request key returns the original import.

An admin can create an app for an active owner, edit any app's configuration,
set/replace/delete its environment names, provision/verify/rotate storage,
deploy exact commits, change its running state and reassign its owner. Creation
uses the target owner's app quota. Adoption and reassignment preserve existing
apps even when the recipient exceeds that quota; future creation remains
quota-limited. Adoption always requires a fresh five-minute password/TOTP
step-up. Reassignment requires that proof, the expected current owner and no
unfinished app operation. Ordinary My
applications routes remain scoped to the signed-in owner even for admins.
Environment values remain write-only for admins. No value is stored in broker
SQLite, returned in reads, placed in an audit record or cached by the React
mutation. Unknown environment writes require resubmitting the original value
and request key; their durable fingerprints retain the keyed HMAC from the
owner resource implementation. Other unknown operations reuse their original
controller key.

The deploy dialog accepts maintenance consent and an optional reviewed sizing
plan. Without a plan the controller preserves the app's accepted worker flavor,
CPU and memory, including an adopted class app's larger allocation. Plan
creation remains an operator capability: obtain a current plan through the
operator and paste its JSON. An app with a retained primary IPv4 requires
maintenance; expect brief cutover downtime after the candidate finishes building.
The broker identifies the Commons app by matching its public URL host with
`ownerPortal.commonsOrigin`. Adoption, reassignment, deploy, stop/start and storage changes display
**Portal sign-in depends on this app** and require separate confirmation. This follows the app onto owner deployment
and storage routes after reassignment; owner routes do not offer stop/start. Local
admin sign-in remains available while Commons is stopped; never rely on a
Commons session for recovery. This confirmation does not prohibit management
of the class app.

Storage deletion is available only in the admin workspace. Remove the resource's
bindings from saved configuration and deploy that configuration before deletion.
Review the app ID and resource type, verify a current managed-data backup, then
reauthenticate with password and a fresh TOTP code. Type the exact application
slug followed by a space and `postgres`, `mongo` or `s3`, for example
`student-app postgres`, in **Delete storage**. This permanently destroys the
resource and data, including a nonempty S3 bucket. The UI warns that nightly
platform backups exist; operator-assisted recovery can lose newer data. Poll the
intent to terminal success and verify the resource disappears. The broker also
refuses deletion while saved bindings reference the resource, serializes app
operations across owner/admin actors, and records a safe admin audit entry.
Owners and staff have no storage-delete or cross-owner write route.

The project controller socket now permits `DELETE /v1/storage/{id}` with its
existing machine-name confirmation and optional S3 `purge` consent. The broker
maps typed confirmation to that contract and supplies `purge:true`. Direct
recovery-console clients must use the project socket for storage deletion.
Cascade application deletion and every `/v1/admin/*` endpoint remain restricted
to the privileged socket; the broker receives neither of those capabilities.
No credential values belong in deletion tickets.

Install matched broker/web releases using protocol 3 and schema 3 together with
the approved replacement admin image. The image carries protocol-3 activation,
the unprivileged bootstrap entry point and the controller route changes above.
There are no Nix isolation changes or new directories. The unreleased schema-3
prototypes are not migration inputs: only published schema 1/2 databases migrate
to the final schema 3, and checksum mismatches fail closed.

The validated `ownerPortal` inventory section carries `enabled`, `commonsOrigin`,
optional identity egress CIDRs/class label, an optional `portalName`, and
quota/rate/session limits. The portal's brand is `portalName`, or
"`displayName` Apps" without one. The
production renderer is `openstack-platform-management-config --platform-config
PATH`. It writes prepared broker/web/identity configs below the operator-owned
setgid roots, copying validated inventory as the operator; root does not write
these inventories at boot. These mode-0640 files are available for inspection
and do not change running releases. The
installer resolves the NixOS inventory symlink and creates release-local mode-0440
config and inventory snapshots for the target component. Each service reads its
own group-readable files without development trust. Apply inventory changes by
installing both components and activating the resulting pair. Enable only after
the Commons origin is confirmed. Production
uses system CAs; no provider signing keys are used by this credential protocol.
CA updates belong to the admin image, not a per-account key rotation.

Defaults are two apps and one held external mutation per owner. Local admins
edit per-owner quotas in **Accounts**; lower limits stop new admission without
cancelling existing operations or deleting apps. Disabled apps still count.
Admin accounts have no app or concurrency limits, so their limits read as null.
Apps an admin creates for an owner still count against that owner's quota, and
every app still accepts one change at a time.
Staff/admin catalog reads enumerate only broker-known resources; global
controller administrator views remain outside this public portal.

### Bootstrap or recover a local admin

Install and activate a matched schema-3/protocol-3 broker/web pair. The admin
image must have the protocol-3 root activator: protocol-2 image code refuses the
new descriptor, so the approved admin replacement is required. The bootstrap
command is unprivileged and must run as the existing `operator` account on the
admin host. It needs no additional directories or sandbox access. Its hash-only
file is `paths.adminState/management-broker-releases/config/admin-enrollment.json`,
inside the existing operator-owned, broker-group-readable setgid directory.

Set `STATE` from inventory and resolve the active broker release. Use its
isolated entry point so the helper matches the installed broker:

```sh
BROKER_RELEASE=$(readlink -f "$STATE/management-active/current/broker")
/run/current-system/sw/bin/management-python3.14 -I -B   "$BROKER_RELEASE/runtime/openstack_platform/management/entry.py" bootstrap   --config "$BROKER_RELEASE/config/management.json"   --requirements "$BROKER_RELEASE/requirements.json"
```

The image package also exposes `openstack-platform-management-bootstrap --config
PATH`. Both forms require the operator identity; root and broker identities are
refused. They atomically replace a mode-0640 file whose group is inherited from
the prepared mode-2750 directory. Only token ID, SHA-256 token hash, realm digest,
creation time and 24 h expiry are stored. Issuing a new URL supersedes the prior
unconsumed URL. Treat the printed URL as a credential; share it only with the
intended admin. No password, plaintext token, or TOTP key belongs in inventory,
environment variables, tickets, access logs or shell history.

Open the complete `/setup#ID.TOKEN` URL in a browser. The fragment never reaches
HTTP requests or Referer; the page removes it from the address bar and POSTs it
under exact Origin and anonymous-CSRF rules. Choose a local username and a
password of at least 12 characters, at most 1024 UTF-8 bytes, excluding the
username (case-insensitive). Enroll the displayed TOTP key in an authenticator
and confirm a code. The key is shown only once and uses no external QR service.
The pending admin cannot sign in until enrollment finishes. Each token ID is
consumed when enrollment starts; replay, expiry, wrong realm and malformed files
are refused. An abandoned or exhausted enrollment needs a new operator URL and
new local username. Enrollment handles expire after 10 min and permit at most
five incorrect code confirmations.

Recovery uses the same command to create a **new** local admin. That admin can
disable the old account or issue password/TOTP reset links from Accounts.
Commons identities cannot become admin; their credentials are checked externally
and are never reset by this UI. No additional sudo or controller capability is
needed for the operator command.

### Manage accounts, roles and quotas

**Accounts** lists and searches local and Commons accounts, including role,
active/pending/disabled status, last sign-in, app count and effective quotas.
Create local owner/staff/admin accounts through 72 h single-use invitations;
copy and privately share the returned fragment URL. The portal sends no email.
Local owner/staff recipients may enable TOTP; admins must enroll it. A staff
account can manage its own apps and read the catalog in the same session.
There is no sign-in mode selector, impersonation or role upgrade in a session.

Changing role, enabling/disabling, revoking sessions, or issuing/resetting local
credentials increments generation and deletes all of the account's sessions and
outstanding account/enrollment links. A new login receives its current DB role.
Admin promotions without a confirmed TOTP factor become pending and receive a
fresh enrollment link. Common accounts may be owner or staff, never admin.
Quota edits use 0–1000 apps and 0–16 concurrent operations; zero stops new
admission. They do not reassign ownership or change controller resources.

Role/enable/disable/session-revoke and reset actions, plus creation of another
local account (owner, staff or admin), and issuing any invitation require password
and a fresh TOTP code validated within five minutes.
Use **Confirm a sensitive account action** when prompted. Codes cannot be reused
within their accepted counter/window; wait for a fresh code if you just enrolled
or signed in. Admin sessions are capped at 1 h absolute/15 min idle. Local login
also has the existing five-failure username/address window and per-source
admission at 12 attempts/minute per lane. Wrong passwords have a shared
20-failure rolling-hour budget across all sources; once it is exhausted, use a
recognized browser. Successful local sign-in retains a signed HttpOnly, Secure,
SameSite=Strict known-device cookie for 90 days. New cookies carry a random device
ID with at most 20 failed credential attempts in a rolling hour. While that
process-local allowance is available, it exempts the account password budget;
it never replaces the password or factor or bypasses account-wide TOTP limits.
Refreshing the cookie preserves its ID and does not reset failures. Known-device
login allows one in-flight request and 12 attempts/minute per account across all
addresses. Admin step-up has its own hash slot and address/failure budget.
A role change, reset, disable or other generation bump invalidates recognition.
Explicit logout clears it. Older cookies remain recognized until expiry but
have no device ID, so they do not exempt the account password budget; successful
local login upgrades them. If the cookie is absent/expired after a flood, wait for the
hourly window or use the supported reset/recovery workflow; anonymous guessing
cannot invalidate an existing device cookie.

Only failures after the correct password affect TOTP: an account-wide 30-second
backoff doubles to one hour, with at most 10 failures in a rolling hour, shared
by login and step-up regardless of source or device recognition. Success clears
the exponential delay without reopening hourly windows. Step-up has reserved
hash capacity but still needs the same password budget/device exemption and
TOTP checks. In-memory account/device admission limits reset on broker restart;
the durable account-wide guessing limits remain. Saturated device tracking falls
back to the account password budget rather than granting an unbounded exemption.
Deploy this follow-up as matched portal releases: it changes neither schema nor
compatibility and needs no admin replacement. A generic credential error
covers invalid accounts, passwords, factors and backoff; throttling/capacity
errors have no credential detail.

Issuing a password reset immediately clears the old password hash. Issuing a
TOTP reset clears the old secret and makes the account pending, even when MFA
was optional; old credentials and password-only fallback cannot sign in.
Password-reset links require the existing TOTP factor when enabled and are void
after five wrong codes. Codes are checked before password hashing. Enrollment
finish does not supply step-up unless a fresh code was verified in that finish
request; use the step-up form afterward when needed. TOTP-reset
links enroll a fresh factor but do not issue a session: sign in with the password
and a fresh code afterward. To recover both factors, the new admin can issue a
TOTP-reset link first, complete its enrollment, then issue a password-reset link.
Issuing a second reset voids any outstanding earlier reset link. Resetting credentials does not
silently enable a disabled account. **Admin audit** records safe actor/target IDs,
action, generation/role/quota metadata and timestamps, never links or secrets.

Break glass is the operator bootstrap command, not the former offline staff-grant
helper. Keep a verified broker backup before recovery or release migration.
A restore retains roles and credential/factor state, increments generations and
deletes all sessions, invitations and pending enrollment handles. It also fences
off every pre-restore operator enrollment file by issuance time, and rejects the
current TOTP window to prevent replay from a rolled-back counter. Wait for a fresh
code after restore, and issue a new operator URL if recovery is needed. Upgrading
a restored v1/v2 backup to schema 3 also fences old enrollment files at migration
time; issue enrollment URLs after the broker has completed migration.

### Review staff reads and recover audit capacity

Staff reads record actor ID, correlation UUID, fixed route, validated resource/
filter IDs, page bound/cursor presence, row count, outcome/status, stale flag and
time in `staff_read_audit`. They never record passwords, cookies, CSRF, raw
queries, response bodies, logs or controller operation references. Account
changes have separate `admin_audit` evidence. Read audits are private to
recovery access; the staff portal exposes no audit endpoint.

Read-audit retention is 30 days. Read traffic triggers bounded pruning batches
of at most 1000 expired rows after the daily interval; if a batch is full, later
reads continue pruning. Inactive databases can retain expired rows until the
next maintenance/read. At 2 million retained read-audit rows, or when a read audit
cannot commit, staff reads return 503 without metadata. The audit row cap does
not deny owner APIs; a shared DB/disk failure can affect both modes. Repeated
throttled denials are aggregated, not written on
every retry. Encrypted backups retain their own history independently.

If the cap is reached, stop admissions/backups using the procedure above. Export
the private audit rows to the approved private recovery evidence destination.
Prune reviewed/expired rows in one broker-identity SQLite transaction and
recompute `staff_read_state.row_count` with `SELECT COUNT(*) FROM
staff_read_audit`; do not reset the counter without removing/exporting the rows.
Set `pruned_at` to the maintenance timestamp, verify integrity/counts and take a
new backup before reopening. Never delete recent evidence merely to admit more
automated staff requests. Staff throttling returns 429, dependency/audit failure
503, with a bounded 30-second retry delay; wait for that delay before retrying.

The fourth backup class is `management-broker`: consistent SQLite online backup,
age encryption with the hosted-controller escrow recipient, committed checksum/
manifest last, and four-class off-site v2 evidence. Legacy v1 bundles remain
readable. An active or staged broker selection requires this class for healthy
export/status, including a broken selector whose release files disappeared.
Checking both keeps recovery evidence mandatory before first activation and
while staging changes independently of an active pair. The
broker HMAC key is not restored; sessions and CSRF are removed. Schema-3 restores
retain roles, increment generations, delete all sessions/account/enrollment tokens
and fence off pre-restore operator enrollment files. To replace state,
stop identity/broker/web, their activation/path units and broker backup timer/
service. Decrypt to broker-owned mode-0600 `management-broker/restore-input.sqlite3`
and use `openstack-platform-management-broker-restore --yes` from the recovery
console. It checks the mounted state volume and stopped units and invalidates
authentication. The next broker startup generates a new private anonymous key,
which also changes the domain-separated environment fingerprint subkey. An
identical environment edit retried with its pre-restore request key returns
`IDEMPOTENCY_CONFLICT` and asks for a new request key. Accepted operations still
poll to completion without their values; unknown or recovery-required
environment intents keep their application scope held and require administrator
reconciliation before a new edit can proceed. Do not force those intents to
success or discard an uncertain controller operation merely to release quota.
Non-secret storage/configuration fingerprints are unaffected by the key change.
Take a new backup after verification; do not manually publish staging ciphertext.
Portal availability is independent of the backup mount.

Install reviewed broker then web archives using the explicit modes described in
maintenance. Each candidate smokes before its staged `current` selection changes;
failed installation preserves prior configuration bytes. The operator marker
identifies a commit and pair. The root oneshot validates both staged descriptors,
complete markers and identical inventory snapshots under the install lock before
atomically selecting `management-active/current`. It then restarts the three
fixed identity/broker/web units; services gain no systemctl rights. One-sided
installation, a mismatched marker, restart or reboot cannot select a mixed pair.
Keep prior complete releases, config snapshots and signed evidence. If activation fails, retain safe
unit/operation IDs and inspect readiness/configuration/TLS/group/compatibility,
without capturing credential bodies. An unknown deploy outcome repeats its
original controller key or polls the recorded operation.

### Portal startup and controller restarts

The broker requires and starts after the existing controller-readiness service,
which probes the project API only after the controller starts. This also ensures
both socket paths exist before the broker constructs its mandatory sandbox.
Controller restarts propagate through readiness to the broker and then web using
`PartOf`, with the original dependency ordering. Broker/web process failures
retry after two seconds. Path units retain their existing trigger limits and
ordering; no sandbox or socket access is relaxed. These unit changes are carried
by the admin image and need a future approved replacement to deploy.

### Reactivate a retained portal pair

Stop admissions and back up the broker/controller databases first. Review the
actual database migration evidence and the retained pair's authentication realm.
The command supports schema 3 and protocol 3 with controller API 1; it refuses
schema-incompatible retained or active descriptors. It cannot inspect the
broker's private database as the operator. An incompatible database needs an
explicit forward repair or verified offline restore, which invalidates sessions
and may require reconciliation of controller/ownership backup skew.

Use the existing admin image command as the unprivileged operator. Set STATE
from the reviewed inventory's `paths.adminState`, NS to its namespace, and each
release variable to a complete directory basename under that component's
`releases` directory (commit-pair-prefix-inventory-hash, without slashes):

```sh
STATE=/srv/app-platform-state
NS=your-installed-namespace
BROKER_RELEASE=REVIEWED_BROKER_RELEASE_BASENAME
WEB_RELEASE=REVIEWED_WEB_RELEASE_BASENAME
openstack-platform-management-reactivate \
  --platform-config "/etc/$NS/platform.json" \
  --broker-release "$BROKER_RELEASE" --web-release "$WEB_RELEASE"
```

The command verifies retained signatures/channel policy, complete markers,
payload hashes/layout, groups/modes, matching descriptors/config snapshots and
deployment identity, then smokes both candidates without a live database or
network call. It keeps the retained inventory/config bytes; reinstalling an
archive would instead snapshot the current inventory. Validation failure leaves
selectors and the activation request unchanged. Success changes both staged
selectors under the install lock and writes the normal mode-0640 marker;
`management-reactivation=requested` does not establish successful activation.
The fixed root oneshot still validates and selects the pair.
Dangling or incomplete active selections and duplicate descriptor keys are
refused before a new request can bypass active-schema review.

After the activation request completes, verify the active selections and unit
results from the recovery console, then check broker/web readiness and take a
new backup before reopening admissions:

```sh
test "$(readlink -f "$STATE/management-active/current/broker")" = \
  "$STATE/management-broker-releases/releases/$BROKER_RELEASE"
test "$(readlink -f "$STATE/management-active/current/web")" = \
  "$STATE/management-web-releases/releases/$WEB_RELEASE"
systemctl show "$NS-management-activate.service" -p Result
systemctl is-active "$NS-management-broker.service" "$NS-management-web.service"
```

If the active paths did not change, inspect the activation unit's result and
fixed unit logs without credential bodies. Repair mismatched/missing evidence,
unsafe ownership, configuration or compatibility before requesting activation
again. Keep the prior active pair until readiness succeeds; services have no
systemctl capability. These procedures remain unaccepted on a live host.
