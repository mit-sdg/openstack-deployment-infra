# Install a platform CLI release without privileges

This procedure installs one committed Python 3.14 management release under
`/srv/openstack-platform` and the matching helper release in admin persistent
state. It is for release maintainers; it does not describe platform commands or
feature behavior.

Initialize a fresh database; do not copy a database or external declarations from another deployment.

## Prerequisites

- Run management commands as the unprivileged owner of `/srv/openstack-platform`.
- Use a clean checkout at the full commit being released.
- Bootstrap the exact Python 3.14.7 and uv 0.12.2 management runtime once; do
  not substitute the host's Python 3.12.
- The current admin must provide Python 3.14 and the helper runtime libraries.
  It does not need Git or another archive/checksum executable, a preinstalled
  helper installer, launcher, release directories, or a replacement image;
  deployment bootstraps those paths as the configured unprivileged admin
  operator.
- `/srv/openstack-platform/bin/platform-openstack` must be a direct, management-owner executable
  with mode `0500` or `0700`. It is the existing authenticated OpenStack
  wrapper; release installation never copies its credentials into an
  environment, archive, or virtual environment.
- `platform-admin` must be defined only by the direct readable file
  `/srv/openstack-platform/.secrets/ssh/config`. The deployment script hard-pins that
  path and does not accept a host, identity file, or SSH configuration override.

A helper candidate is accepted only when
`platform_cli/helper/actions-v1.txt` names the exact complete protocol-v1 action
map. The installer captures the handlers used by the production helper
entrypoint and requires an exact match, including backup, application, and
storage action families. It does not package the foundation-only default map.

The release path runs the state and provider preflights. Do not recreate them
by hand or upload a release first. Runtime bootstrap validates the protected
local executables, SSH identity, and bridge directories. Before archive
extraction, the management installer requires the direct private inventory and
policy plus the protected OpenStack wrapper. Before upload,
`deploy_helper_release.sh` reads the installed inventory and validates the live
admin identity and configured paths, including the accepted immutable Nix store
configuration link. It also confirms that the remote alias selects an
unprivileged account before invoking the helper installer. These checks do not
copy management configuration into `/etc`.

## Bootstrap the management runtime once

From a reviewed checkout on the management host, run as the unprivileged
`/srv/openstack-platform` owner:

```sh
deploy/platform-cli/bootstrap_management_runtime.sh
test "$(/srv/openstack-platform/runtime/python3.14 --version)" = 'Python 3.14.7'
test "$(/srv/openstack-platform/bin/uv --version)" = 'uv 0.12.2 (x86_64-unknown-linux-gnu)'
/srv/openstack-platform/bin/age --version >/dev/null
```

The bootstrap script downloads the exact x86-64 uv 0.12.2 archive, verifies its
checked-in SHA-256, and asks that pinned uv to install exact CPython 3.14.7 under
`/srv/openstack-platform/runtime`. It also validates a system-managed age executable, or the
Nix `.#age` output when `PLATFORM_AGE_COMMAND` is supplied, and atomically
links the verified executable as `/srv/openstack-platform/bin/age`. The CLI searches that
link before `/usr/bin/age` and `/bin/age`. It refuses an age executable that is
not a root/current-user-owned, non-writable regular file. It refuses root and
sudo. Subsequent releases reuse these stable paths and do not require access
to the Nix daemon.

## Install persistent management configuration

The management release never reads deployment configuration from its Git
archive. From a reviewed public checkout, install the inventory and private
policy supplied by the operator's private repository:

```sh
private_repo=/private/path/deployment-config
/srv/openstack-platform/runtime/python3.14 deploy/platform-cli/install_management_config.py \
  --platform "$private_repo/config/platform.json" \
  --policy "$private_repo/config/platform-policy.json"
```

The command refuses root and sudo, symlink inputs, inputs not owned by the
current user, group- or other-writable inputs, and malformed inventory or policy.
It installs direct mode-`0600` inventory under the direct, owner-only
`/srv/openstack-platform/config` directory and the policy at
`/srv/openstack-platform/state/policy.json`. Each file replacement is atomic.
The command prints only a fixed success message, not configuration values.
Keep `config/platform.json` and `config/platform-policy.json` outside the public
release repository; checked-in `*.example.json` files are not installed.

