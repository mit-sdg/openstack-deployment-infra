# Hosts and images

Use this guide to select role images, replace persistent hosts, and prune old images on a running platform. For building, signing, and publishing, see [Releases and upgrades](releases-and-upgrades.md).

## Before you start

Run commands as the unprivileged owner of `/srv/openstack-platform` on the operator host. Have release evidence and published Glance image IDs ready, then set:

```bash
export PLATFORM_CLI=/srv/openstack-platform/bin/openstack-platform
export PLATFORM_CONFIG=/srv/openstack-platform/config/platform.json
export SSH_CONFIG=/srv/openstack-platform/.secrets/ssh/config
export PLATFORM_NAMESPACE="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["namespace"])')"
export PLATFORM_DOMAIN="$(/srv/openstack-platform/runtime/python3.14 -c \
  'import json,os; print(json.load(open(os.environ["PLATFORM_CONFIG"]))["domain"])')"
```

## How images work

### One image per role

Each machine boots a NixOS QCOW2 image built from one Git commit:

| Role | Lifetime | How updates reach it |
| --- | --- | --- |
| `admin`, `ingress`, `storage` | Persistent | `infra replace <role>` |
| `worker` | One per app | Deployment, resize, or fresh enable creating a worker |
| `builder` | Single use | Next build |

Images contain packages, users, services, firewall rules, mounts, and non-secret inventory at `/etc/<namespace>/platform.json`. Credentials, keys, tokens, passwords, and private certificates arrive on a first-boot config drive; see [the templates](../../infra/cloud-init-nixos/). Running hosts aren't upgraded in place.

Setup names images `<prefix>-nixos-<role>-<commit8>`. Glance metadata records role, commit, namespace, prefix, project, and artifact hashes. Mismatched deployment or role metadata is refused.

### Candidates and accepted images

Builds, VM tests, and uploads produce **candidates**. Acceptance requires live role tests on OpenStack and selection by exact Glance ID: local tests cannot prove Nova scheduling, Neutron rules, provider routing, or cross-role behavior. Never select by name, rename a candidate to promote it, or rebuild without the commit suffix.

