# Plan a deployment

Use this guide to prepare a platform for your class. Choose the cloud resources, domain, sign-in method, and release maintainer before you run [Deploy the platform](deploy-the-platform.md).

## Before you begin

You'll need an OpenStack project, an x86_64 Linux operator host, a domain with an HTTPS provider, and a staff member comfortable with Linux and OpenStack. Read [How it works](../how-it-works.md) for the five machine roles.

## Is it a good fit?

Students deploy Node.js or Bun apps from GitHub, each at its own HTTPS address with optional PostgreSQL, MongoDB, and S3 storage. They use the owner portal at your domain, without SSH, cloud, or database administrator access.

It doesn't accept Dockerfiles or arbitrary build commands, support custom domains or multiple instances of one app, or give students shell access. See [What it doesn't do](../how-it-works.md#what-it-doesnt-do).

The **operator** is the staff member who runs setup, keeps backups, and replaces machines.

## OpenStack terms on this page

| Term | Meaning |
| --- | --- |
| Project | Your slice of the cloud. Quotas, networks, and machines belong to a project. |
| Nova server (instance) | A virtual machine. |
| Flavor | A cloud-defined VM size: vCPUs, memory, root disk. |
| Glance image | Bootable disk image in OpenStack's image service. |
| QCOW2 | The disk image file format setup builds and uploads to Glance. |
| Neutron network, subnet, port | A port attaches a VM to a network with its own address. Fixed ports preserve addresses across permanent host replacements. |
| Security group | A set of firewall rules attached to a port. |
| Cinder volume, volume type | Persistent virtual disk; volume type selects its storage backend. |
| Keypair | An SSH public key registered with Nova. |
| Quota | The maximum amount of each resource your project may use. |
| Application credential | A revocable login for one project, separate from your personal password. |

## What setup creates

Setup uses an existing project and network. The setup check stops on resource name collisions. Your chosen **prefix** starts every resource name below.

| Resource | How many | Names | What it's for |
| --- | --- | --- | --- |
| Glance images | 5 | `<prefix>-nixos-<role>-<first 8 characters of the commit>` | One NixOS disk image per role, built from the release commit |
| Nova servers | 3 | `<prefix>-admin-01`, `<prefix>-ingress-01`, `<prefix>-storage-01` | The permanent admin, ingress, and storage hosts |
| Neutron ports | 3 | `<prefix>-admin-public-v4`, `<prefix>-ingress-public-v4`, `<prefix>-storage-service-v4` | A fixed address for each permanent host |
| Security groups | 5 | `<prefix>-admin`, `<prefix>-ingress`, `<prefix>-storage`, `<prefix>-worker`, `<prefix>-builder` | Firewall rules for each role |
| Cinder volumes | 3 | `<prefix>-admin-state`, `<prefix>-data`, `<prefix>-backups` | Durable state, app data, and backups |
| Nova keypair | 1 | `<prefix>-admin` | Recovery SSH key for the permanent hosts |

Images and servers carry deployment metadata that the platform checks before reusing or replacing them.

After setup, the platform creates more machines on its own:

- one **worker** server and port for each app, replaced on every deploy;
- one **builder** server and port for each build, deleted when the build ends.

### What each role does

| Role | Lifetime | In one line |
| --- | --- | --- |
| `admin` | Permanent | Runs the controller, the owner portal, Nomad, monitoring, and backups. |
| `ingress` | Permanent | Runs Traefik, which routes each public hostname to the right app or service. Stores no data. |
| `storage` | Permanent | Runs PostgreSQL, MongoDB, Garage (S3), and the private image registry. |
| `worker` | One per app | Runs one app. No SSH, no permanent disk. |
| `builder` | One per build | Builds one app image from one commit, then is deleted. |

### Volumes and their default sizes

| Volume | Default | Attached to | Holds |
| --- | ---: | --- | --- |
| Admin state | 32 GiB | admin | Controller, Nomad, and helper state |
| Managed data | 500 GiB | storage | PostgreSQL, MongoDB, Garage S3, and registry data |
| Backups | 600 GiB | admin | Encrypted backups and restore evidence |

The backup volume must be at least as large as the managed-data volume. All three use the same volume type. Choose sizes carefully: they become part of the deployment's identity, and setup can't resize them later.

### What the security groups allow

Each permanent host gets exactly one group. Workers and builders get theirs when they're created.

| Group | Inbound traffic allowed |
| --- | --- |
| admin | SSH and ping from your operator address range; Nomad and portal traffic from ingress; Nomad from workers |
| ingress | SSH from admin. In direct mode only, HTTP on port 80 from your HTTPS provider's address ranges. |
| worker | The app port (8080) from ingress |
| builder | SSH from admin |
| storage | Database, S3, and registry ports from workers; the same plus SSH and Garage from admin; the registry from builders; S3 from ingress |

From outside the platform, only your operator range (SSH to admin) and, in direct mode, your provider's ranges (HTTP to ingress) can connect. Keep the operator range as narrow as you can. [Security](../security.md) explains the reasoning.

## The operator host

The **operator host** is the Linux machine and unprivileged account where you run setup, the CLI, backups, and the operator dashboard. Protect it: it holds SSH keys, OpenStack credentials, and backup keys.

### Machine and account

- An `x86_64` Linux machine. It can be a server in the same cloud.
- An ordinary, unprivileged account. Setup refuses to run as root or under `sudo`.
- The directory `/srv/openstack-platform` must already exist, be owned by that account, and have mode `0700`. Creating it needs root, so ask a host administrator.
- The account's user systemd manager must work (`systemctl --user` runs without error). Setup installs a daily backup timer there.
- Lingering should be enabled for the account, so the timer runs while nobody is logged in. `loginctl show-user "$USER" --property=Linger` must print `Linger=yes`; a host administrator can turn it on.
- Enough free disk space for Nix to build five VM images and their dependencies.

### Tools

| Tool | Why |
| --- | --- |
| Nix, with flakes enabled and access to the Nix daemon | Builds the role images and the tools setup uses |
| uv | Installs the Python environment for the checkout |
| Python 3.14 | Required by the code. uv can install it for you. |
| OpenStack CLI (`openstack`) | Used by the read-only setup check. The full setup builds its own copy with Nix. |
| Git, OpenSSH (`ssh`, `scp`, `ssh-keygen`, `ssh-keyscan`), OpenSSL 3, curl | Source checks, the admin connection, key generation, and signature checks |
| `systemctl` | The user backup timer |

QEMU boot-tests each image. Read/write access to `/dev/kvm` enables hardware acceleration; software emulation is slower.

### Network access

The operator host needs to reach:

- your cloud's OpenStack API endpoints;
- GitHub and `releases.hashicorp.com`, for pinned source and runtime downloads;
- the Nix binary cache (`cache.nixos.org`);
- the admin host's fixed address on TCP port 22, from inside the operator address range you configure.

The operator host never connects to the ingress or storage hosts directly.

## The OpenStack project

### Use an empty project, or a unique prefix

Prefer an empty project. In a shared project, choose an unused prefix; the setup check stops on name collisions.

### Credentials

Setup stores your project credential on the operator host and copies it to the admin host for the helper to manage workers and builders.

- Prefer an **application credential** scoped to this project. You can revoke it without touching anyone's password.
- If you use a username and password, use an account that can only reach this project.

### Quotas

The setup check stops if quota is short or unknown. At minimum flavor sizes and default volume sizes, it needs:

| Resource | Setup needs |
| --- | --- |
| Instances | 5 (three permanent hosts, plus room for one worker and one builder) |
| vCPUs | The sum of your five flavors: 13 at the minimum sizes |
| RAM | The sum of your five flavors: 26,624 MiB (26 GiB) at the minimum sizes |
| Volumes | 3 |
| Volume storage | The sum of the three volumes: 1,132 GiB at the defaults |
| Ports | 5 |
| Security groups | 5 |
| Security group rules | 32 |
| Keypairs | 1 |
| Images | 5 |
| Image storage | The total size of the five images, which the release evidence records |

Allow headroom for your class: each app adds a worker and port; each deploy temporarily adds a builder and a second worker until traffic switches. See [Setup check failures](troubleshooting.md#the-setup-check-doesnt-say-ready).

### Flavors

You pick one flavor per role. Each must meet a minimum size:

| Role | Minimum vCPUs | Minimum RAM |
| --- | ---: | ---: |
| admin | 2 | 4 GiB |
| ingress | 2 | 2 GiB |
| storage | 4 | 8 GiB |
| worker | 1 | 4 GiB |
| builder | 4 | 8 GiB |

The worker flavor is also the standard size for every app. Missing flavors are prompted with the smallest qualifying default.

### Volume type

Use one Cinder volume type. Setup offers `production` if present, otherwise the sole type if exactly one exists.

### Network and fixed addresses

Pick one existing network. It must have:

- three unused IPv4 addresses, each in exactly one subnet, for admin, ingress, and storage. Ask your cloud administrator which addresses to reserve; prefer addresses outside automatic allocation pools.
- free automatic addresses for workers and builders;
- outbound internet access for the platform's machines. They pull container images from Docker Hub and app source from GitHub, and in tunnel mode the ingress host connects out to Cloudflare.

In direct ingress mode, your HTTPS provider must also be able to reach the ingress address on TCP port 80.

### The image service

The setup check reads Glance's quota over HTTPS. Two older-cloud cases need an explicit acknowledgement in the setup file: a Glance endpoint that only offers plain HTTP, and a Glance that has no quota usage endpoint. [Configuration](../reference/configuration.md#provider-acknowledgements) lists the exact values.

## Domain and public HTTPS

An external HTTPS provider handles DNS and certificates for your base domain (`<domain>`, for example `apps.example.edu`) and forwards requests to ingress.

| Hostname | What it serves |
| --- | --- |
| `<domain>` | The owner portal |
| `<app-name>.<domain>` | Each app |
| `s3.<domain>` | The public S3 endpoint for presigned upload and download links |
| `projects.<domain>`, `compute.<domain>` | Two extra hostnames for the portal (these are the defaults; you can choose others) |

Your provider must:

- route `<domain>`, `*.<domain>`, and the two extra hostnames to the ingress host;
- serve trusted certificates for every hostname. For `apps.example.edu`, cover `*.apps.example.edu`; `*.example.edu` alone is insufficient;
- keep the original `Host` header, because Traefik routes by hostname;
- allow the path `/healthz`, which setup and health checks use.

### Choose an ingress mode

Choose the ingress mode before setup. It's built into the ingress image.

| | Tunnel (default) | Direct |
| --- | --- | --- |
| How traffic arrives | The ingress host opens an outbound Cloudflare Tunnel; Cloudflare sends requests through it | The provider connects to the ingress host's address on port 80 |
| Inbound ports opened | None. Traefik listens only on `127.0.0.1`. | Port 80, only from the provider's IPv4 ranges |
| Provider to ingress hop | Inside the encrypted tunnel | Plain HTTP over the network |
| What you need | A Cloudflare account and a tunnel token | A provider that publishes stable IPv4 origin ranges, and an ingress address it can reach |

**Tunnel** is the default. The ingress address needn't be internet-reachable.

Use **direct** when your provider publishes exact IPv4 origin ranges and can reach ingress. List those CIDRs in setup; `0.0.0.0/0` is refused.

> [!NOTE]
> Replacing the ingress host later always asks for a Cloudflare tunnel token file, even in direct mode. Keep that in mind if you plan to run without Cloudflare.

For tunnel mode, you'll create the tunnel and its hostname routes in Cloudflare before running setup. [Deploy the platform](deploy-the-platform.md#5-set-up-public-ingress) walks through it.

## Sign-in for the owner portal

Setup leaves the portal off. Choose student sign-in now, then enable it with [Open the owner portal](open-the-portal.md).

- **Local accounts only.** Accounts live in the portal, and portal admins create and manage them. No outside service is involved, although the sign-in page still shows the Commons button.
- **Commons.** [Commons](https://github.com/mit-sdg/commons) is a separate class website. Students click **Sign in with your class account**, approve the portal once on Commons, and come back signed in. The portal never sees their Commons password.
- **Both.** Students sign in with Commons, and you also create local accounts where you need them, for example for portal admins.

Portal admins are always local accounts and must use an authenticator app, whichever option you pick.

If you choose Commons, you need:

- a Commons deployment at a public HTTPS address, for example `https://class.example.edu`;
- Commons configured to accept your portal's origin, `https://<domain>`. Commons' `CONNECT_APP_DOMAIN` setting must cover your platform domain;
- network reach from the admin host to Commons. By default the portal's identity service may only connect to Cloudflare's published address ranges, which suits a Commons served through Cloudflare. Otherwise, you'll list Commons' address ranges when you open the portal.

## Release evidence

Setup requires signed **release evidence** pinning the source commit and five role images. Choose a maintainer to hold the Ed25519 key outside the repository and GitHub.

Images embed your deployment inventory (project, names, addresses), so their evidence is deployment-specific. [Releases and upgrades](releases-and-upgrades.md) covers building and signing it.

## Time and compute

Setup builds all five images with Nix unless matching images for this release and deployment already exist in Glance, such as after an interrupted run or a release pipeline upload.

Plan for a long run:

- Initial Nix downloads and builds can be lengthy. Most subprocess commands have a two-hour timeout; some checks have shorter limits.
- Each image is boot-tested in QEMU with 2 vCPUs and 3 GiB of memory, and must finish booting within about six minutes.
- Setup then uploads the five images and waits for each permanent host to report ready.

Run setup in `tmux` or `screen`. If interrupted, rerun the same command to resume.

## Checklist

Operator host:

- [ ] `x86_64` Linux, unprivileged account, not run through `sudo`
- [ ] `/srv/openstack-platform` exists, owned by the account, mode `0700`
- [ ] `systemctl --user` works and lingering is enabled
- [ ] Nix with flakes, uv, Python 3.14, `openstack`, Git, OpenSSH, OpenSSL 3, curl installed
- [ ] Network access to OpenStack, GitHub, `releases.hashicorp.com`, and the Nix cache

OpenStack:

- [ ] An empty project, or a prefix no existing resource uses
- [ ] An application credential (or a narrowly scoped user) for the project
- [ ] Quota for the table above, plus room for your apps
- [ ] Five flavors that meet the minimum sizes
- [ ] A volume type, and volume sizes (backups at least as large as data)
- [ ] One network, three unused fixed IPv4 addresses on it, and outbound internet for its machines
- [ ] Your operator host's address range, and SSH reachability from it to the admin address

Names and domain:

- [ ] A prefix and namespace (lowercase letters, digits, hyphens)
- [ ] A display name (up to 40 characters) and an organization name
- [ ] A base domain, and an HTTPS provider that covers `<domain>`, `*.<domain>`, and the two extra hostnames
- [ ] An ingress mode: a Cloudflare tunnel and its token, or your provider's IPv4 ranges for direct mode

Sign-in and releases:

- [ ] Local accounts, Commons, or both; for Commons, its origin and its acceptance of `https://<domain>`
- [ ] A person who holds the release signing key and can produce evidence for your deployment

Time:

- [ ] Time for a long, uninterrupted run on the operator host, in `tmux` or similar

## Related

- [Deploy the platform](deploy-the-platform.md)
- [Configuration reference](../reference/configuration.md)
- [How it works](../how-it-works.md)
- [Security](../security.md)
- [Releases and upgrades](releases-and-upgrades.md)
- [Open the owner portal](open-the-portal.md)
- [Troubleshooting](troubleshooting.md)
