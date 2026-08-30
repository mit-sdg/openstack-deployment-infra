# Run the disposable P-07 live release gate

`openstack-platform-acceptance` runs one reviewed, disposable cloud drill and retains a minimal authenticated evidence bundle. It is intended for a protected CI environment or a manually supervised release gate, not for a production deployment or an ordinary project.

The repository supplies `openstack-platform-acceptance-driver`. It composes only the supported operator CLI, controller Unix HTTP API, packaged backup/restore launchers, SSH recovery boundary, and an exact-name OpenStack teardown. Cloud credentials remain in the setup environment/OpenStack wrapper and are never command arguments. The driver requires a direct mode-`0600` configuration through `P07_DRIVER_CONFIG`; its deployment UUID, project UUID, namespace, application slug, inventory, and replacement images are fixed before planning.

## Safety conditions

Do not start a plan until all of these conditions hold:

- the OpenStack project is dedicated to disposable acceptance or the driver can enforce ownership metadata on every mutation;
- the proposed namespace is `p07-<label>-<first-eight-deployment-UUID-characters>` and does not identify an existing deployment;
- the operator has reviewed the repository driver, protected configuration, non-mutating inventory, replacement image UUIDs, and teardown names;
- the signing key is a direct, current-user-owned mode-`0600` file with at least 32 random bytes;
- the state directory is direct, current-user-owned, mode `0700`, and retained across CI retries; and
- the protected environment permits at most one run for the deployment UUID.

The orchestrator refuses an already-owned greenfield scope, a changed driver, an expired plan, an unscoped response, a missing check, an unexpected response field, or a second process using the same state directory. It limits each driver response to 256 KiB, each step to the reviewed timeout, and each invocation to at most 720 minutes. A failed step remains incomplete and the next invocation retries that exact step. Do not delete the checkpoint or repair provider state manually.

A run that cannot resume has not passed. Keep its checkpoint and use the protected driver's deployment-scoped recovery procedure to remove only resources carrying the deployment UUID. Never substitute a project-wide cleanup command.

## 1. Create and review the plan

Create a random deployment UUID. The namespace suffix must equal the first eight characters of that UUID.

```bash
umask 077
export P07_DEPLOYMENT_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
export P07_NAMESPACE="p07-release-${P07_DEPLOYMENT_ID%%-*}"
install -d -m 0700 /private/p07-plan /private/p07-state

uv run openstack-platform-acceptance plan \
  --deployment-id "$P07_DEPLOYMENT_ID" \
  --project-id '<DISPOSABLE_OPENSTACK_PROJECT_UUID>' \
  --namespace "$P07_NAMESPACE" \
  --driver "$PWD/.venv/bin/openstack-platform-acceptance-driver" \
  --output /private/p07-plan/plan.json \
  --max-minutes 360 \
  --step-timeout-seconds 1800
```

`plan` sends one `mode=plan` request. It does not send an execute request. The repository driver validates the token/setup scope, runs `openstack-platform setup` without `--apply`, inventories provider resources read-only, calculates their canonical SHA-256 fingerprint, rejects an existing namespace/prefix, and returns the exact supported action list. Its private transcript contains command/stdin hashes and `mutating: false`; `mutationCount` must be zero.

Review `plan.json`, its printed SHA-256, the deployment/project/namespace binding, bounds, driver and protected-configuration checksums, baseline fingerprint, and ordered actions. Plans expire after 24 hours. Editing a plan invalidates the reviewed checksum and requires a new review.

## 2. Apply or resume the exact plan

Set the opt-in only in the shell that will perform the destructive drill. `--apply`, the environment opt-in, and the deployment-specific confirmation are all required.

```bash
export P07_LIVE_ACCEPTANCE=1
uv run openstack-platform-acceptance run \
  --plan /private/p07-plan/plan.json \
  --driver "$PWD/.venv/bin/openstack-platform-acceptance-driver" \
  --state-directory /private/p07-state \
  --signing-key /private/p07-evidence-hmac.key \
  --apply \
  --confirm "P07:$P07_DEPLOYMENT_ID"
```

The run executes and checkpoints these actions in order:

1. greenfield setup and public platform health;
2. deliberate interruption after a durable operation starts, followed by same-operation resume without a duplicate resource;
3. one application create, PostgreSQL/MongoDB/S3 create-bind-write-read lifecycle, exact-commit deployment, and disable/enable without rebuild or data loss;
4. encrypted external-operator SQLite backup/offline restore and hosted-controller SQLite backup/offline restore;
5. PostgreSQL, MongoDB, and S3 managed-data restore with `RESTORE-MANIFEST` verification;
6. persistent-host replacement retaining the old host until readiness;
7. external admin recovery with controller state and application reconciliation;
8. application/storage deletion; and
9. deployment cleanup, zero owned resources, backup-disposition recording, and equality with the pre-run unrelated-resource fingerprint.

If the command stops, run the same command with the same files and confirmation. Completed events are not replayed. The checkpoint hash chain and exact plan hash prevent resuming against another plan.

## 3. Verify and retain evidence

A passing run writes only fixed check names, booleans, scope identifiers, times, and hashes to:

- `evidence.json` — canonical sanitized result and hash-chained events;
- `evidence.json.sha256` — SHA-256 checksum; and
- `evidence.json.hmac-sha256` — HMAC-SHA-256 signature over the exact evidence bytes.

Provider payloads, resource IDs, command output, credentials, secret values, backup contents, and signing material are not accepted into evidence. Verify the retained copy before release sign-off:

