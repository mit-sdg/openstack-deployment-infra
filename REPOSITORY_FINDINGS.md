# Deployment and UI readiness findings

Reviewed 2026-08-30. This file separates work that gates a real deployment from improvements that can follow after UI implementation starts.

## Current decision

- **Start management UI implementation:** yes. The controller/setup and P-level implementation gates are complete.
- **Deploy to production users:** no. The independent-audit P-02 implementation defect is corrected on this branch; P-01 and P-07 defects and the pending Nix/live evidence gates remain unresolved.
- **Run an internal infrastructure deployment:** reasonable after CI; use it to produce the first live acceptance evidence.

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

- 431 unit/packaging/security/recovery tests pass;
- Ruff format and lint pass;
- strict mypy passes for 63 source files;
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

## Production gates

An independent clean-worktree audit classified P-01 and P-02 as partial and P-07 as failed. This branch corrects the audited P-02 drill defect. P-03 through P-06 and P-08 pass code review, subject to the external Nix/live evidence stated below.

### P-01 — Real non-mutating setup preflight — partial

`setup check` now reads and strictly parses Glance image-count and aggregate image-storage quota, using all five byte sizes bound into signed role-artifact evidence. Unlimited, unknown, byte, and GiB formatter variants are covered; missing size evidence and unknown or insufficient capacity cannot report ready. The path remains read-only in mutation-spy coverage. Real-cloud non-mutation evidence remains outstanding.

### P-02 — Off-site backup and full loss recovery — audit defect corrected in code

The admin now creates encrypted managed-data evidence that includes restorable
OCI manifests and blobs, alongside PostgreSQL, MongoDB, and Garage. The
provider-neutral recovery command exports selected committed hosted-controller,
operator-state, and managed-data sets to operator-mounted off-site storage with
an append-only canonical manifest and checksums; import verifies into an empty
private workspace. Monitoring requires a fresh secret-free export receipt.
Bounded restore tools and the full-loss drill recover both SQLite databases,
managed services, and an accepted app artifact without GitHub or the original
registry. Off-site retention remains provider-owned and is applied only after a
newer verified bundle and drill evidence exist. A guarded daily timer verifies a distinct mounted filesystem, rejects local/bind/stale sinks, discovers only committed sets, post-verifies the copy, and updates a credential-free health receipt.

The corrected packaged drill has separate `--full` and `--verify-only` modes. Full mode invokes the supported external operator and hosted-controller restore launchers against explicit absent mode-`0700` replacement directories, never their live destinations. The launchers enforce deployment identity, schema, integrity, foreign-key, and unfinished-operation rules; the drill then proves restored image-selection, application, and accepted-deployment records. Managed restoration is mandatory in full mode, and complete evidence is emitted only after both SQLite restores, record checks, archive checks, and every managed restore pass. Verify-only mode invokes no restore launcher and cannot emit `DRILL-EVIDENCE.json`. Fake-tool integration tests prove launcher arguments and prove operator, hosted, or managed restore failures cannot produce completion evidence. Live execution against disposable replacement services remains an external evidence gate.

### P-03 — Browser-to-controller trust boundary — resolved

The browser renderer and trusted authorization broker now have separate fixed identities. `management-web` cannot open either controller socket or read broker state. The AF_UNIX-only `management-broker` is the exact `SO_PEERCRED` peer for ordinary project routes; the operator-only privileged socket contains admin reads and destructive deletion. Browser authentication, ownership, quota, and CSRF enforcement remain implementation requirements for the broker/UI, not host-boundary gaps.

### P-04 — Bounded Unix HTTP transport — resolved

The transport enforces exact peer identity, global/per-peer connection limits, deterministic overload responses, absolute header/body/write and idle deadlines, a request-per-connection cap, strict JSON parser failures, and shutdown that closes stalled clients. Real socket tests cover slow headers, incomplete bodies, keep-alive, floods, peer rejection, and shutdown.

### P-05 — Direct origin bypass — resolved

`publicIngress.mode=tunnel` is the secure default: Traefik binds loopback and Neutron/NixOS expose no public origin port. `direct` requires exact canonical provider IPv4 CIDRs and applies the same set to Neutron, host firewall, and trusted forwarded headers. World-open, malformed, missing, duplicate, IPv6, and host-bit CIDRs fail before mutation.

### P-06 — Release compatibility and supply-chain evidence

Implemented by a signed pre-build component manifest plus a signed post-build artifact manifest. The latter binds all five concrete QCOW2 SHA-256 values, normalized recursive Nix closures/output references, exact publication metadata, and the source component-manifest digest. Deterministic SPDX 2.3 evidence covers Python and Nix closure packages, and in-toto/SLSA-style provenance names the concrete subjects. Setup and publication verify these records before image acceptance. CI fetches an immutable digest-pinned HTTPS evidence bundle, safely extracts only the exact bounded inventory, and verifies both signatures against the protected Ed25519 trust root; large SBOMs are not transported as CI secrets.

**Exit gate:** complete. Tamper tests cover component inputs, signatures, evidence, QCOW2 bytes, Nix closure identity, source-manifest binding, and publication metadata while installer tests preserve the previously selected release on failure.

### P-07 — Live release acceptance — resolved in code after independent audit

The repository now provides an explicit-opt-in, plan-first disposable acceptance
orchestrator with exact deployment/project scope, bounded driver calls, durable
resume checkpoints, deliberate interruption injection, fixed sanitized evidence,
a hash chain/checksum/HMAC signature, and offline fake-driver tests. The protected
CI job is skipped unless a manual dispatch opts in, the repository enable variable
is true, and the protected environment is approved.

The reviewed repository driver now implements the documented live contract by
composing the supported setup/operator, capability-separated controller,
backup/restore, recovery, host replacement, and exact-name teardown interfaces.
It kills/restarts the controller during an in-flight candidate deployment and
resumes the identical operation, while the deployed fixture proves PostgreSQL,
MongoDB, and S3 writes/reads through runtime bindings. Contract tests exercise
every action through fake transports, including a plan transcript with zero
mutations.
The independent-audit defects are corrected. Required checks now come from exact typed observations and any missing/false fact fails. Managed recovery proves an exact empty disposable logical target, invokes the destructive replacement restore, and compares exact PostgreSQL/MongoDB/S3 content afterward. Replacement and backup disposition have explicit identity/readiness/data and owned-backup observations. Setup creates immutable deployment/project ownership metadata with exact typed provider projections, including keypair fingerprint/public-key/type/user identity; teardown refuses absent, drifted, substring-only, project-mismatched, or ambiguous ownership before deletion. Negative false-true and adversarial ownership tests protect these boundaries. The live evidence run remains an external release gate.

### P-08 — Critical hardening workstreams — resolved in code

Production shell `eval` transport is replaced by a bounded NUL-delimited allowlist. Secret-bearing services use guarded systemd credentials and no core dumps. Critical replacements use no-follow/type/owner/mode checks, file and directory `fsync`, deterministic stale handling, and fault-injection tests. Unsupported prior state fails before migration/provider mutation/restore. Seeded parser/state/idempotency tests and secret scans cover trust boundaries. Management egress and public origin policy are deny-by-default; protected live acceptance supplies the remaining role-to-role evidence.

Module decomposition and broader typed-inventory cleanup remain non-blocking follow-up work.

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
uv run python -m unittest discover -s tests -q   # 431 passed
```

Shell syntax and packaged entrypoint smoke checks also passed. ShellCheck is unavailable locally. `nix flake check --no-build` could not access `/nix/var/nix/daemon-socket/socket`; Nix evaluation, package smoke, five role VM tests, and QCOW2 boot tests remain CI gates.
