# Documentation

**6.1040 Apps** hosts student web apps in a course's OpenStack project. Students deploy GitHub commits through the owner portal; operators run the machines and course staff manage apps and accounts. Start with the reading path for your job, and use the [glossary](#glossary) for unfamiliar terms.

## The five machine roles

The platform has three persistent hosts and two kinds of disposable VM. A machine role is separate from an account role such as owner, staff, or portal admin.

| Machine role | Job |
| --- | --- |
| **Admin** | Controller, portal, Nomad scheduler, monitoring, and backups |
| **Ingress** | Traefik routes web requests to apps, the portal, or S3 |
| **Storage** | A database server per app resource, shared Garage S3 storage, and the app image registry |
| **Worker** | One app's container; replacement discards the worker's local disk |
| **Builder** | Builds one app image, publishes it to the registry, then is deleted |

[How it works](how-it-works.md) traces browser → Cloudflare → ingress → worker → database on the storage host, and the separate portal → broker → controller management path.

## Reading paths

**New to the platform?** Read [How it works](how-it-works.md). It assumes no knowledge of this repository.

**Deploying it for your class?** Follow these in order:

1. [Plan a deployment](guides/plan-a-deployment.md): what you need and what to decide first.
2. [Deploy the platform](guides/deploy-the-platform.md): from a clean checkout to a running platform.
3. [Open the owner portal](guides/open-the-portal.md): turn on the website students use, and create the first admin.
4. [Run the platform](guides/run-the-platform.md): everyday health checks and routines.

**Running it already?** Keep these close:

- [Manage apps and people](guides/manage-apps-and-people.md)
- [Backups and recovery](guides/backups-and-recovery.md)
- [Hosts and images](guides/hosts-and-images.md)
- [Releases and upgrades](guides/releases-and-upgrades.md)
- [Troubleshooting](guides/troubleshooting.md)

**A student deploying an app?** Read [Deploy an app](guides/for-app-owners.md).

**Course staff or a portal admin?** Read [Manage apps and people](guides/manage-apps-and-people.md) for app management, account roles, and destructive actions. You do not need an operator shell for these portal tasks.

**Changing the code?** Read [Development](development.md), then the [internals reference](reference/internals.md).

## All pages

### Understand

| Page | What it covers |
| --- | --- |
| [How it works](how-it-works.md) | The machines, a deployment from start to finish, data, sign-in, and limits |
| [Security](security.md) | What each boundary protects, and what stays your responsibility |

### Guides

| Page | What it covers |
| --- | --- |
| [Plan a deployment](guides/plan-a-deployment.md) | Requirements, OpenStack resources, domain and HTTPS, sign-in choices |
| [Deploy the platform](guides/deploy-the-platform.md) | Step-by-step setup and verification |
| [Open the owner portal](guides/open-the-portal.md) | Installing the portal, Commons or local sign-in, the first portal admin, staff, quotas |
| [Run the platform](guides/run-the-platform.md) | Health checks, the operator dashboard, routines, teardown |
| [Manage apps and people](guides/manage-apps-and-people.md) | Roles, quotas, managing any app, the audit log |
| [Deploy apps from the command line](guides/deploy-apps-from-the-command-line.md) | Deploying, rollback, sizing, retries, and retained or outbound IPv4 addresses |
| [Backups and recovery](guides/backups-and-recovery.md) | Backup sets, off-site copies, restores, recovery drills |
| [Migrate storage instances](guides/migrate-storage-instances.md) | Move shared database resources to separate database servers with per-resource limits |
| [Hosts and images](guides/hosts-and-images.md) | Image selections, replacing hosts, controller-path preflight, pruning |
| [Releases and upgrades](guides/releases-and-upgrades.md) | Signing, image builds and publication, release installation, migrations, live acceptance |
| [Troubleshooting](guides/troubleshooting.md) | Symptoms, causes, and fixes |
| [Deploy an app](guides/for-app-owners.md) | The student's guide to the owner portal |

### Reference

| Page | What it covers |
| --- | --- |
| [Configuration](reference/configuration.md) | The setup file and the deployment inventory |
| [Operator CLI](reference/operator-cli.md) | Every `openstack-platform` command |
| [Controller API](reference/controller-api.md) | The controller's sockets and routes |
| [Internals](reference/internals.md) | Components, state, lifecycle, and invariants, in depth |
| [Repository map](reference/repository-map.md) | Every file in the repository |

### Contributing

| Page | What it covers |
| --- | --- |
| [Development](development.md) | Local setup, CI checks, portal harness, and dashboard preview |
| [Portal design system](../frontend/DESIGN.md) | Design tokens, components, status vocabulary, and portal copy rules |

## Glossary

