"""Staff and admin app authority over the existing project peer and resource handlers.

Staff and admins have the same app authority, without account-management step-up.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from urllib.parse import urlencode, urlsplit

from ...controller.deployment_config import branch_name, parse_configuration
from ...controller.http import HttpError, Request, Response
from ...controller.storage_contract import RESOURCE_OUTPUTS
from ...validation import flavor_reference, repository_url, slug
from ...validation import uuid as checked_uuid
from ..common import canonical, digest, strict_json
from . import sizing
from .accounts import audit
from .class_reads import ReadLimits, profile
from .client import ControllerUnavailable
from .journal import intent_model
from .resources import operation_quota
from .staff_policy import public_url

if TYPE_CHECKING:
    from .api import Broker


class Authority(NamedTuple):
    """The session behind an app-administration request."""

    sid: str


class AppManagement:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        # Authority is request-local, including concurrent Unix-server threads.
        # An app administration page reads about eight things per view
        # (details, settings, variables, storage, logs, deploy key, team), so
        # it gets a larger budget than the staff metadata views; reads still
        # run two at a time.
        self.read_limits = ReadLimits(burst=40, rate=4)
        self.context: ContextVar[Authority | None] = ContextVar("admin_app_actor", default=None)

    def routes(self) -> list[tuple[str, str, Any]]:
        b = self.broker
        return [
            ("GET", "/v1/all-apps", self.listing),
            ("POST", "/v1/all-apps", b.create),
            ("POST", "/v1/all-apps/adopt", self.adopt),
            ("GET", "/v1/people/eligible-owners", self.owners),
            ("PUT", "/v1/apps/{app}/owner", self.reassign),
            ("DELETE", "/v1/apps/{app}/storage/{resource}", self.delete_storage),
            ("GET", "/v1/apps/{app}/sizes", self.sizes),
            ("GET", "/v1/apps/{app}/resize-plan", self.resize_plan),
            ("GET", "/v1/apps/{app}/builder-size", self.builder_size),
            ("PUT", "/v1/apps/{app}/builder-size", self.set_builder_size),
        ]

    def handle(self, request: Request, handler: Callable[[Request], Response]) -> Response:
        user, sid = self.broker.accounts.admin(request, kind="staff")
        allowed = (
            {"q", "limit", "cursor"}
            if request.path == "/v1/people/eligible-owners"
            else {"limit", "cursor", "q", "ownerId", "status"}
            if request.path == "/v1/all-apps"
            else {"limit", "cursor"}
            if request.path.endswith("/deployments")
            else {"limit", "attention"}
            if request.path.endswith("/activity")
            else {"lines", "offset"}
            if request.path.endswith("/build-log")
            else {"stream"}
            if request.path.endswith("/logs")
            else {"flavor"}
            if request.path.endswith("/resize-plan")
            else set()
        )
        if (
            any(len(v) != 1 or not v[0] for v in request.query.values())
            or set(request.query) - allowed
            or request.method == "GET"
            and request.body is not None
        ):
            raise HttpError(400, "INVALID_REQUEST", "Unexpected request fields.")
        token = self.context.set(Authority(sid))
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
            response = handler(request)
            # Suppress a result if authority was revoked during a controller read.
            with self.broker.database.connect() as db:
                self.check(db)
            if request.method == "GET":
                route = request.path
                for value in request.path_parameters.values():
                    route = route.replace(value, "{id}")
                self.broker.class_reads.audit(
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
            self.broker.accounts.checked_actor(db, context.sid, kind="staff")

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
        if "plan" in body:
            self.sizing_plan(body["plan"], app["id"], model)
        if model.get("requiresMaintenance") is True:
            body["maintenance"] = True
        return body

    def sizes(self, request: Request) -> Response:
        self.broker.own(request)
        return Response(200, {"data": {"items": sizing.flavors(self.broker.client)}})

    def resize_plan(self, request: Request) -> Response:
        _actor, app = self.broker.own(request)
        if set(request.query) != {"flavor"}:
            raise HttpError(400, "INVALID_REQUEST", "Choose a size.")
        reference = flavor_reference(request.query["flavor"][0])
        model = self.observed(app["id"])
        plan = sizing.result(
            self.broker.client,
            f"/v1/applications/{app['id']}/resize-plan?" + urlencode({"flavor": reference}),
        )
        self.sizing_plan(plan, app["id"], model)
        return Response(200, {"data": plan})

    def builder_size(self, request: Request) -> Response:
        _actor, app = self.broker.own(request)
        value = sizing.result(self.broker.client, f"/v1/applications/{app['id']}/builder-size")
        if type(value.get("useDefault")) is not bool:
            raise ControllerUnavailable("invalid builder selection")
        return Response(
            200,
            {
                "data": {
                    "flavor": sizing.flavor(value.get("flavor")),
                    "defaultFlavor": sizing.flavor(value.get("defaultFlavor")),
                    "useDefault": value["useDefault"],
                }
            },
        )

    def set_builder_size(self, request: Request) -> Response:
        actor, app = self.broker.own(request, mutation=True)
        body = sizing.builder_body(request.body)
        return self.dispatch(
            request,
            actor,
            app,
            "builder_size",
            "PUT",
            f"/v1/applications/{app['id']}/builder-size",
            body,
        )

    def owners(self, request: Request) -> Response:
        if set(request.query) - {"limit", "cursor", "q"} or any(
            len(v) != 1 or not v[0] for v in request.query.values()
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid owner page fields.")
        limit = self.broker.class_reads.page_limit(request)
        search = request.query.get("q", ("",))[0]
        if len(search) > 64 or any(ord(c) < 32 for c in search):
            raise HttpError(400, "INVALID_FIELD", "Owner search exceeds its bounds.")
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clause = "(username LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\')"
        args: list[object] = [pattern, pattern]
        with self.broker.database.connect() as db:
            cursor = request.query.get("cursor", (None,))[0]
            if cursor:
                point = db.execute(
                    f"SELECT created,id FROM users WHERE {clause} AND id=?",
                    (*args, checked_uuid(cursor)),
                ).fetchone()
                if point is None:
                    raise HttpError(400, "INVALID_REQUEST", "Unknown owner page cursor.")
                clause += " AND (created<? OR (created=? AND id<?))"
                args += [point["created"], point["created"], point["id"]]
            rows = db.execute(
                f"SELECT id,username,display_name,role,enabled,status FROM users WHERE {clause} ORDER BY created DESC,id DESC LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
        items = [
            {
                "userId": row["id"],
                "username": profile(row["username"], 32),
                "displayName": profile(row["display_name"], 256),
                "role": row["role"],
                "enabled": bool(row["enabled"]),
                "status": row["status"],
            }
            for row in rows[:limit]
        ]
        return Response(
            200,
            {
                "data": {
                    "items": items,
                    "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None,
                    "truncated": len(rows) > limit,
                }
            },
        )

    def listing(self, request: Request) -> Response:
        b = self.broker
        viewer, _sid = b.auth.authenticate(request, kind="staff", touch=False)
        limit, cursor = b.class_reads.page_limit(request), request.query.get("cursor", (None,))[0]
        with b.database.connect() as db:
            parameters: list[object] = []
            where = "a.lifecycle NOT IN ('rejected','deleted')"
            search = request.query.get("q", ("",))[0].strip()
            if len(search) > 64:
                raise HttpError(400, "INVALID_REQUEST", "Search is too long.")
            if search:
                pattern = (
                    "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                where += " AND (a.slug LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\' OR u.display_name LIKE ? ESCAPE '\\')"
                parameters.extend([pattern] * 3)
            owner = request.query.get("ownerId", (None,))[0]
            if owner:
                where += " AND a.user_id=?"
                parameters.append(checked_uuid(owner))
            state = request.query.get("status", (None,))[0]
            status_sql = f"CASE WHEN a.lifecycle!='ready' THEN a.lifecycle WHEN json_extract(o.body,'$.acceptedDeployment') IS NULL THEN 'not_deployed' WHEN o.updated<{time.time() - 30:.6f} THEN 'unknown' WHEN json_extract(o.body,'$.desiredRunning')=0 THEN 'stopped' WHEN json_extract(o.body,'$.health.allocationHealthy')=1 AND json_extract(o.body,'$.health.routeHealthy')=1 THEN 'healthy' WHEN json_extract(o.body,'$.health.allocationHealthy')=0 OR json_extract(o.body,'$.health.routeHealthy')=0 THEN 'unhealthy' ELSE 'unknown' END"
            if state:
                if state not in {
                    "creating",
                    "not_deployed",
                    "stopped",
                    "healthy",
                    "unhealthy",
                    "unknown",
                    "attention",
                }:
                    raise HttpError(400, "INVALID_REQUEST", "Invalid app status.")
                where += " AND " + (
                    "EXISTS (SELECT 1 FROM intents i WHERE i.app_id=a.id AND i.state IN ('blocked','unknown'))"
                    if state == "attention"
                    else f"({status_sql})=?"
                )
                if state != "attention":
                    parameters.append(state)
            if cursor:
                point = db.execute(
                    f"SELECT a.created,a.id FROM apps a JOIN users u ON u.id=a.user_id LEFT JOIN observations o ON o.app_id=a.id WHERE {where} AND a.id=?",
                    (*parameters, checked_uuid(cursor)),
                ).fetchone()
                if point is None:
                    raise HttpError(400, "INVALID_REQUEST", "Unknown page cursor.")
                where += " AND (a.created<? OR (a.created=? AND a.id<?))"
                parameters.extend([point["created"], point["created"], point["id"]])
            # Owner names and the last cached observation come from SQLite in
            # the same read, so the list needs no per-row controller reads.
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT a.*,u.username AS owner_username,u.display_name AS owner_display_name,"
                    f"o.body AS observation,({status_sql}) AS app_state FROM apps a JOIN users u ON u.id=a.user_id"
                    f" LEFT JOIN observations o ON o.app_id=a.id WHERE {where}"
                    " ORDER BY a.created DESC,a.id DESC LIMIT ?",
                    (*parameters, limit + 1),
                )
            ]
        with b.database.connect() as db:
            for row in rows[:limit]:
                row["attention"] = [
                    {
                        **intent_model(item, diagnostic=True, viewer=viewer["id"]),
                        "appSlug": row["slug"],
                    }
                    for item in db.execute(
                        "SELECT * FROM intents WHERE app_id=? AND state IN ('blocked','unknown') ORDER BY created DESC,id DESC",
                        (row["id"],),
                    )
                ]
        # Existence checks use bounded SQLite-only project reads, not live health.
        b.journal.reconcile_page(rows)
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
                            "appState": row["app_state"],
                            "attention": row["attention"],
                            **self.catalog_extras(row),
                        }
                        for row in rows[:limit]
                        if row["lifecycle"] != "deleted"
                    ],
                    "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None,
                    "truncated": len(rows) > limit,
                }
            },
        )

    @staticmethod
    def catalog_extras(row: dict[str, Any]) -> dict[str, Any]:
        """Additive list fields: the owner's names and the last observed URL and deploy."""
        observed: dict[str, Any] = {}
        if row["observation"] is not None:
            try:
                value = strict_json(row["observation"].encode())
                observed = value if isinstance(value, dict) else {}
            except ValueError:
                observed = {}
        accepted = observed.get("acceptedDeployment")
        deployed = accepted.get("acceptedAt") if isinstance(accepted, dict) else None
        return {
            "ownerUsername": profile(row["owner_username"], 32),
            "ownerDisplayName": profile(row["owner_display_name"], 256),
            "url": public_url(observed.get("url")),
            "lastDeployedAt": deployed
            if isinstance(deployed, str) and len(deployed) <= 40
            else None,
        }

    def adopt(self, request: Request) -> Response:
        b = self.broker
        actor, _sid = b.auth.authenticate(request, kind="staff", mutation=True)
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
        body = b.mutation_body(request, {"ownerId", "expectedOwnerId"})
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
                    "Finish the previous change in the app's Overview before changing the owner.",
                )
            db.execute("UPDATE apps SET user_id=? WHERE id=?", (owner, app["id"]))
            # The new owner isn't also a member; other teammates stay.
            db.execute("DELETE FROM app_members WHERE app_id=? AND user_id=?", (app["id"], owner))
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
            or set(request.body) - {"desiredRunning"}
            or type(request.body["desiredRunning"]) is not bool
        ):
            raise HttpError(400, "INVALID_REQUEST", "Supply desiredRunning as a boolean.")
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

    def restart(self, request: Request) -> Response:
        b = self.broker
        actor, app = b.own(request, mutation=True)
        b.mutation_body(request, set())
        return self.dispatch(
            request,
            actor,
            app,
            "app_restart",
            "POST",
            f"/v1/applications/{app['id']}/restart",
            {},
        )

    def delete_storage(self, request: Request) -> Response:
        b = self.broker
        actor, app = b.own(request, mutation=True)
        if (
            not isinstance(request.body, dict)
            or "confirmation" not in request.body
            or set(request.body) - {"confirmation"}
        ):
            raise HttpError(
                400, "INVALID_REQUEST", "Supply the typed application and storage confirmation."
            )
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
