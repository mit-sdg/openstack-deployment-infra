# Platform hardening plan

## Purpose and scope

This plan tracks the remaining work required to reduce configuration drift,
strengthen trust boundaries, and make recovery behavior easier to verify. It is
a work plan, not a claim that the listed controls are already implemented.

The current implementation already centralizes cross-component roles, ports,
accounts, protocol identifiers, installation paths, and required inventory
fields in `infra/lib/platform_contract.json`. Python, Nix, cloud-init rendering,
and standalone infrastructure scripts consume that contract. The controller,
operator, and helper packages are separate, and milestone-specific identifiers
and compatibility paths have been removed.

Every workstream must preserve these invariants:

- malformed input fails before provider, scheduler, or storage mutation;
- an ambiguous provider result becomes recovery-required rather than guessed;
- failed candidate deployment does not remove the accepted deployment;
- secrets do not enter databases, normal output, diagnostics, or command lines;
- database and provider mutations remain idempotent and resumable; and
- unsupported prior state is rejected before it can be adopted.

## Status and order

| ID | Workstream | Status | Depends on |
| --- | --- | --- | --- |
| HARD-00 | Target-toolchain verification | Required before merge | None |
| HARD-01 | Typed deployment inventory | Planned | HARD-02 |
| HARD-02 | Generated implementation contract | Partially implemented | HARD-00 |
| HARD-03 | Provider and lifecycle module decomposition | Started | HARD-00 |
| HARD-04 | Remove shell `eval` configuration transport | Planned | HARD-02 |
| HARD-05 | Release and bootstrap supply-chain evidence | Planned | HARD-02 |
| HARD-06 | Unix-socket peer authentication | Planned | HARD-00 |
| HARD-07 | Systemd credential isolation | Planned | HARD-00 |
| HARD-08 | Crash-durable filesystem replacement | Implemented for supported critical paths | HARD-00 |
| HARD-09 | State-machine, property, and parser fuzz tests | Concrete trust-boundary gates implemented | HARD-01, HARD-03 |
| HARD-10 | Explicit unsupported-state detection | Implemented for migration, provider creation, and restore | HARD-01 |
| HARD-11 | Runtime network and egress restrictions | Planned | HARD-00 |

HARD-00 is the immediate gate. HARD-02 should finish before HARD-01 and HARD-04
so generated types and transport fields have one source. Module decomposition
must preserve existing recovery tests throughout; it must not be combined with
a schema or provider-state migration.

## HARD-00: Target-toolchain verification

**Goal:** Demonstrate that the refactor passes the exact release, package, and
NixOS toolchain rather than only the host Python fallback.

Required checks:

1. Run `uv lock --check` and `uv sync --frozen` with Python 3.14.
2. Run Ruff format and lint checks, mypy, and vulture using the locked versions.
3. Run ShellCheck over deployment, infrastructure, and test shell scripts.
4. Run the complete unit and packaging suite, including the Python-3.14-only
   release installer tests.
5. Build a wheel, install it into an empty environment, and prove that
   `openstack_platform/platform_contract.json` is present and identical to the
   source contract.
6. Run `nix flake check --no-build`, the package smoke check, and all five NixOS
   role VM tests.
7. Build and boot each QCOW2 candidate through the existing image smoke test.

**Exit evidence:** CI links or retained logs for every command, wheel contract
hash equality, five successful VM tests, and five successful image boots. A
skipped release, Nix, or VM test does not satisfy this workstream.

## HARD-01: Typed deployment inventory

**Goal:** Replace runtime dotted-string access such as
`platform.get("paths.root")` with immutable, type-checked inventory models.

Planned structure:

