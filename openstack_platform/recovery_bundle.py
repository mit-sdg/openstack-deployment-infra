"""Provider-neutral, append-only off-site recovery bundle export and import.

The bundle contains only already-encrypted evidence.  A canonical manifest is
written last as the commit marker; existing bundles and imports are never
replaced.  The implementation intentionally knows only filesystems, making a
mounted WORM volume, removable disk, or an object-store FUSE mount an operator
choice rather than a platform credential.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

_FORMAT = "openstack-platform-offsite-recovery-v1"
_COMPONENTS = ("hosted-controller", "operator-state", "managed-data")
# Fresh managed-data volumes default to 500 GiB, so recovery bounds must allow a
# full logical archive rather than silently making mature deployments
# unexportable. They remain finite to reject runaway mounts and malformed input.
_MAX_FILE_BYTES = 1024 * 1024**3
_MAX_TOTAL_BYTES = 4 * 1024 * 1024**3
_MAX_FILES = 64
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{64}")


class RecoveryBundleError(RuntimeError):
    """An unsafe, incomplete, or modified recovery bundle was refused."""


def _fail(message: str) -> NoReturn:
    raise RecoveryBundleError(message)


def _directory(path: Path, *, private_owner: bool) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RecoveryBundleError("recovery directory is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("recovery directory must be a direct directory")
    if metadata.st_mode & (0o022 if private_owner else 0o002):
        _fail("recovery directory has unsafe write permissions")
    if private_owner and metadata.st_uid != os.geteuid():
        _fail("recovery destination must be owned by the current user")
    return metadata


def _regular(path: Path, *, maximum: int = _MAX_FILE_BYTES) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RecoveryBundleError(f"recovery evidence {path.name!r} is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail("recovery evidence must contain only direct regular files")
    if metadata.st_mode & 0o022:
        _fail("recovery evidence files must not be writable by group or other")
    if not 0 < metadata.st_size <= maximum:
        _fail("recovery evidence file is empty or exceeds its size limit")
    return metadata


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                _fail("recovery evidence file exceeded its size limit while reading")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    _directory(root, private_owner=False)
    result: list[Path] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not _NAME.fullmatch(path.name):
            _fail("recovery evidence has an unsafe filename")
        _regular(path)
        result.append(path)
    if not result or len(result) > _MAX_FILES:
        _fail("recovery evidence has an invalid file count")
    return result


def _selected_component_files(name: str, root: Path) -> list[Path]:
    """Select the newest committed SQLite trio or one managed-data directory."""
    available = _files(root)
    if name == "managed-data":
        return available
    by_name = {path.name: path for path in available}
    candidates: list[list[Path]] = []
    for manifest in available:
        if not manifest.name.endswith(".sqlite3.age.manifest"):
            continue
        ciphertext_name = manifest.name.removesuffix(".manifest")
        required = (ciphertext_name, ciphertext_name + ".sha256", manifest.name)
        if all(item in by_name for item in required):
            candidates.append([by_name[item] for item in required])
    if not candidates:
        _fail(f"{name} has no committed encrypted SQLite backup")
    return candidates[-1]


def _age_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if os.read(descriptor, 22) != b"age-encryption.org/v1\n":
            _fail("recovery ciphertext is not age v1")
    finally:
        os.close(descriptor)


def _key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise RecoveryBundleError("recovery evidence manifest is malformed") from error
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key in values:
            _fail("recovery evidence manifest is malformed")
        values[key] = value
    return values


def _validate_component(name: str, files: list[Path]) -> None:
    by_name = {path.name: path for path in files}
    names = set(by_name)
    if name in {"hosted-controller", "operator-state"}:
        ciphertext = [item for item in names if item.endswith(".sqlite3.age")]
        if (
            len(ciphertext) != 1
            or not {
                ciphertext[0] + ".sha256",
                ciphertext[0] + ".manifest",
            }
            <= names
        ):
            _fail(f"{name} evidence is not a committed encrypted SQLite backup")
        selected = ciphertext[0]
        digest = _digest(by_name[selected])
        _age_file(by_name[selected])
        if by_name[selected + ".sha256"].read_text() != f"{digest}  {selected}\n":
            _fail(f"{name} committed checksum does not match")
        manifest_path = by_name[selected + ".manifest"]
        if name == "hosted-controller":
            try:
                manifest = json.loads(manifest_path.read_text())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RecoveryBundleError("hosted-controller manifest is malformed") from error
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "openstack-platform-hosted-controller-backup-v1"
                or manifest.get("name") != selected
                or manifest.get("sha256") != digest
            ):
                _fail("hosted-controller manifest does not match its ciphertext")
        else:
            manifest = _key_values(manifest_path)
            if (
                manifest.get("format_version") != "1"
                or manifest.get("name") != selected
                or manifest.get("encryption") != "age-v1"
                or manifest.get("ciphertext_sha256") != digest
                or manifest.get("sqlite_integrity") != "ok"
            ):
                _fail("operator-state manifest does not match its ciphertext")
    else:
        required = {
            "postgres.age",
            "mongodb.age",
            "garage.age",
            "registry.age",
            "SHA256SUMS",
            "MANIFEST",
        }
        if not required <= names:
            _fail("managed-data evidence omits encrypted data or retained OCI artifacts")
        manifest = _key_values(by_name["MANIFEST"])
        if (
            manifest.get("format_version") != "2"
            or manifest.get("registry") != "distribution-artifacts-tar-gzip"
        ):
            _fail("managed-data manifest is not the recovery-capable format")
        expected_sums = ""
        for filename in ("postgres.age", "mongodb.age", "garage.age", "registry.age"):
            _age_file(by_name[filename])
            expected_sums += f"{_digest(by_name[filename])}  {filename}\n"
        if by_name["SHA256SUMS"].read_text() != expected_sums:
            _fail("managed-data committed checksums do not match")


def _copy(source: Path, destination: Path) -> None:
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o400,
    )
    try:
        total = 0
        while chunk := os.read(source_fd, 1024 * 1024):
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                _fail("recovery evidence exceeded its size limit while copying")
            view = memoryview(chunk)
            while view:
                view = view[os.write(destination_fd, view) :]
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        os.close(destination_fd)


def _write_new(path: Path, payload: bytes, *, mode: int = 0o400) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, mode
    )
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_receipt(path: Path, manifest: dict[str, Any]) -> None:
    _directory(path.parent, private_owner=True)
    payload = _manifest_bytes(
        {
            "format": _FORMAT,
            "bundle": manifest["bundle"],
            "deployment": manifest["deployment"],
            "exportedAt": manifest["createdAt"],
            "manifestSha256": hashlib.sha256(_manifest_bytes(manifest)).hexdigest(),
        }
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def export_bundle(
    destination_root: Path,
    sources: dict[str, Path],
    *,
    deployment: str,
    created_at: datetime | None = None,
    receipt: Path | None = None,
) -> Path:
    """Copy three selected committed evidence sets into one append-only bundle."""
    _directory(destination_root, private_owner=True)
    if set(sources) != set(_COMPONENTS):
        raise ValueError("all recovery components must be supplied exactly once")
    if not _NAME.fullmatch(deployment):
        _fail("deployment identifier is malformed")
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle_name = f"{deployment}-{timestamp}"
    destination = destination_root / bundle_name
    staging = destination_root / f".{bundle_name}.staging-{os.getpid()}"
    if os.path.lexists(destination) or os.path.lexists(staging):
        _fail("recovery bundle already exists")

    inventory: list[dict[str, Any]] = []
    total = 0
    try:
        staging.mkdir(mode=0o700)
        for component in _COMPONENTS:
            selected = _selected_component_files(component, sources[component])
            _validate_component(component, selected)
            component_root = staging / component
            component_root.mkdir(mode=0o700)
            for source in selected:
                metadata = _regular(source)
                total += metadata.st_size
                if total > _MAX_TOTAL_BYTES:
                    _fail("recovery bundle exceeds its total size limit")
                target = component_root / source.name
                _copy(source, target)
                digest = _digest(target)
                if digest != _digest(source):
                    _fail("recovery evidence changed during export")
                inventory.append(
                    {
                        "path": f"{component}/{source.name}",
                        "bytes": metadata.st_size,
                        "sha256": digest,
                    }
                )
            _sync(component_root)
        checksums = "".join(f"{item['sha256']}  {item['path']}\n" for item in inventory).encode()
        _write_new(staging / "SHA256SUMS", checksums)
        manifest = {
            "format": _FORMAT,
            "bundle": bundle_name,
            "deployment": deployment,
            "createdAt": timestamp,
            "files": inventory,
            "totalBytes": total,
        }
        _write_new(staging / "MANIFEST.json", _manifest_bytes(manifest))
        _write_new(
            staging / "MANIFEST.json.sha256",
            f"{hashlib.sha256(_manifest_bytes(manifest)).hexdigest()}  MANIFEST.json\n".encode(),
        )
        _sync(staging)
        os.replace(staging, destination)
        _sync(destination_root)
        if receipt is not None:
            _write_receipt(receipt, manifest)
        return destination
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_manifest(bundle: Path) -> dict[str, Any]:
    _directory(bundle, private_owner=False)
    path = bundle / "MANIFEST.json"
    metadata = _regular(path, maximum=1024 * 1024)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryBundleError("recovery manifest is malformed") from error
    if not isinstance(value, dict) or value.get("format") != _FORMAT:
        _fail("recovery manifest format is unsupported")
    if value.get("bundle") != bundle.name or not isinstance(value.get("files"), list):
        _fail("recovery manifest identity or inventory is malformed")
    checksum = bundle / "MANIFEST.json.sha256"
    _regular(checksum, maximum=256)
    expected = f"{_digest(path)}  MANIFEST.json\n"
    if checksum.read_text() != expected or metadata.st_size > 1024 * 1024:
        _fail("recovery manifest checksum does not match")
    return value


def verify_bundle(bundle: Path) -> dict[str, Any]:
    """Verify the committed manifest, exact inventory, sizes, and SHA-256 values."""
    manifest = _load_manifest(bundle)
    records = manifest["files"]
    if not 1 <= len(records) <= len(_COMPONENTS) * _MAX_FILES:
        _fail("recovery manifest file count is invalid")
    actual: set[str] = set()
    total = 0
    for component in _COMPONENTS:
        component_root = bundle / component
        selected = _files(component_root)
        _validate_component(component, selected)
        actual.update(f"{component}/{path.name}" for path in selected)
    expected: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "bytes", "sha256"}:
            _fail("recovery manifest file record is malformed")
        name, size, digest = record["path"], record["bytes"], record["sha256"]
        if (
            not isinstance(name, str)
            or name.count("/") != 1
            or name.split("/", 1)[0] not in _COMPONENTS
            or not _NAME.fullmatch(name.split("/", 1)[1])
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= _MAX_FILE_BYTES
            or not isinstance(digest, str)
            or not _SHA.fullmatch(digest)
            or name in expected
        ):
            _fail("recovery manifest file record is unsafe")
        path = bundle / name
        metadata = _regular(path)
        if metadata.st_size != size or _digest(path) != digest:
            _fail("recovery evidence size or checksum does not match")
        expected.add(name)
        total += size
    if expected != actual or total != manifest.get("totalBytes") or total > _MAX_TOTAL_BYTES:
        _fail("recovery manifest inventory or total does not match")
    sums = bundle / "SHA256SUMS"
    _regular(sums, maximum=1024 * 1024)
    expected_sums = "".join(f"{record['sha256']}  {record['path']}\n" for record in records)
    if sums.read_text() != expected_sums:
        _fail("recovery checksum inventory does not match the manifest")
    return manifest


def import_bundle(bundle: Path, destination_root: Path) -> Path:
    """Verify and copy a bundle into an empty private recovery staging root."""
    manifest = verify_bundle(bundle)
    _directory(destination_root, private_owner=True)
    destination = destination_root / str(manifest["bundle"])
    if os.path.lexists(destination):
        _fail("recovery import already exists; imports are never replaced")
    staging = destination_root / f".{destination.name}.import-{os.getpid()}"
    if os.path.lexists(staging):
        _fail("recovery import staging path already exists")
    try:
        staging.mkdir(mode=0o700)
        for component in _COMPONENTS:
            target_root = staging / component
            target_root.mkdir(mode=0o700)
            for source in _files(bundle / component):
                _copy(source, target_root / source.name)
            _sync(target_root)
        for name in ("SHA256SUMS", "MANIFEST.json.sha256", "MANIFEST.json"):
            _copy(bundle / name, staging / name)
        _sync(staging)
        os.replace(staging, destination)
        _sync(destination_root)
        verify_bundle(destination)
        return destination
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openstack-platform-recovery", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export", help="commit encrypted evidence to selected off-site storage"
    )
    export.add_argument("--destination", type=Path, required=True)
    export.add_argument("--deployment", required=True)
    export.add_argument("--hosted-controller", type=Path, required=True)
    export.add_argument("--operator-state", type=Path, required=True)
    export.add_argument("--managed-data", type=Path, required=True)
    export.add_argument("--receipt", type=Path)
    verify = commands.add_parser("verify", help="verify an off-site bundle without decrypting it")
    verify.add_argument("bundle", type=Path)
    import_command = commands.add_parser(
        "import", help="verify and stage a bundle for offline restore"
    )
    import_command.add_argument("bundle", type=Path)
    import_command.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            result = export_bundle(
                args.destination,
                {
                    "hosted-controller": args.hosted_controller,
                    "operator-state": args.operator_state,
                    "managed-data": args.managed_data,
                },
                deployment=args.deployment,
                receipt=args.receipt,
            )
            print(f"offsite-export={result} verified=true")
        elif args.command == "verify":
            manifest = verify_bundle(args.bundle)
            print(
                f"offsite-bundle={manifest['bundle']} verified=true files={len(manifest['files'])}"
            )
        else:
            result = import_bundle(args.bundle, args.destination)
            print(f"offsite-import={result} verified=true")
        return 0
    except (RecoveryBundleError, OSError) as error:
        print(f"recovery bundle failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
