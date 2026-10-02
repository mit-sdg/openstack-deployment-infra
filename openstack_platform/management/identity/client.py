"""Bounded fixed-origin HTTPS implementation of Commons bb78c5e authentication."""

from __future__ import annotations

import http.client
import json
import queue
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..common import strict_json
from ..config import development_socket_path, management_peer, origin, socket_path_length

# A class of 200 signing in over a minute averages fewer than four requests/s.
# 64 outbound exchanges leave headroom for bursts; 128 local requests allow a
# bounded waiting queue and stay within the Unix transport's peer-policy ceiling.
IDENTITY_CONNECTIONS = 64


def credentials(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"username", "password"}:
        raise ValueError("invalid credential fields")
    for name, maximum in (("username", 32), ("password", 128)):
        if (
            not isinstance(value[name], str)
            or len(value[name].encode("utf-16-le", errors="surrogatepass")) // 2 > maximum
        ):
            raise ValueError("invalid credential lengths")
    return value


@dataclass(frozen=True)
class IdentityConfig:
    commons_origin: str
    socket: Path
    development: bool = False
    development_ca: Path | None = None
    connect_seconds: float = 3
    read_seconds: float = 5
    response_bytes: int = 8192
    broker_peer: tuple[int, int] = field(
        default_factory=lambda: management_peer("managementBroker")
    )

    @classmethod
    def load(cls, path: Path) -> IdentityConfig:
        import os

        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError("invalid identity configuration")
        value = strict_json(path.read_bytes())
        required = {"commonsOrigin", "socket", "development"}
        allowed = required | {"developmentCa", "connectSeconds", "readSeconds", "responseBytes"}
        if (
            not isinstance(value, dict)
            or not required <= set(value)
            or set(value) - allowed
            or type(value["development"]) is not bool
        ):
            raise ValueError("invalid identity configuration fields")
        development = value["development"]
        ca = value.get("developmentCa")
        if "developmentCa" in value and not development:
            raise ValueError("production identity must use system CAs")
        socket_path = Path(value["socket"])
        if (
            not socket_path.is_absolute()
            or str(socket_path) != os.path.normpath(socket_path)
            or socket_path.is_symlink()
        ):
            raise ValueError("identity socket must be a canonical direct path")
        socket_path_length(socket_path, "identity")
        if development and not development_socket_path(socket_path):
            raise ValueError("development sockets require .tmp or a private harness directory")
        ca_path = Path(ca) if ca is not None else None
        if ca_path and (
            ca_path.is_symlink()
            or not ca_path.resolve().is_relative_to((Path.cwd() / ".tmp").resolve())
            or ca_path.stat().st_size > 65536
        ):
            raise ValueError("development CA must be a bounded public file in .tmp")
        times = []
        for name, default in (("connectSeconds", 3), ("readSeconds", 5)):
            number = value.get(name, default)
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not 0.05 <= number <= 10
            ):
                raise ValueError("identity deadline is invalid")
            times.append(float(number))
        size = value.get("responseBytes", 8192)
        if type(size) is not int or not 1024 <= size <= 16384:
            raise ValueError("identity response cap is invalid")
        return cls(
            origin(value["commonsOrigin"], development=development, allow_http=False),
            socket_path,
            development,
            ca_path,
            times[0],
            times[1],
            size,
            (os.geteuid(), os.getegid()) if development else management_peer("managementBroker"),
        )


