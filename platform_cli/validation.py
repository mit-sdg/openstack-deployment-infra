"""Strict validators shared by management and helper feature code."""

from __future__ import annotations

import re
import uuid as uuid_module
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ValidationError(ValueError):
    """An operator-supplied value did not satisfy the M1 contract."""


_SLUG = re.compile(r"[a-z][a-z0-9-]{1,38}[a-z0-9]")
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9-]{0,38}[a-z0-9]|[a-z]")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_SCRIPT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_OPENSTACK_UUID_HEX = re.compile(r"[0-9a-f]{32}")
_OCI_DIGEST = re.compile(r"[^\s/@]+(?:/[^\s/@]+)+@sha256:[0-9a-f]{64}")
_AGE_RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}")
_GITHUB_COMPONENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?")


def slug(value: object) -> str:
    """Return a lowercase application slug (3-40 safe characters)."""
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValidationError("slug must be 3-40 lowercase letters, numbers, or interior hyphens")
    if "--" in value:
        raise ValidationError("slug must not contain consecutive hyphens")
    return value


def resource_name(value: object = "default") -> str:
    """Return a canonical managed-resource name (1-40 safe characters)."""
    if not isinstance(value, str) or not _RESOURCE_NAME.fullmatch(value):
        raise ValidationError(
            "resource name must be 1-40 lowercase letters, numbers, or interior hyphens"
        )
    if "--" in value:
        raise ValidationError("resource name must not contain consecutive hyphens")
    return value


def uuid(value: object, *, field: str = "UUID") -> str:
    """Return a canonical lowercase RFC 4122 UUID string."""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid_module.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ValidationError(f"{field} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValidationError(f"{field} must be a canonical lowercase UUID")
    return value


def openstack_uuid(value: object, *, field: str = "OpenStack UUID") -> str:
    """Normalize one trusted OpenStack UUID projection.

    OpenStack services may emit either canonical lowercase UUIDs or lowercase
    32-hex UUIDs. This compatibility parser is for provider output only; use
    :func:`uuid` for configuration, requests, and other application inputs.
    """
    if isinstance(value, str) and _OPENSTACK_UUID_HEX.fullmatch(value):
        return str(uuid_module.UUID(hex=value))
    return uuid(value, field=field)


def commit(value: object) -> str:
    """Validate an exact full lowercase Git commit ID."""
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ValidationError("commit must be exactly 40 lowercase hexadecimal characters")
    return value


def env_key(value: object) -> str:
    """Validate one environment variable name."""
    if not isinstance(value, str) or not _ENV_KEY.fullmatch(value):
        raise ValidationError("environment key must start with A-Z and contain only A-Z, 0-9, or _")
    return value


def script_name(value: object) -> str:
    """Validate a package script name, never command text."""
    if not isinstance(value, str) or not _SCRIPT_NAME.fullmatch(value):
        raise ValidationError("script name contains unsupported characters")
    return value


def safe_code(value: object) -> str:
    """Validate a protocol-safe error code."""
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
        raise ValidationError("error code is malformed")
    return value


def repository_url(value: object) -> str:
    """Validate a credential-free public GitHub repository URL.

    The returned URL is canonical and has no trailing slash or ``.git`` suffix.
    """
    if not isinstance(value, str) or len(value) > 256:
        raise ValidationError("repository URL is malformed")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError(
            "repository must be a credential-free https://github.com/OWNER/REPOSITORY URL"
        )
    parts = parsed.path.split("/")
    if len(parts) != 3 or not parts[1] or not parts[2] or parsed.path.endswith("/"):
        raise ValidationError("repository URL must contain exactly an owner and repository")
    owner, repository = parts[1], parts[2]
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not _GITHUB_COMPONENT.fullmatch(owner) or not _GITHUB_COMPONENT.fullmatch(repository):
        raise ValidationError("repository owner or name contains unsupported characters")
    return urlunsplit(("https", "github.com", f"/{owner}/{repository}", "", ""))


def relative_path(value: object, *, field: str = "path", allow_dot: bool = True) -> str:
    """Validate and normalize a POSIX path relative to a checkout."""
    if not isinstance(value, str) or not value or len(value.encode()) > 1024:
        raise ValidationError(f"{field} must be a non-empty repository-relative path")
    if "\\" in value or "\x00" in value:
        raise ValidationError(f"{field} contains unsupported characters")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"{field} must remain inside the repository")
    normalized = str(path)
    if normalized == "." and not allow_dot:
        raise ValidationError(f"{field} must name a file below the repository root")
    if value.startswith("./") or "//" in value:
        raise ValidationError(f"{field} must be normalized")
    return normalized


