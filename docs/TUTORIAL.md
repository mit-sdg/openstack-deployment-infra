# Deploy the first application

This tutorial starts after `openstack-platform setup` has created a healthy platform. It deploys one public application, creates one managed PostgreSQL database, verifies backups, and removes the application again.

For an application that requires its database during startup, the order changes slightly: declare the application, create storage, then deploy.

## What you need

You need:

- a setup result reporting `setup=complete` and healthy platform status;
- working DNS and HTTPS for `<slug>.<platform-domain>`;
- a public, credential-free GitHub repository;
- a full lowercase 40-character source commit; and
- `platform.yaml` plus the supported Node or Bun lockfile at that commit.

Private repositories are not supported. The build fetches the exact commit over HTTPS without credentials.

Set convenient non-secret values on the management host:

```bash
export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
export PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json
export PLATFORM_DOMAIN="$(${PLATFORM_CLI%/bin/openstack-platform}/runtime/python3.14 \
  -c 'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["domain"])')"
```

Or read the domain from your private setup inventory and export it directly.

## 1. Verify the empty platform

```bash
$PLATFORM_CLI status
$PLATFORM_CLI infra list
$PLATFORM_CLI app list
$PLATFORM_CLI storage list
```

`status` must report `healthy`. `infra list` must show accepted admin, ingress, storage, worker, and builder images. Stop if applications or managed resources exist that this deployment did not create; setup and restore are not state-import mechanisms.

## 2. Check the application manifest

A minimal Node application manifest is:

```yaml
version: 1
runtime: node
packages: [.]
scripts:
  build: build
  start: start
port: 8080
health:
  path: /health
```

`packages` are contained repository paths. Script values are package-script names, not shell commands. The relevant package directory must contain `package-lock.json` for Node or `bun.lock`/`bun.lockb` for Bun.

The platform generates its own recipe. Repository Dockerfiles, build arguments, build-time environment, and repository-controlled runtime environment are not accepted inputs.

## 3. Deploy an application that starts without storage

Choose a slug, repository, and immutable commit:

```bash
export APP_SLUG=demo
export APP_REPOSITORY=https://github.com/OWNER/REPOSITORY
export APP_COMMIT=FULL_LOWERCASE_40_CHARACTER_COMMIT

$PLATFORM_CLI app deploy "$APP_SLUG" \
  --repo "$APP_REPOSITORY" \
  --commit "$APP_COMMIT"
$PLATFORM_CLI app show "$APP_SLUG"
```

Acceptance means the single-use builder and its port were removed, the dedicated worker reached readiness, Nomad accepted the constrained job, the application health path passed, and the public route passed. `app show` records the exact commit and immutable image digest.

Verify HTTPS independently:

```bash
test "$(curl --fail --show-error --silent --output /dev/null \
  --write-out '%{http_code}' \
  "https://${APP_SLUG}.${PLATFORM_DOMAIN}/health")" = 200
```

Use bounded logs when a deployment does not reach acceptance:

```bash
$PLATFORM_CLI app logs "$APP_SLUG" --build --list
$PLATFORM_CLI app logs "$APP_SLUG" --build --lines 200
# The build header supplies an ID for an exact historical lookup:
$PLATFORM_CLI app logs "$APP_SLUG" --build --id BUILD_UUID --lines 200
$PLATFORM_CLI app logs "$APP_SLUG" --runtime --lines 200
```

Correct the named dependency and rerun the same deploy command. Do not delete its builder, worker, port, Nomad job, or operation row manually.

## 4. Create and verify managed storage

Create PostgreSQL only after the first deployment has been accepted:

```bash
$PLATFORM_CLI storage create "$APP_SLUG" postgres --name default
$PLATFORM_CLI storage verify "$APP_SLUG" postgres --name default
$PLATFORM_CLI storage show "$APP_SLUG" postgres --name default
```

The output contains non-secret provider identity and configured limits. Credentials remain in the owner-specific Nomad Variable and never enter command output or management SQLite.

Use `mongo` or `s3` instead of `postgres` when required.

### When the application needs storage at startup

Declare the inert application first, create storage, then deploy:

```bash
$PLATFORM_CLI app create "$APP_SLUG"
$PLATFORM_CLI storage create "$APP_SLUG" mongo --name primary
$PLATFORM_CLI storage verify "$APP_SLUG" mongo --name primary
```

At the selected commit, bind that existing resource in `platform.yaml`:

```yaml
storage:
  bindings:
    primary:
      type: mongo
      environment:
        uri: MONGODB_URI
```

The binding maps one typed output to the application's runtime key. It does not provision storage and cannot define arbitrary platform environment. Commit the manifest change, set `APP_COMMIT` to that exact commit, then deploy:

```bash
$PLATFORM_CLI app deploy "$APP_SLUG" \
  --repo "$APP_REPOSITORY" \
  --commit "$APP_COMMIT"
```

`app create` reserves the slug and policy sizing but creates no worker or running job. Storage creation can therefore install credentials before the first application process starts.

## 5. Back up and restore-check

Create an encrypted management-state backup from the manager:

```bash
management_backup="$($PLATFORM_CLI backup)"
printf '%s\n' "$management_backup"
grep -Eq '^backup=platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age sha256=[0-9a-f]{64}$' \
  <<<"$management_backup"
```

Managed data is backed up from admin using the packaged scripts and separate managed-data age identity. Derive the generated namespace and remote root from the installed private inventory, then run:

```bash
export PLATFORM_NAMESPACE="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["namespace"])')"
export PLATFORM_ROOT="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["paths"]["root"])')"
export SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config

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

The restore check uses temporary containers and never overwrites live services. Preserve both backup classes and their distinct age identities outside the deployment before any teardown.

## 6. Remove the tutorial resources

Remove managed storage before removing its application:

```bash
$PLATFORM_CLI storage remove "$APP_SLUG" postgres --name default --confirm default
$PLATFORM_CLI app remove "$APP_SLUG" --confirm "$APP_SLUG"
$PLATFORM_CLI app list
$PLATFORM_CLI storage list
```

For a Mongo example, replace `postgres` with `mongo`. S3 removal additionally requires `--purge-s3` when the bucket is non-empty.

Application removal succeeds only after its Nomad job and Variables, worker and fixed port, and tracked registry manifests are absent. It does not remove shared persistent roles or unrelated OpenStack resources.

Continue with [Operations](OPERATIONS.md) for offline management restore, upgrades, image pruning, recovery, and whole-deployment teardown. Record production evidence with the [acceptance checklist](ACCEPTANCE_CHECKLIST.md).
