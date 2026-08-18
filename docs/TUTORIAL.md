# Deploy one application on a fresh platform

This tutorial takes a new operator from a private checkout to one verified
HTTPS application and one verified PostgreSQL resource, then removes them. It
starts from an empty database and freshly configured OpenStack resources.

For the complete file formats, transfer paths, host contexts, and recovery
rules, keep [OPERATIONS.md](OPERATIONS.md) open beside this tutorial. Commands
marked `<PUBLIC_EXAMPLE>` are safe placeholders; `<SECRET>` is never a value
to paste into tracked documentation.

## What you need

You need:

- an empty OpenStack project with quota for the three persistent roles,
  disposable builders, and application workers;
- OpenStack credentials and an OpenStack SDK in an approved Python 3.14
  environment;
- Nix/QEMU for image evaluation and tests;
- a management host with the unprivileged `/srv/openstack-platform` owner;
- a public DNS/TLS/forwarding service that preserves `Host`; and
- custody for the backup and managed-data age identities; and
- a public GitHub repository holding the application you will deploy, with a
  `platform.yaml` at its root. Private repositories cannot be fetched.

Cloudflare Tunnel is the reference provider, but its token and DNS account are
external inputs. See [PUBLIC_INGRESS.md](PUBLIC_INGRESS.md).

## 1. Verify the checkout and target before mutation

From the repository root, use the locked Python environment for project tests:

```bash
cd /path/to/checkout                 # replace this path
uv --version                         # uv 0.12.2
uv sync --frozen
uv run python --version              # Python 3.14.x
uv run python -m unittest discover -s tests -v
```

Create private inventory and policy copies, set the project target, and verify
that the authenticated token belongs to exactly that project. These checks do
not mutate OpenStack:

```bash
cp -n config/platform.example.json config/platform.json
cp -n config/platform-policy.example.json config/platform-policy.json
chmod 0600 config/platform.json config/platform-policy.json
export PLATFORM_CONFIG="$PWD/config/platform.json"
export PLATFORM_POLICY="$PWD/config/platform-policy.json"
export PRIVATE_BOOTSTRAP=/private/path/platform-bootstrap
export AGE_STORE="$(nix build --no-link --print-out-paths .#age)"
```

