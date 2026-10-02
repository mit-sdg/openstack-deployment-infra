"""Start the complete owner slice on loopback with disposable in-memory providers."""

from __future__ import annotations

import argparse
import datetime
import os
import signal
import ssl
import subprocess
import threading
from pathlib import Path
from typing import Any

from ..broker.main import serve
from ..common import canonical
from ..config import Config
from ..identity.client import IdentityConfig
from ..identity.main import serve as serve_identity
from ..web.server import Reply, WebServer, error_reply
from .commons import Commons
from .controller import FakeController


def tls_context(ca_path: Path | None = None) -> ssl.SSLContext:
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Owner portal loopback development")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    if ca_path is not None:
        ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        ca_path.chmod(0o644)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    cert_fd, key_fd = os.memfd_create("portal-local-cert"), os.memfd_create("portal-local-key")
    try:
        os.write(cert_fd, cert.public_bytes(serialization.Encoding.PEM))
        os.write(
            key_fd,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        context.load_cert_chain(f"/proc/self/fd/{cert_fd}", f"/proc/self/fd/{key_fd}")
    finally:
        os.close(cert_fd)
        os.close(key_fd)
    return context


class HarnessWeb(WebServer):
    def __init__(self, *args: Any, fixture: FakeController, **kwargs: Any) -> None:
        self.fixture = fixture
        super().__init__(*args, **kwargs)

    def handle(self, method: str, target: str, headers: dict[str, str], raw: bytes) -> Reply:
        if target.startswith("/__test__/"):
            if method != "POST" or headers.get("origin") != self.config.portal_origin:
                return error_reply(403, "ORIGIN_REJECTED")
            if target == "/__test__/lost-response":
                self.fixture.drop_next = True
            elif target == "/__test__/failed-deployment":
                self.fixture.failed_next = True
            elif target == "/__test__/recovery-required":
                self.fixture.recovery_next = True
            else:
                return error_reply(404, "NOT_FOUND")
            return Reply(200, b'{"ready":true}')
        return super().handle(method, target, headers, raw)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Loopback owner portal with fake Commons and project controller"
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Use development HTTP cookie names instead of production HTTPS cookies",
    )
    parser.add_argument("--port", type=int, default=9443)
    parser.add_argument("--provider-port", type=int, default=9444)
    parser.add_argument("--state", type=Path, default=Path(".tmp/owner-portal-credentials"))
    parser.add_argument(
        "--vite", action="store_true", help="Start Vite on the portal port (requires --http)"
    )
    args = parser.parse_args()
    if args.vite and not args.http:
        parser.error("--vite requires --http")
    root = args.state.absolute()
    if root.is_symlink() or not root.resolve().is_relative_to((Path.cwd() / ".tmp").resolve()):
        parser.error("state must be under this worktree's .tmp")
    if (
        not 1024 <= args.port <= 65535
        or not 1024 <= args.provider_port <= 65535
        or args.port == args.provider_port
    ):
        parser.error("choose distinct unprivileged loopback ports")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    socket_root = root / "sockets"
    socket_root.mkdir(mode=0o700, exist_ok=True)
    scheme = "http" if args.http else "https"
    origin = f"{scheme}://127.0.0.1:{args.port}"
    commons_origin = f"https://localhost:{args.provider_port}"
    config_path = root / "config.json"
    config_path.write_text(
        canonical(
            {
                "portalOrigin": origin,
                "commonsOrigin": commons_origin,
                "stateDirectory": str(root / "broker"),
                "brokerSocket": str(socket_root / "broker.sock"),
                "controllerSocket": str(socket_root / "project.sock"),
                "identitySocket": str(socket_root / "identity.sock"),
                "development": True,
            }
        )
    )
    config = Config.load(config_path)
    public_ca = root / "development-ca.pem"
    commons_tls = tls_context(public_ca)
    tls = None if args.http else commons_tls
    identity = serve_identity(
        IdentityConfig(
            commons_origin,
            config.identity_socket,
            development=True,
            development_ca=public_ca,
            broker_peer=(os.geteuid(), os.getegid()),
        )
    )
    fixture = FakeController(root / "controller.json")
    controller = fixture.server(config.controller_socket)
    broker, broker_server = serve(config)
    assets = Path.cwd() / "frontend/owner-portal/dist"
    if not args.vite and not (assets / "index.html").is_file():
        parser.error("build frontend/owner-portal before starting HTTPS or static HTTP mode")
    web = HarnessWeb(
        ("127.0.0.1", 0 if args.vite else args.port), config, assets, tls=tls, fixture=fixture
    )
    provider = Commons(("127.0.0.1", args.provider_port), config, assets, tls=commons_tls)
    servers = [controller, identity, broker_server, web, provider]
    threads = [
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        for server in servers
    ]
    vite: subprocess.Popen[bytes] | None = None
    stopped = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for thread in threads:
            thread.start()
        if args.vite:
            environment = {
                **os.environ,
                "PATH": "/home/agent/.local/node-v24.19.0/bin:" + os.environ.get("PATH", ""),
                "OWNER_PORTAL_API_TARGET": f"http://127.0.0.1:{web.server_port}",
            }
            vite = subprocess.Popen(
                [
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.port),
                    "--strictPort",
                ],
                cwd=Path.cwd() / "frontend/owner-portal",
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        print(
            f"Owner portal: {origin} ({'Vite development' if args.vite else 'built assets'})",
            flush=True,
        )
        print(
            "Local fakes only. TLS private keys remain in memory; fixture passwords are development-only. Ctrl-C stops the harness.",
            flush=True,
        )
        while not stopped.wait(0.5):
            if vite is not None and vite.poll() is not None:
                raise RuntimeError("Vite stopped; check the local frontend installation")
    finally:
        if vite is not None:
            # npm can exit before its Vite child when the test runner signals
            # the harness. Always stop the dedicated group, including orphans.
            try:
                os.killpg(vite.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                vite.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(vite.pid, signal.SIGKILL)
                vite.wait(timeout=5)
        broker.journal.close()
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