The policy must replace the example runtime-image digests and age recipient.
The inventory must contain the authenticated OpenStack project's real
`projectId`. Never use an example UUID, digest, or recipient in production.
A fresh deployment starts with no application declarations; applications are
created through the installed control surface.

## Generate the pinned management bridge

Run this on the management host, as the unprivileged `/srv/openstack-platform` owner, after
admin has booted and after the private inventory is installed. Do not hand-edit
`config` or accept a changed host key. The provider wrapper must be a
direct current-user-owned executable at mode `0500` or `0700`; its credential
file is outside the release. A mutable symlink is refused (a protected
root-owned, non-writable `/nix/store` executable is the only accepted symlink
form for the bridge helper).

```sh
install -d -m 0700 /srv/openstack-platform/.secrets/ssh
install -m 0600 /private/path/id_ed25519 /srv/openstack-platform/.secrets/ssh/id_ed25519
bridge_output="$(
  /srv/openstack-platform/runtime/python3.14 deploy/platform-cli/setup_management_bridge.py \
    --platform-config /srv/openstack-platform/config/platform.json \
    --ssh-identity /srv/openstack-platform/.secrets/ssh/id_ed25519 \
    --ssh-config /srv/openstack-platform/.secrets/ssh/config \
    --known-hosts /srv/openstack-platform/.secrets/ssh/known_hosts \
    --provider-command /srv/openstack-platform/bin/platform-openstack
)"
test "$bridge_output" = management-bridge=verified
```

The command verifies the token/project ID, project name, admin serial-console
ED25519 fingerprint, and an `ssh-keyscan` result before atomically writing the
mode-`0600` known-hosts and SSH config files below the mode-`0700` SSH
directory. Expected output is `management-bridge=verified`. Smoke-test the
actual transport and account:

```sh
test "$(stat -c '%a' /srv/openstack-platform/.secrets/ssh/config)" = 600
test "$(stat -c '%a' /srv/openstack-platform/.secrets/ssh/known_hosts)" = 600
test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- id -un)" = agentops
test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- id -u)" -gt 0
test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- \
  printf '%s\n' management-ssh=verified)" = management-ssh=verified
```

The generated alias pins user `agentops`, the configured admin address,
ED25519 host keys, strict host-key checking, no password or agent forwarding,
and bounded connection attempts. The management CLI, helper deployment, and
management-database backup transfer all use this alias and path.

## Install the management release

From the clean public checkout on the management host, after installing the
configuration and policy:

```sh
commit=$(git rev-parse HEAD)
/srv/openstack-platform/runtime/python3.14 deploy/platform-cli/install_release.py \
  --mode management \
  --source "$PWD" \
  --commit "$commit" \
  --python /srv/openstack-platform/runtime/python3.14 \
  --uv /srv/openstack-platform/bin/uv \
  --install-user-units \
  --enable-backup-timer
```

The installer refuses a non-HEAD commit, tracked changes, an archive without
Git's canonical global pax commit comment, a runtime other than Python 3.14, a
stale `uv.lock`, an entrypoint smoke failure, or missing persistent
configuration. It parses the archive marker with the Python standard library.
Installing an archive directly also requires its trusted lowercase SHA-256 via
`--archive-sha256`. The inventory and policy must be direct files owned by the
current user with mode `0600`; both configuration directories must be direct,
owned, and mode `0700`. It uses `uv sync --frozen --no-dev` to create the release-local
environment. Before writing `.complete`, it invokes the candidate's installed
command with sanitized temporary inventory and policy files and requires that
the command load them successfully. It then atomically replaces
`platform-cli/current` without writing to an immutable completed release.

