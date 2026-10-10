"""Garage admin inventory, bounded catalog validation and read-only backup grants."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tarfile
import urllib.parse
from pathlib import Path
from typing import Any, BinaryIO

import boto3
from botocore.config import Config
from lib.http import bounded_json
from lib.platform_config import load
from lib.platform_contract import CONTRACT
from lib.tls import internal_ca_context

MAX_OBJECTS = 5_000_000
MAX_MANIFEST_BYTES = 64 * 1024**2
MAX_OBJECT_BYTES = int(os.environ.get("GARAGE_RESTORE_MAX_OBJECT_BYTES", str(8 * 1024**3)))
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
BUCKET_ID = re.compile(r"[a-f0-9]{64}")
KEY_ID = re.compile(r"GK[a-f0-9]{24}")
SECRET = re.compile(r"[a-f0-9]{64}")
READ_ONLY = {"read": True, "write": False, "owner": False}


def read_env(path: Path, *, private: bool = True) -> dict[str, str]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) not in ((0o600,) if private else (0o600, 0o640))
    ):
        raise RuntimeError("Garage credential input must be a direct private current-user file")
    if metadata.st_size > 65_536:
        raise RuntimeError("Garage credential input exceeds its bound")
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    return values


class Admin:
    def __init__(self, host: str, token: str, ca_file: str) -> None:
        self.base = f"https://{host}:{CONTRACT['ports']['garageRpc']}/v2/"
        self.token = token
        self.context = internal_ca_context(ca_file)

    def request(self, action: str, body: dict[str, Any] | None = None, **query: object) -> Any:
        url = self.base + action
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return bounded_json(
            url,
            method="GET" if body is None else "POST",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            ssl_context=self.context,
            timeout_seconds=30,
            response_limit=16 * 1024**2,
        )


def runtime() -> tuple[Admin, Any, str, dict[str, str]]:
    config = load()
    root = Path(config["paths"]["root"])
    ca_file = os.environ.get("GARAGE_CA_FILE", str(root / "secrets/nomad-cli/internal-ca.pem"))
    creds = read_env(
        Path(os.environ.get("GARAGE_BACKUP_SECRETS", root / "secrets/garage-backup.env"))
    )
    if creds.keys() != {"GARAGE_BACKUP_ACCESS_KEY", "GARAGE_BACKUP_SECRET_KEY"}:
        raise RuntimeError("Garage backup secret file has unexpected keys")
    token = read_env(
        Path(os.environ.get("SECRETS_FILE", root / "secrets/storage-bootstrap.env")), private=False
    )["GARAGE_ADMIN_TOKEN"]
    host = config["addresses"]["storage"]
    admin = Admin(host, token, ca_file)
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{host}:{CONTRACT['ports']['garageS3']}",
        region_name="garage",
        aws_access_key_id=creds["GARAGE_BACKUP_ACCESS_KEY"],
        aws_secret_access_key=creds["GARAGE_BACKUP_SECRET_KEY"],
        verify=ca_file,
        config=Config(
            signature_version="s3v4",
            connect_timeout=8,
            read_timeout=120,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )
    return admin, s3, config["prefix"], creds


def app_buckets(admin: Any, prefix: str) -> list[dict[str, Any]]:
    inventory = admin.request("ListBuckets")
    # Garage v2.3 ListBuckets has a fixed 10,000-row ceiling without pagination.
    if not isinstance(inventory, list) or len(inventory) >= 10_000:
        raise RuntimeError("Garage bucket inventory is malformed or may be truncated")
    result = []
    ids: set[str] = set()
    aliases_seen: set[str] = set()
    for item in inventory:
        if not isinstance(item, dict) or not isinstance(item.get("globalAliases"), list):
            raise RuntimeError("Garage bucket aliases are malformed")
        aliases = item["globalAliases"]
        if any(not isinstance(alias, str) or not BUCKET.fullmatch(alias) for alias in aliases):
            raise RuntimeError("Garage bucket alias is unsafe")
        names = [alias for alias in aliases if alias.startswith(prefix + "-")]
        if not names:
            continue
        identifier = item.get("id")
        if (
            not isinstance(identifier, str)
            or not BUCKET_ID.fullmatch(identifier)
            or identifier in ids
            or aliases_seen.intersection(names)
        ):
            raise RuntimeError("Garage app bucket identity is ambiguous")
        ids.add(identifier)
        aliases_seen.update(names)
        result.append({"id": identifier, "name": sorted(names)[0], "aliases": sorted(aliases)})
    return sorted(result, key=lambda item: item["name"])


def backup_grant(admin: Any, bucket_id: str, key_id: str, *, write: bool = False) -> None:
    values = {"bucketId": bucket_id, "accessKeyId": key_id}
    admin.request(
        "DenyBucketKey",
        {**values, "permissions": {"read": False, "write": not write, "owner": True}},
    )
    permissions = {**READ_ONLY, "write": write}
    admin.request("AllowBucketKey", {**values, "permissions": permissions})
    info = admin.request("GetBucketInfo", id=bucket_id)
    keys = info.get("keys", []) if isinstance(info, dict) else []
    matches = [
        item for item in keys if isinstance(item, dict) and item.get("accessKeyId") == key_id
    ]
    if len(matches) != 1 or matches[0].get("permissions") != permissions:
        raise RuntimeError("Garage backup grant could not be confirmed")


def bucket_metadata(admin: Any, record: dict[str, Any], backup_key: str) -> dict[str, Any]:
    info = admin.request("GetBucketInfo", id=record["id"])
    if not isinstance(info, dict) or info.get("id") != record["id"]:
        raise RuntimeError("Garage bucket metadata identity changed")
    grants = info.get("keys")
    if not isinstance(grants, list):
        raise RuntimeError("Garage bucket grants are malformed")
    keys = []
    for grant in grants:
        if not isinstance(grant, dict):
            raise RuntimeError("Garage key grant is malformed")
        identifier = grant.get("accessKeyId")
        if identifier == backup_key or not any(grant.get("permissions", {}).values()):
            continue
        key = admin.request("GetKeyInfo", id=identifier, showSecretKey="true")
        if not isinstance(key, dict) or key.get("accessKeyId") != identifier:
            raise RuntimeError("Garage app key identity changed")
        keys.append(
            {
                "accessKeyId": identifier,
                "name": key["name"],
                "secretAccessKey": key["secretAccessKey"],
                "permissions": grant["permissions"],
            }
        )
    return {
        **record,
        "quotas": info.get("quotas", {"maxSize": None, "maxObjects": None}),
        "keys": keys,
    }


def validate_manifest(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or type(value.get("format_version")) is not int
        or value["format_version"] not in (1, 2, 3)
    ):
        raise RuntimeError("Garage archive format is unsupported")
    buckets, objects = value.get("buckets"), value.get("objects")
    if (
        not isinstance(buckets, list)
        or len(buckets) >= 10_000
        or any(not isinstance(item, str) or not BUCKET.fullmatch(item) for item in buckets)
        or len(set(buckets)) != len(buckets)
    ):
        raise RuntimeError("Garage archive bucket inventory is malformed")
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS:
        raise RuntimeError("Garage archive object inventory exceeds its bound")
    if value["format_version"] == 3 and objects:
        raise RuntimeError("streamed Garage archive must have an empty header object list")
    seen: set[tuple[str, str]] = set()
    for item in objects:
        if not isinstance(item, dict):
            raise RuntimeError("Garage object record is malformed")
        size, bucket, key = item.get("size"), item.get("bucket"), item.get("key")
        if (
            type(size) is not int
            or not 0 <= size <= MAX_OBJECT_BYTES
            or not isinstance(bucket, str)
            or bucket not in buckets
            or not isinstance(key, str)
            or not key
            or len(key.encode()) > 1024
            or (bucket, key) in seen
        ):
            raise RuntimeError("Garage object record is unsafe")
        seen.add((bucket, key))
    if value["format_version"] in (2, 3):
        metadata = value.get("bucket_metadata")
        if not isinstance(metadata, list) or len(metadata) != len(buckets):
            raise RuntimeError("Garage bucket metadata is incomplete")
        ids: set[str] = set()
        names: set[str] = set()
        for item in metadata:
            if (
                not isinstance(item, dict)
                or item.get("name") not in buckets
                or item["name"] in names
                or not isinstance(item.get("id"), str)
                or not BUCKET_ID.fullmatch(item["id"])
                or item["id"] in ids
            ):
                raise RuntimeError("Garage archived bucket identity is malformed")
            names.add(item["name"])
            ids.add(item["id"])
            aliases = item.get("aliases")
            if (
                not isinstance(aliases, list)
                or item["name"] not in aliases
                or any(
                    not isinstance(alias, str) or not BUCKET.fullmatch(alias) for alias in aliases
                )
            ):
                raise RuntimeError("Garage archived aliases are malformed")
            quotas = item.get("quotas")
            if (
                not isinstance(quotas, dict)
                or set(quotas) != {"maxSize", "maxObjects"}
                or any(v is not None and (type(v) is not int or v < 0) for v in quotas.values())
            ):
                raise RuntimeError("Garage archived quotas are malformed")
            keys = item.get("keys")
            if not isinstance(keys, list) or not keys or len(keys) > 100:
                raise RuntimeError("Garage app key inventory is incomplete")
            key_ids: set[str] = set()
            for key in keys:
                if (
                    not isinstance(key, dict)
                    or not isinstance(key.get("accessKeyId"), str)
                    or not KEY_ID.fullmatch(key["accessKeyId"])
                    or key["accessKeyId"] in key_ids
                    or not isinstance(key.get("secretAccessKey"), str)
                    or not SECRET.fullmatch(key["secretAccessKey"])
                ):
                    raise RuntimeError("Garage archived key identity is malformed")
                key_ids.add(key["accessKeyId"])
                if not isinstance(key.get("name"), str) or not re.fullmatch(
                    r"application-" + re.escape(item["name"]) + r"-[a-f0-9]{8}", key["name"]
                ):
                    raise RuntimeError("Garage archived key name is not app-owned")
                if key.get("permissions") != {"read": True, "write": True, "owner": False}:
                    raise RuntimeError("Garage archived app grant is unsafe")
    return value


def read_manifest(archive: tarfile.TarFile) -> dict[str, Any]:
    member = archive.next()
    if (
        member is None
        or member.name != "manifest.json"
        or not member.isfile()
        or not 0 < member.size <= MAX_MANIFEST_BYTES
    ):
        raise RuntimeError("Garage archive manifest is missing or unsafe")
    handle = archive.extractfile(member)
    if handle is None:
        raise RuntimeError("Garage archive manifest is unreadable")
    with handle:
        return validate_manifest(json.load(handle))


def object_payloads(archive: tarfile.TarFile, manifest: dict[str, Any]) -> Any:
    if manifest["format_version"] == 3:
        yield from streamed_payloads(archive, manifest)
        return
    seen = 0
    while (member := archive.next()) is not None:
        if (
            seen >= len(manifest["objects"])
            or member.name != f"objects/{seen:012d}.bin"
            or not member.isfile()
            or member.size != manifest["objects"][seen]["size"]
        ):
            raise RuntimeError("Garage archive object order or size is malformed")
        handle = archive.extractfile(member)
        if handle is None:
            raise RuntimeError("Garage archive object is unreadable")
        with handle:
            yield manifest["objects"][seen], handle
        seen += 1
    if seen != len(manifest["objects"]):
        raise RuntimeError("Garage archive omitted objects")


def streamed_payloads(archive: tarfile.TarFile, manifest: dict[str, Any]) -> Any:
    """Bound memory independently of the fifty-bucket object population."""
    count = 0
    checksum = hashlib.sha256()
    previous: tuple[str, str] | None = None
    buckets = set(manifest["buckets"])
    while (member := archive.next()) is not None:
        if member.name == "objects-complete.json":
            if not member.isfile() or not 0 < member.size <= 4096:
                raise RuntimeError("Garage object completion record is invalid")
            body = archive.extractfile(member)
            assert body is not None
            footer = json.load(body)
            if (
                footer != {"count": count, "sha256": checksum.hexdigest()}
                or archive.next() is not None
            ):
                raise RuntimeError("Garage object completion record does not match")
            manifest["objectCount"] = count
            return
        if (
            count >= MAX_OBJECTS
            or member.name != f"objects/{count:012d}.json"
            or not member.isfile()
            or not 0 < member.size <= 8192
        ):
            raise RuntimeError("Garage object record order is invalid")
        body = archive.extractfile(member)
        assert body is not None
        raw = body.read()
        record = json.loads(raw)
        # Reuse the existing object bounds without repeatedly scanning grants.
        validate_manifest({"format_version": 1, "buckets": list(buckets), "objects": [record]})
        identity = (record["bucket"], record["key"])
        if previous is not None and identity <= previous:
            raise RuntimeError("Garage object order or identity is invalid")
        previous = identity
        checksum.update(raw)
        member = archive.next()
        if (
            member is None
            or not member.isfile()
            or member.name != f"objects/{count:012d}.bin"
            or member.size != record["size"]
        ):
            raise RuntimeError("Garage object payload is invalid")
        body = archive.extractfile(member)
        assert body is not None
        with body:
            yield record, body
        count += 1
    raise RuntimeError("Garage object completion record is absent")


def verify_archive(stream: BinaryIO, *, admin: Any = None, prefix: str = "") -> dict[str, Any]:
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        manifest = read_manifest(archive)
        if admin is not None:
            expected = app_buckets(admin, prefix)
            if set(manifest["buckets"]) != {item["name"] for item in expected}:
                raise RuntimeError("Garage archive omits or adds app buckets")
            if manifest["format_version"] in (2, 3) and {
                (item["id"], item["name"]) for item in manifest["bucket_metadata"]
            } != {(item["id"], item["name"]) for item in expected}:
                raise RuntimeError("Garage archive bucket identities differ from admin inventory")
        for _record, body in object_payloads(archive, manifest):
            while body.read(1024 * 1024):
                pass
        return manifest


def remap_controller(path: Path, mapping: list[dict[str, str]]) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise RuntimeError("Garage restore requires a private offline controller database")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise RuntimeError("Garage restore requires controller SQLite without sidecars")
    connection = sqlite3.connect(f"file:{path}?mode=rw", uri=True)
    try:
        with connection:
            for item in mapping:
                rows = connection.execute(
                    "SELECT resource_id, provider_id FROM managed_resources WHERE resource_type='s3' AND provider_name=?",
                    (item["name"],),
                ).fetchall()
                if len(rows) != 1 or rows[0][1] not in {item["old_id"], item["new_id"]}:
                    raise RuntimeError("Garage restored bucket does not match controller ownership")
                connection.execute(
                    "UPDATE managed_resources SET provider_id=?, last_verified_at=NULL WHERE resource_id=?",
                    (item["new_id"], rows[0][0]),
                )
    finally:
        connection.close()
