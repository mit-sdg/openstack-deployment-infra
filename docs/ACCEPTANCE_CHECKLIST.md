# Fresh deployment acceptance checklist

Follow [OPERATIONS.md](OPERATIONS.md) in order. Record command-output paths,
timestamps, image UUIDs/checksums, readiness markers, public health,
`RESTORE-MANIFEST`, and reviewer initials in a private evidence system. Never
record credentials, provider payloads, or age identities here.

The infrastructure sections below cover the implemented pre-management state.
The sync-engine management application and authentication application are not
acceptance claims. The separate P-07 release drill exercises the trusted
controller application lifecycle directly; it does not claim that the future
browser management or authentication applications exist.

Check a box only when the observable result passed, not merely when a command
exited zero.

## P-07 disposable live release gate

Use [Run the disposable P-07 live release gate](LIVE_ACCEPTANCE.md). A unit-test
result or successful plan is not live acceptance. Sign off only from a completed
private evidence bundle whose checksum and HMAC signature verify.

- [ ] **P07-01** The reviewed, unexpired plan binds the protected driver checksum,
  disposable deployment UUID/namespace, exact OpenStack project, unrelated-resource
  baseline fingerprint, ordered actions, and duration limits. The greenfield
  preflight reports no deployment-owned resource.
- [ ] **P07-02** The run proves greenfield setup and an intentionally interrupted
  durable operation resumes under the same identity without duplication or stable
  service impact.
- [ ] **P07-03** One application is created, deployed at an exact commit, publicly
  verified, disabled, and enabled without rebuild or data loss. PostgreSQL, MongoDB,
  and S3 each pass create, bind, write, and read checks.
- [ ] **P07-04** External-operator and hosted-controller encrypted SQLite backups
  each pass offline restore; managed PostgreSQL, MongoDB, and S3 pass restore with a
  verified `RESTORE-MANIFEST`.
- [ ] **P07-05** Persistent-host replacement retains the old host through readiness;
  external admin recovery retains controller state and reconciles the application.
- [ ] **P07-06** Application/storage deletion and deployment cleanup leave no owned
  provider resources. A fresh unrelated-resource fingerprint exactly matches the
  plan baseline, and retained backup disposition is recorded.
- [ ] **P07-07** `openstack-platform-acceptance verify` reports
  `p07-evidence=verified result=passed`. The private evidence system retains the
  reviewed plan checksum, sanitized evidence/checksum/signature trio, CI/manual run
  identity, and reviewer approval; it does not retain credentials or provider
  payloads.

## Reset and scope

- [ ] **RESET-01** Human authorization records the from-scratch deployment and
  any data that must be preserved. Evidence: private change approval.
- [ ] **RESET-02** Resources not owned by this deployment are explicitly out of
  scope and untouched. Evidence: a private scope-record entry with the exact
  exclusion list.
- [ ] **RESET-03** Only the configured project/prefix was reconciled; no provider
  row was imported. Evidence: `apply_foundation.py` plan/apply, token
  project/name comparison, and scoped provider evidence.
- [ ] **RESET-04** Operator state starts with no product records. Evidence:

  ```bash
  /srv/openstack-platform/bin/openstack-platform status
  ```

  The status row reports `APPS=0` and `STORAGE=0`. The operator CLI has no
  commands to list or mutate product records.

## Configuration, runtime, and images

- [ ] **CFG-01** Private inventory and policy replace every example identity,
  digest, path, and age recipient. Evidence:

  ```bash
  uv sync --frozen
  nix flake check --impure --no-build --print-build-logs
  ```

- [ ] **CFG-02** The authenticated token and configured project agree. Evidence:
  `verify_openstack_project` from `infra/lib/platform-config.sh`, including
  normalized project UUID and exact project-name comparison.
- [ ] **CFG-03** Locked project checks pass on Python 3.14 with uv 0.12.2:

  ```bash
  uv --version
  uv run python --version
  uv run ruff format --check openstack_platform deploy/releases infra tests
  uv run ruff check openstack_platform deploy/releases infra tests
  uv run mypy
  uv run python -m unittest discover -s tests -v
  ```

- [ ] **IMG-01** All five role images build from one reviewed commit and pass
  static, package, VM, config-drive, and role-specific live checks. Evidence:
  [nix/README.md](../nix/README.md) output and private test logs.
- [ ] **IMG-02** Every published image has full `SOURCE_COMMIT`, complete
  metadata, matching provider/local checksums, separately recorded QCOW2
  SHA-256, active status, configured-project ownership, and UUID evidence.
  Evidence: five successful image-publication outputs and `infra image list`.

## Foundation, bridge, and persistent roles

- [ ] **ROLE-01** Foundation plan was reviewed before apply. Configured ports
  have the expected network, fixed address, and security group. Evidence:
  plan/apply output and three scoped `openstack port show` records.
- [ ] **ROLE-02** Admin, storage, and ingress each emit the exact configured
  readiness marker with no failed required unit. Existing-host scripts reject
  any server/image/flavor/metadata/port/volume mismatch and do not reapply user
  data. Evidence: bounded console output and role-apply results.
