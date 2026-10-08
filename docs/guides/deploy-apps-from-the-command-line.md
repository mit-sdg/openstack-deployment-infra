# Deploy and manage apps from the command line

Use this guide to deploy, roll back, resize, or recover apps through the controller. You need a deployed platform and access to the operator host.

### Common tasks

| Task | Start here |
| --- | --- |
| Deploy | [Shell setup](#set-up-your-shell) → [Select an app](#declare-an-app-or-select-an-existing-one) → [Configuration](#prepare-the-deployment-configuration) → [Deployment path](#choose-a-deployment-path) → [Verify](#poll-and-verify-acceptance) |
| Recover | [Logs, retries, and rollback](#diagnose-resume-or-roll-back) |
| Stop or start | [Enable, disable, or restart](#enable-disable-or-restart-an-app) |
| Change VM size | [Size an app](#size-an-app) |
| Keep an address | [Fixed IPv4](#keep-a-workers-primary-fixed-ipv4) or [Outbound IPv4](#reserve-a-stable-outbound-ipv4) |

## When to use the command line

Use the command line for staff-maintained apps, portal outages, worker reuse, rollback without rebuilding, standalone resizing, and IP reservations. Apps need a GitHub repository, Node.js or Bun, a lockfile, and package scripts; Dockerfiles and arbitrary VM workloads aren't supported.

A command-line app appears in the portal only after a portal admin adopts it. Adoption requires an accepted deployment; see [Manage apps and people](manage-apps-and-people.md).

### How the command line reaches the controller

The controller has no network API. It checks each caller's Linux identity (`SO_PEERCRED`) on two Unix sockets in `/run/<namespace>-controller/`:

| Socket | Caller and routes | Operator access |
| --- | --- | --- |
| `privileged.sock` | Operator account; `/v1/admin/` reads, deployments, sizing, rollback, IP reservations, polling, plus app deletion | `curl` through the pinned `platform-admin` SSH alias |
| `project.sock` | `management-broker`; app declaration, storage, environment, deploy keys, logs, enable/disable/restart | Recovery SSH identity, then `sudo -u management-broker` |

See the [controller API reference](../reference/controller-api.md) for route contracts.

### What a deployment does

The controller validates and records the request, builds the exact commit with rootless BuildKit on a single-use builder, pushes the OCI image to the registry, and deletes the builder. It then creates or validates a worker, starts the Nomad job, and checks scheduler health, the app's health path, and public routing before accepting it and cleaning up the predecessor.

A failed candidate is removed. Rolling deployments keep the accepted version serving; maintenance deployments have a cutover outage.

### How requests behave

Save each keyed request body with its lowercase UUID `Idempotency-Key`. Replaying both returns or resumes the operation; changing the body gives `409 IDEMPOTENCY_CONFLICT`. Unfinished foreground app operations block new work with `409 OPERATION_CONFLICT`, naming the blocking `operationId`. Accepted finishing work retries automatically and permits compatible changes; conflicting requests return `409 POST_ACCEPTANCE_CONFLICT`. See [App changes are blocked with wait until recovery](troubleshooting.md#app-changes-are-blocked-with-wait-until-recovery).

App declaration returns `201`. Infrastructure changes return `202` with `operationId` and `statusUrl`; admission isn't success. A deployment's ID, operation ID, and key are the same UUID. Poll for `running`, `succeeded`, `failed`, or `recovery_required`, along with `phase`, `safeError`, `errorCode`, and `cleanupState`. `recovery_required` means the outcome is uncertain: fix the dependency and replay the request.

Errors contain `code`, `summary`, `correlationId`, and `retryable` under `error`, plus a blocking `operationId` when applicable. They omit secrets and provider output.

## Before you start

- Check [platform health](run-the-platform.md) and [current backups and restore checks](backups-and-recovery.md), including managed data.
- Confirm the app listens on `0.0.0.0` at its configured port and returns 2xx at its health path without redirects. The platform sets `PORT` and `HOST=0.0.0.0`.
- Choose a full 40-character commit pushed to GitHub that passes CI. Review its migrations: rollback never restores data, and deployments take no automatic snapshot. Arrange approved maintenance if you need one.
- Get recovery-account approval for product routes; that account has full sudo on admin.
- Have Bash, `curl`, `jq`, Python 3, and `/srv/openstack-platform/.secrets/ssh/` on the operator host. Install `jq` through the host's package manager: [`bootstrap_operator_runtime.sh`](../../deploy/releases/bootstrap_operator_runtime.sh) doesn't provide it. [`nix/modules/common.nix`](../../nix/modules/common.nix) includes it in guest images, not on the operator host.
- Keep request files, credentials, and logs private. Don't use `set -x` or pass secrets as command arguments.

### Set up your shell

Run once in interactive Bash as the `/srv/openstack-platform` owner to define helpers and create a private request directory.

```bash
umask 077
PLATFORM_ROOT=/srv/openstack-platform
SSH_CONFIG="$PLATFORM_ROOT/.secrets/ssh/config"
RECOVERY_KEY="$PLATFORM_ROOT/.secrets/setup/admin_nova_rsa"
NAMESPACE=$(jq -er .namespace "$PLATFORM_ROOT/config/platform.json")
ADMIN_SOCKET="/run/$NAMESPACE-controller/privileged.sock"
PROJECT_SOCKET="/run/$NAMESPACE-controller/project.sock"

# curl on the privileged socket, as the operator account on the admin host.
admin() {
  local command
  printf -v command '%q ' curl --fail-with-body --silent --show-error \
    --unix-socket "$ADMIN_SOCKET" "$@"
  ssh -F "$SSH_CONFIG" platform-admin -- "$command"
}

# curl on the project socket, as management-broker (uses the recovery account).
project() {
  local command
  printf -v command '%q ' sudo -u management-broker -- \
    curl --fail-with-body --silent --show-error --unix-socket "$PROJECT_SOCKET" "$@"
  ssh -F "$SSH_CONFIG" -o IdentitiesOnly=yes -i "$RECOVERY_KEY" \
    -l ubuntu platform-admin -- "$command"
}

# A fresh idempotency key.
newkey() { python3 -c 'import uuid; print(uuid.uuid4())'; }

# Every item of a paginated admin list: applications, deployments, storage, operations.
admin_all() {
  local cursor='' page
  while :; do
    page=$(admin "http://localhost/v1/admin/$1?limit=100${cursor:+&cursor=$cursor}") || return 1
    jq -c '.items[]' <<<"$page"
    cursor=$(jq -r '.nextCursor // empty' <<<"$page")
    test -n "$cursor" || return 0
  done
}

# The admin record of $APP_ID, and the record of one deployment.
app_record() { admin_all applications | jq -e --arg id "$APP_ID" 'select(.applicationId == $id)'; }
deployment_record() { admin_all deployments | jq -e --arg id "$1" 'select(.deploymentId == $id)'; }

# Poll an operation until it stops running. Succeeds only when it succeeded.
wait_for() {
  local state
  while :; do
    admin "http://localhost/v1/admin/operations/$1" > "operation-$1.json" || return 1
    jq -c '{status, phase, safeError, errorCode, cleanupState, finishing, finishingRetryAttempts, nextRetryAt}' "operation-$1.json"
    state=$(jq -r .status "operation-$1.json")
    case "$state" in
      running) sleep 10 ;;
      succeeded) return 0 ;;
      *) return 1 ;;
    esac
  done
}

mkdir -p -m 700 "$HOME/deployment-requests"
RUN=$(mktemp -d "$HOME/deployment-requests/request.XXXXXXXX")
cd "$RUN" && echo "$RUN"
```

Save the printed path. To resume in another shell, rerun the block without its last three lines and `cd` to that directory. `wait_for` can poll operations from either socket through `/v1/admin/operations/{id}`.

Check controller capabilities:

```bash
admin http://localhost/v1/admin/capabilities > capabilities.json
jq -e '.apiVersion == 1
  and any(.features[]; . == "maintenance-after-build-v1")
  and any(.features[]; . == "reuse-worker-v1")' capabilities.json
```

Both features must be present and the check must print `true`. Otherwise [upgrade the admin image](releases-and-upgrades.md): a checkout or helper update doesn't replace its controller. Don't disable an app to work around missing capabilities.

## Declare an app or select an existing one

Declare each app once; deployments use its UUID (`APP_ID`). Its slug forms `https://<slug>.<domain>`.

### Select an existing app

```bash
SLUG='<APP_SLUG>'
admin_all applications |
  jq -e --arg slug "$SLUG" 'select(.slug == $slug and .deletedAt == null)' > app.json
APP_ID=$(jq -er .applicationId app.json)
```

`app.json` includes `enabled`, `activeDeploymentId` (accepted, not necessarily newest), `sizing`, and `url`.

### Declare a new app

Slugs contain 3–40 lowercase letters, digits, or single interior hyphens, starting with a letter. `admin`, `api`, `auth`, `status`, `www`, and deleted slugs are reserved.

```bash
SLUG='<NEW_APP_SLUG>'
jq -n --arg slug "$SLUG" '{slug: $slug}' > declare.json
newkey > declare.key
project -H 'Content-Type: application/json' -H "Idempotency-Key: $(<declare.key)" \
  --data-binary @- http://localhost/v1/applications < declare.json > declared.json
APP_ID=$(jq -er .applicationId declared.json)
echo "$APP_ID"
```

The response is `201`; save the printed ID. The app starts disabled without a worker.

### Record a baseline

Save state for comparison and rollback:

```bash
BASE="http://localhost/v1/admin/applications/$APP_ID"
PROJECT_BASE="http://localhost/v1/applications/$APP_ID"
app_record > before-app.json
admin_all storage | jq -c --arg id "$APP_ID" 'select(.applicationId == $id)' > before-storage.jsonl
PREVIOUS=$(jq -r '.activeDeploymentId // empty' before-app.json)
if test -n "$PREVIOUS"; then
  deployment_record "$PREVIOUS" > previous-deployment.json
fi
jq '{slug, enabled, activeDeploymentId, sizing, url}' before-app.json
```

New apps have an empty `PREVIOUS` and no `previous-deployment.json`.

## Prepare the deployment configuration

| Request field | Meaning |
| --- | --- |
| `repository` | Credential-free `https://github.com/<OWNER>/<REPOSITORY>`; `.git` is normalized away. |
| `commit` | Full 40-character lowercase SHA to build. |
| `requestedRef` | Branch label, such as `main`. |
| `configurationRevision` | Non-negative integer; keep for unchanged settings, increment for changes. Start at `1`. |
| `configuration` | Build and runtime settings below. |
| `plan` | Reviewed sizing plan; required for replacement, forbidden for reuse. |
| `maintenance` | Boolean, default `false`; `true` stops the predecessor after building. |
| `reuseWorker` | Boolean; `true` requires `maintenance: true`, no `plan`, and the privileged route. |

Set source variables:

```bash
REPOSITORY='https://github.com/<OWNER>/<REPOSITORY>'
REQUESTED_REF=main
COMMIT='<FULL_40_CHARACTER_COMMIT>'
[[ $COMMIT =~ ^[0-9a-f]{40}$ ]] && echo "commit looks valid"
```

### Reuse the accepted configuration

Copy unchanged settings and revision. This also overwrites the repository and branch; reset them afterward if deliberately switching.

```bash
jq -e .configuration previous-deployment.json > configuration.json
CONFIGURATION_REVISION=$(jq -er .configurationRevision previous-deployment.json)
REPOSITORY=$(jq -er .sourceRepository previous-deployment.json)
REQUESTED_REF=$(jq -er .requestedRef previous-deployment.json)
```

### Write a new configuration

Create `configuration.json` in your request directory. This Bun example needs root `build` and `start` scripts, port 3000, and `/health`:

```json
{
  "schemaVersion": 1,
  "build": {
    "runtime": "bun",
    "packages": ["."],
    "buildScript": "build",
    "startScript": "start"
  },
  "runtime": {
    "port": 3000,
    "healthPath": "/health"
  },
  "storageBindings": []
}
```

Adjust it to the app, then set `CONFIGURATION_REVISION=1` or increment the accepted revision.

| Field | Rules |
| --- | --- |
| `schemaVersion` | `1`. |
| `build.runtime` | `node` installs with `npm ci`; `bun` with `bun install --frozen-lockfile`. |
| `build.packages` | 1–32 unique normalized relative paths; `"."` means root. Each needs a regular lockfile, at most 1 MiB: `package-lock.json` for Node.js, `bun.lock` or `bun.lockb` for Bun. |
| `build.buildScript` | Root `package.json` script, run once from root after installation; `null` skips building. Names use letters, digits, `:._-`, at most 128 characters. |
| `build.startScript` | Root script starting the server; same name rules. |
| `runtime.port` | Integer 1–65535, injected as `PORT`. |
| `runtime.healthPath` | Starts with `/`, no query or fragment, at most 256 characters; returns 2xx without redirects. |
| `storageBindings` | At most 32; every active storage resource must be bound. |

Root `package.json` must be a regular file of at most 64 KiB containing the configured scripts. Keep persistent data in managed storage: the container filesystem is read-only and worker disks are replaceable.

Reserved variables include `PORT`, `NODE_ENV=production`, `PLATFORM_ENV=production`, `PLATFORM_PROJECT_ID`, `PLATFORM_PROJECT_SLUG`, `NODE_EXTRA_CA_CERTS` (platform CA), and `AWS_REQUEST_CHECKSUM_CALCULATION=when_required`; `STORAGE__` prefixes are reserved too. The job also sets `HOST=0.0.0.0`.

#### Node.js and Bun versions

Version selection reads root repository files in this order:

| Runtime | Sources |
| --- | --- |
| Bun | `packageManager: "bun@x.y.z"`, `engines.bun`, `.bun-version` |
| Node.js | `engines.node`, `.nvmrc`, `.node-version`; supports `lts/*`, ignores npm/pnpm/Yarn `packageManager` entries |

The builder chooses the newest matching release, preferring Node.js LTS when allowed, using an official digest-pinned `-slim` image. Without a version request, the builder uses the policy default. Impossible ranges or ranges allowing only versions before Node.js 20 or Bun 1.1 fail with build-log guidance. Lookup failure gives `RUNTIME_UNAVAILABLE`; retry later. The deployment's `runtime` records the resolved version after building.

<details>
<summary>Accepted version syntax</summary>

The [parser](../../openstack_platform/runtime_versions.py) accepts a subset of npm semver: exact, partial, x-ranges, `^`, `~`, comparators, hyphen ranges, spaces for AND, `||` for OR. Only releases are chosen; prerelease tags move bounds. Components have at most nine digits; only spaces are allowed as range whitespace; version files are at most 1 KiB. Node.js metadata comes from `nodejs.org`, Bun tags from Docker Hub `oven/bun`.

</details>

### Add managed storage

Create storage before deploying, then bind its outputs. Credentials enter the runtime, never configuration.

```bash
STORAGE_TYPE=postgres   # postgres, mongo, or s3
STORAGE_NAME=default
jq -n --arg type "$STORAGE_TYPE" --arg name "$STORAGE_NAME" '{type: $type, name: $name}' > storage.json
newkey > storage.key
project -H 'Content-Type: application/json' -H "Idempotency-Key: $(<storage.key)" \
  --data-binary @- "$PROJECT_BASE/storage" < storage.json > storage-submitted.json
wait_for "$(jq -er .operationId storage-submitted.json)"
RESOURCE_ID=$(admin_all storage | jq -er --arg id "$APP_ID" --arg type "$STORAGE_TYPE" \
  --arg name "$STORAGE_NAME" \
  'select(.applicationId == $id and .type == $type and .name == $name
    and .lifecycleState == "active") | .resourceId')
jq --arg id "$RESOURCE_ID" \
  '.storageBindings += [{resourceId: $id, outputs: {url: "DATABASE_URL"}}]' \
  configuration.json > configuration.next && mv configuration.next configuration.json
```

This binds PostgreSQL `url` to `DATABASE_URL`; adjust `outputs` for other types. Target names must be unique and unreserved.

| Type | Outputs | Conventional variables |
| --- | --- | --- |
| `postgres` | `url`, `host`, `port`, `database`, `user`, `password`, `sslmode`, `sslrootcert` | `DATABASE_URL`, `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`, `PGSSLROOTCERT` |
| `mongo` | `uri` | `MONGODB_URI` |
| `s3` | `endpoint`, `public_endpoint`, `region`, `access_key_id`, `secret_access_key`, `bucket` | `AWS_ENDPOINT_URL_S3`, `S3_PUBLIC_ENDPOINT`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET` |

S3 `public_endpoint` is `https://s3.<domain>` for browser presigned URLs. See [the storage contract](../../openstack_platform/controller/storage_contract.py).

### Set environment variables

Values live in app-scoped Nomad Variables; controller reads return names only. Send private-file contents through stdin:

```bash
ENV_NAME='<VARIABLE_NAME>'
VALUE_FILE='<PATH_TO_PRIVATE_FILE>'
newkey > env.key
jq -n --rawfile value "$VALUE_FILE" '{value: ($value | rtrimstr("\n"))}' |
  project -X PUT -H 'Content-Type: application/json' -H "Idempotency-Key: $(<env.key)" \
    --data-binary @- "$PROJECT_BASE/environment/$ENV_NAME" > env-submitted.json
wait_for "$(jq -er .operationId env-submitted.json)"
project "$PROJECT_BASE/environment" | jq '{revision, keys}'
```

For bulk import, use `jq -n --rawfile dotenv <FILE> '{dotenv: $dotenv}'` and send it to `POST $PROJECT_BASE/environment/import`. Delete with `DELETE $PROJECT_BASE/environment/<NAME>` and a new key. Names start with a capital letter and use `A-Z`, `0-9`, `_`.

> [!IMPORTANT]
> Environment changes restart active apps and wait for health. Deployments inherit current values; they don't stage environment changes.

### Deploy from a private repository

The build tries credential-free HTTPS first. For private repositories, generate an app deploy key; its private half stays on admin:

```bash
echo '{}' | project -H 'Content-Type: application/json' --data-binary @- \
  "$PROJECT_BASE/source-key" | jq -r '.publicKey, .fingerprint'
```

A GitHub repository admin must add the public key as read-only. Then check branch access and commit inputs:

```bash
jq -n --arg repository "$REPOSITORY" --arg branch "$REQUESTED_REF" \
  '{repository: $repository, branch: $branch}' |
  project -H 'Content-Type: application/json' --data-binary @- \
    "$PROJECT_BASE/source-key/check" | jq .
jq -n --arg repository "$REPOSITORY" --arg commit "$COMMIT" \
  --slurpfile config configuration.json \
  '{repository: $repository, commit: $commit, configuration: $config[0]}' |
  project -H 'Content-Type: application/json' --data-binary @- \
    "$PROJECT_BASE/source/check" | jq .items
```

The first reports `reachable`, branch `head`, and `problem` (`key-refused`, `not-found`, `branch-missing`, `unavailable`). The second lists package, script, runtime, and lockfile checks as `ok`, `problem`, or `unknown`. Both require a deploy key; otherwise they return `keyPresent: false`.

### Reserve optional addresses first

Reserve [fixed IPv4](#keep-a-workers-primary-fixed-ipv4) or [outbound IPv4](#reserve-a-stable-outbound-ipv4) before the deployment that uses it. An app cannot have both.

## Choose a deployment path

| Path | Use for | Downtime |
| --- | --- | --- |
| [Same worker](#deploy-code-to-the-existing-worker), `reuseWorker: true` | Accepted app with a healthy worker; preserves VM, port, IP, flavor, allocation | Image download, startup, and checks; no VM boot |
| [Maintenance replacement](#plan-and-submit-a-worker-replacement), `plan`, `maintenance: true` | First deployment, sizing, OS refresh, single-process apps, fixed IPv4 | Old version serves during build; outage from cutover through VM boot and checks |
| [Rolling replacement](#plan-and-submit-a-worker-replacement), `plan`, `maintenance: false` | Apps tolerating concurrent versions; forbidden with fixed IPv4 | None planned; needs quota for two workers |

Single-process apps have no zero-downtime guarantee. No path restores data.

## Deploy code to the existing worker

After building and checking the artifact, storage, worker identity, and capacity, the controller confirms old allocations have stopped and replaces the job on the same VM. It uses the normal app URL and cached images when available. Reuse cannot change flavor, refresh the OS, or move onto a newly reserved port; those need replacement.

```bash
jq -n --arg repository "$REPOSITORY" --arg ref "$REQUESTED_REF" --arg commit "$COMMIT" \
  --argjson revision "$CONFIGURATION_REVISION" --slurpfile config configuration.json \
  '{repository: $repository, requestedRef: $ref, commit: $commit,
    configurationRevision: $revision, configuration: $config[0],
    maintenance: true, reuseWorker: true}' > deployment.json
newkey > deployment.key
admin -H 'Content-Type: application/json' -H "Idempotency-Key: $(<deployment.key)" \
  --data-binary @- "$BASE/deployments" < deployment.json > submitted.json
DEPLOYMENT_ID=$(jq -er .operationId submitted.json)
```

Both flags must be boolean `true`; omit `plan`. Validation failure never creates a fallback worker. Continue to [verification](#poll-and-verify-acceptance).

## Plan and submit a worker replacement

A flavor is an OpenStack VM size. Review its plan; planning reserves no quota. For first deployment, replace the first line below with `FLAVOR='<FLAVOR_NAME_OR_ID>'` using your [chosen size](#choose-a-flavor-for-a-first-deployment).

```bash
FLAVOR=$(jq -er .sizing.workerFlavor before-app.json)   # or '<FLAVOR_NAME_OR_ID>'
admin --get --data-urlencode "flavor=$FLAVOR" "$BASE/resize-plan" > plan.json
jq '{deploymentId, current, flavor, reserve, activation}' plan.json
```

First-deployment plans show `deploymentId: null`. Review sizing and maintenance downtime; leave active apps enabled through building.

```bash
MAINTENANCE=true   # false only for apps that can run two versions at once
jq -n --arg repository "$REPOSITORY" --arg ref "$REQUESTED_REF" --arg commit "$COMMIT" \
  --argjson revision "$CONFIGURATION_REVISION" --argjson maintenance "$MAINTENANCE" \
  --slurpfile config configuration.json --slurpfile plan plan.json \
  '{repository: $repository, requestedRef: $ref, commit: $commit,
    configurationRevision: $revision, configuration: $config[0],
    plan: $plan[0], maintenance: $maintenance}' > deployment.json
newkey > deployment.key
admin -H 'Content-Type: application/json' -H "Idempotency-Key: $(<deployment.key)" \
  --data-binary @- "$BASE/deployments" < deployment.json > submitted.json
DEPLOYMENT_ID=$(jq -er .operationId submitted.json)
```

Maintenance stops the old job, deletes its worker, and confirms absence before creating the new worker. Rolling replacement overlaps workers until acceptance. Choose overlap only if the app permits it.

## Poll and verify acceptance

Builds take minutes and continue after disconnection. Poll from the saved directory.

```bash
wait_for "$DEPLOYMENT_ID" && echo "deployment succeeded"
```

Check status and phase; [diagnose failures](#diagnose-resume-or-roll-back). After success, verify acceptance and public routing:

```bash
app_record > after-app.json
deployment_record "$DEPLOYMENT_ID" > accepted-deployment.json
jq -e --arg id "$DEPLOYMENT_ID" '.enabled and .activeDeploymentId == $id' after-app.json
jq -e --arg commit "$COMMIT" '.status == "succeeded" and .repositoryCommit == $commit' \
  accepted-deployment.json
jq '{runtime, imageDigest, configurationSha256}' accepted-deployment.json
URL=$(jq -er .url after-app.json)
HEALTH_PATH=$(jq -er .configuration.runtime.healthPath accepted-deployment.json)
curl --fail --silent --show-error "$URL$HEALTH_PATH"
```

Both `jq` checks must print `true`; `curl` prints the health response. Test representative features, compare sizing, configuration, and storage with your baseline, check platform health and completed `cleanupState`. Verify retained fixed `address` and `portId` are attached to the accepted worker; test required outbound addressing or SMTP from the app. Replacement discards old worker files; managed data stays in storage.

## Diagnose, resume, or roll back

### Read the outcome

| Outcome | Action |
| --- | --- |
| `failed` before cutover | Old version still serves. Read logs, correct source/configuration, submit a new key; don't stop the app. |
| `failed`, `RUNTIME_UNAVAILABLE` | Nothing built; retry lookup later with a new key. |
| `failed`, `PLATFORM_BUSY` | No changes began; retry after maintenance with a new key. |
| `failed` in rolling deployment | Candidate removed, accepted version serves; fix and submit a new key. |
| `failed` after maintenance cutover, cleanup confirmed | Accepted pointer unchanged, app stopped; [restore service](#restore-service-after-a-failed-cutover). |
| `recovery_required`, timeout, or lost connection | Fix the named dependency, then [replay unchanged](#resume-an-interrupted-operation). |

Resumption reports `running` and clears the previous error.

### Read logs

Logs use `project.sock` and may contain secrets; keep them private.

```bash
project "http://localhost/v1/deployments/$DEPLOYMENT_ID/build-log?lines=200" | jq -r .text
project "http://localhost/v1/deployments/$DEPLOYMENT_ID/startup-log" > startup-log.json
project "$PROJECT_BASE/runtime-log?lines=200&stream=stderr" | jq -r .text
```

Build logs cover checkout, runtime, installation, and building. Startup logs capture removed unhealthy candidates' allocation status, restarts, events, and output tail; `captured: false` means no record. Runtime logs cover accepted containers, default to `stdout`, accept `stderr`, and allow up to 1,000 lines.

Health checks allow 10 minutes from placement, including image download; repeated crashes can fail sooner.

### Resume an interrupted operation

Replay the body and key to retrieve or resume the operation. Recovery reuses recorded resources where possible and rechecks health.

```bash
admin -H 'Content-Type: application/json' -H "Idempotency-Key: $(<deployment.key)" \
  --data-binary @- "$BASE/deployments" < deployment.json > submitted.json
wait_for "$(jq -er .operationId submitted.json)"
```

A new key cannot bypass unfinished work; a changed body conflicts with the old key.

### Restore service after a failed cutover

Maintenance failures don't automatically roll back code or data.

- After replacement, [enable](#enable-disable-or-restart-an-app) the accepted version on a new worker, or submit a corrected deployment with a fresh plan.
- After same-worker cutover, the candidate job is removed but the VM remains. [Roll back on that worker](#roll-back-on-the-same-worker) to the still-accepted deployment, or submit a corrected same-worker request with a new key. Don't disable: that deletes the VM.

### Choose a rollback target

Rollback restores an accepted image and configuration, keeping environment, credentials, and sizing. Check data compatibility.

```bash
admin_all deployments |
  jq -c --arg id "$APP_ID" 'select(.applicationId == $id and .status == "succeeded")
    | {deploymentId, repositoryCommit, requestedRef, acceptedAt, imageDigest}'
```

Choose a complete source record whose image still exists; registry retention may remove it, and rollback never rebuilds. Resolve unfinished app operations with their original requests first.

### Roll back on the same worker

This keeps the VM, port, and address and can restore a stopped app after failed reuse. Planning checks worker identity and capacity, storage, and image sequentially and can take two minutes. The slug confirmation and `reuseWorker: true` plan authorize downtime; a missing or changed worker is refused.

```bash
TARGET='<DEPLOYMENT_ID_TO_RESTORE>'
admin "$BASE/rollback-plan?deploymentId=$TARGET&reuseWorker=true" > rollback-plan.json
jq '{activeDeploymentId, targetDeploymentId, sourceCommit, imageDigest, configurationSha256,
  environmentRevision, sizing, worker}' rollback-plan.json
jq -n --slurpfile plan rollback-plan.json --arg slug "$SLUG" \
  '{plan: $plan[0], confirmation: $slug}' > rollback.json
newkey > rollback.key
admin -H 'Content-Type: application/json' -H "Idempotency-Key: $(<rollback.key)" \
  --data-binary @- "$BASE/rollback" < rollback.json > rollback-submitted.json
ROLLBACK_ID=$(jq -er .operationId rollback-submitted.json)
wait_for "$ROLLBACK_ID" && echo "rollback succeeded"
```

Verify the new attempt's `activeDeploymentId == $ROLLBACK_ID`, target commit/digest/configuration hash in `deployment_record "$ROLLBACK_ID"`, and [public health](#poll-and-verify-acceptance). The target record stays unchanged.

### Roll back by replacing the worker

Set `TARGET`. Leave apps enabled if overlap and quota permit; with retained fixed IPv4, disable and wait.

```bash
admin --get --data-urlencode "deploymentId=$TARGET" "$BASE/rollback-plan" > rollback-plan.json
jq . rollback-plan.json
```

Review the plan, then rerun the submission block above from `jq -n --slurpfile plan` through `wait_for`, creating a fresh body and key. A failed candidate leaves an enabled predecessor serving. Verify as above.

### What not to do

Don't recover through manual OpenStack changes, SQLite edits, or Nomad garbage collection. Unknown ownership needs escalation; see [Troubleshooting](troubleshooting.md).

## Enable, disable, or restart an app

Send an empty body through `project.sock`:

```bash
ACTION=enable   # enable, disable, or restart
echo '{}' > empty.json
newkey > "$ACTION.key"
project -H 'Content-Type: application/json' -H "Idempotency-Key: $(<"$ACTION.key")" \
  --data-binary @- "$PROJECT_BASE/$ACTION" < empty.json > "$ACTION-submitted.json"
wait_for "$(jq -er .operationId "$ACTION-submitted.json")"
```

| Action | Result |
| --- | --- |
| `disable` | Stops app and deletes worker; keeps configuration, storage, reservations |
| `enable` | Boots the accepted deployment on a worker at pinned sizing; needs an accepted deployment |
| `restart` | Restarts container in place without rebooting VM |

## Size an app

New apps use the policy flavor; operators can pin custom sizing per app.

### Plan and resize

Resize replaces the worker using the accepted build, then checks health and routing. Code, environment, and storage stay unchanged.

1. Plan the target flavor:

```bash
FLAVOR='<FLAVOR_NAME_OR_ID>'
admin --get --data-urlencode "flavor=$FLAVOR" "$BASE/resize-plan" > resize-plan.json
jq . resize-plan.json
```

2. Review `current.enabled`, `flavor`, `reserve`, and `activation`. Allow quota for overlapping workers.
3. Submit:

```bash
jq -n --slurpfile plan resize-plan.json --arg slug "$SLUG" \
  '{plan: $plan[0], confirmation: $slug}' > resize.json
newkey > resize.key
admin -H 'Content-Type: application/json' -H "Idempotency-Key: $(<resize.key)" \
  --data-binary @- "$BASE/resize" < resize.json > resize-submitted.json
RESIZE_ID=$(jq -er .operationId resize-submitted.json)
wait_for "$RESIZE_ID" && app_record | jq .sizing
```

Success shows updated `workerFlavor`, `cpuMHz`, and `memoryMiB`. Sizing stays pinned across deployments and disable/enable.

<details>
<summary>How the CPU and memory allocation is computed</summary>

From registered capacity, reserve the largest of 10% rounded up, 200 MHz CPU/512 MiB RAM, or Nomad's reserve. Containers get the remainder. Flavors below 576 MiB RAM and nodes below pinned allocations fail.

</details>

### Resize an app that can't run two copies

Either submit a replacement deployment with the new flavor's plan and `maintenance: true`, keeping service through building, or disable and wait, fetch a fresh resize plan with `current.enabled: false`, and resize the existing build. The latter confirms old worker absence before creating another.

### Choose a flavor for a first deployment

Include a post-declaration plan (`deploymentId: null`) in the [replacement request](#plan-and-submit-a-worker-replacement). Allow reserve headroom: 2048 MiB total cannot provide 2048 MiB for the container.

### Sizing failures and retries

Plan or confirmation mismatch creates no VM; review a fresh plan and key. Health failure preserves the worker and size; investigate before retrying. Uncertain work needs the original body and key.

## Retry application and storage operations

Poll first; deletion's admin operation URL remains readable after the app is gone. Replay unknown or `recovery_required` work unchanged, including secret values: the controller doesn't store request bodies. Replaying `failed` returns the failure without rebuilding; corrections need a new key.

Rejected builds become `failed` only after builder, port, and temporary registry tag cleanup is confirmed. Uncertain cleanup stays `recovery_required`; replay retries cleanup only, even after restart. Investigate remaining artifacts separately; they aren't automatically deleted or adopted. Failed-build cleanup never removes serving workers or storage.

Storage kinds are `storage.create`, `storage.verify`, `storage.rotate`, `storage.remove`; recovery requires exact kind and scope. Legacy `storage.<type>.<action>` operations remain blocked: obtain a separately reviewed recovery plan. There is no cancel or force-unlock endpoint; don't edit SQLite.

| Error | Action |
| --- | --- |
| `400 INVALID_REQUEST`, `INVALID_JSON`, `INVALID_QUERY` | Correct the field named by `summary`. |
| `404 NOT_FOUND` | Check route and socket; use the other transport if needed. |
| `503 PEER_IDENTITY_REJECTED` | Check transport account. |
| `409 IDEMPOTENCY_CONFLICT` | Original body, or a new key for new work. |
| `409 OPERATION_CONFLICT` | Finish the blocking operation named in the error. |
| `409 STATE_CONFLICT` | Check `app_record`. |
| `503 CONNECTION_LIMIT`, `OPERATION_QUEUE_FULL`, `DEPENDENCY_UNAVAILABLE` | Retry unchanged later. |
| `504 DEADLINE_EXCEEDED` | Retry unchanged. |

## Keep a worker's primary fixed IPv4

On provider-routed worker networks, reserve a specific IPv4 on a controller-owned Neutron port. It survives disable/enable, maintenance replacement, resize, and rollback. Cloud routing and policy still apply; reservation creates no floating IP, router, extra interface, ingress rule, or DNS, and doesn't establish public reachability.

Fixed and [outbound reservations](#reserve-a-stable-outbound-ipv4) are mutually exclusive. Workers cannot overlap on a fixed address: reuse for code updates, maintenance for replacements, disable first for standalone resize or replacement rollback. Take a fresh controller backup before the platform's first reservation.

### Reserve the address

Use the worker network and IPv4 subnet UUIDs:

```bash
NETWORK_ID='<WORKER_NETWORK_UUID>'
SUBNET_ID='<WORKER_IPV4_SUBNET_UUID>'
ADDRESS='<IPV4_ADDRESS>'
jq -n --arg network "$NETWORK_ID" --arg subnet "$SUBNET_ID" --arg address "$ADDRESS" \
  '{networkId: $network, subnetId: $subnet, address: $address}' > fixed-ip-plan-request.json
admin -H 'Content-Type: application/json' --data-binary @- \
  "$BASE/fixed-ip/plan" < fixed-ip-plan-request.json | jq .
jq '. + {action: "reserve"}' fixed-ip-plan-request.json > fixed-ip.json
newkey > fixed-ip.key
admin -H 'Content-Type: application/json' -H "Idempotency-Key: $(<fixed-ip.key)" \
  --data-binary @- "$BASE/fixed-ip" < fixed-ip.json > fixed-ip-submitted.json
wait_for "$(jq -er .operationId fixed-ip-submitted.json)"
admin "$BASE/fixed-ip" | jq .
```

The read-only plan checks worker network, IPv4 subnet, usable CIDR address, and project security group. Addresses outside automatic allocation pools are allowed, but `availability: unproven-until-reserved` means only reservation proves availability and permission.

The final read rechecks the provider and returns `portId`, `address`, `phase`, and `attachment` (`detached`/`attached`, with `serverId`). It doesn't test traffic.

### Move the app onto the address

Reservation leaves the current worker untouched and sizing pinned.

1. For an active app, submit a maintenance replacement; building precedes cutover. Enable and worker reuse cannot swap an ordinary worker onto this port.
2. For a disabled or new app, the next replacement deployment, resize, or rollback uses the port.
3. After acceptance, confirm the same `portId` and `address` are attached through `admin "$BASE/fixed-ip"`; test normal HTTPS and dependencies.

### Release the address

Disable or failed-candidate cleanup preserves the detached port. To give it up, disable and wait, then release with a new key:

```bash
echo '{"action": "release"}' > fixed-ip-release.json
newkey > fixed-ip-release.key
admin -H 'Content-Type: application/json' -H "Idempotency-Key: $(<fixed-ip-release.key)" \
  --data-binary @- "$BASE/fixed-ip" < fixed-ip-release.json > fixed-ip-release-submitted.json
wait_for "$(jq -er .operationId fixed-ip-release-submitted.json)"
```

Release deletes the exact owned port and loses the address. App deletion removes workers before releasing it.

### Recover a reservation

Replay uncertain creation or deletion with the original body and key. Lost-create recovery requires the exact recorded ownership marker; no match doesn't permit a second allocation. Unresolved worker creation blocks release. Ownership drift, changed groups, extra addresses, trunks, or another VM's attachment block changes; there is no name-based adoption or forced release.

Don't modify ports externally: Neutron lacks compare-and-set for attachment/deletion.

## Reserve a stable outbound IPv4

A floating IPv4 gives external allowlists a stable source address across replacement, resize, and disable/enable. It doesn't change URL, Cloudflare, TLS, guest routing, or firewall rules; direct app access at that address isn't supported. Handover doesn't preserve open connections.

Builders and candidates don't use the address. The app must pass health without it; handover happens only after acceptance. Provider-routed public worker networks should use [fixed IPv4](#keep-a-workers-primary-fixed-ipv4). This feature retains no port and creates no interface, network, or router; zero floating-IP quota or no matching routed subnet makes it unavailable.

### Check cloud capability

Take a fresh controller backup before the first reservation. Require exactly one IPv4 worker subnet and one project router connecting it to the external network with SNAT. Controller credentials need reads for quotas, networks, subnets, routers, ports, servers, security groups, and floating IPs, plus floating-IP creation/association/deletion. Preserve project worker security groups, port security, and ingress-only app rules; the controller changes no rules.

```bash
EXTERNAL_NETWORK_ID='<EXTERNAL_NETWORK_UUID>'
jq -n --arg network "$EXTERNAL_NETWORK_ID" '{externalNetworkId: $network}' \
  > public-ip-plan-request.json
admin -H 'Content-Type: application/json' --data-binary @- \
  "$BASE/public-ip/plan" < public-ip-plan-request.json > public-ip-plan.json
jq '{supported, reasons, floatingIpQuota, floatingIpsUsed, routerId}' public-ip-plan.json
```

This read-only plan needs no key and reserves no quota. Require `supported: true`; reasons include `floating_ip_quota_exhausted` and `no_unique_visible_snat_router_for_worker_subnet`. Failed or malformed provider reads fail the plan.

### Allocate, attach, or release

| Action | Result |
| --- | --- |
| `allocate` | Creates platform-owned IP; release/app deletion deletes it |
| `attach` | Uses a supplied project IP on the chosen network, unassociated and unreserved; release detaches without deleting |
| `release` | Gives up reservation |
| `reconcile` | Checks completed reservation and converges recorded association; unrelated changes block it |

Allocate:

```bash
jq -n --arg network "$EXTERNAL_NETWORK_ID" \
  '{action: "allocate", externalNetworkId: $network}' > public-ip.json
newkey > public-ip.key
admin -H 'Content-Type: application/json' -H "Idempotency-Key: $(<public-ip.key)" \
  --data-binary @- "$BASE/public-ip" < public-ip.json > public-ip-submitted.json
wait_for "$(jq -er .operationId public-ip-submitted.json)"
admin "$BASE/public-ip" | jq .
```

Other bodies, each requiring a new key:

```text
{"action": "attach", "externalNetworkId": "<EXTERNAL_NETWORK_UUID>", "floatingIpId": "<FLOATING_IP_UUID>"}
{"action": "release"}
{"action": "reconcile"}
```

GET reports recorded state, not a fresh provider check: `floatingIpId`, `address`, `phase`, `ownership` (`allocated`/`supplied`), `portId`, `pendingPortId`, `allocationMarker`. `reserved` means unassociated; `active` confirms mapping to the accepted worker.

Reserve before first deployment if needed. Disable detaches before deleting the worker; healthy enable reattaches. Unused reservations may cost money; releasing allocated addresses loses them.

### Verify the outbound address

Call a controlled endpoint from the accepted app, excluding admin, builders, or previews. Repeat after resize and disable/enable; also test normal HTTPS. Provider association alone doesn't prove the app's outbound source.

### Recover handover or release

The old worker retains the IP until candidate health and durable acceptance. Handover then moves and verifies the mapping before old-worker deletion; after acceptance it only moves forward.

- Failed or ambiguous handover keeps the old worker and marks `recovery_required`. Replay the deployment or resize request, not a new public-IP request; recovery checks candidate health and exact old and pending ports. Unrelated associations block it.
- Lost allocation responses can adopt only one exact unassociated ownership-marker match. Zero or multiple matches stay blocked; no automatic re-create or forced abandon. Escalate with operation, reservation, and marker IDs; don't edit SQLite or delete look-alikes.
- Ambiguous release retains its record for same-key retry. App deletion waits for release before deleting the worker.

IP and deployment changes share the app lock. Don't change floating IPs externally; Neutron reassociation isn't atomic.

## Related

- [Controller API reference](../reference/controller-api.md)
- [Platform internals](../reference/internals.md)
- [Manage apps and people](manage-apps-and-people.md)
- [Run the platform](run-the-platform.md)
- [Backups and recovery](backups-and-recovery.md)
- [Releases and upgrades](releases-and-upgrades.md)
- [Troubleshooting](troubleshooting.md)
