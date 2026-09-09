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
predecessor absence and never overlap workers. Enable also refuses to substitute the port while an old
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
| Hosted controller | Admin controller SQLite | `<paths.backups>/hosted-controller` | Operator escrow; not admin |
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
account; the private age identity remains off-platform.

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
to admin.

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
sudo openstack-platform-hosted-controller-restore --yes
sudo systemctl start \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-hosted-controller-backup.timer"
```

The launcher refuses active controller/backup units and unsafe input. It
validates deployment identity, complete known schema, SQLite integrity, foreign
keys, and unfinished operations before atomic replacement. On refusal, the
current database remains unchanged.

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
- **A backup or restore check fails:** identify which of the three backup
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