The stable launchers are `/srv/openstack-platform/bin/openstack-platform`,
`/srv/openstack-platform/bin/openstack-platform-restore`, and
`/srv/openstack-platform/bin/openstack-platform-install-config`. They are atomic symlinks into the
selected complete release; callers must use these fixed paths rather than a
release-internal virtualenv or `current` path. The restore launcher fixes the
managed destination at `/srv/openstack-platform/state/platform.sqlite3` and
rejects destination/configuration overrides. The command launcher defaults to
`/srv/openstack-platform/config/platform.json` and the mode-`0600` policy under private state,
and verifies both files before each invocation. It also verifies and explicitly
selects the protected `/srv/openstack-platform/bin/platform-openstack` wrapper; it never assumes an
`openstack` command is on `PATH`, and it does not export credentials. Private
state and logs are under `/srv/openstack-platform/state` with mode `0700`.
The installed inventory binds the database's deployment marker to the
project, namespace, stable resource inventory, and state paths, so an offline
restore from another deployment is refused before replacement.

Verify the selected commit and timer rather than relying on installer exit
status alone:

```sh
test "$(cat /srv/openstack-platform/platform-cli/current/.complete)" = "$commit"
test -x "$(readlink -e /srv/openstack-platform/bin/openstack-platform)"
test -x "$(readlink -e /srv/openstack-platform/bin/openstack-platform-restore)"
/srv/openstack-platform/bin/openstack-platform --help
/srv/openstack-platform/bin/openstack-platform-restore --help
systemctl --user is-enabled openstack-platform-backup.timer
systemctl --user list-timers openstack-platform-backup.timer --no-pager
```

For later non-secret inventory or policy updates, rerun the installed
command against the private repository:

```sh
/srv/openstack-platform/bin/openstack-platform-install-config \
  --platform "$private_repo/config/platform.json" \
  --policy "$private_repo/config/platform-policy.json"
```

The timer explicitly supplies `/srv/openstack-platform/config/platform.json`, the stable state
directory, and the private policy path to the ordinary `platform backup`
command. That command derives the remote staging path from
`paths.backups` as `<paths.backups>/m1/.staging/<name>`; the helper accepts it
into `<paths.backups>/m1/<name>` and writes checksum/manifest evidence. It does
not copy a live WAL file and does not replace the existing PostgreSQL,
MongoDB, or Garage backup timer. Those managed-data checks run on admin with
the packaged age executable, as described in [OPERATIONS.md](OPERATIONS.md).

## Install the helper release

After the integrated action-map smoke check passes locally, run:

```sh
deploy/platform-cli/deploy_helper_release.sh "$(git rev-parse HEAD)"
```

The script reads the deployment identity and all four `paths` values from the
stable direct mode-`0600` management inventory at
`/srv/openstack-platform/config/platform.json` (or the explicit `PLATFORM_CONFIG` path),
rejecting unsafe path syntax. Before uploading anything, it checks the live
admin inventory at `/etc/<namespace>/platform.json`. A direct readable regular
file remains accepted. The NixOS form may instead be a symlink whose resolved
absolute target is a direct regular file under `/nix/store`, owned by root, not
group- or world-writable, and readable by `agentops`. A dangling link, mutable
or arbitrary target outside `/nix/store`, non-regular target, or target reached
by a chain that resolves outside `/nix/store` is rejected.

The parsed live identity must match the management inventory's project,
project ID, namespace, and complete `paths` object. The installer repeats the
file and identity checks before selecting a release. This prevents a valid but
unrelated image inventory from choosing helper state or data paths. The helper
release is under `<paths.adminState>/controller/platform-cli/releases/<commit>/`
and its stable launcher is `<paths.root>/bin/openstack-platform-helper`; the
management bridge invokes that fixed helper command through the pinned alias.
The helper launcher retains the stable `/etc/<namespace>/platform.json`
selection; when it is the accepted NixOS symlink, the launcher rechecks and
resolves it to the direct store file before starting the strict configuration
loader. Management-provided authenticated health checks propagate the same
`PLATFORM_CONFIG=/etc/<namespace>/platform.json`; the storage check uses a
fixed wrapper to set it before importing its script.

