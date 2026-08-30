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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from .config import load_platform
from .contracts import CONTROLLER_BACKUP_DIRECTORY, HOSTED_CONTROLLER_BACKUP_DIRECTORY
from .validation import ValidationError

_FORMAT = "openstack-platform-offsite-recovery-v1"
_COMPONENTS = ("hosted-controller", "operator-state", "managed-data")
# Fresh managed-data volumes default to 500 GiB. Configurable streaming bounds
# allow mature deployments while retaining hard limits against runaway mounts.
_HARD_MAX_FILE_BYTES = 4 * 1024**4
_HARD_MAX_TOTAL_BYTES = 8 * 1024**4
_DEFAULT_FILE_BYTES = 1024**4
_DEFAULT_TOTAL_BYTES = 4 * 1024**4
_MAX_FILES = 64
_CONFIG_FORMAT = "openstack-platform-offsite-export-config-v1"
_TIMESTAMP_DIRECTORY = re.compile(r"20[0-9]{6}T[0-9]{6}Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{64}")


class RecoveryBundleError(RuntimeError):
    """An unsafe, incomplete, or modified recovery bundle was refused."""


@dataclass(frozen=True, slots=True)
class Bounds:
    maximum_file_bytes: int = _DEFAULT_FILE_BYTES
    maximum_total_bytes: int = _DEFAULT_TOTAL_BYTES

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_file_bytes, bool)
            or not isinstance(self.maximum_file_bytes, int)
            or not 1024**3 <= self.maximum_file_bytes <= _HARD_MAX_FILE_BYTES
            or isinstance(self.maximum_total_bytes, bool)
            or not isinstance(self.maximum_total_bytes, int)
            or not self.maximum_file_bytes <= self.maximum_total_bytes <= _HARD_MAX_TOTAL_BYTES
        ):
            raise RecoveryBundleError("off-site export bounds are invalid")


_DEFAULT_BOUNDS = Bounds()


@dataclass(frozen=True, slots=True)
class OffsiteConfig:
    destination: Path
    mount_source: str
    filesystem_type: str
    bounds: Bounds
    maximum_receipt_age_hours: int


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


def _regular(path: Path, *, maximum: int = _HARD_MAX_FILE_BYTES) -> os.stat_result:
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


def _digest(path: Path, *, maximum: int = _HARD_MAX_FILE_BYTES) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > maximum:
                _fail("recovery evidence file exceeded its size limit while reading")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _files(root: Path, *, bounds: Bounds = _DEFAULT_BOUNDS) -> list[Path]:
    _directory(root, private_owner=False)
    result: list[Path] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not _NAME.fullmatch(path.name):
            _fail("recovery evidence has an unsafe filename")
        _regular(path, maximum=bounds.maximum_file_bytes)
        result.append(path)
    if not result or len(result) > _MAX_FILES:
        _fail("recovery evidence has an invalid file count")
    return result


def _selected_component_files(
    name: str, root: Path, *, bounds: Bounds = _DEFAULT_BOUNDS
) -> list[Path]:
    """Select the newest committed SQLite trio or one managed-data directory."""
    if name == "managed-data":
        return _files(root, bounds=bounds)
    _directory(root, private_owner=False)
    manifests = [
        path
        for path in sorted(root.iterdir(), key=lambda path: path.name)
        if path.name.endswith(".sqlite3.age.manifest") and _NAME.fullmatch(path.name)
    ]
    if not manifests:
        _fail(f"{name} has no committed encrypted SQLite backup")
    manifest = manifests[-1]
    ciphertext_name = manifest.name.removesuffix(".manifest")
    selected = [
        root / ciphertext_name,
        root / f"{ciphertext_name}.sha256",
        manifest,
    ]
    if not all(os.path.lexists(path) for path in selected):
        _fail(f"latest committed {name} backup is incomplete")
    for path in selected:
        _regular(path, maximum=bounds.maximum_file_bytes)
    return selected


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


def _validate_component(
    name: str, files: list[Path], *, bounds: Bounds = _DEFAULT_BOUNDS
) -> dict[str, str]:
    observed: dict[str, str] = {}
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
        digest = _digest(by_name[selected], maximum=bounds.maximum_file_bytes)
        observed[selected] = digest
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
            digest = _digest(by_name[filename], maximum=bounds.maximum_file_bytes)
            observed[filename] = digest
            expected_sums += f"{digest}  {filename}\n"
        if by_name["SHA256SUMS"].read_text() != expected_sums:
            _fail("managed-data committed checksums do not match")
    return observed


