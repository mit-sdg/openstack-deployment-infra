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
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

FORMAT = "openstack-platform-release-v1"
ARTIFACT_FORMAT = "openstack-platform-role-artifacts-v1"
SBOM_FORMAT = "SPDX-2.3"
PROVENANCE_FORMAT = "https://in-toto.io/Statement/v1"
UNSIGNED_ACKNOWLEDGEMENT = "I_UNDERSTAND_THIS_IS_NOT_PRODUCTION"
ROLES = ("admin", "ingress", "storage", "worker", "builder")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_EVIDENCE = 16 * 1024 * 1024
_MAX_BUNDLE_FILE = 32 * 1024 * 1024
_MAX_BUNDLE = 128 * 1024 * 1024
_BUNDLE_FILES = (
    "release-manifest.json",
    "release-manifest.sig",
    "release.sbom.json",
    "release.provenance.json",
    "artifacts/role-artifacts.json",
    "artifacts/role-artifacts.sig",
    "artifacts/role-artifacts.sbom.spdx.json",
    "artifacts/role-artifacts.provenance.json",
)
_MAX_CLOSURE_ENTRIES = 10_000
_MAX_REFERENCES = 2_048


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


def _spdx_package(name: str, version: str, *, reference: str) -> dict[str, Any]:
    identifier = _sha256_bytes(reference.encode())[:20]
    return {
        "SPDXID": f"SPDXRef-Package-{identifier}",
        "name": name,
        "versionInfo": version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "copyrightText": "NOASSERTION",
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": reference,
            }
        ],
    }


def _spdx_document(commit: str, packages: list[dict[str, Any]], *, name: str) -> dict[str, Any]:
    packages = sorted(packages, key=lambda item: str(item["SPDXID"]))
    return {
        "spdxVersion": SBOM_FORMAT,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": name,
        "documentNamespace": f"https://openstack-platform.invalid/spdx/{commit}/{name}",
        "creationInfo": {
            "created": "1970-01-01T00:00:00Z",
            "creators": ["Tool: openstack-platform-release-manifest-v1"],
        },
        "packages": packages,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": item["SPDXID"],
            }
            for item in packages
        ],
    }


def _python_spdx_packages(lockfile: Path) -> list[dict[str, Any]]:
    return [
        _spdx_package(name, version, reference=f"pkg:pypi/{name}@{version}")
        for item in _packages(lockfile)
        for name, version in ((item["name"], item["version"]),)
    ]


def _closure_projection(path: Path) -> tuple[list[dict[str, Any]], str]:
    document = _load(path)
    raw_entries: list[tuple[str, Any]]
    if all(isinstance(key, str) and key.startswith("/nix/store/") for key in document):
        raw_entries = list(document.items())
    else:
        _fail("Nix path-info evidence must be an object keyed by store path")
    if not raw_entries or len(raw_entries) > _MAX_CLOSURE_ENTRIES:
        _fail("Nix closure evidence has an invalid entry count")
    projection: list[dict[str, Any]] = []
    for store_path, raw in sorted(raw_entries):
        if not isinstance(raw, dict):
            _fail("Nix closure entry is malformed")
        nar_hash = raw.get("narHash")
        nar_size = raw.get("narSize")
        references = raw.get("references", [])
        if (
            not isinstance(nar_hash, str)
            or not re.fullmatch(r"sha256-[A-Za-z0-9+/=]{43,48}", nar_hash)
            or isinstance(nar_size, bool)
            or not isinstance(nar_size, int)
            or nar_size < 0
            or not isinstance(references, list)
            or len(references) > _MAX_REFERENCES
            or any(
                not isinstance(item, str) or not item.startswith("/nix/store/")
                for item in references
            )
        ):
            _fail("Nix closure entry identity is malformed")
        projection.append(
            {
                "storePath": Path(store_path).name,
                "narHash": nar_hash,
                "narSize": nar_size,
                "references": sorted(Path(item).name for item in references),
            }
        )
    return projection, _sha256_bytes(_canonical(projection))


