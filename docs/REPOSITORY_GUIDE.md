# Tracked-file guide

This reference maps every Git-tracked file to its repository role. Use it to
find an implementation or test; use [Platform internals](INTERNALS.md) to
understand runtime interactions. The source tree groups files by delivery
boundary, so adjacent files do not necessarily run in the same process.

`infra/lib/platform_contract.json` is the cross-language contract. Python, Nix,
and shell adapters consume it rather than maintaining independent role,
account, port, path, and protocol constants.

## Repository and automation metadata

- `.github/CODEOWNERS` — requires owner review for the image-build and publication security boundary.
- `.github/workflows/ci.yml` — defines static checks, recipe smoke tests, Nix evaluation, VM/image builds, protected image publication, and opt-in live acceptance.
- `.gitignore` — excludes private deployment inventory, credentials, build products, environments, and local operational output.
- `LICENSE` — Apache License 2.0 terms for the repository.
- `README.md` — project entry point, current support boundary, and routes into task-specific documentation.
- `flake.lock` — pins the exact Nix flake inputs selected by `flake.nix`; regenerate it through Nix rather than editing it manually.
- `flake.nix` — composes the five NixOS configurations, image packages, VM/evaluation checks, package smoke test, development shell, and formatter.
- `pyproject.toml` — declares the Python package, Python 3.14 requirement, console entry points, dependencies, build settings, and static-tool configuration.
- `uv.lock` — generated, exact Python dependency resolution used by development, CI, and release inputs.

## Example configuration

These files are sanitized schemas/examples. Real inventory and credentials stay
outside Git as described in [Deploy the platform](DEPLOYMENT.md).

- `config/live-acceptance-driver.example.json` — example protected-runner command, identity, path, timeout, and transcript configuration for the live acceptance driver.
- `config/offsite-export.example.json` — example destination, mount identity, filesystem type, and bounds for scheduled off-site recovery export.
- `config/platform-policy.example.json` — example private operator policy for worker/storage capacity, runtime images, and backup recipient.
- `config/platform.example.json` — complete non-secret deployment inventory consumed by Python, Nix, and infrastructure scripts.

## Release deployment

- `deploy/releases/bootstrap_operator_runtime.sh` — bootstraps pinned release tooling below `/srv/openstack-platform` as its unprivileged owner.
- `deploy/releases/deploy_helper_release.sh` — transfers and installs one commit-addressed helper release through the pinned admin bridge.
- `deploy/releases/install_operator_config.py` — atomically installs validated non-secret operator configuration without privilege escalation.
- `deploy/releases/install_release.py` — verifies, extracts, smoke-tests, and atomically selects immutable operator or helper release archives.
- `deploy/releases/release_smoke.py` — checks the operator entry point or the helper action manifest/handler composition before release selection.
- `deploy/releases/setup_operator_bridge.py` — preflights, writes, and validates the pinned SSH/OpenStack bridge between operator and admin hosts.
- `deploy/releases/systemd/openstack-platform-backup.service` — user-service unit that runs the external operator-state backup command.
- `deploy/releases/systemd/openstack-platform-backup.timer` — daily user timer for the external operator-state backup service.

## Documentation

- `docs/DEPLOYMENT.md` — human-facing description of the platform, OpenStack resources, support/security model, setup, ingress, and verification.
- `docs/DEVELOPMENT.md` — canonical local environment and repository validation workflow.
- `docs/INTERNALS.md` — implementation architecture, state ownership, control surfaces, internal API, and future management boundary.
- `docs/MAINTENANCE.md` — maintainer runbook for signed releases, role images, publication, installation, and live acceptance.
- `docs/OPERATIONS.md` — operator procedures for health, backup, off-site export, restore, replacement, pruning, troubleshooting, and recovery.
- `docs/REPOSITORY_GUIDE.md` — this path-by-path source and test index.
- `docs/architecture-overview.svg` — architecture diagram retained as a reusable implementation visual and syntax-validated in CI.

## Infrastructure payloads and scripts

Files under `infra/` are installed into images or invoked by setup, the
operator, the helper, CI, or systemd. They are not a second public CLI.

### Backup and recovery

