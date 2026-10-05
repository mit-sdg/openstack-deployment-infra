"""Owner resource routes and explicit, credential-free controller projections."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, Any

from ...controller.deployment_config import parse_configuration
from ...controller.http import HttpError, Request, Response
from ...controller.storage_contract import (
    OUTPUT_ENVIRONMENT_KEYS,
    RESERVED_ENVIRONMENT_KEYS,
    RESERVED_ENVIRONMENT_PREFIX,
    RESOURCE_OUTPUTS,
)
from ...validation import ValidationError, bounded_text, env_key
from ...validation import uuid as checked_uuid
from ..common import canonical, digest, object_body, strict_json, utc
from .client import ControllerUnavailable
from .journal import intent_model

if TYPE_CHECKING:
    from .api import Broker


def owner_key(value: object) -> str:
    try:
        name = env_key(value)
    except ValidationError:
        raise HttpError(
            400,
            "INVALID_ENV_NAME",
            "Use an uppercase name starting with A–Z, containing only A–Z, 0–9 and underscores (at most 128 characters).",
        ) from None
    if name in RESERVED_ENVIRONMENT_KEYS or name.startswith(RESERVED_ENVIRONMENT_PREFIX):
        raise HttpError(400, "RESERVED_ENV_NAME", "This variable name is reserved by the platform.")
    return name


def environment_metadata(self: Broker, app_id: str) -> dict[str, Any]:
    status, data = self.client.request("GET", f"/v1/applications/{app_id}/environment")
    if (
        status != 200
        or data.get("applicationId") != app_id
        or not isinstance(data.get("keys"), list)
    ):
        raise ControllerUnavailable("invalid environment observation")
    return {
        "revision": data["revision"],
        "updatedAt": utc(data.get("updatedAt")),
        "items": [
            {"name": owner_key(item["name"]), "updatedAt": utc(data.get("updatedAt"))}
            for item in data["keys"]
            if item.get("owner") == "staff"
        ],
    }


def environment(self: Broker, request: Request) -> Response:
    user, app = self.own(request)
    data = environment_metadata(self, app["id"])
    with self.database.connect() as db:
        data["intents"] = [
            intent_model(row, diagnostic=user["role"] in {"staff", "admin"}, viewer=user["id"])
            for row in db.execute(
                "SELECT * FROM intents WHERE app_id=? AND kind IN ('env_set','env_delete') AND state NOT IN ('succeeded','failed') ORDER BY created",
                (app["id"],),
            )
        ]
    return Response(200, {"data": data})


def mutate_environment(self: Broker, request: Request) -> Response:
    user, app = self.own(request, mutation=True)
    name = owner_key(request.path_parameters["key"])
    if request.method == "PUT":
        body = object_body(request.body, {"value"})
        # The controller and helper cap a single value at 65,536 UTF-8 bytes.
        # Controller admission also applies any tighter deployment policy.
        try:
            bounded_text(body["value"], field="environment value", maximum=65_536)
        except (ValidationError, UnicodeError):
            raise HttpError(
                400,
                "INVALID_ENV_VALUE",
                "Values must be text without NUL bytes, at most 65,536 UTF-8 bytes.",
            ) from None
        kind = "env_set"
    else:
        if request.body not in (None, {}):
            raise HttpError(400, "INVALID_REQUEST", "Environment deletion takes no fields.")
        body, kind = {}, "env_delete"
    key = request.idempotency_key()
    material = canonical({"method": request.method, "path": request.path, "body": body})
    if kind == "env_set":
        # A DB copy must not provide an offline oracle for low-entropy values.
        # Separate this MAC from the anonymous challenge/CSRF purposes while
        # retaining the existing private key's crash-durable lifecycle.
        subkey = hmac.digest(
            self.auth.anonymous.key, b"owner-portal/env-fingerprint/v1", hashlib.sha256
        )
        fingerprint = hmac.new(subkey, material.encode(), hashlib.sha256).hexdigest()
    else:
        fingerprint = digest(material)
    with self.database.connect(write=True) as db:
        try:
            existing = self.existing(db, user["id"], key, fingerprint)
        except HttpError as error:
            if kind != "env_set" or error.code != "IDEMPOTENCY_CONFLICT":
                raise
            raise HttpError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This environment request key belongs to a different request, or its fingerprint key changed after restore. Use a new request key after any in-flight operation has finished or been reconciled.",
            ) from None
        if existing is None:
            cfg = db.execute(
                "SELECT configuration FROM configurations WHERE app_id=? AND revision=(SELECT revision FROM apps WHERE id=?)",
                (app["id"], app["id"]),
            ).fetchone()
            if cfg:
                for binding in strict_json(cfg[0].encode())["storageBindings"]:
                    if name in binding["outputs"].values():
                        raise HttpError(
                            409,
                            "ENV_BINDING_CONFLICT",
                            f"{name} is a storage binding target. Remove or rename that binding first.",
                        )
            count = db.execute(
                "SELECT COUNT(*) FROM intents WHERE user_id=? AND kind IN ('env_set','env_delete') AND created>?",
                (user["id"], time.time() - 60),
            ).fetchone()[0]
            if count >= 30:
                raise HttpError(
                    429,
                    "RATE_LIMITED",
                    "Wait a minute before editing more environment variables.",
                    retryable=True,
                )
            operation_quota(self, db, user["id"], app["id"])
            identifier = self.record(
                db,
                user["id"],
                app["id"],
                kind,
                key,
                fingerprint,
                request.method,
                f"/v1/applications/{app['id']}/environment/{name}",
                {"names": [name]},
                str(uuid.uuid4()),
            )
            db.execute(
                "UPDATE audit SET action=? WHERE intent_id=?", (f"{kind}:{name}", identifier)
            )
        else:
            identifier = existing["id"]
    # Values exist only in this invocation. Background recovery polls accepted
    # operations but cannot replay a write without the owner resubmitting it.
    self.journal.dispatch(identifier, secret_body=body)
    self.journal.wake.set()
    return self.intent_response(identifier, user["id"], 202)


def storage_resources(self: Broker, app_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    for _ in range(100):
        path = f"/v1/applications/{app_id}/storage?limit=100" + (
            f"&cursor={checked_uuid(cursor)}" if cursor else ""
        )
        status, page = self.client.request("GET", path)
        if status != 200 or not isinstance(page.get("items"), list):
            raise ControllerUnavailable("invalid storage observation")
        for item in page["items"]:
            if item.get("applicationId") != app_id or item.get("type") not in RESOURCE_OUTPUTS:
                raise ControllerUnavailable("invalid storage ownership")
            items.append(
                {
                    "resourceId": checked_uuid(item["resourceId"]),
                    "type": item["type"],
                    "label": item.get("displayLabel") or item["type"],
                    "status": {
                        "active": "ready",
                        "creating": "provisioning",
                        "recovery_required": "failed",
                        "removing": "removing",
                    }.get(item["lifecycleState"], item["lifecycleState"]),
                    "createdAt": utc(item.get("createdAt")),
                    "verifiedAt": utc(item.get("lastVerifiedAt")),
                    "defaultBindings": dict(OUTPUT_ENVIRONMENT_KEYS[item["type"]]),
                }
            )
        cursor = page.get("nextCursor")
        if not cursor:
            return items
    raise ControllerUnavailable("storage collection limit")


def storage(self: Broker, request: Request) -> Response:
    user, app = self.own(request)
    items = storage_resources(self, app["id"])
    with self.database.connect() as db:
        progress = [
            {
                **intent_model(
                    row, diagnostic=user["role"] in {"staff", "admin"}, viewer=user["id"]
                ),
                "type": strict_json(row["body"].encode()).get("type"),
            }
            # Every teammate's storage changes, so nobody starts a duplicate.
            for row in db.execute(
                "SELECT * FROM intents WHERE app_id=? AND kind LIKE 'storage_%' ORDER BY created DESC LIMIT 20",
                (app["id"],),
            )
        ]
    return Response(200, {"data": {"items": items, "intents": progress}})


def mutate_storage(self: Broker, request: Request) -> Response:
    user, app = self.own(request, mutation=True)
    creating = "resource" not in request.path_parameters
    if creating:
        body = self.identity_mutation_body(request, app, {"type"})
        if not isinstance(body["type"], str) or body["type"] not in RESOURCE_OUTPUTS:
            raise HttpError(400, "INVALID_FIELD", "Choose postgres, mongo or s3.")
        path, kind = f"/v1/applications/{app['id']}/storage", "storage_create"
    else:
        self.identity_mutation_body(request, app, set())
        resource = checked_uuid(request.path_parameters["resource"])
        action = request.path.rsplit("/", 1)[1]
        body, path, kind = {}, f"/v1/storage/{resource}/{action}", f"storage_{action}"
    key = request.idempotency_key()
    fingerprint = digest(canonical({"path": request.path, "body": body}))
    with self.database.connect(write=True) as db:
        existing = self.existing(db, user["id"], key, fingerprint)
        if existing is not None:
            identifier = existing["id"]
        else:
            resources = storage_resources(self, app["id"])
            if creating:
                pending = db.execute(
                    "SELECT body FROM intents WHERE app_id=? AND kind='storage_create' AND state NOT IN ('succeeded','failed')",
                    (app["id"],),
                ).fetchall()
                if any(item["type"] == body["type"] for item in resources) or any(
                    strict_json(row[0].encode())["type"] == body["type"] for row in pending
                ):
                    raise HttpError(
                        409,
                        "STORAGE_TYPE_EXISTS",
                        f"This application already has {body['type']} storage or a pending creation.",
                    )
            elif not any(item["resourceId"] == resource for item in resources):
                raise HttpError(404, "NOT_FOUND", "Storage not found for this application.")
            operation_quota(self, db, user["id"], app["id"])
            identifier = self.record(
                db,
                user["id"],
                app["id"],
                kind,
                key,
                fingerprint,
                "POST",
                path,
                body,
                str(uuid.uuid4()),
            )
    self.journal.dispatch(identifier)
    self.journal.wake.set()
    return self.intent_response(identifier, user["id"], 202)


def validate_bindings(
    self: Broker, db: sqlite3.Connection, app_id: str, configuration: str
) -> None:
    parsed = parse_configuration(configuration)
    if not parsed.storage_bindings:
        return
    resources = {item["resourceId"]: item for item in storage_resources(self, app_id)}
    names = {item["name"] for item in environment_metadata(self, app_id)["items"]}
    for row in db.execute(
        "SELECT body FROM intents WHERE app_id=? AND kind='env_set' AND state NOT IN ('succeeded','failed')",
        (app_id,),
    ):
        names.update(strict_json(row[0].encode())["names"])
    deleting = {
        row[0].rsplit("/", 1)[1]
        for row in db.execute(
            "SELECT path FROM intents WHERE app_id=? AND kind='storage_delete' AND state NOT IN ('succeeded','failed')",
            (app_id,),
        )
    }
    for binding in parsed.storage_bindings:
        resource = resources.get(binding.resource_id)
        if resource is None or binding.resource_id in deleting:
            raise HttpError(400, "INVALID_BINDING", "Storage must belong to this application.")
        for output, target in binding.outputs:
            if output not in RESOURCE_OUTPUTS[resource["type"]]:
                raise HttpError(
                    400, "INVALID_BINDING", f"Unknown output for {resource['type']} storage."
                )
            if target in names:
                raise HttpError(
                    409,
                    "ENV_BINDING_CONFLICT",
                    f"{target} already exists as an environment variable.",
                )


# Staff and admins administer every app, so neither has app or concurrency limits.
UNLIMITED_ROLES = frozenset({"staff", "admin"})


def unlimited(db: sqlite3.Connection, user_id: str) -> bool:
    """Staff and admin accounts have no app or concurrency limits."""
    row = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    return row is not None and row[0] in UNLIMITED_ROLES


def operation_quota(self: Broker, db: sqlite3.Connection, user_id: str, app_id: str) -> None:
    held = db.execute(
        "SELECT app_id FROM intents WHERE user_id=? AND kind IN ('deploy','storage_create','storage_verify','storage_rotate','storage_delete','env_set','env_delete','app_enable','app_disable','app_restart') AND state NOT IN ('succeeded','failed')",
        (user_id,),
    ).fetchall()
    if db.execute(
        "SELECT 1 FROM intents WHERE app_id=? AND kind IN ('deploy','storage_create','storage_verify','storage_rotate','storage_delete','env_set','env_delete','app_enable','app_disable','app_restart') AND state NOT IN ('succeeded','failed')",
        (app_id,),
    ).fetchone():
        raise HttpError(
            409, "APP_BUSY", "Wait for or recover this application's current operation first."
        )
    if unlimited(db, user_id):
        return
    policy = db.execute("SELECT concurrent FROM quotas WHERE user_id=?", (user_id,)).fetchone()
    if len(held) >= (self.config.concurrency_limit if policy is None else policy[0]):
        raise HttpError(409, "QUOTA_EXCEEDED", "Wait for your current operation to finish.")
