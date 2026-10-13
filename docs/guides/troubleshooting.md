# Troubleshooting

Find your symptom below, check the evidence, and fix the cause. Read [Before you change anything](#before-you-change-anything) before attempting recovery.

The commands use the shell variables and the `recovery` function from [Run the platform](run-the-platform.md#set-up-your-shell). Replace angle-bracket placeholders with reviewed values. Commands marked `recovery sudo` run as root on admin; other CLI commands run as the unprivileged operator.

## Before you change anything

1. **Write down what you see.** Keep the operation ID or correlation ID, the phase, and the error text. These are safe to record. Credentials, provider output, and environment values are not.
2. **Look again.** Rerun the read (`$PLATFORM_CLI status`, `infra list`, the dashboard). An observation that is unavailable doesn't mean something is gone; the platform keeps its accepted state until it can see again.
3. **Fix the cause, then repeat the same action.** Almost everything in this platform resumes when you repeat the identical command or request. See [Recovery rules](#recovery-rules).
4. **Don't take shortcuts.** Never edit a database row, bypass the helper, rename, detach, or delete a cloud resource, or print a credential to get past an error.

## Find your symptom

| You see | Go to |
| --- | --- |
| `setup check` doesn't say `ready`, or setup stopped | [Setup doesn't finish](#setup-doesnt-finish) |
| `status` says `degraded` | [`status` shows degraded](#status-shows-degraded) |
| `unavailable:` from the CLI (exit code 4) | [The CLI reports a dependency unavailable](#the-cli-reports-a-dependency-unavailable) |
| `conflict:` from the CLI (exit code 3) | [The CLI reports a conflict](#the-cli-reports-a-conflict) |
| A persistent host is stopped, missing, or never becomes ready | [A host won't come back](#a-host-wont-come-back) |
| `https://<domain>/healthz` doesn't answer `OK` | [Public health fails](#public-health-fails) |
| The health snapshot or dashboard shows a failed platform check | [A platform check fails](#a-platform-check-fails) |
| A backup is missing, failed, or a restore check fails | [A backup failed or is missing](#a-backup-failed-or-is-missing) |
| The off-site check fails | [The off-site export fails](#the-off-site-export-fails) |
| A restore refuses to run | [A restore is refused](#a-restore-is-refused) |
| The dashboard won't load or shows warnings | [The dashboard won't open or shows warnings](#the-dashboard-wont-open-or-shows-warnings) |
| App changes are blocked with “wait until recovery” | [App changes are blocked with wait until recovery](#app-changes-are-blocked-with-wait-until-recovery) |
| An app deploy hangs, fails, or needs recovery | [An app deploy is stuck or failed](#an-app-deploy-is-stuck-or-failed) |
| People can't sign in to the owner portal | [People can't sign in to the portal](#people-cant-sign-in-to-the-portal) |
| Image selection or pruning is refused | [An image change is refused](#an-image-change-is-refused) |

## Setup doesn't finish

### The setup check doesn't say ready

**Likely cause:** insufficient quota, an occupied fixed address, a name collision, missing tools, or invalid release evidence. The check reads OpenStack without changing cloud resources.

**Exact check:** run from the release checkout with the protected file you plan to apply:

```bash
uv run openstack-platform setup check --env-file /private/path/setup.env --json
```

**Fix:** use the finding to choose the next action:

| Finding | Fix |
| --- | --- |
| Quota shortfall | Request quota or reduce sizes within the role minimums in [Plan a deployment](plan-a-deployment.md#flavors). Unknown quota needs provider inspection access or the documented Glance acknowledgement. |
| Occupied address or name collision | Choose an unused address or prefix. Do not delete a resource on name evidence alone. |
| Missing tool | Install the named tool, then rerun the check. |
| Release evidence error | Match the checkout commit, evidence, and trusted signing key. |
| Ownership or mode error | Use a direct file owned by the operator, mode `0600`. |

Success is `setup-check=ready` in text output or `ready: true` in JSON. An early validation error can stop before either result is printed.

### Setup stopped partway

**Likely cause:** a build, upload, provider request, SSH connection, or readiness check failed after setup had created some resources.

**Exact check:** read the command's `error:` line and the last completed progress message. If the CLI is already installed, inspect the host named by the error:

```bash
/srv/openstack-platform/bin/openstack-platform infra logs admin --lines 200
```

Use `storage` or `ingress` instead if that host failed. Before the CLI is installed, retain setup output for diagnosis.

**Fix:** correct the named dependency, then rerun the identical `setup ... --apply` command with the same setup file and workspace. Keep `/srv/openstack-platform/setup` and `/srv/openstack-platform/.secrets/setup`: they hold generated credentials and topology records. Success ends with `setup=complete`.

Do not edit or delete partially created resources or generated secrets. To change project, names, addresses, flavors, or volume sizes, use a new empty workspace and a cloud scope without earlier resources.

### Project or deployment identity mismatch

**Likely cause:** the active credentials, inventory, or state directory belongs to another deployment.

**Exact check:** verify which files the command selected. For the installed defaults:

```bash
printf '%s\n' "${PLATFORM_CONFIG:-/srv/openstack-platform/config/platform.json}"
stat -c '%U:%a:%F' /srv/openstack-platform/config/platform.json \
  /srv/openstack-platform/state/platform.sqlite3
```

Also check any explicit `--platform-config` or `--state-directory` arguments. Inspect credential file locations privately; do not print credentials or the full inventory.

**Fix:** load the intended credentials, inventory, and state directory. Rerun the read before attempting a mutation. Do not substitute an example UUID or edit identity records.

### An unexpected server, port, volume, image, or host key

**Likely cause:** cloud resources or SSH identity differ from the platform's recorded ownership evidence.

**Exact check:** for image and persistent-host identities, run:

```bash
$PLATFORM_CLI infra list
$PLATFORM_CLI infra image list
```

Compare the exact IDs and commit with your deployment and release evidence. For SSH failures, use [The admin bridge fails](#the-admin-bridge-fails).

**Fix:** establish ownership and correct the named dependency before replaying the original action. Do not rename, detach, adopt, or delete a resource because its name resembles the inventory.

## Status and the CLI

### `status` shows degraded

**Likely cause:** missing image selections, an unavailable or unhealthy observation, or unfinished work.

**Exact check:**

```bash
$PLATFORM_CLI status
$PLATFORM_CLI infra list
```

Use the dashboard's **Needs attention** and **Operations** lists for app and storage details.

| Evidence | Likely cause | Fix |
| --- | --- | --- |
| `INFRA` below `5` | Missing operator image selection | Select the reviewed role image with [Hosts and images](hosts-and-images.md). |
| `UNAVAILABLE` above `0` | A host, app, or storage observation failed | Restore the named dependency, then repeat the read. |
| `UNHEALTHY` above `0` | A host, allocation, app route, or storage resource answered with a problem | Follow the matching symptom below. App routes must return 2xx with the accepted `X-Platform-Deployment` marker. |
| Counts look normal; unfinished operation appears | Work still owns its scope | Wait for running work. For `recovery_required`, fix the dependency and replay the same request ID. |

Success is `healthy` with zero unavailable and unhealthy observations and no unfinished operations. CLI status and the timer snapshot test different things: check both.

### The CLI reports a dependency unavailable

`status` fails with `unavailable: hosted controller status is unavailable` (exit code 4) when it can't reach the controller. It never falls back to old local records.

1. Check the SSH bridge to the admin host:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- id -un
   ```

   It must print `agentops`. If it doesn't connect, or reports a changed host key, see [The admin bridge fails](#the-admin-bridge-fails).

2. Check the controller's units:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl status \
     "$PLATFORM_NAMESPACE-controller.service" \
     "$PLATFORM_NAMESPACE-controller-readiness.service"
   ```

   If they aren't active, see [The controller isn't ready](#the-controller-isnt-ready).

While the controller is down, `$PLATFORM_CLI infra list` still works, because it needs only the operator state and OpenStack.

### The admin bridge fails

**Likely cause:** private bridge files are missing or inaccessible, SSH is blocked, or admin's host identity differs from the recorded key.

**Exact check:**

```bash
stat -c '%U:%a:%F' "$SSH_CONFIG" \
  /srv/openstack-platform/.secrets/ssh/known_hosts
ssh -F "$SSH_CONFIG" platform-admin -- id -un
$PLATFORM_CLI infra list
```

**Fix:** restore the protected files or network access. If admin's identity changed, regenerate the bridge with the matching release using [Releases and upgrades](releases-and-upgrades.md#generate-the-operator-bridge). Do not accept a changed key or redirect the alias by hand. Success is a pinned SSH connection that prints `agentops`.

### The CLI reports a conflict

Exit code `3` and a `conflict:` line mean the CLI refused to proceed.

| Likely cause | Exact check | Fix |
| --- | --- | --- |
| Another command holds a lock | Read the lock name in the conflict and check running operator commands or dashboard collection | Wait for the holder to finish, then repeat the command. |
| An infrastructure operation is unfinished | Read the operation kind and ID in the conflict; inspect its host with `$PLATFORM_CLI infra list` and `$PLATFORM_CLI infra logs <role> --lines 200` | Fix the dependency, then rerun the original command, for example `$PLATFORM_CLI infra replace admin --yes`. |
| Provider drift or uncertain outcome | Compare the named identity and recorded phase with the release evidence and bounded CLI output | Recover the recorded operation through [Hosts and images](hosts-and-images.md#retry-an-interrupted-replacement). |

Only one infrastructure operation can be unfinished. Do not clear it or begin another mutation to bypass it.

## Hosts and the controller

### A host won't come back

**Likely cause:** the VM is stopped, the provider cannot be observed, or first-boot inputs, mounts, or services failed.

**Exact check:** substitute the affected persistent role:

```bash
$PLATFORM_CLI infra list
$PLATFORM_CLI infra logs storage --lines 200
```

A healthy boot prints `<namespace> NixOS <role> services ready`. A failed boot prints `<namespace> NixOS <role> readiness failed`; admin also prints failed units.

| Evidence | Fix |
| --- | --- |
| `stopped` | Run `$PLATFORM_CLI infra start <role>`, then check logs and service health. |
| `active` but hung | Run `$PLATFORM_CLI infra reboot <role> --yes`, then check logs again. |
| Every host is `unknown` | Restore OpenStack API access or the protected `platform-openstack` wrapper. |
| `error` and the old host can still be identified | Diagnose the console, then use [replacement](hosts-and-images.md#replace-a-persistent-host) if needed. |
| `missing` | Confirm provider access and exact ownership evidence. Replacement needs the recorded old server; it cannot recover a deleted host by name. Use [Backups and recovery](backups-and-recovery.md) for lost resources. |
| Reboot repeats a readiness failure | Correct the named dependency, or publish and select a fixed image before replacement. |

Replacement retains the old server, fixed port, and volumes until acceptance. Never delete the old server or detach volumes manually. Cloud-init does not rerun on an existing host; changed first-boot data needs a replacement.

### The controller isn't ready

**Likely cause:** a missing policy, image seed, helper release, or state mount; refused path metadata; or a release/schema mismatch.

**Exact check:** inspect preparation and both services:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- systemctl status \
  "$PLATFORM_NAMESPACE-controller-prepare.service" \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-controller-readiness.service"
recovery sudo journalctl -u "$PLATFORM_NAMESPACE-controller-prepare.service" \
  -u "$PLATFORM_NAMESPACE-controller.service" -n 100 --no-pager
```

A skipped condition names a missing prerequisite. A preparation failure names a refused path or seed. A schema error needs the compatible controller release.

**Fix:** restore the matching helper, policy, seed, or mount. Repair only paths named by [root-path preflight](hosts-and-images.md#preflight-controller-paths-before-an-admin-image-upgrade). Keep the old seed until the hosted selections have moved; do not edit SQLite or migration rows. Then restart and verify:

```bash
recovery sudo systemctl restart "$PLATFORM_NAMESPACE-controller.service"
recovery sudo systemctl restart "$PLATFORM_NAMESPACE-controller-readiness.service"
ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-controller-readiness.service"
```

Require two `active` lines. The controller uses local Unix sockets; a missing portal does not prevent controller readiness.

### Public health fails

**Likely cause:** DNS or TLS failure, a disconnected tunnel, direct-provider filtering, or incorrect hostname forwarding.

**Exact check:** test both hostnames used by the health timer and read ingress boot output:

```bash
curl --fail --show-error --silent "https://$PLATFORM_DOMAIN/healthz"
curl --fail --show-error --silent "https://wildcard-health.$PLATFORM_DOMAIN/healthz"
$PLATFORM_CLI infra logs ingress --lines 200
```

**Fix:** use the failing stage to choose the correction:

| Evidence | Fix |
| --- | --- |
| DNS lookup fails | Route the exact name and wildcard through the HTTPS provider. |
| Certificate validation fails | Serve a trusted certificate covering both hostnames. |
| Provider cannot reach origin | Reconnect the tunnel, or correct the exact direct-provider CIDRs. |
| Wrong route or body | Preserve the original `Host` header and route requests to Traefik. |
| Tunnel token revoked or rotated | [Replace ingress](hosts-and-images.md#replace-the-ingress-host) with a current protected token file. |

Both public requests must return `OK` (the automated checker trims surrounding whitespace). A request to ingress's IP does not test this path. Do not open ingress to `0.0.0.0/0`.

### A platform check fails

**Likely cause:** one of the timer's six dependencies failed, or the timer did not produce a fresh snapshot.

**Exact check:**

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  cat "$PLATFORM_ROOT/persistent/status/$PLATFORM_NAMESPACE.json" |
  /srv/openstack-platform/runtime/python3.14 -m json.tool
ssh -F "$SSH_CONFIG" platform-admin -- systemctl status \
  "$PLATFORM_NAMESPACE-platform-health.timer" \
  "$PLATFORM_NAMESPACE-platform-health.service"
```

Checks run in table order and stop at the first failure. Read `error` first. A storage alarm records `checks.managed_services.state: degraded`; otherwise the first missing entry identifies the failed check. Later checks did not run.

| Failed check | Likely cause | Exact check | Fix |
| --- | --- | --- | --- |
| `openstack` | A core host or worker is not `ACTIVE` | `$PLATFORM_CLI infra list`; affected app in the dashboard | Follow [host recovery](#a-host-wont-come-back) or replay the blocked app request after provider recovery. |
| `public_ingress` | Exact or wildcard health request failed | The two `curl` commands [above](#public-health-fails) | Fix DNS, TLS, tunnel, or forwarding. |
| `managed_services` | Service failure or storage alarm | Read `error` and `checks.managed_services.alarms`; run `$PLATFORM_CLI infra logs storage --lines 200` | Fix the named service; for alarms use [storage checks](#database-writes-or-connections-fail). |
| `nomad` | Too few ready clients or a failed Raft query | `ssh -F "$SSH_CONFIG" platform-admin -- "$PLATFORM_ROOT/bin/$PLATFORM_NAMESPACE-nomad" node status -json` and the same launcher with `operator raft list-peers` | Restore the missing worker's Nomad readiness or admin's Nomad service. |
| `backup` | Missing, incomplete, unsupported, or older-than-36-hour set; failed backup or migration checkpoint receipt | Read `error`, then [backup journals](#a-backup-failed-or-is-missing) | Correct the failing dependency and rerun the backup or migration request. |
| `offsite_recovery` | Export is unconfigured, unmounted, stale, or unverifiable | [Off-site checks](#the-off-site-export-fails) | Configure export, remount its filesystem, or repair and rerun export. |

If the snapshot is absent or stale, read the service journal:

```bash
recovery sudo journalctl -u "$PLATFORM_NAMESPACE-platform-health.service" -n 50 --no-pager
```

Fix the missing credential or other dependency named there. As root, restart the timer and run a fresh check:

```bash
recovery sudo systemctl restart "$PLATFORM_NAMESPACE-platform-health.timer"
recovery sudo systemctl start "$PLATFORM_NAMESPACE-platform-health.service"
```

A passing run writes `healthy: true` with `error: null`. Off-site export must be configured for all six checks to pass.

### Database writes or connections fail

**Likely cause:** a database size or connection limit, an instance memory limit, an unavailable instance, or storage-host capacity pressure.

Each PostgreSQL or MongoDB resource has its own server on storage, under `<namespace>-database@<id>.service`. CPU, memory (without swap), connection, and XFS project quotas apply per instance. Garage remains shared, with byte and object quotas per bucket.

**Exact check:** read bounded storage metrics through the privileged controller socket:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- curl --fail-with-body --silent --show-error \
  --unix-socket "/run/$PLATFORM_NAMESPACE-controller/privileged.sock" \
  http://localhost/v1/admin/status |
  /srv/openstack-platform/runtime/python3.14 -c \
  'import json,sys; print(json.dumps(json.load(sys.stdin)["storageHost"], indent=2))'
```

These metrics are cached. Check their observation time and the health snapshot's alarm codes before acting. Inspect the affected resource's usage and limits in the portal.

| Evidence | Fix |
| --- | --- |
| MongoDB writes blocked above the size limit | Delete data until usage is at most 95% of the limit, or raise the resource's size limit. Reads and deletes still work. The hard XFS quota is 150% of the size limit. |
| `instance_connections_high` | Reduce the app's connection pool or raise that instance's connection limit. The MongoDB URI uses `maxPoolSize` equal to the limit; mongod permits another 10 connections. PostgreSQL `max_connections` is the limit plus 5. |
| Instance fails after a connection or memory change | Those changes restart that instance. Check its readiness and replay the original limit-change request if it requires recovery. |
| `instance_unavailable` | Restore the instance's service dependency; do not redirect the app to a fallback shared database. |
| `storage_volume_high` | Inspect managed data and registry usage before requesting volume capacity. The registry is not backed up; app images can be rebuilt from Git. |
| `storage_memory_high` or `storage_container_memory_high` | Inspect the host and instance memory budgets, then use the supported sizing or limit-change procedure. |

Shared PostgreSQL and MongoDB servers can still hold sealed fallback copies after migration. New resources use instances. [Migrate storage instances](migrate-storage-instances.md) covers shared resources that still need migration before raising connection limits.

### S3 creation reports `BACKUP_KEY_MISSING` or requires recovery

**Likely cause:** the helper cannot read the persisted Garage backup access-key ID, that exact key is absent, or candidate cleanup could not prove absence.

Check that `<paths.root>/secrets/garage-backup.env` on admin contains a nonempty `GARAGE_BACKUP_ACCESS_KEY`, then inspect that exact ID in Garage's key inventory. Keep the secret access key out of logs and shared diagnostics. Multiple keys named `platform-backup` are allowed; the helper uses the persisted ID. Restore the matching backup credentials or follow [Backups and recovery](backups-and-recovery.md) to initialize the backup key before retrying creation.

For a recovery-required create, inspect the helper diagnostic and exact operation-owned bucket and app-key IDs. A deleted bucket alone does not prove complete cleanup. Confirm app-key absence too, and replay the original request and idempotency key after resolving the recorded uncertainty. Do not delete resources solely by their display name.

## Backups and restores

### A backup failed or is missing

**Likely cause:** a missing recipient or mount, a stopped timer, a concurrent backup, or a failed database/S3 dependency.

**Exact check:** identify the backup class and read its result:

| Set | Command |
| --- | --- |
| Operator state (operator-host user unit) | `systemctl --user status openstack-platform-backup.service` |
| Hosted controller and deploy keys | `recovery sudo journalctl -u "$PLATFORM_NAMESPACE-hosted-controller-backup.service" -n 20 --no-pager` |
| Portal broker | `recovery sudo journalctl -u "$PLATFORM_NAMESPACE-management-broker-backup.service" -n 20 --no-pager` |
| Managed data | `recovery sudo journalctl -u "$PLATFORM_NAMESPACE-platform-backup.service" -n 20 --no-pager` |
| Pre-migration database checkpoint | `recovery sudo journalctl -u "$PLATFORM_NAMESPACE-resource-backup@<type>-<db>.service" -n 20 --no-pager` (replace `<type>` with `postgres` or `mongo`, and `<db>` with the database name from the migration) |

Managed-data backups run at 03:15 UTC plus up to 30 minutes. A failed pre-migration checkpoint blocks migration and also fails the health snapshot's `backup` check.

| Evidence | Exact check | Fix |
| --- | --- | --- |
| Portal backup skipped | `recovery sudo stat -c '%U:%a:%F' "$PLATFORM_ADMIN_STATE/management-broker-releases/config/backup-recipient.txt"` | Install the protected recipient through [Verify backups and schedules](backups-and-recovery.md#verify-backups-and-schedules). |
| User timer stops at logout | `loginctl show-user "$USER" --property=Linger` | Have the operator-host administrator enable lingering; require `Linger=yes`. |
| `retention=failed reason=<reason>` | Read the named pruning error in the backup journal | The backup committed; repair pruning and rerun the backup tool. |
| `another platform backup is running` | `ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active "$PLATFORM_NAMESPACE-platform-backup.service"` | Wait for the current backup. |
| S3 archive fails | Read the managed-data journal; run `$PLATFORM_CLI infra logs storage --lines 200` | Restore bucket access or Garage health, then take a fresh backup. |
| Backup mount missing | `ssh -F "$SSH_CONFIG" platform-admin -- mountpoint "$PLATFORM_BACKUPS"` | Restore the intended mount before rerunning backups. |
| Restore check fails | Read the failing `postgres`, `mongodb`, or `garage` stage in the restore-check output | Keep the set; fix the identity, checksum, archive, or test-container dependency, then repeat the check. |

**Fix and verify:** rerun the affected backup using [Backups and recovery](backups-and-recovery.md), then require a committed set and a passing restore check. Never publish a staged file manually or restore an unchecked archive over live data.

### The off-site export fails

**Likely cause:** export is not configured, its destination is not mounted, its inputs are incomplete, or its receipt has expired. As verified on 2026-10-10, production has no off-site export configured, so `offsite_recovery` fails there.

**Exact check:**

```bash
ssh -F "$SSH_CONFIG" platform-admin -- systemctl status \
  "$PLATFORM_NAMESPACE-offsite-export.service"
ssh -F "$SSH_CONFIG" platform-admin -- \
  stat -c '%U:%a:%F' "$PLATFORM_ROOT/persistent/offsite-export.json"
recovery sudo journalctl -u "$PLATFORM_NAMESPACE-offsite-export.service" -n 20 --no-pager
```

A missing config skips the export unit, but the health checker still attempts `offsite_recovery` and fails. It does not count an unconfigured export as healthy.

| Evidence | Exact check | Fix |
| --- | --- | --- |
| Missing config or skipped condition | The `stat` and unit-status commands above | [Configure export](backups-and-recovery.md#export-recovery-evidence-off-site) to a separate filesystem mounted on admin. |
| Config must be current-user-owned mode `0600` | The config's `stat` result | Reinstall it as `agentops`, mode `0600`. |
| Destination is not a distinct mounted filesystem | On admin, run `findmnt --mountpoint <DESTINATION>` using the configured path | Mount it. Remount after every admin replacement. |
| Mount identity mismatch | On admin, run `findmnt --mountpoint <DESTINATION> -o SOURCE,FSTYPE,TARGET` | Restore the intended mount; change configuration only after reviewing the new identity. |
| Destination uses local backup filesystem | Compare destination and backup mounts with `findmnt` | Use separate storage. |
| Destination ownership or mode refused | On admin, run `stat -c '%U:%a:%F' <DESTINATION>` | Make the mount point `agentops`-owned, mode `0700`. |
| Missing committed component | Read the named component in the export journal | Run that backup first. An installed portal requires its broker backup in the bundle. |
| Stale or mismatched receipt | Read the receipt below and compare `exportedAt` with the configured `maximumReceiptAgeHours` | Repair the mount or inputs, then export a new verified bundle. |

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  cat "$PLATFORM_ROOT/persistent/status/offsite-export.json"
```

**Fix and verify:** after correcting the cause, run export and read its result:

```bash
recovery sudo systemctl start "$PLATFORM_NAMESPACE-offsite-export.service"
recovery sudo journalctl -u "$PLATFORM_NAMESPACE-offsite-export.service" -n 20 --no-pager
```

Require `offsite-scheduled-export=<namespace>-<timestamp> verified=true`, then a passing `offsite_recovery` entry in the next health snapshot. A failed export leaves the previous receipt and bundle intact.

### A restore is refused

**Likely cause:** invalid input metadata, incompatible state, an active writer, or damaged backup evidence.

**Exact check:** keep the launcher's refusal text. Check metadata without printing contents:

```bash
stat -c '%U:%a:%F:%s' '<BACKUP_FILE>' '<AGE_IDENTITY_FILE>'
```

For hosted input files, run this through the recovery account on admin. Check the unit named by a refusal with `systemctl is-active <UNIT>` on its host.

**Fix:** correct the named input in a private directory and rerun the installed launcher using [Backups and recovery](backups-and-recovery.md). A hosted-controller restore can commit the database before a deploy-key restore fails: keep the controller stopped and repeat the restore. Managed-data restore writes to services in sequence, so a later failure can leave a partial restore; keep those services isolated.

| Evidence in refusal | Fix |
| --- | --- |
| Owner, mode, or file type | Restore inputs and identities must be direct files (not links) owned by the expected account, mode `0600`. Hosted-controller inputs on the admin host must be owned by `platform-controller`, portal inputs by `management-broker`. |
| An active unit named by the restore launcher | Stop the named unit first. See the procedures in [Backups and recovery](backups-and-recovery.md). |
| Lock, or `another operation holds` | Stop the operator's backup timer, the dashboard, and any running operator command |
| Different deployment identity | The backup belongs to another deployment, or you used the wrong platform config |
| `UNSUPPORTED_PRIOR_STATE` or schema | The backup comes from an unsupported or newer version. See [Recovery rules](#recovery-rules). |
| Integrity or foreign keys | The file is damaged. Pick an earlier set and check its checksum. |
| Unfinished operations in the backup | Pick a set taken when nothing was in progress |
| Unfinished operations in the current database | Resume or finish them first |
| Sidecar files (`-wal`, `-shm`) | The database wasn't closed cleanly. Make sure the service using it is stopped. |
| Size | SQLite backups are limited to 1 GiB; age identities to 64 KiB and deploy-key archives to 32 MiB |

## The operator dashboard

### The dashboard won't open or shows warnings

| Symptom | Likely cause | Exact check | Fix |
| --- | --- | --- | --- |
| The browser can't connect | Dashboard or SSH forward stopped | `systemctl --user status openstack-platform-dashboard` on the operator host | Restart the dashboard and reconnect the [SSH forward](run-the-platform.md#start-it-and-connect). Enable lingering if logout stopped it. |
| Loopback `Host` refusal | Browser used another hostname | Read the browser address | Open `http://localhost:<port>`. |
| `Lost contact with the dashboard service` | Forward or service disconnected | Check the forward's terminal and dashboard user service | Restore the connection. The page retains the last snapshot. |
| **Controller API** says `Unreachable` | Admin read failed | `$PLATFORM_CLI status` | Follow [dependency checks](#the-cli-reports-a-dependency-unavailable). |
| `Snapshot is stale` or `Snapshot unavailable` | Health timer stopped or snapshot unreadable | Timer and service commands under [A platform check fails](#a-platform-check-fails) | Repair the named dependency and run the health service. |
| OpenStack observation unavailable | Provider read failed or timed out | `$PLATFORM_CLI infra list` | Restore API/wrapper access; wait if operator state is locked. |
| `Last seen healthy` or an older server state | Current read failed; displayed value is cached | Check the footer's source ages | Repair the source and wait for a fresh collection. |

## Apps

### An app deploy is stuck or failed

A deployment accepts a new version only after scheduler and public health checks pass. A build or candidate-health failure normally leaves the accepted version in place. A failure after traffic cutover can still require recovery or rollback; inspect the phase and cleanup result before assuming the old version is serving.

**Likely cause:** source access, package or runtime validation, candidate health, or an uncertain helper/provider outcome.

**Exact check for a failed deploy:**

- In the owner portal, the deployment page shows the build output and, for a version that stopped after starting, a record of why it stopped. Owners see guidance; staff and portal admins also see an internal error code.
- Operators can read build and runtime logs through the controller; see [Deploy apps from the command line](deploy-apps-from-the-command-line.md).

Check for these app failures: no `package.json` at the repository root, a missing lockfile, a health path that redirects, doesn't return 2xx, or returns more than 4096 bytes, or a Node.js or Bun version request the platform refuses. A private repository without a working deploy key fails with `SOURCE_REJECTED`. Point the owner to [Deploy an app](for-app-owners.md).

**Fix:** correct a definitive app/build failure and submit a new deployment. For an uncertain outcome, preserve the original request.

**A deploy is stuck or needs recovery.** The operation is `recovery_required`: something outside the controller (the helper, OpenStack, Nomad, a lock) gave an answer the controller couldn't classify, so it stopped and kept the app on hold rather than guess.

1. Find the operation in the dashboard's **Operations** list (status `Needs recovery`). Note its ID, phase, and error. With the `admin` helper from [the command-line guide](deploy-apps-from-the-command-line.md#set-up-your-shell), check `admin "http://localhost/v1/admin/operations/<OPERATION_UUID>"`. Read `status`, `phase`, `safeError`, and `cleanupState`.
2. Fix the cause the error names. Releases that don't match are a common one: install matching controller and helper releases (see [Releases and upgrades](releases-and-upgrades.md)).
3. Resume it with the identical request:
   - In the owner portal, the request shows **This application requires controller recovery.** with a **Resume** button. Pressing it sends the same request again.
   - For a request an operator sent to the controller, repeat it with the same method, path, body, and `Idempotency-Key` (the original request UUID). Use the saved body and key as shown in [Resume an interrupted operation](deploy-apps-from-the-command-line.md#resume-an-interrupted-operation).

Don't send a changed request under the old key (the controller treats it as a conflict), and don't clear the operation to free the app. An environment-variable change that ended unknown asks you to enter the value again; that is expected, because values are never stored where the portal can read them back.

### App changes are blocked with 'wait until recovery'

**Likely cause:** an earlier change still owns work for the app. The conflict names its blocking operation ID. `OPERATION_CONFLICT` covers unfinished foreground work; `POST_ACCEPTANCE_CONFLICT` covers a change that could interfere with an accepted deployment's finishing work. The portal can show `APP_BUSY` until it has observed the controller's finishing state.

**Exact check:** use the `admin` and `wait_for` helpers from [Deploy apps from the command line](deploy-apps-from-the-command-line.md#set-up-your-shell) to read the operation through the privileged socket:

```bash
OPERATION_ID='<BLOCKING_OPERATION_UUID>'
admin "http://localhost/v1/admin/operations/$OPERATION_ID" > operation.json
jq '{operationId, kind, status, phase, safeError, cleanupState,
     finishing, finishingRetryAttempts, nextRetryAt}' operation.json
```

If `finishing` is `true`, acceptance already succeeded. The controller retries address handover, exact predecessor cleanup, or registry retention automatically, with five waits totaling 25 minutes plus execution time. `status: running` and a `nextRetryAt` mean another attempt is scheduled. Environment set/delete/import, storage verify/rotate, storage limit changes and usage collection, and restart can proceed. Enable can be a no-op after handover. Disable also waits for confirmed predecessor job absence, so an old route cannot keep serving; creating a worker for a stopped app must wait. Another deploy, resize, rollback, deletion, or address change must wait until finishing completes.

An older checkpoint can have `cleanupState: confirmed` because its builder was removed while predecessor cleanup still remains. Read `status`, `finishing`, and `nextRetryAt` together.

**Fix:** allow scheduled finishing work to run while you correct its dependency. After all five retries fail, `status` becomes `recovery_required` and `nextRetryAt` is `null`. Read the controller journal with `recovery sudo journalctl -u "$PLATFORM_NAMESPACE-controller.service" -n 100 --no-pager` to find the bounded `helper failure` line: it names the action, error class, and safe code. Fix that dependency, then replay the original request with its original `Idempotency-Key`. Replaying can also request an earlier attempt while the controller is waiting; it shares admission with the automatic retry. Do not use a new key, edit state, or remove the predecessor by hand.

For command-line requests, use the saved body and key with the same socket helper, HTTP method, and path. Then run `wait_for "$OPERATION_ID"`; success means the operation reads `succeeded` and cleanup is confirmed. A healthy active app alone does not prove finishing completed.

For portal-started operations, the broker's `intents` table in `<adminState>/management-broker/management.sqlite3` records the exact `method`, `path`, `body`, and `controller_key`. Use a read-only query through the recovery account to select the row whose `controller_key` matches the operation ID. Keep the body in a private file, remove only the broker's server-only `_portalAdmin` marker if present, and replay the recorded method/path/body with `Idempotency-Key: <controller_key>` as `management-broker` on the project socket. The `project` helper in the command-line guide uses that account. Never print request bodies or secret values. Environment values are omitted from broker state; a lost environment request needs its original value supplied again.

If `finishing` is `false`, the controller has not proven acceptance and cannot automatically repeat the request. Read the phase and safe error, fix the cause, and replay the exact original request and key. [An app deploy is stuck or failed](#an-app-deploy-is-stuck-or-failed) describes candidate and health failures.

## Owner portal

### People can't sign in to the portal

**Likely cause:** a cancelled or expired sign-in, account state, wrong local credentials, request limits, or an unavailable identity service.

**Exact check:** read the sign-in message and code, then use the matching row:

| Symptom/code | Likely cause | Exact check | Fix |
| --- | --- | --- | --- |
| `COMMONS_CANCELLED` | Person cancelled on Commons | Confirm whether they pressed Cancel | Start again from the portal. |
| `SIGN_IN_EXPIRED` | One-time code expired, was used, or is invalid; browser flow exceeded 10 minutes | Retry once from a fresh portal sign-in page; if one account still fails, check its Commons state | Renew the sign-in or correct the Commons account/approval. |
| `IDENTITY_UNAVAILABLE` | Portal identity service cannot reach Commons | Check the three services and identity journal below | Restore DNS, TLS, or allowed network access. |
| `ACCOUNT_DISABLED` | Portal account is disabled or inactive | Portal admin opens **Accounts** and checks its state | Activate the intended account if authorized. |
| `RATE_LIMITED` | Address or account exceeded sign-in limits | Confirm the error after repeated attempts | Wait and retry. |
| `INVALID_CREDENTIALS` | Wrong local password or authenticator code | Check username and use a fresh code | Correct credentials; wait if codes were rate-limited. |
| `CSRF_REJECTED` | Sign-in page expired | Reload the page | Retry from the new page. |
| `AUTH_UNAVAILABLE` | Portal cannot finish authentication | Check broker/web unit state and their journals | Restore the named service dependency. |

**Checks and fix for Commons availability:** people who are already signed in stay signed in; only new sign-ins fail.

1. Check the portal's services (as `agentops`):

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
     "$PLATFORM_NAMESPACE-management-identity.service" \
     "$PLATFORM_NAMESPACE-management-broker.service" \
     "$PLATFORM_NAMESPACE-management-web.service"
   ```

2. Read why the identity service failed (**root**):

   ```bash
   recovery sudo journalctl -u "$PLATFORM_NAMESPACE-management-identity.service" -n 50 --no-pager
   ```

   Look for `identity-check=unavailable reason=<reason>`. A reason starting with `dns:` points to name resolution, `tls-verify:` or `tls:` to Commons' certificate, and a timeout to the network path. The identity service may only reach the addresses in `ownerPortal.identityEgressCidrs` (or the platform's default ranges); if Commons moved, update that setting. See [Configuration](../reference/configuration.md).

3. Check that Commons itself is up and that it accepts your portal. Commons must allow the portal's origin (`https://<domain>`) through its `CONNECT_APP_DOMAIN` setting. If Commons shows an error instead of asking the student to approve, fix that on the Commons side.

**Fix for a locked-out portal admin:** check the local account's state in **Accounts** using another portal admin if available. If no portal admin can sign in, issue a setup link for a new local portal admin from admin, then use it to fix or reset the old account. See [Open the owner portal](open-the-portal.md). If Commons is stopped, new Commons sign-ins fail; existing sessions keep working. A local portal admin can still sign in.

## Images

### An image change is refused

**Likely cause:** incomplete provenance, wrong ownership or OpenStack status, incompatible metadata, changed selections, or an expired prune plan.

**Exact check:**

```bash
$PLATFORM_CLI infra image list
$PLATFORM_CLI infra list
```

Compare exact image IDs, roles, and commits with the signed publication evidence. For prune failures, read the refused drift or expiry condition. For hosted selection, inspect its operation and expected current image ID using [Hosts and images](hosts-and-images.md#update-hosted-role-image-selections).

**Fix:** correct the named cause. Rerun `infra image prune` to review a fresh plan before applying it. A hosted selection in `recovery_required` needs its original body and request ID; a changed expected ID needs a new request after re-reading selections. Do not overwrite tested image names or widen deletion scope manually.

## Recovery rules

When an outcome is uncertain, preserve the evidence and recover the recorded operation.

- **Same action, same request.** Operations are recorded before they act and checked again when they resume. To recover, repeat the identical command, or for the controller the same method, path, body, and idempotency key. A changed request under an old key is a conflict, not a retry.
- **Look again before you act.** An unavailable reading doesn't erase accepted state. Diagnose the named dependency, then decide.
- **An uncertain outcome keeps its scope on hold.** Other operations can't change that scope. Fix the dependency and resume; don't clear it.
- **Never guess identities.** Don't substitute example IDs, hand-edit UUIDs to match, or treat a matching name as ownership. The platform never adopts, changes, or deletes a resource based on its name alone.
- **Leave the evidence alone.** Don't edit database rows, migration records, ownership markers, cloud metadata, or generated secrets. Don't bypass the helper. Don't delete an old server or detach a volume during a replacement.
- **Rollbacks are separate.** Rolling back a release, restoring a database, and recovering cloud state are three separate operations. Rolling back code doesn't roll back data.
- **Unsupported old state stays put.** `UNSUPPORTED_PRIOR_STATE` can mean an unsupported control surface or an unknown future migration. For future migrations, use the matching newer release. Archive unsupported legacy state and start a new namespace with empty state and backup directories. Don't edit migration rows, ownership markers, or cloud metadata to force adoption.
- **Record only safe evidence.** In your tickets and notes, keep exact non-secret identities, the operation or correlation ID, the phase, the failed check, checksums, and readiness results. Keep provider output, credentials, secret values, and age identities out.

## Related

- [Run the platform](run-the-platform.md)
- [Backups and recovery](backups-and-recovery.md)
- [Hosts and images](hosts-and-images.md)
- [Deploy apps from the command line](deploy-apps-from-the-command-line.md)
- [Open the owner portal](open-the-portal.md)
- [Manage apps and people](manage-apps-and-people.md)
- [Operator CLI reference](../reference/operator-cli.md)
- [Internals](../reference/internals.md)