- `DeploymentIdentity`: project name and UUID, namespace, prefix, and domain;
- `DeploymentPaths`: root, admin state, backup, and storage data paths;
- `PersistentAddresses`, `PersistentHosts`, and `PersistentPorts`;
- `RoleImages` and `RoleFlavors`, keyed by the contract role type;
- `PersistentVolumes`, including names, labels, sizes, and volume type;
- `InternalNames`, `ContainerImages`, `ToolVersions`, and `Checksums`; and
- typed recovery domains, PKI names, ingress routes, and operator CIDR.

The loader must reject unknown keys, missing nested fields, invalid types,
unsafe paths, malformed addresses, and unsupported contract versions before it
returns a model. Callers must not retain a general-purpose dotted lookup escape
hatch.

**Exit evidence:** no production call to `PlatformConfig.get`, mypy coverage for
all inventory consumers, malformed-field tests at every nested model boundary,
and unchanged deployment-identity hash fixtures for equivalent valid input.

## HARD-02: Generated implementation contract

**Goal:** Make the JSON contract the sole authored source while avoiding drift
between independent Python and Nix validators.

Remaining tasks:

1. Define a strict schema for `infra/lib/platform_contract.json`, including
   account shapes, installation paths, identifier syntax, ports, roles, and
   inventory-field arrays.
2. Generate the typed Python projection and Nix projection from that schema.
3. Package the exact source JSON in the wheel and record its SHA-256 in release
   evidence.
4. Make Nix evaluation compare the expected contract version and required
   sections before role evaluation.
5. Make release smoke tests compare source, wheel, and Nix contract hashes.
6. Reject unknown contract fields and versions in every consumer.

Generated files must be deterministic and checked with a clean-tree diff in CI.
The bootstrap script may keep a minimal reviewed trust anchor because it runs
before the managed Python runtime exists; that exception must remain explicit
and tested.

**Exit evidence:** one authored schema and JSON document, deterministic generated
artifacts, no handwritten role/port/account projections, and hash equality
across source archive, wheel, and Nix build.

## HARD-03: Provider and lifecycle module decomposition

**Goal:** Reduce the remaining large modules without weakening transaction,
cleanup, or recovery boundaries.

Required splits:

- OpenStack image publication, inventory, selection, and pruning;
- persistent-host observation, power operations, replacement, and recovery;
- OpenStack command transport and bounded provider projection;
- source acquisition and deterministic recipe generation;
- builder lifecycle and worker lifecycle;
- deployment promotion, environment mutation, logs, and registry retention;
- PostgreSQL provider operations;
- MongoDB provider operations;
- Garage S3 provider operations;
- storage credential publication and recovery handlers;
- database schema/migrations, repositories, operation journal, and backup; and
- thin composition modules that expose the controller and helper APIs.

Provider modules may return typed projections only. They must not expose raw
OpenStack, Nomad, PostgreSQL, MongoDB, Garage, or subprocess documents to
service layers. Splits must be performed one recovery domain at a time while
all existing interruption tests continue passing.

**Exit evidence:** no multipurpose provider module containing unrelated
resource lifecycles, explicit dependency direction, no circular imports, typed
service boundaries, and passing recovery tests after every split.

## HARD-04: Remove shell `eval` configuration transport

**Goal:** Replace `eval "$(... platform_config.py shell)"` with a transport that
does not execute generated shell text.

Acceptable designs are:

- a protected environment file written atomically and parsed with a strict
  allowlist;
- a NUL-delimited key/value stream read without evaluation; or
- explicit bounded field retrieval into named shell variables.

The transport must reject duplicate keys, unknown keys, embedded NULs, invalid
UTF-8, unsafe values, incomplete reads, symlinks, ownership drift, and
oversized input. Shell callers must fail if any required variable is absent.

**Exit evidence:** no production `eval`, adversarial quoting and newline tests,
ShellCheck success, and unchanged valid shell projections from the example
inventory.

## HARD-05: Release and bootstrap supply-chain evidence

**Goal:** Bind every accepted release to reviewed source and reproducible input
evidence.

Required controls:

- move Python and uv bootstrap pins into a separately reviewed bootstrap
  manifest or equivalent trust anchor;
