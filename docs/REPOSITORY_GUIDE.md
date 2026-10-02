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
- `deploy/releases/install_release.py` — verifies, extracts, smoke-tests, and selects operator/helper or staged broker/web release archives.
- `deploy/releases/migrate_legacy_controller.py` — authenticates and imports terminal legacy controller state into a private current-schema hosted-controller candidate.
- `deploy/releases/release_smoke.py` — checks the operator entry point or the helper action manifest/handler composition before release selection.
- `deploy/releases/setup_operator_bridge.py` — preflights, writes, and validates the pinned SSH/OpenStack bridge between operator and admin hosts.
- `deploy/releases/systemd/openstack-platform-backup.service` — user-service unit that runs the external operator-state backup command.
- `deploy/releases/systemd/openstack-platform-backup.timer` — daily user timer for the external operator-state backup service.

## Documentation

- `docs/DEPLOYMENT.md` — human-facing description of the platform, OpenStack resources, support/security model, setup, ingress, and verification.
- `docs/DEVELOPMENT.md` — canonical local environment and repository validation workflow.
- `docs/INTERNALS.md` — implementation architecture, state ownership, control surfaces, internal API, and future management boundary.
- `docs/MAINTENANCE.md` — maintainer runbook for signed releases, role images, publication, installation, and live acceptance.
- `docs/APPLICATION_DEPLOYMENTS.md` — authenticated curl workflow for declaration, build-first maintenance deployment, polling, recovery, and retained-artifact rollback without the management UI.
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
- `infra/lib/owner_portal_config.py` — byte-identical copy of the owner-portal inventory validator, so infra shipped without the Python package can still validate platform configuration.
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
- `infra/openstack/publish_nixos_image.sh` — verifies production/development trust and exact artifact bytes, uploads or verifies one QCOW2, and checks its Glance identity and metadata.
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
- `nix/pkgs/openstacksdk-security-group-project-alias.patch` — preserves legacy Neutron tenant-only security-group ownership in SDK/OSC projections.
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
- `openstack_platform/fixed_ip.py` — exact retained primary Neutron port capability, ownership, and provider UUID validation.
- `openstack_platform/floating_ip.py` — bounded, project-scoped Neutron capability and floating IPv4 ownership operations.
- `openstack_platform/contracts.py` — packaged typed access to the canonical cross-component JSON contract.
- `openstack_platform/durable.py` — no-follow, fsync-backed primitives for crash-durable local file replacement.
- `openstack_platform/host_keys.py` — verifies console/keyscan evidence and atomically pins the fixed admin SSH host key.
- `openstack_platform/host_user_data.py` — validates protected inputs and renders role-specific cloud-init templates.
- `openstack_platform/image_pipeline.py` — exact-run image build retention, CI/signing-input verification, and no-rebuild production publication.
- `openstack_platform/ingress_credentials.py` — validates per-replacement protected connector-token files and stages ingress user-data without a credential store.
- `openstack_platform/installation.py` — central definitions of installed filesystem locations used by entry points.
- `openstack_platform/openstack.py` — bounded provider operations for images and persistent-host power/replacement lifecycle.
- `openstack_platform/operator.py` — `openstack-platform` command parser and operator-level setup/status/dashboard/backup/restore/infra orchestration.
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
- `openstack_platform/controller/deployment_reads.py` — allowlisted immutable configuration and exact per-attempt source repository reads.
- `openstack_platform/controller/rollback.py` — retained-artifact rollback target validation and compare-and-set review plans.
- `openstack_platform/controller/deployment_service.py` — coordinates candidate build/deploy/accept/recovery independently of HTTP parsing.
- `openstack_platform/controller/maintenance.py` — journals verified process stop and optional worker removal after build/preflight, under the deployment's application lock.
- `openstack_platform/controller/worker_reuse.py` — read-only exact accepted-worker identity, image, and capacity validation for same-worker deployments.
- `openstack_platform/controller/environment_service.py` — write-only environment mutation orchestration.
- `openstack_platform/controller/hosted_backup.py` — creates encrypted committed backups of the admin-hosted controller database.
- `openstack_platform/controller/http.py` — bounded HTTP/1.1 JSON server over Unix sockets with peer credential and resource enforcement.
- `openstack_platform/controller/image_service.py` — validated hosted role-image metadata selection and immutable worker/builder provisioning snapshots.
- `openstack_platform/controller/log_service.py` — bounded reads of runtime and retained build logs.
- `openstack_platform/controller/fixed_ip_service.py` — app-locked retained primary port reservations, generation binding, and uncertain-create recovery.
- `openstack_platform/controller/public_ip_service.py` — app-scoped floating IPv4 reservation, handover, and release journals.
- `openstack_platform/controller/main.py` — `openstack-platform-controller` executable composition and startup.
- `openstack_platform/controller/nomad_jobs.py` — renders generated Nomad jobs and validates job/placement/route identities.
- `openstack_platform/controller/sizing.py` — reviewed per-app flavor plans and measured capacity budgets with OS/service reserves.
- `openstack_platform/controller/seed_images.py` — validates seed evidence, preserves retained/journal-proven selections across admin replacement, and seeds missing roles.
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
- `openstack_platform/helper/errors.py` — shared safe exception identity for console and Python module entrypoints.
- `openstack_platform/helper/nomad.py` — Nomad Variable reads and owner-scoped compare-and-set updates.
- `openstack_platform/helper/worker_capacity.py` — exact owned Nomad node readiness and allocatable CPU/RAM observations.
- `openstack_platform/helper/production.py` — lazily constructs concrete production handlers and trusted local service clients.
- `openstack_platform/helper/registry_artifact.py` — bounded read-only manifest and blob availability checks within one application registry repository.
- `openstack_platform/helper/storage.py` — trusted provider operations for PostgreSQL, MongoDB, and Garage/S3 resources and credentials.

