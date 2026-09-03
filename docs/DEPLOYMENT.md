# Deploy the platform

This guide explains what the platform creates, what it supports, how its
security boundaries work, and how to create a deployment. It is written for the
person responsible for one OpenStack project. You do not need to understand the
controller's internal API to use this guide.

## What is available today

The repository can create and operate the infrastructure, including role
images, persistent hosts, ingress, managed data services, backups, recovery
tooling, and the local application controller.

The browser management UI and its authentication service do not exist yet.
There is therefore no supported application-owner workflow today. A successful
installation is an operator-managed platform foundation, not a finished hosted
product. The operator CLI deliberately contains no application or database
commands, and installing an old release is not a supported way to add them.

> **TODO:** implement the management UI, authorization broker, and external
> authentication integration described below. Until then, treat application
> lifecycle support as an internal controller capability rather than a user
> feature.

When the management UI is added, an application owner will be able to create a
project, point it at a public GitHub repository, choose a Node or Bun package
and package scripts, add write-only environment values, request PostgreSQL,
MongoDB, or S3 storage, and follow deployment progress. The UI will send typed
requests to the local controller; it will not give users OpenStack, SSH, Nomad,
registry, or database-administrator credentials.

The planned first version does not include private repositories, Dockerfiles,
arbitrary build commands, custom domains, teams, previews, scaling, scheduled
jobs, shell access, database consoles, or credential export.

## What appears in OpenStack

Setup uses one existing OpenStack project and one existing network. It does not
create a new OpenStack project or adopt unrelated resources that happen to have
a matching name.

A fresh deployment creates or selects:

- five private NixOS images: `admin`, `ingress`, `storage`, `worker`, and
  `builder`;
- three persistent servers: `admin`, `ingress`, and `storage`;
- one fixed Neutron port and address for each persistent server;
- security groups scoped to the operator, internal roles, and selected ingress
  mode;
- an admin-state volume, a managed-data volume, and a backup volume;
- SSH keypairs used only at the platform's controlled boundaries; and
- image and resource metadata that bind each object to the deployment and
  OpenStack project.

Names begin with the deployment prefix from the setup file. The exact generated
inventory is installed at
`/srv/openstack-platform/setup/config/platform.json` on the operator host.

The default fresh volume sizes are:

| Volume | Default | Contents |
| --- | ---: | --- |
| Admin state | 32 GiB | Controller, Nomad, and operator-helper state |
| Managed data | 500 GiB | PostgreSQL, MongoDB, Garage S3, and OCI registry data |
| Backups | 600 GiB | Encrypted logical backups and restore evidence |

The backup volume must be at least as large as the managed-data volume. The
selected flavors and volume type must exist in the target cloud. Setup can
choose the smallest visible flavor that meets a role baseline, but fixed
addresses are always explicit.

Workers are replaceable and hold no durable data. The future management
workflow will create one worker for each enabled application. Builders are
single-use machines: they receive one source snapshot, build it with rootless
BuildKit, push an immutable image, and are then deleted with their fixed port.

## How the roles fit together

- **Admin** runs the Nomad control plane, local application controller,
  constrained helper, monitoring, and backup coordination. Its state survives
  server replacement on attached volumes.
- **Ingress** runs Traefik and forwards public hostnames to healthy platform or
  application services. It stores no durable application data.
- **Storage** runs PostgreSQL, MongoDB, Garage-compatible S3, and the private OCI
  registry on the managed-data volume.
- **Worker** runs one application's Nomad allocation. It has no SSH service and
  no persistent disk.
- **Builder** performs one bounded build and expires. It is not an application
  runtime host.

The three persistent servers can be replaced without replacing their retained
volumes. A replacement is accepted only after identity and readiness checks;
the old server remains available until that decision is made.

## Why the deployment boundary is safer

The platform does not rely on one broad administrator process. It uses several
independent controls that limit what each component can do:

