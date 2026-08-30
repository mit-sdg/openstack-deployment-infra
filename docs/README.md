# Documentation

This repository currently provides OpenStack/NixOS infrastructure, automated
setup, backup/restore tooling, an operator-only infrastructure CLI, and an
hosted local controller API. Setup installs its policy and helper release, and
admin starts it on restricted project and privileged Unix sockets. This repository does not provide a
self-service portal or authentication application.

The former product CLI and repository deployment manifest are retired. Future
UI readers should use the
[management application specification](MANAGEMENT_APP_SPEC.md), which is an
implementation target rather than a description of a running product.

A deployment starts with an empty controller SQLite database. Tooling creates accepted
state instead of importing provider records. Operator actions change only
resources named by the private infrastructure inventory.

## Learn by doing

- [Verify a fresh platform and its backups](TUTORIAL.md) — confirm the current
  pre-management result: infrastructure, public health, backup, and restore
  checks.

## Complete a task

- [Automated greenfield setup](SETUP.md) — turn one protected environment file
  into keys, images, VMs, volumes, releases, and backup configuration.
- [Operator journey](OPERATIONS.md) — manual checkpoints, secret-file formats,
  foundation/ACL/role bootstrap, backups, offline restore, upgrades, cleanup,
  and recovery.
- [Troubleshooting](TROUBLESHOOTING.md) — diagnostic chains for observable
  infrastructure, ingress, backup, restore, and release failures.
- [Public ingress](PUBLIC_INGRESS.md) — configure current platform DNS, TLS,
  forwarding, and original-host preservation.
- [Release installation](RELEASE_INSTALLER.md) — install committed management
  and helper releases.
- [Release supply-chain evidence](RELEASE_SUPPLY_CHAIN.md) — generate and verify
  signed compatibility manifests, SBOMs, provenance, and development evidence.
- [Image publication](IMAGE_PUBLISHING.md) — publish commit-addressed role
  images through CI.

## Look up a contract

- [Configuration reference](CONFIGURATION.md) — private infrastructure
  inventory and policy fields.
- [Operator CLI and local controller API](CONTROL_PLANE_CONTRACT.md) — exact
  current commands, Unix transport, routes, idempotency, responses, and
  recovery boundaries.
- [Management-to-controller boundary](MANAGEMENT_CONTROLLER_BOUNDARY.md) —
  project and privileged sockets, peer identity, route capabilities, limits,
  and the future web service's host sandbox contract.
- [NixOS image reference](../nix/README.md) — image outputs, tests, exact-UUID
  selection, and role acceptance.

## Understand the design

- [Architecture](ARCHITECTURE.md) — current roles, control surfaces, state
  ownership, isolation, and failure boundaries.
- [Management application specification](MANAGEMENT_APP_SPEC.md) — future
  sync-engine UI, external authentication contract, authorization, controller
  requests, reconciliation, and acceptance evidence.
- [Self-hosted management implementation plan](IMPLEMENTATION_PLAN.md) —
  completed controller/retirement work and remaining management UI, auth, cutover,
  and recovery phases.
- [Platform hardening plan](HARDENING_PLAN.md) — remaining verification,
  typed-configuration, supply-chain, isolation, durability, fuzzing, and
  network-boundary work with required exit evidence.

## Verify acceptance

- [Traceable acceptance checklist](ACCEPTANCE_CHECKLIST.md) — current
  infrastructure, operator/controller boundary, backups, restore, upgrade, and
  cleanup evidence.
- [Disposable P-07 live release gate](LIVE_ACCEPTANCE.md) — plan, protected
  driver contract, resumable execution, and sanitized authenticated evidence.

Keep live identifiers, credentials, incident notes, and handoff records out of
tracked documentation. Store them in the deployment's private operations
system.
