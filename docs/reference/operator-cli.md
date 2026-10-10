# Operator CLI reference

This page lists every command of `openstack-platform`, the operator's command-line tool, with its options and whether it changes anything. It is for operators who already have a deployment; to create one, start with [Deploy the platform](../guides/deploy-the-platform.md).

The command list comes from the argument parser in [`openstack_platform/operator.py`](../../openstack_platform/operator.py).

## What the CLI covers

`openstack-platform` manages infrastructure: greenfield setup, platform status, the operator dashboard, role images, the three persistent hosts, and backup and restore of its own state.

It has no application, deployment, environment, or managed-storage mutation commands. App owners, staff, and portal admins change apps through the [owner portal](../guides/manage-apps-and-people.md). An operator who needs to act on an app directly uses the [controller API](controller-api.md), as shown in [Deploy apps from the command line](../guides/deploy-apps-from-the-command-line.md). `status` only shows aggregate app and storage counts.

## How you run it

Run every command as the unprivileged operator account that owns `/srv/openstack-platform` (the [operator host](../README.md#glossary)). Never run it as root or through `sudo`.

After setup, use the installed launcher. The pages in this documentation set a variable for it:

```bash
export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
$PLATFORM_CLI status
```

The launcher pins the inventory, state directory, and policy to their installed paths, checks that the configuration directory is mode `0700` and the inventory and policy are mode `0600`, and refuses any attempt to point them elsewhere. It also requires the protected provider wrapper `/srv/openstack-platform/bin/platform-openstack`.

`setup` is the exception: you run it from a clean checkout of the repository with `uv run openstack-platform setup ...`, because it builds the role images from that source.

## Global options

These options go before the command. The installed launcher sets all three for you.

| Option | Default | Meaning |
| --- | --- | --- |
| `--platform-config PATH` | `$PLATFORM_CONFIG`, else `/srv/openstack-platform/config/platform.json` | The deployment inventory |
| `--state-directory PATH` | `/srv/openstack-platform/state` | The operator state directory, which holds `platform.sqlite3` |
| `--policy PATH` | `STATE_DIRECTORY/policy.json` | The private operator policy |

## Command summary

| Command | Changes anything? | What it does |
| --- | --- | --- |
| [`setup check`](#setup-check) | No | Read-only preflight of a new deployment |
| [`setup --apply`](#setup---apply) | Yes | Create a complete new deployment |
| [`status`](#status) | No | Platform health in one line |
| [`dashboard`](#dashboard) | No | Serve the read-only operator dashboard |
| [`backup`](#backup) | Writes a backup | Back up the operator's own state |
| [`restore`](#restore) | Yes, offline | Replace the operator's state from a backup |
| [`infra list`](#infra-list) | No | Selected image and live state of each role |
| [`infra image list`](#infra-image-list) | No | Role images in the OpenStack project |
| [`infra image set`](#infra-image-set) | Yes | Select the image for a role |
| [`infra image prune`](#infra-image-prune) | With `--apply` | Plan, then delete, unused images |
| [`infra logs`](#infra-logs) | No | Serial console tail of a persistent host |
| [`infra start`](#infra-start-stop-and-reboot) | Yes | Start a persistent host |
| [`infra stop`](#infra-start-stop-and-reboot) | Yes | Stop a persistent host |
| [`infra reboot`](#infra-start-stop-and-reboot) | Yes | Reboot a persistent host |
| [`infra replace`](#infra-replace) | Yes | Replace a persistent host with a new server |

In the synopses below, `ROLE` is one of `admin`, `ingress`, or `storage` for `infra logs`, `start`, `stop`, `reboot`, and `replace`, and any of the five roles for `infra image set`.

## Setup

### setup check

```text
openstack-platform setup check --env-file PATH [--cloudflare-token-file PATH] [--json]
```

Authenticates to OpenStack with the protected setup file, resolves the project, network, flavors, and volume type, checks quotas, fixed addresses, reserved names, local tools, ingress settings, and release evidence, and prints a plan. It only reads from OpenStack: it creates no workspace, credentials, images, or cloud resources.

| Option | Meaning |
| --- | --- |
| `--env-file PATH` | Required. The protected setup file ([configuration reference](configuration.md)) |
| `--cloudflare-token-file PATH` | A Cloudflare Tunnel token file; requires `PLATFORM_INGRESS_MODE=tunnel` |
| `--json` | Print the plan as JSON instead of text |

The text output starts with `setup-check=ready` or `setup-check=failed` and ends with `no resources or credentials were created`. The command exits with an error when the plan is not ready (name collisions, an occupied fixed address, or insufficient or unknown quota). Running `setup` with neither `check` nor `--apply` also performs this check.

### setup --apply

```text
openstack-platform setup --env-file PATH [--workspace PATH] [--cloudflare-token-file PATH] --apply
```

Creates a complete deployment: inventory and secrets, security groups and fixed ports, the five role images, the three persistent hosts, the operator and helper releases, the controller, and backup schedules. [Internals](internals.md#greenfield-setup-flow) describes each step. It prompts for OpenStack credentials the setup file leaves out, reading secrets without echo.

| Option | Meaning |
| --- | --- |
| `--env-file PATH` | Required. The protected setup file |
| `--workspace PATH` | Where setup keeps its generated inventory, policy, and image evidence; default `/srv/openstack-platform/setup` |
| `--cloudflare-token-file PATH` | Configure Cloudflare Tunnel on the ingress host; without it, public ingress is left pending |
| `--apply` | Required to make changes; cannot be combined with `check` |

On success the output ends with `setup=complete project=<name> project-id=<uuid>`, the inventory path, and `public-ingress=cloudflare-configured` or `public-ingress=pending external provider configuration`. Setup is resumable: after a failure, fix the named problem and rerun the identical command with the same workspace. See [Deploy the platform](../guides/deploy-the-platform.md).

## Status and dashboard

### status

```text
openstack-platform status
```

Prints one table row:

```text
STATE    INFRA  APPS  STORAGE  LIVE  UNAVAILABLE  UNHEALTHY
healthy  5      3     4        3     0            0
```

| Column | Source |
| --- | --- |
| `STATE` | `healthy` or `degraded` |
| `INFRA` | Roles with a selected image in operator state (5 when complete) |
| `APPS`, `STORAGE` | Counts of apps and managed storage resources in the hosted controller |
| `LIVE`, `UNAVAILABLE`, `UNHEALTHY` | Live observations from the hosted controller |

`status` reads `GET /v1/admin/status` on the controller's privileged socket through the `platform-admin` SSH alias. If the controller cannot be reached, the command fails as unavailable (exit code 4) rather than falling back to local records. `STATE` is `degraded` when any role has no selected image, any observation is unavailable or unhealthy, or any operation is unfinished in either the operator or the hosted controller state.

### dashboard

```text
openstack-platform dashboard [--socket PATH] [--interval SECONDS]
```

Serves the read-only [operator dashboard](../guides/run-the-platform.md#open-the-operator-dashboard) until interrupted. It listens only on a private Unix socket, admits only processes running as the operator account, and answers only requests whose `Host` is a loopback name. You reach it by forwarding a local port over SSH.

| Option | Default | Meaning |
| --- | --- | --- |
| `--socket PATH` | `STATE_DIRECTORY/run/dashboard.sock` | The Unix socket to serve (mode `0600`) |
| `--interval SECONDS` | `60` | Seconds between refreshes, from 30 to 900 |

The dashboard has no accounts and no mutation routes. Each refresh reads the controller's database-backed admin routes over one SSH session, the operator state, and public health probes; [Internals](internals.md#operator-host) has the details.

## Backup and restore

### backup

```text
openstack-platform backup
```

Backs up the operator's own SQLite state (`STATE_DIRECTORY/platform.sqlite3`). It takes an online SQLite snapshot, encrypts it locally to the `backupAgeRecipient` in the operator policy, copies the ciphertext to the admin host, and has the helper commit it under `<paths.backups>/controller`. It prints:

```text
backup=platform-20261006T021500Z.sqlite3.age sha256=<64 hex characters>
```

This is one of several backup classes; it does not back up the hosted controller, the portal, or app data. The operator's user timer `openstack-platform-backup.timer` runs this command daily. See [Backups and recovery](../guides/backups-and-recovery.md).

### restore

```text
openstack-platform restore BACKUP [--age-identity IDENTITY] --yes
```

Verifies an operator-state backup and atomically replaces `STATE_DIRECTORY/platform.sqlite3` with it. It works offline: it contacts no provider, SSH host, or network service. It checks deployment identity, schema, SQLite integrity, foreign keys, sidecar files, and unfinished operations before it replaces anything. On any failure the existing database is left unchanged.

| Option | Meaning |
| --- | --- |
| `BACKUP` | The `platform-<timestamp>.sqlite3.age` ciphertext |
| `--age-identity IDENTITY` | The escrowed age identity that decrypts it |
| `--yes` | Required; confirms replacement |

Stop the operator's backup timer and every other operator command first; restore refuses to run while another command holds the state lock. On success it prints `restore=verified schema-version=<n> integrity=ok`.

For restores and drills, the installed launcher `/srv/openstack-platform/bin/openstack-platform-restore` does the same with fixed paths and adds a drill mode; see [Other installed commands](#other-installed-commands).

## Infrastructure

### infra list

```text
openstack-platform infra list
```

Prints one row per role with columns `ROLE`, `IMAGE`, `COMMIT`, and `LIVE`. `IMAGE` and `COMMIT` come from the image selection in operator state; `LIVE` is a bounded provider observation (`unknown` when it could not be made). It does not need the hosted controller, so it works during a controller outage.

### infra image list

```text
openstack-platform infra image list
```

Lists the project's private images with columns `ROLE`, `NAME`, `UUID`, `STATUS`, and `COMMIT`. `ROLE` and `COMMIT` come from the platform metadata on each image, so use this to find the UUID of a newly published role image. Read-only.

### infra image set

```text
openstack-platform infra image set ROLE IMAGE
```

Selects the image for one role in operator state. `ROLE` is one of `admin`, `ingress`, `storage`, `worker`, or `builder`. `IMAGE` is the image's UUID, or its name when exactly one project image has that name. The command checks that the image's metadata matches this deployment and role, then records its exact UUID, source commit, and compatibility hash, and prints `selected role=<role> image=<uuid>`.

This selection decides which image `infra replace` uses for a persistent host. It does not change the hosted controller's own selection, which decides the image of new workers and builders; that is a separate controller operation. [Hosts and images](../guides/hosts-and-images.md#update-hosted-role-image-selections) covers both.

### infra image prune

```text
openstack-platform infra image prune
openstack-platform infra image prune --apply [--yes]
```

Deletes old role images in two steps:

1. Without `--apply`, the command plans. It prints the UUIDs it would delete and a line like `plan=<hash> expires=<time> review=<count>`. Nothing is deleted. A plan expires after 15 minutes.
2. With `--apply`, it applies the newest plan. It asks for confirmation (or takes `--yes`), observes every image again under the infrastructure lock, and deletes exactly the planned UUIDs. It prints `deleted=<count> plan=<hash>`.

The plan only deletes images whose platform metadata fully matches this deployment. It always protects the images selected in operator state, images used by any server in the project, images referenced by an unfinished operation, and the two newest images of each role. `review` counts images with incomplete platform metadata: the plan never deletes them, so inspect them yourself.

### infra logs

```text
openstack-platform infra logs ROLE [--lines COUNT]
```

Prints the tail of the host's Nova serial console. `--lines` is 1 to 2000, default 200. Read-only.

### infra start, stop, and reboot

```text
openstack-platform infra start ROLE
openstack-platform infra stop ROLE [--yes]
openstack-platform infra reboot ROLE [--yes]
```

Change the power state of one persistent host and wait for the result: `stop` waits for `SHUTOFF`; `start` and `reboot` wait for `ACTIVE`, a fresh readiness marker on the console, and the role's health check. `stop` and `reboot` interrupt the role, so they ask for confirmation (or take `--yes`). Each prints `role=<role> server=<uuid> status=<status>`. If the command is interrupted, run the same command again: it resumes the recorded operation.

### infra replace

```text
openstack-platform infra replace ROLE [--yes] [--cloudflare-tunnel-token-file PATH]
```

Replaces a persistent host with a new server built from the role's selected image (set it first with `infra image set`) and the inventory's `flavors.<role>`. The target flavor is resolved and checked before the old host is stopped; edit it in the operator inventory to resize the host on its next replacement. The old server is stopped but kept, the fixed port and data volumes move to the new server, and the old server is deleted only after the new one passes its identity and readiness checks. If the new server fails, the old one is restored with its original flavor. A quota refusal also restores the old host without deleting resources when the provider confirms no candidate exists. This interrupts the role; it is not zero-downtime.

| Option | Meaning |
| --- | --- |
| `--yes` | Skip the confirmation prompt |
| `--cloudflare-tunnel-token-file PATH` | Ingress only: the tunnel token for a fresh ingress replacement. Ignored, with a notice, when resuming a recorded replacement. |

On success it prints a JSON observation with the operation ID, the old and new server IDs, and the selected image. When flavors differ, the confirmation prompt names the change and the observation includes `"flavor": {"from": "<old flavor>", "to": "<new flavor>"}`. Recovery uses the recorded target flavor ID. If the command stops partway, run it again: an attempt that had not been accepted rolls back to the old host (start a fresh replacement afterwards), and an accepted one finishes its cleanup. See [Replace a persistent host](../guides/hosts-and-images.md#replace-a-persistent-host).

## Confirmation and safety

`stop`, `reboot`, `replace`, and `infra image prune --apply` print what they will do and ask you to type `yes`. When standard input is not a terminal, they require `--yes` instead. `restore` always requires `--yes`.

Commands that change infrastructure record an operation in the operator state and hold the `infrastructure` lock. Only one infrastructure operation can be unfinished at a time. If one is unfinished, other mutating commands stop with a conflict (exit code 3) until you finish it by rerunning the same command.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | The operation failed safely; read the error |
| `2` | Usage or validation error |
| `3` | Conflict: a lock is busy, another operation is unfinished, drift was detected, or recovery is required |
| `4` | A dependency (the admin host, the controller, or the helper) is unavailable |
| `130` | Interrupted |

An unexpected failure prints a correlation ID and writes a private diagnostic under `STATE_DIRECTORY/diagnostics`. The installed launcher exits with `78` when it refuses a path override or finds unsafe configuration permissions, and `69` when no accepted release is installed.

## Other installed commands

These launchers are installed alongside `openstack-platform`. The guides linked in the table describe when to use them.

| Command | Where it runs | Purpose |
| --- | --- | --- |
| `/srv/openstack-platform/bin/openstack-platform-restore` | Operator host, as the operator | Offline restore of operator state into the installed state directory. `--replacement-state-directory DIR`, given first, restores into an absent `platform.sqlite3` inside a private (`0700`) drill directory instead. Then `BACKUP [--age-identity IDENTITY] --yes`. See [Backups and recovery](../guides/backups-and-recovery.md). |
| `/srv/openstack-platform/bin/openstack-platform-install-config` | Operator host, as the operator | Install a new inventory and policy atomically: `--platform PATH --policy PATH`. See [Releases and upgrades](../guides/releases-and-upgrades.md). |
| `/srv/openstack-platform/bin/platform-openstack` | Operator host | Protected wrapper that runs the OpenStack client with the deployment's credentials |
| `openstack-platform-hosted-controller-restore` | Admin host, root on the recovery console | Restore the hosted controller database (and deploy keys). See [Backups and recovery](../guides/backups-and-recovery.md). |
| `openstack-platform-management-broker-restore` | Admin host, root on the recovery console | Restore the owner portal broker database |
| `openstack-platform-management-reactivate` | Admin host, as the operator | Request activation of a retained portal release pair. See [Releases and upgrades](../guides/releases-and-upgrades.md). |

## Related

- [Run the platform](../guides/run-the-platform.md)
- [Hosts and images](../guides/hosts-and-images.md)
- [Backups and recovery](../guides/backups-and-recovery.md)
- [Controller API](controller-api.md)
- [Internals](internals.md)


## PostgreSQL rollout repair

`openstack-platform-storage-repair` is a separate local command installed on the
admin VM. Run it as agentops after the storage host replacement, admin image and
matching helper release, and portal pair are installed. It calls the local privileged
controller socket, prints a request UUID, and waits for the controller operation.

```sh
openstack-platform-storage-repair
# Reconcile an interrupted run using its printed UUID:
openstack-platform-storage-repair --request-id UUID
```

The command reapplies the current resource-row connection limits to each database
and current login, together with PostgreSQL statement, idle-transaction, lock and
temporary-file settings. It does not rotate credentials. The assignments are
idempotent; retrying an interrupted request resumes from completed resources.
`--timeout` defaults to 300 seconds and accepts 1–3600 seconds. The raw packaged
entrypoint requires `--socket PATH`; the Nix wrapper supplies the deployment's
privileged socket.
