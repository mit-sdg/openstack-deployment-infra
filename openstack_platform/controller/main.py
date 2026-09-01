"""Executable entry point for the local production controller."""

from __future__ import annotations

import argparse
import grp
import os
import signal
import threading
from pathlib import Path
from types import FrameType

from .. import runtime
from ..config import load
from ..installation import (
    DEFAULT_CONTROLLER_INVENTORY,
    DEFAULT_CONTROLLER_SOCKET,
    DEFAULT_CONTROLLER_STATE,
)
from . import database as db
from .api import ControllerAPI
from .http import ControllerServer, PeerPolicy

_DEFAULT_PLATFORM = Path(os.environ.get("PLATFORM_CONFIG", str(DEFAULT_CONTROLLER_INVENTORY)))
_DEFAULT_STATE = DEFAULT_CONTROLLER_STATE
_DEFAULT_SOCKET = DEFAULT_CONTROLLER_SOCKET


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openstack-platform-controller",
        description="local Unix-socket application controller",
        allow_abbrev=False,
    )
    parser.add_argument("--platform-config", type=Path, default=_DEFAULT_PLATFORM)
    parser.add_argument("--state-directory", type=Path, default=_DEFAULT_STATE)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--socket", type=Path, default=_DEFAULT_SOCKET)
    parser.add_argument("--socket-group")
    parser.add_argument("--privileged-socket", type=Path)
    parser.add_argument("--privileged-socket-group")
    parser.add_argument(
        "--project-peer",
        action="append",
        type=_peer,
        metavar="UID:GID",
        help="exact SO_PEERCRED identity allowed on the project socket (repeatable)",
    )
    parser.add_argument(
        "--privileged-peer",
        action="append",
        type=_peer,
        metavar="UID:GID",
        help="exact SO_PEERCRED identity allowed on the privileged socket (repeatable)",
    )
    parser.add_argument("--max-connections-per-peer", type=int, default=8)
    return parser


def _peer(value: str) -> tuple[int, int]:
    try:
        uid_text, gid_text = value.split(":", 1)
        uid, gid = int(uid_text), int(gid_text)
    except ValueError:
        raise argparse.ArgumentTypeError("peer must be UID:GID") from None
    if uid < 0 or gid < 0 or str(uid) != uid_text or str(gid) != gid_text:
        raise argparse.ArgumentTypeError("peer must contain canonical non-negative integers")
    return uid, gid


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_directory = runtime.ensure_private_directory(args.state_directory, create=True)
    platform_path = args.platform_config.resolve(strict=True)
    policy_path = (
        args.policy.resolve(strict=True)
        if args.policy is not None
        else (state_directory / "policy.json").resolve(strict=True)
    )
    config = load(platform_path, policy_path)
    identity = db.deployment_identity(config.platform)
    with runtime.lock(state_directory, "database-maintenance", wait=True):
        connection = db.connect(
            state_directory / "platform.sqlite3",
            identity=identity,
            check_same_thread=False,
        )
        try:
            db.migrate(connection, identity=identity)
        except BaseException:
            connection.close()
            raise
    api: ControllerAPI | None = None
    try:
        socket_path = args.socket
        privileged_socket = args.privileged_socket or socket_path.with_name("privileged.sock")
        if not socket_path.is_absolute() or not privileged_socket.is_absolute():
            raise ValueError("controller socket paths must be absolute")
        if socket_path == privileged_socket:
            raise ValueError("project and privileged controller sockets must differ")
        socket_gid = grp.getgrnam(args.socket_group).gr_gid if args.socket_group else None
        privileged_gid = (
            grp.getgrnam(args.privileged_socket_group).gr_gid
            if args.privileged_socket_group
            else None
        )
        project_peers = frozenset(args.project_peer or [(os.geteuid(), os.getegid())])
        privileged_peers = frozenset(args.privileged_peer or [(os.geteuid(), os.getegid())])
        api = ControllerAPI(connection, config, state_directory)
        project_server = ControllerServer(
            str(socket_path),
            api.router("project"),
            socket_gid=socket_gid,
            peer_policy=PeerPolicy(project_peers, args.max_connections_per_peer),
        )
        try:
            privileged_server = ControllerServer(
                str(privileged_socket),
                api.router("privileged"),
                socket_gid=privileged_gid,
                peer_policy=PeerPolicy(privileged_peers, args.max_connections_per_peer),
            )
        except BaseException:
            project_server.server_close()
            raise
        servers = (project_server, privileged_server)
        stopping = threading.Event()

        def stop(_signal: int, _frame: FrameType | None) -> None:
            if not stopping.is_set():
                stopping.set()
                for server in servers:
                    threading.Thread(target=server.shutdown, daemon=True).start()

        previous = {
            selected: signal.signal(selected, stop) for selected in (signal.SIGINT, signal.SIGTERM)
        }
        threads = [
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2})
            for server in servers
        ]
        try:
            for thread in threads:
                thread.start()
            stopping.wait()
        finally:
            for selected, handler in previous.items():
                signal.signal(selected, handler)
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()
    finally:
        if api is not None:
            api.close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