def _copy(source: Path, destination: Path, *, maximum: int = _HARD_MAX_FILE_BYTES) -> str:
    source_fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o400,
    )
    digest = hashlib.sha256()
    try:
        total = 0
        while chunk := os.read(source_fd, 1024 * 1024):
            total += len(chunk)
            if total > maximum:
                _fail("recovery evidence exceeded its size limit while copying")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(destination_fd, view) :]
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    return digest.hexdigest()


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
    bounds: Bounds = _DEFAULT_BOUNDS,
    destination_validator: Callable[[], None] | None = None,
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
            selected = _selected_component_files(component, sources[component], bounds=bounds)
            _validate_component(component, selected, bounds=bounds)
            component_root = staging / component
            component_root.mkdir(mode=0o700)
            for source in selected:
                metadata = _regular(source, maximum=bounds.maximum_file_bytes)
                total += metadata.st_size
                if total > bounds.maximum_total_bytes:
                    _fail("recovery bundle exceeds its total size limit")
                target = component_root / source.name
                digest = _copy(source, target, maximum=bounds.maximum_file_bytes)
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
            "limits": {
                "maximumFileBytes": bounds.maximum_file_bytes,
                "maximumTotalBytes": bounds.maximum_total_bytes,
            },
            "totalBytes": total,
        }
        _write_new(staging / "MANIFEST.json", _manifest_bytes(manifest))
        _write_new(
            staging / "MANIFEST.json.sha256",
            f"{hashlib.sha256(_manifest_bytes(manifest)).hexdigest()}  MANIFEST.json\n".encode(),
        )
        if destination_validator is not None:
            destination_validator()
        _sync(staging)
        os.replace(staging, destination)
        _sync(destination_root)
        verified = verify_bundle(destination)
        if verified != manifest:
            _fail("post-copy recovery bundle verification did not match")
        if destination_validator is not None:
            destination_validator()
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


def _manifest_bounds(manifest: dict[str, Any]) -> Bounds:
    limits = manifest.get("limits")
    if not isinstance(limits, dict) or set(limits) != {
        "maximumFileBytes",
        "maximumTotalBytes",
    }:
        _fail("recovery manifest limits are malformed")
    return Bounds(limits["maximumFileBytes"], limits["maximumTotalBytes"])


def verify_bundle(bundle: Path) -> dict[str, Any]:
    """Verify the committed manifest, exact inventory, sizes, and SHA-256 values."""
    manifest = _load_manifest(bundle)
    bounds = _manifest_bounds(manifest)
    records = manifest["files"]
    if not 1 <= len(records) <= len(_COMPONENTS) * _MAX_FILES:
        _fail("recovery manifest file count is invalid")
    actual: set[str] = set()
    observed_digests: dict[str, str] = {}
    total = 0
    for component in _COMPONENTS:
        component_root = bundle / component
        selected = _files(component_root, bounds=bounds)
        component_digests = _validate_component(component, selected, bounds=bounds)
        observed_digests.update(
            {f"{component}/{name}": digest for name, digest in component_digests.items()}
        )
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
            or not 0 < size <= bounds.maximum_file_bytes
            or not isinstance(digest, str)
            or not _SHA.fullmatch(digest)
            or name in expected
        ):
            _fail("recovery manifest file record is unsafe")
        path = bundle / name
        metadata = _regular(path, maximum=bounds.maximum_file_bytes)
        observed = observed_digests.get(name)
        if observed is None:
            observed = _digest(path, maximum=bounds.maximum_file_bytes)
        if metadata.st_size != size or observed != digest:
            _fail("recovery evidence size or checksum does not match")
        expected.add(name)
        total += size
    if (
        expected != actual
        or total != manifest.get("totalBytes")
        or total > bounds.maximum_total_bytes
    ):
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
    bounds = _manifest_bounds(manifest)
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
            for source in _files(bundle / component, bounds=bounds):
                _copy(
                    source,
                    target_root / source.name,
                    maximum=bounds.maximum_file_bytes,
                )
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


