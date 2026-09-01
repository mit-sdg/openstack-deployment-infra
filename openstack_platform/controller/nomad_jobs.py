"""Nomad job rendering and exact generated-job identity validation."""

from __future__ import annotations

import hashlib
import re
import uuid as uuid_module

from ..config import PlatformConfig
from ..contracts import (
    APPLICATION_HOST_PORT,
    DEPLOYMENT_ROUTE_HEADER,
    NOMAD_CANDIDATE_IMAGE_KEY,
    NOMAD_CANDIDATE_JOB_SHA_KEY,
    NOMAD_RECIPE_SHA_KEY,
    NOMAD_ROUTE_MARKER_KEY,
    NOMAD_SOURCE_COMMIT_KEY,
)
from ..validation import (
    ValidationError,
    bounded_text,
    commit,
    oci_digest_pin,
    sha256_hex,
    slug,
    uuid,
)
from .application_models import Manifest
from .storage_contract import canonical_secret_key, canonical_secret_keys


def render_nomad_job(
    *,
    application_id: str,
    application_slug: str,
    image: str,
    manifest: Manifest,
    platform: PlatformConfig,
    cpu_mhz: int,
    memory_mib: int,
    source_commit: str,
    recipe_hash: str,
    candidate: bool = False,
    placement_id: str | None = None,
    staged: bool = False,
    promoted: bool = False,
    route_marker: str | None = None,
    route_priority: int = 100,
) -> str:
    """Render one stable or isolated candidate Nomad job."""
    identifier = uuid(application_id, field="application ID")
    placement = uuid(placement_id or identifier, field="placement ID")
    marker_id = uuid(route_marker or identifier, field="route marker")
    app_slug = slug(application_slug)
    if not all(isinstance(value, bool) for value in (candidate, staged, promoted)):
        raise ValidationError("deployment route selectors must be boolean")
    if promoted and not staged:
        raise ValidationError("only a staged job can be route-promoted")
    if isinstance(route_priority, bool) or not 100 <= route_priority <= 1_000_000_000:
        raise ValidationError("route priority must be from 100 through 1000000000")
    job_id = f"{app_slug}-candidate" if candidate else app_slug
    image_pin = oci_digest_pin(image, field="application image")
    if isinstance(cpu_mhz, bool) or not isinstance(cpu_mhz, int) or not 100 <= cpu_mhz <= 2_000:
        raise ValidationError("scheduler CPU must be an integer from 100 through 2000 MHz")
    if (
        isinstance(memory_mib, bool)
        or not isinstance(memory_mib, int)
        or not 64 <= memory_mib <= 3_072
    ):
        raise ValidationError("scheduler memory must be an integer from 64 through 3072 MiB")
    checked_source = commit(source_commit)
    checked_recipe = sha256_hex(recipe_hash, field="recipe hash")
    hostname = f"{app_slug}.{platform.domain}"
    preview_hostname = f"{app_slug}-preview.{platform.domain}"
    variable = f"nomad/jobs/{app_slug}"
    routers = [(f"{job_id}-preview", preview_hostname, 100) if staged else (job_id, hostname, 100)]
    if promoted:
        routers.append((f"{job_id}-promoted", hostname, route_priority))
    route_tags = "\n".join(
        f"""        "traefik.http.routers.{router}.entrypoints=web",
        "traefik.http.routers.{router}.rule=Host(`{host}`)",
        "traefik.http.routers.{router}.priority={priority}",
        "traefik.http.routers.{router}.service={job_id}",
        "traefik.http.routers.{router}.middlewares={job_id}-headers","""
        for router, host, priority in routers
    )
    binding_aliases = "\n".join(
        f'{{{{ $value := index . "{canonical_secret_key(binding.resource_type, binding.name, output)}" }}}}\n{target}={{{{ $value | toJSON }}}}'
        for binding in manifest.storage_bindings
        for output, target in binding.environment
    )
    storage_keys = sorted(
        key
        for binding in manifest.storage_bindings
        for key in canonical_secret_keys(binding.resource_type, binding.name)
    )
    if not storage_keys:
        runtime_item = "{{ $key }}={{ $value | toJSON }}"
    else:
        comparisons = " ".join(f'(ne $key "{key}")' for key in storage_keys)
        predicate = comparisons if len(storage_keys) == 1 else f"and {comparisons}"
        runtime_item = (
            f"{{{{ if {predicate} }}}}{{{{ $key }}}}={{{{ $value | toJSON }}}}\n{{{{ end }}}}"
        )
    job = f'''job "{job_id}" {{
  region      = "{platform.region}"
  datacenters = ["{platform.datacenter}"]
  type        = "service"

  constraint {{
    attribute = "${{meta.application_id}}"
    operator  = "="
    value     = "{placement}"
  }}

  update {{
    max_parallel      = 1
    min_healthy_time  = "10s"
    healthy_deadline  = "3m"
    progress_deadline = "5m"
    auto_revert       = false
    canary            = 0
  }}

  group "app" {{
    count          = 1
    shutdown_delay = "5s"
    network {{
      mode = "bridge"
      port "http" {{
        static = {APPLICATION_HOST_PORT}
        to     = {manifest.port}
      }}
    }}

    restart {{
      attempts = 3
      interval = "5m"
      delay    = "10s"
      mode     = "fail"
    }}

    service {{
      provider     = "nomad"
      name         = "app-{job_id}"
      port         = "http"
      address_mode = "host"
      tags = [
        "{platform.namespace}.platform=true",
        "traefik.enable=true",
{route_tags}
        "traefik.http.services.{job_id}.loadbalancer.server.port={APPLICATION_HOST_PORT}",
        "traefik.http.middlewares.{job_id}-headers.headers.contenttypenosniff=true",
        "traefik.http.middlewares.{job_id}-headers.headers.referrerpolicy=no-referrer",
        "traefik.http.middlewares.{job_id}-headers.headers.customresponseheaders.{DEPLOYMENT_ROUTE_HEADER}={marker_id}",
      ]
      check {{
        name     = "http-ready"
        type     = "http"
        path     = "{manifest.health_path}"
        interval = "10s"
        timeout  = "3s"
      }}
    }}

    volume "internal-ca" {{
      type      = "host"
      source    = "{platform.namespace}-internal-ca"
      read_only = true
    }}

    task "app" {{
      driver = "docker"
      config {{
        image           = "{image_pin}"
        ports           = ["http"]
        force_pull      = true
        readonly_rootfs = true
        security_opt    = ["no-new-privileges:true"]
        cap_drop        = ["all"]
        cap_add         = ["net_bind_service"]
        pids_limit      = 256
      }}
      volume_mount {{
        volume      = "internal-ca"
        destination = "/platform-ca"
        read_only   = true
      }}
      env {{
        HOST = "0.0.0.0"
        PORT = "{manifest.port}"
      }}
      template {{
        destination = "secrets/app.env"
        env         = true
        perms       = "0600"
        change_mode = "restart"
        data = <<EOH
{{{{ with nomadVar "{variable}" }}}}
{{{{ range $key, $value := . }}}}{runtime_item}
{{{{ end }}}}
{binding_aliases}
{{{{ end }}}}
EOH
      }}
      resources {{
        cpu    = {cpu_mhz}
        memory = {memory_mib}
      }}
      logs {{
        max_files     = 3
        max_file_size = 10
      }}
      kill_timeout = "30s"
    }}
  }}
}}
'''
    # Nomad normalizes submitted HCL, so recovery cannot compare the inspected
    # JSON to the original text. Embed identities for both the exact generated
    # job and its immutable image. A repeated submission can then distinguish
    # this candidate from an accepted predecessor without guessing by version.
    identity = hashlib.sha256(job.encode()).hexdigest()
    source_markers = (
        ""
        if checked_source is None
        else f'    {NOMAD_SOURCE_COMMIT_KEY} = "{checked_source}"\n'
        f'    {NOMAD_RECIPE_SHA_KEY} = "{checked_recipe}"\n'
    )
    marker = f'''  meta {{
    {NOMAD_CANDIDATE_JOB_SHA_KEY} = "{identity}"
    {NOMAD_CANDIDATE_IMAGE_KEY} = "{image_pin}"
    {NOMAD_ROUTE_MARKER_KEY} = "{marker_id}"
{source_markers}  }}

'''
    return job.replace(f'job "{job_id}" {{\n', f'job "{job_id}" {{\n{marker}', 1)


