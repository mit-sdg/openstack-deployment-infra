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
from .http import ControllerServer

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
    return parser


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
    try:
        socket_path = args.socket
        if not socket_path.is_absolute():
            raise ValueError("controller socket path must be absolute")
        socket_gid = None
        if args.socket_group is not None:
            socket_gid = grp.getgrnam(args.socket_group).gr_gid
        server = ControllerServer(
            str(socket_path),
            ControllerAPI(connection, config, state_directory).router(),
            socket_gid=socket_gid,
        )
        stopping = threading.Event()

        def stop(_signal: int, _frame: FrameType | None) -> None:
            if not stopping.is_set():
                stopping.set()
                threading.Thread(target=server.shutdown, daemon=True).start()

        previous = {
            selected: signal.signal(selected, stop)
            for selected in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            for selected, handler in previous.items():
                signal.signal(selected, handler)
            server.server_close()
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
