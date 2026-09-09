"""Read-only, bounded availability checks for one digest-pinned application image."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, NoReturn

from ..runtime import HttpResult, bounded_http
from ..validation import oci_digest_pin, slug
from .main import HelperActionError

_MANIFESTS = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_INDEXES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def verify_image(
    host: str,
    application_slug: str,
    image: str,
    *,
    authorization: str,
    ssl_context: object,
    http: Callable[..., HttpResult] = bounded_http,
) -> Mapping[str, Any]:
    checked_slug = slug(application_slug)
    pin = oci_digest_pin(image, field="retained image")
    repository = f"projects/{checked_slug}/app"
    if not pin.startswith(f"{host}/{repository}@"):
        raise HelperActionError(
            "INVALID_ARGS", "artifact is outside the application registry repository"
        )
    base = f"https://{host}/v2/{repository}"
    headers = {"Authorization": authorization, "Accept": ", ".join(sorted(_MANIFESTS | _INDEXES))}
    deadline = time.monotonic() + 30
    manifests: set[str] = set()
    blobs: set[str] = set()

    def fail() -> NoReturn:
        raise HelperActionError(
            "ARTIFACT_UNAVAILABLE", "retained image content is unavailable or invalid"
        )

    def request(path: str, method: str) -> HttpResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail()
        result = http(
            f"{base}/{path}",
            method=method,
            headers=headers,
            timeout_seconds=min(remaining, 10),
            response_limit=8 * 1024 * 1024,
            ssl_context=ssl_context,
            allow_redirects=False,
        )
        if result.status != 200:
            fail()
        return result

    def descriptor(value: object) -> tuple[str, int]:
        if not isinstance(value, dict):
            fail()
        assert isinstance(value, dict)
        digest, size = value.get("digest"), value.get("size")
        if (
            not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            or type(size) is not int
            or size < 0
        ):
            fail()
        assert isinstance(digest, str) and isinstance(size, int)
        return digest, size

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                fail()
            value[key] = item
        return value

    def manifest(digest: str, expected_size: int | None = None) -> None:
        if digest in manifests:
            return
        if len(manifests) >= 32:
            fail()
        manifests.add(digest)
        result = request(f"manifests/{digest}", "GET")
        if f"sha256:{hashlib.sha256(result.body).hexdigest()}" != digest or (
            expected_size is not None and len(result.body) != expected_size
        ):
            fail()
        try:
            value = json.loads(result.body, object_pairs_hook=pairs)
        except (ValueError, UnicodeError):
            fail()
        if not isinstance(value, dict) or value.get("schemaVersion") != 2:
            fail()
        if value.get("mediaType") in _INDEXES:
            children = value.get("manifests")
            if not isinstance(children, list) or not 1 <= len(children) <= 32:
                fail()
            for child in children:
                child_digest, size = descriptor(child)
                manifest(child_digest, size)
        elif value.get("mediaType") in _MANIFESTS:
            layers = value.get("layers")
            if not isinstance(layers, list) or len(layers) > 512:
                fail()
            for item in [value.get("config"), *layers]:
                blob_digest, size = descriptor(item)
                if blob_digest in blobs:
                    continue
                if len(blobs) >= 512:
                    fail()
                blobs.add(blob_digest)
                observed = request(f"blobs/{blob_digest}", "HEAD")
                if observed.headers.get(
                    "docker-content-digest"
                ) != blob_digest or observed.headers.get("content-length") != str(size):
                    fail()
        else:
            fail()

    manifest(pin.rsplit("@", 1)[1])
    return {"slug": checked_slug, "image": pin, "available": True}
