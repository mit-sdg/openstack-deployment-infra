"""Per-replacement protected token input; no credential store or guest access."""

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

from . import host_user_data
from .config import PlatformConfig
from .validation import ValidationError, uuid

_MAX_BYTES = 16_384


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
                raise ValidationError(
                    "ingress credential must be a direct operator-owned 0600 file"
                )
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


def _token(data: bytes) -> str:
    """Check connector token structure offline, not Cloudflare validity or routing."""
    try:
        token = data.decode("ascii")
        if token.endswith("\r\n"):
            token = token[:-2]
        elif token.endswith("\n"):
            token = token[:-1]
        if not token or len(token) > 8192 or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", token) is None:
            raise ValueError("invalid encoding")
        document = json.loads(base64.b64decode(token, validate=True), object_pairs_hook=_object)
        if not isinstance(document, dict) or set(document) != {"a", "t", "s"}:
            raise ValueError("invalid fields")
        account, tunnel, secret = (document[key] for key in ("a", "t", "s"))
        if (
            not isinstance(account, str)
            or re.fullmatch(r"[0-9a-f]{32}", account) is None
            or not isinstance(tunnel, str)
            or not isinstance(secret, str)
            or len(base64.b64decode(secret, validate=True)) != 32
        ):
            raise ValueError("invalid fields")
        uuid(tunnel, field="tunnel UUID")
        return token
    except (ValueError, UnicodeError, binascii.Error, ValidationError, RecursionError):
        raise ValidationError("Cloudflare connector token is malformed") from None


@contextmanager
def staged_replacement_user_data(
    platform: PlatformConfig, token_file: Path | None, *, maximum_bytes: int
) -> Iterator[str]:
    """Read the explicit input once and render before any provider call.

    Private temporary copies prevent source changes between validation and
    rendering. They are unlinked on normal and exceptional context exit; the
    operator's original file is neither changed nor retained in platform state.
    """
    if token_file is None:
        raise ValidationError("fresh ingress replacement requires --cloudflare-tunnel-token-file")
    token = _token(_read(token_file))
    with TemporaryDirectory(prefix="platform-ingress-credentials-") as temporary:
        snapshot = Path(temporary) / "token"
        fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(token)
        # Only the explicit, validated file supplies the token. Environment
        # overrides cannot disable Cloudflare or select an unvalidated source.
        environment = dict(os.environ)
        environment["ENABLE_CLOUDFLARED"] = "true"
        environment["CLOUDFLARE_TUNNEL_TOKEN_FILE"] = str(snapshot)
        inputs = host_user_data.inputs_from_environment("ingress", environment=environment)
        with host_user_data.staged_host_user_data(
            platform, "ingress", {}, inputs=inputs, maximum_bytes=maximum_bytes
        ) as path:
            yield path
