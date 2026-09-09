"""Build once, retain evidence, and publish the retained production image bytes.

GitHub scheduling and artifact transport are deliberately separate from these
commands. No signing key is accepted here: signed evidence is supplied by the
external signer, or the explicit emergency production acknowledgement is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

from . import release_manifest as release
from .config import PlatformConfig, _plain, load_platform
from .openstack import publisher_metadata

GATES = ("static", "generated-recipes", "nix-eval", "package-tests", "role-vm-tests")
IMAGE_INPUTS = (
    "flake.nix",
    "flake.lock",
    "config/platform.example.json",
    "nix",
    "infra",
    "openstack_platform",
    "deploy",
    "pyproject.toml",
    "uv.lock",
    "LICENSE",
    ":(exclude)*.md",
)


def publication_context(values: dict[str, str]) -> dict[str, str]:
    """Require an exact main run, never a PR or development publication."""
    context = {
        "commit": values.get("GITHUB_SHA", ""),
        "repository": values.get("GITHUB_REPOSITORY", ""),
        "runId": values.get("GITHUB_RUN_ID", ""),
        "runAttempt": values.get("GITHUB_RUN_ATTEMPT", ""),
        "event": values.get("GITHUB_EVENT_NAME", ""),
        "ref": values.get("GITHUB_REF", ""),
    }
    if (
        not re.fullmatch(r"[0-9a-f]{40}", context["commit"])
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", context["repository"])
        or any(not re.fullmatch(r"[1-9][0-9]*", context[key]) for key in ("runId", "runAttempt"))
        or context["event"] not in ("push", "workflow_dispatch")
        or context["ref"] != "refs/heads/main"
    ):
        raise release.ReleaseVerificationError("production images require an exact main CI run")
    return context


def relevant_push(repository: Path, before: str, commit: str) -> bool:
    """Detect image changes; fail closed if the comparison cannot be established."""
    if not re.fullmatch(r"[0-9a-f]{40}", before) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise release.ReleaseVerificationError("invalid push commit")
    if before == "0" * 40:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", commit, "--", *IMAGE_INPUTS[:-1]],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )
        return any(not name.endswith(".md") for name in result.stdout.splitlines())
    result = subprocess.run(
        ["git", "diff", "--quiet", before, commit, "--", *IMAGE_INPUTS],
        cwd=repository,
        check=False,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise release.ReleaseVerificationError("previous push commit is unavailable")
    return result.returncode == 1


def _inventory_sha256(platform: PlatformConfig) -> str:
    return release._sha256_bytes(release._canonical(_plain(platform.document)))


def _image_names(images: dict[str, str], commit: str) -> dict[str, str]:
    return {
        role: f"{re.sub(r'-[0-9a-f]{8}$', '', name)}-{commit[:8]}" for role, name in images.items()
    }


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _run(arguments: list[str], repository: Path, values: dict[str, str], log: Path) -> None:
    with log.open("wb") as stream:
        stream.write(("command=" + json.dumps(arguments) + "\n").encode())
        stream.flush()
        subprocess.run(
            arguments,
            cwd=repository,
            env=values,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
        )


def build_role(
    repository: Path, root: Path, platform: Path, role: str, values: dict[str, str]
) -> None:
    context = publication_context(values)
    if role not in release.ROLES:
        raise release.ReleaseVerificationError("unsupported image role")
    release._verify_checkout(repository, context["commit"])
    inventory = load_platform(platform)
    directory = root / role
    directory.mkdir(parents=True, exist_ok=False)
    child = {
        **values,
        "PLATFORM_CONFIG": str(platform.resolve()),
        "PLATFORM_QEMU_SERIAL_LOG": str(directory / "qemu-serial.log"),
    }
    link = directory / "result"
    _run(
        [
            "nix",
            "build",
            "--impure",
            "--print-build-logs",
            "--out-link",
            str(link),
            f".#{role}-image",
        ],
        repository,
        child,
        directory / "build.log",
    )
    output = link.resolve(strict=True)
    candidates = list(output.rglob("*.qcow2"))
    if len(candidates) != 1:
        raise release.ReleaseVerificationError("build must produce exactly one QCOW2")
    qcow = directory / f"{role}.qcow2"
    subprocess.run(
        ["cp", "--reflink=auto", "--sparse=always", "--", str(candidates[0]), str(qcow)], check=True
    )
    link.unlink()
    with (directory / "path-info.json").open("wb") as stream:
        subprocess.run(
            ["nix", "path-info", "--json", "--recursive", str(output)],
            cwd=repository,
            env=child,
            stdout=stream,
            check=True,
        )
    digest = release._artifact_sha256(qcow)
    _run(
        [str(repository / "tests/smoke_openstack_image.sh"), role, str(qcow)],
        repository,
        child,
        directory / "qemu.log",
    )
    if release._artifact_sha256(qcow) != digest:
        raise release.ReleaseVerificationError("QEMU changed the retained image")
    record = {
        "role": role,
        "context": context,
        "inventorySha256": _inventory_sha256(inventory),
        "outputStorePath": str(output),
        "publicationMetadata": dict(publisher_metadata(inventory, role, context["commit"])),
        "qemu": "passed",
        "sha256": {
            name: release._artifact_sha256(directory / name)
            for name in (
                f"{role}.qcow2",
                "path-info.json",
                "build.log",
                "qemu.log",
                "qemu-serial.log",
            )
        },
    }
    _json(directory / "record.json", record)


def write_inventory(repository: Path, output: Path, values: dict[str, str]) -> None:
    """Keep the validated, versioned build inventory outside retained artifacts."""
    context = publication_context(values)
    # Publication can be paused while retaining a real, promotable production
    # build. Do not silently build example inventory when protected inputs exist.
    configured = values.get("PLATFORM_CONFIG_JSON") or values.get("OS_PROJECT_ID")
    if configured or values.get("OPENSTACK_PUBLISH_ENABLED") == "true":
        if not values.get("PLATFORM_CONFIG_JSON") or not values.get("OS_PROJECT_ID"):
            raise release.ReleaseVerificationError(
                "production build inventory inputs are incomplete"
            )
        document = json.loads(values["PLATFORM_CONFIG_JSON"])
        document["projectId"] = str(UUID(values["OS_PROJECT_ID"]))
    else:
        document = release._load(repository / "config/platform.example.json")
    # These names are embedded in the immutable guest inventory and used by
    # hosted selection seeding. Signing policy must never change them.
    document["images"] = _image_names(document["images"], context["commit"])
    with open(output, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(document, stream)
    load_platform(output)


def signing_inputs(
    repository: Path,
    root: Path,
    platform: Path,
    *,
    github_repository: str,
    run_id: str,
    output: Path,
) -> None:
    """Export relocated direct inputs only from a reviewed main build run.

    This offline signer command reads GitHub metadata; it never invents a
    GITHUB_* environment or builds images. The private signing key is not an input.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repository) or not re.fullmatch(
        r"[1-9][0-9]*", run_id
    ):
        raise release.ReleaseVerificationError("invalid signing source repository or run")
    prefix = f"repos/{github_repository}/actions/runs/{run_id}"

    def github(endpoint: str) -> dict[str, Any]:
        result = subprocess.run(["gh", "api", endpoint], check=True, capture_output=True, text=True)
        document = json.loads(result.stdout)
        if not isinstance(document, dict):
            raise release.ReleaseVerificationError("invalid GitHub run metadata")
        return document

    run = github(prefix)
    context = release._load(root / "admin/record.json")["context"]
    if (
        run.get("id") != int(run_id)
        or run.get("head_sha") != context["commit"]
        or run.get("head_branch") != "main"
        or run.get("event") not in ("push", "workflow_dispatch")
        or run.get("head_repository", {}).get("full_name") != github_repository
        or run.get("path") != ".github/workflows/ci.yml"
        or context["repository"] != github_repository
        or context["runId"] != run_id
        or context["ref"] != "refs/heads/main"
        or context["event"] != run["event"]
        or not re.fullmatch(r"[1-9][0-9]*", context["runAttempt"])
        or int(context["runAttempt"]) > run["run_attempt"]
    ):
        raise release.ReleaseVerificationError("signing source is not the exact main CI build")
    jobs = github(f"{prefix}/jobs?filter=latest&per_page=100")
    required = {
        "Static checks",
        "Build, start, and health-check generated Bun and Node recipes",
        "Nix evaluation and formatting",
        "Test packaged binaries",
        *(f"Test {role} VM" for role in release.ROLES),
        *(f"Build production {role} image" for role in release.ROLES),
    }
    rows = jobs.get("jobs", [])
    if jobs.get("total_count") != len(rows) or any(
        len(matches := [job for job in rows if job.get("name") == name]) != 1
        or matches[0].get("conclusion") != "success"
        or matches[0].get("head_sha") != context["commit"]
        for name in required
    ):
        raise release.ReleaseVerificationError(
            "source run CI and all five image builds must succeed"
        )
    release._verify_checkout(repository, context["commit"])
    inventory = load_platform(platform)
    inventory = replace(
        inventory,
        document={
            **inventory.document,
            "projectId": inventory.project_id,
            "images": _image_names(inventory.get("images"), context["commit"]),
        },
    )
    inputs = _inputs(root, inventory, context)
    for role, value in inputs.items():
        projection, _ = release._closure_projection(Path(value["pathInfo"]))
        if Path(value["outputStorePath"]).name not in {item["storePath"] for item in projection}:
            raise release.ReleaseVerificationError(
                f"signing input output is absent from closure: {role}"
            )
    if output.exists() or output.with_suffix(".context.json").exists():
        raise release.ReleaseVerificationError("signing inputs destination already exists")
    _json(output, inputs)
    _json(
        output.with_suffix(".context.json"),
        {**context, "inventorySha256": _inventory_sha256(inventory)},
    )


