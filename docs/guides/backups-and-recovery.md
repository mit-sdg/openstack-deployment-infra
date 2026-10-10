# Backups and recovery

For operators, these five backup sets protect different parts of the platform. All are stored on the admin host's backup volume (`$PLATFORM_BACKUPS`); operator state is backed up on the operator host first. Two private age keys decrypt them:

| Backup set | What it protects | Location on admin host | How often it runs (UTC) | Which key decrypts it |
| --- | --- | --- | --- | --- |
| Hosted controller | The controller database: apps, deployments, sizing, storage records, environment variable names (not values), and the operation journal | `$PLATFORM_BACKUPS/hosted-controller` | Daily at 02:15 (plus up to 30 min) | Controller escrow identity |
| Deploy keys | Each app's deploy key pair for private GitHub repositories | `$PLATFORM_BACKUPS/hosted-controller` (committed with hosted controller) | With hosted controller | Controller escrow identity |
| Owner portal | The portal database: accounts, roles, quotas, app ownership, pending requests, and audit logs | `$PLATFORM_BACKUPS/management-broker` | Daily at 02:30 (plus up to 15 min) | Controller escrow identity |
| Operator state | The operator CLI database: role image selections and infrastructure operations | `$PLATFORM_BACKUPS/controller` (uploaded from operator host) | Daily at 02:45 (plus up to 30 min), operator user timer | Controller escrow identity |
| Managed data | Every app's PostgreSQL, MongoDB, and S3 data, including S3 keys, bucket quotas, and grants | `$PLATFORM_BACKUPS/<namespace>/<timestamp>/` | Daily at 03:15 (plus up to 30 min) | Managed-data identity |

