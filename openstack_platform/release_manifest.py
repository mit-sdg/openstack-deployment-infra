"""Deterministic release compatibility manifests and supply-chain evidence.

Production manifests are detached-signature verified against an operator-selected
Ed25519 public key.  Unsigned evidence is accepted only when both the manifest
and the invocation explicitly identify a non-production development release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

FORMAT = "openstack-platform-release-v1"
SBOM_FORMAT = "openstack-platform-sbom-v1"
PROVENANCE_FORMAT = "openstack-platform-provenance-v1"
UNSIGNED_ACKNOWLEDGEMENT = "I_UNDERSTAND_THIS_IS_NOT_PRODUCTION"
ROLES = ("admin", "ingress", "storage", "worker", "builder")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_EVIDENCE = 16 * 1024 * 1024


class ReleaseVerificationError(RuntimeError):
    """Release evidence is absent, malformed, untrusted, or incompatible."""


def _fail(message: str) -> NoReturn:
    raise ReleaseVerificationError(message)


def _canonical(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_size > _MAX_EVIDENCE:
            _fail(f"release evidence must be a bounded direct file: {path}")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ReleaseVerificationError(f"release evidence is unavailable: {path}") from error


def _load(path: Path) -> dict[str, Any]:
    _sha256_file(path)
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ReleaseVerificationError(f"release evidence is invalid JSON: {path}") from error
    if not isinstance(value, dict):
        _fail(f"release evidence must be a JSON object: {path}")
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _tree_hash(root: Path, paths: list[Path], *, domain: str) -> str:
    digest = hashlib.sha256((domain + "\0").encode())
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


def _files(root: Path, directory: str, suffixes: tuple[str, ...]) -> list[Path]:
    return [
        path for path in (root / directory).rglob("*") if path.is_file() and path.suffix in suffixes
    ]


def component_set(repository: Path, commit: str) -> dict[str, Any]:
    """Return the canonical compatibility projection for one source commit."""
    if not _COMMIT.fullmatch(commit):
        _fail("source commit must be a full lowercase Git commit")
    contract = repository / "infra/lib/platform_contract.json"
    lockfile = repository / "uv.lock"
    actions = repository / "openstack_platform/helper/actions-v1.txt"
    required = (
        contract,
        lockfile,
        actions,
        repository / "pyproject.toml",
        repository / "flake.lock",
    )
    if any(not path.is_file() or path.is_symlink() for path in required):
        _fail("release source is missing a required compatibility input")

    python_files = _files(repository, "openstack_platform", (".py", ".txt"))
    wheel_inputs = [repository / "pyproject.toml", contract, *python_files]
    controller_files = _files(repository, "openstack_platform/controller", (".py",))
    database_text = (repository / "openstack_platform/controller/database.py").read_text()
    versions = [int(value) for value in re.findall(r"Migration\(\s*(\d+),", database_text)]
    if not versions:
        _fail("controller schema has no migration version")
    helper_actions = tuple(actions.read_text().splitlines())
    if not helper_actions or helper_actions != tuple(sorted(set(helper_actions))):
        _fail("helper action manifest is empty, unsorted, or duplicated")

    nix_inputs = [repository / "flake.nix", repository / "flake.lock", contract]
    nix_inputs.extend(_files(repository, "nix", (".nix",)))
    nix_hash = _tree_hash(repository, nix_inputs, domain="role-image-inputs-v1")
    roles = {
        role: {
            "flakeAttribute": f"{role}-image",
            "identity": _sha256_bytes(f"role-image-v1\0{commit}\0{role}\0{nix_hash}".encode()),
        }
        for role in ROLES
    }
    ui_placeholder = {"status": "not-shipped", "contractVersion": 0}
    ui_placeholder["identity"] = _sha256_bytes(_canonical(ui_placeholder))
    return {
        "sourceCommit": commit,
        "contract": {"version": 1, "sha256": _sha256_file(contract)},
        "lockfile": {"name": "uv.lock", "sha256": _sha256_file(lockfile)},
        "operatorWheel": {
            "kind": "deterministic-source-inputs",
            "sha256": _tree_hash(repository, wheel_inputs, domain="operator-wheel-inputs-v1"),
        },
        "helper": {"protocolVersion": 1, "actionsSha256": _sha256_file(actions)},
        "controller": {
            "apiVersion": 1,
            "schemaVersion": max(versions),
            "sha256": _tree_hash(repository, controller_files, domain="controller-v1"),
        },
        "ui": ui_placeholder,
        "roleImages": roles,
    }


def _packages(lockfile: Path) -> list[dict[str, str]]:
    text = lockfile.read_text(encoding="utf-8")
    packages = re.findall(
        r'(?ms)^\[\[package\]\].*?^name = "([^"]+)"\s*\nversion = "([^"]+)"', text
    )
    return [{"name": name, "version": version} for name, version in sorted(set(packages))]


def _openssl(argv: list[str], *, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(argv, input=input_bytes, capture_output=True, check=False)
    except OSError as error:
        raise ReleaseVerificationError("OpenSSL is required for release signatures") from error
    if result.returncode:
        _fail("release signature operation failed")
    return result.stdout


def _public_key_sha256(key: Path, *, private: bool) -> str:
    arguments = ["openssl", "pkey"]
    if not private:
        arguments.append("-pubin")
    arguments.extend(("-in", str(key), "-pubout", "-outform", "DER"))
    return _sha256_bytes(_openssl(arguments))


def _verify_checkout(repository: Path, commit: str) -> None:
    try:
        head = subprocess.run(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", repository, "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseVerificationError("release evidence requires a Git checkout") from error
    if head != commit or dirty:
        _fail("release evidence requires the exact clean source commit")


def generate(
    repository: Path, commit: str, output: Path, *, signing_key: Path | None, unsigned: bool
) -> Path:
    if unsigned == (signing_key is not None):
        _fail("choose exactly one of a production signing key or unsigned development mode")
    _verify_checkout(repository, commit)
    components = component_set(repository, commit)
    output.mkdir(parents=True, exist_ok=True)
    sbom = {
        "format": SBOM_FORMAT,
        "sourceCommit": commit,
        "packages": _packages(repository / "uv.lock"),
    }
    sbom_path = output / "release.sbom.json"
    sbom_path.write_bytes(_canonical(sbom))
    component_digest = _sha256_bytes(_canonical(components))
    provenance = {
        "format": PROVENANCE_FORMAT,
        "sourceCommit": commit,
        "subject": {"name": "openstack-platform-component-set", "sha256": component_digest},
        "buildType": "https://openstack-platform.invalid/release/v1",
        "materials": [
            {"name": "uv.lock", "sha256": components["lockfile"]["sha256"]},
            {"name": "platform-contract", "sha256": components["contract"]["sha256"]},
        ],
    }
    provenance_path = output / "release.provenance.json"
    provenance_path.write_bytes(_canonical(provenance))
    if unsigned:
        trust = {"mode": "development-unsigned", "warning": "NOT FOR PRODUCTION"}
    else:
        assert signing_key is not None
        trust = {
            "mode": "production-ed25519",
            "publicKeySha256": _public_key_sha256(signing_key, private=True),
        }
    manifest = {
        "format": FORMAT,
        "releaseChannel": "development-unsigned" if unsigned else "production",
        "components": components,
        "evidence": {
            "sbom": {"file": sbom_path.name, "sha256": _sha256_file(sbom_path)},
            "provenance": {"file": provenance_path.name, "sha256": _sha256_file(provenance_path)},
        },
        "trust": trust,
    }
    manifest_path = output / "release-manifest.json"
    manifest_path.write_bytes(_canonical(manifest))
    if signing_key is not None:
        signature = output / "release-manifest.sig"
        _openssl(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(signing_key),
                "-in",
                str(manifest_path),
                "-out",
                str(signature),
            ]
        )
    return manifest_path


def verify(
    repository: Path,
    manifest_path: Path,
    *,
    expected_commit: str,
    signature: Path | None,
    trust_root: Path | None,
    allow_unsigned_development: bool = False,
) -> dict[str, Any]:
    """Verify trust, evidence, and every local compatibility input."""
    manifest = _load(manifest_path)
    if manifest.get("format") != FORMAT:
        _fail("release manifest format is unsupported")
    trust = manifest.get("trust")
    channel = manifest.get("releaseChannel")
    if not isinstance(trust, dict):
        _fail("release manifest trust policy is malformed")
    if trust.get("mode") == "production-ed25519" and channel == "production":
        if signature is None or trust_root is None:
            _fail("production release requires a signature and explicit trust root")
        _sha256_file(signature)
        _sha256_file(trust_root)
        if _public_key_sha256(trust_root, private=False) != trust.get("publicKeySha256"):
            _fail("release trust root does not match the signed manifest")
        _openssl(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                str(trust_root),
                "-sigfile",
                str(signature),
                "-in",
                str(manifest_path),
            ]
        )
    elif trust.get("mode") == "development-unsigned" and channel == "development-unsigned":
        if not allow_unsigned_development or os.environ.get("PLATFORM_ENVIRONMENT") == "production":
            _fail("unsigned development release requires explicit non-production acknowledgement")
        if signature is not None or trust_root is not None:
            _fail("unsigned development evidence must not present production trust material")
    else:
        _fail("release trust mode and channel are inconsistent")

    components = manifest.get("components")
    if components != component_set(repository, expected_commit):
        _fail(
            "release component set does not match source, contract, lockfile, schema, API, UI, helper, or role images"
        )
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"sbom", "provenance"}:
        _fail("release manifest evidence set is incomplete")
    loaded_evidence: dict[str, dict[str, Any]] = {}
    for name in ("sbom", "provenance"):
        record = evidence[name]
        if (
            not isinstance(record, dict)
            or set(record) != {"file", "sha256"}
            or not _SHA256.fullmatch(str(record["sha256"]))
        ):
            _fail(f"release {name} evidence record is malformed")
        path = manifest_path.parent / str(record["file"])
        if path.parent != manifest_path.parent or _sha256_file(path) != record["sha256"]:
            _fail(f"release {name} evidence hash does not match")
        loaded_evidence[name] = _load(path)
    if (
        loaded_evidence["sbom"].get("format") != SBOM_FORMAT
        or loaded_evidence["sbom"].get("sourceCommit") != expected_commit
    ):
        _fail("release SBOM is incompatible")
    provenance = loaded_evidence["provenance"]
    expected_subject = _sha256_bytes(_canonical(components))
    if (
        provenance.get("format") != PROVENANCE_FORMAT
        or provenance.get("sourceCommit") != expected_commit
        or provenance.get("subject")
        != {"name": "openstack-platform-component-set", "sha256": expected_subject}
    ):
        _fail("release provenance is incompatible")
    return manifest


def verify_from_environment(
    repository: Path, commit: str, values: dict[str, str]
) -> dict[str, Any]:
    manifest = values.get("PLATFORM_RELEASE_MANIFEST")
    if not manifest:
        _fail("PLATFORM_RELEASE_MANIFEST is required before setup mutation")
    acknowledgement = values.get("PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT")
    if acknowledgement and acknowledgement != UNSIGNED_ACKNOWLEDGEMENT:
        _fail("unsigned development acknowledgement is invalid")
    return verify(
        repository,
        Path(manifest),
        expected_commit=commit,
        signature=Path(values["PLATFORM_RELEASE_SIGNATURE"])
        if values.get("PLATFORM_RELEASE_SIGNATURE")
        else None,
        trust_root=Path(values["PLATFORM_RELEASE_TRUST_ROOT"])
        if values.get("PLATFORM_RELEASE_TRUST_ROOT")
        else None,
        allow_unsigned_development=acknowledgement == UNSIGNED_ACKNOWLEDGEMENT,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify platform release evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("generate")
    create.add_argument("--repository", type=Path, default=Path.cwd())
    create.add_argument("--commit", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--signing-key", type=Path)
    create.add_argument("--unsigned-development", action="store_true")
    check = commands.add_parser("verify")
    check.add_argument("--repository", type=Path, default=Path.cwd())
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--commit", required=True)
    check.add_argument("--signature", type=Path)
    check.add_argument("--trust-root", type=Path)
    check.add_argument("--allow-unsigned-development", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "generate":
        path = generate(
            args.repository,
            args.commit,
            args.output,
            signing_key=args.signing_key,
            unsigned=args.unsigned_development,
        )
        print(f"release-manifest={path}")
    else:
        verify(
            args.repository,
            args.manifest,
            expected_commit=args.commit,
            signature=args.signature,
            trust_root=args.trust_root,
            allow_unsigned_development=args.allow_unsigned_development,
        )
        print("release-manifest=verified")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseVerificationError as error:
        print(f"release verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
