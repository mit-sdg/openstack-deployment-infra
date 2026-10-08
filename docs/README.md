# Documentation

These docs explain what the platform is, how to deploy it for a class, and how to run it. Start with the path that matches what you're doing.

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

**Admin host, ingress host, storage host.** The three permanent virtual machines. The admin host runs the controller, the owner portal, Nomad, and backups. The ingress host runs Traefik. The storage host runs the databases, S3, and the image registry.

**App owner.** A person, usually a student, who owns apps in the owner portal.

**Builder.** A virtual machine created for one build and deleted afterward.

**Commons.** A separate class website. Students sign in to the owner portal with their Commons account.

**Controller.** The service on the admin host that records apps and deployments and carries them out. It listens only on local Unix sockets.

**Deploy key.** A read-only SSH key the portal creates for one private GitHub repository.

**Garage.** The S3-compatible file store on the storage host.

**Helper.** A fixed program the controller calls to act on OpenStack, Nomad, and storage. It runs only allowlisted actions.

**Inventory.** The private, non-secret file that describes your deployment: project, names, addresses, images, and paths.

**Managed storage.** The per-app PostgreSQL database, MongoDB database, and S3 bucket.

**Nomad.** The scheduler that runs each app's container on its worker.

**NixOS.** The Linux distribution every machine runs. Its whole configuration is built from code in this repository.

**Operator.** The person who deploys and runs the platform.

**Operator dashboard.** A read-only web page the operator runs locally to see the platform's health.

**Operator host.** The x86_64 Linux machine where an unprivileged account runs setup and the `openstack-platform` tool.

**Owner portal.** The website app owners use, at your domain. It's made of three services on the admin host: `management-web`, `management-broker`, and `management-identity`.

**Portal admin.** The owner portal role with full app management plus accounts, roles, quotas, and the audit log. Always a local account with an authenticator app.

**Release evidence.** Signed files that record exactly which source commit and built artifacts make up a release.

**Role.** One of the five kinds of machine: admin, ingress, storage, worker, builder.

**Staff.** The owner portal role for course staff. Staff have the same app-management permissions as portal admins, including ownership changes, maintenance and resizing, and storage deletion. Account management, quotas, and the audit log remain admin-only.

**Traefik.** The reverse proxy on the ingress host that routes each hostname to the right app or service.

**Worker.** The virtual machine that runs one app.
