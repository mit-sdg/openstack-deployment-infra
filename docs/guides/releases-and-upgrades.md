# Releases and upgrades

Use this guide to prepare signed releases and upgrade a platform. It assumes you are a maintainer or an advanced operator. [Automated setup](deploy-the-platform.md) handles first deployment; [Hosts and images](hosts-and-images.md) covers rollout to running hosts.

## Page map

- Maintainers: [evidence and trust](#what-a-release-is), [produce a release](#producing-a-release), [live acceptance](#run-disposable-live-acceptance), [sign-off](#sign-off-a-release).
- Operators: [install and upgrade](#installing-and-upgrading), [database migrations](#database-migrations), [portal](#upgrade-the-owner-portal), [backup formats](#upgrade-backup-formats).

## What a release is

A **release** is one Git commit and its operator tools, helper, five role images, and two portal archives. **Release evidence** binds the reviewed commit and bytes to manifests signed with an offline Ed25519 key. Evidence is checked before setup creates state or calls Nix or OpenStack, installers switch releases, publication uploads to Glance, or the portal runs new code.

Different commits, tracked source changes, changed dependency locks, rebuilt images, or swapped files fail verification. The signature identifies the signing-key holder; keeping that key out of CI and platform hosts prevents either from forging signed evidence.

### What the evidence contains

| File | What it binds |
| --- | --- |
| `release-manifest.json` (component manifest) | The full source commit, the implementation contract (`infra/lib/platform_contract.json`), `uv.lock`, the packaged Python and dashboard files, the helper's action list, the controller's API and schema versions, the owner portal's source identity and protocol versions, and an identity for each role image's Nix inputs |
| `release.sbom.json`, `release.provenance.json` | An SPDX 2.3 bill of materials for the locked Python and npm dependencies, and in-toto provenance naming the component set |
| `artifacts/role-artifacts.json` (artifact manifest) | For each of the five QCOW2 images: its SHA-256 and size, its Nix output and full closure, and the exact Glance metadata it must carry. Also the component manifest's digest and, for CI builds, the build run |
| `artifacts/role-artifacts.sbom.spdx.json`, `artifacts/role-artifacts.provenance.json` | A bill of materials adding every Nix closure, and provenance naming each image and closure |
| `management-artifacts.json` | For a portal release: the broker and web archives, every built web asset, and the pair's compatibility versions |
| `*.sig` | Detached Ed25519 signatures over the manifests |

Manifests, bills of materials, and provenance aren't secrets. The signing key is.

### Trust modes

| Mode | `releaseChannel` and `trust.mode` | Use it for | Verification accepts it only with |
| --- | --- | --- | --- |
| Signed production | `production`, `production-ed25519` | Every normal release | The detached signature and the matching public trust root |
| Unsigned production | `production`, `production-unsigned` | A temporary emergency when signing isn't available | The exact acknowledgement `I_ACCEPT_UNSIGNED_PRODUCTION_IMAGES` (`PLATFORM_ALLOW_UNSIGNED_PRODUCTION` or `--allow-unsigned-production`) and no signature material |
| Development | `development-unsigned`, `development-unsigned` | Tests, CI, and development clouds | The exact acknowledgement `I_UNDERSTAND_THIS_IS_NOT_PRODUCTION` (`PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT` or `--allow-unsigned-development`) |

Unsigned production retains integrity checks (hashes, CI gates, boot tests, source and closure binding, provider checks) but loses authenticity. Development evidence is refused whenever `PLATFORM_ENVIRONMENT=production`, even with the unsigned-production acknowledgement.

## Producing a release

Start with the [key](#create-the-signing-key) and [evidence](#produce-release-evidence), then [test](#build-and-test-role-images) and [publish](#publish-image-candidates).

## Create the signing key

Create the key once. Keep it outside the repository, GitHub, and every platform host. Verifiers use its public half, the **trust root**.

```bash
umask 077
openssl genpkey -algorithm ED25519 -out /private/release-signing-key.pem
openssl pkey -in /private/release-signing-key.pem -pubout \
  -out /private/release-trust-root.pem
```

## Produce release evidence

From a clean checkout at the exact release commit, generate the component manifest, then build all five images and generate the artifact manifest. The tools reject a different `HEAD` or uncommitted tracked changes.

### Check frontend freshness first

Before signing or publishing changes under `frontend/`, require **Frontend workspace and committed dashboard freshness** (`dashboard-frontend`) at that commit. The manifest hashes committed `openstack_platform/dashboard/static/` bytes; freshness checks that they match the sources. With Node 24.19.0 and npm 11.17.0, the job installs the locked workspace, runs all checks, rebuilds, compares committed files and untracked output, and browser-tests the production content security policy. It also gates publication. Review source and generated output.

### Generate the component manifest

```bash
commit=$(git rev-parse HEAD)
python3 -m openstack_platform.release_manifest generate \
  --repository "$PWD" --commit "$commit" \
  --output /private/releases/"$commit" \
  --signing-key /private/release-signing-key.pem
python3 -m openstack_platform.release_manifest verify \
  --repository "$PWD" --commit "$commit" \
  --manifest /private/releases/"$commit"/release-manifest.json \
  --signature /private/releases/"$commit"/release-manifest.sig \
  --trust-root /private/release-trust-root.pem
```

You should see `release-manifest=<path>` and then `release-manifest=verified`.

### Generate the artifact manifest

Each image embeds the deployment inventory, including commit-suffixed names, and Glance metadata binds project, namespace, and prefix. Build all five roles with the inventory setup or publication will use. Setup rebuilds and rejects hashes differing from this manifest.

1. Build each role and record its Nix closure:

   ```bash
   export PLATFORM_CONFIG=/private/platform.json
   install -d -m 0700 /private/build
   for role in admin ingress storage worker builder; do
     nix build --impure --print-build-logs \
       --out-link "/private/build/result-$role" ".#$role-image"
     nix path-info --json --recursive "$(readlink -f "/private/build/result-$role")" \
       > "/private/build/$role.path-info.json"
   done
   ```

2. Generate `artifact-inputs.json`, one entry per role: QCOW2 path (`qcow2`), closure record (`pathInfo`), Nix output (`outputStorePath`), and exact Glance metadata (`publicationMetadata`):

   ```bash
   uv run --frozen python - "$commit" /private/build \
     > /private/releases/"$commit"/artifact-inputs.json <<'PY'
   import json, os, sys
   from pathlib import Path
   from openstack_platform.config import load_platform
   from openstack_platform.openstack import publisher_metadata

   commit, build = sys.argv[1], Path(sys.argv[2])
   platform = load_platform(Path(os.environ["PLATFORM_CONFIG"]))
   inputs = {}
   for role in ("admin", "ingress", "storage", "worker", "builder"):
       output = (build / f"result-{role}").resolve()
       (qcow2,) = output.rglob("*.qcow2")
       inputs[role] = {
           "qcow2": str(qcow2),
           "pathInfo": str(build / f"{role}.path-info.json"),
           "outputStorePath": str(output),
           "publicationMetadata": dict(publisher_metadata(platform, role, commit)),
       }
   print(json.dumps(inputs, indent=2, sort_keys=True))
   PY
   ```

3. Generate and verify the signed artifact manifest:

   ```bash
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

   You should see `artifact-manifest=<path>` and then `artifact-manifest=verified`.

### Package evidence for CI

Bundle only the eight manifest, signature, SBOM, and provenance files for CI; never include the signing key.

```bash
python3 -m openstack_platform.release_manifest bundle-create \
  --source /private/releases/"$commit" \
  --output /private/releases/"$commit"/release-evidence.tar
sha256sum /private/releases/"$commit"/release-evidence.tar
```

Serve the tar at an immutable HTTPS URL without redirects; HTTP is refused. Set `RELEASE_EVIDENCE_URL`, `RELEASE_EVIDENCE_SHA256`, and `RELEASE_TRUST_ROOT_PEM` in CI. For publication, [sign the retained CI build](#sign-a-ci-build-and-publish-it).

### Unsigned development evidence

For tests and development clouds:

```bash
python3 -m openstack_platform.release_manifest generate \
  --repository "$PWD" --commit "$(git rev-parse HEAD)" \
  --output /tmp/platform-development-evidence \
  --unsigned-development
```

Verification then needs `--allow-unsigned-development`, or this exact setting in the setup file:

```text
PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT=I_UNDERSTAND_THIS_IS_NOT_PRODUCTION
```

The same acknowledgement is required for `artifact-generate --unsigned-development`. `openstack-platform-install-release` treats broker and web installs on admin as production and rejects development evidence. Operator and helper installs allow explicitly acknowledged development evidence, including helper deployment over the bridge. `PLATFORM_ENVIRONMENT=production` rejects it in every mode.

## Build and test role images

Local builds remain [candidates](hosts-and-images.md#candidates-and-accepted-images) until live-tested. The flake exposes one output per role:

```text
.#admin-image
.#ingress-image
.#storage-image
.#worker-image
.#builder-image
```

Set the private inventory through `PLATFORM_CONFIG`; `--impure` reads it outside the flake source. Keep it out of Git (`config/platform.json` is ignored):

```bash
export PLATFORM_CONFIG="$PWD/config/platform.json"
nix flake check --impure --no-build --print-build-logs
nix build --impure .#builder-image
```

Without `PLATFORM_CONFIG`, builds use `config/platform.example.json`. Nix daemon access is required; never bypass an approved build path through a rootful container socket. CI tests images in three layers:

1. **Package smoke** (`package-smoke`) runs the packaged Nomad, Traefik, BuildKit, age, OpenStack client, Python, controller, and release installer.
2. **Role VM tests** (`vm-admin`, `vm-ingress`, `vm-storage`, `vm-worker`, `vm-builder`) boot NixOS test machines with test-only certificates and filesystems.
3. **QCOW2 boot test** ([`tests/smoke_openstack_image.sh`](../../tests/smoke_openstack_image.sh)) boots each exact QCOW2 under QEMU with an OpenStack config drive and waits for `qcow-config-drive-smoke=passed role=<role>`.

For example, run the builder VM test locally:

```bash
nix build --print-build-logs .#checks.x86_64-linux.vm-builder
```

These tests exclude live Nova scheduling, Neutron rules, provider routing, Glance uploads, and cross-role checks; use live acceptance. Before admin replacement, run the [root-path preflight](hosts-and-images.md#preflight-controller-paths-before-an-admin-image-upgrade). A build cannot validate live admin files.

## Publish image candidates

CI on `main` or manual publication uploads verified Glance candidates without selecting images or replacing hosts.

### How CI publishes

Publication starts on an authorized `main` dispatch with `publish=true`, or a `main` push changing image inputs: `flake.nix`, `flake.lock`, `config/platform.example.json`, `nix/`, `infra/`, `openstack_platform/`, `frontend/`, `deploy/`, `pyproject.toml`, `uv.lock`, or `LICENSE`. Markdown-only changes do not count. Missing previous-push commits fail the comparison and block publication.

1. Five role runners build the run's exact commit in parallel (`max-parallel: 5`, `fail-fast: false`) with protected inventory and project ID, then QEMU-boot a QCOW2 copy. Uncompressed `production-role-<run-id>-<role>` artifacts retain the QCOW2, closure JSON, source/run record, full inventory SHA-256 (without inventory), and build and QEMU logs for 30 days. Failure logs use `production-logs-<run-id>-<attempt>-<role>`. Builders receive no OpenStack passwords, bootstrap credentials, private certificates, or signing keys.
2. `publish-images` ("Verify and publish retained production images") requires static checks, generated recipe tests, Nix evaluation, package tests, all five role VM tests and builds, and frontend freshness. It verifies that run's five artifacts, then uploads `production-evidence-<run-id>-<attempt>` before OpenStack calls. Keep this evidence (manifests, SBOMs, provenance, and `publication.json` binding CI results and every retained file's SHA-256) and the role artifacts; receipts cannot replace images.
3. Publication is serialized (`openstack-images-provider`) and never rebuilds. It verifies content, closure, commit, metadata, owner, and status; uploads; waits for active status; and checks provider checksums. Without provider SHA-256 (including SHA-512 defaults), it downloads and SHA-256-checks bytes, up to three attempts. Same-name images are reused only after all metadata and byte checks; ambiguous or mismatched images fail without replacement.

All modes use inventory role names suffixed with the commit's first eight characters (setup: `<prefix>-nixos-<role>-<commit8>`). Publication checks the entire inventory hash, including names. Images embed it at `/etc/<namespace>/platform.json`; hosted-controller seeds require exact names. Signing mode cannot change names, configuration, or bytes.

Protected inventory and project ID must be configured together. Builds use them even with `OPENSTACK_PUBLISH_ENABLED=false`, retaining promotable artifacts. One missing input fails; with publication disabled and both absent, example-inventory builds cannot be promoted to real deployments.

### Configure the `openstack-images` environment

Create `openstack-images`; allow `main` and, for development publication, only chosen same-repository PR branch patterns.

| Name | Kind | Purpose |
| --- | --- | --- |
| `PLATFORM_CONFIG_JSON` | Secret | The production deployment inventory |
| `DEVELOPMENT_PLATFORM_CONFIG_JSON` | Secret | A development inventory with its own namespace, prefix, addresses, and volumes |
| `OS_AUTH_URL`, `OS_USERNAME`, `OS_PASSWORD`, `OS_PROJECT_ID` | Secrets | OpenStack credentials for publication |
| `OPENSTACK_PUBLISH_ENABLED` | Variable | `true` to publish; empty or `false` to build without publishing |
| `OPENSTACK_UNSIGNED_PRODUCTION` | Variable | Empty or `false` for signed mode; `I_ACCEPT_UNSIGNED_PRODUCTION_IMAGES` for the [temporary unsigned mode](#temporarily-disable-production-signing). Any other value stops publication |
| `RELEASE_EVIDENCE_URL`, `RELEASE_EVIDENCE_SHA256`, `RELEASE_TRUST_ROOT_PEM` | Variables | The signed evidence bundle and public trust root for signed mode |

Review effective settings: environment values override repository values. Password authentication (`OS_AUTH_TYPE=password`) requires a project-scoped user. Require environment reviewers for attended publication, protect `main` against direct and force pushes, and retain [CODEOWNERS](../../.github/CODEOWNERS) coverage for workflow, Nix, and infrastructure files. Never expose secrets through `pull_request_target`.

### Sign a CI build and publish it

Sign retained CI bytes and their build run externally, then rerun only publication. This also promotes unsigned production images without rebuilding. Within 30-day artifact retention, use a trusted machine with the key, Python 3, OpenSSL, authenticated GitHub CLI read access, a clean release-commit checkout, the original private inventory and project ID, and space for five QCOW2s.

1. Use the original `main` build run, including a successful unsigned run. In signed mode, missing evidence stops `publish-images` after builds succeed: `Signed publication awaits RELEASE_EVIDENCE_URL; sign retained role artifacts externally, then rerun only this job`. Missing or stale evidence needs signing, not rebuilding.

2. Download artifacts and export signing inputs on that machine. Replace run ID and paths:

   ```bash
   repository=<OWNER>/<REPOSITORY>
   run=<RUN_ID>
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

   `signing-inputs` checks the GitHub run and its jobs and rejects forks, PRs, differing commits or workflows, incomplete or failed source CI or builds, and mixed build attempts. A run failed only on publication is allowed. It checks inventory-derived commit-suffixed names, inventory and file hashes, closures, role metadata, and clean checkout, then writes `inputs.json` and `inputs.context.json` without Nix or rebuilding.

3. Sign and bundle, retaining build context:

   ```bash
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

   Provenance binds repository, commit, event, ref, run ID, build attempt, and inventory hash. Signatures for other bytes, names, or context fail publication. Downloaded closures remove the signer's need for a Nix store.

4. Serve the bundle over HTTPS without redirects. Set effective `RELEASE_EVIDENCE_URL`, `RELEASE_EVIDENCE_SHA256`, and public `RELEASE_TRUST_ROOT_PEM`; clear `OPENSTACK_UNSIGNED_PRODUCTION` or set `false`. Without valid signed evidence, this stops publication.

5. Rerun only `publish-images` on the original run: **Re-run failed jobs** if awaiting evidence, or **Re-run job** on a successful unsigned publication. Never dispatch anew, rerun all jobs, or change commits. Publication verifies the original five-role build against the bundle's source/run binding.

6. Require `production-evidence-<run-id>-<attempt>` to record `production-ed25519`, original QCOW2 hashes, inventory hash, and build context. Role logs must end with `published role=<role> image=<uuid> status=active ...`.

   Previously unsigned images also require `signed-reattestation=verified`. After signature, source, closure, metadata, and SHA-256 checks, publication updates `<namespace>_artifact_manifest_sha256`, saves its former value in `<namespace>_previous_artifact_manifest_sha256`, and re-reads to confirm (namespace dashes become underscores). Unsigned evidence cannot update this metadata. IDs, names, and bytes stay unchanged; setup can reuse the ID. Keep original unsigned and subsequent signed evidence.

On inventory, commit, or role-hash differences, stop and investigate; never edit records or sign rebuilt substitutes. Expired artifacts require a new reviewed run and signatures for its fresh images.

After partial uploads or a signed metadata update whose final check failed, rerun the same job to verify/reuse exact matches and finish. It does not rebuild, rename, or replace bytes. Never downgrade evidence with an unsigned manifest.

### Temporarily disable production signing

Use only when signing is unavailable and written authorization explicitly accepts missing authenticity. All integrity checks still apply (see [Trust modes](#trust-modes)); evidence uses `trust.mode=production-unsigned`, never development mode.

1. Keep `OPENSTACK_PUBLISH_ENABLED=true` and the protected deployment and provider secrets in place.
2. Set `OPENSTACK_UNSIGNED_PRODUCTION=I_ACCEPT_UNSIGNED_PRODUCTION_IMAGES`. No other spelling is accepted.
3. Let a relevant push to `main` run. This mode needs no evidence URL, hash, PEM, or bundle.
4. Require all five builds and **Verify and publish retained production images** to succeed. Check `publication.json` for the real `main` commit and run and all CI gate results. Keep the role artifacts and the reported Glance IDs.

The workflow forwards the acknowledgement as `PLATFORM_ALLOW_UNSIGNED_PRODUCTION` only for verification/publication. [Sign the retained CI build](#sign-a-ci-build-and-publish-it) to restore signed mode.

### Publish a development build before merge

To test a branch on a real development cloud before merging, dispatch the workflow on the exact head of an open pull request:

```bash
gh workflow run CI --ref '<open-pr-branch>' \
  -f publish=false \
  -f live_acceptance=false \
  -f development_publish=true
```

Development publication requires a manual dispatch in protected `openstack-images`, `OPENSTACK_PUBLISH_ENABLED=true`, and the exact head of exactly one open same-repository PR into `main`. It rejects `main` and forks. Five parallel jobs build with `DEVELOPMENT_PLATFORM_CONFIG_JSON`, retaining compact hashes and closures and uncompressed role QCOW2s for one day. An aggregation job creates shared unsigned evidence. Five serialized jobs each download, QEMU-boot, verify, and publish one exact QCOW2 without rebuilding. Require all five successes and `development-role-evidence-<commit>`.

### Publish by hand

[`publish_nixos_image.sh`](../../infra/openstack/publish_nixos_image.sh) takes role, QCOW2, Nix output, and `nix path-info` file; other inputs come from the environment. It verifies release/artifact evidence and project before uploading.

1. From the release checkout, set the evidence and provider variables:

   ```bash
   export PLATFORM_CONFIG=/private/platform.json
   export OSC=/srv/openstack-platform/bin/platform-openstack
   export OS_PROJECT_NAME="$(python3 infra/lib/platform_config.py get project)"
   export SOURCE_COMMIT="$(git rev-parse HEAD)"
   export PLATFORM_RELEASE_MANIFEST=/private/releases/$SOURCE_COMMIT/release-manifest.json
   export PLATFORM_RELEASE_SIGNATURE=/private/releases/$SOURCE_COMMIT/release-manifest.sig
   export PLATFORM_RELEASE_TRUST_ROOT=/private/release-trust-root.pem
   export PLATFORM_ARTIFACT_MANIFEST=/private/releases/$SOURCE_COMMIT/artifacts/role-artifacts.json
   export PLATFORM_ARTIFACT_SIGNATURE=/private/releases/$SOURCE_COMMIT/artifacts/role-artifacts.sig
   export PLATFORM_ARTIFACT_TRUST_ROOT=/private/release-trust-root.pem
   ```

   Use the build inventory's role name and matching `OS_PROJECT_NAME`; other projects are refused.

2. Verify one role's image and export the identities it prints:

   ```bash
   role=worker
   qcow2=<PATH_TO_ROLE_QCOW2>
   output=<NIX_OUTPUT_STORE_PATH>
   path_info=<PATH_TO_ROLE_PATH_INFO_JSON>
   verification=$(python3 -m openstack_platform.release_manifest verify-role \
     --component-manifest "$PLATFORM_RELEASE_MANIFEST" \
     --manifest "$PLATFORM_ARTIFACT_MANIFEST" \
     --signature "$PLATFORM_ARTIFACT_SIGNATURE" \
     --trust-root "$PLATFORM_ARTIFACT_TRUST_ROOT" \
     --role "$role" --qcow2 "$qcow2" --path-info "$path_info" \
     --output-store-path "$output" --platform "$PLATFORM_CONFIG" \
     --commit "$SOURCE_COMMIT")
   while IFS='=' read -r key value; do
     case "$key" in
       artifact_manifest_sha256) export PLATFORM_ARTIFACT_MANIFEST_SHA256=$value ;;
       qcow2_sha256) export PLATFORM_ARTIFACT_QCOW2_SHA256=$value ;;
       nix_closure_sha256) export PLATFORM_ARTIFACT_NIX_CLOSURE_SHA256=$value ;;
       nix_output) export PLATFORM_ARTIFACT_NIX_OUTPUT=$value ;;
     esac
   done <<< "$verification"
   ```

3. Publish:

   ```bash
   infra/openstack/publish_nixos_image.sh "$role" "$qcow2" "$output" "$path_info"
   ```

   You should see `published role=<role> image=<uuid> status=active checksum=<md5> sha256=<provider|download> source_commit=<commit>`.

Record Glance ID, provider checksum, QCOW2 SHA-256, source commit, and artifact-manifest SHA-256.

## Installing and upgrading

Choose [image rollout](#accept-an-image), [manual installation](#install-releases-outside-automated-setup), [portal upgrades](#upgrade-the-owner-portal), or [backup updates](#upgrade-backup-formats).

## Accept an image

Live-test the candidate in its role, then select its exact Glance ID. [Hosts and images](hosts-and-images.md#candidates-and-accepted-images) gives checks, selection commands, and replacement steps; follow its [rollout order](hosts-and-images.md#roll-out-new-role-images).

## Install releases outside automated setup

Use manual installation for reviewed standalone updates or recovery (admin replacement or operator-host rebuild). Setup installs matching releases initially. Run as the unprivileged `/srv/openstack-platform` owner from a clean checkout at the full release commit with generated, verified evidence. Admin must already provide Python 3.14 and helper runtime libraries; its image does. Releases never contain or replace the protected `platform-admin` SSH alias or `platform-openstack` credential wrapper (setup loads `/srv/openstack-platform/.secrets/openstack.env`).

### Bootstrap the stable runtime

Once per operator host, bootstrap shared pinned Python and uv under `/srv/openstack-platform`. The script verifies uv SHA-256, refuses root or sudo, links `age`, creates `/srv/openstack-platform/.secrets/ssh/id_ed25519` if absent, and creates a missing `platform-openstack` wrapper (`PLATFORM_OPENSTACK_CLI` selects its underlying CLI).

```bash
deploy/releases/bootstrap_operator_runtime.sh
test "$(/srv/openstack-platform/runtime/python3.14 --version)" = 'Python 3.14.7'
test "$(/srv/openstack-platform/bin/uv --version)" = \
  'uv 0.12.2 (x86_64-unknown-linux-gnu)'
