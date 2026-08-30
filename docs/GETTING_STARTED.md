# Prepare to create a fresh deployment

Use this page to decide whether the current infrastructure release fits and to
prepare the operator host for [`openstack-platform setup`](SETUP.md). Setup
creates OpenStack resources and private deployment material; this page does not
mutate the cloud.

## Check the current completion boundary

The supported result today is an operator-managed infrastructure deployment
with public platform health, role lifecycle, encrypted backups, and restore
checks. The local product controller API is implemented in code, but setup does
not run it as a service. The sync-engine management application and its
external authentication application are not implemented. There is therefore no
supported workflow for end users to sign in, deploy applications, or manage
storage.

The former product CLI and repository-owned deployment manifest are retired.
Do not plan an installation around old commands or an old release binary. The
future browser workflow and its intended application constraints are defined in
[MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md).

The OpenStack project needs quota for three persistent roles, disposable
builders, one worker per future active application, five private Glance images,
three fixed Neutron ports, and three Cinder volumes. Fresh setup defaults to 32
GiB of admin state, 500 GiB of managed data, and 600 GiB of encrypted backups. The backup volume must not be smaller than managed data.

## Prepare the operator host

Use an `x86_64-linux` host with:

- Nix and flakes;
- `git`, `ssh`, `ssh-keygen`, `openssl`, `curl`, and a user systemd manager;
- network access to OpenStack, GitHub, the Nix cache, OCI registries, and the
  persistent-role addresses; and
- an unprivileged operator account that owns mode-`0700`
  `/srv/openstack-platform`.

Setup refuses root and `sudo`. Creating that management directory and
installing Nix are host-administration prerequisites, not OpenStack operations.

From a clean full-commit checkout, verify the source before supplying
credentials:

```bash
cd /path/to/openstack-deployment-infra
uv --version                         # locked development version: 0.12.2
uv sync --frozen
uv run python --version              # Python 3.14.x
uv run python -m unittest discover -s tests -v
nix flake check --no-build --print-build-logs
```

Setup later builds pinned OpenStack/Python, age, QEMU, and config-drive tooling
through the flake. A failed source check is a stop condition.

## Obtain external inputs

You need:

1. A protected OpenStack environment containing password or
   application-credential authentication for the target project.
2. Stable fixed IPv4 addresses for admin, ingress, and storage on the selected
   network. Put a required stable public ingress address in
   `PLATFORM_INGRESS_ADDRESS`.
3. A public base domain and management source CIDR.
4. A Cloudflare Tunnel token, or another provider satisfying
   [the public ingress contract](PUBLIC_INGRESS.md).

Cloudflare account, tunnel, DNS, and certificate creation are external. Setup
can inject an existing token but does not administer the Cloudflare account.

Do not create SSH keys, age identities, service passwords, PKI, Glance images,
keypairs, VMs, or volumes manually. Setup generates or provisions them.

## Review and run setup

Create the direct mode-`0600` environment file described in
[Create a platform from one environment file](SETUP.md#create-the-protected-environment-file).
Then run the authenticated, non-mutating preflight:

```bash
uv run openstack-platform setup check --env-file /private/path/setup.env
```

Create the deployment only after the check reports `setup-check=ready` for the target project, quota, fixed addresses, provider choices, names, tooling, ingress, and sources:

```bash
uv run openstack-platform setup \
  --env-file /private/path/setup.env \
  --cloudflare-token-file /private/path/cloudflare-tunnel-token \
  --apply
```

Setup prompts for omitted choices that cannot be discovered safely. A
non-interactive run must supply them in the environment file. Setup never
guesses fixed addresses.

Success requires healthy persistent roles, five accepted image selections, an
enabled management backup timer, and—when a Cloudflare token is supplied—an
`OK` response from the public platform health route.

## Verify the supported result

After setup reports completion, follow the
[fresh-platform tutorial](TUTORIAL.md) to verify infrastructure, public ingress,
management backup, and managed-data restore checking.

Use [Operations](OPERATIONS.md) for backup, restore, upgrades, individual setup
checkpoints, and recovery. Use the
[configuration reference](CONFIGURATION.md) for generated inventory and policy
fields. Future UI implementers should start with
[MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md), not the operator CLI.