After preflight, the script creates the configured admin release, private
incoming, releases, and `root/bin` directories through the pinned alias. It
then creates the exact Git archive, computes its SHA-256 locally, and extracts
the installer from the same commit. The script uploads the archive and
installer with `scp`, passes the trusted checksum separately, and invokes
`/run/current-system/sw/bin/python3.14` directly. The installer verifies the
full archive digest and then requires Git's canonical
40-lowercase-hex global pax commit comment to equal the requested commit before
extraction. No remote `git`, checksum utility, sudo command, preinstalled
installer, helper launcher, or image replacement is required.

The installer extracts below
`<paths.adminState>/controller/platform-cli/releases/<commit>/`, captures and
checks the production action map, writes `.complete`, atomically changes
`current`, and atomically selects the launcher below `<paths.root>/bin`. The
script finally invokes that configured launcher with a malformed envelope and
requires the protocol-v1 `INVALID_REQUEST` response. Temporary incoming files
are removed on both success and failure.

## Recover before acceptance

A failure before `.complete` leaves `current` unchanged and removes the
candidate staging directory. A transferred archive is removed when the remote
installer exits. An already complete release is smoke-tested again before it
can be selected.

To recover to a previously installed complete management release, select it
with an atomic symlink replacement and rerun the entrypoint check. This changes
the executable release only; it does not migrate or restore platform database
state:

```sh
old=FULL_PREVIOUS_COMMIT
cd /srv/openstack-platform/platform-cli
ln -s "releases/$old" .current.rollback
mv -Tf .current.rollback current
/srv/openstack-platform/bin/openstack-platform --help
```

Use the corresponding persistent release root on admin for helper rollback.
Do not select a directory without a matching `.complete` file.

## Diagnose blocked installation

- **`release runtime must be Python 3.14`**: rerun the unprivileged runtime
  bootstrap and pass `/srv/openstack-platform/runtime/python3.14`; do not use
  `/usr/bin/python3`.
- **`tracked source changes must be committed`**: commit the intended release
  or restore the checkout. The installer never packages working-tree changes.
- **`release artifact SHA-256 does not match`**: discard the incoming artifact
  and rerun the deployment from the clean checkout; do not recompute a checksum
  on admin or bypass the local checksum supplied by the deployment script.
- **`release artifact is not a commit-addressed git archive`**: rebuild with
  the checked-in deployment script. Plain tar files and noncanonical fabricated
  pax metadata are not release artifacts.
- **`protected OpenStack command is missing`**: restore the direct,
  management-owner `/srv/openstack-platform/bin/platform-openstack` wrapper at mode `0500` or
  `0700`; do not put credentials in the release or substitute an unauthenticated
  command from `PATH`.
- **`stable management platform config ...`**: install the direct mode-`0600`
  inventory first and verify all four configured paths; do not create
  deployment-specific path constants in the helper script.
- **`live admin platform config symlink target ...`**: restore the
  root-generated `/etc/<namespace>/platform.json` link to its immutable,
  root-owned `/nix/store` regular file. Do not point it at a copied file under
  `/tmp`, `/srv`, a home directory, or an intermediate link that resolves to
  mutable storage, and do not relax store-file permissions to make it readable.
- **`does not match management project, namespace, and paths`**: rebuild or
  activate the admin NixOS configuration from the same reviewed inventory used
  by management. Do not deploy against an image with another project's identity
  or helper paths.
- **`integration-owned helper action manifest is missing`**: helper composition
  is not integrated. Do not add a foundation-only manifest to bypass the gate.
- **`production helper action map does not exactly match`**: update composition
  or the integration-owned manifest so they describe one complete protocol-v1
  release, then rerun tests and installation.
- **`install persistent management configuration before installing a release`**:
  run the configuration and policy steps above as the same unprivileged account;
  do not copy a real deployment file into the release checkout.
- **`management configuration ownership or mode is invalid`**: replace symlinks
  with direct regular files owned by the management account and install them at
  mode `0600`; do not relax the launcher check.
- **The user timer cannot connect to systemd**: verify the management account's
  user manager and linger state, then rerun with `--enable-backup-timer`. Do not
  install this timer as root merely to bypass a missing user session.
