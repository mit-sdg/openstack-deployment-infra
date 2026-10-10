# Migrate shared databases to isolated instances

This procedure retains the live PostgreSQL 17 and MongoDB 8 data while moving each
resource into its own instance on `xl.16core` (16 vCPU / 64 GiB). Controller
migrations 7 and 8 add instance/usage state and the durable worker allowlist cache.
The storage replacement has the accepted host outage; subsequent copies stop one
app at a time. Budget export + restore + verification + restart: usually seconds
to minutes for small databases, with a configurable 1800-second deadline **per app**.
Measure the throwaway rehearsal before scheduling the class app. Local Nix evaluation checks the configuration; the storage/admin VM tests in CI
exercise Podman, nftables and the native database tools. No live migration was
performed by the backend worker.

For the local database regression check, with rootless Podman and idle loopback
ports 5432, 27017, 30000 and 30001, run:

```bash
uv run python tests/run_storage_databases.py
```

It creates and removes four throwaway PostgreSQL 17/MongoDB 8 containers and runs
native backups/restores, rotation with existing data, block/unblock permissions,
authenticated migration copy/abort/replay, limits, collector caching, privileged
repair and deletion. It adapts TLS and host lifecycle for local execution;
the storage VM test additionally verifies TLS, systemd, cgroups, quotas and firewall.

## Prepare capacity and releases

Use a reviewed checkout on the operator host. Set these paths to the existing
installation, and keep the inventory/policy mode 0600:

```bash
export PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json
export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
export PLATFORM_ROOT=/srv/openstack-platform
export OSC=/srv/openstack-platform/bin/platform-openstack
```

