# Create a platform from one environment file

Use `openstack-platform setup` to create a new deployment in an empty OpenStack project. The command generates the private inventory and policy, credentials, PKI, five NixOS role images, OpenStack resources, operator bridge, operator/helper releases, image selections, and backup initialization.

The admin role starts the controller on its restricted local socket after setup installs the policy and helper release. Setup verifies the service, socket permissions, and API readiness before reporting success. The management and authentication applications are not implemented; see [MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md).

The command is resumable: rerun the same command with the same protected environment file and workspace after correcting a failed dependency. It verifies an existing named resource before reusing it and refuses an existing setup inventory that differs from the requested deployment.

## Before running setup

Run setup from a clean, complete commit on an `x86_64-linux` operator host. The host must have:

- Nix with flakes enabled; setup builds its OpenStack client, Python SDK, `age`, QEMU, and config-drive smoke tooling from the pinned flake;
- `git`, `ssh`, `ssh-keygen`, `openssl`, `curl`, and a user systemd manager;
- an unprivileged management account that owns an existing mode-`0700` `/srv/openstack-platform`; and
- OpenStack quota for five private images, three persistent VMs, disposable builders and workers, three fixed ports, and 32 + 500 + 200 GiB of Cinder storage by default.

Setup refuses root and `sudo`. Creating and assigning `/srv/openstack-platform` is a management-host administration task outside the OpenStack project; perform it before giving the environment file to the unprivileged operator.

The OpenStack project should be empty of resources using the selected prefix. Setup does not adopt an unrelated server, volume, image, port, security group, keypair, controller database, or private setup workspace merely because its name matches.

## Create the protected environment file

Create a direct mode-`0600` UTF-8 file. Setup reads literal `KEY=value` and `export KEY=value` assignments without sourcing or executing the file. Single-quote values containing shell metacharacters. Duplicate assignments and non-private files are refused.

A complete password-authentication file has this shape:

```dotenv
OS_AUTH_URL='https://identity.example/v3'
OS_USERNAME='operator@example.org'
OS_PASSWORD='<SECRET>'
OS_PROJECT_NAME='new-project'
OS_USER_DOMAIN_NAME='Default'
OS_PROJECT_DOMAIN_NAME='Default'
OS_REGION_NAME='RegionOne'
OS_INTERFACE='public'

PLATFORM_PREFIX='new-project'
PLATFORM_NAMESPACE='new-project'
PLATFORM_DISPLAY_NAME='New Project Platform'
PLATFORM_ORGANIZATION='Example Organization'
PLATFORM_DOMAIN='apps.example.org'
PLATFORM_NETWORK='public'
# Secure default; no public origin listener.
PLATFORM_INGRESS_MODE='tunnel'
PLATFORM_OPERATOR_CIDR='192.0.2.10/32'
PLATFORM_ADMIN_ADDRESS='192.0.2.11'
PLATFORM_INGRESS_ADDRESS='192.0.2.12'
PLATFORM_STORAGE_ADDRESS='192.0.2.13'
PLATFORM_VOLUME_TYPE='production'
PLATFORM_ADMIN_FLAVOR='standard.2c4g'
PLATFORM_INGRESS_FLAVOR='standard.2c2g'
PLATFORM_STORAGE_FLAVOR='standard.4c8g'
PLATFORM_WORKER_FLAVOR='standard.2c4g'
PLATFORM_BUILDER_FLAVOR='standard.4c8g'
PLATFORM_ADMIN_STATE_GIB='32'
PLATFORM_DATA_GIB='500'
PLATFORM_BACKUP_GIB='200'

# Optional provider-neutral routes and internal names:
PLATFORM_RECOVERY_DOMAINS='projects.apps.example.org,compute.apps.example.org'
PLATFORM_STATIC_INGRESS_ROUTES_JSON='{}'
PLATFORM_STORAGE_INTERNAL_NAME='storage.new-project.internal'
PLATFORM_OBJECT_STORAGE_INTERNAL_NAME='s3.new-project.internal'
```

Use `OS_AUTH_TYPE=v3applicationcredential` with `OS_APPLICATION_CREDENTIAL_ID` and `OS_APPLICATION_CREDENTIAL_SECRET` instead of username/password when the cloud provides application credentials.

`OS_PROJECT_ID` is optional. Setup obtains the authenticated token project UUID, canonicalizes it, looks the project up again, and requires its name to equal `OS_PROJECT_NAME` before creating anything.

### Platform values and defaults

