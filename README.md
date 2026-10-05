# A small application platform for OpenStack

This repository builds a NixOS-based platform for hosting small HTTP
applications inside one OpenStack project. It creates the cloud foundation,
five machine roles, PostgreSQL/MongoDB/S3 services, private image registry,
public ingress, backups, recovery tooling, an operator CLI, and a local
application controller.

## Current status

Infrastructure deployment and operation are implemented. The owner portal is
implemented but not deployed, so there is not yet a supported workflow for
application owners on a live platform. Students will sign in with their class
username/password, checked server-side with Commons; the merged authenticate
endpoint must be configured and pass live acceptance before availability is
claimed. Commons remains the external class-account sign-in method. Local portal
accounts store salted scrypt password hashes in the broker DB; admins are local
accounts and must enroll TOTP. Owners manage their own apps. Staff also read the
course catalog and manage any broker app like admins, with no app limits. Only
local admins manage accounts, quotas and the audit log, create apps for other
owners, adopt or reassign apps, delete storage, and allow maintenance outages or
resizing. Admins can adopt existing controller apps by UUID, including Commons;
class-app changes require an extra confirmation and retained-IP deploys require
maintenance, so only admins can deploy those apps. Roles are assigned in the
broker DB and captured at sign-in; security changes revoke all of an account's
sessions. Owner sessions default to 8 h/30 min idle, staff 1 h/10 min, admin
1 h/15 min. Commons password changes/archiving do not revoke issued sessions.
The operator issues a hash-only, single-use setup URL for initial admin enrollment
or recovery; no password goes in inventory or environment variables. The operator
dashboard remains separate. See [portal operations](docs/OPERATIONS.md#owner-portal-operations).

Today, an operator can create and recover a platform with:

- persistent admin, ingress, and storage hosts;
- replaceable application workers and single-use builders;
- exact-image and persistent-host lifecycle controls;
- a read-only operator dashboard for role, application, operation, and
  platform-health status;
- separate encrypted backups for controller, operator, broker identity/ownership
  state, deploy keys, and managed data; and
- a local controller and locally tested owner portal with isolated identity checks.

The operator dashboard and owner portal use shared React presentation components
and theme tokens in the `frontend/` workspace. They remain separate apps with
separate API clients and servers: operator evidence stays behind its private Unix
socket. The dashboard's generated browser assets are committed with the CLI, so
operator installation requires no Node tooling. See the
[dashboard preview and frontend checks](docs/DEVELOPMENT.md#preview-the-operator-dashboard).

The implemented local application workflow supports GitHub repositories (public
ones credential-free, private ones through a per-app read-only deploy key) and
typed Node or Bun configuration. Users will not receive SSH, OpenStack, Nomad,
registry, or database-administrator credentials. Arbitrary build commands and
Dockerfiles are outside the current contract. Owners can set write-only environment variables and provision one
PostgreSQL database, MongoDB database, and S3 bucket per app. They choose which
storage outputs bind to which environment names; credentials never appear in
portal responses. Storage deletion requires an administrator. PostgreSQL connections currently
use the URL binding, which includes the password; individual PostgreSQL password
and S3 secret key bindings need a platform update. See
[owner portal operations](docs/OPERATIONS.md#owner-portal-operations).

## Documentation

- [Deploy an application with curl](docs/APPLICATION_DEPLOYMENTS.md) — operator
  deployment, maintenance cutover, recovery, and rollback without the management UI.

- [Deploy the platform](docs/DEPLOYMENT.md) — what the platform creates, what it
  supports, its security model, setup, ingress, and verification.
- [Operate and recover it](docs/OPERATIONS.md) — health, the read-only
  dashboard, backups, off-site export, restore, host replacement, pruning, and
  troubleshooting.
- [Platform internals](docs/INTERNALS.md) — component ownership, state,
  controller/helper boundaries, internal API, and the owner portal.
- [Release and platform maintenance](docs/MAINTENANCE.md) — signed releases,
  role images, publication, installation, and live acceptance.
- [Development workflow](docs/DEVELOPMENT.md) — local environment and checks.
- [Tracked-file guide](docs/REPOSITORY_GUIDE.md) — the purpose of every file in
  Git.

## License

Licensed under the [Apache License 2.0](LICENSE).
