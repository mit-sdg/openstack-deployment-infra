# A small application platform for OpenStack

This repository builds a NixOS-based platform for hosting small HTTP
applications inside one OpenStack project. It creates the cloud foundation,
five machine roles, PostgreSQL/MongoDB/S3 services, private image registry,
public ingress, backups, recovery tooling, an operator CLI, and a local
application controller.

## Current status

Infrastructure deployment and operation are implemented. The owner portal is
implemented but not deployed, so there is not yet a supported workflow for
application owners on a live platform. Students will sign in with their class
username/password, checked server-side with Commons; the merged authenticate
endpoint must be configured and pass live acceptance before availability is
claimed. Owner sessions expire after 8 h absolute or 30 min idle. Allowlisted
instructors and TAs can re-enter their credentials for a separate read-only staff
view of the broker-known catalog; staff sessions expire after 1 h or 10 min idle.
Commons password changes/archiving do not revoke issued sessions. Enrollment,
quota changes and revocation remain recovery-console operations; the operator
dashboard and privileged platform data remain separate. See
[owner portal operations](docs/OPERATIONS.md#owner-portal-operations).

Today, an operator can create and recover a platform with:

- persistent admin, ingress, and storage hosts;
- replaceable application workers and single-use builders;
- exact-image and persistent-host lifecycle controls;
- a read-only operator dashboard for role, application, operation, and
  platform-health status;
- separate encrypted backups for controller, operator, broker identity/ownership
  state, and managed data, including retained application images; and
- a local controller and locally tested owner portal with isolated identity checks.

The implemented local application workflow supports public, credential-free GitHub
repositories and typed Node or Bun configuration. Users will not receive SSH,
OpenStack, Nomad, registry, or database-administrator credentials. Private
repositories, arbitrary build commands, and Dockerfiles are outside the current
contract.

## Documentation

- [Deploy an application with curl](docs/APPLICATION_DEPLOYMENTS.md) — operator
  deployment, maintenance cutover, recovery, and rollback without the management UI.

- [Deploy the platform](docs/DEPLOYMENT.md) — what the platform creates, what it
  supports, its security model, setup, ingress, and verification.
- [Operate and recover it](docs/OPERATIONS.md) — health, the read-only
  dashboard, backups, off-site export, restore, host replacement, pruning, and
  troubleshooting.
- [Platform internals](docs/INTERNALS.md) — component ownership, state,
  controller/helper boundaries, internal API, and the owner portal.
- [Release and platform maintenance](docs/MAINTENANCE.md) — signed releases,
  role images, publication, installation, and live acceptance.
- [Development workflow](docs/DEVELOPMENT.md) — local environment and checks.
- [Tracked-file guide](docs/REPOSITORY_GUIDE.md) — the purpose of every file in
  Git.

## License

Licensed under the [Apache License 2.0](LICENSE).
