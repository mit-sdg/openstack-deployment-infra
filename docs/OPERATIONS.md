# Provision, operate, back up, restore, and remove a fresh deployment

For ordinary greenfield creation, use the automated [setup command](SETUP.md).
Use this expanded procedure to audit or recover an individual setup checkpoint,
or to operate a deployment after setup. When followed manually, it provisions a
new OpenStack project from end to end. It assumes a checkout at the reviewed
commit, an empty operator state directory, and no records to migrate. Commands run in three places:

- **Operator host / repository root** is the unprivileged owner of `/srv/openstack-platform`.
  It holds the checkout, private inventory, policy, OpenStack wrapper, and
  operator CLI.
- **Admin host** is the unprivileged `agentops` account reached only through
  the generated `platform-admin` SSH alias. It holds the Nomad/helper state,
  configured backup volume, and the packaged infrastructure scripts.
- **Storage and ingress bootstrap** runs from the operator host and sends a
  temporary config-drive payload to OpenStack. It does not copy a checkout to a
  guest.

Do not use root or `sudo` for management, bridge, release, or CLI commands.
Only resources named by the private inventory are in scope; never inspect,
change, stop, replace, or delete anything else in the OpenStack project.

## External inputs and safe notation

The following inputs cannot be supplied by this repository:

| Input | Where it is used | Custody and safe verification |
| --- | --- | --- |
| OpenStack authentication (`OS_AUTH_URL`, account/credential, domain, and the configured project) | foundation, image publication, and host lifecycle | Keep in a mode-`0600` environment file and a protected wrapper. Verify only the token project ID and project name; never print the credential. |
| Cloudflare Tunnel token, when `ENABLE_CLOUDFLARED=true` | ingress config-drive payload | Keep in a direct mode-`0600` one-token file. Verify the public hostname and HTTPS health route, never the token. An external ingress provider may be used with `false`. |
| Backup age identity | decrypting an offline controller SQLite backup | Keep the `AGE-SECRET-KEY-...` file outside Git and outside SQLite, in an offline/escrowed operator record. Verify file ownership and mode only. |
| Admin managed-data age identity | encrypting and verifying PostgreSQL, MongoDB, and Garage backups | Keep `/paths.root/persistent/secrets/backup-age-key.txt` on admin and escrow a protected copy before relying on the backups. Verify its mode without reading it. |

Replace `<PUBLIC_EXAMPLE>` with a real, non-secret configuration value.
Generate or obtain `<SECRET>` values privately; never commit, print, or paste
them into a ticket. The JSON examples are sanitized templates, not deployment
inputs.

## 1. Prepare the operator host and private inputs

Run the repository checks from a clean checkout. The locked project test
environment is Python 3.14 (`uv.lock` requires `==3.14.*`) with uv 0.12.2:

```bash
cd /path/to/checkout                 # replace this public path
uv --version                         # must be uv 0.12.2
uv sync --frozen
uv run python --version              # must report Python 3.14.x
uv run python -m unittest discover -s tests -v
```

The OpenStack SDK used by `apply_foundation.py` is a separate operator
prerequisite. Use an approved Python 3.14 interpreter that provides that SDK
for that script; do not use it as a substitute for the locked `uv` test
commands.

Create the private copies, then set the checkout variables for this shell:

```bash
cp -n config/platform.example.json config/platform.json
cp -n config/platform-policy.example.json config/platform-policy.json
chmod 0600 config/platform.json config/platform-policy.json
export PLATFORM_CONFIG="$PWD/config/platform.json"
export PLATFORM_POLICY="$PWD/config/platform-policy.json"
```

Edit both private JSON files. Replace every example project, UUID, network,
address, host, resource name, flavor, domain, path, runtime digest, and age
recipient. `platform.json` contains deployment identity and paths, not
credentials. `platform-policy.json` contains the public backup age recipient and
runtime image digests, not the private age identity or service credentials.
The project name and UUID must identify the same authenticated OpenStack
project. See [CONFIGURATION.md](CONFIGURATION.md) for the field contract.
Derive configuration-dependent shell values only after saving those edits:

```bash
export PLATFORM_NAMESPACE="$(uv run python infra/lib/platform_config.py get namespace)"
export PLATFORM_ROOT="$(uv run python infra/lib/platform_config.py get paths.root)"
export PLATFORM_DOMAIN="$(uv run python infra/lib/platform_config.py get domain)"
```

### Generate operator keys, age identity, and bootstrap files

Keep this directory outside the checkout. Existing institutional keys may be
used instead of generating new ones, but the files must meet the modes below:

```bash
export PRIVATE_BOOTSTRAP=/private/path/platform-bootstrap
umask 077
install -d -m 0700 "$PRIVATE_BOOTSTRAP"
ssh-keygen -q -t ed25519 -f "$PRIVATE_BOOTSTRAP/id_ed25519" -N ''
ssh-keygen -q -t ed25519 -f "$PRIVATE_BOOTSTRAP/builder_operator_ed25519" -N ''
chmod 0600 "$PRIVATE_BOOTSTRAP"/*_ed25519
chmod 0644 "$PRIVATE_BOOTSTRAP"/*_ed25519.pub
```

Use the first key as both `ADMIN_PUBLIC_KEY` and `OPERATOR_PUBLIC_KEY`. The
admin host's disposable-builder SSH client uses the second key. Both halves are
transferred to admin later: the public half to
`$PLATFORM_ROOT/secrets/builder_operator_ed25519.pub`, which is injected into
each builder, and the private half to
`$PLATFORM_ROOT/secrets/builder_operator_ed25519`, mode `0600`, which the
client passes to `ssh -i`.

Generate the backup age identity with the packaged Nix age derivation before
installing management. The identity file is private; only its recipient
belongs in the policy:

```bash
AGE_STORE="$(nix build --no-link --print-out-paths .#age)"
export BACKUP_AGE_IDENTITY="$PRIVATE_BOOTSTRAP/backup-age-identity.txt"
"$AGE_STORE/bin/age-keygen" -o "$BACKUP_AGE_IDENTITY" >/dev/null
chmod 0600 "$BACKUP_AGE_IDENTITY"
export CONTROLLER_BACKUP_AGE_RECIPIENT="$(awk '$1 == "#" && $2 == "public" && $3 == "key:" { print $4; exit }' "$BACKUP_AGE_IDENTITY")"
case "$CONTROLLER_BACKUP_AGE_RECIPIENT" in age1*) ;; *) exit 1 ;; esac
```