/srv/openstack-platform/bin/age --version >/dev/null
```

The script ends with `operator-runtime=python-3.14.7 uv-0.12.2`.

### Install inventory and policy

Inputs must be direct files you own, unwritable by others; policy must also be readable only by you. The installer validates and atomically replaces copies without printing contents.

```bash
private_repo=/private/path/deployment-config
/srv/openstack-platform/runtime/python3.14 \
  deploy/releases/install_operator_config.py \
  --platform "$private_repo/config/platform.json" \
  --policy "$private_repo/config/platform-policy.json"
```

You should see `operator-config=installed`. The inventory lands at `/srv/openstack-platform/config/platform.json` and the policy at `/srv/openstack-platform/state/policy.json`.

### Generate the operator bridge

Generate the pinned `platform-admin` SSH alias after admin is ready. The CLI and scripts use it for admin calls. Install any existing operator key trusted by admin first:

```bash
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

`setup_operator_bridge.py` verifies authenticated project, admin address, console ED25519 fingerprint, scanned host key, unprivileged `agentops` account, and fixed provider wrapper. Never hand-edit output or accept changed host-key evidence.

### Install operator and helper

1. Install the operator CLI on the operator host. Use the same commit for admin's fixed controller helper:

   ```bash
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
   ```

   Before `.complete` and atomic `current` selection, the installer verifies signature, component hashes, canonical commit archive, runtime, lockfile, configuration, and entrypoint smoke tests. Expect `installed=operator commit=<commit> release=<path>`; `/srv/openstack-platform/bin` launchers select only complete releases.

