# Troubleshoot a deployment

Use this page when setup, a role lifecycle operation, backup, restore, or public
health check fails. Run commands from the operator host unless a section says
admin. Do not clear SQLite rows, bypass the helper, detach provider resources,
print secrets, or inspect resources outside the installed inventory.

Initialize the non-secret paths first:

```bash
export PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json
export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
export SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config
export PLATFORM_NAMESPACE="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["namespace"])')"
export PLATFORM_ROOT="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["paths"]["root"])')"
```

## Setup preflight is not ready

**Evidence:** rerun the machine-readable, non-mutating check:

```bash
uv run openstack-platform setup check \
  --env-file /private/path/setup.env --json
```

Inspect `nameCollisions`, `quotaDeltas`, fixed-address availability, tooling,
ingress, and release evidence. Unknown Glance limits or missing signed QCOW2
sizes deliberately report not ready.

**Correction:** correct the protected environment or provider quota. Do not
create resources to bypass the preflight. Rerun the same check until it reports
ready, then use the same environment for `--apply`.

## Setup stopped after mutation

**Evidence:** keep the workspace and read the final safe phase/error. For an
existing role, use bounded provider console output and the exact readiness
marker rather than `ACTIVE` alone.

**Correction:** restore the named dependency and rerun the identical setup
command with the same environment and workspace. Setup verifies generated
material and named resources before reuse. Do not edit a partial server, port,
volume, keypair, image, database row, or generated secret. A changed deployment
identity requires an empty workspace and empty provider scope.

## Project identity or private input is rejected

**Evidence:** project errors identify a token/configured UUID or name mismatch.
Private-input errors identify the file, expected owner/mode, or exact key set.
Check metadata only:

```bash
stat -c '%U:%G %a %F' /private/path/setup.env \
  /srv/openstack-platform/bin/platform-openstack \
  /srv/openstack-platform/.secrets/ssh/config
```

**Correction:** load the intended OpenStack credential and inventory. Replace
symlinks or writable files with direct owner-controlled files at the documented
mode. Never substitute example UUIDs or print a credential to compare it.

## A persistent role is `ACTIVE` but unavailable

**Evidence:** use the bounded operator views:

```bash
$PLATFORM_CLI status
$PLATFORM_CLI infra list
$PLATFORM_CLI infra logs admin --lines 200
```

For `ingress` or `storage`, select that role. Compare the configured server,
selected image UUID, flavor, fixed port/address, attached volumes, operation
metadata, and exact readiness marker. An existing-host bootstrap script
correctly refuses identity drift and does not reapply cloud-init.

**Correction:** fix the dependency or publish a corrected role image. After the
control surface exists, use only:

```bash
$PLATFORM_CLI infra image set ROLE TESTED_IMAGE_UUID
$PLATFORM_CLI infra replace ROLE --yes
```

Never delete the old host first or detach a retained volume. An ambiguous result
is recovery-required; rerun the same replacement after restoring the named
dependency.

## The operator bridge or helper is unavailable

**Evidence:** verify direct private bridge metadata and the unprivileged alias:

```bash
stat -c '%U:%G %a %F' \
  /srv/openstack-platform/.secrets/ssh/config \
  /srv/openstack-platform/.secrets/ssh/known_hosts
test "$(ssh -F "$SSH_CONFIG" platform-admin -- id -un)" = agentops
```

A console ED25519 fingerprint mismatch, wrong token project, mutable provider
wrapper, or root remote account is a stop condition.