- **Exact project and resource identity.** Mutations require the authenticated
  OpenStack project UUID and name to match the private inventory. Existing
  resources are checked by UUID, metadata, network, address, image, flavor, and
  volume attachment before reuse. Ambiguous results stop for recovery instead
  of guessing.
- **Immutable, tested images.** Images are built from a complete Git commit,
  boot-tested, bound to signed release and artifact evidence, and selected by
  exact Glance UUID. A successful upload alone does not accept an image.
- **No baked-in deployment secrets.** Images contain services and non-secret
  site configuration. OpenStack credentials, SSH authorization, Nomad tokens,
  service credentials, tunnel tokens, and private PKI material arrive through
  protected first-boot or runtime paths rather than the Nix store.
- **Short-lived build authority.** Builders receive only the internal trust and
  registry access needed to build. They do not receive application runtime or
  managed-storage credentials, cannot use cloud metadata, and are removed after
  the build.
- **Restricted workloads.** Workers expose no SSH service and cannot use cloud
  metadata. Generated jobs reject privileged containers and host volumes and
  apply resource, capability, process, and log limits.
- **Closed public origin by default.** Tunnel mode binds ingress to loopback and
  opens no Neutron or host-firewall HTTP origin. Direct mode accepts only the
  exact IPv4 ranges published by the external HTTPS provider; `0.0.0.0/0` is
  rejected.
- **Local control API.** The application controller listens on Unix sockets,
  not a public TCP port. Linux peer credentials separate ordinary project
  operations from destructive operator operations. The future browser renderer
  will not be able to open either controller socket directly.
- **Typed application input.** The controller accepts a public,
  credential-free GitHub URL, exact commit, supported package scripts, runtime
  port and health path, and typed storage bindings. It rejects shell commands,
  Dockerfiles, host paths, provider IDs, and unknown fields.
- **Separated backups.** Hosted-controller state, external operator state, and
  managed data have different encrypted backup sets. Restore tools validate
  deployment identity, schema, integrity, and committed manifests before
  replacement.

These controls reduce credential exposure and make unexpected state fail
closed. They do not remove the need for a protected operator account, narrow
OpenStack credentials, reviewed release evidence, off-site key custody,
provider backups/retention, and regular recovery drills.

## Before deployment

Use an unprivileged account on an `x86_64-linux` host. It must own an existing
mode-`0700` `/srv/openstack-platform`; creating that directory and configuring
the account's user systemd manager are host-administration tasks. Setup refuses
root and `sudo`.

The host needs:

- Nix with flakes enabled and access to its daemon;
- uv and Python 3.14;
- an OpenStack CLI for the read-only preflight;
- Git, OpenSSH, OpenSSL, and curl;
- network access to OpenStack, GitHub, the Nix cache, OCI registries, and the
  three selected persistent addresses; and
- quota for five images, three persistent servers, disposable builders and
  workers, three fixed ports, and the selected Cinder volumes.

Use an otherwise empty OpenStack project or, at minimum, a unique prefix with no
existing resources using its reserved names. The current setup path builds and
boot-tests all five QCOW2 images locally. This is intentionally auditable but
can take substantial time and compute capacity; there is no supported prebuilt
image fast path yet.

