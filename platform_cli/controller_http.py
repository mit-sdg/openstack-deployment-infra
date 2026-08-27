"""Bounded JSON HTTP transport for the local controller Unix socket."""

from __future__ import annotations

import json
import re
import socketserver
import uuid as uuid_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlsplit

_MAX_BODY = 1_048_576
_MAX_RESPONSE = 1_048_576
_PATH_PARAMETER = re.compile(r"\{([a-z][a-zA-Z0-9]*)\}")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_RESPONSE_HEADERS = {"Location", "Retry-After"}


class HttpError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        summary: str,
        *,
        retryable: bool = False,
        operation_id: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.summary = summary
        self.retryable = retryable
        self.operation_id = operation_id
        super().__init__(summary)


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    path_parameters: Mapping[str, str]
    query: Mapping[str, tuple[str, ...]]
    headers: Mapping[str, str]
    body: object | None

    def idempotency_key(self) -> str:
        value = self.headers.get("idempotency-key")
        if value is None or _IDEMPOTENCY_KEY.fullmatch(value) is None:
            raise HttpError(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key is required")
        return value


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, object] | tuple[object, ...] | list[object]
    headers: Mapping[str, str] | None = None


Handler = Callable[[Request], Response]


@dataclass(frozen=True, slots=True)
class _Route:
    method: str
    pattern: re.Pattern[str]
    parameter_names: tuple[str, ...]
    handler: Handler


class Router:
    def __init__(self) -> None:
        self._routes: list[_Route] = []

    def add(self, method: str, path: str, handler: Handler) -> None:
        names = tuple(_PATH_PARAMETER.findall(path))
        expression = _PATH_PARAMETER.sub(r"(?P<\1>[^/]+)", re.escape(path))
        # re.escape also escapes braces, so substitute against the escaped form.
        for name in names:
            expression = expression.replace(
                re.escape("{" + name + "}"),
                f"(?P<{name}>[^/]+)",
            )
        pattern = re.compile(f"^{expression}$")
        normalized_method = method.upper()
        if any(
            item.method == normalized_method and item.pattern.pattern == pattern.pattern
            for item in self._routes
        ):
            raise ValueError("duplicate controller route")
        self._routes.append(_Route(normalized_method, pattern, names, handler))

    def dispatch(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: object | None,
    ) -> Response:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/v1/"):
            raise HttpError(404, "NOT_FOUND", "controller route does not exist")
        try:
            query = {
                key: tuple(values)
                for key, values in parse_qs(
                    parsed.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=32,
                ).items()
            }
        except ValueError:
            raise HttpError(400, "INVALID_QUERY", "controller query is malformed") from None
        path_matched = False
        for route in self._routes:
            match = route.pattern.fullmatch(parsed.path)
            if match is None:
                continue
            path_matched = True
            if route.method != method.upper():
                continue
            request = Request(
                method=route.method,
                path=parsed.path,
                path_parameters=match.groupdict(),
                query=query,
                headers={key.lower(): value for key, value in headers.items()},
                body=body,
            )
            return route.handler(request)
        if path_matched:
            raise HttpError(405, "METHOD_NOT_ALLOWED", "controller method is not allowed")
        raise HttpError(404, "NOT_FOUND", "controller route does not exist")


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class ControllerServer(_ThreadingUnixServer):
    def __init__(self, socket_path: str, router: Router) -> None:
        self.router = router
        super().__init__(socket_path, ControllerRequestHandler)


class ControllerRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "openstack-platform-controller"
    sys_version = ""

    @property
    def controller_server(self) -> ControllerServer:
        server = self.server
        if not isinstance(server, ControllerServer):
            raise RuntimeError("controller handler has an invalid server")
        return server

    def log_message(self, _format: str, *_args: object) -> None:
        # Access logging belongs to the service wrapper and must not accidentally
        # include request bodies containing environment values.
        return

    def _body(self) -> object | None:
        transfer = self.headers.get("Transfer-Encoding")
        if transfer is not None:
            raise HttpError(400, "INVALID_REQUEST", "chunked request bodies are unsupported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            raise HttpError(400, "INVALID_REQUEST", "Content-Length must not be repeated")
        idempotency_keys = self.headers.get_all("Idempotency-Key", [])
        if len(idempotency_keys) > 1:
            raise HttpError(400, "INVALID_REQUEST", "Idempotency-Key must not be repeated")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except ValueError:
            raise HttpError(400, "INVALID_REQUEST", "Content-Length is malformed") from None
        if length < 0 or length > _MAX_BODY:
            raise HttpError(413, "REQUEST_TOO_LARGE", "controller request exceeds its size limit")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise HttpError(400, "INVALID_REQUEST", "controller request body was incomplete")
        if not raw:
            return None
        if self.headers.get_content_type() != "application/json":
            raise HttpError(415, "UNSUPPORTED_MEDIA_TYPE", "controller requests must use JSON")

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        def constant(_value: str) -> None:
            raise ValueError("non-finite number")

        try:
            return json.loads(
                raw,
                object_pairs_hook=pairs,
                parse_constant=constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise HttpError(400, "INVALID_JSON", "controller request must be strict JSON") from None

    def _write(self, response: Response, correlation_id: str) -> None:
        payload = json.dumps(
            response.body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
        if len(payload) > _MAX_RESPONSE:
            raise HttpError(500, "RESPONSE_TOO_LARGE", "controller response exceeded its limit")
        extra_headers = response.headers or {}
        if any(
            key not in _RESPONSE_HEADERS
            or not isinstance(value, str)
            or not value
            or "\r" in value
            or "\n" in value
            or len(value) > 1_024
            for key, value in extra_headers.items()
        ):
            raise HttpError(500, "INVALID_RESPONSE", "controller response headers were invalid")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Correlation-ID", correlation_id)
        for key, value in extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, error: HttpError, correlation_id: str) -> None:
        detail: dict[str, object] = {
            "code": error.code,
            "summary": error.summary[:1024],
            "correlationId": correlation_id,
            "retryable": error.retryable,
        }
        if error.operation_id is not None:
            detail["operationId"] = error.operation_id
        body: dict[str, object] = {"error": detail}
        self._write(Response(error.status, body), correlation_id)

    def _handle(self) -> None:
        correlation_id = str(uuid_module.uuid4())
        try:
            body = self._body()
            response = self.controller_server.router.dispatch(
                self.command,
                self.path,
                dict(self.headers.items()),
                body,
            )
            self._write(response, correlation_id)
        except HttpError as error:
            self._error(error, correlation_id)
        except Exception:
            self._error(
                HttpError(500, "INTERNAL_ERROR", "unexpected controller failure"),
                correlation_id,
            )

    do_GET = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
