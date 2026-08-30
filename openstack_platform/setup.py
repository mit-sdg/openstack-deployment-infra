"""Greenfield setup orchestration for an OpenStack platform deployment.

The setup command turns one protected environment file into the private
inventory, generated credentials, role images, and persistent platform roles.
It deliberately delegates provider mutations to the repository's existing,
role-specific scripts instead of introducing a second lifecycle implementation.
"""

from __future__ import annotations

import getpass
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO
from uuid import UUID

from . import durable
from .config import load_platform, load_policy
from .contracts import IMAGE_ROLES, OPERATOR_SSH_ALIAS, PERSISTENT_ROLES
from .installation import OPERATOR_ROOT

_ENV_ASSIGNMENT = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]")
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")


class SetupError(RuntimeError):
    """The greenfield setup cannot safely continue."""


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    project_name: str


@dataclass(frozen=True, slots=True)
class SetupPaths:
    repository: Path
    workspace: Path
    platform: Path
    policy: Path
    bootstrap: Path
    pki: Path
    openstack_environment: Path
    openstack_wrapper: Path
    ssh_directory: Path


def _fail(message: str) -> NoReturn:
    raise SetupError(message)


def _direct_private_file(path: Path, *, field: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise SetupError(f"{field} is missing") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        _fail(f"{field} must be a direct current-user-owned mode-0600 file")
    return metadata


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        _fail(f"private setup directory is unsafe: {path}")
    path.chmod(0o700)


def _atomic_private_write(path: Path, content: str) -> None:
    _private_directory(path.parent)
    try:
        durable.atomic_write(
            path,
            content.encode("utf-8"),
            mode=0o600,
            maximum_bytes=1_048_576,
        )
    except durable.DurableReplaceError as error:
        raise SetupError("configuration could not be installed durably") from error


def load_environment_file(path: Path) -> dict[str, str]:
    """Read literal dotenv/OpenRC assignments without executing the file."""
    metadata = _direct_private_file(path, field="setup environment file")
    if metadata.st_size > 1_048_576:
        _fail("setup environment file exceeds its 1048576-byte limit")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise SetupError("setup environment file must be UTF-8") from error
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("#!"):
            continue
        match = _ENV_ASSIGNMENT.fullmatch(line)
        if match is None:
            # Horizon OpenRC files contain prompts and conditionals. They are
            # ignored; the resulting missing password is requested explicitly.
            continue
        name, encoded = match.groups()
        try:
            fields = shlex.split(encoded, comments=True, posix=True)
        except ValueError as error:
            raise SetupError(f"malformed assignment on environment-file line {number}") from error
        if len(fields) != 1:
            raise SetupError(f"assignment on environment-file line {number} is not literal")
        value = fields[0]
        quoted = encoded.strip().startswith(("'", '"'))
        if not quoted and ("$" in value or "`" in value or "$(" in value):
            # Never evaluate unquoted shell expansion from a credential file.
            continue
        if name in values:
            _fail(f"duplicate environment assignment: {name}")
        values[name] = value
    return values


def _command(
    argv: Sequence[str | Path],
    *,
    environment: Mapping[str, str],
    cwd: Path | None = None,
    capture: bool = False,
    stdin: bytes | None = None,
    timeout: int = 7_200,
) -> str:
    encoded = tuple(str(item) for item in argv)
    try:
        result = subprocess.run(
            encoded,
            cwd=cwd,
            env=dict(environment),
            input=stdin,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError(f"setup dependency failed to execute: {Path(encoded[0]).name}") from error
    if result.returncode != 0:
        detail = ""
        if capture and result.stderr:
            detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:300]
        suffix = f": {detail}" if detail else ""
        raise SetupError(f"setup command failed ({Path(encoded[0]).name}){suffix}")
    if not capture:
        return ""
    return result.stdout.decode("utf-8", "strict")


def _json_command(
    argv: Sequence[str | Path], *, environment: Mapping[str, str], cwd: Path | None = None
) -> Any:
    raw = _command(argv, environment=environment, cwd=cwd, capture=True)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise SetupError(f"{Path(str(argv[0])).name} returned malformed JSON") from error


def _repository_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if not (root / "flake.nix").is_file() or not (root / "infra").is_dir():
        _fail("setup must run from a complete platform source release")
    return root


def _source_commit(repository: Path, environment: Mapping[str, str]) -> str:
    supplied = environment.get("PLATFORM_SOURCE_COMMIT")
    if supplied:
        if not _FULL_COMMIT.fullmatch(supplied):
            _fail("PLATFORM_SOURCE_COMMIT must be a full lowercase commit")
        return supplied
    git = shutil.which("git")
    if git is None or not (repository / ".git").exists():
        # Installed releases are named by their complete commit.
        for parent in repository.parents:
            if _FULL_COMMIT.fullmatch(parent.name) and (parent / ".complete").is_file():
                if (parent / ".complete").read_text(encoding="utf-8").strip() == parent.name:
                    return parent.name
        _fail("setup source commit is unavailable")
    commit = _command(
        (git, "rev-parse", "HEAD"), environment=environment, cwd=repository, capture=True
    ).strip()
    if not _FULL_COMMIT.fullmatch(commit):
        _fail("setup source commit is malformed")
    dirty = subprocess.run(
        (git, "status", "--porcelain", "--untracked-files=normal"),
        cwd=repository,
        env=dict(environment),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if dirty.returncode != 0 or dirty.stdout:
        _fail("setup requires a clean full-commit source checkout")
    return commit


def _build_nix_output(
    repository: Path,
    environment: Mapping[str, str],
    attribute: str,
    *,
    platform: Path | None = None,
) -> Path:
    """Build one flake output using the exact conventional attribute syntax."""
    nix = shutil.which("nix")
    if nix is None:
        _fail("Nix is required to build setup tooling and role images")
    child = dict(environment)
    if platform is not None:
        child["PLATFORM_CONFIG"] = str(platform)
    output = _command(
        (
            nix,
            "--extra-experimental-features",
            "nix-command flakes",
            "build",
            "--impure",
            "--no-link",
            "--print-out-paths",
            f".#{attribute}",
        ),
        environment=child,
        cwd=repository,
        capture=True,
    ).strip()
    path = Path(output.splitlines()[-1]) if output else Path()
    if not path.is_absolute() or not path.exists():
        _fail(f"Nix did not produce .#{attribute}")
    return path


def _openstack_environment(values: Mapping[str, str]) -> dict[str, str]:
    child = os.environ.copy()
    child.update({key: value for key, value in values.items() if key.startswith("OS_")})
    child.setdefault("OS_AUTH_TYPE", "password")
    child.setdefault("OS_USER_DOMAIN_NAME", "Default")
    child.setdefault("OS_PROJECT_DOMAIN_NAME", "Default")
    child.setdefault("OS_IDENTITY_API_VERSION", "3")
    child.setdefault("OS_INTERFACE", "public")
    return child


def _credential_requirements(
    values: dict[str, str],
    input_reader: Callable[[str], str],
    secret_reader: Callable[[str], str],
) -> None:
    def required_text(name: str, label: str) -> None:
        if values.get(name):
            return
        answer = input_reader(f"{label}: ").strip()
        if answer:
            values[name] = answer

    required_text("OS_AUTH_URL", "OpenStack identity URL")
    required_text("OS_PROJECT_NAME", "OpenStack project name")
    auth_type = values.get("OS_AUTH_TYPE", "password")
    if "applicationcredential" in auth_type:
        required_text("OS_APPLICATION_CREDENTIAL_ID", "OpenStack application credential ID")
        if not values.get("OS_APPLICATION_CREDENTIAL_SECRET"):
            secret = secret_reader("OpenStack application credential secret: ")
            if secret:
                values["OS_APPLICATION_CREDENTIAL_SECRET"] = secret
        required = (
            "OS_AUTH_URL",
            "OS_PROJECT_NAME",
            "OS_APPLICATION_CREDENTIAL_ID",
            "OS_APPLICATION_CREDENTIAL_SECRET",
        )
    else:
        required_text("OS_USERNAME", "OpenStack username")
        if not values.get("OS_PASSWORD"):
            password = secret_reader("OpenStack password: ")
            if password:
                values["OS_PASSWORD"] = password
        required = ("OS_AUTH_URL", "OS_PROJECT_NAME", "OS_USERNAME", "OS_PASSWORD")
    missing = [name for name in required if not values.get(name)]
    if missing:
        _fail("setup environment is missing: " + ", ".join(missing))


def _project_identity(openstack: Path, environment: Mapping[str, str]) -> ProjectIdentity:
    raw_id = _command(
        (openstack, "token", "issue", "-f", "value", "-c", "project_id"),
        environment=environment,
        capture=True,
    ).strip()
    try:
        project_id = str(UUID(raw_id))
    except (ValueError, AttributeError) as error:
        raise SetupError("authenticated OpenStack project UUID is unavailable") from error
    configured_id = environment.get("OS_PROJECT_ID") or environment.get("OS_TENANT_ID")
    if configured_id:
        try:
            canonical_configured = str(UUID(configured_id))
        except (ValueError, AttributeError) as error:
            raise SetupError("configured OpenStack project UUID is malformed") from error
        if canonical_configured != project_id:
            _fail("authenticated OpenStack project does not match the configured project UUID")
    project_name = environment.get("OS_PROJECT_NAME")
    if not project_name:
        _fail("authenticated OpenStack project name is unavailable")
    return ProjectIdentity(project_id, project_name)


def _prompt(
    values: dict[str, str],
    name: str,
    label: str,
    *,
    default: str | None,
    input_reader: Callable[[str], str],
) -> str:
    current = values.get(name)
    if current:
        return current
    suffix = f" [{default}]" if default else ""
    answer = input_reader(f"{label}{suffix}: ").strip()
    selected = answer or default
    if not selected:
        _fail(f"setup requires {name}")
    values[name] = selected
    return selected


def _slug_default(value: str) -> str:
    selected = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:32].strip("-")
    if len(selected) < 3:
        selected = f"app-{selected}".strip("-")
    return selected


def _network_default(
    openstack: Path, environment: Mapping[str, str], values: Mapping[str, str]
) -> str | None:
    if values.get("PLATFORM_NETWORK"):
        return values["PLATFORM_NETWORK"]
    rows = _json_command(
        (openstack, "network", "list", "-f", "json", "-c", "Name"),
        environment=environment,
    )
    if not isinstance(rows, list):
        _fail("OpenStack network inventory is malformed")
    names = sorted(
        str(row.get("Name")) for row in rows if isinstance(row, dict) and row.get("Name")
    )
    return names[0] if len(names) == 1 else None


def _flavor_inventory(openstack: Path, environment: Mapping[str, str]) -> list[dict[str, Any]]:
    rows = _json_command(
        (openstack, "flavor", "list", "--long", "-f", "json"), environment=environment
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _fail("OpenStack flavor inventory is malformed")
    return rows


def _field(row: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _select_flavor(rows: Sequence[Mapping[str, Any]], vcpus: int, memory_mib: int) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for row in rows:
        try:
            name = str(_field(row, "Name"))
            cpu = int(_field(row, "VCPUs", "vcpus"))
            memory = int(_field(row, "RAM", "ram"))
        except (TypeError, ValueError):
            continue
        if cpu >= vcpus and memory >= memory_mib:
            candidates.append((memory, cpu, name))
    return min(candidates)[2] if candidates else None


def _volume_type_default(openstack: Path, environment: Mapping[str, str]) -> str | None:
    rows = _json_command(
        (openstack, "volume", "type", "list", "-f", "json", "-c", "Name"),
        environment=environment,
    )
    if not isinstance(rows, list):
        _fail("OpenStack volume-type inventory is malformed")
    names = sorted(
        str(row.get("Name")) for row in rows if isinstance(row, dict) and row.get("Name")
    )
    if "production" in names:
        return "production"
    return names[0] if len(names) == 1 else None


def _filesystem_label(namespace: str, suffix: str) -> str:
    candidate = f"{namespace}-{suffix}"
    if len(candidate.encode()) <= 12:
        return candidate
    digest = hashlib.sha256(namespace.encode()).hexdigest()[:4]
    budget = 12 - len(suffix) - len(digest) - 2
    stem = namespace[:budget].rstrip("-")
    return f"{stem}-{suffix}-{digest}"


def _positive_integer(value: str, *, field: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise SetupError(f"{field} must be an integer") from error
    if not 1 <= parsed <= 16_384:
        _fail(f"{field} must be from 1 through 16384 GiB")
    return parsed


def _platform_document(
    repository: Path,
    values: dict[str, str],
    project: ProjectIdentity,
    commit: str,
    openstack: Path,
    provider_environment: Mapping[str, str],
    input_reader: Callable[[str], str],
) -> dict[str, Any]:
    loaded = json.loads((repository / "config/platform.example.json").read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        _fail("tracked platform template is malformed")
    base: dict[str, Any] = loaded
    prefix = _prompt(
        values,
        "PLATFORM_PREFIX",
        "OpenStack resource prefix",
        default=_slug_default(project.project_name),
        input_reader=input_reader,
    )
    namespace = _prompt(
        values,
        "PLATFORM_NAMESPACE",
        "Internal namespace",
        default=prefix,
        input_reader=input_reader,
    )
    if not _SAFE_NAME.fullmatch(namespace):
        _fail("PLATFORM_NAMESPACE must be 3-32 lowercase letters, numbers, or hyphens")
    display = _prompt(
        values,
        "PLATFORM_DISPLAY_NAME",
        "Platform display name",
        default=project.project_name,
        input_reader=input_reader,
    )
    organization = _prompt(
        values,
        "PLATFORM_ORGANIZATION",
        "Certificate organization",
        default=display,
        input_reader=input_reader,
    )
    domain = _prompt(
        values,
        "PLATFORM_DOMAIN",
        "Public base domain",
        default=None,
        input_reader=input_reader,
    )
    network = _prompt(
        values,
        "PLATFORM_NETWORK",
        "OpenStack network",
        default=_network_default(openstack, provider_environment, values),
        input_reader=input_reader,
    )
    ssh_connection = os.environ.get("SSH_CONNECTION", "").split()
    operator_default = f"{ssh_connection[0]}/32" if ssh_connection else None
    operator_cidr = _prompt(
        values,
        "PLATFORM_OPERATOR_CIDR",
        "Operator source CIDR",
        default=operator_default,
        input_reader=input_reader,
    )
    try:
        ipaddress.ip_network(operator_cidr)
    except ValueError as error:
        raise SetupError("PLATFORM_OPERATOR_CIDR is malformed") from error
    addresses: dict[str, str] = {}
    for role in PERSISTENT_ROLES:
        value = _prompt(
            values,
            f"PLATFORM_{role.upper()}_ADDRESS",
            f"Fixed {role} IPv4 address",
            default=None,
            input_reader=input_reader,
        )
        try:
            addresses[role] = str(ipaddress.IPv4Address(value))
        except ValueError as error:
            raise SetupError(f"PLATFORM_{role.upper()}_ADDRESS is malformed") from error
    flavors = _flavor_inventory(openstack, provider_environment)
    requirements = {
        "admin": (2, 4096),
        "ingress": (2, 2048),
        "storage": (4, 8192),
        "worker": (1, 4096),
        "builder": (4, 8192),
    }
    selected_flavors: dict[str, str] = {}
    for role, requirement in requirements.items():
        selected_flavors[role] = _prompt(
            values,
            f"PLATFORM_{role.upper()}_FLAVOR",
            f"OpenStack {role} flavor",
            default=_select_flavor(flavors, *requirement),
            input_reader=input_reader,
        )
    volume_type = _prompt(
        values,
        "PLATFORM_VOLUME_TYPE",
        "Cinder volume type",
        default=_volume_type_default(openstack, provider_environment),
        input_reader=input_reader,
    )
    admin_gib = _positive_integer(values.get("PLATFORM_ADMIN_STATE_GIB", "32"), field="admin state")
    data_gib = _positive_integer(values.get("PLATFORM_DATA_GIB", "500"), field="managed data")
    backup_gib = _positive_integer(values.get("PLATFORM_BACKUP_GIB", "200"), field="backup")
    names = {role: f"{prefix}-{role}-01" for role in PERSISTENT_ROLES}
    ports = {
        "admin": f"{prefix}-admin-public-v4",
        "ingress": f"{prefix}-ingress-public-v4",
        "storage": f"{prefix}-storage-service-v4",
    }
    image_suffix = commit[:8]
    recovery_raw = values.get("PLATFORM_RECOVERY_DOMAINS")
    recovery_domains = (
        [item.strip() for item in recovery_raw.split(",") if item.strip()]
        if recovery_raw
        else [f"projects.{domain}", f"compute.{domain}"]
    )
    if len(recovery_domains) != 2:
        _fail("PLATFORM_RECOVERY_DOMAINS must contain exactly two comma-separated hostnames")
    static_routes_raw = values.get("PLATFORM_STATIC_INGRESS_ROUTES_JSON", "{}")
    try:
        static_routes = json.loads(static_routes_raw)
    except json.JSONDecodeError as error:
        raise SetupError("PLATFORM_STATIC_INGRESS_ROUTES_JSON is malformed") from error
    if not isinstance(static_routes, dict):
        _fail("PLATFORM_STATIC_INGRESS_ROUTES_JSON must be a JSON object")
    base.update(
        {
            "project": project.project_name,
            "projectId": project.project_id,
            "displayName": display,
            "organization": organization,
            "prefix": prefix,
            "namespace": namespace,
            "domain": domain,
            "recoveryDomains": recovery_domains,
            "staticIngressRoutes": static_routes,
            "datacenter": values.get("PLATFORM_DATACENTER", namespace),
            "region": values.get("PLATFORM_REGION", "global"),
            "network": network,
            "internalNames": {
                "storage": values.get(
                    "PLATFORM_STORAGE_INTERNAL_NAME", f"storage.{prefix}.internal"
                ),
                "objectStorage": values.get(
                    "PLATFORM_OBJECT_STORAGE_INTERNAL_NAME", f"s3.{prefix}.internal"
                ),
            },
            "pki": {"internalCaFile": f"{namespace}-internal-ca.pem"},
            "operatorCidr": operator_cidr,
            "addresses": addresses,
            "hosts": names,
            "ports": ports,
            "volumes": {
                "adminState": {
                    "name": f"{prefix}-admin-state",
                    "label": _filesystem_label(namespace, "state"),
                    "sizeGiB": admin_gib,
                    "type": volume_type,
                },
                "backup": {
                    "name": f"{prefix}-backups",
                    "label": _filesystem_label(namespace, "bak"),
                    "sizeGiB": backup_gib,
                    "type": volume_type,
                },
                "data": {
                    "name": f"{prefix}-data",
                    "label": _filesystem_label(namespace, "data"),
                    "sizeGiB": data_gib,
                    "type": volume_type,
                },
            },
            "images": {role: f"{prefix}-nixos-{role}-{image_suffix}" for role in IMAGE_ROLES},
            "flavors": selected_flavors,
            "paths": {
                "root": f"/srv/{namespace}",
                "adminState": f"/srv/{namespace}-state",
                "backups": f"/srv/{namespace}-backups",
                "data": f"/srv/{namespace}-data",
            },
        }
    )
    return base


def _ensure_key(path: Path, *, key_type: str = "ed25519") -> None:
    if key_type not in {"ed25519", "rsa"}:
        _fail("generated SSH key type is unsupported")
    if path.exists():
        _direct_private_file(path, field="generated SSH private key")
        if not path.with_suffix(path.suffix + ".pub").is_file():
            _fail("generated SSH key is missing its public half")
        return
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        _fail("ssh-keygen is required")
    _private_directory(path.parent)
    arguments: tuple[str | Path, ...] = (ssh_keygen, "-q", "-t", key_type)
    if key_type == "rsa":
        arguments += ("-b", "4096")
    _command(arguments + ("-N", "", "-f", path), environment=os.environ)
    path.chmod(0o600)
    path.with_suffix(path.suffix + ".pub").chmod(0o644)


def _ensure_secret_files(paths: SetupPaths) -> None:
    admin = paths.bootstrap / "admin-bootstrap.env"
    storage = paths.bootstrap / "storage-bootstrap.env"
    if not admin.exists():
        _atomic_private_write(admin, f"NOMAD_GOSSIP_KEY={_openssl_random('base64')}\n")
    else:
        _direct_private_file(admin, field="admin bootstrap secrets")
    if not storage.exists():
        values = {
            "POSTGRES_PASSWORD": _openssl_random("hex"),
            "MONGO_PASSWORD": _openssl_random("hex"),
            "GARAGE_RPC_SECRET": _openssl_random("hex"),
            "GARAGE_ADMIN_TOKEN": _openssl_random("hex"),
            "GARAGE_METRICS_TOKEN": _openssl_random("hex"),
            "REGISTRY_HTTP_SECRET": _openssl_random("hex"),
            "REGISTRY_BUILDER_PASSWORD": _openssl_random("hex"),
            "REGISTRY_RUNTIME_PASSWORD": _openssl_random("hex"),
        }
        _atomic_private_write(storage, "".join(f"{key}={value}\n" for key, value in values.items()))
    else:
        _direct_private_file(storage, field="storage bootstrap secrets")


def _openssl_random(encoding: str) -> str:
    openssl = shutil.which("openssl")
    if openssl is None:
        _fail("openssl is required")
    argv = (
        (openssl, "rand", "-base64", "32")
        if encoding == "base64"
        else (openssl, "rand", "-hex", "32")
    )
    return _command(argv, environment=os.environ, capture=True).strip()


def _ensure_age_identity(age_store: Path, path: Path) -> str:
    keygen = age_store / "bin/age-keygen"
    if not path.exists():
        _private_directory(path.parent)
        _command((keygen, "-o", path), environment=os.environ, capture=True)
        path.chmod(0o600)
    _direct_private_file(path, field="operator backup age identity")
    recipient = _command((keygen, "-y", path), environment=os.environ, capture=True).strip()
    if not recipient.startswith("age1"):
        _fail("generated age recipient is malformed")
    return recipient


def _write_policy(
    repository: Path,
    destination: Path,
    recipient: str,
    platform: Mapping[str, Any],
    values: Mapping[str, str],
) -> None:
    policy = json.loads(
        (repository / "config/platform-policy.example.json").read_text(encoding="utf-8")
    )
    if not isinstance(policy, dict):
        _fail("tracked policy template is malformed")
    standard = policy.get("standard")
    runtime_images = policy.get("runtimeImages")
    if not isinstance(standard, dict) or not isinstance(runtime_images, dict):
        _fail("tracked policy template is incomplete")
    standard["workerFlavor"] = platform["flavors"]["worker"]
    runtime_images["bun"] = values.get(
        "PLATFORM_BUN_RUNTIME_IMAGE",
        "docker.io/oven/bun@sha256:621f249399228db47cf34611ee662585e77e015250ed29d5d0932b2d3282f0b0",
    )
    runtime_images["node"] = values.get(
        "PLATFORM_NODE_RUNTIME_IMAGE",
        "docker.io/library/node@sha256:65932751ed4073ed02f5c04e494e4b2572a891b7dbea0568a863dc80341bf848",
    )
    policy["backupAgeRecipient"] = recipient
    _atomic_private_write(destination, json.dumps(policy, indent=2) + "\n")
    load_policy(destination, require_private=True)


def _write_openstack_wrapper(
    paths: SetupPaths,
    values: Mapping[str, str],
    openstack: Path,
) -> None:
    assignments = {
        key: value
        for key, value in values.items()
        if key.startswith("OS_") and value and key not in {"OS_TOKEN"}
    }
    _atomic_private_write(
        paths.openstack_environment,
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in sorted(assignments.items())),
    )
    wrapper = (
        "#!/bin/sh\nset -eu\nset -a\n"
        f". {shlex.quote(str(paths.openstack_environment))}\n"
        "set +a\n"
        f'exec {shlex.quote(str(openstack))} "$@"\n'
    )
    _atomic_private_write(paths.openstack_wrapper, wrapper)
    paths.openstack_wrapper.chmod(0o700)


def _qcow_path(output: Path) -> Path:
    resolved = output.resolve()
    candidates = sorted(resolved.rglob("*.qcow2"))
    if len(candidates) != 1 or not candidates[0].is_file():
        _fail(f"role image output has {len(candidates)} QCOW2 files")
    return candidates[0]


def _existing_image_id(
    wrapper: Path,
    environment: Mapping[str, str],
    name: str,
    role: str,
    commit: str,
    namespace: str,
) -> str | None:
    rows = _json_command(
        (wrapper, "image", "list", "--private", "--name", name, "-f", "json"),
        environment=environment,
    )
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and (row.get("Name") or row.get("name")) == name
    ]
    if not matches:
        return None
    if len(matches) != 1:
        _fail(f"multiple images are named {name}")
    image_id = str(matches[0].get("ID") or matches[0].get("id"))
    detail = _json_command(
        (wrapper, "image", "show", image_id, "-f", "json"), environment=environment
    )
    properties = detail.get("properties") if isinstance(detail, dict) else None
    key = namespace.replace("-", "_")
    if (
        not isinstance(properties, dict)
        or properties.get(f"{key}_source_commit") != commit
        or properties.get(f"{key}_role") != role
        or (detail.get("status") or detail.get("Status")) != "active"
    ):
        _fail(f"existing image does not match this setup release: {name}")
    try:
        return str(UUID(image_id))
    except ValueError as error:
        raise SetupError("existing image UUID is malformed") from error


def _build_and_publish_images(
    paths: SetupPaths,
    platform: Mapping[str, Any],
    provider_environment: Mapping[str, str],
    python_store: Path,
    commit: str,
) -> dict[str, str]:
    images_directory = paths.workspace / "images"
    _private_directory(images_directory)
    smoke_store = _build_nix_output(paths.repository, provider_environment, "imageSmoke")
    image_ids: dict[str, str] = {}
    child = dict(provider_environment)
    child["PLATFORM_CONFIG"] = str(paths.platform)
    child["OSC"] = str(paths.openstack_wrapper)
    child["SOURCE_COMMIT"] = commit
    child["PYTHONPATH"] = str(paths.repository)
    child["PATH"] = f"{python_store / 'bin'}:{os.environ.get('PATH', '')}"
    for role in IMAGE_ROLES:
        name = str(platform["images"][role])
        existing = _existing_image_id(
            paths.openstack_wrapper,
            child,
            name,
            role,
            commit,
            str(platform["namespace"]),
        )
        if existing is not None:
            image_ids[role] = existing
            continue
        output = _build_nix_output(
            paths.repository, child, f"{role}-image", platform=paths.platform
        )
        qcow = _qcow_path(output)
        _command(
            (smoke_store / "bin/openstack-platform-image-smoke", role, qcow),
            environment=child,
            cwd=paths.repository,
        )
        publish = _command(
            (paths.repository / "infra/openstack/publish_nixos_image.sh", role, qcow),
            environment=child,
            cwd=paths.repository,
            capture=True,
        )
        match = re.search(r"published role=\w+ image=([0-9a-f-]{36}) status=active", publish)
        if match is None:
            _fail(f"image publisher returned malformed evidence for {role}")
        image_ids[role] = str(UUID(match.group(1)))
    _atomic_private_write(
        paths.workspace / "image-ids.json", json.dumps(image_ids, indent=2) + "\n"
    )
    return image_ids


def _script_environment(
    provider_environment: Mapping[str, str], paths: SetupPaths, python_store: Path
) -> dict[str, str]:
    child = dict(provider_environment)
    child.update(
        {
            "PLATFORM_CONFIG": str(paths.platform),
            "OSC": str(paths.openstack_wrapper),
            "PATH": f"{python_store / 'bin'}:{os.environ.get('PATH', '')}",
        }
    )
    return child


def _apply_foundation(
    paths: SetupPaths, environment: Mapping[str, str], python_store: Path
) -> None:
    _command(
        (
            python_store / "bin/python3",
            paths.repository / "infra/openstack/apply_foundation.py",
            "--apply",
        ),
        environment=environment,
        cwd=paths.repository,
    )


def _bootstrap_roles(
    paths: SetupPaths,
    platform: Mapping[str, Any],
    environment: Mapping[str, str],
    python_store: Path,
    age_store: Path,
    cloudflare_token: Path | None,
    commit: str,
    image_ids: Mapping[str, str],
) -> bool:
    operator_key = paths.ssh_directory / "id_ed25519"
    nova_key = paths.bootstrap / "admin_nova_rsa"
    builder_key = paths.bootstrap / "builder_operator_ed25519"
    child = _script_environment(environment, paths, python_store)
    child.update(
        {
            "ADMIN_PUBLIC_KEY": str(nova_key.with_suffix(".pub")),
            "OPERATOR_PUBLIC_KEY": str(operator_key.with_suffix(".pub")),
            "ADMIN_SECRETS_FILE": str(paths.bootstrap / "admin-bootstrap.env"),
            "STORAGE_SECRETS_FILE": str(paths.bootstrap / "storage-bootstrap.env"),
            "PKI_DIR": str(paths.pki),
        }
    )
    _command(
        (paths.repository / "infra/openstack/apply_admin.sh",),
        environment=child,
        cwd=paths.repository,
    )
    runtime_environment = dict(child)
    runtime_environment.update(
        {
            "PLATFORM_AGE_COMMAND": str(age_store / "bin/age"),
            "PLATFORM_OPENSTACK_WRAPPER": str(paths.openstack_wrapper),
        }
    )
    _command(
        (paths.repository / "deploy/releases/bootstrap_operator_runtime.sh",),
        environment=runtime_environment,
        cwd=paths.repository,
    )
    runtime_python = OPERATOR_ROOT / "runtime/python3.14"
    _command(
        (
            runtime_python,
            paths.repository / "deploy/releases/setup_operator_bridge.py",
            "--platform-config",
            paths.platform,
            "--ssh-identity",
            operator_key,
            "--ssh-config",
            paths.ssh_directory / "config",
            "--known-hosts",
            paths.ssh_directory / "known_hosts",
            "--provider-command",
            paths.openstack_wrapper,
        ),
        environment=child,
        cwd=paths.repository,
    )
    ssh_config = paths.ssh_directory / "config"
    namespace = str(platform["namespace"])
    remote_root = str(platform["paths"]["root"])
    guest_config = f"/etc/{namespace}/platform.json"
    _command(
        (
            "ssh",
            "-F",
            ssh_config,
            OPERATOR_SSH_ALIAS,
            "--",
            "env",
            f"PLATFORM_CONFIG={guest_config}",
            f"{remote_root}/infra/nomad/bootstrap_acl.sh",
        ),
        environment=child,
    )
    nomad_tokens = paths.bootstrap / "nomad-tokens.env"
    _command(
        (
            "scp",
            "-F",
            ssh_config,
            "--",
            f"{OPERATOR_SSH_ALIAS}:{remote_root}/secrets/nomad-tokens.env",
            nomad_tokens,
        ),
        environment=child,
    )
    nomad_tokens.chmod(0o600)
    _command(
        (paths.repository / "infra/openstack/apply_storage.sh",),
        environment=child,
        cwd=paths.repository,
    )
    provisioning = f"{remote_root}/persistent/secrets/provisioning-pki"
    _command(
        (
            "ssh",
            "-F",
            ssh_config,
            OPERATOR_SSH_ALIAS,
            "--",
            "install",
            "-d",
            "-m",
            "0700",
            f"{remote_root}/secrets",
            provisioning,
        ),
        environment=child,
    )
    transfers = [
        (paths.openstack_environment, f"{remote_root}/secrets/openstack.env", "0600"),
        (
            paths.bootstrap / "storage-bootstrap.env",
            f"{remote_root}/secrets/storage-bootstrap.env",
            "0600",
        ),
        (
            builder_key.with_suffix(".pub"),
            f"{remote_root}/secrets/builder_operator_ed25519.pub",
            "0600",
        ),
        (builder_key, f"{remote_root}/secrets/builder_operator_ed25519", "0600"),
        (
            paths.pki / str(platform["pki"]["internalCaFile"]),
            f"{provisioning}/{platform['pki']['internalCaFile']}",
            "0644",
        ),
        (paths.pki / "nomad-worker.pem", f"{provisioning}/nomad-worker.pem", "0644"),
        (paths.pki / "nomad-worker-key.pem", f"{provisioning}/nomad-worker-key.pem", "0600"),
    ]
    internal_ca_name = str(platform["pki"]["internalCaFile"])
    if internal_ca_name != "internal-ca.pem":
        transfers.append((paths.pki / internal_ca_name, f"{provisioning}/internal-ca.pem", "0644"))
    for source, destination, mode in transfers:
        _command(
            ("scp", "-F", ssh_config, "--", source, f"{OPERATOR_SSH_ALIAS}:{destination}"),
            environment=child,
        )
        _command(
            ("ssh", "-F", ssh_config, OPERATOR_SSH_ALIAS, "--", "chmod", mode, destination),
            environment=child,
        )
    ingress = dict(child)
    ingress["NOMAD_TOKENS_FILE"] = str(nomad_tokens)
    if cloudflare_token is None:
        ingress["ENABLE_CLOUDFLARED"] = "false"
        pending = True
    else:
        _direct_private_file(cloudflare_token, field="Cloudflare tunnel token")
        ingress["ENABLE_CLOUDFLARED"] = "true"
        ingress["CLOUDFLARE_TUNNEL_TOKEN_FILE"] = str(cloudflare_token)
        pending = False
    _command(
        (paths.repository / "infra/openstack/apply_ingress.sh",),
        environment=ingress,
        cwd=paths.repository,
    )
    _command(
        (
            runtime_python,
            paths.repository / "deploy/releases/install_operator_config.py",
            "--platform",
            paths.platform,
            "--policy",
            paths.policy,
        ),
        environment=child,
        cwd=paths.repository,
    )
    _command(
        (
            runtime_python,
            paths.repository / "deploy/releases/install_release.py",
            "--mode",
            "operator",
            "--source",
            paths.repository,
            "--commit",
            commit,
            "--python",
            runtime_python,
            "--uv",
            OPERATOR_ROOT / "bin/uv",
            "--install-user-units",
            "--enable-backup-timer",
        ),
        environment=child,
        cwd=paths.repository,
    )
    _command(
        (paths.repository / "deploy/releases/deploy_helper_release.sh", commit),
        environment=child,
        cwd=paths.repository,
    )
    launcher = OPERATOR_ROOT / "bin/openstack-platform"
    _command((launcher, "status"), environment=child)
    _verify_controller_boundary(paths, platform, child)
    for role in IMAGE_ROLES:
        _command((launcher, "infra", "image", "set", role, image_ids[role]), environment=child)
    _command(
        (
            "ssh",
            "-F",
            ssh_config,
            OPERATOR_SSH_ALIAS,
            "--",
            "env",
            f"PLATFORM_CONFIG={guest_config}",
            "python3",
            f"{remote_root}/infra/backup/init_garage_backup_key.py",
        ),
        environment=child,
    )
    backup_key = f"{remote_root}/persistent/secrets/backup-age-key.txt"
    _command(
        (
            "ssh",
            "-F",
            ssh_config,
            OPERATOR_SSH_ALIAS,
            "--",
            "sh",
            "-c",
            f"test -f {shlex.quote(backup_key)} || {shlex.quote(remote_root + '/bin/age-keygen')} -o {shlex.quote(backup_key)} >/dev/null; chmod 0600 {shlex.quote(backup_key)}",
        ),
        environment=child,
    )
    status_output = _command((launcher, "status"), environment=child, capture=True)
    status_lines = [line for line in status_output.splitlines() if line.strip()]
    if len(status_lines) < 2 or status_lines[1].split()[0] != "healthy":
        _fail("fresh deployment did not reach healthy platform status")
    _command(
        ("systemctl", "--user", "is-enabled", "openstack-platform-backup.timer"),
        environment=child,
        capture=True,
    )
    if not pending:
        curl = shutil.which("curl")
        if curl is None:
            _fail("curl is required to verify configured public ingress")
        health = _command(
            (
                curl,
                "--fail",
                "--show-error",
                "--silent",
                "--user-agent",
                "openstack-platform-setup/1",
                f"https://{platform['domain']}/healthz",
            ),
            environment=child,
            capture=True,
            timeout=60,
        )
        if health.strip() != "OK":
            _fail("public platform health returned an unexpected response")
    return pending


def _verify_controller_boundary(
    paths: SetupPaths, platform: Mapping[str, Any], environment: Mapping[str, str]
) -> None:
    """Require the hosted controller service and its restricted API to be ready."""
    namespace = str(platform["namespace"])
    expected = f"controller-boundary=verified namespace={namespace}"
    script = r"""set -euo pipefail
namespace=$1
controller="${namespace}-controller.service"
readiness="${namespace}-controller-readiness.service"
hosted_backup_timer="${namespace}-hosted-controller-backup.timer"
socket="/run/${namespace}-controller/controller.sock"

for attempt in {1..180}; do
  if systemctl is-active --quiet "$controller" && \
     systemctl is-active --quiet "$readiness" && \
     test "$(systemctl show --property=Result --value "$readiness")" = success && \
     test -S "$socket"; then
    break
  fi
  sleep 1
done
systemctl is-active --quiet "$controller" || {
  echo "hosted controller service is not active" >&2
  exit 1
}
systemctl is-active --quiet "$readiness" || {
  echo "hosted controller API readiness service is not active" >&2
  exit 1
}
test "$(systemctl show --property=Result --value "$readiness")" = success || {
  echo "hosted controller API readiness check did not succeed" >&2
  exit 1
}
systemctl is-enabled --quiet "$hosted_backup_timer" || {
  echo "hosted controller backup timer is not enabled" >&2
  exit 1
}
test "$(stat -Lc '%F|%U|%G|%a' -- "$socket")" = \
  'socket|platform-controller|controller-api|660' || {
  echo "hosted controller socket boundary is invalid" >&2
  exit 1
}
# The operator account must not cross the management-only socket boundary.
set +e
curl --fail --silent --show-error --max-time 5 --unix-socket "$socket" \
  'http://localhost/v1/admin/applications?limit=1' >/dev/null 2>&1
operator_access_status=$?
set -e
test "$operator_access_status" -eq 7 || {
  echo "operator account unexpectedly crossed the controller API boundary" >&2
  exit 1
}
printf 'controller-boundary=verified namespace=%s\n' "$namespace"
"""
    result = _command(
        (
            "ssh",
            "-F",
            paths.ssh_directory / "config",
            OPERATOR_SSH_ALIAS,
            "--",
            "bash",
            "-s",
            "--",
            namespace,
        ),
        environment=environment,
        capture=True,
        stdin=script.encode("utf-8"),
        timeout=200,
    ).strip()
    if result != expected:
        _fail("hosted controller boundary returned unexpected verification evidence")


def _paths(repository: Path, workspace: Path) -> SetupPaths:
    return SetupPaths(
        repository=repository,
        workspace=workspace,
        platform=workspace / "config/platform.json",
        policy=workspace / "config/platform-policy.json",
        bootstrap=OPERATOR_ROOT / ".secrets/setup",
        pki=OPERATOR_ROOT / ".secrets/setup/pki",
        openstack_environment=OPERATOR_ROOT / ".secrets/openstack.env",
        openstack_wrapper=OPERATOR_ROOT / "bin/platform-openstack",
        ssh_directory=OPERATOR_ROOT / ".secrets/ssh",
    )


def _print_plan(output: TextIO) -> None:
    print("setup plan:", file=output)
    print("  authenticate and discover the configured OpenStack project", file=output)
    print(
        "  generate private inventory, keys, service credentials, age identity, and PKI",
        file=output,
    )
    print("  create security groups and reserve the three fixed ports", file=output)
    print("  build, QEMU-smoke, and publish five commit-addressed NixOS role images", file=output)
    print("  create three persistent VMs and their volumes", file=output)
    print(
        "  install the operator bridge, policy and releases; verify the hosted controller and backups",
        file=output,
    )
    print("rerun with --apply to perform these actions", file=output)


def run_setup(
    *,
    env_file: Path,
    workspace: Path,
    cloudflare_token: Path | None,
    apply: bool,
    input_reader: Callable[[str], str] = input,
    secret_reader: Callable[[str], str] = getpass.getpass,
    output: TextIO,
) -> None:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        _fail("run setup as the unprivileged operator owner")
    if not apply:
        load_environment_file(env_file)
        _print_plan(output)
        return None
    repository = _repository_root()
    values = load_environment_file(env_file)
    _credential_requirements(values, input_reader, secret_reader)
    provider_environment = _openstack_environment(values)
    commit = _source_commit(repository, provider_environment)
    _private_directory(OPERATOR_ROOT)
    _private_directory(workspace)
    paths = _paths(repository, workspace)
    for directory in (
        paths.workspace / "config",
        paths.bootstrap.parent,
        paths.bootstrap,
        paths.ssh_directory,
    ):
        _private_directory(directory)
    python_store = _build_nix_output(repository, provider_environment, "python")
    age_store = _build_nix_output(repository, provider_environment, "age")
    openstack = python_store / "bin/openstack"
    project = _project_identity(openstack, provider_environment)
    provider_environment = dict(provider_environment)
    provider_project_id = (
        values.get("OS_PROJECT_ID") or values.get("OS_TENANT_ID") or project.project_id
    )
    provider_environment["OS_PROJECT_ID"] = provider_project_id
    provider_environment["OS_PROJECT_NAME"] = project.project_name
    values["OS_PROJECT_ID"] = provider_project_id
    values["OS_PROJECT_NAME"] = project.project_name
    document = _platform_document(
        repository,
        values,
        project,
        commit,
        openstack,
        provider_environment,
        input_reader,
    )
    if paths.platform.exists():
        existing = json.loads(paths.platform.read_text(encoding="utf-8"))
        if existing != document:
            _fail("existing setup inventory differs; use a new empty workspace")
    else:
        _atomic_private_write(paths.platform, json.dumps(document, indent=2) + "\n")
    load_platform(paths.platform)
    _ensure_key(paths.ssh_directory / "id_ed25519")
    _ensure_key(paths.bootstrap / "admin_nova_rsa", key_type="rsa")
    _ensure_key(paths.bootstrap / "builder_operator_ed25519")
    _ensure_secret_files(paths)
    recipient = _ensure_age_identity(age_store, paths.bootstrap / "backup-age-identity.txt")
    if paths.policy.exists():
        policy = load_policy(paths.policy, require_private=True)
        if policy.backup_age_recipient != recipient:
            _fail("existing setup policy uses another backup age identity")
    else:
        _write_policy(repository, paths.policy, recipient, document, values)
    _write_openstack_wrapper(paths, values, openstack)
    child = _script_environment(provider_environment, paths, python_store)
    _command(
        (repository / "infra/pki/generate_internal_pki.sh", paths.pki),
        environment=child,
        cwd=repository,
    )
    _apply_foundation(paths, child, python_store)
    image_ids = _build_and_publish_images(
        paths, document, provider_environment, python_store, commit
    )
    pending = _bootstrap_roles(
        paths,
        document,
        provider_environment,
        python_store,
        age_store,
        cloudflare_token,
        commit,
        image_ids,
    )
    print(
        f"setup=complete project={project.project_name} project-id={project.project_id}",
        file=output,
    )
    print(f"inventory={paths.platform}", file=output)
    if pending:
        print("public-ingress=pending external provider configuration", file=output)
    else:
        print("public-ingress=cloudflare-configured", file=output)