From a clean checkout at the exact release commit:

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -q
nix flake check --no-build --print-build-logs
```

The Python suite must pass, and the Nix command must finish without an
evaluation failure.

## Prepare release evidence

Production setup requires two signed records before it creates a workspace or
contacts OpenStack:

1. a component manifest for the source commit, Python lock, implementation
   contract, helper actions, and schema/API versions; and
2. an artifact manifest for the five built QCOW2 files, their Nix closures,
   sizes, hashes, and intended publication metadata.

A release maintainer generates these records with an Ed25519 signing key held
outside the repository. The setup operator receives the manifests, detached
signatures, and public trust root as direct private files. See [Release and
platform maintenance](MAINTENANCE.md#release-evidence) for the exact generation
and verification commands.

Unsigned development evidence is accepted only with the explicit
`I_UNDERSTAND_THIS_IS_NOT_PRODUCTION` acknowledgement and is refused when the
environment is marked production.

## Create the protected setup file

Create a direct, current-user-owned mode-`0600` UTF-8 file. Setup parses
`KEY=value` or `export KEY=value` assignments without sourcing the file.
Duplicate keys and unsafe file permissions are refused.

A non-interactive password-authenticated setup has this shape:

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
PLATFORM_BACKUP_GIB='600'

PLATFORM_SOURCE_COMMIT='<full-lowercase-commit>'
PLATFORM_RELEASE_MANIFEST='/private/releases/<commit>/release-manifest.json'
PLATFORM_RELEASE_SIGNATURE='/private/releases/<commit>/release-manifest.sig'
PLATFORM_RELEASE_TRUST_ROOT='/private/release-trust-root.pem'
PLATFORM_ARTIFACT_MANIFEST='/private/releases/<commit>/artifacts/role-artifacts.json'
PLATFORM_ARTIFACT_SIGNATURE='/private/releases/<commit>/artifacts/role-artifacts.sig'
PLATFORM_ARTIFACT_TRUST_ROOT='/private/release-trust-root.pem'
```

Use `OS_AUTH_TYPE=v3applicationcredential` with
`OS_APPLICATION_CREDENTIAL_ID` and `OS_APPLICATION_CREDENTIAL_SECRET` instead
of username/password when the cloud supports application credentials.
`OS_PROJECT_ID` is optional, but setup always obtains the token's project UUID
and requires its canonical project name to match `OS_PROJECT_NAME`.

Non-secret values can be omitted in an interactive run when setup can discover,
default, or prompt for them. It never guesses a fixed address. Important
defaults are tunnel ingress, 32/500/600 GiB volumes, namespace as datacenter,
`global` as Nomad region, and `projects.<domain>` plus `compute.<domain>` as the
two recovery hostnames.

## Choose public ingress

The external provider owns public DNS and browser-trusted HTTPS. It must
preserve the original `Host` header and allow `/healthz`.

### Authenticated tunnel

Tunnel mode is the default. Traefik listens on `127.0.0.1:80`; Neutron and the
host firewall expose no public HTTP origin. For the reference Cloudflare Tunnel,
put only the existing tunnel token in a separate direct mode-`0600` file and
pass it during apply. Without a token, setup completes with ingress pending and
does not open an origin.

### Provider-restricted direct origin

Use direct mode only when the HTTPS provider publishes stable IPv4 origin
ranges:

```dotenv
PLATFORM_INGRESS_MODE='direct'
PLATFORM_PROVIDER_CIDRS='203.0.113.0/24,198.51.100.7/32'
```

The same exact ranges control Neutron, the NixOS firewall, and trusted forwarded
headers. Missing ranges, host bits, duplicates, IPv6, and `0.0.0.0/0` fail
before mutation. Public clients still connect to the external provider over
HTTPS; the provider connects to ingress port 80.

## Run the read-only check

Run the authenticated preflight before applying:

```bash
uv run openstack-platform setup check \
  --env-file /private/path/setup.env
```

Add `--json` for machine-readable output. The check authenticates, resolves the
project and provider choices, evaluates compute/network/Cinder/Glance quotas,
checks fixed addresses and reserved names, validates tooling and ingress, and
verifies the release sources. It performs provider reads only: it does not
create a workspace, generate credentials, build images, or mutate OpenStack.

Continue only when the result reports `setup-check=ready`. HTTPS is required for
the catalog-selected Glance usage endpoint by default. On a provider whose
Glance endpoint is intentionally HTTP-only, add this exact acknowledgement to
the protected setup environment:

```dotenv
PLATFORM_ALLOW_HTTP_GLANCE='I_UNDERSTAND_GLANCE_CREDENTIALS_USE_HTTP'
```