The final `case` checks readability; it does not display a secret. Put
`CONTROLLER_BACKUP_AGE_RECIPIENT` in the private policy's `backupAgeRecipient`, then verify
that the identity file remains outside Git. Do not copy the identity into
`config/`, `/srv/openstack-platform/state`, or the admin helper.

Create the exact role secret files. These are dotenv-like `KEY=value` files;
values must be non-empty, and each file must contain exactly the listed keys.
Do not quote values or add unrelated keys:

```bash
export ADMIN_SECRETS_FILE="$PRIVATE_BOOTSTRAP/admin-bootstrap.env"
export STORAGE_SECRETS_FILE="$PRIVATE_BOOTSTRAP/storage-bootstrap.env"

cat >"$ADMIN_SECRETS_FILE" <<EOF
NOMAD_GOSSIP_KEY=$(openssl rand -base64 32 | tr -d '\n')
EOF

cat >"$STORAGE_SECRETS_FILE" <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 32)
MONGO_PASSWORD=$(openssl rand -hex 32)
GARAGE_RPC_SECRET=$(openssl rand -hex 32)
GARAGE_ADMIN_TOKEN=$(openssl rand -hex 32)
GARAGE_METRICS_TOKEN=$(openssl rand -hex 32)
REGISTRY_HTTP_SECRET=$(openssl rand -hex 32)
REGISTRY_BUILDER_PASSWORD=$(openssl rand -hex 32)
REGISTRY_RUNTIME_PASSWORD=$(openssl rand -hex 32)
EOF
chmod 0600 "$ADMIN_SECRETS_FILE" "$STORAGE_SECRETS_FILE"
for file in "$ADMIN_SECRETS_FILE" "$STORAGE_SECRETS_FILE"; do
  test -f "$file" && test ! -L "$file" && test "$(stat -c '%a' "$file")" = 600
done
```

The builder and runtime registry passwords are used to create an htpasswd
file; their plaintext values are not put in the storage config-drive payload.
Do not use these generated values as application credentials.

Generate internal PKI once and retain the directory privately for future
replacement. The script creates a mode-`0700` directory, a mode-`0600` CA
private key and leaf private keys, and readable certificates:

```bash
export PKI_DIR="$PRIVATE_BOOTSTRAP/pki"
infra/pki/generate_internal_pki.sh "$PKI_DIR"
test "$(stat -c '%a' "$PKI_DIR")" = 700
```

The generated directory must contain the configured CA filename plus
`nomad-server.pem`, `nomad-server-key.pem`, `nomad-cli.pem`,
`nomad-cli-key.pem`, `nomad-ingress.pem`, `nomad-ingress-key.pem`,
`nomad-worker.pem`, `nomad-worker-key.pem`, `storage.pem`, and
`storage-key.pem`. The apply scripts reject symlinks and missing direct files.

Cloudflare is an external input. If using it, obtain a tunnel token from the
Cloudflare account, put exactly that token (one line, no whitespace) in a
private direct file, and set:

```bash
export CLOUDFLARE_TUNNEL_TOKEN_FILE="$PRIVATE_BOOTSTRAP/cloudflare-tunnel-token"
chmod 0600 "$CLOUDFLARE_TUNNEL_TOKEN_FILE"
test -f "$CLOUDFLARE_TUNNEL_TOKEN_FILE" && test ! -L "$CLOUDFLARE_TUNNEL_TOKEN_FILE"
```

Do not generate a fake token. If using an institutional or other provider,
use `export ENABLE_CLOUDFLARED=false` and satisfy the complete contract in
[PUBLIC_INGRESS.md](PUBLIC_INGRESS.md).

## 2. Scope and reconcile the OpenStack foundation

OpenStack credentials are required here but are not supplied by the
repository. On the operator host, provision a protected environment file
and a direct wrapper. The following is a **shape-only example**: replace every
`<PUBLIC_EXAMPLE>` or `<SECRET>` before use, and never commit either file:

```bash
export OPENSTACK_ENV=/srv/openstack-platform/.secrets/openstack.env
export OPENSTACK_WRAPPER=/srv/openstack-platform/bin/platform-openstack
umask 077
install -d -m 0700 /srv/openstack-platform/.secrets /srv/openstack-platform/bin
cat >"$OPENSTACK_ENV" <<'EOF'
OS_AUTH_URL='<PUBLIC_EXAMPLE_IDENTITY_V3_URL>'
OS_USERNAME='<PUBLIC_EXAMPLE_AUTOMATION_ACCOUNT>'
OS_PASSWORD='<SECRET_OPENSTACK_PASSWORD>'
OS_PROJECT_NAME='<PUBLIC_EXAMPLE_PROJECT_NAME>'
OS_PROJECT_ID='<PUBLIC_EXAMPLE_PROJECT_UUID>'
OS_AUTH_TYPE='password'
OS_USER_DOMAIN_NAME='Default'
OS_PROJECT_DOMAIN_NAME='Default'
OS_IDENTITY_API_VERSION='3'
OS_INTERFACE='public'
EOF
chmod 0600 "$OPENSTACK_ENV"
cat >"$OPENSTACK_WRAPPER" <<'EOF'
#!/bin/sh
set -eu
set -a
. "/srv/openstack-platform/.secrets/openstack.env"
set +a
exec openstack "$@"
EOF
chmod 0500 "$OPENSTACK_WRAPPER"
test -f "$OPENSTACK_WRAPPER" && test ! -L "$OPENSTACK_WRAPPER"
```

Replace each placeholder in this shape-only file with a shell-quoted value,
using a protected editor or secret manager. Do not put a password in a command
argument or unquoted assignment. Keep the file at mode `0600`; the quoted
wrapper argument list passes provider commands without exposing credentials.

Use the configured project identity for this shell without printing a token or
credential. Run this from the repository root; the supported helper accepts
both compact and canonical UUID representations from OpenStack:

