# Self-hosted management implementation baseline

This inventory records the historical implementation boundary before the
self-hosted management work began. It is an archived Phase 0 extraction aid,
not a description of the current code and not a supported product contract.
Current behavior is documented in
[`CONTROL_PLANE_CONTRACT.md`](CONTROL_PLANE_CONTRACT.md).

## CLI composition

| Area | Current entry points | Extraction boundary |
| --- | --- | --- |
| Argument parsing and output | `platform_cli/cli.py` | Keep parsing, prompting, table rendering, and process exit handling in a temporary CLI adapter. Move mutation orchestration and typed results behind services. |
| Application deployment | `_app_create`, `_app_deploy`, `_recover_app_deployment`, and `_app_remove` in `platform_cli/cli.py`; primitives and `execute_deployment_workflow` in `platform_cli/app.py` | One application service must own create, deployment, recovery, lifecycle, environment, and deletion independently of `argparse`. |
| Managed storage | `_storage_mutation` in `platform_cli/cli.py`; provider orchestration in `platform_cli/storage.py` | One storage service must expose typed provision, verify, rotate, label, and remove operations without CLI namespace objects or rendered output. |
| Infrastructure | infrastructure branches in `platform_cli/cli.py`; `platform_cli/openstack.py` and `platform_cli/status.py` | Infrastructure mutations remain operator-facing. Safe read models are reused by the later administrator API. |
| Backup and restore | backup dispatch in `platform_cli/cli.py`; `platform_cli/restore.py` and helper backup actions | These remain CLI/recovery concerns and do not become management-product operations. |

## Trusted helper boundary

The fixed action manifest is `platform_cli/helper/actions-v1.txt`, assembled by
`platform_cli/helper/production.py`, and dispatched by
`platform_cli/helper/main.py`. Application actions are implemented in
`platform_cli/helper/app.py`; Nomad Variable compare-and-set behavior is in
`platform_cli/helper/nomad.py`; managed storage actions are in
`platform_cli/helper/storage.py`.

The controller must keep the action allowlist, strict JSON argument checking,
bounded output, deadlines, and safe helper errors. Local Unix-socket transport
must not turn helper executable paths, raw Nomad input, provider IDs, or
provider output into runtime-selected management inputs.

## State and recovery

| State | Current implementation | Required transition |
| --- | --- | --- |
| SQLite schema and migrations | `platform_cli/db.py` | Append-only migrations add deployment attempts, immutable snapshots, lifecycle state, idempotency fingerprints/results, environment revisions, resource UUIDs/labels, worker roles, and tombstones. |
| Operation journals | `operations` records and deployment recovery phases in `platform_cli/db.py`, `platform_cli/app.py`, `platform_cli/storage.py`, and `platform_cli/cli.py` | Preserve checkpoints while moving orchestration into services and then the controller. Every externally mutating phase must remain resumable. |
| Locking and deadlines | `platform_cli/runtime.py` and operation-specific callers | Preserve global infrastructure scopes, per-application scopes, bounded lock acquisition, and one durable whole-operation deadline. |
| Installed state | `/srv/openstack-platform/state` on the external management host according to the current contract and installers under `deploy/platform-cli/` | Move controller state, locks, diagnostics, and build logs to the retained admin-state volume through the offline cutover procedure. |
| Backup and restore | `platform_cli/restore.py`, `infra/backup/`, release launchers, and current operations documentation | Retain deployment identity, encryption, integrity, manifest, ownership, retention, and offline-restore checks while adding an independently accessible encrypted copy. |

## Demonstrated failed-candidate risk

The current deployment uses one Nomad job named by the stable application slug
and one application worker:

- `render_nomad_job` in `platform_cli/app.py` sets `canary = 0` and
  `auto_revert = false`;
- `_deploy_handler` in `platform_cli/helper/app.py` submits the replacement to
  that shared job;
- failed health handling calls `app.remove`; and
- `_remove_handler` executes `nomad job stop -purge` for the shared job.

Consequently, failed-candidate cleanup cannot prove that the previously
accepted allocation and route remain available. The expected-failure test
`ApplicationHelperTests.test_candidate_cleanup_preserves_shared_accepted_job`
records this gap. It must become an ordinary passing preservation test when the
candidate design lands.

## Verification surfaces

- `tests/test_platform_app.py`: source, recipe, builder, worker, deployment, and
  environment orchestration.
- `tests/test_platform_helper_app.py`: constrained Nomad and Variable actions.
- `tests/test_platform_storage.py`: managed storage lifecycle and recovery.
- `tests/test_platform_cli_integration.py`: current CLI composition and durable
  operation behavior.
- `tests/test_platform_foundation_db.py`: schema identity, migrations, and
  operation records.
- `tests/test_packaging_release.py` and Nix checks: release and role packaging.
- Live Nomad, Traefik, OpenStack, backup, and recovery drills: required evidence
  for candidate safety and cutover; mocks alone are insufficient.

The existing live application is observation-only during early phases. Its
fresh cutover occurs only after candidate preservation and storage-linkage tests
pass, with any downtime limited to the later announced window.
