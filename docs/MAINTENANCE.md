# Release and platform maintenance

This runbook is for maintainers who prepare signed releases, build and publish
role images, install a release outside automated setup, or run protected live
acceptance. Deployment and ordinary recovery belong in [Deploy the
platform](DEPLOYMENT.md) and [Operations](OPERATIONS.md).

## Release evidence

Production setup and release installation require a signed component manifest
and a signed post-build role-artifact manifest. Verification happens before
setup creates local state or calls Nix/OpenStack and before an installer changes
a selected release.

Keep the Ed25519 signing key outside the repository. From a clean release
commit:

```sh
commit=$(git rev-parse HEAD)
python3 -m openstack_platform.release_manifest generate \
  --repository "$PWD" --commit "$commit" \
  --output /private/releases/"$commit" \
  --signing-key /private/release-signing-key.pem
openssl pkey -in /private/release-signing-key.pem -pubout \
  -out /private/release-trust-root.pem
python3 -m openstack_platform.release_manifest verify \
  --repository "$PWD" --commit "$commit" \
  --manifest /private/releases/"$commit"/release-manifest.json \
  --signature /private/releases/"$commit"/release-manifest.sig \
  --trust-root /private/release-trust-root.pem
```

The component manifest binds the full source commit, implementation contract,
`uv.lock`, deterministic wheel inputs, helper action manifest, controller API
and schema versions, and the explicit not-shipped UI placeholder. Its SPDX 2.3
SBOM describes the Python component set.

After building all five roles, prepare an `artifact-inputs.json` object keyed by
`admin`, `ingress`, `storage`, `worker`, and `builder`. Each entry contains the
QCOW2 path, output store path, `nix path-info --json --recursive` file, and exact
canonical Glance publication metadata. Generate and verify post-build evidence:

```sh
python3 -m openstack_platform.release_manifest artifact-generate \
  --component-manifest /private/releases/"$commit"/release-manifest.json \
  --inputs /private/releases/"$commit"/artifact-inputs.json \
  --output /private/releases/"$commit"/artifacts \
  --signing-key /private/release-signing-key.pem
python3 -m openstack_platform.release_manifest artifact-verify \
  --component-manifest /private/releases/"$commit"/release-manifest.json \
  --manifest /private/releases/"$commit"/artifacts/role-artifacts.json \
  --signature /private/releases/"$commit"/artifacts/role-artifacts.sig \
  --trust-root /private/release-trust-root.pem
```

The artifact manifest binds each QCOW2 hash/size, normalized Nix output and
recursive closure, component-manifest digest, and publication projection. Its
combined SBOM includes Python and unique Nix closure identities; provenance
names the concrete subjects.

For CI transport, package only the bounded signed evidence:

```sh
python3 -m openstack_platform.release_manifest bundle-create \
  --source /private/releases/"$commit" \
  --output /private/releases/"$commit"/release-evidence.tar
sha256sum /private/releases/"$commit"/release-evidence.tar
```

Publish the immutable tar over HTTPS. The protected environment receives its
URL, SHA-256, and public trust root through `RELEASE_EVIDENCE_URL`,
`RELEASE_EVIDENCE_SHA256`, and `RELEASE_TRUST_ROOT_PEM`. Manifests, SBOMs, and
provenance are artifacts, not CI secrets; the signing key never enters CI.

### Unsigned development evidence

Unsigned evidence is not a production fallback:

```sh
python3 -m openstack_platform.release_manifest generate \
  --repository "$PWD" --commit "$(git rev-parse HEAD)" \
  --output /tmp/platform-development-evidence \
  --unsigned-development
```

Verification requires `--allow-unsigned-development` or the exact setup value:

```text
PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT=I_UNDERSTAND_THIS_IS_NOT_PRODUCTION
```

The same acknowledgement is required for unsigned artifact evidence. Unsigned
verification is refused when `PLATFORM_ENVIRONMENT=production`.

## Build and test role images

The flake exposes:

