"""Production controller API composition over the bounded Unix HTTP transport."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .. import openstack, remote
from ..config import Config
from ..instance_contract import CapacityError, hard_quota
from ..runtime import safe_summary
from ..validation import (
    ValidationError,
    bounded_text,
    commit,
    env_key,
    flavor_reference,
    repository_url,
    resource_name,
    uuid,
)
from . import application_runtime as app
from . import builder_settings, fixed_ip_service, sizing, status, storage, storage_limits
from . import database as db
from .application_service import ApplicationService
from .async_operations import AsyncOperationExecutor
from .builder_settings import (
    APP_BUILDER_SIZE_KIND,
    DEFAULT_BUILDER_SIZE_KIND,
    BuilderSettingsService,
)
from .deployment_config import branch_name, parse_configuration
from .deployment_reads import configuration_snapshot, source_repository
from .deployment_service import (
    DeploymentDeadlineError,
    DeploymentRequest,
    DeploymentService,
)
from .environment_service import EnvironmentMutationRequest, EnvironmentService
from .http import HttpError, Request, Response, Router
from .image_service import IMAGE_SELECTION_KIND, ImageSelectionService, hosted_role
from .log_service import LogService
from .service_support import ServiceDeadlineError, logged_helper, operation_deadline, wall_deadline
from .storage_contract import validate_expected_quotas, validate_quotas
from .storage_instances import MIGRATE_KIND, InstanceMigrationService
from .storage_limits import StorageLimitsService
from .storage_service import StorageMutationRequest, StorageService

API_VERSION = 1
_MAX_PAGE = 100
_MAX_LOG_LINES = 1_000
# Public codes for failures a caller can act on, keyed by the final phase that
# records them durably.
_FAILURE_CODES = {"platform_busy": "PLATFORM_BUSY"}
# How long a finished live app probe answers further reads of the same
# accepted state. It matches the broker's owner-page cache, and the
# projection's checkedAt still says when the probe ran.
_LIVE_REUSE_SECONDS = 2.0
# Readers sharing another reader's probe wait at most this long, then answer
# 504: the broker gives up sooner, so longer waits would only pile up
# connections behind a hung probe.
_LIVE_FOLLOWER_WAIT_SECONDS = 20.0

# A checkout check names the runtime version it asks for, or the default.
_RUNTIME_CHECKS = frozenset({"runtime-version", "runtime-default"})
HelperCaller = Callable[..., Mapping[str, object]]


def _failure_code(operation: db.Operation) -> str | None:
    # A build that couldn't look up runtime versions ends like a rejected one,
    # but the owner should simply try again.
    if (
        operation.phase == "build_rejected"
        and operation.refs.get("rejection") == "runtime_unavailable"
    ):
        return "RUNTIME_UNAVAILABLE"
    return _FAILURE_CODES.get(operation.phase)


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
        timeout = float(
            config.policy.limits.migration_app_seconds
            if action.startswith("storage.instances.") or action == "storage.backup.ensure"
            else config.policy.limits.helper_seconds
        )
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


@dataclass(slots=True)
class _Flight:
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, object] | None = None
    finished_at: float | None = None


class _SingleFlight:
    """Run at most one probe per key, and share its result briefly.

    Callers with a key that is already being probed wait for that probe rather
    than start their own, so a burst of page polls costs one helper process.
    """

    def __init__(self, reuse_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._reuse_seconds = reuse_seconds
        self._clock = clock
        self._guard = threading.Lock()
        self._flights: dict[Hashable, _Flight] = {}

    def run(
        self,
        key: Hashable,
        probe: Callable[[], dict[str, object]],
        *,
        wait_seconds: float,
    ) -> dict[str, object]:
        with self._guard:
            now = self._clock()
            for expired in [
                item
                for item, flight in self._flights.items()
                if flight.finished_at is not None
                and now - flight.finished_at >= self._reuse_seconds
            ]:
                del self._flights[expired]
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = self._flights[key] = _Flight()
        if leader:
            try:
                flight.result = probe()
            finally:
                with self._guard:
                    flight.finished_at = self._clock()
                    # A probe that raised is not shared; the next caller retries.
                    if flight.result is None and self._flights.get(key) is flight:
                        del self._flights[key]
                flight.done.set()
        elif not flight.done.wait(wait_seconds):
            raise TimeoutError("shared live observation did not finish")
        if flight.result is None:
            raise TimeoutError("shared live observation failed")
        return dict(flight.result)


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
        retry_poll_seconds: float = 1.0,
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory
        self._lock = threading.RLock()
        if helper_caller is None:
            local = LocalHelperTransport(config)
            helper_caller = local.service
            observer_helper = local.observer
        helper_caller = logged_helper(helper_caller)
        if observer_helper is None:
            # Tests may inject one config-shaped helper. Adapt it without ever
            # falling back to the SSH transport.
            def observe(
                action: str, values: Mapping[str, object], **_bounds: object
            ) -> Mapping[str, object]:
                return helper_caller(
                    self.config,
                    action,
                    values,
                    deadline=time.monotonic() + self.config.policy.limits.helper_seconds,
                )

            observe.__dict__["_logs_helper_failures"] = True
            observer_helper = observe
        self.helper_caller = helper_caller
        self.observer_helper = logged_helper(observer_helper, config_shaped=False)
        self._live = _SingleFlight(_LIVE_REUSE_SECONDS)
        self.applications = ApplicationService(
            connection, config, state_directory, helper_caller=helper_caller
        )
        self.logs = LogService(connection, config, state_directory, helper_caller=helper_caller)
        database_path = Path(connection.execute("PRAGMA database_list").fetchone()["file"])
        self._database_path = database_path
        self.executor = AsyncOperationExecutor(
            database_path,
            connection,
            workers=operation_workers,
            capacity=operation_capacity,
            retry_poll_seconds=retry_poll_seconds,
            finishing_work=self._finish_operation,
        )

    def _finish_operation(self, connection: sqlite3.Connection, operation_id: str) -> None:
        from .deployment_service import finish_accepted_operation

        deadline = time.monotonic() + min(120, self.config.policy.limits.process_seconds)
        operation = db.get_operation(connection, operation_id)
        if operation is None or not operation.finishing:
            raise db.DatabaseError("accepted finishing intent is missing")
        from .. import runtime

        with runtime.lock(self.state_directory, operation.scope, wait=True, deadline=deadline):
            operation = db.renew_operation_deadline(
                connection, operation_id, wall_deadline(deadline)
            )
            from .fixed_ip_service import worker_helper

            finish_accepted_operation(
                connection,
                self.config,
                operation,
                helper_caller=worker_helper(connection, self.helper_caller),
                deadline=deadline,
            )

    def close(self) -> None:
        self.executor.close()

    def wait_for_operations(self) -> None:
        self.executor.wait()

    def router(self, capability: Literal["all", "project", "privileged"] = "all") -> Router:
        """Build a route set for one host-enforced socket capability."""
        router = Router()
        destructive = {
            ("POST", "/v1/applications/{id}/delete"),
        }
        routes = (
            ("GET", "/v1/health", self._health),
            ("GET", "/v1/flavors", self._flavors),
            ("GET", "/v1/settings/default-builder-size", self._builder_settings),
            ("PUT", "/v1/settings/default-builder-size", self._select_builder_size),
            ("POST", "/v1/applications", self._create_application),
            ("GET", "/v1/applications/{id}", self._get_application),
            ("GET", "/v1/applications/{id}/resize-plan", self._resize_plan),
            ("GET", "/v1/applications/{id}/builder-size", self._application_builder_size),
            ("PUT", "/v1/applications/{id}/builder-size", self._select_application_builder_size),
            ("POST", "/v1/applications/{id}/enable", self._enable_application),
            ("POST", "/v1/applications/{id}/disable", self._disable_application),
            ("POST", "/v1/applications/{id}/restart", self._restart_application),
            ("POST", "/v1/applications/{id}/delete", self._delete_application),
            ("POST", "/v1/applications/{id}/deployments", self._create_deployment),
            ("GET", "/v1/applications/{id}/deployments", self._list_deployments),
            ("GET", "/v1/deployments/{id}", self._get_deployment),
            ("GET", "/v1/deployments/{id}/build-log", self._build_log),
            ("GET", "/v1/deployments/{id}/startup-log", self._startup_log),
            ("GET", "/v1/applications/{id}/runtime-log", self._runtime_log),
            ("GET", "/v1/applications/{id}/source-key", self._get_source_key),
            ("POST", "/v1/applications/{id}/source-key", self._create_source_key),
            ("DELETE", "/v1/applications/{id}/source-key", self._delete_source_key),
            ("POST", "/v1/applications/{id}/source-key/check", self._check_source_key),
            ("POST", "/v1/applications/{id}/source/commits", self._source_commits),
            ("POST", "/v1/applications/{id}/source/check", self._source_preflight),
            ("GET", "/v1/applications/{id}/environment", self._get_environment),
            ("PUT", "/v1/applications/{id}/environment/{key}", self._put_environment),
            ("DELETE", "/v1/applications/{id}/environment/{key}", self._delete_environment),
            ("POST", "/v1/applications/{id}/environment/import", self._import_environment),
            ("POST", "/v1/applications/{id}/storage", self._create_storage),
            ("GET", "/v1/applications/{id}/storage", self._list_storage),
            ("GET", "/v1/storage/{id}", self._get_storage),
            ("PUT", "/v1/storage/{id}/limits", self._storage_limits),
            ("PATCH", "/v1/storage/{id}/label", self._label_storage),
            ("POST", "/v1/storage/{id}/verify", self._verify_storage),
            ("POST", "/v1/storage/{id}/rotate", self._rotate_storage),
            ("DELETE", "/v1/storage/{id}", self._delete_storage),
            ("GET", "/v1/operations/{id}", self._get_operation),
            ("GET", "/v1/admin/status", self._admin_status),
            ("GET", "/v1/admin/capabilities", self._capabilities),
            ("GET", "/v1/admin/hosts", self._admin_hosts),
            ("GET", "/v1/admin/images", self._admin_images),
            ("GET", "/v1/admin/settings/default-builder-size", self._builder_settings),
            ("PUT", "/v1/admin/settings/default-builder-size", self._select_builder_size),
            ("POST", "/v1/admin/images/{role}/selection", self._select_hosted_image),
            ("GET", "/v1/admin/applications", self._admin_applications),
            ("GET", "/v1/admin/applications/{id}/fixed-ip", self._get_fixed_ip),
            ("POST", "/v1/admin/applications/{id}/fixed-ip/plan", self._plan_fixed_ip),
            ("POST", "/v1/admin/applications/{id}/fixed-ip", self._mutate_fixed_ip),
            ("GET", "/v1/admin/applications/{id}/public-ip", self._get_public_ip),
            ("POST", "/v1/admin/applications/{id}/public-ip/plan", self._plan_public_ip),
            ("POST", "/v1/admin/applications/{id}/public-ip", self._mutate_public_ip),
            ("GET", "/v1/admin/applications/{id}/resize-plan", self._resize_plan),
            ("POST", "/v1/admin/applications/{id}/resize", self._resize),
            ("GET", "/v1/admin/applications/{id}/rollback-plan", self._rollback_plan),
            ("POST", "/v1/admin/applications/{id}/rollback", self._rollback),
            ("POST", "/v1/admin/applications/{id}/deployments", self._operator_deployment),
            ("GET", "/v1/admin/operations/{id}", self._get_operation),
            ("GET", "/v1/admin/deployments", self._admin_deployments),
            ("GET", "/v1/admin/storage", self._admin_storage),
            ("POST", "/v1/admin/storage/repair-postgres", self._repair_postgres),
            ("POST", "/v1/admin/storage/migrate-instances", self._migrate_instances),
            ("POST", "/v1/admin/storage/abort-migration", self._abort_instances),
            ("GET", "/v1/admin/operations", self._admin_operations),
        )
        for method, path, handler in routes:
            is_privileged = path.startswith("/v1/admin/") or (method, path) in destructive
            if capability == "project" and is_privileged:
                continue
            if capability == "privileged" and not is_privileged:
                continue
            router.add(method, path, self._safe(handler))
        return router

    @staticmethod
    def _health(_request: Request) -> Response:
        return Response(200, {"status": "ok", "capability": "project"})

    def _safe(self, handler: Callable[[Request], Response]) -> Callable[[Request], Response]:
        def call(request: Request) -> Response:
            # A slow provider observation must not block operation polling or
            # liveness. Polling uses its own connection, never the shared writer.
            # Slow external reads of one app (Nomad logs, deploy-key git reads)
            # also skip the lock: they read the database through their own
            # snapshot and must not stall deploys and status reads behind them.
            # So do the app page's database-only reads (environment names,
            # storage): queued behind slow locked work, they would time out.
            # The app status read probes the helper and public route for up to
            # helperSeconds, so it holds neither the lock nor its snapshot then.
            independent = handler in (
                self._get_operation,
                self._health,
                self._get_application,
                self._runtime_log,
                self._get_source_key,
                self._check_source_key,
                self._source_commits,
                self._source_preflight,
                self._get_environment,
                self._list_storage,
                self._get_storage,
            )
            with nullcontext() if independent else self._lock:
                try:
                    return handler(request)
                except HttpError:
                    raise
                except CapacityError as error:
                    raise HttpError(400, error.code, str(error)) from None
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
                except db.FinishingOperationConflictError as error:
                    raise HttpError(
                        409,
                        "POST_ACCEPTANCE_CONFLICT",
                        "accepted operation has finishing work pending; this change must wait until cleanup or recovery completes",
                        operation_id=error.operation_id,
                    ) from None
                except db.UnfinishedOperationError as error:
                    raise HttpError(
                        409,
                        "OPERATION_CONFLICT",
                        "another operation is unfinished for this resource scope",
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
                    retryable = error.code in {
                        "INSTANCE_MANAGER_UNAVAILABLE",
                        "INSTANCE_COPY_IN_PROGRESS",
                    }
                    raise HttpError(
                        503 if retryable else 502, error.code, error.message, retryable=retryable
                    ) from None
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
        required: set[str] | frozenset[str] = frozenset(),
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

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        """One read transaction on a private query-only connection, never the writer.

        Handlers outside the API lock read through this, so a coherent view
        never waits for, or interleaves with, locked work on the shared writer.
        """
        with closing(db.connect(self._database_path, create=False)) as connection:
            connection.execute("PRAGMA query_only = ON")
            with db.transaction(connection, immediate=False):
                yield connection

    def _application(
        self, identifier: str, connection: sqlite3.Connection | None = None
    ) -> db.Application:
        application = db.get_application(
            self.connection if connection is None else connection,
            uuid(identifier, field="application ID"),
        )
        if application is None:
            raise HttpError(404, "APPLICATION_NOT_FOUND", "application does not exist")
        return application

    def _application_snapshot(self, identifier: str) -> db.Application:
        """The app from a private read-only connection, for handlers outside the lock."""
        with self._snapshot() as connection:
            return self._application(identifier, connection)

    def _resource(
        self, identifier: str, connection: sqlite3.Connection | None = None
    ) -> db.ManagedResource:
        resource = db.get_managed_resource(
            self.connection if connection is None else connection,
            uuid(identifier, field="storage resource ID"),
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

    def _operation_response(self, operation_id: str, *, admin: bool = False) -> Response:
        status_url = f"/v1/{'admin/' if admin else ''}operations/{operation_id}"
        body: dict[str, object] = {
            "operationId": operation_id,
            "statusUrl": status_url,
            "result": {"kind": "operation", "id": operation_id},
        }
        return Response(
            202,
            body,
            {"Location": status_url},
        )

    def _is_recovery_result(self, claimed: db.IdempotencyRequest) -> bool:
        if claimed.result_id is None:
            return False
        dispatch = db.get_operation_dispatch(self.connection, claimed.result_id)
        operation = db.get_operation(self.connection, claimed.result_id)
        return (
            dispatch is not None
            and operation is not None
            and dispatch.status == "recovery_required"
            and (operation.status == "recovery_required" or operation.finishing)
        )

    def _external(
        self,
        request: Request,
        work: Callable[[sqlite3.Connection, str], object],
        *,
        kind: str,
        scope: str,
        claimed: db.IdempotencyRequest | None = None,
        admin: bool = False,
    ) -> Response:
        claimed = self._claim(request) if claimed is None else claimed
        admin = admin or request.path.startswith("/v1/admin/")

        def execute(connection: sqlite3.Connection) -> object:
            return work(connection, claimed.request_id)

        if claimed.result_id is not None:
            dispatch = db.get_operation_dispatch(self.connection, claimed.result_id)
            operation = db.get_operation(self.connection, claimed.result_id)
            if (
                dispatch is not None
                and operation is not None
                and dispatch.status == "recovery_required"
                and (operation.status == "recovery_required" or operation.finishing)
            ):
                self.executor.resubmit_recovery(
                    self.connection,
                    operation_id=claimed.result_id,
                    kind=kind,
                    scope=scope,
                    work=execute,
                )
            return self._operation_response(claimed.result_id, admin=admin)
        if (
            kind in {"app.deploy", "app.enable"}
            and scope.startswith("app-")
            and any(
                resource.instance_id is not None
                for resource in db.list_managed_resources(self.connection, application_id=scope[4:])
            )
        ):
            # Before accepting a long operation, return a retryable dependency
            # failure if instance network administration is unavailable.
            try:
                available = self.helper_caller(
                    self.config, "storage.instances.available", {}, deadline=time.monotonic() + 10
                )
            except Exception:
                raise HttpError(
                    503,
                    "INSTANCE_MANAGER_UNAVAILABLE",
                    "storage instance manager is unavailable; retry",
                    retryable=True,
                ) from None
            if available.get("available") is not True:
                raise HttpError(
                    503,
                    "INSTANCE_MANAGER_UNAVAILABLE",
                    "storage instance manager is unavailable; retry",
                    retryable=True,
                )
        self.executor.submit(
            self.connection,
            operation_id=claimed.request_id,
            kind=kind,
            scope=scope,
            work=execute,
        )
        return self._operation_response(claimed.request_id, admin=admin)

    def _create_application(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"slug"}, required={"slug"})
        claimed = self._claim(request)
        if claimed.result_id is not None:
            application = self._application(claimed.result_id)
        else:
            existing = db.get_application(self.connection, claimed.request_id)
            if existing is None:
                created = self.applications.declare(body["slug"], application_id=claimed.request_id)
                application = self._application(created.application_id)
            else:
                application = existing
                if application.slug != body["slug"]:
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
        # Accepted state and the probe's inputs come from one snapshot, closed
        # before the probe so it never pins a read transaction for minutes.
        with self._snapshot() as connection:
            application = self._application(self._path_uuid(request), connection)
            model = status.app_show(connection, application.application_id)
            if model is None:
                raise HttpError(404, "APPLICATION_NOT_FOUND", "application does not exist")
            deployment = db.get_deployment(connection, application.application_id)
            observe = status.application_observer(
                connection,
                self.config,
                helper_caller=self.observer_helper,
            )
            model["requiresMaintenance"] = (
                fixed_ip_service.get(connection, application.application_id) is not None
            )
        # Reads that saw the same app and deployment rows share one probe. A
        # deploy, enable or disable changes those rows, so the next read probes.
        limits = self.config.policy.limits
        model["live"] = self._live.run(
            (application, deployment),
            lambda: status.app_live(application.application_id, observe=observe),
            wait_seconds=min(
                _LIVE_FOLLOWER_WAIT_SECONDS, limits.helper_seconds + limits.http_seconds
            ),
        )
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

    def _restart_application(self, request: Request) -> Response:
        self._no_query(request)
        self._body(request, allowed=set(), allow_absent=True)
        application = self._application(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: ApplicationService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).restart(application.application_id, request_id=key),
            kind="app.restart",
            scope=f"app-{application.application_id}",
        )

    def _delete_application(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"confirmation"}, required={"confirmation"})
        claimed = self._claim(request)
        if claimed.result_id is not None and not self._is_recovery_result(claimed):
            return self._operation_response(claimed.result_id, admin=True)
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
            admin=True,
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
                "maintenance",
                "plan",
            },
            required={
                "repository",
                "commit",
                "requestedRef",
                "configurationRevision",
                "configuration",
            },
        )
        if type(body.get("maintenance", False)) is not bool or (
            "plan" in body and not isinstance(body["plan"], dict)
        ):
            raise ValidationError("maintenance must be boolean and plan must be an object")
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
                    body.get("plan"),
                    maintenance=body.get("maintenance", False),
                )
            ),
            kind="app.deploy",
            scope=f"app-{application.application_id}",
        )

    def _flavors(self, request: Request) -> Response:
        self._no_query(request)
        if request.body is not None:
            raise HttpError(400, "INVALID_BODY", "read routes do not accept a body")
        return Response(
            200,
            {
                "items": [
                    sizing.flavor_projection(item)
                    for item in openstack.observe_flavors(self.config.platform)
                    if item.ram_mib - sizing.reserve(item.ram_mib, sizing.MEMORY_RESERVE_MIB) >= 64
                ]
            },
        )

    def _builder_settings(self, request: Request) -> Response:
        self._no_query(request)
        if request.body is not None:
            raise HttpError(400, "INVALID_BODY", "read routes do not accept a body")
        flavor = openstack.observe_flavor_capacity(
            self.config.platform, builder_settings.default_flavor(self.connection, self.config)
        )
        return Response(200, {"flavor": sizing.flavor_projection(flavor)})

    def _select_builder_size(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request, allowed={"flavor", "expectedFlavor"}, required={"flavor", "expectedFlavor"}
        )
        reference = flavor_reference(body["flavor"])
        expected = flavor_reference(body["expectedFlavor"])
        return self._external(
            request,
            lambda connection, key: BuilderSettingsService(
                connection, self.config, self.state_directory
            ).select(reference, expected, request_id=key),
            kind=DEFAULT_BUILDER_SIZE_KIND,
            scope="infrastructure",
        )

    def _application_builder_size(self, request: Request) -> Response:
        self._no_query(request)
        if request.body is not None:
            raise HttpError(400, "INVALID_BODY", "read routes do not accept a body")
        application_id = self._application(self._path_uuid(request)).application_id
        selected = db.get_application_builder_flavor(self.connection, application_id)
        default = openstack.observe_flavor_capacity(
            self.config.platform, builder_settings.default_flavor(self.connection, self.config)
        )
        flavor = (
            default
            if selected is None
            else openstack.observe_flavor_capacity(self.config.platform, selected)
        )
        return Response(
            200,
            {
                "flavor": sizing.flavor_projection(flavor),
                "defaultFlavor": sizing.flavor_projection(default),
                "useDefault": selected is None,
            },
        )

    def _select_application_builder_size(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request, allowed={"flavor", "expectedFlavor"}, required={"flavor", "expectedFlavor"}
        )
        reference = None if body["flavor"] is None else flavor_reference(body["flavor"])
        expected = (
            None if body["expectedFlavor"] is None else flavor_reference(body["expectedFlavor"])
        )
        application_id = self._application(self._path_uuid(request)).application_id
        return self._external(
            request,
            lambda connection, key: BuilderSettingsService(
                connection, self.config, self.state_directory
            ).select(reference, expected, request_id=key, application_id=application_id),
            kind=APP_BUILDER_SIZE_KIND,
            scope=f"app-{application_id}",
        )

    def _resize_plan(self, request: Request) -> Response:
        if (
            request.body is not None
            or set(request.query) != {"flavor"}
            or len(request.query["flavor"]) != 1
        ):
            raise HttpError(
                400, "INVALID_QUERY", "supply exactly one flavor query field and no body"
            )
        application = self._application(self._path_uuid(request))
        return Response(
            200,
            sizing.plan(
                self.connection, self.config, application.application_id, request.query["flavor"][0]
            ),
        )

    def _resize(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request, allowed={"plan", "confirmation"}, required={"plan", "confirmation"}
        )
        if not isinstance(body["plan"], dict):
            raise ValidationError("resize plan must be an object")
        claimed = self._claim(request)
        if claimed.result_id is not None and not self._is_recovery_result(claimed):
            return self._operation_response(claimed.result_id, admin=True)
        application = self._application(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: DeploymentService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).resize(
                application.application_id,
                body["plan"],
                confirmation=body["confirmation"],
                request_id=key,
            ),
            kind="app.deploy",
            scope=f"app-{application.application_id}",
            claimed=claimed,
        )

    def _rollback_plan(self, request: Request) -> Response:
        if (
            request.body is not None
            or set(request.query) not in ({"deploymentId"}, {"deploymentId", "reuseWorker"})
            or len(request.query["deploymentId"]) != 1
            or request.query.get("reuseWorker", ("false",)) not in (("true",), ("false",))
        ):
            raise HttpError(
                400,
                "INVALID_QUERY",
                "supply deploymentId and optional boolean reuseWorker, with no body",
            )
        application = self._application(self._path_uuid(request))
        return Response(
            200,
            DeploymentService(
                self.connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).rollback_plan(
                application.application_id,
                request.query["deploymentId"][0],
                reuse_worker=request.query.get("reuseWorker") == ("true",),
            ),
        )

    def _rollback(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request, allowed={"plan", "confirmation"}, required={"plan", "confirmation"}
        )
        if not isinstance(body["plan"], dict):
            raise ValidationError("rollback plan must be an object")
        claimed = self._claim(request)
        if claimed.result_id is not None and not self._is_recovery_result(claimed):
            return self._operation_response(claimed.result_id, admin=True)
        application = self._application(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: DeploymentService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).rollback(
                application.application_id,
                body["plan"],
                confirmation=body["confirmation"],
                request_id=key,
            ),
            kind="app.deploy",
            scope=f"app-{application.application_id}",
            claimed=claimed,
        )

    def _capabilities(self, request: Request) -> Response:
        self._no_query(request)
        if request.body is not None:
            raise HttpError(400, "INVALID_BODY", "read routes do not accept a body")
        return Response(
            200,
            {
                "apiVersion": API_VERSION,
                "features": ["maintenance-after-build-v1", "reuse-worker-v1"],
            },
        )

    def _operator_deployment(self, request: Request) -> Response:
        self._no_query(request)
        fields = {
            "repository",
            "commit",
            "requestedRef",
            "configurationRevision",
            "configuration",
        }
        body = self._body(
            request, allowed=fields | {"plan", "maintenance", "reuseWorker"}, required=fields
        )
        if (
            type(body.get("maintenance", False)) is not bool
            or type(body.get("reuseWorker", False)) is not bool
        ):
            raise ValidationError("maintenance and worker reuse consent must be boolean")
        if body.get("reuseWorker", False):
            if body.get("maintenance") is not True or "plan" in body:
                raise ValidationError(
                    "worker reuse requires maintenance consent and no sizing plan"
                )
        elif not isinstance(body.get("plan"), dict):
            raise ValidationError("replacement requires a sizing plan object")
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
                    body.get("plan"),
                    maintenance=body.get("maintenance", False),
                    reuse_worker=body.get("reuseWorker", False),
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

    def _source_key(self, application: db.Application, mode: str) -> Response:
        """The app's deploy key, as its public half only; the helper keeps the rest."""
        result = self.helper_caller(
            self.config,
            "app.source.key",
            {"slug": application.slug, "mode": mode},
            deadline=operation_deadline(self.config),
        )
        present = result.get("present")
        public = result.get("publicKey")
        if not isinstance(present, bool) or (
            present
            and (
                not isinstance(public, str)
                or not public.startswith("ssh-ed25519 ")
                or len(public) > 256
                or not str(result.get("fingerprint", "")).startswith("SHA256:")
                or not isinstance(result.get("createdAt"), str)
            )
        ):
            raise app.ApplicationError("helper returned an invalid deploy key")
        return Response(
            200,
            {
                "applicationId": application.application_id,
                "present": present,
                **(
                    {
                        "publicKey": public,
                        "fingerprint": result["fingerprint"],
                        "createdAt": result["createdAt"],
                    }
                    if present
                    else {}
                ),
            },
        )

    def _get_source_key(self, request: Request) -> Response:
        self._no_query(request)
        return self._source_key(self._application_snapshot(self._path_uuid(request)), "read")

    def _create_source_key(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"replace"}, allow_absent=True)
        if not isinstance(body.get("replace", False), bool):
            raise HttpError(400, "INVALID_BODY", "replace must be boolean")
        mode = "replace" if body.get("replace") else "create"
        return self._source_key(self._application(self._path_uuid(request)), mode)

    def _delete_source_key(self, request: Request) -> Response:
        """Remove the app's deploy key; an app without one already has the result."""
        self._no_query(request)
        self._body(request, allowed=set(), allow_absent=True)
        return self._source_key(self._application(self._path_uuid(request)), "delete")

    def _source_commits(self, request: Request) -> Response:
        return self._source_read(request, preflight=False)

    def _source_preflight(self, request: Request) -> Response:
        return self._source_read(request, preflight=True)

    def _source_read(self, request: Request, *, preflight: bool) -> Response:
        self._no_query(request)
        application = self._application_snapshot(self._path_uuid(request))
        fields = (
            {"repository", "commit", "configuration"} if preflight else {"repository", "branch"}
        )
        body = self._body(request, allowed=fields, required=fields)
        values: dict[str, object] = {
            "slug": application.slug,
            "repository": repository_url(body["repository"]),
        }
        if preflight:
            values.update(
                commit=commit(body["commit"]),
                configuration=parse_configuration(body["configuration"]).canonical_json(),
            )
        else:
            values["branch"] = branch_name(body["branch"])
        try:
            result = self.helper_caller(
                self.config,
                "app.source.preflight" if preflight else "app.source.commits",
                values,
                # The helper's git reads stop at 25 s; the broker waits 30 s.
                deadline=min(operation_deadline(self.config), time.monotonic() + 27),
            )
        except (remote.HelperError, ServiceDeadlineError):
            raise HttpError(
                503,
                "SOURCE_UNAVAILABLE",
                "Repository information is unavailable. Try again later.",
                retryable=True,
            ) from None
        if result.get("keyPresent") is False:
            return Response(200, {"applicationId": application.application_id, "keyPresent": False})
        items = result.get("items")
        if result.get("keyPresent") is not True or not isinstance(items, list):
            raise app.ApplicationError("helper returned invalid source evidence")
        expected: set[str] = set()
        if preflight:
            configuration = parse_configuration(body["configuration"])
            expected = {
                "package-json",
                "script:" + configuration.start_script,
                *_RUNTIME_CHECKS,
                *("lockfile:" + package for package in configuration.packages),
            }
            if configuration.build_script is not None:
                expected.add("script:" + configuration.build_script)
        if len(items) > (len(expected) if preflight else 5):
            raise app.ApplicationError("helper source evidence exceeds its bounds")
        projected: list[dict[str, object]] = []
        for item in items:
            if not isinstance(item, dict):
                raise app.ApplicationError("helper source item is invalid")
            if preflight:
                if item.get("id") not in expected or item.get("state") not in {
                    "ok",
                    "problem",
                    "unknown",
                }:
                    raise app.ApplicationError("helper checkout check is invalid")
                projected.append(
                    {
                        key: bounded_text(item[key], field="checkout check", maximum=512)
                        for key in (
                            "id",
                            "label",
                            "state",
                            *(["problem"] if item.get("state") == "problem" else []),
                        )
                    }
                )
            else:
                projected.append(
                    {
                        "sha": commit(item.get("sha")),
                        "message": bounded_text(
                            item.get("message"), field="commit message", maximum=200
                        ),
                        "author": None
                        if item.get("author") is None
                        else bounded_text(item["author"], field="commit author", maximum=100),
                        "date": bounded_text(item.get("date"), field="commit date", maximum=40),
                    }
                )
        # An older helper reports no runtime check; a newer one reports one.
        if preflight and (
            {item["id"] for item in projected} | _RUNTIME_CHECKS != expected
            or sum(item["id"] in _RUNTIME_CHECKS for item in projected) > 1
        ):
            raise app.ApplicationError("helper checkout checks are incomplete")
        return Response(
            200,
            {"applicationId": application.application_id, "keyPresent": True, "items": projected},
        )

    def _check_source_key(self, request: Request) -> Response:
        self._no_query(request)
        application = self._application_snapshot(self._path_uuid(request))
        body = self._body(
            request, allowed={"repository", "branch"}, required={"repository", "branch"}
        )
        result = self.helper_caller(
            self.config,
            "app.source.check",
            {
                "slug": application.slug,
                "repository": repository_url(body["repository"]),
                "branch": branch_name(body["branch"]),
            },
            deadline=operation_deadline(self.config),
        )
        if result.get("keyPresent") is False:
            return Response(200, {"applicationId": application.application_id, "keyPresent": False})
        head = result.get("head")
        problem = result.get("problem")
        if (
            result.get("keyPresent") is not True
            or not isinstance(result.get("reachable"), bool)
            or not (head is None or (isinstance(head, str) and len(head) == 40))
            or problem not in {None, "key-refused", "not-found", "branch-missing", "unavailable"}
        ):
            raise app.ApplicationError("helper returned invalid repository access evidence")
        return Response(
            200,
            {
                "applicationId": application.application_id,
                "keyPresent": True,
                "reachable": result["reachable"],
                "head": head,
                "problem": problem,
            },
        )

    def _startup_log(self, request: Request) -> Response:
        self._no_query(request)
        attempt = self._attempt(self._path_uuid(request))
        record = self.logs.startup(attempt.application_id, attempt.deployment_id)
        return Response(
            200,
            {
                "deploymentId": attempt.deployment_id,
                "captured": record is not None,
                **({"startup": record} if record is not None else {}),
            },
        )

    def _runtime_log(self, request: Request) -> Response:
        application = self._application_snapshot(self._path_uuid(request))
        lines, _offset = self._log_query(request, allow_offset=False, allow_stream=True)
        stream = self._single_query(request, "stream") or "stdout"
        chunk = self.logs.runtime_for(application, lines=lines, stderr=stream == "stderr")
        return Response(
            200,
            {
                "applicationId": application.application_id,
                "stream": stream,
                "text": chunk.text,
                "state": chunk.state,
                "nextOffset": chunk.next_offset,
                "truncated": chunk.truncated,
            },
        )

    def _get_environment(self, request: Request) -> Response:
        self._no_query(request)
        with self._snapshot() as connection:
            application = self._application(self._path_uuid(request), connection)
            revision = db.get_environment_revision(connection, application.application_id)
            if revision is None:
                raise db.DatabaseError("application environment revision is missing")
            keys = db.list_environment_keys(connection, application_id=application.application_id)
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
        claimed = self._claim(request)
        if claimed.result_id is None and resource_type != "s3":
            from .storage_capacity import check

            check(
                self.connection,
                None,
                {
                    "memoryBytes": 536870912,
                    "connections": self.config.policy.standard.postgres_connections
                    if resource_type == "postgres"
                    else 10,
                },
            )
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
            claimed=claimed,
            kind="storage.create",
            scope=f"app-{application.application_id}",
        )

    def _list_storage(self, request: Request) -> Response:
        with self._snapshot() as connection:
            application = self._application(self._path_uuid(request), connection)
            resources = db.list_managed_resources(
                connection, application_id=application.application_id
            )
        items = [self._storage_model(item) for item in resources]
        return Response(200, self._page(request, items, "resourceId"))

    def _get_storage(self, request: Request) -> Response:
        self._no_query(request)
        with self._snapshot() as connection:
            resource = self._resource(self._path_uuid(request), connection)
        return Response(200, self._storage_model(resource))

    def _storage_limits(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request, allowed={"quotas", "expectedQuotas"}, required={"quotas", "expectedQuotas"}
        )
        resource = self._resource(self._path_uuid(request))
        target = validate_quotas(resource.resource_type, body["quotas"])
        expected = validate_expected_quotas(resource.resource_type, body["expectedQuotas"])
        claimed = self._claim(request)
        if claimed.result_id is None:
            from .storage_capacity import check

            check(self.connection, resource.resource_id, target)
            storage_limits.validate_reduction(resource, target)
            accepted = storage_limits.quotas(resource)
            if (
                resource.resource_type != "s3"
                and resource.instance_id is None
                and (
                    target["connections"] > accepted["connections"]
                    or resource.resource_type == "mongo"
                    and target["connections"] != accepted["connections"]
                )
            ):
                raise HttpError(
                    409,
                    "INSTANCE_MIGRATION_REQUIRED",
                    "migrate this resource before increasing shared connection caps",
                )
        return self._external(
            request,
            lambda connection, key: StorageLimitsService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).select(resource.resource_id, target, expected, request_id=key),
            claimed=claimed,
            kind=storage_limits.LIMITS_KIND,
            scope=f"app-{resource.application_id}",
        )

    def _migrate_instances(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"applicationIds"}, allow_absent=True)
        selected = body.get("applicationIds")
        if selected is not None:
            if not isinstance(selected, list) or not 1 <= len(selected) <= 50:
                raise HttpError(400, "INVALID_REQUEST", "applicationIds must contain 1..50 UUIDs")
            selected = tuple(sorted({uuid(value, field="application ID") for value in selected}))
            for identifier in selected:
                self._application(identifier)
        return self._external(
            request,
            lambda connection, key: InstanceMigrationService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).migrate(request_id=key, application_ids=selected),
            kind=MIGRATE_KIND,
            scope="infrastructure",
        )

    def _abort_instances(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(request, allowed={"applicationIds"}, allow_absent=True)
        selected = body.get("applicationIds")
        if selected is not None:
            if not isinstance(selected, list) or not 1 <= len(selected) <= 50:
                raise HttpError(400, "INVALID_REQUEST", "applicationIds must contain 1..50 UUIDs")
            selected = tuple(sorted({uuid(value, field="application ID") for value in selected}))
        return self._external(
            request,
            lambda connection, key: InstanceMigrationService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).abort(request_id=key, application_ids=selected),
            kind="storage.instances.abort",
            scope="storage-abort",
        )

    def _repair_postgres(self, request: Request) -> Response:
        self._no_query(request)
        self._body(request, allowed=set(), allow_absent=True)
        return self._external(
            request,
            lambda connection, key: StorageLimitsService(
                connection, self.config, self.state_directory, helper_caller=self.helper_caller
            ).repair_postgres(request_id=key),
            kind=storage_limits.REPAIR_KIND,
            scope="infrastructure",
        )

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
            kind=f"storage.{action}",
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
        if claimed.result_id is not None and not self._is_recovery_result(claimed):
            return self._operation_response(claimed.result_id, admin=False)
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
            kind="storage.remove",
            scope=f"app-{resource.application_id}",
            claimed=claimed,
            admin=False,
        )

    def _get_operation(self, request: Request) -> Response:
        self._no_query(request)
        identifier = self._path_uuid(request)
        # One short read snapshot keeps the domain/dispatch fallback coherent
        # without holding the API lock throughout unrelated external reads.
        with self._snapshot() as connection:
            operation = db.get_operation(connection, identifier)
            dispatch = db.get_operation_dispatch(connection, identifier)
        if operation is not None:
            return Response(200, self._operation_model(operation, dispatch))
        if dispatch is None:
            raise HttpError(404, "OPERATION_NOT_FOUND", "operation does not exist")
        return Response(200, self._dispatch_model(dispatch))

    def _get_fixed_ip(self, request: Request) -> Response:
        from .fixed_ip_service import FixedIPService

        self._no_query(request)
        if request.body is not None:
            raise HttpError(400, "INVALID_BODY", "read routes do not accept a body")
        application = self._application(self._path_uuid(request))
        return Response(
            200,
            FixedIPService(self.connection, self.config, self.state_directory).read(
                application.application_id
            ),
        )

    def _plan_fixed_ip(self, request: Request) -> Response:
        from .fixed_ip_service import FixedIPService

        self._no_query(request)
        fields = {"networkId", "subnetId", "address"}
        body = self._body(request, allowed=fields, required=fields)
        application = self._application(self._path_uuid(request))
        return Response(
            200,
            FixedIPService(self.connection, self.config, self.state_directory).plan(
                application.application_id, body["networkId"], body["subnetId"], body["address"]
            ),
        )

    def _mutate_fixed_ip(self, request: Request) -> Response:
        from ..fixed_ip import ipv4
        from .fixed_ip_service import FixedIPService

        self._no_query(request)
        body = self._body(
            request, allowed={"action", "networkId", "subnetId", "address"}, required={"action"}
        )
        if body["action"] == "reserve":
            expected = {"action", "networkId", "subnetId", "address"}
            uuid(body.get("networkId"), field="worker network UUID")
            uuid(body.get("subnetId"), field="worker subnet UUID")
            ipv4(body.get("address"))
        elif body["action"] == "release":
            expected = {"action"}
        else:
            raise ValidationError("invalid retained fixed IP action")
        if body.keys() != expected:
            raise ValidationError("retained fixed IP action fields are invalid")
        application = self._application(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: FixedIPService(
                connection, self.config, self.state_directory
            ).mutate(
                application.application_id,
                action=body["action"],
                network_id=body.get("networkId"),
                subnet_id=body.get("subnetId"),
                address=body.get("address"),
                request_id=key,
            ),
            kind="app.fixed-ip",
            scope=f"app-{application.application_id}",
        )

    def _get_public_ip(self, request: Request) -> Response:
        from .public_ip_service import model

        self._no_query(request)
        application = self._application(self._path_uuid(request))
        return Response(200, model(self.connection, application.application_id))

    def _plan_public_ip(self, request: Request) -> Response:
        from .public_ip_service import PublicIPService

        self._no_query(request)
        body = self._body(request, allowed={"externalNetworkId"}, required={"externalNetworkId"})
        application = self._application(self._path_uuid(request))
        result = PublicIPService(self.connection, self.config, self.state_directory).plan(
            application.application_id,
            uuid(body["externalNetworkId"], field="external network UUID"),
        )
        return Response(200, result)

    def _mutate_public_ip(self, request: Request) -> Response:
        from .public_ip_service import PublicIPService

        self._no_query(request)
        body = self._body(
            request, allowed={"action", "externalNetworkId", "floatingIpId"}, required={"action"}
        )
        action = body["action"]
        if action not in ("allocate", "attach", "release", "reconcile"):
            raise ValidationError("invalid public IP action")
        expected = {"action"}
        if action in {"allocate", "attach"}:
            expected.add("externalNetworkId")
            uuid(body.get("externalNetworkId"), field="external network UUID")
        if action == "attach":
            expected.add("floatingIpId")
            uuid(body.get("floatingIpId"), field="floating IP UUID")
        if set(body) != expected:
            raise ValidationError("public IP action fields are invalid")
        application = self._application(self._path_uuid(request))
        return self._external(
            request,
            lambda connection, key: PublicIPService(
                connection, self.config, self.state_directory
            ).mutate(
                application.application_id,
                action=action,
                network_id=body.get("externalNetworkId"),
                floating_ip_id=body.get("floatingIpId"),
                request_id=key,
            ),
            kind="app.public-ip",
            scope=f"app-{application.application_id}",
        )

    def _admin_status(self, request: Request) -> Response:
        self._no_query(request)
        observers = status.live_observers(
            self.connection,
            self.config,
            helper_caller=self.observer_helper,
        )
        return Response(
            200,
            {
                **status.status_show(
                    self.connection,
                    observe_infrastructure=observers.infrastructure,
                    observe_application=observers.application,
                    observe_storage=status.cached_storage_observer(self.connection),
                ),
                "storageHost": storage_limits.host_model(self.connection),
            },
        )

    def _admin_hosts(self, request: Request) -> Response:
        self._no_query(request)
        observe = status.infrastructure_observer(
            self.config.platform,
            self.connection,
            timeout_seconds=self.config.policy.limits.process_seconds,
        )
        return Response(200, {"items": status.infra_list(self.connection, observe=observe)})

    def _select_hosted_image(self, request: Request) -> Response:
        self._no_query(request)
        body = self._body(
            request, allowed={"imageId", "expectedImageId"}, required={"imageId", "expectedImageId"}
        )
        role = hosted_role(request.path_parameters["role"])
        image_id = uuid(body["imageId"], field="image UUID")
        expected_image_id = uuid(body["expectedImageId"], field="expected current image UUID")
        return self._external(
            request,
            lambda connection, key: ImageSelectionService(
                connection, self.config, self.state_directory
            ).select(role, image_id, expected_image_id, request_id=key),
            kind=IMAGE_SELECTION_KIND,
            scope="infrastructure",
        )

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
            "application.worker_flavor, application.scheduler_cpu_mhz, application.scheduler_memory_mib, "
            "application.updated_at, tombstone.deleted_at, accepted.deployment_id AS active_deployment_id "
            "FROM applications AS application LEFT JOIN application_slug_tombstones "
            "AS tombstone USING (slug) LEFT JOIN active_deployments AS accepted "
            "ON accepted.application_id = application.application_id "
            "ORDER BY application.slug, application.application_id"
        ).fetchall()
        items = [
            {
                "applicationId": row["application_id"],
                "slug": row["slug"],
                "enabled": bool(row["desired_running"]),
                "activeDeploymentId": row["active_deployment_id"],
                "sizing": {
                    "workerFlavor": row["worker_flavor"],
                    "cpuMHz": row["scheduler_cpu_mhz"],
                    "memoryMiB": row["scheduler_memory_mib"],
                },
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
        items = [
            self._operation_model(
                item, db.get_operation_dispatch(self.connection, item.operation_id)
            )
            for item in operations
        ]
        items.extend(self._dispatch_model(item) for item in queued)
        items.sort(
            key=lambda item: (str(item["updatedAt"]), str(item["operationId"])), reverse=True
        )
        return Response(
            200,
            self._page(request, items, "operationId"),
        )

    def _application_model(self, application: db.Application) -> dict[str, object]:
        active = db.get_active_deployment(self.connection, application.application_id)
        return {
            "applicationId": application.application_id,
            "slug": application.slug,
            "url": application.url,
            "enabled": application.desired_running,
            "activeDeploymentId": None if active is None else active.deployment_id,
            "sizing": {
                "workerFlavor": application.worker_flavor,
                "cpuMHz": application.scheduler_cpu_mhz,
                "memoryMiB": application.scheduler_memory_mib,
            },
            "createdAt": application.created_at,
            "updatedAt": application.updated_at,
        }

    def _deployment_model(self, attempt: db.DeploymentAttempt) -> dict[str, object]:
        return {
            "deploymentId": attempt.deployment_id,
            "applicationId": attempt.application_id,
            "status": attempt.status,
            "snapshotKind": attempt.snapshot_kind,
            "repositoryCommit": attempt.source_commit,
            "requestedRef": attempt.requested_ref,
            "configurationRevision": attempt.configuration_revision,
            "configuration": configuration_snapshot(attempt),
            "configurationSha256": attempt.configuration_sha256,
            "sourceRepository": source_repository(self.connection, attempt),
            "environmentRevision": attempt.environment_revision,
            "recipeHash": attempt.recipe_hash,
            "imageDigest": attempt.image_digest,
            "runtime": db.deployment_runtime(self.connection, attempt.deployment_id),
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
        quotas = storage_limits.quotas(resource)
        result: dict[str, object] = {
            "resourceId": resource.resource_id,
            "applicationId": resource.application_id,
            "type": resource.resource_type,
            "name": resource.resource_name,
            "displayLabel": resource.display_label,
            "lifecycleState": resource.lifecycle_state,
            "quotas": quotas,
            "usage": storage_limits.usage_model(resource),
            "isolation": "instance" if resource.instance_id else "shared",
            "hardQuotaBytes": hard_quota(resource.measured_target_bytes or 0)
            if resource.instance_id
            else None,
            "writeBlock": storage_limits.block_model(resource),
            "lastVerifiedAt": resource.last_verified_at,
            "createdAt": resource.created_at,
            "updatedAt": resource.updated_at,
        }
        if admin:
            result["providerId"] = resource.provider_id
            result["providerName"] = resource.provider_name
            result["usageError"] = resource.usage_error
            result.update(
                instanceId=resource.instance_id,
                instancePort=resource.instance_port,
                migrationState=resource.migration_state,
            )
        return result

    @staticmethod
    def _operation_model(
        operation: db.Operation, dispatch: db.OperationDispatch | None = None
    ) -> dict[str, object]:
        # Admission of a retry precedes domain preflight/deadline renewal. Do
        # not present the previous attempt's error as a newly failed retry.
        retry_active = (
            operation.status == "recovery_required"
            and dispatch is not None
            and dispatch.status in {"pending", "running"}
        )
        retry = operation.refs.get("finishing_retry", {})
        return {
            "operationId": operation.operation_id,
            "kind": operation.kind,
            "scope": operation.scope,
            "status": "running" if retry_active else operation.status,
            "phase": operation.phase,
            "startedAt": operation.started_at,
            "updatedAt": operation.updated_at,
            "deadlineAt": operation.deadline_at,
            "safeError": None if retry_active else operation.safe_error,
            "errorCode": _failure_code(operation) if operation.status == "failed" else None,
            "cleanupState": operation.cleanup_state,
            "finishing": operation.finishing,
            "finishingRetryAttempts": retry.get("attempts", 0),
            "nextRetryAt": retry.get("next_at"),
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
            "errorCode": None,
            "cleanupState": "pending",
            "finishing": dispatch.finishing,
            "finishingRetryAttempts": 0,
            "nextRetryAt": None,
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

    def _log_query(
        self, request: Request, *, allow_offset: bool, allow_stream: bool = False
    ) -> tuple[int, int | None]:
        allowed = {"lines", "offset"} if allow_offset else {"lines"}
        if allow_stream:
            allowed.add("stream")
        if set(request.query) - allowed:
            raise HttpError(400, "INVALID_QUERY", "log query fields are invalid")
        if allow_stream and self._single_query(request, "stream") not in {
            None,
            "stdout",
            "stderr",
        }:
            raise HttpError(400, "INVALID_QUERY", "stream must be stdout or stderr")
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