**Correction:** rerun the supported bridge setup from the same installed
inventory and SSH identity as documented in
[Release installation](RELEASE_INSTALLER.md#generate-the-pinned-operator-bridge).
For a helper release failure, rerun
`deploy/releases/deploy_helper_release.sh <commit>` from the matching clean
commit. Do not hand-edit `known_hosts`, select a host from request input, or copy
credentials into a release.

## The hosted controller is not ready

**Evidence:** on admin, inspect bounded unit state and socket metadata:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- systemctl status \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-controller-readiness.service" --no-pager
```

Setup requires the policy, complete helper release, active service/readiness,
mode-`0660` project socket, exact broker peer boundary, and hosted-backup timer.

**Correction:** reinstall the matching policy/helper release or restore the
named retained-state dependency, then restart the controller and readiness
units. Do not start a release-internal controller manually. The management and
authentication applications are separate and remain unimplemented.

## Public health fails

Check in this order:

1. the exact platform hostname resolves;
2. the certificate is browser trusted and covers that hostname;
3. the external provider uses the configured tunnel or exact direct CIDRs;
4. the provider preserves the original `Host` header;
5. ingress can serve `/healthz`; and
6. the response body is exactly `OK`.

A request to the ingress IP is not equivalent. In tunnel mode the public origin
port must remain closed. In direct mode a source outside `providerCidrs` must be
rejected. Use [Public ingress](PUBLIC_INGRESS.md) for mode-specific correction;
never add `0.0.0.0/0` as a diagnostic rule.

## Image selection or pruning is refused

**Evidence:** `infra image list` identifies incomplete provenance,
incompatibility, wrong ownership/status, or a missing source commit. Prune may
also report a missing or ambiguous server image projection.

**Correction:** publish and live-test a full-commit candidate, then select its
exact UUID. For pruning, correct the provider projection and create a new plan.
Do not overwrite a tested image name or broaden the deletion set manually.

## A backup job fails

First identify the class:

- hosted controller: `<namespace>-hosted-controller-backup.service` on admin;
- operator state: `openstack-platform backup` on the operator host; or
- managed data: `<namespace>-platform-backup.service` on admin.

**Evidence:** inspect only bounded service output and direct-file/mount metadata.
Confirm the expected age executable, public recipient or identity file,
configured backup mount, Garage backup key, and final manifest/checksum names.

**Correction:** restore the named direct file or mount and rerun that backup
class. Do not manually move a staged ciphertext, copy a live SQLite WAL, or
replace an age identity while retained backups use it. Exact commands and
accepted locations are in
[Back up all state classes](OPERATIONS.md#back-up-all-state-classes).

## Managed restore-check fails

**Evidence:** run the packaged check on admin, not from the checkout:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- env \
  PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
  AGE="$PLATFORM_ROOT/bin/age" \
  AGE_KEY="$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt" \
  "$PLATFORM_ROOT/infra/backup/verify_latest_restore.sh"
```

The failure identifies PostgreSQL, MongoDB, Garage, OCI, checksum, manifest, or
identity validation. A missing `RESTORE-MANIFEST` means the set has not passed.

**Correction:** retain the failed set, restore the packaged dependency, and
rerun the check. It uses temporary services and must never be redirected to
overwrite live data.

## Offline restore is refused

**Evidence:** the safe error identifies owner/mode/type, age identity, deployment
identity, schema, integrity, foreign keys, sidecars, unfinished operations,
lock, or size. A deployment-identity mismatch means project, namespace, stable
resource inventory, or state paths differ; normal image/version changes do not
cause it.

**Correction:** leave the destination untouched, correct the named condition in
a private directory, and rerun the same installed restore launcher. Do not edit
migration rows, delete sidecars, or use a generic destination override. See
[Restore external operator state offline](OPERATIONS.md#restore-external-operator-state-offline)
or [Restore the hosted controller](OPERATIONS.md#restore-the-hosted-controller).

## An operation is unfinished or recovery-required

**Evidence:** retain the operation/correlation ID, scope, phase, and safe error.
A deadline may have expired while waiting for a lock, helper, provider, build,
or health dependency.

**Correction:** restore that dependency and repeat the identical current
command or controller request. Controller recovery requires the same method,
path, body, and idempotency key. Never allocate a replacement key for the same
intent or clear the operation row manually.

## State is unsupported

`UNSUPPORTED_PRIOR_STATE` is not corruption recovery. Preserve the database,
provider evidence, and backup roots. Choose a new deployment namespace and empty
state roots, run greenfield setup, and import application data only through a
reviewed format-specific procedure. Do not edit markers or metadata to make old
state appear current.

For unresolved failures, report only bounded safe evidence: operation or
correlation ID, phase, exact non-secret identity arguments, and the failed
check. Keep credentials, provider payloads, and age identity contents out of
logs and tickets.
