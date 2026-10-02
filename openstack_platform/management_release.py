"""Management archives and artifact evidence using the existing release trust gate."""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import release_manifest as evidence

FORMAT = "openstack-platform-management-artifacts-v1"
MAXIMUM_ARCHIVE = 128 * 1024**2
MAXIMUM_MEMBER = 32 * 1024**2
MAXIMUM_MEMBERS = 4096
COMPATIBILITY = {
    "brokerProtocolVersion": 2,
    "webProtocolVersion": 2,
    "authProtocolVersion": 2,
    "brokerSchemaVersion": 2,
    "controllerApiVersion": 1,
}
BASE = ("__init__.py", "config.py", "contracts.py", "validation.py", "owner_portal_config.py")
CONTROLLER = (
    "__init__.py",
    "http.py",
    "application_models.py",
    "deployment_config.py",
    "storage_contract.py",
)


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or len(name) > 256
        or path.is_absolute()
        or path.as_posix() != name
        or any(
            part in {"..", ".", "", "node_modules", "__pycache__", ".git", ".tmp"}
            for part in path.parts
        )
        or "\\" in name
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
    ):
        raise ValueError("unsafe management archive member")
    return name


def tar_files(archive: Path | bytes) -> dict[str, bytes]:
    if isinstance(archive, Path):
        if (
            archive.is_symlink()
            or not archive.is_file()
            or archive.stat().st_size > MAXIMUM_ARCHIVE
        ):
            raise ValueError("management archive must be a bounded direct file")
        raw = archive.read_bytes()
    else:
        raw = archive
    if len(raw) > MAXIMUM_ARCHIVE:
        raise ValueError("management archive exceeds its byte limit")
    files: dict[str, bytes] = {}
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as bundle:
        for index, member in enumerate(bundle):
            if index >= MAXIMUM_MEMBERS:
                raise ValueError("management archive member limit exceeded")
            name = safe_name(member.name.rstrip("/") if member.isdir() else member.name)
            if name in seen:
                raise ValueError("duplicate management archive member")
            seen.add(name)
            if not member.isfile() and not member.isdir():
                raise ValueError("management archive contains a link or special file")
            if member.isdir():
                continue
            if not 0 <= member.size <= MAXIMUM_MEMBER:
                raise ValueError("management archive member exceeds size limit")
            total += member.size
            if total > MAXIMUM_ARCHIVE:
                raise ValueError("management archive unpacked size limit exceeded")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError("management archive has no member data")
            data = stream.read(member.size + 1)
            if len(data) != member.size:
                raise ValueError("management archive is truncated")
            files[name] = data
    if not files:
        raise ValueError("management archive is empty")
    return files


def record(data: bytes) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def write_tar(path: Path, files: dict[str, bytes]) -> None:
    with (
        path.open("xb") as stream,
        tarfile.open(fileobj=stream, mode="w:", format=tarfile.USTAR_FORMAT) as bundle,
    ):
        for name, data in sorted(files.items()):
            safe_name(name)
            member = tarfile.TarInfo(name)
            member.mode, member.mtime, member.size = 0o440, 0, len(data)
            bundle.addfile(member, io.BytesIO(data))
    path.chmod(0o600)


def runtime_files(source: dict[str, bytes], mode: str) -> dict[str, bytes]:
    names = {f"openstack_platform/{name}" for name in BASE}
    names |= {f"openstack_platform/controller/{name}" for name in CONTROLLER}
    names |= {
        f"openstack_platform/management/{name}"
        for name in ("__init__.py", "entry.py", "common.py", "config.py", "settings.py")
    }
    for name in source:
        if name.startswith("openstack_platform/management/broker/") and (
            mode == "broker" or name.endswith(("/__init__.py", "/client.py"))
        ):
            names.add(name)
        if mode == "broker" and name.startswith("openstack_platform/management/identity/"):
            names.add(name)
        if mode == "web" and name.startswith("openstack_platform/management/web/"):
            names.add(name)
    result = {f"runtime/{name}": source[name] for name in sorted(names)}
    result["runtime/openstack_platform/platform_contract.json"] = source[
        "infra/lib/platform_contract.json"
    ]
    return result