```bash
uv run openstack-platform-acceptance verify \
  --evidence-directory /private/p07-state \
  --signing-key /private/p07-evidence-hmac.key
```

The required result is `p07-evidence=verified result=passed`. Store the three evidence files in the private release evidence system. Keep the signing key separately; HMAC proves possession of the shared key, not a public third-party identity. Record the reviewed plan checksum and reviewer identity outside the evidence bundle.

## Enable the protected CI gate

The normal push, pull-request, schedule, and manual workflows do not run live acceptance. To make a manual run eligible, configure all of the following:

- a repository or organization variable `P07_ACCEPTANCE_ENABLED=true`;
- a protected GitHub environment named `p07-live-acceptance` with required reviewers;
- environment secrets `P07_PROJECT_ID`, `P07_DRIVER_CONFIG_BASE64`, and `P07_EVIDENCE_HMAC_KEY_BASE64`;
- a self-hosted runner carrying both `self-hosted` and `p07-live-acceptance` labels; and
- direct mode-`0600` setup environment, SSH configuration, age identity, and other paths named by the protected driver configuration.

Dispatch the CI workflow with `live_acceptance=true`. Without both the dispatch input and enable variable, the job is skipped. If the protected environment, secrets, runner, or driver is absent, no execute request can be sent: environment approval and the private-input checks precede plan creation, and apply occurs only after a plan exists. A job retry with the same GitHub run ID uses the external private checkpoint directory and resumes the exact plan. Disable the repository variable after the release window.

## Protected repository driver configuration

Copy [`config/p07-driver.example.json`](../config/p07-driver.example.json) outside the checkout, replace every example value, set mode `0600`, and set `P07_DRIVER_CONFIG` to that direct current-user-owned file. The schema is version 1 and closed: unknown or missing fields are rejected. It contains these sections:

- deployment/project/namespace identity;
- fixed operator executable, inventory, policy, state, setup environment/workspace, and OpenStack wrapper paths;
- fixed SSH/SCP executables, mode-`0600` SSH config, admin/recovery aliases, controller socket/curl paths, and admin backup/root/state paths;
- fixed age executable/identity and private local staging/offline-restore directories;
- deployment-scoped application slug, public GitHub repository, exact commit/ref, and typed controller configuration;
- exact ingress/admin replacement image UUIDs; and
- a private command transcript path; and
- the exact `destroy-after-verified-restore` backup disposition, acknowledging that disposable backup volumes are removed only after all restore checks pass.

The setup environment must contain the exact `OS_PROJECT_ID` and `PLATFORM_NAMESPACE`. The application slug must end in the deployment UUID's first eight characters. Executable paths and remote paths are absolute; no configurable argv or shell command is accepted. The driver rechecks the OpenStack token project and protected setup/inventory identity before every mutation.

## Protected driver protocol

The driver reads one strict JSON object from standard input, writes one strict JSON object to standard output, and writes no required information to standard error. It must finish within the orchestrator timeout. Nonzero exit, malformed JSON, duplicate fields, extra fields, or output above 256 KiB fails the current step. The orchestrator discards standard error and does not retain raw standard output.

Every request contains:

```json
{
  "schemaVersion": 1,
  "mode": "plan",
  "action": "full_drill",
  "scope": {
    "deploymentId": "12345678-1234-4234-9234-123456789abc",
    "projectId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "namespace": "p07-release-12345678"
  },
  "requiredActions": ["greenfield_setup"],
  "bounds": {"maxMinutes": 360, "stepTimeoutSeconds": 1800}
}
```

The shown `requiredActions` array is abbreviated; the real request contains the complete ordered list in the plan. A successful plan response has exactly these fields:

```json
{
  "schemaVersion": 1,
  "ok": true,
  "deploymentId": "12345678-1234-4234-9234-123456789abc",
  "projectId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "namespace": "p07-release-12345678",
  "capabilities": ["greenfield_setup"],
  "driverConfigurationSha256": "<64 lowercase hexadecimal characters>",
  "baselineFingerprint": "<64 lowercase hexadecimal characters>",
  "ownedResources": []
}
```

`capabilities` must exactly equal the requested complete action list. `driverConfigurationSha256` binds every protected path, identity, application input, image UUID, and backup disposition to the reviewed plan; execute refuses configuration drift. The baseline fingerprint must cover every unrelated resource whose mutable provider representation is in scope for the drill, using a stable canonical projection. It must exclude volatile fields such as observation timestamps while including identity and mutable state that the drill could change.

Execute requests use `mode=execute`, one plan action, `planSha256`, `driverConfigurationSha256`, `baselineFingerprint`, and the same scope. A successful response has exactly these fields:

```json
{
  "schemaVersion": 1,
  "ok": true,
  "deploymentId": "12345678-1234-4234-9234-123456789abc",
  "projectId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "namespace": "p07-release-12345678",
  "action": "greenfield_setup",
  "checks": {
    "emptyScopeObserved": true,
    "planApplied": true,
    "platformHealthy": true,
    "deploymentScoped": true
  }
}
```

The exact checks for every action are in the reviewed plan. The driver must return a check as `true` only after observing the named outcome, not merely after a command exits zero. `cleanup_verify.unrelatedFingerprintUnchanged` means a fresh canonical unrelated-resource inventory equals `baselineFingerprint`; `cleanup_verify.ownedResourcesAbsent` means every provider type used by the drill was queried and no resource carrying the deployment UUID remains. Preserved encrypted backups must have an explicit approved disposition before `backupsDispositionRecorded` is true.
