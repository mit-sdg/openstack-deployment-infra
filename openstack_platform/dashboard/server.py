"""Private HTTP transport for the read-only operator dashboard.

Production serves only a mode-0600 Unix socket inside a private operator
directory and admits only peers with the server's own UID, so access requires
an SSH session as the operator (for example, OpenSSH local forwarding to the
socket). There are no mutation routes: ``POST /api/refresh`` only wakes the
single refresh loop early. Responses carry a strict same-origin content policy,
and every request must name a loopback ``Host`` to defeat DNS rebinding.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import socketserver
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import FrameType
from typing import Any, TextIO, cast
from urllib.parse import urlsplit

from .. import remote, runtime
from ..config import Config
from ..controller.http import linux_peer_credentials, prepare_socket_path
from . import sources
from .service import Collector, DashboardService, pending_snapshot

STATIC = Path(__file__).with_name("static")
MAXIMUM_CONNECTIONS = 32
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/assets/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/assets/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/assets/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
_LOOPBACK_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?")
_RELEASE = re.compile(r"[0-9a-f]{40}")
SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=()"),
)


@dataclass(frozen=True, slots=True)
class Asset:
    body: bytes
    content_type: str
    etag: str


def _etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def load_assets(directory: Path = STATIC) -> dict[str, Asset]:
    """Read the fixed static allowlist once; nothing else is served from disk."""
    assets: dict[str, Asset] = {}
    for route, (name, content_type) in _ASSETS.items():
        body = (directory / name).read_bytes()
        assets[route] = Asset(body, content_type, _etag(body))
    return assets


def release_commit(module: Path = Path(__file__)) -> str | None:
    """Return the commit-addressed release directory serving this code, if any."""
    return next(
        (parent.name for parent in module.resolve().parents if _RELEASE.fullmatch(parent.name)),
        None,
    )


class _DashboardServer(socketserver.ThreadingMixIn):
    """Shared state and a hard bound on concurrently served connections."""

    daemon_threads = True
    block_on_close = False
    service: DashboardService
    assets: dict[str, Asset]
    _slots: threading.BoundedSemaphore

    def _attach(self, service: DashboardService, assets: dict[str, Asset]) -> None:
        self.service = service
        self.assets = assets
        self._slots = threading.BoundedSemaphore(MAXIMUM_CONNECTIONS)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            cast(socketserver.BaseServer, self).shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class UnixDashboardServer(_DashboardServer, socketserver.UnixStreamServer):
    """Mode-0600 Unix socket admitting only the operator's own UID."""

    def __init__(
        self,
        socket_path: str,
        service: DashboardService,
        assets: dict[str, Asset],
        *,
        peer_credentials: Callable[[socket.socket], tuple[int, int]] = linux_peer_credentials,
    ) -> None:
        if len(socket_path.encode()) > 100:
            raise ValueError("dashboard socket path is too long for a Unix socket")
        prepare_socket_path(socket_path)
        self._attach(service, assets)
        self._peer_credentials = peer_credentials
        socketserver.UnixStreamServer.__init__(self, socket_path, DashboardHandler)
        os.chmod(socket_path, 0o600, follow_symlinks=False)

    def verify_request(self, request: Any, client_address: Any) -> bool:
        try:
            uid, _gid = self._peer_credentials(request)
        except (OSError, ValueError):
            return False
        return uid == os.geteuid()

    def server_close(self) -> None:
        path = self.server_address
        super().server_close()
        if isinstance(path, str):
            try:
                metadata = os.lstat(path)
            except FileNotFoundError:
                return
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
                os.unlink(path)