def _retained_context(root: Path, values: dict[str, str]) -> dict[str, str]:
    current = publication_context(values)
    context = release._load(root / "admin/record.json").get("context")
    if (
        not isinstance(context, dict)
        or set(context) != set(current)
        or any(context[key] != value for key, value in current.items() if key != "runAttempt")
        or not isinstance(context["runAttempt"], str)
        or not re.fullmatch(r"[1-9][0-9]*", context["runAttempt"])
        or int(context["runAttempt"]) > int(current["runAttempt"])
    ):
        raise release.ReleaseVerificationError("retained images do not belong to this main CI run")
    # A retried publication may reuse an earlier complete build from this run.
    # _inputs still requires all five role records to have this exact context.
    return context


def _inputs(root: Path, platform: Path | PlatformConfig, context: dict[str, str]) -> dict[str, Any]:
    inputs = {}
    inventory = load_platform(platform) if isinstance(platform, Path) else platform
    for role in release.ROLES:
        directory = root / role
        record = release._load(directory / "record.json")
        names = {f"{role}.qcow2", "path-info.json", "build.log", "qemu.log", "qemu-serial.log"}
        metadata = dict(publisher_metadata(inventory, role, context["commit"]))
        if (
            set(record)
            != {
                "role",
                "context",
                "inventorySha256",
                "outputStorePath",
                "publicationMetadata",
                "qemu",
                "sha256",
            }
            or record["role"] != role
            or record["context"] != context
            or record["inventorySha256"] != _inventory_sha256(inventory)
            or record["publicationMetadata"] != metadata
            or record["qemu"] != "passed"
            or not isinstance(record["sha256"], dict)
            or set(record["sha256"]) != names
            or not re.fullmatch(
                r"/nix/store/[a-z0-9]{32}-[^/]{1,160}", str(record["outputStorePath"])
            )
            or {path.name for path in directory.iterdir()} != names | {"record.json"}
        ):
            raise release.ReleaseVerificationError(f"incomplete or mixed-run role evidence: {role}")
        for name in names:
            if release._artifact_sha256(directory / name) != record["sha256"][name]:
                raise release.ReleaseVerificationError(
                    f"retained role evidence changed: {role}/{name}"
                )
        if (
            f"qcow-config-drive-smoke=passed role={role} accelerator="
            not in (directory / "qemu.log").read_text()
        ):
            raise release.ReleaseVerificationError(f"missing QEMU success evidence: {role}")
        inputs[role] = {
            "qcow2": str(directory / f"{role}.qcow2"),
            "pathInfo": str(directory / "path-info.json"),
            "outputStorePath": record["outputStorePath"],
            "publicationMetadata": metadata,
        }
    return inputs


