"""Single-flight evidence collection and the cached dashboard snapshot.

Browsers never trigger platform reads directly. They read the cached snapshot;
one background loop refreshes it at a fixed interval, and a manual request can
only wake that loop early, never start a second concurrent refresh.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from typing import Any

from ..config import PlatformConfig
from . import model, sources
from .model import Evidence, Observed
from .sources import AdminReads, OperatorReads, Probe, SourceError

HISTORY_LENGTH = 24
MAXIMUM_ROUTE_PROBES = 8
MINIMUM_MANUAL_REFRESH_SECONDS = 10.0
# A failed controller section keeps its last successful records instead of
# erasing them; these are the record fields each reader section replaces.
CARRIED_SECTIONS: Mapping[str, tuple[str, ...]] = {
    "capabilities": ("api_version", "features"),
    "images": ("images",),
    "applications": ("applications",),
    "deployments": ("deployments", "deployments_truncated"),
    "storage": ("storage",),
    "operations": ("operations", "operations_truncated"),
}


def _background[T](function: Callable[[], T], name: str) -> Future[T]:
    """Run one read on a daemon thread so shutdown never waits for a slow source."""
    future: Future[T] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(function())
        except BaseException as error:
            future.set_exception(error)

    threading.Thread(target=run, name=name, daemon=True).start()
    return future


def _failure(error: BaseException, label: str) -> str:
    if isinstance(error, SourceError):
        return str(error)
    return f"the {label} read failed unexpectedly ({error.__class__.__name__})"


class Collector:
    """Gather every source for one refresh and keep last-good observations."""

    def __init__(
        self,
        platform: PlatformConfig,
        *,
        read_admin: Callable[[], AdminReads],
        read_operator: Callable[[], OperatorReads],
        probe: Callable[..., Probe] = sources.probe,
        probe_timeout_seconds: float = 10,
        release: str | None = None,
        now: Callable[[], str] = sources.now_timestamp,
    ) -> None:
        self._platform = platform
        self._read_admin = read_admin
        self._read_operator = read_operator
        self._probe = probe
        self._probe_timeout = probe_timeout_seconds
        self._release = release
        self._now = now
        self._admin: Observed[AdminReads] = Observed()
        self._operator: Observed[OperatorReads] = Observed()
        self._history: dict[str, deque[tuple[str, str]]] = {}
        self._section_success: dict[str, str] = {}

    def _observe[T](self, previous: Observed[T], future: Future[T], label: str) -> Observed[T]:
        try:
            value = future.result()
        except Exception as error:
            # Keep the last accepted evidence visible, explicitly marked stale.
            return Observed(previous.value, False, previous.observed_at, _failure(error, label))
        return Observed(value, True, self._now(), None)

    def _carry_forward(
        self, previous: AdminReads | None, observed: Observed[AdminReads]
    ) -> Observed[AdminReads]:
        current = observed.value
        if not observed.ok or current is None or observed.observed_at is None:
            return observed
        updates: dict[str, Any] = {}
        for section, fields in CARRIED_SECTIONS.items():
            if section not in current.section_errors:
                self._section_success[section] = observed.observed_at
            elif previous is not None:
                updates.update({name: getattr(previous, name) for name in fields})
        if not updates:
            return observed
        return dataclasses.replace(observed, value=dataclasses.replace(current, **updates))

    def _route_targets(self, reads: AdminReads | None) -> dict[str, tuple[str, str]]:
        if reads is None:
            return {}
        deployments = {item.deployment_id: item for item in reads.deployments}
        targets: dict[str, tuple[str, str]] = {}
        for application in reads.applications:
            if (
                application.deleted_at is not None
                or not application.enabled
                or application.active_deployment_id is None
            ):
                continue
            deployment = deployments.get(application.active_deployment_id)
            if deployment is None or deployment.health_path is None:
                continue
            url = sources.route_url(self._platform, application.slug, deployment.health_path)
            targets[application.application_id] = (url, deployment.deployment_id)
        return targets

    def _probe_route(self, url: str) -> Probe:
        try:
            return self._probe(url, timeout_seconds=self._probe_timeout)
        except Exception:
            return Probe(url, self._now(), None, None, error="probe failed")

    def _probe_routes(self, targets: Mapping[str, tuple[str, str]]) -> dict[str, Probe]:
        pending = [(application_id, url) for application_id, (url, _) in targets.items()]
        results: dict[str, Probe] = {}
        lock = threading.Lock()

        def work() -> None:
            while True:
                with lock:
                    if not pending:
                        return
                    application_id, url = pending.pop()
                probe = self._probe_route(url)
                with lock:
                    results[application_id] = probe

        workers = [
            threading.Thread(target=work, name="dashboard-route", daemon=True)
            for _ in range(min(MAXIMUM_ROUTE_PROBES, len(pending)))
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        return results

    def _probe_ingress(self) -> tuple[Probe, ...]:
        domain = self._platform.domain
        return tuple(
            self._probe(url, timeout_seconds=self._probe_timeout, expect_body=b"OK")
            for url in (f"https://{domain}/healthz", f"https://wildcard-health.{domain}/healthz")
        )

    def _remember(
        self,
        routes: Mapping[str, Probe],
        targets: Mapping[str, tuple[str, str]],
        *,
        prune: bool,
    ) -> None:
        for application_id, probe in routes.items():
            _outcome, tone = model.route_outcome(probe, targets[application_id][1], application_id)
            history = self._history.setdefault(application_id, deque(maxlen=HISTORY_LENGTH))
            history.append((probe.checked_at, tone))
        if not prune:
            return
        for application_id in set(self._history) - set(targets):
            # A stopped or removed application has no route to remember.
            del self._history[application_id]

    def collect(self) -> dict[str, Any]:
        admin = _background(self._read_admin, "dashboard-admin")
        operator = _background(self._read_operator, "dashboard-operator")
        ingress = _background(self._probe_ingress, "dashboard-ingress")
        self._admin = self._carry_forward(
            self._admin.value, self._observe(self._admin, admin, "controller")
        )
        targets = self._route_targets(self._admin.value)
        routes = self._probe_routes(targets)
        self._operator = self._observe(self._operator, operator, "operator state")
        try:
            ingress_probes = ingress.result()
        except Exception:
            ingress_probes = ()
        reads = self._admin.value
        # Only a fresh application list may forget the history of an absent app.
        fresh = self._admin.ok and reads is not None and "applications" not in reads.section_errors
        self._remember(routes, targets, prune=fresh)
        return model.build_snapshot(
            Evidence(
                platform=self._platform,
                generated_at=self._now(),
                admin=self._admin,
                operator=self._operator,
                ingress=ingress_probes,
                routes=routes,
                history={key: tuple(value) for key, value in self._history.items()},
                release=self._release,
                admin_sections=dict(self._section_success),
            )
        )


def pending_snapshot(platform: PlatformConfig, release: str | None = None) -> dict[str, Any]:
    """The document served before the first refresh completes."""
    try:
        name = platform.get("displayName")
    except KeyError:
        name = None
    return {
        "schemaVersion": model.SCHEMA_VERSION,
        "state": "pending",
        "platform": {
            "name": name if isinstance(name, str) and name else platform.project_name,
            "domain": platform.domain,
            "region": platform.region,
            "namespace": platform.namespace,
            "release": release,
        },
    }


class DashboardService:
    """Own the refresh loop and serve the latest snapshot as JSON bytes."""

    def __init__(
        self,
        collect: Callable[[], dict[str, Any]],
        initial: dict[str, Any],
        *,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = sources.now_timestamp,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("dashboard refresh interval must be positive")
        self._collect = collect
        self._interval = interval_seconds
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._snapshot = initial
        self._refreshing = False
        self._started_at: str | None = None
        self._completed_at: str | None = None
        self._duration_ms: int | None = None
        self._error: str | None = None
        self._last_start: float | None = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("dashboard refresh loop already started")
        self._thread = threading.Thread(target=self._loop, name="dashboard-refresh", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 1) -> None:
        # The loop and its reads run on daemon threads; a slow source must not
        # hold shutdown, so wait only briefly for a refresh that is finishing.
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout_seconds)

    def _loop(self) -> None:
        while not self._stopping.is_set():
            self.refresh()
            self._wake.wait(self._interval)
            self._wake.clear()

    def refresh(self) -> None:
        """Run one collection unless another is already in progress."""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
            self._last_start = self._clock()
            self._started_at = self._now()
        started = self._clock()
        snapshot: dict[str, Any] | None = None
        error: str | None = None
        try:
            snapshot = self._collect()
        except Exception as failure:
            error = f"The last refresh failed unexpectedly ({failure.__class__.__name__})"
        with self._lock:
            if snapshot is not None:
                self._snapshot = snapshot
            self._error = error
            self._duration_ms = round((self._clock() - started) * 1000)
            self._completed_at = self._now()
            self._refreshing = False

    def request_refresh(self) -> bool:
        """Wake the loop early; refuse while busy or within the manual floor."""
        with self._lock:
            if self._refreshing or (
                self._last_start is not None
                and self._clock() - self._last_start < MINIMUM_MANUAL_REFRESH_SECONDS
            ):
                return False
        self._wake.set()
        return True

    def document(self) -> bytes:
        with self._lock:
            payload = dict(self._snapshot)
            payload["refresh"] = {
                "intervalSeconds": self._interval,
                "inProgress": self._refreshing,
                "startedAt": self._started_at,
                "completedAt": self._completed_at,
                "durationMs": self._duration_ms,
                "error": self._error,
            }
        return json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