Once configured, off-site export copies the newest sets to external storage daily at 05:00 UTC, plus up to 30 minutes. See [Export recovery evidence off site](#export-recovery-evidence-off-site).

Before you start:

- Set the variables and `recovery` function from [Run the platform](run-the-platform.md#set-up-your-shell). **Root** steps use the admin recovery console.
- Know where your escrow copies of the two backup identities are (see [The two keys](#the-two-keys)).

## How backups work

Restore each set separately: controller records can't replace app data, or vice versa. Backup payloads are encrypted with age; SQLite snapshots and deploy-key archives are staged privately before encryption. A failed instance dump fails the entire managed-data set and is visible to backup health via STATUS.json. Retention/pruning and offsite export cover the same complete set. Checksums and manifests remain readable. An age *identity* is a private key; a *recipient* is its public key.

### The two keys

| Key | Encrypts | Where the private half lives | Where the public half lives |
| --- | --- | --- | --- |
| Controller escrow identity | Hosted controller, deploy keys, owner portal, operator state | Created by setup on the operator host at `/srv/openstack-platform/.secrets/setup/backup-age-identity.txt`. Never copy it to the admin host. | `backupAgeRecipient` in the operator policy (`/srv/openstack-platform/state/policy.json`); the admin host gets a copy |
| Managed-data identity | Managed data | On the admin host at `$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt`, because backups and restore checks need it there | Derived from the identity each time a backup runs |

Keep both private keys outside the platform and operator host, accessible to another staff member. Protect them like passwords: anyone holding a key can decrypt its backups. Without it, those backups are unreadable.

> [!WARNING]
> Don't replace or regenerate `backup-age-key.txt` on the admin host while any backup you might need depends on it. Older sets can only be decrypted with the key that encrypted them.

### How a set is committed

SQLite and deploy-key sets have an encrypted file, plus `.sha256` and `.manifest` files. Managed-data directories hold `postgres.age`, `mongodb.age`, `garage.age`, `postgres-catalog.json`, `mongodb-catalog.json`, `SHA256SUMS`, and `MANIFEST` (format 4). Catalogs omit secrets; DB payloads include encrypted managed binding credentials and role settings/users, covering shared and isolated resources together. The manifest commits a SQLite set; managed-data directories are published after their manifest is written. A missing manifest means an incomplete set. Export and health checks may refuse incomplete evidence. Never finish a set by hand.

### What is not backed up

- **App container images.** Managed-data sets leave them out. After a full restore, you rebuild each app from its accepted commit. See [Rebuild app images after a full restore](#rebuild-app-images-after-a-full-restore).
- **Runtime environment values.** Nomad's encrypted variables aren't backed up; controller records keep only names. Escrow values you can't regenerate. Managed-data sets contain S3 keys, but don't restore Nomad variables.
- **Running infrastructure.** Backups don't recreate servers, volumes, workers, or Nomad state.

### How long sets are kept

| Set | What stays on the backup volume |
| --- | --- |
| Hosted controller, deploy keys, owner portal | Every complete set from the last 14 days, and always the newest three, however old. Pruned after each backup. |
| Operator state | The newest 14 sets |
| Managed data | Sets from the last 14 days. Pruned after each backup. |
| Off-site bundles | Never pruned by the platform. Set retention on your storage provider. |

Copy anything you need for longer than two weeks off site.

<details>
<summary>Retention details for the hosted-controller and portal sets</summary>

Retention uses filename timestamps. Pruning removes the manifest first, so interruption leaves an uncommitted set. Deploy-key archives older than 14 days are removed only when no remaining database manifest names them or shares their timestamp. Old incomplete leftovers are removed too.

Pruning leaves `.staging`, links, foreign-owned files, unexpected names, and incomplete committed sets alone, logging lines that start with `retention ignored`. Success prints `retention=ok removed=<n> kept=<n>`, counting databases and deploy-key archives separately. A failure prints `retention=failed reason=<reason>` and fails the unit; the new backup remains committed. Fix the cause and rerun.

</details>

## Verify backups and schedules

Do this weekly, before any risky change, and after any host replacement.

1. Check that every timer is active and ran recently:

   ```bash
   systemctl --user is-enabled openstack-platform-backup.timer
   systemctl --user list-timers openstack-platform-backup.timer
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl list-timers \
     "$PLATFORM_NAMESPACE-hosted-controller-backup.timer" \
     "$PLATFORM_NAMESPACE-management-broker-backup.timer" \
     "$PLATFORM_NAMESPACE-platform-backup.timer" \
     "$PLATFORM_NAMESPACE-offsite-export.timer"
   ```

   Expect `enabled`, with `LAST` and `NEXT` times within a day. Enable [lingering](run-the-platform.md#start-it-and-connect) so the operator timer survives logout. A timer firing doesn't prove its backup succeeded; check the sets below.

2. List the newest sets:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- ls -lt \
     "$PLATFORM_BACKUPS/hosted-controller" \
     "$PLATFORM_BACKUPS/management-broker" \
     "$PLATFORM_BACKUPS/controller" \
     "$PLATFORM_BACKUPS/$PLATFORM_NAMESPACE"
   ```

   You should see, near the top of each listing, a set from the last day:

   - `hosted-controller-<timestamp>.sqlite3.age`, `.sha256`, and `.manifest`, and `hosted-controller-source-keys-<timestamp>.tar.age` with its own `.sha256` and `.manifest`;
   - `management-broker-<timestamp>.sqlite3.age`, `.sha256`, and `.manifest`;
   - `platform-<timestamp>.sqlite3.age`, `.sha256`, and `.manifest` (operator state);
   - a managed-data directory named `<timestamp>`.

3. If the owner portal is installed, check that its backup has a recipient to encrypt to:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- cat \
     "$PLATFORM_ADMIN_STATE/management-broker-releases/config/backup-recipient.txt"
   ```

   Expect one `age1` recipient matching `backupAgeRecipient` in `/srv/openstack-platform/state/policy.json`. A missing file causes the backup unit to skip. Create it from the policy and make it readable by the broker:

   ```bash
   /srv/openstack-platform/runtime/python3.14 -c \
     'import json; print(json.load(open("/srv/openstack-platform/state/policy.json"))["backupAgeRecipient"])' |
     ssh -F "$SSH_CONFIG" platform-admin -- tee \
     "$PLATFORM_ADMIN_STATE/management-broker-releases/config/backup-recipient.txt"
   ssh -F "$SSH_CONFIG" platform-admin -- chmod 0640 \
     "$PLATFORM_ADMIN_STATE/management-broker-releases/config/backup-recipient.txt"
   ```

   The config directory assigns the `management-broker` group to new files. Run a portal backup (step 4) and confirm it appears.

4. Take fresh backups before a change.

   Operator state, from the operator host:

   ```bash
   operator_backup="$($PLATFORM_CLI backup)"
   printf '%s\n' "$operator_backup"
   grep -Eq '^backup=platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age sha256=[0-9a-f]{64}$' \
     <<<"$operator_backup"
   ```

   Expect `backup=platform-<timestamp>.sqlite3.age sha256=<checksum>`. The CLI snapshots and encrypts locally, then commits an encrypted set on admin.

   Managed data, run on the admin host as `agentops`:

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
       "$PLATFORM_ROOT/infra/backup/run_platform_backup.sh"
   )"
   printf '%s\n' "$managed_backup"
   grep -Eq '^platform backup complete: .+$' <<<"$managed_backup"
   ```

   Expect `postgres backup verified`, `mongodb backup verified`, `garage backup verified`, then `platform backup complete: <directory>`. Garage adds read-only backup grants as needed; any unreadable bucket fails the backup.

   Hosted controller and deploy keys (**root**):

   ```bash
   recovery sudo systemctl start "$PLATFORM_NAMESPACE-hosted-controller-backup.service"
   recovery sudo journalctl -u "$PLATFORM_NAMESPACE-hosted-controller-backup.service" -n 5 --no-pager
   ```

   The journal shows `hosted-controller-backup=hosted-controller-<timestamp>.sqlite3.age sha256=<checksum>` followed by `retention=ok removed=<n> kept=<n>`.

   Owner portal (**root**):

   ```bash
   recovery sudo systemctl start "$PLATFORM_NAMESPACE-management-broker-backup.service"
   recovery sudo journalctl -u "$PLATFORM_NAMESPACE-management-broker-backup.service" -n 5 --no-pager
   ```

   The journal shows `management-broker-backup=management-broker-<timestamp>.sqlite3.age sha256=<checksum>` and a `retention=` line.

5. Check that the newest managed-data set really restores. This doesn't touch live services:

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

   The script restores databases into temporary containers and checks the S3 archive against Garage's bucket list. It cleans up containers on exit and writes `RESTORE-MANIFEST` only on success. Expect `latest platform restore=verified evidence=<directory>/RESTORE-MANIFEST`.

If any step fails, see [A backup failed or is missing](troubleshooting.md#a-backup-failed-or-is-missing).

## Export recovery evidence off site

The backup volume sits next to the data it protects. An off-site copy survives the loss of the whole OpenStack project.

The admin timer copies the newest committed sets, including the portal once installed, into a new `<namespace>-<timestamp>` directory on the off-site mount. It verifies copies before updating `$PLATFORM_ROOT/persistent/status/offsite-export.json`. Existing bundles stay intact. Payloads remain encrypted; manifests and checksums are readable. The configuration holds no provider credentials.

The off-site destination must be:

- a separate filesystem mounted directly at the configured path on the admin host;
- owned by `agentops` with mode `0700`;
- on a different device from the backup volume;
- protected by your provider's versioning, object lock, or write-once retention.

Mount it yourself before each export, and re-create the mount after admin replacement.

1. With the destination mounted on the admin host, record its identity:

   ```bash
   export OFFSITE_MOUNT=/mnt/institutional-recovery
   ssh -F "$SSH_CONFIG" platform-admin -- mountpoint "$OFFSITE_MOUNT"
   ssh -F "$SSH_CONFIG" platform-admin -- findmnt -n -o SOURCE,FSTYPE --target "$OFFSITE_MOUNT"
   ssh -F "$SSH_CONFIG" platform-admin -- stat -c '%U:%a' "$OFFSITE_MOUNT"
   ```

   Expect a mount point, its source and filesystem type, and `agentops:700`. Use the source and type in the configuration.

2. On the operator host, from a checkout of this repository, make a private copy of the example configuration and edit it:

   ```bash
   install -m 0600 config/offsite-export.example.json \
     /private/path/offsite-export.json
   ${EDITOR:?set EDITOR} /private/path/offsite-export.json
   ```

   | Field | Set it to |
   | --- | --- |
   | `format` | Leave as `openstack-platform-offsite-export-config-v1` |
   | `destination` | The mount path, for example `/mnt/institutional-recovery` |
   | `mountSource` | The `SOURCE` that `findmnt` printed |
   | `filesystemType` | The `FSTYPE` that `findmnt` printed |
   | `limits.maximumFileBytes` | Largest single file to copy (1 GiB to 4 TiB) |
   | `limits.maximumTotalBytes` | Largest bundle (at least the file limit, at most 8 TiB) |
   | `maximumReceiptAgeHours` | How old the newest export may be before health checks fail (1 to 168; the example uses 36) |

3. Install it on the admin host:

   ```bash
   scp -F "$SSH_CONFIG" -- /private/path/offsite-export.json \
     platform-admin:/home/agentops/offsite-export.json
   ssh -F "$SSH_CONFIG" platform-admin -- install -m 0600 \
     /home/agentops/offsite-export.json \
     "$PLATFORM_ROOT/persistent/offsite-export.json"
   ```

4. Run one export now instead of waiting for the timer (**root**):

   ```bash
   recovery sudo systemctl start "$PLATFORM_NAMESPACE-offsite-export.service"
   recovery sudo journalctl -u "$PLATFORM_NAMESPACE-offsite-export.service" -n 5 --no-pager
   ```

   Success shows `offsite-scheduled-export=<namespace>-<timestamp> verified=true`. A failure shows `recovery bundle failed: <reason>` and leaves the previous receipt in place.

5. On the next health run, normally within five minutes, `offsite_recovery` passes if earlier checks pass, and names the bundle and its age. See [Check health](run-the-platform.md#check-health). You can also read the receipt:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- \
     cat "$PLATFORM_ROOT/persistent/status/offsite-export.json"
   ```

Export refuses an unmounted, changed, non-private, or same-device destination. Health also rejects an expired receipt or a bundle missing an installed portal's set.

Apply retention on the provider side only after a newer bundle and its complete-loss drill evidence are safely stored.

<details>
<summary>Bundle formats</summary>

Each bundle holds one directory per set (`hosted-controller`, `operator-state`, `managed-data`, and `management-broker` when present), plus `SHA256SUMS`, `MANIFEST.json`, and `MANIFEST.json.sha256`. Version 3 bundles include the hosted-controller set's deploy-key archive. Version 2 added the portal set, and version 1 has only the first three sets. All versions can be verified and imported; versions 1 and 2 have no deploy keys.

</details>

## Restore the hosted controller

This replaces the controller's records of apps, deployments, and storage resources.

> [!WARNING]
> Restore only a damaged or incorrect database, using the newest set before the problem. Newer records disappear; leftover cloud resources aren't adopted automatically.

Before you start, check that:

- the dashboard lists no Running or Needs recovery operations. Finish or recover them first; one narrow exception appears below;
- you have a fresh backup of the current database if the controller still runs;
- any portal restore uses the same night's set, so ownership matches.

Decrypt on the operator host; keep the controller escrow key off admin.

1. On the operator host, fetch and check the set you chose:

   ```bash
   install -d -m 0700 "$HOME/hosted-restore"
   cd "$HOME/hosted-restore"
   ssh -F "$SSH_CONFIG" platform-admin -- ls "$PLATFORM_BACKUPS/hosted-controller"
   BACKUP=hosted-controller-YYYYMMDDTHHMMSSZ.sqlite3.age
   for file in "$BACKUP" "$BACKUP.sha256" "$BACKUP.manifest"; do
     scp -F "$SSH_CONFIG" "platform-admin:$PLATFORM_BACKUPS/hosted-controller/$file" .
   done
   sha256sum --check "$BACKUP.sha256"
   cat "$BACKUP.manifest"; echo
   ```

   `sha256sum` prints `<file>: OK`. The manifest is one JSON line. If it contains `"sourceKeys"`, that value names the deploy-key archive taken with this backup.

2. Decrypt the database:

   ```bash
   /srv/openstack-platform/bin/age --decrypt \
     --identity /srv/openstack-platform/.secrets/setup/backup-age-identity.txt \
     --output hosted-controller-restore.sqlite3 "$BACKUP"
   chmod 0600 hosted-controller-restore.sqlite3
   ```

3. If the manifest names a deploy-key archive, fetch, check, and decrypt it too:

   ```bash
   KEYS=hosted-controller-source-keys-YYYYMMDDTHHMMSSZ.tar.age
   for file in "$KEYS" "$KEYS.sha256" "$KEYS.manifest"; do
     scp -F "$SSH_CONFIG" "platform-admin:$PLATFORM_BACKUPS/hosted-controller/$file" .
   done
   sha256sum --check "$KEYS.sha256"
   /srv/openstack-platform/bin/age --decrypt \
     --identity /srv/openstack-platform/.secrets/setup/backup-age-identity.txt \
     --output hosted-controller-source-keys.tar "$KEYS"
   chmod 0600 hosted-controller-source-keys.tar
   ```

   The tar holds private keys. Don't unpack it.

4. Copy the decrypted files to the admin host:

   ```bash
   scp -F "$SSH_CONFIG" -- hosted-controller-restore.sqlite3 \
     platform-admin:/home/agentops/hosted-controller-restore.sqlite3
   # Only with a deploy-key archive:
   scp -F "$SSH_CONFIG" -- hosted-controller-source-keys.tar \
     platform-admin:/home/agentops/hosted-controller-source-keys.tar
   ```

5. Stop the controller, its backup, and the portal's path units (**root**). Active portal path units can restart the portal, which would pull the controller back up:

   ```bash
   recovery sudo systemctl stop \
     "$PLATFORM_NAMESPACE-management-broker.path" \
     "$PLATFORM_NAMESPACE-management-web.path" \
     "$PLATFORM_NAMESPACE-hosted-controller-backup.timer" \
     "$PLATFORM_NAMESPACE-hosted-controller-backup.service" \
     "$PLATFORM_NAMESPACE-controller.service"
   ```

   Stopping the controller also stops its readiness check and the owner portal. The portal is unavailable until step 8.

6. Move the inputs into place (**root**):

   ```bash
   recovery sudo install -m 0600 -o platform-controller -g platform-controller \
     /home/agentops/hosted-controller-restore.sqlite3 \
     "$PLATFORM_ADMIN_STATE/controller/restore-input.sqlite3"
   recovery sudo rm -f /home/agentops/hosted-controller-restore.sqlite3
   # Only with a deploy-key archive:
   recovery sudo install -m 0600 -o platform-controller -g platform-controller \
     /home/agentops/hosted-controller-source-keys.tar \
     "$PLATFORM_ADMIN_STATE/controller/restore-source-keys.tar"
   recovery sudo rm -f /home/agentops/hosted-controller-source-keys.tar
   ```

7. Run the restore (**root**):

   Confirm the controller is inactive and the staged file is owned by `platform-controller`, mode `0600`. The launcher also checks its backup service and timer:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active "$PLATFORM_NAMESPACE-controller.service" || true
   recovery sudo stat -c '%U:%a' "$PLATFORM_ADMIN_STATE/controller/restore-input.sqlite3"
   ```

   Expect `inactive` and `platform-controller:600`. Stop if either check fails; do not treat an SSH failure as proof the service stopped.

   > [!WARNING]
   > This replaces the live controller database. Records created since the backup are lost; their cloud resources remain and aren't adopted automatically.

   ```bash
   recovery sudo openstack-platform-hosted-controller-restore --yes
   ```

   Expect `restore=verified schema-version=<n> integrity=ok`. Before replacement, the launcher checks stopped units, input metadata, deployment identity, schema, integrity, foreign keys, unfinished operations, and any deploy-key archive against the restored apps. Validation refusals leave the database unchanged. Success deletes staged inputs.

8. Start everything again (**root**):

   ```bash
   recovery sudo systemctl start \
     "$PLATFORM_NAMESPACE-controller.service" \
     "$PLATFORM_NAMESPACE-hosted-controller-backup.timer" \
     "$PLATFORM_NAMESPACE-management-broker.path" \
     "$PLATFORM_NAMESPACE-management-web.path"
   ```

9. Verify, then take a fresh backup:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
     "$PLATFORM_NAMESPACE-controller.service" \
     "$PLATFORM_NAMESPACE-controller-readiness.service"
   $PLATFORM_CLI status
   recovery sudo systemctl start "$PLATFORM_NAMESPACE-hosted-controller-backup.service"
   ```

   Both units print `active`, and `status` shows the app and storage counts you expect from the restored set.

10. Delete the decrypted copies on the operator host:

    ```bash
    rm -f "$HOME/hosted-restore/hosted-controller-restore.sqlite3" \
      "$HOME/hosted-restore/hosted-controller-source-keys.tar"
    ```

The database commits before deploy keys. If key installation fails, keep the controller stopped, leave staged inputs in place, and repeat step 7 before restarting.

Older backups lack a deploy-key archive and leave existing keys alone. After complete loss, create replacement keys in each private app's portal settings and add them on GitHub.

Restoring the hosted controller doesn't recreate cloud servers, Nomad jobs, workers, or app data.

<details>
<summary>Exception: replacing a database that has one stuck recovery-required operation</summary>

If admin replacement is blocked because controller preparation refuses changed image selections with one `recovery_required` operation, keep a fresh current backup and confirm the replacement has no unfinished work. Only then acknowledge that exact operation:

```bash
recovery sudo openstack-platform-hosted-controller-restore --yes \
  --replace-current-recovery-required-operation <operation-uuid>
```

All unfinished state must belong to that one `recovery_required` operation. Other states or operations are refused. Don't bypass recoverable work.

</details>

## Restore the owner portal database

This replaces the portal database with the selected backup.

> [!WARNING]
> Restoring loses newer portal changes and invalidates sessions, invitations, enrollment/reset links, and existing setup links. Roles, passwords, and authenticator enrollments survive. Wait for a fresh authenticator code before signing in.

1. On the operator host, fetch, check, and decrypt the set. It uses the controller escrow identity:

   ```bash
   install -d -m 0700 "$HOME/portal-restore"
   cd "$HOME/portal-restore"
   ssh -F "$SSH_CONFIG" platform-admin -- ls "$PLATFORM_BACKUPS/management-broker"
   BACKUP=management-broker-YYYYMMDDTHHMMSSZ.sqlite3.age
   for file in "$BACKUP" "$BACKUP.sha256" "$BACKUP.manifest"; do
     scp -F "$SSH_CONFIG" "platform-admin:$PLATFORM_BACKUPS/management-broker/$file" .
   done
   sha256sum --check "$BACKUP.sha256"
   /srv/openstack-platform/bin/age --decrypt \
     --identity /srv/openstack-platform/.secrets/setup/backup-age-identity.txt \
     --output management-broker-restore.sqlite3 "$BACKUP"
   chmod 0600 management-broker-restore.sqlite3
   scp -F "$SSH_CONFIG" -- management-broker-restore.sqlite3 \
     platform-admin:/home/agentops/management-broker-restore.sqlite3
   ```

2. Stop the portal, its activation and path units, and its backup (**root**):

   ```bash
   recovery sudo systemctl stop \
     "$PLATFORM_NAMESPACE-management-activate.path" \
     "$PLATFORM_NAMESPACE-management-broker.path" \
     "$PLATFORM_NAMESPACE-management-web.path" \
     "$PLATFORM_NAMESPACE-management-broker-backup.timer" \
     "$PLATFORM_NAMESPACE-management-broker-backup.service" \
     "$PLATFORM_NAMESPACE-management-web.service" \
     "$PLATFORM_NAMESPACE-management-broker.service" \
     "$PLATFORM_NAMESPACE-management-identity.service" \
     "$PLATFORM_NAMESPACE-management-activate.service"
   ```

3. Move the input into place (**root**):

   ```bash
   recovery sudo install -m 0600 -o management-broker -g management-broker \
     /home/agentops/management-broker-restore.sqlite3 \
     "$PLATFORM_ADMIN_STATE/management-broker/restore-input.sqlite3"
   recovery sudo rm -f /home/agentops/management-broker-restore.sqlite3
   ```

   Confirm every unit stopped in step 2 is inactive with `systemctl status` through the admin SSH alias. Check the staged file's owner and mode:

   ```bash
   recovery sudo stat -c '%U:%a' "$PLATFORM_ADMIN_STATE/management-broker/restore-input.sqlite3"
   ```

   The command must print `management-broker:600`.

   > [!WARNING]
   > This replaces live portal records and loses changes since the backup. It invalidates sessions and account links, signing everyone out.

   Run the restore (**root**):

   ```bash
   recovery sudo openstack-platform-management-broker-restore --yes
   ```

   Expect `management-broker-restore=verified sessions=invalidated`. The launcher checks the state mount, stopped portal units, and portal identity; success deletes the input.

4. Start the portal again (**root**). The path units start the broker and web services:

   ```bash
   recovery sudo systemctl start \
     "$PLATFORM_NAMESPACE-management-identity.service" \
     "$PLATFORM_NAMESPACE-management-broker.path" \
     "$PLATFORM_NAMESPACE-management-web.path" \
     "$PLATFORM_NAMESPACE-management-activate.path" \
     "$PLATFORM_NAMESPACE-management-broker-backup.timer"
   ```

5. Verify, then take a fresh portal backup and delete the local decrypted copy:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
     "$PLATFORM_NAMESPACE-management-broker.service" \
     "$PLATFORM_NAMESPACE-management-web.service"
   recovery sudo systemctl start "$PLATFORM_NAMESPACE-management-broker-backup.service"
   rm -f "$HOME/portal-restore/management-broker-restore.sqlite3"
   ```

   Both services print `active`. Sign in to the portal with a fresh authenticator code.

After a portal restore:

- The broker creates a new private signing key on startup. An environment-variable edit retried with a request key from before the restore is refused with `IDEMPOTENCY_CONFLICT`; submit the edit again as a new request.
- Unfinished requests keep apps on hold. A portal admin must resolve unknown outcomes or controller recovery before new changes. Don't force success to free quota.
- If you need a new portal admin, issue a new setup link. See [Open the owner portal](open-the-portal.md).

## Restore external operator state

Restore the CLI's operator-host database offline, without cloud or network access.

Before you start, finish or recover any unfinished infrastructure operation; the restore refuses to replace a database with one.

1. Fetch and check the set you want, on the operator host:

   ```bash
   install -d -m 0700 "$HOME/operator-restore"
   cd "$HOME/operator-restore"
   ssh -F "$SSH_CONFIG" platform-admin -- ls "$PLATFORM_BACKUPS/controller"
   BACKUP=platform-YYYYMMDDTHHMMSSZ.sqlite3.age
   for file in "$BACKUP" "$BACKUP.sha256" "$BACKUP.manifest"; do
     scp -F "$SSH_CONFIG" "platform-admin:$PLATFORM_BACKUPS/controller/$file" .
   done
   sha256sum --check "$BACKUP.sha256"
   chmod 0600 "$BACKUP"
   ```

   The backup and identity must be direct, current-user-owned files, mode `0600`. Give escrow copies the same mode in a private directory.

2. Stop everything that uses the operator state, including the dashboard if it runs:

   ```bash
   systemctl --user stop openstack-platform-dashboard
   systemctl --user stop openstack-platform-backup.timer openstack-platform-backup.service
   ```

3. Restore:

   Confirm step 2 stopped the backup service and any dashboard. Check the timer and input metadata:

   ```bash
   systemctl --user is-active openstack-platform-backup.timer || true
   stat -c '%U:%a' "$HOME/operator-restore/$BACKUP" /srv/openstack-platform/.secrets/setup/backup-age-identity.txt
   ```

   Expect `inactive` and your username with `600` for both files. Stop on a failed check; an unavailable user manager doesn't prove the timer stopped.

   > [!WARNING]
   > This overwrites `/srv/openstack-platform/state/platform.sqlite3`, losing image selections and infrastructure records added since the backup.

   Run the restore:

   ```bash
   restore_output="$(
     /srv/openstack-platform/bin/openstack-platform-restore \
       "$HOME/operator-restore/$BACKUP" \
       --age-identity /srv/openstack-platform/.secrets/setup/backup-age-identity.txt \
       --yes
   )"
   printf '%s\n' "$restore_output"
   grep -Eq '^restore=verified schema-version=[0-9]+ integrity=ok$' <<<"$restore_output"
   ```

   Expect `restore=verified schema-version=<n> integrity=ok`. Validation checks deployment identity, schema, integrity, foreign keys, sidecars, locks, and unfinished operations before replacement.

4. Start the timer again and check the result:

   ```bash
   systemctl --user start openstack-platform-backup.timer
   $PLATFORM_CLI status
   $PLATFORM_CLI infra list
   ```

   Start the dashboard again too, if you use it.

### Rehearse it without touching live state

To prove a backup and its identity work, restore into a private, empty directory instead. The option must come first:

```bash
install -d -m 0700 "$HOME/offline-state"
/srv/openstack-platform/bin/openstack-platform-restore \
  --replacement-state-directory "$HOME/offline-state" \
  "$HOME/operator-restore/$BACKUP" \
  --age-identity /srv/openstack-platform/.secrets/setup/backup-age-identity.txt \
  --yes
```

Use a current-user-owned directory, mode `0700`, outside live state, with no `platform.sqlite3`. Delete it afterwards. Timers and the dashboard may keep running.

## Check and restore managed data

Run the non-destructive check in [Verify backups and schedules](#verify-backups-and-schedules), step 5, weekly.

Managed-data restore writes to the services in your platform config. Use empty replacement services, usually through the [complete-loss drill](#drill-a-complete-loss-recovery).

Before running, confirm the config's storage address points to isolated, empty replacement services. Check that `AGE_KEY` is a direct file, mode `0600`.

> [!WARNING]
> This drops and recreates database contents and restores S3 buckets. Never target healthy or non-empty services. A later failure can leave earlier services restored; keep targets isolated until verification passes.

To run it on its own:

```bash
PLATFORM_CONFIG=/private/replacement-platform.json \
AGE_KEY=/escrow/managed-age-identity.txt \
GARAGE_RESTORE_CONTROLLER_DATABASE=/private/replacement/hosted-controller/platform.sqlite3 \
  infra/backup/restore_managed_data.sh --yes /mnt/recovered/<bundle>/managed-data
```

It prints `managed-data-restore=verified source=<set>` when done.

What you need:

- The managed-data identity as a mode `0600` file (`AGE_KEY`).
- A host laid out like the admin host, such as a replacement admin host. The script finds its tools and credentials under the config's `paths.root`: `bin/age`, `secrets/storage-bootstrap.env` (mode `0600`), and `secrets/nomad-cli/internal-ca.pem`. Override them with the `AGE`, `SECRETS_FILE`, and `CA_FILE` variables, and set `SERVICE_CHECK_PYTHON` to a Python 3 interpreter. The matching admin image supplies PostgreSQL 17 clients, MongoDB tools, age, Podman and `openstack-platform-storage-backup`. It needs the authenticated instance manager, storage-bootstrap/Nomad credentials and a private offline controller database. Shared-resource payloads restore into isolated instances too.
- A replacement hosted-controller database (`GARAGE_RESTORE_CONTROLLER_DATABASE`) whenever the set contains S3 buckets: a private, current-user-owned, mode `0600` copy with no `-wal` or `-shm` files next to it. Garage gives restored buckets new IDs, and the restore writes the new IDs into this database in one transaction after checking each bucket's original name and ID. Keep the controller and its backup stopped, and install the updated database with the [hosted-controller restore](#restore-the-hosted-controller) afterwards.

S3 restore brings back each app's keys with their original IDs and secrets, recreates the apps' read and write grants, and restores the objects. The backup key gets write access only during the restore, and every bucket it touched goes back to read-only before success is reported. If taking that write access away fails, deny write and owner access for the backup key yourself before reopening services.

Keep the set until verification passes. S3 catalog format 1 lacks app keys and grants, so bucket restore is refused until they're recovered separately. Managed-data format 2 contains `registry.age`; restore skips it. Rebuild images instead.

## Drill a complete-loss recovery

Run a drill at least once a term to test recovery from an off-site bundle and both escrowed identities.

The drill script, `infra/backup/full_loss_recovery_drill.sh`, has two modes:

| Mode | What it does | Destructive? |
| --- | --- | --- |
| `--verify-only` | Verifies and imports the bundle, decrypts both databases, checks integrity and required schema tables, checks the portal database and deploy-key archive if present, and checks the S3 archive offline | No. It writes nothing outside its work directory and produces no evidence file. |
| `--full` | Everything above, then restores both databases into private replacement directories, checks that they contain image selections, apps, and accepted deployments, restores the portal database, and runs the destructive managed-data restore | Yes, to the services named in the replacement platform config. Commits `DRILL-EVIDENCE.json` only if every step passes. |

`--verify-only` doesn't count as a completed drill.

What you need:

- The bundle, copied back from off-site storage to a local path.
- An absolute work directory that doesn't exist yet.
- Both identities and a replacement platform config, each a direct file owned by you with mode `0600`.
- A replacement config with the same project, namespace, names, addresses, ports, volumes, paths, and domain. Image and release choices may differ. Isolate drill services while preserving that identity.
- For `--full`: empty PostgreSQL, MongoDB, and Garage targets and a [restore-capable host](#check-and-restore-managed-data). The bundle needs both controller databases, three managed-data archives, an image selection, and an app with an accepted deployment. Include S3 objects and app grants to exercise their recovery. Format-4 full drills additionally require one isolated PostgreSQL and MongoDB resource with sample data; both actual imports must succeed before isolatedDatabases evidence is recorded.
- The tools the script calls, on your `PATH` or named with these variables:

  | Variable | Default command |
  | --- | --- |
  | `RECOVERY_COMMAND` | `openstack-platform-recovery` |
  | `OPERATOR_RESTORE_LAUNCHER` | `openstack-platform-restore` (it must be the installed launcher, such as `/srv/openstack-platform/bin/openstack-platform-restore`, because the drill uses `--replacement-state-directory`) |
  | `HOSTED_RESTORE_LAUNCHER` | `openstack-platform-controller-restore` |
  | `BROKER_RESTORE_LAUNCHER` | `openstack-platform-management-broker-backup` |
  | `MANAGED_RESTORE_LAUNCHER` | `infra/backup/restore_managed_data.sh` next to the drill script |
  | `AGE` | `age` |

Run the verify-only mode first:

```bash
infra/backup/full_loss_recovery_drill.sh --verify-only \
  /mnt/recovered/$PLATFORM_NAMESPACE-YYYYMMDDTHHMMSSZ \
  /srv/full-loss-verification \
  /escrow/controller-age-identity.txt \
  /escrow/managed-age-identity.txt \
  /private/replacement-platform.json
```

It ends with `recovery archives=verified` and `full-loss-drill=verify-only evidence=none`.

Before the full drill, confirm verify-only passed, the work directory is absent, and the replacement config targets isolated, empty services.

> [!WARNING]
> The full drill restores data into the configured services. Never point it at live production services.

```bash
infra/backup/full_loss_recovery_drill.sh --full \
  /mnt/recovered/$PLATFORM_NAMESPACE-YYYYMMDDTHHMMSSZ \
  /srv/full-loss-drill \
  /escrow/controller-age-identity.txt \
  /escrow/managed-age-identity.txt \
  /private/replacement-platform.json
test -f /srv/full-loss-drill/DRILL-EVIDENCE.json
```

It prints a `restore=verified` line for each controller database, `managed-data-restore=verified source=<set>`, and finally `full-loss-drill=verified evidence=/srv/full-loss-drill/DRILL-EVIDENCE.json`. Evidence records the bundle, restored counts, deploy keys, and portal/session recovery. Keep it with the bundle.

## Rebuild app images after a full restore

The replacement registry has no app images. Rebuild each app from its accepted commit; starting it alone won't work.

Before you start, make sure you have:

- access to every app's repository, and restored (or replacement) deploy keys for private ones;
- selected builder and worker images;
- the apps' runtime environment values, restored from your own secret escrow, because no backup contains them;
- the `admin` and `project` transport functions from [Deploy apps from the command line](deploy-apps-from-the-command-line.md), which call the controller's privileged and project sockets.

1. List the apps that need a rebuild: every app that isn't deleted and has an accepted deployment. Write down each app's `enabled` value.

   ```bash
   admin "http://localhost/v1/admin/applications?limit=100" > applications.json
   jq -r '.items[] | select(.deletedAt == null and .activeDeploymentId != null)
     | [.applicationId, .slug, .activeDeploymentId, .enabled] | @tsv' applications.json
   jq -r '.nextCursor // empty' applications.json
   ```

   If the last command prints an ID, fetch the next page with `&cursor=<that ID>` added to the URL and repeat.

2. For each app, rebuild its accepted deployment as a new deployment request:

   ```bash
   APP_ID=<applicationId>
   DEPLOYMENT_ID=<activeDeploymentId>
   project "http://localhost/v1/deployments/$DEPLOYMENT_ID" > previous-deployment.json
   jq -e '{repository: .sourceRepository, requestedRef: .requestedRef,
     commit: .repositoryCommit, configuration: .configuration,
     configurationRevision: .configurationRevision}
     | select(.repository != null and .requestedRef != null and .configuration != null)' \
     previous-deployment.json > rebuild.json
   python3 -c 'import uuid; print(uuid.uuid4())' > rebuild-key.txt
   project -H 'Content-Type: application/json' \
     -H "Idempotency-Key: $(<rebuild-key.txt)" --data-binary @- \
     "http://localhost/v1/applications/$APP_ID/deployments" \
     < rebuild.json > rebuild-operation.json
   STATUS_URL=$(jq -er .statusUrl rebuild-operation.json)
   project "http://localhost$STATUS_URL" > rebuild-status.json
   ```

   If `jq -e` fails, recover the missing repository, ref, or configuration from the original request. Don't infer them from current settings.

3. Repeat the status request until it reports `succeeded`. If it reports `recovery_required`, fix the cause and resubmit the exact same `rebuild.json` with the same key from `rebuild-key.txt`; see [Troubleshooting](troubleshooting.md#an-app-deploy-is-stuck-or-failed).

4. Check the app's accepted commit and its public health before moving to the next app.

Some things to expect:

- The build resolves the repository's Node.js or Bun version request again, so a version range can pick a newer release than the original build used.
- Apps with a retained primary IPv4 address can't be rebuilt this way. Use the privileged deployment with a fresh sizing plan and `maintenance: true`, as described in [Deploy apps from the command line](deploy-apps-from-the-command-line.md).
- A rebuild starts the app even if it was stopped. If an app should stay stopped, stop it again after you've verified the rebuild.

## Related

- [Run the platform](run-the-platform.md)
- [Troubleshooting](troubleshooting.md)
- [Deploy apps from the command line](deploy-apps-from-the-command-line.md)
- [Hosts and images](hosts-and-images.md)
- [Security](../security.md)
- [Internals](../reference/internals.md)

Per-resource restore uses `openstack-platform-storage-backup restore --type postgres|mongo`
with repeatable `--resource UUID`, a private offline `--controller-database PATH`, and
`--catalog PATH` pointing to the other provider's verified catalog to reserve its ports.
The managed-data restore wrapper supplies both catalogs automatically. Restore
provisions missing isolated instances, reuses recorded ports and current caps, restores
data/users, updates canonical/candidate owned bindings and controller identity, and
verifies current worker IPs before reopening access. Identical completed payload replay
skips destructive import. Keep the app/controller stopped until state installation
and verification finish. For capacity, encrypted metadata, source backup gating and
rehearsal commands, see [Migrate storage instances](migrate-storage-instances.md).