def _trust(root: Path, values: dict[str, str]) -> dict[str, str]:
    development, production = release.unsigned_environment(values)
    if development:
        raise release.ReleaseVerificationError("development mode cannot publish production images")
    child = dict(values)
    evidence = root / "evidence"
    child["PLATFORM_RELEASE_MANIFEST"] = str(evidence / "release-manifest.json")
    child["PLATFORM_ARTIFACT_MANIFEST"] = str(evidence / "artifacts/role-artifacts.json")
    if production:
        if any(
            values.get(key)
            for key in (
                "PLATFORM_RELEASE_SIGNATURE",
                "PLATFORM_RELEASE_TRUST_ROOT",
                "PLATFORM_ARTIFACT_SIGNATURE",
                "PLATFORM_ARTIFACT_TRUST_ROOT",
            )
        ) or any(evidence.rglob("*.sig")):
            raise release.ReleaseVerificationError(
                "unsigned production must not include signing material"
            )
    else:
        if not values.get("PLATFORM_RELEASE_TRUST_ROOT") or not values.get(
            "PLATFORM_ARTIFACT_TRUST_ROOT"
        ):
            raise release.ReleaseVerificationError(
                "signed production requires external trust roots"
            )
        child["PLATFORM_RELEASE_SIGNATURE"] = str(evidence / "release-manifest.sig")
        child["PLATFORM_ARTIFACT_SIGNATURE"] = str(evidence / "artifacts/role-artifacts.sig")
    return child