_JOB_MARKER_BLOCK = re.compile(
    r"  meta \{\n"
    rf'    {re.escape(NOMAD_CANDIDATE_JOB_SHA_KEY)}\s*=\s*"([0-9a-f]{{64}})"\n'
    rf'    {re.escape(NOMAD_CANDIDATE_IMAGE_KEY)}\s*=\s*"([^"\n]+@sha256:[0-9a-f]{{64}})"\n'
    rf'(?:    {re.escape(NOMAD_ROUTE_MARKER_KEY)}\s*=\s*"[0-9a-f-]{{36}}"\n)?'
    rf'    {re.escape(NOMAD_SOURCE_COMMIT_KEY)}\s*=\s*"[0-9a-f]{{40}}"\n'
    rf'    {re.escape(NOMAD_RECIPE_SHA_KEY)}\s*=\s*"[0-9a-f]{{64}}"\n'
    r"  \}\n\n"
)


def nomad_job_id(nomad_job: str, application_slug: str) -> str:
    """Return the only stable or candidate job ID allowed for an application."""
    job = bounded_text(nomad_job, field="Nomad job", maximum=262_144)
    app_slug = slug(application_slug)
    match = re.match(r'^job "([^"\n]+)" \{\n', job)
    if match is None or match.group(1) not in {app_slug, f"{app_slug}-candidate"}:
        raise ValidationError("Nomad job ID is not the stable or candidate application job")
    return match.group(1)


