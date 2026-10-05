"""Resolve a runtime version request to an official image pinned by digest.

app.build calls this after validating the exact checkout. Node.js releases come
from nodejs.org's release index and Bun releases from the oven/bun tags on
Docker Hub. The newest release that satisfies the request and is no older than
the oldest supported line wins; its -slim tag is then resolved to the digest of
its multi-platform index, falling back to the next release while a new tag is
not published yet. Every lookup is anonymous, HTTPS-only, bounded in time and
size, and never follows a redirect. Docker Hub's pull token is short-lived,
anonymous, and never logged.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Protocol

from ..runtime_versions import (
    IMAGE_REPOSITORIES,
    IMAGE_VARIANT,
    OLDEST_LINES,
    RUNTIME_NAMES,
    ResolvedRuntime,
    RuntimeRequest,
    parse_version,
    unmatched,
)
from ..validation import ValidationError

NODE_RELEASES = "https://nodejs.org/dist/index.json"
REGISTRY = "https://registry-1.docker.io"
REGISTRY_TOKEN = "https://auth.docker.io/token"
INDEX_MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
USER_AGENT = "openstack-platform-helper/1 (runtime version lookup)"
REQUEST_SECONDS = 10.0
LOOKUP_SECONDS = 60.0
RELEASES_BYTES = 2_097_152
TAGS_BYTES = 524_288
TOKEN_BYTES = 65_536
TAG_PAGE_SIZE = 1_000
MAXIMUM_TAG_PAGES = 20
# Releases tried, newest first, when a matching tag is not published yet.
MAXIMUM_IMAGE_ATTEMPTS = 3
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SYSTEM_CA_BUNDLES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/ssl/certs/ca-bundle.crt"),
)

_Version = tuple[int, int, int]


class RuntimeLookupError(RuntimeError):
    """Release or image metadata couldn't be read; deploying later may work."""

    def __init__(self, runtime: str) -> None:
        super().__init__(
            f"Couldn't look up {RUNTIME_NAMES[runtime]} versions. "
            "Try deploying again in a few minutes."
        )


