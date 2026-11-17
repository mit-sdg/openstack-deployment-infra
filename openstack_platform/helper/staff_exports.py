"""Staff snapshots of one app database, kept in a private Garage bucket for a day.

A snapshot is a zip made on the admin host: readable files for staff, a
restorable dump, and a manifest with checksums. The helper uploads it to a
bucket that no app key, backup key or website can reach, using a key that can
use only that bucket. Results carry object metadata and short-lived links,
never the key.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import durable
from ..validation import ValidationError, bounded_text, resource_name, slug, uuid
from . import storage
from .main import HelperActionError

EXPORT_LIMIT_BYTES = 2 * 1024**3
# Uploaded snapshots are deleted a day after they were taken.
RETENTION = timedelta(hours=24)
LINK_SECONDS = 300
# Room for a few snapshots of the largest allowed size at once.
BUCKET_QUOTA_BYTES = 20 * 1024**3
BUCKET_QUOTA_OBJECTS = 10_000
# A helper killed mid-export leaves its work directory; exports end long before this.
STALE_WORK_SECONDS = 6 * 3600
FORMAT = "platform-staff-snapshot-v1"
EXPORT_ARGS = frozenset(
    {
        "applicationId",
        "applicationSlug",
        "resourceId",
        "resourceName",
        "displayLabel",
        "providerName",
        "instanceId",
        "deadlineAt",
    }
)
OBJECT_KEY = re.compile(
    r"exports/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
    r"([0-9]{8}T[0-9]{6}Z)-[0-9a-f]{16}\.zip"
)
_SAFE_COLLECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_KEY_ID = re.compile(r"GK[a-f0-9]{24}")
_SECRET = re.compile(r"[A-Za-z0-9+/=_-]{20,128}")
_CONSISTENCY = {
    "mongo": (
        "Each collection was read in one pass while the app kept running, so "
        "collections can be a few seconds apart."
    ),
    "postgres": "pg_dump read the whole database from one transaction snapshot.",
}


def _too_large() -> HelperActionError:
    return HelperActionError(
        "EXPORT_TOO_LARGE", "the database is larger than the 2 GiB snapshot limit"
    )


def _expired() -> HelperActionError:
    return HelperActionError("EXPORT_NOT_FOUND", "the snapshot has expired or was deleted")


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Budget:
    """Bytes a snapshot may still write; one budget covers every file in it."""

    def __init__(self, limit: int | None = None) -> None:
        self.remaining = EXPORT_LIMIT_BYTES if limit is None else limit

    def take(self, size: int) -> None:
        self.remaining -= size
        if self.remaining < 0:
            raise _too_large()


@dataclass(frozen=True, slots=True)
class ExportRequest:
    kind: str
    application_id: str
    application_slug: str
    resource_id: str
    resource_name: str
    label: str
    database: str
    instance_id: str


def export_request(args: Mapping[str, Any], kind: str) -> ExportRequest:
    """Validate the exact export arguments and bind the database to its app."""
    if kind not in _CONSISTENCY:
        raise HelperActionError("UNKNOWN_ACTION", "storage export type is not registered")
    if args.keys() != EXPORT_ARGS:
        raise HelperActionError("INVALID_ARGS", f"storage.{kind}.export arguments are invalid")
    application_id = uuid(args["applicationId"], field="application ID")
    name = resource_name(args["resourceName"])
    label = bounded_text(args["displayLabel"], field="storage label", maximum=400)
    if not label.strip():
        raise ValidationError("storage label must not be empty")
    # The database name derives from the app and resource, so a request can
    # name only the database that belongs to the app it names.
    token = storage._RESOURCE_CONTEXT.set((kind, name))
    try:
        expected = storage._identity(application_id)[1]
    finally:
        storage._RESOURCE_CONTEXT.reset(token)
    if args["providerName"] != expected:
        raise HelperActionError("IDENTITY_MISMATCH", "database does not belong to this app")
    return ExportRequest(
        kind=kind,
        application_id=application_id,
        application_slug=slug(args["applicationSlug"]),
        resource_id=uuid(args["resourceId"], field="resource ID"),
        resource_name=name,
        label=label,
        database=expected,
        instance_id=uuid(args["instanceId"], field="instance ID"),
    )


def bucket_name(namespace: str, prefix: str) -> str:
    """The exports bucket. App buckets, which backups copy, start with the prefix."""
    name = f"staff-exports-{namespace}"
    if name.startswith(f"{prefix}-") or len(name) > 63:
        raise HelperActionError(
            "DEPENDENCY_UNAVAILABLE", "the staff export bucket name would overlap app buckets"
        )
    return name


def download_name(request: ExportRequest, taken: datetime) -> str:
    label = re.sub(r"[^a-z0-9]+", "-", request.label.lower()).strip("-")[:40].strip("-")
    stamp = taken.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{request.application_slug}-{label or request.kind}-{stamp}.zip"


def object_key(resource_id: str, taken: datetime) -> str:
    stamp = taken.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"exports/{uuid(resource_id, field='resource ID')}/{stamp}-{secrets.token_hex(8)}.zip"


def checked_object_key(value: object) -> str:
    if not isinstance(value, str) or not OBJECT_KEY.fullmatch(value):
        raise HelperActionError("INVALID_ARGS", "snapshot object key is invalid")
    return value


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024**2):
            result.update(chunk)
    return result.hexdigest()


def _private_output(path: Path) -> Any:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    return os.fdopen(descriptor, "wb")


def describe(directory: Path, name: str, **extra: Any) -> dict[str, Any]:
    path = directory / name
    return {"name": name, **extra, "bytes": path.stat().st_size, "sha256": file_digest(path)}


def write_collections(
    database: Any, directory: Path, budget: Budget, deadline: float
) -> list[dict[str, Any]]:
    """One JSON Lines file per collection, in relaxed Extended JSON."""
    from bson import json_util

    options = json_util.RELAXED_JSON_OPTIONS
    (directory / "collections").mkdir(mode=0o700)
    names = sorted(
        str(item["name"])
        for item in database.list_collections()
        if item.get("type", "collection") != "view" and not str(item["name"]).startswith("system.")
    )
    entries: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, collection in enumerate(names, start=1):
        # Odd names, or ones that differ only by case, get a numbered file;
        # the manifest maps every file back to its collection.
        stem = collection if _SAFE_COLLECTION.fullmatch(collection) else ""
        if not stem or stem.lower() in used:
            stem = f"collection-{index}"
        used.add(stem.lower())
        name = f"collections/{stem}.jsonl"
        documents = 0
        with _private_output(directory / name) as output:
            for document in database[collection].find({}, batch_size=500):
                line = (json_util.dumps(document, json_options=options) + "\n").encode()
                budget.take(len(line))
                output.write(line)
                documents += 1
                if documents % 500 == 0 and time.monotonic() > deadline:
                    raise HelperActionError("DEADLINE_EXPIRED", "the snapshot ran out of time")
        entries.append(describe(directory, name, collection=collection, documents=documents))
    return entries


def run_dump(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    path: Path,
    budget: Budget,
    deadline: float,
) -> None:
    """Stream one dump command into a private file, stopping at the size limit."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HelperActionError("DEADLINE_EXPIRED", "the snapshot ran out of time")
    with _private_output(path) as output:
        # The dump stays in the helper's process group, so a controller
        # timeout that stops the helper also stops it.
        process = subprocess.Popen(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
        )
        timer = threading.Timer(remaining, process.kill)
        timer.start()
        try:
            assert process.stdout is not None
            while chunk := process.stdout.read(1024**2):
                budget.take(len(chunk))
                output.write(chunk)
            status = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
    if time.monotonic() >= deadline:
        raise HelperActionError("DEADLINE_EXPIRED", "the snapshot ran out of time")
    if status != 0:
        raise HelperActionError("EXPORT_FAILED", f"{Path(argv[0]).name} did not finish")


