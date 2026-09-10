# Deploy an application without the management UI

Use the controller HTTP API for operator-assisted deployments. The management
UI is not required. These commands support applications accepted by the platform's
Node/Bun package-script build contract, not arbitrary Dockerfiles or VM workloads.

The `maintenance: true` option requires a controller advertising
`maintenance-after-build-v1` and its matching helper release. It builds and checks
the artifact while the accepted application serves, then stops the exact accepted
job and worker before starting its replacement. This avoids concurrent application
processes, but **does not provide zero downtime**: worker boot and application
startup occur during the cutover. A retained primary IPv4 cannot attach to both
workers simultaneously.

## Preconditions and access

- Verify controller, helper, registry, storage, ingress, selected images, and backup
  readiness using [Operations](OPERATIONS.md). Keep recent managed-data backups
  and restore-verification evidence. Record the accepted deployment before changing it.
- Confirm the application listens on its configured port, has a health endpoint,
  and uses supported package scripts. Resolve a full Git commit and check its CI.
- Review database migrations. Deployment rollback restores code/configuration,
  **not database contents**. An incompatible migration can make code rollback unsafe.
  If a quiesced backup is required, use an explicitly approved maintenance procedure;
  this API does not automatically take a quiesced data-volume snapshot.
- The operator needs the privileged socket. App declaration, environment, storage,
  and project reads need the project socket as its admitted broker identity.
  Both are host-enforced Unix peer capabilities, not public unauthenticated endpoints.
- Use Bash, `curl`, `jq`, Python 3, and the installed pinned SSH configuration.
  Do not use `set -x`, put secrets in command arguments, or print private JSON/logs.

The following transport functions run on the **management host as its operator**.
They only transport `curl`; they contain no deployment logic. Replace `61040` if
using another installed namespace. The project transport uses the recovery SSH
identity and `sudo -u management-broker`; obtain approval for that scope first.
Do not loosen socket permissions or grant the operator additional Unix groups.

```bash
set -euo pipefail
umask 077
NAMESPACE=61040
SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config
RECOVERY_KEY=/srv/openstack-platform/.secrets/setup/admin_nova_rsa
ADMIN_SOCKET="/run/${NAMESPACE}-controller/privileged.sock"
PROJECT_SOCKET="/run/${NAMESPACE}-controller/project.sock"

admin() {
  local command
  printf -v command '%q ' curl --fail-with-body --silent --show-error \
    --unix-socket "$ADMIN_SOCKET" "$@"
  ssh -F "$SSH_CONFIG" platform-admin -- "$command"
}
project() {
  local command
  printf -v command '%q ' sudo -u management-broker -- \
    curl --fail-with-body --silent --show-error --unix-socket "$PROJECT_SOCKET" "$@"
  ssh -F "$SSH_CONFIG" -o IdentitiesOnly=yes -i "$RECOVERY_KEY" \
    -l ubuntu platform-admin -- "$command"
}

mkdir -p -m 700 "$HOME/deployment-requests"
RUN=$(mktemp -d "$HOME/deployment-requests/request.XXXXXXXX")
cd "$RUN"
admin http://localhost/v1/admin/capabilities > capabilities.json
jq -e '.apiVersion == 1 and (.features | index("maintenance-after-build-v1") != null)' \
  capabilities.json >/dev/null
```

A missing route, rejected feature, or failed capability check means **stop and
upgrade the controller/helper through the supported release process**. Do not
fall back to disabling the old application before building. A new source checkout
or helper alone does not upgrade the controller baked into the admin image.

## Declare once, or select an existing application

For an existing application, set `APP_ID` to its canonical UUID and `SLUG` to its
slug. Do not create a second declaration on each deployment.

For a new application, choose an unused slug:

```bash
SLUG=my-app
python3 -c 'import uuid; print(uuid.uuid4())' > declare-key.txt
jq -n --arg slug "$SLUG" '{slug:$slug}' > declare.json
project -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(<declare-key.txt)" --data-binary @- \
  http://localhost/v1/applications < declare.json > declared.json
APP_ID=$(jq -er .applicationId declared.json)
```

