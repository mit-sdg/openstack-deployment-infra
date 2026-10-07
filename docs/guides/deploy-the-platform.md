# Deploy the platform

Deploy a new platform into your OpenStack project from a clean release checkout. Follow these steps on the operator host, checking each result before continuing.

## Before you start

- Complete the [planning checklist](plan-a-deployment.md#checklist).
- Log in as the unprivileged owner of `/srv/openstack-platform`. Don't run setup as root or through `sudo`.
- Start `tmux` or `screen`; image builds take a long time.
- Have your release commit, public trust root (`.pem`), and evidence files or bundle URL and SHA-256.

Every command runs from the root of your source checkout unless stated otherwise.

## 1. Check out and test the release

Release evidence is tied to an exact commit and clean checkout.

1. Clone the repository and check out the release commit:

   ```bash
   git clone <REPOSITORY_URL> openstack-platform-source
   cd openstack-platform-source
   git checkout <RELEASE_COMMIT>
   ```

   Git reports checkout progress; `git checkout` selects your release commit.

2. Confirm the checkout is clean:

   ```bash
   git status --short
   ```

   This prints nothing. If it lists files, save your work and use a fresh checkout.

3. Install the Python virtual environment and run the test suite:

   ```bash
   uv sync --frozen
   uv run python -m unittest discover -s tests -q
   ```

   `uv sync` reports dependency installation or verification. The tests end with `OK` (possibly with skipped tests). Resolve failures before continuing; [Development](../development.md) covers the toolchain.

4. Verify that Nix evaluates every machine role:

   ```bash
   nix flake check --no-build --print-build-logs
   ```

   It must exit successfully. For toolchain problems, see [Development](../development.md).

## 2. Create a private directory

Keep credentials and evidence outside the checkout in a directory only you can read:

```bash
export PRIVATE="$HOME/platform-private"
mkdir -p -m 0700 "$PRIVATE"
```

These commands are silent. Check `ls -ld "$PRIVATE"` for `drwx------`. If the directory already existed, `mkdir` doesn't reset its mode; use `chmod 0700 "$PRIVATE"` if needed. In a new shell, export `PRIVATE` again.

## 3. Get the release evidence

Setup verifies two signed records:

| Record | Contents |
| --- | --- |
| `release-manifest.json` and `.sig` | Pins source commit, Python lockfile, schemas, and helper actions |
| `artifacts/role-artifacts.json` and `.sig` | Pins the SHA-256, size, and Nix closure of each of the 5 role images |

Your **trust root** is the Ed25519 public key used to verify both signatures. Setup checks your checkout and each built image against these records.

> [!IMPORTANT]
> Images embed your [inventory](../reference/configuration.md#generated-inventory). Give the maintainer your non-secret setup values and project UUID before they [generate the artifact manifest](releases-and-upgrades.md#generate-the-artifact-manifest). Mismatches are detected after security groups and ports have been created.

1. Save the public key as `$PRIVATE/release-trust-root.pem`, obtained through a trusted channel separate from the evidence.

2. Record the current commit hash:

   ```bash
   commit=$(git rev-parse HEAD)
   ```

   This assignment is silent; `$commit` holds the full hash.

3. Use the option matching your evidence:

   **Bundle URL and SHA-256:**

   ```bash
   uv run python -m openstack_platform.release_manifest bundle-fetch \
     --url '<EVIDENCE_URL>' \
     --sha256 '<EVIDENCE_SHA256>' \
     --commit "$commit" \
     --trust-root "$PRIVATE/release-trust-root.pem" \
     --destination "$PRIVATE/releases/$commit"
   ```

   The destination varies; successful verification prints:

   ```text
   release-evidence=/home/<you>/platform-private/releases/<commit> verified=true
   ```

   **Loose files:** copy them to `$PRIVATE/releases/$commit/`, preserving `artifacts/`, then run:

   ```bash
   evidence="$PRIVATE/releases/$commit"
   uv run python -m openstack_platform.release_manifest verify \
     --commit "$commit" \
     --manifest "$evidence/release-manifest.json" \
     --signature "$evidence/release-manifest.sig" \
     --trust-root "$PRIVATE/release-trust-root.pem"
   uv run python -m openstack_platform.release_manifest artifact-verify \
     --component-manifest "$evidence/release-manifest.json" \
     --manifest "$evidence/artifacts/role-artifacts.json" \
     --signature "$evidence/artifacts/role-artifacts.sig" \
     --trust-root "$PRIVATE/release-trust-root.pem"
   ```

   The commands print `release-manifest=verified`, then `artifact-manifest=verified`.

For signature errors, check the commit and key with your maintainer; see [Setup check failures](troubleshooting.md#the-setup-check-doesnt-say-ready).

## 4. Create the setup file

Setup reads literal `KEY='value'` assignments, without running shell code or expanding variables.

1. Create the private setup file using `$PRIVATE` and `$commit` from above:

   ```bash
   install -m 0600 /dev/null "$PRIVATE/setup.env"
   cat >> "$PRIVATE/setup.env" <<EOF
   PLATFORM_RELEASE_MANIFEST='$PRIVATE/releases/$commit/release-manifest.json'
   PLATFORM_RELEASE_SIGNATURE='$PRIVATE/releases/$commit/release-manifest.sig'
   PLATFORM_RELEASE_TRUST_ROOT='$PRIVATE/release-trust-root.pem'
   PLATFORM_ARTIFACT_MANIFEST='$PRIVATE/releases/$commit/artifacts/role-artifacts.json'
   PLATFORM_ARTIFACT_SIGNATURE='$PRIVATE/releases/$commit/artifacts/role-artifacts.sig'
   PLATFORM_ARTIFACT_TRUST_ROOT='$PRIVATE/release-trust-root.pem'
   EOF
   ```

   These commands are silent. The file now holds six evidence paths. Don't repeat this step when resuming: it overwrites the file.

2. Set `EDITOR` to an editor executable, open the file, and add the other values:

   ```bash
   "${EDITOR:?set EDITOR}" "$PRIVATE/setup.env"
   ```

The editor opens the file with its six evidence paths. Replace every placeholder and example name and address below. Keep the evidence paths already written; don't paste them twice.

```bash
# OpenStack login: an application credential for this project
OS_AUTH_URL='https://openstack.example.edu:5000/v3'
OS_AUTH_TYPE='v3applicationcredential'
OS_APPLICATION_CREDENTIAL_ID='<APPLICATION_CREDENTIAL_ID>'
OS_APPLICATION_CREDENTIAL_SECRET='<APPLICATION_CREDENTIAL_SECRET>'
OS_PROJECT_NAME='<PROJECT_NAME>'
OS_REGION_NAME='<REGION>'
OS_INTERFACE='public'

# Names
PLATFORM_PREFIX='cs101'
PLATFORM_NAMESPACE='cs101'
PLATFORM_DISPLAY_NAME='CS 101'
PLATFORM_ORGANIZATION='Example University'
PLATFORM_DOMAIN='apps.example.edu'

# Network and fixed addresses
PLATFORM_NETWORK='<NETWORK_NAME>'
PLATFORM_OPERATOR_CIDR='192.0.2.10/32'
PLATFORM_ADMIN_ADDRESS='192.0.2.11'
PLATFORM_INGRESS_ADDRESS='192.0.2.12'
PLATFORM_STORAGE_ADDRESS='192.0.2.13'

# Public ingress (see step 5)
PLATFORM_INGRESS_MODE='tunnel'

# Machine sizes and volumes
PLATFORM_ADMIN_FLAVOR='<FLAVOR_AT_LEAST_2_VCPU_4_GIB>'
PLATFORM_INGRESS_FLAVOR='<FLAVOR_AT_LEAST_2_VCPU_2_GIB>'
PLATFORM_STORAGE_FLAVOR='<FLAVOR_AT_LEAST_4_VCPU_8_GIB>'
PLATFORM_WORKER_FLAVOR='<FLAVOR_AT_LEAST_1_VCPU_4_GIB>'
PLATFORM_BUILDER_FLAVOR='<FLAVOR_AT_LEAST_4_VCPU_8_GIB>'
PLATFORM_VOLUME_TYPE='<VOLUME_TYPE>'
PLATFORM_ADMIN_STATE_GIB='32'
PLATFORM_DATA_GIB='500'
PLATFORM_BACKUP_GIB='600'

# Release evidence (added by the command above)
PLATFORM_RELEASE_MANIFEST='/home/<you>/platform-private/releases/<FULL_COMMIT>/release-manifest.json'
PLATFORM_RELEASE_SIGNATURE='/home/<you>/platform-private/releases/<FULL_COMMIT>/release-manifest.sig'
PLATFORM_RELEASE_TRUST_ROOT='/home/<you>/platform-private/release-trust-root.pem'
PLATFORM_ARTIFACT_MANIFEST='/home/<you>/platform-private/releases/<FULL_COMMIT>/artifacts/role-artifacts.json'
PLATFORM_ARTIFACT_SIGNATURE='/home/<you>/platform-private/releases/<FULL_COMMIT>/artifacts/role-artifacts.sig'
PLATFORM_ARTIFACT_TRUST_ROOT='/home/<you>/platform-private/release-trust-root.pem'
```

`PLATFORM_OPERATOR_CIDR` must match the source address the admin host sees from your operator host (`/32` for one IPv4 address).

For password login, replace `OS_AUTH_TYPE` and `OS_APPLICATION_CREDENTIAL_*` with:

```bash
OS_USERNAME='<USERNAME>'
OS_PASSWORD='<PASSWORD>'
OS_USER_DOMAIN_NAME='Default'
OS_PROJECT_DOMAIN_NAME='Default'
```

You can omit `OS_PASSWORD` to have setup prompt for it securely without terminal echo.

Use single quotes and a regular file you own, mode `0600`. Duplicate keys are rejected; unquoted expansions are ignored. Save inventory settings explicitly: prompt answers aren't saved, and reruns need identical inputs. Avoid a shell loaded with another project's `OS_*` values. Horizon OpenRC files are accepted after setting mode `0600`. See [Configuration](../reference/configuration.md#setup-file).

## 5. Set up public ingress

Route `<domain>`, `*.<domain>`, and your secondary hostnames (defaults: `projects.<domain>`, `compute.<domain>`) to ingress. Use your [chosen mode](plan-a-deployment.md#choose-an-ingress-mode).

### Tunnel mode with Cloudflare

The ingress host runs `cloudflared`; Traefik listens on `127.0.0.1:80` with no public inbound web port.

1. Create a tunnel in Cloudflare and copy its connector token. The ingress host runs `cloudflared` for you.

2. Add the hostname routes above, all forwarding to `http://127.0.0.1:80`.

3. Save the token into a dedicated private file (single line, no extra text):

   ```bash
   install -m 0600 /dev/null "$PRIVATE/cloudflare-tunnel-token"
   "${EDITOR:?set EDITOR}" "$PRIVATE/cloudflare-tunnel-token"
   ```

   `install` is silent; the editor opens an empty file. Retain the token for replacements. See [Token requirements](../reference/configuration.md#cloudflare-tunnel-token-file).

When you supply a token, setup verifies that `https://<domain>/healthz` answers `OK` before completing.

Without a token, omit `--cloudflare-token-file`. Ingress stays pending. Later, follow [Replace the ingress host](hosts-and-images.md#replace-the-ingress-host) with a current token.

### Direct mode

In direct mode, your HTTPS provider connects directly to the ingress host IP on port 80.

1. Edit `$PRIVATE/setup.env` to set these values, replacing its existing ingress mode:

   ```bash
   PLATFORM_INGRESS_MODE='direct'
   PLATFORM_PROVIDER_CIDRS='203.0.113.0/24,198.51.100.7/32'
   ```

   List exact IPv4 ranges separated by commas. Wildcards (`0.0.0.0/0`), IPv6 ranges, host bits, and duplicates are rejected.

2. Route the hostnames above to the ingress IP on port 80, preserving `Host`.

Setup does not verify public reachability in direct mode. You verify it manually in step 8.

## 6. Run the read-only check

The check reads cloud resources, quotas, and evidence without changing OpenStack:

   ```bash
   uv run openstack-platform setup check --env-file "$PRIVATE/setup.env"
   ```

In tunnel mode, add `--cloudflare-token-file "$PRIVATE/cloudflare-tunnel-token"` to check ownership and mode. This check doesn't validate token contents or routing.

Output resembles this; project values, sizes, and paths vary. `...` omits lines:

```text
setup-check=ready project=cs101 project-id=6f1c2d3e-4b5a-4c6d-8e7f-90a1b2c3d4e5
network=campus-net (11111111-2222-4333-8444-555555555555)
flavor.admin=standard.2c4g (2 vCPU, 4096 MiB)
...
volume-type=production
fixed-address.admin=192.0.2.11 available
fixed-address.ingress=192.0.2.12 available
fixed-address.storage=192.0.2.13 available
quota.instances=+5 available=20 shortfall=0
quota.cores=+13 available=64 shortfall=0
...
quota.image_storage_bytes=+9876543210 available=unlimited shortfall=0
name-collisions=none
toolchain=x86_64-linux ready
tool.nix=/nix/var/nix/profiles/default/bin/nix
...
ingress=authenticated-tunnel domain=apps.example.edu
release=0123456789abcdef0123456789abcdef01234567
image.admin=cs101-nixos-admin-01234567 source=signed-reproducible-build size-bytes=1234567890
...
runtime-image.node=docker.io/library/node@sha256:...
service-image.postgres=docker.io/library/postgres@sha256:...
...
no resources or credentials were created
```

Proceed to step 7 only when the first line reports `setup-check=ready`. Add `--json` if you need JSON output for automation.

### What to do if the check fails

For `setup-check=failed` or an `error:` line:

| Output or error | What to do |
| --- | --- |
| `name-collision=<kind>:<name>` | Choose an unused prefix. Don't remove resources based on names alone. |
| `fixed-address.<role>=<ip> occupied` | Choose an unused address. |
| `quota.<name>=... shortfall=<N>` | Request quota or reduce sizes within role minimums. |
| `quota.<name>=... shortfall=None` | Quota is unknown. Ask your cloud administrator for inspection access. |
| `setup environment file must be a direct current-user-owned mode-0600 file` | Check ownership and file type; set mode with `chmod 0600 "$PRIVATE/setup.env"`. |
| `required local setup toolchain is unavailable: <tools>` | Install the named tools. |
| `configured <role> flavor is below the setup baseline` | Choose a larger flavor. |
| `release evidence preflight failed: ...` | Check the commit, evidence, and trust root. |
| `OpenStack Glance endpoint uses HTTP; ...` or `OpenStack Glance has no quota usage endpoint; ...` | Read [Provider acknowledgements](../reference/configuration.md#provider-acknowledgements). |

See [Setup check failures](troubleshooting.md#the-setup-check-doesnt-say-ready).

## 7. Create the deployment

Run setup inside `tmux` or `screen`:

```bash
uv run openstack-platform setup \
  --env-file "$PRIVATE/setup.env" \
  --cloudflare-token-file "$PRIVATE/cloudflare-tunnel-token" \
  --apply
```

Omit `--cloudflare-token-file` in direct mode or to leave ingress pending.

Setup rechecks the release, builds tooling, saves inventory and secrets, and creates security groups and ports. It builds, verifies, smoke-boots, and uploads images; boots admin, storage, and ingress with their volumes and credentials; and installs the pinned operator runtime, SSH bridge, CLI, helper, and backup timer. It checks controller sockets, accepts five images, and requires healthy status. With a token, it also checks public health.

Builds and uploads can be quiet. Progress includes `created security group`, `qcow-config-drive-smoke=passed`, and `operator-config=installed`. Success ends with these lines (project values vary):

```text
setup=complete project=cs101 project-id=6f1c2d3e-4b5a-4c6d-8e7f-90a1b2c3d4e5
inventory=/srv/openstack-platform/setup/config/platform.json
public-ingress=cloudflare-configured
```

The last line is `public-ingress=pending external provider configuration` in direct mode or without a token.

If setup fails with an `error:` line, do not manually delete partially created resources. Follow [Resume a stopped setup](#resume-a-stopped-setup) and see [Troubleshooting](troubleshooting.md#setup-stopped-partway).

## 8. Verify the result

1. Set shell variables (silent). [Run the platform](run-the-platform.md#set-up-your-shell) defines the full set:

   ```bash
   export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
   export PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json
   export SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config
   export PLATFORM_NAMESPACE="$(/srv/openstack-platform/runtime/python3.14 -c \
     'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["namespace"])')"
   export PLATFORM_DOMAIN="$(/srv/openstack-platform/runtime/python3.14 -c \
     'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["domain"])')"
   ```

2. Check platform state:

   ```bash
   $PLATFORM_CLI status
   ```

   A new deployment reports:

   ```text
   STATE    INFRA  APPS  STORAGE  LIVE  UNAVAILABLE  UNHEALTHY
   healthy  5      0     0        3     0            0
   ```

   `STATE` must be `healthy`. Nonzero unavailable or unhealthy counts need [investigation](troubleshooting.md#status-shows-degraded).

3. Check infrastructure roles:

   ```bash
   $PLATFORM_CLI infra list
   ```

   Image names and commits match your release. For example:

   ```text
   ROLE     IMAGE                         COMMIT                                    LIVE
   admin    cs101-nixos-admin-01234567    0123456789abcdef0123456789abcdef01234567  active
   builder  cs101-nixos-builder-01234567  0123456789abcdef0123456789abcdef01234567  unknown
   ingress  cs101-nixos-ingress-01234567  0123456789abcdef0123456789abcdef01234567  active
   storage  cs101-nixos-storage-01234567  0123456789abcdef0123456789abcdef01234567  active
   worker   cs101-nixos-worker-01234567   0123456789abcdef0123456789abcdef01234567  unknown
   ```

   In a fresh deployment, worker and builder rows show `unknown`. Permanent hosts must be `active`; otherwise see [Host recovery](troubleshooting.md#a-host-wont-come-back). Worker state is reported through apps, so its role row stays `unknown`. Builder state reflects unfinished build operations.

4. Verify controller services on the admin host:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
     "$PLATFORM_NAMESPACE-controller.service" \
     "$PLATFORM_NAMESPACE-controller-readiness.service"
   ```

   Both lines print `active`. Otherwise see [The controller isn't ready](troubleshooting.md#the-controller-isnt-ready).

5. Test the public health check endpoint:

   ```bash
   curl --fail --show-error --silent "https://$PLATFORM_DOMAIN/healthz"
   ```

   Expect `OK`. This checks DNS, TLS, forwarding, and Traefik. Configure pending ingress first. See [Public health fails](troubleshooting.md#public-health-fails).

6. Before going live, run and restore-check each backup set with [Backups and recovery](backups-and-recovery.md). The portal is still off; enable it next.

For file locations created by setup on the operator host, see [Configuration](../reference/configuration.md#files-setup-creates-on-the-operator-host).

## Resume a stopped setup

Setup saves generated inputs and checks existing resources before reusing them. Fix the cause named in `error:`, then rerun the identical `setup ... --apply` command with the same file and workspace.

- Do not delete `/srv/openstack-platform/setup` or `/srv/openstack-platform/.secrets`. They store generated private keys, passwords, and topology mappings.
- Do not modify inventory-shaping variables (prefix, names, fixed IP addresses, flavors, or volume sizes). Modifying them causes setup to stop with `existing setup inventory differs; use a new empty workspace`.
- Do not manually delete partially created servers, ports, volumes, or security groups in OpenStack. Setup validates and reconciles them.
- Role images already present in Glance matching release evidence are reused without rebuilding.
- Most subprocess commands time out after two hours; some checks have shorter limits. Timeouts report `error: setup dependency failed to execute: <tool>`. Fix the cause and rerun.

To change identity or topology, use a fresh workspace (`--workspace`) and a project scope without earlier resources. Don't repurpose installed operator state for another deployment.

## Next steps

1. [Open the owner portal](open-the-portal.md): install the portal and create the first portal admin.
2. [Run the platform](run-the-platform.md): health checks and everyday operations.
3. [Backups and recovery](backups-and-recovery.md): backup schedules and off-site recovery.

To decommission a deployment, review the [teardown boundary](run-the-platform.md#teardown-boundary).

## Related

- [Plan a deployment](plan-a-deployment.md)
- [Configuration reference](../reference/configuration.md)
- [Operator CLI reference](../reference/operator-cli.md)
- [Releases and upgrades](releases-and-upgrades.md)
- [Hosts and images](hosts-and-images.md)
- [Troubleshooting](troubleshooting.md)
