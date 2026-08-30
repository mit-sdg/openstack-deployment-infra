# Verify a fresh platform

Use this tutorial after `openstack-platform setup` reports `setup=complete`. It
verifies the installed operator boundary, hosted controller, public ingress,
three backup classes, restore checks, and schedules. It does not provide a
browser workflow; the management and authentication applications are not
implemented.

A successful run ends with five accepted role images, three healthy persistent
roles, a ready local controller, committed encrypted backup evidence, and a
managed-data `RESTORE-MANIFEST`.

## Set the installed paths

Run as the unprivileged owner of `/srv/openstack-platform` on the operator host.
You need the generated `platform-admin` SSH alias, configured public DNS/HTTPS,
and the managed-data age identity created by setup.

```bash
export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
export PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json
export SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config
export PLATFORM_NAMESPACE="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["namespace"])')"
export PLATFORM_ROOT="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["paths"]["root"])')"
export PLATFORM_DOMAIN="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["domain"])')"
```

## 1. Confirm the installed control surfaces

```bash
$PLATFORM_CLI --help
$PLATFORM_CLI status
$PLATFORM_CLI infra list
$PLATFORM_CLI infra image list
```

The supported top-level commands are `setup`, `status`, `backup`, `restore`,
and `infra`. Stop if the binary exposes application or storage product
commands; that is an obsolete release.

`status` must report five accepted infrastructure roles and three available
persistent-role observations. On a new deployment, `APPS` and `STORAGE` are
zero. Nonzero values indicate restored controller state; preserve it and follow
the recovery procedure rather than installing an older CLI.

Setup also starts the controller on admin. Verify the service and readiness
unit through the pinned alias:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-controller-readiness.service"
```

Both lines must be `active`. The controller has no public listener and is not a
browser application.

## 2. Verify public ingress

```bash
test "$(curl --fail --show-error --silent \
  "https://${PLATFORM_DOMAIN}/healthz")" = OK
printf '%s\n' public-platform-health=verified
```

The exact `OK` body verifies public DNS, certificate validation, external
forwarding, original-host preservation, ingress, and the static platform route.
It does not prove that user login or a management UI exists.

If it fails, follow [Public ingress](PUBLIC_INGRESS.md) before continuing.

## 3. Create both SQLite backups

Create the hosted-controller backup on admin:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl start "$PLATFORM_NAMESPACE-hosted-controller-backup.service"
ssh -F "$SSH_CONFIG" platform-admin -- \
  journalctl -u "$PLATFORM_NAMESPACE-hosted-controller-backup.service" -n 5 --no-pager
```

A successful run reports `hosted-controller-backup=... sha256=...` and commits
its evidence under `<paths.backups>/hosted-controller`.

Back up the separate external operator state:

```bash
operator_backup="$($PLATFORM_CLI backup)"
printf '%s\n' "$operator_backup"
grep -Eq '^backup=platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age sha256=[0-9a-f]{64}$' \
  <<<"$operator_backup"
```

This commits under `<paths.backups>/controller`. The two SQLite sets are not
interchangeable, and neither contains its age identity.

## 4. Back up and restore-check managed data

Run the packaged managed-data backup on admin:

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

The committed set contains encrypted PostgreSQL, MongoDB, Garage, and retained
OCI registry artifacts. Verify the latest set without replacing live services:

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

The check uses temporary PostgreSQL and MongoDB containers and validates Garage
and OCI archives. It removes temporary resources and writes `RESTORE-MANIFEST`
only after every check passes. It never overwrites live managed data.

## 5. Confirm schedules and off-site health

```bash
systemctl --user is-enabled openstack-platform-backup.timer
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl is-enabled "$PLATFORM_NAMESPACE-hosted-controller-backup.timer"
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl is-enabled "$PLATFORM_NAMESPACE-platform-backup.timer"
```

If off-site export is configured, also require a fresh verified receipt:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- openstack-platform-recovery status \
  --platform-config "/etc/$PLATFORM_NAMESPACE/platform.json" \
  --config "$PLATFORM_ROOT/persistent/offsite-export.json" \
  --receipt "$PLATFORM_ROOT/persistent/status/offsite-export.json"
```

You have verified the supported infrastructure result. Use
[Operations and disaster recovery](OPERATIONS.md) for restore, off-site export,
role replacement, image pruning, and recovery rules. Record release evidence
with the [acceptance checklist](ACCEPTANCE_CHECKLIST.md).
