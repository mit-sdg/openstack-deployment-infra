"""Fixed JSON protocol v1 and pinned helper SSH invocation."""

from __future__ import annotations

import json
import os
import re
import shlex
import uuid as uuid_module
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import PROTOCOL_VERSION
from .runtime import CommandFailure, run
from .validation import ValidationError, bounded_text, safe_code, uuid

DEFAULT_REQUEST_LIMIT = 1_048_576
DEFAULT_RESPONSE_LIMIT = 1_048_576
DEFAULT_SSH_CONFIG = Path("/srv/openstack-platform/.secrets/ssh/config")
SSH_TARGET = "platform-admin"
HELPER_COMMAND_NAME = "openstack-platform-helper"


def helper_command_path(deployment_root: str) -> str:
    """Absolute path of the helper launcher inside the admin deployment root.

    The launcher is linked into ``<paths.root>/bin`` by the role image and by
    the helper release installer, but nothing puts that directory on the remote
    login PATH. Addressing it by name therefore fails to resolve, so callers
    pass the configured root and the helper is invoked by absolute path.
    """
    root = PurePosixPath(deployment_root)
    if not root.is_absolute():
        raise ValidationError("deployment root must be an absolute path")
    return str(root / "bin" / HELPER_COMMAND_NAME)


_ACTION = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+")
_ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REQUEST_KEYS = {"version", "requestId", "action", "args"}
_SUCCESS_KEYS = {"version", "requestId", "ok", "result"}
_FAILURE_KEYS = {"version", "requestId", "ok", "error"}


class ProtocolError(RuntimeError):
    """The peer did not implement the complete fixed protocol v1 envelope."""


class HelperError(RuntimeError):
    """A helper-declared failure containing only its safe code and summary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class DependencyUnavailable(HelperError):
    """The pinned SSH transport or a helper dependency is unavailable."""

    def __init__(self, message: str) -> None:
        super().__init__("DEPENDENCY_UNAVAILABLE", message)


def _ssh_config_path(value: str | os.PathLike[str]) -> str:
    path = os.fspath(value)
    if not path or "\x00" in path or len(path.encode()) > 4_096 or not Path(path).is_absolute():
        raise ValidationError("pinned SSH config path must be a bounded absolute path")
    return path


def pinned_admin_command(
    remote_command: Sequence[str],
    *,
    ssh_config_path: str | os.PathLike[str] = DEFAULT_SSH_CONFIG,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Build an argv pinned to the deployment SSH config and admin alias.

    OpenSSH passes its command through a remote shell. Joining validated argv
    elements into one quoted argument prevents inventory paths from becoming
    shell syntax while retaining the fixed destination.
    """
    arguments = tuple(remote_command)
    if not arguments or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in arguments
    ):
        raise ValidationError("remote admin command must be a non-empty NUL-free argv")
    command = shlex.join(arguments)
    if environment:
        assignments: list[str] = []
        for key, value in sorted(environment.items()):
            if (
                not isinstance(key, str)
                or not _ENVIRONMENT_KEY.fullmatch(key)
                or not isinstance(value, str)
                or not value
                or "\x00" in value
                or len(value.encode()) > 4_096
            ):
                raise ValidationError("remote admin environment is malformed")
            assignments.append(f"{key}={shlex.quote(value)}")
        command = " ".join((*assignments, command))
    return (
        "ssh",
        "-F",
        _ssh_config_path(ssh_config_path),
        SSH_TARGET,
        "--",
        command,
    )


def pinned_admin_scp(
    local_path: str | os.PathLike[str],
    remote_path: str,
    *,
    ssh_config_path: str | os.PathLike[str] = DEFAULT_SSH_CONFIG,
) -> tuple[str, ...]:
    """Build a fixed SCP upload argv using the same pinned admin config."""
    local = os.fspath(local_path)
    if not local or "\x00" in local or len(local.encode()) > 4_096:
        raise ValidationError("local SCP path must be bounded and NUL-free")
    if (
        not isinstance(remote_path, str)
        or not re.fullmatch(r"/[A-Za-z0-9._/-]{1,4095}", remote_path)
        or "//" in remote_path
    ):
        raise ValidationError("remote SCP path must be a bounded absolute path")
    return (
        "scp",
        "-F",
        _ssh_config_path(ssh_config_path),
        "--",
        local,
        f"{SSH_TARGET}:{remote_path}",
    )