def build_snapshot(repository: Path, commit: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    receipt = evidence._load(repository / "frontend/owner-portal/build-receipt.json")
    if (
        receipt.get("format") != "openstack-platform-owner-build-v1"
        or receipt.get("dirty") is not False
        or receipt.get("sourceCommit") != commit
        or receipt.get("npmLockSha256")
        != evidence._sha256_file(repository / "frontend/owner-portal/package-lock.json")
    ):
        raise ValueError("management build receipt is dirty, stale or mismatched")
    root = repository / "frontend/owner-portal/dist"
    payload = {
        safe_name(path.relative_to(root).as_posix()): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assets = {name: record(data) for name, data in payload.items()}
    if (
        any(path.is_symlink() for path in root.rglob("*"))
        or assets != receipt.get("assets")
        or "index.html" not in assets
        or ".vite/manifest.json" not in assets
    ):
        raise ValueError("built assets differ from their build receipt")
    result = {name: receipt[name] for name in ("nodeVersion", "assetManifestSha256", "assets")}
    if result["assetManifestSha256"] != assets[".vite/manifest.json"]["sha256"]:
        raise ValueError("generated Vite manifest hash differs from build receipt")
    evidence.component_set(repository, commit, ui_build=result)
    return result, payload


def build_record(repository: Path, commit: str) -> dict[str, Any]:
    return build_snapshot(repository, commit)[0]


def generate(
    repository: Path,
    commit: str,
    output: Path,
    *,
    signing_key: Path | None = None,
    unsigned: bool = False,
    unsigned_production: bool = False,
) -> Path:
    evidence._verify_checkout(repository, commit)
    build, assets = build_snapshot(repository, commit)
    channel, trust = evidence._generation_trust(signing_key, unsigned, unsigned_production)
    component = evidence.generate(
        repository,
        commit,
        output,
        signing_key=signing_key,
        unsigned=unsigned,
        unsigned_production=unsigned_production,
        ui_build=build,
    )
    source_tar = output / "source.tar"
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "archive",
            "--format=tar",
            f"--output={source_tar.absolute()}",
            commit,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    source = tar_files(source_tar)
    if any(
        name.endswith((".pem", ".key", ".sqlite3")) and not name.startswith("docs/")
        for name in source
    ):
        raise ValueError("source archive contains signing material or harness state")
    requirements = {"python": "3.14", "identityTls": "system-ca", "schemaVersion": 2}
    artifacts = {}
    for mode in ("broker", "web"):
        files = {
            "source.tar": source_tar.read_bytes(),
            **runtime_files(source, mode),
            "requirements.json": evidence._canonical(requirements),
        }
        if mode == "web":
            files.update({"static/" + name: data for name, data in assets.items()})
        name = f"{mode}-{commit}.tar"
        archive = output / name
        write_tar(archive, files)
        artifacts[mode] = {
            "file": name,
            **record(archive.read_bytes()),
            "files": {path: record(data) for path, data in files.items()},
        }
    source_tar.unlink()
    component_document = evidence._load(component)
    binding = {
        "sourceCommit": commit,
        "compatibility": COMPATIBILITY,
        "ui": component_document["components"]["ui"],
        "archives": artifacts,
        "runtime": requirements,
        "sourceManifestSha256": evidence._sha256_file(component),
    }
    pair_identity = hashlib.sha256(evidence._canonical(binding)).hexdigest()
    sbom = evidence._load(output / "release.sbom.json")
    sbom_path = output / "management.sbom.json"
    sbom_path.write_bytes(evidence._canonical(sbom))
    provenance = {
        "_type": evidence.PROVENANCE_FORMAT,
        "subject": [
            {"name": value["file"], "digest": {"sha256": value["sha256"]}}
            for value in artifacts.values()
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://example.com/openstack-platform/management/v1",
                "externalParameters": {
                    "sourceCommit": commit,
                    "nodeVersion": build["nodeVersion"],
                    "assetManifestSha256": build["assetManifestSha256"],
                },
                "resolvedDependencies": [
                    {
                        "uri": "file:release-manifest.json",
                        "digest": {"sha256": binding["sourceManifestSha256"]},
                    },
                    {
                        "uri": "file:package-lock.json",
                        "digest": {
                            "sha256": component_document["components"]["ui"]["npmLockSha256"]
                        },
                    },
                ],
            },
            "runDetails": {"builder": {"id": "openstack-platform-management-release-v1"}},
        },
    }
    provenance_path = output / "management.provenance.json"
    provenance_path.write_bytes(evidence._canonical(provenance))
    document = {
        "format": FORMAT,
        "releaseChannel": channel,
        "trust": trust,
        **binding,
        "pairIdentity": pair_identity,
        "evidence": {
            "sbom": {"file": sbom_path.name, "sha256": evidence._sha256_file(sbom_path)},
            "provenance": {
                "file": provenance_path.name,
                "sha256": evidence._sha256_file(provenance_path),
            },
        },
    }
    path = output / "management-artifacts.json"
    path.write_bytes(evidence._canonical(document))
    if signing_key:
        evidence._openssl(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(signing_key),
                "-in",
                str(path),
                "-out",
                str(output / "management-artifacts.sig"),
            ]
        )
    return path


