# Security

This page explains the platform's trust boundaries and the operator's responsibilities. Read it before deploying or changing access between components. The [glossary](README.md#glossary) defines the security terms; the [internals reference](reference/internals.md) describes the mechanisms.

## The idea

The platform separates web traffic, sign-in, authorization, and cloud operations. Each component gets only the access its job needs. Unexpected input is refused; uncertain changes stop for recovery. A compromised component can still misuse its allowed access; these boundaries limit the reach of that access.

## Boundaries

### Students and their apps

- **App input is typed.** The controller accepts a GitHub URL, an exact commit, a supported runtime, script names from `package.json`, a port, a health check path, and storage bindings. It rejects caller-supplied shell commands, host paths, provider IDs, and unknown fields, and ignores repository Dockerfiles. Package scripts still execute untrusted app code inside the builder.
- **Builds run on throwaway machines.** Each build gets a new builder VM with registry access to push the requested image. Builders can't use the cloud's metadata service, never see app runtime or storage credentials, and are deleted afterward.
- **Apps run in restricted containers.** Each app runs alone on its own worker. Workers have no SSH, can't reach cloud metadata, and accept app traffic only from the ingress host. Generated Nomad jobs use non-privileged containers, allow only a fixed read-only internal CA mount, and set resource, capability, process, and log limits.
- **Each app sees only its own secrets.** Environment variables and database credentials are injected through Nomad Variables scoped to one app. The controller stores no values, and controller and portal APIs never return them after saving. App code can read its injected values.
- **Private repositories use per-app deploy keys.** The portal creates a read-only key for one repository. It grants no access to anything else.

### Managed storage

Each app's PostgreSQL or MongoDB resource runs in a separate database server on the storage host. Its Podman container has systemd CPU and memory limits, no swap, connection caps, and an XFS project quota on its files. Per-instance nftables rules allow that app's worker IPs and fixed administration addresses. Other apps do not receive its credentials or network access.

These are per-instance limits on a shared host, not separate storage VMs. A host or volume failure affects all instances. MongoDB's sampled logical-size limit blocks writes above the limit and allows reads and deletes; writes resume at 95% or less. The hard filesystem quota is 150% of the logical limit, rounded up to a MiB. PostgreSQL reports logical size and has the same physical quota. Changing an instance's memory or connection limits restarts it.

S3 buckets share one Garage service. Each app receives an access key limited to its bucket, with byte and object quotas. [How it works](how-it-works.md#where-data-lives) explains where data and credentials live.

### The owner portal

- **The website has limited access.** `management-web` serves pages and forwards requests. It can't reach the controller, the identity service, or the portal database.
- **One service owns accounts and decisions.** `management-broker` owns users, sessions, ownership, quotas, and the audit log. It has no network access and is the only portal service that can talk to the controller.
- **Sign-in never touches Commons passwords.** The browser approves sign-in on Commons, and `management-identity` exchanges a single-use code with Commons server to server. PKCE binds the code to the initiating sign-in; a copied code alone cannot be redeemed. Redemption uses only the configured Commons origin, with network access limited to allowlisted addresses. Codes are not persisted or logged.
- **Production sessions are server-side.** Session cookies are opaque, `Secure`, and `HttpOnly`, with the `__Host-` prefix. Authenticated changes require a matching `Origin` and a session-bound CSRF token. The local HTTP development harness uses different cookie names and omits `Secure`.
- **The most powerful accounts are local.** Portal admins are local accounts with an authenticator app (TOTP). A Commons account can never become a portal admin. The first admin is created from a single-use setup link, so no password ever sits in configuration.
- **Sensitive admin actions require recent re-authentication.** This is called step-up: the local password and a fresh authenticator code must have been checked within the last five minutes. The table below distinguishes it from typing a deletion confirmation. Staff metadata reads are audited.

| Account role | Maximum session lifetime | Idle timeout |
| --- | --- | --- |
| Owner (default) | 8 hours | 30 minutes |
| Staff | 1 hour | 10 minutes |
| Portal admin | 1 hour | 15 minutes |

Configuration can shorten the session limits.

| Action | Portal authority | Extra check |
| --- | --- | --- |
| Delete storage | Staff or admin | Type the app slug and resource type; no step-up |
| Delete an entire app, including manual resume | Admin only | Type the exact app slug and have current step-up |
| Create or change accounts | Admin only | Current step-up |
| Other shared app management | Staff or admin | No password or authenticator confirmation |

App deletion removes the worker, database instances and their data, bucket and objects, environment values, deploy key, and memberships. It has no undo operation. Recovery requires backups and separately escrowed secrets. See [Manage apps and people](guides/manage-apps-and-people.md) before using it.

### The controller and the cloud

- **No network API.** The controller listens on two local Unix sockets. Linux identifies the connecting process (`SO_PEERCRED`) before any request is read. The hosted project socket permits only the broker's exact user/group IDs; the privileged socket permits only the operator account's exact IDs. The broker's sandbox also hides the privileged socket.
- **Portal roles are enforced by the broker.** A portal admin still uses `project.sock`, not `privileged.sock`. App deletion (`POST /v1/applications/{id}/delete`) exists on both sockets. On the project path, the broker checks the portal admin role, step-up, and exact slug. Platform-wide `/v1/admin/...` routes are available only on the privileged socket. The controller trusts the admitted broker for portal-user authorization; it does not authenticate each browser user.
- **No broad credentials in the web-facing process.** The controller calls a fixed helper that accepts one strict request type and runs only allowlisted actions. Input can't choose an executable, a host, a credential, or a shell command.
- **Recorded identity before reuse.** Setup and operator tools check the OpenStack project ID and name against the inventory. Resource reuse checks the recorded IDs, ownership metadata, and expected configuration or attachments. A matching name alone is insufficient; ambiguous evidence stops for recovery.
- **Unknown outcomes stay unknown.** If a cloud call's result is uncertain, the operation is journaled and resumed later with exact evidence. It isn't retried blindly or marked as done.

### Images and releases

- **Images come from one commit and are tested.** Each role image is built from a clean, complete Git commit, boot-tested, tied to signed release evidence, and selected by its exact OpenStack image ID. An upload alone doesn't make an image trusted; a live check on a real machine does.
- **No secrets in images.** Images contain software and non-secret configuration only. Cloud credentials, SSH keys, service passwords, tunnel tokens, and certificate keys arrive at first boot through protected paths, never through the Nix store.

### The network edge

- **No public origin by default.** In tunnel mode, Traefik listens only on `127.0.0.1` and nothing in OpenStack opens a public web port. The HTTPS provider (for example Cloudflare Tunnel) brings traffic in.
- **Direct mode is restricted.** If you expose the ingress host directly, only the exact IPv4 ranges your HTTPS provider publishes may connect, enforced by both OpenStack security groups and the host firewall. A rule open to the whole internet (`0.0.0.0/0`) is refused.

### Backups

- **Separate, encrypted sets.** Controller state, portal state, deploy keys, operator state, and app data are backed up separately and encrypted. The controller escrow key stays off the admin host. The managed-data key also lives on the admin host for restore checks. Keep off-site escrow copies of both.
- **Restores check before they replace.** Restore tools check the relevant deployment identity, schema, integrity, and manifest before replacing state.
- **Backups have gaps.** The image registry and Nomad Variable values are not backed up. Rebuild images from Git and escrow secret values you cannot regenerate. Off-site export needs a mounted filesystem on admin, re-mounted after replacement; local encrypted backups alone do not provide off-site recovery.

## What's still your job

These controls reduce what an attacker or a bug can do, but they don't replace good operations. You're responsible for:

- protecting the operator host account, and keeping the OpenStack credentials it uses as narrow as your cloud allows;
- reviewing release evidence before you deploy or upgrade;
- keeping off-site copies of backup decryption keys and protecting the release signing key;
- copying backups off site and checking that the copies restore;
- choosing your HTTPS provider and protecting its account and tunnel token;
- running recovery drills, so a real recovery isn't the first one;
- deciding who gets staff and portal admin roles.

## Related

- [How it works](how-it-works.md)
- [Plan a deployment](guides/plan-a-deployment.md)
- [Backups and recovery](guides/backups-and-recovery.md)
- [Internals reference](reference/internals.md)