def _nix_spdx_packages(closures: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    unique = {entry["storePath"]: entry for role in sorted(closures) for entry in closures[role]}
    return [
        _spdx_package(
            str(entry["storePath"]).split("-", 1)[-1],
            "nix-store",
            reference=(
                f"pkg:generic/{quote(str(entry['storePath']), safe='')}"
                f"?narHash={quote(str(entry['narHash']), safe='')}"
            ),
        )
        for entry in unique.values()
    ]


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
    sbom = _spdx_document(
        commit,
        _python_spdx_packages(repository / "uv.lock"),
        name="openstack-platform-python",
    )
    sbom_path = output / "release.sbom.json"
    sbom_path.write_bytes(_canonical(sbom))
    component_digest = _sha256_bytes(_canonical(components))
    provenance = {
        "_type": PROVENANCE_FORMAT,
        "subject": [
            {
                "name": "openstack-platform-component-set",
                "digest": {"sha256": component_digest},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://openstack-platform.invalid/release/v1",
                "externalParameters": {"sourceCommit": commit},
                "resolvedDependencies": [
                    {"uri": "file:uv.lock", "digest": {"sha256": components["lockfile"]["sha256"]}},
                    {
                        "uri": "file:platform_contract.json",
                        "digest": {"sha256": components["contract"]["sha256"]},
                    },
                ],
            },
            "runDetails": {"builder": {"id": "openstack-platform-release-manifest-v1"}},
        },
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
    sbom = loaded_evidence["sbom"]
    if (
        sbom.get("spdxVersion") != SBOM_FORMAT
        or sbom.get("documentNamespace")
        != f"https://openstack-platform.invalid/spdx/{expected_commit}/openstack-platform-python"
        or not isinstance(sbom.get("packages"), list)
    ):
        _fail("release SBOM is incompatible")
    provenance = loaded_evidence["provenance"]
    expected_subject = _sha256_bytes(_canonical(components))
    if (
        provenance.get("_type") != PROVENANCE_FORMAT
        or provenance.get("subject")
        != [{"name": "openstack-platform-component-set", "digest": {"sha256": expected_subject}}]
        or provenance.get("predicate", {})
        .get("buildDefinition", {})
        .get("externalParameters", {})
        .get("sourceCommit")
        != expected_commit
    ):
        _fail("release provenance is incompatible")
    return manifest


def _artifact_sha256(path: Path) -> str:
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_size < 1:
            _fail(f"role artifact must be a direct non-empty file: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        raise ReleaseVerificationError(f"role artifact is unavailable: {path}") from error


def _artifact_input(path: Path) -> dict[str, dict[str, Any]]:
    document = _load(path)
    if set(document) != set(ROLES):
        _fail("artifact input must contain exactly all role images")
    result: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        value = document[role]
        if not isinstance(value, dict) or set(value) != {
            "qcow2",
            "pathInfo",
            "outputStorePath",
            "publicationMetadata",
        }:
            _fail(f"artifact input for {role} is malformed")
        metadata = value["publicationMetadata"]
        if (
            not isinstance(metadata, dict)
            or not metadata
            or len(metadata) > 64
            or any(
                not isinstance(key, str)
                or not isinstance(item, str)
                or not key
                or len(key) > 128
                or len(item) > 512
                for key, item in metadata.items()
            )
        ):
            _fail(f"publication metadata for {role} is malformed")
        result[role] = value
    return result


def generate_artifact_manifest(
    component_manifest: Path,
    inputs_path: Path,
    output: Path,
    *,
    signing_key: Path | None,
    unsigned: bool,
) -> Path:
    """Generate signed post-build identities for all five concrete role artifacts."""
    if unsigned == (signing_key is not None):
        _fail("choose exactly one of a production signing key or unsigned development mode")
    component = _load(component_manifest)
    if component.get("format") != FORMAT:
        _fail("source component manifest format is unsupported")
    components = component.get("components")
    if not isinstance(components, dict) or not _COMMIT.fullmatch(
        str(components.get("sourceCommit"))
    ):
        _fail("source component manifest has no canonical commit")
    commit = str(components["sourceCommit"])
    inputs = _artifact_input(inputs_path)
    records: dict[str, dict[str, Any]] = {}
    closures: dict[str, list[dict[str, Any]]] = {}
    for role in ROLES:
        value = inputs[role]
        projection, closure_sha256 = _closure_projection(Path(value["pathInfo"]))
        output_identity = Path(str(value["outputStorePath"])).name
        if output_identity not in {item["storePath"] for item in projection}:
            _fail(f"Nix output identity for {role} is absent from its closure")
        closures[role] = projection
        records[role] = {
            "qcow2Sha256": _artifact_sha256(Path(value["qcow2"])),
            "nixOutput": output_identity,
            "nixClosureSha256": closure_sha256,
            "publicationMetadata": dict(sorted(value["publicationMetadata"].items())),
        }
    output.mkdir(parents=True, exist_ok=True)
    try:
        source_sbom_record = component["evidence"]["sbom"]
        source_sbom_path = component_manifest.parent / source_sbom_record["file"]
        if _sha256_file(source_sbom_path) != source_sbom_record["sha256"]:
            _fail("source component SBOM hash does not match")
        source_sbom = _load(source_sbom_path)
        python_packages = source_sbom["packages"]
    except (KeyError, TypeError) as error:
        raise ReleaseVerificationError("source component SBOM is malformed") from error
    if not isinstance(python_packages, list) or any(
        not isinstance(item, dict) for item in python_packages
    ):
        _fail("source component SBOM packages are malformed")
    sbom = _spdx_document(
        commit,
        [*python_packages, *_nix_spdx_packages(closures)],
        name="openstack-platform-python-nix",
    )
    sbom_path = output / "role-artifacts.sbom.spdx.json"
    sbom_path.write_bytes(_canonical(sbom))
    subjects = [
        {"name": f"{role}.qcow2", "digest": {"sha256": records[role]["qcow2Sha256"]}}
        for role in ROLES
    ]
    subjects.extend(
        {
            "name": f"{role}.nix-closure",
            "digest": {"sha256": records[role]["nixClosureSha256"]},
        }
        for role in ROLES
    )
    provenance = {
        "_type": PROVENANCE_FORMAT,
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://openstack-platform.invalid/nix-role-images/v1",
                "externalParameters": {"sourceCommit": commit},
                "resolvedDependencies": [
                    {
                        "uri": "file:release-manifest.json",
                        "digest": {"sha256": _sha256_file(component_manifest)},
                    }
                ],
            },
            "runDetails": {"builder": {"id": "nix-openstack-role-image-v1"}},
        },
    }
    provenance_path = output / "role-artifacts.provenance.json"
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
        "format": ARTIFACT_FORMAT,
        "releaseChannel": "development-unsigned" if unsigned else "production",
        "sourceComponentManifest": {
            "sha256": _sha256_file(component_manifest),
            "componentSetSha256": _sha256_bytes(_canonical(components)),
        },
        "sourceCommit": commit,
        "roleArtifacts": records,
        "evidence": {
            "sbom": {"file": sbom_path.name, "sha256": _sha256_file(sbom_path)},
            "provenance": {"file": provenance_path.name, "sha256": _sha256_file(provenance_path)},
        },
        "trust": trust,
    }
    manifest_path = output / "role-artifacts.json"
    manifest_path.write_bytes(_canonical(manifest))
    if signing_key is not None:
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
                str(output / "role-artifacts.sig"),
            ]
        )
    return manifest_path


