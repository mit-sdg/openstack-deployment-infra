#!/usr/bin/env python3
"""Restore Garage objects, keys and grants with temporary write permission."""

from __future__ import annotations

import os
import sys
import tarfile
from pathlib import Path
from typing import Any, BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup.garage_catalog import (  # noqa: E402
    app_buckets,
    backup_grant,
    object_payloads,
    read_manifest,
    remap_controller,
    runtime,
)


def restore_archive(
    admin: Any,
    s3: Any,
    prefix: str,
    creds: dict[str, str],
    stream: BinaryIO,
    *,
    controller_database: Path | None = None,
) -> tuple[int, int]:
    key_id = creds["GARAGE_BACKUP_ACCESS_KEY"]
    touched: list[str] = []
    mapping = []
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        manifest = read_manifest(archive)
        if manifest["format_version"] == 1 and manifest["buckets"]:
            raise RuntimeError(
                "Legacy Garage archives have no app keys; recover grants separately before restore"
            )
        metadata = manifest.get("bucket_metadata", [])
        if metadata and controller_database is None:
            raise RuntimeError(
                "Garage bucket restore requires the offline replacement controller database"
            )
        # Look up keys without treating an admin transport failure as absence.
        keys = admin.request("ListKeys")
        if not isinstance(keys, list) or len(keys) >= 10_000:
            raise RuntimeError("Garage key inventory is malformed or may be truncated")
        known_keys = {item["id"] for item in keys}
        if key_id not in known_keys:
            admin.request(
                "ImportKey",
                {
                    "accessKeyId": key_id,
                    "secretAccessKey": creds["GARAGE_BACKUP_SECRET_KEY"],
                    "name": "platform-backup",
                },
            )
        admin.request("UpdateKey", {"deny": {"createBucket": True}}, id=key_id)
        existing = {item["name"]: item for item in app_buckets(admin, prefix)}
        try:
            for bucket in metadata:
                if not bucket["name"].startswith(prefix + "-"):
                    raise RuntimeError("Garage archive belongs to a different prefix")
                current = existing.get(bucket["name"])
                if current is None:
                    current = admin.request("CreateBucket", {"globalAlias": bucket["name"]})
                identifier = current["id"]
                # Include in cleanup before a call that may apply then lose its response.
                touched.append(identifier)
                backup_grant(admin, identifier, key_id, write=True)
                for alias in bucket["aliases"]:
                    if alias != bucket["name"]:
                        admin.request(
                            "AddBucketAlias", {"bucketId": identifier, "globalAlias": alias}
                        )
                admin.request("UpdateBucket", {"quotas": bucket["quotas"]}, id=identifier)
                for key in bucket["keys"]:
                    if key["accessKeyId"] not in known_keys:
                        admin.request(
                            "ImportKey",
                            {
                                name: key[name]
                                for name in ("accessKeyId", "secretAccessKey", "name")
                            },
                        )
                        known_keys.add(key["accessKeyId"])
                    observed = admin.request(
                        "GetKeyInfo", id=key["accessKeyId"], showSecretKey="true"
                    )
                    if any(
                        observed.get(name) != key[name]
                        for name in ("accessKeyId", "secretAccessKey", "name")
                    ):
                        raise RuntimeError("Garage app key conflicts with restore identity")
                    admin.request(
                        "DenyBucketKey",
                        {
                            "bucketId": identifier,
                            "accessKeyId": key["accessKeyId"],
                            "permissions": {"read": False, "write": False, "owner": True},
                        },
                    )
                    admin.request(
                        "AllowBucketKey",
                        {
                            "bucketId": identifier,
                            "accessKeyId": key["accessKeyId"],
                            "permissions": key["permissions"],
                        },
                    )
                restored_info = admin.request("GetBucketInfo", id=identifier)
                for key in bucket["keys"]:
                    matches = [
                        grant
                        for grant in restored_info.get("keys", [])
                        if grant.get("accessKeyId") == key["accessKeyId"]
                    ]
                    if len(matches) != 1 or matches[0].get("permissions") != key["permissions"]:
                        raise RuntimeError("Garage restored app grant could not be confirmed")
                mapping.append(
                    {"name": bucket["name"], "old_id": bucket["id"], "new_id": identifier}
                )
            seen = 0
            for record, body in object_payloads(archive, manifest):
                s3.put_object(
                    Bucket=record["bucket"],
                    Key=record["key"],
                    Body=body,
                    ContentLength=record["size"],
                )
                if (
                    s3.head_object(Bucket=record["bucket"], Key=record["key"]).get("ContentLength")
                    != record["size"]
                ):
                    raise RuntimeError("Garage restored object size did not match")
                seen += 1
            if controller_database is not None:
                remap_controller(controller_database, mapping)
        finally:
            # Attempt every revocation even if one fails. A revocation failure
            # must prevent successful restore evidence, including on an error path.
            failed = False
            for identifier in touched:
                try:
                    backup_grant(admin, identifier, key_id)
                except Exception:
                    failed = True
            if failed:
                raise RuntimeError("Garage restore backup write revocation is unconfirmed")
    return seen, len(manifest["buckets"])


def main() -> int:
    try:
        admin, s3, prefix, creds = runtime()
        path = os.environ.get("GARAGE_RESTORE_CONTROLLER_DATABASE")
        try:
            objects, buckets = restore_archive(
                admin,
                s3,
                prefix,
                creds,
                sys.stdin.buffer,
                controller_database=Path(path) if path else None,
            )
        finally:
            s3.close()
        print(f"garage-restore=verified objects={objects} buckets={buckets}")
        return 0
    except Exception:
        print(
            "Garage restore failed; objects, app grants or read-only backup access could not be confirmed",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
