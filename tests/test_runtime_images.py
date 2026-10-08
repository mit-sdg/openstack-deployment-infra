"""Resolving runtime version requests to official images pinned by digest.

nodejs.org and Docker Hub are replaced by a fixed fake; nothing here uses the
network."""

from __future__ import annotations

import functools
import json
import re
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from openstack_platform.config import RuntimeImages, load_platform
from openstack_platform.controller import application_runtime as app
from openstack_platform.controller.application_models import Manifest
from openstack_platform.helper import production, runtime_images
from openstack_platform.helper.main import HelperActionError
from openstack_platform.helper.runtime_images import (
    HttpResponse,
    RuntimeLookupError,
    Unreachable,
    resolve_runtime,
)
from openstack_platform.runtime_versions import (
    RuntimeRequest,
    RuntimeVersionError,
    runtime_request,
)
from openstack_platform.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]
BUILD = "11111111-1111-4111-8111-111111111111"
DEFAULTS = RuntimeImages(
    bun="registry.example/bun@sha256:" + "b" * 64,
    node="registry.example/node@sha256:" + "a" * 64,
)
NODE_DIGEST = "sha256:" + "1" * 64
BUN_DIGEST = "sha256:" + "2" * 64
INDEX = "application/vnd.oci.image.index.v1+json"


def node_request(value: str) -> RuntimeRequest | None:
    return runtime_request("node", {"engines": {"node": value}}, {}.get)


class FakeRegistry:
    """nodejs.org and Docker Hub as a fixed set of answers."""

    def __init__(
        self,
        *,
        node: list[dict[str, object]] | None = None,
        bun_pages: list[list[str]] | None = None,
        missing: set[str] | None = None,
    ) -> None:
        self.node = node or []
        self.bun_pages = bun_pages or [[]]
        self.missing = missing or set()
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []
        self.failures: dict[str, Callable[[], HttpResponse]] = {}

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], *, limit: int, timeout: float
    ) -> HttpResponse:
        self.calls.append((method, url, dict(headers)))
        assert 0 < timeout <= 10
        for prefix, failure in self.failures.items():
            if url.startswith(prefix):
                return failure()
        if url == runtime_images.NODE_RELEASES:
            return HttpResponse(200, {}, json.dumps(self.node).encode())
        if url.startswith(runtime_images.REGISTRY_TOKEN):
            return HttpResponse(200, {}, b'{"token":"anonymous.pull.token"}')
        assert headers.get("Authorization") == "Bearer anonymous.pull.token"
        if "/tags/list" in url:
            page = 0 if "last=" not in url else int(url.rsplit("last=", 1)[1])
            link = {}
            if page + 1 < len(self.bun_pages):
                link = {"link": f'</v2/oven/bun/tags/list?n=1000&last={page + 1}>; rel="next"'}
            return HttpResponse(200, link, json.dumps({"tags": self.bun_pages[page]}).encode())
        assert method == "HEAD" and "/manifests/" in url, url
        assert headers["Accept"] == (
            "application/vnd.oci.image.index.v1+json, "
            "application/vnd.docker.distribution.manifest.list.v2+json"
        )
        tag = url.rsplit("/", 1)[1]
        if tag in self.missing:
            return HttpResponse(404, {}, b"")
        digest = NODE_DIGEST if "/library/node/" in url else BUN_DIGEST
        return HttpResponse(200, {"docker-content-digest": digest, "content-type": INDEX}, b"")

    def urls(self, method: str = "HEAD") -> list[str]:
        return [url for verb, url, _headers in self.calls if verb == method]


NODE_INDEX = [
    {"version": "v26.1.0", "lts": False},
    {"version": "v24.9.0", "lts": "Krypton"},
    {"version": "v22.12.0", "lts": "Jod"},
    {"version": "v22.11.0", "lts": "Jod"},
    {"version": "v22.10.0", "lts": False},
    {"version": "v20.18.1", "lts": "Iron"},
    {"version": "v19.9.0", "lts": False},
    {"version": "v18.20.5", "lts": "Hydrogen"},
    {"version": "not-a-version"},
    "ignored",
]