class Unreachable(Exception):
    """One HTTPS exchange failed before a response arrived."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    # Lowercase header names.
    headers: Mapping[str, str]
    body: bytes


class Http(Protocol):
    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], *, limit: int, timeout: float
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _tls_context() -> ssl.SSLContext:
    # Like identity's client: the OS bundle, not ambient SSL_CERT_FILE.
    bundle = next((path for path in _SYSTEM_CA_BUNDLES if path.is_file()), None)
    context = ssl.create_default_context(cafile=None if bundle is None else str(bundle))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def https_request(
    method: str, url: str, headers: Mapping[str, str], *, limit: int, timeout: float
) -> HttpResponse:
    """One bounded HTTPS exchange. Error statuses are returned, not raised."""
    if not url.startswith("https://"):
        raise ValueError("runtime lookups use HTTPS")
    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": USER_AGENT, **headers}
    )
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=_tls_context())
    )

    def response(status: int, received: Message, body: bytes) -> HttpResponse:
        if len(body) > limit:
            raise Unreachable("response exceeded its size limit")
        return HttpResponse(status, {key.lower(): value for key, value in received.items()}, body)

    try:
        with opener.open(request, timeout=timeout) as answer:
            return response(answer.status, answer.headers, answer.read(limit + 1))
    except urllib.error.HTTPError as error:
        with error:
            return response(error.code, error.headers, error.read(limit + 1))
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise Unreachable(error.__class__.__name__) from None


class _Lookup:
    """One resolution's lookups, sharing a deadline and a registry token."""

    def __init__(self, runtime: str, http: Http, clock: Callable[[], float]) -> None:
        self.runtime = runtime
        self.path = IMAGE_REPOSITORIES[runtime].removeprefix("docker.io/")
        self.http = http
        self.clock = clock
        self.deadline = clock() + LOOKUP_SECONDS
        self.token: str | None = None

    def fetch(
        self,
        url: str,
        *,
        limit: int,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RuntimeLookupError(self.runtime)
        try:
            return self.http(
                method, url, headers or {}, limit=limit, timeout=min(REQUEST_SECONDS, remaining)
            )
        except Unreachable:
            raise RuntimeLookupError(self.runtime) from None

    def json(self, url: str, *, limit: int) -> Any:
        answer = self.fetch(url, limit=limit)
        if answer.status != 200:
            raise RuntimeLookupError(self.runtime)
        try:
            return json.loads(answer.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeLookupError(self.runtime) from None

    def authorization(self) -> dict[str, str]:
        if self.token is None:
            query = urllib.parse.urlencode(
                {"service": "registry.docker.io", "scope": f"repository:{self.path}:pull"}
            )
            value = self.json(f"{REGISTRY_TOKEN}?{query}", limit=TOKEN_BYTES)
            token = value.get("token") if isinstance(value, dict) else None
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", token):
                raise RuntimeLookupError(self.runtime)
            self.token = token
        return {"Authorization": f"Bearer {self.token}"}

    def releases(self) -> list[tuple[_Version, bool]]:
        """Published releases, newest first, each with whether it is LTS."""
        found: dict[_Version, bool] = {}
        if self.runtime == "node":
            index = self.json(NODE_RELEASES, limit=RELEASES_BYTES)
            if not isinstance(index, list):
                raise RuntimeLookupError(self.runtime)
            for item in index:
                name = item.get("version") if isinstance(item, dict) else None
                if isinstance(name, str) and name.startswith("v"):
                    version = parse_version(name.removeprefix("v"))
                    if version is not None:
                        # lts is false, or the name of the release's LTS line.
                        found[version] = isinstance(item.get("lts"), str)
        else:
            suffix = f"-{IMAGE_VARIANT}"
            page = f"/v2/{self.path}/tags/list?n={TAG_PAGE_SIZE}"
            for _ in range(MAXIMUM_TAG_PAGES):
                answer = self.fetch(REGISTRY + page, limit=TAGS_BYTES, headers=self.authorization())
                if answer.status != 200:
                    raise RuntimeLookupError(self.runtime)
                try:
                    tags = json.loads(answer.body).get("tags")
                except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                    raise RuntimeLookupError(self.runtime) from None
                if not isinstance(tags, list):
                    raise RuntimeLookupError(self.runtime)
                for tag in tags:
                    if isinstance(tag, str) and tag.endswith(suffix):
                        version = parse_version(tag.removesuffix(suffix))
                        if version is not None:
                            found[version] = False
                following = re.fullmatch(
                    r'<(/v2/[^>]+)>\s*;\s*rel="next"', answer.headers.get("link", "")
                )
                if following is None:
                    break
                page = following[1]
                if not page.startswith(f"/v2/{self.path}/tags/list?"):
                    raise RuntimeLookupError(self.runtime)
            else:
                raise RuntimeLookupError(self.runtime)
        return sorted(found.items(), reverse=True)

    def index_digest(self, version: _Version) -> str | None:
        """The digest of a release's -slim index, or None if it isn't published."""
        tag = ".".join(map(str, version)) + f"-{IMAGE_VARIANT}"
        answer = self.fetch(
            f"{REGISTRY}/v2/{self.path}/manifests/{tag}",
            method="HEAD",
            limit=0,
            headers={**self.authorization(), "Accept": ", ".join(INDEX_MEDIA_TYPES)},
        )
        if answer.status == 404:
            return None
        digest = answer.headers.get("docker-content-digest", "")
        media_type = answer.headers.get("content-type", "").split(";", 1)[0].strip()
        if answer.status != 200 or not _DIGEST.fullmatch(digest):
            raise RuntimeLookupError(self.runtime)
        if media_type not in INDEX_MEDIA_TYPES:
            raise RuntimeLookupError(self.runtime)
        return digest


def resolve_runtime(
    runtime: str,
    request: RuntimeRequest | None,
    default_image: str,
    *,
    http: Http = https_request,
    clock: Callable[[], float] = time.monotonic,
) -> ResolvedRuntime:
    """The image one build uses. No request keeps the policy's default pin.

    Raises RuntimeVersionError when no published release matches, and
    RuntimeLookupError when release or image metadata can't be read.
    """
    if runtime not in RUNTIME_NAMES:
        raise ValidationError("runtime must be node or bun")
    if request is None:
        return ResolvedRuntime(runtime, None, default_image, "default")
    if not isinstance(request, RuntimeRequest) or request.runtime != runtime:
        raise ValidationError("runtime version request is malformed")
    lookup = _Lookup(runtime, http, clock)
    oldest = OLDEST_LINES[runtime]
    matches = [
        version
        for version, lts in lookup.releases()
        if version[:2] >= oldest
        and (lts if request.versions is None else request.versions.admits(version))
    ]
    if not matches:
        raise unmatched(request)
    for version in matches[:MAXIMUM_IMAGE_ATTEMPTS]:
        digest = lookup.index_digest(version)
        if digest is not None:
            return ResolvedRuntime(
                runtime,
                ".".join(map(str, version)),
                f"{IMAGE_REPOSITORIES[runtime]}@{digest}",
                request.source,
            )
    raise RuntimeLookupError(runtime)