2. Deploy the helper release through the bridge:

   ```bash
   export PLATFORM_RELEASE_MANIFEST=/private/releases/"$commit"/release-manifest.json
   export PLATFORM_RELEASE_SIGNATURE=/private/releases/"$commit"/release-manifest.sig
   export PLATFORM_RELEASE_TRUST_ROOT=/private/release-trust-root.pem
   deploy/releases/deploy_helper_release.sh "$commit"
   ```

   The script checks deployment inventories, transfers the commit archive over pinned SSH, verifies its digest, Git marker, and full production action list, switches atomically, and requires `INVALID_REQUEST` for an invalid probe. Admin needs no Git, sudo, or preinstalled installer. Expect `helper-release=<commit>:verified`.

3. Verify the selection and schedules:

   ```bash
   test "$(cat /srv/openstack-platform/operator-releases/current/.complete)" = "$commit"
   /srv/openstack-platform/bin/openstack-platform --help
   /srv/openstack-platform/bin/openstack-platform-restore --help
   systemctl --user is-enabled openstack-platform-backup.timer
   ```

Failed installs keep the previous complete selection. Selecting older releases restores neither databases nor OpenStack state. For unsigned production, replace installer signature and trust-root options with `--allow-unsigned-production`. For helper deployment, set `PLATFORM_ALLOW_UNSIGNED_PRODUCTION=I_ACCEPT_UNSIGNED_PRODUCTION_IMAGES` and omit signature and trust-root variables.

