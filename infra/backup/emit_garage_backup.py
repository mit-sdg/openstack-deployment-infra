#!/usr/bin/env python3
"""Stream a restorable Garage object catalog and payload tarball to stdout."""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
from pathlib import Path

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.platform_config import load  # noqa: E402

CONFIG = load()
HOST = CONFIG["addresses"]["storage"]
ROOT = Path(CONFIG["paths"]["root"])
SECRETS_FILE = Path(os.environ.get("GARAGE_BACKUP_SECRETS", ROOT / "secrets/garage-backup.env"))
CA_FILE = os.environ.get("GARAGE_CA_FILE", str(ROOT / "secrets/nomad-cli/internal-ca.pem"))


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    required = {"GARAGE_BACKUP_ACCESS_KEY", "GARAGE_BACKUP_SECRET_KEY"}
    if values.keys() != required:
        raise RuntimeError("Garage backup secret file has unexpected keys")
    return values


def add_bytes(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(value))


def main() -> int:
    creds = read_env(SECRETS_FILE)
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{HOST}:9000",
        region_name="garage",
        aws_access_key_id=creds["GARAGE_BACKUP_ACCESS_KEY"],
        aws_secret_access_key=creds["GARAGE_BACKUP_SECRET_KEY"],
        verify=CA_FILE,
        config=Config(
            signature_version="s3v4",
            connect_timeout=8,
            read_timeout=30,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )
    objects: list[dict[str, object]] = []
    buckets: list[str] = []
    for bucket_record in sorted(
        s3.list_buckets().get("Buckets", []), key=lambda item: item["Name"]
    ):
        bucket = bucket_record["Name"]
        buckets.append(bucket)
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for summary in page.get("Contents", []):
                objects.append(
                    {
                        "bucket": bucket,
                        "key": summary["Key"],
                        "size": summary["Size"],
                        "etag": summary.get("ETag", "").strip('"'),
                        "last_modified": summary["LastModified"].isoformat(),
                    }
                )
    manifest = {
        "format_version": 1,
        "endpoint": f"https://{HOST}:9000",
        "buckets": buckets,
        "objects": objects,
    }
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
        add_bytes(archive, "manifest.json", json.dumps(manifest, sort_keys=True).encode() + b"\n")
        for index, record in enumerate(objects):
            response = s3.get_object(Bucket=str(record["bucket"]), Key=str(record["key"]))
            body = response["Body"]
            info = tarfile.TarInfo(f"objects/{index:012d}.bin")
            size = record["size"]
            if isinstance(size, bool) or not isinstance(size, int):
                raise RuntimeError("Garage object size is malformed")
            info.size = size
            info.mode = 0o600
            archive.addfile(info, body)
            body.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