def mongo_dump_argv(tool: str, configuration: Path, database: str) -> list[str]:
    # No --dumpDbUsersAndRoles: staff need the data, not the app's credentials.
    return [
        tool,
        "--config",
        str(configuration),
        "--db",
        database,
        "--gzip",
        "--archive",
        "--numParallelCollections=1",
    ]


def postgres_dump_argv(tool: str, host: str, port: int, database: str) -> list[str]:
    return [
        tool,
        "--no-password",
        "--format=plain",
        "--no-owner",
        "--no-privileges",
        "--host",
        host,
        "--port",
        str(port),
        "--username",
        "platform_admin",
        "--dbname",
        database,
    ]


def manifest(
    request: ExportRequest, taken: datetime, files: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "app": request.application_slug,
        "applicationId": request.application_id,
        "resourceId": request.resource_id,
        "resourceLabel": request.label,
        "databaseType": request.kind,
        "database": request.database,
        "takenAt": timestamp(taken),
        "consistency": _CONSISTENCY[request.kind],
        "files": [dict(item) for item in files],
    }


def build_archive(
    directory: Path, files: Sequence[Mapping[str, Any]], summary: Mapping[str, Any], path: Path
) -> None:
    """Zip the content files, removing each once it is in the archive."""
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for item in files:
            source = directory / item["name"]
            archive.write(
                source,
                arcname=item["name"],
                # gzip output does not compress further.
                compress_type=zipfile.ZIP_STORED
                if item["name"].endswith(".gz")
                else zipfile.ZIP_DEFLATED,
            )
            source.unlink()
        archive.writestr("MANIFEST.json", json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    if path.stat().st_size > EXPORT_LIMIT_BYTES:
        raise _too_large()


def require_space(directory: Path) -> None:
    # The content files and the zip can exist side by side at full size.
    if shutil.disk_usage(directory).free < 2 * EXPORT_LIMIT_BYTES:
        raise HelperActionError(
            "DEPENDENCY_UNAVAILABLE", "the admin host has too little free disk for a snapshot"
        )


def sweep_work(directory: Path, now: float) -> None:
    """Remove work left by an export that was stopped before it cleaned up."""
    for child in directory.iterdir():
        try:
            metadata = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(metadata.st_mode) and now - metadata.st_mtime > STALE_WORK_SECONDS:
            shutil.rmtree(child, ignore_errors=True)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def read_key(path: Path) -> tuple[str, str] | None:
    """The exports key, if one was stored; it must be a private direct file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > 1024
        ):
            raise HelperActionError("INVALID_STATE", "staff export key is not a private file")
        text = os.read(descriptor, 1025).decode("ascii", errors="replace")
    finally:
        os.close(descriptor)
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    access, secret = values.get("ACCESS_KEY_ID", ""), values.get("SECRET_ACCESS_KEY", "")
    if not _KEY_ID.fullmatch(access) or not _SECRET.fullmatch(secret):
        raise HelperActionError("INVALID_STATE", "staff export key is malformed")
    return access, secret


def ensure_store(
    admin: Any, *, bucket: str, key_name: str, key_file: Path, lock_file: Path
) -> tuple[str, str]:
    """Create the private bucket and its key once; later calls only confirm them."""
    with _locked(lock_file):
        bucket_id = storage._s3_bucket_id(admin, bucket)
        if bucket_id is None:
            created = admin.request("/CreateBucket", {"globalAlias": bucket})
            if not isinstance(created, Mapping) or not isinstance(created.get("id"), str):
                raise HelperActionError(
                    "PROVIDER_RESPONSE_INVALID", "Garage bucket identity is invalid"
                )
            bucket_id = created["id"]
        # A new bucket has no website access, and nothing here enables it.
        admin.request(
            "/UpdateBucket",
            {"quotas": {"maxSize": BUCKET_QUOTA_BYTES, "maxObjects": BUCKET_QUOTA_OBJECTS}},
            {"id": bucket_id},
        )
        named = {
            str(item.get("accessKeyId", item.get("id")))
            for item in storage._s3_key_inventory(admin)
            if isinstance(item, Mapping) and item.get("name") == key_name
        }
        stored = read_key(key_file)
        if stored is None or stored[0] not in named:
            # A key whose secret was never stored can't be used again.
            for orphan in sorted(named):
                admin.request("/DeleteKey", {}, {"id": orphan})
            created_key = admin.request("/CreateKey", {"name": key_name, "neverExpires": True})
            if (
                not isinstance(created_key, Mapping)
                or not _KEY_ID.fullmatch(str(created_key.get("accessKeyId", "")))
                or not _SECRET.fullmatch(str(created_key.get("secretAccessKey", "")))
            ):
                raise HelperActionError(
                    "PROVIDER_RESPONSE_INVALID", "Garage key response is invalid"
                )
            stored = (created_key["accessKeyId"], created_key["secretAccessKey"])
            durable.atomic_write(
                key_file,
                f"ACCESS_KEY_ID={stored[0]}\nSECRET_ACCESS_KEY={stored[1]}\n".encode(),
                mode=0o600,
                maximum_bytes=1024,
            )
        else:
            for duplicate in sorted(named - {stored[0]}):
                admin.request("/DeleteKey", {}, {"id": duplicate})
        admin.request(
            "/AllowBucketKey",
            {
                "bucketId": bucket_id,
                "accessKeyId": stored[0],
                "permissions": {"read": True, "write": True, "owner": False},
            },
        )
        return stored


def upload(client: Any, bucket: str, key: str, path: Path, *, filename: str, sha256: str) -> None:
    size = path.stat().st_size
    with path.open("rb") as body:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentLength=size,
            ContentType="application/zip",
            ContentDisposition=f'attachment; filename="{filename}"',
            # Downloads pass through the public edge; no cache may keep a copy.
            CacheControl="private, no-store",
            Metadata={"sha256": sha256},
        )
    if int(client.head_object(Bucket=bucket, Key=key)["ContentLength"]) != size:
        raise HelperActionError("EXPORT_FAILED", "the uploaded snapshot size does not match")


def sweep(client: Any, bucket: str, now: datetime) -> int:
    """Delete snapshots older than a day; returns how many were deleted."""
    removed = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="exports/"):
        for item in page.get("Contents", []):
            if now - item["LastModified"] >= RETENTION:
                client.delete_object(Bucket=bucket, Key=item["Key"])
                removed += 1
    return removed


def _missing(error: Exception) -> bool:
    response = getattr(error, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, Mapping) else None
    return code in {"404", "NoSuchKey", "NotFound"}


def link(internal: Any, public: Any, bucket: str, key: str, now: datetime) -> dict[str, str]:
    """A five-minute download link for a snapshot that is still within its day."""
    checked = checked_object_key(key)
    try:
        head = internal.head_object(Bucket=bucket, Key=checked)
    except Exception as error:
        if _missing(error):
            raise _expired() from None
        raise
    if now - head["LastModified"] >= RETENTION:
        raise _expired()
    url = public.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": checked}, ExpiresIn=LINK_SECONDS
    )
    return {"url": url, "expiresAt": timestamp(now + timedelta(seconds=LINK_SECONDS))}
