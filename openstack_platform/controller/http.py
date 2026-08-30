"""Bounded JSON HTTP transport for the local controller Unix socket."""

from __future__ import annotations

import errno
import json
import os
import re
import select
import socket
import socketserver
import stat
import struct
import threading
import uuid as uuid_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlsplit

_MAX_BODY = 1_048_576
_MAX_RESPONSE = 1_048_576
_PATH_PARAMETER = re.compile(r"\{([a-z][a-zA-Z0-9]*)\}")
_IDEMPOTENCY_KEY = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_RESPONSE_HEADERS = {"Location", "Retry-After"}


@dataclass(frozen=True, slots=True)
class TransportLimits:
    """Fixed resource bounds for the local HTTP transport."""

    global_connections: int = 64
    peer_connections: int = 16
    header_seconds: float = 5.0
    body_seconds: float = 30.0
    idle_seconds: float = 15.0
    write_seconds: float = 5.0
    requests_per_connection: int = 100

    def __post_init__(self) -> None:
        values = (
            self.global_connections,
            self.peer_connections,
            self.header_seconds,
            self.body_seconds,
            self.idle_seconds,
            self.write_seconds,
            self.requests_per_connection,
        )
        if any(value <= 0 for value in values):
            raise ValueError("controller transport limits must be positive")
        if self.peer_connections > self.global_connections:
            raise ValueError("per-peer connection limit cannot exceed global limit")


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
            raise HttpError(
                400,
                "INVALID_IDEMPOTENCY_KEY",
                "Idempotency-Key must be a canonical UUID",
            )
        return value


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, object] | tuple[object, ...] | list[object]
    headers: Mapping[str, str] | None = None


Handler = Callable[[Request], Response]
PeerCredentialsReader = Callable[[socket.socket], tuple[int, int]]
_BaseRequest = socket.socket | tuple[bytes, socket.socket]


@dataclass(frozen=True, slots=True)
class PeerPolicy:
    """Exact Linux process identities permitted to use one controller socket."""

    allowed: frozenset[tuple[int, int]]
    max_connections_per_peer: int = 8

    def __post_init__(self) -> None:
        if not self.allowed or any(uid < 0 or gid < 0 for uid, gid in self.allowed):
            raise ValueError("controller peer allowlist must contain valid UID/GID pairs")
        if self.max_connections_per_peer < 1 or self.max_connections_per_peer > 128:
            raise ValueError("controller per-peer connection limit is invalid")


