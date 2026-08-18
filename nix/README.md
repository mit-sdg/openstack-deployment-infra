# Build and validate NixOS role images

Use this procedure to build OpenStack QCOW2 images without putting cloud credentials or secrets in the Nix store. It covers the role outputs in this repository and the private deployment inventory in `config/platform.json`.

A successful derivation produces a candidate, not an accepted image. Before selecting its exact provider UUID, every candidate must pass a disposable live test for its role.

## Image outputs

```text
.#admin-image
.#ingress-image
.#storage-image
.#worker-image
.#builder-image
```

Build one role:

```sh
PLATFORM_CONFIG="$PWD/config/platform.json" nix build --impure .#builder-image
```

Run static checks before publishing. The Python commands use Python 3.14
from the locked uv 0.12.2 environment:

```sh
PLATFORM_CONFIG="$PWD/config/platform.json" nix flake check --impure --no-build
nix fmt -- .
git diff --exit-code -- '*.nix'
uv --version                       # uv 0.12.2
uv sync --frozen
uv run python --version            # Python 3.14.x
uv run python -m compileall -q infra tests
uv run python -m unittest discover -s tests -v
uv run ruff format --check platform_cli deploy/platform-cli infra tests
uv run ruff check platform_cli deploy/platform-cli infra tests
uv run mypy
find infra -type f \( -name '*.sh' -o -path 'infra/control/bin/*' \) \
  -exec bash -n {} +
```

If the unprivileged operator cannot access the Nix daemon socket on the management host, a build there requires the deployment's approved privileged-build procedure. Do not use a rootful container socket as a workaround.

## Automated package, VM, and image tests

Every pull request and `main` build runs three runtime-oriented layers in
addition to evaluation:

1. `package-smoke` executes the packaged Nomad, Traefik, BuildKit daemon,
   BuildKit client, and BuildKit runtime binaries.
2. `vm-<role>` boots each role with `runNixOSTest`. Test-only PKI, local
   filesystems, and service configuration replace deployment secrets and Cinder
   volumes. The checks exercise the role's primary local services, including a
   rootless BuildKit worker, Nomad and Docker on a worker, the admin Nomad
   server, Traefik health, and storage Nginx configuration.
3. After each OpenStack QCOW2 is built, `tests/smoke_openstack_image.sh` boots
   that exact file under QEMU with an OpenStack config drive and requires
   cloud-init to emit a serial-console completion marker. The protected
   build-and-publish path runs the same smoke test before contacting OpenStack.

Run one VM test locally on a Linux host with Nix and QEMU:

```sh
nix build --print-build-logs .#checks.x86_64-linux.vm-builder
```

Run the packaged-binary check with:

```sh
nix build --print-build-logs .#checks.x86_64-linux.package-smoke
```

KVM is used when `/dev/kvm` is available; QEMU can fall back to software
emulation. These tests do not emulate Nova scheduling, Neutron security groups,
Glance upload behavior, provider networking, or the production service
relationships. A candidate therefore still requires the disposable live tests
and, for persistent roles, the replacement safeguards below.

## Image contents and provisioning boundary

Images contain role packages, users, service units, firewall policy, mounts, and non-secret site configuration. They do not contain:

- OpenStack credentials;
- authorized operator keys;
- Nomad tokens or gossip keys;
- registry, database, or object-storage credentials;
- Cloudflare tokens; or
- private PKI material.

Role-specific config-drive templates under `infra/cloud-init-nixos/` provide required first-boot material. OpenStack lifecycle commands must use `--use-config-drive`; passing user data without enabling a config drive results in cloud-init falling back to `DataSourceNone`.

Common image requirements include Nova serial output on `ttyS0`, config-drive cloud-init, and disabled unbounded OpenStack metadata fetching. Downloaded generic ELF binaries must be patched for the Nix store and run as an install check during the package build.

## Build and publish a candidate

Use a versioned candidate name in `config/platform.json`, such as `example-nixos-worker-v2`. Build the corresponding output and identify the exact QCOW2 file to publish.

Publish with a full source commit in the environment. The script refuses a
missing/partial commit and refuses to overwrite an existing image name:

```sh
uv sync --frozen
export PLATFORM_CONFIG="$PWD/config/platform.json"
export PATH="$PWD/.venv/bin:$PATH"
export OSC=/srv/openstack-platform/bin/platform-openstack
export SOURCE_COMMIT="$(git rev-parse HEAD)"
sha256sum /path/to/worker.qcow2
SOURCE_COMMIT="$SOURCE_COMMIT" OSC="$OSC" \
  infra/openstack/publish_nixos_image.sh worker /path/to/worker.qcow2
```

Repeat the same command for `admin`, `ingress`, `storage`, and `builder` with
the corresponding QCOW2 files. The publisher verifies the configured project,
records the full source commit and complete metadata projection, verifies
owner/status/checksum evidence, and refuses to overwrite an image with the
configured name. It compares the local OpenStack-compatible MD5 with Glance's
returned `checksum` field before reporting success. Record the provider
checksum, candidate’s SHA-256, Glance ID, size, build status, source commit,
and log path.