```bash
set -a
. "$OPENSTACK_ENV"
set +a
export OS_PROJECT_NAME="$(uv run python infra/lib/platform_config.py get project)"
export OS_PROJECT_ID="$(uv run python infra/lib/platform_config.py get projectId)"
export OSC="$OPENSTACK_WRAPPER"
source infra/lib/platform-config.sh
load_platform_config
verify_openstack_project "$OSC"
```

The wrapper is the only provider command later used by the pinned bridge. Its
mode must be `0500` or `0700`, it must be owned by the operator user, and it
must not print credentials. The bridge also accepts an executable symlink only
when it resolves into a root-owned, non-writable `/nix/store` file; a mutable
symlink is refused.

From the repository root, first review the non-deleting plan:

```bash
python3 infra/openstack/apply_foundation.py
```

This command must run in the approved Python 3.14 environment containing the
OpenStack SDK, with `PLATFORM_CONFIG`, `OS_PROJECT_NAME`, and `OS_PROJECT_ID`
set as above. Review every security-group and port action. Apply only the
reviewed plan:

```bash
python3 infra/openstack/apply_foundation.py --apply
```

The script creates or updates only the configured foundation security groups
and fixed ports. It never deletes a resource. Verify the configured network,
fixed address, and security-group identity for all three ports without
inspecting unrelated servers:

```bash
network_id="$("$OSC" network show "$PLATFORM_NETWORK" -f value -c id)"
for role in admin ingress storage; do
  port="$(uv run python infra/lib/platform_config.py get "ports.$role")"
  address="$(uv run python infra/lib/platform_config.py get "addresses.$role")"
  security_group_id="$("$OSC" security group show "$PLATFORM_PREFIX-$role" -f value -c id)"
  test "$("$OSC" port show "$port" -f value -c name)" = "$port"
  test "$("$OSC" port show "$port" -f value -c network_id)" = "$network_id"
  fixed_ips="$("$OSC" port show "$port" -f value -c fixed_ips)"
  grep -Fq "$address" <<<"$fixed_ips"
  security_groups="$("$OSC" port show "$port" -f value -c security_group_ids)"
  grep -Fq "$security_group_id" <<<"$security_groups"
  printf 'foundation-port=%s:verified\n' "$role"
done
```

If a configured name is already owned by an unexpected resource, stop. Do not
rename, repurpose, detach, or delete it.

## 3. Boot admin, establish the pinned bridge, and bootstrap ACLs

Set the apply inputs on the operator host. The environment variables name
files; they do not contain secret values:

```bash
export ADMIN_PUBLIC_KEY="$PRIVATE_BOOTSTRAP/id_ed25519.pub"
export OPERATOR_PUBLIC_KEY="$PRIVATE_BOOTSTRAP/id_ed25519.pub"
export KEYPAIR_NAME="$(uv run python infra/lib/platform_config.py get prefix)-admin"
```

Boot admin first. The command renders a mode-`0600` temporary config-drive
payload and removes it on exit. It creates missing configured admin and backup
volumes, checks the size and type of existing ones, attaches them, and waits for
the exact serial-console readiness marker:

```bash
ADMIN_PUBLIC_KEY="$ADMIN_PUBLIC_KEY" \
OPERATOR_PUBLIC_KEY="$OPERATOR_PUBLIC_KEY" \
ADMIN_SECRETS_FILE="$ADMIN_SECRETS_FILE" \
PKI_DIR="$PKI_DIR" \
OSC="$OSC" \
infra/openstack/apply_admin.sh
```

`ACTIVE` is not readiness. Record the server/volume IDs and the marker
`<namespace> NixOS admin services ready` in private evidence. Verify that the
required volumes are attached to the configured server and have
`delete_on_termination=false`; do not print the secret-bearing config-drive
payload.

### Bootstrap the operator runtime before the bridge

Still on the operator host, from the repository root and as the unprivileged
`/srv/openstack-platform` owner, bootstrap the pinned Python, uv, age, protected OpenStack
wrapper, and local bridge prerequisites before generating the bridge. This
local preflight does not contact OpenStack or SSH; the bridge command below
performs the authenticated project, console-fingerprint, and key-scan checks.

```bash
PLATFORM_AGE_COMMAND="$AGE_STORE/bin/age" \
deploy/releases/bootstrap_operator_runtime.sh
test "$(/srv/openstack-platform/runtime/python3.14 --version)" = 'Python 3.14.7'
test "$(/srv/openstack-platform/bin/uv --version)" = 'uv 0.12.2 (x86_64-unknown-linux-gnu)'
/srv/openstack-platform/bin/age --version >/dev/null
```

The script creates or validates the operator SSH identity and runs the
bridge's `--preflight` checks for protected local executables and directories.
Do not continue with bridge generation if this command fails.

Each `apply_admin.sh`, `apply_storage.sh`, and `apply_ingress.sh` invocation is
fail-closed for an existing server. Before it waits for readiness, it verifies
the exact configured server UUID/name, image and flavor UUID/name, deployment
metadata, fixed port/address, attached volumes, and non-deleting volume flags.
A mismatch or ambiguous provider projection stops without applying user data or
reusing the host. Existing-host success means only that the verified host
passed the readiness wait; these scripts do not reapply cloud-init user data.

### Generate and smoke-test the operator bridge

Run this on the **operator host**, as the `/srv/openstack-platform` owner, after admin has
booted. The bridge generator obtains the authenticated project identity from
the protected wrapper, compares the admin console's ED25519 fingerprint with a
fresh `ssh-keyscan`, and atomically writes the known-hosts and SSH config
files. It does not accept a host, key, or SSH configuration override from the
CLI after generation.

```bash
export SSH_DIR=/srv/openstack-platform/.secrets/ssh
export SSH_IDENTITY="$SSH_DIR/id_ed25519"
export SSH_CONFIG="$SSH_DIR/config"
export KNOWN_HOSTS="$SSH_DIR/known_hosts"
install -d -m 0700 "$SSH_DIR"
install -m 0600 "$PRIVATE_BOOTSTRAP/id_ed25519" "$SSH_IDENTITY"
bridge_output="$(
  /srv/openstack-platform/runtime/python3.14 deploy/releases/setup_operator_bridge.py \
    --platform-config "$PLATFORM_CONFIG" \
    --ssh-identity "$SSH_IDENTITY" \
    --ssh-config "$SSH_CONFIG" \
    --known-hosts "$KNOWN_HOSTS" \
    --provider-command "$OPENSTACK_WRAPPER"
)"
test "$bridge_output" = operator-bridge=verified
```