### Read-only operator dashboard

- `openstack_platform/dashboard/__init__.py` — marks and describes the read-only operator dashboard package.
- `openstack_platform/dashboard/model.py` — pure projection of dashboard evidence into role, application, operation, check, and issue status.
- `openstack_platform/dashboard/preview.py` — loopback preview that renders synthetic evidence through the production parsers and projection.
- `openstack_platform/dashboard/server.py` — private Unix-socket HTTP server, static asset allowlist, security headers, and `dashboard` command composition.
- `openstack_platform/dashboard/service.py` — single-flight refresh loop, last-good source retention, route-check history, and snapshot serialization.
- `openstack_platform/dashboard/sources.py` — fixed admin reader, strict parsers for privileged and operator reads, and public route probes.
- `openstack_platform/dashboard/static/dashboard.css` — dashboard layout, light and dark themes, and responsive desktop and mobile styles.
- `openstack_platform/dashboard/static/dashboard.js` — browser renderer that polls the cached snapshot and builds the view with DOM APIs.
- `openstack_platform/dashboard/static/favicon.svg` — dashboard browser icon.
- `openstack_platform/dashboard/static/index.html` — dashboard document shell and icon sprite.
- `openstack_platform/dashboard/static/theme.js` — applies a saved light or dark preference before first paint.

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
- `tests/fixtures/openstack/neutron_security_group_tenant_only.json` — sanitized legacy Neutron response with authoritative tenant ownership and no project field.
- `tests/fixtures/openstack/provider_uuid_outputs.json` — compact and canonical UUID projections returned by different OpenStack surfaces.
- `tests/fixtures/retained_openstack.py` — offline OSC/Nomad process double for real worker helper and lifecycle integration tests.
- `tests/install_ci_apt_packages.sh` — bounded retry wrapper for fixed CI-only APT package installation.
- `tests/product_fixtures.py` — reusable builders for accepted application/deployment product state.
- `tests/repository_fixtures.py` — creates clean temporary Git repositories, with optional fixture substitutions before the fixture commit, for release-sensitive tests.
- `tests/smoke_generated_recipes.sh` — generates, builds, starts, and health-checks real Node and Bun recipes with rootless Podman.
- `tests/smoke_openstack_image.sh` — boots an exact role QCOW2 with QEMU/config-drive and waits for its completion marker.

