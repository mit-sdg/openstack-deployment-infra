"""Bounded HTTP server for static assets and typed broker/auth responses."""

from __future__ import annotations

import http.client
import http.server
import io
import ipaddress
import mimetypes
import re
import socket
import socketserver
import ssl
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..broker.client import ControllerUnavailable, ProjectClient
from ..common import MANAGEMENT_REQUESTS, canonical, strict_json
from ..config import Config


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: tuple[tuple[str, str], ...] = ()


class WebServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 32

    def __init__(
        self,
        address: tuple[str, int],
        config: Config,
        assets: Path,
        *,
        tls: ssl.SSLContext | None = None,
        capacity: int = MANAGEMENT_REQUESTS,
        deadline: float = 5,
    ) -> None:
        self.config, self.assets, self.tls = config, assets.resolve(), tls
        self.capacity, self.deadline = capacity, deadline
        self.clients: set[socket.socket] = set()
        self.client_lock = threading.Lock()
        self.broker = ProjectClient(config.broker_socket, timeout=15, capacity=MANAGEMENT_REQUESTS)
        self.allowed_assets = {
            "/" + str(p.relative_to(self.assets))
            for p in self.assets.rglob("*")
            if p.is_file()
            and not p.is_symlink()
            and p.resolve().is_relative_to(self.assets)
            and not any(part.startswith(".") for part in p.relative_to(self.assets).parts)
        }
        super().__init__(address, WebHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        client, address = super().get_request()
        if self.tls:
            client = self.tls.wrap_socket(client, server_side=True, do_handshake_on_connect=False)
        return client, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Exceptions at an HTTP boundary must not dump request-local credentials.
        del request, client_address

    def process_request(self, request: Any, client_address: Any) -> None:
        address = str(ipaddress.ip_address(client_address[0]))
        allowed = (
            ipaddress.ip_address(address).is_loopback
            if self.config.development
            else address in self.config.trusted_ingress_peers
        )
        if not allowed:
            request.close()
            return
        with self.client_lock:
            if len(self.clients) >= self.capacity:
                request.close()
                return
            self.clients.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self.client_lock:
                self.clients.discard(request)
            request.close()
            raise

    def shutdown_request(self, request: Any) -> None:
        with self.client_lock:
            self.clients.discard(request)
        super().shutdown_request(request)

    def shutdown(self) -> None:
        with self.client_lock:
            for client in self.clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        super().shutdown()

    def handle(self, method: str, target: str, headers: dict[str, str], raw: bytes) -> Reply:
        parsed = urlsplit(target)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or "%" in parsed.path
            or "\\" in parsed.path
            or ".." in parsed.path
        ):
            return error_reply(400, "INVALID_REQUEST")
        if headers.get("host") != urlsplit(self.config.portal_origin).netloc:
            return error_reply(400, "INVALID_HOST")
        if parsed.path == "/healthz" and method == "GET":
            return Reply(200, b'{"ready":true}')
        if parsed.path.startswith(("/api/", "/auth/")):
            return self.forward(method, parsed.path, parsed.query, headers, raw)
        if method != "GET":
            return error_reply(405, "METHOD_NOT_ALLOWED")
        path = parsed.path
        if path in self.allowed_assets:
            file = self.assets / path.lstrip("/")
            if (
                not file.resolve().is_relative_to(self.assets)
                or file.is_symlink()
                or file.stat().st_size > 1048576
            ):
                return error_reply(404, "NOT_FOUND")
            content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            cache = (
                "public, max-age=31536000, immutable" if path.startswith("/assets/") else "no-store"
            )
            return Reply(200, file.read_bytes(), content_type, (("Cache-Control", cache),))
        if re.fullmatch(
            r"/(?:|sign-in|apps(?:/new|/[a-f0-9-]{36}(?:/(?:configuration|deploy|deployments(?:/[a-f0-9-]{36})?))?)?)",
            path,
        ):
            index = self.assets / "index.html"
            if index.is_file() and index.stat().st_size <= 1048576:
                return Reply(200, index.read_bytes(), "text/html; charset=utf-8")
        return error_reply(404, "NOT_FOUND")

    def forward(
        self, method: str, path: str, query: str, headers: dict[str, str], raw: bytes
    ) -> Reply:
        if path.startswith("/auth/"):
            if path not in {"/auth/options", "/auth/login"}:
                return error_reply(404, "NOT_FOUND")
            target = "/v1" + path
        elif re.fullmatch(
            r"/api/v1/(?:session|logout|apps(?:/[a-f0-9-]{36}(?:/configuration|/deployments(?:/[a-f0-9-]{36}(?:/build-log)?)?)?)?|intents(?:/[a-f0-9-]{36}(?:/resume)?)?)",
            path,
        ):
            target = path.removeprefix("/api")
        else:
            return error_reply(404, "NOT_FOUND")
        body: object = None
        try:
            if raw:
                content_type = headers.get("content-type", "")
                if content_type != "application/json" or (
                    path == "/auth/login" and len(raw) > 4096
                ):
                    return error_reply(415, "UNSUPPORTED_MEDIA_TYPE")
                body = strict_json(raw)
            if method == "GET" and raw:
                return error_reply(400, "INVALID_REQUEST")
            forwarded = {
                key: value
                for key, value in headers.items()
                if key
                in {
                    "cookie",
                    "origin",
                    "x-csrf-token",
                    "idempotency-key",
                    "sec-fetch-site",
                    "sec-fetch-mode",
                }
            }
            # This internal context is never copied from a browser header.
            # Production admits only configured ingress peers, whose dedicated
            # forwarding header must contain one overwritten canonical IP.
            if self.config.development:
                address = headers.get("_peer_address", "127.0.0.1")
            else:
                if headers.get("_peer_address") not in self.config.trusted_ingress_peers:
                    return error_reply(403, "INGRESS_PEER_REJECTED")
                if self.config.client_address_header != "cf-connecting-ip":
                    return error_reply(400, "INVALID_CLIENT_ADDRESS_CONFIG")
                address = headers.get(self.config.client_address_header, "")
            try:
                forwarded["x-portal-client-address"] = str(ipaddress.ip_address(address))
            except ValueError:
                return error_reply(400, "INVALID_CLIENT_ADDRESS")
            status, value = self.broker.request(
                method, target + ("?" + query if query else ""), body, headers=forwarded
            )
            if status == 401:
                return Reply(
                    status,
                    canonical(value).encode(),
                    headers=(
                        ("Set-Cookie", self.cookie({"name": "session", "value": "", "maxAge": 0})),
                    ),
                )
            browser = value.pop("browser", {})
            if not isinstance(browser, dict) or set(browser) - {"cookies", "status"}:
                raise ValueError("invalid broker directive")
            extra: list[tuple[str, str]] = [
                ("Set-Cookie", self.cookie(cookie)) for cookie in browser.get("cookies", [])
            ]
            if path == "/auth/login" and status == 200:
                returned = value.get("data", {}).get("returnPath")
                if not isinstance(returned, str) or not re.fullmatch(
                    r"/(?:apps(?:/[a-z0-9/-]+)?|activity)", returned
                ):
                    raise ValueError("invalid sign-in return path")
            if browser.get("status") == 204:
                return Reply(204, b"", headers=tuple(extra))
            return Reply(status, canonical(value).encode(), headers=tuple(extra))
        except (ValueError, UnicodeError):
            return error_reply(400, "INVALID_REQUEST")
        except ControllerUnavailable:
            return error_reply(503, "BROKER_UNAVAILABLE")

    def cookie(self, directive: Any) -> str:
        if not isinstance(directive, dict) or set(directive) != {"name", "value", "maxAge"}:
            raise ValueError("invalid cookie directive")
        names = {"login": self.config.login_cookie, "session": self.config.session_cookie}
        value, age = directive["value"], directive["maxAge"]
        if (
            directive["name"] not in names
            or not isinstance(value, str)
            or (
                value
                and not re.fullmatch(
                    r"[A-Za-z0-9_-]{43}"
                    if directive["name"] == "session"
                    else r"[A-Za-z0-9_.-]{1,160}",
                    value,
                )
            )
            or type(age) is not int
            or not 0 <= age <= 86400
        ):
            raise ValueError("invalid cookie directive")
        secure = "; Secure" if self.config.portal_origin.startswith("https:") else ""
        same_site = "Strict" if directive["name"] == "login" else "Lax"
        return f"{names[directive['name']]}={value}; Path=/; Max-Age={age}{secure}; HttpOnly; SameSite={same_site}"

    def csp(self) -> str:
        return "default-src 'none'; script-src 'self'; script-src-attr 'none'; style-src 'self'; style-src-attr 'none'; img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"