The resulting `config` defines the `platform-admin` alias with user `agentops`
and the configured admin address. It sets `IdentitiesOnly yes`, allows only
ED25519 host keys, uses strict host-key checking, disables password and agent
forwarding, sets a ten-second connect timeout, and makes one connection
attempt. Verify ownership and modes, then exercise the alias without printing a
secret:

```bash
test "$(stat -c '%a' "$SSH_DIR")" = 700
test "$(stat -c '%a' "$SSH_CONFIG")" = 600
test "$(stat -c '%a' "$KNOWN_HOSTS")" = 600
PLATFORM_ADMIN_ADDRESS="$(uv run python infra/lib/platform_config.py get addresses.admin)"
bridge_options="$(ssh -F "$SSH_CONFIG" -G platform-admin)"
for expected in \
  "hostname $PLATFORM_ADMIN_ADDRESS" \
  'user agentops' \
  'identitiesonly yes' \
  'stricthostkeychecking true' \
  'hostkeyalgorithms ssh-ed25519' \
  'forwardagent no' \
  'connecttimeout 10' \
  'connectionattempts 1'; do
  grep -Fqx "$expected" <<<"$bridge_options"
done
test "$(ssh -F "$SSH_CONFIG" platform-admin -- id -un)" = agentops
test "$(ssh -F "$SSH_CONFIG" platform-admin -- id -u)" -gt 0
test "$(ssh -F "$SSH_CONFIG" platform-admin -- printf '%s\n' management-ssh=verified)" = management-ssh=verified
```

A console/keyscan mismatch is a stop condition. Do not accept a new key by
editing `known_hosts`; investigate the selected server and project identity.
The bridge is the sole SSH path used to install operator and helper releases.

Bootstrap Nomad ACLs on the **admin host** through that alias. The script is
already packaged in the admin image and writes the bootstrap response,
read-only Traefik policy, and controller/Traefik token file below the
configured root. It is idempotent and prints no token:

```bash
acl_output="$(
  ssh -F "$SSH_CONFIG" platform-admin -- env \
    PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
    "$PLATFORM_ROOT/infra/nomad/bootstrap_acl.sh"
)"
test "$acl_output" = nomad-acl-and-raft=healthy
```

The generated admin file is:

```text
$PLATFORM_ROOT/secrets/nomad-tokens.env
NOMAD_CONTROLLER_TOKEN=<SECRET>
NOMAD_TRAEFIK_TOKEN=<SECRET>
```

It must be a direct mode-`0600` file. Transfer it to the operator host only
for the ingress config-drive, using the pinned bridge; do not print it:

```bash
export NOMAD_TOKENS_FILE="$PRIVATE_BOOTSTRAP/nomad-tokens.env"
scp -F "$SSH_CONFIG" -- \
  "platform-admin:$PLATFORM_ROOT/secrets/nomad-tokens.env" \
  "$NOMAD_TOKENS_FILE"
chmod 0600 "$NOMAD_TOKENS_FILE"
test -f "$NOMAD_TOKENS_FILE" && test ! -L "$NOMAD_TOKENS_FILE"
test "$(grep -c '^NOMAD_.*_TOKEN=.' "$NOMAD_TOKENS_FILE")" = 2
```

The local copy is temporary; retain an approved private recovery copy only if
its custody is documented. The admin copy must remain for the helper and
Nomad operations.

## 4. Boot storage and ingress with exact transfers

The storage file and PKI files are sent in temporary config-drive payloads and
must be direct readable files. Boot storage from the operator host:

```bash
STORAGE_SECRETS_FILE="$STORAGE_SECRETS_FILE" \
OPERATOR_PUBLIC_KEY="$OPERATOR_PUBLIC_KEY" \
PKI_DIR="$PKI_DIR" \
OSC="$OSC" \
infra/openstack/apply_storage.sh
```

After the storage readiness marker
`<namespace> NixOS storage services ready`, transfer the private inputs that
admin-side lifecycle and helper scripts require. The storage host does not
receive the admin's OpenStack credentials; the admin host receives a separate
copy for its constrained tools:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- install -d -m 0700 \
  "$PLATFORM_ROOT/secrets" \
  "$PLATFORM_ROOT/persistent/secrets/provisioning-pki"
scp -F "$SSH_CONFIG" -- "$OPENSTACK_ENV" \
  "platform-admin:$PLATFORM_ROOT/secrets/openstack.env"
scp -F "$SSH_CONFIG" -- "$STORAGE_SECRETS_FILE" \
  "platform-admin:$PLATFORM_ROOT/secrets/storage-bootstrap.env"
scp -F "$SSH_CONFIG" -- "$PRIVATE_BOOTSTRAP/builder_operator_ed25519.pub" \
  "platform-admin:$PLATFORM_ROOT/secrets/builder_operator_ed25519.pub"
scp -F "$SSH_CONFIG" -- \
  "$PRIVATE_BOOTSTRAP/builder_operator_ed25519" \
  "platform-admin:$PLATFORM_ROOT/secrets/builder_operator_ed25519"
scp -F "$SSH_CONFIG" -- \
  "$PKI_DIR/$(uv run python infra/lib/platform_config.py get pki.internalCaFile)" \
  "$PKI_DIR/nomad-worker.pem" "$PKI_DIR/nomad-worker-key.pem" \
  "platform-admin:$PLATFORM_ROOT/persistent/secrets/provisioning-pki/"
ssh -F "$SSH_CONFIG" platform-admin -- chmod 0600 \
  "$PLATFORM_ROOT/secrets/openstack.env" \
  "$PLATFORM_ROOT/secrets/storage-bootstrap.env" \
  "$PLATFORM_ROOT/secrets/builder_operator_ed25519.pub" \
  "$PLATFORM_ROOT/secrets/builder_operator_ed25519" \
  "$PLATFORM_ROOT/persistent/secrets/provisioning-pki/nomad-worker-key.pem"