def load_offsite_config(path: Path) -> OffsiteConfig:
    metadata = _regular(path, maximum=64 * 1024)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        _fail("off-site export config must be current-user-owned mode 0600")
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryBundleError("off-site export config is malformed") from error
    if not isinstance(document, dict) or set(document) != {
        "destination",
        "filesystemType",
        "format",
        "limits",
        "maximumReceiptAgeHours",
        "mountSource",
    }:
        _fail("off-site export config fields are invalid")
    if document["format"] != _CONFIG_FORMAT:
        _fail("off-site export config format is unsupported")
    destination_value = document["destination"]
    if not isinstance(destination_value, str) or "\x00" in destination_value:
        _fail("off-site destination is malformed")
    destination = Path(destination_value)
    if (
        not destination.is_absolute()
        or destination == Path("/")
        or destination != Path(os.path.normpath(destination_value))
    ):
        _fail("off-site destination must be a canonical absolute path")
    source = document["mountSource"]
    filesystem = document["filesystemType"]
    if (
        not isinstance(source, str)
        or not 1 <= len(source) <= 512
        or any(character in source for character in "\x00\r\n")
        or not isinstance(filesystem, str)
        or not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", filesystem)
    ):
        _fail("off-site mount identity is malformed")
    limits = document["limits"]
    if not isinstance(limits, dict) or set(limits) != {
        "maximumFileBytes",
        "maximumTotalBytes",
    }:
        _fail("off-site export limits are malformed")
    bounds = Bounds(limits["maximumFileBytes"], limits["maximumTotalBytes"])
    age = document["maximumReceiptAgeHours"]
    if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= 168:
        _fail("off-site receipt age is invalid")
    return OffsiteConfig(destination, source, filesystem, bounds, age)


def _unescape_mount_field(value: str) -> str:
    return re.sub(
        r"\\(040|011|012|134)",
        lambda match: {"040": " ", "011": "\t", "012": "\n", "134": "\\"}[match.group(1)],
        value,
    )


def _mount_identity(
    destination: Path, *, mountinfo_path: Path = Path("/proc/self/mountinfo")
) -> tuple[str, str]:
    try:
        lines = mountinfo_path.read_text().splitlines()
    except OSError as error:
        raise RecoveryBundleError("mount table is unavailable") from error
    matches: list[tuple[str, str]] = []
    for line in lines:
        before, separator, after = line.partition(" - ")
        fields = before.split()
        trailing = after.split()
        if not separator or len(fields) < 6 or len(trailing) < 2:
            _fail("mount table is malformed")
        if _unescape_mount_field(fields[4]) == str(destination):
            matches.append((_unescape_mount_field(trailing[1]), trailing[0]))
    if len(matches) != 1:
        _fail("off-site destination is not a distinct mounted filesystem")
    return matches[0]


def validate_offsite_sink(
    config: OffsiteConfig,
    backup_root: Path,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    device_resolver: Callable[[Path], int] | None = None,
) -> None:
    destination_metadata = _directory(config.destination, private_owner=True)
    _directory(backup_root, private_owner=False)
    if stat.S_IMODE(destination_metadata.st_mode) != 0o700:
        _fail("off-site destination must be mode 0700")
    source, filesystem = _mount_identity(config.destination, mountinfo_path=mountinfo_path)
    if source != config.mount_source or filesystem != config.filesystem_type:
        _fail("off-site destination mount identity does not match configuration")
    try:
        destination_real = config.destination.resolve(strict=True)
        backup_real = backup_root.resolve(strict=True)
    except OSError as error:
        raise RecoveryBundleError("backup or off-site path could not be resolved") from error
    resolve_device = device_resolver or (lambda path: path.stat().st_dev)
    if (
        resolve_device(config.destination) == resolve_device(backup_root)
        or destination_real == backup_real
        or destination_real.is_relative_to(backup_real)
        or backup_real.is_relative_to(destination_real)
    ):
        _fail("off-site destination must not use the local backup filesystem")


def discover_latest_sources(
    backup_root: Path, namespace: str, *, bounds: Bounds
) -> dict[str, Path]:
    _directory(backup_root, private_owner=False)
    managed_root = backup_root / namespace
    _directory(managed_root, private_owner=False)
    candidates = sorted(
        path
        for path in managed_root.iterdir()
        if _TIMESTAMP_DIRECTORY.fullmatch(path.name)
        and path.is_dir()
        and not path.is_symlink()
        and (path / "MANIFEST").is_file()
    )
    if not candidates:
        _fail("no committed managed-data backup exists")
    managed = candidates[-1]
    sources = {
        "hosted-controller": backup_root / HOSTED_CONTROLLER_BACKUP_DIRECTORY,
        "operator-state": backup_root / CONTROLLER_BACKUP_DIRECTORY,
        "managed-data": managed,
    }
    for component, root in sources.items():
        selected = _selected_component_files(component, root, bounds=bounds)
        _validate_component(component, selected, bounds=bounds)
    return sources


