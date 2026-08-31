# A small application platform for OpenStack

This repository builds a NixOS-based platform for hosting small HTTP
applications inside one OpenStack project. It creates the cloud foundation,
five machine roles, PostgreSQL/MongoDB/S3 services, private image registry,
public ingress, backups, recovery tooling, an operator CLI, and a local
application controller.

## Current status

Infrastructure deployment and operation are implemented. The browser management
UI and its external authentication service are not, so there is not yet a
supported workflow for application owners.

Today, an operator can create and recover a platform with:

- persistent admin, ingress, and storage hosts;
- replaceable application workers and single-use builders;
- exact-image and persistent-host lifecycle controls;
- separate encrypted backups for controller state, operator state, and managed
  data, including retained application images; and
- a local controller ready for the future management application.

The planned application workflow supports public, credential-free GitHub
repositories and typed Node or Bun configuration. Users will not receive SSH,
OpenStack, Nomad, registry, or database-administrator credentials. Private
repositories, arbitrary build commands, and Dockerfiles are outside the current
contract.

## Documentation

- [Deploy the platform](docs/DEPLOYMENT.md) — what the platform creates, what it
  supports, its security model, setup, ingress, and verification.
- [Operate and recover it](docs/OPERATIONS.md) — health, backups, off-site
  export, restore, host replacement, pruning, and troubleshooting.
- [Platform internals](docs/INTERNALS.md) — component ownership, state,
  controller/helper boundaries, internal API, and the future management UI.
- [Release and platform maintenance](docs/MAINTENANCE.md) — signed releases,
  role images, publication, installation, and live acceptance.
- [Development workflow](docs/DEVELOPMENT.md) — local environment and checks.
- [Tracked-file guide](docs/REPOSITORY_GUIDE.md) — the purpose of every file in
  Git.

## License

Licensed under the [Apache License 2.0](LICENSE).