ssh -F "$SSH_CONFIG" platform-admin -- chmod 0644 \
  "$PLATFORM_ROOT/persistent/secrets/provisioning-pki/$(uv run python infra/lib/platform_config.py get pki.internalCaFile)" \
  "$PLATFORM_ROOT/persistent/secrets/provisioning-pki/nomad-worker.pem"
```

The admin-local `openstack.env` must contain the OpenStack credentials needed
by its packaged wrapper; it must also have the configured project identity
available to the wrapper. The admin-local storage file has the same eight keys
as the management `STORAGE_SECRETS_FILE`. The private builder key is used only
by admin-side disposable builder SSH. Verify only modes, direct-file status,
and the token project ID through the wrapper; do not `cat` any of these files.

`$PLATFORM_ROOT/secrets` is a symlink onto the admin state volume, so these
inputs survive admin replacement. Files in the admin home directory do not.
The platform reads the builder SSH key only when a deployment reaches its build
step, so a missing copy appears later as an authentication failure rather than
at replacement time.

All guest-side services and health checks load the guest inventory at
`/etc/$PLATFORM_NAMESPACE/platform.json`. Commands sent through the bridge set
`PLATFORM_CONFIG` to that path; the storage health checker uses a fixed Python
wrapper so the assignment is applied before its module imports configuration.
Do not substitute the management checkout's `PLATFORM_CONFIG` for a guest
health check.

Boot ingress only after the ACL token file exists. The default is Cloudflare;
set `ENABLE_CLOUDFLARED=false` and omit the token variable for another
provider:

```bash
ENABLE_CLOUDFLARED=true \
CLOUDFLARE_TUNNEL_TOKEN_FILE="$CLOUDFLARE_TUNNEL_TOKEN_FILE" \
NOMAD_TOKENS_FILE="$NOMAD_TOKENS_FILE" \
OPERATOR_PUBLIC_KEY="$OPERATOR_PUBLIC_KEY" \
PKI_DIR="$PKI_DIR" \
OSC="$OSC" \
infra/openstack/apply_ingress.sh
```

The script removes its temporary user-data file. Configure the external DNS,
TLS, and forwarding service to preserve the original `Host` header and forward
HTTP to ingress port 80. Then verify the first public result:

```bash
export PLATFORM_HOSTNAME="$PLATFORM_DOMAIN"
test "$(curl --fail --show-error --silent "https://$PLATFORM_HOSTNAME/healthz")" = OK
printf '\npublic-platform-health=verified\n'
```

The response body must be `OK`. This verifies DNS, certificate validation,
provider forwarding, host preservation, ingress, and Traefik's platform route.
The Cloudflare token, DNS account, and certificate are external inputs; this
repository does not create account-specific DNS records.

## 5. Install releases and verify empty state

### Verify the exact operator runtime and age executable

The operator runtime was bootstrapped before the bridge in step 3. On the
operator host, from the reviewed checkout and as the `/srv/openstack-platform` owner, verify
the stable paths before installing releases:

```bash
test "$(/srv/openstack-platform/runtime/python3.14 --version)" = 'Python 3.14.7'
test "$(/srv/openstack-platform/bin/uv --version)" = 'uv 0.12.2 (x86_64-unknown-linux-gnu)'
/srv/openstack-platform/bin/age --version >/dev/null
```

The expected versions are Python 3.14.7 and uv 0.12.2. Management backup
searches `/srv/openstack-platform/bin/age`, then `/usr/bin/age` and `/bin/age`, and refuses an
unowned or group/world-writable executable. The admin image separately
packages age and age-keygen at `$PLATFORM_ROOT/bin/age` and
`$PLATFORM_ROOT/bin/age-keygen`; managed-data scripts use those immutable
links, not a checkout copy.

Install the current inventory and policy atomically, then install the matching
operator release and user backup timer:

```bash
/srv/openstack-platform/runtime/python3.14 deploy/releases/install_operator_config.py \
  --platform "$PLATFORM_CONFIG" \
  --policy "$PLATFORM_POLICY"
commit="$(git rev-parse HEAD)"
/srv/openstack-platform/runtime/python3.14 deploy/releases/install_release.py \
  --mode operator \
  --source "$PWD" \
  --commit "$commit" \
  --python /srv/openstack-platform/runtime/python3.14 \
  --uv /srv/openstack-platform/bin/uv \
  --install-user-units \
  --enable-backup-timer