```text
.#admin-image
.#ingress-image
.#storage-image
.#worker-image
.#builder-image
```

Use the private inventory outside the flake source:

```sh
export PLATFORM_CONFIG="$PWD/config/platform.json"
nix flake check --impure --no-build --print-build-logs
nix build --impure .#builder-image
```

The operator needs access to the Nix daemon. Do not use a rootful container
socket to bypass an unavailable approved build path.

Repository CI has three image-oriented layers:

1. `package-smoke` executes packaged Nomad, Traefik, BuildKit, age, OpenStack,
   Python, controller, and release-installer entry points.
2. `vm-admin`, `vm-ingress`, `vm-storage`, `vm-worker`, and `vm-builder` boot
   NixOS tests with test-only PKI and filesystems.
3. `tests/smoke_openstack_image.sh` boots each exact QCOW2 under QEMU with an
   OpenStack config drive and requires its serial completion marker.

Run one VM test locally with:

```sh
nix build --print-build-logs .#checks.x86_64-linux.vm-builder
```

A derivation, VM test, or QEMU smoke test creates a candidate, not an accepted
OpenStack image. It does not test Nova scheduling, Neutron rules, provider
networking, Glance upload, or all production role relationships.

Images contain packages, users, units, firewall policy, mounts, and non-secret
site configuration. They do not contain OpenStack credentials, operator keys,
Nomad secrets, registry/database/S3 credentials, tunnel tokens, or private PKI.
First-boot templates under `infra/cloud-init-nixos/` carry protected inputs on a
config drive. Lifecycle calls must enable config-drive use.

## Publish image candidates

The `CI` workflow publishes production candidates only when a protected `main`
push changes an image input and `OPENSTACK_PUBLISH_ENABLED=true`, or when an
authorized manual dispatch requests signed publication from `main`. Image
inputs are `flake.nix`, `flake.lock`, `nix/`, `infra/`, `openstack_platform/`,
`deploy/`, `pyproject.toml`, `uv.lock`, and `LICENSE`; Markdown-only changes are
excluded.

The protected matrix checks out one exact commit, installs private inventory,
derives a commit-suffixed name for all roles, builds each role once, verifies
signed artifact evidence, QEMU-boots that QCOW2, authenticates to the exact
project, and publishes the same file. The publisher verifies content,
closure/output identity, commit, metadata, owner/status, and provider checksum.
It waits for asynchronous uploads to become active. When Glance does not expose
a provider SHA-256, it downloads the accepted image and verifies SHA-256 before
reporting publication success. It never overwrites an existing image name.

For real-cloud testing before merge, manually dispatch the workflow against the
exact same-repository PR branch:

```sh
gh workflow run CI --ref '<open-pr-branch>' \
  -f publish=false \
  -f live_acceptance=false \
  -f development_publish=true
```

Development publication is refused on `main`, on a fork, when the branch is not
the exact head of one open PR targeting `main`, or without the protected
environment. Five preparation jobs build the roles in parallel and upload
compact hashes, sizes, and closure projections plus one exact QCOW2 artifact per
role. The QCOW2 artifacts use no additional compression and expire after one
day. An aggregation job downloads only compact evidence and emits the shared
unsigned manifest. Five dependent jobs each download one exact QCOW2, QEMU-boot, verify, and
publish it without rebuilding an independently varying QCOW2. Provider-facing
publication is limited to two concurrent jobs, and a missing Glance SHA-256
triggers up to three bounded download-verification attempts. Success requires
all five publish jobs and the
`development-role-evidence-<commit>` artifact. Never expose these
secrets through `pull_request_target` or trigger development publication
automatically.