### Test modules

- `tests/test_application_runtime.py` — source, recipe, BuildKit, worker, deployment, cleanup, and retention runtime tests.
- `tests/test_application_health_observation.py` — accepted health-path/route-marker checks and intentionally disabled application observations.
- `tests/test_application_sizing.py` — opaque flavor IDs, per-app plans, default preservation, resize acceptance, retries, and rollback tests.
- `tests/test_application_deployment_docs.py` — Bash syntax and real configuration-schema validation for the operator curl runbook.
- `tests/test_helper_worker_capacity.py` — production capacity dispatch, pinned Nomad field/worker identity contract, deadlines, and sanitized failures.
- `tests/test_ci_publication.py` — guards the CI path set that triggers role-image publication.
- `tests/test_build_failure_cleanup.py` — deterministic build-rejection classification and exact builder/registry absence checks.
- `tests/test_controller_api.py` — controller route composition, capability split, responses, idempotency, and service integration tests.
- `tests/test_controller_database.py` — schema, migration, identity, journal, state transition, and database recovery tests.
- `tests/test_legacy_controller_migration.py` — legacy marker rejection and complete application/storage/deployment import tests.
- `tests/test_controller_hosting.py` — static Nix/controller account, socket, backup, and service-hosting boundary checks.
- `tests/test_controller_http.py` — Unix HTTP parsing, deadlines, keep-alive, peer policy, overload, shutdown, and socket security tests.
- `tests/test_controller_images.py` — hosted image selection CAS, provider validation, recovery, capability, and pinned provisioning tests.
- `tests/test_controller_recovery.py` — storage-kind recovery, rejected-build terminalization, crash/retry, and privileged polling tests.
- `tests/test_controller_seed_images.py` — hosted image-seed identity, selection, and idempotence tests.
- `tests/test_dashboard.py` — dashboard admin reader, parser, probe, status projection, refresh, HTTP security, command, static asset, and release-identity tests.
- `tests/test_deployment_config.py` — typed deployment configuration and Git branch/ref resolution tests.
- `tests/test_documentation.py` — documentation links, consolidated reader paths, interface claims, route coverage, and repository-index checks.
- `tests/test_full_loss_recovery_drill.py` — full and verify-only recovery drill command/evidence/failure-boundary tests.
- `tests/test_hardening_properties.py` — generated property cases for durable writes, parsers, state boundaries, idempotency, and secret redaction.
- `tests/test_helper_registry_artifact.py` — scoped read-only artifact graph verification, missing content, digest tampering, and secret-safe results.
- `tests/test_retained_rollback.py` — authoritative reads, no-build rollback, storage drift, current secrets, acceptance recovery, and optional FIP handover ordering.
- `tests/test_helper_application_actions.py` — Nomad helper deployment, ownership, health, promotion, environment, logs, and removal tests.
- `tests/test_host_user_data.py` — protected-input validation and cloud-init rendering tests for each role.
- `tests/test_ingress_credentials.py` — per-replacement token validation, rotation, cleanup, non-persistence, and CLI contract tests.
- `tests/test_hosted_controller_backup.py` — hosted SQLite backup encryption, evidence, permissions, and failure cleanup tests.
- `tests/test_image_pipeline.py` — retained-byte, CI/source-run identity, signed promotion, unsigned rollback, and publication gate tests.
- `tests/test_infra_http.py` — bounded infrastructure HTTP helper redirect, size, status, and JSON tests.
- `tests/test_live_acceptance.py` — plan immutability, checkpoint/resume, evidence chain, signature, and failure tests.
- `tests/test_live_acceptance_driver.py` — repository driver protocol, observations, interruption, recovery, ownership, and teardown tests.
- `tests/test_namespace.py` — namespace propagation and inventory validation across Python and Nix role sources.
- `tests/test_neutron_sdk_projection.py` — mandatory Nix package-smoke regression using the actual SDK and OSC against a loopback tenant-only Neutron fixture.
- `tests/test_openstack_lifecycle_scripts.py` — shell lifecycle deletion, ambiguity, ownership, and exact-resource behavior tests.
- `tests/test_operator_bridge.py` — operator bridge preflight, generated SSH/provider wrappers, key pinning, and drift tests.
- `tests/test_operator_integration.py` — operator command and helper protocol integration tests over fake dependencies.
- `tests/test_operator_live_health.py` — hosted-authoritative operator status and public-only tunnel readiness regression tests.
- `tests/test_packaging_release.py` — release archive identity, runtime paths, configuration validation, installer, and atomic-selection tests.
- `tests/test_platform_contract.py` — parity and required-value checks for JSON, Python, Nix, shell, and packaged contracts.
- `tests/test_platform_foundation_nomad.py` — owner-scoped Nomad Variable merge and compare-and-set behavior tests.
- `tests/test_platform_foundation_runtime_remote.py` — runtime bounds/redaction, remote protocol, backup acceptance, provider command, and path tests.
- `tests/test_platform_foundation_validation_config.py` — shared validators plus inventory/policy parsing and rejection tests.
- `tests/test_platform_host_keys.py` — console fingerprint, keyscan, known-hosts matching, drift, and atomic pin tests.
- `tests/test_platform_openstack.py` — image selection/pruning and persistent-host power/replacement/recovery provider tests.
- `tests/test_fixed_ip.py` — retained primary port HTTP/helper/shell lifecycle, reuse without provider mutation, drift, compact UUID, maintenance, and lost-response integration tests.
- `tests/test_worker_reuse.py` — opt-in same-worker releases, retained-image rollback, quiescence checkpoints, drift, failure cleanup, and controller restart tests.
- `tests/test_public_ip.py` — offline floating IPv4 capability, ownership, retry, handover, and release tests.
- `tests/test_platform_restore.py` — encrypted/plain offline restore validation, operation-state, permissions, and atomicity tests.
- `tests/test_platform_services.py` — application, deployment, environment, storage, log, and helper-failure service tests.
- `tests/test_platform_setup.py` — environment parsing, read-only preflight, inventory generation, hosted-controller gates, resume, and CLI tests.
- `tests/test_platform_storage.py` — controller storage state machine and concrete PostgreSQL/MongoDB/Garage helper tests.
- `tests/test_recovery_bundle.py` — off-site export/import, manifest bounds, mount validation, scheduling, and receipt tests.
- `tests/test_registry_artifact_streaming.py` — bounded OCI manifest/blob export and import graph-validation tests.
- `tests/test_release_manifest.py` — component manifest, SBOM, provenance, signature, bundle, and source-binding tests.
- `tests/test_role_artifact_manifest.py` — post-build QCOW2/Nix closure/publication artifact evidence and tamper tests.
- `tests/test_verify_persistent_host.py` — exact provider projection validation for safely reusing persistent hosts.

