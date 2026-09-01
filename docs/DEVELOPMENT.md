# Validate a repository change

Use this workflow to create the locked Python environment and run the checks
that can execute without OpenStack credentials. Python commands require Python
3.14; Nix outputs support `x86_64-linux`.

Deployment setup is a separate operator procedure. Follow [Deploy the
platform](DEPLOYMENT.md) instead of using development commands to provision
infrastructure.

## Create the Python environment

From a clean repository checkout with [uv](https://docs.astral.sh/uv/)
installed:

```sh
uv sync --frozen
uv run python --version
```

The reported Python version must be 3.14.x. `uv.lock` is authoritative for the
resolved Python environment; change `pyproject.toml` first and regenerate the
lock when intentionally changing dependencies.

## Run the Python checks

Run the unit and integration-style test suite:

```sh
uv run python -m unittest discover -s tests -q
```

Run the static checks used by CI:

```sh
uv lock --check
uv run ruff format --check openstack_platform deploy/releases infra tests
uv run ruff check openstack_platform deploy/releases infra tests
uv run mypy
uv run vulture openstack_platform deploy infra tests --min-confidence 80 --sort-by-size
uv run python -m compileall -q openstack_platform deploy infra tests
```

Vulture succeeds silently when it finds no code at or above the configured
confidence threshold.

## Run shell and document checks

Bash syntax can be checked without provider credentials:

```sh
find deploy infra tests -type f -name '*.sh' -exec bash -n {} +
```

CI also runs ShellCheck and validates every example JSON file plus
`docs/architecture-overview.svg`. The documentation tests check relative links,
current control-surface claims, and the tracked-file index:

```sh
uv run python -m unittest tests.test_documentation -v
```

## Run Nix checks

A Linux host with flakes enabled and access to the Nix daemon can evaluate the
flake without building its outputs:

```sh
nix flake check --no-build --print-build-logs
```

Run the repository formatter separately and review any resulting changes:

```sh
nix fmt -- .
git diff -- '*.nix'
```

Building images, running role VM tests, and smoke-testing QCOW2 files require
additional Nix/QEMU resources. Follow [Build and test role
images](MAINTENANCE.md#build-and-test-role-images) for those workflows.

## Understand the test layout

Tests use only the standard-library `unittest` runner. Test modules generally
mirror a production boundary rather than one source file. For example,
`test_platform_storage.py` spans controller and helper storage behavior, while
`test_platform_foundation_runtime_remote.py` covers local runtime, protocol,
and backup-acceptance foundations. The [tracked-file
guide](REPOSITORY_GUIDE.md#tests) states the scope of each fixture, smoke script,
and test module.

Tests that need clean-repository behavior copy the current source into a
temporary Git repository through `tests/repository_fixtures.py`. You therefore
do not need to commit a change before running the suite; production release and
setup commands still enforce their own clean-checkout requirements.
