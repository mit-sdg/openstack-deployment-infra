from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openstack_platform.config import load, load_platform, load_policy
from openstack_platform.validation import (
    ValidationError,
    commit,
    env_key,
    health_path,
    openstack_uuid,
    relative_path,
    repository_url,
    resolve_inside,
    sha256_hex,
    slug,
    uuid,
)

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "registry.example/image@sha256:" + "a" * 64
RECIPIENT = "age1" + "q" * 58


def policy_document() -> dict[str, object]:
    return {
        "standard": {
            "workerFlavor": "example.1c2g",
            "cpuMHz": 1000,
            "memoryMiB": 2048,
            "postgresConnections": 10,
            "postgresMeasuredBytes": 2147483648,
            "mongoMeasuredBytes": 2147483648,
            "s3Bytes": 5368709120,
            "s3Objects": 100000,
        },
        "runtimeImages": {"bun": DIGEST, "node": "registry.example/image@sha256:" + "b" * 64},
        "backupAgeRecipient": RECIPIENT,
    }


def platform_document() -> dict[str, object]:
    document: dict[str, object] = json.loads(
        (ROOT / "config/platform.example.json").read_text(encoding="utf-8")
    )
    document["hosts"] = {
        **document["hosts"],  # type: ignore[dict-item]
        "admin": "example-admin",
    }
    return document