def _verify(
    repository: Path, root: Path, platform: Path, values: dict[str, str]
) -> tuple[dict[str, str], dict[str, Any]]:
    context = _retained_context(root, values)
    release._verify_checkout(repository, context["commit"])
    inputs = _inputs(root, platform, context)
    child = _trust(root, values)
    release.verify_from_environment(repository, context["commit"], child)
    artifact = release.verify_artifact_from_environment(
        Path(child["PLATFORM_RELEASE_MANIFEST"]), child
    )
    provenance = release._load(root / "evidence/artifacts/role-artifacts.provenance.json")
    if artifact["releaseChannel"] != "production" or provenance.get("predicate", {}).get(
        "runDetails", {}
    ).get("metadata", {}).get("buildContext") != {
        **context,
        "inventorySha256": _inventory_sha256(load_platform(platform)),
    }:
        raise release.ReleaseVerificationError(
            "production evidence must bind this exact CI build context"
        )
    for role, record in inputs.items():
        release.verify_role_artifact(
            artifact,
            role,
            qcow2=Path(record["qcow2"]),
            path_info=Path(record["pathInfo"]),
            output_store_path=Path(record["outputStorePath"]),
            publication_metadata=record["publicationMetadata"],
        )
    return child, artifact


def prepare(
    repository: Path, root: Path, platform: Path, gates: Path, values: dict[str, str]
) -> None:
    """Seal all-role evidence only after every existing CI gate succeeds."""
    context = _retained_context(root, values)
    release._verify_checkout(repository, context["commit"])
    results = release._load(gates)
    if results != {gate: "success" for gate in GATES}:
        raise release.ReleaseVerificationError("all production CI gates must succeed")
    if (root / "publication.json").exists():
        raise release.ReleaseVerificationError("publication receipt already exists")
    inputs = _inputs(root, platform, context)
    child = _trust(root, values)
    _, production = release.unsigned_environment(child)
    if production:
        evidence = root / "evidence"
        if evidence.exists():
            raise release.ReleaseVerificationError("unsigned evidence destination already exists")
        component = release.generate(
            repository,
            context["commit"],
            evidence,
            signing_key=None,
            unsigned=False,
            unsigned_production=True,
        )
        inputs_path = root / "artifact-inputs.json"
        _json(inputs_path, inputs)
        try:
            release.generate_artifact_manifest(
                component,
                inputs_path,
                evidence / "artifacts",
                signing_key=None,
                unsigned=False,
                unsigned_production=True,
                build_context={
                    **context,
                    "inventorySha256": _inventory_sha256(load_platform(platform)),
                },
            )
        finally:
            inputs_path.unlink()
    _verify(repository, root, platform, child)
    _json(
        root / "publication.json",
        {
            "format": "openstack-platform-image-publication-v1",
            "context": context,
            "ci": results,
            "files": _inventory(root),
        },
    )