def helper_ssh_command(
    *,
    ssh_config_path: str | os.PathLike[str] = DEFAULT_SSH_CONFIG,
    helper_command: str = HELPER_COMMAND_NAME,
) -> tuple[str, ...]:
    return (
        "ssh",
        "-F",
        _ssh_config_path(ssh_config_path),
        SSH_TARGET,
        "--",
        helper_command,
    )


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    action: str
    args: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class Response:
    request_id: str
    ok: bool
    result: Mapping[str, Any] | None = field(default=None, repr=False)
    error_code: str | None = None
    error_message: str | None = field(default=None, repr=False)


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _json_object(payload: bytes | str, *, maximum_bytes: int, name: str) -> dict[str, Any]:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > maximum_bytes:
        raise ProtocolError(f"{name} exceeds its {maximum_bytes}-byte limit")

    def reject_constant(_value: str) -> None:
        raise ProtocolError("JSON contains a non-finite number")

    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicates,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as error:
        raise ProtocolError(f"{name} must be UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise ProtocolError(f"{name} is malformed JSON or has extra output") from error
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be one JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, name: str) -> None:
    if value.keys() != expected:
        unknown = value.keys() - expected
        missing = expected - value.keys()
        details = []
        if missing:
            details.append(f"missing {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown {', '.join(sorted(unknown))}")
        raise ProtocolError(f"{name} fields are invalid: {'; '.join(details)}")


def parse_request(payload: bytes | str, *, maximum_bytes: int = DEFAULT_REQUEST_LIMIT) -> Request:
    """Parse a strict protocol-v1 request envelope."""
    value = _json_object(payload, maximum_bytes=maximum_bytes, name="helper request")
    _exact_keys(value, _REQUEST_KEYS, name="helper request")
    if isinstance(value["version"], bool) or value["version"] != PROTOCOL_VERSION:
        raise ProtocolError("helper request version must be exactly 1")
    try:
        request_id = uuid(value["requestId"], field="requestId")
    except ValidationError as error:
        raise ProtocolError(str(error)) from error
    action = value["action"]
    if not isinstance(action, str) or not _ACTION.fullmatch(action):
        raise ProtocolError("helper request action is malformed")
    if not isinstance(value["args"], dict):
        raise ProtocolError("helper request args must be an object")
    return Request(request_id=request_id, action=action, args=value["args"])


def encode_request(
    action: str,
    args: Mapping[str, Any],
    *,
    request_id: str | None = None,
    maximum_bytes: int = DEFAULT_REQUEST_LIMIT,
) -> tuple[str, bytes]:
    """Build one bounded request; the payload is suitable only for stdin."""
    if not _ACTION.fullmatch(action):
        raise ValidationError("helper action is malformed")
    identifier = (
        uuid(request_id, field="requestId") if request_id is not None else str(uuid_module.uuid4())
    )
    try:
        payload = json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "requestId": identifier,
                "action": action,
                "args": dict(args),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as error:
        raise ValidationError("helper arguments must contain only finite JSON values") from error
    if len(payload) > maximum_bytes:
        raise ValidationError(f"helper request exceeds its {maximum_bytes}-byte limit")
    return identifier, payload


