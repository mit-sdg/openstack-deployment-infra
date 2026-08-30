# Deployment configuration reference

This reference defines the infrastructure inventory for a new deployment. Use
it with empty operator state; it is not a state-import format. No external
rows are copied into the database, and only resources named here are touched.
It does not configure an application build. Future application configuration
is a typed management-owned snapshot sent to the local controller API; the
source repository does not supply platform configuration.

Role-image builds and repository tools use `config/platform.json` from the
operator's private repository. The installed command reads its persistent copy
at `/srv/openstack-platform/config/platform.json`, outside immutable release
archives. The file contains deployment names and other non-secret settings.
Start from
[`config/platform.example.json`](../config/platform.example.json); do not add
credentials or private keys.

Implementation-level roles, service ports, system accounts, protocol metadata,
and required inventory paths are not copied into each deployment inventory.
They are defined once in `infra/lib/platform_contract.json`, validated in CI,
and consumed directly by Python, standalone infrastructure scripts, and Nix.
Changing that versioned contract is a coordinated platform release, not an
operator configuration edit.

## Select a configuration

Repository tools use `PLATFORM_CONFIG` when it is set. Set it explicitly in
each source-checkout session so the selected deployment is clear:

```bash
export PLATFORM_CONFIG="$PWD/config/platform.json"
```

When `PLATFORM_CONFIG` is unset, Nix uses the committed example. A file selected
through the environment is outside the flake source, so Nix commands that use
it must include `--impure`:

```bash
nix flake check --impure --no-build
nix build --impure .#builder-image
```

When `PLATFORM_CONFIG` is unset, infrastructure scripts first look for installed
host copies, then fall back to `config/platform.json`. The installed
`/srv/openstack-platform/bin/openstack-platform` launcher instead pins
`/srv/openstack-platform/config/platform.json`; use the unprivileged installation procedure in
[`RELEASE_INSTALLER.md`](RELEASE_INSTALLER.md) to update that direct mode-`0600`
file and private policy atomically without printing configuration values.
Guest services and authenticated health checks use the guest copy at
`/etc/<namespace>/platform.json`. Management-to-admin health commands propagate
`PLATFORM_CONFIG=/etc/<namespace>/platform.json`; the storage checker is run
through a fixed Python wrapper that sets this value before importing the checker.
This prevents a checkout or management-host config from being used on a guest.

## Top-level fields

| Field | Meaning |
| --- | --- |
| `project` | OpenStack project name. Lifecycle scripts refuse to run when `OS_PROJECT_NAME` differs. |
| `projectId` | Stable canonical lowercase OpenStack project UUID. Mutations require it to match the authenticated token; provider helpers normalize compact provider output before comparison. |
| `displayName` | Human-readable platform name used in service descriptions, registry authentication, and the internal CA name. |
| `organization` | Organization written to internal CA and leaf-certificate subjects. |
| `prefix` | Prefix for OpenStack servers, ports, images, volumes, security groups, and related provider resources. |
| `namespace` | Internal name used for services, paths, scheduler metadata, readiness markers, and container names. |
| `domain` | Base hostname used to form application URLs as `<slug>.<domain>`. |
| `recoveryDomains` | Two additional hostnames routed to the platform service. Current scripts read the first two entries. |
| `publicIngress` | Required strict origin policy: authenticated `tunnel`, or `direct` with exact provider IPv4 CIDRs. |
| `staticIngressRoutes` | Optional named routes from exact public hostnames to trusted HTTP origins outside Nomad. |
| `datacenter` | Nomad datacenter name. |
| `region` | Nomad region name. |
| `network` | OpenStack network used for persistent and disposable hosts. |
| `internalNames` | Internal DNS names for storage and object storage. |
| `pki` | Internal CA filename. |
| `operatorCidr` | CIDR allowed to reach operator interfaces such as admin SSH. |
| `metadataAddress` | Cloud metadata address blocked on builders and workers. |
| `addresses` | Fixed addresses for the persistent roles. |
| `hosts` | Server names for the persistent roles. |
| `ports` | Fixed Neutron port names for the persistent roles. |
| `volumes` | Persistent volume names, labels, sizes, and types. |
| `images` | Configured Glance image names used by role lifecycle/bootstrap scripts. The control surface selects an accepted published image by its exact provider UUID. |
| `flavors` | OpenStack flavor names for persistent roles and builders. New application workers use `standard.workerFlavor` from the private policy. |
| `versions` | Nomad, Traefik, and BuildKit versions packaged in role images. |
| `checksums` | SHA-256 checksums for downloaded Nomad, Traefik, and BuildKit archives; these are separate from the provider checksum verified when publishing a QCOW2. |
| `containers` | Digest-pinned container images used by storage and ingress. |
| `paths` | Persistent and runtime paths mounted or used by role services. The configured `backups` path holds separate hosted-controller, operator-state, and managed-data backup sets. |

