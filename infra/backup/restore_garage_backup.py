#!/usr/bin/env python3
"""Restore a bounded Garage catalog archive from stdin without path extraction."""

from __future__ import annotations

import json
import os
import re
import sys
import tarfile
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.platform_config import load  # noqa: E402
from lib.platform_contract import CONTRACT  # noqa: E402

CONFIG = load()
HOST = CONFIG["addresses"]["storage"]
ROOT = Path(CONFIG["paths"]["root"])
SECRETS_FILE = Path(os.environ.get("GARAGE_BACKUP_SECRETS", ROOT / "secrets/garage-backup.env"))
CA_FILE = os.environ.get("GARAGE_CA_FILE", str(ROOT / "secrets/nomad-cli/internal-ca.pem"))
MAX_OBJECTS = 100_000
MAX_OBJECT_BYTES = int(os.environ.get("GARAGE_RESTORE_MAX_OBJECT_BYTES", str(8 * 1024**3)))
BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


def secrets() -> dict[str, str]:
    metadata = SECRETS_FILE.lstat()
    if not SECRETS_FILE.is_file() or SECRETS_FILE.is_symlink() or metadata.st_mode & 0o077:
        raise RuntimeError("Garage backup secret file must be a direct private file")
    values = dict(line.split("=", 1) for line in SECRETS_FILE.read_text().splitlines() if line)
    if values.keys() != {"GARAGE_BACKUP_ACCESS_KEY", "GARAGE_BACKUP_SECRET_KEY"}:
        raise RuntimeError("Garage backup secret file has unexpected keys")
    return values


def main() -> int:
    try:
        creds = secrets()
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{HOST}:{CONTRACT['ports']['garageS3']}",
            region_name="garage",
            aws_access_key_id=creds["GARAGE_BACKUP_ACCESS_KEY"],
            aws_secret_access_key=creds["GARAGE_BACKUP_SECRET_KEY"],
            verify=CA_FILE,
            config=Config(connect_timeout=8, read_timeout=120, retries={"max_attempts": 2}),
        )
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
            member = archive.next()
            if (
                member is None
                or member.name != "manifest.json"
                or not member.isfile()
                or member.size > 64 * 1024**2
            ):
                raise RuntimeError("Garage archive manifest is missing or unsafe")
            handle = archive.extractfile(member)
            manifest = json.load(handle) if handle is not None else None
            if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
                raise RuntimeError("Garage archive manifest is unsupported")
            buckets = manifest.get("buckets")
            objects = manifest.get("objects")
            if (
                not isinstance(buckets, list)
                or not isinstance(objects, list)
                or len(objects) > MAX_OBJECTS
                or any(not isinstance(item, str) or not BUCKET.fullmatch(item) for item in buckets)
            ):
                raise RuntimeError("Garage archive inventory is malformed")
            for bucket in buckets:
                try:
                    s3.create_bucket(Bucket=bucket)
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") not in {
                        "BucketAlreadyExists",
                        "BucketAlreadyOwnedByYou",
                    }:
                        raise
            seen = 0
            for member in archive:
                if (
                    seen >= len(objects)
                    or member.name != f"objects/{seen:012d}.bin"
                    or not member.isfile()
                ):
                    raise RuntimeError("Garage archive object order is malformed")
                record = objects[seen]
                if not isinstance(record, dict):
                    raise RuntimeError("Garage archive object record is malformed")
                size = record.get("size")
                bucket = record.get("bucket")
                key = record.get("key")
                if (
                    isinstance(size, bool)
                    or not isinstance(size, int)
                    or not 0 <= size <= MAX_OBJECT_BYTES
                    or member.size != size
                    or bucket not in buckets
                    or not isinstance(key, str)
                    or not key
                    or len(key.encode()) > 1024
                ):
                    raise RuntimeError("Garage archive object is unsafe")
                body = archive.extractfile(member)
                if body is None:
                    raise RuntimeError("Garage archive object is unreadable")
                s3.put_object(Bucket=bucket, Key=key, Body=body, ContentLength=size)
                observed = s3.head_object(Bucket=bucket, Key=key).get("ContentLength")
                if observed != size:
                    raise RuntimeError("Garage restored object size did not match")
                seen += 1
            if seen != len(objects):
                raise RuntimeError("Garage archive omitted objects")
        print(f"garage-restore=verified objects={seen} buckets={len(buckets)}")
        return 0
    except (OSError, ValueError, RuntimeError, tarfile.TarError, ClientError) as error:
        print(f"Garage restore failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