```

The release installer preflights the operator state and provider inputs: it
requires a clean full-commit checkout, direct owner-only configuration files,
a protected direct `/srv/openstack-platform/bin/platform-openstack`, frozen `uv.lock`, and a
successful sanitized entrypoint smoke test before selecting a release. The
helper deployment separately preflights the installed inventory and live admin
project/namespace/path identity before uploading anything. These checks are
automated; do not manually copy state into a release or bypass the protected
wrapper. Credentials never enter the release archive. Verify the selected
release and timer:

```bash
test "$(cat /srv/openstack-platform/operator-releases/current/.complete)" = "$commit"
test -x "$(readlink -e /srv/openstack-platform/bin/openstack-platform)"
test -x "$(readlink -e /srv/openstack-platform/bin/openstack-platform-restore)"
/srv/openstack-platform/bin/openstack-platform --help >/dev/null
/srv/openstack-platform/bin/openstack-platform-restore --help >/dev/null
systemctl --user is-enabled openstack-platform-backup.timer
```

### Install and smoke-test the helper release

From the same clean checkout, the helper deployment reads project identity and
all four configured paths from the installed mode-`0600` operator inventory.
It verifies the live admin inventory, transfers a commit-addressed archive and
installer through the pinned alias, atomically selects a complete helper
release, removes temporary remote files, and sends a malformed-envelope smoke
request:

```bash
helper_output="$(deploy/releases/deploy_helper_release.sh "$commit")"
printf '%s\n' "$helper_output"
test "$(tail -n1 <<<"$helper_output")" = "helper-release=$commit:verified"
```

A live admin `/etc/<namespace>/platform.json` may be the NixOS symlink to a root-owned,
non-writable regular file under `/nix/store`; never replace it with the
management copy.

The first CLI invocation creates the empty schema. Verify it before selecting
images:

```bash
status_output="$(/srv/openstack-platform/bin/openstack-platform status)"
printf '%s\n' "$status_output"
grep -Eq '^degraded +0 +0 +0 +0 +3 +0$' <<<"$status_output"
```

The columns are `STATE INFRA APPS STORAGE LIVE UNAVAILABLE UNHEALTHY`.
Record zero accepted applications and zero accepted managed resources. The
fresh state reports `degraded` with three unavailable observations for admin,
ingress, and storage until accepted image records are selected. The operator
CLI intentionally has no product list or mutation commands. If `APPS` or
`STORAGE` is nonzero, stop and preserve the database for controlled management
integration or recovery; do not install an older CLI or delete rows.

Select the five accepted image UUIDs from `infra image list`. The image names
are lookup labels; the CLI records and verifies the provider UUID, full source
commit, and compatibility metadata:

```bash
/srv/openstack-platform/bin/openstack-platform infra image list
/srv/openstack-platform/bin/openstack-platform infra image set admin ADMIN_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra image set ingress INGRESS_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra image set storage STORAGE_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra image set builder BUILDER_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra image set worker WORKER_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra list
```

Replace each `*_IMAGE_UUID` with the exact UUID from the accepted, active,
configured-project image record. Do not select an image with missing or
incompatible metadata.

## Product management is not yet an operator procedure

The product CLI and repository-owned deployment manifest have been retired.
Installing the policy and helper release causes admin to start the local
controller service, but the management and authentication applications are not
implemented. Do not invoke release-internal entry points or install an older CLI
to create applications or storage.

Future operators and UI implementers must use
[MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md) for the management/authentication
boundary and [CONTROL_PLANE_CONTRACT.md](CONTROL_PLANE_CONTRACT.md) for the
implemented local API. Until that integration is deployed, infrastructure and
backup verification are the supported completion boundary.

## 6. Back up, restore, and reconcile

### Hosted controller database on admin

The live controller database is
`<paths.adminState>/controller/state/platform.sqlite3`. Admin backs it up daily
with `<namespace>-hosted-controller-backup.timer`; this is separate from the
external operator-state backup described below. Verify the unit and create an
on-demand backup through the pinned alias:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl is-enabled "$PLATFORM_NAMESPACE-hosted-controller-backup.timer"
ssh -F "$SSH_CONFIG" platform-admin -- \
  systemctl start "$PLATFORM_NAMESPACE-hosted-controller-backup.service"
ssh -F "$SSH_CONFIG" platform-admin -- \
  journalctl -u "$PLATFORM_NAMESPACE-hosted-controller-backup.service" -n 5 --no-pager
```

A successful run reports
`hosted-controller-backup=hosted-controller-...sqlite3.age sha256=...` and
commits a ciphertext, `.sha256`, and final `.manifest` below
`<paths.backups>/hosted-controller/`. It uses SQLite's online backup API and the
policy `backupAgeRecipient`; plaintext temporary state is removed. The files
are mode `0640`, owned by the controller account and readable by `agentops`, so
the pinned alias can copy a committed set off-host. A backup is accepted only
when the manifest exists. Keep an independently stored ciphertext and evidence
set; loss of both admin volumes otherwise loses this recovery path.

Hosted restore is deliberately unavailable through the ordinary operator CLI.
It requires the approval-gated `ubuntu` recovery account and an offline
controller. First verify the manifest and ciphertext checksum, copy the
ciphertext off admin, and decrypt it on the operator recovery host with the
escrowed identity. Do not copy the age identity to admin. Copy the resulting
mode-`0600` SQLite file through the pinned alias to
`/home/agentops/hosted-controller-restore.sqlite3`.

In an approval-gated `ubuntu` session on the selected admin host, run:

```bash
sudo systemctl stop \
  "$PLATFORM_NAMESPACE-hosted-controller-backup.timer" \
  "$PLATFORM_NAMESPACE-hosted-controller-backup.service" \
  "$PLATFORM_NAMESPACE-controller.service"
sudo install -m 0600 -o platform-controller -g platform-controller \
  /home/agentops/hosted-controller-restore.sqlite3 \
  "$PLATFORM_ADMIN_STATE/controller/restore-input.sqlite3"
sudo rm -f /home/agentops/hosted-controller-restore.sqlite3
sudo openstack-platform-hosted-controller-restore --yes
sudo systemctl start \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-hosted-controller-backup.timer"
```

The fixed launcher refuses non-root use, an active controller/backup unit, or
an unsafe restore input. It runs as `platform-controller`, validates deployment
identity, known schema, SQLite integrity, foreign keys, and unfinished
operations, atomically replaces only the hosted database, fsyncs its directory,
and removes the staged plaintext after success. On refusal the current database
is unchanged and the input remains for diagnosis. After startup, require
`<namespace>-controller-readiness.service`, reconcile the management view, and
create a fresh hosted-controller backup. Restore does not recreate OpenStack,
Nomad, worker, or managed-data state.

### External operator-state database backup

Run this on the **operator host** as the unprivileged `/srv/openstack-platform` owner:

```bash
controller_backup_output="$(/srv/openstack-platform/bin/openstack-platform backup)"
printf '%s\n' "$controller_backup_output"
grep -Eq '^backup=platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age sha256=[0-9a-f]{64}$' <<<"$controller_backup_output"
```

The CLI creates a SQLite online-backup copy under the private operator state,
encrypts it with `backupAgeRecipient` using the verified operator age
executable, and transfers it through the pinned alias to the configured
admin-side path:

```text
<paths.backups>/controller/.staging/platform-YYYYMMDDTHHMMSSZ.sqlite3.age
```

The helper verifies the age-v1 header and ciphertext SHA-256, then publishes an
evidence set on the same backup filesystem. It fsyncs the ciphertext and
checksum before the final manifest rename. The manifest is the commit marker,
so readers and retention never accept a partial trio. A retry reconciles a
ciphertext or evidence move interrupted before that marker. The helper
preserves a malformed committed set for operator attention. Retention counts
only complete sets. The accepted paths are:

