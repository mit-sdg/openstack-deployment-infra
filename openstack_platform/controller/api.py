"""Production controller API composition over the bounded Unix HTTP transport."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import openstack, remote
from ..config import Config
from ..runtime import safe_summary
from ..validation import ValidationError, bounded_text, env_key, resource_name, uuid
from . import application_runtime as app
from . import database as db
from . import status, storage
from .application_service import ApplicationService
from .async_operations import AsyncOperationExecutor
from .deployment_config import parse_configuration
from .deployment_service import (
    DeploymentDeadlineError,
    DeploymentRequest,
    DeploymentService,
)
from .environment_service import EnvironmentMutationRequest, EnvironmentService
from .http import HttpError, Request, Response, Router
from .log_service import LogService
from .service_support import ServiceDeadlineError
from .storage_service import StorageMutationRequest, StorageService

_MAX_PAGE = 100
_MAX_LOG_LINES = 1_000
HelperCaller = Callable[..., Mapping[str, object]]


class LocalHelperTransport:
    """The controller's sole fixed helper transport."""

    def __init__(self, config: Config, helper_command: str | None = None) -> None:
        root = config.platform.get("paths.root")
        if not isinstance(root, str):
            raise ValidationError("configured helper root is unavailable")
        expected = remote.helper_command_path(root)
        if helper_command is not None and helper_command != expected:
            raise ValidationError("controller helper executable is fixed by inventory")
        self.command = expected

    def service(
        self,
        config: Config,
        action: str,
        args: Mapping[str, object],
        *,
        deadline: float | None = None,
    ) -> Mapping[str, object]:
        timeout = float(config.policy.limits.helper_seconds)
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise ServiceDeadlineError("operation exceeded its whole-operation deadline")
        return remote.call_local_helper(
            action,
            args,
            timeout_seconds=timeout,
            helper_command=self.command,
            request_limit=config.policy.limits.helper_request_bytes,
            response_limit=config.policy.limits.helper_response_bytes,
            stderr_limit=config.policy.limits.stderr_bytes,
        )

    def observer(
        self,
        action: str,
        args: Mapping[str, object],
        **bounds: object,
    ) -> Mapping[str, object]:
        timeout = bounds.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValidationError("helper timeout is invalid")
        return remote.call_local_helper(
            action,
            args,
            timeout_seconds=float(timeout),
            helper_command=self.command,
            request_limit=_integer_bound(bounds, "request_limit", 1_048_576),
            response_limit=_integer_bound(bounds, "response_limit", 1_048_576),
            stderr_limit=_integer_bound(bounds, "stderr_limit", 262_144),
        )


def _integer_bound(values: Mapping[str, object], key: str, default: int) -> int:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"helper {key} is invalid")
    return value


