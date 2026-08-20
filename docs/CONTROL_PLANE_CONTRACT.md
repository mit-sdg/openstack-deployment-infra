# `openstack-platform` command reference

`openstack-platform` is the unprivileged staff control surface for one
deployment. It stores accepted non-secret records and operation checkpoints in
`/srv/openstack-platform/state`. It invokes the constrained admin helper through
the pinned `platform-admin` SSH alias.

The database starts empty and is initialized on first invocation.
Reconciliation does not import external rows. Commands are limited to the
resource names in your platform configuration.

## Create a greenfield deployment

```text
openstack-platform setup --env-file PATH [--workspace PATH]
openstack-platform setup --env-file PATH [--workspace PATH]
  [--cloudflare-token-file PATH] --apply
```

`setup` is the only command that runs before an installed inventory, policy, or
management database exists. The default invocation validates the private input
file and prints a non-mutating summary. `--apply` authenticates to the named
OpenStack project, generates private deployment material, reserves the fixed
ports, builds and QEMU-tests five role images, creates the VMs and persistent
volumes, then installs and verifies the management/helper releases and backups.

The environment file must be a direct current-user-owned mode-`0600` file.
Literal dotenv and OpenRC assignments are parsed without executing the file.
Standard `OS_*` credentials select the project. `PLATFORM_*` assignments supply
stable names, domain, network, fixed addresses, management CIDR, flavors,
volume type, and optional size overrides. Missing non-secret choices prompt
with a discovered or documented default when one exists; a non-interactive run
must provide choices that cannot be inferred. Missing password authentication
prompts through a hidden terminal input.

Fresh volume defaults are 32 GiB for admin state, 500 GiB for managed data, and
200 GiB for backups. Image names include the first eight characters of the
clean source commit. Setup reuses its generated secret material and verifies
named resources on retry; it refuses a different generated inventory in the
same workspace. The default workspace is
`/srv/openstack-platform/setup`.

Cloudflare account, tunnel, DNS, and certificate creation are external. A
direct mode-`0600` token file enables `cloudflared` during ingress first boot.
Without it, setup completes the OpenStack deployment and reports public ingress
as pending. See [SETUP.md](SETUP.md) for the complete environment contract,
ordered mutations, verification, and retry boundary.

## Preconditions

Install matching management/helper releases as described in [RELEASE_INSTALLER.md](RELEASE_INSTALLER.md). Install:

- the real project name and UUID in mode-`0600` `/srv/openstack-platform/config/platform.json`;
- a mode-`0600` policy at `/srv/openstack-platform/state/policy.json` with reviewed runtime digests and age recipient;
- `platform-admin` with strict host-key checking and the fixed remote helper command.

For commands other than `setup`, persistent roles, Nomad ACLs, registry,
internal PKI, and public ingress must be healthy. Run as the unprivileged
`/srv/openstack-platform` owner. Optional global paths are:

```text
openstack-platform [--platform-config PATH] [--state-directory PATH] [--policy PATH] COMMAND
```

The helper does not read management SQLite. Only validated, non-secret action arguments cross SSH. The management side also requires the generated `platform-admin` bridge at direct mode-`0600` `/srv/openstack-platform/.secrets/ssh/config`; use [`OPERATIONS.md`](OPERATIONS.md) to generate and smoke-test it rather than hand-writing SSH settings.

## Read state

```text
openstack-platform status
openstack-platform infra list
openstack-platform infra image list
openstack-platform app list
openstack-platform app show SLUG
openstack-platform storage list [SLUG]
openstack-platform storage show SLUG postgres|mongo|s3 [--name NAME]
```

The first invocation creates or migrates the fresh SQLite schema. Its
version-zero marker is bound to the deployment project UUID, namespace, and
stable inventory identity, including resource names and state paths. The CLI
rejects a database copied from another deployment, or an older unbound
database, before enabling SQLite WAL sidecars. Changing images, flavors,
versions, checksums, or container pins does not change this state identity.
Read commands combine accepted records with bounded live
observations. Unavailable observations do not erase accepted records; `status`
reports degraded state when observations are unavailable or unhealthy.

On a fresh database with no accepted images, `status` reports three unavailable
observations for the persistent admin, ingress, and storage hosts:
`degraded  0  0  0  0  3  0`. Builder observation is included only while a
build operation is unfinished, and worker observations require an accepted
application.

Storage reads show `providerId` and `providerName` alongside configured limits.
The table output columns are `SLUG TYPE NAME PROVIDER_ID PROVIDER_NAME STATE QUOTA
VERIFIED HEALTH`. PostgreSQL and MongoDB measured-byte fields are targets, not
observations; no periodic usage collector is supported.

## Select images and operate persistent roles

```text
openstack-platform infra image set admin|ingress|storage|worker|builder IMAGE
openstack-platform infra image prune
openstack-platform infra image prune --apply --yes
openstack-platform infra start  admin|ingress|storage
openstack-platform infra stop   admin|ingress|storage --yes
openstack-platform infra reboot admin|ingress|storage --yes
openstack-platform infra replace admin|ingress|storage --yes
openstack-platform infra logs admin|ingress|storage [--lines COUNT]
```