Configure an `openstack-images` GitHub environment whose deployment policies
allow `main` and only the selected same-repository PR branch patterns. Store
`PLATFORM_CONFIG_JSON`, `DEVELOPMENT_PLATFORM_CONFIG_JSON`, `OS_AUTH_URL`,
`OS_USERNAME`, `OS_PASSWORD`, and `OS_PROJECT_ID` as environment secrets. The
development inventory must use an isolated namespace, prefix, addresses, and
volumes. Prefer a restricted Keystone application credential when the cloud
supports one. Require human review if publication must not be unattended,
protect `main` from direct/force push, and keep `.github/CODEOWNERS` on the
workflow/Nix/infrastructure boundary.

A reviewed manual publication uses the same four-argument contract:

```sh
export PLATFORM_CONFIG="$PWD/config/platform.json"
export OSC=/srv/openstack-platform/bin/platform-openstack
export SOURCE_COMMIT="$(git rev-parse HEAD)"
export PLATFORM_RELEASE_MANIFEST=/private/releases/$SOURCE_COMMIT/release-manifest.json
export PLATFORM_ARTIFACT_MANIFEST=/private/releases/$SOURCE_COMMIT/artifacts/role-artifacts.json
export PLATFORM_ARTIFACT_SIGNATURE=/private/releases/$SOURCE_COMMIT/artifacts/role-artifacts.sig
export PLATFORM_ARTIFACT_TRUST_ROOT=/private/release-trust-root.pem
# Export the exact manifest/QCOW2/closure/output values printed by verify-role.

infra/openstack/publish_nixos_image.sh \
  ROLE /path/to/role.qcow2 /nix/store/ROLE_OUTPUT /private/role.path-info.json
```

Record the Glance UUID, provider checksum, QCOW2 SHA-256, source commit, and
artifact-manifest identity. Publication does not select the image or replace a
host.

## Accept an image

Every candidate needs a disposable live test for its role. Common acceptance
requires exact project/image identity, config-drive completion, no failed
required unit, expected firewall/metadata behavior, and complete deletion of
disposable resources.

For a builder, prove rootless BuildKit readiness, pinned host-key SSH from admin,
metadata denial, one OCI build/push/delete, expiry timer, and deletion of both
server and port.

For a worker, prove config-drive completion, healthy Nomad/Docker identity, no
SSH, one constrained allocation, metadata denial, rejection of privileged
containers, ingress-only reachability, digest-pinned private-registry pull,
reboot recovery, and deletion of jobs/server/port.

For persistent roles, test the role's service and readiness contract in a
disposable scope. Before accepting storage replacement, require a fresh managed
restore check; before admin replacement, require fresh hosted-controller and
operator-state backups.

Select only the exact tested Glance UUID:

```sh
/srv/openstack-platform/bin/openstack-platform infra image set ROLE TESTED_IMAGE_UUID
```

For an existing persistent role, replace through:

```sh
/srv/openstack-platform/bin/openstack-platform infra replace ROLE --yes
/srv/openstack-platform/bin/openstack-platform infra logs ROLE --lines 200
```

Use only `admin`, `ingress`, or `storage` for replacement. Never rename a
candidate as promotion, rebuild it to remove a version suffix, delete the
current host first, or manually detach retained volumes.

## Install releases outside automated setup

Automated setup installs matching operator/helper releases. Use this section
only for a reviewed standalone release update or recovery.

Run as the unprivileged owner of `/srv/openstack-platform`. The checkout must be
clean and at the full released commit. Generate/verify release evidence first.
The admin host must already provide Python 3.14 and its helper runtime
libraries. The operator's `platform-openstack` wrapper and `platform-admin` SSH
alias are protected stable inputs, not release contents.

### Bootstrap the stable runtime once

```sh
deploy/releases/bootstrap_operator_runtime.sh
test "$(/srv/openstack-platform/runtime/python3.14 --version)" = 'Python 3.14.7'
test "$(/srv/openstack-platform/bin/uv --version)" = \
  'uv 0.12.2 (x86_64-unknown-linux-gnu)'
/srv/openstack-platform/bin/age --version >/dev/null
```

The script verifies exact downloads and refuses root/sudo. Later releases reuse
the stable runtime.

### Install inventory and policy