def _inventory(root: Path) -> dict[str, str]:
    expected = {
        f"{role}/{name}"
        for role in release.ROLES
        for name in (
            f"{role}.qcow2",
            "path-info.json",
            "build.log",
            "qemu.log",
            "qemu-serial.log",
            "record.json",
        )
    }
    component = release._load(root / "evidence/release-manifest.json")
    signed = component.get("trust", {}).get("mode") == "production-ed25519"
    expected.update(
        f"evidence/{name}" for name in release._BUNDLE_FILES if signed or not name.endswith(".sig")
    )
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise release.ReleaseVerificationError("retained artifacts must not contain symlinks")
        if not path.is_dir() and path != root / "publication.json":
            files[path.relative_to(root).as_posix()] = release._artifact_sha256(path)
    if set(files) != expected:
        raise release.ReleaseVerificationError(
            "retained artifact inventory contains missing or unexpected files"
        )
    return files


def publish(repository: Path, root: Path, platform: Path, values: dict[str, str]) -> None:
    """Verify every retained role before the first provider call; never rebuild."""
    receipt = release._load(root / "publication.json")
    if (
        set(receipt) != {"format", "context", "ci", "files"}
        or receipt["format"] != "openstack-platform-image-publication-v1"
        or receipt["context"] != _retained_context(root, values)
        or receipt["ci"] != {gate: "success" for gate in GATES}
        or receipt["files"] != _inventory(root)
    ):
        raise release.ReleaseVerificationError(
            "retained publication receipt does not match this CI run"
        )
    child, artifact = _verify(repository, root, platform, values)
    child.update(
        {
            "PLATFORM_CONFIG": str(platform),
            "SOURCE_COMMIT": receipt["context"]["commit"],
            "OS_PROJECT_NAME": load_platform(platform).project_name,
            "PLATFORM_ARTIFACT_MANIFEST_SHA256": release._sha256_file(
                Path(child["PLATFORM_ARTIFACT_MANIFEST"])
            ),
        }
    )
    for role in release.ROLES:
        record = artifact["roleArtifacts"][role]
        child.update(
            {
                "PLATFORM_ARTIFACT_QCOW2_SHA256": record["qcow2Sha256"],
                "PLATFORM_ARTIFACT_NIX_CLOSURE_SHA256": record["nixClosureSha256"],
                "PLATFORM_ARTIFACT_NIX_OUTPUT": record["nixOutput"],
            }
        )
        subprocess.run(
            [
                str(repository / "infra/openstack/publish_nixos_image.sh"),
                role,
                str(root / role / f"{role}.qcow2"),
                f"/nix/store/{record['nixOutput']}",
                str(root / role / "path-info.json"),
            ],
            cwd=repository,
            env=child,
            check=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path)
    parser.add_argument("--platform", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("context").add_argument("--before", default="")
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--output", type=Path, required=True)
    signing = commands.add_parser("signing-inputs")
    signing.add_argument("--github-repository", required=True)
    signing.add_argument("--run-id", required=True)
    signing.add_argument("--output", type=Path, required=True)
    commands.add_parser("build-role").add_argument("role", choices=release.ROLES)
    commands.add_parser("prepare").add_argument("--gates", type=Path, required=True)
    commands.add_parser("publish")
    args = parser.parse_args(argv)
    values = dict(os.environ)
    if args.command == "context":
        context = publication_context(values)
        relevant = context["event"] == "workflow_dispatch" or relevant_push(
            args.repository, args.before, context["commit"]
        )
        print(f"publish={str(relevant).lower()}\ncommit_sha={context['commit']}")
        return 0
    if args.command == "inventory":
        write_inventory(args.repository, args.output, values)
        return 0
    if args.root is None or args.platform is None:
        parser.error("--root and --platform are required for image commands")
    repository, root, platform = (
        path.resolve() for path in (args.repository, args.root, args.platform)
    )
    if args.command == "signing-inputs":
        signing_inputs(
            repository,
            root,
            platform,
            github_repository=args.github_repository,
            run_id=args.run_id,
            output=args.output,
        )
    elif args.command == "build-role":
        build_role(repository, root, platform, args.role, values)
    elif args.command == "prepare":
        prepare(repository, root, platform, args.gates, values)
    else:
        publish(repository, root, platform, values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