def scheduled_export(
    platform_config: Path,
    offsite_config: Path,
    receipt: Path,
    *,
    now: datetime | None = None,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    device_resolver: Callable[[Path], int] | None = None,
) -> Path:
    platform = load_platform(platform_config)
    backup_value = platform.get("paths.backups")
    if not isinstance(backup_value, str):
        _fail("configured backup root is malformed")
    backup_root = Path(backup_value)
    config = load_offsite_config(offsite_config)
    validate_offsite_sink(
        config,
        backup_root,
        mountinfo_path=mountinfo_path,
        device_resolver=device_resolver,
    )
    sources = discover_latest_sources(backup_root, platform.namespace, bounds=config.bounds)
    return export_bundle(
        config.destination,
        sources,
        deployment=platform.namespace,
        created_at=now,
        receipt=receipt,
        bounds=config.bounds,
        destination_validator=lambda: validate_offsite_sink(
            config,
            backup_root,
            mountinfo_path=mountinfo_path,
            device_resolver=device_resolver,
        ),
    )


def recovery_status(
    platform_config: Path,
    offsite_config: Path,
    receipt: Path,
    *,
    now: datetime | None = None,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    device_resolver: Callable[[Path], int] | None = None,
) -> dict[str, Any]:
    platform = load_platform(platform_config)
    backup_root = Path(str(platform.get("paths.backups")))
    config = load_offsite_config(offsite_config)
    validate_offsite_sink(
        config,
        backup_root,
        mountinfo_path=mountinfo_path,
        device_resolver=device_resolver,
    )
    metadata = _regular(receipt, maximum=64 * 1024)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        _fail("off-site export receipt must be current-user-owned mode 0600")
    try:
        value = json.loads(receipt.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryBundleError("off-site export receipt is malformed") from error
    if (
        not isinstance(value, dict)
        or value.get("format") != _FORMAT
        or value.get("deployment") != platform.namespace
        or not isinstance(value.get("bundle"), str)
        or not _NAME.fullmatch(value["bundle"])
        or not value["bundle"].startswith(f"{platform.namespace}-")
        or not _SHA.fullmatch(str(value.get("manifestSha256", "")))
    ):
        _fail("off-site export receipt is malformed")
    try:
        exported = datetime.strptime(value["exportedAt"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError) as error:
        raise RecoveryBundleError("off-site export receipt time is malformed") from error
    current = (now or datetime.now(UTC)).astimezone(UTC)
    age = current - exported
    if age < timedelta(0) or age > timedelta(hours=config.maximum_receipt_age_hours):
        _fail("off-site export receipt is stale")
    bundle = config.destination / value["bundle"]
    manifest = _load_manifest(bundle)
    _manifest_bounds(manifest)
    if hashlib.sha256(_manifest_bytes(manifest)).hexdigest() != value["manifestSha256"]:
        _fail("off-site receipt does not match the retained bundle")
    return {
        "configured": True,
        "mounted": True,
        "bundle": value["bundle"],
        "exportedAt": value["exportedAt"],
        "ageHours": round(age.total_seconds() / 3600, 2),
        "verified": True,
    }


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
    scheduled = commands.add_parser(
        "scheduled-export", help="export latest committed evidence using persistent configuration"
    )
    scheduled.add_argument("--platform-config", type=Path, required=True)
    scheduled.add_argument("--config", type=Path, required=True)
    scheduled.add_argument("--receipt", type=Path, required=True)
    status = commands.add_parser("status", help="show credential-free off-site export status")
    status.add_argument("--platform-config", type=Path, required=True)
    status.add_argument("--config", type=Path, required=True)
    status.add_argument("--receipt", type=Path, required=True)
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
        elif args.command == "import":
            result = import_bundle(args.bundle, args.destination)
            print(f"offsite-import={result} verified=true")
        elif args.command == "scheduled-export":
            result = scheduled_export(
                args.platform_config,
                args.config,
                args.receipt,
            )
            print(f"offsite-scheduled-export={result.name} verified=true")
        else:
            value = recovery_status(args.platform_config, args.config, args.receipt)
            print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except (RecoveryBundleError, ValidationError, OSError) as error:
        print(f"recovery bundle failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