class ControllerAPI:
    """Strict route handlers backed by typed product services."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        state_directory: Path,
        *,
        helper_caller: HelperCaller | None = None,
        observer_helper: Callable[..., Mapping[str, object]] | None = None,
        operation_workers: int = 4,
        operation_capacity: int = 32,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory
        self._lock = threading.RLock()
        if helper_caller is None:
            local = LocalHelperTransport(config)
            helper_caller = local.service
            observer_helper = local.observer
        if observer_helper is None:
            # Tests may inject one config-shaped helper. Adapt it without ever
            # falling back to the SSH transport.
            def observe(
                action: str, values: Mapping[str, object], **_bounds: object
            ) -> Mapping[str, object]:
                return helper_caller(  # type: ignore[misc]
                    self.config,
                    action,
                    values,
                    deadline=time.monotonic() + self.config.policy.limits.helper_seconds,
                )

            observer_helper = observe
        self.helper_caller = helper_caller
        self.observer_helper = observer_helper
        self.applications = ApplicationService(
            connection, config, state_directory, helper_caller=helper_caller
        )
        self.logs = LogService(connection, config, state_directory, helper_caller=helper_caller)
        database_path = Path(connection.execute("PRAGMA database_list").fetchone()["file"])
        self.executor = AsyncOperationExecutor(
            database_path,
            connection,
            workers=operation_workers,
            capacity=operation_capacity,
        )

    def close(self) -> None:
        self.executor.close()

    def wait_for_operations(self) -> None:
        self.executor.wait()

    def router(self) -> Router:
        router = Router()
        routes = (
            ("POST", "/v1/applications", self._create_application),
            ("GET", "/v1/applications/{id}", self._get_application),
            ("POST", "/v1/applications/{id}/enable", self._enable_application),
            ("POST", "/v1/applications/{id}/disable", self._disable_application),
            ("POST", "/v1/applications/{id}/delete", self._delete_application),
            ("POST", "/v1/applications/{id}/deployments", self._create_deployment),
            ("GET", "/v1/applications/{id}/deployments", self._list_deployments),
            ("GET", "/v1/deployments/{id}", self._get_deployment),
            ("GET", "/v1/deployments/{id}/build-log", self._build_log),
            ("GET", "/v1/applications/{id}/runtime-log", self._runtime_log),
            ("GET", "/v1/applications/{id}/environment", self._get_environment),
            ("PUT", "/v1/applications/{id}/environment/{key}", self._put_environment),
            ("DELETE", "/v1/applications/{id}/environment/{key}", self._delete_environment),
            ("POST", "/v1/applications/{id}/environment/import", self._import_environment),
            ("POST", "/v1/applications/{id}/storage", self._create_storage),
            ("GET", "/v1/applications/{id}/storage", self._list_storage),
            ("GET", "/v1/storage/{id}", self._get_storage),
            ("PATCH", "/v1/storage/{id}/label", self._label_storage),
            ("POST", "/v1/storage/{id}/verify", self._verify_storage),
            ("POST", "/v1/storage/{id}/rotate", self._rotate_storage),
            ("DELETE", "/v1/storage/{id}", self._delete_storage),
            ("GET", "/v1/operations/{id}", self._get_operation),
            ("GET", "/v1/admin/status", self._admin_status),
            ("GET", "/v1/admin/hosts", self._admin_hosts),
            ("GET", "/v1/admin/images", self._admin_images),
            ("GET", "/v1/admin/applications", self._admin_applications),
            ("GET", "/v1/admin/deployments", self._admin_deployments),
            ("GET", "/v1/admin/storage", self._admin_storage),
            ("GET", "/v1/admin/operations", self._admin_operations),
        )
        for method, path, handler in routes:
            router.add(method, path, self._safe(handler))
        return router

    def _safe(self, handler: Callable[[Request], Response]) -> Callable[[Request], Response]:
        def call(request: Request) -> Response:
            with self._lock:
                try:
                    return handler(request)
                except HttpError:
                    raise
                except ValidationError as error:
                    raise HttpError(400, "INVALID_REQUEST", safe_summary(error)) from None
                except db.DispatchQueueFullError:
                    raise HttpError(
                        503,
                        "OPERATION_QUEUE_FULL",
                        "controller operation capacity is exhausted",
                        retryable=True,
                    ) from None
                except db.IdempotencyConflictError:
                    raise HttpError(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "Idempotency-Key was already used for different input",
                    ) from None
                except db.UnfinishedOperationError as error:
                    raise HttpError(
                        409,
                        "OPERATION_CONFLICT",
                        "another operation is unfinished for this application",
                        operation_id=error.operation_id,
                    ) from None
                except (DeploymentDeadlineError, ServiceDeadlineError, TimeoutError):
                    raise HttpError(
                        504,
                        "DEADLINE_EXCEEDED",
                        "controller operation exceeded its deadline",
                        retryable=True,
                    ) from None
                except remote.DependencyUnavailable:
                    raise HttpError(
                        503,
                        "DEPENDENCY_UNAVAILABLE",
                        "a controller dependency is unavailable",
                        retryable=True,
                    ) from None
                except remote.HelperError as error:
                    raise HttpError(502, error.code, error.message) from None
                except db.DatabaseError:
                    raise HttpError(
                        409, "STATE_CONFLICT", "controller state prevented the request"
                    ) from None
                except (
                    app.ApplicationError,
                    storage.StorageOperationError,
                    openstack.OpenStackError,
                ):
                    raise HttpError(
                        502,
                        "EXTERNAL_OPERATION_FAILED",
                        "external operation did not complete safely",
                    ) from None

        return call

    @staticmethod
    def _body(
        request: Request,
        *,
        allowed: set[str],
        required: set[str] = frozenset(),
        allow_absent: bool = False,
    ) -> dict[str, Any]:
        if request.body is None and allow_absent:
            return {}
        if not isinstance(request.body, dict) or any(
            not isinstance(key, str) for key in request.body
        ):
            raise HttpError(400, "INVALID_BODY", "request body must be a JSON object")
        keys = set(request.body)
        if not required <= keys or not keys <= allowed:
            raise HttpError(400, "INVALID_BODY", "request body fields are invalid")
        return request.body

    @staticmethod
    def _no_query(request: Request) -> None:
        if request.query:
            raise HttpError(400, "INVALID_QUERY", "this route does not accept query fields")

    @staticmethod
    def _path_uuid(request: Request, name: str = "id") -> str:
        return uuid(request.path_parameters[name], field=f"{name} path parameter")

    def _application(self, identifier: str) -> db.Application:
        application = db.get_application(self.connection, uuid(identifier, field="application ID"))
        if application is None:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "application does not exist")
        return application

    def _resource(self, identifier: str) -> db.ManagedResource:
        resource = db.get_managed_resource(
            self.connection, uuid(identifier, field="storage resource ID")
        )
        if resource is None:
            raise HttpError(404, "STORAGE_NOT_FOUND", "managed storage does not exist")
        return resource

    def _attempt(self, identifier: str) -> db.DeploymentAttempt:
        attempt = db.get_deployment_attempt(
            self.connection, uuid(identifier, field="deployment ID")
        )
        if attempt is None:
            raise HttpError(404, "DEPLOYMENT_NOT_FOUND", "deployment does not exist")
        return attempt

    def _fingerprint(self, request: Request) -> str:
        return db.request_fingerprint(
            {"method": request.method, "path": request.path, "body": request.body}
        )

    def _claim(self, request: Request) -> db.IdempotencyRequest:
        return db.claim_idempotency_request(
            self.connection,
            request_id=request.idempotency_key(),
            request_fingerprint=self._fingerprint(request),
        )

    def _operation_response(
        self, operation_id: str, *, result: Mapping[str, object] | None = None
    ) -> Response:
        body: dict[str, object] = {
            "operationId": operation_id,
            "statusUrl": f"/v1/operations/{operation_id}",
            "result": {"kind": "operation", "id": operation_id},
        }
        return Response(
            202,
            body,
            {"Location": f"/v1/operations/{operation_id}"},
        )

    def _external(
        self,
        request: Request,
        work: Callable[[sqlite3.Connection, str], object],
        *,
        kind: str,
        scope: str,
        claimed: db.IdempotencyRequest | None = None,
    ) -> Response:
        claimed = self._claim(request) if claimed is None else claimed
        if claimed.result_id is not None:
            return self._operation_response(claimed.result_id)
        self.executor.submit(
            self.connection,
            operation_id=claimed.request_id,
            kind=kind,
            scope=scope,
            work=lambda connection: work(connection, claimed.request_id),
        )
        return self._operation_response(claimed.request_id)

    def _create_application(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"slug"}, required={"slug"})
        claimed = self._claim(request)
        if claimed.result_id is not None:
            application = self._application(claimed.result_id)
        else:
            application = db.get_application(self.connection, claimed.request_id)
            if application is None:
                created = self.applications.declare(body["slug"], application_id=claimed.request_id)
                application = self._application(created.application_id)
            elif application.slug != body["slug"]:
                raise db.IdempotencyConflictError("application result does not match request")
            db.complete_idempotency_request(
                self.connection,
                request_id=claimed.request_id,
                result_kind="application",
                result_id=application.application_id,
            )
        return Response(
            201,
            self._application_model(application),
            {"Location": f"/v1/applications/{application.application_id}"},
        )

    def _get_application(self, request: Request) -> Response:
        self._no_query(request)
        if request.body is not None:
            raise HttpError(400, "INVALID_BODY", "read routes do not accept a body")
        application = self._application(self._path_uuid(request))
        observe = status.application_observer(
            self.connection,
            self.config,
            helper_caller=self.observer_helper,
        )
        model = status.app_show(
            self.connection,
            application.application_id,
            observe=observe,
        )
        if model is None:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "application does not exist")
        return Response(200, model)

    def _enable_application(self, request: Request) -> Response:
        self._no_query(request)
        self._body(request, allowed=set(), allow_absent=True)
        application = self._application(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: ApplicationService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).enable(application.application_id, request_id=key),
            kind="app.enable",
            scope=f"app-{application.application_id}",
        )

    def _disable_application(self, request: Request) -> Response:
        self._no_query(request)
        self._body(request, allowed=set(), allow_absent=True)
        application = self._application(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: ApplicationService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).disable(application.application_id, request_id=key),
            kind="app.disable",
            scope=f"app-{application.application_id}",
        )

    def _delete_application(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"confirmation"}, required={"confirmation"})
        claimed = self._claim(request)
        if claimed.result_id is not None:
            return self._operation_response(claimed.result_id)
        application = self._application(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: ApplicationService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).delete(
                application.application_id,
                confirmation=body["confirmation"],
                request_id=key,
            ),
            kind="app.delete",
            scope=f"app-{application.application_id}",
            claimed=claimed,
        )

    def _create_deployment(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request,
            allowed={
                "repository",
                "commit",
                "requestedRef",
                "configurationRevision",
                "configuration",
            },
            required={
                "repository",
                "commit",
                "requestedRef",
                "configurationRevision",
                "configuration",
            },
        )
        application = self._application(self._path_uuid(request))
        configuration = parse_configuration(body["configuration"])
        return self._external(
            request,
            lambda connection, key: DeploymentService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).deploy(
                DeploymentRequest(
                    application.slug,
                    body["repository"],
                    body["requestedRef"],
                    body["commit"],
                    body["configurationRevision"],
                    configuration,
                    key,
                )
            ),
            kind="app.deploy",
            scope=f"app-{application.application_id}",
        )

    def _list_deployments(self, request: Request) -> Response:
        application = self._application(self._path_uuid(request))
        items = [
            self._deployment_model(item)
            for item in db.list_deployment_attempts(self.connection, application.application_id)
        ]
        return Response(200, self._page(request, items, "deploymentId"))

    def _get_deployment(self, request: Request) -> Response:
        self._no_query(request)
        return Response(200, self._deployment_model(self._attempt(self._path_uuid(request))))

    def _build_log(self, request: Request) -> Response:
        attempt = self._attempt(self._path_uuid(request))
        lines, offset = self._log_query(request, allow_offset=True)
        build, chunk = self.logs.build(
            attempt.application_id,
            build_id=attempt.deployment_id,
            lines=lines,
            offset=offset,
        )
        return Response(
            200,
            {
                "deploymentId": build.build_id,
                "text": chunk.text,
                "state": chunk.state,
                "nextOffset": chunk.next_offset,
                "truncated": chunk.truncated,
            },
        )

    def _runtime_log(self, request: Request) -> Response:
        application = self._application(self._path_uuid(request))
        lines, _offset = self._log_query(request, allow_offset=False)
        chunk = self.logs.runtime(application.application_id, lines=lines)
        return Response(
            200,
            {
                "applicationId": application.application_id,
                "text": chunk.text,
                "state": chunk.state,
                "nextOffset": chunk.next_offset,
                "truncated": chunk.truncated,
            },
        )

    def _get_environment(self, request: Request) -> Response:
        self._no_query(request)
        application = self._application(self._path_uuid(request))
        revision = db.get_environment_revision(self.connection, application.application_id)
        if revision is None:
            raise db.DatabaseError("application environment revision is missing")
        keys = db.list_environment_keys(self.connection, application_id=application.application_id)
        return Response(
            200,
            {
                "applicationId": application.application_id,
                "revision": revision.revision,
                "updatedAt": revision.updated_at,
                "keys": [{"name": item.key_name, "owner": item.owner} for item in keys],
            },
        )

    def _put_environment(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"value"}, required={"value"})
        application = self._application(self._path_uuid(request))
        key_name = env_key(request.path_parameters["key"])
        value = bounded_text(
            body["value"],
            field="environment value",
            maximum=self.config.policy.limits.environment_value_bytes,
        )
        return self._external(
            request,
            lambda connection, key: EnvironmentService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).mutate(
                EnvironmentMutationRequest(
                    "set", application.application_id, {key_name: value}, request_id=key
                )
            ),
            kind="app.env.set",
            scope=f"app-{application.application_id}",
        )

    def _delete_environment(self, request: Request) -> Response:
        self._no_query(request)
        self._body(request, allowed=set(), allow_absent=True)
        application = self._application(self._path_uuid(request))
        key_name = env_key(request.path_parameters["key"])
        return self._external(
            request,
            lambda connection, key: EnvironmentService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).mutate(
                EnvironmentMutationRequest(
                    "unset",
                    application.application_id,
                    removals=(key_name,),
                    request_id=key,
                )
            ),
            kind="app.env.unset",
            scope=f"app-{application.application_id}",
        )

    def _import_environment(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"dotenv"}, required={"dotenv"})
        application = self._application(self._path_uuid(request))
        dotenv = bounded_text(
            body["dotenv"],
            field="dotenv input",
            maximum=self.config.policy.limits.dotenv_bytes,
        )
        updates = app.parse_dotenv(dotenv, maximum_bytes=self.config.policy.limits.dotenv_bytes)
        return self._external(
            request,
            lambda connection, key: EnvironmentService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).mutate(
                EnvironmentMutationRequest(
                    "import", application.application_id, updates, request_id=key
                )
            ),
            kind="app.env.import",
            scope=f"app-{application.application_id}",
        )

    def _create_storage(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request,
            allowed={"type", "name"},
            required={"type"},
        )
        application = self._application(self._path_uuid(request))
        resource_type = self._resource_type(body["type"])
        machine_name = resource_name(body.get("name", "default"))
        return self._external(
            request,
            lambda connection, key: StorageService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).mutate(
                StorageMutationRequest(
                    "create",
                    application.application_id,
                    (resource_type,),
                    resource_name=machine_name,
                    request_id=key,
                )
            ),
            kind=f"storage.{resource_type}.create",
            scope=f"app-{application.application_id}",
        )

    def _list_storage(self, request: Request) -> Response:
        application = self._application(self._path_uuid(request))
        items = [
            self._storage_model(item)
            for item in db.list_managed_resources(
                self.connection, application_id=application.application_id
            )
        ]
        return Response(200, self._page(request, items, "resourceId"))

    def _get_storage(self, request: Request) -> Response:
        self._no_query(request)
        return Response(200, self._storage_model(self._resource(self._path_uuid(request))))

    def _label_storage(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"displayLabel"}, required={"displayLabel"})
        resource = self._resource(self._path_uuid(request))
        claimed = self._claim(request)
        if claimed.result_id is not None:
            renamed = self._resource(claimed.result_id)
        else:
            renamed = db.rename_managed_resource(
                self.connection, resource.resource_id, body["displayLabel"]
            )
            db.complete_idempotency_request(
                self.connection,
                request_id=claimed.request_id,
                result_kind="storage",
                result_id=renamed.resource_id,
            )
        return Response(200, self._storage_model(renamed))

    def _verify_storage(self, request: Request) -> Response:
        return self._storage_action(request, "verify")

    def _rotate_storage(self, request: Request) -> Response:
        return self._storage_action(request, "rotate")

    def _storage_action(self, request: Request, action: str) -> Response:
        self._no_query(request)
        self._body(request, allowed=set(), allow_absent=True)
        resource = self._resource(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: StorageService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).mutate(
                StorageMutationRequest(
                    action,  # type: ignore[arg-type]
                    resource.application_id,
                    (resource.resource_type,),
                    resource_name=resource.resource_name,
                    request_id=key,
                )
            ),
            kind=f"storage.{resource.resource_type}.{action}",
            scope=f"app-{resource.application_id}",
        )

    def _delete_storage(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request,
            allowed={"confirmation", "purge"},
            required={"confirmation"},
        )
        claimed = self._claim(request)
        if claimed.result_id is not None:
            return self._operation_response(claimed.result_id)
        resource = self._resource(self._path_uuid(request))
        purge = body.get("purge", False)
        if not isinstance(purge, bool):
            raise HttpError(400, "INVALID_BODY", "purge must be boolean")
        return self._external(
            request,
            lambda connection, key: StorageService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).mutate(
                StorageMutationRequest(
                    "remove",
                    resource.application_id,
                    (resource.resource_type,),
                    resource_name=resource.resource_name,
                    confirm_name=body["confirmation"],
                    purge_s3=purge,
                    request_id=key,
                )
            ),
            kind=f"storage.{resource.resource_type}.remove",
            scope=f"app-{resource.application_id}",
            claimed=claimed,
        )

    def _get_operation(self, request: Request) -> Response:
        self._no_query(request)
        identifier = self._path_uuid(request)
        operation = db.get_operation(self.connection, identifier)
        if operation is not None:
            return Response(200, self._operation_model(operation))
        dispatch = db.get_operation_dispatch(self.connection, identifier)
        if dispatch is None:
            raise HttpError(404, "OPERATION_NOT_FOUND", "operation does not exist")
        return Response(200, self._dispatch_model(dispatch))

    def _admin_status(self, request: Request) -> Response:
        self._no_query(request)
        observers = status.live_observers(
            self.connection,
            self.config,
            helper_caller=self.observer_helper,
        )
        return Response(
            200,
            status.status_show(
                self.connection,
                observe_infrastructure=observers.infrastructure,
                observe_application=observers.application,
                observe_storage=observers.storage,
            ),
        )

    def _admin_hosts(self, request: Request) -> Response:
        self._no_query(request)
        observe = status.infrastructure_observer(
            self.config.platform,
            self.connection,
            timeout_seconds=self.config.policy.limits.process_seconds,
        )
        return Response(200, {"items": status.infra_list(self.connection, observe=observe)})

    def _admin_images(self, request: Request) -> Response:
        self._no_query(request)
        return Response(
            200,
            {
                "items": [
                    {
                        "role": item.role,
                        "imageId": item.image_id,
                        "displayName": item.display_name,
                        "sourceCommit": item.source_commit,
                        "compatibilityHash": item.compatibility_hash,
                        "selectedAt": item.selected_at,
                    }
                    for item in db.list_image_selections(self.connection)
                ]
            },
        )

    def _admin_applications(self, request: Request) -> Response:
        # Include tombstones for diagnosis without exposing worker/provider data.
        rows = self.connection.execute(
            "SELECT application.application_id, application.slug, "
            "application.desired_running, application.url, application.created_at, "
            "application.updated_at, tombstone.deleted_at "
            "FROM applications AS application LEFT JOIN application_slug_tombstones "
            "AS tombstone USING (slug) ORDER BY application.slug, application.application_id"
        ).fetchall()
        items = [
            {
                "applicationId": row["application_id"],
                "slug": row["slug"],
                "enabled": bool(row["desired_running"]),
                "url": row["url"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "deletedAt": row["deleted_at"],
            }
            for row in rows
        ]
        return Response(200, self._page(request, items, "applicationId"))

    def _admin_deployments(self, request: Request) -> Response:
        rows = self.connection.execute(
            "SELECT deployment_id FROM deployment_attempts "
            "ORDER BY requested_at DESC, deployment_id DESC"
        ).fetchall()
        attempts = [
            attempt
            for row in rows
            if (attempt := db.get_deployment_attempt(self.connection, row["deployment_id"]))
            is not None
        ]
        return Response(
            200,
            self._page(
                request,
                [self._deployment_model(item) for item in attempts],
                "deploymentId",
            ),
        )

    def _admin_storage(self, request: Request) -> Response:
        items = [
            self._storage_model(item, admin=True)
            for item in db.list_managed_resources(self.connection)
        ]
        return Response(200, self._page(request, items, "resourceId"))

    def _admin_operations(self, request: Request) -> Response:
        rows = self.connection.execute(
            "SELECT operation_id FROM operations ORDER BY updated_at DESC, operation_id DESC"
        ).fetchall()
        operations = [
            operation
            for row in rows
            if (operation := db.get_operation(self.connection, row["operation_id"])) is not None
        ]
        known = {item.operation_id for item in operations}
        queued = [
            item
            for item in db.list_operation_dispatches(self.connection)
            if item.operation_id not in known
        ]
        items = [self._operation_model(item) for item in operations]
        items.extend(self._dispatch_model(item) for item in queued)
        items.sort(
            key=lambda item: (str(item["updatedAt"]), str(item["operationId"])), reverse=True
        )
        return Response(
            200,
            self._page(request, items, "operationId"),
        )

    @staticmethod
    def _application_model(application: db.Application) -> dict[str, object]:
        return {
            "applicationId": application.application_id,
            "slug": application.slug,
            "url": application.url,
            "enabled": application.desired_running,
            "createdAt": application.created_at,
            "updatedAt": application.updated_at,
        }

    @staticmethod
    def _deployment_model(attempt: db.DeploymentAttempt) -> dict[str, object]:
        return {
            "deploymentId": attempt.deployment_id,
            "applicationId": attempt.application_id,
            "status": attempt.status,
            "snapshotKind": attempt.snapshot_kind,
            "repositoryCommit": attempt.source_commit,
            "requestedRef": attempt.requested_ref,
            "configurationRevision": attempt.configuration_revision,
            "environmentRevision": attempt.environment_revision,
            "recipeHash": attempt.recipe_hash,
            "imageDigest": attempt.image_digest,
            "nomadVersion": attempt.nomad_version,
            "safeError": attempt.safe_error,
            "cleanupState": attempt.cleanup_state,
            "requestedAt": attempt.requested_at,
            "updatedAt": attempt.updated_at,
            "acceptedAt": attempt.accepted_at,
            "lastHealthyAt": attempt.last_healthy_at,
        }

    @staticmethod
    def _storage_model(resource: db.ManagedResource, *, admin: bool = False) -> dict[str, object]:
        quotas: dict[str, object] = {}
        for key, value in (
            ("postgresConnections", resource.postgres_connections),
            ("measuredTargetBytes", resource.measured_target_bytes),
            ("s3Bytes", resource.s3_bytes),
            ("s3Objects", resource.s3_objects),
        ):
            if value is not None:
                quotas[key] = value
        result: dict[str, object] = {
            "resourceId": resource.resource_id,
            "applicationId": resource.application_id,
            "type": resource.resource_type,
            "name": resource.resource_name,
            "displayLabel": resource.display_label,
            "lifecycleState": resource.lifecycle_state,
            "quotas": quotas,
            "lastVerifiedAt": resource.last_verified_at,
            "createdAt": resource.created_at,
            "updatedAt": resource.updated_at,
        }
        if admin:
            result["providerId"] = resource.provider_id
            result["providerName"] = resource.provider_name
        return result

    @staticmethod
    def _operation_model(operation: db.Operation) -> dict[str, object]:
        return {
            "operationId": operation.operation_id,
            "kind": operation.kind,
            "scope": operation.scope,
            "status": operation.status,
            "phase": operation.phase,
            "startedAt": operation.started_at,
            "updatedAt": operation.updated_at,
            "deadlineAt": operation.deadline_at,
            "safeError": operation.safe_error,
            "cleanupState": operation.cleanup_state,
        }

    @staticmethod
    def _dispatch_model(dispatch: db.OperationDispatch) -> dict[str, object]:
        status = "recovery_required" if dispatch.status == "recovery_required" else "running"
        phase = {
            "pending": "queued",
            "running": "executing",
            "recovery_required": "startup_interrupted",
            "finished": "finishing",
        }[dispatch.status]
        return {
            "operationId": dispatch.operation_id,
            "kind": dispatch.kind,
            "scope": dispatch.scope,
            "status": status,
            "phase": phase,
            "startedAt": dispatch.created_at,
            "updatedAt": dispatch.updated_at,
            "deadlineAt": None,
            "safeError": dispatch.safe_error,
            "cleanupState": "pending",
        }

    @staticmethod
    def _resource_type(value: object) -> str:
        if value not in {"postgres", "mongo", "s3"}:
            raise ValidationError("storage type must be postgres, mongo, or s3")
        assert isinstance(value, str)
        return value

    @staticmethod
    def _single_query(request: Request, name: str) -> str | None:
        values = request.query.get(name)
        if values is None:
            return None
        if len(values) != 1 or not values[0]:
            raise HttpError(400, "INVALID_QUERY", f"{name} query field is invalid")
        return values[0]

    def _page(
        self,
        request: Request,
        items: Sequence[Mapping[str, object]],
        identity_key: str,
    ) -> Mapping[str, object]:
        if set(request.query) - {"limit", "cursor"}:
            raise HttpError(400, "INVALID_QUERY", "pagination query fields are invalid")
        raw_limit = self._single_query(request, "limit")
        try:
            limit = 50 if raw_limit is None else int(raw_limit)
        except ValueError:
            raise HttpError(400, "INVALID_QUERY", "limit must be an integer") from None
        if not 1 <= limit <= _MAX_PAGE:
            raise HttpError(400, "INVALID_QUERY", "limit must be from 1 through 100")
        cursor = self._single_query(request, "cursor")
        start = 0
        if cursor is not None:
            uuid(cursor, field="pagination cursor")
            for index, item in enumerate(items):
                if item.get(identity_key) == cursor:
                    start = index + 1
                    break
            else:
                raise HttpError(400, "INVALID_QUERY", "pagination cursor is unknown")
        selected = list(items[start : start + limit])
        truncated = start + limit < len(items)
        return {
            "items": selected,
            "nextCursor": selected[-1][identity_key] if truncated and selected else None,
            "truncated": truncated,
        }

    def _log_query(self, request: Request, *, allow_offset: bool) -> tuple[int, int | None]:
        allowed = {"lines", "offset"} if allow_offset else {"lines"}
        if set(request.query) - allowed:
            raise HttpError(400, "INVALID_QUERY", "log query fields are invalid")
        raw_lines = self._single_query(request, "lines")
        try:
            lines = 200 if raw_lines is None else int(raw_lines)
        except ValueError:
            raise HttpError(400, "INVALID_QUERY", "lines must be an integer") from None
        if not 1 <= lines <= _MAX_LOG_LINES:
            raise HttpError(400, "INVALID_QUERY", "lines must be from 1 through 1000")
        raw_offset = self._single_query(request, "offset") if allow_offset else None
        try:
            offset = None if raw_offset is None else int(raw_offset)
        except ValueError:
            raise HttpError(400, "INVALID_QUERY", "offset must be an integer") from None
        if offset is not None and not 0 <= offset <= self.config.policy.limits.build_log_bytes:
            raise HttpError(400, "INVALID_QUERY", "offset is outside the build log limit")
        return lines, offset
