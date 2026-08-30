# A small application platform for OpenStack

This repository builds and operates NixOS infrastructure for hosting small HTTP
applications in one OpenStack project. It provides automated greenfield setup,
five role images, PostgreSQL/MongoDB/S3 services, encrypted recovery evidence,
an operator-only infrastructure CLI, and a local application controller API.

## Current status

Infrastructure setup, operation, backup, restore, and the local controller are
implemented. There is not yet a supported end-user application workflow: the
sync-engine management application and its external authentication application
do not exist. Do not install an older release to recover the retired
application/storage CLI or repository deployment manifest.

The current supported result is an operator-managed deployment with:

- persistent `admin`, `ingress`, and `storage` hosts;
- replaceable workers and single-use builders;
- exact-image and persistent-host lifecycle controls;
- separate hosted-controller, operator-state, and managed-data backups,
  including retained OCI artifacts and off-site export/import; and
- a controller on capability-separated local Unix sockets for future management
  integration.

End users receive no SSH, OpenStack, Nomad, registry, or database-administrator
credentials. Future application input is restricted to public,
credential-free GitHub source and typed Node or Bun configuration. Private
repositories and arbitrary build commands are outside the current contract.

## Create the first deployment

Use an unprivileged account on an `x86_64-linux` host. It must own a mode-`0700`
`/srv/openstack-platform` and have Nix with flakes, Git, OpenSSH, OpenSSL,
`curl`, a user systemd manager, and network access to OpenStack, GitHub, the Nix
cache, OCI registries, and the three persistent-role addresses. Setup refuses
root and `sudo`.

Start from a clean full-commit checkout:

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -q
nix flake check --no-build --print-build-logs
```

Create the direct mode-`0600` OpenStack/setup environment described in
[Automated setup](docs/SETUP.md). It names the target project, fixed addresses,
flavors, volume type, public domain, ingress policy, and signed release
evidence. Then run the read-only preflight:

```bash
uv run openstack-platform setup check --env-file /private/path/setup.env
```

Continue only when it reports `setup-check=ready`. Apply the reviewed plan:

```bash
uv run openstack-platform setup \
  --env-file /private/path/setup.env \
  --cloudflare-token-file /private/path/cloudflare-tunnel-token \
  --apply
```

Omit the token option when another provider satisfies the
[public ingress contract](docs/PUBLIC_INGRESS.md). Successful setup ends with
`setup=complete`, starts and verifies the hosted controller, selects five role
images, enables backup timers, and reports either configured or pending public
ingress.

Follow [Verify a fresh platform](docs/TUTORIAL.md) to check the installed CLI,
controller, public route, all three backup classes, and scheduled jobs.

## Control boundaries

`openstack-platform` manages setup, status, images, persistent hosts, external
operator-state backup, and offline restore. It has no product commands.

`openstack-platform-controller` owns application, deployment, environment,
storage, and operation state over separate project and privileged Unix sockets.
The future `management-broker`, not the browser renderer, is the only management
peer accepted on the project socket. Browser authentication, ownership, quota,
and CSRF enforcement remain management-application responsibilities.

See [Architecture and trust boundaries](docs/ARCHITECTURE.md) for the model and
[Operator CLI and controller API](docs/CONTROL_PLANE_CONTRACT.md) for the exact
interfaces.

## Documentation

- [Automated setup](docs/SETUP.md)
- [Fresh-platform verification](docs/TUTORIAL.md)
- [Operations and disaster recovery](docs/OPERATIONS.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Configuration reference](docs/CONFIGURATION.md)
- [Operator CLI and controller API reference](docs/CONTROL_PLANE_CONTRACT.md)
- [Architecture and trust boundaries](docs/ARCHITECTURE.md)
- [Management application specification](docs/MANAGEMENT_APP_SPEC.md)
- [Documentation index](docs/README.md)

Release, image, acceptance, and maintainer documents are indexed separately in
[`docs/README.md`](docs/README.md).

## Code organization

- `openstack_platform/controller/` owns product state and lifecycle services.
- `openstack_platform/helper/` is the constrained action boundary.
- `openstack_platform/operator.py` adapts the operator CLI.
- `infra/lib/platform_contract.json` is the shared role, account, port, path,
  and protocol contract consumed by Python, Nix, and infrastructure scripts.

## License

Licensed under the [Apache License 2.0](LICENSE).
