"""Admin-only storage limits through durable project-socket intents."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any

from ...controller.http import HttpError, Request, Response
from ...validation import uuid as checked_uuid
from ..common import canonical, digest, object_body
from .accounts import audit
from .resources import operation_quota

if TYPE_CHECKING:
    from .api import Broker

QUOTA_BOUNDS = {
    "connections": (1, 100),
    "sizeBytes": (1073741824, 549755813888),
    "memoryBytes": (536870912, 8589934592),
    "cpuMillicores": (100, 4000),
    "s3Bytes": (1048576, 549755813888),
    "s3Objects": (1, 100000000),
}
QUOTA_FIELDS = {
    "postgres": {"sizeBytes", "connections", "memoryBytes", "cpuMillicores"},
    "mongo": {"sizeBytes", "connections", "memoryBytes", "cpuMillicores"},
    "s3": {"s3Bytes", "s3Objects"},
}


def quotas(value: object, resource_type: str) -> dict[str, Any]:
    result = object_body(value, QUOTA_FIELDS[resource_type])
    for key, number in result.items():
        low, high = QUOTA_BOUNDS[key]
        if (
            type(number) is not int
            or not low <= number <= high
            or key == "memoryBytes"
            and number % 1048576
        ):
            raise HttpError(
                400, "INVALID_REQUEST", "Choose storage limits within the allowed range."
            )
    return dict(result)


class StorageLimits:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    def change(self, request: Request) -> Response:
        b = self.broker
        actor, sid = b.accounts.admin(request)
        _actor, app = b.own(request, mutation=True)
        if request.query:
            raise HttpError(400, "INVALID_REQUEST", "Unexpected query fields.")
        resource_id = checked_uuid(request.path_parameters["resource"])
        body = object_body(request.body, {"quotas", "expectedQuotas"})
        key = request.idempotency_key()
        fingerprint = digest(canonical({"path": request.path, "body": body}))
        with b.database.connect(write=True) as db:
            b.accounts.checked_actor(db, sid)
            prior = b.existing(db, actor["id"], key, fingerprint)
            if prior is None:
                resource = next(
                    (
                        item
                        for item in b.storage_resources(app["id"])
                        if item["resourceId"] == resource_id
                    ),
                    None,
                )
                if resource is None:
                    raise HttpError(404, "NOT_FOUND", "Storage not found for this app.")
                body = {name: quotas(value, resource["type"]) for name, value in body.items()}
                operation_quota(b, db, actor["id"], app["id"])
                identifier = b.record(
                    db,
                    actor["id"],
                    app["id"],
                    "storage_limits",
                    key,
                    fingerprint,
                    "PUT",
                    f"/v1/storage/{resource_id}/limits",
                    {**body, "resourceId": resource_id},
                    str(uuid.uuid4()),
                )
                audit(
                    db,
                    actor["id"],
                    None,
                    "storage_limits_requested",
                    {
                        **body,
                        "resourceId": resource_id,
                        "applicationId": app["id"],
                        "intentId": identifier,
                    },
                    time.time(),
                )
            else:
                identifier = prior["id"]
        b.journal.dispatch(identifier)
        b.journal.wake.set()
        return b.intent_response(identifier, actor["id"], 202)
