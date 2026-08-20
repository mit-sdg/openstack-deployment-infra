# Troubleshoot a platform checkpoint

Use this page when a checkpoint in [OPERATIONS.md](OPERATIONS.md) fails. The
commands either diagnose the failure or repeat its recovery operation. Run
repository commands from the checkout. Run managed backup and restore scripts
on admin through the pinned `platform-admin` alias. Do not clear SQLite rows,
bypass the helper, detach resources manually, print a secret, or touch a
resource the inventory does not name.

When a command below uses variables, initialize them on the management host
from the private inventory without printing it:

```bash
export PLATFORM_CONFIG="$PWD/config/platform.json"
export PLATFORM_NAMESPACE="$(uv run python infra/lib/platform_config.py get namespace)"
export PLATFORM_ROOT="$(uv run python infra/lib/platform_config.py get paths.root)"
export SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config
```

## Project identity and private inputs

**Symptom:** foundation or CLI reports a project name/UUID mismatch.

**Evidence:** On the management host, from the repository root, load the
protected credential file and use the supported project helper. It normalizes
compact and canonical provider UUIDs without printing credentials:

```bash
set -a
. "/srv/openstack-platform/.secrets/openstack.env"
set +a
source infra/lib/platform-config.sh
load_platform_config
export OS_PROJECT_NAME="$PLATFORM_PROJECT"
export OS_PROJECT_ID="$PLATFORM_PROJECT_ID"
export OSC=/srv/openstack-platform/bin/platform-openstack
verify_openstack_project "$OSC"
```

**Correction:** Load the intended OpenStack environment and private inventory.
Stop before mutation; do not substitute the example UUID or inspect another
project.

**Symptom:** private input is rejected as malformed or unsafe.

**Evidence:** The error names a file/key set or mode. Check only metadata:

```bash
for file in "$ADMIN_SECRETS_FILE" "$STORAGE_SECRETS_FILE" \
  "$CLOUDFLARE_TUNNEL_TOKEN_FILE"; do
  test -z "${file:-}" || {
    test -f "$file" && test ! -L "$file"
    test "$(stat -c '%a' "$file")" = 600
  }
done
```

