# Fresh deployment acceptance checklist

Run the procedures in [OPERATIONS.md](OPERATIONS.md) in order. Record command
output paths, timestamps, image UUID/checksums, readiness markers, public
health results, `RESTORE-MANIFEST`, and reviewer initials in a private evidence
system. Do not put credentials, provider payloads, or age identities in this
checklist.

The command names below are the evidence boundary. A checked box means the
observable result passed, not merely that a command exited zero.

## Reset and scope

- [ ] **RESET-01** Human authorization records the from-scratch deployment and
  which application data, if any, must be preserved. Evidence: private change
  approval in your operations system.
- [ ] **RESET-02** Resources in the OpenStack project that this deployment does
  not own are explicitly out of scope and untouched. Evidence: the private
  scope-record entry in the deployment evidence index, which preserves the
  exact exclusion list without copying UUIDs into Git. A missing private scope
  record is a live evidence gap.
- [ ] **RESET-03** Only the configured OpenStack project/prefix was reconciled;
  no external row was imported. Evidence: `apply_foundation.py` plan/apply,
  token project/name comparison, and scoped provider evidence.
- [ ] **RESET-04** Management state is fresh and empty. Evidence from the
  management host:

  ```bash
  /srv/openstack-platform/bin/openstack-platform status
  /srv/openstack-platform/bin/openstack-platform app list
  /srv/openstack-platform/bin/openstack-platform storage list
  ```

  The first invocation creates the schema; the initial application and storage
  lists contain zero accepted records.

## Configuration, runtime, and images

- [ ] **CFG-01** Private inventory/policy replace every example identity, digest,
  path, and age recipient. Evidence:

  ```bash
  uv sync --frozen
  nix flake check --impure --no-build --print-build-logs
  ```

