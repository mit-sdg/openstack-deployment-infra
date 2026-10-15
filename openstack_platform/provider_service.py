"""Local, fixed-action OpenStack read service under an existing credential holder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .provider import receive
from .provider_api import API
from .provider_protocol import MAX_RESPONSE, ProviderError, timeout, validate

MAX_REQUEST = 16_384


def load_credentials(path: Path) -> dict[str, str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in {0o600, 0o640}
            or metadata.st_size > MAX_RESPONSE
        ):
            raise ProviderError("configuration")
        with os.fdopen(descriptor, "r", closefd=False) as stream:
            values = {}
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, separator, raw = line.partition("=")
                if not separator or not (
                    key.startswith("OS_") or key == "PLATFORM_ALLOW_HTTP_GLANCE"
                ):
                    raise ProviderError("configuration")
                parts = shlex.split(raw, comments=False)
                if len(parts) != 1:
                    raise ProviderError("configuration")
                if key in values:
                    raise ProviderError("configuration")
                values[key] = parts[0]
            return values
    finally:
        os.close(descriptor)


def credential_identity(path: Path) -> tuple[object, ...]:
    """The file and its content, ignoring timestamps.

    Admin's controller-prepare re-applies the same owner and mode on every
    controller start, which changes ctime but not the credentials.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_RESPONSE:
            raise ProviderError("configuration")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest = hashlib.sha256(stream.read(MAX_RESPONSE + 1)).hexdigest()
    finally:
        os.close(descriptor)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mode,
        digest,
    )


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    request_queue_size = 32
    daemon_threads = True
    block_on_close = False

    def __init__(
        self, path: str, api: API, peers: frozenset[tuple[int, int]], *, group: int | None = None
    ) -> None:
        self.api = api
        self.peers = peers
        self.slots = threading.BoundedSemaphore(16)
        parent = Path(path).parent
        metadata = parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ProviderError("configuration")
        if Path(path).exists() or Path(path).is_symlink():
            old = Path(path).lstat()
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.geteuid():
                raise ProviderError("configuration")
            # A restart may remove only its own stale socket. Fail closed if
            # another provider is already listening.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(path)
                except ConnectionRefusedError:
                    Path(path).unlink()
                else:
                    raise ProviderError("configuration")
        super().__init__(path, Handler)
        if group is not None:
            os.chown(path, -1, group)
        os.chmod(path, 0o660 if group is not None else 0o600)
        self.socket_inode = Path(path).stat().st_ino

    def verify_request(self, request: Any, client_address: Any) -> bool:
        raw = request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, gid = struct.unpack("3i", raw)
        return (uid, gid) in self.peers and self.slots.acquire(blocking=False)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver's default traceback may contain provider exceptions.
        pass

    def server_close(self) -> None:
        super().server_close()
        path = Path(str(self.server_address))
        if path.exists() and path.lstat().st_ino == self.socket_inode:
            path.unlink()


class Handler(socketserver.BaseRequestHandler):
    server: Server
    request: socket.socket

    def handle(self) -> None:
        started = time.monotonic()
        deadline = started + 5
        try:
            document = json.loads(receive(self.request, MAX_REQUEST, deadline))
            if (
                not isinstance(document, dict)
                or set(document) != {"version", "action", "params", "timeout"}
                or type(document["version"]) is not int
                or document["version"] != 1
            ):
                raise ProviderError("invalid")
            action, params = validate(document["action"], document["params"])
            deadline = started + min(60, timeout(document["timeout"]))
            result = self.server.api.execute(action, params, deadline)
            response = (
                json.dumps({"ok": True, "result": result}, separators=(",", ":")).encode() + b"\n"
            )
            if len(response) > MAX_RESPONSE + 128:
                raise ProviderError("size")
        except ProviderError as error:
            response = (
                json.dumps({"ok": False, "code": error.code, "status": error.status}).encode()
                + b"\n"
            )
        except Exception:
            response = b'{"ok":false,"code":"unavailable","status":0}\n'
        try:
            self.request.settimeout(max(0.001, deadline - time.monotonic()))
            self.request.sendall(response)
        except OSError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--peer", action="append", required=True, help="exact UID:GID")
    parser.add_argument("--socket-group", type=int)
    args = parser.parse_args(argv)
    try:
        parsed_peers = [peer.split(":") for peer in args.peer]
        if any(len(peer) != 2 for peer in parsed_peers):
            raise ProviderError("configuration")
        peers = frozenset((int(peer[0]), int(peer[1])) for peer in parsed_peers)
        if any(not 0 <= value < 2**32 for peer in peers for value in peer):
            raise ProviderError("configuration")
        identity = credential_identity(args.credentials)
        values = load_credentials(args.credentials)
        changed = threading.Event()
        running: list[Server] = []

        def check_credentials() -> None:
            try:
                if credential_identity(args.credentials) == identity:
                    return
            except (OSError, ProviderError):
                pass
            # Refuse this read, then exit non-zero so the service manager
            # restarts with the current credentials.
            if not changed.is_set():
                changed.set()
                for server in running:
                    threading.Thread(target=server.shutdown, daemon=True).start()
            raise ProviderError("configuration")

        check_credentials()
        api = API(values, check_credentials=check_credentials)
        parent = Path(args.socket).parent
        if not parent.exists():
            from .runtime import ensure_private_directory

            ensure_private_directory(parent)
        with Server(args.socket, api, peers, group=args.socket_group) as server:
            running.append(server)

            def stop(_signal: int, _frame: Any) -> None:
                threading.Thread(target=server.shutdown, daemon=True).start()

            notify_path = os.environ.get("NOTIFY_SOCKET")
            if notify_path:
                address = "\x00" + notify_path[1:] if notify_path.startswith("@") else notify_path
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
                    notify.connect(address)
                    notify.sendall(b"READY=1")
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            server.serve_forever(poll_interval=0.1)
        if changed.is_set():
            print("OpenStack credentials changed; restarting provider", file=sys.stderr)
            return 1
        return 0
    except Exception:
        print(
            "OpenStack provider service unavailable; provider details were withheld",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
