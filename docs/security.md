# Security

This page explains how the platform limits what each part can do, and what it leaves to you. Read it before you deploy, and again before you change a boundary in the code. The [internals reference](reference/internals.md) has the exact mechanisms.

## The idea

The platform separates web traffic, sign-in, authorization, and cloud operations. Each component gets only the access its job needs. Unexpected input is refused; uncertain changes stop for recovery. A compromised component can still misuse its allowed access; these boundaries limit the reach of that access.

## Boundaries

### Students and their apps

- **App input is typed.** The controller accepts a GitHub URL, an exact commit, a supported runtime, script names from `package.json`, a port, a health check path, and storage bindings. It rejects caller-supplied shell commands, host paths, provider IDs, and unknown fields, and ignores repository Dockerfiles. Package scripts still execute untrusted app code inside the builder.
- **Builds run on throwaway machines.** Each build gets a new builder VM with registry access to push the requested image. Builders can't use the cloud's metadata service, never see app runtime or storage credentials, and are deleted afterward.
- **Apps run in restricted containers.** Each app runs alone on its own worker. Workers have no SSH, can't reach cloud metadata, and accept app traffic only from the ingress host. Generated Nomad jobs use non-privileged containers, allow only a fixed read-only internal CA mount, and set resource, capability, process, and log limits.
- **Each app sees only its own secrets.** Environment variables and database credentials are injected through Nomad Variables scoped to one app. The controller stores no values, and controller and portal APIs never return them after saving. App code can read its injected values.
- **Private repositories use per-app deploy keys.** The portal creates a read-only key for one repository. It grants no access to anything else.

### The owner portal

- **The website has limited access.** `management-web` serves pages and forwards requests. It can't reach the controller, the identity service, or the portal database.
- **One service owns accounts and decisions.** `management-broker` owns users, sessions, ownership, quotas, and the audit log. It has no network access and is the only portal service that can talk to the controller.
- **Sign-in never touches Commons passwords.** With Sign in with Commons, the browser approves the portal on Commons, and `management-identity` exchanges a single-use code with Commons server to server. Code redemption uses only the configured Commons origin; network access is limited to allowlisted addresses. Codes are never stored or logged.
- **Sessions are server-side and short.** Session cookies are opaque, `Secure`, `HttpOnly`, and bound to the portal's origin. Changes require a matching `Origin` and a CSRF token. By default, owner sessions expire after 8 hours or 30 minutes idle. Staff sessions are capped at 1 hour or 10 minutes idle; portal admins at 1 hour or 15 minutes. Configuration can shorten these limits.
- **The most powerful accounts are local.** Portal admins are local accounts with an authenticator app (TOTP). A Commons account can never become a portal admin. The first admin is created from a single-use setup link, so no password ever sits in configuration.
- **Dangerous actions need more.** Staff and portal admins delete storage by typing the app and storage names to confirm. App actions need no password or authenticator confirmation. Creating or changing accounts remains admin-only and requires re-entering the password and a fresh authenticator code within the last five minutes. Staff reads of student data are recorded in the audit log.

### The controller and the cloud

- **No network API.** The controller listens on two local Unix sockets. The operating system identifies the connecting process (`SO_PEERCRED`) before any request is read. The portal broker gets ordinary operations; only the operator's account gets administrator reads and destructive operations.
- **No broad credentials in the web-facing process.** The controller calls a fixed helper that accepts one strict request type and runs only allowlisted actions. Input can't choose an executable, a host, a credential, or a shell command.
- **Exact identity before any change.** Setup and the operator tools refuse to act unless the OpenStack project's ID and name match your private inventory. Existing resources are checked by ID, metadata, network, address, image, flavor, and attachment before they're reused. A matching name alone is never enough, and an ambiguous result stops for recovery.
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
