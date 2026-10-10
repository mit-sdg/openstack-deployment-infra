# Migrate shared databases to isolated instances

This is the **one-time migration** from shared PostgreSQL and MongoDB servers to one database server per resource. It was completed for all live apps in production by 2026-10-10. New resources already use isolated instances; do not repeat this procedure as routine maintenance.

An operator still needs it when an older installation has shared database resources, or when recovery leaves a tracked resource on a shared server. Check the controller's storage inventory first. The shared production servers still run with sealed fallback copies; this procedure retains those copies. Removing shared servers is a separate future change.

Each isolated resource runs in a Podman container under `<namespace>-database@<instance-id>.service` on storage. The instance has its own CPU, memory, connection, and XFS disk quota, plus a firewall allowlist for its app's workers. S3 continues to use one shared Garage service with a separate bucket and restricted key per app; S3 is not part of this migration.

## Prepare capacity and releases

Run operator commands from a reviewed checkout on the operator host. Admin commands run as `agentops`; root commands use the recovery console. Set the shell variables and `recovery` function from [Run the platform](run-the-platform.md#set-up-your-shell).

1. Pause app changes and resolve unfinished foreground and finishing operations. Follow the [release checklist](releases-and-upgrades.md#roll-out-a-release) for the operation checks. Keep both escrowed age identities and take fresh hosted-controller, operator-state, and portal backups using [Verify backups and schedules](backups-and-recovery.md#verify-backups-and-schedules).

   **Success:** no unfinished operation remains and each required backup has its checksum and manifest. If a backup or operation check fails, resolve it before replacing hosts or migrating data.

2. Check whether any PostgreSQL or MongoDB resource still needs migration. Run on admin:

   ```bash
   NS=<namespace>
   SOCKET="/run/$NS-controller/privileged.sock"
   curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
     'http://localhost/v1/admin/storage?limit=100' > storage.json
   jq '.items[] | select((.type == "postgres" or .type == "mongo") and .isolation == "shared")
     | {applicationId, resourceId, type, providerName, lifecycleState, migrationState, quotas, usage}' \
     storage.json
   jq -r '.nextCursor // empty' storage.json
   ```

   If `nextCursor` prints an ID, fetch the next page with `&cursor=<that ID>` and repeat. **Success:** you have an exact list of shared resources and their apps. If no database resource is shared, no migration is needed. S3's `shared` isolation is expected.

3. Confirm storage capacity before reserving instances. On the storage recovery console:

   ```bash
   NS=<namespace>
   DATA=$(jq -r .paths.data "/etc/$NS/platform.json")
   sudo du -sx --block-size=1 -- "$DATA/postgres" "$DATA/mongodb" \
     "$DATA/registry" "$DATA/object-storage"
   df -h "$DATA"
   ```

   The current manager reserves 32 GiB each for shared PostgreSQL and MongoDB, 100 GiB for registry, and 16 GiB for migration archives. Initial shared-directory assignment requires another 384 MiB of free room within each fixed cap. Garage reservation is the larger of 125% of bucket byte quotas and 4 KiB per reserved object, plus 16 GiB overhead. Total disk reservations must fit within 85% of the data filesystem.

   Each database's hard quota is 150% of its logical size limit, rounded up to a whole MiB. Migration requires source usage plus 384 MiB for PostgreSQL or 128 MiB for MongoDB to fit that quota. A resource over 14 GiB is refused until the 16 GiB migration archive reservation is increased in reviewed code. The instance memory budget remains 36 GiB while shared servers are retained, with 2 GiB of separate maintenance headroom.

   **Success:** source sizes, instance caps, archive space, memory, and total reservations fit. If they do not, raise the affected resource limit or provision capacity before migration. Use [host resizing and replacement](hosts-and-images.md#resize-a-persistent-host) for infrastructure changes. Changing inventory volume sizes alone does not extend Cinder volumes.

4. Confirm compatible storage/admin images, the matching helper, and a matched portal pair are installed. The storage image must provide the instance manager, database units, XFS project quotas, and instance firewall. The admin image must provide format-4 backups and the migration commands. Follow [Releases and upgrades](releases-and-upgrades.md#roll-out-a-release) and [Replace the storage host](hosts-and-images.md#replace-the-storage-host) when these are absent.

   The foundation needs TCP 30000–30999 from worker and admin security groups to storage. From the reviewed operator checkout, with the protected OpenStack environment loaded:

   ```bash
   export OSC=/srv/openstack-platform/bin/platform-openstack
   set -a
   source /srv/openstack-platform/.secrets/openstack.env
   set +a
   export OS_PROJECT_ID=$(jq -r .projectId "$PLATFORM_CONFIG")
   export OS_PROJECT_NAME=$(jq -r .project "$PLATFORM_CONFIG")
   /srv/openstack-platform/runtime/python3.14 infra/openstack/apply_foundation.py
   /srv/openstack-platform/runtime/python3.14 infra/openstack/apply_foundation.py --apply
   "$OSC" security group rule list "$(jq -r .prefix "$PLATFORM_CONFIG")-storage"
   ```

   Review the plan before `--apply`. The reconciler adds rules without deleting existing resources; per-instance nftables rules restrict each database port further. Do not open the range to arbitrary client addresses.

   Verify the storage certificate's DNS name on the operator host:

   ```bash
   STORAGE_DNS=$(jq -r .internalNames.storage "$PLATFORM_CONFIG")
   openssl verify -CAfile /srv/openstack-platform/.secrets/setup/pki/internal-ca.pem \
     -verify_hostname "$STORAGE_DNS" \
     /srv/openstack-platform/.secrets/setup/pki/storage.pem
   ```

   **Success:** the security-group rules exist, the certificate prints `OK`, and role readiness passes. If the certificate lacks the inventory name, install a reviewed replacement leaf signed by the existing CA before continuing. Do not change the CA to bypass verification.

5. Check the controller policy's migration limits:

   | Field under `limits` | Default | Accepted range |
   | --- | --- | --- |
   | `migrationBackupMaxAgeMinutes` | 60 minutes | 1–1440 minutes |
   | `migrationAppSeconds` | 1800 seconds per app | 120–7200 seconds |

   These fields are optional. If changed, install them in operator and hosted-controller policy through the protected configuration procedure. The app deadline starts after the backup gate and covers stopping, copying, verifying, and restarting the app. CLI polling has its own `--timeout`, default and maximum 7200 seconds. Measure a rehearsal before setting a maintenance window; elapsed time is not proof of completion.

## Back up, rehearse and cut over

1. On admin as `agentops`, take a complete managed-data backup and test its restore:

   ```bash
   mountpoint "$(jq -r .paths.backups "/etc/$NS/platform.json")"
   openstack-platform-managed-backup
   PLATFORM_CONFIG="$(readlink -f "/etc/$NS/platform.json")" \
     /srv/openstack-platform/infra/backup/verify_latest_restore.sh
   ```

   **Success:** the backup prints `platform backup complete: <directory>`; its `MANIFEST` says `format_version=4` and it has both database catalogs. The restore check ends with `latest platform restore=verified evidence=<directory>/RESTORE-MANIFEST`.

   The backup covers shared sources and accepted instances. Failed inventory, credentials, or dumps fail the entire set. If either command fails, inspect the error and fix the backup before proceeding. Off-site export needs a separate mounted filesystem; [production has not configured it](backups-and-recovery.md#export-recovery-evidence-off-site). Local migration checkpoints do not replace an off-site complete set.

2. Rehearse with a disposable app that has representative shared database data:

   ```bash
   openstack-platform-storage-migrate --application <REHEARSAL_APP_UUID>
   ```

   Save the printed `Instance migration request-id=<UUID>` before the command completes. **Success:** `Instance migration completed`, instance isolation in the storage inventory, correct app data, and working public app health. If rehearsal fails, use the replay or abort procedure below before migrating other apps.

3. Migrate a reviewed app selection:

   ```bash
   openstack-platform-storage-migrate --application <APP_UUID>
   ```

   Repeat `--application UUID` to select several apps. Omit it only when you intend to migrate all eligible apps:

   ```bash
   openstack-platform-storage-migrate
   ```

   The controller stops one running app at a time. It freezes source logins, copies and verifies data, publishes new binding values, and resubmits the accepted Nomad job with the storage DNS mapping. Stopped or never-deployed apps stay stopped. **Success:** `Instance migration completed`; verify each resource and app as in step 2.

   If the command loses its response, times out, or reports reconciliation is needed, fix the cause and replay the **same request ID and app selection**:

   ```bash
   openstack-platform-storage-migrate --request-id <ORIGINAL_REQUEST_UUID> \
     --application <APP_UUID> --timeout 7200
   ```

   Preserve an omitted or repeated `--application` selection exactly. Do not create a new migration intent to work around a stopped app or an unfinished operation.

4. Before publication, an interrupted migration can be aborted:

   ```bash
   openstack-platform-storage-migrate --abort --application <APP_UUID>
   ```

   Save the abort's printed request ID. If interrupted, repeat `--abort`, the same ID, and the same selection. Abort restores source login permissions and the accepted shared job, releases the app reservation, and retains source and target data. Aborted apps need a new migration intent later; the original master skips them on replay.

   **Rollback boundary:** after any selected endpoint is published, abort refuses `MIGRATION_ALREADY_PUBLISHED`. Replay the original migration instead. Switching the app back to the sealed source would lose writes made to the instance.

5. Verify all pages of the storage inventory again. Every migrated PostgreSQL/MongoDB resource must have `isolation: instance`, an `instanceId`, and an `instancePort`. Test app reads/writes and public health; check fresh usage and database memory, connection, disk, and availability alarms. Then run on admin:

   ```bash
   openstack-platform-storage-repair
   openstack-platform-managed-backup
   ```

   **Success:** `PostgreSQL repair completed` and a new committed format-4 backup. Repair reapplies PostgreSQL role timeouts and connection caps, normalizes object ownership, and repairs old shared IP-based bindings. If repair is interrupted, replay its printed ID with `--request-id <UUID>`. Run the restore check again before relying on the new backup.

### The backup gate and a failed checkpoint

Before each resource starts or resumes cutover, the controller requires a successful source backup within `migrationBackupMaxAgeMinutes`. It reads an operator-owned mode-0640 receipt from `$PLATFORM_BACKUPS/<namespace>-migration-receipts`; it cannot read ciphertext or the managed-data key.

A missing or stale receipt starts `<namespace>-resource-backup@<postgres|mongo>-<providerName>.service` through a restricted polkit rule. That unit dumps only the selected shared resource, encrypts and verifies the archive, and publishes the receipt after syncing it. The unit has a 3600-second timeout. An initial migration keeps the app serving during this gate; an already stopped replay stays stopped.

Inspect or retry a failed gate from the admin recovery console:

```bash
NS=<namespace>
DATABASE=<providerName>
sudo systemctl start "$NS-resource-backup@mongo-$DATABASE.service"
sudo journalctl -u "$NS-resource-backup@mongo-$DATABASE.service" -n 30 --no-pager
```

Use `postgres` for PostgreSQL. The provider name is admin-only and has the form `p_<20 lowercase hex characters>`. **Success:** the unit exits successfully and the receipt/status files record verification and success. A successful retry clears the failed checkpoint status; then replay the migration intent.

Checkpoint ciphertext stays private under `$PLATFORM_BACKUPS/<namespace>/migration-checkpoints/<type>-<providerName>/`. It receives age-based pruning after 14 days, both during checkpoint creation and during nightly managed-data backups. These checkpoints are not included in the complete managed-data off-site bundle.

## Recover a resource or a lost storage host

Use [managed-data restore and the full-loss drill](backups-and-recovery.md#check-and-restore-managed-data) for data recovery. A format-4 restore can move a tracked shared resource directly into an instance; run the migration only if the restored controller still tracks a shared endpoint.

For one resource, use a [restore-capable admin host](backups-and-recovery.md#check-and-restore-managed-data). Verify the set's `SHA256SUMS` before using its provider archive and the other provider's catalog:

```bash
set -o pipefail
age --decrypt --identity /escrow/managed-age-identity.txt /backup/postgres.age | \
  PLATFORM_CONFIG=/private/replacement-platform.json \
  openstack-platform-storage-backup restore --type postgres \
    --resource <RESOURCE_UUID> --controller-database /private/offline-controller.sqlite3 \
    --catalog /backup/mongodb-catalog.json
```

For MongoDB, use `mongodb.age`, `--type mongo`, and `--catalog /backup/postgres-catalog.json`. The other provider's verified catalog reserves its archived ports against allocation. The offline controller must be a direct, current-user-owned mode-0600 file without SQLite sidecars.

Restore can terminate PostgreSQL sessions and replace MongoDB contents. Keep the target app and hosted controller stopped until data verification and offline state installation finish. **Success:** `database-restore=verified kind=<type> shared=<n> isolated=<n>`, correct data inventory, restored bindings, and verified worker access. The supported [hosted-controller restore](backups-and-recovery.md#restore-the-hosted-controller) installs the remapped controller database.

If restore fails, keep the targets isolated, fix the stated credential, inventory, or capacity problem, and replay the same payload. A completed identical payload skips destructive import. Rebuild missing app images and recover arbitrary runtime environment values from escrow; the managed backup contains database binding credentials.
