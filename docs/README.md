# Documentation

This repository contains OpenStack/NixOS infrastructure, an automated
OpenStack setup command, and an unprivileged staff control surface. It is not a
self-service portal.

A deployment starts with an empty management database. The tooling creates its
own state instead of importing records from another system. Every command in
this repository creates, changes, and deletes only the resources named in your
platform configuration. Some commands list servers and images project-wide to
decide what they may touch, but nothing outside your inventory is modified.

## Learn by doing

- [Tutorial: deploy the first application](TUTORIAL.md) — deploy one app on a setup-created platform, verify managed storage and backups, and clean up.

## Complete a task

- [Automated greenfield setup](SETUP.md) — turn one protected environment file into generated keys, images, VMs, volumes, releases, and backup configuration.
- [Operator journey](OPERATIONS.md) — exact manual checkpoints, secret-file formats, foundation/ACL/role bootstrap, backups, offline restore, upgrades, cleanup, and recovery.
- [Troubleshooting](TROUBLESHOOTING.md) — diagnostic chains for common observable failures.
- [Public ingress](PUBLIC_INGRESS.md) — configure DNS, TLS, forwarding, and host preservation.
- [Release installation](RELEASE_INSTALLER.md) — install the committed management and helper releases.
- [Image publication](IMAGE_PUBLISHING.md) — publish commit-addressed role images through CI.

## Look up a contract

- [Configuration reference](CONFIGURATION.md) — private inventory and policy fields.
- [`openstack-platform` command reference](CONTROL_PLANE_CONTRACT.md) — commands, preconditions, results, destructive actions, and recovery.
- [NixOS image reference](../nix/README.md) — image outputs, tests, exact-UUID selection, and role acceptance.

## Understand the design

- [Architecture](ARCHITECTURE.md) — roles, state ownership, isolation, and failure boundaries.
- [Roadmap](ROADMAP.md) — what is implemented today and what comes next.

## Verify acceptance

- [Traceable acceptance checklist](ACCEPTANCE_CHECKLIST.md) — command-to-evidence mapping for a fresh deployment, first application, backups, restore, upgrade, and cleanup.

Keep live deployment identifiers, credentials, incident notes, and handoff
records out of tracked documentation. Store them in your deployment's private
operations system.