## Local owner portal slice

- `openstack_platform/host_paths.py` — handle-based root controller file copying and credential metadata preparation, with no-follow component traversal.
- `nix/lib/controller-paths.nix` — one controller preparation plan shared by read-only preflight and handle-based application.
- `tests/test_host_paths.py` — symlink, hardlink, FIFO, wrong-owner and replacement-race coverage for root preparation helpers.
- `openstack_platform/management/__init__.py` — marks the owner management boundary without loading optional dependencies.
- `openstack_platform/management/common.py` — strict JSON/base64url, canonical data, digests, opaque tokens, and UTC presentation helpers.
- `openstack_platform/management/config.py` — validates closed management configuration and loopback development constraints.
- `openstack_platform/management/broker/__init__.py` — marks the authoritative owner broker package.
- `openstack_platform/management/broker/api.py` — closed owner routes, ownership, quota admission, configuration snapshots, and authorized controller reads.
- `openstack_platform/management/broker/auth.py` — login flows, replay prevention, browser-bound completion, opaque sessions, CSRF, and logout.
- `openstack_platform/management/broker/client.py` — fixed bounded Unix HTTP client with no TCP fallback.
- `openstack_platform/management/broker/database.py` — private broker SQLite schema, migration evidence/locking, and short transactions.
- `openstack_platform/management/broker/journal.py` — durable intent leases, same-key retries, operation polling, and recovery states.
- `openstack_platform/management/broker/main.py` — broker entry point using the existing controller transport unchanged.
- `openstack_platform/management/web/__init__.py` — marks the disposable browser web package.
- `openstack_platform/management/web/server.py` — bounded HTTP, static serving, closed broker forwarding, typed cookie directives, and CSP/security headers.
- `openstack_platform/management/web/main.py` — static web entry point without development-provider imports.
- `openstack_platform/management/dev/__init__.py` — marks explicitly local development doubles.
- `openstack_platform/management/dev/__main__.py` — loopback HTTPS/Vite harness with in-memory signing/TLS keys and controlled lifecycle.
- `openstack_platform/management/dev/controller.py` — real project-socket double with immutable fixture deployments, checkpoints, and lost-response/recovery faults.
- `tests/test_management.py` — offline broker authentication, ownership/quota race, intent recovery, and web/Unix transport tests.
- `frontend/owner-portal/package.json` — exact frontend and browser-test dependencies plus local scripts.
- `frontend/owner-portal/package-lock.json` — locked npm graph for Node 24 development and builds.
- `frontend/owner-portal/tsconfig.json` — strict frontend and smoke-test TypeScript checks.
- `frontend/owner-portal/vite.config.ts` — React production build, local API proxy, and Vitest configuration.
- `frontend/owner-portal/playwright.config.ts` — cached-Chromium HTTPS smoke with worktree-confined output.
- `frontend/owner-portal/index.html` — static external-script entry document and origin-preserving HTML referrer policy.
- `frontend/owner-portal/public/theme.js` — external pre-paint light/dark preference script.
- `frontend/owner-portal/public/favicon.svg` — code-native portal mark.
- `frontend/owner-portal/src/main.tsx` — React root and in-memory TanStack Query setup.
- `frontend/owner-portal/src/api.ts` — typed API/response validation, same-key CSRF retry, and typed settings validation.
- `frontend/owner-portal/src/App.tsx` — accessible owner routes, configuration, exact-commit review, status/history, and build logs.
- `frontend/owner-portal/src/app.css` — external dashboard-language tokens, light/dark themes, cards, responsive tables/forms, and drawer styling.
- `frontend/owner-portal/src/test-setup.ts` — Vitest DOM assertions and component cleanup.
- `frontend/owner-portal/src/App.test.tsx` — configuration behavior, errors, status semantics, API decoding, and CSRF retry tests.
- `frontend/owner-portal/e2e/owner-flow.spec.ts` — two-owner HTTPS product smoke, lost-response recovery, strict CSP, and sanitized visual evidence.