- `infra/backup/emit_garage_backup.py` — streams a restorable Garage object catalog and payload archive.
- `infra/backup/emit_logical_backup.sh` — selects PostgreSQL, MongoDB, or Garage and emits its logical backup stream.
- `infra/backup/full_loss_recovery_drill.sh` — verifies or performs a bounded full-loss drill from an off-site bundle and escrowed identities.
- `infra/backup/init_garage_backup_key.py` — creates and persists the non-expiring read-only Garage backup key once.
- `infra/backup/registry_artifact.py` — exports or imports bounded OCI Distribution manifests and reachable blobs as a validated tar stream.
- `infra/backup/restore_garage_backup.py` — restores a Garage catalog stream without extracting untrusted paths.
- `infra/backup/restore_managed_data.sh` — destructively restores verified managed-data evidence into replacement services.
- `infra/backup/run_platform_backup.sh` — coordinates encrypted managed-service and registry backup evidence.
- `infra/backup/verify_latest_restore.sh` — restores the latest managed backups into disposable containers and records verification evidence.

### First-boot configuration

- `infra/cloud-init-nixos/admin.yaml` — cloud-init template for the persistent admin/control host and attached state/backup volumes.
- `infra/cloud-init-nixos/builder.yaml` — cloud-init template for a disposable builder and its restricted build inputs.
- `infra/cloud-init-nixos/ingress.yaml` — cloud-init template for the persistent ingress host and optional tunnel token.
- `infra/cloud-init-nixos/storage.yaml` — cloud-init template for the persistent managed-data/registry host.
- `infra/cloud-init-nixos/worker.yaml` — cloud-init template for a replaceable application worker without SSH access.

### Shared infrastructure libraries

- `infra/lib/http.py` — bounded HTTP request and JSON helpers used by monitoring and backup scripts.
- `infra/lib/platform-config.sh` — shell adapter that loads an allowlisted NUL-delimited projection of platform configuration.
- `infra/lib/platform_config.py` — validates and projects non-secret platform configuration for shell consumers.
- `infra/lib/platform_contract.json` — canonical cross-component roles, ports, accounts, executables, paths, protocols, and inventory keys.
- `infra/lib/platform_contract.py` — strict Python loader for the canonical JSON contract when running directly from `infra`.
- `infra/lib/tls.py` — creates TLS contexts rooted in the retained internal certificate authority.

### Monitoring and Nomad

- `infra/monitor/check_platform.py` — writes a bounded, secret-free platform health snapshot from the control plane.
- `infra/monitor/check_services.py` — performs authenticated service checks on the admin host without printing credentials.
- `infra/nomad/bootstrap_acl.sh` — bootstraps Nomad ACLs and scoped tokens into protected environment files.

### OpenStack lifecycle

- `infra/openstack/apply_admin.sh` — creates or verifies the admin server, fixed port, and persistent volumes.
- `infra/openstack/apply_foundation.py` — idempotently establishes project identity, security groups, rules, and fixed ports.
- `infra/openstack/apply_ingress.sh` — creates or verifies the ingress server and fixed port.
- `infra/openstack/apply_storage.sh` — creates or verifies the storage server, fixed port, and data volume.
- `infra/openstack/builder_execute.py` — receives a bounded source archive and runs the fixed rootless BuildKit operation on a disposable builder.
- `infra/openstack/builder_lifecycle.sh` — creates, observes, and deletes builders and their fixed ports with exact ownership checks.
- `infra/openstack/persistent-host.sh` — shared shell functions for safe persistent-server and attached-volume reconciliation.
- `infra/openstack/pin_ephemeral_host_key.sh` — derives and pins an ephemeral host SSH key from provider console evidence.
- `infra/openstack/publish_nixos_image.sh` — verifies signed artifact inputs, uploads one QCOW2, and checks its Glance identity and metadata.
- `infra/openstack/render_host_user_data.py` — renders protected persistent-role cloud-init from exact volume and environment inputs.
- `infra/openstack/verify_persistent_host.py` — checks an existing persistent host projection before an apply script reuses it.
- `infra/openstack/worker_lifecycle.sh` — creates, observes, and deletes worker servers and ports with Nomad/ownership checks.

### PKI and registry maintenance

- `infra/pki/generate_internal_pki.sh` — generates the internal CA and role/service certificates in a protected output directory.
- `infra/registry/delete_manifest.py` — deletes one controller-owned registry manifest after validating registry credentials and identity.
- `infra/registry/registry-gc.sh` — runs offline registry garbage collection under the storage-host service contract.