def error_reply(status: int, code: str) -> Reply:
    return Reply(
        status,
        canonical(
            {
                "error": {
                    "code": code,
                    "summary": "The request could not be completed.",
                    "correlationId": str(uuid.uuid4()),
                    "retryable": status == 503,
                }
            }
        ).encode(),
    )


class WebHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "management-web"

    def setup(self) -> None:
        self.timer: threading.Timer | None = None
        self.request_count = 0
        self.request.settimeout(self.web.deadline)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()
        assert isinstance(self.rfile, io.BufferedReader)
        self.rfile = HeaderReader(self.rfile.detach())

    @property
    def web(self) -> WebServer:
        assert isinstance(self.server, WebServer)
        return self.server

    def arm(self, seconds: float) -> None:
        self.cancel_timer()

        def close() -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        self.timer = threading.Timer(seconds, close)
        self.timer.daemon = True
        self.timer.start()

    def cancel_timer(self) -> None:
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def handle_one_request(self) -> None:
        assert isinstance(self.rfile, HeaderReader)
        self.rfile.remaining = 16384
        self.arm(self.web.deadline if not self.request_count else 15)
        try:
            super().handle_one_request()
            self.request_count += 1
            if self.request_count >= 100:
                self.close_connection = True
        except http.client.LineTooLong:
            self.request_version = "HTTP/1.1"
            self.close_connection = True
            self.write_reply(error_reply(431, "HEADERS_TOO_LARGE"))
        finally:
            self.cancel_timer()

    def parse_request(self) -> bool:
        self.arm(self.web.deadline)
        return super().parse_request()

    def finish(self) -> None:
        self.cancel_timer()
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        del message, explain
        self.close_connection = True
        self.write_reply(error_reply(405 if code == 501 else code, "INVALID_REQUEST"))

    def dispatch(self) -> None:
        try:
            if (
                sum(len(k) + len(v) for k, v in self.headers.items()) > 16384
                or len(self.headers) > 64
            ):
                self.close_connection = True
                return self.write_reply(error_reply(431, "HEADERS_TOO_LARGE"))
            for name in (
                "Host",
                "Content-Length",
                "Idempotency-Key",
                "Cookie",
                "Origin",
                "X-CSRF-Token",
                "Content-Type",
            ):
                if len(self.headers.get_all(name, [])) > 1:
                    self.close_connection = True
                    return self.write_reply(error_reply(400, "INVALID_REQUEST"))
            length = self.headers.get("Content-Length", "0")
            if self.headers.get("Transfer-Encoding") or not length.isdigit():
                self.close_connection = True
                return self.write_reply(error_reply(400, "INVALID_REQUEST"))
            if int(length) > 1048576:
                self.close_connection = True
                return self.write_reply(error_reply(413, "REQUEST_TOO_LARGE"))
            self.arm(30)
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                self.close_connection = True
                return
            self.cancel_timer()
            metadata = {k.lower(): v for k, v in self.headers.items()}
            metadata["_peer_address"] = str(ipaddress.ip_address(self.client_address[0]))
            reply = self.web.handle(self.command, self.path, metadata, raw)
            self.write_reply(reply)
        except (OSError, ValueError):
            self.close_connection = True

    def write_reply(self, reply: Reply) -> None:
        if len(reply.body) > 1048576:
            reply = error_reply(500, "RESPONSE_TOO_LARGE")
        self.arm(self.web.deadline)
        self.connection.settimeout(self.web.deadline)
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        self.send_header("Content-Security-Policy", self.web.csp())
        self.send_header("X-Content-Type-Options", "nosniff")
        if not any(key.lower() == "referrer-policy" for key, _value in reply.headers):
            self.send_header(
                "Referrer-Policy",
                "strict-origin" if reply.content_type.startswith("text/html") else "no-referrer",
            )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("X-Correlation-ID", str(uuid.uuid4()))
        if not any(key == "Cache-Control" for key, _value in reply.headers):
            self.send_header("Cache-Control", "no-store")
        for key, value in reply.headers:
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if reply.body:
            self.wfile.write(reply.body)
        self.cancel_timer()

    do_GET = dispatch
    do_POST = dispatch
    do_PUT = dispatch


class HeaderReader(io.BufferedReader):
    """Bound header bytes during parsing, before the standard parser allocates them."""

    remaining = 16384

    def readline(self, size: int | None = -1) -> bytes:
        size = -1 if size is None else size
        allowed = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        line = super().readline(allowed)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise http.client.LineTooLong("management header limit")
        return line
