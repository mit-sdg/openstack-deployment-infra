"""Runtime version requests and the evidence of the runtime a build used.

The owner portal's deploy check (utils/runtimeVersions.ts) runs the same
runtime-version-cases.json, so both parsers agree on every shared case."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from openstack_platform.config import RuntimeImages
from openstack_platform.runtime_versions import (
    RuntimeVersionError,
    parse_range,
    resolved_runtime,
    runtime_request,
)
from openstack_platform.validation import ValidationError

CASES = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "frontend/owner-portal/src/utils/runtime-version-cases.json"
    ).read_text()
)
DEFAULTS = RuntimeImages(
    bun="registry.example/bun@sha256:" + "b" * 64,
    node="registry.example/node@sha256:" + "a" * 64,
)
NODE_DIGEST = "sha256:" + "1" * 64
BUN_DIGEST = "sha256:" + "2" * 64


def version(text: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


class SharedCaseTests(unittest.TestCase):
    def test_ranges_match_the_shared_cases(self) -> None:
        self.assertGreater(len(CASES["ranges"]), 40)
        for case in CASES["ranges"]:
            with self.subTest(case["range"]):
                parsed = parse_range(case["range"])
                if not case["valid"]:
                    self.assertIsNone(parsed)
                    continue
                assert parsed is not None
                for item in case["admits"]:
                    self.assertTrue(parsed.admits(version(item)), item)
                for item in case["rejects"]:
                    self.assertFalse(parsed.admits(version(item)), item)

    def test_requests_match_the_shared_cases(self) -> None:
        self.assertGreater(len(CASES["requests"]), 30)
        for case in CASES["requests"]:
            with self.subTest(case["name"]):
                files: dict[str, str] = case["files"]
                read = files.get
                if "error" in case:
                    with self.assertRaises(RuntimeVersionError) as caught:
                        runtime_request(case["runtime"], case["packageJson"], read)
                    self.assertEqual(str(caught.exception), case["error"])
                    continue
                request = runtime_request(case["runtime"], case["packageJson"], read)
                if case["request"] is None:
                    self.assertIsNone(request)
                else:
                    assert request is not None
                    self.assertEqual(
                        {"source": request.source, "label": request.describe()}, case["request"]
                    )

    def test_files_are_read_only_when_package_json_asks_for_nothing(self) -> None:
        read: list[str] = []

        def reader(name: str) -> str | None:
            read.append(name)
            return None

        runtime_request("node", {"engines": {"node": "22"}}, reader)
        runtime_request("bun", {"packageManager": "bun@1.3.4"}, reader)
        self.assertEqual(read, [])
        runtime_request("node", {}, reader)
        runtime_request("bun", {}, reader)
        self.assertEqual(read, [".nvmrc", ".node-version", ".bun-version"])


class EvidenceTests(unittest.TestCase):
    def evidence(self, **changes: object) -> dict[str, object]:
        return {
            "runtime": "node",
            "version": "22.11.0",
            "image": f"docker.io/library/node@{NODE_DIGEST}",
            "source": "engines.node >=22 <23",
            **changes,
        }

    def test_valid_evidence_is_accepted(self) -> None:
        accepted = [
            self.evidence(),
            self.evidence(version="24.9.0", source=".nvmrc lts/*"),
            self.evidence(version="22.11.0", source=".node-version v22"),
            {
                "runtime": "bun",
                "version": "1.3.4",
                "image": f"docker.io/oven/bun@{BUN_DIGEST}",
                "source": "packageManager bun@1.3.4",
            },
            {"runtime": "node", "version": None, "image": DEFAULTS.node, "source": "default"},
        ]
        for value in accepted:
            with self.subTest(value):
                runtime = str(value["runtime"])
                self.assertEqual(
                    resolved_runtime(value, runtime=runtime, defaults=DEFAULTS).evidence(), value
                )

    def test_evidence_outside_what_a_build_could_resolve_is_refused(self) -> None:
        refused = [
            None,
            {**self.evidence(), "extra": True},
            self.evidence(runtime="bun"),
            self.evidence(image="docker.io/library/node:22-slim"),
            self.evidence(image=f"docker.io/library/nodejs@{NODE_DIGEST}"),
            self.evidence(image=f"docker.io/oven/bun@{BUN_DIGEST}"),
            self.evidence(image=f"registry.example/library/node@{NODE_DIGEST}"),
            self.evidence(version="22.11"),
            self.evidence(version="18.20.5", source="engines.node 18"),
            self.evidence(version="23.0.0"),
            self.evidence(source="engines.bun >=22"),
            self.evidence(source="engines.node"),
            self.evidence(source="package.json >=22"),
            self.evidence(source="engines.node foo"),
            self.evidence(source="default"),
            self.evidence(version=None, image=DEFAULTS.bun, source="default"),
        ]
        for value in refused:
            with self.subTest(value), self.assertRaises(ValidationError):
                resolved_runtime(value, runtime="node", defaults=DEFAULTS)

    def test_resolved_images_replace_only_their_runtimes_pin(self) -> None:
        resolved = resolved_runtime(self.evidence(), runtime="node", defaults=DEFAULTS)
        self.assertEqual(
            resolved.runtime_images(DEFAULTS),
            RuntimeImages(bun=DEFAULTS.bun, node=f"docker.io/library/node@{NODE_DIGEST}"),
        )
        default = resolved_runtime(
            {"runtime": "bun", "version": None, "image": DEFAULTS.bun, "source": "default"},
            runtime="bun",
            defaults=DEFAULTS,
        )
        self.assertEqual(default.runtime_images(DEFAULTS), DEFAULTS)


if __name__ == "__main__":
    unittest.main()