- verify hashes and signatures when the upstream artifact supports signatures;
- prefer Nix-built bootstrap artifacts when the deployment environment allows
  it;
- record source commit, contract hash, lockfile hash, wheel hash, and helper
  action-manifest hash for each accepted release;
- produce an SBOM for Python dependencies and relevant Nix closure contents;
- reject a dirty source tree, stale lock, contract mismatch, or incomplete
  action map before `.complete` is written; and
- retain no downloaded candidate after failed verification.

**Exit evidence:** tamper tests for every recorded hash, retained release
manifest and SBOM, signature-verification evidence where applicable, and an
installer test proving that `current` remains unchanged after each failure.

## HARD-06: Unix-socket peer authentication

**Goal:** Supplement filesystem socket permissions with process identity checks.

The server must obtain peer credentials with `SO_PEERCRED` on supported Linux
systems and compare the UID/GID against the contract-selected management-web
identity. It must reject a connection when credentials are absent, malformed,
or outside policy. Add bounded per-peer connection concurrency and preserve the
existing request-size and deadline limits.

Evaluate systemd socket activation so systemd owns socket creation, mode,
group, backlog, and cleanup. If socket activation is not adopted, retain tests
for stale socket replacement and refusal to unlink non-sockets.

**Exit evidence:** accepted and rejected UID/GID tests, unavailable-peer-identity
test, connection-limit test, reboot/stale-socket test, and proof that the
controller has no TCP listener.

## HARD-07: Systemd credential isolation

**Goal:** Keep long-lived secrets out of process environment blocks and broadly
readable filesystem trees.

Move suitable service credentials to systemd `LoadCredential` or encrypted
credential facilities. Services must read credentials through the credential
directory and pass only the minimum required values to children. Disable core
dumps for secret-bearing services and verify that diagnostics and journald do
not contain credential values.

The management-web identity must remain unable to read controller, OpenStack,
Nomad, storage-administrator, builder, backup, and age credentials. Nomad and
storage services must not gain controller/provider credentials as a side effect
of this change.

**Exit evidence:** `/proc/<pid>/environ` checks, credential-directory ownership
checks, child-environment allowlist tests, core-dump policy checks, and secret
scans across logs, diagnostics, SQLite, and normal output.

## HARD-08: Crash-durable filesystem replacement

**Goal:** Make every accepted atomic replacement durable across process and host
failure.

Audit configuration installation, release selection, known-hosts replacement,
database backup, restore, diagnostics, manifests, and evidence files for:

- `O_NOFOLLOW` or equivalent direct-file opening;
- regular-file, owner, and mode validation;
- bounded reads and writes;
- file `fsync` before rename;
- parent-directory `fsync` after rename;
- same-filesystem replacement;
- deterministic stale-temporary cleanup; and
- refusal to overwrite an unexpected file type.

**Implemented evidence (2026-08-30):** `openstack_platform.durable` now provides
bounded, no-follow, owner/mode/type-checked replacement with a same-directory
prepared-file commit path, file and parent-directory `fsync`, deterministic
private stale-temp reconciliation, and refusal of unexpected destination or
temporary objects. Configuration generation, private diagnostics, build logs,
build state, SQLite backup, and offline restore use it. Release selection,
release evidence, operator configuration, SSH bridge installation, known-hosts,
hosted backup evidence, and accepted-backup publication have equivalent local
commit checks; cross-directory hosted-backup moves sync both source and target
directories. `tests/test_hardening_properties.py` injects interruption before
write, after write, after file `fsync`, after rename, and after directory
`fsync`; every state is old-or-new and a retry converges. Symlink stale-temp and
unexpected destination regressions prove no unrelated object is removed.

**Exit evidence:** fault-injection tests before write, after write, after file
`fsync`, after rename, and after directory `fsync`, with a documented resulting
state and retry path for each interruption.

## HARD-09: State-machine, property, and parser fuzz tests