| Variable | Behavior when omitted |
| --- | --- |
| `PLATFORM_PREFIX` | Prompts with a normalized project-name default. |
| `PLATFORM_NAMESPACE` | Prompts with the prefix as its default. |
| `PLATFORM_DISPLAY_NAME` | Prompts with the project name as its default. |
| `PLATFORM_ORGANIZATION` | Prompts with the display name as its default. |
| `PLATFORM_DOMAIN` | Prompts with no default. |
| `PLATFORM_NETWORK` | Uses the only visible network; otherwise prompts. |
| `PLATFORM_INGRESS_MODE` | `tunnel`, with no public origin listener. |
| `PLATFORM_PROVIDER_CIDRS` | Must be omitted in tunnel mode. Direct mode requires a comma-separated list of canonical non-default IPv4 CIDRs. |
| `PLATFORM_OPERATOR_CIDR` | Uses the SSH client address as `/32` when available; otherwise prompts. |
| `PLATFORM_*_ADDRESS` | Prompts for each stable IPv4 address. Setup never guesses a fixed address. |
| `PLATFORM_*_FLAVOR` | Selects the smallest visible flavor meeting the role baseline; prompts if none does. |
| `PLATFORM_VOLUME_TYPE` | Uses `production`, or the only visible type; otherwise prompts. |
| `PLATFORM_ADMIN_STATE_GIB` | `32`. |
| `PLATFORM_DATA_GIB` | `500`. |
| `PLATFORM_BACKUP_GIB` | `200`. |
| `PLATFORM_DATACENTER` | Namespace. |
| `PLATFORM_REGION` | `global`. |
| `PLATFORM_RECOVERY_DOMAINS` | `projects.<domain>,compute.<domain>`; exactly two hostnames are required. |
| `PLATFORM_STATIC_INGRESS_ROUTES_JSON` | Empty JSON object. |
| `PLATFORM_STORAGE_INTERNAL_NAME` | `storage.<prefix>.internal`. |
| `PLATFORM_OBJECT_STORAGE_INTERNAL_NAME` | `s3.<prefix>.internal`. |
| `PLATFORM_BUN_RUNTIME_IMAGE` | The digest-pinned Bun image in the setup implementation. |
| `PLATFORM_NODE_RUNTIME_IMAGE` | The digest-pinned Node image in the setup implementation. |

Interactive prompts are for non-secret deployment choices. If `OS_PASSWORD` is absent, setup requests it through a hidden terminal prompt. A non-interactive run must provide every value that cannot be discovered or defaulted.

## Review the operation

The default invocation is non-mutating and does not generate credentials:

```bash
uv run openstack-platform setup --env-file /private/path/setup.env
```

It prints the major phases that `--apply` will perform. Keep the environment file and workspace private.

## Configure Cloudflare or another ingress provider

Setup validates ingress mode and provider CIDRs during the non-mutating check,
before creating its workspace or contacting OpenStack. The generated default is
an authenticated tunnel with no public origin. Cloudflare account, tunnel, DNS,
and certificate ownership remain external to OpenStack. To inject an existing
tunnel token during first boot, put exactly the token in a separate direct
mode-`0600` file and pass:

```bash
--cloudflare-token-file /private/path/cloudflare-tunnel-token
```

Without that option, tunnel mode boots ingress without `cloudflared` and reports
`public-ingress=pending external provider configuration`; it does not expose an
origin. Configure an authenticated on-host tunnel satisfying
[the public ingress contract](PUBLIC_INGRESS.md), then verify
`https://<PLATFORM_DOMAIN>/healthz` returns exactly `OK`.

For a provider that must connect directly, set both values in the environment
file and omit the tunnel-token option:

```dotenv
PLATFORM_INGRESS_MODE='direct'
PLATFORM_PROVIDER_CIDRS='203.0.113.0/24,198.51.100.7/32'
```

Setup rejects missing, malformed, duplicate, IPv6, host-bit, and
`0.0.0.0/0` entries before mutation. Setup with a Cloudflare token requires the
public route to return `OK` before reporting completion.

## Create the deployment

Run from the clean repository root:

```bash
uv run openstack-platform setup \
  --env-file /private/path/setup.env \
  --cloudflare-token-file /private/path/cloudflare-tunnel-token \
  --apply
```

Omit the Cloudflare option when using another provider. The default private workspace is `/srv/openstack-platform/setup`; override it with `--workspace` only for a distinct empty deployment attempt.

The command performs these ordered checkpoints:

1. Builds pinned OpenStack/Python and age tooling and verifies the authenticated project.
2. Writes a private inventory and policy; generates management/builder SSH identities, service secrets, backup identity, and PKI.
3. Creates or verifies security groups and immediately reserves the three fixed ports.
4. Builds each role image from the exact clean commit, boots the QCOW2 under QEMU with a config drive, and publishes it with commit and compatibility metadata.
5. Boots admin with its 32 GiB state and 200 GiB backup volumes and waits for the exact readiness marker.
6. Creates and verifies the pinned operator bridge and bootstraps Nomad ACLs.
7. Boots storage with its 500 GiB data volume, transfers only the required private provisioning inputs, and waits for readiness.
8. Boots ingress and verifies its readiness marker.
9. Installs the policy and matching operator/helper releases, then requires the hosted controller service, restricted socket, and API readiness check to succeed.
10. Selects all five exact image UUIDs, initializes managed-backup credentials, verifies the management backup timer, and requires healthy platform status.

Successful output ends with:

```text
setup=complete project=<name> project-id=<uuid>
inventory=/srv/openstack-platform/setup/config/platform.json
public-ingress=cloudflare-configured
```

The last line is `public-ingress=pending external provider configuration` when no Cloudflare token was supplied.

## Resume a stopped setup

Keep the generated workspace and `/srv/openstack-platform/.secrets/setup`. Correct the named dependency, then rerun the identical `setup ... --apply` command. Do not delete or edit a partially created server, volume, port, keypair, image, database row, or generated secret to force progress.

Setup reuses generated private material and verifies commit-addressed images and configured resources before continuing. It refuses a changed inventory in the same workspace. To request different stable names, addresses, volume sizes, or project identity, use an empty workspace and an empty provider resource set.

After completion, use the [fresh-platform tutorial](TUTORIAL.md) to verify public health and both backup classes. For bounded diagnosis after a role exists, use [Troubleshooting](TROUBLESHOOTING.md). The expanded manual procedure in [Operations](OPERATIONS.md) remains the recovery and audit reference for each setup checkpoint.
