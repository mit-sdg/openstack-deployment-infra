"""The broker is the sole admitted peer; readiness never contacts Commons."""

from __future__ import annotations

import argparse
from pathlib import Path

from ...controller.http import (
    ControllerServer,
    HttpError,
    PeerPolicy,
    Request,
    Response,
    Router,
    TransportLimits,
)
from ..common import MANAGEMENT_REQUESTS
from .client import CommonsClient, IdentityConfig


def serve(config: IdentityConfig) -> ControllerServer:
    client = CommonsClient(config)
    router = Router()

    def redeem(request: Request) -> Response:
        if request.query:
            raise HttpError(400, "invalid_request", "The redeem request is invalid.")
        error, result = client.redeem(request.body)
        if error:
            raise HttpError(
                {"invalid_request": 400, "invalid_code": 400, "unavailable": 503}[error],
                error,
                "The sign-in code could not be redeemed.",
                retryable=error == "unavailable",
            )
        return Response(200, {"data": result})

    router.add("POST", "/v1/redeem", redeem)
    router.add("GET", "/v1/health", lambda _request: Response(200, {"ready": True}))
    return ControllerServer(
        str(config.socket),
        router,
        peer_policy=PeerPolicy(
            frozenset({config.broker_peer}), max_connections_per_peer=MANAGEMENT_REQUESTS
        ),
        socket_gid=config.broker_peer[1],
        limits=TransportLimits(
            global_connections=MANAGEMENT_REQUESTS, peer_connections=MANAGEMENT_REQUESTS
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Broker-only Commons sign-in code redeemer")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = IdentityConfig.load(args.config)
    if config.development:
        raise ValueError("production identity entry point cannot enable development trust")
    server = serve(config)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
