# Deployment and UI readiness findings

Reviewed 2026-08-30. This file separates work that gates a real deployment from improvements that can follow after UI implementation starts.

## Current decision

- **Start management UI implementation:** yes, after this branch passes CI/Nix gates. The five controller/setup blockers found in the first review are fixed here.
- **Deploy to production users:** not yet. The remaining production gates are listed below.
- **Run an internal infrastructure deployment:** reasonable after CI and one live acceptance run; do not treat it as production-ready.

## Original blockers — fixed on this branch

### F-01 — Back up the actual hosted controller database — resolved

The hosted database at `<paths.adminState>/controller/state/platform.sqlite3` now has:

- a distinct encrypted evidence set under `<paths.backups>/hosted-controller`;
- a daily hardened systemd backup service/timer;
- a root-only offline restore launcher using existing identity/schema/integrity/operation checks; and
- documentation that distinguishes hosted-controller state from the external operator CLI database.

The setup acceptance check also requires the hosted-controller backup timer to be enabled.

### F-02 — Return `202` before external work and keep reads responsive — resolved

External mutations now use a durable SQLite dispatch journal and bounded executor:

- four workers and 32 admitted operations;
- per-application serialization;
- independent worker database connections;
- prompt `202` responses and responsive reads/polling;
- same-key idempotent replay without duplicate execution; and
- restart handling for queued and interrupted operations.

Audit found and corrected a recovery deadlock in the first implementation: a `recovery_required` operation can now be resumed only by repeating the identical method, path, body, and idempotency key. Secret request bodies are supplied again by the caller and are not persisted in the dispatch journal.

### F-03 — Restore a green Python CI baseline — resolved locally

The stale release-installer test expectations were corrected and the repository was formatted. Local evidence:

- 362 unit/packaging tests pass;
- Ruff format and lint pass;
- strict mypy passes for 56 source files;
- vulture, compileall, entrypoint smoke tests, JSON/SVG checks, and shell syntax pass.

Nix evaluation and VM builds still require CI because this environment cannot access the Nix daemon.

### F-04 — Remove contradictory controller documentation — resolved

Current docs now consistently state that setup installs the policy/helper release and the admin role starts the hosted controller. Hosted-controller and external operator-state backups are named separately.

### F-05 — Make controller readiness a setup gate — resolved

Setup now refuses completion unless the admin host confirms:

- controller and readiness services are active;
- the readiness result succeeded;
- the socket is `platform-controller:controller-api` mode `0660`;
- the unprivileged operator cannot cross the management-only socket boundary; and
- the hosted-controller backup timer is enabled.

## Production deployment blockers

These do **not** prevent UI coding, but they block exposing the finished UI to real users.

### P-01 — Real non-mutating setup preflight — resolved

`setup check` now reuses strict setup resolution and produces human or JSON plans covering authenticated project identity, quota deltas, exact network/flavor/volume/address choices, collisions, local tooling, ingress, and commit/digest-pinned release and image sources. Mutation-spy and adversarial tests prove the path does not write files, build outputs, generate credentials, or issue provider mutations.

### P-02 — Off-site backup and full loss recovery — resolved

The admin now creates encrypted managed-data evidence that includes restorable
OCI manifests and blobs, alongside PostgreSQL, MongoDB, and Garage. The
provider-neutral recovery command exports selected committed hosted-controller,
operator-state, and managed-data sets to operator-mounted off-site storage with
an append-only canonical manifest and checksums; import verifies into an empty
private workspace. Monitoring requires a fresh secret-free export receipt.
Bounded restore tools and the full-loss drill recover both SQLite databases,
managed services, and an accepted app artifact without GitHub or the original
registry. Off-site retention remains provider-owned and is applied only after a
newer verified bundle and drill evidence exist.

### P-03 — Harden the browser-to-controller trust boundary

Socket access still grants every product mutation and admin read. A compromise of the internet-facing `management-web` process therefore grants full product control. Linux peer credentials, per-peer connection limits, and management-web network egress restrictions remain planned rather than implemented.

**Exit gate:** implement the selected containment model before public launch. At minimum require `SO_PEERCRED`, bounded peer concurrency, strict web-process filesystem/network isolation, separate ordinary/admin authorization paths, and tests proving the web identity cannot access provider/admin credentials or endpoints.

### P-04 — Bound Unix HTTP connection resources

The Unix server still creates an unbounded thread per connection and has no header/read/idle timeout or request-per-connection cap.

**Exit gate:** add connection limits and deadlines; test slow headers, short bodies, idle keep-alive, floods, overload behavior, and shutdown with stalled clients.

### P-05 — Prevent direct origin bypass

Ingress security groups allow ports 80 and 443 from `0.0.0.0/0`. Once the management route exists, direct clients can supply its `Host` header and bypass provider-layer filtering/rate limiting.