```sh
private_repo=/private/path/deployment-config
/srv/openstack-platform/runtime/python3.14 \
  deploy/releases/install_operator_config.py \
  --platform "$private_repo/config/platform.json" \
  --policy "$private_repo/config/platform-policy.json"
```

Inputs and installed files must be direct, owner-controlled, and private. The
command validates and atomically installs them without printing values.

### Generate the operator bridge

After admin is ready:

```sh
install -d -m 0700 /srv/openstack-platform/.secrets/ssh
install -m 0600 /private/path/id_ed25519 \
  /srv/openstack-platform/.secrets/ssh/id_ed25519
bridge_output=$(
  /srv/openstack-platform/runtime/python3.14 \
    deploy/releases/setup_operator_bridge.py \
    --platform-config /srv/openstack-platform/config/platform.json \
    --ssh-identity /srv/openstack-platform/.secrets/ssh/id_ed25519 \
    --ssh-config /srv/openstack-platform/.secrets/ssh/config \
    --known-hosts /srv/openstack-platform/.secrets/ssh/known_hosts \
    --provider-command /srv/openstack-platform/bin/platform-openstack
)
test "$bridge_output" = operator-bridge=verified
test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config \
  platform-admin -- id -un)" = agentops
```

The command binds the authenticated project, configured admin address, console
ED25519 fingerprint, scanned host key, unprivileged user, and fixed provider
wrapper. Do not hand-edit its output or accept changed host evidence.

### Install operator and helper

```sh
commit=$(git rev-parse HEAD)
/srv/openstack-platform/runtime/python3.14 deploy/releases/install_release.py \
  --mode operator \
  --source "$PWD" \
  --commit "$commit" \
  --release-manifest /private/releases/"$commit"/release-manifest.json \
  --release-signature /private/releases/"$commit"/release-manifest.sig \
  --release-trust-root /private/release-trust-root.pem \
  --python /srv/openstack-platform/runtime/python3.14 \
  --uv /srv/openstack-platform/bin/uv \
  --install-user-units \
  --enable-backup-timer

deploy/releases/deploy_helper_release.sh "$commit"
```

The installer verifies signature, component hashes, canonical commit archive,
runtime/lock, configuration, and entrypoint smoke tests before writing
`.complete` and atomically selecting `current`. Stable launchers under
`/srv/openstack-platform/bin` point only into complete releases.

The helper deployment rechecks operator and immutable guest inventory identity,
uploads a commit-addressed archive through the pinned alias, verifies its digest
and Git archive marker, checks the complete production action map, atomically
selects the helper, and requires a protocol-v1 `INVALID_REQUEST` smoke response.
It needs no remote Git, sudo, checksum utility, or preinstalled installer.

Verify selection and schedules:

```sh
test "$(cat /srv/openstack-platform/operator-releases/current/.complete)" = "$commit"
/srv/openstack-platform/bin/openstack-platform --help
/srv/openstack-platform/bin/openstack-platform-restore --help
systemctl --user is-enabled openstack-platform-backup.timer
```

A failed candidate leaves the prior complete release selected. Selecting a
prior executable does not restore database or provider state.

### Database migration order

Controller migrations are forward-only. Before selecting a release that raises
the schema version, stop mutation intake and take/verify controller backups.
Install matching operator/helper code, migrate once, start the controller, and
verify its API before replacing role images.

Executable rollback is safe only while the old controller accepts the current
schema. After an incompatible forward migration, stop the controller, restore
the pre-upgrade database offline, then select/start the old complete release.
Database restore and provider-state recovery remain separate.

## Disposable live acceptance

`openstack-platform-acceptance` performs a destructive, deployment-scoped cloud
drill. Use a protected CI environment or supervised release gate, never an
ordinary production project.

Before planning, require:

- a dedicated disposable project or exact ownership metadata on every mutation;
- a namespace of the form
  `acceptance-<label>-<first-eight-deployment-UUID-characters>`;