Save that UUID. Declaration returns `201`; it does not start a worker.
All subsequent mutating operations below return an operation to poll. For a
project mutation, extract its returned `statusUrl` and poll with
`project "http://localhost$STATUS_URL"`; use `admin` for privileged mutations.
The two sockets deliberately expose different routes.

For either branch, record the baseline privately:

```bash
BASE="http://localhost/v1/admin/applications/$APP_ID"
PROJECT_BASE="http://localhost/v1/applications/$APP_ID"
project "$PROJECT_BASE" > before-app.json
project "$PROJECT_BASE/storage" > before-storage.json
PREVIOUS=$(jq -r '.activeDeploymentId // empty' before-app.json)
if test -n "$PREVIOUS"; then
  project "http://localhost/v1/deployments/$PREVIOUS" > previous-deployment.json
fi
```

## Prepare configuration and optional resources

For an unchanged configuration, preserve the accepted snapshot and revision:

```bash
jq -e '.configuration' previous-deployment.json > configuration.json
CONFIGURATION_REVISION=$(jq -er .configurationRevision previous-deployment.json)
```

For a first deployment or intentional configuration change, create a reviewed
`configuration.json`. This minimal example requires a root `package.json` with
`build` and `start` scripts; adjust it to the repository rather than assuming it fits:

```json
{
  "schemaVersion": 1,
  "build": {"runtime": "bun", "packages": ["."], "buildScript": "build", "startScript": "start"},
  "runtime": {"port": 3000, "healthPath": "/health"},
  "storageBindings": []
}
```

Use `node` instead of `bun` for Node, and `null` for `buildScript` when no build
script is needed. Set a non-negative `CONFIGURATION_REVISION` (for example, `1`
for a new configuration). Increment it for an intentional configuration change.

Provision required storage before deployment using
`POST /v1/applications/{id}/storage`, body `{"type":"mongo","name":"default"}`
(or type `postgres`/`s3`), with its own persisted request/key. Poll its operation,
then list application storage and record the resource UUID. Bind the resource in
`storageBindings`, for example
`{"resourceId":"RESOURCE_UUID","outputs":{"uri":"MONGODB_URI"}}` for Mongo.
Use the supported output names in
[`storage_contract.py`](../openstack_platform/controller/storage_contract.py).
Never copy generated credentials into configuration JSON or environment overrides.
All active managed resources must have deployment bindings.

For user-owned runtime values, the project API supports
`PUT /v1/applications/{id}/environment/{KEY}` with `{"value":"..."}`, or
`POST /v1/applications/{id}/environment/import` with `{"dotenv":"..."}`.
Read values from protected files into request JSON, send via stdin, and persist
separate keys. Environment reads return metadata/key names, not secret values.
**Changing environment values is a separate operation and may restart the current
application.** This deployment flow preserves existing values; it does not stage
secret changes for an atomic configuration cutover.

