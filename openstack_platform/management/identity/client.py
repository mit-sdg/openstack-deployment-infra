"""Bounded fixed-origin HTTPS redemption of Commons Connect sign-in codes."""

from __future__ import annotations

import errno
import http.client
import json
import queue
import re
import socket
import ssl
import sys
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
# Commons issues "<voucher id>.<credential>"; clients treat it as opaque.
CONNECT_CODE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def describe(error: BaseException) -> str:
    """Name a failure for operators without echoing codes or response bodies."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return f"tls-verify:{error.verify_message}"
    if isinstance(error, ssl.SSLError):
        return f"tls:{error.reason or type(error).__name__}"
    if isinstance(error, socket.gaierror):
        return f"dns:{error.errno}"
    if isinstance(error, OSError) and error.errno is not None:
        return f"{type(error).__name__}:{errno.errorcode.get(error.errno, error.errno)}"
    return type(error).__name__


def unavailable(reason: str) -> tuple[str, None]:
    print(f"identity-check=unavailable reason={reason}", file=sys.stderr, flush=True)
    return "unavailable", None


def redemption(value: object, *, development: bool) -> dict[str, str]:
    """Accept one opaque code and the app origin it was issued to, nothing else."""
    if not isinstance(value, dict) or set(value) != {"code", "app"}:
        raise ValueError("invalid redeem fields")
    code, app = value["code"], value["app"]
    if not isinstance(code, str) or not CONNECT_CODE.fullmatch(code):
        raise ValueError("invalid connect code")
    # Commons compares the exact origin text, so only its canonical form is sent.
    if not isinstance(app, str) or app != app.lower():
        raise ValueError("invalid app origin")
    origin(app, development=development)
    return {"code": code, "app": app}


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
            unavailable("connect-capacity")
            return None
        if time.monotonic() >= deadline:
            self.connect_capacity.release()
            unavailable("connect-deadline")
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
        failure: list[str] = []
        phase = ["dns"]
        addresses: list[str] = []

        def worker() -> None:
            try:
                candidates = socket.getaddrinfo(
                    parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
                )
                for *_rest, sockaddr in candidates:
                    if str(sockaddr[0]) not in addresses:
                        addresses.append(str(sockaddr[0]))
                phase[0] = "connect"
                # Give each candidate a fair share of the remaining deadline, so
                # one unreachable address (for example filtered IPv6) cannot
                # starve the others. TLS then gets whatever time is left.
                connected = None
                for index, (family, kind, proto, _name, sockaddr) in enumerate(candidates):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    candidate = socket.socket(family, kind, proto)
                    candidate.settimeout(remaining / (len(candidates) - index))
                    try:
                        candidate.connect(sockaddr)
                    except OSError as error:
                        candidate.close()
                        failure[:] = [describe(error)]
                        continue
                    connected = candidate
                    break
                if connected is None:
                    # failure holds the last candidate's connect error.
                    if not failure:
                        failure.append("connect-deadline")
                    if not cancelled.is_set():
                        result.put_nowait(False)
                    return
                phase[0] = "tls"
                connected.settimeout(max(0.001, deadline - time.monotonic()))
                connection.sock = self.context.wrap_socket(
                    connected, server_hostname=parsed.hostname
                )
                if not cancelled.is_set():
                    result.put_nowait(True)
            except Exception as error:
                failure[:] = [describe(error)]
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
            unavailable(
                f"connect-timeout:phase={phase[0]} addresses={','.join(addresses) or 'none'}"
            )
            return None
        if not ok:
            connection.close()
            unavailable("connect:" + (failure[0] if failure else "unknown"))
            return None
        return connection

    def redeem(self, value: object) -> tuple[str | None, dict[str, str] | None]:
        try:
            checked = redemption(value, development=self.config.development)
        except ValueError:
            return "invalid_request", None
        deadline = time.monotonic() + self.config.connect_seconds
        if not self.request_capacity.acquire(timeout=max(0, deadline - time.monotonic())):
            return unavailable("request-capacity")
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
            return unavailable("no-socket")

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
                "/api/connect/redeem",
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
                return unavailable(f"content-type status={response.status}")
            length = response.getheader("Content-Length")
            if length is not None and (
                not length.isdecimal() or int(length) > self.config.response_bytes
            ):
                return unavailable(f"content-length status={response.status}")
            raw = response.read(self.config.response_bytes + 1)
            if len(raw) > self.config.response_bytes:
                return unavailable(f"oversize status={response.status}")
            body = strict_json(raw)
            if response.status == 400:
                # Every refusal is one uniform code; Commons never says why.
                if body == {"error": "CONNECT_CODE_INVALID"}:
                    return "invalid_code", None
                if body == {"error": "INVALID_REQUEST"}:
                    return "invalid_request", None
                return unavailable("error-body status=400")
            if (
                response.status != 200
                or not isinstance(body, dict)
                or set(body) != {"user", "username", "displayName", "email"}
            ):
                return unavailable(f"response-shape status={response.status}")
            if (
                any(
                    not isinstance(body[name], str) or len(body[name]) > limit
                    for name, limit in (
                        ("user", 36),
                        ("username", 32),
                        ("displayName", 256),
                        ("email", 320),
                    )
                )
                or not body["username"]
            ):
                return unavailable("response-bounds")
            subject = str(uuid.UUID(body["user"]))
            if subject != body["user"]:
                return unavailable("identity-mismatch")
            return None, {
                "subject": subject,
                "username": body["username"],
                "displayName": body["displayName"] or body["username"],
            }
        except Exception as error:
            return unavailable(describe(error))
        finally:
            timer.cancel()
            connection.close()