1. Verify the new storage and admin VM tests in CI, resolve unfinished foreground
   and finishing operations, and take hosted-controller/operator backups using
   [the existing backup procedure](backups-and-recovery.md#verify-backups-and-schedules).
   Retain both escrowed age identities. Check shared directories on the storage
   recovery console before the first instance reservation:

   ```bash
   NAMESPACE=<namespace>
   DATA=$(jq -r .paths.data "/etc/$NAMESPACE/platform.json")
   sudo du -sx --block-size=1 -- "$DATA/postgres" "$DATA/mongodb" \
     "$DATA/registry" "$DATA/object-storage"
   ```

   PostgreSQL/MongoDB sources must fit their temporary 32 GiB projects, registry
   its 100 GiB project, and Garage max(125% of bucket byte quotas, 4 KiB/object) +16 GiB overhead. S3 object
   reservations total at most five million, matching fifty default 100,000-object quotas
   and the bounded streaming exporter.
   Leave at least 384 MiB inside each initial cap. The manager refuses the initial
   assignment if a directory is already too large. Enlarge the corresponding code
   reservation and volume before rollout if necessary. Source usage must fit each
   instance's 150% hard quota with 384 MiB PG /128 MiB Mongo headroom. A database
   over 14 GiB needs a larger migration archive project than the current 16 GiB.

2. Change the operator inventory and the **complete** production CI inventory:

   ```bash
   umask 077
   jq '.flavors.storage="xl.16core" | .volumes.data.sizeGiB=1024 |
       .volumes.backup.sizeGiB=8192' "$PLATFORM_CONFIG" > "$PLATFORM_CONFIG.new"
   mv "$PLATFORM_CONFIG.new" "$PLATFORM_CONFIG"
   gh secret set PLATFORM_CONFIG_JSON --repo mit-sdg/openstack-deployment-infra \
     --env openstack-images < "$PLATFORM_CONFIG"
   ```

   These commands are operator rollout actions, not commands run by this worker.
   Preserve all other inventory fields, including worker/builder flavors, addresses,
   namespace/prefix and `internalNames.storage`. The last name must match the
   storage certificate SAN; new PKI generation includes it explicitly and refuses
   a reused leaf missing that SAN. This check is a hard prerequisite. Verify:

   ```bash
   STORAGE_DNS=$(jq -r .internalNames.storage "$PLATFORM_CONFIG")
   openssl verify -CAfile "$PLATFORM_ROOT/.secrets/setup/pki/internal-ca.pem" \
     -verify_hostname "$STORAGE_DNS" "$PLATFORM_ROOT/.secrets/setup/pki/storage.pem"
   ```

   If changing that name, regenerate leaf certificates with the existing CA using
   `infra/pki/generate_internal_pki.sh` in a fresh private directory containing the
   escrowed CA certificate/key; install the resulting storage leaf/key as the
   replacement's PKI inputs. Keep the CA unchanged. Standard existing storage names
   already appear in the SAN; no new secret is required.

   Verify the actual flavor and available project quota before replacement:

   ```bash
   "$OSC" flavor show xl.16core -c vcpus -c ram -c disk
   "$OSC" quota show "$(jq -r .projectId "$PLATFORM_CONFIG")"
   ```

   Require 16 vCPUs /65536 MiB. Replacement retains the old storage VM until
   acceptance, so storage alone temporarily needs 20 vCPUs /72 GiB with the
   current 4-vCPU/8-GiB predecessor, plus the other hosts/workers and both boot
   disks. Cinder quota must cover the 1 TiB data and proposed 8 TiB backup volumes.

3. Extend the existing Cinder data volume before replacing storage:

   ```bash
   DATA_VOLUME=$(jq -r .volumes.data.name "$PLATFORM_CONFIG")
   "$OSC" volume set --size 1024 "$DATA_VOLUME"
   "$OSC" volume show "$DATA_VOLUME" -c size -c status
   ```

   Wait for size 1024 and the extension to finish. In-use extension needs Cinder
   backend support; otherwise perform it while detached during the replacement
   maintenance window. Do not force its state to available. The new image runs
   idempotent `xfs_growfs` after mount and **before** shared services or the manager,
   so the larger filesystem requires no manual storage-root work. If the volume
   is extended after boot, rerun growth on the storage recovery console:

   ```bash
   sudo systemctl restart "$NAMESPACE-storage-growfs.service"
   ```

   Check `df -h "$DATA"` before continuing. Fifty default
   logical quotas total 450 GiB. Physical admission reserves 300 GiB DB hard caps,
   312.5 GiB S3, and 197 GiB fixed allowances = **809.5 GiB**; 500 GiB is insufficient.
   One TiB's 85% admission/warning boundary is 870.4 GiB. Registry's 100 GiB is a
   2 GiB/app allowance; arbitrary image histories have no finite worst case.

   The existing 600 GiB backup volume does not hold fourteen full 450 GiB sets.
   At full defaults, fourteen sets plus staging need roughly 6.6 TiB before
   compression; provision at least 8 TiB, and comparable offsite capacity. Extend
   the existing backup volume with `"$OSC" volume set --size 8192 <backup-volume>`;
   have its mounted filesystem grown using the existing admin-volume procedure.
   Automatic growth added here is specifically for the storage **data** filesystem.

4. Apply the existing foundation reconciler from this reviewed checkout:

   ```bash
   set -a
   source "$PLATFORM_ROOT/.secrets/openstack.env"
   set +a
   export OS_PROJECT_ID=$(jq -r .projectId "$PLATFORM_CONFIG")
   export OS_PROJECT_NAME=$(jq -r .project "$PLATFORM_CONFIG")
   "$PLATFORM_ROOT/runtime/python3.14" infra/openstack/apply_foundation.py
   "$PLATFORM_ROOT/runtime/python3.14" infra/openstack/apply_foundation.py --apply
   "$OSC" security group rule list "$(jq -r .prefix "$PLATFORM_CONFIG")-storage"
   ```

   The reconciler adds ingress TCP **30000–30999** to the storage group from the
   `<prefix>-worker` and `<prefix>-admin` security groups. It retains existing
   rules/ports and never deletes resources, so this is safe while apps serve.
   No CIDR opening for student traffic is needed; nftables narrows each allocated
   port to verified app worker addresses. These ports are also reserved from the
   host ephemeral allocator.

5. Replace storage using the reviewed image and protected inputs documented in
   [Replace the storage host](hosts-and-images.md#replace-the-storage-host):

   ```bash
   export OPERATOR_PUBLIC_KEY="$PLATFORM_ROOT/.secrets/ssh/id_ed25519.pub"
   export STORAGE_SECRETS_FILE="$PLATFORM_ROOT/.secrets/setup/storage-bootstrap.env"
   export PKI_DIR="$PLATFORM_ROOT/.secrets/setup/pki"
   "$PLATFORM_CLI" infra image set storage <NEW_STORAGE_IMAGE_UUID>
   "$PLATFORM_CLI" infra replace storage --yes
   ```

   Require successful growth, nginx, authenticated manager and shared DB readiness.
   Sources remain at 4 cores /8 GiB each. During transition, normal instances admit
   36 GiB, with 2 GiB maintenance headroom: source + shared + restore caps total
   at most 60 GiB, leaving 4 GiB for OS/control/cache. Removing the sources later
   allows raising the normal instance budget to 50 GiB, enough for fifty default
   pairs; until then admission safely rejects capacity beyond 36 GiB.

6. Replace admin with the matching image, install the accepted helper release,
   and ship portal broker/web together. Follow
   [release installation](releases-and-upgrades.md#install-operator-and-helper):

   ```bash
   commit=<FULL_REVIEWED_COMMIT>
   export PLATFORM_RELEASE_MANIFEST=/private/releases/$commit/release-manifest.json
   export PLATFORM_RELEASE_SIGNATURE=/private/releases/$commit/release-manifest.sig
   export PLATFORM_RELEASE_TRUST_ROOT=/private/release-trust-root.pem
   deploy/releases/deploy_helper_release.sh "$commit"
   ```

   The image includes PostgreSQL 17 clients, MongoDB tools, age, Podman, XFS/nft,
   and the pinned Python dependencies; no apt or ad-hoc Nix installation on admin
   is needed. Reuse existing storage-bootstrap, Garage admin/backup key, Nomad
   tokens, CA and managed-data age identity. The manager creates private per-instance
   admin passwords itself. Replacing admin alone leaves the durable old helper
   selected; install the matching release before sending new actions.

   New policy fields are optional: `limits.migrationBackupMaxAgeMinutes=60` and
   `limits.migrationAppSeconds=1800` (bounds 1–1440 and 120–7200). For larger apps,
   add them to both operator and hosted-controller policies using the existing
   protected-policy installation procedure. The per-app deadline starts after the
   backup gate and covers quiescence, both copies, verification and restart. CLI polling defaults to 7200;
   it is independent of each app's deadline. Existing process/helper defaults can
   remain unchanged.

## Back up, rehearse and cut over

On admin as agentops, run the new managed backup and the existing actual restore
check before migration:

```bash
openstack-platform-managed-backup
PLATFORM_CONFIG=/etc/<namespace>/platform.json \
  /srv/openstack-platform/persistent/platform/infra/backup/verify_latest_restore.sh
```

Require `latest platform restore=verified`, the new format-4 catalogs, and healthy
backup/offsite status. Every run backs up shared sources **and** accepted isolated
instances; failed inventory, credentials, native dumps or instance access fail the
whole set and alert. Encryption, fourteen-day pruning and offsite export are unchanged.
No recurring manual instance snapshot precondition replaces these logical backups.

Rehearse, then migrate named apps:

```bash
openstack-platform-storage-migrate --application <REHEARSAL_APP_UUID>
openstack-platform-storage-migrate --application <CLASS_APP_UUID>
openstack-platform-storage-migrate
```

Repeat `--application UUID` for multiple named apps. Save each printed request UUID.
Replay the same UUID **and the same selection** after an interrupted attempt:

```bash
openstack-platform-storage-migrate --request-id <UUID> \
  --application <CLASS_APP_UUID> --timeout 7200
```

Before each resource starts/resumes cutover, a successful source backup within the
configured age is required. The controller reads only an operator-owned,
mode-0640 receipt from `<backups>/<namespace>-migration-receipts`; it cannot read
backup payloads or the age identity. A missing/stale receipt triggers the
operator-owned `<namespace>-resource-backup@<postgres|mongo>-<database>.service`.
Polkit allows the controller to start only those validated backup units. Each
checkpoint dumps exactly that shared resource, age-encrypts it with the existing
managed-data identity, decrypts/verifies the archive, and publishes a receipt after
fsync. No Garage export or unrelated instance dump runs in this gate. It has a
separate one-hour bound while the app keeps serving on an initial migration;
quiesced replays stay quiesced until completion or abort. The 1800-second app budget
is refreshed after the gate. Supplemental checkpoints remain private under
`<backups>/<namespace>/migration-checkpoints` and are pruned after fourteen days,
including by the nightly job. Nightly complete managed-data sets retain the existing
offsite export; these local cutover checkpoints supplement them.

To inspect or retry a failed checkpoint on admin (database is the admin-only
`providerName`, such as `p_<20 hex characters>`):

```bash
NAMESPACE=<namespace>
DATABASE=<providerName>
sudo systemctl start "$NAMESPACE-resource-backup@mongo-$DATABASE.service"
sudo journalctl -u "$NAMESPACE-resource-backup@mongo-$DATABASE.service" -n 30
```

Use `postgres` for PostgreSQL. Failed checkpoints alert through existing backup
health; a successful retry clears that checkpoint's failed status. Frozen
source backups preserve the original app login/role for restore. An app stops once;
source logins are frozen; target data is copied, counted and cheaply checksummed,
sealed, then published. The accepted Nomad job is upgraded with the inventory DNS
host mapping and resubmitted so new binding values reach stable or candidate workers.
Stopped/never-deployed apps stay stopped. PostgreSQL extension DDL imports separately as admin;
app schemas/data/constraints import as the app owner, through the actual app login
(all app objects become owned by its owner role). Import sessions disable statement,
lock and idle-in-transaction timeouts; admin temporarily lifts only the unpublished
login's temp-file cap and restores 256 MiB in `finally`. Publication waits for this
cleanup. The instance memory/CPU/XFS caps still bound the import.
Mongo gets a temporary minimum 1 GiB cap and serial restore workers from the separate
2 GiB maintenance reserve. Disconnected copies cancel their process group; replay
cleans orphaned copies instead of waiting behind their instance lock.

CPU/memory limits may be raised prospectively on a shared resource before migration
so a large app can receive its intended instance cap; shared connections still require
migration before a raise.

Before publication, abort an interrupted app if returning to the shared source is
necessary:

```bash
openstack-platform-storage-migrate --abort --application <APP_UUID>
```

Replay an interrupted abort using its printed UUID and `--abort` plus the same app
selection. It restores PostgreSQL LOGIN/the checkpointed Mongo role, prevents old
copy attempts from publishing, restarts the accepted shared job, and releases the
app reservation. It retains target and source data; a later explicit resource deletion cleans both. A partial abort skips those apps
when the original master resumes; use a **new** migration intent to migrate them later.
After publication, abort refuses `MIGRATION_ALREADY_PUBLISHED`: replay the original
migration. Switching back then would lose writes made to the isolated instance.

After all apps migrate, verify admin resource isolation/ports, working bindings,
fresh usage and no unexpected connection/availability/memory/disk alarms. Run:

```bash
openstack-platform-storage-repair
openstack-platform-managed-backup
```

Repair reapplies the four role timeouts and current connection caps. It also repairs
old shared IP-based PostgreSQL bindings after installing the container DNS mapping;
use it earlier if a shared Node app needs that repair before its own migration.
Database URLs use the existing certificate DNS name on both shared and isolated ports.

## Recover a resource or a lost storage host

Use [managed-data restore and the full-loss drill](backups-and-recovery.md#check-and-restore-managed-data).
Format 4 restores users/roles and encrypted owned bindings, provisions missing isolated
instances at recorded ports, verifies data inventory, remaps the private offline
controller DB, then verifies current workers before reopening their firewall access.
Keep the hosted controller stopped while installing/restoring its offline state;
existing unrelated worker apps can keep serving. A completed identical payload replay
skips destructive import. Keep the target app stopped until verification succeeds.
Restore terminates existing target PostgreSQL sessions and replaces the target
MongoDB database, including collections absent from the snapshot. Accepted instance
limits remain authoritative: PostgreSQL role/database connection caps are reapplied,
and MongoDB write access is reconciled against restored usage and the current size
limit before worker access opens.
Mongo backup inventory includes databases with app users and no collections, so a
new resource or an app that dropped its last collection retains backup coverage.
PostgreSQL sessions authenticate as the capped login (`session_user`) and use the
stable NOLOGIN owner (`current_user`) for object creation. Rotation, repair and
restore reassign older login-owned objects to that owner within the resource's
database before applying the shared role settings. This keeps tables, functions
and DDL access available after credential rotation. Run the existing one-time
PostgreSQL repair after rollout to normalize current resources.

For a single resource, decrypt the provider archive into the native restore CLI:

```bash
age --decrypt --identity /escrow/managed-age-identity.txt /backup/postgres.age | \
  PLATFORM_CONFIG=/private/replacement-platform.json \
  openstack-platform-storage-backup restore --type postgres \
    --resource <RESOURCE_UUID> --controller-database /private/offline-controller.sqlite3 \
    --catalog /backup/mongodb-catalog.json
```

Use `mongodb.age`, `--type mongo` and `--catalog /backup/postgres-catalog.json` for MongoDB. The other provider's catalog reserves its archived ports against new shared-resource instance allocation. The controller file must be a direct,
current-user-owned mode-0600 offline database without SQLite sidecars. Install the
remapped controller state through its existing supported restore launcher before
starting it. A tracked shared pre-migration resource restores into its current or a fresh isolated
instance; old untracked source copies stay retained in the encrypted archive. Rebuild missing app images after host loss; arbitrary runtime environment
values still come from escrow, while managed DB binding credentials are in the backup.

A format-4 `--full` loss drill compares actual restored-instance counts with the
managed entries in both catalogs. An absent engine records `no-managed-resources`;
any present engine must restore all its managed databases. It does not require
keeping a throwaway PostgreSQL resource. The `isolatedDatabases` evidence records
both the state and restored-instance count. CI's storage
VM test also deletes/recreates/restores both engines, changes their limits, copies
both shared sources and checks app credentials, data and stale-target cleanup.

A hard-cap crash can require an admin size raise even with minute polling and 50%
headroom; logical and physical bytes differ. Raising the quota applies before DB
connection attempts and collectors retain no failed app reservation. Reductions need
fresh logical usage plus margin. The manager measures physical allocation while
serving and rejects unsafe reductions before stopping. An admitted reduction stops
cleanly, rechecks allocation to cover concurrent writes, applies the quota, and starts
again. Memory/connection edits restart that instance; CPU edits and disk raises apply
live. The first limits edit or repair of a saved job with an old DNS mapping stops
that app, publishes the binding, then redeploys with the host mapping once. Interrupted
redeploys resume from the applied checkpoint without repeating the stop or limit edit. Stops use
PG fast SIGINT /Mongo SIGTERM, mixed kill mode and a 180-second grace period.

The follow-up removes shared containers, frozen databases/users, legacy listeners and
source reservations; adjusts monitoring/backups to absent shared services; and raises
normal/maintenance instance memory budgets to 50/52 GiB. This rollout retains sources
and never deletes their data automatically. Order matters: old storage lacks manager
support; old helpers reject new actions; unmatched portal/controller quota models fail;
and migration must wait for the new backup/restore coverage and successful rehearsal.
