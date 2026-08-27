# A small application platform for OpenStack

[![CI](https://github.com/mit-sdg/openstack-deployment-infra/actions/workflows/ci.yml/badge.svg)](https://github.com/mit-sdg/openstack-deployment-infra/actions/workflows/ci.yml)

This platform hosts web applications without giving users cloud credentials or
server access. Each application gets a stable HTTPS URL, health-checked
deployments, and optional managed data services.

A class can give each student a project and create shared projects for teams.
A hackathon or research group can host many small applications in one OpenStack
project. In both cases, organizers keep the infrastructure credentials, and
databases survive worker replacement.

Applications can use multiple named PostgreSQL, MongoDB, and S3-compatible
resources. A narrow `platform.yaml` binding maps selected outputs from an
existing named resource to runtime environment keys; repositories cannot
provision resources or supply arbitrary platform-controlled environment. The
platform also runs a private image registry, encrypted backups,
restore checks, and health monitoring.

## Current status

The infrastructure, operator tools, and greenfield setup command are
implemented. A protected environment file plus interactive answers for any
missing deployment choices can create the keys, images, network foundation,
VMs, volumes, management bridge, releases, and backup configuration.
Cloudflare or another public-ingress account remains an external input.

The self-service portal and production API are not implemented. This repository
is useful today for an operator-managed platform or as a base for another
control plane; it is not yet a service where end users sign in and deploy
applications themselves. The accepted
[self-hosted management implementation plan](docs/IMPLEMENTATION_PLAN.md)
defines the replacement UI, controller API, state migration, and CLI reduction.

## What it provides

A deployment:

- builds an exact source commit in a single-use builder;
- publishes the resulting container image by immutable digest;
- runs each active application project on a dedicated, replaceable worker;
- routes public traffic only to healthy applications;
- keeps managed data separate from application workers;
- supports scoped, rotatable database and object-storage credentials;
- cleans up failed deployment candidates without rebuilding source; and
- takes encrypted backups of managed data and control-plane state, with
  restore verification.

Participants receive no SSH keys, OpenStack or scheduler credentials, registry
credentials, or database administrator passwords.

Applications currently run as one OCI container, serve HTTP on one port, and
provide a health path. The current scheduler configuration runs one application
instance per project. Periodic managed-storage usage collection is not
implemented.

Source must come from a **public** GitHub repository. Deployments fetch the
exact commit over HTTPS with no credentials, so a private repository cannot be
deployed. Private repositories remain outside the initial self-service target.

Every command acts only on the resources your configuration names, so unrelated
servers and volumes in the same OpenStack project are left alone.

## Set up your own deployment

The platform is not specific to any one institution or course. To run it on
your own OpenStack project:

1. [Automated setup](docs/SETUP.md) — create a protected environment file,
   review the plan, then generate and verify the complete platform.
2. [Tutorial](docs/TUTORIAL.md) — deploy a verified HTTPS application and a
   managed database, then clean them up.
3. [Operations](docs/OPERATIONS.md) — back up, restore, upgrade, troubleshoot,
   or execute an individual setup checkpoint manually.
4. [Image publication](docs/IMAGE_PUBLISHING.md) — understand the image
   acceptance contract or configure protected CI publication.

Automated setup writes the private source inventory below its protected
workspace and installs it at `/srv/openstack-platform/config/platform.json`.
`config/platform.example.json` remains the tracked template documenting every
field; the [configuration reference](docs/CONFIGURATION.md) explains each one.

## Designed to be extended

Small role-specific services and narrow operator commands make up the current
platform. The accepted implementation plan extracts application orchestration
behind a typed local controller API, runs the management application on admin,
and leaves the CLI as an infrastructure and recovery surface.

Cloudflare Tunnel is the reference public-ingress setup, but it is not required.
An institutional or managed ingress service can be used instead, provided it
meets the [public ingress contract](docs/PUBLIC_INGRESS.md). PostgreSQL,
MongoDB, and S3-compatible storage are included; additional managed services
can be added by implementing their provisioning, credential, health, backup,
and cleanup lifecycle.

## How it fits together

![Platform architecture](docs/architecture-overview.svg)

| Role | Responsibility |
| --- | --- |
| `admin` | Runs the scheduler control plane, constrained operator helper, and monitoring |
| `ingress` | Routes public requests to healthy applications |
| `storage` | Runs PostgreSQL, MongoDB, object storage, and the private image registry |
| `worker` | Runs one application project without durable local state |
| `builder` | Builds one source snapshot, then is deleted |

The [architecture guide](docs/ARCHITECTURE.md) explains the isolation, data, and
failure boundaries.

## Documentation

- [Automated setup](docs/SETUP.md) — create a complete greenfield deployment
  from one protected environment file
- [Getting started](docs/GETTING_STARTED.md) — applicability and management-host prerequisites
- [Tutorial](docs/TUTORIAL.md) — deploy the first application and managed service
- [Operations](docs/OPERATIONS.md) — deployment, backup, restore, and cleanup
  procedures
- [Troubleshooting](docs/TROUBLESHOOTING.md) — symptom, evidence, correction
- [Configuration reference](docs/CONFIGURATION.md) — every deployment setting
- [Image publication](docs/IMAGE_PUBLISHING.md) — local and CI publication
- [Public ingress](docs/PUBLIC_INGRESS.md) — provider-neutral DNS and HTTPS
  requirements
- [`openstack-platform` commands](docs/CONTROL_PLANE_CONTRACT.md) — the
  installed staff interface for infrastructure, applications, storage, and
  recovery
- [Self-hosted management implementation plan](docs/IMPLEMENTATION_PLAN.md) —
  accepted target architecture, migration phases, API boundary, and acceptance
  criteria
- [Documentation index](docs/README.md) — all guides and references

## License

Licensed under the [Apache License 2.0](LICENSE).
