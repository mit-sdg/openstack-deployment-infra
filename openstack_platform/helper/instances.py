"""Authenticated instance administration; secret replies stay inside the helper."""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..runtime import HttpStatusFailure, RuntimeFailure, bounded_http
from ..validation import ValidationError, uuid
from .main import HelperActionError


@dataclass(frozen=True)
class InstanceClient:
    base: str
    token: str = field(repr=False)
    context: ssl.SSLContext = field(repr=False)

    def call(self, action: str, identifier: str | None = None, **values: Any) -> dict[str, Any]:
        body = {"action": action, **values}
        if action not in {"list", "backup-inventory", "ping"}:
            body["instanceId"] = uuid(identifier, field="instance ID")
        try:
            response = bounded_http(
                self.base + "/platform/instances",
                method="POST",
                data=json.dumps(body).encode(),
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                },
                ssl_context=self.context,
                timeout_seconds=values.get("seconds", 1800) + 30
                if action == "copy"
                else 120
                if action
                in {
                    "start",
                    "create",
                    "restore-create",
                    "limits",
                    "remove",
                    "restore-begin",
                    "restore-finish",
                }
                else 5,
                response_limit=65536,
            )
        except HttpStatusFailure as error:
            try:
                code = json.loads(error.body)["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = None
            known = {
                "MEMORY_BUDGET_EXCEEDED",
                "CONNECTION_BUDGET_EXCEEDED",
                "DISK_BUDGET_EXCEEDED",
                "INVALID_INSTANCE_REQUEST",
                "INSTANCE_OPERATION_FAILED",
                "INSTANCE_NOT_READY",
                "SIZE_BELOW_USAGE",
                "MIGRATION_ALREADY_PUBLISHED",
                "INSTANCE_COPY_IN_PROGRESS",
            }
            if not isinstance(code, str) or code not in known:
                code = "PROVIDER_RESPONSE_INVALID"
            raise HelperActionError(code, "storage instance request was rejected") from None
        except (RuntimeFailure, OSError):
            raise HelperActionError(
                "INSTANCE_MANAGER_UNAVAILABLE",
                "storage instance manager is unavailable; retry the operation",
            ) from None
        result = json.loads(response.body)
        if not isinstance(result, dict):
            raise HelperActionError("PROVIDER_RESPONSE_INVALID", "instance response is invalid")
        return result


def connect_ready(connect: Any) -> Any:
    deadline = time.monotonic() + 60
    while True:
        try:
            client = connect()
            return client
        except Exception:
            if time.monotonic() >= deadline:
                raise HelperActionError(
                    "INSTANCE_NOT_READY", "instance database did not become ready"
                ) from None
            time.sleep(0.25)


def metadata(
    args: Mapping[str, Any],
) -> tuple[
    dict[str, Any],
    str | None,
    dict[str, int] | None,
    list[str],
    str,
    dict[str, int],
    Any,
    str | None,
    str | None,
]:
    values = dict(args)
    if (
        not {
            "instanceId",
            "instanceQuotas",
            "workerIds",
            "resourceId",
            "reservations",
            "retainedWorker",
            "workloadJobId",
            "stagedInstanceId",
        }
        <= values.keys()
    ):
        raise HelperActionError("INVALID_ARGS", "storage endpoint metadata is required")
    resource_id = uuid(values.pop("resourceId"), field="resource ID")
    reservations = values.pop("reservations")
    retained = values.pop("retainedWorker")
    job_id = values.pop("workloadJobId")
    if job_id is not None and job_id not in {
        args["applicationSlug"],
        args["applicationSlug"] + "-candidate",
    }:
        raise ValidationError("storage workload job identity is invalid")
    identifier = values.pop("instanceId")
    identifier = None if identifier is None else uuid(identifier, field="instance ID")
    staged = values.pop("stagedInstanceId")
    if staged is not None and (identifier is not None or staged != resource_id):
        raise ValidationError("staged instance identity does not match the shared resource")
    quotas = values.pop("instanceQuotas")
    workers = values.pop("workerIds")
    if not isinstance(workers, list) or len(workers) != 3:
        raise ValidationError("storage worker identities are invalid")
    workers = [uuid(value, field="worker ID") for value in workers]
    return values, identifier, quotas, workers, resource_id, reservations, retained, job_id, staged