Image selection resolves a provider image and records its UUID, full source
commit, and compatibility hash. The image must be complete and accepted;
selection rejects incomplete metadata.

Image pruning is plan-first. The plan protects selected images, images named by
unfinished operations, images referenced by servers, and the newest bounded
number of complete images per role; metadata-bearing but incomplete images are
review-only, not deletion candidates. Apply re-observes the exact UUID,
fingerprint, server-reference, and protection plan under the infrastructure
lock and refuses drift. A missing or malformed server image projection fails
closed rather than being treated as a volume-booted server. Deletions are
checkpointed one UUID at a time; an ambiguous or interrupted result becomes
recovery-required and must be reconciled before continuing.

Persistent replacement through `infra replace` retains the current server and
required volumes until the replacement passes role readiness. Before readiness
can count as acceptance, the provider is re-read and must show the exact
selected image UUID, retained flavor UUID, configured server name, and this
operation's provenance metadata. On failure it restores the prior host. This
CLI operation is the sole supported persistent-host replacement path; do not
delete the old server first or detach volumes manually.

## Declare an application

An application must exist before managed storage or staff environment values
can be addressed by its slug. A deployment creates one, which is enough when
the application starts without a database. An application that reads its
database at startup needs the database first, so declare it up front:

```text
openstack-platform app create SLUG
```

The declaration records the slug, the public URL, and the standard worker and
scheduler sizing from policy. It is not running and has no deployment, worker,
or source until a deployment is accepted, so it does not make the deployment
unavailable. Creating an existing slug is refused.

## Deploy an application

The repository must be public, credential-free GitHub HTTPS. `COMMIT` is a full
lowercase 40-character commit. The selected builder and worker images must be
accepted current images. The repository must contain a supported
`platform.yaml` and lockfile.

```text
openstack-platform app deploy SLUG \
  --repo https://github.com/OWNER/REPOSITORY \
  --commit COMMIT \
  [--config platform.yaml]
openstack-platform app logs SLUG --build [--lines COUNT]
openstack-platform app logs SLUG --runtime [--lines COUNT] [--follow]
```

`platform.yaml` accepts runtime `bun` or `node`, contained package paths, package-script names for build/start, an application port, a health path, and the typed `storage.bindings` mapping below. It does not accept Dockerfile instructions, command text, build arguments, build-time environment, resource settings, or provisioning requests. Repository `Dockerfile*` files may coexist as inert source files; the platform generates `recipe/Dockerfile` separately.

```yaml
storage:
  bindings:
    primary:                 # exact managed-resource name
      type: mongo
      environment:
        uri: MONGODB_URI     # typed output: runtime target
```

Binding names are 1–40 lowercase letters, numbers, or interior hyphens. Types are `postgres`, `mongo`, and `s3`. Output names are type-specific: PostgreSQL has `url`, `host`, `port`, `database`, `user`, `password`, `sslmode`, and `sslrootcert`; MongoDB has `uri`; S3 has `endpoint`, `region`, `access_key_id`, `secret_access_key`, `ca_bundle`, `bucket`, and `force_path_style`. Targets must be unique environment names and cannot be platform-reserved or start with `STORAGE__`.

Deployment requires every referenced `(application, type, name)` row to be active and every selected canonical output to exist in the application's Nomad Variable. It fails before job submission when a resource or output is missing, or when a target conflicts with an existing runtime key. Bindings neither create nor remove storage.

The operation acquires the exact commit, creates a single-use builder, generates a recipe with a policy-pinned runtime image, pushes one immutable digest, deletes the builder and fixed port, creates/verifies the dedicated worker, submits a constrained Nomad job, and accepts only after scheduler and public health pass. A failed candidate is removed and its manifest cleanup is confirmed. The command has one policy-bounded whole-operation deadline: lock waits, source acquisition, builder creation/build/cleanup, helper calls, and health probes receive only the remaining time. The helper and BuildKit receive the same durable wall-clock deadline, so a build cannot outlive its recorded operation. There is no manual job-history rollback command for an already accepted deployment.

## Manage application environment

Supply values through a hidden prompt, bounded stdin, or strict dotenv file.
They remain in Nomad Variables and never enter SQLite or command output.

```text
openstack-platform app env set SLUG KEY
printf '%s' "$VALUE" | openstack-platform app env set SLUG KEY
openstack-platform app env import SLUG --file PRIVATE.env
openstack-platform app env unset SLUG KEY [KEY ...]
openstack-platform app env list SLUG
```

Each mutation requires restart, scheduler health, and public health. Generated recipes keep the image default `NODE_ENV=production`; platform-owned keys, including `NODE_ENV`, and storage-owned keys cannot be changed through staff environment commands. Staff `set`, `import`, and `unset` refuse reserved keys. Variable updates use ModifyIndex compare-and-set.

## Manage storage

A declared or deployed application must exist before creation. Every identity is `(application_id, type, name)`; `--name` defaults to `default`.