def _verify_artifact_trust(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    signature: Path | None,
    trust_root: Path | None,
    allow_unsigned_development: bool,
) -> None:
    trust = manifest.get("trust")
    channel = manifest.get("releaseChannel")
    if not isinstance(trust, dict):
        _fail("artifact manifest trust policy is malformed")
    if trust.get("mode") == "production-ed25519" and channel == "production":
        if signature is None or trust_root is None:
            _fail("production artifact manifest requires a signature and explicit trust root")
        _sha256_file(signature)
        _sha256_file(trust_root)
        if _public_key_sha256(trust_root, private=False) != trust.get("publicKeySha256"):
            _fail("artifact trust root does not match the signed manifest")
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
        if (
            not allow_unsigned_development
            or os.environ.get("PLATFORM_ENVIRONMENT") == "production"
            or signature is not None
            or trust_root is not None
        ):
            _fail("unsigned development artifact evidence requires explicit non-production mode")
    else:
        _fail("artifact trust mode and channel are inconsistent")


def verify_artifact_manifest(
    component_manifest: Path,
    manifest_path: Path,
    *,
    signature: Path | None,
    trust_root: Path | None,
    allow_unsigned_development: bool = False,
) -> dict[str, Any]:
    manifest = _load(manifest_path)
    if manifest.get("format") != ARTIFACT_FORMAT:
        _fail("artifact manifest format is unsupported")
    _verify_artifact_trust(
        manifest,
        manifest_path,
        signature=signature,
        trust_root=trust_root,
        allow_unsigned_development=allow_unsigned_development,
    )
    component = _load(component_manifest)
    components = component.get("components")
    source = manifest.get("sourceComponentManifest")
    if (
        component.get("format") != FORMAT
        or not isinstance(components, dict)
        or source
        != {
            "sha256": _sha256_file(component_manifest),
            "componentSetSha256": _sha256_bytes(_canonical(components)),
        }
        or manifest.get("sourceCommit") != components.get("sourceCommit")
    ):
        _fail("artifact manifest does not bind the accepted source component manifest")
    records = manifest.get("roleArtifacts")
    if not isinstance(records, dict) or set(records) != set(ROLES):
        _fail("artifact manifest does not contain exactly all role artifacts")
    for role, record in records.items():
        if (
            not isinstance(record, dict)
            or set(record)
            != {"qcow2Sha256", "nixOutput", "nixClosureSha256", "publicationMetadata"}
            or not _SHA256.fullmatch(str(record.get("qcow2Sha256")))
            or not _SHA256.fullmatch(str(record.get("nixClosureSha256")))
            or not re.fullmatch(r"[a-z0-9]{32}-[^/]{1,160}", str(record.get("nixOutput")))
            or not isinstance(record.get("publicationMetadata"), dict)
        ):
            _fail(f"artifact identity for {role} is malformed")
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"sbom", "provenance"}:
        _fail("artifact SBOM/provenance evidence is incomplete")
    loaded: dict[str, dict[str, Any]] = {}
    for name in ("sbom", "provenance"):
        record = evidence[name]
        if not isinstance(record, dict) or set(record) != {"file", "sha256"}:
            _fail(f"artifact {name} record is malformed")
        path = manifest_path.parent / str(record["file"])
        if path.parent != manifest_path.parent or _sha256_file(path) != record["sha256"]:
            _fail(f"artifact {name} hash does not match")
        loaded[name] = _load(path)
    if loaded["sbom"].get("spdxVersion") != SBOM_FORMAT:
        _fail("artifact SBOM is not SPDX 2.3")
    expected_subjects = [
        {"name": f"{role}.qcow2", "digest": {"sha256": records[role]["qcow2Sha256"]}}
        for role in ROLES
    ] + [
        {"name": f"{role}.nix-closure", "digest": {"sha256": records[role]["nixClosureSha256"]}}
        for role in ROLES
    ]
    if (
        loaded["provenance"].get("_type") != PROVENANCE_FORMAT
        or loaded["provenance"].get("subject") != expected_subjects
    ):
        _fail("artifact provenance subjects do not match role artifacts")
    return manifest