- [ ] **CFG-02** The token and configured project agree. Evidence is
  `verify_openstack_project` from `infra/lib/platform-config.sh`, as used in
  [OPERATIONS.md](OPERATIONS.md#2-scope-and-reconcile-the-openstack-foundation).
  The helper compares project identity after normalizing compact/canonical
  provider UUIDs and checks the configured project name.
- [ ] **CFG-03** Locked project checks pass on Python 3.14 with uv 0.12.2:

  ```bash
  uv --version
  uv run python --version
  uv run ruff format --check platform_cli deploy/platform-cli infra tests
  uv run ruff check platform_cli deploy/platform-cli infra tests
  uv run mypy
  uv run python -m unittest discover -s tests -v
  ```

- [ ] **IMG-01** All five role images build from one reviewed commit and pass
  static, package, VM, config-drive, and role-specific live checks. Evidence:
  [nix/README.md](../nix/README.md) output and private test logs.
- [ ] **IMG-02** Every published image has full `SOURCE_COMMIT`, complete
  metadata, a provider checksum matching the local publisher checksum, a
  separately recorded QCOW2 SHA-256, active status, configured-project owner,
  and UUID evidence. Evidence: the five successful
  [image publication](IMAGE_PUBLISHING.md#publish-all-five-roles-from-one-reviewed-commit)
  outputs and `infra image list`.

## Foundation, bridge, and role bootstrap

- [ ] **ROLE-01** Foundation plan was reviewed before apply; configured ports
  have the expected network, fixed address, and security group. Evidence:
  `apply_foundation.py` plan/apply output and three scoped `openstack port show`
  records.
- [ ] **ROLE-02** Admin, storage, and ingress each emit the configured exact
  readiness marker, with no failed required unit. Existing-host apply scripts
  first fail closed on any server/image/flavor/metadata/port/volume identity
  mismatch and do not reapply user data. Evidence: bounded serial console
  output and `apply_admin.sh`, `apply_storage.sh`, and `apply_ingress.sh`
  results.
- [ ] **ROLE-03** `setup_management_bridge.py` returns
  `management-bridge=verified`; its SSH config/known-hosts are direct mode
  `0600` files below a mode-`0700` directory; and the alias smoke returns an
  unprivileged UID. Evidence:

  ```bash
  test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- id -un)" = agentops
  test "$(ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- id -u)" -gt 0
  ```

- [ ] **ROLE-04** Nomad ACL bootstrap returns `nomad-acl-and-raft=healthy`.
  Evidence: private admin output, mode-`0600`
  `<paths.root>/secrets/nomad-tokens.env`, and the ingress transfer record.
  Never record token contents.
- [ ] **ROLE-05** Admin receives direct mode-`0600` OpenStack/storage inputs,
  provisioning PKI, and builder SSH identity at the exact paths in
  [OPERATIONS.md](OPERATIONS.md#4-boot-storage-and-ingress-with-exact-transfers).
  Evidence: private ownership/mode checks only.
- [ ] **ROLE-06** Public platform health passes through the external DNS/TLS
  service with original `Host` preserved:

  ```bash
  test "$(curl --fail --show-error --silent "https://$PLATFORM_HOSTNAME/healthz")" = OK
  ```

  Expected body: `OK`. Cloudflare token/account or the equivalent external
  ingress service is recorded as an external input, never as a secret value.

## First application and managed storage

- [ ] **APP-01** A public credential-free repository, full lowercase 40-character
  commit, `platform.yaml`, and supported lockfile pass validation. Evidence:
  `app deploy` output and the source commit record.
- [ ] **APP-02** Builder and fixed port are absent after deployment; worker is
  ready and scheduler health passes. Evidence: `app show`, `infra list`, and
  bounded cleanup evidence.
- [ ] **APP-03** Application public health passes at the exact configured
  hostname and health path. Evidence:

  ```bash
  test "$(curl --fail --show-error --silent --output /dev/null \
    --write-out '%{http_code}' "https://<slug>.<domain>/<health-path>")" = 200
  ```

- [ ] **APP-04** `app show` records the immutable image digest, exact commit,
  recipe identity, and public URL. Credentials are absent from output/logs.
- [ ] **DATA-01** Storage is created only after app acceptance and then passes
  provider access/application health verification:

  ```bash
  /srv/openstack-platform/bin/openstack-platform storage create SLUG postgres
  /srv/openstack-platform/bin/openstack-platform storage verify SLUG postgres
  /srv/openstack-platform/bin/openstack-platform storage show SLUG postgres
  ```

- [ ] **DATA-02** Storage credentials are absent from SQLite, command output,
  management logs, and evidence. `storage list/show` exposes the non-secret
  `PROVIDER_ID` and `PROVIDER_NAME` columns. The recorded storage row contains
  only provider identity, ownership, limits, and checkpoints.

## Backups, restore, upgrade, and cleanup

- [ ] **BACKUP-01** `openstack-platform backup` produces an age-v1 encrypted
  management database accepted under `<paths.backups>/m1`, with checksum and
  manifest evidence. The manifest is the commit marker and retention counts only
  complete ciphertext/checksum/manifest trios. Evidence includes the output
  name/SHA-256 and private file metadata, not database content. Verify
  `/srv/openstack-platform/bin/age --version` and the configured staging path
  `<paths.backups>/m1/.staging`.
- [ ] **BACKUP-02** On **admin**, not management, managed-data backup emits
  PostgreSQL, MongoDB, and Garage archives, and the restore check writes
  mode-`0600` `RESTORE-MANIFEST` only after all temporary checks pass. Evidence:

  ```bash
  ssh -F /srv/openstack-platform/.secrets/ssh/config platform-admin -- env \
    PLATFORM_CONFIG="/etc/$PLATFORM_NAMESPACE/platform.json" \
    AGE="$PLATFORM_ROOT/bin/age" \
    AGE_KEY="$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt" \
    "$PLATFORM_ROOT/infra/backup/verify_latest_restore.sh"
  ```

- [ ] **BACKUP-03** Offline restore is rehearsed with the operator age
  identity and `--yes` through the installed fixed-destination
  `openstack-platform-restore` launcher. Evidence:
  `restore=verified schema-version=... integrity=ok`, destination mode `0600`,
  and post-restore `status`/read-list reconciliation.
  Restore must match the installed deployment's project/namespace/stable
  inventory identity; a backup from another deployment is refused. The
  identity remains outside Git and SQLite.
- [ ] **UPGRADE-01** A tested role image is selected by UUID and
  `openstack-platform infra replace ROLE --yes` retains old host/volumes until
  readiness. Acceptance re-reads the replacement and requires the exact
  selected image UUID, retained flavor UUID, configured name, and operation
  provenance before declaring the role healthy. Evidence: operation phase,
  `infra logs`, and live provider/health records. No delete-first action is
  accepted.
- [ ] **UPGRADE-02** Management/helper release installation selects a complete
  full-commit release and leaves the prior complete release available for
  executable recovery. Evidence: `.complete`, release smoke, helper malformed
  envelope response, and timer state.
- [ ] **CLEAN-01** Managed storage removal uses exact-slug confirmation and
  proves provider absence and environment-key removal; S3 purge is supplied
  explicitly only when authorized.
- [ ] **CLEAN-02** Application removal proves job, variables, worker/fixed port,
  and all tracked current/prior/failed-candidate manifests are absent:

  ```bash
  /srv/openstack-platform/bin/openstack-platform app remove SLUG --confirm SLUG
  /srv/openstack-platform/bin/openstack-platform app list
  /srv/openstack-platform/bin/openstack-platform storage list
  ```

- [ ] **RECOVER-01** At least one interrupted-operation drill reruns the exact
  command and reaches a recorded result without manual SQLite/provider edits.
  Evidence: operation ID, phase, safe recovery output, and final observation.

## Sign-off

- Operator: ____________________  Date: __________
- Reviewer: ____________________  Date: __________
- Deployment/project: ____________________
- Evidence index: ____________________
