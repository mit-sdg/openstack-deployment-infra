#!/usr/bin/env python3
"""Import an authenticated legacy v5 controller snapshot into the current schema."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid as uuid_module
from collections import defaultdict
from pathlib import Path
from typing import Any, NoReturn, cast

from openstack_platform.config import load_platform
from openstack_platform.controller import database as db
from openstack_platform.controller.deployment_config import (
    DeploymentConfiguration,
    parse_configuration,
)

_LEGACY_MARKER_PREFIX = b"openstack-platform:\x6d1:deployment:"
_LEGACY_MIGRATIONS = {
    1: "cf8d18f643190729f6170502f7dba1ef205c3606c9bffb4755f85ddd71439d7c",
    2: "838dfcdf2836b43ba710e074719ff7ad6df6a05129ab6623581dd8b507273bac",
    3: "e8df44865e5556cfeabbfb4247da67e7f4d8f12b4ff679062be24ea0953dddcb",
    4: "005b20d03b9bc92d1f4db6a18e1fff3b71da16ab2a2ac28ecf8336a84b71c2ad",
    5: "8c49d7273c89a13a7cae3a7e4d2de6a94234bbe28a6088ca47f75520b780f1ae",
}
_LEGACY_TABLES = {
    "applications",
    "deployments",
    "environment_keys",
    "image_selections",
    "managed_resources",
    "operations",
    "schema_migrations",
}
_STORAGE_NAMESPACE = uuid_module.UUID("65854878-fd68-4d8f-9908-fd698a156b19")


class ImportFailure(RuntimeError):
    """The legacy snapshot could not be authenticated or imported safely."""


def _fail(message: str) -> NoReturn:
    raise ImportFailure(message)


def _private_file(path: Path, *, label: str) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        _fail(f"{label} must be a direct current-user-owned mode-0600 file")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _legacy_marker(identity: db.DeploymentIdentity) -> str:
    encoded = json.dumps(
        {
            "projectId": identity.project_id,
            "namespace": identity.namespace,
            "configIdentity": identity.config_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(_LEGACY_MARKER_PREFIX + encoded).hexdigest()


def _validate_legacy(connection: sqlite3.Connection, identity: db.DeploymentIdentity) -> None:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        _fail("legacy SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        _fail("legacy SQLite foreign-key check failed")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != _LEGACY_TABLES:
        _fail("legacy SQLite table set is unsupported")
    migrations = dict(
        connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
    )
    expected = {0: _legacy_marker(identity), **_LEGACY_MIGRATIONS}
    if migrations != expected:
        _fail("legacy SQLite marker or migration checksums are unsupported")
    unfinished = connection.execute(
        "SELECT 1 FROM operations WHERE status IN ('running', 'recovery_required') LIMIT 1"
    ).fetchone()
    if unfinished is not None:
        _fail("legacy SQLite has an unfinished operation")


def _load_mapping(path: Path) -> dict[str, Any]:
    _private_file(path, label="legacy application mapping")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImportFailure("legacy application mapping is malformed") from error
    if not isinstance(value, dict) or not value:
        _fail("legacy application mapping must be a nonempty object")
    return cast(dict[str, Any], value)


def _storage_id(identity: db.DeploymentIdentity, application_id: str, kind: str, name: str) -> str:
    material = f"{identity.project_id}:{identity.namespace}:{application_id}:{kind}:{name}"
    return str(uuid_module.uuid5(_STORAGE_NAMESPACE, material))


def _configuration(
    raw: object,
    resources: dict[tuple[str, str], str],
) -> tuple[str, DeploymentConfiguration]:
    if not isinstance(raw, dict) or set(raw) != {"requestedRef", "configuration"}:
        _fail("legacy application mapping entry is malformed")
    requested_ref = raw["requestedRef"]
    configuration = raw["configuration"]
    if not isinstance(requested_ref, str) or not isinstance(configuration, dict):
        _fail("legacy application requested ref or configuration is malformed")
    if set(configuration) != {"schemaVersion", "build", "runtime", "storageBindings"}:
        _fail("legacy application configuration fields are malformed")
    bindings = configuration["storageBindings"]
    if not isinstance(bindings, list):
        _fail("legacy application storage bindings are malformed")
    converted: list[dict[str, object]] = []
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {
            "resourceType",
            "resourceName",
            "outputs",
        }:
            _fail("legacy application storage binding is malformed")
        resource_type = binding["resourceType"]
        resource_name = binding["resourceName"]
        if not isinstance(resource_type, str) or not isinstance(resource_name, str):
            _fail("legacy application storage binding identity is malformed")
        key = (resource_type, resource_name)
        resource_id = resources.get(key)
        if resource_id is None or not isinstance(binding["outputs"], dict):
            _fail("legacy application storage binding has no imported resource")
        converted.append({"resourceId": resource_id, "outputs": binding["outputs"]})
    parsed = parse_configuration({**configuration, "storageBindings": converted})
    parsed.manifest({identifier: key for key, identifier in resources.items()})
    return requested_ref, parsed


def _copy_build_log(source_state: Path, destination_state: Path, relative: str) -> None:
    source = (source_state / relative).resolve()
    try:
        source.relative_to(source_state.resolve())
    except ValueError:
        _fail("legacy build log escapes its state directory")
    _private_file(source, label="legacy build log")
    destination = destination_state / relative
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


def import_legacy(
    *,
    source_database: Path,
    source_state: Path,
    destination_state: Path,
    platform_path: Path,
    mapping_path: Path,
) -> Path:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        _fail("run legacy controller import as the unprivileged platform owner")
    _private_file(source_database, label="legacy SQLite snapshot")
    if destination_state.exists():
        _fail("legacy import destination must be absent")
    parent = destination_state.parent
    metadata = parent.lstat()
    if not parent.is_dir() or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        _fail("legacy import destination parent is unsafe")
    platform = load_platform(platform_path)
    identity = db.deployment_identity(platform)
    mapping = _load_mapping(mapping_path)

    source = sqlite3.connect(f"file:{source_database}?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    try:
        _validate_legacy(source, identity)
        applications = source.execute("SELECT * FROM applications ORDER BY slug").fetchall()
        deployments = {
            row["application_id"]: row
            for row in source.execute("SELECT * FROM deployments").fetchall()
        }
        images = source.execute("SELECT * FROM image_selections ORDER BY role").fetchall()
        resources = source.execute(
            "SELECT * FROM managed_resources ORDER BY application_id, resource_type, resource_name"
        ).fetchall()
        environment = source.execute(
            "SELECT * FROM environment_keys ORDER BY application_id, owner, key_name"
        ).fetchall()
        if set(mapping) != {row["slug"] for row in applications}:
            _fail("legacy application mapping does not match the snapshot")

        destination_state.mkdir(mode=0o700)
        destination_path = destination_state / "platform.sqlite3"
        destination = db.connect(destination_path, identity=identity)
        try:
            db.migrate(destination, identity=identity)
            for row in images:
                db.put_image_selection(
                    destination,
                    role=row["role"],
                    image_id=row["image_id"],
                    display_name=row["display_name"],
                    source_commit=row["source_commit"],
                    compatibility_hash=row["compatibility_hash"],
                    selected_at=row["selected_at"],
                )
            resources_by_application: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
            for row in resources:
                resources_by_application[row["application_id"]][
                    (row["resource_type"], row["resource_name"])
                ] = _storage_id(
                    identity,
                    row["application_id"],
                    row["resource_type"],
                    row["resource_name"],
                )
            owners: dict[tuple[str, str], list[str]] = defaultdict(list)
            for row in environment:
                owners[(row["application_id"], row["owner"])].append(row["key_name"])
            for row in applications:
                application_id = row["application_id"]
                if application_id not in deployments:
                    _fail("legacy application has no accepted deployment")
                accepted = deployments[application_id]
                requested_ref, configuration = _configuration(
                    mapping[row["slug"]], resources_by_application[application_id]
                )
                db.put_application(
                    destination,
                    application_id=application_id,
                    application_slug=row["slug"],
                    repository_url=row["repository_url"],
                    desired_running=bool(row["desired_running"]),
                    url=row["url"],
                    worker_server_id=row["worker_server_id"],
                    worker_server_name=row["worker_server_name"],
                    worker_port_id=row["worker_port_id"],
                    worker_port_name=row["worker_port_name"],
                    worker_flavor=row["worker_flavor"],
                    scheduler_cpu_mhz=row["scheduler_cpu_mhz"],
                    scheduler_memory_mib=row["scheduler_memory_mib"],
                    now=row["created_at"],
                )
                for resource in resources:
                    if resource["application_id"] != application_id:
                        continue
                    resource_id = resources_by_application[application_id][
                        (resource["resource_type"], resource["resource_name"])
                    ]
                    db.put_managed_resource(
                        destination,
                        resource_id=resource_id,
                        application_id=application_id,
                        resource_type=resource["resource_type"],
                        resource_name=resource["resource_name"],
                        provider_id=resource["provider_id"],
                        provider_name=resource["provider_name"],
                        lifecycle_state=resource["lifecycle_state"],
                        postgres_connections=resource["postgres_connections"],
                        measured_target_bytes=resource["measured_target_bytes"],
                        s3_bytes=resource["s3_bytes"],
                        s3_objects=resource["s3_objects"],
                        last_verified_at=resource["last_verified_at"],
                        now=resource["created_at"],
                    )
                for (owner_application, owner), keys in owners.items():
                    if owner_application == application_id:
                        db.set_environment_keys(
                            destination,
                            application_id=application_id,
                            owner=owner,
                            keys=keys,
                        )
                revision = db.advance_environment_revision(
                    destination, application_id=application_id, expected_revision=0
                ).revision
                operation = source.execute(
                    "SELECT * FROM operations WHERE kind = 'app.deploy' AND scope = ? "
                    "AND status = 'succeeded' AND phase = 'accepted' AND candidate_digest = ? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (f"app-{application_id}", accepted["image_digest"]),
                ).fetchone()
                if operation is None:
                    _fail("legacy accepted deployment has no terminal operation identity")
                assert operation is not None
                deployment_id = operation["operation_id"]
                fingerprint = hashlib.sha256(
                    f"legacy-import:{application_id}:{deployment_id}".encode()
                ).hexdigest()
                db.claim_idempotency_request(
                    destination,
                    request_id=deployment_id,
                    request_fingerprint=fingerprint,
                    now=operation["started_at"],
                )
                db.create_deployment_attempt(
                    destination,
                    deployment_id=deployment_id,
                    application_id=application_id,
                    source_commit=accepted["source_commit"],
                    requested_ref=requested_ref,
                    configuration_revision=1,
                    configuration=configuration,
                    environment_revision=revision,
                    idempotency_request_id=deployment_id,
                    now=operation["started_at"],
                )
                db.checkpoint_deployment_attempt(
                    destination,
                    deployment_id,
                    status="succeeded",
                    recipe_hash=accepted["recipe_hash"],
                    image_digest=accepted["image_digest"],
                    nomad_job=accepted["nomad_job"],
                    nomad_job_sha256=accepted["nomad_job_sha256"],
                    nomad_version=accepted["nomad_version"],
                    build_log_path=accepted["build_log_path"],
                    cleanup_state="confirmed",
                    now=accepted["accepted_at"],
                )
                _copy_build_log(source_state, destination_state, accepted["build_log_path"])
            db.validate_complete_schema(destination, identity=identity)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                _fail("imported SQLite integrity check failed")
            if destination.execute("PRAGMA foreign_key_check").fetchall():
                _fail("imported SQLite foreign-key check failed")
        finally:
            destination.close()
    except BaseException:
        if destination_state.exists():
            shutil.rmtree(destination_state)
        raise
    finally:
        source.close()

    receipt = {
        "format": 1,
        "sourceSha256": _sha256(source_database),
        "destinationSha256": _sha256(destination_path),
        "applications": len(applications),
        "storageResources": len(resources),
        "imageSelections": len(images),
        "schemaVersion": db.MIGRATIONS[-1].version,
    }
    receipt_path = destination_state / "LEGACY-IMPORT-RECEIPT.json"
    descriptor = os.open(
        receipt_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return destination_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-database", type=Path, required=True)
    parser.add_argument("--source-state", type=Path, required=True)
    parser.add_argument("--destination-state", type=Path, required=True)
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--application-mapping", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        destination = import_legacy(
            source_database=args.source_database,
            source_state=args.source_state,
            destination_state=args.destination_state,
            platform_path=args.platform,
            mapping_path=args.application_mapping,
        )
    except (ImportFailure, OSError, sqlite3.DatabaseError) as error:
        print(f"legacy controller import failed: {error}", file=sys.stderr)
        return 1
    print(f"legacy-controller-import=verified destination={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