The optional CI publication path enforces the same no-overwrite boundary. For a `main` push that changes `flake.nix`, `flake.lock`, or anything under `nix/` or `infra/`, its protected matrix appends the first eight characters of the commit SHA to every configured base image name, builds once with the private inventory, and publishes that same QCOW2 while recording the full SHA as image metadata. The `infra/` tree is included because it is embedded in the admin image and referenced by the storage image. The workflow skips names that already identify the same commit, rejects suffix collisions, and keeps credentials isolated in the `openstack-images` GitHub environment. Configure it through [`docs/IMAGE_PUBLISHING.md`](../docs/IMAGE_PUBLISHING.md). Automatic upload is not role acceptance or role selection.

Nix’s disk-image helper may print repeated `virtiofs` directory traversal messages even when it succeeds. Neither those messages nor a zero exit status is live-boot evidence. Top-level Nix failures remain fatal; the disposable role test is authoritative.

## Test a builder candidate

Accept a builder only after every check below passes:

1. OpenStack reports `ACTIVE` and a nonempty Nova serial console.
2. Config-drive cloud-init reaches its final marker.
3. The dedicated readiness marker confirms rootless BuildKit is active and its socket exists.
4. Before the first SSH connection, the ED25519 host fingerprint read from the console matches a scan from admin.
5. SSH access succeeds only for the dedicated unprivileged operator connecting from admin.
6. No system units have failed.
7. Cloud metadata access is denied.
8. BuildKit reports a worker and completes a rootless OCI build.
9. Registry TLS and builder authentication work.
10. A test manifest can be pushed by digest and removed.
11. The expiry timer is active.
12. Lifecycle deletion removes both the server and its fixed port.

The BuildKit unit needs a restricted `PATH` containing the rootless user-namespace wrappers, BuildKit binaries, `slirp4netns`, `fuse-overlayfs`, `iproute2`, iptables, and util-linux. `/run/wrappers/bin` must precede ordinary package binaries so `newuidmap` and `newgidmap` resolve to NixOS wrappers.

## Test a worker candidate

Workers intentionally have no SSH. Test them through the trusted serial console, Nomad, and disposable workloads:

1. The config drive is present and the cloud-init marker appears.
2. Nomad configuration passes validation before the agent starts.
3. The lifecycle reports a `ready` node with the exact project UUID, slug, class, managed-by metadata, node class, and a detected healthy Docker driver.
4. No unexpected halt or required-service failure appears on the serial console.
5. TCP/22 is unavailable.
6. A constrained container allocation succeeds.
7. A workload cannot reach cloud metadata.
8. A privileged-container request is rejected for the expected reason.
9. A workload on port 8080 is reachable from ingress and not exposed to unrelated sources.
10. A private-registry image is pulled by immutable digest with runtime credentials.
11. A hard reboot returns the same node to `ready` with no failed or unexpected units.
12. Test jobs, manifests, server, and fixed port are deleted.

Set Nomad `cni_path` to a directory containing only CNI plugin executables, currently `/etc/cni/bin` backed by `pkgs.cni-plugins/bin`. Never point it at `/run/current-system/sw/bin`: Nomad probes each entry as a plugin and can execute unrelated system commands.

Worker readiness ignores stale Nomad nodes in the `down` state that have the same name. Server-side Nomad garbage collection may temporarily retain a record of a deleted worker; that record is not live compute.

## Select the tested image by exact identity

There is no image rename or manual promotion step. After the candidate passes
its role checks, retain its exact Glance UUID and checksums, select that UUID
in the fresh control database, and use the supported persistent replacement
operation when a deployed role must change:

```sh
/srv/openstack-platform/bin/openstack-platform infra image set ROLE TESTED_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra replace ROLE --yes
```

Use `admin`, `ingress`, or `storage` for `ROLE`. Do not rebuild merely to
remove a version suffix, rename a candidate, delete the current role first, or
change the configured image name to make a candidate canonical; the tested
provider identity is the deployment input.

## Clean up a failed test

Delete only the disposable server and its fixed port through the matching lifecycle. Purge temporary Nomad jobs and test registry manifests. Treat any snapshot or disk downloaded for offline diagnosis as secret-bearing because it may contain cloud-init material. Keep it mode 0600 in a private directory, then delete both the local data and the Glance snapshot immediately after diagnosis.

Retain build logs and the local candidate artifact until a replacement is accepted. Remove only failed or obsolete Glance candidates through the reviewed image-prune plan so they cannot be selected accidentally.

## Replace a persistent host through the CLI

NixOS configurations mount existing filesystems by label and never format
persistent data volumes. Stable labels and mounts are defined in
`config/platform.json` and the role modules. The staff CLI is the sole safe
persistent-host replacement path; do not delete a server first, detach a volume
manually, or recreate the host with provider commands.

After publishing and live-testing a new role image, select its exact UUID and
run one replacement at a time from the management host:

```sh
/srv/openstack-platform/bin/openstack-platform infra image set ingress NEW_INGRESS_IMAGE_UUID
/srv/openstack-platform/bin/openstack-platform infra replace ingress --yes
/srv/openstack-platform/bin/openstack-platform infra logs ingress --lines 200
```

Use `admin`, `ingress`, or `storage` as the role. Before replacing storage,
complete a fresh managed-data restore check; before replacing admin, complete a
fresh M1 backup. The command retains the old server, fixed port, and volumes
until readiness passes, then removes only the retained old host. If readiness
fails it restores the prior host. An ambiguous provider result is
`recovery-required`; rerun the same CLI operation after restoring its named
dependency. Keep the tested image identity and private backup evidence.