class ResolutionTests(unittest.TestCase):
    def resolve(self, registry: FakeRegistry, runtime: str, request: object) -> object:
        return resolve_runtime(
            runtime,
            request,  # type: ignore[arg-type]
            getattr(DEFAULTS, runtime),
            http=registry,
        )

    def test_no_request_keeps_the_policy_image_without_any_lookup(self) -> None:
        registry = FakeRegistry()
        resolved = resolve_runtime("node", None, DEFAULTS.node, http=registry)
        self.assertEqual(
            resolved.evidence(),
            {"runtime": "node", "version": None, "image": DEFAULTS.node, "source": "default"},
        )
        self.assertEqual(registry.calls, [])

    def test_node_picks_the_newest_matching_release(self) -> None:
        registry = FakeRegistry(node=NODE_INDEX)
        resolved = resolve_runtime("node", node_request(">=22 <23"), DEFAULTS.node, http=registry)
        self.assertEqual(
            resolved.evidence(),
            {
                "runtime": "node",
                "version": "22.12.0",
                "image": f"docker.io/library/node@{NODE_DIGEST}",
                "source": "engines.node >=22 <23",
            },
        )
        self.assertEqual(
            registry.urls(),
            ["https://registry-1.docker.io/v2/library/node/manifests/22.12.0-slim"],
        )
        token = [url for _method, url, _headers in registry.calls if "auth.docker.io" in url]
        self.assertEqual(
            token,
            [
                "https://auth.docker.io/token?service=registry.docker.io"
                "&scope=repository%3Alibrary%2Fnode%3Apull"
            ],
        )

    def test_an_open_node_range_prefers_the_newest_lts_release(self) -> None:
        registry = FakeRegistry(node=NODE_INDEX)
        resolved = resolve_runtime("node", node_request(">=20"), DEFAULTS.node, http=registry)
        # 26.1.0 is newer but a Current release; 24.9.0 is the newest LTS.
        self.assertEqual(resolved.evidence()["version"], "24.9.0")
        current = resolve_runtime("node", node_request("26"), DEFAULTS.node, http=registry)
        # A range that admits only Current releases still gets the newest one.
        self.assertEqual(current.evidence()["version"], "26.1.0")

    def test_lts_asks_for_the_newest_long_term_support_release(self) -> None:
        registry = FakeRegistry(node=NODE_INDEX)
        request = runtime_request("node", {}, {".nvmrc": "lts/*"}.get)
        resolved = resolve_runtime("node", request, DEFAULTS.node, http=registry)
        self.assertEqual((resolved.version, resolved.source), ("24.9.0", ".nvmrc lts/*"))

    def test_releases_older_than_the_oldest_line_are_never_chosen(self) -> None:
        registry = FakeRegistry(node=NODE_INDEX)
        resolved = resolve_runtime("node", node_request("^18 || ^20"), DEFAULTS.node, http=registry)
        self.assertEqual(resolved.version, "20.18.1")
        old_only = FakeRegistry(node=[{"version": "v19.9.0", "lts": False}])
        with self.assertRaises(RuntimeVersionError) as caught:
            resolve_runtime("node", node_request(">=19"), DEFAULTS.node, http=old_only)
        self.assertEqual(str(caught.exception), 'No Node.js release matches engines.node ">=19".')

    def test_an_unpublished_newest_tag_falls_back_to_the_next_release(self) -> None:
        registry = FakeRegistry(node=NODE_INDEX, missing={"22.12.0-slim"})
        resolved = resolve_runtime("node", node_request("22"), DEFAULTS.node, http=registry)
        self.assertEqual(resolved.version, "22.11.0")
        self.assertEqual(
            [url.rsplit("/", 1)[1] for url in registry.urls()],
            ["22.12.0-slim", "22.11.0-slim"],
        )
        absent = FakeRegistry(
            node=NODE_INDEX, missing={"22.12.0-slim", "22.11.0-slim", "22.10.0-slim"}
        )
        with self.assertRaises(RuntimeLookupError):
            resolve_runtime("node", node_request("22"), DEFAULTS.node, http=absent)
        self.assertEqual(len(absent.urls()), 3)

    def test_an_unsatisfied_request_is_rejected_by_name(self) -> None:
        with self.assertRaises(RuntimeVersionError) as caught:
            resolve_runtime(
                "node", node_request(">=99"), DEFAULTS.node, http=FakeRegistry(node=NODE_INDEX)
            )
        self.assertEqual(str(caught.exception), 'No Node.js release matches engines.node ">=99".')

    def test_bun_reads_every_tag_page_and_only_slim_releases(self) -> None:
        registry = FakeRegistry(
            bun_pages=[
                ["1.0.0-slim", "1.1.0-slim", "1.2.0", "latest", "slim"],
                ["1.3.4-slim", "1.3.5-alpine", "1.3-slim", "canary-slim", "1.4.0-debian"],
                ["1.3.3-slim", "1.3.10-slim", "1.4.0-canary-slim"],
            ]
        )
        request = runtime_request("bun", {"engines": {"bun": "^1.3"}}, {}.get)
        resolved = resolve_runtime("bun", request, DEFAULTS.bun, http=registry)
        self.assertEqual(
            resolved.evidence(),
            {
                "runtime": "bun",
                "version": "1.3.10",
                "image": f"docker.io/oven/bun@{BUN_DIGEST}",
                "source": "engines.bun ^1.3",
            },
        )
        self.assertEqual(len(registry.urls("GET")), 4)  # one token, three pages
        exact = runtime_request("bun", {"packageManager": "bun@1.3.4"}, {}.get)
        self.assertEqual(
            resolve_runtime("bun", exact, DEFAULTS.bun, http=registry).version, "1.3.4"
        )

    def test_a_tag_page_link_must_stay_in_the_repository(self) -> None:
        registry = FakeRegistry(bun_pages=[["1.3.4-slim"]])
        registry.failures[runtime_images.REGISTRY + "/v2/oven/bun/tags/list"] = lambda: (
            HttpResponse(200, {"link": '</v2/other/tags/list?last=1>; rel="next"'}, b'{"tags":[]}')
        )
        request = runtime_request("bun", {"packageManager": "bun@1.3.4"}, {}.get)
        with self.assertRaises(RuntimeLookupError):
            resolve_runtime("bun", request, DEFAULTS.bun, http=registry)
        endless = FakeRegistry()
        endless.failures[runtime_images.REGISTRY + "/v2/oven/bun/tags/list"] = lambda: HttpResponse(
            200, {"link": '</v2/oven/bun/tags/list?last=x>; rel="next"'}, b'{"tags":[]}'
        )
        with self.assertRaises(RuntimeLookupError):
            resolve_runtime("bun", request, DEFAULTS.bun, http=endless)

    def test_lookup_failures_are_retryable_and_name_the_runtime(self) -> None:
        def unreachable() -> HttpResponse:
            raise Unreachable("TimeoutError")

        failures: list[tuple[str, Callable[[], HttpResponse]]] = [
            (runtime_images.NODE_RELEASES, unreachable),
            (runtime_images.NODE_RELEASES, lambda: HttpResponse(503, {}, b"")),
            (runtime_images.NODE_RELEASES, lambda: HttpResponse(200, {}, b"{not json")),
            (runtime_images.NODE_RELEASES, lambda: HttpResponse(200, {}, b"{}")),
            (runtime_images.REGISTRY_TOKEN, lambda: HttpResponse(200, {}, b'{"token":7}')),
            (runtime_images.REGISTRY_TOKEN, lambda: HttpResponse(429, {}, b"")),
            (
                runtime_images.REGISTRY + "/v2/library/node/manifests/",
                lambda: HttpResponse(200, {"content-type": INDEX}, b""),
            ),
            (
                runtime_images.REGISTRY + "/v2/library/node/manifests/",
                lambda: HttpResponse(
                    200,
                    {
                        "docker-content-digest": NODE_DIGEST,
                        "content-type": "application/vnd.oci.image.manifest.v1+json",
                    },
                    b"",
                ),
            ),
            (
                runtime_images.REGISTRY + "/v2/library/node/manifests/",
                lambda: HttpResponse(401, {}, b""),
            ),
        ]
        for prefix, failure in failures:
            with self.subTest(prefix):
                registry = FakeRegistry(node=NODE_INDEX)
                registry.failures[prefix] = failure
                with self.assertRaises(RuntimeLookupError) as caught:
                    resolve_runtime("node", node_request("22"), DEFAULTS.node, http=registry)
                self.assertEqual(
                    str(caught.exception),
                    "Couldn't look up Node.js versions. Try deploying again in a few minutes.",
                )

    def test_lookups_share_one_bounded_deadline(self) -> None:
        now = [0.0]

        def clock() -> float:
            now[0] += 25
            return now[0]

        registry = FakeRegistry(node=NODE_INDEX)
        with self.assertRaises(RuntimeLookupError):
            resolve_runtime("node", node_request("22"), DEFAULTS.node, http=registry, clock=clock)
        self.assertLess(len(registry.calls), 3)

    def test_only_https_is_requested(self) -> None:
        with self.assertRaises(ValueError):
            runtime_images.https_request(
                "GET", "http://nodejs.org/dist/index.json", {}, limit=1, timeout=1
            )

    def test_a_malformed_request_is_refused_before_any_lookup(self) -> None:
        registry = FakeRegistry(node=NODE_INDEX)
        with self.assertRaises(ValidationError):
            self.resolve(registry, "node", object())
        bun = runtime_request("bun", {"packageManager": "bun@1.3.4"}, {}.get)
        with self.assertRaises(ValidationError):
            self.resolve(registry, "node", bun)
        self.assertEqual(registry.calls, [])