The controller database is deployment-bound. Its deployment marker hashes the
project UUID, namespace, and a stable inventory projection containing resource
names, network/address identity, volumes, paths, and PKI naming. Image, flavor,
version, checksum, and container selections are intentionally excluded so
normal image/release upgrades do not strand state. A copied database or restore
candidate with a different stable identity is rejected before it can be used;
changing those identity fields means starting from a new database, not copying
an existing one.

The loader checks required paths and the strict public-ingress policy in
addition to top-level groups, names, `namespace`, and the internal CA filename.
Keep the remaining nested structure shown in the example. Run the checks at the
end of this page after every change.

## Private operator policy

`config/platform-policy.json` is installed separately from the inventory and
must be a direct current-user-owned mode-`0600` file. It contains:

- `standard`, including worker flavor and managed-service targets;
- `runtimeImages.bun` and `runtimeImages.node`, each pinned by OCI digest; and
- `backupAgeRecipient`, a public `age1...` recipient used to encrypt the hosted-controller and external operator-state SQLite backups.

The recipient is not an age identity. Keep the matching
`AGE-SECRET-KEY-...` file in operator custody, outside Git and the management
database. The operator runtime bootstrap validates and exposes a protected
age executable at `/srv/openstack-platform/bin/age`. The admin NixOS role
separately provides `<paths.root>/bin/age` and
`<paths.root>/bin/age-keygen` for managed-data backups. Neither backup process
prints or transfers an age identity.

## Naming

### Display and organization names

`displayName` and `organization` accept 1–40 and 1–64 characters,
respectively. Use ASCII letters, numbers, spaces, periods, underscores, and
hyphens; start with a letter or number and do not end with a space. These values
are embedded in certificate subjects, so changing them does not alter
certificates that have already been issued.

### `prefix`

`prefix` identifies external provider resources. Changing it points lifecycle
commands at a different resource set; it does not rename existing resources.

Choose a short value unique within the OpenStack project. The configuration
loader does not enforce a character pattern, but derived names must be valid for
OpenStack and the services that use them.

### `namespace`

`namespace` identifies local implementation resources. It must contain 3–32
lowercase letters, numbers, or hyphens, start and end with a letter or number,
and contain no other characters.

The namespace is used in:

- service and container names;
- local configuration and secret directories;
- Nomad node classes, metadata, and service tags;
- runtime and persistent paths; and
- readiness markers.

Every image and control-plane component in a deployment must use the same
namespace. Changing it selects a new resource and state namespace; it does not
copy state from another namespace.

### Application slugs

Application slugs are separate from `prefix` and `namespace`. The implemented
local controller accepts 3–40 lowercase letters, numbers, or hyphens. A slug
must start with a letter, end with a letter or number, cannot contain
consecutive hyphens, and cannot use controller-reserved names such as `admin`,
`api`, `auth`, `status`, or `www`.

There is no operator CLI for creating slugs. The future management application
will own user authorization and submit an accepted slug to the controller; see
[MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md).