<details>
<summary>How the helper launcher survives admin image changes</summary>

The launcher uses stable `/run/current-system/sw/bin/python3.14` across admin-image changes. Every install, including complete retained releases, must pass a real invalid-request launcher smoke test without provider actions. Broken launchers fail closed. Never edit releases or `.complete`; select an intact compatible release or install a reviewed one before infrastructure replacement.

</details>

### Database migrations

The hosted-controller database on admin and the operator database share a forward-only schema. New code migrates on first open; older code rejects it with `database has an unknown future migration`. Schema rollback requires a restore. Compare the old and new manifests' `components.controller.schemaVersion`:

```bash
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["components"]["controller"]["schemaVersion"])' \
  /private/releases/<commit>/release-manifest.json
```

If the schema version increases:

1. Pause app changes, and take and verify hosted-controller and operator-state backups. See [Backups and recovery](backups-and-recovery.md).
2. Install the matching operator and helper releases.
3. Let each database migrate once: the operator's on its first command, the hosted one when the new admin image's controller starts.
4. Start the controller and check its API before you replace any other role image.

Older executables must accept the active schema. After a migrated controller accepts changes, rollback requires stopping it, restoring pre-upgrade backups, and selecting old releases and role images. Database restoration and OpenStack recovery remain separate.

### Import a legacy external controller