def encode_success(request_id: str, result: Mapping[str, Any]) -> bytes:
    uuid(request_id, field="requestId")
    return json.dumps(
        {"version": PROTOCOL_VERSION, "requestId": request_id, "ok": True, "result": dict(result)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def encode_failure(request_id: str, code: str, message: str) -> bytes:
    uuid(request_id, field="requestId")
    code = safe_code(code)
    message = bounded_text(message, field="error message", maximum=1_024)
    if "\n" in message or "\r" in message:
        raise ValidationError("error message must be one line")
    return json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "requestId": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def parse_response(
    payload: bytes | str,
    *,
    expected_request_id: str,
    maximum_bytes: int = DEFAULT_RESPONSE_LIMIT,
) -> Response:
    """Parse a strict response and reject mismatched request identity."""
    expected_request_id = uuid(expected_request_id, field="expected requestId")
    value = _json_object(payload, maximum_bytes=maximum_bytes, name="helper response")
    if not isinstance(value.get("ok"), bool):
        raise ProtocolError("helper response ok must be boolean")
    _exact_keys(value, _SUCCESS_KEYS if value["ok"] else _FAILURE_KEYS, name="helper response")
    if isinstance(value["version"], bool) or value["version"] != PROTOCOL_VERSION:
        raise ProtocolError("helper response version must be exactly 1")
    try:
        request_id = uuid(value["requestId"], field="requestId")
    except ValidationError as error:
        raise ProtocolError(str(error)) from error
    if request_id != expected_request_id:
        raise ProtocolError("helper response requestId does not match the request")
    if value["ok"]:
        if not isinstance(value["result"], dict):
            raise ProtocolError("helper success result must be an object")
        return Response(request_id=request_id, ok=True, result=value["result"])
    error = value["error"]
    if not isinstance(error, dict) or error.keys() != {"code", "message"}:
        raise ProtocolError("helper failure error must contain only code and message")
    try:
        code = safe_code(error["code"])
        message = bounded_text(error["message"], field="error message", maximum=1_024)
    except ValidationError as validation_error:
        raise ProtocolError(str(validation_error)) from validation_error
    if "\n" in message or "\r" in message:
        raise ProtocolError("helper error message must be one line")
    return Response(
        request_id=request_id,
        ok=False,
        error_code=code,
        error_message=message,
    )


def _response_result(
    payload: bytes,
    *,
    request_id: str,
    response_limit: int,
) -> Mapping[str, Any]:
    response = parse_response(
        payload,
        expected_request_id=request_id,
        maximum_bytes=response_limit,
    )
    if not response.ok:
        assert response.error_code is not None and response.error_message is not None
        if response.error_code == "DEPENDENCY_UNAVAILABLE":
            raise DependencyUnavailable(response.error_message)
        raise HelperError(response.error_code, response.error_message)
    assert response.result is not None
    return response.result


def call_helper(
    action: str,
    args: Mapping[str, Any],
    *,
    timeout_seconds: float,
    request_limit: int = DEFAULT_REQUEST_LIMIT,
    response_limit: int = DEFAULT_RESPONSE_LIMIT,
    stderr_limit: int = 262_144,
    request_id: str | None = None,
    command_runner: Callable[..., Any] = run,
    ssh_config_path: str | os.PathLike[str] = DEFAULT_SSH_CONFIG,
    helper_command: str = HELPER_COMMAND_NAME,
) -> Mapping[str, Any]:
    """Invoke exactly the pinned helper SSH command and return a non-secret result.

    Connect-time bounds are configured on the pinned ``platform-admin`` SSH alias
    in the explicit deployment config; this function additionally enforces the
    whole-call deadline.
    """
    identifier, payload = encode_request(
        action,
        args,
        request_id=request_id,
        maximum_bytes=request_limit,
    )
    try:
        completed = command_runner(
            helper_ssh_command(ssh_config_path=ssh_config_path, helper_command=helper_command),
            timeout_seconds=timeout_seconds,
            stdin=payload,
            stdout_limit=response_limit + 1,
            stderr_limit=stderr_limit,
            inherit_env=("HOME", "USER", "SSH_AUTH_SOCK"),
            check=True,
        )
    except CommandFailure:
        # Bounded stderr remains available to callers through the exception for
        # private diagnostics, but it is deliberately absent from this message.
        raise DependencyUnavailable("platform helper SSH is unavailable or timed out") from None
    return _response_result(
        completed.stdout,
        request_id=identifier,
        response_limit=response_limit,
    )


def call_local_helper(
    action: str,
    args: Mapping[str, Any],
    *,
    timeout_seconds: float,
    helper_command: str,
    request_limit: int = DEFAULT_REQUEST_LIMIT,
    response_limit: int = DEFAULT_RESPONSE_LIMIT,
    stderr_limit: int = 262_144,
    request_id: str | None = None,
    command_runner: Callable[..., Any] = run,
) -> Mapping[str, Any]:
    """Invoke one fixed local helper executable with the same strict protocol."""
    command = Path(helper_command)
    if (
        not helper_command
        or "\x00" in helper_command
        or not command.is_absolute()
        or os.path.normpath(helper_command) != helper_command
    ):
        raise ValidationError("local helper command must be a canonical absolute path")
    identifier, payload = encode_request(
        action,
        args,
        request_id=request_id,
        maximum_bytes=request_limit,
    )
    try:
        completed = command_runner(
            (helper_command,),
            timeout_seconds=timeout_seconds,
            stdin=payload,
            stdout_limit=response_limit + 1,
            stderr_limit=stderr_limit,
            inherit_env=("HOME", "USER"),
            check=True,
        )
    except CommandFailure:
        raise DependencyUnavailable("local platform helper is unavailable or timed out") from None
    return _response_result(
        completed.stdout,
        request_id=identifier,
        response_limit=response_limit,
    )