- reviewed protected driver configuration and replacement image UUIDs;
- a direct mode-`0600` HMAC key with at least 32 random bytes;
- a retained direct mode-`0700` state directory; and
- at most one run for that deployment UUID.

Create and review the non-mutating plan:

```bash
umask 077
export LIVE_ACCEPTANCE_DEPLOYMENT_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
export LIVE_ACCEPTANCE_NAMESPACE="acceptance-release-${LIVE_ACCEPTANCE_DEPLOYMENT_ID%%-*}"
install -d -m 0700 /private/live-acceptance-plan /private/live-acceptance-state

uv run openstack-platform-acceptance plan \
  --deployment-id "$LIVE_ACCEPTANCE_DEPLOYMENT_ID" \
  --project-id '<DISPOSABLE_OPENSTACK_PROJECT_UUID>' \
  --namespace "$LIVE_ACCEPTANCE_NAMESPACE" \
  --driver "$PWD/.venv/bin/openstack-platform-acceptance-driver" \
  --output /private/live-acceptance-plan/plan.json \
  --max-minutes 360 \
  --step-timeout-seconds 1800
```

The plan binds deployment/project/namespace, driver and protected-configuration
hashes, unrelated-resource baseline, ordered actions, and bounds. It performs
provider reads only and expires after 24 hours. Editing it invalidates review.

Apply or resume the exact plan only with all three opt-ins:

```bash
export LIVE_ACCEPTANCE_APPLY=1
uv run openstack-platform-acceptance run \
  --plan /private/live-acceptance-plan/plan.json \
  --driver "$PWD/.venv/bin/openstack-platform-acceptance-driver" \
  --state-directory /private/live-acceptance-state \
  --signing-key /private/live-acceptance-evidence-hmac.key \
  --apply \
  --confirm "LIVE-ACCEPTANCE:$LIVE_ACCEPTANCE_DEPLOYMENT_ID"
```

The drill proves greenfield setup, same-intent recovery after interruption, one
exact-commit application with PostgreSQL/MongoDB/S3 data, disable/enable,
restoration of both SQLite classes and managed data, persistent-host
replacement, external admin recovery, product deletion, exact owned-resource
cleanup, backup disposition, and an unchanged unrelated-resource fingerprint.
No check is inferred from command exit alone.

If interrupted, rerun with the same plan, driver, state directory, key, and
confirmation. Do not delete checkpoints or repair provider resources manually.

Verify retained evidence:

```bash
uv run openstack-platform-acceptance verify \
  --evidence-directory /private/live-acceptance-state \
  --signing-key /private/live-acceptance-evidence-hmac.key
```

The required result is
`live-acceptance-evidence=verified result=passed`. Retain the sanitized JSON,
SHA-256, HMAC, reviewed plan checksum, and reviewer identity in the private
release system. Keep the HMAC key separately.

The GitHub job additionally requires `LIVE_ACCEPTANCE_ENABLED=true`, a protected
`live-acceptance` environment, the three documented protected secrets, and a
self-hosted runner labeled `live-acceptance`. Normal push, pull-request,
schedule, and manual workflows do not execute the drill.

## Release sign-off

Before production use, retain private evidence that:

- static checks, unit tests, Nix evaluation, package smoke, all role VM tests,
  and all five QCOW2 boot tests pass at the exact commit;
- component and role-artifact signatures verify against the selected public
  trust root;
- every selected image has accepted role-live evidence and exact Glance UUID,
  commit, checksums, closure, and metadata;
- setup and public health pass in the intended project;
- hosted-controller, external operator-state, and managed-data backups are
  committed, restore-checked, and exported off site;
- a full-loss recovery drill has current evidence;
- persistent-host replacement has passed for release-relevant changes; and
- disposable live acceptance verifies when it is a release requirement.

Evidence records may contain bounded resource identities, hashes, readiness
results, timestamps, correlation IDs, and reviewer approval. They must not
contain credentials, provider payloads, age identities, secret values, or
backup contents.
