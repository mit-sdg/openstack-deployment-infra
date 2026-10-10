#!/usr/bin/env python3
"""Stream every app bucket, its recovery grants and objects to stdout."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any, BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup.garage_catalog import (  # noqa: E402
    MAX_MANIFEST_BYTES,
    MAX_OBJECTS,
    app_buckets,
    backup_grant,
    bucket_metadata,
    runtime,
    validate_manifest,
)


def export_archive(admin: Any, s3: Any, prefix: str, key_id: str, stream: BinaryIO) -> None:
    expected = app_buckets(admin, prefix)
    metadata = []
    for bucket in expected:
        backup_grant(admin, bucket["id"], key_id)
        metadata.append(bucket_metadata(admin, bucket, key_id))
    manifest = validate_manifest(
        {
            "format_version": 3,
            "buckets": [item["name"] for item in expected],
            "bucket_metadata": metadata,
            "objects": [],
        }
    )

    def add_json(archive: tarfile.TarFile, name: str, value: Any) -> bytes:
        payload = json.dumps(value, sort_keys=True).encode() + b"\n"
        if len(payload) > MAX_MANIFEST_BYTES:
            raise RuntimeError("Garage backup metadata exceeds its bound")
        member = tarfile.TarInfo(name)
        member.size, member.mode = len(payload), 0o600
        archive.addfile(member, io.BytesIO(payload))
        return payload

    with tarfile.open(fileobj=stream, mode="w|gz") as archive:
        add_json(archive, "manifest.json", manifest)
        index = 0
        checksum = hashlib.sha256()
        for bucket in expected:
            # ListObjectsV2 is ordered by key; the decoder checks monotonicity
            # without keeping millions of names in RAM. Even empty buckets list.
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket["name"]):
                for summary in page.get("Contents", []):
                    if index >= MAX_OBJECTS:
                        raise RuntimeError("Garage backup object inventory exceeds its bound")
                    record = {
                        "bucket": bucket["name"],
                        "key": summary["Key"],
                        "size": summary["Size"],
                        "etag": summary.get("ETag", ""),
                    }
                    validate_manifest(
                        {"format_version": 1, "buckets": [bucket["name"]], "objects": [record]}
                    )
                    raw = add_json(archive, f"objects/{index:012d}.json", record)
                    checksum.update(raw)
                    response = s3.get_object(
                        Bucket=record["bucket"], Key=record["key"], IfMatch=record["etag"]
                    )
                    body = response["Body"]
                    try:
                        if response.get("ContentLength") != record["size"]:
                            raise RuntimeError("Garage object changed during backup")
                        info = tarfile.TarInfo(f"objects/{index:012d}.bin")
                        info.size, info.mode = record["size"], 0o600
                        archive.addfile(info, body)
                    finally:
                        body.close()
                    index += 1
        add_json(archive, "objects-complete.json", {"count": index, "sha256": checksum.hexdigest()})
    if app_buckets(admin, prefix) != expected:
        raise RuntimeError("Garage bucket inventory changed during backup; retry")


def main() -> int:
    try:
        admin, s3, prefix, creds = runtime()
        try:
            export_archive(admin, s3, prefix, creds["GARAGE_BACKUP_ACCESS_KEY"], sys.stdout.buffer)
        finally:
            s3.close()
        return 0
    except Exception:
        # Provider errors and key metadata can contain credential material.
        print(
            "Garage backup failed; app bucket coverage or reads could not be confirmed",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