**Goal:** Exercise invariants across malformed input, concurrency, and every
recovery checkpoint.

Targets include:

- controller HTTP framing, paths, query parsing, and duplicate JSON keys;
- implementation contract and deployment inventory parsing;
- generated Nomad job identity and route-marker parsing;
- OpenStack and helper provider projections;
- deployment, host replacement, image pruning, storage, and environment state
  transitions;
- idempotency-key replay and conflicting fingerprints; and
- concurrent lock acquisition and deadline exhaustion.

Properties must include “invalid input causes no provider mutation,” “a lost
response does not create a second resource,” and “pre-promotion failure leaves
the accepted deployment referenced.” Fuzz failures must retain a minimized,
non-secret reproducer.

**Implemented evidence (2026-08-30):** deterministic seeded tests now mutate
implementation contracts and inventory JSON, generate malformed controller
routes and HTTP framing, and verify invalid input never reaches a mutation
handler. Existing database tests plus the new unsupported-state fixtures cover
idempotency fingerprint conflicts, replay, durable transition legality, and
invalid-state no-mutation. Seeds are fixed and failing unittest subtests retain
the minimized input. Diagnostic assertions scan responses and replacement
errors for injected secret sentinels.

**Exit evidence:** deterministic seeded property tests in CI, parser fuzz corpus,
state-transition coverage for every durable phase, and retained minimized
regressions for discovered failures.

## HARD-10: Explicit unsupported-state detection

**Goal:** Fail early and clearly when a deployment contains state from an
unsupported prior control surface or contract version.

Detection must occur before migration, provider reconciliation, restore, Nomad
submission, or storage mutation. The error must distinguish unsupported prior
state from corruption and cross-deployment identity mismatch without exposing
raw state. No automatic adoption or guessing is permitted.

Cover database markers, backup directory shape, Nomad metadata, storage
ownership markers, installed release layout, and implementation contract
version. Document the supported fresh-cutover procedure separately from normal
restore.

**Implemented evidence (2026-08-30):** controller databases without the
control-plane marker and databases with future migrations raise the stable safe
`UNSUPPORTED_PRIOR_STATE` classification before WAL setup or migration.
Offline restore preserves that classification and leaves the accepted database
unchanged. PostgreSQL, MongoDB, and Garage deterministic identities containing
pre-existing unowned state return `UNSUPPORTED_PRIOR_STATE` before their first
create/update call rather than adopting it. Seeded and fixture tests verify
byte-for-byte no mutation for migration and restore; provider unit tests verify
no create call. Corruption and deployment-identity mismatch retain separate
errors.

**Exit evidence:** fixtures for each unsupported state class, zero provider calls,
a stable safe error code, and documentation directing operators to fresh
cutover rather than manual marker editing.

## HARD-11: Runtime network and egress restrictions

**Goal:** Limit each component to the network destinations required by its role.

Required controls and tests:

- controller/helper egress restricted to configured OpenStack, Nomad, storage,
  registry, source, and health-check destinations where practical;
- management-web denied access to provider and administrator endpoints;
- source acquisition pinned to the supported HTTPS host and protected against
  redirects, proxy-variable injection, credential helpers, and DNS changes
  across validation/fetch boundaries;
- metadata-address blocking verified for host and forwarded traffic;
- storage ports reachable only from the exact role security groups that require
  them; and
- public ingress preserving `Host` while exposing no controller socket or
  storage administration route.

**Exit evidence:** role-to-role connectivity matrix, denied-path tests, DNS and
redirect tests for source acquisition, metadata guard tests, and live Neutron
security-group verification.

## Completion criteria

The hardening plan is complete when every workstream has its exit evidence,
the target Python and Nix toolchains pass without skips, generated artifacts
match a clean tree, no supported operation depends on shell evaluation or
untyped inventory lookup, and live failure drills preserve accepted application
and data state at every recovery boundary.
