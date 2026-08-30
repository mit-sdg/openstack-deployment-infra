# Verify a fresh platform and its backups

This tutorial starts after `openstack-platform setup` reports completion. It
verifies the supported pre-management state: persistent infrastructure, public
platform ingress, management-state backup, and managed-data restore checking.
It does not deploy an application. The application/storage CLI has been
retired, and the browser management and authentication applications are not
implemented.

A successful run ends with healthy persistent roles, an accepted encrypted
management backup, and a `RESTORE-MANIFEST` for the latest managed-data backup.

## What you need

Run on the operator host as the unprivileged owner of
`/srv/openstack-platform`. You need:

- setup output ending in `setup=complete`;
- configured external DNS and HTTPS for the platform hostname;
- the generated operator bridge at
  `/srv/openstack-platform/.secrets/ssh/config`; and
- the managed-data age identity initialized on admin as described in
  [Operations](OPERATIONS.md#managed-data-backup-and-restore-check-on-admin).

Set the installed paths and derive non-secret values from the private
inventory:

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

## 1. Verify the operator boundary

Inspect the installed command tree:

```bash
$PLATFORM_CLI --help
```

The supported top-level commands are `setup`, `status`, `backup`, `restore`,
and `infra`. Stop if the installed binary exposes product lifecycle commands;
that indicates an obsolete release. Do not use the obsolete release to modify
state.

The local controller API exists as an implementation boundary, but setup does
not start it as a service. Do not run it manually to work around the missing
management application. Future UI work follows
[MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md).

## 2. Verify accepted and live infrastructure

```bash
$PLATFORM_CLI status
$PLATFORM_CLI infra list
$PLATFORM_CLI infra image list
```

`status` must report `healthy`, five accepted infrastructure roles, zero
accepted applications, zero managed-storage resources, and three available
persistent-role observations. `infra list` must show accepted image metadata
for admin, ingress, storage, worker, and builder. The worker and builder are not
persistent live hosts.

If `APPS` or `STORAGE` is nonzero, stop. The state contains pre-cutover or
restored product records that the operator CLI intentionally cannot inspect or
mutate. Preserve the database and plan the controlled management/controller
cutover; do not install an older CLI.

## 3. Verify public platform ingress

```bash
test "$(curl --fail --show-error --silent \
  "https://${PLATFORM_DOMAIN}/healthz")" = OK
printf '%s\n' public-platform-health=verified
```

The exact body `OK` verifies public DNS, certificate validation, external
forwarding, original-host preservation, ingress, and the platform route. It
does not prove that a user-facing application workflow exists.

If the request fails, use [Public ingress](PUBLIC_INGRESS.md) to check DNS, TLS,
origin routing, and `Host` preservation separately.

## 4. Create both SQLite backups

Create the hosted-controller backup on admin:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl start "$PLATFORM_NAMESPACE-hosted-controller-backup.service"
ssh -F "$SSH_CONFIG" platform-admin -- \
  journalctl -u "$PLATFORM_NAMESPACE-hosted-controller-backup.service" -n 5 --no-pager
```

A successful run reports `hosted-controller-backup=... sha256=...` and commits
its evidence under `<paths.backups>/hosted-controller`.

Back up the separate external operator CLI state:

```bash
operator_backup="$($PLATFORM_CLI backup)"
printf '%s\n' "$operator_backup"
grep -Eq '^backup=platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age sha256=[0-9a-f]{64}$' \
  <<<"$operator_backup"
```

This evidence is accepted under `<paths.backups>/controller`. Neither SQLite
backup substitutes for the other; both outputs expose only a name and
ciphertext checksum.

## 5. Back up and restore-check managed data

Run the packaged managed-data backup on admin through the pinned alias:

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

Now verify the latest managed backup without overwriting live services:

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

The check decrypts into temporary storage, starts temporary PostgreSQL and
MongoDB containers, validates the Garage archive, and removes temporary
resources on success or failure. It writes `RESTORE-MANIFEST` only after every
check passes. It never replaces live managed data.

Preserve the management-backup age identity and the separate admin managed-data
age identity outside the deployment. Neither backup class contains its
identity.

## 6. Confirm scheduled backups

```bash
systemctl --user is-enabled openstack-platform-backup.timer
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl is-enabled "$PLATFORM_NAMESPACE-hosted-controller-backup.timer"
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl is-enabled "$PLATFORM_NAMESPACE-platform-backup.timer"
```

The timers cover external operator state, hosted-controller state, and managed
PostgreSQL/MongoDB/Garage data respectively. Registry blobs are not backed up and are
rebuilt from source.

You have now verified the complete supported pre-management operating result.
Continue with [Operations](OPERATIONS.md) for offline management restore, image
upgrades, recovery, and teardown. Use the
[acceptance checklist](ACCEPTANCE_CHECKLIST.md) to record production evidence.
