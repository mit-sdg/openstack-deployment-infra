"""Closed owner API; ownership and quota decisions precede controller access."""

from __future__ import annotations

import sqlite3
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

from ...controller.deployment_config import branch_name, parse_configuration
from ...controller.http import HttpError, Request, Response, Router
from ...validation import ValidationError, commit, repository_url, slug
from ...validation import uuid as checked_uuid
from ..common import canonical, digest, object_body, strict_json, utc
from ..config import Config
from . import resources
from .accounts import Accounts
from .admin_apps import AdminApps
from .auth import Auth
from .client import ControllerUnavailable, ProjectClient
from .database import Database
from .journal import ADMIN_ONLY_INTENTS, Journal, intent_model
from .members import Members, activity
from .runtime_logs import RuntimeLogs
from .source_keys import SourceKeys
from .staff import StaffReads

RESERVED = {"admin", "api", "auth", "status", "www", "platform", "class"}
# Apps a user may work on: their own, and those they're a team member of.
ACCESS = "(user_id=? OR id IN (SELECT app_id FROM app_members WHERE user_id=?))"
DEFAULT_CONFIGURATION: dict[str, Any] = {
    "schemaVersion": 1,
    "build": {"runtime": "node", "packages": ["."], "buildScript": None, "startScript": "start"},
    "runtime": {"port": 3000, "healthPath": "/health"},
    "storageBindings": [],
}


