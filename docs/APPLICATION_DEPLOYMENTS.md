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
workers simultaneously. For routine code updates, [reuse the existing worker](#reuse-the-existing-worker-for-code-updates)
to avoid VM replacement while retaining its port and IP.

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

## Reuse the existing worker for code updates

Use this opt-in path for an application with an accepted deployment and a ready
worker, including an already-attached retained primary IPv4. It keeps the exact
server, port, worker image, flavor, and scheduler allocation. It does not migrate
an ordinary worker onto a newly reserved port. First deployment, resizing, and
worker image upgrades use the [replacement path below](#plan-and-submit-a-worker-replacement).

Install matching controller/helper releases through the supported admin-image
upgrade and take a fresh hosted-controller backup first. The controller must
advertise `reuse-worker-v1`; its helper must support `app.stop`. Workers need no
new SSH service or build tools. Do not downgrade the controller with unfinished
reuse operations or stopped applications whose workers are retained.

Builds still run on isolated builders while the accepted app serves. After
artifact/storage and exact worker/capacity checks, the controller confirms the
old Nomad allocations have stopped, removes the old job, and starts the new job
on the same worker. **There is downtime** for image pulling, process startup,
and health checks, but no worker deletion/boot wait. Review migration safety;
this mode does not permit concurrent application versions or roll back data.

Using the protected request directory, application variables, and configuration
prepared above:

```bash
jq -e '.features | index("reuse-worker-v1") != null' capabilities.json >/dev/null
REPOSITORY=https://github.com/your-org/your-app
COMMIT=your-full-40-character-commit
REQUESTED_REF=main
jq -n --arg repository "$REPOSITORY" --arg commit "$COMMIT" \
  --arg ref "$REQUESTED_REF" --argjson revision "$CONFIGURATION_REVISION" \
  --slurpfile config configuration.json \
  '{repository:$repository,commit:$commit,requestedRef:$ref,
    configurationRevision:$revision,configuration:$config[0],
    maintenance:true,reuseWorker:true}' > deployment.json
python3 -c 'import uuid; print(uuid.uuid4())' > deployment-key.txt
admin -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(<deployment-key.txt)" --data-binary @- \
  "$BASE/deployments" < deployment.json > submitted.json
STATUS_URL=$(jq -er .statusUrl submitted.json)
```

Do not include a sizing `plan`, even for the same flavor. Both flags must be
JSON booleans. Omitted `reuseWorker` preserves the existing replacement behavior;
reuse validation failure never falls back to creating a different worker.
Continue with [polling and verification](#poll-and-verify-acceptance), not the
replacement submission below. Compare the worker server/port UUIDs with the
baseline and verify the actual outbound source IP and required SMTP connectivity
from the accepted application. Code updates do not refresh the OS; schedule
worker replacement separately.

## Plan and submit a worker replacement

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
Retained-primary-port replacements require maintenance or an already-disabled app.
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
- **`failed` with confirmed cleanup:** the operation is terminal. For reuse
  failures after cutover, the candidate job is removed but the worker remains, the accepted pointer is
  unchanged, and the application is recorded stopped. Use the same-worker
  rollback below or submit a new reuse request/key. For replacement, review a
  new plan/key or restore the accepted application with project
  `POST /v1/applications/{id}/enable`, body `{}`, its own key, then poll.
- **Lost response:** repeating the same POST/key returns or resumes the existing
  operation. A changed body with that key is a conflict, not a new deployment.

Build logs: project `GET /v1/deployments/{operationId}/build-log?lines=100`.
Runtime logs: project `GET /v1/applications/{id}/runtime-log?lines=100`.
Capture them privately; application logs can contain sensitive values. There is
no `GET /v1/admin/deployments/{id}` route. Use the project read above, or filter
the paginated `GET /v1/admin/deployments` list.

### Roll back on the same worker

For a reuse deployment, **do not disable the application first**: explicit disable
still deletes the worker, including a worker retained after a failed deployment.
Resolve unfinished operations with their original request/key, confirm database
compatibility, then obtain a fresh plan. Set `PREVIOUS` to a complete successful
deployment for this application. After a failed cutover, it may be the still-
accepted deployment whose process is now stopped.

```bash
admin "$BASE/rollback-plan?deploymentId=$PREVIOUS&reuseWorker=true" > rollback-plan.json
jq -n --slurpfile plan rollback-plan.json --arg slug "$SLUG" \
  '{plan:$plan[0],confirmation:$slug}' > rollback.json
python3 -c 'import uuid; print(uuid.uuid4())' > rollback-key.txt
admin -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(<rollback-key.txt)" --data-binary @- \
  "$BASE/rollback" < rollback.json > rollback-submitted.json
STATUS_URL=$(jq -er .statusUrl rollback-submitted.json)
```

Poll with the same privileged polling procedure. Verify the accepted artifact,
application health, and unchanged server/port/IP. This uses the retained image
without building, keeps current secrets and sizing, and never restores database
contents. The reviewed plan's `reuseWorker: true` plus slug confirmation authorizes
the single-process downtime. Missing/drifted worker identity blocks reuse rather
than provisioning a replacement.

### Roll back by replacing the worker

To roll back an **already accepted** deployment for a single-process app using replacement:
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