`deploy/releases/migrate_legacy_controller.py` imports operator-host controller schema 5 into the hosted schema. Require migrations `0` through `5`, matching project, namespace, and inventory markers, and finished operations (except the documented rollback receipt below). Invalid sources create no destination.

1. Stop app changes. Create and restore-check the external operator-state and managed-data backups. Take an online SQLite snapshot of the legacy database with mode `0600`.

2. Write a mapping file (`0600`) for every legacy slug, source ref, and current configuration. Storage bindings name existing resources by type and name so import can assign new IDs:

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

3. For exactly one rolled-back `infra.replace` left at `replacement_created`, confirm the recorded replacement server is gone, the old server owns recorded port/volumes, and role readiness and public health pass. Save IDs and time in a private receipt (`0600`):

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

   Any mismatch, an extra unfinished operation, or a receipt with no unfinished operation is refused.

4. From the exact destination release checkout, run the importer. The destination directory must not exist. Leave out `--replacement-rollback-receipt` if you didn't need step 3.

   ```bash
   uv run python deploy/releases/migrate_legacy_controller.py \
     --source-database /private/legacy/platform.sqlite3 \
     --source-state /srv/openstack-platform/state \
     --destination-state /private/hosted-controller-import \
     --platform /private/current-platform.json \
     --application-mapping /private/application-mapping.json \
     --replacement-rollback-receipt /private/replacement-rollback.json
   ```

   Expect `legacy-controller-import=verified destination=<path>`. Output contains current-schema `platform.sqlite3`, imported deployments' build logs, and `LEGACY-IMPORT-RECEIPT.json` with source/destination checksums and counts.