class LoopbackDashboardServer(_DashboardServer, socketserver.TCPServer):
    """Loopback TCP listener used only by the fixture-backed preview."""

    allow_reuse_address = True

    def __init__(self, port: int, service: DashboardService, assets: dict[str, Asset]) -> None:
        self._attach(service, assets)
        socketserver.TCPServer.__init__(self, ("127.0.0.1", port), DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "openstack-platform-dashboard"
    sys_version = ""
    timeout = 15

    @property
    def dashboard(self) -> _DashboardServer:
        return cast(_DashboardServer, self.server)

    def log_message(self, _format: str, *_args: object) -> None:
        # Access logging is deliberately absent; the dashboard has no audit role.
        return

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        del message, explain
        self.close_connection = True
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.1"
        status = 405 if code == 501 else code
        self._respond(status, _plain(status))

    def _respond(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
        *,
        cache: str = "no-store",
        etag: str | None = None,
        head: bool = False,
    ) -> None:
        self.send_response(status)
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Cache-Control", cache)
        if etag is not None:
            self.send_header("ETag", etag)
        if status == 304:
            self.end_headers()
            return
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _admitted(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or not _LOOPBACK_HOST.fullmatch(hosts[0]):
            self.close_connection = True
            self._respond(421, b"The dashboard only answers loopback Host names.\n")
            return False
        length = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or length not in ([], ["0"]):
            self.close_connection = True
            self._respond(413, b"The dashboard does not accept request bodies.\n")
            return False
        return True

    def _read(self, head: bool) -> None:
        if not self._admitted():
            return
        path = urlsplit(self.path).path
        if path == "/api/snapshot":
            body = self.dashboard.service.document()
            etag = _etag(body)
            if self.headers.get("If-None-Match") == etag:
                self._respond(304, b"", etag=etag)
                return
            self._respond(200, body, "application/json; charset=utf-8", etag=etag, head=head)
            return
        asset = self.dashboard.assets.get("/" if path == "/index.html" else path)
        if asset is None:
            self._respond(404, _plain(404), head=head)
            return
        if self.headers.get("If-None-Match") == asset.etag:
            self._respond(304, b"", cache="no-cache", etag=asset.etag)
            return
        self._respond(
            200, asset.body, asset.content_type, cache="no-cache", etag=asset.etag, head=head
        )

    def do_GET(self) -> None:
        self._read(head=False)

    def do_HEAD(self) -> None:
        self._read(head=True)

    def do_POST(self) -> None:
        if not self._admitted():
            return
        if urlsplit(self.path).path != "/api/refresh":
            self._respond(404, _plain(404))
            return
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        # A custom header forces a CORS preflight that this server never grants,
        # so a cross-site page cannot even wake the refresh loop.
        if (
            self.headers.get("X-Dashboard-Refresh") != "1"
            or (origin is not None and origin != f"http://{host}")
            or site not in (None, "same-origin")
        ):
            self._respond(403, b"Refresh requests must come from the dashboard itself.\n")
            return
        accepted = self.dashboard.service.request_refresh()
        body = json.dumps({"accepted": accepted}).encode()
        self._respond(202, body, "application/json; charset=utf-8")


def _plain(status: int) -> bytes:
    reasons = {404: "Not found", 405: "Method not allowed"}
    return f"{reasons.get(status, 'Request rejected')}.\n".encode()


def serve(server: socketserver.BaseServer, service: DashboardService) -> None:
    """Run the refresh loop and HTTP server until SIGINT or SIGTERM."""

    def terminate(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    service.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        service.stop()
        server.server_close()


def run_operator_dashboard(
    config: Config,
    *,
    read_operator: Callable[[], sources.OperatorReads],
    socket_path: Path,
    interval_seconds: float,
    output: TextIO,
    ssh_config: str | Path = remote.DEFAULT_SSH_CONFIG,
) -> None:
    """Serve live operator evidence on the private Unix socket."""
    release = release_commit()
    platform = config.platform
    collector = Collector(
        platform,
        read_admin=lambda: sources.read_admin(platform, ssh_config=ssh_config),
        read_operator=read_operator,
        probe_timeout_seconds=min(10, config.policy.limits.http_seconds),
        release=release,
    )
    service = DashboardService(
        collector.collect,
        pending_snapshot(platform, release),
        interval_seconds=interval_seconds,
    )
    socket_path = socket_path.absolute()
    runtime.ensure_private_directory(socket_path.parent)
    server = UnixDashboardServer(str(socket_path), service, load_assets())
    print(f"dashboard=listening socket={socket_path}", file=output, flush=True)
    print(
        "From your workstation: "
        f"ssh -N -L 127.0.0.1:8470:{socket_path} <operator-host>, "
        "then open http://localhost:8470",
        file=output,
        flush=True,
    )
    serve(server, service)
