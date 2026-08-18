# Prepare a fresh deployment

This page covers the local preparation stage: validate that the platform fits, create private inventory and policy, and evaluate/build a role image. It does not provision OpenStack resources. Continue with the [operator journey](OPERATIONS.md) for foundation creation and first deployment.

## Applicability and safety

Use a new OpenStack project and a new management state directory, and initialize the database empty rather than copying rows from another deployment. Every command in this repository acts only on the resources named in your platform configuration. Unrelated servers, volumes, and images that share the same OpenStack project are never inspected, changed, or deleted.

The platform fits when applications can run as one OCI container, serve HTTP on one port, and expose a health path; one OpenStack project can host the five roles and one replaceable worker per active application; and an operator can manage keys, secrets, backups, and recovery.

## Prerequisites

You need:

- an OpenStack project authorized to create servers, Neutron ports and security groups, Cinder volumes, and Glance images;
- quota for three persistent roles, disposable builders, and one worker per active application;
- Nix with flakes enabled and an `x86_64-linux` builder;
- Python 3.14 with the OpenStack SDK for foundation reconciliation, plus uv 0.12.2 for the locked repository environment;
- OpenStack credentials kept in a private mode-`0600` environment file and protected wrapper;
- an operator-controlled age identity for offline restore;
- a Cloudflare token and DNS/TLS account, or another provider meeting [the ingress contract](PUBLIC_INGRESS.md); and
- a management SSH key whose public half can be installed on the admin role.

The example inventory uses 32 GiB admin state, 200 GiB managed data, and 100 GiB backups. These are examples, not established minimums. Review the private policy for worker and managed-storage settings.

## Create private inputs

From the repository root:

```bash
cp -n config/platform.example.json config/platform.json
cp -n config/platform-policy.example.json config/platform-policy.json
```

Edit both JSON files. Replace every example project, UUID, network, address, hostname, resource name, flavor, domain, path, runtime digest, and age recipient. Keep credentials, private keys, and the real policy outside version control. The management installer accepts only the current inventory and private policy; it creates no project declaration directory.

Set the local inventory explicitly:

```bash
export PLATFORM_CONFIG="$PWD/config/platform.json"
```

Read [CONFIGURATION.md](CONFIGURATION.md) before changing names or paths. The OpenStack project name and UUID must identify the same target.

Check the result before building anything:

```bash
uv run python infra/lib/platform_config.py validate
```

This reports every missing or malformed field at once. Run it again after any
later edit: a configuration that is merely valid JSON can still be missing a
nested field, and the next symptom would otherwise be an image build failing
partway through.

## Evaluate the image definitions

```bash
uv sync --frozen
uv run python -m unittest discover -s tests -v
nix flake check --impure --no-build --print-build-logs
```

A successful check evaluates all five role configurations. The locked Python
check and flake evaluation do not contact OpenStack or prove first-boot
readiness.

## Build and test a candidate

Build one role, for example the disposable builder:

```bash
nix build --impure --print-build-logs .#builder-image
```

The `result` link contains a QCOW2 candidate. Follow [the image reference](../nix/README.md) to run static, VM, config-drive, and disposable live checks before publication. A successful Nix build is not image acceptance.

## Continue

Follow [OPERATIONS.md](OPERATIONS.md) in order. It covers foundation reconciliation, PKI and secrets, role bootstrap, public ingress, release installation, fresh database initialization, first deployment, storage, backup/restore, upgrades, cleanup, and recovery. Use [ACCEPTANCE_CHECKLIST.md](ACCEPTANCE_CHECKLIST.md) to record evidence.

To have CI build and publish role images for you instead of publishing them by
hand, set up the GitHub environment, secrets, and variable described in
[IMAGE_PUBLISHING.md](IMAGE_PUBLISHING.md). That is the only place this
repository expects deployment-specific values to be stored; your inventory and
policy stay untracked.
