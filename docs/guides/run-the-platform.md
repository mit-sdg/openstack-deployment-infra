# Run the platform day to day

Use this guide after setup to check health, open the operator dashboard, and find logs. It also covers routine maintenance and the checks before retiring a deployment.

Before you start:

- Complete initial setup. See [Deploy the platform](deploy-the-platform.md).
- Sign in to the operator host as the unprivileged account that owns `/srv/openstack-platform`. Never run operator commands with `sudo` on the operator host.

## Where commands run

Most operations run on the operator host. Routine status checks query the admin host through SSH, and a few maintenance tasks use root on the admin host.

| Where | Signed in as | Used for |
| --- | --- | --- |
| Operator host | The operator account that owns `/srv/openstack-platform` | The `openstack-platform` CLI, the operator dashboard, operator-state backup and restore, decrypting backups |
| Admin host, through the `platform-admin` SSH alias | `agentops`, the platform's operator account on the admin host | Read-only checks (unit state, status files, backup listings), managed-data backup and restore checks, off-site export configuration |
| Admin host, through the recovery console | `ubuntu`, which has `sudo` | Root-only tasks: starting or stopping system units on demand, reading system journals, and database restores |

The `platform-admin` alias is configured at `/srv/openstack-platform/.secrets/ssh/config`. It pins the admin host address, the `agentops` account, the host key, and the identity file, so you never type an address or accept a new host key by hand.

The recovery console signs in as `ubuntu`, which can use `sudo`, with the key at `/srv/openstack-platform/.secrets/setup/admin_nova_rsa`. Follow your organization's approval rules for root access.

## Set up your shell

The guides refer to a handful of shell variables. Set them once per shell session (or put these lines in a private file in the operator account and source it):

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

