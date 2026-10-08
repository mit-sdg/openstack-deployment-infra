# Development

Set up a contributor environment, run CI checks, and try the owner portal and operator dashboard locally. These commands need no OpenStack credentials and provision no infrastructure. For a live platform, follow [Deploy the platform](guides/deploy-the-platform.md).

## What you need

| Tool | Version | Used for |
| --- | --- | --- |
| Linux on x86_64 | | Everything. Nix outputs target `x86_64-linux`, and the local portal uses Linux-only features (Unix sockets, `memfd`). |
| [uv](https://docs.astral.sh/uv/) | any recent release (CI pins 0.12.2) | The Python environment and every Python command |
| Python | 3.14.x | Required by `pyproject.toml`. uv uses or installs it. |
| Node.js and npm | Node 24.19.0, npm 11.17.0 | The frontend workspace (`frontend/package.json` pins both) |
| Nix with flakes | | Optional: Nix evaluation, formatting, VM tests, and image builds |
| ShellCheck | | Optional: the shell lint CI runs |
| Podman (rootless) | | Optional: the generated recipe smoke test |

## Set up the environment

1. Create the locked Python environment from the repository root:

   ```bash
   uv sync --frozen
   uv run python --version
   ```

   Expect `Python 3.14.x`. Development dependencies include ruff, mypy, vulture, and `cryptography` for the local portal harness.

2. Install the frontend workspace:

   ```bash
   node --version
   npm --version
   npm --prefix frontend ci
   ```

   Expect `v24.19.0` and `11.17.0`. To match CI, install npm with `npm install --global npm@11.17.0` if needed.

`frontend/` has three workspace packages and one lockfile, `frontend/package-lock.json`:

- `frontend/shared` provides presentation-only components, tokens, and fonts as `@openstack-platform/ui`.
- `frontend/owner-portal` is the owner portal's React app.
- `frontend/operator-dashboard` is the operator dashboard's React app.

Don't create or regenerate a lockfile inside one package.

To change Python dependencies, edit `pyproject.toml`, run `uv lock`, and commit both files.

If you use Nix, `nix develop` opens a shell with `deadnix`, `jq`, `nixfmt-rfc-style`, `python3`, `shellcheck`, and `statix`. It doesn't include uv or Node.js.

## Run the checks

Run the pull-request CI jobs locally with these commands.

| CI job | What it checks | Local section |
| --- | --- | --- |
| Static checks | Python formatting, lint, types, dead code, tests, shell scripts, example configuration | [Python tests](#python-tests), [Python static checks](#python-static-checks), [Shell checks](#shell-checks), [Configuration and entry point checks](#configuration-and-entry-point-checks) |
| Owner portal frontend and HTTPS smoke | Portal format, types, unit tests, build, Playwright smoke, management release evidence | [Verify the portal](#verify-the-portal) |
| Frontend workspace and committed dashboard freshness | All frontend packages, both builds, committed dashboard output, dashboard Playwright smoke | [Frontend checks](#frontend-checks) |
| Build, start, and health-check generated Bun and Node recipes | Real app builds from generated recipes | [Generated recipe smoke test](#generated-recipe-smoke-test) |
| Nix evaluation and formatting, packaged binaries, role VM tests, image builds | Nix configurations, packages, and the five role images | [Nix checks](#nix-checks) |

Image publication and live acceptance run only on `main` or by manual dispatch. See [Releases and upgrades](guides/releases-and-upgrades.md).

### Python tests

Run the whole suite:

```bash
uv run python tests/run_parallel.py --jobs 4
```

The runner uses one isolated process per module and reports failures in full. CI uses four workers. While iterating, run one module or class:

```bash
uv run python -m unittest tests.test_documentation -v
uv run python -m unittest tests.test_management_releases.ManagementReleaseTests -v
```

No commit is needed: `tests/repository_fixtures.py` copies current source into a clean temporary Git repository.

Use the default temporary directory to keep Unix socket paths short:

```bash
unset TMPDIR TEMP TMP
```

Some tests require a tool or explicit opt-in:

| Test | Runs when |
| --- | --- |
| Dashboard wheel build (`tests/test_dashboard.py`) | `uv` is on `PATH` |
| Fixed IP curl transport (`tests/test_fixed_ip.py`) | `curl` is on `PATH` |
| Private repository keys (`tests/test_private_repositories.py`) | `ssh-keygen` is on `PATH` |
| Release smoke (`tests/test_packaging_release.py`) | Python is 3.14 |
| OpenStack SDK projection (`tests/test_neutron_sdk_projection.py`) | The OpenStack SDK and client are importable. The Nix `package-smoke` check runs it. |
| Real Node build of portal archives (`tests/test_management_releases.py`) | `OWNER_PORTAL_RELEASE_INTEGRATION=1` |

### Python static checks

```bash
uv lock --check
uv run ruff format --check openstack_platform deploy/releases infra tests
uv run ruff check openstack_platform deploy/releases infra tests
uv run mypy
uv run vulture openstack_platform deploy infra tests --min-confidence 80 --sort-by-size
uv run python -m compileall -q openstack_platform deploy infra tests
```

Vulture is silent if it finds no unused code at 80% confidence or higher. Fix formatting with `uv run ruff format openstack_platform deploy/releases infra tests`.

### Shell checks

Check Bash syntax and run ShellCheck with the same exclusions as CI:

```bash
find deploy infra tests -type f -name '*.sh' -exec bash -n {} +
find deploy infra tests -type f -name '*.sh' -print0 \
  | xargs -0 shellcheck -e SC1090,SC1091,SC2016
```

### Configuration and entry point checks

Validate the shared contract and example inventory, then smoke-test the entry points:

```bash
uv run python infra/lib/platform_contract.py
PLATFORM_CONFIG=config/platform.example.json \
  uv run python infra/lib/platform_config.py validate
PLATFORM_CONFIG="$PWD/config/platform.example.json" \
  uv run python deploy/releases/release_smoke.py \
  helper --source "$PWD" --launcher "$PWD/.venv/bin/openstack-platform-helper"
uv run openstack-platform --help >/dev/null
```

Expect `platform-config=valid` and `release-smoke=helper:ok`. CI also parses the example JSON and architecture SVG, and generates and verifies unsigned evidence with `python -m openstack_platform.release_manifest`.

### Documentation tests

```bash
uv run python -m unittest tests.test_documentation -v
```

Tests check relative links, forbid the production domain and retired commands, and require each tracked file exactly once in the [repository map](reference/repository-map.md). Update the map whenever you add, rename, or delete a file.

### Frontend checks

```bash
npm --prefix frontend run format:check
npm --prefix frontend run format:check --workspaces
npm --prefix frontend run check:source
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run check:freshness
```

- The two `format:check` commands run Prettier on the root package and workspaces.
- `check:source` rejects inline code, styles, HTML injection, cross-app imports, and app, network, or router dependencies in `shared`. Expect `First-party CSP and app import boundaries passed`.
- `typecheck` runs `tsc --noEmit` in each workspace.
- `test` runs the source policy tests, then Vitest in each workspace.
- `build` writes the ignored portal `frontend/owner-portal/dist/` and committed dashboard `openstack_platform/dashboard/static/`.
- `check:freshness` compares dashboard output against the commit, including untracked files. Commit the rebuild first ([Change the dashboard UI](#change-the-dashboard-ui)).

The browser tests are covered in [Preview the operator dashboard](#preview-the-operator-dashboard) and [Verify the portal](#verify-the-portal).

### Generated recipe smoke test

Build and health-check the Node.js and Bun fixtures in `tests/fixtures/apps/` with generated recipes. This needs rootless Podman; CI installs `fuse-overlayfs`, `podman`, `slirp4netns`, and `uidmap`:

```bash
uv sync --frozen --no-dev
tests/smoke_generated_recipes.sh
```

Run `uv sync --frozen` again afterward to restore the development tools.

### Nix checks

Evaluate the flake without building anything, then check formatting:

```bash
nix flake check --no-build --print-build-logs
nix fmt -- .
git diff --exit-code -- '*.nix'
```

The last command fails if `nix fmt` changed a file. Review and commit those changes.

For heavier checks, use a Linux host with enough disk, preferably with KVM:

```bash
nix build --print-build-logs .#checks.x86_64-linux.package-smoke
nix build --print-build-logs .#checks.x86_64-linux.vm-admin
```

`package-smoke` runs the packaged binaries, including the OpenStack SDK projection test. The VM checks are `vm-admin`, `vm-ingress`, `vm-storage`, `vm-worker`, and `vm-builder`. To build and boot a role image, see [Hosts and images](guides/hosts-and-images.md).

## Understand the test layout

Python tests use `unittest`. Modules cover boundaries rather than individual source files: `tests/test_platform_storage.py`, for example, covers the controller state machine and helper database and S3 operations.

| Prefix | Covers |
| --- | --- |
| `test_controller_*` | The controller: HTTP transport, API routes, database, images, recovery |
| `test_helper_*` | The helper's fixed actions |
| `test_management_*`, `test_owner_*` | The owner portal: broker, web, identity, accounts, staff and admin authority, releases, local harness |
| `test_platform_*` | Shared foundations: contract, configuration, setup, OpenStack operations, storage, restore |
| `test_operator_*` | The operator CLI and its SSH bridge to the admin host |
| `test_application_*`, `test_deployment_*`, `test_runtime_*` | Builds, deployments, sizing, health, and runtime versions |
| Others | One focused area each, such as backups, fixed and floating IPs, image publication, live acceptance, documentation |

Supporting files:

- `tests/fixtures/apps/` holds small Node.js and Bun apps that the recipe smoke test builds and runs.
- `tests/fixtures/openstack/` holds recorded provider outputs, such as the UUID formats different OpenStack tools print.
- `tests/fixtures/retained_openstack.py` substitutes for OpenStack and Nomad while running real helper and lifecycle code.
- `tests/product_fixtures.py` builds accepted app and deployment state. `tests/repository_fixtures.py` creates clean temporary Git repositories.
- Python and TypeScript share `frontend/owner-portal/src/utils/preflight-cases.json` (commit checks) and `frontend/owner-portal/src/utils/runtime-version-cases.json` (runtime versions).

Vitest tests are `*.test.ts` and `*.test.tsx` under each package's `src/`. Playwright tests live in `frontend/owner-portal/e2e/` and `frontend/operator-dashboard/e2e/`; source policy tests in `frontend/scripts/source-policy.test.mjs`.

NixOS VM tests for each role are in `nix/tests/default.nix`.

The [repository map](reference/repository-map.md#tests) gives a one-line scope for every test module and fixture.

## Preview the operator dashboard

Preview the read-only [operator dashboard](guides/run-the-platform.md) with synthetic data:

1. Start the preview:

   ```bash
   uv run python -m openstack_platform.dashboard.preview --scenario mixed
   ```

   You should see `preview=listening url=http://localhost:8470 scenario=mixed`.

2. Open `http://localhost:8470`.

The preview uses loopback TCP and `config/platform.example.json`, never OpenStack or an admin host. Synthetic data goes through production parsers and status rules.

| Scenario | Shows |
| --- | --- |
| `mixed` (default) | Apps in every state: serving, failing, deploying, stopped, needing recovery, and a failed update |
| `healthy` | Every app serving (one stopped on purpose) |
| `outage` | Public ingress failing, and app routes returning gateway errors |
| `unreachable` | Admin reads fail after the first refresh |
| `empty` | No apps and no health snapshot |

Options: `--port` (default 8470), `--interval` (seconds between refreshes, default 15), `--delay` (seconds added to each refresh, to see loading states), and `--platform-config` (another inventory file).

The preview serves `openstack_platform/dashboard/static/` without Node.js. It reloads each complete rebuild, keeping the previous bundle during a build. Restart after Python changes.

### Change the dashboard UI

1. Run a build watcher next to the preview:

   ```bash
   npm --prefix frontend/operator-dashboard run dev
   ```

   This runs `vite build --watch`. Vite hot reload injects inline code and styles forbidden by the dashboard's Content Security Policy, so preview built files.

2. When you're done, rebuild:

   ```bash
   npm --prefix frontend/operator-dashboard run build
   ```

3. Commit all changes under `openstack_platform/dashboard/static/` with your source. Content hashes rename `assets/index-*.js` or `assets/style-*.css`; update the [repository map](reference/repository-map.md).

4. After you commit, confirm that a fresh build matches what you committed:

   ```bash
   npm --prefix frontend/operator-dashboard run build
   npm --prefix frontend run check:freshness
   ```

   You should see `Committed dashboard output is fresh, including the full file inventory`.

Output contains HTML, JavaScript, CSS, and SVG, without source maps, a Vite manifest, or commit, time, and checkout-path metadata.

### Run the dashboard browser tests

1. Install the Playwright Chromium headless shell once:

   ```bash
   cd frontend
   npx playwright install --only-shell chromium
   cd ..
   ```

   On a fresh machine, add `--with-deps` to install the system libraries too (CI does).

2. Run the tests:

   ```bash
   npm --prefix frontend/operator-dashboard run smoke
   ```

Playwright starts previews on ports 8480–8484. Tests cover polling, ETags, refresh, reconnects, drawers, preserved view state, and production CSP violations or unexpected requests. They check 320, 390, 768, and 1440px in both themes, reduced motion, forced colors, and print. Screenshots: `.tmp/dashboard-screenshots/after/<width>-<light|dark>.png`.

## Run the local owner portal

The local [owner portal](how-it-works.md) harness substitutes for Commons and the controller. Use it for UI work, sign-in and roles, and browser tests.

### How the harness works

`python -m openstack_platform.management.dev` starts five servers on loopback:

| Server | Address | What it is |
| --- | --- | --- |
| Web | `https://127.0.0.1:9443` | The real `management-web` server, serving the built portal from `frontend/owner-portal/dist/` |
| Broker | Unix socket | The real `management-broker`, with its own SQLite database |
| Identity | Unix socket | The real `management-identity` service, which redeems Commons sign-in codes |
| Fake Commons | `https://localhost:9444` | A stand-in for the class site: a sign-in and approval page, and code redemption |
| Fake controller | Unix socket | A stand-in for the controller's project socket that simulates builds, deployments, and storage |

Web, broker, and identity use production code. The harness contacts no live platform or class site; your browser still reads public commits from GitHub.

Key details for debugging:

- Sockets use `/tmp/owner-portal-sockets-<uid>-<random>` (mode `0700`), below the 107-byte path limit. Shutdown or failed startup removes it.
- A one-day self-signed certificate covers `localhost` and `127.0.0.1`; its key stays in memory. Development identity trusts it; production uses system certificate authorities.
- The `--state` directory must be inside the checkout's `.tmp/`; default: `.tmp/owner-portal-accounts`. It holds `config.json`, `broker/`, `controller.json`, and `development-ca.pem`.
- Always run the harness from the repository root so it finds `frontend/owner-portal/dist/`.

### Start the portal

1. Set up the environment and build the portal:

   ```bash
   uv sync --frozen
   npm --prefix frontend ci
   npm --prefix frontend/owner-portal run build
   ```

   The build ends with `owner-build-receipt=written`.

2. Start the harness:

   ```bash
   uv run python -m openstack_platform.management.dev
   ```

   You should see:

   ```text
   Owner portal: https://127.0.0.1:9443 (built assets)
   Local fakes only. TLS private keys remain in memory; fixture passwords are development-only. Ctrl-C stops the harness.
   ```

3. Open `https://127.0.0.1:9443/sign-in` and accept the browser's certificate warning.

4. Choose **Sign in with your class account**. The browser goes to the fake Commons at `https://localhost:9444`. Accept the certificate warning there too.

5. Enter a fixture username and password from the table below, then choose **Allow**. You land on the **My apps** page. **Cancel** returns you to the sign-in page with a cancelled message.

Press Ctrl-C in the harness terminal to stop everything.

### Fixture accounts

These are public test values, not real class credentials.

| Username | Password | Name | Role and purpose |
| --- | --- | --- | --- |
| `alice` | `local-alice-password` | Alice Student | App owner |
| `bob` | `local-bob-password` | Bob Student | A second app owner, to check that owners can't see each other's apps |
| `carol` | `local-carol-password` | Carol Student | An archived Commons account. The fake Commons lets her approve, then refuses the code, so the portal shows a sign-in error. |
| `taylor` | `local-taylor-password` | Taylor Instructor | Staff. The harness gives this account the staff role each time it starts. |

Fake Commons codes work once, expire after 60 seconds, and are bound to the portal origin. Fixture emails are `<username>@example.com`.

Commons accounts can be app owners or staff, but never portal admins. A portal admin is always a local account (see the next section).

The unowned `operator-class-fixture` and `operator-class-fixture-mobile` apps have larger sizing and a retained address for practicing admin adoption.

### Things to try

- Create an app with a repository URL and Node.js or Bun settings. Any 40-character lowercase hexadecimal commit works. Builds and health checks are simulated; `https://<slug>.apps.example.com` URLs don't resolve.
- Change environment variables and add PostgreSQL, MongoDB, or S3. The fake controller stores names and metadata, never secret values.
- Sign in as `taylor` in another browser profile to see the staff view, then compare it with an owner's.

To simulate failures, send a `POST` with the portal's `Origin` header to one of the harness's test endpoints:

| Endpoint | Effect |
| --- | --- |
| `/__test__/lost-response` | The fake controller drops its reply to the next deployment request, as if the connection broke |
| `/__test__/failed-deployment` | The next deployment fails |
| `/__test__/recovery-required` | The next deployment ends in the "Needs attention" state |
| `/__test__/pause-storage-creation` | New storage stays in the creating state |
| `/__test__/finish-storage-creation` | Paused storage finishes creating |

For example:

```bash
curl -k -X POST -H 'Origin: https://127.0.0.1:9443' \
  https://127.0.0.1:9443/__test__/failed-deployment
```

The response is `{"ready":true}`. The browser tests use these endpoints, and production servers don't have them.

### Create a portal admin and local accounts

Create the first portal admin with a one-time setup link, as in production. There is no built-in admin password.

1. With the harness running, open another terminal in the repository root and run:

   ```bash
   uv run openstack-platform-management-bootstrap --config .tmp/owner-portal-accounts/config.json
   ```

   It prints a single-use setup link, valid for 24 hours, of the form `https://127.0.0.1:9443/setup#<token>`.

2. Open the link. Choose a username and password for the local admin, then scan the QR code (or enter the key) in an authenticator app and enter a code.

3. Sign in under **Use a local account** on the sign-in page. There is no role selector: each account's role decides what it sees.

4. Go to **People** and create a local account. Choose the role (owner, staff, or admin) and copy the setup link. The portal may ask for your password and authenticator code again first (step-up).

5. Open the setup link in another browser profile or a private window to finish that account's setup.

Compare the navigation for owner, staff, and portal admin. For what each role can do, see [Manage apps and people](guides/manage-apps-and-people.md).

Development setup files belong to your user. Production requires operator ownership and the broker's setgid directory ([Open the portal](guides/open-the-portal.md)).

### Use Vite with hot reload

For fast UI work, run the portal through Vite's development server:

```bash
uv run python -m openstack_platform.management.dev \
  --http --vite --port 9445 --provider-port 9446 \
  --state .tmp/portal-vite
```

Open `http://127.0.0.1:9445/sign-in`.

- `--vite` requires `--http` and Node.js/npm on `PATH`. It starts `npm run dev` in `frontend/owner-portal`; Vite proxies `/api`, `/auth`, and `/__test__` to the harness.
- HTTP mode uses development cookie names (for example `portal-dev-session` instead of `__Host-portal-session`). The fake Commons still uses HTTPS.
- Vite allows inline code. Check CSP-sensitive changes using the built portal over HTTPS.
- Use separate `--state` directories for each portal and Commons origin pair.

`--http` without `--vite` serves the built portal over plain HTTP.

### Reset or upgrade local state

To start over, stop the harness and delete its state directory:

> [!WARNING]
> This permanently deletes all local accounts, sessions, and simulated apps created in the harness.

```bash
rm -rf .tmp/owner-portal-accounts
```

Unknown schemas, mismatched migration checksums, and changed origins are refused. Use a fresh `--state` directory rather than editing the database. Older username-based Commons accounts are not reassigned to Commons user IDs.

## Verify the portal

For portal changes, run these CI checks:

1. Check and build the portal:

   ```bash
   npm --prefix frontend/owner-portal run format:check
   npm --prefix frontend/owner-portal run typecheck
   npm --prefix frontend/owner-portal test
   npm --prefix frontend/owner-portal run build
   ```

2. Install the Playwright Chromium headless shell once:

   ```bash
   cd frontend/owner-portal
   npx playwright install --only-shell chromium
   cd ../..
   ```

3. Run the browser tests. The default mode serves the built portal over HTTPS under the production Content Security Policy:

   ```bash
   npm --prefix frontend/owner-portal run smoke
   ```

   You can also run them over plain HTTP and through Vite:

   ```bash
   OWNER_PORTAL_SMOKE_MODE=http npm --prefix frontend/owner-portal run smoke
   OWNER_PORTAL_SMOKE_MODE=vite npm --prefix frontend/owner-portal run smoke
   ```

4. Run the full Python suite and static checks ([Python tests](#python-tests), [Python static checks](#python-static-checks)).

Playwright resets `.tmp/e-accounts-<mode>` before each run. Ports are 9543 (HTTPS), 9553 (HTTP), and 9563 (Vite); Commons uses the next port. `OWNER_PORTAL_SMOKE_PORT` overrides the port and adds it to the state path.

| Spec | Covers |
| --- | --- |
| `e2e/owner-flow.spec.ts` | Two owners signing in through the fake Commons, isolation between them, configuration, deploy and review, build output, recovery after a lost response, sign-out, and CSP, at desktop and phone widths in light and dark |
| `e2e/resources.spec.ts` | Environment variables, renamed PostgreSQL bindings, the variable names a deploy injects, and credential rotation |
| `e2e/staff-flow.spec.ts` | Bootstrapping a portal admin with an authenticator, inviting local staff, owner and staff boundaries, adopting a seeded operator app, staff creating, adopting, reassigning, deploying, choosing grouped worker sizes and app build machines, and deleting storage without step-up, with admin-only default build machine, account-management boundaries, and the audit log |

Traces and video are off; screenshots go to `.tmp/owner-portal-playwright/screenshots/`. The staff/admin sizing spec also saves deploy, app build machine, and platform settings screenshots at desktop and 390px widths in light and dark mode under `/tmp/staff-admin-ui-shots/`. The same spec saves All apps with conditional attention and Overview Machines with default and app-specific build machines in both layouts and themes under `/tmp/attention-size-shots/`. On failure, `tests/collect_owner_portal_artifacts.py` collects fixture screenshots and sanitized diagnostics (method, path, status, error code), limited to 2 MiB per file and 20 MiB total. CI uploads no traces, videos, headers, query strings, or response bodies.

### Build portal release archives locally

Test [commit-bound portal archives](guides/releases-and-upgrades.md) with a real `npm ci` and Vite build in a clean temporary checkout:

```bash
OWNER_PORTAL_RELEASE_INTEGRATION=1 uv run python -m unittest tests.test_management_releases.BuildIntegrationTests -v
uv run python -m unittest tests.test_management_releases.ManagementReleaseTests -v
```

These archives are test output, not a release of your working tree.

## Change the portal UI

- Follow the [portal design system](../frontend/DESIGN.md) for components, tokens, status words, and copy.
- Preview the backend-free [component gallery](../frontend/DESIGN.md#gallery). It never ships in production.
- Keep routing and API calls in the app. `frontend/shared` stays presentation-only, and `npm --prefix frontend run check:source` enforces that.
- When you add a bundled dependency or font, add its licence to `frontend/THIRD_PARTY_NOTICES.md`. The portal build ships that file as `third-party-notices.txt`.

## Before you open a pull request

- Run the checks for every area you touched. CI runs all of them.
- Update the [repository map](reference/repository-map.md) for every added, renamed, or deleted file.
- If you changed the dashboard, commit the rebuilt `openstack_platform/dashboard/static/` output.
- If you changed behavior an operator or app owner sees, update the guide that describes it.

## Related

- [How it works](how-it-works.md)
- [Internals reference](reference/internals.md)
- [Repository map](reference/repository-map.md)
- [Portal design system](../frontend/DESIGN.md)
- [Releases and upgrades](guides/releases-and-upgrades.md)
- [Hosts and images](guides/hosts-and-images.md)