class CommonValidationTests(unittest.TestCase):
    def test_identifiers_are_canonical(self) -> None:
        self.assertEqual(slug("my-app"), "my-app")
        self.assertEqual(env_key("DATABASE_URL"), "DATABASE_URL")
        self.assertEqual(commit("a" * 40), "a" * 40)
        self.assertEqual(sha256_hex("b" * 64), "b" * 64)
        self.assertEqual(
            uuid("00000000-0000-4000-8000-000000000000"),
            "00000000-0000-4000-8000-000000000000",
        )
        for invalid in ("A", "bad--slug", "-bad", "bad_"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                slug(invalid)
        with self.assertRaises(ValidationError):
            commit("A" * 40)
        with self.assertRaises(ValidationError):
            sha256_hex("B" * 64)
        for invalid_uuid in (
            "abcdef00000040008000000000000000",
            "ABCDEF00-0000-4000-8000-000000000000",
        ):
            with self.subTest(invalid_uuid=invalid_uuid), self.assertRaises(ValidationError):
                uuid(invalid_uuid)
        with self.assertRaises(ValidationError):
            env_key("lowercase")

    def test_openstack_uuid_normalizes_only_exact_lowercase_provider_forms(self) -> None:
        canonical = "7a3c91d2-4b8e-42f0-9c15-6de0f28a15b3"
        compact = "7a3c91d24b8e42f09c156de0f28a15b3"
        self.assertEqual(openstack_uuid(canonical), canonical)
        self.assertEqual(openstack_uuid(compact), canonical)
        for malformed in (
            compact.upper(),
            canonical.upper(),
            compact[:-1],
            compact + "0",
            "{" + canonical + "}",
            "urn:uuid:" + canonical,
            canonical + "\n",
            0,
            None,
        ):
            with self.subTest(malformed=malformed), self.assertRaises(ValidationError):
                openstack_uuid(malformed)

    def test_repository_url_is_public_github_only(self) -> None:
        self.assertEqual(
            repository_url("https://github.com/example-org/example-app.git"),
            "https://github.com/example-org/example-app",
        )
        for invalid in (
            "http://github.com/o/r",
            "https://token@github.com/o/r",
            "https://github.com:443/o/r",
            "https://example.com/o/r",
            "https://github.com/o/r/extra",
            "https://github.com/o/r?token=x",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                repository_url(invalid)

    def test_paths_and_health_paths_are_normalized_and_contained(self) -> None:
        self.assertEqual(relative_path("frontend/package.json"), "frontend/package.json")
        self.assertEqual(health_path("/health/ready"), "/health/ready")
        for invalid in ("../outside", "/absolute", "a//b", "./a", "a\\b"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                relative_path(invalid)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "inside").write_text("ok")
            outside = root.parent / "outside-foundation-test"
            outside.write_text("no")
            try:
                (root / "link").symlink_to(outside)
                self.assertEqual(resolve_inside(root, "inside"), root / "inside")
                with self.assertRaises(ValidationError):
                    resolve_inside(root, "link")
            finally:
                outside.unlink()


class ConfigTests(unittest.TestCase):
    def test_checked_in_policy_example_loads(self) -> None:
        path = Path(__file__).resolve().parents[1] / "config/platform-policy.example.json"
        policy = load_policy(path, require_private=False)
        self.assertEqual(policy.standard.cpu_mhz, 1000)
        self.assertEqual(
            policy.runtime_images.bun,
            "docker.io/oven/bun@sha256:621f249399228db47cf34611ee662585e77e015250ed29d5d0932b2d3282f0b0",
        )
        self.assertEqual(
            policy.runtime_images.node,
            "docker.io/library/node@sha256:65932751ed4073ed02f5c04e494e4b2572a891b7dbea0568a863dc80341bf848",
        )

    def write_json(self, directory: Path, name: str, value: object, *, mode: int = 0o600) -> Path:
        path = directory / name
        path.write_text(json.dumps(value))
        path.chmod(mode)
        return path

    def test_loads_concrete_identity_profile_and_conservative_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            platform_path = self.write_json(
                directory, "platform.json", platform_document(), mode=0o644
            )
            policy_path = self.write_json(directory, "policy.json", policy_document())
            result = load(platform_path, policy_path)
            self.assertEqual(result.platform.project_name, "example-project")
            self.assertEqual(result.platform.project_id, "00000000-0000-4000-8000-000000000000")
            self.assertEqual(result.platform.get("hosts.admin"), "example-admin")
            self.assertEqual(result.policy.standard.cpu_mhz, 1000)
            self.assertEqual(result.policy.standard.memory_mib, 2048)
            self.assertEqual(result.policy.limits.helper_response_bytes, 1_048_576)
            with self.assertRaises(TypeError):
                result.platform.document["project"] = "changed"  # type: ignore[index]

    def test_policy_requires_private_file_and_exact_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            policy_path = self.write_json(directory, "policy.json", policy_document(), mode=0o644)
            with self.assertRaisesRegex(ValidationError, "permissions"):
                load_policy(policy_path)
            policy_path.chmod(0o600)
            document = policy_document()
            document["unknown"] = True
            self.write_json(directory, "policy.json", document)
            with self.assertRaisesRegex(ValidationError, "unknown keys"):
                load_policy(policy_path)

    def test_policy_rejects_classes_and_unpinned_images(self) -> None:
        cases: list[dict[str, object]] = []
        with_class = policy_document()
        with_class["standard"]["class"] = "personal"  # type: ignore[index]
        cases.append(with_class)
        no_pin = policy_document()
        no_pin["runtimeImages"]["bun"] = "registry.example/image:latest"  # type: ignore[index]
        cases.append(no_pin)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for index, document in enumerate(cases):
                path = self.write_json(directory, f"policy-{index}.json", document)
                with self.subTest(index=index), self.assertRaises(ValidationError):
                    load_policy(path)

    def test_policy_cpu_is_configurable_within_safe_bounds(self) -> None:
        document = policy_document()
        document["standard"]["cpuMHz"] = 500  # type: ignore[index]
        with tempfile.TemporaryDirectory() as temporary:
            path = self.write_json(Path(temporary), "policy.json", document)
            self.assertEqual(load_policy(path).standard.cpu_mhz, 500)

    def test_public_ingress_is_explicit_and_never_world_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            valid = platform_document()
            valid["publicIngress"] = {
                "mode": "direct",
                "providerCidrs": ["203.0.113.0/24", "198.51.100.7/32"],
            }
            path = self.write_json(directory, "direct.json", valid, mode=0o644)
            self.assertEqual(load_platform(path).get("publicIngress.mode"), "direct")

            invalid = (
                {"mode": "direct", "providerCidrs": []},
                {"mode": "direct", "providerCidrs": ["0.0.0.0/0"]},
                {"mode": "direct", "providerCidrs": ["203.0.113.7/24"]},
                {"mode": "tunnel", "providerCidrs": ["203.0.113.0/24"]},
                {"mode": "legacy", "providerCidrs": []},
            )
            for index, ingress in enumerate(invalid):
                document = platform_document()
                document["publicIngress"] = ingress
                candidate = self.write_json(
                    directory, f"invalid-ingress-{index}.json", document, mode=0o644
                )
                with self.subTest(ingress=ingress), self.assertRaises(ValidationError):
                    load_platform(candidate)

    def test_json_duplicates_and_inventory_unknown_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            duplicate = directory / "duplicate.json"
            duplicate.write_text('{"project":"one","project":"two"}')
            with self.assertRaisesRegex(ValidationError, "duplicate"):
                load_platform(duplicate)
            document = platform_document()
            document["mystery"] = 1
            path = self.write_json(directory, "platform.json", document, mode=0o644)
            with self.assertRaisesRegex(ValidationError, "unknown keys"):
                load_platform(path)


if __name__ == "__main__":
    unittest.main()