**Correction:** Regenerate the exact direct mode-`0600` file. Admin requires
only `NOMAD_GOSSIP_KEY`. Storage requires the eight keys in
[OPERATIONS.md](OPERATIONS.md#generate-operator-keys-age-identity-and-bootstrap-files),
and ingress requires both Nomad token keys. Do not add quotes, `export`, or
unrelated keys.

**Symptom:** Nix evaluation fails.

**Evidence:** Run from the repository root:

```bash
export PLATFORM_CONFIG="$PWD/config/platform.json"
nix flake check --impure --no-build --print-build-logs
```

**Correction:** Replace example identities, digests, paths, or recipient in the
private copy. Keep credentials and private keys out of JSON.

## Foundation and role bootstrap

**Symptom:** foundation plan names an unexpected existing resource.

**Evidence:** The plan or `openstack port show NAME` shows a different network,
address, owner, or security group.

**Correction:** Stop. The foundation reconciler is non-deleting; reconcile
ownership with the cloud operator. Do not rename, repurpose, detach, or delete
the resource.

**Symptom:** OpenStack reports `ACTIVE`, but a role is not ready.

**Evidence:** `openstack console log show HOST` lacks the exact configured
readiness marker or contains a definitive bootstrap failure; `openstack server
show HOST` shows the state.

**Correction:** Inspect bounded serial evidence and failed units. Correct the
key, secret, PKI, selected image, volume attachment, or config-drive input.
Do not rerun an `apply_*` script against the existing failed server: those
scripts do not reapply user data. Build and live-test a corrected role image,
then use `openstack-platform infra replace ROLE --yes` after the control surface is
available. Before that point, obtain a reviewed rebuild/replacement of only the
configured role. `ACTIVE` alone is not readiness.

**Symptom:** an `apply_*` script refuses an existing host before waiting for
bootstrap.

**Evidence:** The safe error names a server/image/flavor, deployment metadata,
fixed port/address, volume attachment, or `delete_on_termination` mismatch.
The existing-host verifier compares the exact UUID/name and rejects malformed
or ambiguous provider projections.

**Correction:** Stop and reconcile the configured resource identity. These
scripts are fail-closed and do not reapply user data to an existing server.
Use a reviewed image rebuild/replacement boundary rather than rerunning an
apply script against a failed host.

**Symptom:** `setup_management_bridge.py` fails or the SSH smoke returns root.

**Evidence:** Check only ownership/modes and repeat the wrapper's non-secret
project identity checks:

```bash
stat -c '%U:%G %a %F' /srv/openstack-platform/bin/platform-openstack \
  /srv/openstack-platform/.secrets/ssh/config \
  /srv/openstack-platform/.secrets/ssh/known_hosts
bridge_output="$(
  /srv/openstack-platform/runtime/python3.14 deploy/platform-cli/setup_management_bridge.py \
    --platform-config /srv/openstack-platform/config/platform.json \
    --ssh-identity /srv/openstack-platform/.secrets/ssh/id_ed25519 \
    --ssh-config /srv/openstack-platform/.secrets/ssh/config \
    --known-hosts /srv/openstack-platform/.secrets/ssh/known_hosts \
    --provider-command /srv/openstack-platform/bin/platform-openstack
)"
test "$bridge_output" = management-bridge=verified
test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- id -un)" = agentops
test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- id -u)" -gt 0
```

**Correction:** Run as the unprivileged management owner. Make the wrapper a
direct owner-only mode-`0500`/`0700` executable, check that its token is scoped
to the configured project, and rerun the automation. A console ED25519
fingerprint/keyscan mismatch is a stop condition, not a reason to edit
`known_hosts`.

**Symptom:** ACL bootstrap succeeds but ingress rejects its token file.

**Evidence:** On management, verify the transferred file is direct, mode
`0600`, and has exactly two `NOMAD_*_TOKEN` assignments without printing it.
On admin, verify the source path is `$PLATFORM_ROOT/secrets/nomad-tokens.env`.

**Correction:** Rerun `bootstrap_acl.sh` on admin and transfer the generated
file through the pinned alias. If no ingress server exists yet, use the
corrected inputs for its initial `apply_ingress.sh` provisioning. If an
existing ingress host received the bad input, do not rerun `apply_ingress.sh`:
build and live-test a corrected ingress image, then use the supported
`openstack-platform infra replace ingress --yes` path, or obtain a reviewed
pre-control-surface rebuild. Do not invent or paste a Nomad token.

**Symptom:** admin helper reports missing storage/OpenStack/provisioning input.

**Evidence:** The admin-side paths are:

```text
<paths.root>/secrets/openstack.env                 mode 0600
<paths.root>/secrets/storage-bootstrap.env         mode 0600
<paths.root>/secrets/builder_operator_ed25519.pub  direct readable file
<paths.root>/secrets/builder_operator_ed25519      mode 0600
<paths.root>/persistent/secrets/provisioning-pki/  mode 0700
```

**Correction:** Transfer the exact files described in
[OPERATIONS.md](OPERATIONS.md#4-boot-storage-and-ingress-with-exact-transfers),
repair only their ownership/modes, and rerun the owning command. Never copy the
management inventory into the admin Nix-store configuration path.

## Public ingress

**Symptom:** DNS does not resolve or TLS does not cover the hostname.

**Evidence:** Query the exact platform/application hostname and inspect the
provider certificate/route separately.

**Correction:** Add the wildcard/per-host record or certificate in the
external provider. Do not disable certificate validation in acceptance checks.

**Symptom:** Traefik returns no route while ingress is reachable.

**Evidence:** The external service replaced the original `Host` header, or the
hostname is not exactly `<slug>.<domain>`.

**Correction:** Preserve `Host`, forward to ingress port 80, and repeat the
public HTTPS health request. A request to the ingress IP is not equivalent.

**Symptom:** Cloudflare ingress bootstrap rejects the token.

**Evidence:** The token file is missing, a symlink, mode other than `0600`,
contains whitespace, or is shorter than the provider token contract.

**Correction:** Obtain a fresh token from Cloudflare and install it as a
direct mode-`0600` file. If no ingress server exists yet, use it for initial
`apply_ingress.sh` provisioning. For an existing failed ingress host, do not
rerun `apply_ingress.sh`; build and live-test a corrected ingress image, then
use `openstack-platform infra replace ingress --yes`, or obtain a reviewed
pre-control-surface rebuild. With another provider set
`ENABLE_CLOUDFLARED=false` and satisfy the same forwarding/host/certificate
contract.

## Images, deployments, and replacement

**Symptom:** `infra image set` rejects an image.

**Evidence:** `infra image list` shows missing source commit, incomplete
metadata, wrong project owner/status, or a compatibility mismatch.

**Correction:** Publish a new full-commit candidate with `SOURCE_COMMIT`, run
all role/live acceptance checks, and select its exact UUID. Do not use an image
with incomplete provenance or overwrite a tested image name.

**Symptom:** builder or worker remains after a failed operation.

**Evidence:** `app show`, the operation error, or bounded provider observation
reports cleanup/recovery-required.

**Correction:** Restore OpenStack/SSH/registry/Nomad connectivity and rerun the
same owning CLI operation. Do not create a second resource or delete by name.

**Symptom:** `infra image prune` refuses a plan because a server image is
missing, malformed, or ambiguous.

**Evidence:** The provider server list omitted its image projection or returned
an invalid/ambiguous image value.

**Correction:** Stop. Pruning fails closed in this case; it does not assume the
server booted from a volume. Correct the provider projection or reconcile the
server identity, then create a new plan. Never broaden the deletion set by
hand.

**Symptom:** persistent role replacement is stuck or readiness fails.

**Evidence:** `openstack-platform infra logs ROLE --lines 200` and the operation
phase identify the dependency. The old host/volumes should remain retained
until readiness succeeds.

**Correction:** Use only:

```bash
/srv/openstack-platform/bin/openstack-platform infra replace ROLE --yes
```

after restoring the named dependency. Never delete the old server first or
detach a volume manually. An ambiguous provider result is recovery-required;
rerun the same command with the same selected image and role.

**Symptom:** application candidate fails health.

**Evidence:** `app show SLUG` reports scheduler/public health failure; bounded
`app logs SLUG --build` or `--runtime` identifies the build/runtime symptom.

**Correction:** Fix the public repository manifest, lockfile, runtime, health
path, or ingress route and deploy the intended full commit again. Failed
candidate cleanup is bounded recovery, not a manual job-history rollback.

## Backups and restore

**Symptom:** `backup` cannot stage or accept a file.

**Evidence:** Check the configured root and metadata without listing content:

```bash
backup_root="$(uv run python infra/lib/platform_config.py get paths.backups)"
test "$(ssh -F "$SSH_CONFIG" platform-admin -- stat -c '%a' "$backup_root/m1/.staging")" = 700
test -x /srv/openstack-platform/bin/age
/srv/openstack-platform/bin/age --version >/dev/null
```

**Correction:** Verify `/srv/openstack-platform/bin/age --version`, the policy's public age
recipient, the pinned bridge, and admin backup-volume mount. The expected
remote staging path is `$backup_root/m1/.staging`; accepted files are under
`$backup_root/m1`. Do not put a live WAL file in staging or manually move a
ciphertext.

**Symptom:** managed-data backup or restore check reports missing backup,
checksum/manifest failure, or cannot find `age`.

**Evidence:** You are on admin through the pinned alias, and the commands use
the installed paths. Preserve these overrides when rerunning; they select the
admin image's packaged dependencies rather than a checkout copy:

```bash
managed_backup_output="$(
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
printf '%s\n' "$managed_backup_output"
grep -Eq '^platform backup complete: .+$' <<<"$managed_backup_output"
restore_output="$(
  ssh -F "$SSH_CONFIG" platform-admin -- env \
    PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
    AGE="$PLATFORM_ROOT/bin/age" \
    AGE_KEY="$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt" \
    "$PLATFORM_ROOT/infra/backup/verify_latest_restore.sh"
)"
printf '%s\n' "$restore_output"
grep -Eq '^latest platform restore=verified evidence=.+/RESTORE-MANIFEST$' <<<"$restore_output"
```

**Correction:** Restore the admin storage secret, Garage backup key, packaged
`$PLATFORM_ROOT/bin/age`, admin backup identity, mounted backup volume, or
storage service. Rerun those exact admin commands, keeping their path
overrides. Keep failed archives; never overwrite live databases with an
unverified archive.

**Symptom:** offline restore refuses the backup.

**Evidence:** The error names a private file mode/owner, age identity, schema,
integrity, WAL/SHM sidecar, unfinished operation, deployment identity, lock,
or size condition. A deployment identity error means the backup's project,
namespace, stable resource inventory, or state paths do not match the installed
inventory; image/flavor/version upgrades alone do not cause that error.

**Correction:** Leave the destination untouched. Fix the named condition in a
private directory and rerun the installed managed restore launcher. It fixes
the managed destination and reports a verified schema/integrity result:

```bash
restore_output="$(
  /srv/openstack-platform/bin/openstack-platform-restore \
    /private/path/accepted.sqlite3.age \
    --age-identity /private/path/backup-age-identity.txt --yes
)"
printf '%s\n' "$restore_output"
grep -Eq '^restore=verified schema-version=[0-9]+ integrity=ok$' <<<"$restore_output"
```

The tool is offline and atomic; it does not restore provider resources. After a
successful restore, run `status`, `infra list`, `app list`, and `storage list`
to reconcile accepted records with live observations. Do not import external
rows or edit SQLite/provider state by hand.

**Symptom:** a command reports that its operation deadline or lock deadline was
reached.

**Evidence:** The command stopped while waiting for a scope lock, helper,
provider, build, or health dependency. The operation record retains its wall
clock deadline and phase.

**Correction:** Restore the named dependency and rerun the same command with
the same identity arguments. A second command cannot wait indefinitely behind a
held lock, and a builder cannot continue beyond the recorded deadline.

## Releases and tests

**Symptom:** installer rejects runtime, ownership, mode, archive, age, or
helper action map.

**Evidence:** The installer prints the specific safe preflight failure.

**Correction:** Use Python 3.14.7, uv 0.12.2, a clean full-commit checkout,
`uv.lock` with `uv sync --frozen`, direct mode-`0600` private inputs, the
protected wrapper, and the generated bridge. Do not install as root or bypass
the integration smoke.

Run the locked project checks from the repository root:

```bash
uv --version
uv sync --frozen
uv run ruff format --check platform_cli deploy/platform-cli infra tests
uv run ruff check platform_cli deploy/platform-cli infra tests
uv run mypy
uv run python -m unittest discover -s tests -v
```

**Symptom:** fresh CLI state contains unexpected rows or restored records do
not match live resources.

**Evidence:** `status`, `app list`, `storage list`, `infra list`, or a private
operation record names a record not created by this deployment.

**Correction:** Stop mutations and preserve the database and operation ID.
Escalate the bounded evidence. Do not delete rows, adopt an external resource,
or inspect a resource the inventory does not name.

For an unresolved failure, report only the safe correlation or operation ID,
phase, exact identity arguments, and bounded evidence. Do not report
credentials, unrestricted provider output, or age identity contents.
