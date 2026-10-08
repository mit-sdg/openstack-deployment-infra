"""Admin-only default builder selection through the project peer."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from ...controller.http import HttpError, Request, Response
from ..common import canonical, digest
from . import sizing
from .accounts import audit

if TYPE_CHECKING:
    from .api import Broker


class BuilderSettings:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    def read(self, request: Request) -> Response:
        b = self.broker
        _actor, sid = b.accounts.admin(request)
        if request.query or request.body is not None:
            raise HttpError(400, "INVALID_REQUEST", "Unexpected request fields.")
        selected = sizing.result(b.client, "/v1/settings/default-builder-size")
        sizes = [item for item in sizing.flavors(b.client) if item["ram_mib"] >= 1024]
        with b.database.connect() as db:
            b.accounts.checked_actor(db, sid)
        return Response(
            200, {"data": {"flavor": sizing.flavor(selected.get("flavor")), "sizes": sizes}}
        )

    def change(self, request: Request) -> Response:
        b = self.broker
        actor, sid = b.accounts.admin(request)
        if request.query:
            raise HttpError(400, "INVALID_REQUEST", "Unexpected query fields.")
        body = sizing.builder_body(request.body)
        if any(value is None for value in body.values()):
            raise HttpError(400, "INVALID_REQUEST", "Choose a default builder size.")
        key, fingerprint = (
            request.idempotency_key(),
            digest(canonical({"path": request.path, "body": body})),
        )
        with b.database.connect(write=True) as db:
            b.accounts.checked_actor(db, sid)
            prior = b.existing(db, actor["id"], key, fingerprint)
            if prior is None:
                intent = b.record(
                    db,
                    actor["id"],
                    None,
                    "default_builder_size",
                    key,
                    fingerprint,
                    "PUT",
                    "/v1/settings/default-builder-size",
                    body,
                    str(uuid.uuid4()),
                )
                audit(
                    db,
                    actor["id"],
                    None,
                    "default_builder_size_requested",
                    {**body, "intentId": intent},
                    time.time(),
                )
            else:
                intent = prior["id"]
        b.journal.dispatch(intent)
        b.journal.wake.set()
        return b.intent_response(intent, actor["id"], 202)