5. Verify receipt, schema, apps, storage, image selections, live Nomad job, worker identity, and public health. Use [offline hosted-controller restore](backups-and-recovery.md) to install. Never copy migration rows or build records manually.

After imported-controller changes, rollback requires stopping it, restoring the pre-import external-controller backup, and selecting old complete releases and role images.

## Upgrade the owner portal

Install a **matched pair** from one commit: `broker-<commit>.tar` (broker and identity) and `web-<commit>.tar` (web server and built site). [Open the owner portal](open-the-portal.md) covers initial installation, `ownerPortal` settings, and first portal admin.

### Build and sign the portal release

Build on a separate machine, never on admin, from a clean checkout at the exact commit with Node 24.19.0 and npm 11.17.0. The web receipt binds commit, tree cleanliness, lockfile, Node version, and every generated file; stale or dirty output is refused.

```bash
commit=$(git rev-parse HEAD)
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend/owner-portal run build
uv run python -m openstack_platform.management_release build \
  --repository . --commit "$commit" --output /private/portal/"$commit" \
  --signing-key /private/release-signing-key.pem
uv run python -m openstack_platform.management_release verify \
  --repository . --commit "$commit" \
  --component-manifest /private/portal/"$commit"/release-manifest.json \
  --manifest /private/portal/"$commit"/management-artifacts.json \
  --source-signature /private/portal/"$commit"/release-manifest.sig \
  --signature /private/portal/"$commit"/management-artifacts.sig \
  --trust-root /private/release-trust-root.pem
```

Expect `management-artifacts=<path>`, then `management-artifacts=verified archives=broker,web`. Output includes archives, component manifest (including built site), `management-artifacts.json`, signatures, SBOMs, and provenance.

Signatures use the release's Ed25519 key and trust modes; checksums cannot establish authenticity. Archives contain verified source and narrow runtimes; only web includes `static/`. Neither includes Node, npm, Vite, `node_modules`, signing material, or test state; frontend tooling never runs on admin. CI builds and verifies unsigned development archives after same-commit HTTPS smoke, without publishing.

Print the archive hashes from the verified descriptor; you'll pass them to the installer:

```bash
python3 - /private/portal/"$commit"/management-artifacts.json <<'PY'
import json, sys
document = json.load(open(sys.argv[1]))
for name, record in sorted(document["archives"].items()):
    print(name, record["sha256"], record["file"])
print("compatibility", json.dumps(document["compatibility"], sort_keys=True))
PY
```

### Install a matched pair

1. Pause portal deployments and other changes, announce maintenance, prevent backups during switching, and take verified broker and hosted-controller backups. See [Backups and recovery](backups-and-recovery.md).

2. Compute deployment identity from the operator-host release checkout. Inventory mismatches are refused.

   ```bash
   uv run python -c 'import sys; from openstack_platform.config import load_platform, platform_config_identity; print(platform_config_identity(load_platform(sys.argv[1])))' \
     /srv/openstack-platform/config/platform.json
   ```

3. Copy as the operator account; inputs must be owned by you. Keep SBOM and provenance beside their manifests. On the operator host, set `SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config`.

   ```bash
   scp -F "$SSH_CONFIG" -r /private/portal/"$commit" "platform-admin:portal-$commit"
   scp -F "$SSH_CONFIG" /private/release-trust-root.pem "platform-admin:portal-$commit/"
   ```