| Role | Required live evidence |
| --- | --- |
| All | Exact project/image ID, config-drive completion, required units healthy, expected firewall/metadata behavior, all disposable resources deleted |
| `builder` | Rootless BuildKit ready, pinned SSH from admin, metadata blocked, OCI build/push/delete, expiry timer, server/port deletion |
| `worker` | Nomad/Docker identity, no SSH, constrained allocation, metadata blocked, privileged containers refused, ingress-only access, digest-pinned registry pull, reboot recovery, job/server/port deletion |
| Persistent roles | Services/readiness in a disposable scope; [live acceptance](releases-and-upgrades.md#run-disposable-live-acceptance) exercises replacement |

### Two selection records

Keep these independent records in step:

| Record | Changed through | Used for |
| --- | --- | --- |
| Operator state | `openstack-platform infra image set` | `infra list`, persistent replacement, prune protection |
| Hosted controller | Image selection API | New workers/builders; seeded from a file at admin boot |

Neither updates the other or changes a running machine.

## See which images are selected

1. Read operator selections and live hosts:

   ```bash
   $PLATFORM_CLI infra list
   ```

   Columns are `ROLE`, `IMAGE`, `COMMIT`, `LIVE`. Persistent roles show Nova state (`active`, `stopped`, `building`, `error`, `missing`); workers show `unknown`, as do builders without a running build.

2. List platform-metadata images, newest first:

   ```bash
   $PLATFORM_CLI infra image list
   ```

   Columns are `ROLE`, `NAME`, `UUID`, `STATUS`, `COMMIT`.

3. Read hosted selections:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- curl --fail-with-body --silent --show-error \
     --unix-socket "/run/$PLATFORM_NAMESPACE-controller/privileged.sock" \
     http://localhost/v1/admin/images
   ```

   `items` has one entry per role: `role`, `imageId`, `displayName`, `sourceCommit`, `compatibilityHash`, `selectedAt`.

## Roll out new role images

For a release changing all five images:

1. Confirm publication and live tests; record exact IDs.
2. [Install the matching operator release](releases-and-upgrades.md#install-operator-and-helper).
3. Run the new admin image's [root-path preflight](#preflight-controller-paths-before-an-admin-image-upgrade).
4. [Replace admin](#replace-the-admin-host), including matching helper, all five hosted selections, and seed refresh.
5. [Replace storage](#replace-the-storage-host).
6. [Replace ingress](#replace-the-ingress-host).
7. Record worker/builder selections in operator state for listing and prune protection:

   ```bash
   $PLATFORM_CLI infra image set worker <NEW_WORKER_IMAGE_UUID>
   $PLATFORM_CLI infra image set builder <NEW_BUILDER_IMAGE_UUID>
   ```

Existing workers retain their boot image. Replacement deployments, resizes, and fresh enables use the new selection; worker reuse keeps the existing OS.

## Update hosted role image selections

Change one role at a time, supplying the expected current ID so concurrent changes are refused. Only worker/builder selections affect provisioning. Persistent-role selections are seed metadata; changing them doesn't replace hosts.

1. Open an operator shell on admin and set its namespace:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin
   ```

   ```bash
   PLATFORM_NAMESPACE=<namespace>
   SOCKET="/run/${PLATFORM_NAMESPACE}-controller/privileged.sock"
   ```

2. Note the role's current ID:

   ```bash
   curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
     http://localhost/v1/admin/images
   ```

3. Replace both placeholders with reviewed IDs from publication evidence and step 2, then submit with a fresh request ID:

   ```bash
   REQUEST_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
   echo "$REQUEST_ID"
   curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
     -H 'Content-Type: application/json' -H "Idempotency-Key: $REQUEST_ID" \
     --data '{"imageId":"<NEW_WORKER_IMAGE_UUID>","expectedImageId":"<CURRENT_WORKER_IMAGE_UUID>"}' \
     http://localhost/v1/admin/images/worker/selection
   ```

   `202` means admitted, not changed. Save the ID.

4. Poll until `status: succeeded`:

   ```bash
   curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
     "http://localhost/v1/admin/operations/$REQUEST_ID"
   ```

5. Read selections again and confirm the new ID.
6. Repeat for other roles, changing the route (for example `/v1/admin/images/builder/selection`), each with its current ID and a fresh request ID.

A `failed` operation changed nothing: re-read and review the expected ID and provider image, then submit a new key. For `recovery_required` or a lost response, replay the identical body and ID. Recovery rechecks the image and reconciles possibly committed writes; don't edit SQLite.

<details>
<summary>How selections interact with deployments in progress</summary>

Deployments record builder/worker IDs before building; resizes record worker IDs before provisioning. Recovery retains those IDs. Work not yet recording images uses the selection at that point. A fresh enable chooses the current selection; an interrupted enable retains its recorded ID. Keep images needed by recorded operations and running workers.

Selection validation holds the infrastructure lock and can take minutes. Deployments wait up to 10 minutes within their deadline; other API requests still work. Before resource creation, a lock timeout gives `failed`, phase `platform_busy`, code `PLATFORM_BUSY`. No recovery is needed; retry later, as the portal prompts.

</details>

## Replace a persistent host

Replace `admin`, `ingress`, or `storage` to use its selected image and inventory flavor while retaining ports and data volumes.

### What a replacement does

1. Checks the operator-selected image, resolves `flavors.<role>`, and checks the fixed port and volumes.
2. Stops and renames the old server, retaining it.
3. Transfers port and volumes to a server using the selected image and configured flavor, with fresh first-boot data.
4. Checks console readiness, role health, and exact image/flavor/name/provenance.
5. Deletes the old server only after acceptance.

Failed checks delete the candidate, return resources, and restart the old host. Services are unavailable during replacement and rollback.

> [!WARNING]
> Never manually delete the old/current server or detach volumes to make room. Replacement needs them for rollback.

### Before you replace

- Have the published, live-tested image ID.
- For storage, take a fresh managed-data backup and restore check producing `RESTORE-MANIFEST`.
- For admin, take fresh controller/operator-state backups and a portal broker backup if installed; run [preflight](#preflight-controller-paths-before-an-admin-image-upgrade).
- Finish other infrastructure operations; unfinished work blocks replacement.
- Export protected first-boot files; the CLI reads them before touching the old host.

See [Backups and recovery](backups-and-recovery.md) for commands.

| Role | Variable | Automated setup path |
| --- | --- | --- |
| all | `OPERATOR_PUBLIC_KEY` | `/srv/openstack-platform/.secrets/ssh/id_ed25519.pub` |
| all | `PKI_DIR` | `/srv/openstack-platform/.secrets/setup/pki` |
| `admin` | `ADMIN_SECRETS_FILE` | `/srv/openstack-platform/.secrets/setup/admin-bootstrap.env` |
| `ingress` | `NOMAD_TOKENS_FILE` | `/srv/openstack-platform/.secrets/setup/nomad-tokens.env` |
| `storage` | `STORAGE_SECRETS_FILE` | `/srv/openstack-platform/.secrets/setup/storage-bootstrap.env` |

Every fresh [ingress replacement](#replace-the-ingress-host) also requires a Cloudflare connector token file.

### Resize a persistent host

1. Edit `flavors.<role>` in the operator inventory, `/srv/openstack-platform/config/platform.json`, to the desired provider flavor name. Keep the file operator-owned and mode `0600`.
2. Follow the replacement procedure below for that role, including its backups and protected first-boot inputs, then run `infra replace <role>`. You can keep the current selected image; resizing doesn't require a new image. Ingress still requires `--cloudflare-tunnel-token-file`.
3. Confirm the flavor change in the prompt. On success, the JSON observation includes `"flavor": {"from": "<old flavor>", "to": "<new flavor>"}`. Check the role's health as described below.

Downtime is the same as for any replacement. The CLI validates the target flavor before stopping the old host. The old server keeps its original flavor for rollback; allow enough quota for both servers until acceptance. If creation is refused and the provider confirms no candidate exists, replacement returns the ports and volumes and restarts the old host.

### Replace the storage host

1. Export inputs:

   ```bash
   export OPERATOR_PUBLIC_KEY=/srv/openstack-platform/.secrets/ssh/id_ed25519.pub
   export STORAGE_SECRETS_FILE=/srv/openstack-platform/.secrets/setup/storage-bootstrap.env
   export PKI_DIR=/srv/openstack-platform/.secrets/setup/pki
   ```

2. Select the tested image:

   ```bash
   $PLATFORM_CLI infra image set storage <NEW_STORAGE_IMAGE_UUID>
   ```

   Expect `selected role=storage image=<NEW_STORAGE_IMAGE_UUID>`; mismatched metadata is refused.

3. Replace:

   ```bash
   $PLATFORM_CLI infra replace storage --yes
   ```

   Success prints a JSON observation with `kind: persistent-host-replacement-observation`, old/new server and selected image IDs, `oldHostRetainedUntilReady: true`, and `exactIdentityVerified: true`.

4. Verify:

   ```bash
   $PLATFORM_CLI infra logs storage --lines 200
   $PLATFORM_CLI infra list
   ```

   Look for `<namespace> NixOS storage services ready` on the console and `storage` active on the new image.

`replacement readiness failed; retained host was restored` means the old host serves again. Inspect logs, fix the cause, and start a fresh replacement.

### Replace the admin host

Controller code is baked into admin's image; operator/helper releases alone don't update it. Use this sequence for every admin image change, keeping namespace, project, prefix, and certificate identity unchanged.

Commit-specific image names are baked into inventory. Admin seeds hosted selections from `<adminState>/operator/image-selections.json` on its retained volume. Seeding preserves records and refuses unexplained differences. Keep the old seed until selections move so interrupted steps and automatic rollback can boot.

1. Pause app changes; finish in-flight operations or record them and retain their images. Take [backups](#before-you-replace). Protect a copy of the unchanged seed; all five records must match hosted selections. Resolve unexplained mismatches without database edits.
2. Run the candidate's [preflight](#preflight-controller-paths-before-an-admin-image-upgrade); require `root-path-preflight=ok refusals=0`.
3. Replace from the operator host:

   ```bash
   export OPERATOR_PUBLIC_KEY=/srv/openstack-platform/.secrets/ssh/id_ed25519.pub
   export ADMIN_SECRETS_FILE=/srv/openstack-platform/.secrets/setup/admin-bootstrap.env
   export PKI_DIR=/srv/openstack-platform/.secrets/setup/pki
   $PLATFORM_CLI infra image set admin <NEW_ADMIN_IMAGE_UUID>
   $PLATFORM_CLI infra replace admin --yes
   $PLATFORM_CLI infra list
   ```

   Admin boots against the unchanged seed/database. Replacement checks Nomad, not the full controller; keep app changes paused.

4. Check controller/readiness:

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
     "$PLATFORM_NAMESPACE-controller.service" \
     "$PLATFORM_NAMESPACE-controller-readiness.service"
   ```

   Expect `active` twice.

5. [Install the same commit's helper release](releases-and-upgrades.md#install-operator-and-helper). It lives on the retained volume, so image replacement doesn't update it.
6. [Move all five hosted selections](#update-hosted-role-image-selections) using reviewed new/current IDs. Require `succeeded` for each. Ingress/storage aren't replaced by this step. Completed changes provide seed evidence, so the old seed still works if interrupted.
7. Refresh the seed as the operator on admin, in step 6's shell. The script verifies all roles against baked image names before replacing the file:

   ```bash
   (
     set -euo pipefail
     umask 077
     PLATFORM_ADMIN_STATE="$(jq -r .paths.adminState "/etc/${PLATFORM_NAMESPACE}/platform.json")"
     SOCKET="/run/${PLATFORM_NAMESPACE}-controller/privileged.sock"
     seed="$PLATFORM_ADMIN_STATE/operator/image-selections.json"
     test -f "$seed" && test ! -L "$seed"
     test "$(stat -c '%u:%a' "$seed")" = "$(id -u):600"
     temporary="$(mktemp "${seed}.XXXXXX")"
     trap 'rm -f -- "$temporary"' EXIT
     curl --fail-with-body --silent --show-error --unix-socket "$SOCKET" \
       http://localhost/v1/admin/images |
       jq --exit-status --slurpfile guest "/etc/${PLATFORM_NAMESPACE}/platform.json" '
         if (.items | map(.role) | sort) != (["admin","ingress","storage","worker","builder"] | sort)
           or any(.items[]; .displayName != $guest[0].images[.role])
         then error("selected roles do not match the baked inventory")
         else {schemaVersion: 1, projectId: $guest[0].projectId, namespace: $guest[0].namespace,
           images: (.items | map({key: .role, value: {imageId, displayName, sourceCommit, compatibilityHash}}) | from_entries)}
         end' > "$temporary"
     sync -f "$temporary"
     mv -T "$temporary" "$seed"
     sync -f "$seed"
   )
   ```

   `selected roles do not match the baked inventory` leaves the seed unchanged. Correct the hosted selection first.

8. Open a [root session](#run-a-root-step-on-the-admin-host), set `PLATFORM_NAMESPACE=<namespace>`, and restart. Don't run the seed program against SQLite by hand.

   ```bash
   sudo systemctl restart "$PLATFORM_NAMESPACE-controller.service"
   sudo systemctl restart "$PLATFORM_NAMESPACE-controller-readiness.service"
   sudo systemctl show -p Result "$PLATFORM_NAMESPACE-controller-prepare.service"
   sudo systemctl is-active "$PLATFORM_NAMESPACE-controller.service" \
     "$PLATFORM_NAMESPACE-controller-readiness.service"
   ```

   The prepare unit runs once and exits: require `Result=success`, then both services `active`.

9. Re-read hosted selections; confirm `infra list` shows admin's new image, then resume app changes. Operator state remains separate.

The controller database is retained. If a premature seed refresh blocks startup, restore the protected seed file, not another database.

> [!IMPORTANT]
> Host rollback doesn't downgrade schemas. If the new controller migrated the database, the old controller refuses it. Don't force-restore or delete migration rows; see [Database migrations](releases-and-upgrades.md#database-migrations).

### Replace the ingress host

Every fresh replacement needs the current Cloudflare connector token file. The CLI cannot recover it from the old host, prompt for it, or retain it. Retrieve it through your credential custody; if inaccessible, stop rather than bypassing SSH host-key checks.

Require an operator-owned regular file, mode `0600`, one link, at most 16 KiB. Contents must be the base64 connector token (at most 8192 characters), optionally one trailing newline; `TUNNEL_TOKEN=...` files and API tokens are invalid.

Local validation checks protection and structure, not Cloudflare validity or DNS. Confirm tunnel/domain in Cloudflare; revoked tokens can pass locally. Readiness must pass before deleting the old host. Replacement always enables the connector, regardless of `ENABLE_CLOUDFLARED=false` or `CLOUDFLARE_TUNNEL_TOKEN_FILE`.

1. Export inputs and select the tested image:

   ```bash
   export OPERATOR_PUBLIC_KEY=/srv/openstack-platform/.secrets/ssh/id_ed25519.pub
   export NOMAD_TOKENS_FILE=/srv/openstack-platform/.secrets/setup/nomad-tokens.env
   export PKI_DIR=/srv/openstack-platform/.secrets/setup/pki
   $PLATFORM_CLI infra image set ingress <NEW_INGRESS_IMAGE_UUID>
   ```

2. Pass the token path:

   ```bash
   $PLATFORM_CLI infra replace ingress --yes \
     --cloudflare-tunnel-token-file <PATH_TO_CONNECTOR_TOKEN_FILE>
   ```

   Missing/malformed files fail before stopping the old host.

3. Check console and public health:

   ```bash
   $PLATFORM_CLI infra logs ingress --lines 200
   test "$(curl --fail --show-error --silent "https://$PLATFORM_DOMAIN/healthz")" = OK
   ```

The CLI leaves your file unchanged, reads it once into first-boot data, and removes private temporary copies on completion/failure. Forced termination can leave copies; clean up as protected material. State, operation records, and logs don't store the token. Rotation must leave the retained old host able to serve during rollback; its credentials aren't updated.

### Retry an interrupted replacement

An interrupted or uncertain replacement records recovery state and blocks other infrastructure work. Fix provider/dependency access, then retry with the same inventory and state directory:

```bash
$PLATFORM_CLI infra replace <ROLE> --yes
```

| Interruption point | Retry result |
| --- | --- |
| Before acceptance | Restores old host, prints `Retained old host restored; start a fresh replacement separately.`, stops; start a separate replacement afterward |
| After acceptance | Rechecks exact provenance, fixed resources, readiness, and ingress public health; deletes old server. Also handles `replacement accepted but retained old-server cleanup remains` |
| Before any observation | Starts fresh |

Recovery uses the target flavor ID recorded when replacement began, even if the inventory flavor later changes. An ingress recovery doesn't read tokens or rerender data. Passing the token flag prints `Recovering recorded replacement; token file is not read.`; it cannot change an existing candidate's credentials. Passing the path on every attempt is safe; only fresh attempts read it.

Ingress public `https://<domain>/healthz` gets up to 120 seconds, no redirects, a 2xx response, and body `OK` after trimming whitespace. Tunnel HTTP listens on loopback; direct HTTP admits only provider addresses, so recovery uses public HTTPS.

## Preflight controller paths before an admin image upgrade

Run this read-only check before every admin image replacement, using the candidate image's rules against live metadata. Admin's root prepare service checks paths without following links and repairs metadata through open handles; unacceptable files prevent controller startup. Building/evaluating an image doesn't prove live paths pass.

Root is required to traverse private paths. Preflight reads no contents, changes no metadata, copies nothing, and doesn't start/stop units. Don't grant operator sudo or loosen modes.

1. On the build machine at the exact candidate commit, set `PLATFORM_CONFIG` to its inventory and build:

   ```bash
   nix build --impure .#root-path-preflight --out-link .tmp/root-path-preflight
   ls .tmp/root-path-preflight/share/root-path-preflight/
   ```

   Expect `host_paths.py` and `controller-path-plan.json`; review paths/account IDs.

2. Copy both to admin; as root place them in a root-controlled directory such as `/run/root-path-review`, using the [approved administration route](#run-a-root-step-on-the-admin-host).
3. Run as root:

   ```bash
   python3.14 -I -B /run/root-path-review/host_paths.py preflight \
     --plan /run/root-path-review/controller-path-plan.json
   ```

   Require exit 0 and `root-path-preflight=ok refusals=0`; retain the report with your change review.

Refusals show expected/observed `uid:gid:mode:nlink`, type, and size, or the missing/unreachable path. Defer replacement, repair only those paths through administration, and rerun the same plan. Don't recursively `chown`/`chmod` the volume.

Preflight excludes JSON contents, TLS trust, migrations, provider state, and helper compatibility. Boot preparation rechecks the same plan. Installed `openstack-platform-root-path-preflight` uses the current image's plan, not the candidate's.

<details>
<summary>What the plan requires, and why</summary>

| Path | Requirement |
| --- | --- |
| Ancestors | Direct directories owned by root or relevant account; no followed links |
| Policy/seed source | Regular operator-owned `0600`, at most 1 MiB; any group/link count since only read |
| Controller copy directory | Controller-owned, no group/other write; other bits unrestricted |
| Existing copy destination | Absent or non-directory, including links; replaced without opening/modifying old entry, so metadata doesn't matter |
| Private credentials/directories | Operator-owned `0600`/`0640` or `0700`/`0750`; readable groups limited to operator/controller. Regular files whose metadata changes require one link to avoid changing outside aliases |
| Public certificates/`.pub` | Direct regular operator-owned `0644`; any group/link count |

</details>

## Run a root step on the admin host

Preflight and controller restarts need root; `agentops` has no sudo. Admin's approval-gated `ubuntu` recovery account has passwordless sudo. OpenStack installs its key from the admin keypair; setup retains the private key below. Use it only for approved root steps through the pinned alias:

```bash
ssh -F "$SSH_CONFIG" -o User=ubuntu \
  -o IdentityFile=/srv/openstack-platform/.secrets/setup/admin_nova_rsa \
  -o IdentitiesOnly=yes platform-admin
```

## Storage binding names on older admin images

Before commit `0cbe47a`, controller fingerprint validation rejected PostgreSQL `password` and S3 `secret_access_key` bindings with HTTP 400 before admission. Current portal releases bind every output, including `secret_access_key` to `AWS_SECRET_ACCESS_KEY`, and need a newer controller.

1. Replace admin with a newer image through the approved procedure; merging/building isn't deployment.
2. Install the portal release afterward.

Fingerprints are unchanged: retry admitted/in-flight operations with their original body and ID. Pre-admission rejection has nothing to resume; resubmit, or start a new portal deployment. Never change configuration under the old key. PostgreSQL `DATABASE_URL` already includes the password.

## Prune old images

Prune Glance images through a reviewed plan and a rechecked apply.

1. Plan:

   ```bash
   $PLATFORM_CLI infra image prune
   ```

   Expect `DELETE UUID` entries and `plan=<hash> expires=<UTC time> review=<count>`. Expiry is 15 minutes; deletion limit is 100 images. Operator selections, project server images, unfinished operator-operation images, and the two newest valid images per role are protected. Mismatched platform metadata goes to `review`, never deletion.

2. Compare deletions with [hosted selections](#see-which-images-are-selected) and recorded deployment/resize needs; only operator selections are automatically protected. If any appear, update operator state and replan before applying.
3. Apply:

   ```bash
   $PLATFORM_CLI infra image prune --apply --yes
   ```

   Under the infrastructure lock, the CLI re-reads images/protections. Drift in images, selections, servers, or expiry fails without deleting; replan. Success prints `deleted=<count> plan=<hash>`.

Interrupted apply resumes with the same apply command. Missing/malformed provider data stops pruning.

## Related

- [Releases and upgrades](releases-and-upgrades.md)
- [Backups and recovery](backups-and-recovery.md)
- [Troubleshooting](troubleshooting.md)
- [Operator CLI reference](../reference/operator-cli.md)
- [Controller API reference](../reference/controller-api.md)
- [Internals reference](../reference/internals.md)
