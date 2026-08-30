# A small application platform for OpenStack

This repository builds and operates the OpenStack infrastructure for hosting
small HTTP applications. It provides NixOS role images, greenfield setup,
persistent data services, encrypted backups, a constrained helper, an
operator-only infrastructure CLI, and a local application controller API.

## Current status

The infrastructure, setup, backup, restore, and infrastructure-operator paths
are implemented. The repository also contains the typed local controller API
and application lifecycle services that a future management application will
call.

There is not yet a supported end-user application workflow. The sync-engine
management application and its external authentication application do not exist
in this repository. The admin role does run the controller API on a restricted
local socket once its policy and helper release are installed. The former
application/storage CLI and repository deployment manifest have been retired.
Do not use an older `openstack-platform` binary or old instructions to manage
product state.

The future browser product is specified in the
[management application specification](docs/MANAGEMENT_APP_SPEC.md). That
specification is an implementation target, not a claim that the UI or
authentication service is available today.

## Implemented infrastructure

A deployment provides:

- persistent `admin`, `ingress`, and `storage` roles;
- replaceable application workers and single-use builders for the controller;
- PostgreSQL, MongoDB, S3-compatible storage, and a private OCI registry;
- provider-scoped host lifecycle and exact-image selection;
- public DNS/HTTPS integration through Cloudflare Tunnel or another provider;
- separate encrypted hosted-controller, operator-state, and managed-data backups,
  including retained OCI recovery artifacts and provider-neutral off-site export/import; and
- a local Unix-socket controller contract for future product integration.

Participants receive no SSH keys, OpenStack or scheduler credentials, registry
credentials, or database administrator passwords. Application source remains
limited to public, credential-free GitHub repositories and typed Node or Bun
configuration when the future management application is implemented. Private
repositories are outside the current target.

Every operator command acts only on resources named by the deployment
configuration, leaving unrelated resources in the OpenStack project alone.

## Set up a deployment

To create the infrastructure in your own OpenStack project:

1. [Prepare the operator host](docs/GETTING_STARTED.md).
2. [Run automated setup](docs/SETUP.md) from a protected environment file.
3. [Verify the fresh platform](docs/TUTORIAL.md).
4. Use [Operations](docs/OPERATIONS.md) for backup, restore, upgrades, recovery,
   and individual setup checkpoints.

Automated setup writes its private source inventory below the protected
workspace and installs it at
`/srv/openstack-platform/config/platform.json`. The tracked
`config/platform.example.json` documents the shape; the
[configuration reference](docs/CONFIGURATION.md) explains every field.

Setup requires an external public-ingress account. Cloudflare Tunnel is the
reference, but an institutional service can be used when it satisfies the
[public ingress contract](docs/PUBLIC_INGRESS.md).

## Operator and controller boundaries

`openstack-platform` is now an operator-only command. It supports setup,
platform status, external operator-state backup/offline restore, image selection
and pruning, and persistent-host lifecycle. It does not expose application,
environment, deployment, or managed-storage commands. See the
[operator CLI and local controller reference](docs/CONTROL_PLANE_CONTRACT.md).

The controller accepts bounded HTTP/1.1 JSON on separate project and privileged
Unix sockets and owns application, deployment, environment, storage, operation,
and safe administrator-read routes. Exact Linux peer credentials and route sets
enforce each host capability. The future trusted `management-broker`—not the
browser-facing renderer—is the only management peer allowed on the project
socket; browser authorization and project ownership remain broker obligations. Future UI and authentication behavior belongs in
[`docs/MANAGEMENT_APP_SPEC.md`](docs/MANAGEMENT_APP_SPEC.md).

## Code organization

The Python distribution is `openstack_platform`, not a CLI-named package:

- `openstack_platform/operator.py` contains only the operator command adapter;
- `openstack_platform/controller/` owns product state, lifecycle services, and
  the local API, with application value objects and Nomad rendering separated
  from deployment orchestration;
- `openstack_platform/helper/` is the constrained action boundary; and
- `openstack_platform/contracts.py` provides typed access to the shared
  implementation contract; and
- the remaining top-level modules provide shared configuration, provider,
  installation, and runtime support.

Cross-component roles, ports, accounts, protocol names, and required inventory
paths are defined once in `infra/lib/platform_contract.json`. Python packages,
standalone infrastructure scripts, and Nix load that same contract. Nix
inventory validation lives in `nix/lib/inventory.nix`; role modules consume the
validated inventory rather than redefining deployment identity.

## Architecture

![Platform architecture](docs/architecture-overview.svg)

| Role | Responsibility |
| --- | --- |
| `admin` | Runs the scheduler control plane, local controller, constrained helper, monitoring, and backup staging |
| `ingress` | Routes public requests to healthy platform services |
| `storage` | Runs PostgreSQL, MongoDB, object storage, and the private image registry |
| `worker` | Runs one application project without durable local state |
| `builder` | Builds one source snapshot, then is deleted |

The [architecture guide](docs/ARCHITECTURE.md) explains state ownership,
isolation, and failure boundaries.

## Documentation

- [Automated setup](docs/SETUP.md)
- [Getting started](docs/GETTING_STARTED.md)
- [Fresh-platform tutorial](docs/TUTORIAL.md)
- [Operations](docs/OPERATIONS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Image publication](docs/IMAGE_PUBLISHING.md)
- [Public ingress](docs/PUBLIC_INGRESS.md)
- [Operator CLI and local controller API](docs/CONTROL_PLANE_CONTRACT.md)
- [Management application specification](docs/MANAGEMENT_APP_SPEC.md)
- [Platform hardening plan](docs/HARDENING_PLAN.md)
- [Documentation index](docs/README.md)

## License

Licensed under the [Apache License 2.0](LICENSE).