- `frontend/owner-portal/.prettierrc.json` — pinned frontend formatter policy with print width 100.
- `frontend/owner-portal/src/components/AppFrame.tsx` — shared owner AppFrame component and presentation behavior.
- `frontend/owner-portal/src/components/BoundaryText.tsx` — shared owner BoundaryText component and presentation behavior.
- `frontend/owner-portal/src/components/DeploymentRow.tsx` — shared owner DeploymentRow component and presentation behavior.
- `frontend/owner-portal/src/components/Feedback.tsx` — shared owner Feedback component and presentation behavior.
- `frontend/owner-portal/src/components/Mark.tsx` — shared owner Mark component and presentation behavior.
- `frontend/owner-portal/src/components/Operation.tsx` — shared owner Operation component and presentation behavior.
- `frontend/owner-portal/src/components/Status.tsx` — shared owner Status component and presentation behavior.
- `frontend/owner-portal/src/components/ThemeButton.tsx` — shared owner ThemeButton component and presentation behavior.
- `frontend/owner-portal/src/components/presentation.test.tsx` — shared owner presentation.test component and presentation behavior.
- `frontend/owner-portal/src/hooks/useIntentPolling.ts` — owner useIntentPolling lifecycle and data-fetching hook.
- `frontend/owner-portal/src/hooks/useSession.ts` — owner useSession lifecycle and data-fetching hook.
- `frontend/owner-portal/src/pages/Configuration.tsx` — owner configuration page module.
- `frontend/owner-portal/src/pages/Dashboard.tsx` — owner dashboard page module.
- `frontend/owner-portal/src/pages/Deploy.tsx` — owner deploy page module.
- `frontend/owner-portal/src/pages/Deployment.tsx` — owner deployment page module.
- `frontend/owner-portal/src/pages/History.tsx` — owner history page module.
- `frontend/owner-portal/src/pages/NewApp.tsx` — owner newapp page module.
- `frontend/owner-portal/src/pages/Overview.tsx` — owner overview page module.
- `frontend/owner-portal/src/pages/SignIn.tsx` — owner signin page module.
- `frontend/owner-portal/src/shell/PortalShell.tsx` — responsive portal shell, navigation, theme, and session controls.
- `frontend/owner-portal/src/utils/presentation.ts` — owner date, commit, health, and operation phase presentation helpers.
- `openstack_platform/management/broker/anonymous.py` — private HMAC key, stateless expiring anonymous challenges, and bounded per-client-address limits.
- `tests/test_management_contract.py` — real project-socket broker contract and fake/real wire-shape, errors, cleanup, and read evidence.
- `openstack_platform/management/backup.py` — online SQLite backup, encrypted evidence, verification and offline session-invalidating restore.
- `tests/test_management_backup.py` — fourth-class backup, off-site compatibility, restore guards and full-loss drill tests.
- `tests/test_management_platform.py` — explicit ingress identity configuration and Nix hosting boundary checks.

