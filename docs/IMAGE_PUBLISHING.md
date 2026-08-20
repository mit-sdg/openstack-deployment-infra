# Publish new role images automatically

This procedure publishes role-image candidates. It does not import platform
state, replace servers, or attach volumes. For automatic runs, the `CI`
workflow builds and uploads commit-addressed NixOS role images in the same
matrix job. It runs when a push to `main` changes `flake.nix`, `flake.lock`, or
anything under `nix/` or `infra/`. The `infra/` tree is an image input because
the admin image embeds it and the storage image references its registry-GC
implementation. The workflow only publishes images; it does not select them or
replace servers.

Publication remains disabled until the `openstack-images` GitHub environment has OpenStack credentials and the repository variable `OPENSTACK_PUBLISH_ENABLED` is set to `true`.

## Publication behavior

The workflow processes `admin`, `ingress`, `storage`, `worker`, and `builder` as follows:

1. detects whether the complete push range contains a publishable image-input change;
2. waits for static checks and Nix evaluation to pass;
3. checks out the exact commit and installs the private deployment inventory from the protected environment;
4. appends the first eight characters of the commit SHA to every configured base image name;
5. builds each role once with that derived private inventory;
6. locates and hashes the resulting QCOW2 before any OpenStack credentials enter the step environment;
7. verifies that the authenticated token belongs to the configured OpenStack project ID;
8. records the full SHA as image metadata and skips an existing role only when that metadata matches; and
9. publishes that same QCOW2 through `infra/openstack/publish_nixos_image.sh`, which compares the local OpenStack-compatible MD5 checksum with Glance's returned `checksum` field before reporting success.

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

## Publish all five roles from one reviewed commit

This is the complete local example. Run it from the repository root after
`uv sync --frozen`, with a protected `/srv/openstack-platform/bin/platform-openstack` wrapper and
an unused/versioned image name for every role in the private inventory. The
wrapper owns the OpenStack credentials; no credential is placed in an argument.

```sh
uv sync --frozen
export PLATFORM_CONFIG="$PWD/config/platform.json"
export PATH="$PWD/.venv/bin:$PATH"
export OSC=/srv/openstack-platform/bin/platform-openstack
export SOURCE_COMMIT="$(git rev-parse HEAD)"
test "$SOURCE_COMMIT" = "$(git rev-parse --verify HEAD)"
for role in admin ingress storage worker builder; do
  qcow="$(find -L "result-$role" -type f -name '*.qcow2' -print -quit)"
  test -f "$qcow" && test ! -L "$qcow" && test -r "$qcow"
  sha256sum "$qcow"
  SOURCE_COMMIT="$SOURCE_COMMIT" OSC="$OSC" \
    infra/openstack/publish_nixos_image.sh "$role" "$qcow"
done
```

The publisher requires `SOURCE_COMMIT` to be the full lowercase 40-character
SHA. It emits the complete role metadata projection: managed-by, metadata
version, namespace, prefix, project ID, role, compatibility hash, and
`<namespace-with-hyphens-replaced-by-underscores>_source_commit`. It refuses to
overwrite an existing image name, verifies owner/project and `active` status,
and compares the local OpenStack-compatible MD5 checksum with the Glance
`checksum` field. It prints the image UUID, `status=active`, provider checksum,
and full source commit. Record that provider checksum and the separately
computed QCOW2 SHA-256, then run the live role acceptance procedure before
`infra image set`.

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

1. Change `flake.nix`, `flake.lock`, or a file under `nix/` or `infra/`.
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

Automatic publication runs inside `CI` for a push from this repository to protected `main` whose complete push range includes an image-input path listed above. Changes limited to documentation, workflows, tests, configuration examples, or other non-image paths use the ordinary example-image build path. Pull requests, scheduled CI, forked runs, and force-push ranges whose prior commit is unavailable do not publish images.

A manual `workflow_dispatch` with `publish` enabled targets the selected commit without requiring Nix change context. Use it only on protected `main` to recover a missing or interrupted publication for a commit that already passed the required review and tests.

## Disable or recover

Disable new runs before rotating credentials or investigating unexpected behavior:

```sh
gh variable set OPENSTACK_PUBLISH_ENABLED --body false
```

If the workflow's raw project-ID preflight fails, replace any incorrect **`openstack-images` environment** secret with the exact text emitted by `openstack token issue`; do not add or remove UUID hyphens for that CI comparison. For local provider checks, use `verify_openstack_project` rather than a raw string comparison. If an upload fails partway through, find the commit-addressed image in Glance and check whether its state is `queued`, `saving`, or `killed`. Remove only the failed candidate before rerunning; leave a healthy image with the same commit-addressed name unchanged.