## Nix image definitions

- `nix/lib/constants.nix` — imports the canonical JSON contract and exposes validated constants to Nix modules.
- `nix/lib/inventory.nix` — strictly loads and validates the selected deployment inventory for Nix evaluation.
- `nix/modules/common.nix` — common NixOS users, packages, cloud-init, networking, security, logging, and platform paths shared by all roles.
- `nix/pkgs/default.nix` — pins/packages third-party binaries, the Python application, release tools, and helper launcher used by role images.
- `nix/roles/admin.nix` — admin role services: Nomad control, controller/helper, monitoring, backup, release credentials, and state mounts.
- `nix/roles/builder.nix` — rootless BuildKit builder role, restricted SSH execution, metadata denial, and expiry.
- `nix/roles/ingress.nix` — Traefik ingress role, tunnel/direct listener policy, routes, and trusted-forwarder controls.
- `nix/roles/storage.nix` — PostgreSQL, MongoDB, Garage, registry, TLS, firewall, and persistent data services.
- `nix/roles/worker.nix` — Nomad/Docker worker role, workload isolation, registry trust, and no-SSH policy.
- `nix/tests/default.nix` — NixOS VM tests for each role plus shared test-only inventory, PKI, storage, and service fixtures.

## Python package

The `openstack_platform` package supplies installed entry points and shared
implementation. The controller owns product state; the helper performs a fixed
set of trusted admin-host actions; top-level modules own infrastructure and
cross-cutting boundaries.

### Top-level infrastructure and shared modules

- `openstack_platform/__init__.py` — package description and protocol version.
- `openstack_platform/acceptance.py` — plan, checkpoint, evidence, and verification engine for disposable live acceptance.
- `openstack_platform/acceptance_live_driver.py` — reviewed adapter from acceptance protocol actions to supported repository/operator interfaces.
- `openstack_platform/config.py` — typed, strict loading of deployment inventory and private operator policy.
- `openstack_platform/contracts.py` — packaged typed access to the canonical cross-component JSON contract.
- `openstack_platform/durable.py` — no-follow, fsync-backed primitives for crash-durable local file replacement.
- `openstack_platform/host_keys.py` — verifies console/keyscan evidence and atomically pins the fixed admin SSH host key.
- `openstack_platform/host_user_data.py` — validates protected inputs and renders role-specific cloud-init templates.
- `openstack_platform/installation.py` — central definitions of installed filesystem locations used by entry points.
- `openstack_platform/openstack.py` — bounded provider operations for images and persistent-host power/replacement lifecycle.
- `openstack_platform/operator.py` — `openstack-platform` command parser and operator-level setup/status/backup/restore/infra orchestration.
- `openstack_platform/recovery_bundle.py` — append-only off-site bundle export, verification, import, discovery, and scheduled-export status.
- `openstack_platform/release_manifest.py` — deterministic compatibility manifests, SBOM/provenance, signatures, artifact binding, and evidence bundles.
- `openstack_platform/remote.py` — protocol-v1 request/response validation and pinned local or SSH helper invocation.
- `openstack_platform/restore.py` — offline integrity/schema/identity checks and atomic SQLite database replacement.
- `openstack_platform/runtime.py` — private-directory, lock, bounded process/HTTP, redaction, and diagnostic primitives.
- `openstack_platform/setup.py` — resumable greenfield preflight and apply orchestration across release, Nix, OpenStack, and hosted services.
- `openstack_platform/validation.py` — shared strict validators for names, UUIDs, commits, URLs, paths, digests, and bounded text.

### Controller

