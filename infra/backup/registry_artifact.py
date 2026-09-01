#!/usr/bin/env python3
"""Stream bounded Distribution registry artifacts to and from a tar archive.

Credentials come only from a protected file. Layers are never accumulated in
memory: export streams each verified registry response into tar, while verify
and import hash each member into a private disk spool. Only bounded manifests
and the archive index are materialized in memory.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import ssl
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.platform_config import load  # noqa: E402
from lib.platform_contract import CONTRACT  # noqa: E402

FORMAT = "openstack-platform-registry-artifacts-v2"
DEFAULT_MAX_FILE_BYTES = 1024**4
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024**4
HARD_MAX_FILE_BYTES = 4 * 1024**4
HARD_MAX_TOTAL_BYTES = 8 * 1024**4
DEFAULT_MAX_MANIFEST_BYTES = 64 * 1024**2
MAX_ITEMS = 100_000
CHUNK = 1024 * 1024
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


@dataclass(frozen=True, slots=True)
class Bounds:
    maximum_file_bytes: int
    maximum_total_bytes: int
    maximum_manifest_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_file_bytes, bool)
            or not isinstance(self.maximum_file_bytes, int)
            or not 1024**3 <= self.maximum_file_bytes <= HARD_MAX_FILE_BYTES
            or isinstance(self.maximum_total_bytes, bool)
            or not isinstance(self.maximum_total_bytes, int)
            or not self.maximum_file_bytes <= self.maximum_total_bytes <= HARD_MAX_TOTAL_BYTES
            or isinstance(self.maximum_manifest_bytes, bool)
            or not isinstance(self.maximum_manifest_bytes, int)
            or not 1024 <= self.maximum_manifest_bytes <= 1024**3
            or self.maximum_manifest_bytes > self.maximum_file_bytes
        ):
            raise RuntimeError("registry artifact bounds are invalid")


@dataclass(slots=True)
class BlobMeta:
    digest: str
    size: int
    kind: str
    repositories: set[str] = field(default_factory=set)


class RegistryClient(Protocol):
    def paginated(self, path: str, field: str) -> list[Any]: ...

    def get_manifest(
        self, repository: str, reference: str, *, expected_size: int | None = None
    ) -> tuple[bytes, str]: ...

    def open_blob(
        self, repository: str, digest: str, size: int
    ) -> contextlib.AbstractContextManager[BinaryIO]: ...

    def upload_blob(self, repository: str, digest: str, path: Path, size: int) -> None: ...

    def put_manifest(
        self, repository: str, reference: str, path: Path, size: int, media_type: str
    ) -> None: ...


def _environment_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    try:
        return default if value is None else int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error


def configured_bounds() -> Bounds:
    return Bounds(
        _environment_int("REGISTRY_BACKUP_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES),
        _environment_int("REGISTRY_BACKUP_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES),
        _environment_int("REGISTRY_BACKUP_MAX_MANIFEST_BYTES", DEFAULT_MAX_MANIFEST_BYTES),
    )


def _runtime_paths() -> tuple[Path, Path, str]:
    config = load()
    root = Path(config["paths"]["root"])
    secrets = Path(
        os.environ.get("REGISTRY_BACKUP_SECRETS", root / "secrets/storage-bootstrap.env")
    )
    ca_file = Path(os.environ.get("REGISTRY_CA_FILE", root / "secrets/nomad-cli/internal-ca.pem"))
    host = os.environ.get(
        "REGISTRY_BACKUP_HOST",
        f"{config['addresses']['storage']}:{CONTRACT['ports']['registry']}",
    )
    return secrets, ca_file, host


def credentials(path: Path) -> str:
    values: dict[str, str] = {}
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink() or metadata.st_mode & 0o077:
        raise RuntimeError("registry backup secret file must be a direct private file")
    for line in path.read_text().splitlines():
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
    def __init__(self, bounds: Bounds) -> None:
        secrets, ca_file, host = _runtime_paths()
        self.bounds = bounds
        self.host = host
        self.base = f"https://{host}/v2"
        self.authorization = credentials(secrets)
        self.context = ssl.create_default_context(cafile=ca_file)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | BinaryIO | None = None,
        content_type: str | None = None,
        content_length: int | None = None,
    ) -> Any:
        headers = {"Authorization": self.authorization, "Accept": ACCEPT}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        request = urllib.request.Request(
            self.base + path,
            data=cast(Any, body),
            headers=headers,
            method=method,
        )
        return urllib.request.urlopen(request, context=self.context, timeout=120)

    @staticmethod
    def _content_length(headers: Any, *, maximum: int, expected: int | None = None) -> int:
        value = headers.get("Content-Length")
        try:
            length = int(value)
        except (TypeError, ValueError) as error:
            raise RuntimeError("registry response requires a valid Content-Length") from error
        if length < 0 or length > maximum or (expected is not None and length != expected):
            raise RuntimeError("registry response Content-Length is outside its expected bound")
        return length

    def _small_request(self, method: str, path: str) -> tuple[bytes, Any]:
        with self._request(method, path) as response:
            length = self._content_length(
                response.headers, maximum=self.bounds.maximum_manifest_bytes
            )
            payload = response.read(length + 1)
            if len(payload) != length or response.read(1):
                raise RuntimeError("registry metadata length did not match Content-Length")
            return payload, response.headers

    def paginated(self, path: str, field: str) -> list[Any]:
        values: list[Any] = []
        current: str | None = path
        pages = 0
        while current is not None:
            payload, headers = self._small_request("GET", current)
            document = json.loads(payload)
            page = document.get(field) if isinstance(document, dict) else None
            if page is None:
                page = []
            if not isinstance(page, list):
                raise RuntimeError("registry returned malformed paginated JSON")
            values.extend(page)
            if len(values) > MAX_ITEMS or pages >= 1000:
                raise RuntimeError("registry inventory exceeded its configured bound")
            link = headers.get("Link")
            if link is None:
                current = None
            else:
                match = re.fullmatch(r"<([^>]+)>;\s*rel=\"?next\"?", link.strip())
                if match is None:
                    raise RuntimeError("registry returned a malformed pagination link")
                parsed = urllib.parse.urlsplit(match.group(1))
                if parsed.netloc and parsed.netloc != self.host:
                    raise RuntimeError("registry pagination escaped the configured host")
                current = parsed.path.removeprefix("/v2")
                if parsed.query:
                    current += "?" + parsed.query
            pages += 1
        return values

    def get_manifest(
        self, repository: str, reference: str, *, expected_size: int | None = None
    ) -> tuple[bytes, str]:
        with self._request("GET", f"/{repository}/manifests/{reference}") as response:
            length = self._content_length(
                response.headers,
                maximum=self.bounds.maximum_manifest_bytes,
                expected=expected_size,
            )
            payload = response.read(length + 1)
            if len(payload) != length or response.read(1):
                raise RuntimeError("registry manifest length did not match Content-Length")
            return payload, response.headers.get_content_type()

    @contextlib.contextmanager
    def open_blob(self, repository: str, digest: str, size: int) -> Iterator[BinaryIO]:
        response = self._request("GET", f"/{repository}/blobs/{digest}")
        try:
            self._content_length(
                response.headers,
                maximum=self.bounds.maximum_file_bytes,
                expected=size,
            )
            yield cast(BinaryIO, response)
        finally:
            response.close()

    def upload_blob(self, repository: str, digest: str, path: Path, size: int) -> None:
        try:
            with self._request("HEAD", f"/{repository}/blobs/{digest}"):
                return
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        with self._request("POST", f"/{repository}/blobs/uploads/") as response:
            location = response.headers.get("Location")
        if not isinstance(location, str):
            raise RuntimeError("registry omitted upload location")
        parsed = urllib.parse.urlsplit(location)
        if parsed.netloc and parsed.netloc != self.host:
            raise RuntimeError("registry upload escaped the configured host")
        upload_path = parsed.path.removeprefix("/v2")
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("digest", digest))
        with path.open("rb") as handle:
            with self._request(
                "PUT",
                upload_path + "?" + urllib.parse.urlencode(query),
                body=handle,
                content_length=size,
            ):
                pass

    def put_manifest(
        self, repository: str, reference: str, path: Path, size: int, media_type: str
    ) -> None:
        with path.open("rb") as handle:
            with self._request(
                "PUT",
                f"/{repository}/manifests/{reference}",
                body=handle,
                content_type=media_type,
                content_length=size,
            ):
                pass


class HashingReader:
    """A bounded reader that proves the streamed byte count and digest."""

    def __init__(self, source: BinaryIO, *, size: int, digest: str) -> None:
        self.source = source
        self.size = size
        self.expected_digest = digest
        self.count = 0
        self.hash = hashlib.sha256()

    def read(self, amount: int = -1) -> bytes:
        remaining = self.size - self.count
        if remaining <= 0:
            return b""
        requested = remaining if amount < 0 else min(amount, remaining)
        payload = self.source.read(requested)
        if not payload:
            raise RuntimeError("registry blob ended before its descriptor size")
        self.count += len(payload)
        self.hash.update(payload)
        return payload

    def finish(self) -> None:
        if self.count != self.size or self.source.read(1):
            raise RuntimeError("registry blob length did not match its descriptor")
        if "sha256:" + self.hash.hexdigest() != self.expected_digest:
            raise RuntimeError("registry blob digest did not match")


def _descriptor(value: object, *, bounds: Bounds) -> tuple[str, int]:
    digest = value.get("digest") if isinstance(value, dict) else None
    size = value.get("size") if isinstance(value, dict) else None
    if (
        not isinstance(digest, str)
        or not DIGEST.fullmatch(digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= bounds.maximum_file_bytes
    ):
        raise RuntimeError("registry manifest descriptor requires a valid digest and size")
    return digest, size


def _manifest_descriptors(
    document: Mapping[str, Any], *, bounds: Bounds
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    children_value = document.get("manifests", [])
    layers_value = document.get("layers", [])
    if not isinstance(children_value, list) or not isinstance(layers_value, list):
        raise RuntimeError("registry manifest descriptor inventory is malformed")
    children = [_descriptor(item, bounds=bounds) for item in children_value]
    blobs = [_descriptor(item, bounds=bounds) for item in layers_value]
    config = document.get("config")
    if config is not None:
        blobs.append(_descriptor(config, bounds=bounds))
    return children, blobs


def _retain_blob(
    inventory: dict[str, BlobMeta],
    *,
    digest: str,
    size: int,
    kind: str,
    repository: str,
) -> int:
    existing = inventory.get(digest)
    if existing is None:
        inventory[digest] = BlobMeta(digest, size, kind, {repository})
        return size
    elif existing.size != size or existing.kind != kind:
        raise RuntimeError("registry digest has conflicting descriptor evidence")
    else:
        existing.repositories.add(repository)
    return 0


def _tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, io.BytesIO(payload))


def _index_document(
    tags: Sequence[dict[str, str]], inventory: Mapping[str, BlobMeta], media: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "manifests": list(tags),
        "blobs": [
            {
                "digest": item.digest,
                "kind": item.kind,
                "repositories": sorted(item.repositories),
                "size": item.size,
            }
            for item in sorted(inventory.values(), key=lambda value: value.digest)
        ],
        "manifestMediaTypes": dict(sorted(media.items())),
    }


def export_archive(
    registry: RegistryClient,
    *,
    output: BinaryIO = sys.stdout.buffer,
    bounds: Bounds | None = None,
) -> dict[str, Any]:
    selected_bounds = bounds or configured_bounds()
    catalog = registry.paginated("/_catalog?n=1000", "repositories")
    tags: list[dict[str, str]] = []
    inventory: dict[str, BlobMeta] = {}
    manifest_payloads: dict[str, bytes] = {}
    manifest_documents: dict[str, Mapping[str, Any]] = {}
    media_types: dict[str, str] = {}
    resolving: set[str] = set()
    inventory_total = 0

    def associate_tree(repository: str, digest: str, depth: int = 0) -> None:
        if depth > 32:
            raise RuntimeError("registry manifest nesting exceeded its configured bound")
        meta = inventory[digest]
        if repository in meta.repositories:
            return
        meta.repositories.add(repository)
        document = manifest_documents[digest]
        children, blobs = _manifest_descriptors(document, bounds=selected_bounds)
        for child_digest, _ in children:
            associate_tree(repository, child_digest, depth + 1)
        for blob_digest, _ in blobs:
            inventory[blob_digest].repositories.add(repository)

    def retain_manifest(
        repository: str,
        reference: str,
        *,
        expected_digest: str | None = None,
        expected_size: int | None = None,
        depth: int = 0,
    ) -> tuple[str, str]:
        nonlocal inventory_total
        if depth > 32:
            raise RuntimeError("registry manifest nesting exceeded its configured bound")
        if expected_digest is not None and expected_digest in resolving:
            raise RuntimeError("registry manifest nesting contains a cycle")
        if expected_digest is not None and expected_digest in manifest_payloads:
            if inventory[expected_digest].size != expected_size:
                raise RuntimeError("registry child manifest size conflicts with its descriptor")
            associate_tree(repository, expected_digest)
            return expected_digest, media_types[expected_digest]
        payload, media_type = registry.get_manifest(
            repository, reference, expected_size=expected_size
        )
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if expected_digest is not None and digest != expected_digest:
            raise RuntimeError("registry child manifest digest did not match")
        if len(payload) > selected_bounds.maximum_manifest_bytes:
            raise RuntimeError("registry manifest exceeded its configured bound")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("registry manifest is malformed") from error
        if not isinstance(document, dict):
            raise RuntimeError("registry manifest is malformed")
        inventory_total += _retain_blob(
            inventory,
            digest=digest,
            size=len(payload),
            kind="manifest",
            repository=repository,
        )
        manifest_payloads[digest] = payload
        manifest_documents[digest] = document
        media_types[digest] = media_type
        resolving.add(digest)
        children, blobs = _manifest_descriptors(document, bounds=selected_bounds)
        for child_digest, child_size in children:
            retain_manifest(
                repository,
                child_digest,
                expected_digest=child_digest,
                expected_size=child_size,
                depth=depth + 1,
            )
        for blob_digest, blob_size in blobs:
            inventory_total += _retain_blob(
                inventory,
                digest=blob_digest,
                size=blob_size,
                kind="blob",
                repository=repository,
            )
        resolving.remove(digest)
        if len(inventory) > MAX_ITEMS or inventory_total > selected_bounds.maximum_total_bytes:
            raise RuntimeError("registry archive exceeded its configured bound")
        return digest, media_type

    for repository in sorted(catalog):
        if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
            raise RuntimeError("registry returned an unsafe repository")
        repository_tags = registry.paginated(f"/{repository}/tags/list?n=1000", "tags")
        for tag in sorted(set(repository_tags)):
            if not isinstance(tag, str) or not TAG.fullmatch(tag):
                raise RuntimeError("registry returned an unsafe tag")
            digest, media_type = retain_manifest(repository, tag)
            tags.append(
                {
                    "repository": repository,
                    "tag": tag,
                    "digest": digest,
                    "mediaType": media_type,
                }
            )

    index = _index_document(tags, inventory, media_types)
    index_payload = json.dumps(index, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(index_payload) > selected_bounds.maximum_manifest_bytes:
        raise RuntimeError("registry archive index exceeded its configured bound")
    with tarfile.open(fileobj=output, mode="w|gz") as archive:
        _tar_bytes(archive, "index.json", index_payload)
        for digest, meta in sorted(inventory.items()):
            name = f"blobs/sha256/{digest.removeprefix('sha256:')}"
            if meta.kind == "manifest":
                _tar_bytes(archive, name, manifest_payloads[digest])
                continue
            repository = sorted(meta.repositories)[0]
            with registry.open_blob(repository, digest, meta.size) as source:
                reader = HashingReader(source, size=meta.size, digest=digest)
                info = tarfile.TarInfo(name)
                info.size = meta.size
                info.mode = 0o600
                info.mtime = 0
                archive.addfile(info, cast(BinaryIO, reader))
                reader.finish()
    return index


def _parse_index(payload: bytes, *, bounds: Bounds) -> tuple[dict[str, Any], dict[str, BlobMeta]]:
    try:
        index = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("registry archive index is malformed") from error
    if not isinstance(index, dict) or index.get("format") != FORMAT:
        raise RuntimeError("registry archive format is unsupported")
    records = index.get("blobs")
    media_types = index.get("manifestMediaTypes")
    tags = index.get("manifests")
    if (
        not isinstance(records, list)
        or len(records) > MAX_ITEMS
        or not isinstance(media_types, dict)
        or not isinstance(tags, list)
        or len(tags) > MAX_ITEMS
    ):
        raise RuntimeError("registry archive index inventory is malformed")
    inventory: dict[str, BlobMeta] = {}
    total = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "digest",
            "kind",
            "repositories",
            "size",
        }:
            raise RuntimeError("registry archive blob record is malformed")
        digest, size, kind, repositories = (
            record["digest"],
            record["size"],
            record["kind"],
            record["repositories"],
        )
        if (
            not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or digest in inventory
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 <= size <= bounds.maximum_file_bytes
            or kind not in {"blob", "manifest"}
            or not isinstance(repositories, list)
            or not repositories
            or len(repositories) != len(set(repositories))
            or any(
                not isinstance(item, str) or not REPOSITORY.fullmatch(item) for item in repositories
            )
        ):
            raise RuntimeError("registry archive blob record is unsafe")
        if kind == "manifest" and size > bounds.maximum_manifest_bytes:
            raise RuntimeError("registry archive manifest exceeded its configured bound")
        inventory[digest] = BlobMeta(digest, size, kind, set(repositories))
        total += size
        if total > bounds.maximum_total_bytes:
            raise RuntimeError("registry archive exceeded its total bound")
    if set(media_types) != {
        digest for digest, meta in inventory.items() if meta.kind == "manifest"
    } or any(
        not isinstance(value, str) or not value.startswith("application/")
        for value in media_types.values()
    ):
        raise RuntimeError("registry archive manifest media inventory is malformed")
    for tag in tags:
        if not isinstance(tag, dict) or set(tag) != {
            "repository",
            "tag",
            "digest",
            "mediaType",
        }:
            raise RuntimeError("registry archive tag record is malformed")
        if (
            not isinstance(tag["repository"], str)
            or not REPOSITORY.fullmatch(tag["repository"])
            or not isinstance(tag["tag"], str)
            or not TAG.fullmatch(tag["tag"])
            or tag["digest"] not in media_types
            or tag["mediaType"] != media_types[tag["digest"]]
            or tag["repository"] not in inventory[tag["digest"]].repositories
        ):
            raise RuntimeError("registry archive tag identity is invalid")
    return index, inventory


def _spool_member(
    source: BinaryIO,
    path: Path,
    *,
    size: int,
    digest: str,
    maximum: int,
) -> None:
    if size > maximum:
        raise RuntimeError("registry archive member exceeded its configured bound")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    observed = hashlib.sha256()
    count = 0
    try:
        while count < size:
            payload = source.read(min(CHUNK, size - count))
            if not payload:
                raise RuntimeError("registry archive member ended early")
            count += len(payload)
            observed.update(payload)
            view = memoryview(payload)
            while view:
                view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if count != size or "sha256:" + observed.hexdigest() != digest:
        path.unlink(missing_ok=True)
        raise RuntimeError("registry archive member checksum did not match")


def _load_manifest_path(path: Path, meta: BlobMeta, *, bounds: Bounds) -> Mapping[str, Any]:
    if meta.size > bounds.maximum_manifest_bytes:
        raise RuntimeError("registry archive manifest exceeded its configured bound")
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("registry archive manifest is malformed") from error
    if not isinstance(document, dict):
        raise RuntimeError("registry archive manifest is malformed")
    return document


def _validate_graph(
    index: Mapping[str, Any],
    inventory: Mapping[str, BlobMeta],
    manifest_paths: Mapping[str, Path],
    *,
    bounds: Bounds,
) -> dict[str, Mapping[str, Any]]:
    documents: dict[str, Mapping[str, Any]] = {}
    for digest, path in manifest_paths.items():
        document = _load_manifest_path(path, inventory[digest], bounds=bounds)
        documents[digest] = document
        children, blobs = _manifest_descriptors(document, bounds=bounds)
        for child_digest, child_size in children:
            child = inventory.get(child_digest)
            if child is None or child.kind != "manifest" or child.size != child_size:
                raise RuntimeError("registry archive omits a child manifest descriptor")
        for blob_digest, blob_size in blobs:
            blob = inventory.get(blob_digest)
            if blob is None or blob.kind != "blob" or blob.size != blob_size:
                raise RuntimeError("registry archive omits a blob descriptor")
        for repository in inventory[digest].repositories:
            for child_digest, _ in children:
                if repository not in inventory[child_digest].repositories:
                    raise RuntimeError("registry archive child repository evidence is incomplete")
            for blob_digest, _ in blobs:
                if repository not in inventory[blob_digest].repositories:
                    raise RuntimeError("registry archive blob repository evidence is incomplete")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(digest: str, depth: int = 0) -> None:
        if digest in visited:
            return
        if depth > 32 or digest in visiting:
            raise RuntimeError("registry archive manifest nesting is unsafe")
        visiting.add(digest)
        children, _ = _manifest_descriptors(documents[digest], bounds=bounds)
        for child_digest, _ in children:
            visit(child_digest, depth + 1)
        visiting.remove(digest)
        visited.add(digest)

    for tag in cast(list[dict[str, Any]], index["manifests"]):
        visit(tag["digest"])
    if visited != set(manifest_paths):
        raise RuntimeError("registry archive contains unreachable manifests")
    return documents


def _upload_manifest_tree(
    registry: RegistryClient,
    repository: str,
    root_digest: str,
    documents: Mapping[str, Mapping[str, Any]],
    inventory: Mapping[str, BlobMeta],
    manifest_paths: Mapping[str, Path],
    media_types: Mapping[str, str],
    *,
    bounds: Bounds,
) -> None:
    uploaded: set[str] = set()

    def upload(digest: str, depth: int = 0) -> None:
        if digest in uploaded:
            return
        if depth > 32:
            raise RuntimeError("registry archive manifest nesting is unsafe")
        children, _ = _manifest_descriptors(documents[digest], bounds=bounds)
        for child_digest, _ in children:
            upload(child_digest, depth + 1)
        meta = inventory[digest]
        registry.put_manifest(
            repository,
            digest,
            manifest_paths[digest],
            meta.size,
            media_types[digest],
        )
        uploaded.add(digest)

    upload(root_digest)


def process_archive(
    *,
    source: BinaryIO = sys.stdin.buffer,
    registry: RegistryClient | None = None,
    bounds: Bounds | None = None,
    spool_root: Path | None = None,
) -> dict[str, Any]:
    selected_bounds = bounds or configured_bounds()
    with tempfile.TemporaryDirectory(prefix="registry-artifact-", dir=spool_root) as temporary:
        work = Path(temporary)
        os.chmod(work, 0o700)
        manifest_paths: dict[str, Path] = {}
        with tarfile.open(fileobj=source, mode="r|gz") as archive:
            first = archive.next()
            if (
                first is None
                or first.name != "index.json"
                or not first.isfile()
                or first.size > selected_bounds.maximum_manifest_bytes
            ):
                raise RuntimeError("registry archive index is missing or unsafe")
            first_handle = archive.extractfile(first)
            if first_handle is None:
                raise RuntimeError("registry archive index is unreadable")
            index_payload = first_handle.read(first.size + 1)
            if len(index_payload) != first.size:
                raise RuntimeError("registry archive index length did not match")
            index, inventory = _parse_index(index_payload, bounds=selected_bounds)
            expected = sorted(inventory)
            seen: list[str] = []
            member = archive.next()
            while member is not None:
                if not member.isfile() or not re.fullmatch(
                    r"blobs/sha256/[0-9a-f]{64}", member.name
                ):
                    raise RuntimeError("registry archive member is unsafe")
                digest = "sha256:" + member.name.rsplit("/", 1)[1]
                if len(seen) >= len(expected) or digest != expected[len(seen)]:
                    raise RuntimeError("registry archive member order or inventory is invalid")
                meta = inventory[digest]
                if member.size != meta.size:
                    raise RuntimeError("registry archive member size disagrees with its index")
                handle = archive.extractfile(member)
                if handle is None:
                    raise RuntimeError("registry archive member is unreadable")
                path = work / digest.removeprefix("sha256:")
                _spool_member(
                    cast(BinaryIO, handle),
                    path,
                    size=meta.size,
                    digest=digest,
                    maximum=selected_bounds.maximum_file_bytes,
                )
                seen.append(digest)
                if meta.kind == "manifest":
                    manifest_paths[digest] = path
                else:
                    if registry is not None:
                        for repository in sorted(meta.repositories):
                            registry.upload_blob(repository, digest, path, meta.size)
                    path.unlink()
                member = archive.next()
            if seen != expected:
                raise RuntimeError("registry archive blob inventory is incomplete")

        documents = _validate_graph(
            index,
            inventory,
            manifest_paths,
            bounds=selected_bounds,
        )
        if registry is not None:
            media_types = cast(dict[str, str], index["manifestMediaTypes"])
            for tag in cast(list[dict[str, str]], index["manifests"]):
                repository = tag["repository"]
                _upload_manifest_tree(
                    registry,
                    repository,
                    tag["digest"],
                    documents,
                    inventory,
                    manifest_paths,
                    media_types,
                    bounds=selected_bounds,
                )
                root = inventory[tag["digest"]]
                registry.put_manifest(
                    repository,
                    tag["tag"],
                    manifest_paths[tag["digest"]],
                    root.size,
                    tag["mediaType"],
                )
        return index


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("action", choices=("export", "import", "verify"))
    args = parser.parse_args()
    try:
        bounds = configured_bounds()
        if args.action == "export":
            index = export_archive(Registry(bounds), bounds=bounds)
        elif args.action == "import":
            index = process_archive(registry=Registry(bounds), bounds=bounds)
        else:
            index = process_archive(bounds=bounds)
        print(
            f"registry-artifacts={args.action}-verified "
            f"manifests={len(index['manifests'])} blobs={len(index['blobs'])}",
            file=sys.stderr if args.action == "export" else sys.stdout,
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
