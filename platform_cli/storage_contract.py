"""Canonical secret-free contracts shared by runtime and storage boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .validation import ValidationError, slug, uuid

RESOURCE_TYPES = ("postgres", "mongo", "s3")
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
ALL_ENVIRONMENT_KEYS = frozenset(
    key for resource_keys in ENVIRONMENT_KEYS.values() for key in resource_keys
)
FIXED_PLATFORM_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        "NODE_ENV": "production",
        "PLATFORM_ENV": "production",
    }
)
PLATFORM_ENVIRONMENT_KEYS = frozenset(
    {
        *FIXED_PLATFORM_ENVIRONMENT,
        "PLATFORM_PROJECT_ID",
        "PLATFORM_PROJECT_SLUG",
        "PORT",
    }
)


def platform_environment_values(
    application_id: str,
    application_slug: str,
    application_port: int,
) -> dict[str, str]:
    """Return the complete exact platform-owned runtime environment."""
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


__all__ = [
    "ALL_ENVIRONMENT_KEYS",
    "ENVIRONMENT_KEYS",
    "FIXED_PLATFORM_ENVIRONMENT",
    "PLATFORM_ENVIRONMENT_KEYS",
    "RESOURCE_TYPES",
    "platform_environment_values",
]
