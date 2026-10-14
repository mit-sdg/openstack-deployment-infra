# OpenStack provider reads

OpenStack reads use a persistent Keystone session in the existing credential-holding account. Python callers use a local Unix socket directly; reviewed shell scripts use a small read launcher. Mutations retain their current credential wrappers and command runners. There is no CLI fallback for a migrated read.

This reference records the call-site survey, transport contract, and first-stage migration. Read [Internals](internals.md#processes-and-trust-boundaries) and [Security](../security.md#the-controller-and-the-cloud) before changing its authority.

## Credential custody

On admin, `nix/lib/controller-paths.nix` already makes `openstack.env` operator-owned and controller-group-readable (`0640`), and the controller runs with that group. The helper runs as its caller; it is not a separate privileged credential holder. The existing VM test explicitly checks that `platform-controller` can read this file and `nomad` cannot. The new provider runs as the existing operator, `agentops`. No account, credential-file permission, controller group membership, or portal authority changes.

The admin socket is `/run/<namespace>-provider/provider.sock`, owned by `agentops:controller-api`, mode `0660`, in an `agentops:controller-api`, mode-`0750` directory. The provider unit uses `controller-api` as its primary group so systemd preserves that traversal group when it manages the runtime directory. Before reading any bytes, the service checks Linux `SO_PEERCRED` for exactly:

| Caller | UID | Primary GID |
| --- | --- | --- |
| Operator tools and health service | Contract operator UID | Contract operator GID |
| Controller and its helper children | Contract controller UID | Contract controller-socket GID (`controller-api`) |

The broker also belongs to `controller-api`; its different UID is refused. Web, identity, Nomad, worker, and builder accounts receive no access. Python clients also verify the daemon UID. Systemd creates the runtime directory with the provider unit’s user/group; it does not authenticate to OpenStack.

On the operator host, the user service runs as the same account that owns `/srv/openstack-platform/.secrets/openstack.env`. Its socket is `STATE_DIRECTORY/run/provider.sock`, mode `0600`, with the user's exact UID:GID as its sole peer. Releases carry software only. Setup and CI publication keep a session for their process lifetime under their existing account; concurrent publisher children share a private temporary socket that is removed afterward.

## Call-site survey

The table describes the original calls and their retained application contracts. `OSC` denotes the credentialed command configured by a reviewed launcher. Application arguments cannot select these executables. The operator's protected wrapper historically accepted the operator's command arguments; the helper's action protocol and its Python/shell callers supplied fixed commands. The new socket independently restricts actions and their fields.

| Component and call sites | Original credentials / runner | Service and reads | Mutations remaining | Parsing, limits, and deadlines |
| --- | --- | --- | --- | --- |
| Operator `openstack.py`; `operator.py`, dashboard provider observations | Operator-owned `platform-openstack`; `_run` / `_json_command` through `runtime.run` | Keystone scoped project; Glance private images and metadata; Nova server/flavor inventory, detail, console and volume attachments; Neutron ports; Cinder volumes | Image delete; server power, rename, create/delete; Nova port and volume attach/detach | JSON columns and bounded console bytes; case-insensitive fields; UUID normalization only at provider output; default 1 MiB stdout / 256 KiB stderr. One context deadline spans nested calls: reads normally 30 s, power 600 s, replacement/recovery 900 s with 30–60 s call budgets. Exact project, names, IDs, image metadata, attachments, deletion flags and readiness remain checked in callers. |
| Controller `image_service.py`, `builder_settings.py`, `sizing.py`, `status.py`, `application_service.py`, `deployment_service.py`, `seed_images.py` | `platform-controller`; fixed Nix `PLATFORM_OPENSTACK_COMMAND` wrapper reading the existing group-readable file | Same provider projections, including fresh image-selection CAS and size validation | Indirect worker/builder lifecycle through helper; infrastructure operations retain existing journals | Public read caches retain their existing TTL/single-flight rules; mutation validation bypasses them. Existing 30 s read and whole-operation budgets remain in callers. Seed file validation remains offline. Provider failures remain unavailable observations, never evidence of absence. |
| Controller `fixed_ip.py` / `floating_ip.py` and their services | Same controller wrapper; deadline passed to `_run` / `_json_command` | Neutron networks, subnets, routers, ports, floating IPs, SGs/rules and quotas; Nova worker identity | Port create/delete; floating IP create/set/unset/delete | Typed JSON; exact project, reservation markers, metadata, SG membership, network/subnet/IP and attachment checks; journal intent precedes allocation. Remaining whole-operation deadline bounds each call. |
| Helper `production.py`, `application_runtime.py`, `worker_capacity.py` | Helper runs as controller or operator caller; `provider_command(platform, tool)` selects namespace-scoped absolute launchers | Worker capacity and identity; reads inside worker/builder create, observe and cleanup | Worker/builder creation and deletion | Closed helper protocol remains 1 MiB. Application provider output is normally 64 KiB with 256 KiB stderr; worker/build phases keep their existing deadlines and redacted errors. Nomad/registry/storage clients are unaffected. |
| `worker_lifecycle.sh`, `builder_lifecycle.sh`, `pin_ephemeral_host_key.sh` | Admin fixed worker/builder/key-pin launchers source `openstack.env`; fixed `OSC` | Scoped token project, exact-name server/port lookups, flavor, port and server detail, console | Neutron port create/delete; Nova create and `delete --wait` | Existing JSON verifier scripts, exact application/generation ownership, protected user-data, retained-port exclusions, polling attempts, helper process/output deadline remain. Each read launcher adds a 60 s cap. SSH host keys still come from bounded authenticated console evidence. |
| `apply_admin.sh`, `apply_storage.sh`, `apply_ingress.sh`, `persistent-host.sh` | Setup/operator credential wrapper; run as unprivileged operator | Token scope, images/flavors, servers, ports, Cinder volume type/size/status, Nova keypair and attachments, console | Volume/keypair/server creation | Existing fixed-resource verifiers, deterministic names, one fixed port, exact devices and `delete_on_termination=false` remain. Existing polling and caller process budgets remain; read calls additionally cap at 60 s. |
| `setup.py` preflight, reuse checks and image bootstrap | Setup file parsed as literals; credentials in setup process and protected generated wrapper | Keystone token project/name; Nova flavors, limits, quotas, servers and keypairs; Neutron networks/subnets/ports/SGs/rules/quotas; Cinder types/volumes/limits/quotas; Glance images and usage | Foundation and provisioning delegated to reviewed scripts; QCOW2 verification download uses a protected local streaming API; uploads remain CLI | Read-only check still creates no workspace or credential files. Setup now renders the same small JSON/value projections directly from its session; secret-bearing token output and the separate curl usage request are removed. Default setup subprocess timeout was 7200 s; reads now cap at 60 s, usage at 30 s. Quota 404 still requires its exact explicit acknowledgement. |
| `apply_foundation.py` | Existing SDK connection in the operator process | Neutron network/SG/rule/port observations | Reviewed foundation reconciliation | Already used `openstack.connect()` once per invocation. Its ownership checks and SDK security-group project alias patch remain. It was not a per-read CLI path. |
| `publish_nixos_image.sh`, `image_pipeline.py`, CI production/development publisher | Existing CI project credentials or operator wrapper | Scoped project, private image exact-name lookup and image checksum/size/provenance | Image create/upload/set | Existing all-role verification barrier, immutable artifact evidence and byte checks remain. Five concurrent production publisher children share one temporary read service. Uploads still use OSC. Source publication must use the provider launcher or `provider_local` scope. |
| `infra/monitor/check_platform.py` (`<namespace>-platform-health.service`) | Existing operator account; fixed `OPENSTACK` wrapper | Nova server inventory | None | Existing 45 s subprocess timeout and health projection; wrapper reads use the socket. Exception output contains only fixed provider errors. No new cloud calls run as root. |
| `setup_operator_bridge.py`, acceptance driver configured provider command | Existing protected operator wrapper | Token project, console host-key evidence; acceptance inventory/cleanup observations | Existing explicit acceptance operations | Bridge discards provider stderr and bounds commands. Acceptance retains its transcript, size/deadline and exact-scope checks. Configure its provider command to the installed `platform-openstack` frontend; acceptance is not run by this change. |
| Root preparation, restore/activation units; worker/builder/ingress/storage guest services | Root preparation only normalizes already-authorized paths; guest roles hold no OpenStack credentials | No direct OpenStack call | None | Preparation and seed validation stay offline. Privileged storage actions, database restore and systemd activation do not acquire cloud credentials. |

## Session and endpoint contract

`provider_api.py` uses minimal REST with `keystoneauth1`, which was already in the Nix OpenStack CLI closure and is now an explicit locked Python dependency. The UV lock previously contained neither the SDK nor Keystone authentication. The SDK remains for foundation reconciliation and the mutation CLI. Direct REST avoids SDK resource-name resolution, lazy fetches, logging and retries outside the caller's budget, while keeping supported authentication and expiry handling in Keystone's maintained plugin. See [Keystone sessions](https://docs.openstack.org/keystoneauth/latest/using-sessions.html).

A Keystone v3 auth URL is required. Password/v3password and applicationcredential/v3applicationcredential authentication are supported. The plugin keeps a project-scoped token in memory and refreshes before expiry. One bounded lock coordinates authentication and catalog selection. Eight exclusively leased HTTP sessions share the plugin; each has reusable connection pools and no shared mutable requests session. A revoked token permits one reauthentication and one repeat of the read. There are no other transport/status retries.

Catalog selection uses `OS_INTERFACE` and optional `OS_REGION_NAME`, requiring one unique endpoint URL. Duplicate records for the same URL are accepted; multiple different matching URLs fail closed. Nova and Cinder require versioned catalog endpoints; Glance and Neutron can append their fixed v2/v2.0 path. Nova requests pin 2.46 so server flavor IDs and extended volume evidence retain their shape. Glance custom properties are separated from standard attributes; Neutron ownership uses `tenant_id` only if `project_id` is absent, preserving explicit null and foreign ownership. See the [Nova API](https://docs.openstack.org/api-ref/compute/) and [Glance API](https://docs.openstack.org/api-ref/image/v2/).

TLS verification is required, with system CAs or `OS_CACERT`. Environment proxies and redirects are disabled. Auth, Nova, Neutron and Cinder URLs must be HTTPS. HTTP Glance requires `PLATFORM_ALLOW_HTTP_GLANCE=I_UNDERSTAND_GLANCE_CREDENTIALS_USE_HTTP` in the service's protected credential file or setup/CI environment, preserving the explicit setup acknowledgement. No caller can override transport policy through the socket.

Tokens, passwords and endpoint/error response bodies are never returned as errors or written to service logs. Auth/session state is discarded at process restart; the next read authenticates again. Credential rotation requires restarting the provider service. The daemon checks the credential file's identity, permissions and timestamps before each action; changes fail closed until restart, preventing reads with a stale credential/region snapshot. Pause mutations during credential rotation as before.

## REST endpoint inventory

Paths below are relative to the selected versioned service endpoint; authentication uses the configured Keystone v3 URL. Only console retrieval uses a service POST. Mutation URLs remain in the current CLI/SDK implementations.

| Service | Fixed read endpoints |
| --- | --- |
| Keystone v3 | `POST /auth/tokens` for a scoped token/catalog; subsequent project verification reads the cached access record. No token-export endpoint is exposed. |
| Nova | `GET /flavors/detail`, `/flavors/{id}`, `/servers/detail`, `/servers/{id}`, `/servers/{id}/os-volume_attachments`, `/os-keypairs`, `/os-keypairs/{name}`, `/limits`, `/os-quota-sets/{project}`, `/os-quota-sets/{project}/detail`; `POST /servers/{id}/action` with only `os-getConsoleOutput`. |
| Glance v2 | `GET /images` with fixed name/private visibility filters, `/images/{id}`, `/info/usage`; setup locally streams `/images/{id}/file` into its protected temporary file. |
| Neutron v2.0 | `GET /networks`, `/subnets`, `/routers`, `/ports`, `/floatingips`, `/security-groups`, `/security-group-rules` and each corresponding `/{id}` detail path; `/quotas/{project}` and `/quotas/{project}/details`. Only the allowlisted name/project/network/device/fixed-IP/SG filters are accepted. |
| Cinder v3 | `GET /volumes/detail`, `/volumes/{id}`, `/types`, `/types/{id}`, `/limits`, `/os-quota-sets/{project}`, `/os-quota-sets/{project}/detail`. |

## Socket and resource bounds

The line-delimited JSON request has exactly `version`, `action`, `params`, and `timeout`. Version is integer 1. `provider_protocol.py` is the fixed action/field allowlist: identity, image catalog/usage, console reads, volume attachments, quotas/limits, and show/list for the surveyed resources. There is no arbitrary request, mutation, upload, token-export or exec action.

There are at most sixteen admitted socket connections and eight active API sessions. Requests are at most 16 KiB; input must arrive within five seconds. Each request has an absolute monotonic deadline, capped at sixty seconds and including session admission, authentication, pages, response body and output. Existing shorter client and operation deadlines still apply. HTTP timeouts use the remaining budget. A connection watchdog interrupts trickled status/headers/body reads at the absolute deadline. DNS resolution uses at most eight outstanding jobs, with bounded caller waits; a late resolver cannot send an authenticated request. Compression is disabled to enforce decoded byte bounds. HTTP headers also use requests/urllib3's parser limits.

Authentication and each API response are limited to 1 MiB. Inventories have a 500-row limit and a 1 MiB cumulative JSON limit; pagination preserves the original filters and rebuilds only the marker at the selected endpoint. Provider next-page URLs are never followed with a token. Repeated IDs, malformed envelopes, ambiguous names, unknown actions and over-limit responses fail closed. Caller stdout limits (including 64 KiB helper projections) still apply after projection. These inventory and read-time caps tighten some previously subprocess-only bounds.

Glance inventory contains full metadata, eliminating the old eight-process image-show fan-out and its 32 KiB-per-detail fallback. Observation results remain ephemeral; token reuse does not cache provider resources or authorize adoption. All deterministic-name, ownership, CAS, idempotency, lifecycle and recovery checks remain in their existing modules. A service outage yields the existing redacted unavailable/failure result and never falls back to a CLI read.

## Rollout and rollback

This is a coordinated software rollout; no live deployment or acceptance was performed to prepare it.

1. Publish and obtain disposable live acceptance for the new admin image and matching helper/operator releases. CI must run the Nix package and admin VM checks. Review catalog service types/versions, region/interface and CA/HTTP-Glance configuration before selecting the image.
2. Pause changes, finish operations, and take the existing backups. On an existing operator installation, prepare the new mutation-wrapper artifact by copying the current protected `platform-openstack` wrapper to `platform-openstack-mutate`, preserving owner-only executable permissions. Review that it sources the existing credential file and invokes the pinned OSC. Fresh setup generates both artifacts itself.
3. Install the new operator release and its user units before issuing read-dependent replacement commands. The installer requires the protected mutation wrapper, creates the read and daemon launchers, reloads units, enables and restarts `openstack-platform-provider.service`. The socket is created before systemd receives `READY=1`; the first provider read authenticates on demand. Verify `infra list` and `infra image list` through the installed launcher.
4. Replace admin through the established retained-host procedure. Its new image includes the provider package/unit and read launchers; the root path plan and credential custody are unchanged. The controller orders startup after provider readiness, but retains independent lifetime so provider restarts do not destroy controller state. Install the matching helper on the retained volume. Verify provider socket metadata, controller readiness, status, image CAS and a disposable build/deploy before resuming changes.
5. CI production and development publication use their process-scoped provider automatically. Standalone source publication uses `uv run --frozen python -m openstack_platform.provider_local -- infra/openstack/publish_nixos_image.sh ...`; it requires the existing project environment and never exports a token.

Provider restart affects only in-flight reads. Requests can fail during restart; existing journals and unknown-outcome rules apply to enclosing operations. The service does not replay any mutation. Admin systemd restarts failures after two seconds, advertises readiness only after socket bind, bounds stop to five seconds, disables core dumps and capabilities, and restricts filesystem writes to its runtime directory. The operator unit also disables core dumps and privilege escalation.

Rollback uses the retained admin image and matching retained helper/operator release, with operations paused and the existing schema rollback rules. Restore the protected original mutation wrapper as the old release's `platform-openstack` artifact when downgrading the operator, and disable the new user provider unit. Do not restore SQLite to mask an unknown provider outcome. This PR makes no database migration and stores no tokens on disk.

## Mutation follow-up

Setup's existing-image verification streams Glance image bytes through the same local session into its already-open, owner-only temporary file, with a 16 GiB cap and 7200-second deadline, followed by the unchanged independent SHA-256 comparison. This binary path is available only to setup, which already holds credentials; the socket has no caller-selected file path.

No REST mutation implementation or dual read backend is included. Server/port/volume/image mutations still execute the current reviewed CLI commands. The next PR should extend typed actions for server create/delete/power/rename, Nova port/volume attachment, Neutron port and floating-IP actions, and image delete/set/upload, then remove each migrated CLI path. Uploads need a separate bounded file-transfer design: never permit a caller-selected daemon path.

Mutation tests must inject lost responses before/after effects, preserve exact operation/generation markers, quota-refusal distinctions, asynchronous waits, retained ports and `delete_on_termination=false`, and demonstrate no blind retry. Keep the existing journal and ownership checks above the transport. Add new socket authority only after establishing equivalence with the current helper and operator action scopes.

## Local evidence

`tests/test_provider.py` uses a loopback HTTP/TLS cloud fixture to cover token issuance/refresh/revocation, password/application credentials, connection reuse, concurrency, catalog selection, TLS/proxy policy, response and inventory limits, pagination, deadlines, peer rejection, fixed actions, safe errors and no CLI fallback. Retained-port lifecycle tests now serve the real read protocol against their state double; setup quota tests exercise the session interface. Admin VM tests add installed-unit readiness, socket mode and operator/controller/broker peer checks without contacting a cloud.

The console action accepts an explicit 1–2000 line tail or the provider's full console buffer when the reviewed script omits a line count; both retain the 1 MiB response limit.

Run the local benchmark with:

```bash
uv run --with python-openstackclient python tests/benchmark_provider.py
```

On this development host with Python 3.14.7, ten sequential flavor reads against the fake server measured **1573.787 ms median CLI**, **11.633 ms cold API**, **2.930 ms median warm API**, and **4.876 ms median warm socket/API read**. CLI issued ten Keystone tokens; the persistent client issued one. The benchmark clears ambient credential variables and uses only the loopback fixture. These numbers measure process/import/auth overhead, not production HTTPS latency, Nova scheduling or provider load.

The user-reported live baseline was 2–3 seconds of CLI startup/authentication per call; eight concurrent image-detail processes drove approximately 80% CPU pressure on the old two-core admin and 50% on the current four-core admin. Admin status took 30–45 seconds, two build-machine flavor reads took 6–9 seconds, and each role's image-selection CAS took about 2.5 minutes. No live measurements were taken for this PR.

Final local checks passed: `uv run python -m unittest discover -s tests` (842 tests, four existing skips), ruff, ruff format check, strict mypy, lock validation, vulture, compileall, documentation tests, Bash syntax, ShellCheck, Nix formatting and syntax parsing.

Nix evaluation/VM execution require the CI runner: this workspace cannot access the Nix daemon socket. Local Nix syntax parsing is available. The remaining acceptance questions are the cloud's versioned Nova/Cinder catalog shapes, Nova 2.46 availability, TLS/CA and any explicitly acknowledged HTTP Glance endpoint, and actual status/CAS/build latency after rollout.
