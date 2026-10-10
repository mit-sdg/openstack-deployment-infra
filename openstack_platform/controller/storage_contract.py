"""Canonical secret-free contracts shared by runtime and storage boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..validation import ValidationError, resource_name, slug, uuid

# Public contract for callers that must preserve the controller's existing
# secret-key rejection behavior without importing controller database code.
SECRET_KEY_PATTERN = r"(?:^|_)(?:password|passwd|secret|token|credential|private_key|user_data|cloud_init|env_value|source_contents?)(?:$|_)"

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
            "public_endpoint",
            "region",
            "access_key_id",
            "secret_access_key",
            "bucket",
        ),
    }
)
# Outputs that come from platform configuration rather than a provider. The job
# renders them directly, so they are never stored with the credentials.
DERIVED_OUTPUTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"postgres": (), "mongo": (), "s3": ("public_endpoint",)}
)
# The public S3 API answers at this label of the platform domain, which no app
# may use as its slug. Browsers reach it through public ingress, so presigned
# URLs signed for it work from an app's pages.
PUBLIC_S3_LABEL = "s3"
# Outputs earlier releases published. Existing app variables can still hold
# their keys until the resource's credentials are next written, which removes
# them; they never reach an app's environment. S3 clients need neither: the
# endpoint is an IP address, so they address buckets by path, and every app
# trusts the platform CA through NODE_EXTRA_CA_CERTS.
RETIRED_OUTPUTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"postgres": (), "mongo": (), "s3": ("ca_bundle", "force_path_style")}
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
            "S3_ENDPOINT",
            "S3_BUCKET",
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
                "public_endpoint": "S3_PUBLIC_ENDPOINT",
                "region": "AWS_REGION",
                "access_key_id": "AWS_ACCESS_KEY_ID",
                "secret_access_key": "AWS_SECRET_ACCESS_KEY",
                "bucket": "S3_BUCKET",
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
    return _secret_key(resource_type, checked_name, output)


def _secret_key(resource_type: str, checked_name: str, output: str) -> str:
    # Hyphens are the only non-alphanumeric resource-name character, making this
    # encoding injective. The prefix is reserved from staff runtime keys.
    return f"STORAGE__{resource_type.upper()}__{checked_name.replace('-', '_').upper()}__{output.upper()}"


def stored_outputs(resource_type: str) -> tuple[str, ...]:
    """Outputs a provider writes into the app Variable."""
    derived = DERIVED_OUTPUTS[resource_type]
    return tuple(output for output in RESOURCE_OUTPUTS[resource_type] if output not in derived)


def canonical_secret_keys(resource_type: str, name: str) -> tuple[str, ...]:
    return tuple(
        canonical_secret_key(resource_type, name, output)
        for output in stored_outputs(resource_type)
    )


def public_s3_endpoint(domain: str) -> str:
    return f"https://{PUBLIC_S3_LABEL}.{domain}"


def derived_output(resource_type: str, output: str, *, domain: str) -> str:
    if output not in DERIVED_OUTPUTS.get(resource_type, ()):
        raise ValidationError("managed-storage output is not derived")
    return public_s3_endpoint(domain)


def retired_secret_keys(resource_type: str, name: str) -> tuple[str, ...]:
    """Keys of RETIRED_OUTPUTS that an older release may have stored."""
    checked_name = resource_name(name)
    if resource_type not in RESOURCE_TYPES:
        raise ValidationError("storage type must be postgres, mongo, or s3")
    return tuple(
        _secret_key(resource_type, checked_name, output)
        for output in RETIRED_OUTPUTS[resource_type]
    )


def _stored_mapping(resource_type: str) -> dict[str, str]:
    outputs = stored_outputs(resource_type)
    return {
        output: key
        for output, key in OUTPUT_ENVIRONMENT_KEYS[resource_type].items()
        if output in outputs
    }


def canonicalize_environment(
    resource_type: str, name: str, values: Mapping[str, str]
) -> dict[str, str]:
    mapping = _stored_mapping(resource_type)
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
    mapping = _stored_mapping(resource_type)
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
# Every app container mounts the platform CA here (see nomad_jobs).
APPLICATION_CA_PATH = "/platform-ca/internal-ca.crt"
# Rendered straight into each app job, like HOST. Unlike the values above they
# never enter the app's Variable, so running jobs stay untouched until their
# next deploy.
JOB_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        # Node and Bun verify the platform's TLS services (S3, PostgreSQL,
        # MongoDB) with no client settings.
        "NODE_EXTRA_CA_CERTS": APPLICATION_CA_PATH,
        # The AWS SDK otherwise signs an empty-body CRC32 into presigned PUT
        # URLs, which Garage then rejects for any real upload.
        "AWS_REQUEST_CHECKSUM_CALCULATION": "when_required",
    }
)
# Names owners and staff cannot set or bind.
RESERVED_ENVIRONMENT_KEYS = PLATFORM_ENVIRONMENT_KEYS | frozenset(JOB_ENVIRONMENT)
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


# Shared legacy services remain only for migration. Instance budgets are in
# instance_contract and are reserved across PostgreSQL and MongoDB together.
POSTGRES_GLOBAL_CONNECTIONS = 100
MONGO_GLOBAL_CONNECTIONS = 100
MAX_STORAGE_BYTES = 500 * 1024**3
MIN_STORAGE_BYTES = 1024**2


def validate_quotas(resource_type: str, value: object) -> dict[str, int]:
    if resource_type in {"postgres", "mongo"}:
        from ..instance_contract import validate_limits

        return validate_limits(value)
    if (
        resource_type != "s3"
        or not isinstance(value, dict)
        or set(value) != {"s3Bytes", "s3Objects"}
    ):
        raise ValidationError("quotas must contain exactly the resource's applicable fields")
    result: dict[str, int] = {}
    for name, number in value.items():
        lower, upper = (
            (1, 100_000_000) if name == "s3Objects" else (MIN_STORAGE_BYTES, MAX_STORAGE_BYTES)
        )
        if isinstance(number, bool) or not isinstance(number, int) or not lower <= number <= upper:
            raise ValidationError(f"{name} must be an integer from {lower} through {upper}")
        result[name] = number
    return result


def validate_expected_quotas(resource_type: str, value: object) -> dict[str, int]:
    fields = (
        {"s3Bytes", "s3Objects"}
        if resource_type == "s3"
        else {"sizeBytes", "connections", "memoryBytes", "cpuMillicores"}
    )
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or any(
            isinstance(number, bool) or not isinstance(number, int) or not 0 < number <= 2**63 - 1
            for number in value.values()
        )
    ):
        raise ValidationError("expectedQuotas must be the complete accepted integer quota snapshot")
    return dict(value)
