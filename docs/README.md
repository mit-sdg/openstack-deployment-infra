# Documentation

The repository currently provides automated OpenStack/NixOS setup, an
operator-only infrastructure CLI, a hosted local controller, and backup/recovery
tooling. It does not provide the future browser management or authentication
applications.

## Deploy and operate

Follow this path for a new deployment:

1. [Automated setup](SETUP.md) — prepare the protected environment, run the
   read-only preflight, and create the deployment.
2. [Verify a fresh platform](TUTORIAL.md) — verify the CLI/controller boundary,
   public ingress, backups, restore checks, and schedules.
3. [Operations and disaster recovery](OPERATIONS.md) — run backups, off-site
   export, restore drills, role replacement, pruning, and teardown safeguards.
4. [Troubleshooting](TROUBLESHOOTING.md) — diagnose failures by observable
   symptom.

## Reference

- [Configuration](CONFIGURATION.md) — deployment inventory, policy, names,
  ingress, volumes, paths, and validation.
- [Operator CLI and controller API](CONTROL_PLANE_CONTRACT.md) — exact syntax,
  routes, transport bounds, idempotency, responses, and errors.
- [Public ingress](PUBLIC_INGRESS.md) — tunnel/direct origin policy and provider
  requirements.
- [NixOS role images](../nix/README.md) — image outputs, tests, publication
  inputs, and exact-UUID selection.

## Architecture and future UI

- [Architecture and trust boundaries](ARCHITECTURE.md) — role/state ownership,
  control surfaces, isolation, backup classes, and failure boundaries.
- [Management application specification](MANAGEMENT_APP_SPEC.md) — the future
  renderer/broker host boundary, authentication, authorization, records, UI,
  controller use, reconciliation, and required evidence. It is an
  implementation target, not a running product claim.

## Release and acceptance workflows

These pages are for release operators and maintainers rather than ordinary
platform use:

- [Release installation](RELEASE_INSTALLER.md)
- [Release supply-chain evidence](RELEASE_SUPPLY_CHAIN.md)
- [Role image publication](IMAGE_PUBLISHING.md)
- [Fresh deployment acceptance checklist](ACCEPTANCE_CHECKLIST.md)
- [Disposable live acceptance gate](LIVE_ACCEPTANCE.md)

## Maintainer-only plans

[Management implementation](IMPLEMENTATION_PLAN.md) and
[platform hardening](HARDENING_PLAN.md) track unfinished engineering work and
historical exit criteria. They are not operator instructions or descriptions of
available UI behavior. Transfer actionable items to issue tracking when a
project tracker is available.

Keep deployment identifiers, credentials, incident notes, approvals, and live
evidence in the private operations system, not tracked Markdown.