- `openstack_platform/controller/__init__.py` — marks and describes the trusted controller package.
- `openstack_platform/controller/api.py` — composes controller routes, domain services, database, helper transport, and socket capability split.
- `openstack_platform/controller/application_models.py` — immutable storage-binding, manifest, and generated-recipe value objects.
- `openstack_platform/controller/application_runtime.py` — source acquisition, recipe generation, build, worker, deploy, health, cleanup, and registry-retention primitives.
- `openstack_platform/controller/application_service.py` — application declaration, enable/disable, and destructive lifecycle orchestration.
- `openstack_platform/controller/async_operations.py` — bounded worker pool for durably accepted, per-application serialized mutations.
- `openstack_platform/controller/database.py` — SQLite schema, migrations, deployment identity, journals, and short product-state operations.
- `openstack_platform/controller/deployment_config.py` — parses typed UI-owned deployment configuration and validates repository checkouts.
- `openstack_platform/controller/deployment_service.py` — coordinates candidate build/deploy/accept/recovery independently of HTTP parsing.
- `openstack_platform/controller/environment_service.py` — write-only environment mutation orchestration.
- `openstack_platform/controller/hosted_backup.py` — creates encrypted committed backups of the admin-hosted controller database.
- `openstack_platform/controller/http.py` — bounded HTTP/1.1 JSON server over Unix sockets with peer credential and resource enforcement.
- `openstack_platform/controller/log_service.py` — bounded reads of runtime and retained build logs.
- `openstack_platform/controller/main.py` — `openstack-platform-controller` executable composition and startup.
- `openstack_platform/controller/nomad_jobs.py` — renders generated Nomad jobs and validates job/placement/route identities.
- `openstack_platform/controller/service_support.py` — shared deadlines, helper transport protocol, and mutation guards.
- `openstack_platform/controller/status.py` — safe infrastructure, application, storage, operation, and live status read models.
- `openstack_platform/controller/storage.py` — low-level PostgreSQL, MongoDB, and S3 operation state machine and helper calls.
- `openstack_platform/controller/storage_contract.py` — canonical owner, secret-key, and environment mappings shared by storage/runtime code.
- `openstack_platform/controller/storage_service.py` — managed-storage request validation and mutation orchestration.

### Constrained helper

- `openstack_platform/helper/__init__.py` — marks and describes the unprivileged admin-host helper package.
- `openstack_platform/helper/actions-v1.txt` — release-bound allowlist of protocol-v1 action names the helper may dispatch.
- `openstack_platform/helper/application_actions.py` — fixed Nomad deployment, health, promotion, log, removal, and environment handlers.
- `openstack_platform/helper/main.py` — one-request helper dispatcher plus committed backup/retention evidence handling.
- `openstack_platform/helper/nomad.py` — Nomad Variable reads and owner-scoped compare-and-set updates.
- `openstack_platform/helper/production.py` — lazily constructs concrete production handlers and trusted local service clients.
- `openstack_platform/helper/storage.py` — trusted provider operations for PostgreSQL, MongoDB, and Garage/S3 resources and credentials.

## Tests

Fixture applications are deliberately small but executable; JSON provider
fixtures preserve exact formatter/identity variants. Test modules use
`unittest` and are named for the behavior boundary they protect.

### Fixtures and smoke scripts

- `tests/fixtures/apps/bun/bun.lock` — locked Bun fixture dependency graph used by generated-recipe smoke tests.
- `tests/fixtures/apps/bun/package.json` — Bun fixture build/start scripts and dependency declaration.
- `tests/fixtures/apps/bun/server.ts` — Bun HTTP fixture with a deterministic health endpoint.
- `tests/fixtures/apps/node/package-lock.json` — locked npm dependency graph for the Node fixture.
- `tests/fixtures/apps/node/package.json` — Node fixture start command and package metadata.
- `tests/fixtures/apps/node/server.js` — Node HTTP fixture with a deterministic readiness endpoint.
- `tests/fixtures/openstack/glance_quota_formatter_outputs.json` — Glance quota API/CLI output variants, including unknown and unlimited values.
- `tests/fixtures/openstack/provider_uuid_outputs.json` — compact and canonical UUID projections returned by different OpenStack surfaces.
- `tests/install_ci_apt_packages.sh` — bounded retry wrapper for fixed CI-only APT package installation.
- `tests/product_fixtures.py` — reusable builders for accepted application/deployment product state.
- `tests/repository_fixtures.py` — creates clean temporary Git repositories from the current worktree for release-sensitive tests.
- `tests/smoke_generated_recipes.sh` — generates, builds, starts, and health-checks real Node and Bun recipes with rootless Podman.
- `tests/smoke_openstack_image.sh` — boots an exact role QCOW2 with QEMU/config-drive and waits for its completion marker.

### Test modules