**Exit gate:** use an authenticated tunnel/no public listener or restrict origin traffic to configured provider ranges. If direct ingress remains supported, provision origin TLS before routing management traffic.

### P-06 — Release compatibility and supply-chain evidence

Implemented by the deterministic signed release manifest, retained SBOM and provenance evidence, explicit Ed25519 trust root, pre-mutation setup/installer verification, immutable GitHub Action SHAs, and documented forward-only database ordering. The absent UI remains an explicit versioned placeholder in the component set rather than an omitted identity. Unsigned evidence is accepted only as visibly marked development evidence with an exact acknowledgement and is refused when the environment is production.

**Exit gate:** complete. Tamper tests cover the signature, evidence, contract, lockfile, wheel inputs, helper actions, controller, and role-image inputs while installer tests preserve the previously selected release on failure.

### P-07 — Live release acceptance — orchestrator implemented; live evidence required

The repository now provides an explicit-opt-in, plan-first disposable acceptance
orchestrator with exact deployment/project scope, bounded driver calls, durable
resume checkpoints, deliberate interruption injection, fixed sanitized evidence,
a hash chain/checksum/HMAC signature, and offline fake-driver tests. The protected
CI job is skipped unless a manual dispatch opts in, the repository enable variable
is true, and the protected environment is approved.

The reviewed repository driver now implements the documented live contract by
composing the supported setup/operator, controller, backup/restore, recovery, host
replacement, and exact-name teardown interfaces. Contract tests exercise every
action through fake transports, including a plan transcript with zero mutations.
The gate remains closed until a protected runner executes it and a reviewer accepts
a verified passing evidence bundle; no live pass is claimed here.

### P-08 — Complete critical hardening workstreams

The following items in `docs/HARDENING_PLAN.md` are production gates: remove shell `eval` transport, credential isolation, crash-durable replacements, unsupported-state rejection, secret/egress tests, and parser/state-machine fuzzing at trust boundaries.

Module decomposition and full typed-inventory cleanup can proceed independently unless they block one of those controls.

## Important but not blocking UI work or an internal deployment

### N-01 — Signed prebuilt-image fast path

Fresh setup builds and boot-tests five QCOW2 images locally. Keep that auditable path, but add a signed prebuilt release path for ordinary installations. This is important to the ease-of-setup goal, not a correctness prerequisite for an internal source-built deployment.

### N-02 — Documentation compression

Contradictory safety claims are fixed, but tracked Markdown remains large and repetitive. After contracts stabilize, consolidate into:

- README/quick start;
- setup;
- operations and disaster recovery;
- generated configuration/CLI/API reference;
- architecture/trust boundaries; and
- UI contract.

Move implementation plans to issue tracking or mark them maintainer-only.

### N-03 — Provider/lifecycle module decomposition

Several modules remain very large. Split one recovery domain at a time after behavior is protected by tests. Do not combine decomposition with schema or state-machine changes.

### N-04 — Broader typed inventory cleanup

Replacing remaining dotted configuration lookups improves maintainability and static assurance. It can land incrementally and does not need to delay UI screens or an internal deployment.

### N-05 — Product conveniences outside the current contract

Private repositories, webhooks, custom domains, teams, previews, scaling, scheduled jobs, cancellation, shell access, database consoles, credential reveal/export, and persistent runtime-log archives remain explicitly out of scope.

## Foundations to preserve

- Typed deployment input rejects shell commands, Dockerfiles, host paths, provider IDs, unknown fields, and credential-bearing repository URLs.
- Source commits and runtime images use exact identities/digests.
- End users receive no OpenStack, Nomad, SSH, registry, or database-admin credentials.
- Candidate promotion, operation journals, cleanup evidence, deployment identity binding, and refusal to guess ambiguous provider results are sound.
- Environment values are write-only and excluded from API reads and controller SQLite.
- Nix role tests, bounded subprocess helpers, and exact provider-resource checks provide a strong base for the remaining live acceptance work.

## Verification performed after integration

```text
uv lock --check
uv run ruff format --check openstack_platform deploy/releases infra tests
uv run ruff check openstack_platform deploy/releases infra tests
uv run mypy
uv run vulture openstack_platform deploy infra tests --min-confidence 80 --sort-by-size
uv run python -m compileall -q openstack_platform deploy infra tests
uv run python -m unittest discover -s tests -q   # 362 passed
```

Shell syntax and packaged entrypoint smoke checks also passed. ShellCheck is unavailable locally. `nix flake check --no-build` could not access `/nix/var/nix/daemon-socket/socket`; Nix evaluation, package smoke, five role VM tests, and QCOW2 boot tests remain CI gates.