- [ ] **ROLE-03** `setup_operator_bridge.py` returns
  `operator-bridge=verified`; its config/known-hosts files are direct mode
  `0600` below a mode-`0700` directory; and the alias is unprivileged:

  ```bash
  test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- id -un)" = agentops
  test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- id -u)" -gt 0
  ```

- [ ] **ROLE-04** Nomad ACL bootstrap returns
  `nomad-acl-and-raft=healthy`. Evidence: private admin output, direct mode
  `0600` token file, and ingress transfer record; never token contents.
- [ ] **ROLE-05** Admin receives the exact direct mode-`0600`
  OpenStack/storage inputs, provisioning PKI, and builder SSH identity from
  [Operations](OPERATIONS.md#4-boot-storage-and-ingress-with-exact-transfers).
  Evidence: ownership and mode checks only.
- [ ] **ROLE-06** Public platform health passes through external DNS/TLS with
  original `Host` preserved:

  ```bash
  test "$(curl --fail --show-error --silent "https://$PLATFORM_HOSTNAME/healthz")" = OK
  ```

## Current control-surface boundary

- [ ] **CTRL-01** Installed CLI help exposes only `setup`, `status`, `backup`,
  `restore`, and `infra` at the top level. No product lifecycle command is
  present. Evidence: installed `--help` output and CLI integration tests.
- [ ] **CTRL-02** `status` reports five accepted image roles, three available
  persistent observations, and zero application/storage records. Evidence:
  status and `infra list` output.
- [ ] **CTRL-03** Controller transport/API tests pass for restricted Unix-socket
  mode, bounded strict JSON, canonical UUID idempotency, safe errors, product
  routes, and administrator reads. Evidence:

  ```bash
  uv run python -m unittest \
    tests.test_controller_http tests.test_controller_api -v
  ```

- [ ] **CTRL-04** Setup installs the controller policy/helper and succeeds only
  after the admin controller service, restricted socket, and API readiness unit
  pass. No public listener exposes the controller. Evidence: setup tests/output,
  active units, socket metadata, and architecture review.

## Backups, restore, upgrade, and cleanup

- [ ] **BACKUP-00** `<namespace>-hosted-controller-backup.service` produces a
  committed encrypted trio under `<paths.backups>/hosted-controller`; an
  off-host decrypted mode-`0600` copy is restored with the controller and
  hosted-backup units stopped through
  `openstack-platform-hosted-controller-restore --yes`, followed by controller
  readiness and a fresh backup. This is not the external operator-state backup.
- [ ] **BACKUP-01** `openstack-platform backup` produces an age-v1 encrypted
  external operator-state database under `<paths.backups>/controller` with checksum and final
  manifest evidence. Retention counts only complete trios. Evidence: output
  name/SHA-256 and private metadata, not database content.
- [ ] **BACKUP-02** On admin, managed-data backup emits PostgreSQL, MongoDB, and
  Garage archives; restore checking writes mode-`0600` `RESTORE-MANIFEST` only
  after all temporary checks pass. Evidence:

  ```bash
  ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- env \
    PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
    AGE="$PLATFORM_ROOT/bin/age" \
    AGE_KEY="$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt" \
    "$PLATFORM_ROOT/infra/backup/verify_latest_restore.sh"
  ```

- [ ] **BACKUP-03** Offline restore is rehearsed with the operator age identity
  through `openstack-platform-restore ... --yes`. Evidence:
  `restore=verified schema-version=... integrity=ok`, destination mode `0600`,
  and post-restore `status`/`infra list` reconciliation. A backup with a
  different deployment identity is refused.
- [ ] **UPGRADE-01** A tested role image is selected by UUID and
  `openstack-platform infra replace ROLE --yes` retains the old host/volumes
  until readiness. Acceptance re-reads exact image UUID, retained flavor UUID,
  configured name, and operation provenance. No delete-first action is used.
- [ ] **UPGRADE-02** Operator/helper release installation selects a complete
  full-commit release and retains the prior complete release for executable
  recovery. Evidence: `.complete`, release smoke, helper malformed-envelope
  response, and timer state.
- [ ] **CLEAN-01** Image cleanup uses `infra image prune` followed by reviewed
  `--apply --yes`; selected, server-referenced, and operation-referenced images
  remain protected.
- [ ] **CLEAN-02** Whole-deployment teardown has separate human authorization,
  preserves both backup classes and age identities, and scopes provider removal
  to the configured project/prefix. It does not use retired product commands.
- [ ] **RECOVER-01** An interrupted infrastructure operation is rerun with the
  same command identity and reaches a recorded result without manual SQLite or
  provider edits. Evidence: operation ID, phase, safe output, and final
  observation.

## Sign-off

- Operator: ____________________  Date: __________
- Reviewer: ____________________  Date: __________
- Deployment/project: ____________________
- Evidence index: ____________________