def resolve_inside(root: Path, value: object, *, field: str = "path") -> Path:
    """Resolve a validated relative path and reject symlink escape."""
    relative = relative_path(value, field=field)
    resolved_root = root.resolve(strict=True)
    resolved = (resolved_root / relative).resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise ValidationError(f"{field} resolves outside the repository")
    return resolved


def health_path(value: object) -> str:
    """Validate a simple absolute HTTP health endpoint path."""
    if not isinstance(value, str) or not (1 <= len(value) <= 256):
        raise ValidationError("health path is malformed")
    if not value.startswith("/") or value.startswith("//") or "?" in value or "#" in value:
        raise ValidationError("health path must be an absolute path without query or fragment")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise ValidationError("health path contains unsupported characters")
    if ".." in PurePosixPath(value).parts or "\\" in value:
        raise ValidationError("health path must be normalized")
    return value


def sha256_hex(value: object, *, field: str = "SHA-256") -> str:
    """Require one canonical lowercase SHA-256 hexadecimal value."""
    if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
        raise ValidationError(f"{field} must be 64 lowercase hexadecimal characters")
    return value


def oci_digest_pin(value: object, *, field: str = "runtime image") -> str:
    """Require an OCI image reference pinned by a lowercase SHA-256 digest."""
    if not isinstance(value, str) or len(value) > 512 or not _OCI_DIGEST.fullmatch(value):
        raise ValidationError(f"{field} must be an image@sha256 digest pin")
    return value


def age_recipient(value: object) -> str:
    """Require a canonical age X25519 recipient."""
    if not isinstance(value, str) or not _AGE_RECIPIENT.fullmatch(value):
        raise ValidationError("backupAgeRecipient must be a canonical age recipient")
    return value


def bounded_text(value: object, *, field: str, maximum: int) -> str:
    """Validate a non-NUL UTF-8 string by encoded size."""
    if not isinstance(value, str) or "\x00" in value:
        raise ValidationError(f"{field} must be text without NUL bytes")
    if len(value.encode("utf-8")) > maximum:
        raise ValidationError(f"{field} exceeds its {maximum}-byte limit")
    return value


def load_strict_yaml(text: str | bytes, *, maximum_bytes: int = 65_536) -> Any:
    """Parse bounded YAML without duplicate keys, aliases, or custom tags.

    PyYAML is imported lazily so infrastructure-only users that never parse an
    application configuration do not need to import it at startup.
    """
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > maximum_bytes:
        raise ValidationError(f"YAML exceeds its {maximum_bytes}-byte limit")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("YAML must be UTF-8") from error

    try:
        import yaml
    except ImportError as error:  # pragma: no cover - packaging supplies PyYAML
        raise RuntimeError("strict YAML parsing requires PyYAML") from error

    class StrictLoader(yaml.SafeLoader):
        node_count = 0
        node_depth = 0

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise ValidationError("YAML aliases are not supported")
            self.node_count += 1
            if self.node_count > 2_048:
                raise ValidationError("YAML contains too many nodes")
            self.node_depth += 1
            if self.node_depth > 64:
                raise ValidationError("YAML nesting is too deep")
            try:
                return super().compose_node(parent, index)
            finally:
                self.node_depth -= 1

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
        loader.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as error:
                raise ValidationError("YAML mapping keys must be scalar") from error
            if duplicate:
                raise ValidationError(f"YAML contains duplicate key {key!r}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    try:
        documents = list(yaml.load_all(decoded, Loader=StrictLoader))
    except ValidationError:
        raise
    except yaml.YAMLError as error:
        raise ValidationError("YAML is malformed or uses an unsupported tag") from error
    if len(documents) != 1 or documents[0] is None:
        raise ValidationError("YAML must contain exactly one non-empty document")
    return documents[0]
