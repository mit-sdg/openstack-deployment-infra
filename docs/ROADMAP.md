# Platform roadmap

## Scope

This repository contains OpenStack/NixOS role images, a restart-aware staff CLI, constrained application and managed-storage helpers, backup, recovery, and monitoring. It does not include an authenticated production API, self-service product, or turnkey installer.

A deployment is created from scratch. Reconciliation may preserve safety evidence for configured resources, but no external row is imported into the database, and only resources the inventory names are in scope.

## Current capability

| Capability | State |
| --- | --- |
| OpenStack/NixOS role definitions | Implemented; CI runs package, VM, config-drive, and image checks |
| OpenStack image publication | Implemented through manual and gated CI paths; credentials remain private |
| Disposable builder and project-worker lifecycle | Implemented behind `openstack-platform` with checkpoints and cleanup confirmation |
| Generated Bun and Node recipes | Implemented and exercised by generated-recipe smoke tests |
| Scheduler job rendering | Implemented as a constrained helper action |
| Managed-service lifecycle | Implemented per type; credentials stay out of SQLite |
| Backup and monitoring | management-database backup and managed-data backup/restore checks exist; usage collection does not |
| Production controller/API and authentication | Not implemented |
| End-user portal and GitHub App | Not implemented |
| Restart-safe reconciliation | Implemented for supported CLI mutations; no multi-user controller exists |

## Now — staff control surface

An operator with the CLI installed can:

- deploy an application from an exact Git commit, and get either a working
  HTTPS URL or a clear reason it was rejected;
- replace admin, ingress, or storage with a new image, keeping the old server
  until the new one proves healthy;
- create, verify, rotate, and remove a PostgreSQL, MongoDB, or S3 resource for
  an application without ever handling its credentials;
- set and remove application environment variables without those values
  reaching the database or command output;
- back up the management database, and check that a managed-data backup
  actually restores; and
- install a new release of the tooling without root.

Operations are resumable. If a command is interrupted, running it again picks
up from the last recorded checkpoint instead of starting over or applying the
same change twice.

There is no way to import state from another system, and nothing reads or
writes resources the inventory does not name.

## 1. Identity, roster, and GitHub App

- institution-supported authentication;
- student, staff, team, and project authorization;
- roster reconciliation;
- GitHub App repository selection and signed webhook handling; and
- short-lived, repository-scoped source access.

Exit criteria: an authorized user can connect only an allowed repository/project; each delivery is idempotent per commit; revoked access blocks new work without deleting retained project state.

## 2. End-to-end orchestration

- allowlisted deployment-file validation;
- exact source acquisition and immutable publication;
- worker reconciliation, health observation, bounded logs, cancellation, timeout, retry, cleanup, and rollback; and
- revision retention and registry-manifest reference tracking.

Exit criteria: a reviewed exact commit reaches a healthy HTTPS endpoint without operator intervention; failure leaves disposable compute deleted and diagnosable state; restart during any phase converges to a defined result.

## 3. Managed services

- controller operations for provision, attach, rotate, detach, and delete;
- encrypted scoped credentials;
- usage collection and quota policy;
- partial-failure journals; and
- backup catalogs linked to managed resources.

## 4. Production portal

- project/team dashboard;
- repository connection and deployment progress;
- bounded logs and revision history;
- managed-service status, usage, rotation, and deletion; and
- accessible asynchronous recovery states.

## 5. Reliability and security

- threat-model and privilege-boundary review;
- secret-redaction, builder isolation, and workload isolation tests;
- class-scale concurrency and quota tests;
- provider/dependency fault injection; and
- backup restoration and persistent-role recovery drills.

## 6. Rollout

1. validate a private reference application end to end;
2. run a staff-only pilot;
3. run a limited student pilot;
4. resolve authorization and recovery findings;
5. publish student-facing documentation; and
6. expand only after rollback and restore drills pass.

The [acceptance checklist](ACCEPTANCE_CHECKLIST.md) is the evidence boundary for a fresh deployment.
