"""Management web entry point; static files and broker only."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import Config
from .server import WebServer


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve owner portal assets and the closed broker API"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    config = Config.load(args.config)
    if config.development:
        raise ValueError("production web entry point cannot enable development mode")
    server = WebServer((args.bind, args.port), config, args.assets)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