def verify_role_artifact(
    manifest: dict[str, Any],
    role: str,
    *,
    qcow2: Path,
    path_info: Path,
    output_store_path: Path,
    publication_metadata: dict[str, str],
) -> dict[str, Any]:
    if role not in ROLES:
        _fail("artifact role is unsupported")
    projection, closure_sha256 = _closure_projection(path_info)
    output_identity = output_store_path.name
    if output_identity not in {item["storePath"] for item in projection}:
        _fail("built Nix output is absent from closure evidence")
    actual = {
        "qcow2Sha256": _artifact_sha256(qcow2),
        "nixOutput": output_identity,
        "nixClosureSha256": closure_sha256,
        "publicationMetadata": dict(sorted(publication_metadata.items())),
    }
    if manifest["roleArtifacts"].get(role) != actual:
        _fail(f"built {role} artifact or publication metadata does not match signed evidence")
    return actual


def verify_artifact_from_environment(
    component_manifest: Path, values: dict[str, str]
) -> dict[str, Any]:
    path = values.get("PLATFORM_ARTIFACT_MANIFEST")
    if not path:
        _fail("PLATFORM_ARTIFACT_MANIFEST is required before setup mutation")
    acknowledgement = values.get("PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT")
    return verify_artifact_manifest(
        component_manifest,
        Path(path),
        signature=Path(values["PLATFORM_ARTIFACT_SIGNATURE"])
        if values.get("PLATFORM_ARTIFACT_SIGNATURE")
        else None,
        trust_root=Path(values["PLATFORM_ARTIFACT_TRUST_ROOT"])
        if values.get("PLATFORM_ARTIFACT_TRUST_ROOT")
        else None,
        allow_unsigned_development=acknowledgement == UNSIGNED_ACKNOWLEDGEMENT,
    )


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


