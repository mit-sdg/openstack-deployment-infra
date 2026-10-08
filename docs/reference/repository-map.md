# Repository map

Find each tracked file and its purpose here. For runtime relationships, read the [internals reference](internals.md).

Files are grouped by directory, not by runtime call graph.

> [!NOTE]
> `tests/test_documentation.py` requires every tracked file exactly once and checks that listed paths exist. Update this page with file additions, renames, or deletions.

## Repository root

Metadata, pinned toolchains, and CI. Python, Nix, and shell share `infra/lib/platform_contract.json` ([infra](#infra)) for roles, accounts, ports, paths, and protocols.

- `.gitattributes` — marks committed dashboard output as generated.
- `.github/CODEOWNERS` — assigns reviewers for CI, Nix, infrastructure, and release documentation.
- `.github/workflows/ci.yml` — static, frontend, recipe, Nix, image publication, and acceptance checks.
- `.gitignore` — excludes credentials, private inventory, builds, environments, and temporary files.
- `LICENSE` — Apache License 2.0.
- `README.md` — platform overview and starting points.
- `flake.lock` — pinned Nix inputs; update with Nix.
- `flake.nix` — role configurations, images, packages, checks, development shell, and formatter.
- `pyproject.toml` — Python requirements, dependencies, entry points, and tool settings.
- `uv.lock` — locked Python dependencies for development, CI, and releases.

## config

Sanitized example configuration. Real inventory and credentials stay outside Git; see the [configuration reference](configuration.md).

- `config/live-acceptance-driver.example.json` — example acceptance runner, identity, paths, timeouts, and transcript settings.
- `config/offsite-export.example.json` — example export destination, mount identity, filesystem, and size bounds.
- `config/platform-policy.example.json` — example capacity, runtime image, and backup recipient policy.
- `config/platform.example.json` — non-secret inventory example shared by Python, Nix, and scripts.

## deploy

Tools that install and select releases on the operator host and the admin host. See [Releases and upgrades](../guides/releases-and-upgrades.md).

- `deploy/releases/bootstrap_operator_runtime.sh` — installs pinned tooling under `/srv/openstack-platform` without privileges.
- `deploy/releases/deploy_helper_release.sh` — copies and installs a helper release through pinned SSH.
- `deploy/releases/install_operator_config.py` — validates and atomically installs unprivileged operator configuration.
- `deploy/releases/install_release.py` — verifies, stages, smoke-tests, and selects releases.
- `deploy/releases/migrate_legacy_controller.py` — imports an authenticated legacy snapshot into the current schema.
- `deploy/releases/release_smoke.py` — checks entry points and helper actions before release selection.
- `deploy/releases/setup_operator_bridge.py` — generates and validates the pinned SSH and OpenStack bridge.
- `deploy/releases/systemd/openstack-platform-backup.service` — user service for off-host operator state backup.
- `deploy/releases/systemd/openstack-platform-backup.timer` — daily operator backup timer.

## docs

The documentation. Start at the [documentation home](../README.md).

- `docs/README.md` — reading paths, page index, and glossary.
- `docs/how-it-works.md` — platform tour, app builds, routing, data, and sign-in.
- `docs/security.md` — trust boundaries, access limits, and operator responsibilities.
- `docs/development.md` — local setup, CI checks, portal harness, and dashboard preview.
- `docs/images/architecture.svg` — architecture diagram for the README and system tour.
- `docs/guides/plan-a-deployment.md` — requirements and decisions before setup.
- `docs/guides/deploy-the-platform.md` — new platform setup and verification.
- `docs/guides/run-the-platform.md` — operator variables, health checks, dashboard, and teardown.
- `docs/guides/backups-and-recovery.md` — backup sets, off-site export, restores, and recovery drills.
- `docs/guides/troubleshooting.md` — symptoms, fixes, and recovery rules.
- `docs/guides/deploy-apps-from-the-command-line.md` — controller requests, rollback, sizing, retries, and IPv4 reservations.
- `docs/guides/open-the-portal.md` — portal installation, first admin, sign-in, staff, and quotas.
- `docs/guides/manage-apps-and-people.md` — portal apps, accounts, roles, quotas, and audit log.
- `docs/guides/for-app-owners.md` — student guide to deploying through the portal.
- `docs/guides/hosts-and-images.md` — image selection, host replacement, preflight, and pruning.
- `docs/guides/releases-and-upgrades.md` — signing, images, release installation, migrations, and live acceptance.
- `docs/reference/configuration.md` — setup and inventory settings.
- `docs/reference/internals.md` — components, state, lifecycles, portal internals, and failure rules.
- `docs/reference/controller-api.md` — controller transport and routes.
- `docs/reference/operator-cli.md` — every `openstack-platform` command.
- `docs/reference/repository-map.md` — every tracked file and its purpose.

## frontend

One npm workspace, two React apps, and a shared design system, using `frontend/package-lock.json`. See [frontend checks](../development.md#frontend-checks).

### Workspace root

- `frontend/DESIGN.md` — portal tokens, components, usage rules, status vocabulary, and copy.
- `frontend/THIRD_PARTY_NOTICES.md` — bundled dependency licences; shipped as `third-party-notices.txt`.
- `frontend/package.json` — workspaces, pinned toolchains, and shared check and build scripts.
- `frontend/package-lock.json` — locked dependency graph for all three packages.
- `frontend/scripts/check-freshness.mjs` — compares dashboard output with Git, including untracked files.
- `frontend/scripts/check-source.mjs` — rejects inline code, styles, HTML injection, and cross-app imports.
- `frontend/scripts/source-policy.test.mjs` — tests forbidden source patterns and import boundaries.
- `frontend/scripts/theme-plugin.ts` — emits shared `theme.js` and serves it during portal development.

### Shared design system

`@openstack-platform/ui` supplies components, tokens, and fonts without routing or networking. The dashboard uses its `legacy/` primitives.

- `frontend/shared/package.json` — component, CSS, and theme exports; React peer dependency.
- `frontend/shared/tsconfig.json` — strict TypeScript settings for the package.
- `frontend/shared/vite.config.ts` — Vitest settings for the package's tests.
- `frontend/shared/src/index.ts` — the package's public exports.
- `frontend/shared/src/tokens.css` — color, type, spacing, layout, shape, shadow, and motion tokens.
- `frontend/shared/src/ui.css` — component and utility styles in `ui-base` and `ui` layers.
- `frontend/shared/src/fonts.css` — self-hosted Latin fonts and metric-matched fallback.
- `frontend/shared/src/theme.js` — applies the saved theme before first paint.
- `frontend/shared/src/Icon.tsx` — the design system's SVG icon set.
- `frontend/shared/src/Mark.tsx` — the SVG brand mark.
- `frontend/shared/src/BoundaryText.tsx` — line-break points for long URLs and paths.
- `frontend/shared/src/components/Button.tsx` — buttons, variants, sizes, loading state, and spinner.
- `frontend/shared/src/components/Data.tsx` — responsive tables, lists, details, code blocks, and copy fields.
- `frontend/shared/src/components/Feedback.tsx` — status, alerts, errors, empty states, skeletons, and toasts.
- `frontend/shared/src/components/Field.tsx` — labelled form controls, hints, and errors.
- `frontend/shared/src/components/Overlay.tsx` — responsive dialogs, tabs, and segmented controls.
- `frontend/shared/src/components/Page.tsx` — pages, headers, sections, cards, layout, and signed-out flows.
- `frontend/shared/src/components/Shell.tsx` — brand, navigation, account menu, and phone menu.
- `frontend/shared/src/components/Text.tsx` — relative times and short copyable IDs.
- `frontend/shared/src/components/ThemeToggle.tsx` — system, light, and dark theme preferences.
- `frontend/shared/src/components.test.tsx` — component behavior and accessibility tests.
- `frontend/shared/src/presentation.test.tsx` — text escaping and independent theme preference tests.
- `frontend/shared/src/test-setup.ts` — test DOM cleanup and preference reset.
- `frontend/shared/src/legacy/Feedback.tsx` — legacy dashboard empty, loading, and error states.
- `frontend/shared/src/legacy/Layout.tsx` — legacy dashboard cards, activity rows, and shell.
- `frontend/shared/src/legacy/StatusBadge.tsx` — legacy dashboard status badge.
- `frontend/shared/src/legacy/ThemeButton.tsx` — legacy dashboard theme selector.
- `frontend/shared/src/legacy/primitives.css` — legacy dashboard controls, cards, and status styles.
- `frontend/shared/src/legacy/tokens.css` — legacy dashboard color, type, and status tokens.

### Owner portal

The portal serves app owners, staff, and admins. Portal API calls go through its web server to the broker; GitHub commit checks run in the browser. Ignored `dist/` output ships in portal releases.

#### Build and configuration

- `frontend/owner-portal/.prettierrc.json` — Prettier settings with print width 100.
- `frontend/owner-portal/package.json` — portal dependencies and development, check, build, and test scripts.
- `frontend/owner-portal/tsconfig.json` — strict TypeScript settings for the app and browser tests.
- `frontend/owner-portal/vite.config.ts` — build, harness proxy, third-party notices, and Vitest settings.
- `frontend/owner-portal/playwright.config.ts` — browser tests using fresh HTTPS, HTTP, or Vite harnesses.
- `frontend/owner-portal/index.html` — HTML entry with external scripts only.
- `frontend/owner-portal/gallery.html` — component gallery entry, served only during development.
- `frontend/owner-portal/public/favicon.svg` — the portal's browser icon.
- `frontend/owner-portal/scripts/build-receipt.mjs` — records commit, lockfile, Node.js version, and output hashes.

#### App core

- `frontend/owner-portal/src/hooks/useAppDestination.ts` — keeps the task navigation and app back link consistent with personal or class access.
- `frontend/owner-portal/src/hooks/useClassReads.tsx` — class read polling, hidden-tab pause, idle expiry and sequential refresh.
- `frontend/owner-portal/src/classApi.ts` — typed, cancellable People and Activity metadata reads.
- `frontend/owner-portal/src/appManagementApi.ts` — typed class app catalog, adoption, ownership and storage deletion requests.
- `frontend/owner-portal/src/main.tsx` — React root and in-memory query cache.
- `frontend/owner-portal/src/App.tsx` — sign-in, setup, task destinations and shared app routes.
- `frontend/owner-portal/src/api.ts` — typed requests, response checks, settings validation, and CSRF retries.
- `frontend/owner-portal/src/sizingApi.ts` — typed flavor lists, worker plans, builder settings, and size labels.
- `frontend/owner-portal/src/authOptions.ts` — shared query for CSRF, sign-in label, and platform name.
- `frontend/owner-portal/src/adminApi.ts` — account, quota, step-up, and audit requests.
- `frontend/owner-portal/src/styles/app.css` — shared font, token, and component stylesheet imports.
- `frontend/owner-portal/src/shell/PortalShell.tsx` — task navigation, theme toggle, and account menu.
- `frontend/owner-portal/src/hooks/useIntentPolling.ts` — polls activity and individual operations until completion.
- `frontend/owner-portal/src/hooks/useSession.ts` — session loading, sign-out, and expiry redirects.
- `frontend/owner-portal/src/test-setup.ts` — Vitest DOM matchers and cleanup.

#### Shared app and sign-in pages

- `frontend/owner-portal/src/pages/SignIn.tsx` — class sign-in, local accounts, and returned errors.
- `frontend/owner-portal/src/pages/Enrollment.tsx` — one-time account setup and reset with authenticator enrollment.
- `frontend/owner-portal/src/pages/Dashboard.tsx` — the app list and recent activity.
- `frontend/owner-portal/src/pages/NewApp.tsx` — create an app.
- `frontend/owner-portal/src/pages/Overview.tsx` — app state, deployment, start, stop, restart, and activity.
- `frontend/owner-portal/src/pages/Configuration.tsx` — source, build, runtime, environment, storage, and repository access.
- `frontend/owner-portal/src/pages/Deploy.tsx` — commit selection, grouped worker size plans, read-only build machine summary, checks, review, and deploy.
- `frontend/owner-portal/src/pages/PlatformSettings.tsx` — admin-only default build machine selection and progress.
- `frontend/owner-portal/src/pages/Deployment.tsx` — deployment details, runtime, build output, and startup record.
- `frontend/owner-portal/src/pages/History.tsx` — an app's deployment history.
- `frontend/owner-portal/src/pages/Logs.tsx` — an app's runtime logs.
- `frontend/owner-portal/src/pages/Team.tsx` — an app's team.
- `frontend/owner-portal/src/pages/app-pages.css` — app page layout using design tokens.

#### Class pages and account setup

- `frontend/owner-portal/src/pages/Audit.tsx` — admin-only account and app action audit.
- `frontend/owner-portal/src/pages/People.tsx` — all people, their apps and activity, with admin-only account controls.
- `frontend/owner-portal/src/pages/AllApps.test.tsx` — catalog status, conditional attention, and adaptive polling tests.
- `frontend/owner-portal/src/pages/AllApps.tsx` — searchable app catalog with status filters, create-for and adoption.
- `frontend/owner-portal/src/pages/Activity.tsx` — class activity with independently paged changes needing attention.
- `frontend/owner-portal/src/pages/admin/common.tsx` — step-up flow, owner picker, and admin error messages.
- `frontend/owner-portal/src/pages/admin/admin.css` — admin and account layout using tokens.
- `frontend/owner-portal/src/pages/admin/QrCode.tsx` — the authenticator setup QR code, drawn as SVG.

#### Components

- `frontend/owner-portal/src/components/AccountControls.tsx` — admin-only account creation, roles, limits, access and recovery controls on People.
- `frontend/owner-portal/src/components/DeploymentStatus.tsx` — deployment status joined with the original change and shared Resume action.
- `frontend/owner-portal/src/components/ClassRecords.tsx` — shared pagers, loading states and class activity adapters.
- `frontend/owner-portal/src/components/AppManagement.tsx` — elevated ownership and storage controls on shared Settings.
- `frontend/owner-portal/src/components/AppFrame.tsx` — app header, state, URL, and tabs.
- `frontend/owner-portal/src/components/AttentionActivity.test.tsx` — tests for the attention list: who sees it, the original actor, and Resume.
- `frontend/owner-portal/src/components/AttentionActivity.tsx` — an app's activity that needs attention (blocked or unknown), with who started each and a Resume button, for owners, staff, and admins.
- `frontend/owner-portal/src/components/BoundaryText.tsx` — re-exports the shared `BoundaryText`.
- `frontend/owner-portal/src/components/BuilderSize.tsx` — staff and admin Settings card for per-app build machine selection, reset, and progress.
- `frontend/owner-portal/src/components/Machines.tsx` — staff/admin Overview worker capacity and effective build machine, with sizing links.
- `frontend/owner-portal/src/components/Machines.test.tsx` — machine copy, capacity fallback, sizing links, and owner visibility tests.
- `frontend/owner-portal/src/components/SizeOptions.tsx` — size choices grouped by flavor family, sorted by CPU and RAM, with the current size marked.
- `frontend/owner-portal/src/components/CommitChecks.tsx` — pre-deploy checks, runtime versions, and review problems.
- `frontend/owner-portal/src/components/DeploymentRow.tsx` — deployment list row.
- `frontend/owner-portal/src/components/EnvironmentSection.tsx` — write-only environment editor and progress.
- `frontend/owner-portal/src/components/Feedback.tsx` — load-error messages and retryable `QueryError`, using shared presentation components directly.
- `frontend/owner-portal/src/components/LogViewer.tsx` — runtime output and error logs for owner/admin pages.
- `frontend/owner-portal/src/components/TimestampedLog.tsx` — local-time log columns, timestamp toggles, and UTC copy/download exports.
- `frontend/owner-portal/src/components/TimestampedLog.test.tsx` — timestamp parsing, legacy rendering, toggles, and log exports.
- `frontend/owner-portal/src/components/Mark.tsx` — re-exports the shared `Mark`.
- `frontend/owner-portal/src/components/Operation.tsx` — activity rows and lists.
- `frontend/owner-portal/src/components/RecentCommits.tsx` — browser-side GitHub commit picker.
- `frontend/owner-portal/src/components/Repository.tsx` — repository links labelled owner/repo.
- `frontend/owner-portal/src/components/RepositoryAccess.tsx` — private repository deploy key lifecycle and access checks.
- `frontend/owner-portal/src/components/StartupRecord.tsx` — failed startup summary, events, and output.
- `frontend/owner-portal/src/components/Status.tsx` — consistent state labels, tones, and badge rules.
- `frontend/owner-portal/src/components/StorageSection.tsx` — storage creation, bindings, checks, and credential rotation.
- `frontend/owner-portal/src/components/TeamSection.tsx` — team table with add, remove, and leave actions.
- `frontend/owner-portal/src/components/ThemeButton.tsx` — theme toggle with the portal's storage key.

#### Utilities

- `frontend/owner-portal/src/utils/presentation.ts` — times, IDs, runtime labels, app states, and activity titles.
- `frontend/owner-portal/src/utils/github.ts` — GitHub commit reads without cookies or referrers.
- `frontend/owner-portal/src/utils/preflight.ts` — public commit checks against build checkout rules.
- `frontend/owner-portal/src/utils/preflight-cases.json` — browser/Python checkout validation cases.
- `frontend/owner-portal/src/utils/runtimeVersions.ts` — repository runtime version parser matching Python.
- `frontend/owner-portal/src/utils/runtime-version-cases.json` — browser/Python runtime version request cases.

#### Component gallery

- `frontend/owner-portal/src/gallery/main.tsx` — gallery entry point.
- `frontend/owner-portal/src/gallery/Gallery.tsx` — every design system component in its variants and states.
- `frontend/owner-portal/src/gallery/gallery.css` — gallery-only layout.

#### Unit tests

- `frontend/owner-portal/src/App.test.tsx` — settings, conflicts, status, API decoding, CSRF, and runtime actions.
- `frontend/owner-portal/src/Sizing.test.tsx` — worker plan forwarding, size summaries, owner boundaries, and builder controls.
- `frontend/owner-portal/src/ClassPages.test.tsx` — task navigation, shared Resume, metadata validation and account step-up tests.
- `frontend/owner-portal/src/Routing.test.tsx` — nested routes and redirects from old staff links.
- `frontend/owner-portal/src/Shell.test.tsx` — branding, role navigation, and app list tests.
- `frontend/owner-portal/src/components/CommitChecks.test.tsx` — commit problems, versions, unreadable sources, and rate limits.
- `frontend/owner-portal/src/components/LogViewer.test.tsx` — log streams, refresh, and unavailable states.
- `frontend/owner-portal/src/components/RepositoryAccess.test.tsx` — deploy keys, access checks, and private commit reads.
- `frontend/owner-portal/src/components/StartupRecord.test.tsx` — startup wording and errors-first display.
- `frontend/owner-portal/src/components/TeamSection.test.tsx` — owner team management and member departure.
- `frontend/owner-portal/src/components/presentation.test.tsx` — activity, URLs, times, badges, and app states.
- `frontend/owner-portal/src/components/resources.test.tsx` — write-only resources, validation, reload recovery, and identity confirmations.
- `frontend/owner-portal/src/pages/Deployment.test.tsx` — reported build runtime and version source.
- `frontend/owner-portal/src/pages/SignIn.test.tsx` — class sign-in, local form, and errors.
- `frontend/owner-portal/src/pages/admin/QrCode.test.tsx` — generated QR code round-trip decoding.
- `frontend/owner-portal/src/utils/github.test.ts` — commit parsing, request options, and errors.
- `frontend/owner-portal/src/utils/preflight.test.ts` — shared cases, truncated trees, and requests.
- `frontend/owner-portal/src/utils/runtimeVersions.test.ts` — shared runtime cases through the browser parser.

#### Browser tests

- `frontend/owner-portal/e2e/helpers.ts` — shared sign-in, app creation, and settings steps for the smoke journeys.
- `frontend/owner-portal/e2e/owner-flow.spec.ts` — owner sign-in, app creation, deploy key, deployment, logs, and sign-out.
- `frontend/owner-portal/e2e/resources.spec.ts` — write-only environment variables and PostgreSQL bindings.
- `frontend/owner-portal/e2e/staff-flow.spec.ts` — staff finding an owner app and using its settings, people, and activity pages.

### Operator dashboard

The read-only [dashboard](../guides/run-the-platform.md) ships in `openstack_platform/dashboard/static/`, so operators need no Node.js.

- `frontend/operator-dashboard/package.json` — dashboard dependencies, build, watch, and test scripts.
- `frontend/operator-dashboard/tsconfig.json` — strict TypeScript settings for app and browser tests.
- `frontend/operator-dashboard/vite.config.ts` — reproducible static build without source maps or manifest.
- `frontend/operator-dashboard/playwright.config.ts` — one Python preview per browser-test scenario.
- `frontend/operator-dashboard/index.html` — HTML entry with external scripts and no inline styles.
- `frontend/operator-dashboard/public/favicon.svg` — dashboard browser icon source.
- `frontend/operator-dashboard/src/main.tsx` — dashboard React root.
- `frontend/operator-dashboard/src/App.tsx` — shell, view state, refresh, and connection status.
- `frontend/operator-dashboard/src/Applications.tsx` — app table, filters, sorting, and search.
- `frontend/operator-dashboard/src/Drawer.tsx` — read-only details, storage evidence, and copy buttons.
- `frontend/operator-dashboard/src/Icons.tsx` — dashboard SVG icons and status shapes.
- `frontend/operator-dashboard/src/Sections.tsx` — overview, attention, roles, operations, checks, and sources.
- `frontend/operator-dashboard/src/api.ts` — same-origin snapshot reads and guarded refresh requests.
- `frontend/operator-dashboard/src/app.css` — dashboard layout and print styles using shared tokens.
- `frontend/operator-dashboard/src/polling.ts` — single-flight polling, ETags, visibility, and refresh state.
- `frontend/operator-dashboard/src/presentation.tsx` — formatting, guarded external links, and route history.
- `frontend/operator-dashboard/src/snapshot.ts` — types for version 1 Python snapshots.
- `frontend/operator-dashboard/src/useSnapshot.ts` — snapshot subscription and one-second clock.
- `frontend/operator-dashboard/src/polling.test.ts` — polling cadence, refresh, reconnect, abort, and coalescing.
- `frontend/operator-dashboard/src/presentation.test.tsx` — hostile text, filters, focus, and time formatting.
- `frontend/operator-dashboard/src/test-fixtures.ts` — domain-generic snapshot fixtures.
- `frontend/operator-dashboard/src/test-setup.ts` — test DOM and mock cleanup.
- `frontend/operator-dashboard/e2e/dashboard.spec.ts` — scenarios, widths, themes, CSP, polling, accessibility, and print.

## infra

Internal scripts and payloads called by setup, operators, helpers, systemd, and CI.

### Backup and recovery

- `infra/backup/emit_garage_backup.py` — streams bucket inventory, key grants, and objects for backup.
- `infra/backup/emit_logical_backup.sh` — logical PostgreSQL, MongoDB, or Garage backup streams.
- `infra/backup/full_loss_recovery_drill.sh` — checks or runs full-loss recovery using escrowed keys.
- `infra/backup/garage_catalog.py` — Garage inventories, catalogs, read-only grants, and bucket ID remapping.
- `infra/backup/init_garage_backup_key.py` — creates and saves the read-only Garage backup key.
- `infra/backup/restore_garage_backup.py` — restores objects, keys, and grants with archive-path checks.
- `infra/backup/restore_managed_data.sh` — destructively restores a verified set into replacement services.
- `infra/backup/run_platform_backup.sh` — encrypted managed-data backups and evidence.
- `infra/backup/verify_garage_backup.py` — validates archives against current bucket inventory.
- `infra/backup/verify_latest_restore.sh` — test restores in disposable containers and result evidence.

### First-boot configuration

- `infra/cloud-init-nixos/admin.yaml` — admin first-boot template with state and backup volumes.
- `infra/cloud-init-nixos/builder.yaml` — single-use builder first-boot template and build inputs.
- `infra/cloud-init-nixos/ingress.yaml` — ingress first-boot template with optional tunnel token.
- `infra/cloud-init-nixos/storage.yaml` — storage first-boot template for managed data and registry.
- `infra/cloud-init-nixos/worker.yaml` — worker first-boot template without SSH access.

### Shared libraries

- `infra/lib/health_alert_config.py` — reads and validates the non-secret health alert settings in the inventory (shell-side copy).
- `infra/lib/health_alerts.py` — health alert state machine: consecutive-failure threshold, repeat interval, recovery message, and state kept on the admin volume.
- `infra/lib/http.py` — bounded monitoring and backup HTTP/JSON helpers.
- `infra/lib/owner_portal_config.py` — copied portal validator for infrastructure scripts.
- `infra/lib/platform-config.sh` — allowlisted shell projection of inventory.
- `infra/lib/platform_config.py` — inventory validation and shell projection.
- `infra/lib/platform_contract.json` — canonical roles, ports, accounts, paths, protocols, and inventory keys.
- `infra/lib/platform_contract.py` — strict contract loader for infrastructure scripts.
- `infra/lib/tls.py` — TLS contexts trusting the internal certificate authority.

### Monitoring and Nomad

- `infra/monitor/check_platform.py` — secret-free control-plane health snapshot.
- `infra/monitor/check_services.py` — authenticated admin service checks without printing credentials.
- `infra/monitor/send_health_alert.py` — private webhook sender: takes the URL on stdin, posts over verified HTTPS with no redirects, and never logs the URL.
- `infra/monitor/test_health_alert.py` — sends an operator test alert without changing alert state.
- `infra/nomad/bootstrap_acl.sh` — Nomad ACL bootstrap and protected scoped token files.

### OpenStack lifecycle

- `infra/openstack/apply_admin.sh` — creates or verifies admin server, fixed port, and volumes.
- `infra/openstack/apply_foundation.py` — creates or verifies security groups, rules, and fixed ports.
- `infra/openstack/apply_ingress.sh` — creates or verifies ingress server and fixed port.
- `infra/openstack/apply_storage.sh` — creates or verifies storage server, port, and data volume.
- `infra/openstack/builder_execute.py` — builder source reception and fixed rootless BuildKit execution.
- `infra/openstack/builder_lifecycle.sh` — builder server/port lifecycle with exact ownership checks.
- `infra/openstack/persistent-host.sh` — shared persistent server and volume reconciliation.
- `infra/openstack/pin_ephemeral_host_key.sh` — pins host SSH keys from provider console evidence.
- `infra/openstack/publish_nixos_image.sh` — verifies evidence and bytes, then publishes to Glance.
- `infra/openstack/render_host_user_data.py` — renders protected persistent-host cloud-init inputs.
- `infra/openstack/verify_persistent_host.py` — validates existing hosts before reuse.
- `infra/openstack/worker_lifecycle.sh` — worker server/port lifecycle with Nomad and ownership checks.

### PKI and registry

- `infra/pki/generate_internal_pki.sh` — internal CA and role/service certificates.
- `infra/registry/delete_manifest.py` — deletes one controller-owned registry image manifest.
- `infra/registry/registry-gc.sh` — offline storage-host registry garbage collection.

## nix

NixOS definitions for the five role images, the packages they contain, and the VM tests.

- `nix/lib/constants.nix` — validated constants from the shared contract.
- `nix/lib/controller-paths.nix` — controller file preparation plan for preflight and apply.
- `nix/lib/inventory.nix` — strict inventory validation for Nix evaluation.
- `nix/modules/common.nix` — shared accounts, cloud-init, networking, security, logging, and paths.
- `nix/pkgs/default.nix` — pinned tools, Python packages, release tooling, and helper launcher.
- `nix/pkgs/openstacksdk-security-group-project-alias.patch` — reads security group ownership from older Neutron responses.
- `nix/roles/admin.nix` — Nomad, controller/helper, portal, monitoring, backups, and state mounts.
- `nix/roles/builder.nix` — rootless BuildKit, restricted SSH, blocked metadata, and expiry.
- `nix/roles/ingress.nix` — Traefik, tunnel/direct listeners, routes, and trusted forwarders.
- `nix/roles/storage.nix` — databases, Garage, registry, TLS, firewall, and data mount.
- `nix/roles/worker.nix` — Nomad client, Docker, isolation, registry trust, and no SSH.
- `nix/tests/default.nix` — role VM tests with inventory, certificates, and service fixtures.

## openstack_platform

Installed commands (`pyproject.toml`) and shared code. The controller owns product state, the helper performs trusted actions, and `management` supplies the portal. Top-level modules handle setup, operators, and shared foundations.

### Operator and setup

- `openstack_platform/operator.py` — operator setup, status, dashboard, backup, restore, and infrastructure CLI.
- `openstack_platform/setup.py` — resumable preflight, releases, images, provisioning, and service setup.
- `openstack_platform/config.py` — typed inventory and private operator policy validation.
- `openstack_platform/installation.py` — fixed installed command paths.
- `openstack_platform/remote.py` — version 1 helper protocol and pinned local/SSH transport.
- `openstack_platform/openstack.py` — bounded image, power, and persistent-host replacement operations.
- `openstack_platform/fixed_ip.py` — retained worker primary ports, capability checks, and ownership validation.
- `openstack_platform/floating_ip.py` — project-scoped floating IPv4 operations and ownership checks.
- `openstack_platform/host_keys.py` — console/keyscan evidence checks and atomic SSH key pinning.
- `openstack_platform/host_paths.py` — controller file preparation through handles without following links.
- `openstack_platform/host_user_data.py` — protected input validation and persistent-role cloud-init rendering.
- `openstack_platform/ingress_credentials.py` — non-persistent tunnel token validation for ingress replacement.

### Releases, images, and acceptance

- `openstack_platform/release_manifest.py` — manifests, SBOM, provenance, signatures, artifact binding, and bundles.
- `openstack_platform/image_pipeline.py` — retained image builds, evidence, and publication without rebuilding.
- `openstack_platform/management_release.py` — commit-bound portal archive builds and verification.
- `openstack_platform/acceptance.py` — disposable acceptance plans, checkpoints, evidence, and verification.
- `openstack_platform/acceptance_live_driver.py` — live acceptance through operator commands and controller API.

### Backups and recovery

- `openstack_platform/backup_retention.py` — controller/portal backup pruning by age and count, manifest first.
- `openstack_platform/recovery_bundle.py` — append-only off-site bundle export, verification, import, and scheduling.
- `openstack_platform/restore.py` — offline validation and atomic controller database replacement.

### Shared foundations

- `openstack_platform/__init__.py` — package description and helper protocol version.
- `openstack_platform/contracts.py` — typed shared contract access.
- `openstack_platform/durable.py` — file replacement with `fsync` and no link following.
- `openstack_platform/health_alert_config.py` — reads and validates the non-secret health alert settings in the inventory (packaged copy).
- `openstack_platform/runtime.py` — private directories, locks, bounded I/O, redaction, and diagnostics.
- `openstack_platform/log_timestamps.py` — bounded timestamped build-log byte sink for admin recording.
- `openstack_platform/runtime_versions.py` — repository runtime requests, supported semver, and build runtime validation.
- `openstack_platform/validation.py` — name, UUID, commit, URL, path, digest, and text validators.
- `openstack_platform/owner_portal_config.py` — dependency-free portal inventory validation.

### Controller

The admin-host controller owns app state in SQLite. See the [controller API](controller-api.md).

- `openstack_platform/controller/__init__.py` — package marker for the controller.
- `openstack_platform/controller/main.py` — controller startup and composition.
- `openstack_platform/controller/api.py` — routes, services, helper transport, and socket authority split.
- `openstack_platform/controller/builder_settings.py` — effective per-app builder sizes and audited default and override selections.
- `openstack_platform/controller/http.py` — bounded Unix HTTP/1.1 server with peer checks.
- `openstack_platform/controller/database.py` — SQLite schema, migrations, identity, journals, and state operations.
- `openstack_platform/controller/async_operations.py` — bounded worker pool serializing changes per app.
- `openstack_platform/controller/service_support.py` — shared deadlines, helper transport, and mutation guards.
- `openstack_platform/controller/status.py` — infrastructure, app, storage, operation, and live-state read models.
- `openstack_platform/controller/application_models.py` — immutable storage, configuration, and recipe value objects.
- `openstack_platform/controller/application_service.py` — app creation, start, stop, and deletion.
- `openstack_platform/controller/application_runtime.py` — source, recipes, builds, workers, health, cleanup, and retention.
- `openstack_platform/controller/log_timestamps.cjs` — runtime-native container entrypoint with byte-safe timestamps and process-group signal forwarding.
- `openstack_platform/controller/deployment_config.py` — typed configuration parsing and checkout validation.
- `openstack_platform/controller/deployment_reads.py` — allowlisted deployment configuration and source reads.
- `openstack_platform/controller/deployment_service.py` — deployment build, cutover, acceptance, and recovery.
- `openstack_platform/controller/maintenance.py` — planned app stop and worker removal after building.
- `openstack_platform/controller/rollback.py` — retained deployment validation and rollback review plans.
- `openstack_platform/controller/worker_reuse.py` — read-only acceptance checks for worker reuse.
- `openstack_platform/controller/sizing.py` — worker size plans, capacity budgets, and service reserves.
- `openstack_platform/controller/nomad_jobs.py` — job rendering, placement checks, and route identity validation.
- `openstack_platform/controller/environment_service.py` — write-only environment variable changes.
- `openstack_platform/controller/finishing_retries.py` — durable, bounded retries of finishing work (removing the previous version) after a deployment goes live.
- `openstack_platform/controller/storage.py` — managed storage state machine and helper calls.
- `openstack_platform/controller/storage_contract.py` — storage ownership, secret keys, and environment mappings.
- `openstack_platform/controller/storage_service.py` — managed storage request validation and execution.
- `openstack_platform/controller/log_service.py` — bounded runtime and saved build log reads.
- `openstack_platform/controller/image_service.py` — role image selection and worker/builder provisioning snapshots.
- `openstack_platform/controller/seed_images.py` — initial selections preserving newer records after admin replacement.
- `openstack_platform/controller/fixed_ip_service.py` — journaled retained primary port reservations.
- `openstack_platform/controller/public_ip_service.py` — journaled outbound IPv4 reservations, handovers, and releases.
- `openstack_platform/controller/hosted_backup.py` — encrypted committed controller database backups.
- `openstack_platform/controller/source_key_backup.py` — deploy key backups and offline restore staging.

### Helper

The unprivileged admin-host helper accepts only `actions-v1.txt` actions.

- `openstack_platform/helper/__init__.py` — package marker for the helper.
- `openstack_platform/helper/actions-v1.txt` — release-bound allowlist of helper actions.
- `openstack_platform/helper/main.py` — single-request dispatch, backup evidence, and retention handling.
- `openstack_platform/helper/production.py` — on-demand action handlers and service clients.
- `openstack_platform/helper/errors.py` — shared helper error types.
- `openstack_platform/helper/application_actions.py` — Nomad deployment, health, promotion, logs, removal, and environment actions.
- `openstack_platform/helper/nomad.py` — owned-key Variable reads and compare-and-set updates.
- `openstack_platform/helper/worker_capacity.py` — Nomad worker readiness and allocatable CPU/memory reads.
- `openstack_platform/helper/registry_artifact.py` — read-only registry manifest and layer checks.
- `openstack_platform/helper/runtime_images.py` — matching official `-slim` runtime images pinned by digest.
- `openstack_platform/helper/storage.py` — PostgreSQL, MongoDB, Garage operations, and credentials.

### Operator dashboard

Run with `openstack-platform dashboard`. Committed `static/` output comes from `npm --prefix frontend run build`.

- `openstack_platform/dashboard/__init__.py` — package description for the dashboard.
- `openstack_platform/dashboard/model.py` — role, app, operation, check, and issue status from evidence.
- `openstack_platform/dashboard/preview.py` — synthetic scenarios using production parsers and status rules.
- `openstack_platform/dashboard/server.py` — private Unix HTTP server, asset allowlist, headers, and CLI.
- `openstack_platform/dashboard/service.py` — single-flight refresh, last good data, route history, and snapshots.
- `openstack_platform/dashboard/sources.py` — fixed admin reader, strict parsers, and public probes.
- `openstack_platform/dashboard/static/assets/index-DrkvrrYi.js` — generated dashboard JavaScript bundle.
- `openstack_platform/dashboard/static/assets/style-D_0t2pU2.css` — generated dashboard stylesheet.
- `openstack_platform/dashboard/static/favicon.svg` — generated dashboard browser icon.
- `openstack_platform/dashboard/static/index.html` — generated HTML with external scripts and stylesheet.
- `openstack_platform/dashboard/static/theme.js` — generated shared theme script.

### Owner portal services

The admin-host portal services (`management-web`, `management-broker`, `management-identity`) and their release, backup, and development tools.

#### Shared configuration

- `openstack_platform/management/__init__.py` — package marker without optional imports.
- `openstack_platform/management/common.py` — strict parsing, canonical data, digests, tokens, and UTC times.
- `openstack_platform/management/config.py` — closed portal configuration and loopback-only development validation.
- `openstack_platform/management/settings.py` — production service configuration rendering from inventory.

#### Broker

The broker authorizes portal actions and owns accounts, sessions, ownership, quotas, and audit. Only the broker calls the controller.

- `openstack_platform/management/broker/__init__.py` — package marker for the broker.
- `openstack_platform/management/broker/main.py` — production broker entry point without development imports.
- `openstack_platform/management/broker/app_management.py` — shared app authority, catalog filters, sizing, adoption, ownership and storage deletion.
- `openstack_platform/management/broker/sizing.py` — closed flavor projections, cached capacity enrichment, and controller sizing reads.
- `openstack_platform/management/broker/builder_settings.py` — admin-only default builder size reads and durable audited changes.
- `openstack_platform/management/broker/class_reads.py` — bounded audited People and Activity projections.
- `openstack_platform/management/broker/api.py` — owner routes with ownership and quota checks.
- `openstack_platform/management/broker/auth.py` — Commons/local sign-in, sessions, CSRF, and sign-out.
- `openstack_platform/management/broker/anonymous.py` — signed anonymous challenges and per-address request budgets.
- `openstack_platform/management/broker/local_auth.py` — local sign-in, guessing budgets, and recognized browsers.
- `openstack_platform/management/broker/local_auth_limits.py` — sign-in admission and per-browser failure windows.
- `openstack_platform/management/broker/local_security.py` — scrypt passwords, TOTP, and hashed one-time links.
- `openstack_platform/management/broker/known_device.py` — returning-browser recognition cookies, separate from sign-in credentials.
- `openstack_platform/management/broker/bootstrap.py` — first admin bootstrap CLI and one-time setup links.
- `openstack_platform/management/broker/accounts.py` — admin accounts, step-up, setup/reset links, and audit.
- `openstack_platform/management/broker/members.py` — app teams and activity feeds.
- `openstack_platform/management/broker/resources.py` — environment and storage routes with secret-free results.
- `openstack_platform/management/broker/runtime_logs.py` — authorized runtime logs with bounded shared reads.
- `openstack_platform/management/broker/source_keys.py` — audited public deploy key routes and serialized access checks.
- `openstack_platform/management/broker/staff_policy.py` — staff metadata limits and URL checks.
- `openstack_platform/management/broker/journal.py` — durable mutations, same-key retries, polling, and recovery.
- `openstack_platform/management/broker/database.py` — broker SQLite schema, migrations, and short transactions.
- `openstack_platform/management/broker/client.py` — bounded project-socket client without network fallback.

#### Identity and web

- `openstack_platform/management/identity/__init__.py` — package marker for the identity service.
- `openstack_platform/management/identity/main.py` — identity Unix socket service restricted to the broker.
- `openstack_platform/management/identity/client.py` — HTTPS Commons code redemption with system certificate authorities.
- `openstack_platform/management/web/__init__.py` — package marker for the web service.
- `openstack_platform/management/web/main.py` — static serving and broker forwarding entry point.
- `openstack_platform/management/web/server.py` — bounded HTTP, fixed forwarding, cookies, redirects, and security headers.

#### Releases, installation, and backup

- `openstack_platform/management/entry.py` — isolated release service startup and offline smoke checks.
- `openstack_platform/management/installation.py` — verified release staging and web/broker activation requests.
- `openstack_platform/management/activation.py` — root validation and atomic activation of staged pairs.
- `openstack_platform/management/rollback.py` — retained portal pair verification and reactivation CLI.
- `openstack_platform/management/backup.py` — encrypted broker backups and offline restore invalidating sessions.

#### Local development harness

- `openstack_platform/management/dev/__init__.py` — loopback-only development package marker.
- `openstack_platform/management/dev/__main__.py` — local web, broker, identity, fake Commons, and controller harness.
- `openstack_platform/management/dev/commons.py` — fake approval, single-use codes, and redemption.
- `openstack_platform/management/dev/controller.py` — fake project-socket controller with state and fault injection.

## tests

Python `unittest` modules grouped by boundary. See [test layout](../development.md#understand-the-test-layout).

### Fixtures and scripts

- `tests/fixtures/apps/bun/bun.lock` — locked dependencies for the Bun fixture app.
- `tests/fixtures/apps/bun/package.json` — the Bun fixture app's scripts and dependency.
- `tests/fixtures/apps/bun/server.ts` — the Bun fixture app: an HTTP server with a health endpoint.
- `tests/fixtures/apps/node/package-lock.json` — locked dependencies for the Node.js fixture app.
- `tests/fixtures/apps/node/package.json` — the Node.js fixture app's start script and metadata.
- `tests/fixtures/apps/node/server.js` — the Node.js fixture app: an HTTP server with a readiness endpoint.
- `tests/fixtures/openstack/glance_quota_formatter_outputs.json` — recorded Glance quota outputs, including unknown and unlimited values.
- `tests/fixtures/openstack/neutron_security_group_tenant_only.json` — a sanitized older Neutron response that names the owner only as a tenant.
- `tests/fixtures/openstack/provider_uuid_outputs.json` — compact and canonical UUID forms printed by different OpenStack tools.
- `tests/fixtures/retained_openstack.py` — offline stand-in for the OpenStack CLI and Nomad, so real helper and shell code runs unchanged.
- `tests/product_fixtures.py` — builders for accepted app and deployment state.
- `tests/repository_fixtures.py` — creates clean temporary Git repositories for tests that need a committed checkout.
- `tests/collect_owner_portal_artifacts.py` — collects the size-limited, credential-free portal test artifacts CI uploads on failure.
- `tests/install_ci_apt_packages.sh` — installs fixed CI packages with retries and a time limit.
- `tests/smoke_generated_recipes.sh` — generates, builds, starts, and health-checks the Node.js and Bun recipes with rootless Podman.
- `tests/smoke_openstack_image.sh` — boots one role image in QEMU with a config drive and waits for it to finish first boot.

### Controller, helper, and storage

- `tests/test_controller_api.py` — route wiring, the socket split, responses, idempotency, lock-free reads, and service integration.
- `tests/test_controller_database.py` — schema, migrations, identity, journals, state changes, and database recovery.
- `tests/test_controller_hosting.py` — how Nix hosts the controller: accounts, sockets, backups, and services.
- `tests/test_controller_http.py` — Unix socket HTTP parsing, deadlines, keep-alive, peer checks, overload, and shutdown.
- `tests/test_controller_images.py` — image selection compare-and-set, provider checks, recovery, and pinned provisioning.
- `tests/test_controller_recovery.py` — storage recovery, rejected builds, crash and retry, and lock waits.
- `tests/test_controller_seed_images.py` — initial image seeding: identity, selection, and repeat runs.
- `tests/test_legacy_controller_migration.py` — rejecting legacy markers and importing legacy app, storage, and deployment state.
- `tests/test_hosted_controller_backup.py` — controller database backups: encryption, evidence, permissions, cleanup, and retention.
- `tests/test_helper_application_actions.py` — helper Nomad actions: deploy, ownership, health, promotion, environment, logs, and removal.
- `tests/test_helper_registry_artifact.py` — registry image checks: missing content, tampered digests, and secret-safe results.
- `tests/test_helper_worker_capacity.py` — worker capacity reads against the pinned Nomad API fields, deadlines, and failures.
- `tests/test_platform_services.py` — app, deployment, environment, storage, and log services, including helper failures.
- `tests/test_platform_storage.py` — the controller's storage state machine and the helper's PostgreSQL, MongoDB, and Garage operations.
- `tests/test_platform_foundation_nomad.py` — Nomad Variable merges and compare-and-set limited to owned keys.

### Builds and deployments

- `tests/test_application_runtime.py` — source fetch, recipes, BuildKit, workers, deployment, cleanup, and retention.
- `tests/test_log_timestamps.py` — Node.js and Bun wrapper streams, partial and long lines, binary bytes, exit and signal behavior, and build recording.
- `tests/test_application_health_observation.py` — health path and route checks, and stopped apps.
- `tests/test_application_sizing.py` — worker sizes, per-app plans, resizing, retries, and rollback.
- `tests/test_portal_sizing.py` — project sizing routes, builder defaults and overrides, schema migration, and recovery.
- `tests/test_application_deployment_docs.py` — checks that the command-line deploy guide's examples are valid Bash and valid configuration.
- `tests/test_build_failure_cleanup.py` — classifying rejected builds and confirming the builder and registry image are gone.
- `tests/test_checkout_preflight_parity.py` — runs the shared preflight cases through the build's `validate_checkout`.
- `tests/test_deployment_config.py` — typed deployment configuration and Git branch and ref resolution.
- `tests/test_deployment_runtime.py` — the controller's checks and records of the runtime each build used.
- `tests/test_runtime_versions.py` — the shared runtime version cases and checks of a build's reported runtime.
- `tests/test_runtime_images.py` — resolving versions to pinned images against a fake nodejs.org and Docker Hub.
- `tests/test_retained_rollback.py` — rollback without rebuilding: reads, storage drift, current secrets, recovery, and address handover order.
- `tests/test_worker_reuse.py` — same-worker deployments, retained-image rollback, drift, failure cleanup, and controller restarts.
- `tests/test_private_repositories.py` — deploy keys for private GitHub repositories: pinned host key, fetch, access checks, and key lifecycle.
- `tests/test_fixed_ip.py` — retained primary ports through the HTTP, helper, and shell layers, including drift and lost responses.
- `tests/test_public_ip.py` — floating IPv4 reservation, ownership, retries, handover, and release.

### Owner portal

- `tests/test_management.py` — broker sign-in, ownership and limit races, change recovery, and the web and Unix transports.
- `tests/test_management_accounts.py` — local credentials, one-time setup links, roles, and portal admin account actions.
- `tests/test_management_admin_apps.py` — staff and portal admin app authority, admin-only actions, adoption, audit, and secrecy.
- `tests/test_management_backup.py` — broker backups, retention, off-site compatibility, restore guards, and the full-loss drill.
- `tests/test_management_contract.py` — the broker against the real controller project router and the fake controller.
- `tests/test_management_dev.py` — the local harness: socket paths in deep checkouts, private directories, and cleanup after a failed start.
- `tests/test_management_identity.py` — the Commons Connect redeem contract, TLS and peer limits, failures, and schema upgrades.
- `tests/test_management_login_admission.py` — guessing budgets across accounts and browsers, recognized devices, and reserved capacity.
- `tests/test_management_platform.py` — portal inventory settings and how Nix hosts the portal services.
- `tests/test_management_releases.py` — portal release archives: trust checks, hostile archives, installation, and an opt-in real Node.js build.
- `tests/test_management_runtime_logs.py` — runtime log access, streams, sharing, and bounded tails.
- `tests/test_management_source_keys.py` — deploy key ownership, removal, audit, and shared access checks.
- `tests/test_management_staff.py` — staff metadata views, limits, paging, audit, and role sessions.
- `tests/test_management_teams.py` — app teams: members, owner-only changes, leaving, limits, and admin reassignment.
- `tests/test_owner_resources.py` — environment and storage through the real controller project router, with no secret values exposed.
- `tests/test_owner_portal_artifacts.py` — the CI artifact collector's allowlist, credential removal, links, and size limits.

### Operator, setup, and infrastructure

- `tests/test_operator_bridge.py` — the operator's SSH and OpenStack bridge: preflight, generated wrappers, key pinning, and drift.
- `tests/test_operator_integration.py` — operator commands and the helper protocol against fake dependencies.
- `tests/test_operator_live_health.py` — operator status from the hosted controller, and tunnel readiness.
- `tests/test_platform_setup.py` — setup: environment parsing, preflight, inventory generation, controller gates, resume, and the CLI.
- `tests/test_platform_openstack.py` — image selection and pruning, and persistent host power, replacement, and recovery.
- `tests/test_platform_host_keys.py` — console fingerprints, keyscan, known-hosts matching, drift, and atomic pinning.
- `tests/test_host_user_data.py` — protected input checks and cloud-init rendering for each role.
- `tests/test_host_paths.py` — admin host file preparation refuses links, FIFOs, wrong owners, and replacement races.
- `tests/test_ingress_credentials.py` — tunnel token checks, rotation, cleanup, and non-persistence.
- `tests/test_openstack_lifecycle_scripts.py` — shell lifecycle scripts: exact resources, ownership, ambiguity, and deletion.
- `tests/test_verify_persistent_host.py` — validating an existing persistent host before reuse.
- `tests/test_neutron_sdk_projection.py` — the patched OpenStack SDK against an older Neutron response; runs in the Nix package smoke test.
- `tests/test_infra_http.py` — the infrastructure HTTP helper: redirects, size limits, status codes, and JSON.
- `tests/test_namespace.py` — the deployment namespace across Python and Nix sources, and inventory validation.
- `tests/test_platform_contract.py` — the contract matches across JSON, Python, Nix, shell, and the packaged copy.
- `tests/test_platform_foundation_runtime_remote.py` — runtime limits and redaction, the helper protocol, backup acceptance, and paths.
- `tests/test_platform_foundation_validation_config.py` — shared validators and inventory and policy parsing.
- `tests/test_dashboard.py` — the operator dashboard: reader, parsers, probes, status, refresh, HTTP security, command, and static assets.

### Backups and recovery

- `tests/test_garage_backup.py` — Garage backups: read-only grants, complete coverage, bounded archives, and recovery.
- `tests/test_managed_data_backup.py` — managed-data backups and compatibility with older restores.
- `tests/test_full_loss_recovery_drill.py` — the full-loss recovery drill in full and check-only modes.
- `tests/test_platform_restore.py` — offline controller restore: validation, permissions, and atomic replacement.
- `tests/test_recovery_bundle.py` — off-site bundles: export, import, manifest limits, mount checks, scheduling, and receipts.

### Releases, images, and acceptance

- `tests/test_packaging_release.py` — release archives: identity, runtime paths, configuration checks, the installer, and atomic selection.
- `tests/test_release_manifest.py` — release manifests, SBOM, provenance, signatures, bundles, and source binding.
- `tests/test_role_artifact_manifest.py` — role image evidence after a build, and tamper detection.
- `tests/test_image_pipeline.py` — retained image bytes, CI run identity, signed promotion, and publication gates.
- `tests/test_ci_publication.py` — the CI paths that trigger image publication.
- `tests/test_controller_finishing.py` — finishing retries: recovery without rebuilding, restart persistence, exhaustion, allowed and blocked changes meanwhile, and secret-free failure logging.
- `tests/test_live_acceptance.py` — live acceptance plans, checkpoints and resume, the evidence chain, and signatures.
- `tests/test_live_acceptance_driver.py` — the live acceptance driver: protocol, observations, interruption, recovery, and teardown.

### Repository checks

- `tests/test_documentation.py` — documentation checks: the set of pages, links and section anchors, images, required facts, no production domain or retired names, and this map against `git ls-files`.
- `tests/test_hardening_properties.py` — generated cases for durable writes, parsers, state boundaries, idempotency, and secret redaction.
- `tests/test_health_alerts.py` — health alert thresholds, repeats, recovery, message formatting without secrets, HTTPS-only URLs, timeouts, and delivery failures not affecting health.