- `tests/test_application_runtime.py` — source, recipe, BuildKit, worker, deployment, cleanup, and retention runtime tests.
- `tests/test_ci_publication.py` — guards the CI path set that triggers role-image publication.
- `tests/test_controller_api.py` — controller route composition, capability split, responses, idempotency, and service integration tests.
- `tests/test_controller_database.py` — schema, migration, identity, journal, state transition, and database recovery tests.
- `tests/test_controller_hosting.py` — static Nix/controller account, socket, backup, and service-hosting boundary checks.
- `tests/test_controller_http.py` — Unix HTTP parsing, deadlines, keep-alive, peer policy, overload, shutdown, and socket security tests.
- `tests/test_deployment_config.py` — typed deployment configuration and Git branch/ref resolution tests.
- `tests/test_documentation.py` — documentation links, consolidated reader paths, interface claims, route coverage, and repository-index checks.
- `tests/test_full_loss_recovery_drill.py` — full and verify-only recovery drill command/evidence/failure-boundary tests.
- `tests/test_hardening_properties.py` — generated property cases for durable writes, parsers, state boundaries, idempotency, and secret redaction.
- `tests/test_helper_application_actions.py` — Nomad helper deployment, ownership, health, promotion, environment, logs, and removal tests.
- `tests/test_host_user_data.py` — protected-input validation and cloud-init rendering tests for each role.
- `tests/test_hosted_controller_backup.py` — hosted SQLite backup encryption, evidence, permissions, and failure cleanup tests.
- `tests/test_infra_http.py` — bounded infrastructure HTTP helper redirect, size, status, and JSON tests.
- `tests/test_live_acceptance.py` — plan immutability, checkpoint/resume, evidence chain, signature, and failure tests.
- `tests/test_live_acceptance_driver.py` — repository driver protocol, observations, interruption, recovery, ownership, and teardown tests.
- `tests/test_namespace.py` — namespace propagation and inventory validation across Python and Nix role sources.
- `tests/test_openstack_lifecycle_scripts.py` — shell lifecycle deletion, ambiguity, ownership, and exact-resource behavior tests.
- `tests/test_operator_bridge.py` — operator bridge preflight, generated SSH/provider wrappers, key pinning, and drift tests.
- `tests/test_operator_integration.py` — operator command and helper protocol integration tests over fake dependencies.
- `tests/test_packaging_release.py` — release archive identity, runtime paths, configuration validation, installer, and atomic-selection tests.
- `tests/test_platform_contract.py` — parity and required-value checks for JSON, Python, Nix, shell, and packaged contracts.
- `tests/test_platform_foundation_nomad.py` — owner-scoped Nomad Variable merge and compare-and-set behavior tests.
- `tests/test_platform_foundation_runtime_remote.py` — runtime bounds/redaction, remote protocol, backup acceptance, provider command, and path tests.
- `tests/test_platform_foundation_validation_config.py` — shared validators plus inventory/policy parsing and rejection tests.
- `tests/test_platform_host_keys.py` — console fingerprint, keyscan, known-hosts matching, drift, and atomic pin tests.
- `tests/test_platform_openstack.py` — image selection/pruning and persistent-host power/replacement/recovery provider tests.
- `tests/test_platform_restore.py` — encrypted/plain offline restore validation, operation-state, permissions, and atomicity tests.
- `tests/test_platform_services.py` — application, deployment, environment, storage, log, and helper-failure service tests.
- `tests/test_platform_setup.py` — environment parsing, read-only preflight, inventory generation, hosted-controller gates, resume, and CLI tests.
- `tests/test_platform_storage.py` — controller storage state machine and concrete PostgreSQL/MongoDB/Garage helper tests.
- `tests/test_recovery_bundle.py` — off-site export/import, manifest bounds, mount validation, scheduling, and receipt tests.
- `tests/test_registry_artifact_streaming.py` — bounded OCI manifest/blob export and import graph-validation tests.
- `tests/test_release_manifest.py` — component manifest, SBOM, provenance, signature, bundle, and source-binding tests.
- `tests/test_role_artifact_manifest.py` — post-build QCOW2/Nix closure/publication artifact evidence and tamper tests.
- `tests/test_verify_persistent_host.py` — exact provider projection validation for safely reusing persistent hosts.

## Keeping this guide current

When adding, deleting, or renaming a tracked file, update this guide in the same
change. `tests/test_documentation.py` checks that every path returned by
`git ls-files` has one backtick-delimited entry here. The check allows a new,
not-yet-added guide entry during local editing, but CI verifies it once the file
is tracked.
