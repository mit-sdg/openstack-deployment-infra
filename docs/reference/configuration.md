# Configuration reference

Look up setup values, generated inventory, and operator configuration here. Follow [Deploy the platform](../guides/deploy-the-platform.md) for setup steps.

## Configuration at a glance

| File | Location | Written by | Sensitivity |
| --- | --- | --- | --- |
| [Setup file](#setup-file) | Operator host, mode `0600` | Operator | Secret |
| [Tunnel token](#cloudflare-tunnel-token-file) | Operator host, mode `0600` | Operator | Secret |
| Release evidence and trust root | Operator host | [Maintainer](../guides/releases-and-upgrades.md) | Integrity-critical |
| [Inventory](#generated-inventory) | Operator host and VMs | Setup | Private, no secrets |
| [Policy](#operator-policy) | Operator and admin hosts | Setup | Private |
| [Contract](#implementation-contract) | `infra/lib/platform_contract.json` | Source code | Public |

## Setup file

Pass the setup file to `openstack-platform setup` with `--env-file`.

### Format

| Rule | Detail |
| --- | --- |
| Syntax | `KEY='value'` or `export KEY='value'` per line; blanks and `#` comments skipped. |
| Parsing | Literal values; no shell execution or expansion. |
| Quoting | Use single quotes. An unquoted value that contains `$` or a backtick is ignored. |
| Other lines | Non-assignment OpenRC lines skipped. Unquoted empty/multi-word values rejected; quoted spaces and `''` accepted. |
| Duplicates | Refused. Each key may appear once. |
| File | A regular file (not a symlink), owned by you, mode exactly `0600`, UTF-8, at most 1 MiB. |
| Missing values | [Prompted](#prompts-and-defaults); answers aren't saved. |

OpenStack subprocesses inherit shell `OS_*` variables; file values take precedence. Required credentials are still prompted for when absent from the file. Avoid a shell loaded with another project's credentials.

### Prompts and defaults

Setup prompts for required login values, names, domain, network, operator CIDR, fixed addresses, flavors, and volume type. Passwords and credential secrets are typed without echo. Defaults appear in brackets; domain and fixed addresses have none. The tables below give defaults and limits.

Save inventory settings explicitly. Prompt answers aren't written back, and resumed setup must generate the same inventory. Other variables take their defaults without prompting.

### OpenStack login

Setup authenticates and stores the `OS_*` values it uses, except `OS_TOKEN`.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `OS_AUTH_URL` | Yes | | Keystone (identity) URL, for example `https://openstack.example.edu:5000/v3` |
| `OS_PROJECT_NAME` | Yes | | Must match the authenticated project name. |
| `OS_AUTH_TYPE` | No | `password` | Set `v3applicationcredential` to log in with an application credential |
| `OS_APPLICATION_CREDENTIAL_ID` | With application credentials | | Application credential ID |
| `OS_APPLICATION_CREDENTIAL_SECRET` | With application credentials | | Application credential secret |
| `OS_USERNAME` | With password login | | User name |
| `OS_PASSWORD` | With password login | | Password |
| `OS_USER_DOMAIN_NAME` | No | `Default` | The user's Keystone domain |
| `OS_PROJECT_DOMAIN_NAME` | No | `Default` | The project's Keystone domain |
| `OS_PROJECT_ID` | No | Taken from the login | Must match the authenticated project UUID. Skips a project lookup restricted credentials may be unable to make. Preserve provider spelling. |
| `OS_REGION_NAME` | No | | Selects one image service region; required for ambiguous catalogs. |
| `OS_INTERFACE` | No | `public` | Which endpoint interface to use |
| `OS_IDENTITY_API_VERSION` | No | `3` | Identity API version |

Any other `OS_*` variable passes through to the OpenStack tools unchanged.

> [!WARNING]
> The admin helper receives this credential to manage workers and builders. Restrict it to this project.

### Names

All four values are prompted for when omitted.

| Variable | Default | Rules | Use |
| --- | --- | --- | --- |
| `PLATFORM_PREFIX` | Lowercase project slug, hyphens for punctuation, cut to 32 characters | 2–32 lowercase letters, digits, hyphens; alphanumeric ends | OpenStack resource prefix |
| `PLATFORM_NAMESPACE` | Prefix | 3–32 lowercase letters, digits, hyphens; alphanumeric ends | Units, paths, metadata keys, sockets |
| `PLATFORM_DISPLAY_NAME` | Project name | 1–40 letters, digits, spaces, `.`, `_`, `-`; alphanumeric start, no trailing space | Portal brand and internal CA name |
| `PLATFORM_ORGANIZATION` | Display name | 1–64 characters, same rules | Certificate organization |

`setup check` doesn't validate display name or organization; apply does before cloud mutations. Set both explicitly if your project name doesn't qualify.

### Domain and hostnames

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `PLATFORM_DOMAIN` | Yes | | Lowercase base domain: portal at `<domain>`, apps at `<app-name>.<domain>`, S3 at `s3.<domain>`, all HTTPS. |
| `PLATFORM_RECOVERY_DOMAINS` | No | `projects.<domain>,compute.<domain>` | Exactly two comma-separated hostnames that ingress also routes to the portal |
| `PLATFORM_STORAGE_INTERNAL_NAME` | No | `storage.<prefix>.internal` | Private storage name. Keep default to match certificates. |
| `PLATFORM_OBJECT_STORAGE_INTERNAL_NAME` | No | `s3.<prefix>.internal` | Private S3 name. Keep default to match certificates. |
| `PLATFORM_DATACENTER` | No | The namespace | Nomad datacenter name |
| `PLATFORM_REGION` | No | `global` | Keep `global` to match Nomad certificates (`server.global.nomad`). |

### Network and addresses

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `PLATFORM_NETWORK` | Yes (prompted) | The only visible network | Existing Neutron network; must match exactly once. |
| `PLATFORM_OPERATOR_CIDR` | Yes (prompted) | Your SSH client's address | Admin SSH and ping source CIDR, without host bits. |
| `PLATFORM_ADMIN_ADDRESS` | Yes | | Fixed IPv4 address for the admin host |
| `PLATFORM_INGRESS_ADDRESS` | Yes | | Fixed IPv4 address for the ingress host. In direct mode, your HTTPS provider connects here on port 80. |
| `PLATFORM_STORAGE_ADDRESS` | Yes | | Fixed IPv4 address for the storage host |

Choose distinct, unallocated addresses, each within exactly one subnet of the network.

### Public ingress

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `PLATFORM_INGRESS_MODE` | No | `tunnel` | `tunnel`: outbound Cloudflare Tunnel. `direct`: provider HTTP to ingress port 80. |
| `PLATFORM_PROVIDER_CIDRS` | In direct mode | | Comma-separated provider IPv4 CIDRs; required in direct mode, refused in tunnel mode. No host bits, duplicates, IPv6, or `/0`. |
| `PLATFORM_STATIC_INGRESS_ROUTES_JSON` | No | `{}` | Named routes, e.g. `{"one-off": {"hostname": "one-off.apps.example.edu", "origin": "http://192.0.2.14:4444"}}`. Setup only checks the outer object. |

Provider CIDRs govern the ingress security group, host firewall, and trusted `X-Forwarded-*` headers.

### Machine sizes

| Variable | Required | Minimum | Meaning |
| --- | --- | --- | --- |
| `PLATFORM_ADMIN_FLAVOR` | Yes (prompted) | 2 vCPUs, 4,096 MiB | Flavor for the admin host |
| `PLATFORM_INGRESS_FLAVOR` | Yes (prompted) | 2 vCPUs, 2,048 MiB | Flavor for the ingress host |
| `PLATFORM_STORAGE_FLAVOR` | Yes (prompted) | 4 vCPUs, 8,192 MiB | Flavor for the storage host |
| `PLATFORM_WORKER_FLAVOR` | Yes (prompted) | 1 vCPU, 4,096 MiB | Flavor for app workers. Also the standard size for every app. |
| `PLATFORM_BUILDER_FLAVOR` | Yes (prompted) | 4 vCPUs, 8,192 MiB | Flavor for builders |

Names must match one visible flavor meeting the minimum. The prompt offers the smallest qualifying flavor, by RAM then vCPUs.

### Volumes

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `PLATFORM_VOLUME_TYPE` | Yes (prompted) | `production`, or the only type | Cinder volume type for all three volumes. Must match exactly one type. |
| `PLATFORM_ADMIN_STATE_GIB` | No | `32` | Size in GiB of `<prefix>-admin-state`: controller, Nomad, and helper state |
| `PLATFORM_DATA_GIB` | No | `500` | Size in GiB of `<prefix>-data`: PostgreSQL, MongoDB, Garage S3, and registry data |
| `PLATFORM_BACKUP_GIB` | No | `600` | Size in GiB of `<prefix>-backups`: encrypted backups. Must be at least `PLATFORM_DATA_GIB`. |

Sizes: integers 1–16,384 GiB, locked into deployment identity.

### App runtime images

Fallback images for apps without runtime version requests. Repositories requesting a version use its official image instead.

| Variable | Required | Default |
| --- | --- | --- |
| `PLATFORM_NODE_RUNTIME_IMAGE` | No | `docker.io/library/node@sha256:65932751ed4073ed02f5c04e494e4b2572a891b7dbea0568a863dc80341bf848` |
| `PLATFORM_BUN_RUNTIME_IMAGE` | No | `docker.io/oven/bun@sha256:621f249399228db47cf34611ee662585e77e015250ed29d5d0932b2d3282f0b0` |

Both must be pinned by digest (`<image>@sha256:<64 hex characters>`). Setup writes them into the [operator policy](#operator-policy).

### Release evidence

| Variable | Required | Meaning |
| --- | --- | --- |
| `PLATFORM_RELEASE_MANIFEST` | Yes | Path to `release-manifest.json`, the component manifest |
| `PLATFORM_RELEASE_SIGNATURE` | For signed releases | Path to `release-manifest.sig` |
| `PLATFORM_RELEASE_TRUST_ROOT` | For signed releases | Path to the Ed25519 public key (PEM) that verifies it |
| `PLATFORM_ARTIFACT_MANIFEST` | Yes | Path to `artifacts/role-artifacts.json`, the role-artifact manifest |
| `PLATFORM_ARTIFACT_SIGNATURE` | For signed releases | Path to `artifacts/role-artifacts.sig` |
| `PLATFORM_ARTIFACT_TRUST_ROOT` | For signed releases | Path to the public key that verifies it, usually the same file as `PLATFORM_RELEASE_TRUST_ROOT` |
| `PLATFORM_SOURCE_COMMIT` | No | Full 40-character lowercase commit. When absent, setup uses the checkout's `HEAD` and requires the checkout to be clean. |

Use absolute paths. Apply verifies both manifests before creating files or calling OpenStack; the read-only check authenticates first. Built images must match the artifact record. Existing Glance images are reused only after metadata and checksum checks; absent a Glance SHA-256, setup downloads and hashes the image.

#### Unsigned evidence

Unsigned evidence requires one exact acknowledgement:

| Variable | Exact value | Effect |
| --- | --- | --- |
| `PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT` | `I_UNDERSTAND_THIS_IS_NOT_PRODUCTION` | Accepts development evidence made with `--unsigned-development`. Refused when the environment variable `PLATFORM_ENVIRONMENT=production` is set. |
| `PLATFORM_ALLOW_UNSIGNED_PRODUCTION` | `I_ACCEPT_UNSIGNED_PRODUCTION_IMAGES` | Accepts production evidence made while signing was temporarily disabled. The signature and trust root variables must then be absent. |

Mutually exclusive; other values refused. See [Releases and upgrades](../guides/releases-and-upgrades.md).

### Provider acknowledgements

Older image services require these exact acknowledgements:

| Variable | Exact value | When you need it | Effect |
| --- | --- | --- | --- |
| `PLATFORM_ALLOW_HTTP_GLANCE` | `I_UNDERSTAND_GLANCE_CREDENTIALS_USE_HTTP` | The image service endpoint in your cloud's catalog uses `http://` | Allows token transmission over HTTP for quota reads. HTTPS verification and redirect restrictions remain. |
| `PLATFORM_ALLOW_UNAVAILABLE_GLANCE_QUOTA` | `I_UNDERSTAND_GLANCE_QUOTA_IS_UNVERIFIED` | Glance answers `404` for its quota usage endpoint | Reports image quotas as `unverified-legacy-provider`. Confirm capacity for five images yourself. |

## Cloudflare tunnel token file

Token options: `--cloudflare-token-file` for setup/check; `--cloudflare-tunnel-token-file` for ingress replacement.

| Rule | Detail |
| --- | --- |
| Contents | Base64 connector token, optionally one trailing newline. No `TUNNEL_TOKEN=` prefix or API token. |
| Checks | `setup check` checks file ownership and mode, not contents. Apply accepts at most 65,536 file bytes and requires at least 50 token characters without internal whitespace. Replacement requires a base64 connector token of at most 8,192 characters. |
| File | Direct regular file owned by you, mode `0600`, no symlink. Replacement also rejects hardlinks. |
| Mode | Only valid with `PLATFORM_INGRESS_MODE='tunnel'` for setup. `infra replace ingress` always needs it. |

Apply places the token at `/etc/<namespace>/secrets/cloudflared.env`, root-owned mode `0600`. Keep your local token file for replacements.

Configure Cloudflare routes for `<domain>`, `*.<domain>`, and extra hostnames to `http://127.0.0.1:80`.

## Generated inventory

The **inventory**, `platform.json`, describes project identity, topology, images, and pinned versions. It holds no secrets, but keep it private.

### Where it lives

| Copy | Path | Used by |
| --- | --- | --- |
| Setup workspace | `/srv/openstack-platform/setup/config/platform.json` | Setup; reruns stop on differences. |
| Operator | `/srv/openstack-platform/config/platform.json` | The `openstack-platform` tool on the operator host |
| Hosts | `/etc/<namespace>/platform.json` | All hosts; embedded in the image, read-only. |

Setup prints the workspace path on its `inventory=` line when it finishes.

### What it contains

Setup replaces these fields in `config/platform.example.json`; other fields are copied unchanged:

| Key | Contents | Comes from |
| --- | --- | --- |
| `project`, `projectId` | Project name and UUID | Your login, checked against `OS_PROJECT_NAME` |
| `displayName`, `organization` | Display and certificate organization names | `PLATFORM_DISPLAY_NAME`, `PLATFORM_ORGANIZATION` |
| `prefix`, `namespace` | Resource name prefix and internal namespace | `PLATFORM_PREFIX`, `PLATFORM_NAMESPACE` |
| `domain`, `recoveryDomains` | Base domain and the two extra hostnames | `PLATFORM_DOMAIN`, `PLATFORM_RECOVERY_DOMAINS` |
| `publicIngress` | `mode` and `providerCidrs` | `PLATFORM_INGRESS_MODE`, `PLATFORM_PROVIDER_CIDRS` |
| `staticIngressRoutes` | Extra fixed routes | `PLATFORM_STATIC_INGRESS_ROUTES_JSON` |
| `datacenter`, `region` | Nomad datacenter and region | `PLATFORM_DATACENTER`, `PLATFORM_REGION` |
| `network` | Network name | `PLATFORM_NETWORK` |
| `internalNames` | `storage` and `objectStorage` private names | `PLATFORM_STORAGE_INTERNAL_NAME`, `PLATFORM_OBJECT_STORAGE_INTERNAL_NAME` |
| `pki.internalCaFile` | `<namespace>-internal-ca.pem` | Derived |
| `operatorCidr` | Operator address range | `PLATFORM_OPERATOR_CIDR` |
| `addresses` | `admin`, `ingress`, `storage` fixed addresses | `PLATFORM_<ROLE>_ADDRESS` |
| `hosts` | `<prefix>-admin-01`, `<prefix>-ingress-01`, `<prefix>-storage-01` | Derived |
| `ports` | `<prefix>-admin-public-v4`, `<prefix>-ingress-public-v4`, `<prefix>-storage-service-v4` | Derived |
| `volumes` | `adminState`, `backup`, `data`: names, labels (≤12 characters), sizes, type | Derived, plus `PLATFORM_*_GIB` and `PLATFORM_VOLUME_TYPE` |
| `images` | `<prefix>-nixos-<role>-<first 8 characters of the commit>` for each role | Derived |
| `flavors` | One flavor name per role | `PLATFORM_<ROLE>_FLAVOR` |
| `paths` | `root`: `/srv/<namespace>`; `adminState`, `backups`, `data`: same path with `-state`, `-backups`, `-data` | Derived |

Copied unchanged from the example file:

| Key | Contents |
| --- | --- |
| `metadataAddress` | Cloud metadata address (`169.254.169.254`), blocked on workers and builders |
| `versions`, `checksums` | Pinned Nomad, Traefik, and BuildKit versions and their download SHA-256 |
| `containers` | Digest-pinned PostgreSQL, MongoDB, Garage, registry, and `cloudflared` images |
| `ownerPortal` | Owner portal settings, with the portal turned off. See [The owner portal section](#the-owner-portal-section). |

Role images embed this inventory. Give the maintainer your non-secret setup values, project UUID, and commit before they build deployment evidence.

### Fields that define the deployment's identity

The controller hashes these identity fields; changing them strands existing state:

`project`, `projectId`, `prefix`, `namespace`, `domain`, `recoveryDomains`, `datacenter`, `region`, `network`, `internalNames`, `addresses`, `hosts`, `ports`, `volumes`, `paths`, `pki`

Images, flavors, versions, checksums, and containers are excluded for upgrades.

### The owner portal section

`ownerPortal` starts disabled. See [Open the owner portal](../guides/open-the-portal.md) to enable sign-in and quotas.

| Field | Default | Limits | Meaning |
| --- | --- | --- | --- |
| `enabled` | `false` | | Whether the portal is on |
| `commonsOrigin` | `https://class.example.com` in setup template | Public HTTPS origin, no path | Set to your Commons site; required when enabled, including local-account deployments. |
| `identityEgressCidrs` | `[]` | Up to 64 ranges, no `/0` | Identity egress allowlist; empty uses Cloudflare ranges. |
| `classLabel` | `class account` | 1 to 80 characters | Completes the button text "Sign in with your `<classLabel>`" |
| `portalName` | "`<displayName>` Apps" | 1 to 80 characters | The portal's brand name |
| `appLimit` | `2` | 1 to 1000 | Default number of apps per owner |
| `concurrencyLimit` | `1` | 1 to 16 | Concurrent changes per owner |
| `absoluteSeconds` | `28800` (8 hours) | 60 to 86400 | Maximum owner session length |
| `idleSeconds` | `1800` (30 minutes) | 60 to 86400, at most `absoluteSeconds` | Owner session idle timeout |
| `anonymousOptionsPerMinute` | `600` | 1 to 100000 | Anonymous options requests per client address/minute |
| `anonymousStartsPerMinute` | `400` | 1 to 100000 | Commons sign-in starts per client address/minute |

### Read or validate an inventory

From the checkout, validate or read inventory:

```bash
PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json \
  /srv/openstack-platform/runtime/python3.14 infra/lib/platform_config.py validate
PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json \
  /srv/openstack-platform/runtime/python3.14 infra/lib/platform_config.py get namespace
```

`validate` prints `platform-config=valid`. `get` prints the value, as JSON for objects and lists.

## Operator policy

Setup writes the private app resource policy and backup public key from `config/platform-policy.example.json`.

| Copy | Path |
| --- | --- |
| Setup workspace | `/srv/openstack-platform/setup/config/platform-policy.json` |
| Operator | `/srv/openstack-platform/state/policy.json` |
| Admin host (operator staging copy) | `<paths.root>/persistent/policy.json` |
| Admin host (active controller policy) | `<paths.adminState>/controller/policy.json`, owned by `platform-controller` |

| Key | Value set by setup | Meaning |
| --- | --- | --- |
| `standard.workerFlavor` | `PLATFORM_WORKER_FLAVOR` | Standard app worker flavor |
| `standard.cpuMHz`, `standard.memoryMiB` | `1000`, `2048` | CPU and memory reserved for each app's container |
| `standard.postgresConnections` | `10` | Connection limit for each app's PostgreSQL database |
| `standard.postgresMeasuredBytes`, `standard.mongoMeasuredBytes` | 2 GiB each | Size target for each app's PostgreSQL and MongoDB database |
| `standard.s3Bytes`, `standard.s3Objects` | 5 GiB, `100000` | Quota for each app's S3 bucket |
| `runtimeImages.node`, `runtimeImages.bun` | `PLATFORM_NODE_RUNTIME_IMAGE`, `PLATFORM_BUN_RUNTIME_IMAGE` | Default app runtime images |
| `backupAgeRecipient` | Generated | Public key for `.secrets/setup/backup-age-identity.txt` under the operator root |

Operator copies must be operator-owned with no group or other access. Setup writes mode `0600`; the installed CLI requires that exact mode.

<details>
<summary>Optional <code>limits</code> block</summary>

Optional `limits` overrides these defaults. Setup omits it.

| Key | Default | Allowed range |
| --- | ---: | --- |
| `sourceBytes` | 52,428,800 | 1,048,576 to 1,073,741,824 |
| `dotenvBytes` | 262,144 | 1,024 to 4,194,304 |
| `environmentValueBytes` | 65,536 | 1 to 1,048,576 |
| `stderrBytes` | 262,144 | 1,024 to 4,194,304 |
| `buildLogBytes` | 10,485,760 | 1,024 to 104,857,600 |
| `helperRequestBytes` | 1,048,576 | 1,024 to 8,388,608 |
| `helperResponseBytes` | 1,048,576 | 1,024 to 8,388,608 |
| `connectSeconds` | 10 | 1 to 120 |
| `httpSeconds` | 30 | 1 to 300 |
| `processSeconds` | 900 | 1 to 7,200 |
| `helperSeconds` | 900 | 1 to 7,200 |
| `pollIntervalSeconds` | 2 | 1 to 30 |

`processSeconds` is also the operator tool's whole-command time limit.

</details>

## Implementation contract

`infra/lib/platform_contract.json` fixes roles, ports, accounts, installation paths, protocol markers, and inventory keys across Python, Nix, and shell.

It isn't a deployment setting. Changing it is a coordinated code release.

Ports from the contract, useful when reading security group rules:

| Name | Port | Used for |
| --- | ---: | --- |
| `ssh` | 22 | SSH |
| `http` | 80 | Traefik public entry point |
| `application`, `managementWeb` | 8080 | App containers on workers, and the portal's web service on admin |
| `traefikHealth` | 8082 | Traefik's local health endpoint |
| `nomadHttp`, `nomadRpc`, `nomadSerf` | 4646, 4647, 4648 | Nomad |
| `postgres` | 5432 | PostgreSQL |
| `mongodb` | 27017 | MongoDB |
| `garageS3` | 9000 | Garage S3 API |
| `garageRpc` | 3903 | Garage cluster RPC |
| `registry` | 5000 | Private image registry |

Also fixed: operator root `/srv/openstack-platform`, `platform.json`, `policy.json`, SSH alias `platform-admin`, account `agentops`.

## Files setup creates on the operator host

Everything lives under `/srv/openstack-platform`, except the user systemd units.

| Path | Contents |
| --- | --- |
| `setup/` | Inventory, policy, image IDs, Nix closure evidence |
| `config/platform.json` | The installed inventory |
| `state/` | Operator state, including `policy.json` and the operator database |
| `.secrets/openstack.env` | The `OS_*` values setup used. Secret. |
| `.secrets/ssh/` | `id_ed25519`, SSH `config`, pinned `known_hosts` |
| `.secrets/setup/` | Bootstrap secrets, recovery key `admin_nova_rsa` (Nova keypair), builder key, Nomad tokens, internal `pki/`, `backup-age-identity.txt`. Secret. |
| `runtime/` | Pinned Python 3.14.7 (`runtime/python3.14`) and uv 0.12.2 |
| `bin/` | `openstack-platform`, `openstack-platform-install-config`, `openstack-platform-restore`, `platform-openstack` (the OpenStack CLI with the stored credential), `uv`, `age` |
| `operator-releases/` | Per-commit releases; `current` selects one |
| `~/.config/systemd/user/openstack-platform-backup.service` and `.timer` | Daily operator backup, 02:45 UTC + up to 30 minutes random delay |

> [!IMPORTANT]
> Keep the controller/operator backup private key, `backup-age-identity.txt`, securely off host. See [Key custody](../guides/backups-and-recovery.md).

### How the operator tool finds its configuration

The installed `/srv/openstack-platform/bin/openstack-platform` always uses:

| Option | Fixed value |
| --- | --- |
| `--platform-config` | `/srv/openstack-platform/config/platform.json` |
| `--state-directory` | `/srv/openstack-platform/state` |
| `--policy` | `/srv/openstack-platform/state/policy.json` |

Other option values are refused. Required modes: `config/` `0700`, operator-owned inventory and policy `0600`, `bin/platform-openstack` `0500` or `0700`. Install updates with `openstack-platform-install-config --platform <FILE> --policy <FILE>`; see [Releases and upgrades](../guides/releases-and-upgrades.md).

## Other configuration files

| Template | Purpose | Where it's covered |
| --- | --- | --- |
| `config/offsite-export.example.json` | Where the admin host copies recovery bundles off site | [Backups and recovery](../guides/backups-and-recovery.md) |
| `config/live-acceptance-driver.example.json` | Inputs for a disposable live acceptance run | [Releases and upgrades](../guides/releases-and-upgrades.md) |

## Related

- [Plan a deployment](../guides/plan-a-deployment.md)
- [Deploy the platform](../guides/deploy-the-platform.md)
- [Operator CLI reference](operator-cli.md)
- [Internals reference](internals.md)
- [Open the owner portal](../guides/open-the-portal.md)
