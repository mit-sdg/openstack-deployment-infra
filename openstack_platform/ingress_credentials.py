"""Local ingress credential escrow; no guest access or online token verification."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from . import durable, host_user_data, runtime
from .config import PlatformConfig
from .validation import ValidationError, uuid

_MAX_BYTES = 16_384
_FILENAME = "ingress-credentials.json"


def _read(path: Path) -> bytes:
    # NONBLOCK also makes a malicious FIFO fail rather than hang at open().
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > _MAX_BYTES
            ):
                raise ValidationError("ingress credential must be a direct owner-owned 0600 file")
            data = os.read(fd, _MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise ValidationError("ingress credential exceeds its size limit")
            return data
        finally:
            os.close(fd)
    except OSError:
        raise ValidationError("ingress credential file is missing or unreadable") from None


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data, object_pairs_hook=_object)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ValidationError("ingress credential JSON is malformed") from None


def _token(data: bytes) -> tuple[str, str]:
    """Validate Cloudflare's base64 {a,t,s} connector token, without echoing it."""
    try:
        token = data.decode("ascii").removesuffix("\n").removesuffix("\r")
        if not token or len(token) > 8192 or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", token) is None:
            raise ValueError("invalid encoding")
        decoded = base64.b64decode(token, validate=True)
        document = _json(decoded)
        account, tunnel, secret = (document.get(key) for key in ("a", "t", "s"))
        if (
            set(document) != {"a", "t", "s"}
            or not isinstance(account, str)
            or re.fullmatch(r"[0-9a-f]{32}", account) is None
            or not isinstance(tunnel, str)
            or not isinstance(secret, str)
            or len(base64.b64decode(secret, validate=True)) != 32
        ):
            raise ValueError("invalid fields")
        tunnel = uuid(tunnel, field="tunnel UUID")
        return token, tunnel
    except (ValueError, UnicodeError, binascii.Error, ValidationError):
        raise ValidationError("Cloudflare connector token is malformed") from None


def _identity(platform: PlatformConfig) -> dict[str, str]:
    return {
        "projectId": platform.project_id,
        "namespace": platform.namespace,
        "domain": platform.domain,
    }


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directory(state_directory: Path, *, create: bool = False) -> Path:
    try:
        for path in (state_directory, state_directory / "credentials"):
            runtime.ensure_private_directory(path, create=create)
            if create:
                # Persist new directory entries as well as the eventual file.
                _sync_directory(path.parent)
        return state_directory / "credentials"
    except (OSError, runtime.RuntimeFailure):
        raise ValidationError(
            "ingress escrow requires direct operator-owned 0700 state/credentials directories"
        ) from None


def _load(platform: PlatformConfig, state_directory: Path) -> tuple[str, str]:
    document = _json(_read(_directory(state_directory) / _FILENAME))
    if (
        set(document) != {"version", "deployment", "tunnelId", "token"}
        or type(document["version"]) is not int
        or document["version"] != 1
        or document["deployment"] != _identity(platform)
        or not isinstance(document["token"], str)
    ):
        raise ValidationError("ingress escrow schema or deployment identity mismatch")
    try:
        encoded = document["token"].encode("ascii")
    except UnicodeError:
        raise ValidationError("Cloudflare connector token is malformed") from None
    token, tunnel = _token(encoded)
    if document["tunnelId"] != tunnel:
        raise ValidationError("ingress escrow tunnel identity mismatch")
    return token, tunnel


def verify_escrow(
    platform: PlatformConfig, state_directory: Path, *, tunnel_id: str | None = None
) -> None:
    """Verify local protection, structure and identity, not remote validity."""
    expected = None if tunnel_id is None else uuid(tunnel_id, field="expected tunnel UUID")
    _, actual = _load(platform, state_directory)
    if expected is not None and actual != expected:
        raise ValidationError("ingress escrow does not match the expected tunnel UUID")


def import_escrow(
    platform: PlatformConfig, state_directory: Path, token_file: Path, *, tunnel_id: str
) -> None:
    """Import a raw token. Re-import is idempotent; changing an escrow is refused."""
    expected = uuid(tunnel_id, field="expected tunnel UUID")
    token, actual = _token(_read(token_file))
    if actual != expected:
        raise ValidationError("ingress credential does not match the expected tunnel UUID")
    directory = _directory(state_directory, create=True)
    with runtime.lock(state_directory, "infrastructure"):
        destination = directory / _FILENAME
        if os.path.lexists(destination):
            existing, existing_id = _load(platform, state_directory)
            if existing != token or existing_id != actual:
                raise ValidationError(
                    "ingress escrow already contains a different credential; import refused"
                )
            try:
                # Finish a prior commit interrupted just after its rename.
                _sync_directory(directory)
            except OSError:
                raise ValidationError("ingress escrow could not be committed durably") from None
            return
        payload = json.dumps(
            {
                "version": 1,
                "deployment": _identity(platform),
                "tunnelId": actual,
                "token": token,
            }
        ).encode()
        try:
            durable.atomic_write(destination, payload, mode=0o600, maximum_bytes=_MAX_BYTES)
        except durable.DurableReplaceError:
            raise ValidationError("ingress escrow could not be committed durably") from None
        verify_escrow(platform, state_directory, tunnel_id=expected)


@contextmanager
def staged_replacement_user_data(
    platform: PlatformConfig, state_directory: Path, *, maximum_bytes: int
) -> Iterator[str]:
    """Capture verified escrow once, force Cloudflare on, render before provider calls."""
    token, _ = _load(platform, state_directory)
    with TemporaryDirectory(prefix="platform-ingress-credentials-") as temporary:
        token_file = Path(temporary) / "token"
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(token)
        # Disabling Cloudflare and ad-hoc token paths cannot bypass escrow.
        environment = dict(os.environ)
        environment["ENABLE_CLOUDFLARED"] = "true"
        environment["CLOUDFLARE_TUNNEL_TOKEN_FILE"] = str(token_file)
        inputs = host_user_data.inputs_from_environment("ingress", environment=environment)
        with host_user_data.staged_host_user_data(
            platform, "ingress", {}, inputs=inputs, maximum_bytes=maximum_bytes
        ) as path:
            yield path
