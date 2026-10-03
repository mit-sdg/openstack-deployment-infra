"""Fixture-backed dashboard preview for interface development and review.

``python -m openstack_platform.dashboard.preview`` serves synthetic evidence on
loopback TCP. It reads only the sanitized example inventory, never contacts
OpenStack or the admin SSH bridge, and passes raw fixture payloads through the
production parsers and projection so the page renders exactly as it would
from live evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid as uuid_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import PlatformConfig, load_platform
from ..contracts import IMAGE_ROLES
from . import sources
from .server import LoopbackDashboardServer, load_assets, serve
from .service import Collector, DashboardService, pending_snapshot
from .sources import AdminReads, OperatorReads, Probe, SourceError

EXAMPLE_INVENTORY = Path(__file__).resolve().parents[2] / "config" / "platform.example.json"
SCENARIOS = ("mixed", "healthy", "outage", "unreachable", "empty")
_IDENTITY = uuid_module.UUID("3b0e6a52-2f5f-4d8e-9a8c-6f1e0b7c9d21")


@dataclass(frozen=True, slots=True)
class FixtureApp:
    slug: str
    runtime: str
    flavor: str
    cpu_mhz: int
    memory_mib: int
    storage: tuple[str, ...]
    deployed_hours: float
    ref: str = "main"
    latency_ms: int = 120


APPS = (
    FixtureApp("course-planner", "bun", "4200", 2000, 2048, ("postgres",), 30, latency_ms=96),
    FixtureApp("lab-inventory", "node", "4100", 1000, 1024, ("postgres", "s3"), 75),
    FixtureApp("grading-service", "node", "4200", 2000, 4096, ("mongo",), 220, latency_ms=184),
    FixtureApp("seminar-signup", "bun", "4100", 1000, 1024, (), 9, latency_ms=64),
    FixtureApp("data-explorer", "node", "4300", 4000, 8192, ("postgres", "s3"), 410),
    FixtureApp("survey-collector", "bun", "4100", 1000, 1024, ("mongo",), 140, latency_ms=88),
    FixtureApp("office-hours", "node", "4100", 1000, 1024, ("postgres",), 52, "release"),
    FixtureApp("thesis-tracker", "bun", "4100", 1000, 2048, ("postgres",), 600, latency_ms=143),
    FixtureApp("alumni-directory", "node", "4100", 1000, 1024, (), 1300),
)


def _id(*parts: str) -> str:
    return str(uuid_module.uuid5(_IDENTITY, "/".join(parts)))


def _commit(*parts: str) -> str:
    return hashlib.sha1("/".join(parts).encode()).hexdigest()


def _at(now: datetime, hours: float = 0.0, minutes: float = 0.0) -> str:
    moment = now - timedelta(hours=hours, minutes=minutes)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class Fixture:
    """Synthetic raw evidence for one named scenario."""

    def __init__(self, platform: PlatformConfig, scenario: str) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown preview scenario: {scenario}")
        self.platform = platform
        self.scenario = scenario
        self.started = datetime.now(UTC)
        self.reads = 0

    @property
    def apps(self) -> Sequence[FixtureApp]:
        return () if self.scenario == "empty" else APPS

    def _behaviour(self, slug: str) -> str:
        if self.scenario == "healthy":
            return "stopped" if slug == "alumni-directory" else "serving"
        if self.scenario == "outage":
            return "stopped" if slug == "alumni-directory" else "gateway"
        return {
            "grading-service": "failing",
            "data-explorer": "deploying",
            "alumni-directory": "stopped",
            "survey-collector": "recovery",
            "thesis-tracker": "failed-update",
        }.get(slug, "serving")

    def _image(self, role: str, now: datetime) -> dict[str, Any]:
        return {
            "role": role,
            "imageId": _id("image", role),
            "displayName": str(self.platform.get(f"images.{role}")),
            "sourceCommit": _commit("platform", "386bac7"),
            "compatibilityHash": hashlib.sha256(role.encode()).hexdigest(),
            "selectedAt": _at(now, hours=26 if role in {"worker", "builder"} else 98),
        }

    def admin_payload(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        applications: list[dict[str, Any]] = []
        deployments: list[dict[str, Any]] = []
        storage: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = []
        for index, app in enumerate(self.apps):
            behaviour = self._behaviour(app.slug)
            application_id = _id("app", app.slug)
            accepted_id = _id("deployment", app.slug, "accepted")
            enabled = behaviour != "stopped"
            applications.append(
                {
                    "applicationId": application_id,
                    "slug": app.slug,
                    "enabled": enabled,
                    "activeDeploymentId": accepted_id,
                    "sizing": {
                        "workerFlavor": app.flavor,
                        "cpuMHz": app.cpu_mhz,
                        "memoryMiB": app.memory_mib,
                    },
                    "url": f"https://{app.slug}.{self.platform.domain}",
                    "createdAt": _at(now, hours=app.deployed_hours + 400 + index * 37),
                    "updatedAt": _at(now, hours=app.deployed_hours),
                    "deletedAt": None,
                }
            )
            history = [("accepted", app.deployed_hours, "succeeded")]
            history.append(("previous", app.deployed_hours + 96 + index * 11, "succeeded"))
            if behaviour == "failed-update":
                history.insert(0, ("failed", 1.4, "failed"))
            if behaviour == "deploying":
                history.insert(0, ("candidate", 0.05, "building"))
            for name, hours, status in history:
                deployment_id = (
                    accepted_id if name == "accepted" else _id("deployment", app.slug, name)
                )
                deployments.append(
                    {
                        "deploymentId": deployment_id,
                        "applicationId": application_id,
                        "status": status,
                        "snapshotKind": "strict",
                        "repositoryCommit": _commit(app.slug, name),
                        "requestedRef": app.ref,
                        "configurationRevision": 3 if name != "previous" else 2,
                        "configuration": {
                            "schemaVersion": 1,
                            "build": {
                                "runtime": app.runtime,
                                "packages": ["."],
                                "buildScript": "build",
                                "startScript": "start",
                            },
                            "runtime": {"port": 3000, "healthPath": "/health"},
                            "storageBindings": [],
                        },
                        "configurationSha256": hashlib.sha256(name.encode()).hexdigest(),
                        "sourceRepository": f"https://github.com/example-org/{app.slug}",
                        "environmentRevision": 4,
                        "recipeHash": hashlib.sha256(app.slug.encode()).hexdigest(),
                        "imageDigest": (
                            f"registry.internal/apps/{app.slug}@sha256:"
                            + hashlib.sha256((app.slug + name).encode()).hexdigest()
                        ),
                        "nomadVersion": 7,
                        "safeError": (
                            "candidate health check failed: /health returned HTTP 500"
                            if status == "failed"
                            else None
                        ),
                        "cleanupState": "confirmed" if status in {"succeeded", "failed"} else None,
                        "requestedAt": _at(now, hours=hours + 0.2),
                        "updatedAt": _at(now, hours=hours),
                        "acceptedAt": _at(now, hours=hours) if status == "succeeded" else None,
                        "lastHealthyAt": _at(now, minutes=4) if name == "accepted" else None,
                    }
                )
            for kind in app.storage:
                quotas: dict[str, int] = {}
                if kind == "postgres":
                    quotas = {"postgresConnections": 10, "measuredTargetBytes": 2_147_483_648}
                elif kind == "mongo":
                    quotas = {"measuredTargetBytes": 2_147_483_648}
                else:
                    quotas = {"s3Bytes": 5_368_709_120, "s3Objects": 100_000}
                state = "recovery_required" if behaviour == "recovery" else "active"
                storage.append(
                    {
                        "resourceId": _id("storage", app.slug, kind),
                        "applicationId": application_id,
                        "type": kind,
                        "name": "default",
                        "displayLabel": None,
                        "lifecycleState": state,
                        "quotas": quotas,
                        "lastVerifiedAt": _at(now, hours=20),
                        "createdAt": _at(now, hours=800),
                        "updatedAt": _at(now, hours=20),
                        "providerId": None,
                        "providerName": None,
                    }
                )
            if behaviour == "deploying":
                operations.append(
                    self._operation(
                        _id("deployment", app.slug, "candidate"),
                        "app.deploy",
                        application_id,
                        "running",
                        "builder_creating",
                        now,
                        3,
                    )
                )
            if behaviour == "recovery":
                operations.append(
                    self._operation(
                        _id("operation", app.slug, "rotate"),
                        "storage.rotate",
                        application_id,
                        "recovery_required",
                        "credentials_rotating",
                        now,
                        34,
                        "MongoDB credential rotation did not confirm the new generation",
                    )
                )
            operations.append(
                self._operation(
                    accepted_id,
                    "app.deploy",
                    application_id,
                    "succeeded",
                    "accepted",
                    now,
                    app.deployed_hours * 60,
                )
            )
            if behaviour == "failed-update":
                operations.append(
                    self._operation(
                        _id("deployment", app.slug, "failed"),
                        "app.deploy",
                        application_id,
                        "failed",
                        "candidate_removed",
                        now,
                        84,
                        "candidate health check failed: /health returned HTTP 500",
                    )
                )
        if self.apps:
            operations.append(
                {
                    "operationId": _id("operation", "image", "worker"),
                    "kind": "infra.image.set",
                    "scope": "infrastructure",
                    "status": "succeeded",
                    "phase": "selected",
                    "startedAt": _at(now, hours=26.1),
                    "updatedAt": _at(now, hours=26),
                    "deadlineAt": None,
                    "safeError": None,
                    "cleanupState": "not_required",
                }
            )
        return {
            "version": sources.READER_VERSION,
            "capabilities": {
                "status": 200,
                "body": {
                    "apiVersion": 1,
                    "features": ["maintenance-after-build-v1", "reuse-worker-v1"],
                },
            },
            "images": {
                "status": 200,
                "body": {"items": [self._image(role, now) for role in IMAGE_ROLES]},
            },
            "applications": self._page(applications),
            "deployments": self._page(deployments),
            "storage": self._page(storage),
            "operations": self._page(operations),
            "units": {"states": {"controller": "active", "readiness": "active"}},
            "health": self._health(now),
        }

    @staticmethod
    def _page(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return {"status": 200, "body": {"items": list(items), "truncated": False}}

    @staticmethod
    def _operation(
        operation_id: str,
        kind: str,
        application_id: str,
        status: str,
        phase: str,
        now: datetime,
        minutes_ago: float,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "operationId": operation_id,
            "kind": kind,
            "scope": f"app-{application_id}",
            "status": status,
            "phase": phase,
            "startedAt": _at(now, minutes=minutes_ago + 6),
            "updatedAt": _at(now, minutes=minutes_ago),
            "deadlineAt": _at(now, minutes=-30) if status == "running" else None,
            "safeError": error,
            "cleanupState": "confirmed" if status != "running" else "pending",
        }

    def _health(self, now: datetime) -> dict[str, Any]:
        if self.scenario == "empty":
            return {"error": "Missing"}
        workers = sum(1 for app in self.apps if self._behaviour(app.slug) != "stopped")
        checks: dict[str, Any] = {"openstack": {"core_active": 3, "workers_active": workers}}
        error = None
        if self.scenario == "outage":
            error = (
                "RuntimeError: public ingress health response is unexpected for "
                f"wildcard-health.{self.platform.domain}"
            )
        else:
            checks.update(
                {
                    "public_ingress": "healthy",
                    "managed_services": "healthy",
                    "nomad": {"ready_clients": workers, "raft": "healthy"},
                    "backup": {"age_hours": 5.2, "encrypted": True, "registry_artifacts": True},
                    "offsite_recovery": {
                        "configured": True,
                        "mounted": True,
                        "verified": True,
                        "ageHours": 5.6,
                    },
                }
            )
        checked = now - timedelta(minutes=2, seconds=13)
        return {
            "body": {
                "checked_at": checked.isoformat(),
                "healthy": error is None,
                "checks": checks,
                "error": error,
            }
        }

    def read_admin(self) -> AdminReads:
        self.reads += 1
        if self.scenario == "unreachable" and self.reads > 1:
            raise SourceError("the pinned SSH bridge could not reach the admin host")
        return sources.parse_admin_reads(json.dumps(self.admin_payload()).encode())

    def read_operator(self) -> OperatorReads:
        now = datetime.now(UTC)
        roles = []
        for role in IMAGE_ROLES:
            persistent = role in {"admin", "ingress", "storage"}
            roles.append(
                {
                    "role": role,
                    "accepted": True,
                    "image": {
                        "displayName": str(self.platform.get(f"images.{role}")),
                        "sourceCommit": _commit("platform", "386bac7"),
                        "selectedAt": _at(now, hours=98),
                    },
                    "live": {
                        "available": persistent,
                        "state": "active" if persistent else "unknown",
                        "health": "unknown",
                        "checkedAt": _at(now),
                    },
                }
            )
        return sources.parse_operator_reads(roles, [])

    def probe(self, url: str, *, timeout_seconds: float, expect_body: bytes | None = None) -> Probe:
        del timeout_seconds
        checked = sources.now_timestamp()
        wobble = int(time.time() / 15) % 7 * 9
        if expect_body is not None:
            failing = self.scenario == "outage" and url.startswith("https://wildcard-health.")
            return Probe(url, checked, 502 if failing else 200, 41 + wobble, None, not failing)
        slug = url.removeprefix("https://").split(".", 1)[0]
        app = next((item for item in self.apps if item.slug == slug), None)
        if app is None:
            return Probe(url, checked, None, None, error="DNS lookup failed")
        behaviour = self._behaviour(slug)
        marker = _id("deployment", slug, "accepted")
        if behaviour == "gateway":
            return Probe(url, checked, 502, 38 + wobble, None, False)
        if behaviour == "failing":
            return Probe(url, checked, 503, app.latency_ms + wobble, marker, False)
        return Probe(url, checked, 200, app.latency_ms + wobble, marker, None)


class _ReloadingAssets(dict[str, Any]):
    """Keep the last complete distribution while a local rebuild is in progress."""

    def __init__(self) -> None:
        self._current = load_assets()
        super().__init__(self._current)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            self._current = load_assets()
        except (OSError, ValueError):
            pass
        return self._current.get(key, default)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openstack_platform.dashboard.preview",
        description="serve the read-only dashboard with synthetic evidence on loopback",
    )
    parser.add_argument("--port", type=int, default=8470)
    parser.add_argument("--scenario", choices=SCENARIOS, default="mixed")
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--platform-config", type=Path, default=EXAMPLE_INVENTORY)
    parser.add_argument("--delay", type=float, default=0, help="seconds to delay each refresh")
    args = parser.parse_args(argv)
    platform = load_platform(args.platform_config)
    fixture = Fixture(platform, args.scenario)

    def read_admin() -> AdminReads:
        time.sleep(args.delay)
        return fixture.read_admin()

    collector = Collector(
        platform,
        read_admin=read_admin,
        read_operator=fixture.read_operator,
        probe=fixture.probe,
        release=_commit("preview"),
    )
    service = DashboardService(
        collector.collect, pending_snapshot(platform), interval_seconds=args.interval
    )
    server = LoopbackDashboardServer(args.port, service, _ReloadingAssets())
    print(
        f"preview=listening url=http://localhost:{args.port} scenario={args.scenario}",
        file=sys.stdout,
        flush=True,
    )
    serve(server, service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