def linux_peer_credentials(connection: socket.socket) -> tuple[int, int]:
    """Return (uid, gid), failing closed when Linux SO_PEERCRED is unavailable."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise OSError("SO_PEERCRED is unavailable")
    raw = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    if len(raw) != struct.calcsize("3i"):
        raise OSError("SO_PEERCRED returned malformed credentials")
    _pid, uid, gid = struct.unpack("3i", raw)
    if uid < 0 or gid < 0:
        raise OSError("SO_PEERCRED returned invalid credentials")
    return uid, gid


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
    # Connections are explicitly closed during shutdown. Daemon workers and a
    # non-blocking close are a final safeguard against interpreter hangs.
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 64


def prepare_socket_path(socket_path: str) -> None:
    """Remove only an owned, inactive Unix socket; reject every other object."""
    path = os.path.abspath(socket_path)
    if path != socket_path or len(path.encode()) > 4_096:
        raise ValueError("controller socket path must be canonical and absolute")
    parent = os.path.dirname(path)
    metadata = os.lstat(parent)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o007
    ):
        raise PermissionError("controller socket directory must be owned and private")
    try:
        socket_metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(socket_metadata.st_mode) or socket_metadata.st_uid != os.geteuid():
        raise FileExistsError("controller socket path is not an owned Unix socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
    except OSError as error:
        if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise OSError("controller socket activity could not be determined") from error
    else:
        raise OSError(errno.EADDRINUSE, "controller socket is already active", path)
    finally:
        probe.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class ControllerServer(_ThreadingUnixServer):
    def __init__(
        self,
        socket_path: str,
        router: Router,
        *,
        peer_policy: PeerPolicy | None = None,
        peer_credentials: PeerCredentialsReader = linux_peer_credentials,
        socket_mode: int = 0o660,
        socket_gid: int | None = None,
        limits: TransportLimits | None = None,
    ) -> None:
        if socket_mode != 0o660:
            raise ValueError("controller socket mode must be 0660")
        prepare_socket_path(socket_path)
        self.router = router
        self.peer_policy = peer_policy or PeerPolicy(frozenset({(os.geteuid(), os.getegid())}))
        self._peer_credentials = peer_credentials
        self.limits = limits or TransportLimits()
        self._connections_lock = threading.Lock()
        self._connections: dict[socket.socket, tuple[int, int]] = {}
        self._peer_connections: dict[tuple[int, int], int] = {}
        self._stopping = threading.Event()
        try:
            super().__init__(socket_path, ControllerRequestHandler)
            os.chmod(socket_path, socket_mode, follow_symlinks=False)
            if socket_gid is not None:
                os.chown(socket_path, -1, socket_gid, follow_symlinks=False)
        except BaseException:
            try:
                socketserver.UnixStreamServer.server_close(self)
            except AttributeError:
                pass
            try:
                if stat.S_ISSOCK(os.lstat(socket_path).st_mode):
                    os.unlink(socket_path)
            except FileNotFoundError:
                pass
            raise

    @property
    def active_connections(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    def process_request(self, request: _BaseRequest, client_address: object) -> None:
        """Authenticate and admit a bounded number of Unix-socket workers."""
        if not isinstance(request, socket.socket):
            raise TypeError("controller received a non-Unix socket request")
        try:
            peer = self._peer_credentials(request)
        except (OSError, ValueError, struct.error):
            self._reject(request, "PEER_CREDENTIALS_UNAVAILABLE")
            return
        if peer not in self.peer_policy.allowed:
            self._reject(request, "PEER_IDENTITY_REJECTED", retryable=False)
            return
        admitted = False
        with self._connections_lock:
            peer_count = self._peer_connections.get(peer, 0)
            peer_limit = min(
                self.limits.peer_connections,
                self.peer_policy.max_connections_per_peer,
            )
            if (
                not self._stopping.is_set()
                and len(self._connections) < self.limits.global_connections
                and peer_count < peer_limit
            ):
                self._connections[request] = peer
                self._peer_connections[peer] = peer_count + 1
                admitted = True
        if not admitted:
            self._reject(request, "CONNECTION_LIMIT")
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            request.close()
            raise

    def _reject(self, request: socket.socket, code: str, *, retryable: bool = True) -> None:
        correlation_id = str(uuid_module.uuid4())
        payload = json.dumps(
            {
                "error": {
                    "code": code,
                    "correlationId": correlation_id,
                    "retryable": retryable,
                    "summary": "controller connection is not authorized or capacity is unavailable",
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"Cache-Control: no-store\r\nConnection: close\r\nRetry-After: 1\r\n"
            + f"X-Correlation-ID: {correlation_id}\r\n\r\n".encode()
            + payload
        )
        try:
            request.settimeout(0.25)
            request.sendall(response)
        except OSError:
            pass
        finally:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()

    def _release(self, request: socket.socket) -> None:
        with self._connections_lock:
            peer = self._connections.pop(request, None)
            if peer is None:
                return
            remaining = self._peer_connections[peer] - 1
            if remaining:
                self._peer_connections[peer] = remaining
            else:
                del self._peer_connections[peer]

    def shutdown_request(self, request: _BaseRequest) -> None:
        try:
            super().shutdown_request(request)
        finally:
            if isinstance(request, socket.socket):
                self._release(request)

    def _close_connections(self) -> None:
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def shutdown(self) -> None:
        self._stopping.set()
        super().shutdown()
        self._close_connections()

    def server_close(self) -> None:
        socket_path = self.server_address
        self._stopping.set()
        self._close_connections()
        super().server_close()
        if isinstance(socket_path, str):
            try:
                metadata = os.lstat(socket_path)
                if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
                    os.unlink(socket_path)
            except FileNotFoundError:
                pass


class ControllerRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "openstack-platform-controller"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self._request_count = 0
        self._deadline_lock = threading.Lock()
        self._deadline_generation = 0
        self._deadline_timer: threading.Timer | None = None
        self._deadline_expired: str | None = None

    def _arm_deadline(self, phase: str, seconds: float, *, close_write: bool) -> None:
        self._cancel_deadline()
        with self._deadline_lock:
            generation = self._deadline_generation

        def expire() -> None:
            with self._deadline_lock:
                if generation != self._deadline_generation:
                    return
                self._deadline_expired = phase
            try:
                self.connection.shutdown(socket.SHUT_RDWR if close_write else socket.SHUT_RD)
            except OSError:
                pass

        timer = threading.Timer(seconds, expire)
        timer.daemon = True
        self._deadline_timer = timer
        self.connection.settimeout(seconds)
        timer.start()

    def _cancel_deadline(self) -> None:
        timer = getattr(self, "_deadline_timer", None)
        with getattr(self, "_deadline_lock", threading.Lock()):
            self._deadline_generation = getattr(self, "_deadline_generation", 0) + 1
        if timer is not None:
            timer.cancel()
        self._deadline_timer = None

    def handle_one_request(self) -> None:
        limits = self.controller_server.limits
        if self._request_count:
            readable, _, _ = select.select([self.connection], [], [], limits.idle_seconds)
            if not readable:
                self.close_connection = True
                return
        self._request_count += 1
        self._deadline_expired = None
        self._arm_deadline("headers", limits.header_seconds, close_write=True)
        try:
            super().handle_one_request()
        except OSError:
            self.close_connection = True
        finally:
            self._cancel_deadline()
        if self._request_count >= limits.requests_per_connection:
            self.close_connection = True

    def finish(self) -> None:
        self._cancel_deadline()
        try:
            super().finish()
        except OSError:
            # Deadline and shutdown paths deliberately tear down the stream.
            pass

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

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace the standard library's HTML parser errors with bounded JSON."""
        del message, explain
        self.close_connection = True
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.1"
        status = 405 if code == 501 else code
        error_code = "METHOD_NOT_ALLOWED" if code == 501 else "INVALID_REQUEST"
        summary = (
            "controller method is not allowed"
            if code == 501
            else "controller HTTP request is malformed"
        )
        self._error(HttpError(status, error_code, summary), str(uuid_module.uuid4()))

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
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            raise HttpError(
                408, "REQUEST_TIMEOUT", "controller request body deadline expired"
            ) from None
        if len(raw) != length:
            if self._deadline_expired == "body":
                raise HttpError(408, "REQUEST_TIMEOUT", "controller request body deadline expired")
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
            parsed: object = json.loads(
                raw,
                object_pairs_hook=pairs,
                parse_constant=constant,
            )
            return parsed
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
        if self.close_connection:
            self.send_header("Connection", "close")
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
        self._cancel_deadline()
        if self._deadline_expired == "headers":
            self.close_connection = True
            return
        if self._request_count >= self.controller_server.limits.requests_per_connection:
            self.close_connection = True
        self._arm_deadline(
            "body",
            self.controller_server.limits.body_seconds,
            close_write=False,
        )
        try:
            body = self._body()
            self._cancel_deadline()
            self.connection.settimeout(self.controller_server.limits.write_seconds)
            response = self.controller_server.router.dispatch(
                self.command,
                self.path,
                dict(self.headers.items()),
                body,
            )
            self._write(response, correlation_id)
        except HttpError as error:
            # Framing failures can leave unread secret-bearing bytes in the
            # stream. Never parse them as a pipelined request.
            self.close_connection = True
            self._error(error, correlation_id)
        except Exception:
            self.close_connection = True
            self._error(
                HttpError(500, "INTERNAL_ERROR", "unexpected controller failure"),
                correlation_id,
            )
        finally:
            self._cancel_deadline()

    do_GET = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
