"""Fixed read projections for Python callers and the platform provider launcher.

The socket protocol contains typed actions, not OpenStack command lines. This
module owns the small argument vocabulary used by reviewed shell scripts.
Mutations still use their existing fixed runner; a read never falls back to OSC.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import runtime
from .provider_protocol import MAX_RESPONSE, remaining, timeout, validate
from .provider_protocol import ProviderError as ProviderError

COMMANDS = {
    ("token", "issue"): "identity",
    ("project", "show"): "identity",
    ("catalog", "show"): "image.catalog",
    ("console", "log", "show"): "console",
    ("server", "volume", "list"): "attachments",
    ("quota", "show"): "quota",
    ("limits", "show"): "limits",
}
for _words, _kind in (
    (("server",), "server"),
    (("flavor",), "flavor"),
    (("image",), "image"),
    (("port",), "port"),
    (("volume",), "volume"),
    (("network",), "network"),
    (("subnet",), "subnet"),
    (("router",), "router"),
    (("keypair",), "keypair"),
    (("floating", "ip"), "floating_ip"),
    (("security", "group"), "security_group"),
    (("security", "group", "rule"), "security_group_rule"),
    (("volume", "type"), "volume_type"),
):
    for _verb in ("show", "list"):
        COMMANDS[(*_words, _verb)] = f"{_kind}.{_verb}"


def is_read(arguments: Sequence[str]) -> bool:
    return any(tuple(arguments[: len(words)]) == words for words in COMMANDS)


@dataclass(frozen=True)
class Read:
    action: str
    params: dict[str, Any]
    columns: tuple[str, ...]
    format: str


def parse(arguments: Sequence[str]) -> Read:
    match = next(
        (
            (words, action)
            for words, action in sorted(COMMANDS.items(), key=lambda item: -len(item[0]))
            if tuple(arguments[: len(words)]) == words
        ),
        None,
    )
    if match is None:
        raise ProviderError("invalid")
    words, action = match
    values = iter(arguments[len(words) :])
    params: dict[str, Any] = {}
    columns = []
    positional = []
    output_format = "json"
    flag_names = {
        "--name": "name",
        "--server": "device_id",
        "--device-id": "device_id",
        "--network": "network_id",
        "--project": "project_id",
        "--fixed-ip": "fixed_ips",
    }
    try:
        for value in values:
            if value in {"-f", "--format"}:
                output_format = next(values)
                if output_format not in {"json", "value"}:
                    raise ProviderError("invalid")
            elif value in {"-c", "--column"}:
                columns.append(next(values))
            elif value in {"--long", "--absolute"}:
                if (value == "--long" and action not in {"image.list", "flavor.list"}) or (
                    value == "--absolute" and action != "limits"
                ):
                    raise ProviderError("invalid")
            elif value == "--private" and action == "image.list":
                params["visibility"] = "private"
            elif value in {"--network", "--usage"} and action == "quota":
                params[value[2:]] = True
            elif value == "--lines" and action == "console":
                params["lines"] = int(next(values))
            elif value in flag_names:
                key = flag_names[value]
                if key in params:
                    raise ProviderError("invalid")
                params[key] = next(values)
                if key == "fixed_ips":
                    params[key] = params[key].replace("ip-address=", "ip_address=")
            elif value.startswith("-"):
                raise ProviderError("invalid")
            else:
                positional.append(value)
    except (StopIteration, ValueError):
        raise ProviderError("invalid") from None
    if action.endswith(".show") or action in {"console", "attachments"}:
        if len(positional) != 1:
            raise ProviderError("invalid")
        params["reference"] = positional.pop()
    if action == "identity" and words == ("project", "show"):
        if len(positional) != 1:
            raise ProviderError("invalid")
        # Identity comes from the authenticated scoped token, with no project
        # enumeration permission. The caller checks the returned UUID/name.
        positional.pop()
    if action == "image.catalog":
        if positional != ["image"]:
            raise ProviderError("invalid")
        positional.clear()
    if action == "security_group_rule.list" and positional:
        if len(positional) != 1:
            raise ProviderError("invalid")
        params["security_group_id"] = positional.pop()
    if positional:
        raise ProviderError("invalid")
    if action == "console":
        params.setdefault("lines", None)
    validate(action, params)
    if len(columns) > 32 or any(len(column) > 128 for column in columns):
        raise ProviderError("invalid")
    return Read(action, params, tuple(columns), output_format)


def socket_path() -> str:
    path = os.environ.get("PLATFORM_PROVIDER_SOCKET")
    if not path:
        from .config import load_platform

        config = load_platform(Path(os.environ["PLATFORM_CONFIG"]))
        path = f"/run/{config.namespace}-provider/provider.sock"
    if not Path(path).is_absolute():
        raise ProviderError("configuration")
    return path


def receive(connection: socket.socket, limit: int, deadline: float) -> bytes:
    chunks = bytearray()
    while True:
        connection.settimeout(remaining(deadline))
        chunk = connection.recv(min(65_536, limit + 1 - len(chunks)))
        if not chunk:
            raise ProviderError("malformed")
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise ProviderError("size")
        if chunks.endswith(b"\n"):
            if b"\n" in chunks[:-1]:
                raise ProviderError("malformed")
            return bytes(chunks[:-1])


def call(
    action: str, params: dict[str, Any], *, timeout_seconds: float, path: str | None = None
) -> Any:
    validate(action, params)
    seconds = timeout(timeout_seconds)
    deadline = time.monotonic() + seconds
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(remaining(deadline))
            connection.connect(path or socket_path())
            # On admin the trusted daemon is the existing operator account; on
            # the operator host it is this account. No impostor daemon is used.
            raw = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _, uid, _ = struct.unpack("3i", raw)
            from .contracts import OPERATOR_ACCOUNT_UID

            expected_uid = int(
                os.environ.get(
                    "PLATFORM_PROVIDER_UID",
                    str(
                        os.geteuid()
                        if path or os.environ.get("PLATFORM_PROVIDER_SOCKET")
                        else OPERATOR_ACCOUNT_UID
                    ),
                )
            )
            if uid != expected_uid:
                raise ProviderError("peer")
            payload = (
                json.dumps(
                    {"version": 1, "action": action, "params": params, "timeout": seconds},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
            connection.settimeout(remaining(deadline))
            connection.sendall(payload)
            result = json.loads(receive(connection, MAX_RESPONSE + 512, deadline))
            if result.get("ok") is not True:
                raise ProviderError(result.get("code", "unavailable"), result.get("status", 0))
            return result["result"]
    except ProviderError:
        raise
    except Exception:
        raise ProviderError() from None


def _field(row: Mapping[str, Any], column: str, *, strict: bool = False) -> Any:
    key = column.lower().replace(" ", "_")
    aliases = {"device": "device", "image": "image", "volume_id": "volumeId"}
    if key == "id" and "volumeId" in row:
        return row["volumeId"]
    for name, value in row.items():
        if name.lower().replace(" ", "_") == aliases.get(key, key).lower():
            if key == "image" and isinstance(value, dict):
                if not value:
                    return None  # Nova's documented boot-from-volume projection.
                identifier = value.get("id")
                if not isinstance(identifier, str) or not identifier:
                    raise ProviderError("malformed")
                return identifier
            return value
    # Like the CLI, a selected attribute the cloud omits (for example Glance
    # multihash on older releases) is null; callers validate what they need.
    # Identity never has a token column to select.
    if strict:
        raise ProviderError("malformed")
    return None


def render(read: Read, result: Any) -> bytes:
    if read.action == "console":
        return (str(result) + "\n").encode()
    if read.columns:

        def select(row: Mapping[str, Any]) -> dict[str, Any]:
            return {
                column: _field(row, column, strict=read.action == "identity")
                for column in read.columns
            }

        result = [select(row) for row in result] if isinstance(result, list) else select(result)
    elif read.action.endswith(".list"):
        # Shell monitoring reads these fixed headings. Python projections use
        # case-insensitive field access. Keep provider IDs and metadata typed.
        result = [
            {
                ({"id": "ID", "name": "Name", "status": "Status"}.get(key, key)): value
                for key, value in row.items()
            }
            for row in result
        ]
    if read.format == "json":
        return json.dumps(result, separators=(",", ":")).encode()
    rows = result if isinstance(result, list) else [result]
    return (
        "\n".join(
            " ".join(
                str(value) if not isinstance(value, (dict, list)) else json.dumps(value)
                for value in row.values()
            )
            for row in rows
        )
        + "\n"
    ).encode()


def run(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 30,
    stdout_limit: int = MAX_RESPONSE,
    check: bool = True,
    **_bounds: Any,
) -> runtime.CommandResult:
    """Read runner injected at the existing bounded command seam, without exec."""
    try:
        read = parse(argv[1:])
        result = call(read.action, read.params, timeout_seconds=timeout_seconds)
        output = render(read, result)
        if len(output) > stdout_limit:
            raise ProviderError("size")
        return runtime.CommandResult(tuple(argv), 0, output, b"", False, False)
    except ProviderError as error:
        result = runtime.CommandResult(tuple(argv), 1, b"", str(error).encode(), False, False)
        if check:
            raise runtime.CommandFailure(str(error), result) from None
        return result
