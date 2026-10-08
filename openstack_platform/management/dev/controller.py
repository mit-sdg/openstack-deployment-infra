"""Project-socket double with immutable source and deterministic fault fixtures."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
import time
import uuid as uuid_module
from pathlib import Path
from typing import Any, cast

from ...controller.deployment_config import parse_configuration
from ...controller.http import (
    ControllerRequestHandler,
    ControllerServer,
    HttpError,
    Request,
    Response,
    Router,
)
from ...controller.storage_contract import RESOURCE_OUTPUTS
from ...runtime_versions import RUNTIME_NAMES
from ...validation import ValidationError, env_key, flavor_reference, slug, uuid
from ..common import canonical, digest, strict_json, utc


def fake_runtime(configuration: dict[str, Any]) -> dict[str, str | None]:
    """A build's runtime: the fake reads no package.json, so the default."""
    runtime = configuration["build"]["runtime"]
    repository = "library/node" if runtime == "node" else "oven/bun"
    return {
        "runtime": runtime,
        "version": None,
        "image": f"docker.io/{repository}@sha256:" + "c" * 64,
        "source": "default",
    }


FLAVORS = (
    {"flavor_id": "100", "name": "worker-small", "vcpus": 1, "ram_mib": 2048, "disk_gib": 20},
    {"flavor_id": "200", "name": "worker-large", "vcpus": 4, "ram_mib": 16384, "disk_gib": 64},
    {"flavor_id": "50", "name": "builder-small", "vcpus": 1, "ram_mib": 1024, "disk_gib": 20},
)