The values come from the installed inventory (the deployment's private description of itself). `PLATFORM_ROOT`, `PLATFORM_BACKUPS`, and `PLATFORM_ADMIN_STATE` are paths on the admin host, used in commands sent over SSH.

| Variable | What it holds |
| --- | --- |
| `PLATFORM_CLI` | The operator CLI launcher. It always runs the currently installed operator release. |
| `PLATFORM_CONFIG` | The installed inventory. The CLI also reads this variable as its default `--platform-config`. |
| `SSH_CONFIG` | The generated SSH config that defines `platform-admin`. |
| `PLATFORM_NAMESPACE` | The deployment's `namespace`. Admin system units are named after it, for example `<namespace>-controller.service`. |
| `PLATFORM_ROOT` | `paths.root`: the platform directory on the admin host. Packaged scripts are under `$PLATFORM_ROOT/infra`, and operator files under `$PLATFORM_ROOT/persistent`. |
| `PLATFORM_BACKUPS` | `paths.backups`: where the admin host mounts the backup volume. |
| `PLATFORM_ADMIN_STATE` | `paths.adminState`: where the admin host mounts its state volume (controller and portal databases, release directories). |
| `PLATFORM_DOMAIN` | `domain`: the public hostname of the platform and the owner portal. |

For steps that need root on the admin host, define this function in the same shell. It opens the recovery console for one command:

```bash
recovery() {
  ssh -F "$SSH_CONFIG" -o IdentitiesOnly=yes \
    -i /srv/openstack-platform/.secrets/setup/admin_nova_rsa \
    -l ubuntu platform-admin -- "$@"
}
```

SSH joins the arguments into one remote command line, so pass only arguments without spaces. The variables expand on the operator host before the command is sent.

> [!WARNING]
> Keep private material out of your terminal history, tickets, and chat. Don't print the inventory, credential files, age identities, raw provider output, or controller operation references. Safe evidence is bounded status output, identifiers the CLI already shows, checksums, manifests, readiness results, and operation or correlation IDs.

## Check health

Run these checks whenever you sit down to work on the platform, and before and after any change.

1. Read the overall status:

   ```bash
   $PLATFORM_CLI status
   ```

   A healthy deployment looks like this:

   ```text
   STATE    INFRA  APPS  STORAGE  LIVE  UNAVAILABLE  UNHEALTHY
   healthy  5      4     3        10    0            0
   ```

   | Column | Meaning | Healthy value |
   | --- | --- | --- |
   | `STATE` | `healthy` or `degraded` | `healthy` |
   | `INFRA` | Roles with a selected image in the operator state | `5` |
   | `APPS` | Apps recorded by the controller, including stopped ones | Your app count |
   | `STORAGE` | Managed storage resources (databases and buckets) recorded by the controller | Your resource count |
   | `LIVE` | Live observations that answered: the three persistent hosts, plus one per app and one per storage resource | `3` + `APPS` + `STORAGE` |
   | `UNAVAILABLE` | Observations that could not be made | `0` |
   | `UNHEALTHY` | Observations that answered with a problem | `0` |

   `status` asks the controller on the admin host for these numbers. If the controller can't be reached, the command fails with `unavailable: hosted controller status is unavailable` instead of guessing. `STATE` is `degraded` if any role lacks a selected image, any observation is unavailable or unhealthy, or any operation is unfinished.

2. List the roles and their images:

   ```bash
   $PLATFORM_CLI infra list
   ```

   ```text
   ROLE     IMAGE         COMMIT    LIVE
   admin    <image-name>  <commit>  active
   builder  <image-name>  <commit>  unknown
   ingress  <image-name>  <commit>  active
   storage  <image-name>  <commit>  active
   worker   <image-name>  <commit>  unknown
   ```

   The admin, ingress, and storage rows should read `active`. `worker` and `builder` normally read `unknown` here: workers are observed per app instead, and a builder exists only during a build. `infra list` needs only the operator state and OpenStack, so it still works when the controller is down.

3. Check the controller's services on the admin host:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
     "$PLATFORM_NAMESPACE-controller.service" \
     "$PLATFORM_NAMESPACE-controller-readiness.service"
   ```

   You should see `active` twice. The readiness unit confirms the controller answers on its local socket.

4. Check the public entry point:

   ```bash
   curl --fail --show-error --silent "https://$PLATFORM_DOMAIN/healthz"
   ```

   The response must be exactly `OK`. That one request proves DNS, the browser-trusted certificate, your HTTPS provider, host routing, and Traefik on the ingress host.

5. Read the platform health snapshot. A timer on the admin host runs a deeper check every five minutes and writes the result to a file the operator can read:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- \
     cat "$PLATFORM_ROOT/persistent/status/$PLATFORM_NAMESPACE.json" |
     /srv/openstack-platform/runtime/python3.14 -m json.tool
   ```

   Look for `"healthy": true`, `"error": null`, and a `checked_at` time within the last few minutes. The checks run in this order and stop at the first failure:

   | Check | What it verifies |
   | --- | --- |
   | `openstack` | The three persistent servers and every worker are `ACTIVE` in OpenStack |
   | `public_ingress` | `https://<domain>/healthz` and `https://wildcard-health.<domain>/healthz` both answer `OK` |
   | `managed_services` | PostgreSQL, MongoDB, Garage, and the image registry respond |
   | `nomad` | Nomad reports at least one ready client per worker, and its Raft peer query succeeds |
   | `backup` | The newest managed-data backup is complete and less than 36 hours old |
   | `offsite_recovery` | The newest off-site export is verified and recent enough |

   If a check fails, `error` contains the exception type and detail. The first missing entry in `checks` identifies the failed check; later checks didn't run.

> [!NOTE]
> Until you configure an off-site export, the `offsite_recovery` check always fails, so the snapshot reports `"healthy": false` even when everything else passes. Set it up with [Export recovery evidence off site](backups-and-recovery.md#export-recovery-evidence-off-site).

If anything isn't healthy, go to [Troubleshooting](troubleshooting.md). An observation that is unavailable doesn't erase what the platform has accepted. Find out which dependency is missing before you change anything, and never edit a database or cloud resource to make status look healthy.

The CLI's exit codes help in scripts: `0` success, `1` a safe failure, `2` a usage or validation error, `3` a conflict (a busy lock, an unfinished operation, or a required recovery), `4` an unavailable dependency, and `130` interrupted. The [operator CLI reference](../reference/operator-cli.md#exit-codes) has details.

## Open the operator dashboard

The operator dashboard is a browser view of the whole platform, served by `openstack-platform dashboard`. It is read-only: it shows status but can't change anything.

It shows:

- an overview with one headline (`All systems operational`, `No issues detected`, `Needs attention`, `Disruption detected`, or `Status unavailable`), the time of the last check, and summary counts;
- **Needs attention**, a list of the problems it found;
- **Infrastructure roles**, the five roles with a status for each;
- **Applications**, every app with search, filters, and sorting. Open a row for its route evidence, deployment history, sizing, storage, and identifiers;
- **Operations**, running and recent operations, including any that need recovery;
- **Platform checks**, the six checks from the platform health snapshot;
- a footer with the age of each data source: `Controller`, `OpenStack`, `Health timer`, and `Public routes`.

### How it's protected

The dashboard listens only on a private Unix socket on the operator host, by default `/srv/openstack-platform/state/run/dashboard.sock`. The socket admits only processes running as the operator account, and the server rejects any request whose `Host` isn't `localhost`, `127.0.0.1`, or `[::1]`. You reach it by forwarding a port over SSH as the operator.

> [!WARNING]
> The dashboard has no sign-in of its own, and it shows administrator-level information about every app. Never publish it through a tunnel, reverse proxy, or shared port.

### Start it and connect

1. On the operator host, check that lingering is on for the operator account, so user services keep running after you log out:

   ```bash
   loginctl show-user "$USER" --property=Linger
   ```

   It must print `Linger=yes`. If it doesn't, ask the host's administrator to enable lingering for this account (`loginctl enable-linger <account>`). Without it, the dashboard and the operator's daily backup timer stop when you log out.

2. Start the dashboard as a transient user service:

   ```bash
   systemd-run --user --unit=openstack-platform-dashboard --collect \
     --property=Restart=on-failure "$PLATFORM_CLI" dashboard
   systemctl --user is-active openstack-platform-dashboard
   ```

   The second command prints `active`. The service restarts after a crash but not after a reboot of the operator host; run the `systemd-run` command again after a reboot. `--interval SECONDS` (30 to 900, default 60) changes how often it collects new data.

3. On your workstation, forward a local port to the socket. Replace `<operator-host>` with the SSH destination that signs you in as the operator account:

   ```bash
   ssh -N -L 127.0.0.1:8470:/srv/openstack-platform/state/run/dashboard.sock <operator-host>
   ```

   Then open `http://localhost:8470`. Any free local port works.

4. Check the view. The first collection can take up to a minute. After that the overview shows `Checked` with a time, the footer shows recent times for all four sources, and the role and app counts match `$PLATFORM_CLI status`.

To stop the dashboard:

```bash
systemctl --user stop openstack-platform-dashboard
```

The dashboard loads its web assets when it starts. After you install a new operator release, stop it and start it again with the `systemd-run` command so it serves the new release's page.

### What the statuses mean

App statuses:

| Status | Meaning |
| --- | --- |
| Serving | The app's health path answered 2xx with the marker of its accepted deployment |
| Unverified | The health path answered 2xx but without the expected deployment marker |
| Failing | The health path answered another status (including a redirect) or didn't answer |
| Deploying, Starting, Stopping, Updating | An operation is running for the app; route failures are expected during the switch |
| Needs recovery | An operation, deployment, or storage resource is `recovery_required`; see [Troubleshooting](troubleshooting.md#an-app-deploy-is-stuck-or-failed) |
| Not deployed | The app is enabled but has no accepted deployment |
| Unknown | The route couldn't be checked (no health path recorded, or the accepted deployment is older than the 200 newest) |
| Stopped | The app is disabled, so its route isn't checked |

Role statuses are `Operational`, `Changing`, `Degraded`, `Failing`, or `Unverified`; builders show `Idle` or `Building`. Missing evidence makes a role `Unverified`, for example when an earlier health check failed. A label such as `Last seen healthy` marks an older observation after a current read failed.

<details>
<summary>How the dashboard collects data</summary>

One background loop collects data at the configured interval. Each collection:

- opens one SSH session to the admin host that sends only `GET` requests to the controller's database-backed admin routes (capabilities, images, applications, the two newest pages of deployments, storage, and the three newest pages of operations), reads the platform health snapshot, and checks the two controller units;
- reads the operator state and asks OpenStack for the persistent servers, with the provider call bounded at 60 seconds;
- probes the public ingress and each enabled app's health path over HTTPS, without credentials or redirects.

It never calls `/v1/admin/status` or `/v1/admin/hosts`, because those hold the controller's shared lock while they probe the cloud. When one source fails, the dashboard keeps that source's last good records and labels their age. The health snapshot counts as stale after 15 minutes.

The browser reads the cached result every 10 seconds (every 2 seconds while a collection is in progress, every 60 seconds while the tab is hidden). The refresh button only wakes the next collection early, at most once every 10 seconds. The theme button cycles system, light, and dark.

</details>

If the page shows warnings or won't load, see [The dashboard won't open or shows warnings](troubleshooting.md#the-dashboard-wont-open-or-shows-warnings).

## Find logs

| What | Where | How to read it |
| --- | --- | --- |
| Operator CLI errors | The command's own output | Errors start with `error:`, `conflict:`, or `unavailable:`. An unexpected failure prints a correlation ID and writes a private trace to `/srv/openstack-platform/state/diagnostics/<correlation-id>.trace`. |
| Operator-state backup timer | User service `openstack-platform-backup.service` on the operator host | `systemctl --user status openstack-platform-backup.service`. If your operator host keeps per-user journals, also `journalctl --user -u openstack-platform-backup.service`. |
| Operator dashboard | User service `openstack-platform-dashboard` | `systemctl --user status openstack-platform-dashboard` |
| Boot and first-start output of a persistent host | The host's serial console in OpenStack | `$PLATFORM_CLI infra logs admin --lines 200` (or `ingress`, `storage`; 1 to 2000 lines, default 200) |
| State of admin system units | systemd on the admin host | `ssh -F "$SSH_CONFIG" platform-admin -- systemctl status <unit>` shows whether a unit ran, failed, or was skipped |
| Journal of admin system units | The admin host's system journal | Root only: `recovery sudo journalctl -u <unit> -n 100 --no-pager` |
| Platform health | `$PLATFORM_ROOT/persistent/status/<namespace>.json` on admin | See [Check health](#check-health) |
| Off-site export receipt | `$PLATFORM_ROOT/persistent/status/offsite-export.json` on admin | See [Backups and recovery](backups-and-recovery.md#export-recovery-evidence-off-site) |
| App build and runtime logs | The controller, through the portal or the API | App owners, staff, and portal admins see them in the portal. Operators use the routes in [Deploy apps from the command line](deploy-apps-from-the-command-line.md). |

The `agentops` account can see unit state but can't read the system journal, which is why journal reads go through the recovery console. App logs can contain sensitive values, so keep copies private.

The admin units you'll look at most:

| Unit | What it is |
| --- | --- |
| `<namespace>-controller.service` | The controller |
| `<namespace>-controller-readiness.service` | Confirms the controller answers |
| `<namespace>-platform-health.timer` | Runs the platform health check every five minutes |
| `<namespace>-hosted-controller-backup.timer` | Daily controller backup |
| `<namespace>-management-broker-backup.timer` | Daily owner portal backup |
| `<namespace>-platform-backup.timer` | Daily managed-data backup |
| `<namespace>-offsite-export.timer` | Daily off-site export |
| `<namespace>-management-web.service`, `<namespace>-management-broker.service`, `<namespace>-management-identity.service` | The owner portal's three services |

## Routine checklist

### Every week

- [ ] `$PLATFORM_CLI status` reports `healthy`, and `infra list` shows the three persistent hosts `active`.
- [ ] The dashboard shows no unexpected `Needs attention` items.
- [ ] The platform health snapshot is recent and healthy.
- [ ] Every backup timer ran in the last day and the newest backup sets are present. See [Verify backups and schedules](backups-and-recovery.md#verify-backups-and-schedules).
- [ ] The managed-data restore check passes on the newest set.
- [ ] The off-site export is current (the snapshot's `offsite_recovery` check passes).

### Every term

At the start and end of each term, and before any large change:

- [ ] Restore the newest operator-state backup into a private drill directory. This proves the controller escrow identity still decrypts it. See [Restore external operator state](backups-and-recovery.md#restore-external-operator-state).
- [ ] Run a complete-loss drill from an off-site bundle, at least in verify-only mode. See [Drill a complete-loss recovery](backups-and-recovery.md#drill-a-complete-loss-recovery).
- [ ] Confirm that you hold escrow copies of both backup identities outside the platform, and that someone else on the course staff knows where they are.
- [ ] Review new releases and role images, and plan upgrades. See [Releases and upgrades](releases-and-upgrades.md).
- [ ] Prune role images you no longer need. See [Hosts and images](hosts-and-images.md).

### After every change

- [ ] Re-run the health checks above.
- [ ] Take fresh backups of the sets the change touched, and confirm they committed.

## Teardown boundary

Tearing down means permanently deleting the deployment's cloud resources: servers, volumes, ports, images, and security groups. The platform's tooling deliberately does not do this. The operator CLI has no teardown command for the deployment or for apps, so no single command can destroy everything by mistake.

If you need to retire a deployment, treat it as a separate, explicitly authorized project. Before any cloud resource is deleted:

1. Confirm that no apps or managed storage remain. `$PLATFORM_CLI status` must show `0` under both `APPS` and `STORAGE`; anything else is a stop condition. Apps are deleted through the controller's privileged deletion route (see the [Controller API](../reference/controller-api.md)), which also removes their storage.
2. Keep and verify every backup set, both backup identities, the off-site bundle names, and the evidence from a complete-loss drill.
3. Stop the scheduled jobs: the operator's `openstack-platform-backup.timer`, and on the admin host `<namespace>-hosted-controller-backup.timer`, `<namespace>-management-broker-backup.timer`, `<namespace>-platform-backup.timer`, and `<namespace>-offsite-export.timer`.
4. Write down any resources in the project that don't belong to the deployment, so they stay out of scope.
5. Have a person review a deletion plan limited to the exact project, the deployment's name prefix, and the ownership evidence for each resource.

Don't use an older release's binary, name matching, or a project-wide delete command to do this.

## Related

- [Backups and recovery](backups-and-recovery.md)
- [Troubleshooting](troubleshooting.md)
- [Deploy apps from the command line](deploy-apps-from-the-command-line.md)
- [Manage apps and people](manage-apps-and-people.md)
- [Hosts and images](hosts-and-images.md)
- [Releases and upgrades](releases-and-upgrades.md)
- [Operator CLI reference](../reference/operator-cli.md)