```text
openstack-platform storage list [SLUG]
openstack-platform storage show SLUG postgres|mongo|s3 [--name NAME]
openstack-platform storage create SLUG postgres|mongo|s3 [--name NAME]
openstack-platform storage verify SLUG [postgres|mongo|s3] [--name NAME]
openstack-platform storage rotate SLUG postgres|mongo|s3 [--name NAME]
openstack-platform storage remove SLUG postgres|mongo|s3 [--name NAME] \
  --confirm NAME [--purge-s3]
```

Each mutation selects one exact instance. Its durable operation records the type and name, so a retry must repeat both. Create generates a provider identity, database/user or bucket/key scoped to that instance, verifies access and application health, writes collision-free `STORAGE__TYPE__NAME__OUTPUT` keys to the app Nomad Variable with compare-and-set, and records only non-secret identity and limits. Rotate and remove affect only that key set and provider instance. Remove requires the exact resource name in `--confirm`, performs preflight before irreversible deletion, and requires `--purge-s3` for a non-empty bucket. Provider absence and canonical-key removal are confirmed before accepted records are deleted.

Do not create credential JSON, synchronize whole Nomad Variables, or use provider administrator credentials as a substitute for these commands.

## Back up and restore management state

```text
openstack-platform backup
openstack-platform restore BACKUP --age-identity IDENTITY --yes
```

`backup` uses SQLite's online backup API, creates a private temporary copy,
encrypts it with the policy `backupAgeRecipient` through the verified age
executable, and stages it through the pinned admin alias. The remote staging
path is `<paths.backups>/m1/.staging/<name>`; helper acceptance verifies an
age-v1 header and SHA-256, then publishes the file at
`<paths.backups>/m1/<name>` with `<name>.sha256` plus
`<name>.manifest`. The manifest is the commit marker: ciphertext and checksum
are written and fsynced before its final rename, and the accepted directory is
fsynced after each rename. Readers and retention count only complete evidence
trios. A retry reconciles a ciphertext/evidence move interrupted before the
manifest appeared; a malformed set that already has a manifest is refused,
not silently repaired. `paths.backups` is read from the installed inventory and
is not a fixed management or checkout path. This backup does not include managed
data.

`restore` is an offline operation. Global options must precede `restore`:

```text
openstack-platform --state-directory /srv/openstack-platform/state \
  restore /private/path/platform-YYYYMMDDTHHMMSSZ.sqlite3.age \
  --age-identity /private/path/backup-age-identity.txt --yes
```

An installed management release also provides the preferred fixed-destination
launcher:

```text
/srv/openstack-platform/bin/openstack-platform-restore \
  /private/path/platform-YYYYMMDDTHHMMSSZ.sqlite3.age \
  --age-identity /private/path/backup-age-identity.txt --yes
```

It always targets `/srv/openstack-platform/state/platform.sqlite3`; do not pass
`--destination` to this launcher. It contacts no provider, helper, SSH, Nomad,
or network service. It requires private direct mode-`0600` source/identity files
and a private mode-`0700` destination directory, decrypts/validates a temporary
candidate, checks the current deployment-bound marker, known schema, SQLite
integrity, foreign keys, and unfinished operations, and then atomically replaces
the destination. A backup from a different project, namespace, stable resource
inventory, or state-path identity is refused; the existing database is left
unchanged. Corrupt, future, unrecognized, unsafe, busy, or unfinished state is also
refused. Restore does not recreate provider resources or import external rows;
run
`status` and the read commands afterward to reconcile live observations. The
fixed launcher supplies the installed inventory and destination, so do not
bypass it with a release-internal restore path.

Managed PostgreSQL, MongoDB, and Garage backups use the admin scripts and
packaged admin age identity documented in [OPERATIONS.md](OPERATIONS.md).

## Remove an application

Remove managed storage first, then confirm the exact slug:

```text
openstack-platform app remove SLUG --confirm SLUG
```

Removal completes only after the Nomad job and Variables, worker and fixed port, and all tracked current, prior accepted, and failed-candidate registry manifests are absent. It does not delete unrelated provider resources.

## Recover an interrupted operation

Provider, SSH, HTTP, Git, Nomad, and SQLite calls do not share one transaction.
Commands serialize infrastructure work globally and application/environment/
storage work per application. Mutating operations use a policy-bounded
whole-call deadline, and their deadline-aware lock waits use non-blocking
probes and fail closed before the deadline rather than blocking indefinitely.
Read-side provider calls retain their individual configured bounds.

1. Do not manually delete, detach, rotate, rename, or recreate the referenced resource.
2. Restore the dependency named by the safe error.
3. Rerun the same command with the same identity arguments and confirmation. The recorded phase either continues, confirms cleanup, restores prior healthy state, or stops with a narrower action.
4. If another command owns the unfinished scope, run the command family named by the error.

Secret values are not persisted for replay. Unexpected management failures provide a correlation ID and private diagnostics below the state directory; helper diagnostics are private on admin at `controller/helper-diagnostics/<correlation-id>.trace`. The trace contains bounded source file/line locations and never secret values or provider payloads. Protocol responses are bounded and do not contain credentials or unrestricted provider payloads.