class HelperBuildTests(unittest.TestCase):
    """app.build resolves the checkout's request before any builder exists."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = SimpleNamespace(
            platform=load_platform(ROOT / "config/platform.example.json"),
            root=self.root,
            admin_state=self.root,
        )
        self.arguments = {
            "buildId": BUILD,
            "slug": "notes",
            "repository": "https://github.com/ada/notes",
            "requestedRef": "main",
            "commit": "a" * 40,
            "configurationRevision": 1,
            "configuration": {
                "schemaVersion": 1,
                "build": {
                    "runtime": "node",
                    "packages": ["."],
                    "buildScript": None,
                    "startScript": "start",
                },
                "runtime": {"port": 8080, "healthPath": "/"},
                "storageBindings": [],
            },
            "builderImageId": "22222222-2222-4222-8222-222222222222",
            "runtimeImages": {"bun": DEFAULTS.bun, "node": DEFAULTS.node},
            "sourceLimit": 1_048_576,
            "buildLogLimit": 65_536,
            "connectSeconds": 5,
            "deadlineAt": "2030-01-01T00:00:00Z",
        }
        self.recipes: list[app.Recipe] = []

    def build(self, registry: FakeRegistry, package: dict[str, Any]) -> Mapping[str, Any]:
        def acquire(_repository: str, _commit: str, destination: Path, **_bounds: Any) -> Path:
            destination.mkdir()
            (destination / "package.json").write_text(json.dumps(package))
            (destination / "package-lock.json").write_text("{}")
            return destination

        def builder(**values: Any) -> app.BuildResult:
            self.recipes.append(values["recipe"])
            values["build_log_sink"].write(b"builder output\n")
            values["build_log_sink"].flush()
            return app.BuildResult(
                BUILD, values["image_name"] + "@sha256:" + "d" * 64, "sha256:" + "d" * 64, True
            )

        with (
            mock.patch.object(production, "helper_runtime", return_value=self.runtime),
            mock.patch.object(app, "acquire_github_commit", side_effect=acquire),
            mock.patch.object(
                production, "resolve_runtime", functools.partial(resolve_runtime, http=registry)
            ),
            mock.patch.object(app, "build_with_disposable_builder", side_effect=builder),
        ):
            return production._build_application(self.arguments)

    def log(self) -> str:
        return (self.root / f"controller/build-logs/notes/{BUILD}.log").read_text()

    def package(self, **fields: Any) -> dict[str, Any]:
        return {"scripts": {"start": "node ."}, **fields}

    def test_the_build_uses_and_reports_the_resolved_runtime(self) -> None:
        result = self.build(
            FakeRegistry(node=NODE_INDEX), self.package(engines={"node": ">=22 <23"})
        )
        image = f"docker.io/library/node@{NODE_DIGEST}"
        self.assertEqual(
            result["runtime"],
            {
                "runtime": "node",
                "version": "22.12.0",
                "image": image,
                "source": "engines.node >=22 <23",
            },
        )
        (recipe,) = self.recipes
        self.assertTrue(recipe.dockerfile.startswith(f"FROM {image}\n".encode()))
        self.assertEqual(result["recipeHash"], recipe.sha256)
        self.assertEqual(result["log"], self.log())
        self.assertTrue(
            all(
                re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ", line)
                for line in self.log().splitlines()
            )
        )
        self.assertIn("builder output", self.log())
        self.assertIn(f"Using Node.js 22.12.0 ({image}) from engines.node >=22 <23.", self.log())

    def test_no_request_builds_the_policy_image_exactly_as_before(self) -> None:
        registry = FakeRegistry(node=NODE_INDEX)
        result = self.build(registry, self.package(packageManager="bun@1.3.4"))
        self.assertEqual(registry.calls, [])
        before = app.generate_recipe(Manifest("node", (".",), None, "start", 8080, "/"), DEFAULTS)
        self.assertEqual(self.recipes, [before])
        self.assertEqual(
            result["runtime"],
            {"runtime": "node", "version": None, "image": DEFAULTS.node, "source": "default"},
        )

    def test_requests_that_cannot_be_built_are_rejected_before_any_builder(self) -> None:
        for engines, message in (
            ("16", 'engines.node "16" asks for Node.js 16, older than the oldest'),
            (">=99", 'No Node.js release matches engines.node ">=99".'),
        ):
            with self.subTest(engines):
                with self.assertRaises(HelperActionError) as caught:
                    self.build(
                        FakeRegistry(node=NODE_INDEX), self.package(engines={"node": engines})
                    )
                self.assertEqual(caught.exception.code, "BUILD_REJECTED")
                self.assertIn(message, self.log())
        self.assertEqual(self.recipes, [])

    def test_a_failed_lookup_is_reported_as_retryable(self) -> None:
        registry = FakeRegistry(node=NODE_INDEX)
        registry.failures[runtime_images.NODE_RELEASES] = lambda: HttpResponse(503, {}, b"")
        with self.assertRaises(HelperActionError) as caught:
            self.build(registry, self.package(engines={"node": "22"}))
        self.assertEqual(caught.exception.code, "RUNTIME_UNAVAILABLE")
        self.assertIn(
            "Couldn't look up Node.js versions. Try deploying again in a few minutes.", self.log()
        )
        state = self.root / f"controller/build-logs/notes/{BUILD}.state"
        self.assertEqual(state.read_text().strip(), "failed")
        self.assertEqual(self.recipes, [])


if __name__ == "__main__":
    unittest.main()
