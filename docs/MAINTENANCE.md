# Release and platform maintenance

This runbook is for maintainers who prepare signed releases, build and publish
role images, install a release outside automated setup, or run protected live
acceptance. Deployment and ordinary recovery belong in [Deploy the
platform](DEPLOYMENT.md) and [Operations](OPERATIONS.md).

## Release evidence

By default, production setup and release installation require a signed component
manifest and a signed post-build role-artifact manifest. The temporary
[unsigned-production exception](#temporarily-disable-production-signing) requires
its own explicit acknowledgement; development evidence is never a production
fallback. Verification happens before
setup creates local state or calls Nix/OpenStack and before an installer changes
a selected release.

Keep the Ed25519 signing key outside the repository **and GitHub**. From a clean release
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
`uv.lock`, deterministic wheel inputs (packaged Python, text, and dashboard
browser assets), helper action manifest, controller API and schema versions, and
the owner UI source/compatibility identity. Its SPDX 2.3 SBOM describes locked
Python/npm dependencies and source identities for local frontend workspace packages.

The operator dashboard is a separate React app using the source-only shared UI
package. Its generated HTML/JS/CSS/SVG output is committed under
`openstack_platform/dashboard/static/`; Git is the durable store. These files are
already included in the operator source-input hash and source-built wheel. No
new installer flags, artifact formats or setup inputs are needed, and no Node
toolchain is installed on the operator host. Signed and explicitly unsigned
production releases keep the existing evidence verification policy unchanged.

Before signing or publishing a release containing frontend changes, require the
`dashboard-frontend` freshness gate: exact Node 24.19.0/npm 11.17.0, locked root
workspace install, all workspace checks, production rebuild, full committed-file
comparison including untracked output, and browser checks under the unchanged
dashboard CSP. The job also blocks image publication. A source hash authenticates
the committed bundle bytes; the required rebuild gate establishes their freshness
against the frontend/shared sources. Review both source and generated output.

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

Development evidence is not a production fallback:

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

The same development acknowledgement is required for development artifact evidence.
Development verification is refused when `PLATFORM_ENVIRONMENT=production`, even
if the separate unsigned-production exception is enabled. The admin installer
sets `PLATFORM_MANAGEMENT_ENVIRONMENT=production`; only broker/web modes apply
that guard to verification. Login sessions and helper/operator modes retain the
existing environment policy, including explicit unsigned-development helper
deployment over the existing bridge. An explicitly supplied global
`PLATFORM_ENVIRONMENT=production` still has its original meaning for every mode.
Local portal development installs require direct paths under the worktree's
real `.tmp` directory; symlinked `.tmp` or state paths are refused.

## Build and test role images

Before changing an existing admin image, complete the mandatory
[root-path preflight](#preflight-controller-paths-before-an-admin-image-upgrade).
The controller requires controller-prepare; a refused path can prevent controller
startup after replacement. Successful evaluation or image building does not
establish that the live path metadata is accepted.

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

Relevant `main` pushes start five independent `ubuntu-latest` matrix runners,
one each for `admin`, `ingress`, `storage`, `worker`, and `builder`, with
`max-parallel: 5` and `fail-fast: false`. They do not wait for signing evidence
or serialize builds behind OpenStack authentication. An authorized manual
`publish=true` dispatch from `main` uses the same pipeline. Image inputs are
`flake.nix`, `flake.lock`, `config/platform.example.json`, `nix/`, `infra/`,
`openstack_platform/`, `deploy/`, `pyproject.toml`, `uv.lock`, and `LICENSE`;
Markdown-only changes are excluded. An unavailable previous push commit fails
path detection rather than silently authorizing publication.

When protected inventory and project UUID inputs exist, builds use them even
while `OPENSTACK_PUBLISH_ENABLED=false`, so a paused publication still retains
real deployment artifacts for later promotion. A partially configured pair
fails closed. Only disabled publication with neither input uses example
inventory; those artifacts cannot be promoted into a different deployment.
Publication itself still requires `OPENSTACK_PUBLISH_ENABLED=true`.
Each production build checks out the native main run's
exact `GITHUB_SHA`, QEMU-boots an overlay of the retained QCOW2, and uploads
`production-role-<run-id>-<role>` with no extra compression and 30-day retention.
Each artifact contains its role directory with the exact QCOW2, closure JSON,
source/run record, full-inventory SHA256 (not the inventory contents), build log,
QEMU summary and full serial log. Failure diagnostics
use `production-logs-<run-id>-<attempt>-<role>` and upload only those log files.
The private inventory lives outside the upload root; build jobs receive no
OpenStack password, bootstrap credentials, private PKI or signing key.

The dependent `publish-images` job requires successful static checks, generated
recipe smoke, Nix evaluation, package tests, all five role VM tests and all five
image builds. It downloads the artifacts **from that same run**, verifies all
roles and CI results, and uploads `production-evidence-<run-id>-<attempt>` before
its first provider call. This artifact contains source/component and role
manifests, SBOM/provenance and `publication.json`, which binds CI context/results
and the exact retained file inventory with SHA256. Keep both role and evidence
artifacts; the receipt alone is not a backup of the images.

Publication is serialized in the `openstack-images-provider` concurrency group
and has no Nix build or QEMU step. It verifies content, closure/output identity,
source commit, metadata, owner/status and provider checksum, then waits for
uploads to become active. Missing provider hashes or Glance's standard SHA512
response use an independent image download/SHA256 check. An existing name is
reused only after the complete metadata and byte checks, never on source commit
alone; ambiguous or mismatched images fail without replacement. Only after
externally signed evidence verifies the exact existing bytes can publication
update the artifact-evidence digest as described below.

### Temporarily disable production signing

This exception removes signature/authenticity assurance, not SHA256, CI, QEMU,
source/closure/provenance or provider gates. It produces `releaseChannel=production`
and `trust.mode=production-unsigned`, never development evidence. Repository or
`openstack-images` environment variables control it; environment values override
repository values. An operator must explicitly review the effective scope.

1. Keep `OPENSTACK_PUBLISH_ENABLED=true` and the protected deployment/provider
   secrets configured.
2. Set `OPENSTACK_UNSIGNED_PRODUCTION=I_ACCEPT_UNSIGNED_PRODUCTION_IMAGES`.
   No other truthy spelling is accepted. Empty or `false` selects signed mode;
   other values stop publication.
3. Let a relevant main push run. No `RELEASE_EVIDENCE_URL`,
   `RELEASE_EVIDENCE_SHA256`, PEM or external bundle is required in this mode.
4. Require all five build jobs and `Verify and publish retained production images`
   to succeed. Inspect `publication.json` for the actual main commit/run and all
   five CI gate results, and retain the role artifacts and reported Glance UUIDs.

The workflow maps the exact variable to
`PLATFORM_ALLOW_UNSIGNED_PRODUCTION=I_ACCEPT_UNSIGNED_PRODUCTION_IMAGES` only
for verification/publication. Both modes use the same `-<commit-eight>` Glance
names and identical build/publication inventory. Nix embeds that inventory in
`/etc/<namespace>/platform.json`; hosted controller seeding requires an exact
name match. The pipeline checks a canonical SHA256 of the entire inventory,
including image names, before publication. Signing policy cannot change the
names, embedded configuration or retained bytes. Publication does not select an
image or replace a host.

### Sign retained GitHub images and re-enable signing

Use a trusted signing machine with the private key, Python, OpenSSL, the GitHub
CLI authenticated for repository read access, and a clean checkout of the source
commit. The signing key must never enter a runner or GitHub secret. Supply the
same private inventory/project UUID used by the builds; it is deliberately not
an artifact. Allow disk space for all five QCOW2s and finish within the 30-day
artifact retention window.

1. Record the main build run ID. For a default signed run, a missing or stale
   external evidence bundle may fail `publish-images` **after** the five builds
   have succeeded and uploaded their bytes. This is the external signing handoff,
   not a reason to rebuild. For an unsigned-to-signed promotion, use the original
   successful main run.
2. On the signing machine, download the five exact artifacts. Replace the run ID,
   checkout and private paths below with the reviewed source run and locations:

   ```sh
   repository=mit-sdg/openstack-deployment-infra
   run=123456789
   retained=/private/retained-images
   platform=/private/platform.json
   signing=/private/signed-release
   commit=$(gh api "repos/$repository/actions/runs/$run" --jq .head_sha)
   cd /private/clean-source-checkout
   test "$(git rev-parse HEAD)" = "$commit"
   mkdir -m 0700 "$retained" "$signing"
   for role in admin ingress storage worker builder; do
     gh run download "$run" --repo "$repository" \
       --name "production-role-$run-$role" --dir "$retained"
   done
   python3 -m openstack_platform.image_pipeline \
     --root "$retained" --platform "$platform" signing-inputs \
     --github-repository "$repository" --run-id "$run" \
     --output "$signing/inputs.json"
   ```

   `signing-inputs` reads the actual GitHub run/jobs APIs and refuses forks, PRs,
   another commit/workflow, incomplete job results, mixed build attempts or failed
   source CI/build jobs. It derives the same commit-suffixed names from the
   supplied private inventory (already-suffixed names are accepted), then verifies
   the full inventory hash, local hashes, closures, role metadata and clean source.
   It writes direct relocated QCOW2 inputs and
   `inputs.context.json`. It does not require or fabricate `GITHUB_*` variables,
   run Nix or rebuild a disk. The overall source run may be failed solely because
   publication is waiting for signatures; all required source jobs must succeed.
3. Sign the component and concrete role evidence, including the exported build
   context, then package only the signed evidence:

   ```sh
   python3 -m openstack_platform.release_manifest generate \
     --repository "$PWD" --commit "$commit" --output "$signing/evidence" \
     --signing-key /private/release-signing-key.pem
   python3 -m openstack_platform.release_manifest artifact-generate \
     --component-manifest "$signing/evidence/release-manifest.json" \
     --inputs "$signing/inputs.json" --build-context "$signing/inputs.context.json" \
     --output "$signing/evidence/artifacts" \
     --signing-key /private/release-signing-key.pem
   python3 -m openstack_platform.release_manifest bundle-create \
     --source "$signing/evidence" --output "$signing/release-evidence.tar"
   sha256sum "$signing/release-evidence.tar"
   ```

   The signed artifact provenance binds the real repository, commit, event/ref,
   run ID, build attempt and full-inventory SHA256. A valid signature for different
   bytes, image names or a different build context will still fail publication. No Nix store is needed on the
   signer: closure JSON and output identities are retained with the QCOW2s.
4. Serve this immutable bounded tar over HTTPS. Configure the reviewed
   `RELEASE_EVIDENCE_URL`, its `RELEASE_EVIDENCE_SHA256`, and the signing key's
   public PEM in `RELEASE_TRUST_ROOT_PEM` in the effective repository/environment
   scope. Clear `OPENSTACK_UNSIGNED_PRODUCTION` or set it to `false`. This is the
   signing rollback: merely unsetting the variable without supplying valid signed
   evidence intentionally stops publication.
5. Re-run **only `publish-images`** on that original run. Use **Re-run failed jobs**
   if it failed awaiting evidence; for promotion of a successful unsigned run,
   use the individual publication job's **Re-run job** control. Do not dispatch a
   new run, re-run all jobs, or change the source SHA. A later run attempt consumes
   the earlier complete five-role build; immutable role artifact names prevent
   accidental replacement. The retry downloads the original roles, fetches the
   signed bundle and verifies the exact source/run binding before publishing.
6. Verify the new `production-evidence-<run-id>-<attempt>` artifact shows
   `production-ed25519`, the original QCOW2 SHA256s, inventory hash and build
   context. For an already-published unsigned image, require
   `signed-reattestation=verified` followed by publication success. Its UUID, name
   and QCOW2 remain unchanged. Only after signature, source/closure/metadata and
   independent provider/download SHA256 checks does the publisher update
   `<namespace>_artifact_manifest_sha256`, recording the previous digest in
   `<namespace>_previous_artifact_manifest_sha256`. It re-reads the image to verify
   the metadata update. Unsigned evidence cannot authorize such an update.
   Preserve both generations of evidence: the original publication was unsigned;
   the retained bytes are authenticated by the new signature now. Setup can reuse
   the same UUID without rebuilding, and hosted seeding still matches the immutable
   guest inventory.

If inventory, source SHA or any role hash differs, stop and identify the mismatch;
do not edit records or regenerate evidence against a rebuild. If artifacts have
expired, that run cannot be promoted. Start a new reviewed build/run and sign its
new identities instead. A failure after one upload can leave some candidates
published; rerunning the same publication job verifies/reuses exact matches and
continues without rebuilding, renaming or replacing image bytes. If a signed
metadata update succeeded but its final observation failed, a retry verifies the
new digest and completes without another update. Do not attempt to downgrade that
image's evidence by retrying with an unsigned manifest.

### Development publication before merge

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
publication is serialized to one job at a time, and a missing Glance SHA-256
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
export PLATFORM_RELEASE_SIGNATURE=/private/releases/$SOURCE_COMMIT/release-manifest.sig
export PLATFORM_RELEASE_TRUST_ROOT=/private/release-trust-root.pem
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

For helper installation, supply the stable guest runtime path
`/run/current-system/sw/bin/python3.14`, not its resolved Nix-store target. The
installer verifies Python 3.14 but preserves the supplied path in the persistent
launcher, so an admin-image replacement can change the underlying closure.
Both new and already-complete helper releases must pass an actual launcher
protocol smoke (an invalid request that dispatches no provider action), not just
an import check using the installer's Python. A dead legacy launcher fails
closed; do not edit the accepted release or its `.complete` marker to bypass it.
Select an intact compatible release or stage a new reviewed release with the
supported installer before replacing infrastructure.

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

### Import a legacy external controller

`deploy/releases/migrate_legacy_controller.py` imports an authenticated legacy
v5 external-controller snapshot into the current hosted-controller schema. Use
it only when the source has migration versions `0` through `5`, the historical
deployment marker matches the destination project/namespace/inventory identity,
and every operation is terminal. Other schemas and unfinished operations fail
without creating the destination.

Before import, stop mutation intake, create and restore-check the external
operator-state and managed-data backups, and take a mode-`0600` SQLite online
snapshot. Prepare a mode-`0600` application mapping keyed by every legacy slug.
Each entry supplies the retained source ref and current configuration snapshot;
storage bindings identify an existing resource by type and name so the importer
can assign its new immutable resource UUID. For example:

```json
{
  "example-app": {
    "requestedRef": "main",
    "configuration": {
      "schemaVersion": 1,
      "build": {
        "runtime": "bun",
        "packages": ["."],
        "buildScript": "build",
        "startScript": "start"
      },
      "runtime": {"port": 3000, "healthPath": "/health"},
      "storageBindings": [
        {
          "resourceType": "mongo",
          "resourceName": "default",
          "outputs": {"uri": "MONGODB_URI"}
        }
      ]
    }
  }
}
```

Run the importer from the exact destination release checkout. The destination
state directory must not exist. Normally every legacy operation must be
terminal. If a replacement rollback completed but the legacy recovery defect
left exactly one `infra.replace` operation at `replacement_created`, first
verify that the recorded candidate UUID is absent, the recorded old server owns
the recorded port and volumes, role readiness succeeds, and product health is
restored. Record those immutable IDs and the verification time in a private
receipt, then pass it with `--replacement-rollback-receipt`. Any field mismatch,
additional unfinished operation, or receipt without an unfinished operation is
rejected.

```json
{
  "format": 1,
  "operationId": "<recorded-operation-uuid>",
  "role": "storage",
  "oldServerId": "<recorded-old-server-uuid>",
  "replacementServerId": "<confirmed-absent-candidate-uuid>",
  "portId": "<recorded-port-uuid>",
  "volumeIds": ["<recorded-volume-uuid>"],
  "verifiedAt": "<UTC-timestamp>"
}
```

Then run the importer:

```sh
python3 deploy/releases/migrate_legacy_controller.py \
  --source-database /private/legacy/platform.sqlite3 \
  --source-state /srv/openstack-platform/state \
  --destination-state /private/hosted-controller-import \
  --platform /private/current-platform.json \
  --application-mapping /private/application-mapping.json \
  --replacement-rollback-receipt /private/replacement-rollback.json
```

Success reports `legacy-controller-import=verified` and creates a current-schema
`platform.sqlite3`, copies only accepted build logs referenced by imported
deployments, and writes `LEGACY-IMPORT-RECEIPT.json` with source/destination
checksums and bounded counts. Verify the receipt, current schema, application,
storage, image selections, live Nomad job, worker identity, and public product
health before installing the database through the offline hosted-controller
restore procedure in [Operations](OPERATIONS.md#restore-the-hosted-controller).
Do not copy migration rows or construct product records manually.

### Database migration order

Controller migrations are forward-only. Before selecting a release that raises
the schema version, stop mutation intake and take/verify controller backups.
Install matching operator/helper code, migrate once, start the controller, and
verify its API before replacing role images.

Executable rollback is safe only while the old controller accepts the current
schema. After the imported hosted controller accepts a mutation, rollback also
requires stopping it, restoring the pre-import external-controller backup, and
selecting the old complete release and role images. Database restore and
provider-state recovery remain separate.

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
  trust root, or a recorded temporary unsigned-production authorization identifies
  the exact run and explicitly accepts the missing authenticity assurance;
- every selected image has accepted role-live evidence and exact Glance UUID,
  commit, checksums, closure, and metadata;
- setup and public health pass in the intended project;
- hosted-controller, external operator-state, management-broker, and managed-data backups are
  committed, restore-checked, and exported off site;
- a full-loss recovery drill has current evidence;
- persistent-host replacement has passed for release-relevant changes; and
- disposable live acceptance verifies when it is a release requirement.

Evidence records may contain bounded resource identities, hashes, readiness
results, timestamps, correlation IDs, and reviewer approval. They must not
contain credentials, provider payloads, age identities, secret values, or
backup contents.

## Preflight controller paths before an admin image upgrade

Run this read-only check from the exact reviewed candidate before **every admin
image replacement**. Use the same inventory and fixed account IDs as the new
image. On the build machine, with `PLATFORM_CONFIG` pointing to that inventory:

```sh
nix build --impure .#root-path-preflight --out-link .tmp/root-path-preflight
```

The result's `share/root-path-preflight/` contains `host_paths.py` and the public
`controller-path-plan.json`. Review the paths/account IDs and transfer both
files through the existing approved administration process. On the existing
admin host, the approved recovery console administrator places them in a
root-controlled directory such as `/run/root-path-review` and runs:

```sh
python3.14 -I -B /run/root-path-review/host_paths.py preflight \
  --plan /run/root-path-review/controller-path-plan.json
```

The image also exposes `openstack-platform-root-path-preflight` for repeating
the installed plan. Root is needed to traverse the controller's private paths;
the operator account cannot inspect them. Use the existing console/admin route,
without adding operator sudo permission or changing access modes for inspection.
The tool reads metadata only: it does not read credential contents, create/copy
files, change modes/owners, replace selectors or start/stop units. It reports all
rejected paths with expected values and observed `uid:gid:mode:nlink`, plus type
and size. Inaccessible or missing components are reported as unavailable, with
the blocking path. Exit zero and `root-path-preflight=ok refusals=0` are mandatory
before proceeding. Keep the metadata-only report with the image change review.

If refused, defer the image upgrade. Investigate each observed mismatch and
repair only the reviewed paths through the existing administration procedure;
then rerun the exact candidate plan. Do not apply blanket recursive chown/chmod
to the state volume. The check does not validate JSON contents, TLS trust,
database migrations, provider state or helper compatibility; their existing
upgrade checks still apply. Changes after preflight are checked again during
preparation, using the same plan and held file descriptors.

The remaining metadata requirements have these purposes:

| Path / operation | Requirement and reason |
| --- | --- |
| Every ancestor | Direct directory, owned by root or the relevant account; holds traversal within the administered namespace. Links are never followed. |
| Policy/image copy source | Regular operator-owned mode-0600 file, up to 1 MiB; same privacy rule as before, with bounded copying compatible with existing JSON/seed limits. Group and link count are unrestricted because source inodes are read only. |
| Controller copy directory | Controller owner, no group/other write; prevents another principal from changing the copy destination namespace. Group and other mode bits are unrestricted. |
| Existing copy destination | Absent or any non-directory entry; replacement never opens or modifies its inode. UID/GID/mode/link count are irrelevant, including symlinks and hardlinks. |
| Private credentials/directories | Operator owner and private modes 0600/0640 or 0700/0750. Group is unrestricted without group access; group-readable/traversable cases admit only operator/controller groups. Metadata-mutated regular files must have one link to prevent changes to aliases outside the reviewed path. |
| Public PEM/.pub | Direct regular operator-owned 0644 file. Any group and link count, matching the previous read-only contract. |

## Owner portal artifacts and installation

The owner portal is implemented but not deployed. No publication or live Commons
acceptance is implied by its build/tests. Commons authentication is checked by a
separate broker-only Unix identity process; credential archive/signing-key logic
is not part of the login contract. Admin needs Python 3.14 with stdlib system-CA
TLS, fixed identity account/network/DNS sandbox, setgid roots, core/umask/mount
hardening, activation and broker backup units. PyJWT/cryptography are not runtime
dependencies; cryptography is dev-only for loopback certificates. Node stays on
the build/CI host. The stable runtime is
`/run/current-system/sw/bin/management-python3.14`.

Build from the exact clean committed source with Node 24. The npm build writes a
receipt binding Git commit/dirty state, npm lock, Node version and every generated
asset including Vite's manifest. Dirty/stale output is rejected. The workspace
lock is `frontend/package-lock.json`; shared UI source and workspace configuration
are bound into the owner frontend identity. Registry dependencies retain npm
integrity pins; local workspace packages carry source digests in the SBOM. Source manifests
identify the shipped UI source and compatibility; their source hash alone never
proves generated bytes. Management artifact evidence binds the actual archives
and asset inventory. The shared descriptor is `management-artifacts.json`.

```sh
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend/owner-portal run build
uv run python -m openstack_platform.management_release build \
  --repository . --commit FULL_COMMIT --output management-evidence \
  --signing-key /private/release-signing-key.pem
uv run python -m openstack_platform.management_release verify \
  --repository . --commit FULL_COMMIT \
  --component-manifest management-evidence/release-manifest.json \
  --manifest management-evidence/management-artifacts.json \
  --source-signature management-evidence/release-manifest.sig \
  --signature management-evidence/management-artifacts.sig \
  --trust-root /private/release-trust-root.pem
```

Replace FULL_COMMIT with a full lowercase source commit. Both detached signatures
use the helper/role evidence's existing Ed25519 trust root and channel policy;
no checksum-only alternative establishes authenticity. Development CI uses
explicit unsigned-development, which production mode rejects. Existing emergency
unsigned-production requires the same explicit missing-authenticity acceptance
as other platform evidence. It is not a signed release. The production signing
key remains outside artifacts. SBOM/provenance include npm integrity pins, Python
locks (including dev dependencies), actual Node version and output hashes.

Archives are `broker-COMMIT.tar` and `web-COMMIT.tar`. Both include the verified
commit-addressed source tar and a narrow runtime payload; only web includes exact
built `static/`. Identity is `bin/management-identity` in broker. No Node/npm/Vite/
bun runtime, node_modules, fake signing material or harness state is packaged.
The source tar is retained for authenticated source verification, not execution
of frontend tooling on admin.

On the prepared mounted admin state volume, as the unprivileged operator, invoke
the installed trusted release installer once for each mode:

```sh
openstack-platform-install-release --mode broker \
  --archive /private/broker-COMMIT.tar --archive-sha256 BROKER_SHA256 \
  --commit FULL_COMMIT --platform-config /etc/PLATFORM_NAMESPACE/platform.json \
  --expected-platform-namespace PLATFORM_NAMESPACE \
  --expected-platform-identity-sha256 DEPLOYMENT_IDENTITY_SHA256 \
  --release-manifest /private/release-manifest.json \
  --release-signature /private/release-manifest.sig \
  --release-trust-root /private/release-trust-root.pem \
  --management-manifest /private/management-artifacts.json \
  --management-signature /private/management-artifacts.sig
```

Repeat with web and WEB_SHA256. Supply hashes from authenticated descriptor
records. DEPLOYMENT_IDENTITY_SHA256 is `platform_config_identity(load_platform(PATH))`
from `openstack_platform.config`, and PLATFORM_NAMESPACE is the inventory namespace;
use the reviewed stable deployment identity, not an archive filename. The
installer deliberately resolves the NixOS `/etc` inventory symlink before opening
the direct target. Preflight reads each original archive, manifest, evidence,
signature and trust-root input once into memory. It verifies private copies of
those bytes, authenticating both manifests, the full archive hash, compatibility
and verifier wheel before candidate Python runs. It retains the exact verified
bytes under a fixed evidence-name allowlist. Candidate imports and contract data
resolve through their verified private package, independently of installed code.
Link/device/duplicate/escape and byte/member bounds apply; the in-memory payload
must match its closed layout, authenticated runtime and generated UI inventory.
Generation hashes and packages a single captured set of asset bytes.

Each staged release contains immutable mode-0440 `config/platform.json` and
`config/management.json`; broker also contains `config/identity.json`. Each
component reads its own inventory with its service group. Configs use the relative
`platform.json` path, so staging, rename and rollback preserve the snapshot.
Release directories include commit, pair identity and inventory hash in their
names. Only the target component is staged; failed smoke leaves prior configs,
selectors and activation requests byte-identical. Files inherit service groups
under mode-2750 directories. Broker/identity or web smoke runs without network or
live DB, before `.complete`, fsync and atomic staged `current` selection. The
installer never clears inherited setgid. Services cannot write releases; reuse
verifies bytes/modes/groups and keeps prior complete releases.

A matching broker/web descriptor identity and identical inventory snapshots are
required before the operator-owned mode-0640 activation marker is written. The
root admin-image oneshot takes the install lock and checks both complete staged
releases against the marker's exact commit and pair identity. It creates a
root-owned pair directory and atomically replaces the single
`management-active/current` selector, then restarts identity, broker and web.
Root requires a direct root-controlled state hierarchy and a root-owned mode-0755
state mount root. Activation and installer input reads use nonblocking no-follow
opens and check for regular files before reading; FIFOs are refused.
Units and boot path watchers run exclusively through that active selector.
Selecting the first half leaves activation pending; restarts or reboot retain the
previous active pair. A rejected marker leaves the active pair unchanged. Stop
admissions during a coordinated upgrade, back up first, and review
schema 3/protocol 3/controller API 1. Schema migration is forward and checksum
validated; archive selection never performs a blind schema downgrade. Retain the
pair's source manifest, descriptor, signatures, trust root, SBOM/provenance and
checksummed archives for rollback review. The GitHub frontend job builds/verifies
unsigned development archives after its HTTPS smoke at the same source commit;
it performs no publication.

The final schema-3 account migration upgrades schemas 1/2 with their original
checksums, preserves user/app/quota/configuration/intent state and invalidates
sessions. It adds local credentials/TOTP, immutable role/generation snapshots,
single-use tokens and private admin/read audit. The earlier unpublished grant
prototype schema-3 checksum is unsupported; reset only disposable development
state. Do not rewrite a recorded checksum or silently downgrade production data.

Coordinate broker/web/auth protocol 3 and schema 3 in the source manifest,
archive requirements, isolated smoke, standalone installer, root activation and
retained-pair validation. The admin image's own activation code pins compatibility;
a protocol-2 image refuses protocol-3 releases and requires the approved host
replacement. The bootstrap command is an unprivileged Python entry point in the
image/controller package and in the broker release. Its mode-0640 hash-only file
uses the existing operator-owned broker-group setgid config directory; no new
sandbox access, directory, password inventory or broker TCP permission is needed.
The same replacement carries project deployment maintenance/plan support, the
retained-IP warning boolean and project storage deletion. Cascade app deletion
and controller administrator routes remain privileged. Nix unit isolation is unchanged.

Take a verified backup, quiesce release/backup admissions, install a matched pair,
verify readiness and take a fresh backup before reopening. Old-schema binaries
are not compatible rollbacks; retained activation refuses incompatible evidence.
A schema-3 restore keeps roles and local credential/factor state, increments
all generations and deletes sessions/invitations/enrollment handles. It rejects
pre-restore enrollment files and advances factor replay counters beyond the
restore window. Recover with a new operator enrollment URL and local admin if
necessary; normal account/role/quota maintenance uses the admin UI. See
[bootstrap and recovery](OPERATIONS.md#bootstrap-or-recover-a-local-admin).

Admin management preparation uses scoped `systemd-tmpfiles` configurations after
the state/backup mounts are present. It never runs path-based `install -d`, chown
or chmod under release or backup directories writable by the operator or a
service. Root creates no prepared `config/platform.json`; the optional renderer
copies validated public inventory as the operator. Root prepares only the broker
backup root; the broker creates `.staging` at service privilege. Runtime directory
group adjustments also use tmpfiles. Controller preparation opens every path
component without following links, checks UID/GID/type/mode on handles and changes
metadata through those handles. Policy/image copies use fresh exclusive files
and directory-relative replacement. Restore cleanup runs as the service account;
operator backup credentials are copied by the operator rather than root.
Each prepared directory is post-checked without following links for its exact
UID/GID/mode. A symlink, FIFO, file or mismatch fails the unit even when tmpfiles
returns zero. Persistent portal directories and the hosted-controller backup
root use create-only ownership/mode semantics: an existing directory is not
silently given to another account. Runtime group adjustments remain on fixed
systemd-created directories beneath root-owned `/run`. Controller/state and
operator mount-root bootstrap rules retain their existing semantics on trusted
mount locations. Root activation opens releases, release, bin, evidence and
config through held checked directory descriptors and reads relative to them.

Use [retained pair reactivation](OPERATIONS.md#reactivate-a-retained-portal-pair)
to roll back compatible executables and their own inventory snapshots. The
operator command re-verifies retained evidence and payloads before smoke and
uses the same staged selectors, install lock, marker and root activation path.


### Hosted deploy-key backup upgrade

The hosted controller, helper and root restore launcher ship in the admin image.
Replace that image to adopt deploy-key backup/restore support. Before replacement,
retain the existing SQLite/managed-data backups; after readiness, run the hosted
backup service and verify both committed SQLite and source-key trios. Require a
version-3 off-site bundle before claiming deploy-key full-loss recovery. Older
bundles remain accepted but contain no deploy keys. See [hosted restore](OPERATIONS.md#restore-the-hosted-controller).

### Managed-data backup format upgrade

The admin image supplies the backup services, restore scripts, monitoring and
Garage helpers. Replace the admin image to adopt managed-data format 3 and its
backup-volume mount dependency. Retain and verify existing format-2 evidence;
it remains readable. New sets exclude app images. After full restore, follow
[rebuild app images](OPERATIONS.md#rebuild-app-images-after-full-restore) using
the controller's accepted commits and settings before reopening apps.

The same admin update grants read-only backup access on new app buckets and
backfills existing buckets at backup time. After replacement, run a fresh
managed-data backup and its coverage check; retain the resulting catalog-format-2
archive before claiming S3 full-loss recovery. The Garage storage image stays
pinned to v2.3.0; these changes use its existing admin API.