class FakeController:
    def __init__(self, state_file: Path | None = None) -> None:
        self.apps: dict[str, dict[str, Any]] = {}
        self.deployments: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, tuple[str, Response]] = {}
        self.environments: dict[str, dict[str, Any]] = {}
        self.resources: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, str | None]] = []
        self.lock = threading.RLock()
        self.drop_next = False
        self.failed_next = False
        self.recovery_next = False
        self.default_builder_flavor = "builder-small"
        self.builder_flavors: dict[str, str | None] = {}
        self.source_keys: dict[str, dict[str, str]] = {}
        # None, or the access problem an access check reports.
        self.source_problem: str | None = None
        self.delay = 0.6
        self.pause_storage_creation = False
        self.error_next: tuple[int, str] | None = None
        self.state_file = state_file
        if state_file is not None and state_file.is_file():
            if state_file.is_symlink() or state_file.stat().st_size > 1048576:
                raise ValueError("invalid fake controller state")
            saved = strict_json(state_file.read_bytes())
            self.default_builder_flavor = saved.get("defaultBuilderFlavor", "builder-small")
            self.builder_flavors = saved.get("builderFlavors", {})
            self.environments = saved.get("environments", {})
            self.resources = saved.get("resources", {})
            self.apps, self.deployments, self.operations = (
                saved["apps"],
                saved["deployments"],
                saved["operations"],
            )
            self.keys = {
                key: (
                    value["fingerprint"],
                    Response(value["status"], value["body"], value.get("headers")),
                )
                for key, value in saved["keys"].items()
            }
            # Upgrade only old local fixture checkpoints to the audited wire
            # projection. No controller or broker product records are adopted.
            for app in self.apps.values():
                app.setdefault("requiresMaintenance", False)
                app.setdefault(
                    "sizing", {"workerFlavor": "worker-small", "cpuMHz": 500, "memoryMiB": 512}
                )
                app["live"].setdefault("available", True)
                app["live"]["checkedAt"] = utc(app["live"].get("checkedAt"))
                if app.get("deployment"):
                    app["deployment"].setdefault(
                        "imageDigest", "registry.example.com/app@sha256:" + "a" * 64
                    )
                    app["deployment"].setdefault("nomadVersion", 1)
                    app["deployment"].setdefault(
                        "lastHealthyAt", app["deployment"].get("acceptedAt")
                    )
            for item in self.deployments.values():
                for name in ("requestedAt", "updatedAt", "acceptedAt", "lastHealthyAt"):
                    item[name] = utc(item.get(name))
                item["snapshotKind"] = "strict"
                item.setdefault("environmentRevision", 0)
                item.setdefault("recipeHash", None)
                item.setdefault("runtime", None)
                item.setdefault("nomadVersion", None)
                if item.get("cleanupState") == "complete":
                    item["cleanupState"] = "confirmed"
            for item in self.operations.values():
                item.setdefault("kind", "app.deploy")
                item.setdefault("startedAt", utc(item.get("updatedAt")))
                item.setdefault("deadlineAt", None)
                item.setdefault("safeError", None)
                item.setdefault("errorCode", None)
                item.setdefault("finishing", False)
                item.setdefault("finishingRetryAttempts", 0)
                item.setdefault("nextRetryAt", None)
                item["updatedAt"] = utc(item.get("updatedAt"))
                if item.get("cleanupState") == "complete":
                    item["cleanupState"] = "confirmed"

    def persist(self) -> None:
        if self.state_file is None:
            return
        value = {
            "defaultBuilderFlavor": self.default_builder_flavor,
            "builderFlavors": self.builder_flavors,
            "apps": self.apps,
            "environments": self.environments,
            "resources": self.resources,
            "deployments": self.deployments,
            "operations": self.operations,
            "keys": {
                key: {
                    "fingerprint": fp,
                    "status": response.status,
                    "body": response.body,
                    "headers": response.headers,
                }
                for key, (fp, response) in self.keys.items()
            },
        }
        temporary = self.state_file.with_suffix(".candidate")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_file)
        finally:
            temporary.unlink(missing_ok=True)

    def router(self) -> Router:
        router = Router()
        routes = [
            ("POST", "/v1/applications", self.create),
            ("GET", "/v1/applications/{app}", self.app),
            ("GET", "/v1/flavors", self.flavors),
            ("GET", "/v1/applications/{app}/resize-plan", self.resize_plan),
            ("GET", "/v1/applications/{app}/builder-size", self.builder_size),
            ("PUT", "/v1/applications/{app}/builder-size", self.builder_size),
            ("GET", "/v1/settings/default-builder-size", self.builder_size),
            ("PUT", "/v1/settings/default-builder-size", self.builder_size),
            ("POST", "/v1/applications/{app}/deployments", self.deploy),
            ("GET", "/v1/applications/{app}/deployments", self.history),
            ("GET", "/v1/deployments/{deployment}", self.deployment),
            ("GET", "/v1/deployments/{deployment}/build-log", self.log),
            ("GET", "/v1/deployments/{deployment}/startup-log", self.startup_log),
            ("GET", "/v1/applications/{app}/runtime-log", self.runtime_log),
            ("GET", "/v1/applications/{app}/source-key", self.source_key),
            ("POST", "/v1/applications/{app}/source-key", self.source_key),
            ("DELETE", "/v1/applications/{app}/source-key", self.source_key),
            ("POST", "/v1/applications/{app}/source-key/check", self.source_check),
            ("POST", "/v1/applications/{app}/source/commits", self.source_commits),
            ("POST", "/v1/applications/{app}/source/check", self.source_preflight),
            ("POST", "/v1/applications/{app}/restart", self.restart),
            ("GET", "/v1/operations/{operation}", self.operation),
            ("GET", "/v1/applications/{app}/environment", self.environment),
            ("PUT", "/v1/applications/{app}/environment/{key}", self.environment_write),
            ("DELETE", "/v1/applications/{app}/environment/{key}", self.environment_write),
            ("GET", "/v1/applications/{app}/storage", self.storage),
            ("POST", "/v1/applications/{app}/storage", self.storage_create),
            ("POST", "/v1/storage/{resource}/verify", self.storage_action),
            ("POST", "/v1/storage/{resource}/rotate", self.storage_action),
            ("GET", "/v1/storage/{resource}", self.storage_resource),
            ("DELETE", "/v1/storage/{resource}", self.storage_delete),
            ("POST", "/v1/applications/{app}/enable", self.running_state),
            ("POST", "/v1/applications/{app}/disable", self.running_state),
        ]
        for method, path, handler in routes:

            def locked(request: Request, handler: Any = handler) -> Response:
                with self.lock:
                    self.calls.append(
                        (request.method, request.path, request.headers.get("idempotency-key"))
                    )
                    if self.error_next:
                        status, code = self.error_next
                        self.error_next = None
                        raise HttpError(
                            status,
                            code,
                            "Fixture dependency unavailable.",
                            retryable=status >= 500,
                            operation_id="00000000-0000-4000-8000-000000000001"
                            if code == "OPERATION_CONFLICT"
                            else None,
                        )
                    try:
                        for field, identifier in request.path_parameters.items():
                            if field == "key":
                                env_key(identifier)
                            else:
                                uuid(identifier)
                        return handler(request)  # type: ignore[no-any-return]
                    except ValidationError:
                        raise HttpError(
                            400, "INVALID_REQUEST", "Request validation failed."
                        ) from None

            router.add(method, path, locked)
        return router

    def server(self, path: Path) -> ControllerServer:
        fixture = self

        class FaultHandler(ControllerRequestHandler):
            def _write(self, response: Response, correlation_id: str) -> None:
                if self.command == "POST" and self.path.endswith("/deployments"):
                    with fixture.lock:
                        drop = fixture.drop_next
                        fixture.drop_next = False
                    if drop:
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                super()._write(response, correlation_id)

        server = ControllerServer(str(path), self.router())
        server.RequestHandlerClass = FaultHandler
        return server

    def replay(self, request: Request) -> Response | None:
        key = request.idempotency_key()
        fingerprint = digest(
            canonical({"method": request.method, "path": request.path, "body": request.body})
        )
        found = self.keys.get(key)
        if found and found[0] != fingerprint:
            raise HttpError(409, "IDEMPOTENCY_CONFLICT", "Changed request under the same key.")
        return None if found is None else found[1]

    def remember(self, request: Request, response: Response) -> Response:
        self.keys[request.idempotency_key()] = (
            digest(
                canonical({"method": request.method, "path": request.path, "body": request.body})
            ),
            response,
        )
        self.persist()
        return response

    def create(self, request: Request) -> Response:
        replay = self.replay(request)
        if replay:
            return replay
        body = self.body(request, {"slug"})
        slug(body["slug"])
        if any(app["slug"] == body["slug"] for app in self.apps.values()):
            raise HttpError(400, "INVALID_REQUEST", "Application already exists.")
        identifier = request.idempotency_key()
        app = {
            "applicationId": identifier,
            "slug": body["slug"],
            "url": f"https://{body['slug']}.apps.example.com",
            "desiredRunning": False,
            "requiresMaintenance": False,
            "activeDeploymentId": None,
            "deployment": None,
            "sizing": {"workerFlavor": "worker-small", "cpuMHz": 500, "memoryMiB": 512},
            "live": {
                "schedulerAvailable": True,
                "available": True,
                "routeAvailable": False,
                "schedulerState": "stopped",
                "allocationHealthy": None,
                "routeHealthy": None,
                "checkedAt": utc(time.time()),
            },
        }
        self.apps[identifier] = app
        created = {
            key: app[key]
            for key in ("applicationId", "slug", "url", "activeDeploymentId", "sizing")
        }
        created.update(enabled=False, createdAt=utc(time.time()), updatedAt=utc(time.time()))
        return self.remember(
            request, Response(201, created, {"Location": f"/v1/applications/{identifier}"})
        )

    def app(self, request: Request) -> Response:
        app = self.apps.get(request.path_parameters["app"])
        if app is None:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        return Response(200, app.copy())

    @staticmethod
    def flavor(reference: str) -> dict[str, Any]:
        reference = flavor_reference(reference)
        for item in FLAVORS:
            if reference in {item["name"], item["flavor_id"]}:
                return dict(item)
        raise HttpError(400, "INVALID_REQUEST", "Flavor does not exist.")

    def flavors(self, _request: Request) -> Response:
        return Response(200, {"items": [dict(item) for item in FLAVORS]})

    def resize_plan(self, request: Request) -> Response:
        self.app(request)
        app_id = request.path_parameters["app"]
        app = self.apps[app_id]
        if set(request.query) != {"flavor"} or len(request.query["flavor"]) != 1:
            raise HttpError(400, "INVALID_QUERY", "Choose a size.")
        plan = {
            "applicationId": app_id,
            "deploymentId": app["activeDeploymentId"],
            "activation": "enable-after-healthy-acceptance",
            "current": {
                "enabled": app["desiredRunning"],
                "flavor": app["sizing"]["workerFlavor"],
                "cpuMHz": app["sizing"]["cpuMHz"],
                "memoryMiB": app["sizing"]["memoryMiB"],
            },
            "flavor": self.flavor(request.query["flavor"][0]),
            "allocation": "measured-worker-capacity-minus-reserve",
            "reserve": {"cpuMHzMinimum": 200, "memoryMiBMinimum": 512, "percentMinimum": 10},
        }
        return Response(200, {**plan, "fingerprint": digest(canonical(plan))})

    def builder_size(self, request: Request) -> Response:
        app_id = request.path_parameters.get("app")
        if app_id is not None:
            self.app(request)
        selected = (
            self.default_builder_flavor if app_id is None else self.builder_flavors.get(app_id)
        )
        if request.method == "GET":
            default = self.flavor(self.default_builder_flavor)
            value: dict[str, Any] = {
                "flavor": self.flavor(selected) if selected is not None else default
            }
            if app_id is not None:
                value.update(defaultFlavor=default, useDefault=selected is None)
            return Response(200, value)
        replay = self.replay(request)
        if replay:
            return replay
        body = self.body(request, {"flavor", "expectedFlavor"})
        if body["expectedFlavor"] != selected:
            raise HttpError(400, "INVALID_REQUEST", "Selection changed.")
        chosen = body["flavor"]
        flavor = (
            self.flavor(chosen) if chosen is not None else self.flavor(self.default_builder_flavor)
        )
        if app_id is None:
            self.default_builder_flavor = flavor["name"]
        else:
            self.builder_flavors[app_id] = None if chosen is None else flavor["name"]
        response = self.resource_operation(
            request,
            app_id or "infrastructure",
            "app.builder-size.set" if app_id else "infra.builder-size.set",
        )
        if app_id is None:
            self.operations[request.idempotency_key()]["scope"] = "infrastructure"
            self.persist()
        return response

    def deploy(self, request: Request) -> Response:
        replay = self.replay(request)
        identifier = request.idempotency_key()
        if replay:
            operation = self.operations[identifier]
            if operation["status"] == "recovery_required":
                operation.update(status="running", phase="building", ready=time.time() + self.delay)
            return replay
        fields = {"repository", "commit", "requestedRef", "configurationRevision", "configuration"}
        if (
            not isinstance(request.body, dict)
            or not fields <= set(request.body)
            or set(request.body) - fields - {"maintenance", "plan"}
        ):
            raise HttpError(400, "INVALID_BODY", "Invalid deployment fields.")
        body = request.body
        app_id = request.path_parameters["app"]
        if app_id not in self.apps:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        for operation in self.operations.values():
            if operation["scope"] == f"app-{app_id}" and operation["status"] in {
                "running",
                "recovery_required",
            }:
                raise HttpError(
                    409,
                    "OPERATION_CONFLICT",
                    "Another operation is unfinished for this resource scope.",
                    operation_id=operation["operationId"],
                )
        configuration = parse_configuration(body["configuration"])
        self.deployments[identifier] = {
            "deploymentId": identifier,
            "applicationId": app_id,
            "status": "running",
            "snapshotKind": "strict",
            "repositoryCommit": body["commit"],
            "sourceRepository": body["repository"],
            "requestedRef": body["requestedRef"],
            "configurationRevision": body["configurationRevision"],
            "configuration": body["configuration"],
            "configurationSha256": digest(configuration.canonical_json()),
            "environmentRevision": 0,
            "recipeHash": None,
            "runtime": None,
            "nomadVersion": None,
            "imageDigest": None,
            "safeError": None,
            "cleanupState": "pending",
            "requestedAt": utc(time.time()),
            "updatedAt": utc(time.time()),
            "acceptedAt": None,
            "lastHealthyAt": None,
        }
        self.operations[identifier] = {
            "operationId": identifier,
            "scope": f"app-{app_id}",
            "kind": "app.deploy",
            "status": "running",
            "phase": "building",
            "cleanupState": "pending",
            "startedAt": utc(time.time()),
            "updatedAt": utc(time.time()),
            "deadlineAt": utc(time.time() + 30),
            "safeError": None,
            "errorCode": None,
            "finishing": False,
            "finishingRetryAttempts": 0,
            "nextRetryAt": None,
            "ready": time.time() + self.delay,
            "fixtureFail": self.failed_next,
            "fixtureRecovery": self.recovery_next,
        }
        self.failed_next = self.recovery_next = False
        return self.remember(
            request,
            Response(
                202,
                {
                    "operationId": identifier,
                    "statusUrl": f"/v1/operations/{identifier}",
                    "result": {"kind": "operation", "id": identifier},
                },
                {"Location": f"/v1/operations/{identifier}"},
            ),
        )

    def operation(self, request: Request) -> Response:
        identifier = request.path_parameters["operation"]
        operation = self.operations.get(identifier)
        if operation is None:
            raise HttpError(404, "OPERATION_NOT_FOUND", "Operation does not exist.")
        if (
            operation["status"] == "running"
            and time.time() >= operation["ready"]
            and not (operation["kind"] == "storage.create" and self.pause_storage_creation)
        ):
            if operation["kind"] != "app.deploy":
                operation.update(
                    status="succeeded",
                    phase="accepted",
                    cleanupState="not_required",
                    updatedAt=utc(time.time()),
                )
                for resource in self.resources.values():
                    if operation["scope"] == f"app-{resource['applicationId']}":
                        resource.update(lifecycleState="active", lastVerifiedAt=utc(time.time()))
                self.persist()
                return Response(200, {k: v for k, v in operation.items() if k != "ready"})
            attempt = self.deployments[identifier]
            if operation["fixtureRecovery"]:
                operation.update(
                    status="recovery_required", phase="startup_interrupted", fixtureRecovery=False
                )
            else:
                failed = operation["fixtureFail"]
                operation.update(
                    status="failed" if failed else "succeeded",
                    phase="build_rejected" if failed else "accepted",
                    cleanupState="confirmed",
                    updatedAt=utc(time.time()),
                )
                attempt.update(
                    status=operation["status"], cleanupState="confirmed", updatedAt=utc(time.time())
                )
                if not failed:
                    attempt.update(
                        acceptedAt=utc(time.time()),
                        lastHealthyAt=utc(time.time()),
                        recipeHash="b" * 64,
                        runtime=fake_runtime(attempt["configuration"]),
                        nomadVersion=1,
                        imageDigest="registry.example.com/app@sha256:" + "a" * 64,
                    )
                    app = self.apps[attempt["applicationId"]]
                    app.update(
                        activeDeploymentId=identifier,
                        desiredRunning=True,
                        deployment={
                            "deploymentId": identifier,
                            "sourceCommit": attempt["repositoryCommit"],
                            "acceptedAt": utc(time.time()),
                            "lastHealthyAt": utc(time.time()),
                            "imageDigest": attempt["imageDigest"],
                            "nomadVersion": 1,
                        },
                        live={
                            "schedulerAvailable": True,
                            "available": True,
                            "routeAvailable": True,
                            "schedulerState": "running",
                            "allocationHealthy": True,
                            "routeHealthy": True,
                            "checkedAt": utc(time.time()),
                        },
                    )
        self.persist()
        return Response(
            200,
            {
                key: value
                for key, value in operation.items()
                if key not in {"ready", "fixtureFail", "fixtureRecovery"}
            },
        )

    def history(self, request: Request) -> Response:
        items = [
            attempt
            for attempt in self.deployments.values()
            if attempt["applicationId"] == request.path_parameters["app"]
        ]
        items.sort(key=lambda attempt: attempt["requestedAt"], reverse=True)
        if request.path_parameters["app"] not in self.apps:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        return Response(200, self.page(request, items))

    def deployment(self, request: Request) -> Response:
        attempt = self.deployments.get(request.path_parameters["deployment"])
        if attempt is None:
            raise HttpError(404, "DEPLOYMENT_NOT_FOUND", "Deployment does not exist.")
        return Response(200, attempt.copy())

    def source_key(self, request: Request) -> Response:
        app = request.path_parameters["app"]
        if app not in self.apps:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        keys = self.source_keys
        if request.method == "DELETE":
            keys.pop(app, None)
        if request.method == "POST" and (
            app not in keys or (isinstance(request.body, dict) and request.body.get("replace"))
        ):
            blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + os.urandom(32)
            fingerprint = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
            keys[app] = {
                "publicKey": "ssh-ed25519 " + base64.b64encode(blob).decode(),
                "fingerprint": "SHA256:" + fingerprint,
                "createdAt": utc(time.time()) or "",
            }
        key = keys.get(app)
        return Response(200, {"applicationId": app, "present": key is not None, **(key or {})})

    def source_commits(self, request: Request) -> Response:
        app = request.path_parameters["app"]
        if app not in self.source_keys:
            return Response(200, {"applicationId": app, "keyPresent": False})
        return Response(
            200,
            {
                "applicationId": app,
                "keyPresent": True,
                "items": [
                    {
                        "sha": "0123456789abcdef0123456789abcdef01234567",
                        "message": "Private repository fixture",
                        "author": "Fixture Author",
                        "date": "2026-10-04T00:00:00Z",
                    }
                ],
            },
        )

    def source_preflight(self, request: Request) -> Response:
        from ...controller.deployment_config import parse_configuration

        app = request.path_parameters["app"]
        if app not in self.source_keys:
            return Response(200, {"applicationId": app, "keyPresent": False})
        assert isinstance(request.body, dict)
        configuration = parse_configuration(request.body["configuration"])
        ids = ["package-json", "script:" + configuration.start_script]
        if configuration.build_script:
            ids.append("script:" + configuration.build_script)
        ids.extend("lockfile:" + package for package in configuration.packages)
        name = RUNTIME_NAMES[configuration.runtime]
        return Response(
            200,
            {
                "applicationId": app,
                "keyPresent": True,
                "items": [
                    *({"id": identifier, "label": identifier, "state": "ok"} for identifier in ids),
                    # Like a checkout that asks for no runtime version.
                    {"id": "runtime-default", "label": f"{name} (platform default)", "state": "ok"},
                ],
            },
        )

    def source_check(self, request: Request) -> Response:
        app = request.path_parameters["app"]
        if app not in self.source_keys:
            return Response(200, {"applicationId": app, "keyPresent": False})
        problem = self.source_problem
        return Response(
            200,
            {
                "applicationId": app,
                "keyPresent": True,
                "reachable": problem is None,
                "head": None if problem else "0123456789abcdef0123456789abcdef01234567",
                "problem": problem,
            },
        )

    def startup_log(self, request: Request) -> Response:
        identifier = request.path_parameters["deployment"]
        if identifier not in self.deployments:
            raise HttpError(404, "DEPLOYMENT_NOT_FOUND", "Deployment does not exist.")
        if self.deployments[identifier]["status"] != "failed":
            return Response(200, {"deploymentId": identifier, "captured": False})
        return Response(
            200,
            {
                "deploymentId": identifier,
                "captured": True,
                "startup": {
                    "found": True,
                    "clientStatus": "failed",
                    "taskState": "dead",
                    "failed": True,
                    "restarts": 3,
                    "events": [
                        {"type": "Started", "message": "Task started by client", "exitCode": None},
                        {
                            "type": "Terminated",
                            "message": "Exit Code: 1",
                            "exitCode": 1,
                            "oomKilled": False,
                        },
                        {
                            "type": "Not Restarting",
                            "message": "Exceeded allowed attempts 3 in interval 5m0s",
                            "exitCode": None,
                        },
                    ],
                    "stdout": "> start\n> node server.js\n\n",
                    "stderr": "Error: Cannot find module 'express'\n    at Module._resolveFilename (node:internal/modules/cjs/loader:1225:15)\n",
                    "stdoutTruncated": False,
                    "stderrTruncated": False,
                    "capturedAt": utc(time.time()),
                },
            },
        )

    def runtime_log(self, request: Request) -> Response:
        app = self.apps.get(request.path_parameters["app"])
        if app is None:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        if set(request.query) - {"lines", "stream"} or request.query.get(
            "stream", ("stdout",)
        ) not in (("stdout",), ("stderr",)):
            raise HttpError(400, "INVALID_QUERY", "Log query is invalid.")
        if not app["desiredRunning"] or not app["activeDeploymentId"]:
            raise HttpError(502, "ALLOCATION_NOT_FOUND", "No running allocation was found.")
        stream = request.query.get("stream", ("stdout",))[0]
        text = (
            "Warning: SESSION_SECRET is short\n"
            if stream == "stderr"
            else "> start\n> node server.js\n\nListening on port 3000\nGET /health 200 2 ms\n"
        )
        return Response(
            200,
            {
                "applicationId": app["applicationId"],
                "stream": stream,
                "text": text,
                "state": "running",
                "nextOffset": len(text.encode()),
                "truncated": False,
            },
        )

    def log(self, request: Request) -> Response:
        identifier = request.path_parameters["deployment"]
        if identifier not in self.deployments:
            raise HttpError(404, "DEPLOYMENT_NOT_FOUND", "Deployment does not exist.")
        if set(request.query) - {"lines", "offset"} or any(
            len(values) != 1 or not values[0] for values in request.query.values()
        ):
            raise HttpError(400, "INVALID_QUERY", "Log query is invalid.")
        try:
            lines = int(request.query.get("lines", ("200",))[0])
            offset = int(request.query.get("offset", ("0",))[0])
        except ValueError:
            raise HttpError(400, "INVALID_QUERY", "Log bounds are invalid.") from None
        if not 1 <= lines <= 1000 or not 0 <= offset <= 1000000:
            raise HttpError(400, "INVALID_QUERY", "Log bounds are outside the configured limits.")
        log = "Preparing exact source snapshot…\nInstalling locked dependencies…\nRunning package scripts…\nImage published. Waiting for health checks.\n"
        return Response(
            200,
            {
                "deploymentId": identifier,
                "text": log[offset:],
                "state": "running"
                if self.deployments[identifier]["status"] == "running"
                else "failed"
                if self.deployments[identifier]["status"] == "failed"
                else "complete",
                "nextOffset": len(log),
                "truncated": False,
            },
        )

    @staticmethod
    def body(request: Request, fields: set[str]) -> dict[str, Any]:
        if not isinstance(request.body, dict) or set(request.body) != fields:
            raise HttpError(400, "INVALID_BODY", "Request body fields are invalid.")
        return request.body

    @staticmethod
    def page(request: Request, items: list[dict[str, Any]]) -> dict[str, Any]:
        if set(request.query) - {"limit", "cursor"} or any(
            len(v) != 1 or not v[0] for v in request.query.values()
        ):
            raise HttpError(400, "INVALID_QUERY", "Pagination query is invalid.")
        try:
            limit = int(request.query.get("limit", ("50",))[0])
        except ValueError:
            raise HttpError(400, "INVALID_QUERY", "Page limit is invalid.") from None
        if not 1 <= limit <= 100:
            raise HttpError(400, "INVALID_QUERY", "Page limit must be 1–100.")
        cursor = request.query.get("cursor", (None,))[0]
        start = 0
        if cursor is not None:
            uuid(cursor)
            indexes = [i for i, item in enumerate(items) if item["deploymentId"] == cursor]
            if not indexes:
                raise HttpError(400, "INVALID_QUERY", "Pagination cursor is unknown.")
            start = indexes[0] + 1
        selected = items[start : start + limit]
        more = start + limit < len(items)
        return {
            "items": selected,
            "nextCursor": selected[-1]["deploymentId"] if more and selected else None,
            "truncated": more,
        }

    def resource_operation(self, request: Request, app_id: str, kind: str) -> Response:
        identifier = request.idempotency_key()
        self.operations[identifier] = {
            "operationId": identifier,
            "scope": f"app-{app_id}",
            "kind": kind,
            "status": "running",
            "phase": "provisioning",
            "cleanupState": "not_required",
            "updatedAt": utc(time.time()),
            "ready": time.time() + self.delay,
        }
        return self.remember(
            request,
            Response(
                202,
                {
                    "operationId": identifier,
                    "statusUrl": f"/v1/operations/{identifier}",
                    "result": {"kind": "operation", "id": identifier},
                },
            ),
        )

    def environment(self, request: Request) -> Response:
        app = request.path_parameters["app"]
        if app not in self.apps:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        return Response(
            200,
            {
                "applicationId": app,
                **self.environments.get(
                    app, {"revision": 0, "updatedAt": utc(time.time()), "keys": []}
                ),
            },
        )

    def environment_write(self, request: Request) -> Response:
        replay = self.replay(request)
        if replay:
            return replay
        app = request.path_parameters["app"]
        self.environment(request)
        name = env_key(request.path_parameters["key"])
        environment = self.environments.setdefault(app, {"revision": 0, "keys": []})
        names = {item["name"] for item in environment["keys"]}
        if request.method == "PUT":
            self.body(request, {"value"})
            names.add(name)
        else:
            names.discard(name)
        environment.update(
            keys=[{"name": n, "owner": "staff"} for n in sorted(names)],
            revision=environment["revision"] + 1,
            updatedAt=utc(time.time()),
        )
        return self.resource_operation(
            request, app, "app.env.set" if request.method == "PUT" else "app.env.unset"
        )

    def storage(self, request: Request) -> Response:
        app = request.path_parameters["app"]
        if app not in self.apps:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        return Response(
            200,
            {
                "items": [r for r in self.resources.values() if r["applicationId"] == app],
                "nextCursor": None,
                "truncated": False,
            },
        )

    def storage_create(self, request: Request) -> Response:
        replay = self.replay(request)
        if replay:
            return replay
        self.storage(request)
        app = request.path_parameters["app"]
        body = self.body(request, {"type"})
        if body["type"] not in RESOURCE_OUTPUTS:
            raise HttpError(400, "INVALID_REQUEST", "Invalid storage type.")
        if any(
            r["type"] == body["type"] and r["applicationId"] == app for r in self.resources.values()
        ):
            raise HttpError(409, "INVALID_REQUEST", "Storage exists.")
        identifier = request.idempotency_key()
        self.resources[identifier] = {
            "resourceId": identifier,
            "applicationId": app,
            "type": body["type"],
            "name": "default",
            "displayLabel": None,
            "lifecycleState": "creating",
            "createdAt": utc(time.time()),
            "updatedAt": utc(time.time()),
            "lastVerifiedAt": None,
            "quotas": {},
        }
        return self.resource_operation(request, app, "storage.create")

    def storage_action(self, request: Request) -> Response:
        replay = self.replay(request)
        if replay:
            return replay
        resource = self.resources.get(request.path_parameters["resource"])
        if resource is None:
            raise HttpError(404, "STORAGE_NOT_FOUND", "Storage does not exist.")
        return self.resource_operation(
            request, resource["applicationId"], "storage." + request.path.rsplit("/", 1)[1]
        )

    def storage_resource(self, request: Request) -> Response:
        resource = self.resources.get(request.path_parameters["resource"])
        if resource is None:
            raise HttpError(404, "STORAGE_NOT_FOUND", "Storage does not exist.")
        return Response(200, dict(resource))

    def storage_delete(self, request: Request) -> Response:
        replay = self.replay(request)
        if replay:
            return replay
        resource = dict(cast(dict[str, Any], self.storage_resource(request).body))
        body = self.body(request, {"confirmation", "purge"})
        if body["confirmation"] != resource["name"]:
            raise HttpError(400, "INVALID_BODY", "Confirmation mismatch.")
        self.resources.pop(request.path_parameters["resource"])
        return self.resource_operation(request, resource["applicationId"], "storage.remove")

    def restart(self, request: Request) -> Response:
        replay = self.replay(request)
        if replay is not None:
            return replay
        app = request.path_parameters["app"]
        if app not in self.apps:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        if not self.apps[app]["desiredRunning"]:
            raise HttpError(409, "APP_STOPPED", "The app must be running.")
        return self.resource_operation(request, app, "app.restart")

    def running_state(self, request: Request) -> Response:
        replay = self.replay(request)
        if replay:
            return replay
        app = request.path_parameters["app"]
        if app not in self.apps:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "Application does not exist.")
        self.apps[app]["desiredRunning"] = request.path.endswith("/enable")
        return self.resource_operation(request, app, "app.state")

    def seed_operator_app(self, identifier: str, public_url: str) -> None:
        """Explicit development fixture: accepted, larger and retained-address app."""
        if identifier in self.apps:
            return
        deployment = str(
            uuid_module.uuid5(uuid_module.NAMESPACE_URL, "fixture-accepted:" + identifier)
        )
        configuration = {
            "schemaVersion": 1,
            "build": {
                "runtime": "node",
                "packages": ["."],
                "buildScript": None,
                "startScript": "start",
            },
            "runtime": {"port": 3000, "healthPath": "/health"},
            "storageBindings": [],
        }
        self.apps[identifier] = {
            "applicationId": identifier,
            "slug": "operator-class-fixture-mobile"
            if identifier.endswith("0083")
            else "operator-class-fixture",
            "url": public_url,
            "activeDeploymentId": deployment,
            "desiredRunning": True,
            "requiresMaintenance": True,
            "sizing": {"workerFlavor": "worker-large", "cpuMHz": 4000, "memoryMiB": 8192},
            "deployment": {
                "deploymentId": deployment,
                "sourceCommit": "a" * 40,
                "acceptedAt": utc(time.time()),
            },
            "live": {
                "schedulerAvailable": True,
                "routeAvailable": True,
                "schedulerState": "running",
                "allocationHealthy": True,
                "routeHealthy": True,
            },
        }
        self.deployments[deployment] = {
            "deploymentId": deployment,
            "applicationId": identifier,
            "status": "succeeded",
            "snapshotKind": "strict",
            "repositoryCommit": "a" * 40,
            "sourceRepository": "https://github.com/example/class-app",
            "requestedRef": "main",
            "configurationRevision": 7,
            "configuration": configuration,
            "configurationSha256": digest(canonical(configuration)),
            "runtime": fake_runtime(configuration),
            "requestedAt": utc(time.time()),
            "acceptedAt": utc(time.time()),
            "updatedAt": utc(time.time()),
            "cleanupState": "confirmed",
        }
        self.persist()
