from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from openstack_platform.config import RuntimeImages
from openstack_platform.controller.application_models import Manifest, StorageBinding
from openstack_platform.controller.application_runtime import generate_recipe
from openstack_platform.controller.deployment_config import parse_configuration, validate_checkout
from openstack_platform.helper import production
from openstack_platform.validation import ValidationError

BUN_IMAGE = f"docker.io/oven/bun@sha256:{'1' * 64}"
NODE_IMAGE = f"docker.io/library/node@sha256:{'2' * 64}"
IMAGES = RuntimeImages(bun=BUN_IMAGE, node=NODE_IMAGE)
RESOURCE_ID = "11111111-1111-4111-8111-111111111111"
INVALID_PATHS = (
    None,
    True,
    42,
    {},
    [],
    "",
    "/",
    "/app/server.js",
    "//host/path",
    "../escape",
    "output/../../escape",
    "output/../server.js",
    "./output",
    "output/./server.js",
    "output/.",
    "output/",
    "output//server.js",
    r"output\server.js",
    "C:/server.js",
    "~/server.js",
    "output/*",
    "output/?",
    "output/[ab]",
    "output/{a,b}",
    "output/$HOME",
    "output/${HOME}",
    "$(id)",
    "`id`",
    "file;id",
    'file"name',
    "file'name",
    "file#name",
    "file name",
    "file\tname",
    "file\x00name",
    "file\x1bname",
    "file\rname",
    "file\nFROM attacker/image",
    "caf\u00e9.js",
    "file\ud800name",
    "a" * 1025,
)


def configuration(runtime: str = "node") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "build": {
            "runtime": runtime,
            "packages": ["."],
            "buildScript": "build",
            "startScript": "start",
        },
        "runtime": {"port": 3000, "healthPath": "/health"},
        "storageBindings": [{"resourceId": RESOURCE_ID, "outputs": {"url": "DATABASE_URL"}}],
    }


