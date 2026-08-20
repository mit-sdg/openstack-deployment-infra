"""Protected, role-specific cloud-init rendering for persistent hosts.

The renderer consumes only reviewed templates, the current non-secret platform
inventory, and direct protected bootstrap/PKI inputs.  Secret-bearing payloads
are written only to owner-only files and are never represented by result
objects or exceptions.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import PlatformConfig
from .validation import ValidationError, uuid

PERSISTENT_ROLES = ("admin", "ingress", "storage")
_TEMPLATE_PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")
_MAX_INPUT_BYTES = 1_048_576
_MAX_SECRET_FILE_BYTES = 65_536
_MAX_KEY_BYTES = 16_384

_EXPECTED_COUNTS: Mapping[str, Mapping[str, int]] = {
    "admin": {
        "__ADMIN_HOST__": 1,
        "__ADMIN_IP__": 1,
        "__ADMIN_VOLUME_ID__": 1,
        "__ADMIN_VOLUME_LABEL__": 1,
        "__AGENTOPS_PUBLIC_KEY__": 1,
        "__BACKUP_VOLUME_ID__": 1,
        "__BACKUP_VOLUME_LABEL__": 1,
        "__DATACENTER__": 1,
        "__INTERNAL_CA_B64__": 2,
        "__NOMAD_CLI_CERT_B64__": 1,
        "__NOMAD_CLI_KEY_B64__": 1,
        "__NOMAD_GOSSIP_KEY__": 1,
        "__NOMAD_SERVER_CERT_B64__": 1,
        "__NOMAD_SERVER_KEY_B64__": 1,
        "__PLATFORM_NAMESPACE__": 9,
        "__REGION__": 1,
    },
    "ingress": {
        "__ADMIN_IP__": 1,
        "__AGENTOPS_PUBLIC_KEY__": 1,
        "__CLOUDFLARED_WRITE_FILE__": 1,
        "__INGRESS_HOST__": 1,
        "__INTERNAL_CA_B64__": 1,
        "__NOMAD_INGRESS_CERT_B64__": 1,
        "__NOMAD_INGRESS_KEY_B64__": 1,
        "__NOMAD_TRAEFIK_TOKEN__": 2,
        "__PLATFORM_DOMAIN__": 1,
        "__PLATFORM_NAMESPACE__": 6,
        "__RECOVERY_DOMAIN_1__": 1,
        "__RECOVERY_DOMAIN_2__": 1,
    },
    "storage": {
        "__AGENTOPS_PUBLIC_KEY__": 1,
        "__DATA_VOLUME_ID__": 1,
        "__DATA_VOLUME_LABEL__": 1,
        "__GARAGE_ADMIN_TOKEN__": 1,
        "__GARAGE_METRICS_TOKEN__": 1,
        "__GARAGE_RPC_SECRET__": 1,
        "__INTERNAL_CA_B64__": 1,
        "__MONGODB_COMBINED_B64__": 1,
        "__MONGO_PASSWORD__": 1,
        "__OBJECT_STORAGE_INTERNAL_NAME__": 1,
        "__PLATFORM_DISPLAY_NAME__": 1,
        "__PLATFORM_NAMESPACE__": 11,
        "__POSTGRES_PASSWORD__": 1,
        "__REGISTRY_HTPASSWD_B64__": 1,
        "__REGISTRY_HTTP_SECRET__": 1,
        "__STORAGE_CERT_B64__": 1,
        "__STORAGE_HOST__": 1,
        "__STORAGE_KEY_B64__": 1,
    },
}

_SECRET_KEYS: Mapping[str, frozenset[str]] = {
    "admin": frozenset({"NOMAD_GOSSIP_KEY"}),
    "ingress": frozenset({"NOMAD_CONTROLLER_TOKEN", "NOMAD_TRAEFIK_TOKEN"}),
    "storage": frozenset(
        {
            "POSTGRES_PASSWORD",
            "MONGO_PASSWORD",
            "GARAGE_RPC_SECRET",
            "GARAGE_ADMIN_TOKEN",
            "GARAGE_METRICS_TOKEN",
            "REGISTRY_HTTP_SECRET",
            "REGISTRY_BUILDER_PASSWORD",
            "REGISTRY_RUNTIME_PASSWORD",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class HostUserDataInputs:
    """Paths only; secret values are never retained in this record."""

    template: Path
    agentops_public_key: Path
    secret_file: Path
    pki_directory: Path
    cloudflare_tunnel_token_file: Path | None = None
    enable_cloudflared: bool = True


def _role(value: str) -> str:
    if value not in PERSISTENT_ROLES:
        raise ValidationError("host user-data role must be admin, ingress, or storage")
    return value


def _environment_path(environment: Mapping[str, str], name: str) -> Path:
    value = environment.get(name)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"set {name} to a protected input file path")
    return Path(value)


def inputs_from_environment(
    role: str,
    *,
    environment: Mapping[str, str] | None = None,
    template: str | Path | None = None,
) -> HostUserDataInputs:
    """Resolve the established apply-script file-path contract from the environment."""
    role = _role(role)
    values = os.environ if environment is None else environment
    template_path = (
        Path(template)
        if template is not None
        else Path(__file__).resolve().parents[1] / "infra" / "cloud-init-nixos" / f"{role}.yaml"
    )
    secret_variable = {
        "admin": "ADMIN_SECRETS_FILE",
        "ingress": "NOMAD_TOKENS_FILE",
        "storage": "STORAGE_SECRETS_FILE",
    }[role]
    enable_cloudflared = True
    tunnel_path: Path | None = None
    if role == "ingress":
        enabled = values.get("ENABLE_CLOUDFLARED", "true")
        if enabled not in {"true", "false"}:
            raise ValidationError("ENABLE_CLOUDFLARED must be true or false")
        enable_cloudflared = enabled == "true"
        if enable_cloudflared:
            tunnel_path = _environment_path(values, "CLOUDFLARE_TUNNEL_TOKEN_FILE")
    return HostUserDataInputs(
        template=template_path,
        agentops_public_key=_environment_path(values, "AGENTOPS_PUBLIC_KEY"),
        secret_file=_environment_path(values, secret_variable),
        pki_directory=_environment_path(values, "PKI_DIR"),
        cloudflare_tunnel_token_file=tunnel_path,
        enable_cloudflared=enable_cloudflared,
    )


def _read_direct(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
    private: bool,
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"{label} must be a readable direct regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise ValidationError(f"{label} must be a bounded direct regular file")
        if private and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise ValidationError(f"{label} must be owner-only and owned by this operator")
        chunks: list[bytes] = []
        length = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum_bytes + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > maximum_bytes:
                raise ValidationError(f"{label} exceeds its safety limit")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_text(value: bytes, *, label: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError(f"{label} must be UTF-8") from error


def _public_key(path: Path) -> str:
    key = _decode_text(
        _read_direct(
            path, label="agentops public key", maximum_bytes=_MAX_KEY_BYTES, private=False
        ),
        label="agentops public key",
    ).strip()
    if "\n" in key or "\r" in key or not key.startswith("ssh-ed25519 "):
        raise ValidationError("agentops public key must be one Ed25519 public-key line")
    return key


def _secret_values(role: str, path: Path) -> dict[str, str]:
    text = _decode_text(
        _read_direct(
            path,
            label=f"{role} bootstrap secret file",
            maximum_bytes=_MAX_SECRET_FILE_BYTES,
            private=True,
        ),
        label=f"{role} bootstrap secret file",
    )
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValidationError(f"{role} bootstrap secret file is malformed")
        name, value = line.split("=", 1)
        if name in values or not value or "\x00" in value or len(value.encode()) > 16_384:
            raise ValidationError(f"{role} bootstrap secret file is malformed")
        values[name] = value
    if values.keys() != _SECRET_KEYS[role]:
        raise ValidationError(f"{role} bootstrap secret file has unexpected keys")
    return values


def _pki(inputs: HostUserDataInputs, name: str, *, private: bool = False) -> bytes:
    if not name or name != Path(name).name:
        raise ValidationError("configured internal CA file name is malformed")
    return _read_direct(
        inputs.pki_directory / name,
        label="private PKI input" if private else "PKI input",
        maximum_bytes=_MAX_INPUT_BYTES,
        private=private,
    )


def _text(platform: PlatformConfig, name: str, *, maximum: int = 512) -> str:
    try:
        value = platform.get(name)
    except KeyError as error:
        raise ValidationError(f"platform inventory is missing {name}") from error
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value.encode()) > maximum
    ):
        raise ValidationError(f"platform inventory {name} is malformed")
    return value


def _volume_id(platform: PlatformConfig, volume_ids: Mapping[str, str], name: str) -> str:
    configured_name = _text(platform, name)
    try:
        value = volume_ids[configured_name]
    except KeyError as error:
        raise ValidationError("host user-data is missing a retained volume identity") from error
    return uuid(value, field="retained volume UUID")


def _expected_volume_names(platform: PlatformConfig, role: str) -> set[str]:
    if role == "admin":
        return {
            _text(platform, "volumes.adminState.name"),
            _text(platform, "volumes.backup.name"),
        }
    if role == "storage":
        return {_text(platform, "volumes.data.name")}
    return set()


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _cloudflared_write_file(platform: PlatformConfig, inputs: HostUserDataInputs) -> str:
    if not inputs.enable_cloudflared:
        return ""
    if inputs.cloudflare_tunnel_token_file is None:
        raise ValidationError("Cloudflare tunnel token input is missing")
    token = _decode_text(
        _read_direct(
            inputs.cloudflare_tunnel_token_file,
            label="Cloudflare tunnel token",
            maximum_bytes=_MAX_SECRET_FILE_BYTES,
            private=True,
        ),
        label="Cloudflare tunnel token",
    ).strip()
    if len(token) < 50 or any(character.isspace() for character in token):
        raise ValidationError("Cloudflare tunnel token is malformed")
    namespace = platform.namespace
    content = _encoded(f"TUNNEL_TOKEN={token}\n".encode())
    return (
        f"  - path: /etc/{namespace}/secrets/cloudflared.env\n"
        '    owner: root:root\n    permissions: "0600"\n    encoding: b64\n'
        f"    content: {content}"
    )


def _htpasswd(username: str, password: str) -> bytes:
    encoded: bytes
    try:
        import bcrypt
    except ModuleNotFoundError:
        # Python 3.12 development hosts may not have the production dependency.
        # Production is exactly Python 3.14 and always takes the audited bcrypt
        # package path from the release lock/Nix closure.
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import crypt  # type: ignore[import-not-found]

        try:
            digest = crypt.crypt(password, crypt.mksalt(crypt.METHOD_BLOWFISH))
        except (ValueError, TypeError) as error:
            raise ValidationError("registry bootstrap password is malformed") from error
        if not isinstance(digest, str) or not digest.startswith("$2"):
            raise ValidationError("registry bootstrap password is malformed") from None
        encoded = digest.encode()
    else:
        try:
            encoded = bytes(bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)))
        except (ValueError, TypeError) as error:
            raise ValidationError("registry bootstrap password is malformed") from error
    return username.encode() + b":" + encoded + b"\n"


def _replacement_values(
    platform: PlatformConfig,
    role: str,
    inputs: HostUserDataInputs,
    volume_ids: Mapping[str, str],
) -> Mapping[str, str]:
    expected_volumes = _expected_volume_names(platform, role)
    if set(volume_ids) != expected_volumes:
        raise ValidationError("host user-data retained volume identities do not match the role")
    key = json.dumps(_public_key(inputs.agentops_public_key))
    secrets = _secret_values(role, inputs.secret_file)
    ca_file = _text(platform, "pki.internalCaFile", maximum=128)
    ca = _encoded(_pki(inputs, ca_file))
    namespace = platform.namespace
    if role == "admin":
        return {
            "__AGENTOPS_PUBLIC_KEY__": key,
            "__ADMIN_VOLUME_ID__": _volume_id(platform, volume_ids, "volumes.adminState.name"),
            "__ADMIN_VOLUME_LABEL__": _text(platform, "volumes.adminState.label", maximum=16),
            "__BACKUP_VOLUME_ID__": _volume_id(platform, volume_ids, "volumes.backup.name"),
            "__BACKUP_VOLUME_LABEL__": _text(platform, "volumes.backup.label", maximum=16),
            "__NOMAD_GOSSIP_KEY__": secrets["NOMAD_GOSSIP_KEY"],
            "__INTERNAL_CA_B64__": ca,
            "__NOMAD_SERVER_CERT_B64__": _encoded(_pki(inputs, "nomad-server.pem")),
            "__NOMAD_SERVER_KEY_B64__": _encoded(
                _pki(inputs, "nomad-server-key.pem", private=True)
            ),
            "__NOMAD_CLI_CERT_B64__": _encoded(_pki(inputs, "nomad-cli.pem")),
            "__NOMAD_CLI_KEY_B64__": _encoded(_pki(inputs, "nomad-cli-key.pem", private=True)),
            "__ADMIN_HOST__": _text(platform, "hosts.admin"),
            "__ADMIN_IP__": _text(platform, "addresses.admin"),
            "__REGION__": platform.region,
            "__DATACENTER__": platform.datacenter,
            "__PLATFORM_NAMESPACE__": namespace,
        }
    if role == "ingress":
        try:
            recovery_domains = platform.get("recoveryDomains")
        except KeyError as error:
            raise ValidationError("platform inventory is missing recoveryDomains") from error
        if not isinstance(recovery_domains, tuple) or len(recovery_domains) < 2:
            raise ValidationError("platform inventory recoveryDomains is malformed")
        recovery_one, recovery_two = recovery_domains[:2]
        if not all(
            isinstance(value, str)
            and value
            and "\n" not in value
            and "\r" not in value
            and "\x00" not in value
            and len(value.encode()) <= 253
            for value in (recovery_one, recovery_two)
        ):
            raise ValidationError("platform inventory recoveryDomains is malformed")
        return {
            "__AGENTOPS_PUBLIC_KEY__": key,
            "__NOMAD_TRAEFIK_TOKEN__": secrets["NOMAD_TRAEFIK_TOKEN"],
            "__CLOUDFLARED_WRITE_FILE__": _cloudflared_write_file(platform, inputs),
            "__INTERNAL_CA_B64__": ca,
            "__NOMAD_INGRESS_CERT_B64__": _encoded(_pki(inputs, "nomad-ingress.pem")),
            "__NOMAD_INGRESS_KEY_B64__": _encoded(
                _pki(inputs, "nomad-ingress-key.pem", private=True)
            ),
            "__INGRESS_HOST__": _text(platform, "hosts.ingress"),
            "__ADMIN_IP__": _text(platform, "addresses.admin"),
            "__PLATFORM_DOMAIN__": platform.domain,
            "__RECOVERY_DOMAIN_1__": recovery_one,
            "__RECOVERY_DOMAIN_2__": recovery_two,
            "__PLATFORM_NAMESPACE__": namespace,
        }
    storage_certificate = _pki(inputs, "storage.pem")
    storage_key = _pki(inputs, "storage-key.pem", private=True)
    htpasswd = _htpasswd("builder", secrets["REGISTRY_BUILDER_PASSWORD"]) + _htpasswd(
        "runtime", secrets["REGISTRY_RUNTIME_PASSWORD"]
    )
    return {
        "__AGENTOPS_PUBLIC_KEY__": key,
        "__DATA_VOLUME_ID__": _volume_id(platform, volume_ids, "volumes.data.name"),
        "__DATA_VOLUME_LABEL__": _text(platform, "volumes.data.label", maximum=16),
        "__POSTGRES_PASSWORD__": secrets["POSTGRES_PASSWORD"],
        "__MONGO_PASSWORD__": secrets["MONGO_PASSWORD"],
        "__GARAGE_RPC_SECRET__": secrets["GARAGE_RPC_SECRET"],
        "__GARAGE_ADMIN_TOKEN__": secrets["GARAGE_ADMIN_TOKEN"],
        "__GARAGE_METRICS_TOKEN__": secrets["GARAGE_METRICS_TOKEN"],
        "__REGISTRY_HTTP_SECRET__": secrets["REGISTRY_HTTP_SECRET"],
        "__INTERNAL_CA_B64__": ca,
        "__STORAGE_CERT_B64__": _encoded(storage_certificate),
        "__STORAGE_KEY_B64__": _encoded(storage_key),
        "__MONGODB_COMBINED_B64__": _encoded(storage_certificate + storage_key),
        "__REGISTRY_HTPASSWD_B64__": _encoded(htpasswd),
        "__STORAGE_HOST__": _text(platform, "hosts.storage"),
        "__OBJECT_STORAGE_INTERNAL_NAME__": _text(platform, "internalNames.objectStorage"),
        "__PLATFORM_NAMESPACE__": namespace,
        "__PLATFORM_DISPLAY_NAME__": _text(platform, "displayName"),
    }


def _render(
    platform: PlatformConfig,
    role: str,
    inputs: HostUserDataInputs,
    volume_ids: Mapping[str, str],
    *,
    maximum_bytes: int,
) -> bytes:
    role = _role(role)
    if isinstance(maximum_bytes, bool) or not 1 <= maximum_bytes <= 16_777_216:
        raise ValidationError("host user-data limit is malformed")
    template = _decode_text(
        _read_direct(
            inputs.template,
            label="cloud-init template",
            maximum_bytes=maximum_bytes,
            private=False,
        ),
        label="cloud-init template",
    )
    if not template.startswith("#cloud-config\n"):
        raise ValidationError("cloud-init template header is malformed")
    expected = _EXPECTED_COUNTS[role]
    found = set(_TEMPLATE_PLACEHOLDER.findall(template))
    if found != set(expected):
        raise ValidationError("cloud-init template placeholders do not match the reviewed renderer")
    replacements = _replacement_values(platform, role, inputs, volume_ids)
    if set(replacements) != set(expected):
        raise AssertionError("reviewed host user-data replacement map is incomplete")
    for placeholder, count in expected.items():
        if template.count(placeholder) != count:
            raise ValidationError("cloud-init template placeholder count changed")
        template = template.replace(placeholder, replacements[placeholder])
    payload = template.encode()
    if not payload or len(payload) > maximum_bytes:
        raise ValidationError("generated cloud-init exceeds its safety limit")
    return payload


def render_host_user_data_file(
    platform: PlatformConfig,
    role: str,
    inputs: HostUserDataInputs,
    volume_ids: Mapping[str, str],
    output: str | Path,
    *,
    maximum_bytes: int = 1_048_576,
) -> None:
    """Render directly into one owner-only regular file, returning no payload."""
    payload = _render(platform, role, inputs, volume_ids, maximum_bytes=maximum_bytes)
    path = Path(output)
    flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ValidationError("host user-data output must be a protected direct file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValidationError("host user-data output must be owned by this operator")
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count == 0:
                raise ValidationError("host user-data output could not be completed")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def staged_host_user_data(
    platform: PlatformConfig,
    role: str,
    volume_ids: Mapping[str, str],
    *,
    inputs: HostUserDataInputs | None = None,
    maximum_bytes: int = 1_048_576,
) -> Iterator[str]:
    """Yield a mode-0600 rendered path and unlink it on every exit path."""
    selected_inputs = inputs or inputs_from_environment(role)
    descriptor, temporary_name = tempfile.mkstemp(prefix="platform-host-user-data-")
    os.close(descriptor)
    try:
        os.chmod(temporary_name, 0o600)
        render_host_user_data_file(
            platform,
            role,
            selected_inputs,
            volume_ids,
            temporary_name,
            maximum_bytes=maximum_bytes,
        )
        yield temporary_name
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