class CommonsClient:
    def __init__(self, config: IdentityConfig) -> None:
        self.config = config
        self.connect_capacity = threading.BoundedSemaphore(IDENTITY_CONNECTIONS)
        self.request_capacity = threading.BoundedSemaphore(IDENTITY_CONNECTIONS)
        if not config.commons_origin.startswith("https:") or urlsplit(config.commons_origin).path:
            raise ValueError("identity requires a fixed HTTPS origin")
        if config.development_ca is not None and not config.development:
            raise ValueError("production identity must use system CAs")
        if config.development:
            self.context = ssl.create_default_context()
        else:
            # Ambient SSL_CERT_FILE/SSL_CERT_DIR must not enable development
            # trust in a production process. These are the OS-managed bundles.
            system_ca = next(
                (
                    path
                    for path in (
                        Path("/etc/ssl/certs/ca-certificates.crt"),
                        Path("/etc/ssl/certs/ca-bundle.crt"),
                    )
                    if path.is_file()
                ),
                None,
            )
            if system_ca is None:
                raise ValueError("system CA bundle is unavailable")
            self.context = ssl.create_default_context(cafile=system_ca)
        if config.development_ca:
            self.context.load_verify_locations(cafile=config.development_ca)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def connect(self, deadline: float | None = None) -> http.client.HTTPSConnection | None:
        deadline = (
            deadline if deadline is not None else time.monotonic() + self.config.connect_seconds
        )
        if not self.connect_capacity.acquire(timeout=max(0, deadline - time.monotonic())):
            return None
        if time.monotonic() >= deadline:
            self.connect_capacity.release()
            return None
        parsed = urlsplit(self.config.commons_origin)
        if parsed.hostname is None:
            raise ValueError("invalid Commons hostname")
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=max(0.001, deadline - time.monotonic()),
            context=self.context,
        )
        result: queue.Queue[bool] = queue.Queue(maxsize=1)
        cancelled = threading.Event()

        def worker() -> None:
            try:
                connection.connect()
                if not cancelled.is_set():
                    result.put_nowait(True)
            except Exception:
                if not cancelled.is_set():
                    result.put_nowait(False)
            finally:
                if cancelled.is_set():
                    connection.close()
                self.connect_capacity.release()

        threading.Thread(target=worker, daemon=True).start()
        try:
            ok = result.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            cancelled.set()
            connection.close()
            return None
        if not ok:
            connection.close()
            return None
        return connection

    def authenticate(self, value: object) -> tuple[str | None, dict[str, str] | None]:
        try:
            checked = credentials(value)
        except ValueError:
            return "invalid_request", None
        deadline = time.monotonic() + self.config.connect_seconds
        if not self.request_capacity.acquire(timeout=max(0, deadline - time.monotonic())):
            return "unavailable", None
        try:
            return self.exchange(checked, deadline)
        finally:
            self.request_capacity.release()

    def exchange(
        self, checked: dict[str, str], deadline: float
    ) -> tuple[str | None, dict[str, str] | None]:
        connection = self.connect(deadline)
        if connection is None:
            return "unavailable", None
        sock = connection.sock
        if sock is None:
            connection.close()
            return "unavailable", None

        def expire() -> None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(self.config.read_seconds, expire)
        timer.daemon = True
        timer.start()
        try:
            sock.settimeout(self.config.read_seconds)
            connection.request(
                "POST",
                "/api/auth/authenticate",
                json.dumps(checked, ensure_ascii=True).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Cache-Control": "no-store",
                },
            )
            response = connection.getresponse()
            if (
                response.getheader("Content-Type", "").split(";")[0].strip().lower()
                != "application/json"
                or response.getheader("Content-Encoding", "identity") != "identity"
            ):
                return "unavailable", None
            length = response.getheader("Content-Length")
            if length is not None and (
                not length.isdecimal() or int(length) > self.config.response_bytes
            ):
                return "unavailable", None
            raw = response.read(self.config.response_bytes + 1)
            if len(raw) > self.config.response_bytes:
                return "unavailable", None
            body = strict_json(raw)
            if response.status in {400, 401, 403}:
                expected = {400: "INVALID_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN"}[
                    response.status
                ]
                if body != {"error": expected}:
                    return "unavailable", None
                return {
                    400: "invalid_request",
                    401: "invalid_credentials",
                    403: "account_disabled",
                }[response.status], None
            if (
                response.status != 200
                or not isinstance(body, dict)
                or set(body) != {"user", "username", "displayName", "email"}
            ):
                return "unavailable", None
            if any(
                not isinstance(body[name], str) or len(body[name]) > limit
                for name, limit in (
                    ("user", 36),
                    ("username", 32),
                    ("displayName", 256),
                    ("email", 320),
                )
            ):
                return "unavailable", None
            subject = str(uuid.UUID(body["user"]))
            if subject != body["user"] or body["username"] != checked["username"]:
                return "unavailable", None
            return None, {
                "subject": subject,
                "username": body["username"],
                "displayName": body["displayName"] or body["username"],
            }
        except Exception:
            return "unavailable", None
        finally:
            timer.cancel()
            connection.close()
