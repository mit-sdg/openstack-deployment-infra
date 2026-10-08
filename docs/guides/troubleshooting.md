# Troubleshooting

Find your symptom below, check the evidence, and fix the cause. Read [Before you change anything](#before-you-change-anything) before attempting recovery.

The commands use the shell variables and the `recovery` function from [Run the platform](run-the-platform.md#set-up-your-shell).

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

`setup check` only reads from OpenStack; it hasn't changed anything. It prints `setup-check=ready` or `setup-check=failed` on its first line.

Rerun it with JSON output to see each finding:

```bash
uv run openstack-platform setup check --env-file /private/path/setup.env --json
```

Look at quota shortfalls, fixed-address availability, name collisions with existing resources, missing local tools, ingress settings, and release evidence. Fix the protected setup file or ask your cloud for more quota, then check again. See [Deploy the platform](deploy-the-platform.md).

### Setup stopped partway

Setup records each step and checks it again when it resumes. Keep the workspace and `/srv/openstack-platform/.secrets/setup` exactly as they are. Fix the dependency named in the error, then rerun the identical `setup ... --apply` command with the same setup file and workspace.

Don't edit or delete a partly created server, port, volume, keypair, image, database row, or generated secret to force progress. Setup reuses a resource only when it matches exactly what it expects. To change the project, names, addresses, or volume sizes, start over with an empty workspace and an empty set of cloud resources.

### Project or deployment identity mismatch

The credentials or inventory you loaded don't belong to the deployment the state describes. Stop before changing anything, and load the intended OpenStack credentials and inventory. Never substitute an example ID, or make two forms of a UUID match by hand.

### An unexpected server, port, volume, image, or host key

Something in the project looks like it belongs to the deployment but doesn't match its records. Work out who owns it. Don't rename, detach, adopt, or delete it because its name resembles the inventory: a name match is never proof of ownership.

## Status and the CLI

### `status` shows degraded

`STATE` turns `degraded` for any of these reasons. Find which one from the other columns:

| Column | What it means | Next step |
| --- | --- | --- |
| `INFRA` below `5` | A role has no selected image in the operator state | Select one; see [Hosts and images](hosts-and-images.md) |
| `UNAVAILABLE` above `0` | Something couldn't be observed: a persistent host, an app, or a storage resource | `$PLATFORM_CLI infra list` for hosts; the dashboard's **Needs attention** list for apps and storage |
| `UNHEALTHY` above `0` | Something answered with a problem, such as a failing app route or a host in `error` | The dashboard shows which app or host |
| All counts look fine | An operation is unfinished in the operator or the controller | The dashboard's **Operations** list; see [An app deploy is stuck or failed](#an-app-deploy-is-stuck-or-failed) or [The CLI reports a conflict](#the-cli-reports-a-conflict) |

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

The `platform-admin` alias pins the admin host's address, account, host key, and identity. If it stops working:

- Check that `$SSH_CONFIG` and the known-hosts file next to it exist and are private to you (look at their metadata only; don't print them).
- If the admin host was replaced outside the supported path, or the host key really changed, regenerate the bridge with the matching release. See [Releases and upgrades](releases-and-upgrades.md).

Don't edit host keys by hand, and don't point the alias at a different host.

### The CLI reports a conflict

Exit code 3 with a `conflict:` message means the CLI refused to act because something else is in progress:

| Message | Meaning | What to do |
| --- | --- | --- |
| A lock message (such as `another operation holds the infrastructure lock` or `could not acquire the infrastructure lock before the deadline`) | Another operator command or dashboard collection is using the operator state | Wait for it to finish and try again |
| An unfinished infrastructure operation, with its kind and ID | An earlier infrastructure command did not finish | Fix what stopped it, then rerun that same command to complete it (for example, `$PLATFORM_CLI infra replace admin --yes`) |
| A recovery-required or drift message | The CLI found a result it cannot classify, or cloud state that differs from its records | Read the named resource and phase, fix the dependency, and rerun the same command. See [Hosts and images](hosts-and-images.md). |

Only one infrastructure operation can be unfinished at a time, so finish it before starting anything else.

## Hosts and the controller

### A host won't come back

A persistent host (admin, ingress, or storage) shows `stopped`, `error`, `missing`, or `unknown` in `infra list`, or shows `active` but its services never become ready.

1. See what OpenStack reports and what the host printed while booting:

   ```bash
   $PLATFORM_CLI infra list
   $PLATFORM_CLI infra logs storage --lines 200
   ```

   Use `admin`, `ingress`, or `storage`. A healthy boot ends with `<namespace> NixOS <role> services ready`; a failed one prints `<namespace> NixOS <role> readiness failed`, and the admin host also prints its failed units.

2. Act on what you found:

   | What you see | What to do |
   | --- | --- |
   | `stopped` | `$PLATFORM_CLI infra start <role>` |
   | `active` but hung or not ready | `$PLATFORM_CLI infra reboot <role> --yes`, then check the serial log again |
   | `unknown` for every host | OpenStack or the protected `platform-openstack` wrapper isn't answering. Check your cloud's status and credentials. |
   | `missing` or `error` | Don't recreate it by hand. Use the supported replacement path. |
   | Ready fails the same way after a reboot | Compare the selected image, flavor, fixed port, volumes, and metadata with the inventory. Fix the dependency, or publish a fixed image and replace the host. |

   A replacement keeps the fixed port and volumes and keeps the old server until the new one passes its checks. See [Hosts and images](hosts-and-images.md). Cloud-init doesn't run again on an existing host, so changing user data needs a replacement.

Never delete the old server or detach a volume by hand during a replacement.

### The controller isn't ready

The controller or its readiness unit isn't active on the admin host.

1. See the unit state (as `agentops`):

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl status \
     "$PLATFORM_NAMESPACE-controller.service"
   ```

   If the status says a start condition wasn't met, a required file is missing: the controller starts only when its policy, its image selections, and a complete helper release are present.

2. Read the journal (**root**):

   ```bash
   recovery sudo journalctl -u "$PLATFORM_NAMESPACE-controller.service" -n 100 --no-pager
   ```

3. Restore the missing or mismatched piece: the matching helper release, the policy, or the state volume. Then restart the controller (**root**):

   ```bash
   recovery sudo systemctl restart "$PLATFORM_NAMESPACE-controller.service"
   ```

   A restart carries through to the readiness unit and the owner portal, which restart after it.

The controller has no network listener. The portal depends on the controller, but a missing or broken portal doesn't stop the controller from being ready.

### Public health fails

`curl https://$PLATFORM_DOMAIN/healthz` doesn't return exactly `OK`. Check these in order, because each depends on the one before:

1. The exact hostname resolves to your HTTPS provider.
2. The certificate is trusted by a browser.
3. The tunnel is connected, or (for a direct origin) the provider's address ranges are allowed.
4. The provider forwards the original `Host` header.
5. The ingress host is up and ready (`$PLATFORM_CLI infra logs ingress --lines 200`).
6. The body is exactly `OK`.

A request straight to the ingress host's IP address doesn't test the same path. Never open the ingress to `0.0.0.0/0` to diagnose. If the tunnel token was rotated or revoked, replace the ingress host with a current token file; see [Hosts and images](hosts-and-images.md).

### A platform check fails

The health timer on the admin host runs six checks every five minutes, in order, and stops at the first failure. Read the snapshot (see [Check health](run-the-platform.md#check-health)). The first check missing from `checks` failed; `error` gives its exception type and detail. The dashboard marks the failed check under **Platform checks**.

| Failed check | Typical message | Where to look |
| --- | --- | --- |
| `openstack` | `a core OpenStack server is not ACTIVE` or `an application worker is not ACTIVE` | [A host won't come back](#a-host-wont-come-back); for a worker, the affected app in the dashboard |
| `public_ingress` | An error beginning `public ingress health response is unexpected for`, followed by the hostname | [Public health fails](#public-health-fails). The check tests both names, so verify the DNS record and wildcard certificate. |
| `managed_services` | A failed service-check command | The storage host: `$PLATFORM_CLI infra logs storage --lines 200` |
| `nomad` | `fewer ready Nomad clients than active workers` | A worker's Nomad client isn't ready; find the affected app in the dashboard |
| `backup` | `no encrypted platform backup exists`, `latest encrypted platform backup is stale` (older than 36 hours), `latest platform backup is incomplete`, or `latest platform backup format is unsupported` | [A backup failed or is missing](#a-backup-failed-or-is-missing) |
| `offsite_recovery` | A failed recovery-status command | [The off-site export fails](#the-off-site-export-fails) |

If the dashboard says `Snapshot is stale` or `Snapshot unavailable`, check the timer and its service. They may be stopped, failing before writing a snapshot, or unable to provide it to the dashboard:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl status "$PLATFORM_NAMESPACE-platform-health.timer"
```

## Backups and restores

### A backup failed or is missing

First work out which set failed. Each has its own job:

| Set | Job | Read its result |
| --- | --- | --- |
| Operator state | `openstack-platform-backup.service` (user unit on the operator host) | `systemctl --user status openstack-platform-backup.service` |
| Hosted controller and deploy keys | `<namespace>-hosted-controller-backup.service` | **Root:** `recovery sudo journalctl -u "$PLATFORM_NAMESPACE-hosted-controller-backup.service" -n 20 --no-pager` |
| Owner portal | `<namespace>-management-broker-backup.service` | **Root:** the same with `management-broker-backup` |
| Managed data | `<namespace>-platform-backup.service` | **Root:** the same with `platform-backup` |

Common causes:

- **The portal backup never appears, yet the unit reports success.** Its recipient file is missing, so the unit skips itself. See the recipient check in [Verify backups and schedules](backups-and-recovery.md#verify-backups-and-schedules).
- **Operator-state backups stop when you log out.** Lingering is off for the operator account, so its user timer stops with your session. See [Open the operator dashboard](run-the-platform.md#start-it-and-connect).
- **`retention=failed reason=<reason>`.** The new backup is committed; only pruning failed. Fix the reported pruning failure and rerun; unexpected names are normally ignored.
- **`another platform backup is running`.** A managed-data backup is already in progress. Wait for it.
- **The managed-data backup fails on S3.** A bucket couldn't be read, or Garage's bucket list changed during the backup. Run a fresh backup; if it fails again, check the storage host.
- **The backup volume isn't mounted on the admin host.** The admin backup units won't run without it. Check the admin host's boot log with `$PLATFORM_CLI infra logs admin --lines 200`.
- **The managed-data restore check fails.** Keep the failing set. Read which stage failed (`postgres`, `mongodb`, or `garage`) and fix the named dependency, such as the identity file, a checksum, or the archive.

Whatever the cause, keep the failed set's evidence, fix the named dependency, and rerun the same tool. Never publish a staged file by hand, and never restore an archive that hasn't passed its checks over live data.

### The off-site export fails

Read the reason (**root**):

```bash
recovery sudo journalctl -u "$PLATFORM_NAMESPACE-offsite-export.service" -n 20 --no-pager
```

Failures print `recovery bundle failed: <reason>`. The common ones:

| Reason | Fix |
| --- | --- |
| The journal says the unit was skipped because a condition wasn't met | The export isn't configured. Install `$PLATFORM_ROOT/persistent/offsite-export.json`; see [Export recovery evidence off site](backups-and-recovery.md#export-recovery-evidence-off-site). |
| `off-site export config must be current-user-owned mode 0600` | Reinstall the config with `install -m 0600` as `agentops` |
| `off-site destination is not a distinct mounted filesystem` | The destination isn't mounted (common after an admin host replacement). Mount it again. |
| `off-site destination mount identity does not match configuration` | The mount source or filesystem type changed. Check it with `findmnt` and update the config only if the new mount is the one you intend. |
| `off-site destination must not use the local backup filesystem` | The destination is on the backup volume's device. Use separate storage. |
| `off-site destination must be mode 0700` or `recovery destination must be owned by the current user` | Make the mount point owned by `agentops` with mode `0700` |
| `no committed managed-data backup exists` | Fix the managed-data backup first |
| A missing committed SQLite backup or an incomplete set, naming the affected component | Verify that each required set exists and is committed. For `management-broker`: once the owner portal is installed, every bundle must include its backup. Run the missing backup, then export again. |

A failed export never replaces the previous receipt or bundle.

The health check's `offsite_recovery` step can also fail while exports succeed. It fails when the newest receipt is older than `maximumReceiptAgeHours`, when the receipt no longer matches the bundle on the mount, and when the portal is installed but the newest bundle has no portal set. Check the receipt's `exportedAt` time:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  cat "$PLATFORM_ROOT/persistent/status/offsite-export.json"
```

### A restore is refused

SQLite restore tools validate inputs before replacing the database. Fix a refused input in a private directory, then rerun the installed launcher. A hosted-controller restore can commit the database before a deploy-key restore fails: keep the controller stopped and repeat the restore. Managed-data restore writes to services in sequence, so a later failure can leave a partial restore; keep those services isolated.

| Reason mentions | What to fix |
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

| You see | What it means | What to do |
| --- | --- | --- |
| The browser can't connect | The SSH forward or the dashboard isn't running | Check `systemctl --user status openstack-platform-dashboard` on the operator host and reconnect the forward. If it stopped when you logged out, turn on lingering. |
| `The dashboard only answers loopback Host names.` | You opened it with a name other than `localhost`, `127.0.0.1`, or `[::1]` | Open `http://localhost:<port>` |
| `Lost contact with the dashboard service` | The forward or the dashboard process stopped while the page was open | As above; the page keeps showing the last snapshot meanwhile |
| The **Controller API** signal says `Unreachable` | The admin read failed | Run `$PLATFORM_CLI status`. If it reports the controller unavailable, see [The CLI reports a dependency unavailable](#the-cli-reports-a-dependency-unavailable). The dashboard keeps the last controller records and labels their age. |
| `Platform health`: `Snapshot is stale` or `Snapshot unavailable` | Health snapshots aren't reaching the dashboard | Check `<namespace>-platform-health.timer`; see [A platform check fails](#a-platform-check-fails) |
| `OpenStack`: `Server state unavailable` or `Provider observation unavailable` | The provider read failed or timed out | Run `$PLATFORM_CLI infra list`. `unknown` there points to the `platform-openstack` wrapper or the OpenStack API. The message `operator state is locked by another command` clears once that command finishes. |
| A status says `Last seen healthy`, or prefixes an older server state with Last seen | Current reads failed; values reflect the last successful observation | Check the footer to see which data source is failing |

## Apps

### An app deploy is stuck or failed

A deployment builds a new version alongside the running one and switches over only after the new version passes its health check. When a new version fails, the platform removes it and the previous accepted version keeps serving.

**A deploy failed.** Find out why before trying again:

- In the owner portal, the deployment page shows the build output and, for a version that stopped after starting, a record of why it stopped. Owners see guidance; staff and portal admins also see an internal error code.
- Operators can read build and runtime logs through the controller; see [Deploy apps from the command line](deploy-apps-from-the-command-line.md).

Common causes are in the app, not the platform: no `package.json` at the repository root, a missing lockfile, a health path that redirects, doesn't return 2xx, or returns more than 4096 bytes, or a Node.js or Bun version request the platform refuses. A private repository without a working deploy key fails with `SOURCE_REJECTED`. Point the owner to [Deploy an app](for-app-owners.md).

**A deploy is stuck or needs recovery.** The operation is `recovery_required`: something outside the controller (the helper, OpenStack, Nomad, a lock) gave an answer the controller couldn't classify, so it stopped and kept the app on hold rather than guess.

1. Find the operation in the dashboard's **Operations** list (status `Needs recovery`). Note its ID, phase, and error.
2. Fix the cause the error names. Releases that don't match are a common one: install matching controller and helper releases (see [Releases and upgrades](releases-and-upgrades.md)).
3. Resume it with the identical request:
   - In the owner portal, the request shows **This application requires controller recovery.** with a **Resume** button. Pressing it sends the same request again.
   - For a request an operator sent to the controller, repeat it with the same method, path, body, and idempotency key. See [Deploy apps from the command line](deploy-apps-from-the-command-line.md).

Don't send a changed request under the old key (the controller treats it as a conflict), and don't clear the operation to free the app. An environment-variable change that ended unknown asks you to enter the value again; that is expected, because values are never stored where the portal can read them back.

### App changes are blocked with 'wait until recovery'

A conflict with a blocking operation ID means an earlier change still owns work for the app. `OPERATION_CONFLICT` covers unfinished foreground work; `POST_ACCEPTANCE_CONFLICT` covers a change that could interfere with an accepted deployment's finishing work. The portal can show `APP_BUSY` until it has observed the controller's finishing state.

Use the `admin`, `admin_all`, and `wait_for` helpers from [Deploy apps from the command line](deploy-apps-from-the-command-line.md#set-up-your-shell) to read the operation through the privileged socket:

```bash
OPERATION_ID='<BLOCKING_OPERATION_UUID>'
admin "http://localhost/v1/admin/operations/$OPERATION_ID" > operation.json
jq '{operationId, kind, status, phase, safeError, cleanupState,
     finishing, finishingRetryAttempts, nextRetryAt}' operation.json
```

If `finishing` is `true`, acceptance already succeeded. The controller retries address handover, exact predecessor cleanup, or registry retention automatically, with five waits totaling 25 minutes plus execution time. `status: running` and a `nextRetryAt` mean another attempt is scheduled. Environment set/delete/import, storage verify/rotate, and restart can proceed. Enable can be a no-op after handover. Disable also waits for confirmed predecessor job absence, so an old route cannot keep serving; creating a worker for a stopped app must wait. Another deploy, resize, rollback, deletion, or address change must wait until finishing completes.

An older checkpoint can have `cleanupState: confirmed` because its builder was removed while predecessor cleanup still remains. Read `status`, `finishing`, and `nextRetryAt` together.

After all five retries fail, `status` becomes `recovery_required` and `nextRetryAt` is `null`. Read the controller service journal using the recovery account to find the bounded `helper failure` line: it names the action, error class, and safe code. Fix that dependency, then replay the original request with its original `Idempotency-Key`. Replaying can also request an earlier attempt while the controller is waiting; it shares admission with the automatic retry. Do not use a new key, edit state, or remove the predecessor by hand.

For command-line requests, use the saved body and key with the same socket helper, HTTP method, and path. Then run `wait_for "$OPERATION_ID"`; success means the operation reads `succeeded` and cleanup is confirmed. A healthy active app alone does not prove finishing completed.

For portal-started operations, the broker's `intents` table in `<adminState>/management-broker/management.sqlite3` records the exact `method`, `path`, `body`, and `controller_key`. Use a read-only query through the recovery account to select the row whose `controller_key` matches the operation ID. Keep the body in a private file, remove only the broker's server-only `_portalAdmin` marker if present, and replay the recorded method/path/body with `Idempotency-Key: <controller_key>` as `management-broker` on the project socket. The `project` helper in the command-line guide uses that account. Never print request bodies or secret values. Environment values are omitted from broker state; a lost environment request needs its original value supplied again.

If `finishing` is `false`, the controller has not proven acceptance and cannot automatically repeat the request. Read the phase and safe error, fix the cause, and replay the exact original request and key. [An app deploy is stuck or failed](#an-app-deploy-is-stuck-or-failed) describes candidate and health failures.

## Owner portal

### People can't sign in to the portal

The sign-in page shows one message when a sign-in doesn't finish:

| Message | Code | What it means and what to do |
| --- | --- | --- |
| `Sign-in was cancelled.` | `COMMONS_CANCELLED` | The person pressed Cancel on Commons. Try again. |
| `That sign-in expired. Try again.` | `SIGN_IN_EXPIRED` | The one-time Commons code was already used, expired, or no longer valid (approval withdrawn, or the person is archived on Commons), or the browser took longer than 10 minutes. Try again from the portal's sign-in page. If it keeps happening for one person, check their Commons account. |
| `Signing in with your class account is unavailable right now. Try again in a minute.` (or with your custom provider label) | `IDENTITY_UNAVAILABLE` | The portal identity service could not reach Commons. See below. |
| `This account is disabled or archived. Contact staff if you think this is a mistake.` | `ACCOUNT_DISABLED` | The portal account is disabled or not yet active. A portal admin can check it under **Accounts**. |
| `Too many attempts. Wait a minute, then try again.` | `RATE_LIMITED` | Sign-in limits for that address or account. Wait and try again. |
| `Username, password or authentication code is incorrect.` | `INVALID_CREDENTIALS` | A local account's password or authenticator code is wrong. After several wrong codes the account must wait before trying again. Use a fresh code each time. |
| `This page expired. Reload it and try again.` | `CSRF_REJECTED` | The sign-in page was open too long. Reload it. |
| `Sign-in is unavailable right now. Try again in a few minutes.` | `AUTH_UNAVAILABLE` | The portal couldn't complete sign-in. Check that its services are running. |

**When Commons sign-in is unavailable.** People who are already signed in stay signed in; only new sign-ins fail.

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

**When a portal admin is locked out.** Don't rely on a Commons session to recover. Issue a new setup link for a new local portal admin from the admin host, then use that account to fix or reset the old one. See [Open the owner portal](open-the-portal.md). If Commons is stopped, new Commons sign-ins fail; existing sessions keep working. A local portal admin can still sign in.

## Images

### An image change is refused

Selecting a role image or pruning old images is refused when the image's provenance is incomplete, it isn't compatible, its OpenStack status or ownership is wrong, or a server's image record doesn't match. Fix the cause, then create a new plan. Don't overwrite a tested image's name or widen the set of images to delete by hand. See [Hosts and images](hosts-and-images.md).

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