Machine roles and portal account roles are separate concepts. These terms appear in the guides and reference pages.

| Term | Meaning here |
| --- | --- |
| **6.1040 Apps / owner portal / portal** | The student-facing product and its website, made of `management-web`, `management-broker`, and `management-identity` on admin. |
| **Accepted deployment / candidate** | The app version recorded as active after health checks pass, and a new version still being checked. |
| **Accepted image** | A VM image that has passed live checks for its role. Uploading an image does not accept it. |
| **Adopt / reassign** | Attach an operator-created app to a portal owner, or change an app's recorded owner. Staff and admins perform these actions. |
| **Admin host** | The persistent VM running the controller, helper, portal, Nomad server, monitoring, and backups. Separate from the operator host. |
| **Age / recipient / identity** | The backup encryption tool, its public encryption key, and its private decryption key. |
| **Allocation / job** | Nomad's placement of an app on a worker, and the specification of the container it runs. |
| **App / application** | A named web app with deployment history, a dedicated serving worker, environment variables, and optional managed storage. |
| **App owner / member** | The person responsible for an app, and a teammate with access to it. The owner account role manages its own apps and apps it belongs to. |
| **Artifact / release** | A built output (image, archive, web bundle), and a source-bound set of those outputs. |
| **Audit log** | Broker records of account and app actions. Staff metadata reads have separate audit records. |
| **Binding** | An environment variable name chosen for a storage connection detail, such as `DATABASE_URL`. |
| **Broker** | `management-broker`, which owns accounts, sessions, ownership, quotas, and authorization and calls the controller's project socket. |
| **Builder** | A disposable VM that builds one app image and is deleted afterward. |
| **BuildKit / build recipe** | The image-building tool and the generated instructions it runs. Package scripts execute untrusted app code. |
| **CA / PKI** | A certificate authority and the certificates and keys that authenticate internal TLS connections. |
| **CIDR / security group** | IP-range notation, and OpenStack's network access rules. |
| **Cinder / Glance / Neutron / Nova** | OpenStack's volume, image, networking, and VM services respectively. |
| **Cloudflare / Cloudflare Tunnel** | An external HTTPS/DNS provider and its tunnel service. `cloudflared` on ingress connects outward to bring requests to local Traefik. |
| **Commons / Commons Connect** | The class website and its sign-in integration. The portal exchanges a single-use code for an identity without seeing the Commons password. |
| **Compatibility hash** | A digest of selected inventory fields used to check whether an image matches deployment configuration. |
| **Compare-and-swap (CAS)** | A change with an expected current selection. The controller refuses it if that selection has changed. |
| **Controller** | The admin-host service that records apps, deployments, and operations and coordinates execution through the helper. It serves local Unix sockets. |
| **CSP** | Content Security Policy: browser rules restricting the scripts, styles, and resources a page may use. |
| **CSRF token / Origin** | A session-bound token and the browser's source-origin header, checked on authenticated portal mutations. |
| **Database instance** | One app's PostgreSQL or MongoDB server, in its own Podman container and `<namespace>-database@<id>` systemd unit on storage. |
| **Deploy key** | A read-only SSH key for an app's private GitHub repository. Its public key is added to GitHub; the platform keeps the private key. |
| **Deployment marker** | The `X-Platform-Deployment` response header used to check that the public route reaches the expected deployment. |
| **Digest / MD5 / SHA-256 / SHA-512** | Content checksums. Image uploads use a provider multihash when available, or Glance MD5 plus exact size; release manifests use SHA-256. |
| **DNS / HTTPS / TLS / mTLS** | Hostname lookup, encrypted HTTP, its encryption protocol, and TLS that authenticates both client and server. |
| **Escrow / off-site copy** | A protected recovery copy of a key or secret, and a backup outside the platform's hosts and volumes. |
| **Fixed IP / retained primary IPv4** | A private address on an OpenStack port. A retained primary port keeps an app's address across worker replacement; moving it requires maintenance. |
| **Flavor / vCPU** | An OpenStack VM size (CPU, memory, disk), and a VM's virtual CPU. |
| **Floating IP / outbound IPv4** | A public OpenStack address attached to a port. An app can use it for outbound traffic; this does not open public app ingress. |
| **Garage / S3 / bucket / access key** | The shared object store on storage, its compatible API, a collection of objects, and credentials limited to an app's bucket. |
| **GiB / MiB** | Binary storage or memory units: 2³⁰ and 2²⁰ bytes. |
| **Health** | Evidence from the Nomad allocation and a public route check. The route check requires the expected deployment marker, not just an HTTP success. |
| **Helper** | A fixed program for allowlisted cloud, scheduler, and storage actions. Caller input cannot select an executable or shell command. |
| **Identity service** | `management-identity`, which redeems sign-in codes at the configured Commons origin. |
| **Image / registry** | A VM image boots a machine; a container image runs an app. The private registry on storage holds app container images. |
| **Image-selection seed** | A protected file used to initialize the controller's role-image selections and checked again at startup. Operators refresh it after completing a rollout; existing proven controller selections remain authoritative. |
| **Ingress host / Traefik / reverse proxy** | The persistent front-door VM, its routing service, and a server that forwards HTTP requests to another service. |
| **Inventory / policy** | Private deployment configuration (project, names, addresses, paths, images), and separate settings for limits and runtime profiles. Neither contains credentials. |
| **Journal / operation / reconciliation** | Durable records of a multi-step change, the change itself, and checking actual state before continuing interrupted work. Ambiguous outcomes require recovery. |
| **Lockfile** | A committed package-manager dependency record used to install the app's packages reproducibly. |
| **Maintenance / worker reuse** | A deployment that stops the accepted version before cutover, and an operator option to deploy on the existing verified worker. Reuse requires maintenance. |
| **Managed storage / resource** | An app's PostgreSQL database, MongoDB database, or S3 bucket. Databases use separate servers; buckets share Garage. |
| **Manifest / SBOM / provenance / release evidence** | An artifact inventory, a software bill of materials, build-origin records, and signed files binding a release to its source and outputs. |
| **Metadata service** | The cloud endpoint exposing instance metadata. Builder and worker workloads are blocked from it. |
| **Namespace / prefix / slug** | The configured service-name namespace, machine-name prefix, and app name used in its hostname. |
| **Nftables / XFS project quota** | The storage host's per-instance network allowlists, and filesystem caps on each database's disk use. |
| **Nix / NixOS / flake / Nix store** | The build tool, machine OS, repository build entry point, and store of build outputs. Secrets must not enter the Nix store. |
| **Nomad / Nomad Variables** | The scheduler and its encrypted runtime secret storage, scoped to an app's job. |
| **Operator / operator host / operator dashboard** | The infrastructure maintainer, the external Linux machine running setup and the CLI, and its local read-only health UI. |
| **Pinned SSH host key** | The exact server key expected by the operator's SSH bridge. |
| **PKCE** | Proof Key for Code Exchange: binds a Commons code to the initiating sign-in. A copied code alone is insufficient to redeem it. |
| **Podman / Docker / container** | Container engines used on builders/storage and workers, and the process environment they run. Container images differ from VM images. |
| **Portal admin** | A local account with TOTP. Has staff permissions plus whole-app deletion, storage-limit editing, accounts, roles, quotas, and the audit log. Separate from an operator shell account. |
| **Preflight / root-path preflight** | Checks before a change. Root-path preflight validates protected controller/helper filesystem paths against the candidate image's plan before replacement. |
| **Project socket / privileged socket** | Local `project.sock` for broker app operations and `privileged.sock` for operator administration. App deletion exists on both; portal authorization lives in the broker. |
| **Quota / limit** | A cap on app creation or resource capacity. Database instances have CPU, memory, connection, and disk caps; buckets have byte and object caps. |
| **Recovery required / tombstone** | An operation awaiting explicit recovery because evidence is uncertain, and a retained record of a deleted app. |
| **Rollback** | Return to an earlier app version or platform release. App rollback does not undo database changes. |
| **Role** | A machine kind (admin, ingress, storage, worker, builder) or portal permission level (owner, staff, admin). Context specifies which. |
| **Schema / migration / checkpoint** | A stored-data format, a controlled change to data or its format, and a verified backup taken before a database-resource migration. |
| **Shared resource** | A database on a shared server. New resources use instances. Sealed shared copies remain as migration fallbacks; see [the migration guide](guides/migrate-storage-instances.md). |
| **Staff** | Course-staff accounts with access to all apps, including ownership, maintenance, resizing, and storage deletion. Whole-app deletion and portal administration remain admin-only. |
| **Staged deployment / preview route** | A new version checked at a temporary hostname before promotion to the app's public route. |
| **Step-up / TOTP** | Recent re-authentication for sensitive admin actions, and a time-based authenticator code used with the local password. |
| **Storage host / volume** | The persistent database, Garage, and registry VM, and an OpenStack disk that survives host replacement. Other volumes hold admin state and backups. |
| **Systemd unit / timer** | A Linux service definition and its schedule. Database template units create separate services per instance. |
| **Unix socket / SO_PEERCRED** | A local interprocess endpoint and the Linux mechanism that verifies a connecting process's user/group IDs before reading a controller request. |
| **Worker / worker budget** | A disposable VM dedicated to one app, and its measured CPU/memory after reserve. Fresh-worker deployments derive container allocations from that budget. |
