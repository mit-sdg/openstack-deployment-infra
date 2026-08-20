"""Canonical secret-free contracts shared by runtime and storage boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .validation import ValidationError, resource_name, slug, uuid

RESOURCE_TYPES = ("postgres", "mongo", "s3")
DEFAULT_RESOURCE_NAME = "default"
# Public output names are stable binding-contract names, not runtime env names.
RESOURCE_OUTPUTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "postgres": (
            "url",
            "host",
            "port",
            "database",
            "user",
            "password",
            "sslmode",
            "sslrootcert",
        ),
        "mongo": ("uri",),
        "s3": (
            "endpoint",
            "region",
            "access_key_id",
            "secret_access_key",
            "ca_bundle",
            "bucket",
            "force_path_style",
        ),
    }
)
# Provider helpers still construct values using these familiar local aliases.
ENVIRONMENT_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "postgres": (
            "DATABASE_URL",
            "PGHOST",
            "PGPORT",
            "PGDATABASE",
            "PGUSER",
            "PGPASSWORD",
            "PGSSLMODE",
            "PGSSLROOTCERT",
        ),
        "mongo": ("MONGODB_URI",),
        "s3": (
            "AWS_ENDPOINT_URL_S3",
            "AWS_REGION",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_CA_BUNDLE",
            "S3_ENDPOINT",
            "S3_BUCKET",
            "S3_FORCE_PATH_STYLE",
        ),
    }
)
OUTPUT_ENVIRONMENT_KEYS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "postgres": MappingProxyType(
            dict(zip(RESOURCE_OUTPUTS["postgres"], ENVIRONMENT_KEYS["postgres"], strict=True))
        ),
        "mongo": MappingProxyType({"uri": "MONGODB_URI"}),
        "s3": MappingProxyType(
            {
                "endpoint": "AWS_ENDPOINT_URL_S3",
                "region": "AWS_REGION",
                "access_key_id": "AWS_ACCESS_KEY_ID",
                "secret_access_key": "AWS_SECRET_ACCESS_KEY",
                "ca_bundle": "AWS_CA_BUNDLE",
                "bucket": "S3_BUCKET",
                "force_path_style": "S3_FORCE_PATH_STYLE",
            }
        ),
    }
)


def storage_owner(resource_type: str, name: str = DEFAULT_RESOURCE_NAME) -> str:
    if resource_type not in RESOURCE_TYPES:
        raise ValidationError("storage type must be postgres, mongo, or s3")
    return f"storage.{resource_type}.{resource_name(name)}"


def canonical_secret_key(resource_type: str, name: str, output: str) -> str:
    checked_name = resource_name(name)
    if resource_type not in RESOURCE_TYPES or output not in RESOURCE_OUTPUTS[resource_type]:
        raise ValidationError("managed-storage output is invalid")
    # Hyphens are the only non-alphanumeric resource-name character, making this
    # encoding injective. The prefix is reserved from staff runtime keys.
    return f"STORAGE__{resource_type.upper()}__{checked_name.replace('-', '_').upper()}__{output.upper()}"


def canonical_secret_keys(resource_type: str, name: str) -> tuple[str, ...]:
    return tuple(
        canonical_secret_key(resource_type, name, output)
        for output in RESOURCE_OUTPUTS[resource_type]
    )


def canonicalize_environment(
    resource_type: str, name: str, values: Mapping[str, str]
) -> dict[str, str]:
    mapping = OUTPUT_ENVIRONMENT_KEYS[resource_type]
    if values.keys() != set(ENVIRONMENT_KEYS[resource_type]):
        raise ValidationError("provider environment outputs are incomplete")
    if resource_type == "s3" and values["AWS_ENDPOINT_URL_S3"] != values["S3_ENDPOINT"]:
        raise ValidationError("provider endpoint outputs conflict")
    return {
        canonical_secret_key(resource_type, name, output): values[key]
        for output, key in mapping.items()
    }


def provider_environment(
    resource_type: str, name: str, values: Mapping[str, str]
) -> dict[str, str]:
    """Convert canonical app-variable items back at the provider boundary."""
    mapping = OUTPUT_ENVIRONMENT_KEYS[resource_type]
    expected = set(canonical_secret_keys(resource_type, name))
    if values.keys() != expected:
        raise ValidationError("managed-storage secret outputs are incomplete")
    result = {
        key: values[canonical_secret_key(resource_type, name, output)]
        for output, key in mapping.items()
    }
    if resource_type == "s3":
        result["S3_ENDPOINT"] = result["AWS_ENDPOINT_URL_S3"]
    return result


FIXED_PLATFORM_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {"NODE_ENV": "production", "PLATFORM_ENV": "production"}
)
PLATFORM_ENVIRONMENT_KEYS = frozenset(
    {*FIXED_PLATFORM_ENVIRONMENT, "PLATFORM_PROJECT_ID", "PLATFORM_PROJECT_SLUG", "PORT"}
)
RESERVED_ENVIRONMENT_PREFIX = "STORAGE__"


def platform_environment_values(
    application_id: str, application_slug: str, application_port: int
) -> dict[str, str]:
    identifier = uuid(application_id, field="application_id")
    checked_slug = slug(application_slug)
    if (
        isinstance(application_port, bool)
        or not isinstance(application_port, int)
        or not 1 <= application_port <= 65_535
    ):
        raise ValidationError("application_port must be from 1 through 65535")
    return {
        **FIXED_PLATFORM_ENVIRONMENT,
        "PLATFORM_PROJECT_ID": identifier,
        "PLATFORM_PROJECT_SLUG": checked_slug,
        "PORT": str(application_port),
    }