```text
<paths.backups>/controller/platform-YYYYMMDDTHHMMSSZ.sqlite3.age
<paths.backups>/controller/platform-YYYYMMDDTHHMMSSZ.sqlite3.age.sha256
<paths.backups>/controller/platform-YYYYMMDDTHHMMSSZ.sqlite3.age.manifest
```

`<paths.backups>` comes only from the installed inventory; it is not a fixed
`/srv/openstack-platform` or checkout path. The output contains the backup name
and ciphertext checksum, not database or credential content. The timer runs
this operation daily at 02:45 UTC with a 30-minute randomized delay. Verify the
timer and accepted evidence with private file metadata; do not list or print
credentials.

These backups contain only the external operator CLI database. They do not
contain the live admin-hosted controller database, PostgreSQL, MongoDB, Garage
objects, registry blobs, or an age identity. Registry blobs are rebuilt from
source.

### Managed-data backup and restore check on admin

These commands must run on the **admin host**, not from the management checkout
and not through `openstack-platform`. The admin image packages the scripts below
its configured root and supplies the immutable age, age-keygen, Podman, and
service-check dependencies:

```bash
# Run the following from the operator host through the pinned alias.
ssh -F "$SSH_CONFIG" platform-admin -- env \
  PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
  python3 "$PLATFORM_ROOT/infra/backup/init_garage_backup_key.py"
```

Run the initialization once after the storage host is healthy. It writes the
non-expiring Garage backup key to
`$PLATFORM_ROOT/secrets/garage-backup.env` with mode `0600`; it prints only
`garage-backup-key=created` or `garage-backup-key=existing`. The admin
`storage-bootstrap.env` must already be present and mode `0600`.

Create the admin managed-data age identity once, only when the file does not
already exist, and escrow it without printing it:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- \
  "$PLATFORM_ROOT/bin/age-keygen" \
  -o "$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt" >/dev/null
ssh -F "$SSH_CONFIG" platform-admin -- \
  chmod 0600 "$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt"
test "$(ssh -F "$SSH_CONFIG" platform-admin -- \
  stat -c '%a' "$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt")" = 600
```

Do not overwrite this identity while backups depend on it. Keep a protected
operator escrow copy outside the deployment; the live admin copy is required
for verification.

Run and verify the managed-data backup from admin through the same alias. The
explicit overrides below are the admin image's packaged paths; preserve them
when rerunning after a failed attempt and do not substitute checkout paths:

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

The first command creates encrypted `postgres.age`, `mongodb.age`, and
`garage.age` under `<paths.backups>/<namespace>/<timestamp>/`, plus `MANIFEST`
and `SHA256SUMS`. The admin role also has its own
`<namespace>-platform-backup.timer` for this managed-data job (03:15 UTC with a
30-minute randomized delay). The management
`openstack-platform-backup.timer` covers only the controller database. The
restore check decrypts each archive only into temporary storage. The second
command starts temporary PostgreSQL and MongoDB containers, checks the Garage
catalog/payload archive, and removes the temporary containers on success or
failure. It atomically writes mode-`0600` `RESTORE-MANIFEST` only when all
checks pass and never overwrites live services. The expected final line is
`latest platform restore=verified .../RESTORE-MANIFEST`.

### Offline external operator-state restore

The stable CLI has an offline restore operation for the external operator-state
database. Copy an accepted ciphertext from admin to a private mode-`0600` file on the operator host; do not use the
staging file while an upload is in progress:

```bash
export BACKUP_NAME=platform-YYYYMMDDTHHMMSSZ.sqlite3.age
export BACKUP_COPY="$PRIVATE_BOOTSTRAP/$BACKUP_NAME"
export BACKUP_ROOT="$(uv run python infra/lib/platform_config.py get paths.backups)"
scp -F "$SSH_CONFIG" -- \
  "platform-admin:$BACKUP_ROOT/controller/$BACKUP_NAME" "$BACKUP_COPY"
chmod 0600 "$BACKUP_COPY"
test -f "$BACKUP_COPY" && test ! -L "$BACKUP_COPY"
```

Stop the operator timer and any operator command before replacement. The
installed restore launcher fixes the managed destination at
`/srv/openstack-platform/state/platform.sqlite3`; do not supply a destination
or use a release-internal virtualenv path. Deliberately confirm replacement
with `--yes`, and assert the tool's verified result:

```bash
systemctl --user stop openstack-platform-backup.timer openstack-platform-backup.service
restore_output="$(
  /srv/openstack-platform/bin/openstack-platform-restore \
    "$BACKUP_COPY" \
    --age-identity "$BACKUP_AGE_IDENTITY" \
    --yes
)"
printf '%s\n' "$restore_output"
grep -Eq '^restore=verified schema-version=[0-9]+ integrity=ok$' <<<"$restore_output"
```

The operation is offline: it contacts no OpenStack, SSH helper, Nomad,
provider, or network service. It accepts an age-v1 ciphertext (or a private,
mode-`0600` SQLite file for controlled offline testing), decrypts to a private
temporary file, checks that its deployment-bound marker matches the installed
project/namespace/stable inventory identity, migrates only known older
schemas, checks schema shape, SQLite integrity, foreign keys, and unfinished
operations, then uses `os.replace` and a directory `fsync` for the destination.
A copied backup from another deployment or an older unbound backup is refused
before replacement. A failed verification removes only its temporary files and
leaves the existing database unchanged. Image/flavor/version upgrades do not
change the marker, but changing
stable resource names, paths, namespace, or project identity does.

It refuses a missing/unsafe identity, symlink or wrong-owner/mode source,
future, unknown, or corrupt state, a deployment-identity mismatch, an
unfinished operation in either source or current database, a busy database
lock, source equal to destination, unsafe state directory, or a file over the
configured 1 GiB limit. It also refuses unsafe WAL/SHM sidecars. Do not bypass
a refusal by deleting SQLite rows or sidecars.

Re-enable the timer and reconcile infrastructure plus aggregate accepted
counts with live observations:

```bash
systemctl --user start openstack-platform-backup.timer
/srv/openstack-platform/bin/openstack-platform status
/srv/openstack-platform/bin/openstack-platform infra list
```

Restore changes accepted SQLite state only. It does not recreate providers,
workers, Nomad jobs, variables, or managed data, and it never imports an
external row. The CLI cannot inspect or mutate restored product records. If
`APPS` or `STORAGE` is nonzero, or accepted infrastructure differs from live
identity, stop mutations and preserve the database, evidence, and operation
IDs for controlled controller/management recovery. Never edit SQLite, invoke
an obsolete product CLI, or change a provider by hand.

For a disposable offline destination rather than the live state path, use
the installed CLI with a separate private state directory. The directory is
the destination parent, so this command does not touch the managed database:

```bash
install -d -m 0700 /private/path/offline-state
/srv/openstack-platform/bin/openstack-platform \
  --state-directory /private/path/offline-state \
  restore "$BACKUP_COPY" \
  --age-identity "$BACKUP_AGE_IDENTITY" \
  --yes