class Broker:
    environment_metadata = resources.environment_metadata
    environment = resources.environment
    mutate_environment = resources.mutate_environment
    storage_resources = resources.storage_resources
    storage = resources.storage
    mutate_storage = resources.mutate_storage
    validate_bindings = resources.validate_bindings

    def __init__(self, config: Config) -> None:
        self.config = config
        self.database = Database(config)
        self.auth = Auth(config, self.database)
        self.client = ProjectClient(config.controller_socket, config.controller_timeout)
        self.journal = Journal(self.database, self.client)
        self.staff = StaffReads(self)
        self.accounts = Accounts(self)
        self.admin_apps = AdminApps(self)
        self.runtime_logs = RuntimeLogs(self)
        self.source_keys = SourceKeys(self)
        self.members = Members(self)
        self.request_actor: ContextVar[tuple[str, str | None] | None] = ContextVar(
            "app_request_actor", default=None
        )

    def router(self) -> Router:
        router = Router()
        common = {
            ("GET", "/v1/auth/options"),
            ("POST", "/v1/auth/login"),
            ("GET", "/v1/session"),
            ("POST", "/v1/logout"),
            ("POST", "/v1/auth/token-info"),
            ("POST", "/v1/auth/enroll"),
            ("POST", "/v1/auth/enroll/finish"),
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
            ("GET", "/v1/apps/{app}/environment", self.environment),
            ("PUT", "/v1/apps/{app}/environment/{key}", self.mutate_environment),
            ("DELETE", "/v1/apps/{app}/environment/{key}", self.mutate_environment),
            ("GET", "/v1/apps/{app}/storage", self.storage),
            ("POST", "/v1/apps/{app}/storage", self.mutate_storage),
            ("POST", "/v1/apps/{app}/storage/{resource}/verify", self.mutate_storage),
            ("POST", "/v1/apps/{app}/storage/{resource}/rotate", self.mutate_storage),
            ("POST", "/v1/apps/{app}/state", self.admin_apps.state),
            ("POST", "/v1/apps/{app}/restart", self.admin_apps.restart),
            ("POST", "/v1/apps/{app}/deployments", self.deploy),
            ("GET", "/v1/apps/{app}/deployments", self.history),
            ("GET", "/v1/apps/{app}/deployments/{deployment}", self.deployment),
            ("GET", "/v1/apps/{app}/deployments/{deployment}/build-log", self.build_log),
            ("GET", "/v1/apps/{app}/deployments/{deployment}/startup-log", self.startup_log),
            ("GET", "/v1/apps/{app}/logs", self.runtime_logs.handle),
            ("GET", "/v1/apps/{app}/activity", lambda request: activity(self, request)),
            ("GET", "/v1/intents", self.intents),
            ("GET", "/v1/intents/{intent}", self.intent),
            ("POST", "/v1/intents/{intent}/resume", self.resume),
            ("GET", "/v1/health", self.health),
            ("POST", "/v1/auth/token-info", self.accounts.token_info),
            ("POST", "/v1/auth/enroll", self.accounts.enroll),
            ("POST", "/v1/auth/enroll/finish", self.accounts.finish),
            ("GET", "/v1/accounts", self.accounts.listing),
            ("POST", "/v1/accounts", self.accounts.create),
            ("PATCH", "/v1/accounts/{user}", self.accounts.change),
            ("PUT", "/v1/accounts/{user}/quotas", self.accounts.quotas),
            ("GET", "/v1/account-audit", self.accounts.history),
            ("POST", "/v1/reauthenticate", self.accounts.reauthenticate),
            ("GET", "/v1/staff/owners", self.staff.owners),
            ("GET", "/v1/staff/owners/{owner}", self.staff.owner),
            ("GET", "/v1/staff/apps", self.staff.apps),
            ("GET", "/v1/staff/apps/{app}", self.staff.app),
            ("GET", "/v1/staff/apps/{app}/deployments", self.staff.deployments),
            ("GET", "/v1/staff/apps/{app}/deployments/{deployment}", self.staff.deployment),
            ("GET", "/v1/staff/operations", self.staff.operations),
        ]
        routes.extend(self.source_keys.routes("/v1/apps"))
        routes.extend(self.members.routes("/v1/apps"))
        routes.extend(self.admin_apps.routes())
        for method, path, handler in routes:

            def guarded(request: Request, handler: Any = handler, route: str = path) -> Response:
                actor_token = self.request_actor.set(None)
                try:
                    if route.startswith("/v1/admin-apps"):
                        return self.admin_apps.handle(request, handler)
                    if route.startswith("/v1/staff/"):
                        return self.staff.handle(request, handler, route)
                    if route in {
                        "/v1/accounts",
                        "/v1/accounts/{user}",
                        "/v1/accounts/{user}/quotas",
                        "/v1/account-audit",
                        "/v1/reauthenticate",
                    }:
                        self.auth.authenticate(
                            request, kind="admin", mutation=request.method != "GET", touch=False
                        )
                        if request.method == "GET" and request.body is not None:
                            raise HttpError(
                                400, "INVALID_REQUEST", "Read requests cannot contain a body."
                            )
                        return cast(Response, handler(request))
                    if (request.method, route) not in common:
                        self.auth.authenticate(request, touch=False)
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
                        else {"stream"}
                        if request.path.endswith("/logs")
                        else {"limit"}
                        if request.path.endswith("/activity")
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
                finally:
                    self.request_actor.reset(actor_token)

            router.add(method, path, guarded)
        return router

    def health(self, request: Request) -> Response:
        if request.query or request.body is not None:
            raise HttpError(400, "INVALID_REQUEST", "Invalid readiness request.")
        return Response(200, {"data": {"ready": True, "protocolVersion": 3}})

    def own(
        self, request: Request, *, mutation: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        admin = request.path.startswith("/v1/admin-apps/")
        user, _sid = self.auth.authenticate(
            request, kind=self.admin_apps.kind() if admin else None, mutation=mutation
        )
        identifier = checked_uuid(request.path_parameters["app"])
        self.request_actor.set((_sid, identifier))
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM apps WHERE id=? AND lifecycle!='rejected'"
                + ("" if admin else " AND " + ACCESS),
                (identifier,) if admin else (identifier, user["id"], user["id"]),
            ).fetchone()
            if row is None:
                raise HttpError(404, "NOT_FOUND", "Application not found.")
            app = dict(row)
            app["access"] = (
                "admin" if admin else "owner" if app["user_id"] == user["id"] else "member"
            )
        if app["lifecycle"] == "deleted" or (
            (mutation or not request.query) and self.journal.observe_deleted(app["id"])
        ):
            raise HttpError(
                410, "APPLICATION_DELETED", "This application was deleted by an administrator."
            )
        return user, app

    def identity_mutation_body(
        self, request: Request, app: dict[str, Any], fields: set[str]
    ) -> dict[str, Any]:
        body = {} if request.body is None and not fields else request.body
        if (
            not isinstance(body, dict)
            or not fields <= set(body)
            or set(body) - fields - {"identityProviderConfirmed"}
        ):
            raise HttpError(400, "INVALID_REQUEST", "Unexpected mutation fields.")
        self.admin_apps.identity_consent(app["id"], body)
        return {key: value for key, value in body.items() if key != "identityProviderConfirmed"}

    def quota(self, user_id: str) -> dict[str, Any]:
        with self.database.connect() as db:
            unlimited = resources.unlimited(db, user_id)
            policy = db.execute("SELECT * FROM quotas WHERE user_id=?", (user_id,)).fetchone()
            apps = db.execute(
                "SELECT lifecycle,COUNT(*) n FROM apps WHERE user_id=? AND lifecycle NOT IN ('rejected','deleted') GROUP BY lifecycle",
                (user_id,),
            ).fetchall()
            held = db.execute(
                "SELECT COUNT(*) FROM intents WHERE user_id=? AND kind IN ('deploy','storage_create','storage_verify','storage_rotate','storage_delete','env_set','env_delete','app_enable','app_disable','app_restart') AND state NOT IN ('succeeded','failed')",
                (user_id,),
            ).fetchone()[0]
        return {
            "apps": {
                "limit": None
                if unlimited
                else self.config.app_limit
                if policy is None
                else policy["apps"],
                "used": sum(row["n"] for row in apps if row["lifecycle"] == "ready"),
                "reserved": sum(row["n"] for row in apps if row["lifecycle"] != "ready"),
            },
            "concurrentOperations": {
                "limit": None
                if unlimited
                else self.config.concurrency_limit
                if policy is None
                else policy["concurrent"],
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
                detail = observed.get("error")
                if (
                    status == 404
                    and isinstance(detail, dict)
                    and detail.get("code") == "APPLICATION_NOT_FOUND"
                ):
                    self.journal.observe_deleted(app["id"], missing=True)
                    result["lifecycleState"] = "deleted"
                    return result
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
        self.journal.reconcile_page(page["items"])
        owners = self.owner_names(
            [item["user_id"] for item in page["items"] if item["user_id"] != user["id"]]
        )
        models = []
        for item in page["items"]:
            if item["lifecycle"] == "deleted":
                continue
            model = self.app_model(item)
            if model["lifecycleState"] == "deleted":
                continue
            model["access"] = "owner" if item["user_id"] == user["id"] else "member"
            if model["access"] == "member":
                model["ownerDisplayName"] = owners.get(item["user_id"])
            models.append(model)
        page["items"] = models
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
        # Apps include the ones the user is a team member of; intents stay their own.
        scope, scope_parameters = (
            (ACCESS, [owner, owner]) if table == "apps" else ("user_id=?", [owner])
        )
        condition = scope + (
            " AND lifecycle NOT IN ('rejected','deleted')" if table == "apps" else ""
        )
        parameters: list[object] = list(scope_parameters)
        with self.database.connect() as db:
            cursor = request.query.get("cursor", (None,))[0]
            if cursor is not None:
                point = db.execute(
                    f"SELECT created,id FROM {table} WHERE {scope} AND id=?",
                    (*scope_parameters, checked_uuid(cursor)),
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

    def owner_names(self, users: list[str]) -> dict[str, str]:
        if not users:
            return {}
        with self.database.connect() as db:
            return {
                row["id"]: row["display_name"]
                for row in db.execute(
                    f"SELECT id,display_name FROM users WHERE id IN ({','.join('?' * len(users))})",
                    users,
                )
            }

    def app(self, request: Request) -> Response:
        _user, app = self.own(request)
        model = self.app_model(app)
        model["access"] = app["access"]
        if app["access"] == "member":
            model["ownerDisplayName"] = self.owner_names([app["user_id"]]).get(app["user_id"])
        model["identityProvider"] = self.admin_apps.identity(model)
        model["configurationChanged"] = False
        if model["activeDeploymentId"]:
            try:
                status, deployed = self.client.request(
                    "GET", f"/v1/deployments/{model['activeDeploymentId']}"
                )
                if status == 200 and deployed.get("applicationId") == app["id"]:
                    model["configurationChanged"] = (
                        deployed.get("configurationRevision") != app["revision"]
                    )
                    accepted_at = deployed.get("acceptedAt")
                    with self.database.connect() as db:
                        rotated = db.execute(
                            "SELECT MAX(updated) FROM intents WHERE app_id=? AND kind='storage_rotate' AND state='succeeded'",
                            (app["id"],),
                        ).fetchone()[0]
                    if rotated and accepted_at and str(utc(rotated)) > str(utc(accepted_at)):
                        model["configurationChanged"] = True
            except ControllerUnavailable:
                model["stale"] = True
        return Response(200, {"data": model})

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

    def record(
        self,
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
        self.admin_apps.check(db)
        context = self.request_actor.get()
        if context is not None:
            actor = self.auth.session_row(db, context[0], self.auth.clock())
            if (
                self.admin_apps.context.get() is None
                and context[1] is not None
                and db.execute(
                    "SELECT 1 FROM apps WHERE id=? AND " + ACCESS,
                    (context[1], actor["id"], actor["id"]),
                ).fetchone()
                is None
            ):
                raise HttpError(404, "NOT_FOUND", "Application not found.")
        identifier, now = str(uuid.uuid4()), time.time()
        stored_body = dict(cast(dict[str, Any], body))
        if self.admin_apps.context.get() is not None:
            stored_body["_portalAdmin"] = True
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
                canonical(stored_body),
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO audit(user_id,app_id,intent_id,action,created) VALUES(?,?,?,?,?)",
            (user, app, identifier, kind, now),
        )
        self.admin_apps.record_audit(db, user, app, kind, identifier, now)
        return identifier

    def create(self, request: Request) -> Response:
        user, _sid = self.auth.authenticate(request, mutation=True)
        self.request_actor.set((_sid, None))
        admin = request.path == "/v1/admin-apps"
        body = object_body(request.body, {"slug", "ownerId"} if admin else {"slug"})
        owner = checked_uuid(body["ownerId"]) if admin else user["id"]
        name = slug(body["slug"])
        if name in RESERVED:
            raise HttpError(409, "SLUG_UNAVAILABLE", "Choose another application name.")
        key, fingerprint = (
            request.idempotency_key(),
            digest(canonical({"path": request.path, "body": body})),
        )
        with self.database.connect(write=True) as db:
            if admin:
                self.admin_apps.check_owner(db, owner)
            existing = self.existing(db, user["id"], key, fingerprint)
            if existing is not None:
                identifier, app_id = existing["id"], existing["app_id"]
            else:
                # Staff and admin accounts have no app limit; an admin
                # creating an app for an owner still uses that owner's quota.
                if not resources.unlimited(db, owner):
                    policy = db.execute(
                        "SELECT apps FROM quotas WHERE user_id=?", (owner,)
                    ).fetchone()
                    count = db.execute(
                        "SELECT COUNT(*) FROM apps WHERE user_id=? AND lifecycle NOT IN ('rejected','deleted')",
                        (owner,),
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
                    (app_id, owner, name, time.time(), identifier),
                )
                if admin:
                    db.execute(
                        "UPDATE admin_audit SET target_id=? WHERE json_extract(details,'$.intentId')=?",
                        (owner, identifier),
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
            {
                "data": {
                    "app": self.app_model(app),
                    "intent": intent_model(intent, diagnostic=user["role"] in {"staff", "admin"}),
                }
            },
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
        try:
            configuration = parse_configuration(body["configuration"]).canonical_json()
        except ValidationError:
            raise HttpError(
                400,
                "INVALID_CONFIGURATION",
                "Check configuration fields and storage bindings: targets must be valid, unique, unreserved environment names.",
            ) from None
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
                self.validate_bindings(db, app["id"], configuration)
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
        body = (
            self.admin_apps.deployment_body(request, app, admin=user["role"] == "admin")
            if request.path.startswith("/v1/admin-apps/")
            else self.identity_mutation_body(request, app, {"configurationRevision", "commit"})
        )
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
                resources.operation_quota(self, db, user["id"], app["id"])
                cfg = db.execute(
                    "SELECT * FROM configurations WHERE app_id=? AND revision=?",
                    (app["id"], revision),
                ).fetchone()
                self.validate_bindings(db, app["id"], cfg["configuration"])
                controller_body = {
                    "repository": cfg["repository"],
                    "requestedRef": cfg["branch"],
                    "commit": sha,
                    "configurationRevision": revision,
                    "configuration": strict_json(cfg["configuration"].encode()),
                }
                if request.path.startswith("/v1/admin-apps/"):
                    controller_body.update(
                        {k: body[k] for k in ("maintenance", "plan") if k in body}
                    )
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
        """An intent its actor started, or one on an app the user works on."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE id=? AND (user_id=? OR app_id IN"
                " (SELECT id FROM apps WHERE " + ACCESS + "))",
                (identifier, user, user, user),
            ).fetchone()
            if row is None:
                raise HttpError(404, "NOT_FOUND", "Operation not found.")
            actor = db.execute("SELECT role FROM users WHERE id=?", (user,)).fetchone()
            model = intent_model(
                row,
                diagnostic=actor is not None and actor[0] in {"staff", "admin"},
                viewer=user,
            )
            app = db.execute(
                "SELECT slug FROM apps WHERE id=? AND " + ACCESS, (row["app_id"], user, user)
            ).fetchone()
            model["appSlug"] = None if app is None else app["slug"]
            return Response(status, {"data": model})

    def intents(self, request: Request) -> Response:
        user, _sid = self.auth.authenticate(request)
        page = self.owned_page(request, user["id"], "intents")
        with self.database.connect() as db:
            slugs = {
                row["id"]: row["slug"]
                for row in db.execute(
                    "SELECT id,slug FROM apps WHERE " + ACCESS, (user["id"], user["id"])
                )
            }
        page["items"] = [
            {
                **intent_model(
                    row, diagnostic=user["role"] in {"staff", "admin"}, viewer=user["id"]
                ),
                "appSlug": slugs.get(row["app_id"]),
            }
            for row in page["items"]
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
            # The actor, or anyone on the app's team, can resume a stuck change.
            row = db.execute(
                "SELECT * FROM intents WHERE id=? AND (user_id=? OR app_id IN"
                " (SELECT id FROM apps WHERE " + ACCESS + "))",
                (identifier, user["id"], user["id"], user["id"]),
            ).fetchone()
            if row is None:
                raise HttpError(404, "NOT_FOUND", "Operation not found.")
            app_state = db.execute(
                "SELECT lifecycle FROM apps WHERE id=?", (row["app_id"],)
            ).fetchone()
            if app_state is not None and app_state[0] == "deleted":
                raise HttpError(
                    410, "APPLICATION_DELETED", "This application was deleted by an administrator."
                )
            if strict_json(row["body"].encode()).get("_portalAdmin") is True:
                # App administration: staff may resume all but admin-only kinds.
                # Origin and CSRF were checked above; the role and step-up are
                # checked in this transaction (a second connection would wait
                # on its lock).
                self.accounts.checked_actor(
                    db,
                    _sid,
                    step_up=row["kind"] == "storage_delete",
                    kind="admin" if row["kind"] in ADMIN_ONLY_INTENTS else "staff",
                )
            elif (
                row["kind"] != "create_app"
                and db.execute(
                    "SELECT 1 FROM apps WHERE id=? AND " + ACCESS,
                    (row["app_id"], user["id"], user["id"]),
                ).fetchone()
                is None
            ):
                raise HttpError(404, "NOT_FOUND", "Application not found.")
            if row["kind"] in {"env_set", "env_delete"}:
                raise HttpError(
                    409,
                    "ENV_RESUBMIT_REQUIRED",
                    "Resubmit the environment edit with its original request key and value to recover it.",
                )
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

    def startup_log(self, request: Request) -> Response:
        """Why a removed candidate stopped: its task events and output tails."""
        _user, app = self.own(request)
        identifier = checked_uuid(request.path_parameters["deployment"])
        self.read_project(f"/v1/deployments/{identifier}", app["id"])
        status, result = self.client.request("GET", f"/v1/deployments/{identifier}/startup-log")
        if status == 404:
            # Controllers from before startup records have none to give.
            return Response(200, {"data": {"captured": False}})
        record = result.get("startup")
        if (
            status != 200
            or result.get("deploymentId") != identifier
            or not isinstance(result.get("captured"), bool)
            or (result["captured"] and not isinstance(record, dict))
        ):
            raise ControllerUnavailable("invalid startup record")
        if not result["captured"]:
            return Response(200, {"data": {"captured": False}})
        assert isinstance(record, dict)

        def text(name: str) -> str:
            value = record.get(name)
            return value[-65_536:] if isinstance(value, str) else ""

        events = []
        for event in record.get("events") or []:
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                code = event.get("exitCode")
                events.append(
                    {
                        "type": event["type"][:64],
                        "message": str(event.get("message") or "")[:512],
                        "exitCode": code
                        if isinstance(code, int) and not isinstance(code, bool)
                        else None,
                        "oomKilled": event.get("oomKilled") is True,
                    }
                )
        restarts = record.get("restarts")
        return Response(
            200,
            {
                "data": {
                    "captured": True,
                    "found": record.get("found") is True,
                    "clientStatus": str(record.get("clientStatus") or "")[:32],
                    "restarts": restarts
                    if isinstance(restarts, int) and not isinstance(restarts, bool)
                    else 0,
                    "events": events[-12:],
                    "stdout": text("stdout"),
                    "stderr": text("stderr"),
                    "capturedAt": utc(record.get("capturedAt")),
                }
            },
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