class RuntimeFilesConfigurationTests(unittest.TestCase):
    def test_omission_retains_exact_legacy_canonical_json_and_hash(self) -> None:
        expected = (
            '{"build":{"buildScript":"build","packages":["."],"runtime":"node",'
            '"startScript":"start"},"runtime":{"healthPath":"/health","port":3000},'
            '"schemaVersion":1,"storageBindings":[{"outputs":{"url":"DATABASE_URL"},'
            '"resourceId":"11111111-1111-4111-8111-111111111111"}]}'
        )
        parsed = parse_configuration(configuration())
        self.assertIsNone(parsed.runtime_files)
        self.assertEqual(parsed.canonical_json(), expected)
        self.assertEqual(parse_configuration(expected), parsed)
        self.assertEqual(
            hashlib.sha256(parsed.canonical_json().encode()).hexdigest(),
            "b0ce64099e3eacce83d71e4aa1dea7ae137caf4e621432eb21714346bef22929",
        )
        self.assertIsNone(parsed.manifest({RESOURCE_ID: ("postgres", "primary")}).runtime_files)

    def test_optional_paths_are_canonical_and_propagate_without_changing_storage(self) -> None:
        for runtime in ("bun", "node"):
            with self.subTest(runtime=runtime):
                document = configuration(runtime)
                document["build"]["runtimeFiles"] = [
                    "package.json",
                    "packages/core/lib",
                    "node_modules",
                    ".output/server",
                ]
                parsed = parse_configuration(document)
                self.assertEqual(parsed.schema_version, 1)
                self.assertEqual(
                    parsed.runtime_files,
                    (".output/server", "node_modules", "package.json", "packages/core/lib"),
                )
                canonical = json.loads(parsed.canonical_json())
                self.assertEqual(canonical["build"]["runtimeFiles"], list(parsed.runtime_files))
                self.assertEqual(parse_configuration(parsed.canonical_json()), parsed)
                self.assertEqual(
                    replace(
                        parsed, runtime_files=tuple(reversed(parsed.runtime_files))
                    ).canonical_json(),
                    parsed.canonical_json(),
                )
                document["build"]["runtimeFiles"].reverse()
                self.assertEqual(
                    parse_configuration(document).canonical_json(), parsed.canonical_json()
                )
                manifest = parsed.manifest({RESOURCE_ID: ("postgres", "primary")})
                self.assertEqual(manifest.runtime_files, parsed.runtime_files)
                self.assertEqual(
                    manifest.storage_bindings,
                    (StorageBinding("primary", "postgres", (("url", "DATABASE_URL"),)),),
                )
                self.assertEqual(canonical["storageBindings"], document["storageBindings"])

    def test_literal_hidden_scoped_and_bounded_paths_are_supported(self) -> None:
        for paths in (
            ["."],
            [".output", "@scope/assets", "nested/a+b-1_2.json", "server..js"],
            ["a" * 1024],
            [f"file-{number}" for number in range(32)],
            ["output", "output-extra", "outputs/file"],
        ):
            with self.subTest(paths=paths):
                document = configuration()
                document["build"]["runtimeFiles"] = paths
                self.assertEqual(parse_configuration(document).runtime_files, tuple(sorted(paths)))

    def test_runtime_files_requires_a_nonempty_bounded_json_array(self) -> None:
        for value in (None, True, {}, "output", ("output",), [], [f"f{i}" for i in range(33)]):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                document = configuration()
                document["build"]["runtimeFiles"] = value
                parse_configuration(document)

    def test_unsafe_paths_are_rejected_before_recipe_or_checkout(self) -> None:
        for path in INVALID_PATHS:
            with self.subTest(path=path), self.assertRaises(ValidationError):
                document = configuration()
                document["build"]["runtimeFiles"] = [path]
                parse_configuration(document)

    def test_duplicates_and_ancestor_overlaps_are_rejected_in_either_order(self) -> None:
        for paths in (
            ["output", "output"],
            [".", "."],
            [".", "package.json"],
            ["output", "output/server.js"],
            ["a", "a-b", "a/b"],
            ["a/b/c", "a/b"],
        ):
            for selected in (paths, list(reversed(paths))):
                with (
                    self.subTest(paths=selected),
                    self.assertRaisesRegex(ValidationError, "unique|overlap"),
                ):
                    document = configuration()
                    document["build"]["runtimeFiles"] = selected
                    parse_configuration(document)

    def test_optional_field_does_not_relax_unknown_missing_or_duplicate_fields(self) -> None:
        document = configuration()
        document["build"]["runtimeFiles"] = ["."]
        document["build"]["runtimeFile"] = "anything"
        with self.assertRaisesRegex(ValidationError, "fields"):
            parse_configuration(document)
        del document["build"]["runtimeFile"]
        del document["build"]["buildScript"]
        with self.assertRaisesRegex(ValidationError, "fields"):
            parse_configuration(document)
        encoded = json.dumps(configuration()).replace(
            '"startScript": "start"',
            '"startScript": "start", "runtimeFiles": ["."], "runtimeFiles": ["output"]',
        )
        with self.assertRaisesRegex(ValidationError, "strict JSON"):
            parse_configuration(encoded)

    def test_generated_paths_need_not_exist_at_checkout_and_locks_are_still_required(self) -> None:
        for runtime, lock in (("bun", "bun.lock"), ("node", "package-lock.json")):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as directory:
                document = configuration(runtime)
                document["build"]["packages"] = [".", "apps/web", "packages/core"]
                document["build"]["runtimeFiles"] = ["apps/web/output", "package.json"]
                parsed = parse_configuration(document)
                root = Path(directory)
                for package in parsed.packages:
                    path = root / package
                    path.mkdir(parents=True, exist_ok=True)
                    (path / lock).write_text("{}")
                (root / "package.json").write_text(
                    json.dumps({"scripts": {"build": "compile", "start": "serve"}})
                )
                self.assertFalse((root / "apps/web/output").exists())
                validate_checkout(parsed, root)
                (root / "packages/core" / lock).unlink()
                with self.assertRaisesRegex(ValidationError, "lockfile"):
                    validate_checkout(parsed, root)


