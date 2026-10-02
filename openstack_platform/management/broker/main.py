"""Management broker entry point; no development providers are imported here."""

from __future__ import annotations

import argparse
from pathlib import Path

from ...controller.http import ControllerServer, PeerPolicy, TransportLimits
from ..common import MANAGEMENT_REQUESTS
from ..config import Config
from .api import Broker


def serve(config: Config) -> tuple[Broker, ControllerServer]:
    broker = Broker(config)
    server = ControllerServer(
        str(config.broker_socket),
        broker.router(),
        peer_policy=PeerPolicy(
            frozenset({config.web_peer}), max_connections_per_peer=MANAGEMENT_REQUESTS
        ),
        socket_gid=config.web_peer[1],
        limits=TransportLimits(
            global_connections=MANAGEMENT_REQUESTS, peer_connections=MANAGEMENT_REQUESTS
        ),
    )
    broker.journal.start()
    return broker, server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local management authorization broker")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = Config.load(args.config)
    if config.development:
        raise ValueError("production broker entry point cannot enable development mode")
    broker, server = serve(config)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        broker.journal.close()


if __name__ == "__main__":
    main()