If a primary static IP is required, use the
[fixed-IP plan/reservation procedure](OPERATIONS.md#retain-a-worker-primary-fixed-ipv4)
**while the old app remains enabled**. Poll reservation success and verify the
address/port ownership before deploying. On subsequent deployments, reuse that
reservation; do not release it or allocate another port. Availability outside an
automatic allocation pool is proven only by successful reservation.

## Update code on the existing worker

An operator can opt into `workerStrategy: "reuse"` for an enabled application
whose dedicated worker is healthy, uses the selected worker role image, and
has capacity for the existing allocation. This applies to both Node and Bun.
It preserves the worker UUID, port, primary address, flavor, and CPU/RAM
allocation. There is no automatic fallback to replacing infrastructure.

Require matching controller/helper releases and the `worker-reuse-v1`
capability. The default remains worker replacement. Reuse requires
`maintenance: true` and **must not include a sizing `plan`**. Use the next
section for first deployment, resizing, role-image upgrades, or a different
primary-port reservation.

The old application serves during the build and post-build preflight. The
controller then stops the exact old job, waits for client-reported task exit,
checkpoints that evidence, and removes its Nomad job without deleting the VM
or detaching its port. The new job starts directly on the public route; there
is no preview/promotion restart. Downtime includes image pull, application
startup, and health checks. This first reuse path does not pre-pull the image
while the old process serves, and does not promise a fixed deployment duration.

Use the transport functions, private `RUN` directory, accepted baseline, and
configuration prepared above. Choose this submission **instead of** the
replacement submission in the next section:

```bash
jq -e '.features | index("worker-reuse-v1") != null' capabilities.json >/dev/null
REPOSITORY=https://github.com/your-org/your-app
COMMIT=your-full-40-character-commit
REQUESTED_REF=main
jq -n --arg repository "$REPOSITORY" --arg commit "$COMMIT" \
  --arg ref "$REQUESTED_REF" --argjson revision "$CONFIGURATION_REVISION" \
  --slurpfile config configuration.json \
  '{repository:$repository,commit:$commit,requestedRef:$ref,
    configurationRevision:$revision,configuration:$config[0],
    maintenance:true,workerStrategy:"reuse"}' > deployment.json
python3 -c 'import uuid; print(uuid.uuid4())' > deployment-key.txt
admin -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(<deployment-key.txt)" --data-binary @- \
  "$BASE/deployments" < deployment.json > submitted.json
STATUS_URL=$(jq -er .statusUrl submitted.json)
```

Continue with [Poll and verify acceptance](#poll-and-verify-acceptance). Check
that the accepted commit changed and sizing stayed unchanged. For a retained
primary port, compare the original `portId` and `serverId` with `GET
"$BASE/fixed-ip"` after acceptance; both must be unchanged.

- An incompatible initial worker is rejected before building or stopping it.
  Review a replacement deployment with a new request/key if needed.
- A build failure or post-build drift does not initiate cutover. Unknown
  outcomes remain `recovery_required`; retry the identical request/key.
- An uncertain process stop blocks the replacement. Do not manually purge its
  job: that destroys the evidence needed to verify client exit on retry.
- After a terminal candidate health failure with confirmed cleanup, the old
  deployment remains accepted, the application is stopped, and the VM/port
  remain. Project `POST /v1/applications/{id}/enable` with `{}` and a new key
  restores the accepted artifact on that worker. Explicit disable/delete can
  reclaim the retained worker instead. No database or storage rollback occurs.

Finish or reconcile outstanding operations before rolling back platform
executables; older controllers do not implement this cutover/recovery policy.

## Plan and submit one immutable deployment

Set the repository, exact reviewed commit, branch label, and target flavor.
Use the existing flavor when retaining sizing; `4200` is this environment's
4-vCPU/16-GiB flavor, not a universal OpenStack ID. A plan does not reserve quota.

```bash
REPOSITORY=https://github.com/your-org/your-app
COMMIT=your-full-40-character-commit
REQUESTED_REF=main
FLAVOR=4200
admin --get --data-urlencode "flavor=$FLAVOR" "$BASE/resize-plan" > plan.json
jq '{applicationId,deploymentId,current,flavor,reserve,activation}' plan.json
```

Review the size and approve the eventual cutover downtime. **Do not disable the
application.** The controller performs the stop after build/preflight success.

```bash
jq -n --arg repository "$REPOSITORY" --arg commit "$COMMIT" \
  --arg ref "$REQUESTED_REF" --argjson revision "$CONFIGURATION_REVISION" \
  --slurpfile config configuration.json --slurpfile plan plan.json \
  '{repository:$repository,commit:$commit,requestedRef:$ref,
    configurationRevision:$revision,configuration:$config[0],
    plan:$plan[0],maintenance:true}' > deployment.json
python3 -c 'import uuid; print(uuid.uuid4())' > deployment-key.txt

admin -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(<deployment-key.txt)" --data-binary @- \
  "$BASE/deployments" < deployment.json > submitted.json
STATUS_URL=$(jq -er .statusUrl submitted.json)
```

`maintenance` is optional and defaults to false. Only the privileged deployment
route accepts it. Ordinary rolling deployment is appropriate only when the app
supports concurrent versions/processes and the network arrangement allows overlap.
Retained-primary-port deployments require maintenance or an already-disabled app.
No endpoint implicitly infers single-process safety from an application name.

## Poll and verify acceptance

`202 Accepted` is admission, **not successful deployment**. Preserve `RUN` if the
connection closes. Poll the returned URL, including during long builds:

```bash
case "$STATUS_URL" in /v1/admin/operations/*) ;; *) exit 1 ;; esac
while :; do
  admin "http://localhost$STATUS_URL" > operation.json || break
  jq '{operationId,status,phase,safeError,cleanupState}' operation.json
  state=$(jq -er .status operation.json)
  case "$state" in
    succeeded) break ;;
    failed|recovery_required) break ;;
    running) sleep 5 ;;
    *) echo 'Unrecognized operation state; inspect before proceeding' >&2; break ;;
  esac
done
jq -es 'length == 1 and .[0].status == "succeeded"' operation.json >/dev/null
```

A resumed operation reports `running` and clears its old error while recovery is
active. A fresh `recovery_required` result means inspect the dependency and retry
only the exact original POST/key. Do not blindly loop mutations.

After success, verify the accepted pointer and public application, not just the
operation status:

```bash
OPERATION_ID=$(jq -er .operationId operation.json)
project "$PROJECT_BASE" > after-app.json
project "http://localhost/v1/deployments/$OPERATION_ID" > accepted-deployment.json
project "$PROJECT_BASE/storage" > after-storage.json
jq -e --arg id "$OPERATION_ID" \
  '.desiredRunning == true and .activeDeploymentId == $id' after-app.json >/dev/null
jq -e --arg commit "$COMMIT" \
  '.status == "succeeded" and .repositoryCommit == $commit' accepted-deployment.json >/dev/null
URL=$(jq -er .url after-app.json)
HEALTH_PATH=$(jq -er .runtime.healthPath configuration.json)
curl --fail --silent --show-error "$URL$HEALTH_PATH"
```

Verify homepage/representative application behavior separately. Compare the accepted
configuration SHA, sizing, storage resource identities and live observations with
the reviewed request/baseline. If reserved, `GET "$BASE/fixed-ip"` must report the
same address/port attached to the accepted worker. Check global platform health,
cleanup state, backups, and a fresh managed restore verification as appropriate.
Worker-local files are disposable; managed data is not copied between workers.

## Diagnose, resume, or restore the previous code

- **Build/preflight failure:** the old app remains serving if cutover has not
  started. Inspect `phase` and app state; do not stop it to unblock a build.
- **Failure after predecessor stop:** downtime continues until recovery or an
  explicit restoration completes. There is no promise of automatic code/data
  rollback. Keep the old deployment ID and compatible retained artifact.
- **Unknown outcome or `recovery_required`:** preserve request bytes and key;
  restore the dependency, then repeat the submission command above. A changed
  request/key must not bypass the blocked application scope. Recovery reuses
  retained build/worker evidence when available and rechecks candidate health.
- **`failed` with confirmed cleanup:** the operation is terminal. Review a new
  plan/key for a new attempt, or restore the accepted application with project
  `POST /v1/applications/{id}/enable`, body `{}`, its own key, then poll.
- **Lost response:** repeating the same POST/key returns or resumes the existing
  operation. A changed body with that key is a conflict, not a new deployment.

Build logs: project `GET /v1/deployments/{operationId}/build-log?lines=100`.
Runtime logs: project `GET /v1/applications/{id}/runtime-log?lines=100`.
Capture them privately; application logs can contain sensitive values. There is
no `GET /v1/admin/deployments/{id}` route. Use the project read above, or filter
the paginated `GET /v1/admin/deployments` list.

To roll back an **already accepted** deployment for a single-process app:
resolve any unfinished operation first; confirm database compatibility; disable
through the project API and poll success; then fetch a **fresh** privileged
`GET "$BASE/rollback-plan?deploymentId=$PREVIOUS"`. Submit
`{"plan": PLAN_OBJECT, "confirmation": "EXACT_SLUG"}` to `POST "$BASE/rollback"`
with a new persisted key and poll its operation. This uses a retained OCI digest,
not a rebuild of a moving branch, and does not restore database contents.

Do not repair a deployment with SQL edits, manual Nova creation, manual port
attachment, edits to an accepted release, or blanket Nomad garbage collection.
Use controller-managed recovery. Escalate an unresolved ownership/provider
ambiguity rather than deleting a resource whose identity is not proven.
