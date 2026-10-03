"""Local-admin app authority over the existing project peer and resource handlers."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from ...controller.deployment_config import branch_name, parse_configuration
from ...controller.http import HttpError, Request, Response
from ...controller.storage_contract import RESOURCE_OUTPUTS
from ...validation import flavor_reference, repository_url, slug
from ...validation import uuid as checked_uuid
from ..common import canonical, digest, object_body
from .accounts import audit
from .client import ControllerUnavailable
from .resources import operation_quota
from .staff import ReadLimits

if TYPE_CHECKING:
    from .api import Broker

IDENTITY_WARNING = "Portal sign-in depends on this app"


class AdminApps:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        # Authority is request-local, including concurrent Unix-server threads.
        self.read_limits = ReadLimits()
        self.context: ContextVar[tuple[str, bool] | None] = ContextVar(
            "admin_app_actor", default=None
        )

    def routes(self) -> list[tuple[str, str, Any]]:
        b = self.broker
        root = "/v1/admin-apps"
        return [
            ("GET", root, self.listing),
            ("POST", root, b.create),
            ("POST", root + "/adopt", self.adopt),
            ("GET", root + "/{app}", self.detail),
            ("GET", root + "/{app}/configuration", b.configuration),
            ("PUT", root + "/{app}/configuration", b.save),
            ("GET", root + "/{app}/environment", b.environment),
            ("PUT", root + "/{app}/environment/{key}", b.mutate_environment),
            ("DELETE", root + "/{app}/environment/{key}", b.mutate_environment),
            ("GET", root + "/{app}/storage", b.storage),
            ("POST", root + "/{app}/storage", b.mutate_storage),
            ("POST", root + "/{app}/storage/{resource}/verify", b.mutate_storage),
            ("POST", root + "/{app}/storage/{resource}/rotate", b.mutate_storage),
            ("DELETE", root + "/{app}/storage/{resource}", self.delete_storage),
            ("POST", root + "/{app}/deployments", b.deploy),
            ("GET", root + "/{app}/deployments", b.history),
            ("GET", root + "/{app}/deployments/{deployment}", b.deployment),
            ("PUT", root + "/{app}/owner", self.reassign),
            ("POST", root + "/{app}/state", self.state),
        ]

    def handle(self, request: Request, handler: Callable[[Request], Response]) -> Response:
        sensitive = (
            request.method == "DELETE"
            and "/storage/" in request.path
            or request.path.endswith("/owner")
        )
        user, sid = self.broker.accounts.admin(request, step_up=sensitive)
        allowed = (
            {"limit", "cursor"}
            if request.path == "/v1/admin-apps" or request.path.endswith("/deployments")
            else {"lines", "offset"}
            if request.path.endswith("/build-log")
            else set()
        )
        if (
            any(len(v) != 1 or not v[0] for v in request.query.values())
            or set(request.query) - allowed
            or request.method == "GET"
            and request.body is not None
        ):
            raise HttpError(400, "INVALID_REQUEST", "Unexpected request fields.")
        token = self.context.set((sid, sensitive))
        stack = ExitStack()
        try:
            if request.method == "GET":
                stack.enter_context(
                    self.read_limits.reserve(
                        user["id"],
                        request.headers.get("x-portal-client-address", "unknown"),
                        self.broker.auth.clock(),
                    )
                )
            # Reuse resource handlers without adding consent fields to their
            # canonical requests or (especially) storing environment values.
            if (
                request.method != "GET"
                and "/storage" in request.path
                and request.method != "DELETE"
            ):
                _actor, app = self.broker.own(request, mutation=True)
                if not isinstance(request.body, dict):
                    raise HttpError(400, "INVALID_REQUEST", "Supply a request object.")
                body = dict(request.body)
                self.identity_consent(app["id"], body)
                body.pop("identityProviderConfirmed", None)
                request = replace(request, body=body)
            response = handler(request)
            # Suppress a result if authority was revoked during a controller read.
            with self.broker.database.connect() as db:
                self.check(db)
            if request.method == "GET":
                route = request.path
                for value in request.path_parameters.values():
                    route = route.replace(value, "{id}")
                self.broker.staff.audit(
                    request,
                    route,
                    user["id"],
                    sid,
                    str(uuid.uuid4()),
                    response.status,
                    "allowed",
                    cast(dict[str, Any], response.body).get("data"),
                )
            return response
        finally:
            stack.close()
            self.context.reset(token)

    def check(self, db: sqlite3.Connection) -> None:
        context = self.context.get()
        if context is not None:
            self.broker.accounts.checked_actor(db, context[0], step_up=context[1])

    def record_audit(
        self, db: sqlite3.Connection, actor: str, app: str, kind: str, intent: str, now: float
    ) -> None:
        if self.context.get() is not None:
            owner = db.execute("SELECT user_id FROM apps WHERE id=?", (app,)).fetchone()
            audit(
                db,
                actor,
                owner[0] if owner else None,
                "app_" + kind,
                {"applicationId": app, "intentId": intent},
                now,
            )

    @staticmethod
    def check_owner(db: sqlite3.Connection, owner: str) -> None:
        row = db.execute("SELECT enabled,status FROM users WHERE id=?", (owner,)).fetchone()
        if row is None or not row["enabled"] or row["status"] != "active":
            raise HttpError(400, "INVALID_OWNER", "Choose an active, enabled account.")

    def observed(self, app: str) -> dict[str, Any]:
        status, model = self.broker.client.request("GET", f"/v1/applications/{app}")
        if status == 404:
            raise HttpError(404, "NOT_FOUND", "Application not found.")
        if status != 200 or model.get("applicationId") != app:
            raise ControllerUnavailable("invalid application observation")
        return model

    def identity(self, model: dict[str, Any]) -> bool:
        value = model.get("url")
        return (
            isinstance(value, str)
            and urlsplit(value).hostname == urlsplit(self.broker.config.commons_origin).hostname
        )

    def identity_consent(
        self, app: str, body: dict[str, Any], model: dict[str, Any] | None = None
    ) -> None:
        if (
            "identityProviderConfirmed" in body
            and type(body["identityProviderConfirmed"]) is not bool
        ):
            raise HttpError(400, "INVALID_FIELD", "Identity-provider confirmation must be boolean.")
        if (
            self.identity(model if model is not None else self.observed(app))
            and body.get("identityProviderConfirmed") is not True
        ):
            raise HttpError(409, "IDENTITY_CONFIRMATION_REQUIRED", IDENTITY_WARNING)

    @staticmethod
    def sizing_plan(value: dict[str, Any], app: str, model: dict[str, Any]) -> None:
        # Validate the public plan shape before durable admission. The controller
        # independently verifies the complete plan against fresh cloud evidence.
        try:
            if (
                set(value)
                != {
                    "applicationId",
                    "deploymentId",
                    "activation",
                    "current",
                    "flavor",
                    "allocation",
                    "reserve",
                    "fingerprint",
                }
                or value["applicationId"] != app
                or value["deploymentId"] != model.get("activeDeploymentId")
            ):
                raise ValueError
            if (
                value["activation"] != "enable-after-healthy-acceptance"
                or value["allocation"] != "measured-worker-capacity-minus-reserve"
            ):
                raise ValueError
            current, flavor = value["current"], value["flavor"]
            if (
                not isinstance(current, dict)
                or set(current) != {"enabled", "flavor", "cpuMHz", "memoryMiB"}
                or not isinstance(flavor, dict)
                or set(flavor) != {"flavor_id", "name", "vcpus", "ram_mib", "disk_gib"}
            ):
                raise ValueError
            flavor_reference(current["flavor"])
            flavor_reference(flavor["flavor_id"])
            flavor_reference(flavor["name"])
            if type(current["enabled"]) is not bool or current["enabled"] != model.get(
                "desiredRunning"
            ):
                raise ValueError
            for item, keys in (
                (current, ("cpuMHz", "memoryMiB")),
                (flavor, ("vcpus", "ram_mib", "disk_gib")),
            ):
                if any(type(item[k]) is not int or not 0 <= item[k] <= 2**31 - 1 for k in keys):
                    raise ValueError
            if value["reserve"] != {
                "cpuMHzMinimum": 200,
                "memoryMiBMinimum": 512,
                "percentMinimum": 10,
            } or any(type(v) is not int for v in value["reserve"].values()):
                raise ValueError
            sizing = model.get("sizing")
            if not isinstance(sizing, dict) or any(
                current[a] != sizing[b]
                for a, b in (
                    ("flavor", "workerFlavor"),
                    ("cpuMHz", "cpuMHz"),
                    ("memoryMiB", "memoryMiB"),
                )
            ):
                raise ValueError
            if value["fingerprint"] != digest(
                canonical({k: v for k, v in value.items() if k != "fingerprint"})
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HttpError(
                400,
                "INVALID_PLAN",
                "Supply the exact current reviewed sizing plan; no extra configuration fields are accepted.",
            ) from None

    def deployment_body(self, request: Request, app: dict[str, Any]) -> dict[str, Any]:
        if (
            not isinstance(request.body, dict)
            or not {"configurationRevision", "commit"} <= set(request.body)
            or set(request.body)
            - {
                "configurationRevision",
                "commit",
                "maintenance",
                "plan",
                "identityProviderConfirmed",
            }
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid deployment fields.")
        body = dict(request.body)
        if (
            type(body.get("maintenance", False)) is not bool
            or "plan" in body
            and not isinstance(body["plan"], dict)
        ):
            raise HttpError(
                400, "INVALID_FIELD", "Maintenance must be boolean and plan must be an object."
            )
        model = self.observed(app["id"])
        self.identity_consent(app["id"], body, model)
        if "plan" in body:
            self.sizing_plan(body["plan"], app["id"], model)
        if model.get("requiresMaintenance") is True and body.get("maintenance") is not True:
            raise HttpError(
                409,
                "MAINTENANCE_REQUIRED",
                "A retained primary IPv4 requires maintenance consent for the brief cutover downtime.",
            )
        return body

    def listing(self, request: Request) -> Response:
        b = self.broker
        limit, cursor = b.staff.page_limit(request), request.query.get("cursor", (None,))[0]
        with b.database.connect() as db:
            parameters: list[object] = []
            where = "lifecycle!='rejected'"
            if cursor:
                point = db.execute(
                    "SELECT created,id FROM apps WHERE id=? AND lifecycle!='rejected'",
                    (checked_uuid(cursor),),
                ).fetchone()
                if point is None:
                    raise HttpError(400, "INVALID_REQUEST", "Unknown page cursor.")
                where += " AND (created<? OR (created=? AND id<?))"
                parameters.extend([point["created"], point["created"], point["id"]])
            rows = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM apps WHERE {where} ORDER BY created DESC,id DESC LIMIT ?",
                    (*parameters, limit + 1),
                )
            ]
        # Local list has no per-app controller fanout. Detail refreshes one app.
        return Response(
            200,
            {
                "data": {
                    "items": [
                        {
                            "applicationId": row["id"],
                            "slug": row["slug"],
                            "ownerId": row["user_id"],
                            "savedRevision": row["revision"],
                            "lifecycleState": row["lifecycle"],
                        }
                        for row in rows[:limit]
                    ],
                    "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None,
                    "truncated": len(rows) > limit,
                }
            },
        )

    def detail(self, request: Request) -> Response:
        _user, app = self.broker.own(request)
        response = self.broker.app(request)
        model = dict(cast(dict[str, Any], response.body)["data"])
        current = self.observed(app["id"])
        model.update(
            ownerId=app["user_id"],
            identityProvider=self.identity(current),
            requiresMaintenance=current.get("requiresMaintenance") is True,
            sizing=current.get("sizing"),
        )
        return Response(200, {"data": model})

    def adopt(self, request: Request) -> Response:
        b = self.broker
        actor, _sid = b.auth.authenticate(request, kind="admin", mutation=True)
        if (
            not isinstance(request.body, dict)
            or "applicationId" not in request.body
            or set(request.body) - {"applicationId", "ownerId"}
        ):
            raise HttpError(
                400, "INVALID_REQUEST", "Supply an application ID and optional owner ID."
            )
        identifier = checked_uuid(request.body["applicationId"])
        owner = checked_uuid(request.body.get("ownerId", actor["id"]))
        key, fingerprint = (
            request.idempotency_key(),
            digest(canonical({"path": request.path, "body": request.body})),
        )
        with b.database.connect() as db:
            prior = b.existing(db, actor["id"], key, fingerprint)
            if prior is not None:
                return Response(200, {"data": {"applicationId": prior["app_id"], "imported": True}})
            if db.execute("SELECT 1 FROM apps WHERE id=?", (identifier,)).fetchone():
                raise HttpError(
                    409, "ALREADY_OWNED", "This application already has a broker owner."
                )
        model = self.observed(identifier)
        active = model.get("activeDeploymentId")
        if active is None:
            raise HttpError(
                409,
                "SNAPSHOT_UNAVAILABLE",
                "Adoption requires a complete accepted deployment snapshot; deploy it through the operator first.",
            )
        status, snapshot = b.client.request("GET", f"/v1/deployments/{checked_uuid(active)}")
        if (
            status != 200
            or snapshot.get("applicationId") != identifier
            or snapshot.get("deploymentId") != active
            or snapshot.get("snapshotKind") != "strict"
        ):
            raise HttpError(
                409,
                "SNAPSHOT_UNAVAILABLE",
                "A complete accepted configuration snapshot is required.",
            )
        revision = snapshot.get("configurationRevision")
        if type(revision) is not int or revision < 1:
            raise HttpError(
                409, "SNAPSHOT_UNAVAILABLE", "A valid accepted configuration revision is required."
            )
        repository, branch = (
            repository_url(snapshot.get("sourceRepository")),
            branch_name(snapshot.get("requestedRef")),
        )
        configuration = parse_configuration(snapshot["configuration"]).canonical_json()
        if digest(configuration) != snapshot.get("configurationSha256"):
            raise HttpError(
                409, "SNAPSHOT_UNAVAILABLE", "Accepted configuration evidence is inconsistent."
            )
        if self.observed(identifier).get("activeDeploymentId") != active:
            raise HttpError(
                409, "SNAPSHOT_CHANGED", "The active deployment changed; retry adoption."
            )
        name = slug(model.get("slug"))
        with b.database.connect(write=True) as db:
            self.check(db)
            prior = b.existing(db, actor["id"], key, fingerprint)
            if prior is None:
                self.check_owner(db, owner)
                if db.execute(
                    "SELECT 1 FROM apps WHERE id=? OR slug=?", (identifier, name)
                ).fetchone():
                    raise HttpError(
                        409,
                        "ALREADY_OWNED",
                        "This application already has a broker owner or conflicting slug.",
                    )
                # Retain all validated storage bindings, never environment values.
                b.validate_bindings(db, identifier, configuration)
                intent = b.record(
                    db,
                    actor["id"],
                    identifier,
                    "adopt_app",
                    key,
                    fingerprint,
                    "POST",
                    "/local/adoption",
                    {},
                    str(uuid.uuid4()),
                )
                db.execute(
                    "INSERT INTO apps(id,user_id,slug,lifecycle,created,create_intent,revision) VALUES(?,?,?,'ready',?,?,?)",
                    (identifier, owner, name, time.time(), intent, revision),
                )
                db.execute(
                    "INSERT INTO configurations VALUES(?,?,?,?,?,?)",
                    (
                        identifier,
                        revision,
                        repository,
                        branch,
                        configuration,
                        digest(configuration),
                    ),
                )
                db.execute("UPDATE intents SET state='succeeded' WHERE id=?", (intent,))
                audit(
                    db,
                    actor["id"],
                    owner,
                    "app_adopted",
                    {"applicationId": identifier, "ownerId": owner, "sourceDeploymentId": active},
                    time.time(),
                )
        return Response(
            201 if prior is None else 200, {"data": {"applicationId": identifier, "imported": True}}
        )

    def reassign(self, request: Request) -> Response:
        b = self.broker
        actor, app = b.own(request, mutation=True)
        body = object_body(request.body, {"ownerId", "expectedOwnerId"})
        owner, expected = checked_uuid(body["ownerId"]), checked_uuid(body["expectedOwnerId"])
        with b.database.connect(write=True) as db:
            self.check(db)
            self.check_owner(db, owner)
            current = db.execute("SELECT user_id FROM apps WHERE id=?", (app["id"],)).fetchone()[0]
            if current != expected:
                raise HttpError(409, "OWNER_CONFLICT", "Ownership changed; reload first.")
            if db.execute(
                "SELECT 1 FROM intents WHERE app_id=? AND state NOT IN ('succeeded','failed')",
                (app["id"],),
            ).fetchone():
                raise HttpError(
                    409,
                    "APP_BUSY",
                    "Resolve this application's current operations before reassigning it.",
                )
            db.execute("UPDATE apps SET user_id=? WHERE id=?", (owner, app["id"]))
            audit(
                db,
                actor["id"],
                owner,
                "app_owner_changed",
                {"applicationId": app["id"], "previousOwnerId": current, "ownerId": owner},
                time.time(),
            )
        return Response(200, {"data": {"ownerId": owner}})

    def state(self, request: Request) -> Response:
        b = self.broker
        actor, app = b.own(request, mutation=True)
        if (
            not isinstance(request.body, dict)
            or "desiredRunning" not in request.body
            or set(request.body) - {"desiredRunning", "identityProviderConfirmed"}
            or type(request.body["desiredRunning"]) is not bool
        ):
            raise HttpError(400, "INVALID_REQUEST", "Supply desiredRunning as a boolean.")
        self.identity_consent(app["id"], request.body)
        action = "enable" if request.body["desiredRunning"] else "disable"
        return self.dispatch(
            request,
            actor,
            app,
            "app_" + action,
            "POST",
            f"/v1/applications/{app['id']}/{action}",
            {},
        )

    def delete_storage(self, request: Request) -> Response:
        b = self.broker
        actor, app = b.own(request, mutation=True)
        if (
            not isinstance(request.body, dict)
            or "confirmation" not in request.body
            or set(request.body) - {"confirmation", "identityProviderConfirmed"}
        ):
            raise HttpError(
                400, "INVALID_REQUEST", "Supply the typed application and storage confirmation."
            )
        self.identity_consent(app["id"], request.body)
        resource_id = checked_uuid(request.path_parameters["resource"])
        # Match single-use journal admission before the resource disappears.
        key, fingerprint = (
            request.idempotency_key(),
            digest(canonical({"path": request.path, "body": request.body})),
        )
        with b.database.connect() as db:
            prior = b.existing(db, actor["id"], key, fingerprint)
            if prior is not None:
                return b.intent_response(prior["id"], actor["id"], 202)
        status, resource = b.client.request("GET", f"/v1/storage/{resource_id}")
        if (
            status != 200
            or resource.get("applicationId") != app["id"]
            or resource.get("type") not in RESOURCE_OUTPUTS
        ):
            raise HttpError(404, "NOT_FOUND", "Storage not found for this application.")
        if request.body["confirmation"] != app["slug"] + " " + resource["type"]:
            raise HttpError(
                400,
                "CONFIRMATION_REQUIRED",
                "Type the application slug, a space, and the storage type.",
            )
        return self.dispatch(
            request,
            actor,
            app,
            "storage_delete",
            "DELETE",
            f"/v1/storage/{resource_id}",
            {"confirmation": resource["name"], "purge": True},
        )

    def dispatch(
        self,
        request: Request,
        actor: dict[str, Any],
        app: dict[str, Any],
        kind: str,
        method: str,
        path: str,
        body: dict[str, Any],
    ) -> Response:
        b = self.broker
        key, fingerprint = (
            request.idempotency_key(),
            digest(canonical({"path": request.path, "body": request.body})),
        )
        with b.database.connect(write=True) as db:
            self.check(db)
            prior = b.existing(db, actor["id"], key, fingerprint)
            if prior is None:
                if kind == "storage_delete":
                    cfg = db.execute(
                        "SELECT configuration FROM configurations WHERE app_id=? AND revision=(SELECT revision FROM apps WHERE id=?)",
                        (app["id"], app["id"]),
                    ).fetchone()
                    if cfg and any(
                        binding.resource_id == path.rsplit("/", 1)[1]
                        for binding in parse_configuration(cfg[0]).storage_bindings
                    ):
                        raise HttpError(
                            409,
                            "STORAGE_BOUND",
                            "Remove this resource's saved bindings before deleting storage.",
                        )
                operation_quota(b, db, actor["id"], app["id"])
                intent = b.record(
                    db,
                    actor["id"],
                    app["id"],
                    kind,
                    key,
                    fingerprint,
                    method,
                    path,
                    body,
                    str(uuid.uuid4()),
                )
            else:
                intent = prior["id"]
        b.journal.dispatch(intent)
        b.journal.wake.set()
        return b.intent_response(intent, actor["id"], 202)
