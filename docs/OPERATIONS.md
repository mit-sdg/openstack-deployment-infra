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
[Size an application](#size-an-application).

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
counts; the operator CLI cannot inspect or mutate those records.

An unavailable live observation does not erase accepted state. Diagnose the
named dependency before mutation; do not edit SQLite or provider resources to
make status appear healthy.

## Size an application

Use the controller's privileged Unix API to select one application's worker
flavor. This does not change the platform's student defaults. The infrastructure
CLI has no product-mutation commands.

Run these commands **locally on admin**, as the operator UID/GID admitted to
`privileged.sock`. Install matching controller and helper releases first; the
helper must provide `app.worker.capacity`. You need `curl`, `jq`, and Python.
No direct SQLite writes or OpenStack server resize commands are supported.

### Plan and resize an accepted application

The app must be enabled and have an accepted deployment. Replacement needs quota
for both the old worker and the target worker/port simultaneously. Worker disks
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

Review `applicationId`, `deploymentId`, `current`, `flavor`, and `reserve`.
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

### Sizing failures and retries

- **Plan drift or invalid confirmation:** no candidate is created. Obtain and
  review a fresh plan, then submit a new request/key.
- **Candidate scheduler, application, or public health failure with confirmed
  cleanup:** the operation is `failed`; the prior accepted size and route remain.
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
use a new reviewed sizing operation. Disabled accepted apps must be enabled
before resize. Plans do not reserve quota, and the platform does not copy local
disk state or resize managed-storage quotas. These examples describe the API;
production provider behavior still requires a release acceptance exercise.

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

Before replacing storage, require a fresh managed-data `RESTORE-MANIFEST`.
Before replacing admin, require fresh hosted-controller and operator-state
backups. Publish and live-test the replacement role image before selecting its
exact UUID.

```bash
$PLATFORM_CLI infra image set ingress NEW_INGRESS_IMAGE_UUID
$PLATFORM_CLI infra replace ingress --yes
$PLATFORM_CLI infra logs ingress --lines 200
```

Use `admin`, `ingress`, or `storage`. Replacement retains the current host,
fixed port, and volumes until the candidate passes readiness and exact
image/flavor/name/provenance checks. On readiness failure it restores the old
host. An ambiguous provider result becomes recovery-required; restore the named
dependency and rerun the same command. Never delete the old server or detach a
volume manually.

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
