"""One-request protocol-v1 helper dispatch and fixed backup acceptance."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from ..remote import (
    DEFAULT_REQUEST_LIMIT,
    DEFAULT_RESPONSE_LIMIT,
    ProtocolError,
    Request,
    encode_failure,
    encode_success,
    parse_request,
)
from ..runtime import ensure_private_directory, safe_summary, write_private_stack_diagnostic
from ..validation import ValidationError, bounded_text, safe_code

Handler = Callable[[Mapping[str, Any]], Mapping[str, Any]]
_ZERO_REQUEST_ID = "00000000-0000-0000-0000-000000000000"
_BACKUP_NAME = re.compile(r"platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMPACT_UTC = re.compile(r"20[0-9]{6}T[0-9]{6}Z")
_AGE_HEADER = b"age-encryption.org/v1\n"


class HelperActionError(RuntimeError):
    """A deliberate safe failure returned to the controller."""

    def __init__(self, code: str, message: str) -> None:
        self.code = safe_code(code)
        self.message = bounded_text(
            safe_summary(message),
            field="helper error message",
            maximum=1_024,
        )
        super().__init__(self.message)


def _regular_private_file(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise HelperActionError("INVALID_BACKUP", "staged backup must be a direct mode-0600 file")
    return metadata


def _read_private_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    _regular_private_file(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(65_536, maximum_bytes + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise HelperActionError("INVALID_BACKUP", "backup evidence exceeds its size limit")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _inspect_ciphertext(path: Path, *, maximum_bytes: int) -> tuple[str, int, bytes]:
    _regular_private_file(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    prefix = b""
    total = 0
    try:
        while chunk := os.read(descriptor, 1_048_576):
            total += len(chunk)
            if total > maximum_bytes:
                raise HelperActionError("INVALID_BACKUP", "backup size is invalid")
            if len(prefix) < len(_AGE_HEADER):
                prefix += chunk[: len(_AGE_HEADER) - len(prefix)]
            digest.update(chunk)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if total == 0:
        raise HelperActionError("INVALID_BACKUP", "backup size is invalid")
    return digest.hexdigest(), total, prefix


def _remove_uncommitted(path: Path) -> None:
    if not os.path.lexists(path):
        return
    _regular_private_file(path)
    path.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest_bytes(
    name: str,
    expected_sha256: str,
    plaintext_sha256: str,
    integrity_checked_at: str,
) -> bytes:
    created_at = name.removeprefix("platform-").removesuffix(".sqlite3.age")
    return (
        "format_version=1\n"
        f"name={name}\n"
        f"created_at={created_at}\n"
        "encryption=age-v1\n"
        f"ciphertext_sha256={expected_sha256}\n"
        f"plaintext_sha256={plaintext_sha256}\n"
        "sqlite_integrity=ok\n"
        f"integrity_checked_at={integrity_checked_at}\n"
    ).encode("ascii")


def _complete_backup(
    *,
    destination: Path,
    checksum_path: Path,
    manifest_path: Path,
    name: str,
    expected_sha256: str,
    manifest: bytes,
    maximum_bytes: int,
) -> int | None:
    if not os.path.lexists(manifest_path):
        return None
    if not all(os.path.lexists(path) for path in (destination, checksum_path)):
        raise HelperActionError("INVALID_BACKUP", "accepted backup evidence is incomplete")
    try:
        digest, size, prefix = _inspect_ciphertext(destination, maximum_bytes=maximum_bytes)
        checksum = _read_private_bytes(checksum_path, maximum_bytes=256)
        observed_manifest = _read_private_bytes(manifest_path, maximum_bytes=4_096)
    except (OSError, HelperActionError) as error:
        if isinstance(error, HelperActionError):
            raise HelperActionError(
                "INVALID_BACKUP", "accepted backup evidence could not be verified"
            ) from None
        raise HelperActionError(
            "INVALID_BACKUP", "accepted backup evidence could not be read"
        ) from None
    if (
        prefix != _AGE_HEADER
        or not hmac.compare_digest(digest, expected_sha256)
        or checksum != f"{expected_sha256}  {name}\n".encode("ascii")
        or observed_manifest != manifest
    ):
        raise HelperActionError("BACKUP_EXISTS", "an accepted backup already has that name")
    return size


def _complete_retention_entry(path: Path, *, maximum_bytes: int) -> bool:
    checksum_path = path.with_name(path.name + ".sha256")
    manifest_path = path.with_name(path.name + ".manifest")
    if not all(os.path.lexists(item) for item in (checksum_path, manifest_path)):
        return False
    try:
        digest, _size, prefix = _inspect_ciphertext(path, maximum_bytes=maximum_bytes)
        checksum = _read_private_bytes(checksum_path, maximum_bytes=256).decode("ascii")
        values: dict[str, str] = {}
        for line in (
            _read_private_bytes(manifest_path, maximum_bytes=4_096).decode("ascii").splitlines()
        ):
            key, separator, value = line.partition("=")
            if not separator or key in values:
                return False
            values[key] = value
    except (OSError, UnicodeDecodeError, HelperActionError):
        return False
    return (
        prefix == _AGE_HEADER
        and checksum == f"{digest}  {path.name}\n"
        and values.get("format_version") == "1"
        and values.get("name") == path.name
        and values.get("encryption") == "age-v1"
        and values.get("ciphertext_sha256") == digest
        and values.get("sqlite_integrity") == "ok"
    )


def _apply_retention(
    destination_directory: Path, *, retention_count: int, maximum_bytes: int
) -> None:
    candidates = [
        path
        for path in destination_directory.iterdir()
        if _BACKUP_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file()
    ]
    for candidate in candidates:
        if _complete_retention_entry(candidate, maximum_bytes=maximum_bytes):
            continue
        # No manifest means the candidate was never committed.  Reconcile
        # leftovers from a crashed promotion, but preserve a malformed set
        # carrying a commit marker for explicit operator attention.
        if not os.path.lexists(candidate.with_name(candidate.name + ".manifest")):
            _remove_uncommitted(candidate.with_name(candidate.name + ".sha256"))
            _remove_uncommitted(candidate)
    accepted = sorted(
        (
            path
            for path in destination_directory.iterdir()
            if _BACKUP_NAME.fullmatch(path.name)
            and not path.is_symlink()
            and path.is_file()
            and _complete_retention_entry(path, maximum_bytes=maximum_bytes)
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    for expired in accepted[retention_count:]:
        # Remove the commit marker first.  If retention is interrupted, the
        # remaining files are unaccepted debris and can never be mistaken for
        # a complete backup set.
        _remove_uncommitted(expired.with_name(expired.name + ".manifest"))
        _remove_uncommitted(expired.with_name(expired.name + ".sha256"))
        _remove_uncommitted(expired)
    _fsync_directory(destination_directory)


def accept_staged_backup(
    *,
    staging_directory: str | Path,
    backup_directory: str | Path,
    name: str,
    expected_sha256: str,
    plaintext_sha256: str,
    integrity_checked_at: str,
    retention_count: int = 14,
    maximum_bytes: int = 1_073_741_824,
) -> Mapping[str, Any]:
    """Verify and retry-safely publish one encrypted SQLite backup.

    The manifest is the commit marker: ciphertext and checksum are durable
    before its final rename, so a reader never treats a partial trio as
    accepted.  A retry reconciles a ciphertext move or evidence rename that
    was interrupted before the marker appeared, and only then reapplies
    retention.
    """
    if not isinstance(name, str) or not _BACKUP_NAME.fullmatch(name):
        raise HelperActionError("INVALID_ARGS", "backup name is malformed")
    if (
        not isinstance(expected_sha256, str)
        or not _SHA256.fullmatch(expected_sha256)
        or not isinstance(plaintext_sha256, str)
        or not _SHA256.fullmatch(plaintext_sha256)
    ):
        raise HelperActionError("INVALID_ARGS", "backup checksum is malformed")
    if not isinstance(integrity_checked_at, str) or not _COMPACT_UTC.fullmatch(
        integrity_checked_at
    ):
        raise HelperActionError("INVALID_ARGS", "backup integrity timestamp is malformed")
    if not 1 <= retention_count <= 365:
        raise ValueError("backup retention count must be from 1 through 365")
    staging = ensure_private_directory(staging_directory)
    destination_directory = ensure_private_directory(backup_directory)
    try:
        if staging.stat().st_dev != destination_directory.stat().st_dev:
            raise HelperActionError(
                "INVALID_BACKUP", "backup staging must share the accepted filesystem"
            )
    except OSError as error:
        raise HelperActionError(
            "INVALID_BACKUP", "backup directories could not be inspected"
        ) from error

    source = staging / name
    destination = destination_directory / name
    checksum_path = destination_directory / f"{name}.sha256"
    manifest_path = destination_directory / f"{name}.manifest"
    temporary_checksum = destination_directory / f".{name}.sha256.tmp"
    temporary_manifest = destination_directory / f".{name}.manifest.tmp"
    manifest = _manifest_bytes(name, expected_sha256, plaintext_sha256, integrity_checked_at)

    accepted_size = _complete_backup(
        destination=destination,
        checksum_path=checksum_path,
        manifest_path=manifest_path,
        name=name,
        expected_sha256=expected_sha256,
        manifest=manifest,
        maximum_bytes=maximum_bytes,
    )
    if accepted_size is not None:
        # A retransferred source is not needed after the commit marker is
        # present.  Remove it only after checking its identity; never remove a
        # malformed or mismatched staged file as a side effect of a read retry.
        try:
            if os.path.lexists(source):
                digest, _size, prefix = _inspect_ciphertext(source, maximum_bytes=maximum_bytes)
                if digest == expected_sha256 and prefix == _AGE_HEADER:
                    _remove_uncommitted(source)
        except (OSError, HelperActionError):
            pass
        _remove_uncommitted(temporary_checksum)
        _remove_uncommitted(temporary_manifest)
        _apply_retention(
            destination_directory,
            retention_count=retention_count,
            maximum_bytes=maximum_bytes,
        )
        return {"name": name, "sha256": expected_sha256, "bytes": accepted_size}

    # No commit marker means any final-name artifacts are an interrupted,
    # unaccepted promotion.  A valid ciphertext can be recovered after the
    # source rename; all other artifacts are removed before retrying.
    recovered_destination = False
    if os.path.lexists(destination):
        try:
            digest, size, prefix = _inspect_ciphertext(destination, maximum_bytes=maximum_bytes)
            recovered_destination = digest == expected_sha256 and prefix == _AGE_HEADER
        except (OSError, HelperActionError):
            recovered_destination = False
        if not recovered_destination:
            _remove_uncommitted(destination)
    _remove_uncommitted(checksum_path)
    _remove_uncommitted(temporary_checksum)
    _remove_uncommitted(temporary_manifest)

    source_size: int
    use_destination = recovered_destination
    if os.path.lexists(source):
        try:
            source_digest, source_size, source_prefix = _inspect_ciphertext(
                source, maximum_bytes=maximum_bytes
            )
        except (OSError, HelperActionError):
            if not use_destination:
                raise HelperActionError(
                    "INVALID_BACKUP", "staged backup could not be verified"
                ) from None
        else:
            if source_digest != expected_sha256:
                if not use_destination:
                    raise HelperActionError(
                        "CHECKSUM_MISMATCH", "staged backup checksum did not match"
                    )
            elif source_prefix != _AGE_HEADER:
                if not use_destination:
                    raise HelperActionError(
                        "INVALID_BACKUP", "staged backup is not age v1 ciphertext"
                    )
            else:
                use_destination = False
    elif not use_destination:
        raise HelperActionError("BACKUP_NOT_FOUND", "staged backup was not found")

    if use_destination:
        source_size = size

    def write_evidence(path: Path, value: bytes) -> None:
        _write_backup_evidence(path, value)

    try:
        write_evidence(temporary_checksum, f"{expected_sha256}  {name}\n".encode("ascii"))
        write_evidence(temporary_manifest, manifest)
        if not use_destination:
            os.replace(source, destination)
            _fsync_directory(destination_directory)
        os.replace(temporary_checksum, checksum_path)
        _fsync_directory(destination_directory)
        # This rename is the only commit point visible to backup readers.
        os.replace(temporary_manifest, manifest_path)
        _fsync_directory(destination_directory)
    finally:
        temporary_checksum.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)

    accepted_size = _complete_backup(
        destination=destination,
        checksum_path=checksum_path,
        manifest_path=manifest_path,
        name=name,
        expected_sha256=expected_sha256,
        manifest=manifest,
        maximum_bytes=maximum_bytes,
    )
    if accepted_size is None:
        raise HelperActionError(
            "INVALID_BACKUP", "backup acceptance did not publish complete evidence"
        )
    _apply_retention(
        destination_directory,
        retention_count=retention_count,
        maximum_bytes=maximum_bytes,
    )
    return {"name": name, "sha256": expected_sha256, "bytes": source_size}


def _write_backup_evidence(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        written = 0
        while written < len(value):
            written += os.write(descriptor, value[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_handler(
    *,
    staging_directory: str | Path,
    backup_directory: str | Path,
    retention_count: int = 14,
) -> Handler:
    """Bind deployment-fixed backup paths into a protocol action."""

    def handle(args: Mapping[str, Any]) -> Mapping[str, Any]:
        if args.keys() != {"name", "sha256", "plaintextSha256", "integrityCheckedAt"}:
            raise HelperActionError(
                "INVALID_ARGS",
                "backup.accept requires name, ciphertext/plaintext checksums, and integrity time",
            )
        return accept_staged_backup(
            staging_directory=staging_directory,
            backup_directory=backup_directory,
            name=args["name"],
            expected_sha256=args["sha256"],
            plaintext_sha256=args["plaintextSha256"],
            integrity_checked_at=args["integrityCheckedAt"],
            retention_count=retention_count,
        )

    return handle


def default_handlers() -> dict[str, Handler]:
    """Return the complete production protocol-v1 action map."""
    # Import lazily so envelope parsing and unit tests do not initialize live
    # Nomad or managed-storage dependencies.
    from .production import production_handlers

    return production_handlers()


def dispatch(
    request: Request,
    handlers: Mapping[str, Handler],
    *,
    diagnostic_directory: str | Path | None = None,
) -> bytes:
    handler = handlers.get(request.action)
    if handler is None:
        return encode_failure(
            request.request_id, "UNKNOWN_ACTION", "helper action is not implemented"
        )
    try:
        result = handler(request.args)
        if not isinstance(result, Mapping):
            raise TypeError("helper handler result is not a mapping")
        return encode_success(request.request_id, result)
    except HelperActionError as error:
        return encode_failure(request.request_id, error.code, error.message)
    except ValidationError:
        return encode_failure(
            request.request_id, "INVALID_ARGS", "helper action arguments are invalid"
        )
    except Exception as error:
        # Provider responses, exception text, locals, and source lines may contain
        # credentials. Persist only trusted stack locations under the request ID.
        try:
            if diagnostic_directory is not None:
                write_private_stack_diagnostic(
                    diagnostic_directory,
                    error,
                    correlation_id=request.request_id,
                )
        except Exception:
            # A diagnostic-path failure must not replace the bounded protocol
            # response or expose details from either exception.
            pass
        return encode_failure(
            request.request_id,
            "ACTION_FAILED",
            f"helper action failed safely; correlation ID {request.request_id}",
        )


def serve_once(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    handlers: Mapping[str, Handler],
    *,
    request_limit: int = DEFAULT_REQUEST_LIMIT,
    response_limit: int = DEFAULT_RESPONSE_LIMIT,
    diagnostic_directory: str | Path | None = None,
) -> int:
    """Read exactly one bounded request and emit exactly one bounded JSON response."""
    payload = input_stream.read(request_limit + 1)
    if len(payload) > request_limit:
        response = encode_failure(
            _ZERO_REQUEST_ID, "REQUEST_TOO_LARGE", "helper request exceeded its size limit"
        )
    else:
        try:
            request = parse_request(payload, maximum_bytes=request_limit)
        except (ProtocolError, ValidationError):
            response = encode_failure(
                _extract_request_id(payload), "INVALID_REQUEST", "helper request is invalid"
            )
        else:
            response = dispatch(
                request,
                handlers,
                diagnostic_directory=diagnostic_directory,
            )
            if len(response) > response_limit:
                response = encode_failure(
                    request.request_id,
                    "RESPONSE_TOO_LARGE",
                    "helper result exceeded its size limit",
                )
    output_stream.write(response)
    output_stream.flush()
    return 0


def _extract_request_id(payload: bytes) -> str:
    """Best-effort correlation for malformed envelopes, never error detail."""
    try:
        value = json.loads(payload)
        request_id = value.get("requestId") if isinstance(value, dict) else None
        if isinstance(request_id, str) and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            request_id,
        ):
            return request_id
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return _ZERO_REQUEST_ID


def main(handlers: Mapping[str, Handler] | None = None) -> int:
    diagnostic_directory: Path | None = None
    selected_handlers: Mapping[str, Handler]
    if handlers is None:
        from .production import helper_runtime

        runtime = helper_runtime()
        diagnostic_directory = runtime.diagnostic_directory
        selected_handlers = default_handlers()
    else:
        selected_handlers = handlers
    return serve_once(
        sys.stdin.buffer,
        sys.stdout.buffer,
        selected_handlers,
        diagnostic_directory=diagnostic_directory,
    )


if __name__ == "__main__":
    raise SystemExit(main())
