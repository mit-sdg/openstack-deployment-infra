"""Closed owner API; ownership and quota decisions precede controller access."""

from __future__ import annotations

import sqlite3
import sys
import time
import uuid
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

from ...controller.deployment_config import branch_name, parse_configuration
from ...controller.http import HttpError, Request, Response, Router
from ...validation import ValidationError, commit, repository_url, slug
from ...validation import uuid as checked_uuid
from ..common import canonical, digest, object_body, strict_json, utc
from ..config import Config
from .auth import Auth
from .client import ControllerUnavailable, ProjectClient
from .database import Database
from .journal import Journal, intent_model
from .staff import StaffReads

RESERVED = {"admin", "api", "auth", "status", "www", "platform", "class"}
DEFAULT_CONFIGURATION: dict[str, Any] = {
    "schemaVersion": 1,
    "build": {"runtime": "node", "packages": ["."], "buildScript": None, "startScript": "start"},
    "runtime": {"port": 3000, "healthPath": "/health"},
    "storageBindings": [],
}


class Broker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.database = Database(config)
        self.auth = Auth(config, self.database)
        self.client = ProjectClient(config.controller_socket, config.controller_timeout)
        self.journal = Journal(self.database, self.client)
        self.staff = StaffReads(self)

    def router(self) -> Router:
        router = Router()
        common = {
            ("GET", "/v1/auth/options"),
            ("POST", "/v1/auth/login"),
            ("GET", "/v1/session"),
            ("POST", "/v1/logout"),
            ("GET", "/v1/health"),
        }
        routes = [
            ("GET", "/v1/auth/options", self.auth.options),
            ("POST", "/v1/auth/login", self.auth.login),
            ("GET", "/v1/session", self.session),
            ("POST", "/v1/logout", self.auth.logout),
            ("GET", "/v1/apps", self.apps),
            ("POST", "/v1/apps", self.create),
            ("GET", "/v1/apps/{app}", self.app),
            ("GET", "/v1/apps/{app}/configuration", self.configuration),
            ("PUT", "/v1/apps/{app}/configuration", self.save),
            ("POST", "/v1/apps/{app}/deployments", self.deploy),
            ("GET", "/v1/apps/{app}/deployments", self.history),
            ("GET", "/v1/apps/{app}/deployments/{deployment}", self.deployment),
            ("GET", "/v1/apps/{app}/deployments/{deployment}/build-log", self.build_log),
            ("GET", "/v1/intents", self.intents),
            ("GET", "/v1/intents/{intent}", self.intent),
            ("POST", "/v1/intents/{intent}/resume", self.resume),
            ("GET", "/v1/health", self.health),
            ("GET", "/v1/staff/owners", self.staff.owners),
            ("GET", "/v1/staff/owners/{owner}", self.staff.owner),
            ("GET", "/v1/staff/apps", self.staff.apps),
            ("GET", "/v1/staff/apps/{app}", self.staff.app),
            ("GET", "/v1/staff/apps/{app}/deployments", self.staff.deployments),
            ("GET", "/v1/staff/apps/{app}/deployments/{deployment}", self.staff.deployment),
            ("GET", "/v1/staff/operations", self.staff.operations),
        ]
        for method, path, handler in routes:

            def guarded(request: Request, handler: Any = handler, route: str = path) -> Response:
                try:
                    if route.startswith("/v1/staff/"):
                        return self.staff.handle(request, handler, route)
                    if (request.method, route) not in common:
                        self.auth.authenticate(request, kind="owner")
                    if request.method == "GET" and request.body is not None:
                        raise HttpError(
                            400, "INVALID_REQUEST", "Read requests cannot contain a body."
                        )
                    allowed_query = (
                        {"limit", "cursor"}
                        if request.path in {"/v1/apps", "/v1/intents"}
                        or request.path.endswith("/deployments")
                        else {"lines", "offset"}
                        if request.path.endswith("/build-log")
                        else set()
                    )
                    if set(request.query) - allowed_query:
                        raise HttpError(400, "INVALID_REQUEST", "Unexpected query fields.")
                    return handler(request)  # type: ignore[no-any-return]
                except ValidationError:
                    raise HttpError(
                        400, "INVALID_FIELD", "A field does not match the supported format."
                    ) from None
                except (sqlite3.OperationalError, ControllerUnavailable) as error:
                    print(f"broker exception={type(error).__name__} route={route}", file=sys.stderr)
                    raise HttpError(
                        503,
                        "STATE_UNAVAILABLE",
                        "The service is temporarily unavailable.",
                        retryable=True,
                    ) from None
                except HttpError:
                    raise
                except Exception as error:
                    print(f"broker exception={type(error).__name__} route={route}", file=sys.stderr)
                    raise

            router.add(method, path, guarded)
        return router

    def health(self, request: Request) -> Response:
        if request.query or request.body is not None:
            raise HttpError(400, "INVALID_REQUEST", "Invalid readiness request.")
        return Response(200, {"data": {"ready": True, "protocolVersion": 3}})

    def own(
        self, request: Request, *, mutation: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        user, _sid = self.auth.authenticate(request, mutation=mutation)
        identifier = checked_uuid(request.path_parameters["app"])
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM apps WHERE id=? AND user_id=? AND lifecycle!='rejected'",
                (identifier, user["id"]),
            ).fetchone()
            if row is None:
                raise HttpError(404, "NOT_FOUND", "Application not found.")
            return user, dict(row)

    def quota(self, user_id: str) -> dict[str, Any]:
        with self.database.connect() as db:
            policy = db.execute("SELECT * FROM quotas WHERE user_id=?", (user_id,)).fetchone()
            apps = db.execute(
                "SELECT lifecycle,COUNT(*) n FROM apps WHERE user_id=? AND lifecycle!='rejected' GROUP BY lifecycle",
                (user_id,),
            ).fetchall()
            held = db.execute(
                "SELECT COUNT(*) FROM intents WHERE user_id=? AND kind='deploy' AND state NOT IN ('succeeded','failed')",
                (user_id,),
            ).fetchone()[0]
        return {
            "apps": {
                "limit": self.config.app_limit if policy is None else policy["apps"],
                "used": sum(row["n"] for row in apps if row["lifecycle"] == "ready"),
                "reserved": sum(row["n"] for row in apps if row["lifecycle"] != "ready"),
            },
            "concurrentOperations": {
                "limit": self.config.concurrency_limit if policy is None else policy["concurrent"],
                "used": held,
                "reserved": 0,
            },
        }

    def session(self, request: Request) -> Response:
        response = self.auth.bootstrap(request)
        body = dict(cast(dict[str, Any], response.body))
        body["data"]["quota"] = self.quota(body["data"]["user"]["id"])
        return Response(200, body)

    @staticmethod
    def page(request: Request, items: list[dict[str, Any]], identity: str) -> dict[str, Any]:
        if set(request.query) - {"limit", "cursor"} or any(
            len(v) != 1 for v in request.query.values()
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid pagination fields.")
        try:
            limit = int(request.query.get("limit", ("50",))[0])
        except ValueError:
            raise HttpError(400, "INVALID_REQUEST", "Invalid page limit.") from None
        if not 1 <= limit <= 100:
            raise HttpError(400, "INVALID_REQUEST", "Page limit must be 1–100.")
        start = 0
        cursor = request.query.get("cursor", (None,))[0]
        if cursor is not None:
            positions = [i for i, item in enumerate(items) if item[identity] == cursor]
            if not positions:
                raise HttpError(400, "INVALID_REQUEST", "Unknown page cursor.")
            start = positions[0] + 1
        selected = items[start : start + limit]
        more = start + limit < len(items)
        return {
            "items": selected,
            "nextCursor": selected[-1][identity] if selected and more else None,
            "truncated": more,
        }

    def app_model(self, app: dict[str, Any], *, cache_seconds: int = 2) -> dict[str, Any]:
        result: dict[str, Any] = {
            "applicationId": app["id"],
            "slug": app["slug"],
            "lifecycleState": app["lifecycle"],
            "savedRevision": app["revision"],
            "url": None,
            "desiredRunning": False,
            "activeDeploymentId": None,
            "acceptedDeployment": None,
            "health": None,
            "stale": True,
            "observedAt": None,
        }
        with self.database.connect() as db:
            cached = db.execute(
                "SELECT * FROM observations WHERE app_id=?", (app["id"],)
            ).fetchone()
        if cached is not None:
            result.update(strict_json(cached["body"].encode()))
            result.update(savedRevision=app["revision"], lifecycleState=app["lifecycle"])
            if time.time() - cached["updated"] < cache_seconds:
                return result
            result["stale"] = True
        if app["lifecycle"] == "ready":
            try:
                status, observed = self.client.request("GET", f"/v1/applications/{app['id']}")
                if status != 200 or observed.get("applicationId") != app["id"]:
                    raise ControllerUnavailable("invalid app observation")
                url = observed.get("url")
                if (
                    not isinstance(url, str)
                    or urlsplit(url).scheme != "https"
                    or not urlsplit(url).hostname
                    or urlsplit(url).username
                ):
                    raise ControllerUnavailable("invalid application URL")
                accepted = observed.get("deployment")
                active = observed.get("activeDeploymentId")
                if active is not None and (
                    not isinstance(accepted, dict)
                    or accepted.get("deploymentId") != checked_uuid(active)
                ):
                    raise ControllerUnavailable("invalid accepted deployment")
                if cached is not None and result["activeDeploymentId"] and not active:
                    raise ControllerUnavailable("accepted evidence disappeared")
                if accepted is not None:
                    accepted = {
                        key: accepted.get(key)
                        for key in ("deploymentId", "sourceCommit", "acceptedAt")
                    }
                    accepted["acceptedAt"] = utc(accepted["acceptedAt"])
                live = observed.get("live")
                if not isinstance(live, dict):
                    raise ControllerUnavailable("invalid health observation")
                live = {
                    key: live.get(key)
                    for key in (
                        "schedulerAvailable",
                        "routeAvailable",
                        "schedulerState",
                        "allocationHealthy",
                        "routeHealthy",
                        "checkedAt",
                    )
                }
                result.update(
                    {
                        "url": observed.get("url"),
                        "desiredRunning": observed.get("desiredRunning") is True,
                        "activeDeploymentId": observed.get("activeDeploymentId"),
                        "acceptedDeployment": accepted,
                        "health": live,
                        "stale": False,
                        "observedAt": utc(time.time()),
                    }
                )
                with self.database.connect(write=True) as db:
                    db.execute(
                        "INSERT INTO observations VALUES(?,?,?) ON CONFLICT(app_id) DO UPDATE SET body=excluded.body,updated=excluded.updated",
                        (app["id"], canonical(result), time.time()),
                    )
            except (ControllerUnavailable, ValidationError):
                pass
        return result

    def apps(self, request: Request) -> Response:
        user, _sid = self.auth.authenticate(request)
        page = self.owned_page(request, user["id"], "apps")
        page["items"] = [self.app_model(item) for item in page["items"]]
        return Response(200, {"data": {**page, "quota": self.quota(user["id"])}})

    def owned_page(self, request: Request, owner: str, table: str) -> dict[str, Any]:
        if table not in {"apps", "intents"}:
            raise ValueError("invalid local collection")
        if set(request.query) - {"limit", "cursor"} or any(
            len(v) != 1 for v in request.query.values()
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid pagination fields.")
        try:
            limit = int(request.query.get("limit", ("50",))[0])
        except ValueError:
            raise HttpError(400, "INVALID_REQUEST", "Invalid page limit.") from None
        if not 1 <= limit <= 100:
            raise HttpError(400, "INVALID_REQUEST", "Page limit must be 1–100.")
        condition = "user_id=?" + (" AND lifecycle!='rejected'" if table == "apps" else "")
        parameters: list[object] = [owner]
        with self.database.connect() as db:
            cursor = request.query.get("cursor", (None,))[0]
            if cursor is not None:
                point = db.execute(
                    f"SELECT created,id FROM {table} WHERE {condition} AND id=?",
                    (*parameters, checked_uuid(cursor)),
                ).fetchone()
                if point is None:
                    raise HttpError(400, "INVALID_REQUEST", "Unknown page cursor.")
                condition += " AND (created<? OR (created=? AND id<?))"
                parameters.extend([point["created"], point["created"], point["id"]])
            rows = [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM {table} WHERE {condition} ORDER BY created DESC,id DESC LIMIT ?",
                    (*parameters, limit + 1),
                )
            ]
        return {
            "items": rows[:limit],
            "nextCursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            "truncated": len(rows) > limit,
        }

    def app(self, request: Request) -> Response:
        _user, app = self.own(request)
        return Response(200, {"data": self.app_model(app)})

    def existing(self, db: sqlite3.Connection, user: str, key: str, fingerprint: str) -> Any:
        row = db.execute(
            "SELECT * FROM intents WHERE user_id=? AND client_key=?", (user, key)
        ).fetchone()
        if row is not None and row["fingerprint"] != fingerprint:
            raise HttpError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This request key already belongs to a different action.",
            )
        return row

    @staticmethod
    def record(
        db: sqlite3.Connection,
        user: str,
        app: str,
        kind: str,
        key: str,
        fingerprint: str,
        method: str,
        path: str,
        body: object,
        controller_key: str,
    ) -> str:
        identifier, now = str(uuid.uuid4()), time.time()
        db.execute(
            "INSERT INTO intents(id,user_id,app_id,kind,client_key,controller_key,fingerprint,method,path,body,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,'prepared',?,?)",
            (
                identifier,
                user,
                app,
                kind,
                key,
                controller_key,
                fingerprint,
                method,
                path,
                canonical(body),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO audit(user_id,app_id,intent_id,action,created) VALUES(?,?,?,?,?)",
            (user, app, identifier, kind, now),
        )
        return identifier

    def create(self, request: Request) -> Response:
        user, _sid = self.auth.authenticate(request, mutation=True)
        body = object_body(request.body, {"slug"})
        name = slug(body["slug"])
        if name in RESERVED:
            raise HttpError(409, "SLUG_UNAVAILABLE", "Choose another application name.")
        key, fingerprint = (
            request.idempotency_key(),
            digest(canonical({"path": request.path, "body": body})),
        )
        with self.database.connect(write=True) as db:
            existing = self.existing(db, user["id"], key, fingerprint)
            if existing is not None:
                identifier, app_id = existing["id"], existing["app_id"]
            else:
                policy = db.execute(
                    "SELECT apps FROM quotas WHERE user_id=?", (user["id"],)
                ).fetchone()
                count = db.execute(
                    "SELECT COUNT(*) FROM apps WHERE user_id=? AND lifecycle!='rejected'",
                    (user["id"],),
                ).fetchone()[0]
                if count >= (self.config.app_limit if policy is None else policy[0]):
                    raise HttpError(409, "QUOTA_EXCEEDED", "Your application quota is full.")
                if db.execute("SELECT 1 FROM apps WHERE slug=?", (name,)).fetchone():
                    raise HttpError(409, "SLUG_UNAVAILABLE", "Choose another application name.")
                app_id = str(uuid.uuid4())
                identifier = self.record(
                    db,
                    user["id"],
                    app_id,
                    "create_app",
                    key,
                    fingerprint,
                    "POST",
                    "/v1/applications",
                    {"slug": name},
                    app_id,
                )
                db.execute(
                    "INSERT INTO apps(id,user_id,slug,lifecycle,created,create_intent) VALUES(?,?,?,'creating',?,?)",
                    (app_id, user["id"], name, time.time(), identifier),
                )
        self.journal.dispatch(identifier)
        with self.database.connect() as db:
            intent = db.execute("SELECT * FROM intents WHERE id=?", (identifier,)).fetchone()
            app = dict(db.execute("SELECT * FROM apps WHERE id=?", (app_id,)).fetchone())
        self.journal.wake.set()
        if intent["state"] == "failed":
            raise HttpError(
                409, "SLUG_UNAVAILABLE", "The controller could not accept this application name."
            )
        return Response(
            201 if intent["state"] == "succeeded" else 202,
            {"data": {"app": self.app_model(app), "intent": intent_model(intent)}},
        )

    def configuration(self, request: Request) -> Response:
        _user, app = self.own(request)
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM configurations WHERE app_id=? AND revision=?",
                (app["id"], app["revision"]),
            ).fetchone()
        return Response(
            200,
            {
                "data": {
                    "revision": app["revision"],
                    "repository": "" if row is None else row["repository"],
                    "branch": "main" if row is None else row["branch"],
                    "configuration": DEFAULT_CONFIGURATION
                    if row is None
                    else strict_json(row["configuration"].encode()),
                    "configurationSha256": None if row is None else row["sha256"],
                }
            },
        )

    def save(self, request: Request) -> Response:
        user, app = self.own(request, mutation=True)
        body = object_body(
            request.body, {"expectedRevision", "repository", "branch", "configuration"}
        )
        repository, branch = repository_url(body["repository"]), branch_name(body["branch"])
        configuration = parse_configuration(body["configuration"]).canonical_json()
        if len(configuration.encode()) > 65536 or body["configuration"].get("storageBindings"):
            raise HttpError(
                400, "INVALID_FIELD", "Storage bindings are not available in this phase."
            )
        revision = body["expectedRevision"]
        if type(revision) is not int or revision < 0:
            raise HttpError(400, "INVALID_FIELD", "Invalid configuration revision.")
        key, fingerprint = (
            request.idempotency_key(),
            digest(canonical({"path": request.path, "body": body})),
        )
        with self.database.connect(write=True) as db:
            existing = self.existing(db, user["id"], key, fingerprint)
            if existing is None:
                current = db.execute(
                    "SELECT revision FROM apps WHERE id=?", (app["id"],)
                ).fetchone()[0]
                if current != revision:
                    raise HttpError(
                        409,
                        "REVISION_CONFLICT",
                        "Settings changed in another tab. Reload before saving.",
                    )
                db.execute(
                    "INSERT INTO configurations VALUES(?,?,?,?,?,?)",
                    (
                        app["id"],
                        revision + 1,
                        repository,
                        branch,
                        configuration,
                        digest(configuration),
                    ),
                )
                db.execute("UPDATE apps SET revision=? WHERE id=?", (revision + 1, app["id"]))
                identifier = self.record(
                    db,
                    user["id"],
                    app["id"],
                    "save_configuration",
                    key,
                    fingerprint,
                    "PUT",
                    "/local/configuration",
                    {"revision": revision + 1},
                    str(uuid.uuid4()),
                )
                db.execute("UPDATE intents SET state='succeeded' WHERE id=?", (identifier,))
        return Response(
            200, {"data": {"revision": revision + 1, "configurationSha256": digest(configuration)}}
        )

    def deploy(self, request: Request) -> Response:
        user, app = self.own(request, mutation=True)
        body = object_body(request.body, {"configurationRevision", "commit"})
        sha = commit(body["commit"])
        revision = body["configurationRevision"]
        if type(revision) is not int or revision < 1:
            raise HttpError(400, "INVALID_FIELD", "Save a valid configuration before deploying.")
        key, fingerprint = (
            request.idempotency_key(),
            digest(canonical({"path": request.path, "body": body})),
        )
        with self.database.connect(write=True) as db:
            existing = self.existing(db, user["id"], key, fingerprint)
            if existing is None:
                current = db.execute("SELECT * FROM apps WHERE id=?", (app["id"],)).fetchone()
                if current["lifecycle"] != "ready" or current["revision"] != revision:
                    raise HttpError(
                        409, "REVISION_CONFLICT", "Reload the saved settings before deploying."
                    )
                held = db.execute(
                    "SELECT * FROM intents WHERE user_id=? AND kind='deploy' AND state NOT IN ('succeeded','failed')",
                    (user["id"],),
                ).fetchall()
                policy = db.execute(
                    "SELECT concurrent FROM quotas WHERE user_id=?", (user["id"],)
                ).fetchone()
                if any(row["app_id"] == app["id"] for row in held):
                    raise HttpError(
                        409, "APP_BUSY", "Resolve the existing deployment before starting another."
                    )
                if len(held) >= (self.config.concurrency_limit if policy is None else policy[0]):
                    raise HttpError(
                        409, "QUOTA_EXCEEDED", "Wait for your current operation to finish."
                    )
                cfg = db.execute(
                    "SELECT * FROM configurations WHERE app_id=? AND revision=?",
                    (app["id"], revision),
                ).fetchone()
                controller_body = {
                    "repository": cfg["repository"],
                    "requestedRef": cfg["branch"],
                    "commit": sha,
                    "configurationRevision": revision,
                    "configuration": strict_json(cfg["configuration"].encode()),
                }
                identifier = self.record(
                    db,
                    user["id"],
                    app["id"],
                    "deploy",
                    key,
                    fingerprint,
                    "POST",
                    f"/v1/applications/{app['id']}/deployments",
                    controller_body,
                    str(uuid.uuid4()),
                )
            else:
                identifier = existing["id"]
        self.journal.dispatch(identifier)
        self.journal.wake.set()
        return self.intent_response(identifier, user["id"], 202)

    def intent_response(self, identifier: str, user: str, status: int = 200) -> Response:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE id=? AND user_id=?", (identifier, user)
            ).fetchone()
            if row is None:
                raise HttpError(404, "NOT_FOUND", "Operation not found.")
            model = intent_model(row)
            app = db.execute(
                "SELECT slug FROM apps WHERE id=? AND user_id=?", (row["app_id"], user)
            ).fetchone()
            model["appSlug"] = None if app is None else app["slug"]
            return Response(status, {"data": model})

    def intents(self, request: Request) -> Response:
        user, _sid = self.auth.authenticate(request)
        page = self.owned_page(request, user["id"], "intents")
        with self.database.connect() as db:
            slugs = {
                row["id"]: row["slug"]
                for row in db.execute("SELECT id,slug FROM apps WHERE user_id=?", (user["id"],))
            }
        page["items"] = [
            {**intent_model(row), "appSlug": slugs.get(row["app_id"])} for row in page["items"]
        ]
        return Response(200, {"data": page})

    def intent(self, request: Request) -> Response:
        user, _sid = self.auth.authenticate(request)
        return self.intent_response(checked_uuid(request.path_parameters["intent"]), user["id"])

    def resume(self, request: Request) -> Response:
        user, _sid = self.auth.authenticate(request, mutation=True)
        object_body(request.body, set())
        identifier = checked_uuid(request.path_parameters["intent"])
        with self.database.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM intents WHERE id=? AND user_id=?", (identifier, user["id"])
            ).fetchone()
            if row is None:
                raise HttpError(404, "NOT_FOUND", "Operation not found.")
            if row["state"] in {"blocked", "unknown"}:
                db.execute(
                    "UPDATE intents SET state='prepared',next_retry=0 WHERE id=?", (identifier,)
                )
        self.journal.dispatch(identifier)
        self.journal.wake.set()
        return self.intent_response(identifier, user["id"], 202)

    def read_project(self, path: str, app: str, *, paged: bool = False) -> dict[str, Any]:
        status, result = self.client.request("GET", path)
        if status == 404:
            raise HttpError(404, "NOT_FOUND", "Deployment not found.")
        if status == 400:
            raise HttpError(400, "INVALID_QUERY", "The requested page or log bounds are invalid.")
        if status != 200:
            raise ControllerUnavailable("read unavailable")
        items = result.get("items") if paged else [result]
        if not isinstance(items, list) or any(
            not isinstance(item, dict) or item.get("applicationId") != app for item in items
        ):
            raise HttpError(404, "NOT_FOUND", "Deployment not found.")
        allowed = {
            "deploymentId",
            "applicationId",
            "status",
            "snapshotKind",
            "repositoryCommit",
            "requestedRef",
            "configurationRevision",
            "configuration",
            "configurationSha256",
            "sourceRepository",
            "imageDigest",
            "cleanupState",
            "requestedAt",
            "updatedAt",
            "acceptedAt",
            "lastHealthyAt",
        }
        models = []
        for item in items:
            model = {key: value for key, value in item.items() if key in allowed}
            for name in ("requestedAt", "updatedAt", "acceptedAt", "lastHealthyAt"):
                if name in model:
                    model[name] = utc(model[name])
            models.append(model)
        return (
            {
                "items": models,
                "nextCursor": result.get("nextCursor"),
                "truncated": result.get("truncated", False),
            }
            if paged
            else models[0]
        )

    def history(self, request: Request) -> Response:
        _user, app = self.own(request)
        if set(request.query) - {"limit", "cursor"} or any(
            len(v) != 1 for v in request.query.values()
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid history query.")
        try:
            limit = int(request.query.get("limit", ("50",))[0])
            if not 1 <= limit <= 100:
                raise ValueError("limit")
            if "cursor" in request.query:
                checked_uuid(request.query["cursor"][0])
        except (ValueError, ValidationError):
            raise HttpError(400, "INVALID_QUERY", "Page limit or cursor is invalid.") from None
        query = urlencode({key: values[0] for key, values in request.query.items()})
        result = self.read_project(
            f"/v1/applications/{app['id']}/deployments" + ("?" + query if query else ""),
            app["id"],
            paged=True,
        )
        return Response(200, {"data": result})

    def deployment(self, request: Request) -> Response:
        _user, app = self.own(request)
        identifier = checked_uuid(request.path_parameters["deployment"])
        return Response(
            200, {"data": self.read_project(f"/v1/deployments/{identifier}", app["id"])}
        )

    def build_log(self, request: Request) -> Response:
        _user, app = self.own(request)
        identifier = checked_uuid(request.path_parameters["deployment"])
        if set(request.query) - {"lines", "offset"} or any(
            len(v) != 1 for v in request.query.values()
        ):
            raise HttpError(400, "INVALID_REQUEST", "Invalid build-log query.")
        try:
            lines = int(request.query.get("lines", ("200",))[0])
            offset = int(request.query.get("offset", ("0",))[0])
        except ValueError:
            raise HttpError(400, "INVALID_REQUEST", "Invalid log bounds.") from None
        if not 1 <= lines <= 1000 or not 0 <= offset <= 1048576:
            raise HttpError(400, "INVALID_REQUEST", "Invalid log bounds.")
        self.read_project(f"/v1/deployments/{identifier}", app["id"])
        status, result = self.client.request(
            "GET", f"/v1/deployments/{identifier}/build-log?lines={lines}&offset={offset}"
        )
        if (
            status != 200
            or result.get("deploymentId") != identifier
            or not isinstance(result.get("text"), str)
            or len(result["text"].encode()) > 262144
        ):
            raise ControllerUnavailable("invalid log result")
        return Response(
            200,
            {
                "data": {
                    key: result.get(key)
                    for key in ("deploymentId", "text", "state", "nextOffset", "truncated")
                }
            },
        )