## Host and network fields

`addresses`, `hosts`, and `ports` use these fixed keys:

```json
{
  "addresses": {
    "admin": "192.0.2.11",
    "ingress": "192.0.2.12",
    "storage": "192.0.2.13"
  },
  "hosts": {
    "admin": "example-admin-01",
    "ingress": "example-ingress-01",
    "storage": "example-storage-01"
  },
  "ports": {
    "admin": "example-admin-public-v4",
    "ingress": "example-ingress-public-v4",
    "storage": "example-storage-service-v4"
  }
}
```

Every address must belong to the configured OpenStack network. Foundation
reconciliation rejects an existing port if it is on the wrong network or lacks
its configured address.

`operatorCidr` sets the source CIDR allowed by the generated admin security
group. Set it to the narrowest operator network that needs access.

`metadataAddress` is normally `169.254.169.254`. Change it only if the target
cloud exposes metadata at another address.

## Public ingress policy

`publicIngress` is required:

```json
{
  "publicIngress": {
    "mode": "tunnel",
    "providerCidrs": []
  }
}
```

`tunnel` is the secure setup default and requires an empty CIDR list. It binds
Traefik to loopback and creates no Neutron or host-firewall HTTP/HTTPS opening.
`direct` requires one or more canonical, unique IPv4 networks; IPv6, host bits,
duplicates, an empty list, and `0.0.0.0/0` are rejected before provider
mutation. Direct mode admits port 80 only from those exact sources at both
network boundaries. See [`PUBLIC_INGRESS.md`](PUBLIC_INGRESS.md) for provider
behavior and migration order.

## Public hostnames

The local controller's job renderer uses `domain` as the hostname suffix:

```text
<application-slug>.<domain>
```

The public ingress service must eventually provide DNS and HTTPS for these
names and forward the original `Host` header to ingress. It can use wildcard
DNS and a wildcard certificate, or provision each project hostname separately.
Current setup verifies only the platform-level health route because the
management and authentication applications do not exist yet.

`recoveryDomains` contains two additional hostnames for the platform-level
route. They are not alternate suffixes for application slugs.

`staticIngressRoutes` routes one-off services that are not registered in Nomad.
Each key becomes an internal Traefik route name; `hostname` is matched exactly,
and `origin` is the HTTP URL reachable from the ingress host:

```json
{
  "staticIngressRoutes": {
    "one-off": {
      "hostname": "one-off.apps.example.com",
      "origin": "http://192.0.2.14:4444"
    }
  }
}
```

Restrict the origin's security group to ingress, and ensure the origin accepts
the public hostname. Traefik preserves the original `Host` header and supports
WebSocket upgrades on these routes.

See [`PUBLIC_INGRESS.md`](PUBLIC_INGRESS.md) for the complete provider contract.

## Persistent volumes and paths

Each volume entry has this structure:

```json
{
  "name": "example-data",
  "label": "example-data",
  "sizeGiB": 200,
  "type": "production"
}
```

`volumes` has three fixed entries:

| Entry | Attached to | Contents |
| --- | --- | --- |
| `volumes.adminState` | admin | Nomad and controller state |
| `volumes.backup` | admin | Encrypted logical backups |
| `volumes.data` | storage | Databases, object storage, and registry data |

Role images mount filesystems by `label`. Host lifecycle scripts create or
attach volumes by `name` and check `sizeGiB`. The current shell export applies
`volumes.data.type` to lifecycle operations for all three volumes, so keep their
`type` values consistent.

`paths` contains:

| Entry | Purpose |
| --- | --- |
| `root` | Installed control tools, runtime secrets, status, and convenience links |
| `adminState` | Mount point for admin and scheduler state |
| `backups` | Mount point for encrypted backup output |
| `data` | Mount point for managed data services and registry data |

Path values must match the corresponding volume labels and the namespace used
by the images. Changing a path does not move existing data.

Backup placement is derived from `paths.backups`, not from a hard-coded host
path:

- The external operator CLI stages uploads at `<paths.backups>/controller/.staging/`
  and publishes its encrypted operator-state SQLite evidence under
  `<paths.backups>/controller/`.
- The admin-hosted controller independently publishes its live SQLite evidence
  under `<paths.backups>/hosted-controller/`. These two SQLite backup sets are
  not interchangeable. In each set the manifest is the commit marker;
  ciphertext and checksum are durable before its final rename.
- The admin managed-data scripts write timestamped directories at
  `<paths.backups>/<namespace>/`.

The policy's `backupAgeRecipient` encrypts both SQLite backup sets, on their
respective hosts. The matching private age identity is not configuration and
must remain in operator custody. The admin managed-data scripts use the packaged
`<paths.root>/bin/age` and `<paths.root>/bin/age-keygen` with their separate
`<paths.root>/persistent/secrets/backup-age-key.txt` identity.

### Persistent off-site export configuration

`config/offsite-export.example.json` is a separate, credential-free admin-host
configuration. Install its private copy at
`<paths.adminState>/operator/offsite-export.json`, owned by `agentops` and mode
`0600`. It is not part of `platform.json` because the operator chooses and
mounts the off-site provider after deployment.

| Entry | Contract |
| --- | --- |
| `format` | Exactly `openstack-platform-offsite-export-config-v1` |
| `destination` | Canonical absolute path of the exact mode-`0700`, `agentops`-owned mount point |
| `mountSource` | Exact source reported for that mount by `/proc/self/mountinfo`/`findmnt` |
| `filesystemType` | Exact mounted filesystem type |
| `limits.maximumFileBytes` | Streaming per-file limit, 1 GiB through 4 TiB |
| `limits.maximumTotalBytes` | Streaming bundle limit, at least the file limit and at most 8 TiB |
| `maximumReceiptAgeHours` | Health/status freshness limit, 1 through 168 hours |

The destination must have a different device identity from `paths.backups` and
must not contain, or be contained by, the local backup path. Configuration does
not mount storage or carry a provider token. See [Operations](OPERATIONS.md#export-encrypted-recovery-evidence-off-site)
for installation, timer, and status commands.

## Images, versions, and checksums

`images` must define `admin`, `ingress`, `storage`, `worker`, and `builder`.
Lifecycle/bootstrap scripts use these values as Glance image names. Published
candidates are not renamed or manually promoted: after role acceptance, the
control surface selects the exact tested Glance UUID. A rebuilt image is not
equivalent to the tested candidate even when it has the same name.

`versions` and `checksums` select downloaded Nomad, Traefik, and BuildKit
artifacts. Update a version and its checksum together. Nix will reject a
checksum mismatch during the build.

Every value in `containers` is an OCI image reference. The example pins each
reference by `sha256` digest. Keep this digest pinning in deployment
configurations so rebuilding a role cannot select a different container under
the same tag.

## Internal PKI filename

`pki.internalCaFile` is the CA certificate filename in the private PKI
directory. It must be a plain filename ending in `.pem`; paths and parent
directory references are rejected. Guests install the certificate as
`internal-ca.pem` under the active namespace.

The configuration contains only the filename. Keep the certificate and private
key outside version control and the Nix store.

## Validate changes

From the repository root, use the locked Python 3.14/uv environment for
project checks:

```bash
export PLATFORM_CONFIG="$PWD/config/platform.json"
uv sync --frozen
uv run python infra/lib/platform_config.py get project
nix flake check --impure --no-build --print-build-logs
uv run python -m unittest discover -s tests -v
```

The Python command checks that the file loads. The flake command evaluates all
five role configurations. Neither command tests a live role. For an
authenticated provider check from the repository root, source the protected
wrapper and use
`verify_openstack_project` from `infra/lib/platform-config.sh`; do not compare
compact and canonical UUID strings literally. Follow
[`../nix/README.md`](../nix/README.md) before publishing or selecting an image.