def create_evidence_bundle(source: Path, output: Path) -> Path:
    """Create a deterministic bounded tar containing only signed release evidence."""
    if os.path.lexists(output):
        _fail("release evidence bundle destination already exists")
    files = [(name, source / name) for name in _BUNDLE_FILES]
    for _name, path in files:
        _sha256_file(path)
    try:
        with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as archive:
            for name, path in files:
                info = tarfile.TarInfo(name)
                info.size = path.stat().st_size
                info.mode = 0o400
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
        if output.stat().st_size > _MAX_BUNDLE:
            output.unlink(missing_ok=True)
            _fail("release evidence bundle exceeds its size limit")
        output.chmod(0o600)
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, tarfile.TarError) as error:
        output.unlink(missing_ok=True)
        raise ReleaseVerificationError("release evidence bundle could not be created") from error
    return output


def extract_evidence_bundle(bundle: Path, destination: Path) -> Path:
    """Extract an exact regular-file inventory into an absent private directory."""
    try:
        metadata = bundle.lstat()
    except OSError as error:
        raise ReleaseVerificationError("release evidence bundle is unavailable") from error
    if (
        not bundle.is_file()
        or bundle.is_symlink()
        or not 0 < metadata.st_size <= _MAX_BUNDLE
        or os.path.lexists(destination)
    ):
        _fail("release evidence bundle or destination is unsafe")
    staging = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    if os.path.lexists(staging):
        _fail("release evidence staging path already exists")
    try:
        staging.mkdir(mode=0o700)
        (staging / "artifacts").mkdir(mode=0o700)
        with tarfile.open(bundle, "r:") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if (
                tuple(names) != _BUNDLE_FILES
                or any(
                    not member.isfile() or not 0 < member.size <= _MAX_BUNDLE_FILE
                    for member in members
                )
                or sum(member.size for member in members) > _MAX_BUNDLE
            ):
                _fail("release evidence bundle inventory is unsafe")
            for member in members:
                source = archive.extractfile(member)
                if source is None:
                    _fail("release evidence bundle member is unavailable")
                target = staging / member.name
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    remaining = member.size
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            _fail("release evidence bundle member ended early")
                        view = memoryview(block)
                        while view:
                            view = view[os.write(descriptor, view) :]
                        remaining -= len(block)
                    if source.read(1):
                        _fail("release evidence bundle member exceeded its declared size")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        os.replace(staging, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, tarfile.TarError, ReleaseVerificationError):
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, _msg, headers, _newurl
    ):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def fetch_evidence_bundle(
    url: str,
    expected_sha256: str,
    destination: Path,
    *,
    expected_commit: str,
    trust_root: Path,
) -> Path:
    """Fetch one digest-pinned HTTPS bundle, extract it safely, and verify signatures."""
    if not url.startswith("https://") or not _SHA256.fullmatch(expected_sha256):
        _fail("release evidence URL or digest is invalid")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_metadata = destination.parent.lstat()
    if (
        destination.parent.is_symlink()
        or not destination.parent.is_dir()
        or parent_metadata.st_uid != os.geteuid()
        or parent_metadata.st_mode & 0o022
    ):
        _fail("release evidence destination directory is unsafe")
    temporary = destination.parent / f".{destination.name}.download-{os.getpid()}.tar"
    if os.path.lexists(temporary):
        _fail("release evidence download staging already exists")
    digest = hashlib.sha256()
    total = 0
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "openstack-platform-release/1"}
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        with opener.open(request, timeout=60) as response:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > _MAX_BUNDLE:
                        _fail("release evidence download exceeds its size limit")
                    digest.update(block)
                    view = memoryview(block)
                    while view:
                        view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if digest.hexdigest() != expected_sha256:
            _fail("release evidence bundle digest does not match")
        extract_evidence_bundle(temporary, destination)
        component = destination / "release-manifest.json"
        verify(
            Path.cwd(),
            component,
            expected_commit=expected_commit,
            signature=destination / "release-manifest.sig",
            trust_root=trust_root,
        )
        verify_artifact_manifest(
            component,
            destination / "artifacts/role-artifacts.json",
            signature=destination / "artifacts/role-artifacts.sig",
            trust_root=trust_root,
        )
    except (OSError, urllib.error.URLError) as error:
        raise ReleaseVerificationError("release evidence bundle fetch failed") from error
    finally:
        temporary.unlink(missing_ok=True)
    return destination


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
    artifact_create = commands.add_parser("artifact-generate")
    artifact_create.add_argument("--component-manifest", type=Path, required=True)
    artifact_create.add_argument("--inputs", type=Path, required=True)
    artifact_create.add_argument("--output", type=Path, required=True)
    artifact_create.add_argument("--signing-key", type=Path)
    artifact_create.add_argument("--unsigned-development", action="store_true")
    artifact_check = commands.add_parser("artifact-verify")
    artifact_check.add_argument("--component-manifest", type=Path, required=True)
    artifact_check.add_argument("--manifest", type=Path, required=True)
    artifact_check.add_argument("--signature", type=Path)
    artifact_check.add_argument("--trust-root", type=Path)
    artifact_check.add_argument("--allow-unsigned-development", action="store_true")
    role_check = commands.add_parser("verify-role")
    role_check.add_argument("--component-manifest", type=Path, required=True)
    role_check.add_argument("--manifest", type=Path, required=True)
    role_check.add_argument("--signature", type=Path)
    role_check.add_argument("--trust-root", type=Path)
    role_check.add_argument("--allow-unsigned-development", action="store_true")
    role_check.add_argument("--role", choices=ROLES, required=True)
    role_check.add_argument("--qcow2", type=Path, required=True)
    role_check.add_argument("--path-info", type=Path, required=True)
    role_check.add_argument("--output-store-path", type=Path, required=True)
    role_check.add_argument("--platform", type=Path, required=True)
    role_check.add_argument("--commit", required=True)
    bundle_create = commands.add_parser("bundle-create")
    bundle_create.add_argument("--source", type=Path, required=True)
    bundle_create.add_argument("--output", type=Path, required=True)
    bundle_fetch = commands.add_parser("bundle-fetch")
    bundle_fetch.add_argument("--url", required=True)
    bundle_fetch.add_argument("--sha256", required=True)
    bundle_fetch.add_argument("--destination", type=Path, required=True)
    bundle_fetch.add_argument("--commit", required=True)
    bundle_fetch.add_argument("--trust-root", type=Path, required=True)
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
    elif args.command == "verify":
        verify(
            args.repository,
            args.manifest,
            expected_commit=args.commit,
            signature=args.signature,
            trust_root=args.trust_root,
            allow_unsigned_development=args.allow_unsigned_development,
        )
        print("release-manifest=verified")
    elif args.command == "bundle-create":
        path = create_evidence_bundle(args.source, args.output)
        print(f"release-evidence-bundle={path} sha256={_artifact_sha256(path)}")
    elif args.command == "bundle-fetch":
        path = fetch_evidence_bundle(
            args.url,
            args.sha256,
            args.destination,
            expected_commit=args.commit,
            trust_root=args.trust_root,
        )
        print(f"release-evidence={path} verified=true")
    elif args.command == "artifact-generate":
        path = generate_artifact_manifest(
            args.component_manifest,
            args.inputs,
            args.output,
            signing_key=args.signing_key,
            unsigned=args.unsigned_development,
        )
        print(f"artifact-manifest={path}")
    else:
        artifact = verify_artifact_manifest(
            args.component_manifest,
            args.manifest,
            signature=args.signature,
            trust_root=args.trust_root,
            allow_unsigned_development=args.allow_unsigned_development,
        )
        if args.command == "verify-role":
            from .config import load_platform
            from .openstack import publisher_metadata

            metadata = dict(
                publisher_metadata(load_platform(args.platform), args.role, args.commit)
            )
            record = verify_role_artifact(
                artifact,
                args.role,
                qcow2=args.qcow2,
                path_info=args.path_info,
                output_store_path=args.output_store_path,
                publication_metadata=metadata,
            )
            manifest_sha = _sha256_file(args.manifest)
            print(f"artifact_manifest_sha256={manifest_sha}")
            print(f"qcow2_sha256={record['qcow2Sha256']}")
            print(f"nix_closure_sha256={record['nixClosureSha256']}")
            print(f"nix_output={record['nixOutput']}")
        else:
            print("artifact-manifest=verified")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseVerificationError as error:
        print(f"release verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
