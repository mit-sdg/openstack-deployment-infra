# Validate a repository change

Use this workflow to create the locked Python environment and run the checks
that can execute without OpenStack credentials. Python commands require Python
3.14; Nix outputs support `x86_64-linux`.

Deployment setup is a separate operator procedure. Follow [Deploy the
platform](DEPLOYMENT.md) instead of using development commands to provision
infrastructure.

## Create the Python environment

From a clean repository checkout with [uv](https://docs.astral.sh/uv/)
installed:

```sh
uv sync --frozen
uv run python --version
```

The reported Python version must be 3.14.x. `uv.lock` is authoritative for the
resolved Python environment; change `pyproject.toml` first and regenerate the
lock when intentionally changing dependencies.

## Run the Python checks

Run the unit and integration-style test suite:

```sh
uv run python -m unittest discover -s tests -q
```

Run the static checks used by CI:

```sh
uv lock --check
uv run ruff format --check openstack_platform deploy/releases infra tests
uv run ruff check openstack_platform deploy/releases infra tests
uv run mypy
uv run vulture openstack_platform deploy infra tests --min-confidence 80 --sort-by-size
uv run python -m compileall -q openstack_platform deploy infra tests
```

Vulture succeeds silently when it finds no code at or above the configured
confidence threshold.

## Preview the operator dashboard

The frontend npm workspace contains `shared` (`@openstack-platform/ui`),
`owner-portal` and `operator-dashboard`, with one `frontend/package-lock.json`.
Use Node 24.19.0 and npm 11.17.0. Shared contains presentation and theme tokens;
the source check rejects app-to-app imports, owner/operator imports into shared,
network/router dependencies in shared, and first-party inline code/styles.

Serve the dashboard with synthetic evidence, then open `http://localhost:8470`:

```sh
uv run python -m openstack_platform.dashboard.preview --scenario mixed
```

The preview listens on loopback TCP, reads only
`config/platform.example.json`, and never contacts OpenStack or an admin host.
Its fixtures pass through the production parsers and status rules. Scenarios
are `mixed` (the default), `healthy`, `outage`, `unreachable` (admin reads fail
after the first refresh), and `empty`. `--port`, `--interval`, and `--delay`
(seconds added to each refresh, for the loading state) adjust it. The preview
uses the committed React production bundle under
`openstack_platform/dashboard/static/` and needs no Node to run. It reloads each
complete distribution and keeps the previous one while a rebuild is incomplete;
restart it after Python changes. Production loads its asset allowlist once.

To change the UI, install the locked workspace and rebuild the committed output:

```sh
npm --prefix frontend ci
npm --prefix frontend run format:check
npm --prefix frontend run format:check --workspaces
npm --prefix frontend run check:source
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run smoke
```

The owner build emits its ignored `dist/` and commit-bound receipt; the dashboard
build replaces its committed `static/` directory. Commit every generated addition
and deletion with the frontend changes, and update their tracked-file guide
entries. Its only files are HTML, JS, CSS and SVG; no source maps or Vite manifest
are committed. The generated output contains no commit, build time or checkout
path. `npm --prefix frontend/operator-dashboard run dev` runs a production build
watcher; keep the Python preview running alongside it. Vite's HMR server injects
development code/styles and is not the dashboard's strict-CSP preview.

The required `dashboard-frontend` CI job pins Node/npm, checks all workspaces,
rebuilds both apps, runs `git diff --exit-code -- openstack_platform/dashboard/static`
and checks the full Git status including untracked output. After committing the
generated files, verify locally with:

```sh
npm --prefix frontend/operator-dashboard run build
npm --prefix frontend run check:freshness
```

Playwright uses the Python preview with all five scenarios and delayed collection.
It checks polling/ETags, refresh, reconnect, drawers and preserved view state,
320/390/768/1440 px in light/dark, reduced motion, forced colors and print. It
collects CSP violations and unexpected requests under the production policy.
Synthetic screenshots are written to `.tmp/dashboard-screenshots/after/`, with
one `<width>-<light|dark>.png` for each viewport/theme pair. No live systems are
needed for these checks.

## Run shell and document checks

Bash syntax can be checked without provider credentials:

```sh
find deploy infra tests -type f -name '*.sh' -exec bash -n {} +
```

CI also runs ShellCheck and validates every example JSON file plus
`docs/architecture-overview.svg`. The documentation tests check relative links,
current control-surface claims, and the tracked-file index:

```sh
uv run python -m unittest tests.test_documentation -v
```

## Run Nix checks

A Linux host with flakes enabled and access to the Nix daemon can evaluate the
flake without building its outputs:

```sh
nix flake check --no-build --print-build-logs
```

Run the repository formatter separately and review any resulting changes:

```sh
nix fmt -- .
git diff -- '*.nix'
```

Building images, running role VM tests, and smoke-testing QCOW2 files require
additional Nix/QEMU resources. Follow [Build and test role
images](MAINTENANCE.md#build-and-test-role-images) for those workflows.

## Understand the test layout

Tests use only the standard-library `unittest` runner. Test modules generally
mirror a production boundary rather than one source file. For example,
`test_platform_storage.py` spans controller and helper storage behavior, while
`test_platform_foundation_runtime_remote.py` covers local runtime, protocol,
and backup-acceptance foundations. The [tracked-file
guide](REPOSITORY_GUIDE.md#tests) states the scope of each fixture, smoke script,
and test module.

Tests that need clean-repository behavior copy the current source into a
temporary Git repository through `tests/repository_fixtures.py`. You therefore
do not need to commit a change before running the suite; production release and
setup commands still enforce their own clean-checkout requirements.

## Run the local owner portal

The portal is implemented locally and is not deployed. Sign-in follows Commons
commit `bb78c5e`: the portal sends the student's exact username and password
through a broker-only Unix identity service to Commons over HTTPS. No password
is stored, audited or logged. The harness replaces Commons and the controller
with loopback doubles; it does not call any live platform or class application.

Use Python 3.14, Node 24.19.0 and npm 11.17.0. The source-only UI package and
owner portal and operator dashboard are npm workspaces rooted in `frontend/`, with one lockfile.
Shared UI components contain presentation only; owner routing and API calls
remain in the portal. Run the import/CSP source check after frontend edits:

```sh
uv sync --frozen
export PATH=/home/agent/.local/node-v24.19.0/bin:$PATH
npm --prefix frontend ci
npm --prefix frontend run check:source
npm --prefix frontend run format:check --workspaces
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend/owner-portal run format:check
npm --prefix frontend/owner-portal run typecheck
npm --prefix frontend/owner-portal test
npm --prefix frontend/owner-portal run build
uv run python -m openstack_platform.management.dev
```

Open `https://127.0.0.1:9443/sign-in` and accept the disposable loopback
certificate. Fixture credentials are `alice` / `local-alice-password`, `bob` /
`local-bob-password`, archived `carol` / `local-carol-password`, and instructor
`taylor` / `local-taylor-password`. They are
public test data, never real class credentials. The fake Commons authenticate
server always uses HTTPS, including when the portal uses HTTP/Vite. The identity
client trusts only the harness's public development CA in development mode.
Private TLS keys remain in memory via Linux memfd. Only development uses
cryptography; production identity uses stdlib TLS and system CAs, with no PyJWT.

The configuration page also supports write-only environment edits and
PostgreSQL/MongoDB/S3 creation, status, bindings, verification and credential
rotation. The development controller persists only environment names and
resource metadata; it simulates these operations without retaining values.
Real project-router tests prove that default bindings include every output of
PostgreSQL, MongoDB and S3 and that each deployment is admitted by the controller
binding-fingerprint normalization. Individual password/secret-key targets remain
owner-chosen; credential values never reach the browser. Broker archives use only
the public storage contract and exclude controller database/runtime modules.
The real project-router tests in `tests/test_owner_resources.py` cover ownership,
limits, binding validation, no-values projections, same-key recovery and the
unchanged image activation compatibility dictionaries. The resource Vitest and
Playwright flows cover write-only edits, renamed/partial PostgreSQL bindings,
deploy injection names, rotation warnings and the absence of storage deletion
for owners/staff. Admin tests verify step-up and typed storage deletion.
Environment edits apply immediately to running apps; bindings and rotations
need deployment. There is no optional dotenv import UI in this release.

Create an application, save a repository URL and Node/Bun settings, then
deploy a full lowercase 40-character hexadecimal fixture commit. Build output,
health and deployment results are simulated. Example application URLs use
example.com and are not local endpoints.

State, public development CA and configs live below
`.tmp/owner-portal-credentials`. The three Unix sockets use a fresh current-user
directory `/tmp/owner-portal-sockets-<uid>-<random>` with mode 0700, so long checkout
paths cannot exceed Linux's 107-byte socket-path limit. Development configuration
accepts sockets only below this worktree's `.tmp` or in such a private directory;
state and the public CA remain confined to `.tmp`. Ctrl-C stops every server and
Vite child and removes the socket directory; startup failures also remove it. Schema
3 preserves ownership, quotas, intents and audit while removing legacy assertion
flows/replays and key IDs and invalidating sessions. Legacy fixtures whose
subjects were usernames are not reassigned by name to Commons UUIDs; use a fresh
harness directory for the new contract. Unknown schemas or checksum/realm
mismatches fail closed. Different portal/Commons origins need a separate state
realm.

For Vite/HMR:

```sh
uv run python -m openstack_platform.management.dev \
  --http --vite --port 9445 --provider-port 9446 \
  --state .tmp/portal-vite
```

Open `http://127.0.0.1:9445/sign-in`. HTTP is explicit loopback development,
with unprefixed development cookies. Production entry points reject development
mode or custom trust. The HTTPS smoke serves built assets under the production
CSP: scripts/styles/form-action are self-only and connect is self plus
`https://api.github.com` and `https://raw.githubusercontent.com` (the deploy
page's recent commits and commit check), with no inline scripts or styles. Passwords never enter URLs, application browser storage or screenshots.

The anonymous HMAC binder cookie is HttpOnly, Path=/, SameSite=Strict, Secure
and __Host-prefixed on HTTPS. Login requires the exact portal Origin plus its
HMAC CSRF token. Session cookies stay Secure/HttpOnly/Lax and expire after 8 h
absolute or 30 min idle. Commons password changes or archiving do not end an
existing Commons portal session. Local security changes, revocation or logout do. Per-address
limits default to 600 options and 400 login attempts/minute, with IPv6 /64
buckets. Five failed credentials exhaust a fixed 60-second budget for the exact
username and address bucket. Pending identity checks reserve budget to prevent
parallel bypass. Further attempts, including correct passwords, return 429
without contacting identity until that window ends; another address is unaffected
and rejected attempts never extend the window. Identity queues within its
three-second connect deadline, with 64 outbound exchanges and 128 local request
slots. An identity outage blocks new sign-ins but preserves existing sessions.

Production requires the validated `ownerPortal` platform section, enabled flag,
Commons HTTPS origin, optional identity egress CIDRs and quota/rate overrides.
The renderer writes broker, web and identity configs from this inventory;
keys/assertion parameters are rejected. The identity service alone has HTTPS
access, restricted to configured CIDRs or the checked-in Cloudflare ranges plus
the local systemd-resolved DNS stub. Review the provider ranges when updating the
inventory. Web accepts only ingress and its overwritten `cf-connecting-ip`;
X-Forwarded-For and X-Real-Ip never choose rate-limit buckets.

### Exercise local accounts and roles

Use a fresh `.tmp/owner-portal-accounts` state directory for the final schema-3
account model. The earlier unpublished staff-grant prototype checksum is refused;
do not edit migration history to make an old test DB appear compatible. Run:

```sh
uv run python -m openstack_platform.management.dev --state .tmp/owner-portal-accounts
```

The harness explicitly gives the Taylor Commons fixture role `staff`; its own
apps and catalog use one immutable role session. Production has no seed admin
password or account. To exercise bootstrap while the local harness runs, use:

```sh
uv run openstack-platform-management-bootstrap --config .tmp/owner-portal-accounts/config.json
```

Open the fragment URL, create a local admin and enroll the one-time displayed
TOTP key. The development config permits current-user file ownership; production
requires the operator UID and broker setgid directory. All credentials remain
public fixture data or locally entered test values; no live Commons or OpenStack
service is contacted. Use Accounts to create a local staff invitation, redeem it
in another browser context, then compare owner/staff/admin navigation. Commons
remains the default sign-in method; local login has a separate method selector,
never a role selector. Admin roles are prohibited for Commons accounts.

Tests verify scrypt salts/cost upgrades, RFC TOTP vectors/window/replay, hash-only
24-hour operator files, 72-hour invites/resets, one-time ID consumption, five-minute
step-up, role/generation transitions, session invalidation, field projections and
safe audit. Playwright bootstraps an admin, enrolls TOTP, invites local staff,
checks Commons owner isolation, denies admin pages to non-admin roles, adopts an
operator class-app fixture, saves its imported configuration and opens its
maintenance deploy dialog. Tokens
are removed from address bars and never enter request URLs. Traces/video remain
disabled. Use the root npm workspace/lockfile and `frontend/shared` from PR #62;
do not install or regenerate an owner-portal-only lockfile.

The harness seeds two explicitly fake operator applications with retained-address
metadata and larger sizing, outside broker ownership. The account-flow smoke
adopts them through the production admin API. Unit/contract tests cover the exact
controller socket route delta, admin-only maintenance/plan fields, preservation
of accepted sizing, import integrity and idempotency, cross-actor busy scopes,
role revocation during reads, keyed environment fingerprints and no-value storage.
Full retained-IP tests run project maintenance cutovers against the existing
offline OpenStack/Nomad adapters. No live app is touched.

## Verify the portal

Use the default temporary directory for the full Python suite:

```sh
unset TMPDIR TEMP TMP
uv lock --check
uv run ruff format --check openstack_platform deploy/releases infra tests
uv run ruff check openstack_platform deploy/releases infra tests
uv run mypy
uv run vulture openstack_platform deploy infra tests --min-confidence 80 --sort-by-size
uv run python -m compileall -q openstack_platform deploy infra tests
uv run python -m unittest discover -s tests -v
npm --prefix frontend/owner-portal run smoke
OWNER_PORTAL_SMOKE_MODE=http npm --prefix frontend/owner-portal run smoke
OWNER_PORTAL_SMOKE_MODE=vite npm --prefix frontend/owner-portal run smoke
```

The identity tests pin Commons' exact two-field request, status/result mapping,
TLS, redirects, duplicate/extra fields, bounds, timeouts and peer enforcement.
Broker tests cover CSRF/Origin, password leakage, generic/archived failures,
throttles, session rotation, ownership/quota races and journal recovery. Contract
tests exercise the real controller project router. SQLite WAL/SHM disappearance
at final close is handled narrowly; other errors remain visible without bodies.

Playwright covers username/password login, invalid and archived accounts,
two-owner isolation, exact deployment/recovery, logout and CSP at desktop/mobile
widths in light/dark. It uses cached Chromium headless shell revision 1200 for
Playwright 1.57.0; no other browsers are needed. CI installs Node 24 and the plain
frozen Python environment, checks/builds frontend, installs only Chromium and
runs the HTTPS smoke. Node tooling is never installed on admin.

Screenshots and sanitized diagnostics live under
`.tmp/owner-portal-playwright`. Failure upload allows only fixture screenshots
and method/path/status/code diagnostics, capped at 2 MiB/file and 20 MiB total;
no traces, videos, request headers, query strings or response bodies. Fixtures
and submitted passwords are not captured. Clear focus and scroll before images
so keyboard-only skip links and sticky navigation are not painted incorrectly.

Management archives and installer modes are implemented. After a clean committed
build, use the maintenance artifact commands; dirty or stale receipts are rejected.
The actual asset build is bound by the artifact descriptor, not inferred from a
source hash. To run the real Node build/archive fixture explicitly:

```sh
OWNER_PORTAL_RELEASE_INTEGRATION=1 uv run python -m unittest tests.test_management_releases.BuildIntegrationTests -v
uv run python -m unittest tests.test_management_releases.ManagementReleaseTests -v
```

The fixture commit exists only in a temporary test checkout. Local integration
archives are development evidence and are not a release of this uncommitted
worktree. Broker/web and identity smoke never touch a live DB or class endpoint.
These tests do not establish production deployment or live Commons acceptance.