```

The destination parent must be a direct current-user-owned mode-`0700`
directory. Inspect the result with that separate operator state directory; do not
point a running deployment at an unverified copy.

## 7. Upgrade safely

Publish and live-test a new commit-addressed role image before selecting it.
For a persistent role, select the new UUID and use the CLI replacement command;
it is the only supported persistent-host replacement path:

```bash
/srv/openstack-platform/bin/openstack-platform infra image set ingress NEW_INGRESS_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra replace ingress --yes
/srv/openstack-platform/bin/openstack-platform infra logs ingress --lines 200
```

Use `admin`, `ingress`, or `storage` as the role. The command retains the
current server, fixed port, and required volumes until the replacement passes
role readiness. Before that readiness can be accepted, it re-reads the
replacement and requires the exact selected image UUID, retained flavor UUID,
configured server name, and operation-provenance metadata. A healthy server
with the wrong image, flavor, name, or operation identity is rejected. On a
readiness failure it restores the prior host. An ambiguous provider result is
`recovery-required`: inspect the recorded operation and rerun the same command.
Never delete the old persistent server first, detach a volume by name, or create
a replacement manually. Run a fresh managed-data restore check before
replacing storage and a fresh controller-database backup before replacing admin.

Upgrade operator/helper releases with the installer from a clean full commit.
It atomically selects only a complete release; retain the previous complete
release for executable recovery. A release rollback does not restore SQLite or
provider state, and a database restore is the separate offline procedure above.

## 8. Clean up and preserve evidence

The operator CLI cannot remove applications or managed storage. If restored or
pre-cutover state has nonzero product counts, preserve it for the controlled
controller/management cutover; do not use an older binary or delete provider
resources manually.

Keep accepted encrypted backups, both age identities, and restore evidence
until the retention decision is recorded. Use image pruning only as a reviewed
plan/apply pair. The plan protects selected images, images referenced by
servers or unfinished operations, and the newest complete images per role;
incomplete metadata-bearing images are review-only. Apply re-observes exact
UUIDs, fingerprints, inventory, and protections under the infrastructure lock.
A missing or malformed server image projection fails closed rather than being
assumed to mean boot-from-volume. If a deletion is interrupted, inspect or
continue only through the recorded checkpointed recovery operation:

```bash
/srv/openstack-platform/bin/openstack-platform infra image prune
/srv/openstack-platform/bin/openstack-platform infra image prune --apply --yes
```

For whole-deployment teardown, first confirm through the deployed management
boundary that no product resources remain. In the current pre-management state,
nonzero product counts are a stop condition rather than permission to delete
provider resources manually. Copy and verify backups/private evidence, stop the
two platform backup timers, and obtain a separate reviewed provider teardown
for only the configured prefix and project. Do not use teardown as a
persistent-role replacement procedure. Remove temporary management copies of
ACL, storage, and Cloudflare files only after their required escrow/replacement
copies are confirmed; do not remove the admin copies while the deployment is
live.

Use [ACCEPTANCE_CHECKLIST.md](ACCEPTANCE_CHECKLIST.md) to record command output
paths, timestamps, image UUID/checksums, readiness markers, public health,
`RESTORE-MANIFEST`, and operation IDs. Keep credentials, unrestricted provider
payloads, and age identities out of tracked evidence.

## Recovery rules

- Project name or UUID mismatch: stop, load the intended OpenStack credential
  file and inventory, and rerun the non-mutating identity check.
- Unexpected foundation resource, port, volume, or host-key identity: stop and
  reconcile ownership; do not rename, detach, or delete it.
- `ACTIVE` without the exact readiness marker: inspect bounded serial output and
  failed units, then stop using that host as a recovery target. Correct the
  private input, build and live-test a replacement role image, and use the
  supported `openstack-platform infra replace ROLE --yes` path once the control
  surface is installed. For a pre-control-surface bootstrap failure, obtain a
  reviewed rebuild/replacement of only the configured role; rerunning an
  `apply_*` script does not reapply user data to an existing server.
- Missing/malformed bootstrap file: compare the exact role key set and mode
  `0600`; regenerate the file without printing its values.
- Helper, SSH, Nomad, provider, or storage dependency unavailable: restore the
  named dependency and rerun the same command with the same identity arguments.
- Unfinished or recovery-required infrastructure operation: preserve the
  database and correlation/operation ID, then rerun the same current CLI
  command. For a product operation, preserve state for controlled controller
  recovery; the operator CLI has no owning product command. Never clear SQLite
  operation rows manually.
- Public platform-health failure: test the exact DNS name, trusted certificate,
  preserved `Host`, provider origin, ingress route, and `/healthz` separately.
- Controller-database backup failure: inspect only safe file metadata and the fixed staging/
  accepted paths; verify `/srv/openstack-platform/bin/age`, the policy recipient, bridge, and
  backup volume, then emit a new backup. Do not copy a live WAL file.
- Managed restore failure: run the check again on admin, not management; keep
  the failed archive and temporary-container evidence, fix the packaged age
  key, storage service, or source archive, and do not overwrite live services.
- Offline restore refusal: leave the destination untouched, resolve the named
  mode/identity/schema/unfinished-operation condition, and rerun with the same
  private files.

For an unresolved failure, report only the safe phase, correlation/operation
ID, exact identity arguments, and bounded evidence. Do not report credentials,
provider payloads, or age identity contents.
