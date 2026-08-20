# Prepare to create a fresh deployment

Use this page to decide whether the platform fits and prepare the management host for [`openstack-platform setup`](SETUP.md). Setup creates the OpenStack resources and private deployment material; this page does not mutate the cloud.

## Check that the platform fits

Use a new OpenStack project and empty management state. The platform currently supports applications that:

- come from a public, credential-free GitHub repository;
- build with the supported Node or Bun manifest and lockfile;
- run as one OCI container;
- serve HTTP on one port and expose a health path; and
- fit one dedicated worker using the configured standard flavor.

The project needs quota for three persistent roles, disposable builders, one worker per active application, five private Glance images, three fixed Neutron ports, and three Cinder volumes. Fresh setup defaults to 32 GiB of admin state, 500 GiB of managed data, and 200 GiB of encrypted backups.

The self-service portal is not implemented. An operator runs the installed command surface and retains the OpenStack, SSH, scheduler, storage-administrator, and backup credentials.

## Prepare the management host

Use an `x86_64-linux` host with:

- Nix and flakes;
- `git`, `ssh`, `ssh-keygen`, `openssl`, `curl`, and a user systemd manager;
- network access to OpenStack, GitHub, the Nix cache, OCI registries, and the persistent role addresses; and
- an unprivileged operator account that owns mode-`0700` `/srv/openstack-platform`.

Setup refuses root and `sudo`. Creating that management directory and installing Nix are host-administration prerequisites, not OpenStack operations.

From a clean full-commit checkout, verify the source before supplying credentials:

```bash
cd /path/to/openstack-deployment-infra
uv --version                         # locked development version: 0.12.2
uv sync --frozen
uv run python --version              # Python 3.14.x
uv run python -m unittest discover -s tests -v
nix flake check --no-build --print-build-logs
```

Setup later builds its own pinned OpenStack/Python, age, QEMU, and config-drive tooling through the flake. A failed source check is a stop condition.

## Obtain the external inputs

You need:

1. A protected OpenStack environment containing password or application-credential authentication for the target project.
2. Stable fixed IPv4 addresses for admin, ingress, and storage on the selected network. If the public ingress address must remain unchanged, put that exact address in `PLATFORM_INGRESS_ADDRESS`.
3. A public base domain and a management source CIDR.
4. A Cloudflare Tunnel token, or another provider satisfying [the public ingress contract](PUBLIC_INGRESS.md).

Cloudflare account, tunnel, DNS, and certificate creation are external. Setup can inject an existing token but does not administer the Cloudflare account.

Do not create SSH keys, age identities, service passwords, PKI, Glance images, keypairs, VMs, or volumes manually. Setup generates or provisions them.

## Review and run setup

Create the direct mode-`0600` environment file described in [Create a platform from one environment file](SETUP.md#create-the-protected-environment-file). Then review the non-mutating operation summary:

```bash
uv run openstack-platform setup --env-file /private/path/setup.env
```

Create the deployment only after checking the target project, fixed addresses, flavor choices, and volume sizes:

```bash
uv run openstack-platform setup \
  --env-file /private/path/setup.env \
  --cloudflare-token-file /private/path/cloudflare-tunnel-token \
  --apply
```

Setup prompts for omitted choices that cannot be discovered safely. A non-interactive run must supply them in the environment file. It does not guess fixed addresses.

Success requires healthy persistent roles, five accepted image selections, an enabled management backup timer, and—when a Cloudflare token is supplied—an `OK` response from the public platform health route.

## Continue with an application

After setup reports completion, follow the [tutorial](TUTORIAL.md) to declare or deploy an application, create managed storage, verify its HTTPS route, and clean it up.

Use the expanded [operations guide](OPERATIONS.md) for backup, restore, upgrades, individual setup checkpoints, and recovery. Use the [configuration reference](CONFIGURATION.md) to look up the generated inventory and policy fields.