def deployment_worker_ids(application_id: str) -> tuple[str, str]:
    """Return the two bounded worker identities available for alternating jobs."""
    identifier = uuid(application_id, field="application ID")
    namespace = uuid_module.UUID(identifier)
    return (
        str(uuid_module.uuid5(namespace, "stable")),
        str(uuid_module.uuid5(namespace, "candidate")),
    )


def nomad_placement_id(nomad_job: str) -> str:
    job = bounded_text(nomad_job, field="Nomad job", maximum=262_144)
    matches = re.findall(
        r'    attribute = "\$\{meta\.application_id\}"\n'
        r'    operator  = "="\n'
        r'    value     = "([0-9a-f-]{36})"',
        job,
    )
    if len(matches) != 1:
        raise ValidationError("Nomad job placement identity was missing or ambiguous")
    return uuid(matches[0], field="Nomad placement ID")


def nomad_route_priority(nomad_job: str) -> int:
    job = bounded_text(nomad_job, field="Nomad job", maximum=262_144)
    priorities = [int(value) for value in re.findall(r'\.priority=([0-9]+)",', job)]
    return max(priorities, default=0)


def nomad_route_marker(nomad_job: str) -> str:
    job = bounded_text(nomad_job, field="Nomad job", maximum=262_144)
    matches = re.findall(rf'    {re.escape(NOMAD_ROUTE_MARKER_KEY)} = "([0-9a-f-]{{36}})"\n', job)
    if len(matches) != 1:
        raise ValidationError("Nomad route marker was missing or ambiguous")
    return uuid(matches[0], field="Nomad route marker")


def nomad_candidate_identity(nomad_job: str) -> tuple[str, str]:
    """Return the exact generated candidate identity.

    Unmarked jobs are unmanaged state and are never treated as deployable or
    candidate identity evidence.
    """
    job = bounded_text(nomad_job, field="Nomad job", maximum=262_144)
    markers = list(_JOB_MARKER_BLOCK.finditer(job))
    if len(markers) != 1:
        raise ValidationError("Nomad job must contain exactly one platform candidate marker")
    marker = markers[0]
    identity, image = marker.groups()
    unmarked_job = job[: marker.start()] + job[marker.end() :]
    if hashlib.sha256(unmarked_job.encode()).hexdigest() != identity:
        raise ValidationError("Nomad job candidate identity does not match the exact job")
    return identity, oci_digest_pin(image, field="Nomad candidate image")