4. Install both halves from the same pair and inventory on admin as the operator (`ssh -F "$SSH_CONFIG" platform-admin`):

   ```bash
   commit=<FULL_COMMIT>
   NS=<namespace>
   IDENTITY=<DEPLOYMENT_IDENTITY_SHA256>
   evidence="$HOME/portal-$commit"
   chmod -R go-rwx "$evidence"
   openstack-platform-install-release --mode broker \
     --archive "$evidence/broker-$commit.tar" --archive-sha256 <BROKER_SHA256> \
     --commit "$commit" --platform-config "/etc/$NS/platform.json" \
     --expected-platform-namespace "$NS" \
     --expected-platform-identity-sha256 "$IDENTITY" \
     --release-manifest "$evidence/release-manifest.json" \
     --release-signature "$evidence/release-manifest.sig" \
     --release-trust-root "$evidence/release-trust-root.pem" \
     --management-manifest "$evidence/management-artifacts.json" \
     --management-signature "$evidence/management-artifacts.sig"
   ```

   Repeat with `--mode web`, `--archive "$evidence/web-$commit.tar"`, and `--archive-sha256 <WEB_SHA256>`. Take both hashes from the verified descriptor in the previous section.

5. **Check the activation** on admin:

   ```bash
   STATE="$(jq -r .paths.adminState "/etc/$NS/platform.json")"
   readlink -f "$STATE/management-active/current/broker"
   readlink -f "$STATE/management-active/current/web"
   systemctl show "$NS-management-activate.service" -p Result
   systemctl is-active "$NS-management-broker.service" "$NS-management-web.service"
   ```

   Require `<commit>-...` paths under `management-broker-releases/releases/` and `management-web-releases/releases/`, `Result=success`, and two `active` results. Sign in, check functionality, take a fresh broker backup, and reopen changes.

If paths stay unchanged, inspect the activation result and logs (`journalctl -u "$NS-management-activate.service"`); fix evidence, ownership, configuration, or compatibility before retrying activation.

<details>
<summary>What the installer and activation check</summary>

Root activation waits for both halves, and the active pair serves until switching. Before running new code, the installer reads archives, manifests, signatures, and trust roots once into memory. It verifies both signatures, full archive hashes, and compatibility, then uses only those bytes. It enforces size and member-count limits, rejects unsafe links, devices, and escaping paths, and checks exact layout, runtime, and built-site inventory. Staged `<commit>-<pair identity prefix>-<inventory hash prefix>` directories retain read-only `config/platform.json`, `config/management.json`, and broker `config/identity.json` for rollback. Smoke tests without network or live databases precede `.complete` and staged `current` switching. Failure preserves configuration, links, and activation request. Services cannot write releases.

Under the install lock, root validates staged halves against requested commit and pair identity, creates a root-owned pair directory, atomically switches `management-active/current`, and restarts identity, broker, and web. Paths are opened without following links; FIFOs and other non-regular files are refused. One staged half leaves activation pending; refused requests leave the active pair unchanged.

</details>

### Compatibility, schemas, and rollback

Descriptor `compatibility` declares broker, web, and authentication protocol 3, broker schema 3, and controller API 1.

Portal worker and builder size controls use additive controller API 1 routes. Release the new admin image and helper together (`app.build` now requires `builderFlavor`), plus the portal web/broker pair. The portal pair and controller can be upgraded in either order: an older controller leaves size controls unavailable with update guidance, while existing portal actions continue. Controller schema migration 6 adds builder defaults and per-app overrides; take a hosted-controller backup first. The migration leaves the inventory builder default effective until an admin changes it. Choose an intended new default explicitly after rollout; the release does not resize machines or set a new default automatically.

