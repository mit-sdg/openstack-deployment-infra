# Platform architecture

The platform hosts small HTTP applications in one OpenStack project. It keeps
persistent control and data services separate from replaceable workers and
single-use builders. The control database starts empty and accepts only state
created by this deployment.

In this documentation, an **OpenStack project** is the cloud tenant that owns infrastructure. An **application project** is one hosted application, its worker, deployments, and managed data.

![Platform architecture](architecture-overview.svg)

## Roles and state ownership

| Role | Lifetime | Responsibility | Persistent state |
| --- | --- | --- | --- |
| `admin` | persistent | Nomad control plane, constrained helper, monitoring, and backup staging | Nomad/helper state and backup volume |
| `ingress` | persistent | Discovers healthy Nomad services and routes public requests | No durable application state |
| `storage` | persistent | PostgreSQL, MongoDB, Garage object storage, and OCI registry | Managed-data volume |
| `worker` | replaceable | Runs one application's allocation | None |
| `builder` | single use | Builds one source snapshot with rootless BuildKit | None |

All role images use one private `config/platform.json`, which provides the deployment identity, resource names, addresses, versions, image names, volume labels, and paths. The private operator policy supplies the standard worker and managed-storage profile and runtime image digests.

## Fresh deployment flow

The operator reconciles the configured foundation, boots and verifies the
three persistent roles, then installs matching management and helper releases.
The first `openstack-platform status` invocation creates the empty SQLite
schema. Its marker is bound to the deployment project UUID, namespace, and
stable inventory identity. There is no import phase. Reconciliation reads the
configured resources and accepted image candidates to confirm what exists; it
never writes a record it did not create. The platform rejects a copied database
or restore from another deployment. Normal image, flavor, version, checksum,
and container upgrades do not change the marker.

For an application deployment, the control surface:

1. validates a public GitHub repository, full source commit, and `platform.yaml`;
2. asks the constrained helper to create a single-use builder;
3. transfers a generated recipe and exact source snapshot, then records the pushed digest;
4. deletes and verifies the builder and its fixed port;
5. creates or verifies the application's dedicated worker;
6. submits the constrained Nomad job; and
7. accepts it only after scheduler and public route health pass.

A failed candidate is removed with bounded cleanup and its registry manifest is cleaned up without rebuilding source. Deployment work, including lock acquisition, source transfer, builder execution/cleanup, helper calls, and health checks, shares one policy-bounded whole-operation deadline. The builder receives the same wall-clock deadline, so cleanup or BuildKit cannot run indefinitely. There is no general operator command for rolling back an already accepted Nomad job.

## Isolation boundaries

Participants do not receive SSH, OpenStack, Nomad, registry, or database-admin credentials. The staff interface accepts validated slugs, source selections, and manifest values, not shell text, provider IDs, host paths, or arbitrary Nomad jobs.

Builders receive registry push access and the internal CA, but not application runtime or managed-service credentials. Builders deny metadata access, expire automatically, and are unconditionally deleted after success, failure, cancellation, or timeout.

Workers have no SSH service, deny metadata access, and accept application traffic only from ingress. The generated job blocks privileged containers and host volumes, uses a read-only root by default, and applies CPU, memory, PID, capability, and log limits. Deleting a worker does not delete managed data or deployment history.

## Persistent data and backups

The admin state volume holds Nomad, helper, and control-plane state. The storage volume holds managed services and registry data. The backup volume holds encrypted logical backups. Managed-service credentials are scoped per application and synchronized through owner-specific Nomad Variable keys; credential values never enter management SQLite.

The management database is backed up separately from PostgreSQL, MongoDB, and Garage. Each backup is encrypted to the policy's age recipient and written under the configured backup root.

A backup counts as accepted only once its manifest exists. The ciphertext and checksum are fsynced before the manifest is renamed into place, so an interrupted run leaves partial files that the next run cleans up rather than mistaking for a real backup. Retention only ever deletes complete sets.

Restore is offline and contacts no provider. It unpacks the backup into a
private temporary file and checks it before touching the database. It refuses a
backup from another deployment, a corrupt or unfinished backup, or one written
by a newer version. Only then does it atomically replace the database. Live
state must be reconciled afterward.

Managed data is checked separately. That check runs on admin in throwaway containers and records evidence only after the restored contents match their checksums. Registry blobs are not backed up at all; they are rebuilt from source.

## Public ingress

A public service supplies DNS and HTTPS, forwards to ingress port 80, preserves the original `Host`, and allows health paths. Traefik uses the hostname to select a healthy Nomad service. Cloudflare Tunnel is the reference provider, not a requirement; the complete provider-neutral contract is in [PUBLIC_INGRESS.md](PUBLIC_INGRESS.md).

## Capacity and application policy

The platform runs one allocation per application. Applications use Bun or Node, one HTTP port, and one health path. Generated recipes select digest-pinned runtime images and set `NODE_ENV=production`. One private standard profile supplies worker flavor, 1,000 scheduler CPU MHz, memory, PostgreSQL connections, monitored database targets, and S3 quotas. PostgreSQL and MongoDB measured-byte values are targets rather than hard quotas, and no periodic usage measurement is installed.

## Failure and recovery boundaries

- A Nix build or Glance upload produces a candidate; live role checks are required for acceptance.
- Builder cleanup is required on both success and failure. An ambiguous provider result is recovery-required, not permission to guess.
- Failed application candidates are removed with bounded cleanup; accepted deployments have no manual job-history rollback command.
- Worker replacement leaves managed data intact.
- Persistent-role replacement retains the prior host and volumes until readiness passes, and accepts a replacement only after exact image UUID, retained flavor UUID, configured name, and operation provenance are re-read from the provider.
- Destructive storage/application cleanup requires explicit confirmation and absence evidence.
- Management and helper releases are selected atomically; keep a complete prior release for executable recovery, not as a database or provider migration source.

Use [OPERATIONS.md](OPERATIONS.md) for actions and [CONTROL_PLANE_CONTRACT.md](CONTROL_PLANE_CONTRACT.md) for exact command behavior.
