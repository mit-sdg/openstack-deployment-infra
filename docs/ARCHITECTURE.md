# Platform architecture

The platform infrastructure hosts small HTTP applications in one OpenStack
project. Persistent control and data services are separate from replaceable
workers and single-use builders. The implemented operator surface manages the
infrastructure; the implemented local controller owns product lifecycle logic.
The browser management and authentication applications do not exist yet.

An **OpenStack project** is the cloud tenant that owns infrastructure. An
**application project** is a future user-facing record containing one hosted
application, its worker, deployments, and managed data.

![Platform architecture](architecture-overview.svg)

## Roles and state ownership

| Role | Lifetime | Responsibility | Persistent state |
| --- | --- | --- | --- |
| `admin` | persistent | Nomad control plane, local controller, constrained helper, monitoring, and backup staging | Controller/Nomad state and backup volume |
| `ingress` | persistent | Routes platform and healthy Nomad services | No durable application state |
| `storage` | persistent | PostgreSQL, MongoDB, Garage object storage, and OCI registry | Managed-data volume |
| `worker` | replaceable | Runs one application's allocation | None |
| `builder` | single use | Builds one source snapshot with rootless BuildKit | None |

All role images use one private `config/platform.json`, which contains
deployment identity, resource names, addresses, versions, image names, volume
labels, and paths. A separate private operator policy supplies the standard
worker/storage profile, runtime image digests, and management-backup age
recipient.

## Current control surfaces

`openstack-platform` is an operator-only interface for setup, status, image
selection/pruning, persistent-host lifecycle, management-state backup, and
offline restore. It has no product commands. The CLI database still reports
aggregate application and storage counts so an operator can detect restored or
pre-cutover state without exposing product mutation paths.

`openstack-platform-controller` implements bounded HTTP/1.1 JSON over a Unix
socket. Its services own application declarations, typed deployment snapshots,
environment metadata, managed-storage lifecycle, operation journals, safe
reads, and destructive cleanup. The controller invokes a fixed local
constrained helper. It does not authenticate users or authorize project/admin
access; socket access is the entire implemented transport boundary.

The admin NixOS role runs that executable under the dedicated trusted
`platform-controller` account after the retained state mount, Nomad, controller
policy, and helper release are available. The sync-engine management
application, browser sessions, quota/ownership model, and external
authentication application remain future work. Their intended boundary is
specified in [MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md).

## Fresh deployment flow

The operator reconciles the configured foundation, boots and verifies the
three persistent roles, and installs matching operator and helper releases.
The first `openstack-platform status` invocation creates an empty SQLite schema
bound to the deployment project UUID, namespace, and stable inventory identity.
There is no import phase. Reconciliation confirms only configured resources and
accepted image candidates; it never writes a provider row as accepted product
state. A copied database or restore from another deployment is rejected.
Normal image, flavor, version, checksum, and container upgrades do not change
the database identity.

A future management application will submit product intent and immutable typed
configuration to the local controller. The repository source contributes code,
`package.json`, and supported lockfiles; it does not control platform
configuration. For a deployment, the controller:

1. validates the application, public credential-free GitHub URL, requested ref,
   exact commit, configuration revision, and typed snapshot;
2. asks the constrained helper to create a single-use builder;
3. transfers a generated recipe and exact source snapshot, then records the
   pushed immutable digest;
4. deletes and verifies the builder and fixed port;
5. creates or verifies the application's dedicated worker;
6. submits the constrained Nomad job; and
7. accepts only after scheduler, application, and public-route health pass.

A failed candidate is removed with bounded cleanup while the prior accepted
route is preserved. Deployment work shares one policy-bounded whole-operation
deadline. There is no general operator command for rolling back or otherwise
mutating product state.

## Isolation boundaries

End users receive no SSH, OpenStack, Nomad, registry, or database-administrator
credentials. The future management backend sends typed product inputs, not
shell text, host paths, arbitrary Nomad jobs, or provider-selected IDs.

Builders receive registry push access and the internal CA, but not application
runtime or managed-service credentials. Builders deny metadata access, expire
automatically, and are deleted after success, failure, cancellation, or
timeout.

Workers have no SSH service, deny metadata access, and accept application
traffic only from ingress. Generated jobs block privileged containers and host
volumes, use a read-only root by default, and apply CPU, memory, PID,
capability, and log limits. Deleting a worker does not delete managed data or
deployment history.

The reserved `management-web` account has access only to its future state and
the controller socket. It must not read controller SQLite, OpenStack
credentials, Nomad tokens, provider administrator credentials, builder keys,
backup keys, age identities, diagnostics, or build logs directly.

## Persistent data and backups

The admin state volume holds controller, Nomad, operator-helper, and diagnostic
state. The storage volume holds
managed services and registry data. The backup volume holds encrypted logical
backups. Managed-service credentials are scoped per application and
synchronized through owner-specific Nomad Variable keys; values never enter
controller SQLite.

The hosted controller SQLite database is backed up separately from PostgreSQL,
MongoDB, and Garage. This boundary also applies during future management
integration and state cutover. Each backup is encrypted to its configured age recipient and
written below the configured backup root. A management backup counts as
accepted only when its ciphertext, checksum, and final manifest exist. The
manifest is the commit marker, so interrupted partial files are not treated as
backups. Restore is offline, checks deployment identity/schema/integrity before
replacement, and contacts no provider.

Managed-data restore checks run on admin in throwaway containers and record
evidence only after restored contents match checksums. Registry blobs are not
backed up; they are rebuilt from source.

## Public ingress

An external service supplies DNS and HTTPS, forwards to ingress port 80,
preserves the original `Host`, and allows the platform health route. Traefik
uses the hostname to select a static platform route or a healthy Nomad service.
Cloudflare Tunnel is the reference, not a requirement. See
[PUBLIC_INGRESS.md](PUBLIC_INGRESS.md).

## Capacity and policy

The controller supports one allocation per application, one HTTP port, one
health path, and Node or Bun builds from typed configuration. Generated recipes
select digest-pinned runtime images and set `NODE_ENV=production`. One standard
policy profile supplies worker flavor, scheduler CPU/memory, PostgreSQL
connections, monitored database targets, and S3 quotas. PostgreSQL and MongoDB
measured-byte values are targets rather than hard quotas; no periodic usage
measurement is installed.

## Failure and recovery boundaries

- A Nix build or Glance upload produces a candidate; live role checks are
  required for acceptance.
- Builder cleanup is required on success and failure. An ambiguous provider
  result is recovery-required, not permission to guess.
- Worker replacement leaves managed data intact.
- Persistent-role replacement retains the prior host and volumes until exact
  identity and readiness checks pass.
- Product mutations are absent from the operator CLI. Do not recover old
  product operations by using an older binary.
- Operator/helper releases are selected atomically; a prior release is an
  executable recovery aid, not a database/provider rollback mechanism.

Use [OPERATIONS.md](OPERATIONS.md) for supported operator actions and
[CONTROL_PLANE_CONTRACT.md](CONTROL_PLANE_CONTRACT.md) for exact CLI and local
API contracts.