Replace all example identities, UUIDs, addresses, paths, image digests, and
age recipient in the private JSON files. Generate the operator keys, exact
admin/storage secret files, backup age identity, and internal PKI as shown in
[OPERATIONS.md](OPERATIONS.md#generate-operator-keys-age-identity-and-bootstrap-files).
Derive configuration-dependent values only after saving those edits, and
create the protected OpenStack environment/wrapper from
[OPERATIONS.md](OPERATIONS.md#2-scope-and-reconcile-the-openstack-foundation):

```bash
export PLATFORM_NAMESPACE="$(uv run python infra/lib/platform_config.py get namespace)"
export PLATFORM_ROOT="$(uv run python infra/lib/platform_config.py get paths.root)"
export PLATFORM_DOMAIN="$(uv run python infra/lib/platform_config.py get domain)"
# Load the private OS_AUTH_URL/account/password environment without printing it.
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
Do not put OpenStack passwords, private keys, Nomad tokens, storage passwords,
Cloudflare tokens, or age identities in JSON or Git.

## 2. Build, live-test, and publish all five role images

Evaluate the private role configuration, then build every role from the same
checkout commit:

```bash
nix flake check --impure --no-build --print-build-logs
for role in admin ingress storage worker builder; do
  nix build --impure --print-build-logs \
    --out-link "result-$role" ".#${role}-image"
done
```

Run the package, VM, config-drive, and role-specific live checks in
[nix/README.md](../nix/README.md). A build or Glance upload is only a candidate;
do not select it before the disposable checks pass.

For a local publication, each configured image name must be a new/versioned
name. The publisher refuses overwrite and requires the full source commit:

```bash
SOURCE_COMMIT="$(git rev-parse HEAD)"
export SOURCE_COMMIT
export OSC=/srv/openstack-platform/bin/platform-openstack
for role in admin ingress storage worker builder; do
  qcow="$(find -L "result-$role" -type f -name '*.qcow2' -print -quit)"
  test -f "$qcow" && test ! -L "$qcow" && test -r "$qcow"
  sha256sum "$qcow"
  PATH="$PWD/.venv/bin:$PATH" \
    SOURCE_COMMIT="$SOURCE_COMMIT" OSC="$OSC" \
    infra/openstack/publish_nixos_image.sh "$role" "$qcow"
done
```

Each successful line reports an image UUID, `status=active`, the provider
checksum, and the full `source_commit`. The publisher computes the local
OpenStack-compatible MD5 checksum and rejects the upload unless Glance reports
the same checksum; record that value and the separately printed local
SHA-256 artifact checksum with the owner project and live-test evidence. The
protected CI route and its complete `SOURCE_COMMIT` metadata behavior are in
[IMAGE_PUBLISHING.md](IMAGE_PUBLISHING.md).

## 3. Bootstrap the persistent roles

Follow [OPERATIONS.md](OPERATIONS.md#2-scope-and-reconcile-the-openstack-foundation)
in order:

1. review and apply the non-deleting foundation plan;
2. generate PKI and the exact mode-`0600` admin/storage files;
3. boot admin and wait for its readiness marker;
4. bootstrap the management runtime and local bridge prerequisites before
   generating the bridge:

```bash
PLATFORM_AGE_COMMAND="$AGE_STORE/bin/age" \
deploy/platform-cli/bootstrap_management_runtime.sh
test "$(/srv/openstack-platform/runtime/python3.14 --version)" = 'Python 3.14.7'
test "$(/srv/openstack-platform/bin/uv --version)" = 'uv 0.12.2 (x86_64-unknown-linux-gnu)'
/srv/openstack-platform/bin/age --version >/dev/null
```

5. run `setup_management_bridge.py` as the management owner and verify the
   pinned alias with `ssh ... -- id -un` returning `agentops`;
6. run `bootstrap_acl.sh` on admin and transfer only the generated
   `nomad-tokens.env` copy needed for ingress;
7. transfer admin-local OpenStack/storage/provisioning inputs and the builder
   key using the exact paths in the operations procedure;
8. boot storage and wait for its readiness marker; and
9. boot ingress, configure external DNS/TLS/forwarding, and verify:

```bash
test "$(curl --fail --show-error --silent "https://$PLATFORM_DOMAIN/healthz")" = OK
```

`ACTIVE` without the exact readiness marker is a failed checkpoint. Do not
continue to the next role or print config-drive payloads.

## 4. Install the control surface and see the first verified result

On the management host, the exact Python 3.14.7/uv 0.12.2 runtime and
packaged age executable were bootstrapped before bridge generation. Install the
private inventory/policy, then install the matching management release and
helper release:

```bash
/srv/openstack-platform/runtime/python3.14 deploy/platform-cli/install_management_config.py \
  --platform "$PLATFORM_CONFIG" --policy "$PLATFORM_POLICY"
commit="$(git rev-parse HEAD)"
/srv/openstack-platform/runtime/python3.14 deploy/platform-cli/install_release.py \
  --mode management --source "$PWD" --commit "$commit" \
  --python /srv/openstack-platform/runtime/python3.14 --uv /srv/openstack-platform/bin/uv \
  --install-user-units --enable-backup-timer
test -x "$(readlink -e /srv/openstack-platform/bin/openstack-platform-restore)"
helper_output="$(deploy/platform-cli/deploy_helper_release.sh "$commit")"
printf '%s\n' "$helper_output"
test "$(tail -n1 <<<"$helper_output")" = "helper-release=$commit:verified"
```

The helper deployment and CLI must use the generated `platform-admin` bridge. The
first status invocation creates the empty management schema. This is the first verified
control-plane result:

```bash
status_output="$(/srv/openstack-platform/bin/openstack-platform status)"
printf '%s\n' "$status_output"
grep -Eq '^degraded +0 +0 +0 +0 +3 +0$' <<<"$status_output"
app_output="$(/srv/openstack-platform/bin/openstack-platform app list)"
storage_output="$(/srv/openstack-platform/bin/openstack-platform storage list)"
grep -Eq '^SLUG +RUNNING +COMMIT +DIGEST +CPU +MEMORY +LIVE$' <<<"$app_output"
grep -Eq '^SLUG +TYPE +PROVIDER_ID +PROVIDER_NAME +STATE +QUOTA +VERIFIED +HEALTH$' <<<"$storage_output"
```

Record zero accepted applications and zero managed resources. A fresh state
reports three unavailable observations for the persistent admin, ingress, and
storage hosts; builder observation is conditional on an unfinished build, and
worker observations begin with an accepted application. If rows exist that
this deployment did not create, stop; there is no import or adoption step.

Select the five accepted UUIDs recorded during image publication:

```bash
/srv/openstack-platform/bin/openstack-platform infra image set admin ADMIN_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra image set ingress INGRESS_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra image set storage STORAGE_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra image set builder BUILDER_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra image set worker WORKER_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra list
```

## 5. Deploy the application and storage

The public source must contain a supported `platform.yaml`, a full lowercase
40-character commit, and the matching lockfile. For Node, the relevant package
directory contains `package-lock.json`; for Bun it contains `bun.lock` or
`bun.lockb`. Script values are package-script names, not shell commands.

```bash
/srv/openstack-platform/bin/openstack-platform app deploy demo \
  --repo https://github.com/OWNER/REPOSITORY \
  --commit FULL_LOWERCASE_40_CHARACTER_COMMIT
/srv/openstack-platform/bin/openstack-platform app show demo
test "$(curl --fail --show-error --silent --output /dev/null \
  --write-out '%{http_code}' "https://demo.$PLATFORM_DOMAIN/HEALTH_PATH")" = 200
```

Success means builder cleanup, worker readiness, scheduler health, public
health, and an immutable digest are all recorded by `app show`. Only then
create managed storage:

```bash
/srv/openstack-platform/bin/openstack-platform storage create demo postgres
/srv/openstack-platform/bin/openstack-platform storage verify demo postgres
/srv/openstack-platform/bin/openstack-platform storage show demo postgres
```

The output contains non-secret provider identity and limits only. Credentials
stay in Nomad Variables and the helper. PostgreSQL/MongoDB measured-byte
values are targets, not collected usage.

## 6. Back up, restore-check, and clean up

The management-database backup runs on the management host and uses the policy recipient; managed-data
backup and restore verification run on admin. Do not run the latter scripts
from the checkout:

```bash
m1_backup_output="$(/srv/openstack-platform/bin/openstack-platform backup)"
printf '%s\n' "$m1_backup_output"
grep -Eq '^backup=platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age sha256=[0-9a-f]{64}$' <<<"$m1_backup_output"
managed_backup_output="$(
  ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- env \
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
  ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- env \
    PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
    AGE="$PLATFORM_ROOT/bin/age" \
    AGE_KEY="$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt" \
    "$PLATFORM_ROOT/infra/backup/verify_latest_restore.sh"
)"
printf '%s\n' "$restore_output"
grep -Eq '^latest platform restore=verified evidence=.+/RESTORE-MANIFEST$' <<<"$restore_output"
```

The expected managed-data result is `latest platform restore=verified` and a
mode-`0600` `RESTORE-MANIFEST`. Accepted backup files are under
`<paths.backups>/m1`; managed-data directories are under
`<paths.backups>/<namespace>`. The two backup classes use different custody
paths and must not be confused.

To test recovery, stop the management timer, copy an accepted encrypted
file to a direct mode-`0600` private path, and run the offline tool:

```bash
systemctl --user stop openstack-platform-backup.timer openstack-platform-backup.service
restore_output="$(
  /srv/openstack-platform/bin/openstack-platform-restore \
    /private/path/platform-YYYYMMDDTHHMMSSZ.sqlite3.age \
    --age-identity /private/path/backup-age-identity.txt --yes
)"
printf '%s\n' "$restore_output"
grep -Eq '^restore=verified schema-version=[0-9]+ integrity=ok$' <<<"$restore_output"
systemctl --user start openstack-platform-backup.timer
/srv/openstack-platform/bin/openstack-platform status
```

The installed `openstack-platform-restore` launcher fixes the managed destination
at `/srv/openstack-platform/state/platform.sqlite3`; it does not accept a
separate destination. Restore verifies age/SQLite/schema/integrity/foreign keys,
the deployment-bound project/namespace/inventory marker, and unfinished
operations in a temporary file, then atomically replaces the destination. A
backup from another deployment or an older unbound backup is refused. Refusals
leave the current database untouched. It contacts no provider and does not restore workers, Nomad
Variables, or managed data; compare live observations after restore and use
checkpointed CLI recovery, never manual SQLite/provider edits.

Remove storage before the application and preserve encrypted evidence and age
identity custody until the retention decision is recorded:

```bash
/srv/openstack-platform/bin/openstack-platform storage remove demo postgres --confirm demo
/srv/openstack-platform/bin/openstack-platform app remove demo --confirm demo
/srv/openstack-platform/bin/openstack-platform app list
/srv/openstack-platform/bin/openstack-platform storage list
```

For the full lifecycle, persistent-role upgrades, image pruning, and teardown,
continue with [OPERATIONS.md](OPERATIONS.md). Record each checkpoint in the
[acceptance checklist](ACCEPTANCE_CHECKLIST.md); do not put credentials,
provider payloads, or age identities in it.
