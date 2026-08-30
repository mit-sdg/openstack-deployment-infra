#!/usr/bin/env python3
"""Export/import a bounded Distribution registry artifact archive on stdin/stdout.

Authentication is read from the protected storage bootstrap environment file;
passwords are never accepted as arguments.  Export follows every retained tag
and embeds its manifest/config/layers.  Import uploads blobs then exact
manifests, so the archive is sufficient without the original registry host.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import ssl
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.platform_config import load  # noqa: E402
from lib.platform_contract import CONTRACT  # noqa: E402

CONFIG = load()
ROOT = Path(CONFIG["paths"]["root"])
SECRETS = Path(os.environ.get("REGISTRY_BACKUP_SECRETS", ROOT / "secrets/storage-bootstrap.env"))
CA_FILE = os.environ.get("REGISTRY_CA_FILE", str(ROOT / "secrets/nomad-cli/internal-ca.pem"))
HOST = os.environ.get(
    "REGISTRY_BACKUP_HOST",
    f"{CONFIG['addresses']['storage']}:{CONTRACT['ports']['registry']}",
)
MAX_RESPONSE = 64 * 1024 * 1024
MAX_TOTAL = int(os.environ.get("REGISTRY_BACKUP_MAX_BYTES", str(32 * 1024**3)))
MAX_ITEMS = 10_000
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)


def credentials() -> str:
    values: dict[str, str] = {}
    metadata = SECRETS.lstat()
    if not SECRETS.is_file() or SECRETS.is_symlink() or metadata.st_mode & 0o077:
        raise RuntimeError("registry backup secret file must be a direct private file")
    for line in SECRETS.read_text().splitlines():
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise RuntimeError("registry backup secret file is malformed")
            values[key] = value
    password = values.get("REGISTRY_BUILDER_PASSWORD")
    if not password:
        raise RuntimeError("registry backup credential is missing")
    return "Basic " + base64.b64encode(f"builder:{password}".encode()).decode()


class Registry:
    def __init__(self) -> None:
        self.base = f"https://{HOST}/v2"
        self.authorization = credentials()
        self.context = ssl.create_default_context(cafile=CA_FILE)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[bytes, Any]:
        headers = {"Authorization": self.authorization, "Accept": ACCEPT}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method=method
        )
        with urllib.request.urlopen(request, context=self.context, timeout=60) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_RESPONSE:
                raise RuntimeError("registry response exceeded its per-object limit")
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) > MAX_RESPONSE:
                raise RuntimeError("registry response exceeded its per-object limit")
            return payload, response.headers

    def json(self, path: str) -> dict[str, Any]:
        payload, _ = self.request("GET", path)
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise RuntimeError("registry returned malformed JSON")
        return value

    def paginated(self, path: str, field: str) -> list[Any]:
        values: list[Any] = []
        current: str | None = path
        pages = 0
        while current is not None:
            payload, headers = self.request("GET", current)
            document = json.loads(payload)
            page = document.get(field) if isinstance(document, dict) else None
            if page is None:
                page = []
            if not isinstance(page, list):
                raise RuntimeError("registry returned malformed paginated JSON")
            values.extend(page)
            if len(values) > MAX_ITEMS or pages >= 100:
                raise RuntimeError("registry inventory exceeded its configured bound")
            link = headers.get("Link")
            if link is None:
                current = None
            else:
                match = re.fullmatch(r"<([^>]+)>;\s*rel=\"?next\"?", link.strip())
                if match is None:
                    raise RuntimeError("registry returned a malformed pagination link")
                parsed = urllib.parse.urlsplit(match.group(1))
                if parsed.netloc and parsed.netloc != HOST:
                    raise RuntimeError("registry pagination escaped the configured host")
                current = parsed.path.removeprefix("/v2")
                if parsed.query:
                    current += "?" + parsed.query
            pages += 1
        return values

    def upload_blob(self, repository: str, digest: str, payload: bytes) -> None:
        try:
            self.request("HEAD", f"/{repository}/blobs/{digest}")
            return
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        _, headers = self.request("POST", f"/{repository}/blobs/uploads/")
        location = headers.get("Location")
        if not isinstance(location, str):
            raise RuntimeError("registry omitted upload location")
        parsed = urllib.parse.urlsplit(location)
        path = parsed.path.removeprefix("/v2")
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("digest", digest))
        self.request("PUT", path + "?" + urllib.parse.urlencode(query), body=payload)


def tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(payload))


def export_archive(registry: Registry) -> None:
    catalog = registry.paginated("/_catalog?n=1000", "repositories")
    records: list[dict[str, str]] = []
    blobs: dict[str, bytes] = {}
    manifest_media_types: dict[str, str] = {}
    total = 0

    def retain_manifest(
        repository: str, reference: str, expected: str | None = None, depth: int = 0
    ) -> tuple[str, str]:
        nonlocal total
        if depth > 32:
            raise RuntimeError("registry manifest nesting exceeded its configured bound")
        manifest, headers = registry.request("GET", f"/{repository}/manifests/{reference}")
        media_type = headers.get_content_type()
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        if expected is not None and digest != expected:
            raise RuntimeError("registry child manifest digest did not match")
        if digest in manifest_media_types:
            return digest, media_type
        document = json.loads(manifest)
        if not isinstance(document, dict):
            raise RuntimeError("registry manifest is malformed")
        manifest_media_types[digest] = media_type
        blobs[digest] = manifest
        total += len(manifest)
        children = document.get("manifests", [])
        if children:
            if not isinstance(children, list):
                raise RuntimeError("registry manifest children are malformed")
            for child in children:
                child_digest = child.get("digest") if isinstance(child, dict) else None
                if not isinstance(child_digest, str) or not DIGEST.fullmatch(child_digest):
                    raise RuntimeError("registry manifest has a malformed child descriptor")
                retain_manifest(repository, child_digest, child_digest, depth + 1)
        descriptors = document.get("layers", [])
        if isinstance(document.get("config"), dict):
            descriptors = [*descriptors, document["config"]]
        if not isinstance(descriptors, list):
            raise RuntimeError("registry manifest blob descriptors are malformed")
        for descriptor in descriptors:
            blob_digest = descriptor.get("digest") if isinstance(descriptor, dict) else None
            if not isinstance(blob_digest, str) or not DIGEST.fullmatch(blob_digest):
                raise RuntimeError("registry manifest has a malformed descriptor")
            if blob_digest not in blobs:
                payload, _ = registry.request("GET", f"/{repository}/blobs/{blob_digest}")
                if "sha256:" + hashlib.sha256(payload).hexdigest() != blob_digest:
                    raise RuntimeError("registry blob digest did not match")
                blobs[blob_digest] = payload
                total += len(payload)
        if total > MAX_TOTAL or len(blobs) > MAX_ITEMS:
            raise RuntimeError("registry archive exceeded its configured bound")
        return digest, media_type

    for repository in sorted(catalog):
        if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
            raise RuntimeError("registry returned an unsafe repository")
        tags = registry.paginated(f"/{repository}/tags/list?n=1000", "tags")
        for tag in sorted(set(tags)):
            if not isinstance(tag, str) or not TAG.fullmatch(tag):
                raise RuntimeError("registry returned an unsafe tag")
            digest, media_type = retain_manifest(repository, tag)
            records.append(
                {
                    "repository": repository,
                    "tag": tag,
                    "digest": digest,
                    "mediaType": media_type,
                }
            )
    index = {
        "format": "openstack-platform-registry-artifacts-v1",
        "manifests": records,
        "blobs": sorted(blobs),
        "manifestMediaTypes": manifest_media_types,
    }
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
        tar_bytes(archive, "index.json", json.dumps(index, sort_keys=True).encode() + b"\n")
        for digest, payload in sorted(blobs.items()):
            tar_bytes(archive, f"blobs/sha256/{digest.removeprefix('sha256:')}", payload)


def read_archive() -> tuple[dict[str, Any], dict[str, bytes]]:
    blobs: dict[str, bytes] = {}
    index: dict[str, Any] | None = None
    total = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
        for number, member in enumerate(archive, 1):
            if number > MAX_ITEMS + 1 or not member.isfile() or member.size > MAX_RESPONSE:
                raise RuntimeError("registry archive member is unsafe")
            handle = archive.extractfile(member)
            if handle is None:
                raise RuntimeError("registry archive member is unreadable")
            payload = handle.read(MAX_RESPONSE + 1)
            total += len(payload)
            if len(payload) != member.size or total > MAX_TOTAL:
                raise RuntimeError("registry archive exceeds its configured bound")
            if member.name == "index.json" and index is None:
                value = json.loads(payload)
                if not isinstance(value, dict):
                    raise RuntimeError("registry archive index is malformed")
                index = value
            elif re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", member.name):
                digest = "sha256:" + member.name.rsplit("/", 1)[1]
                if "sha256:" + hashlib.sha256(payload).hexdigest() != digest or digest in blobs:
                    raise RuntimeError("registry archive blob checksum is invalid")
                blobs[digest] = payload
            else:
                raise RuntimeError("registry archive has an unexpected member")
    if index is None or index.get("format") != "openstack-platform-registry-artifacts-v1":
        raise RuntimeError("registry archive format is unsupported")
    if index.get("blobs") != sorted(blobs):
        raise RuntimeError("registry archive blob inventory does not match")
    manifests = index.get("manifests")
    media_types = index.get("manifestMediaTypes")
    if (
        not isinstance(manifests, list)
        or len(manifests) > MAX_ITEMS
        or not isinstance(media_types, dict)
        or set(media_types) - set(blobs)
        or any(
            not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or not isinstance(media_type, str)
            or not media_type.startswith("application/")
            for digest, media_type in media_types.items()
        )
    ):
        raise RuntimeError("registry archive manifest inventory is malformed")
    for record in manifests:
        if not isinstance(record, dict) or set(record) != {
            "repository",
            "tag",
            "digest",
            "mediaType",
        }:
            raise RuntimeError("registry archive manifest record is malformed")
        repository, tag, digest = record["repository"], record["tag"], record["digest"]
        if (
            not isinstance(repository, str)
            or not REPOSITORY.fullmatch(repository)
            or not isinstance(tag, str)
            or not TAG.fullmatch(tag)
            or digest not in media_types
        ):
            raise RuntimeError("registry archive manifest identity is invalid")
    for digest in media_types:
        document = json.loads(blobs[digest])
        if not isinstance(document, dict):
            raise RuntimeError("registry archive manifest is malformed")
        layers = document.get("layers", [])
        children = document.get("manifests", [])
        if not isinstance(layers, list) or not isinstance(children, list):
            raise RuntimeError("registry archive descriptors are malformed")
        descriptors = [*layers, *children]
        if isinstance(document.get("config"), dict):
            descriptors.append(document["config"])
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("digest"), str)
            or item["digest"] not in blobs
            for item in descriptors
        ):
            raise RuntimeError("registry archive omits a referenced descriptor")
    return index, blobs


def upload_manifest_tree(
    registry: Registry,
    repository: str,
    root_digest: str,
    blobs: dict[str, bytes],
    media_types: dict[Any, Any],
) -> None:
    uploaded: set[str] = set()
    visiting: set[str] = set()

    def upload(manifest_digest: str, depth: int = 0) -> None:
        if manifest_digest in uploaded:
            return
        if depth > 32 or manifest_digest in visiting:
            raise RuntimeError("registry archive manifest nesting is unsafe")
        if manifest_digest not in media_types or manifest_digest not in blobs:
            raise RuntimeError("registry archive omits a referenced manifest")
        visiting.add(manifest_digest)
        document = json.loads(blobs[manifest_digest])
        if not isinstance(document, dict):
            raise RuntimeError("registry archive manifest is malformed")
        children = document.get("manifests", [])
        if not isinstance(children, list):
            raise RuntimeError("registry archive child inventory is malformed")
        for child in children:
            child_digest = child.get("digest") if isinstance(child, dict) else None
            if not isinstance(child_digest, str):
                raise RuntimeError("registry archive child descriptor is malformed")
            upload(child_digest, depth + 1)
        descriptors = document.get("layers", [])
        if isinstance(document.get("config"), dict):
            descriptors = [*descriptors, document["config"]]
        if not isinstance(descriptors, list):
            raise RuntimeError("registry archive blob inventory is malformed")
        for descriptor in descriptors:
            blob_digest = descriptor.get("digest") if isinstance(descriptor, dict) else None
            if blob_digest not in blobs or blob_digest in media_types:
                raise RuntimeError("registry archive omits a referenced blob")
            registry.upload_blob(repository, blob_digest, blobs[blob_digest])
        registry.request(
            "PUT",
            f"/{repository}/manifests/{manifest_digest}",
            body=blobs[manifest_digest],
            content_type=media_types[manifest_digest],
        )
        visiting.remove(manifest_digest)
        uploaded.add(manifest_digest)

    upload(root_digest)


def import_archive(registry: Registry) -> None:
    index, blobs = read_archive()
    manifests = index.get("manifests")
    media_types = index.get("manifestMediaTypes")
    if (
        not isinstance(manifests, list)
        or len(manifests) > MAX_ITEMS
        or not isinstance(media_types, dict)
        or any(
            not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or not isinstance(media_type, str)
            or not media_type.startswith("application/")
            for digest, media_type in media_types.items()
        )
    ):
        raise RuntimeError("registry archive manifest inventory is malformed")
    for record in manifests:
        if not isinstance(record, dict) or set(record) != {
            "repository",
            "tag",
            "digest",
            "mediaType",
        }:
            raise RuntimeError("registry archive manifest record is malformed")
        repository, tag, digest, media_type = (
            record["repository"],
            record["tag"],
            record["digest"],
            record["mediaType"],
        )
        if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
            raise RuntimeError("registry archive repository is unsafe")
        if not isinstance(tag, str) or not TAG.fullmatch(tag) or digest not in blobs:
            raise RuntimeError("registry archive manifest identity is invalid")
        upload_manifest_tree(registry, repository, digest, blobs, media_types)
        registry.request(
            "PUT", f"/{repository}/manifests/{tag}", body=blobs[digest], content_type=media_type
        )


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("action", choices=("export", "import", "verify"))
    args = parser.parse_args()
    try:
        if args.action == "export":
            export_archive(Registry())
        elif args.action == "import":
            import_archive(Registry())
        else:
            index, blobs = read_archive()
            print(
                f"registry-artifacts=verified manifests={len(index['manifests'])} blobs={len(blobs)}"
            )
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
        tarfile.TarError,
        urllib.error.URLError,
    ) as error:
        print(f"registry artifact operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