class SlimRuntimeRecipeTests(unittest.TestCase):
    def test_omission_retains_exact_legacy_recipe_bytes_and_fingerprints(self) -> None:
        cases = (
            (
                Manifest("bun", (".",), "build", "start", 3000, "/health"),
                f"FROM {BUN_IMAGE}\n"
                "WORKDIR /app\n"
                "COPY --chown=65532:65532 . /app\n"
                "WORKDIR /app\n"
                'RUN ["bun","install","--frozen-lockfile"]\n'
                "WORKDIR /app\n"
                'RUN ["bun","run","build"]\n'
                "ENV NODE_ENV=production\n"
                "USER 65532:65532\n"
                "EXPOSE 3000\n"
                'CMD ["bun","run","start"]\n',
                "83c4715fe18fe068a4da755c2e703a3c23bdaabc94961453ea1fbc4dad5408fb",
            ),
            (
                Manifest("node", (".",), None, "serve", 8080, "/ready"),
                f"FROM {NODE_IMAGE}\n"
                "WORKDIR /app\n"
                "COPY --chown=65532:65532 . /app\n"
                "WORKDIR /app\n"
                'RUN ["npm","ci"]\n'
                "WORKDIR /app\n"
                "ENV NODE_ENV=production\n"
                "USER 65532:65532\n"
                "EXPOSE 8080\n"
                'CMD ["npm","run","serve"]\n',
                "63815c6ae38463521cdbc0604ab831681a001107365a10a0e738d9e6cbdae078",
            ),
        )
        for manifest, dockerfile, fingerprint in cases:
            with self.subTest(runtime=manifest.runtime):
                recipe = generate_recipe(manifest, IMAGES)
                self.assertEqual(recipe.dockerfile, dockerfile.encode())
                self.assertEqual(recipe.sha256, fingerprint)

    def test_slim_monorepo_builds_as_before_and_copies_only_exact_nested_paths(self) -> None:
        files = (
            "packages/core/lib",
            "node_modules",
            "apps/web/package.json",
            "apps/web/output",
            "package.json",
            "apps/web/node_modules",
        )
        for runtime, image, tool, install in (
            ("bun", BUN_IMAGE, "bun", '["bun","install","--frozen-lockfile"]'),
            ("node", NODE_IMAGE, "npm", '["npm","ci"]'),
        ):
            with self.subTest(runtime=runtime):
                manifest = Manifest(
                    runtime,
                    (".", "packages/core", "apps/web"),
                    "compile:server",
                    "serve:production",
                    4321,
                    "/ready",
                    runtime_files=files,
                )
                recipe = generate_recipe(manifest, IMAGES)
                expected = [
                    f"FROM {image} AS build",
                    "WORKDIR /app",
                    'COPY --chown=65532:65532 [".","/app"]',
                    "WORKDIR /app",
                    f"RUN {install}",
                    "WORKDIR /app/packages/core",
                    f"RUN {install}",
                    "WORKDIR /app/apps/web",
                    f"RUN {install}",
                    "WORKDIR /app",
                    f'RUN ["{tool}","run","compile:server"]',
                    f"FROM {image}",
                    "WORKDIR /app",
                    *(
                        f'COPY --from=build --chown=65532:65532 ["/app/{path}","/app/{path}"]'
                        for path in sorted(files)
                    ),
                    "ENV NODE_ENV=production",
                    "USER 65532:65532",
                    "EXPOSE 4321",
                    f'CMD ["{tool}","run","serve:production"]',
                    "",
                ]
                self.assertEqual(recipe.dockerfile, "\n".join(expected).encode())
                self.assertEqual(recipe, generate_recipe(manifest, IMAGES))
                self.assertEqual(
                    recipe,
                    generate_recipe(
                        replace(manifest, runtime_files=tuple(reversed(files))), IMAGES
                    ),
                )

    def test_no_build_script_still_installs_then_packages_for_both_runtimes(self) -> None:
        for runtime, tool, install in (
            ("bun", "bun", 'RUN ["bun","install","--frozen-lockfile"]'),
            ("node", "npm", 'RUN ["npm","ci"]'),
        ):
            with self.subTest(runtime=runtime):
                manifest = Manifest(
                    runtime,
                    (".",),
                    None,
                    "serve",
                    8080,
                    "/ready",
                    runtime_files=("src", "package.json"),
                )
                recipe = generate_recipe(manifest, IMAGES).dockerfile.decode().splitlines()
                self.assertEqual([line for line in recipe if line.startswith("RUN ")], [install])
                self.assertEqual(recipe[-1], f'CMD ["{tool}","run","serve"]')
                self.assertEqual(recipe.count("ENV NODE_ENV=production"), 1)
                self.assertGreater(recipe.index("ENV NODE_ENV=production"), recipe.index(install))
                self.assertFalse(any("node_modules" in line for line in recipe))

    def test_dot_selects_all_of_app_not_the_build_root_and_has_distinct_identity(self) -> None:
        for runtime in ("bun", "node"):
            with self.subTest(runtime=runtime):
                legacy = Manifest(runtime, (".",), None, "start", 3000, "/health")
                recipe = generate_recipe(replace(legacy, runtime_files=(".",)), IMAGES)
                copies = [
                    line
                    for line in recipe.dockerfile.decode().splitlines()
                    if line.startswith("COPY --from=")
                ]
                self.assertEqual(copies, ['COPY --from=build --chown=65532:65532 ["/app","/app"]'])
                self.assertNotEqual(recipe.sha256, generate_recipe(legacy, IMAGES).sha256)

    def test_selection_and_existing_recipe_inputs_are_fingerprinted(self) -> None:
        manifest = Manifest("node", (".",), None, "start", 3000, "/health", runtime_files=("src",))
        recipe = generate_recipe(manifest, IMAGES)
        for changes in (
            {"runtime_files": ("output",)},
            {"runtime_files": None},
            {"build_script": "build"},
            {"start_script": "serve"},
            {"packages": (".", "packages/core")},
            {"port": 8080},
            {"health_path": "/ready"},
        ):
            with self.subTest(changes=changes):
                self.assertNotEqual(
                    recipe.sha256, generate_recipe(replace(manifest, **changes), IMAGES).sha256
                )
        self.assertNotEqual(
            recipe.sha256,
            generate_recipe(
                manifest, replace(IMAGES, node=f"docker.io/library/node@sha256:{'3' * 64}")
            ).sha256,
        )

    def test_direct_manifest_callers_cannot_bypass_path_or_list_validation(self) -> None:
        manifest = Manifest("node", (".",), None, "start", 3000, "/health")
        values = [
            *((path,) for path in INVALID_PATHS),
            (),
            "output",
            {},
            True,
            tuple(f"file-{number}" for number in range(33)),
            ("output", "output"),
            ("output", "output/server.js"),
            ("server.js", "."),
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                generate_recipe(replace(manifest, runtime_files=value), IMAGES)

    def test_positional_storage_binding_remains_the_seventh_manifest_argument(self) -> None:
        bindings = (StorageBinding("primary", "postgres", (("url", "DATABASE_URL"),)),)
        manifest = Manifest("node", (".",), None, "start", 3000, "/health", bindings)
        self.assertEqual(manifest.storage_bindings, bindings)
        self.assertIsNone(manifest.runtime_files)


class HelperRuntimeFilesTests(unittest.TestCase):
    def test_helper_and_controller_generate_identical_recipes_with_and_without_selection(
        self,
    ) -> None:
        for runtime_name in ("bun", "node"):
            for selection in (None, ["package.json", "apps/web/output"]):
                with (
                    self.subTest(runtime=runtime_name, selection=selection),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    document = configuration(runtime_name)
                    document["build"]["buildScript"] = None
                    if selection is not None:
                        document["build"]["runtimeFiles"] = selection
                    selected = parse_configuration(document)
                    expected = generate_recipe(
                        selected.manifest({RESOURCE_ID: ("postgres", "primary")}), IMAGES
                    )
                    platform = SimpleNamespace(
                        prefix="example",
                        namespace="app-platform",
                        project_name="project",
                        project_id=RESOURCE_ID,
                        get=lambda key: {
                            "paths.root": directory,
                            "addresses.storage": "192.0.2.40",
                            "flavors.builder": "builder-standard",
                        }[key],
                    )
                    runtime = SimpleNamespace(platform=platform, admin_state=Path(directory))
                    result = SimpleNamespace(
                        build_id=RESOURCE_ID,
                        image=f"registry.example/projects/demo-app/app@sha256:{'b' * 64}",
                        build_log=b"complete\n",
                        build_log_truncated=False,
                        cleanup_confirmed=True,
                    )

                    def acquire(
                        _repository: str,
                        _commit: str,
                        destination: Path,
                        *,
                        selected_runtime: str = runtime_name,
                        **_kwargs: Any,
                    ) -> None:
                        destination.mkdir()
                        (destination / "package.json").write_text(
                            json.dumps({"scripts": {"start": "serve"}})
                        )
                        lock = "bun.lock" if selected_runtime == "bun" else "package-lock.json"
                        (destination / lock).write_text("{}")

                    with (
                        mock.patch.object(production, "helper_runtime", return_value=runtime),
                        mock.patch.object(
                            production.application, "acquire_github_commit", side_effect=acquire
                        ),
                        mock.patch.object(
                            production.application,
                            "build_with_disposable_builder",
                            return_value=result,
                        ) as build,
                    ):
                        response = production._build_application(
                            {
                                "buildId": RESOURCE_ID,
                                "slug": "demo-app",
                                "repository": "https://github.com/example/demo-app",
                                "requestedRef": "main",
                                "commit": "a" * 40,
                                "configurationRevision": 1,
                                "configuration": document,
                                "builderImageId": RESOURCE_ID,
                                "runtimeImages": {"bun": BUN_IMAGE, "node": NODE_IMAGE},
                                "sourceLimit": 4096,
                                "buildLogLimit": 4096,
                                "connectSeconds": 5,
                                "deadlineAt": (
                                    datetime.now(UTC) + timedelta(minutes=10)
                                ).isoformat(),
                            }
                        )
                    self.assertEqual(build.call_args.kwargs["recipe"], expected)
                    self.assertEqual(response["recipeHash"], expected.sha256)


if __name__ == "__main__":
    unittest.main()