This permits the scoped token to cross that HTTP connection; it does not disable
certificate checks for HTTPS endpoints or enable redirects. A legacy Glance
that also returns `404` for its quota usage endpoint requires a second explicit
waiver:

```dotenv
PLATFORM_ALLOW_UNAVAILABLE_GLANCE_QUOTA='I_UNDERSTAND_GLANCE_QUOTA_IS_UNVERIFIED'
```

The check then labels both Glance quota dimensions
`unverified-legacy-provider`; it does not claim measured capacity. Existing
images without Glance `os_hash_*` fields are accepted only after setup downloads
them and verifies the signed QCOW2 SHA-256.

## Create the deployment

For Cloudflare Tunnel:

```bash
uv run openstack-platform setup \
  --env-file /private/path/setup.env \
  --cloudflare-token-file /private/path/cloudflare-tunnel-token \
  --apply
```

Omit the token option for another provider or to leave tunnel configuration
pending. Setup then:

1. verifies the source, release evidence, tools, and OpenStack project;
2. writes private inventory, policy, credentials, SSH identities, PKI, and
   service secrets;
3. creates security groups and reserves the three fixed ports;
4. builds, QEMU-boots, verifies, and publishes all five NixOS images;
5. creates admin and attaches its state and backup volumes;
6. establishes the pinned operator bridge and Nomad ACLs;
7. creates storage with its data volume, then creates ingress;
8. transfers controller-readable lifecycle credentials, installs matching
   operator/helper releases, seeds image selections, and starts the local controller;
9. selects the same five exact image UUIDs in operator state and initializes
   backup schedules; and
10. requires healthy role and controller observations before completion.

Successful output ends with:

```text
setup=complete project=<name> project-id=<uuid>
inventory=/srv/openstack-platform/setup/config/platform.json
public-ingress=cloudflare-configured
```

The final line reports `pending external provider configuration` when no tunnel
token was supplied.

## Verify the result

Run as the unprivileged owner of `/srv/openstack-platform`:

```bash
export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
export PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json
export SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config

$PLATFORM_CLI status
$PLATFORM_CLI infra list
```

The deployment should report five accepted role images and three available
persistent roles. A new controller has zero applications and zero managed
storage records.

Read `namespace` and `domain` from the installed inventory, then verify the
controller units and public path:

```bash
export PLATFORM_NAMESPACE=$(
  /srv/openstack-platform/runtime/python3.14 -c \
  'import json; print(json.load(open("/srv/openstack-platform/config/platform.json"))["namespace"])'
)
export PLATFORM_DOMAIN=$(
  /srv/openstack-platform/runtime/python3.14 -c \
  'import json; print(json.load(open("/srv/openstack-platform/config/platform.json"))["domain"])'
)

ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
  "$PLATFORM_NAMESPACE-controller.service" \
  "$PLATFORM_NAMESPACE-controller-readiness.service"
test "$(curl --fail --show-error --silent \
  "https://$PLATFORM_DOMAIN/healthz")" = OK
```

Both units must be active. The exact `OK` response verifies DNS, certificate
validation, provider forwarding, preserved host routing, and Traefik. It does
not imply that the future login or management UI exists.

Complete all three backup classes and their restore checks in [Operate and
recover a deployment](OPERATIONS.md#back-up-all-state-classes) before treating
the installation as recoverable.

## Resume a stopped setup

Keep the generated workspace and `/srv/openstack-platform/.secrets/setup`.
Correct the dependency named by the safe error, then rerun the identical
`setup ... --apply` command with the same environment and workspace.

Setup reuses generated private material and verifies every existing named
resource before continuing. Do not edit or delete a partially created server,
port, volume, keypair, image, database row, or secret to force progress. To
change project identity, fixed names, addresses, or volume sizes, use an empty
workspace and an empty provider resource scope.

For day-two health, backup, restore, replacement, and failure recovery, continue
with [Operate and recover a deployment](OPERATIONS.md).