- Activation accepts only protocols and schema built into admin; changes require an approved compatible [admin replacement](hosts-and-images.md#replace-the-admin-host) first. Installers reject compatibility differing from the staged pair.
- Migration from schema 1 or 2 to 3 preserves users, apps, quotas, configuration, and intents; adds local accounts, authenticator codes, roles, single-use links, and audit logs; and signs everyone out. Different recorded schema-3 checksums are unsupported. Reset only disposable development data; never rewrite checksums or downgrade production data.
- Activation refuses retained pairs whose evidence mismatches its current schema. [Reactivation](open-the-portal.md#roll-back-to-a-retained-portal-pair) via `openstack-platform-management-reactivate` re-verifies a compatible pair and saved configuration through the same root path.
- Retain each pair's component manifest, descriptor, signatures, trust root, SBOMs, provenance, and checksummed archives.

Restoring a schema-3 broker backup keeps roles and local sign-in credentials, but signs everyone out and invalidates pending invitations and enrollment links. If you need a new portal admin afterward, see [Open the owner portal](open-the-portal.md).

## Upgrade backup formats

Backup services, restore tools, and monitoring ship in admin images; replace admin to adopt these updates. Commands are in [Backups and recovery](backups-and-recovery.md).

### Hosted deploy-key backups

1. Before replacing admin with an image supporting private-repository deploy-key backups, keep and verify existing hosted-controller and managed-data backups.
2. After the new admin is ready, run the hosted-controller backup and confirm two committed sets: the SQLite set, and `hosted-controller-source-keys-<timestamp>.tar.age` with its checksum and manifest.
3. Export a new off-site bundle. Only version 3 off-site bundles carry deploy keys. Versions 1 and 2 still restore, but they contain no keys.

### Managed-data backup format 3

- Adopt format 3 through admin replacement; backups require a mounted backup volume. Keep and verify readable format-2 sets.
- Format 3 contains PostgreSQL, MongoDB, and Garage S3 data, excluding app images. After full restore, rebuild from accepted commits and settings before reopening apps; see [Backups and recovery](backups-and-recovery.md).
- Backup read-only keys are granted on new buckets and added to existing buckets during backup.

After admin replacement, run and restore-check a fresh managed-data backup; retain it before relying on S3 full-loss recovery. Garage archives use catalog format 2; `RESTORE-MANIFEST` uses format 1. Garage's image stays unchanged; backup uses its existing admin API.

## Run disposable live acceptance

This destructive OpenStack drill builds a platform, deploys an app with PostgreSQL, MongoDB, and S3 data, disables and re-enables it, restores both SQLite databases and managed data, replaces a persistent host, recovers admin externally, deletes the app, and cleans up while proving unrelated resources unchanged. Every check requires positive evidence, not exit status alone. Run it only in a protected CI environment or a supervised release gate, never in an ordinary production project.

### Before you plan

- a dedicated disposable project, or exact ownership metadata on everything the drill changes;
- a fresh deployment UUID and a namespace of the form `acceptance-<label>-<first eight characters of the deployment UUID>`, where `<label>` is 1 to 12 lowercase letters, digits, or hyphens;
- the protected driver configuration: a version 1 JSON file naming that deployment UUID, project, and namespace, the operator and admin paths, the test app, and the replacement image IDs. [`openstack_platform/acceptance_live_driver.py`](../../openstack_platform/acceptance_live_driver.py) defines its exact shape;
- a private HMAC key file of at least 32 random bytes, mode `0600`;
- a private state directory, mode `0700`, that you keep after the run;
- at most one run for each deployment UUID.

### Plan, run, and verify

1. Plan without mutating OpenStack. The plan binds deployment, project, and namespace, driver and configuration hashes, unrelated-resource fingerprint, ordered actions, and time limits. It expires after 24 hours; edits invalidate review.

   ```bash
   umask 077
   uv sync --frozen
   export LIVE_ACCEPTANCE_DRIVER_CONFIG=/private/live-acceptance/driver.json
   export LIVE_ACCEPTANCE_DEPLOYMENT_ID="$(python3 -c \
     'import json,os; print(json.load(open(os.environ["LIVE_ACCEPTANCE_DRIVER_CONFIG"]))["deploymentId"])')"
   export LIVE_ACCEPTANCE_PROJECT_ID="$(python3 -c \
     'import json,os; print(json.load(open(os.environ["LIVE_ACCEPTANCE_DRIVER_CONFIG"]))["projectId"])')"
   export LIVE_ACCEPTANCE_NAMESPACE="$(python3 -c \
     'import json,os; print(json.load(open(os.environ["LIVE_ACCEPTANCE_DRIVER_CONFIG"]))["namespace"])')"
   install -d -m 0700 /private/live-acceptance-plan /private/live-acceptance-state

   uv run openstack-platform-acceptance plan \
     --deployment-id "$LIVE_ACCEPTANCE_DEPLOYMENT_ID" \
     --project-id "$LIVE_ACCEPTANCE_PROJECT_ID" \
     --namespace "$LIVE_ACCEPTANCE_NAMESPACE" \
     --driver "$PWD/.venv/bin/openstack-platform-acceptance-driver" \
     --output /private/live-acceptance-plan/plan.json \
     --max-minutes 360 \
     --step-timeout-seconds 1800
   ```

   Expect `live-acceptance-plan=<path> sha256=<hash> actions=<count>`. Limits: 15 to 720 minutes overall, 30 to 3600 seconds per step. Review and record the plan SHA-256.

2. Run with all three opt-ins: `LIVE_ACCEPTANCE_APPLY=1`, `--apply`, and exact confirmation.

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

   Expect `live-acceptance=passed evidence=<path>`. Resume interruptions with identical plan, driver, state directory, key, and confirmation. Never delete checkpoints or repair resources manually.

3. **Verify** the retained evidence:

   ```bash
   uv run openstack-platform-acceptance verify \
     --evidence-directory /private/live-acceptance-state \
     --signing-key /private/live-acceptance-evidence-hmac.key
   ```

   Require `live-acceptance-evidence=verified result=passed`. Privately retain sanitized `evidence.json`, SHA-256 and HMAC files, plan checksum, and reviewer identity. Store the HMAC key separately.

### Run it in CI

**Disposable live acceptance** runs only on manual dispatch with `live_acceptance=true` and `LIVE_ACCEPTANCE_ENABLED=true`:

```bash
gh workflow run CI --ref <branch> \
  -f publish=false \
  -f live_acceptance=true \
  -f development_publish=false
```

Require protected `live-acceptance` secrets `LIVE_ACCEPTANCE_DRIVER_CONFIG_BASE64`, `LIVE_ACCEPTANCE_EVIDENCE_HMAC_KEY_BASE64`, and `LIVE_ACCEPTANCE_PROJECT_ID` matching driver configuration; a self-hosted `live-acceptance` runner must write `/srv/openstack-platform-acceptance/runs/`. Pushes, PRs, schedules, and other manual runs never start the drill. CI uploads plan and sanitized evidence as `live-acceptance-<run-id>`.

## Sign off a release

Before production, retain private evidence that:

- static checks, unit tests, Nix evaluation, package smoke, every role VM test, and all five QCOW2 boot tests passed at the exact commit;
- the component and role-artifact signatures verify against your public trust root, or a recorded temporary unsigned-production authorization names the exact run and explicitly accepts the missing authenticity;
- every selected image has passed its role's live test and has a recorded Glance ID, commit, checksums, closure, and metadata;
- setup and public health checks pass in the intended project;
- hosted-controller, operator-state, portal broker, and managed-data backups are committed, restore-checked, and copied off site;
- a full-loss recovery drill has current evidence;
- persistent-host replacement has passed for changes that affect it;
- disposable live acceptance verifies, when your release process requires it.

Records may contain resource IDs, hashes, readiness results, timestamps, correlation IDs, and reviewer approval. Never include credentials, provider responses, age identities, secret values, or backup contents.

## Related

- Initial setup: [platform](deploy-the-platform.md), [owner portal](open-the-portal.md)
- Operations: [hosts and images](hosts-and-images.md), [backups and recovery](backups-and-recovery.md)
- Background: [security](../security.md), [internals](../reference/internals.md)