- `tests/collect_owner_portal_artifacts.py` — bounded CI upload collector for fixture screenshots and sanitized API failure metadata.
- `tests/test_owner_portal_artifacts.py` — artifact whitelist, credential-field removal, symlink and size-bound coverage.

- `openstack_platform/owner_portal_config.py` — dependency-free public Commons inventory validation.
- `openstack_platform/management/settings.py` — operator rendering of production broker/web/identity configs.
- `openstack_platform/management/identity/__init__.py` — identity integration package boundary.
- `openstack_platform/management/identity/client.py` — bounded system-CA HTTPS Commons client and typed contract.
- `openstack_platform/management/identity/main.py` — broker-only Unix identity process and configuration-only readiness.
- `openstack_platform/management/dev/commons.py` — loopback HTTPS Commons bb78c5e contract double.
- `tests/test_management_identity.py` — Commons contract, TLS/peer limits and schema-2 migration evidence.
- `tests/test_management_dev.py` — long-checkout socket binding, private development directories, path limits and partial-startup cleanup.

- `openstack_platform/management_release.py` — commit-bound broker/web archives and authenticated asset compatibility evidence.
- `openstack_platform/management/entry.py` — isolated release-local service startup and no-network candidate smoke.
- `openstack_platform/management/installation.py` — group-inheriting staging, immutable payload/config checks, staged selection and pair activation requests.
- `openstack_platform/management/activation.py` — admin-image root activation validates the requested staged pair and atomically selects one active pair.
- `openstack_platform/management/rollback.py` — operator re-verification, smoke and normal activation request for a retained compatible pair and its own config snapshot.
- `frontend/owner-portal/scripts/build-receipt.mjs` — Node/Git/npm-lock and actual Vite output receipt.
- `tests/test_management_releases.py` — artifact trust, hostile archives, real-filesystem installation and explicit Node build integration.

## Keeping this guide current

When adding, deleting, or renaming a tracked file, update this guide in the same
change. `tests/test_documentation.py` checks that every path returned by
`git ls-files` has one backtick-delimited entry here. The check allows a new,
not-yet-added guide entry during local editing, but CI verifies it once the file
is tracked.