def verify(
    component: Path,
    manifest: Path,
    *,
    signature: Path | None,
    trust_root: Path | None,
    allow_unsigned_development: bool = False,
    allow_unsigned_production: bool = False,
) -> dict[str, Any]:
    document = evidence._load(manifest)
    evidence._verify_artifact_trust(
        document,
        manifest,
        signature=signature,
        trust_root=trust_root,
        allow_unsigned_development=allow_unsigned_development,
        allow_unsigned_production=allow_unsigned_production,
    )
    source = evidence._load(component)
    if (
        document.get("format") != FORMAT
        or document.get("sourceManifestSha256") != evidence._sha256_file(component)
        or document.get("sourceCommit") != source["components"]["sourceCommit"]
        or document.get("ui") != source["components"]["ui"]
        or document.get("compatibility") != COMPATIBILITY
        or document.get("trust") != source["trust"]
        or document.get("releaseChannel") != source["releaseChannel"]
    ):
        raise ValueError("management descriptor differs from authenticated source compatibility")
    binding = {
        name: document[name]
        for name in (
            "sourceCommit",
            "compatibility",
            "ui",
            "archives",
            "runtime",
            "sourceManifestSha256",
        )
    }
    if (
        document["pairIdentity"] != hashlib.sha256(evidence._canonical(binding)).hexdigest()
        or set(document["archives"]) != {"broker", "web"}
        or document["ui"]["build"] is None
    ):
        raise ValueError("management pair identity or built evidence is invalid")
    for kind in ("sbom", "provenance"):
        entry = document["evidence"][kind]
        if (
            Path(entry["file"]).name != entry["file"]
            or evidence._sha256_file(manifest.parent / entry["file"]) != entry["sha256"]
        ):
            raise ValueError("management SBOM/provenance hash differs")
    sbom = evidence._load(manifest.parent / document["evidence"]["sbom"]["file"])
    provenance = evidence._load(manifest.parent / document["evidence"]["provenance"]["file"])
    if (
        sbom != evidence._load(component.parent / source["evidence"]["sbom"]["file"])
        or provenance.get("subject")
        != [
            {"name": row["file"], "digest": {"sha256": row["sha256"]}}
            for row in document["archives"].values()
        ]
        or provenance["predicate"]["buildDefinition"]["externalParameters"]
        != {
            "sourceCommit": document["sourceCommit"],
            "nodeVersion": document["ui"]["build"]["nodeVersion"],
            "assetManifestSha256": document["ui"]["build"]["assetManifestSha256"],
        }
    ):
        raise ValueError("management evidence projection differs")
    return document


def verify_files(
    files: dict[str, bytes], document: dict[str, Any], mode: str, source: dict[str, bytes]
) -> None:
    """Check the closed payload layout and actual build bytes already in memory."""
    runtime = runtime_files(source, mode)
    assets = document["ui"]["build"]["assets"] if mode == "web" else {}
    for name in assets:
        safe_name(name)
    allowed = (
        {"source.tar", "requirements.json"} | set(runtime) | {"static/" + name for name in assets}
    )
    if set(files) != allowed:
        raise ValueError("management archive layout contains unexpected or missing files")
    if {name: data for name, data in files.items() if name.startswith("runtime/")} != runtime:
        raise ValueError("management runtime differs from authenticated source")
    if files["requirements.json"] != evidence._canonical(document["runtime"]):
        raise ValueError("management runtime requirements differ")
    actual = {
        name.removeprefix("static/"): record(data)
        for name, data in files.items()
        if name.startswith("static/")
    }
    if actual != assets:
        raise ValueError("management web assets differ from generated output evidence")
    if {name: record(data) for name, data in files.items()} != document["archives"][mode]["files"]:
        raise ValueError("management archive inventory differs")


def verify_archive(archive: Path, document: dict[str, Any], mode: str) -> dict[str, bytes]:
    expected = document["archives"][mode]
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > MAXIMUM_ARCHIVE:
        raise ValueError("management archive must be bounded and direct")
    raw = archive.read_bytes()
    if record(raw) != {name: expected[name] for name in ("sha256", "bytes")}:
        raise ValueError("management archive full hash differs from authenticated evidence")
    files = tar_files(raw)
    source = tar_files(files["source.tar"])
    verify_files(files, document, mode, source)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build or verify commit-bound management release artifacts"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build")
    create.add_argument("--repository", type=Path, default=Path.cwd())
    create.add_argument("--commit", required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--signing-key", type=Path)
    create.add_argument("--unsigned-development", action="store_true")
    create.add_argument("--unsigned-production", action="store_true")
    check = commands.add_parser("verify")
    check.add_argument("--repository", type=Path, default=Path.cwd())
    check.add_argument("--commit", required=True)
    check.add_argument("--component-manifest", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--source-signature", type=Path)
    check.add_argument("--signature", type=Path)
    check.add_argument("--trust-root", type=Path)
    check.add_argument("--allow-unsigned-development", action="store_true")
    check.add_argument("--allow-unsigned-production", action="store_true")
    args = parser.parse_args()
    if args.command == "verify":
        policy = {
            "trust_root": args.trust_root,
            "allow_unsigned_development": args.allow_unsigned_development,
            "allow_unsigned_production": args.allow_unsigned_production,
        }
        evidence.verify(
            args.repository,
            args.component_manifest,
            expected_commit=args.commit,
            signature=args.source_signature,
            **policy,
        )
        document = verify(
            args.component_manifest, args.manifest, signature=args.signature, **policy
        )
        for mode in ("broker", "web"):
            verify_archive(
                args.manifest.parent / document["archives"][mode]["file"], document, mode
            )
        print("management-artifacts=verified archives=broker,web")
        return
    path = generate(
        args.repository,
        args.commit,
        args.output,
        signing_key=args.signing_key,
        unsigned=args.unsigned_development,
        unsigned_production=args.unsigned_production,
    )
    print(f"management-artifacts={path}")


if __name__ == "__main__":
    main()
