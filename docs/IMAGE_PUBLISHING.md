# Publish new role images automatically

This procedure publishes role-image candidates. It does not import platform
state, replace servers, or attach volumes. For automatic runs, the `CI`
workflow builds and uploads commit-addressed NixOS role images in the same
matrix job. It runs when a push to `main` changes the flake/lock, Nix or
infrastructure sources, embedded Python/controller or release sources, Python
packaging locks, or the packaged license. Markdown-only changes are excluded.
The workflow only publishes images; it does not select them or replace servers.

Publication remains disabled until the `openstack-images` GitHub environment has OpenStack credentials and the repository variable `OPENSTACK_PUBLISH_ENABLED` is set to `true`.

## Publication behavior

The workflow processes `admin`, `ingress`, `storage`, `worker`, and `builder` as follows:

1. detects whether the complete push range contains a publishable image-input change;
2. waits for static checks and Nix evaluation to pass;
3. checks out the exact commit and installs the private deployment inventory from the protected environment;
4. appends the first eight characters of the commit SHA to every configured base image name;
5. builds each role once with that derived private inventory;
6. locates the QCOW2 and records its Nix output and recursive path information;
7. verifies the signed role record against the exact QCOW2, Nix closure/output,
   source manifest, private inventory, commit, and publication metadata before
   any OpenStack credential enters the step environment;
8. verifies that the authenticated token belongs to the configured OpenStack project ID;
9. records the full SHA as image metadata and skips an existing role only when that metadata matches; and
10. publishes that same QCOW2 through `infra/openstack/publish_nixos_image.sh`,
    which repeats signed role verification and compares the local
    OpenStack-compatible MD5 checksum with Glance's returned `checksum` field
    before reporting success.

For example, a configured base name `example-nixos-worker` at commit
`0123456789abcdef…` becomes `example-nixos-worker-01234567`. All five images
use the same eight-character suffix and embed the same derived image-name set.
The full commit remains the checkout identity and is stored in each Glance
image's deployment-namespaced `source_commit` property.

The publisher does not overwrite an existing image. A rerun leaves an image for
that commit unchanged. If an eight-character suffix collides with an image
carrying a different full commit, publication fails instead of reusing it.
Entries under `images` remain stable base names; the workflow derives immutable
candidate names without changing the stored inventory.

A successful upload does not make an image ready for deployment. Before selecting it in a fresh control database, complete the disposable live tests and follow the persistent-role safeguards in [`../nix/README.md`](../nix/README.md).

## Publish from a reviewed local build

The protected workflow is the recommended publication path. A manual
publication must use the same signed evidence and four-argument publisher
contract; the old `ROLE QCOW2` form is unsupported.

First generate and verify the component and post-build artifact evidence in
[Release supply-chain evidence](RELEASE_SUPPLY_CHAIN.md). For each role, retain:

- the exact QCOW2 path;
- its Nix output store path;
- `nix path-info --json --recursive` output; and
- the verified artifact manifest/signature/trust root and the four values
  emitted by `release_manifest verify-role`.

Invoke the publisher only after exporting those verified values. Replace each
`REPLACE_*` value with the exact output/path for one role:

```sh
export PLATFORM_CONFIG="$PWD/config/platform.json"
export OSC=/srv/openstack-platform/bin/platform-openstack
export SOURCE_COMMIT="$(git rev-parse HEAD)"
export PLATFORM_RELEASE_MANIFEST=/private/releases/$SOURCE_COMMIT/release-manifest.json
export PLATFORM_ARTIFACT_MANIFEST=/private/releases/$SOURCE_COMMIT/artifacts/role-artifacts.json
export PLATFORM_ARTIFACT_SIGNATURE=/private/releases/$SOURCE_COMMIT/artifacts/role-artifacts.sig
export PLATFORM_ARTIFACT_TRUST_ROOT=/private/release-trust-root.pem
# Set these exactly from verify-role output for the selected role:
export PLATFORM_ARTIFACT_MANIFEST_SHA256='REPLACE_WITH_MANIFEST_SHA256'
export PLATFORM_ARTIFACT_QCOW2_SHA256='REPLACE_WITH_QCOW2_SHA256'
export PLATFORM_ARTIFACT_NIX_CLOSURE_SHA256='REPLACE_WITH_CLOSURE_SHA256'
export PLATFORM_ARTIFACT_NIX_OUTPUT='REPLACE_WITH_STORE_BASENAME'

SOURCE_COMMIT="$SOURCE_COMMIT" OSC="$OSC" \
  infra/openstack/publish_nixos_image.sh \
  REPLACE_ROLE /path/to/role.qcow2 /nix/store/REPLACE_ROLE_OUTPUT \
  /private/role.path-info.json
```

The publisher independently reruns signed role verification, checks the Nix
output/closure and QCOW2, verifies project ownership and active Glance status,
and refuses to overwrite an existing name. Record its image UUID, provider
checksum, QCOW2 SHA-256, source commit, and artifact manifest identity before
live role acceptance and `infra image set`.

### One build per published role

Pull-request, scheduled, and ordinary manual checks build with the sanitized
example inventory. When automatic publication is selected, the mutually
exclusive protected matrix replaces that ordinary image-build path: it builds
with `PLATFORM_CONFIG_JSON` and uploads the resulting file before the ephemeral
runner exits. No multi-gigabyte QCOW2 artifact is passed to another job, and no
second image build is required.

Because build and publication share a job, an OpenStack outage can make the
main-branch image check fail after a successful build. You can rerun the job:
it reuses an existing image only when the full `source_commit` metadata
matches.

## Repository security boundary

The publication workflow has access to an OpenStack password that authorizes project operations. Enforce these repository controls before storing or enabling that credential:

- `main` accepts changes only through pull requests;
- all CI checks must pass before merge;
- only the accounts listed in the branch-protection bypass or push allowlist may
  update or merge `main`; restrict that list to the deployment's
  image-publication maintainers;
- force pushes and branch deletion are disabled; and
- `.github/CODEOWNERS` identifies the workflow, Nix source, platform-config parser, and publisher as the image-publication boundary.

The current branch protection does not require an approving review. CODEOWNERS
records ownership of the publication boundary, but branch protection controls
who can update `main`. Organization owners may still have platform-level
emergency powers outside repository configuration.

When available, Keystone application credentials allow independent revocation and restriction. The target cloud currently returns `404` for the application-credential API, so the workflow uses password authentication. The password must remain an environment secret, must never be printed or copied into the inventory, and should be replaced with a dedicated automation identity if the cloud operator provides one.

## Configure the GitHub environment

Create an environment named `openstack-images` and limit deployments to protected branches. Automatic publication runs only for pushes to `main`. Add required reviewers to gate the protected build-and-publish matrix; omit reviewers for unattended publication.

Add these environment secrets:

| Secret | Value |
| --- | --- |
| `PLATFORM_CONFIG_JSON` | Complete private `config/platform.json`; it must not contain credentials |
| `OS_AUTH_URL` | OpenStack Identity v3 endpoint |
| `OS_USERNAME` | OpenStack account name used for publication |
| `OS_PASSWORD` | OpenStack password used for publication |
| `OS_PROJECT_ID` | The project-ID text returned by `openstack token issue` for the scoped token; preserve that exact representation for the workflow's raw preflight comparison (compact or canonical is accepted by its UUID parser) |

Run these GitHub CLI commands from the repository root. They keep secret values out of command arguments and shell history. Set them on the **`openstack-images` environment**, not only at repository scope: an environment secret of the same name takes precedence, and a repository-level secret alone is unavailable to the protected publication job. Set `OS_PROJECT_ID` from the authenticated token without adding or removing UUID hyphens; the workflow canonicalizes that value before writing the private inventory. Local foundation scripts should use `verify_openstack_project` from `infra/lib/platform-config.sh`, which normalizes compact/canonical provider output instead of comparing raw strings.

```sh
gh secret set --env openstack-images PLATFORM_CONFIG_JSON < config/platform.json
gh secret set --env openstack-images OS_AUTH_URL
gh secret set --env openstack-images OS_USERNAME
gh secret set --env openstack-images OS_PASSWORD
openstack token issue -f value -c project_id | \
  gh secret set --env openstack-images OS_PROJECT_ID
```

Enable the workflow only after all five secrets exist and branch protection has been verified:

```sh
gh variable set OPENSTACK_PUBLISH_ENABLED --body true
```

The workflow writes the private inventory with mode `0600` on an ephemeral hosted runner and removes it in an `always()` cleanup step. OpenStack credentials are available only to the image-check and image-publication steps. As with local private builds, Nix embeds the inventory's non-secret site configuration in the role image. The workflow does not upload the inventory or credentials as GitHub artifacts.

## Publish a new image set

1. Change an image input: `flake.nix`, `flake.lock`, `nix/`, `infra/`,
   `openstack_platform/`, `deploy/`, `pyproject.toml`, `uv.lock`, or `LICENSE`.
2. Merge the change to `main` and wait for every CI job to pass.
3. If the environment has required reviewers, approve the deployment.
4. Verify that all five reported image names end in the first eight characters of the CI-tested commit SHA and that their `source_commit` properties contain the full SHA.
5. Verify each uploaded image's provider checksum, separately recorded QCOW2 SHA-256, and Glance status. The publisher has already rejected a checksum mismatch; the workflow skips any role whose image already exists for that commit.

For each CI matrix role, the publication step is equivalent to:

```sh
SOURCE_COMMIT="$GITHUB_SHA" \
  infra/openstack/publish_nixos_image.sh "$ROLE" "$QCOW2"
```

Here `GITHUB_SHA` is the checked-out full commit, `ROLE` is one of
`admin ingress storage worker builder`, and `QCOW2` is the exact file built and
config-drive-smoke-tested in that same job. The workflow derives the
commit-suffixed image name before this command and passes the private
`PLATFORM_CONFIG` and OpenStack environment only to the protected job.

6. Run the role acceptance procedure in
   [`../nix/README.md`](../nix/README.md) before selecting the exact image UUID
   or replacing a persistent host. There is no image rename or manual
   promotion step.

Automatic publication runs inside `CI` for a push from this repository to protected `main` whose complete push range includes an image-input path listed above. Markdown-only, workflow, test, configuration-example, and other non-image changes use the ordinary example-image build path. Pull requests, scheduled CI, forked runs, and force-push ranges whose prior commit is unavailable do not publish images.

A manual `workflow_dispatch` with `publish` enabled targets the selected commit without requiring Nix change context. Use it only on protected `main` to recover a missing or interrupted publication for a commit that already passed the required review and tests.

## Disable or recover

Disable new runs before rotating credentials or investigating unexpected behavior:

```sh
gh variable set OPENSTACK_PUBLISH_ENABLED --body false
```

If the workflow's raw project-ID preflight fails, replace any incorrect **`openstack-images` environment** secret with the exact text emitted by `openstack token issue`; do not add or remove UUID hyphens for that CI comparison. For local provider checks, use `verify_openstack_project` rather than a raw string comparison. If an upload fails partway through, find the commit-addressed image in Glance and check whether its state is `queued`, `saving`, or `killed`. Remove only the failed candidate before rerunning; leave a healthy image with the same commit-addressed name unchanged.
