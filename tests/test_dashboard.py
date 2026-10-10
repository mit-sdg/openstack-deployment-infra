from __future__ import annotations

import copy
import http.client
import io
import json
import os
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from unittest import mock

from openstack_platform import operator, runtime
from openstack_platform.config import Config, load_platform, load_policy
from openstack_platform.dashboard import model, preview, server, service, sources
from openstack_platform.dashboard.model import Evidence, Observed
from openstack_platform.dashboard.sources import Probe, SourceError

ROOT = Path(__file__).resolve().parents[1]


def configuration() -> Config:
    return Config(
        load_platform(ROOT / "config/platform.example.json"),
        load_policy(ROOT / "config/platform-policy.example.json", require_private=False),
    )


PLATFORM = configuration().platform


def now() -> str:
    return sources.now_timestamp()


def fixture(scenario: str = "healthy") -> preview.Fixture:
    return preview.Fixture(PLATFORM, scenario)


def admin_reads(payload: dict[str, Any]) -> sources.AdminReads:
    return sources.parse_admin_reads(json.dumps(payload).encode())


def application_ids(reads: sources.AdminReads) -> dict[str, str]:
    return {item.slug: item.application_id for item in reads.applications}


def serving_probes(reads: sources.AdminReads) -> dict[str, Probe]:
    deployments = {item.deployment_id: item for item in reads.deployments}
    probes: dict[str, Probe] = {}
    for application in reads.applications:
        if not application.enabled or application.active_deployment_id is None:
            continue
        deployment = deployments.get(application.active_deployment_id)
        if deployment is None or deployment.health_path is None:
            continue
        url = sources.route_url(PLATFORM, application.slug, deployment.health_path)
        probes[application.application_id] = Probe(url, now(), 200, 80, deployment.deployment_id)
    return probes


def ingress_probes(status: int = 200) -> tuple[Probe, ...]:
    domain = PLATFORM.domain
    return (
        Probe(f"https://{domain}/healthz", now(), 200, 40, None, True),
        Probe(f"https://wildcard-health.{domain}/healthz", now(), status, 42, None, status == 200),
    )


def evidence(
    reads: sources.AdminReads | None,
    *,
    operator_reads: sources.OperatorReads | None = None,
    routes: dict[str, Probe] | None = None,
    ingress: tuple[Probe, ...] | None = None,
    admin_ok: bool = True,
    operator_ok: bool = True,
    admin_error: str | None = None,
) -> Evidence:
    if operator_reads is None:
        operator_reads = fixture().read_operator()
    if routes is None:
        routes = {} if reads is None else serving_probes(reads)
    return Evidence(
        platform=PLATFORM,
        generated_at=now(),
        admin=Observed(reads, admin_ok, now(), admin_error),
        operator=Observed(operator_reads, operator_ok, now(), None if operator_ok else "down"),
        ingress=ingress_probes() if ingress is None else ingress,
        routes=routes,
    )


def app(snapshot: dict[str, Any], slug: str) -> dict[str, Any]:
    return next(item for item in snapshot["applications"] if item["slug"] == slug)


def role(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in snapshot["roles"] if item["role"] == name)


def signal(role_model: dict[str, Any], label: str) -> dict[str, Any]:
    return next(item for item in role_model["signals"] if item["label"] == label)


class SourceParsingTests(unittest.TestCase):
    def test_reader_envelope_projects_allowlisted_records(self) -> None:
        reads = admin_reads(fixture("mixed").admin_payload())

        self.assertEqual(reads.api_version, 1)
        self.assertIn("reuse-worker-v1", reads.features)
        self.assertEqual(len(reads.applications), len(preview.APPS))
        self.assertEqual({image.role for image in reads.images}, set(sources.IMAGE_ROLES))
        self.assertEqual(reads.controller_units, {"controller": "active", "readiness": "active"})
        self.assertIsNotNone(reads.health)
        self.assertEqual(reads.section_errors, {})
        deployment = reads.deployments[0]
        self.assertEqual(deployment.health_path, "/health")
        self.assertIn(deployment.runtime, {"bun", "node"})
        self.assertTrue(
            deployment.repository and deployment.repository.startswith("https://github.com/")
        )
        self.assertFalse(hasattr(reads.storage[0], "provider_id"))
        self.assertEqual(reads.storage[0].usage["usedBytes"], 1288490188)
        self.assertEqual(reads.storage_host["cpuCount"], 4)
        self.assertEqual(reads.storage_host["postgresConnections"], {"current": 42, "limit": 400})

    def test_blocked_storage_appears_in_needs_attention_even_on_a_serving_app(self) -> None:
        payload = fixture("healthy").admin_payload()
        resource = next(
            item for item in payload["storage"]["body"]["items"] if item["type"] == "mongo"
        )
        resource["writeBlock"] = {"blocked": True, "reason": "size_limit_exceeded", "since": now()}
        snapshot = model.build_snapshot(evidence(admin_reads(payload)))
        application = next(
            item for item in snapshot["applications"] if item["id"] == resource["applicationId"]
        )
        self.assertEqual(application["status"]["key"], "serving")
        issue = next(item for item in snapshot["issues"] if item["target"] == application["id"])
        self.assertEqual((issue["scope"], issue["tone"]), ("application", "warning"))
        self.assertIn("MongoDB", issue["summary"])
        self.assertEqual(snapshot["summary"]["counts"]["applications"]["attention"], 1)

    def test_storage_samples_stay_stale_when_collection_fails_and_host_fields_are_allowlisted(
        self,
    ) -> None:
        payload = fixture("healthy").admin_payload()
        host = payload["status"]["body"]["storageHost"]
        host["providerSecret"] = "hidden"
        resource = payload["storage"]["body"]["items"][0]
        resource["usage"]["providerSecret"] = "hidden"
        reads = admin_reads(payload)
        self.assertNotIn("providerSecret", reads.storage_host)
        self.assertNotIn("providerSecret", reads.storage[0].usage)
        cached = model.build_snapshot(evidence(reads, admin_ok=False))
        self.assertTrue(cached["storageHost"]["stale"])
        self.assertTrue(
            next(item for app in cached["applications"] for item in app["storage"])["usage"][
                "stale"
            ]
        )
        host["cpuCount"] = True
        invalid = admin_reads(payload)
        self.assertIsNone(invalid.storage_host)
        self.assertIn("status", invalid.section_errors)

    def test_malformed_records_are_dropped_and_text_is_redacted(self) -> None:
        payload = fixture("mixed").admin_payload()
        items = payload["applications"]["body"]["items"]
        broken = copy.deepcopy(items[0])
        broken["applicationId"] = "NOT-A-UUID"
        uppercase = copy.deepcopy(items[1])
        uppercase["slug"] = "Bad_Slug"
        items.extend([broken, uppercase])
        deployment = payload["deployments"]["body"]["items"][0]
        deployment["safeError"] = "failed\x1b[31m password=hunter2 Bearer abc.def"
        payload["deployments"]["body"]["items"].append({**deployment, "status": "exploded"})

        reads = admin_reads(payload)

        self.assertEqual(len(reads.applications), len(preview.APPS))
        self.assertEqual(len(reads.deployments), len(payload["deployments"]["body"]["items"]) - 1)
        error = reads.deployments[0].safe_error or ""
        self.assertNotIn("hunter2", error)
        self.assertNotIn("abc.def", error)
        self.assertNotIn("\x1b", error)

        with self.assertRaisesRegex(SourceError, "unsupported version"):
            sources.parse_admin_reads(b'{"version": 2}')
        with self.assertRaisesRegex(SourceError, "malformed"):
            sources.parse_admin_reads(b'{"version": 1, "version": 1}')
        with self.assertRaisesRegex(SourceError, "malformed"):
            sources.parse_admin_reads(b"not json")

    def test_operator_reads_validate_infra_list_projection(self) -> None:
        reads = sources.parse_operator_reads(
            [
                {
                    "role": "admin",
                    "image": {"displayName": "img", "sourceCommit": "a" * 40, "selectedAt": now()},
                    "live": {"available": True, "state": "active", "checkedAt": now()},
                },
                {"role": "intruder", "live": {"available": True, "state": "active"}},
                {"role": "ingress", "live": {"available": True, "state": "melting"}},
            ],
            [
                {
                    "operationId": "11111111-1111-4111-8111-111111111111",
                    "scope": "infrastructure",
                    "kind": "infra.replace",
                    "status": "running",
                    "phase": "candidate_created",
                    "updatedAt": now(),
                    "deadlineAt": None,
                    "builder": False,
                }
            ],
        )
        self.assertEqual([host.role for host in reads.hosts], ["admin", "ingress"])
        self.assertEqual(reads.hosts[1].state, "unknown")
        self.assertEqual(reads.operations[0].kind, "infra.replace")


class FakeController(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []
    pages = 3

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, document: object) -> None:
        body = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests.append(("GET", self.path))
        path, _, query = self.path.partition("?")
        if path == "/v1/admin/capabilities":
            self._send(200, {"apiVersion": 1, "features": ["reuse-worker-v1"]})
        elif path == "/v1/admin/images":
            self._send(200, {"items": []})
        elif path == "/v1/admin/deployments":
            page = 1
            match = re.search(r"cursor=0+(\d)-", query)
            if match:
                page = int(match.group(1)) + 1
            cursor = f"0000000{page}-0000-4000-8000-000000000000"
            self._send(
                200,
                {
                    "items": [{"page": page}],
                    "nextCursor": cursor if page < type(self).pages else None,
                    "truncated": page < type(self).pages,
                },
            )
        elif path == "/v1/admin/storage":
            self._send(503, {"error": {"code": "DEPENDENCY_UNAVAILABLE"}})
        else:
            self._send(200, {"items": [], "nextCursor": None, "truncated": False})

    def do_POST(self) -> None:  # noqa: N802
        type(self).requests.append(("POST", self.path))
        self._send(405, {})


class AdminReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.socket_path = str(self.root / "privileged.sock")
        FakeController.requests = []
        self.server = socketserver.ThreadingUnixStreamServer(self.socket_path, FakeController)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def run_reader(self, status_path: Path) -> dict[str, Any]:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                sources.ADMIN_READER,
                self.socket_path,
                "fixture",
                str(status_path),
                str(sources.DEPLOYMENT_PAGES),
                str(sources.OPERATION_PAGES),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return json.loads(completed.stdout)

    def test_reader_only_gets_database_routes_with_a_bounded_page_budget(self) -> None:
        status_path = self.root / "fixture.json"
        status_path.write_text(json.dumps({"checked_at": now(), "healthy": True, "checks": {}}))

        output = self.run_reader(status_path)

        self.assertEqual({method for method, _path in FakeController.requests}, {"GET"})
        paths = {path.partition("?")[0] for _method, path in FakeController.requests}
        self.assertEqual(
            paths,
            {
                "/v1/admin/capabilities",
                "/v1/admin/images",
                "/v1/admin/applications",
                "/v1/admin/deployments",
                "/v1/admin/storage",
                "/v1/admin/status",
                "/v1/admin/operations",
            },
        )
        self.assertNotIn("/v1/admin/hosts", paths)
        deployments = output["deployments"]["body"]
        self.assertEqual(deployments["items"], [{"page": 1}, {"page": 2}])
        self.assertTrue(deployments["truncated"])
        self.assertEqual(output["storage"]["status"], 503)
        self.assertIn("error", output["units"])
        self.assertTrue(output["health"]["body"]["healthy"])
        reads = sources.parse_admin_reads(json.dumps(output).encode())
        self.assertIn("storage", reads.section_errors)
        self.assertTrue(reads.deployments_truncated)

    def test_read_admin_uses_the_pinned_alias_and_safe_failures(self) -> None:
        commands: list[tuple[str, ...]] = []
        payload = json.dumps(fixture().admin_payload()).encode()

        def runner(command: tuple[str, ...], **_bounds: Any) -> runtime.CommandResult:
            commands.append(command)
            return runtime.CommandResult(command, 0, payload, b"", False, False)

        reads = sources.read_admin(
            PLATFORM, ssh_config="/fixture/ssh/config", command_runner=runner
        )
        self.assertEqual(len(reads.applications), len(preview.APPS))
        command = commands[0]
        self.assertEqual(command[:5], ("ssh", "-F", "/fixture/ssh/config", "platform-admin", "--"))
        self.assertIn("/run/current-system/sw/bin/python3 -I -c", command[5])
        self.assertIn(f"/run/{PLATFORM.namespace}-controller/privileged.sock", command[5])
        self.assertIn(f"/persistent/status/{PLATFORM.namespace}.json", command[5])

        def unreachable(command: tuple[str, ...], **_bounds: Any) -> runtime.CommandResult:
            result = runtime.CommandResult(command, 255, b"", b"denied", False, False)
            raise runtime.CommandFailure("ssh failed", result)

        with self.assertRaisesRegex(SourceError, "could not reach the admin host"):
            sources.read_admin(PLATFORM, command_runner=unreachable)

        def slow(command: tuple[str, ...], **_bounds: Any) -> runtime.CommandResult:
            raise runtime.CommandTimedOut("timed out")

        with self.assertRaisesRegex(SourceError, "timed out"):
            sources.read_admin(PLATFORM, command_runner=slow)


class _Response(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class ProbeTests(unittest.TestCase):
    marker = "22222222-2222-4222-8222-222222222222"

    def opener(self, outcome: Any) -> Callable[..., Any]:
        def open_url(request: Any, timeout: float) -> Any:
            self.assertEqual(request.get_method(), "GET")
            self.assertGreater(timeout, 0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return open_url

    def test_http_errors_and_transport_failures_are_classified(self) -> None:
        headers = Message()
        error = urllib.error.HTTPError("https://x", 503, "unavailable", headers, io.BytesIO(b""))
        probe = sources.probe("https://x.example/h", timeout_seconds=5, opener=self.opener(error))
        self.assertEqual(probe.status, 503)
        cases = {
            "DNS lookup failed": urllib.error.URLError(socket.gaierror("no such host")),
            "connection refused": urllib.error.URLError(ConnectionRefusedError()),
            "TLS certificate was rejected": urllib.error.URLError(ssl.SSLCertVerificationError()),
            "timed out": TimeoutError(),
        }
        for message, failure in cases.items():
            with self.subTest(message=message):
                probe = sources.probe(
                    "https://x.example/h", timeout_seconds=5, opener=self.opener(failure)
                )
                self.assertIsNone(probe.status)
                self.assertEqual(probe.error, message)

        response = _Response(200, b"OK\n", {"X-Platform-Deployment": self.marker})
        probe = sources.probe(
            "https://apps.example.com/healthz",
            timeout_seconds=5,
            expect_body=b"OK",
            opener=self.opener(response),
        )
        self.assertEqual(
            (probe.status, probe.marker, probe.body_ok, probe.error), (200, self.marker, True, None)
        )
        self.assertIsNotNone(probe.latency_ms)

    def test_probes_have_a_wall_clock_bound_and_never_overlap(self) -> None:
        release = threading.Event()

        def drip(_request: Any, timeout: float) -> Any:
            release.wait(10)
            return _Response(200, b"OK", {})

        url = "https://drip.example/health"
        started = datetime.now(UTC)
        probe = sources.probe(url, timeout_seconds=0.3, opener=drip)
        elapsed = (datetime.now(UTC) - started).total_seconds()
        self.assertEqual((probe.status, probe.error), (None, "timed out"))
        self.assertLess(elapsed, 3)
        again = sources.probe(url, timeout_seconds=0.3, opener=drip)
        self.assertEqual(again.error, "previous probe still running")
        release.set()
        for _ in range(50):
            with sources._INFLIGHT_LOCK:
                thread = sources._INFLIGHT.get(url)
            if thread is None or not thread.is_alive():
                break
            thread.join(0.1)
        recovered = sources.probe(
            url, timeout_seconds=2, opener=self.opener(_Response(200, b"OK", {}))
        )
        self.assertEqual(recovered.status, 200)

    def test_probes_require_https_and_bound_the_body(self) -> None:
        with self.assertRaises(ValueError):
            sources.probe("http://apps.example.com/healthz", timeout_seconds=5)
        large = _Response(200, b"x" * 5000, {})
        probe = sources.probe("https://x.example/h", timeout_seconds=5, opener=self.opener(large))
        self.assertEqual(probe.error, "response exceeded 4 KiB")


class ModelTests(unittest.TestCase):
    def test_verified_platform_is_operational(self) -> None:
        reads = admin_reads(fixture("healthy").admin_payload())
        snapshot = model.build_snapshot(evidence(reads))

        self.assertEqual(snapshot["summary"]["tone"], "good")
        self.assertEqual(snapshot["summary"]["headline"], "All systems operational")
        self.assertEqual(snapshot["issues"], [])
        self.assertEqual(
            {item["role"]: item["status"]["key"] for item in snapshot["roles"]},
            {
                "admin": "operational",
                "ingress": "operational",
                "storage": "operational",
                "worker": "operational",
                "builder": "idle",
            },
        )
        self.assertEqual(app(snapshot, "course-planner")["status"]["key"], "serving")
        self.assertEqual(app(snapshot, "alumni-directory")["status"]["key"], "stopped")
        self.assertEqual(app(snapshot, "alumni-directory")["route"]["outcome"], "not-checked")
        self.assertEqual(snapshot["summary"]["counts"]["backup"]["ageHours"], 5.2)
        json.dumps(snapshot, allow_nan=False)
        self.assertNotIn("providerId", json.dumps(snapshot))

    def test_route_marker_decides_whether_the_accepted_deployment_answers(self) -> None:
        reads = admin_reads(fixture("healthy").admin_payload())
        ids = application_ids(reads)
        routes = serving_probes(reads)
        legacy = routes[ids["lab-inventory"]]
        routes[ids["lab-inventory"]] = Probe(legacy.url, now(), 200, 70, ids["lab-inventory"])
        other = routes[ids["office-hours"]]
        routes[ids["office-hours"]] = Probe(
            other.url, now(), 200, 70, "33333333-3333-4333-8333-333333333333"
        )
        bare = routes[ids["seminar-signup"]]
        routes[ids["seminar-signup"]] = Probe(bare.url, now(), 200, 70, None)
        failing = routes[ids["course-planner"]]
        routes[ids["course-planner"]] = Probe(failing.url, now(), 503, 70, None, False)
        down = routes[ids["thesis-tracker"]]
        routes[ids["thesis-tracker"]] = Probe(down.url, now(), None, None, error="timed out")

        snapshot = model.build_snapshot(evidence(reads, routes=routes))

        self.assertEqual(app(snapshot, "lab-inventory")["status"]["key"], "serving")
        self.assertEqual(app(snapshot, "office-hours")["status"]["key"], "unverified")
        self.assertIn("33333333", app(snapshot, "office-hours")["route"]["detail"])
        self.assertEqual(app(snapshot, "seminar-signup")["route"]["outcome"], "wrong-deployment")
        self.assertEqual(app(snapshot, "course-planner")["status"]["key"], "failing")
        self.assertEqual(app(snapshot, "course-planner")["route"]["summary"], "HTTP 503")
        self.assertEqual(app(snapshot, "thesis-tracker")["route"]["summary"], "Timed out")
        self.assertEqual(snapshot["summary"]["headline"], "Disruption detected")
        critical = {issue["subject"] for issue in snapshot["issues"] if issue["tone"] == "critical"}
        self.assertEqual(critical, {"course-planner", "thesis-tracker"})

    def test_operations_take_precedence_and_recovery_is_explained(self) -> None:
        reads = admin_reads(fixture("mixed").admin_payload())
        ids = application_ids(reads)
        routes = serving_probes(reads)
        candidate = routes[ids["data-explorer"]]
        routes[ids["data-explorer"]] = Probe(candidate.url, now(), 502, 50, None, False)

        snapshot = model.build_snapshot(evidence(reads, routes=routes))

        deploying = app(snapshot, "data-explorer")
        self.assertEqual(
            deploying["status"], {"key": "changing", "label": "Deploying", "tone": "info"}
        )
        self.assertEqual(deploying["operation"]["phase"], "Builder creating")
        recovery = app(snapshot, "survey-collector")
        self.assertEqual(recovery["status"]["key"], "needs-recovery")
        issue = next(item for item in snapshot["issues"] if item["subject"] == "survey-collector")
        self.assertIn("did not confirm", issue["detail"])
        self.assertEqual(role(snapshot, "builder")["status"]["key"], "building")
        thesis = app(snapshot, "thesis-tracker")
        self.assertEqual(thesis["status"]["key"], "serving")
        self.assertEqual(thesis["attempts"][0]["status"]["key"], "failed")
        keys = [item["status"]["key"] for item in snapshot["operations"]]
        self.assertLess(keys.index("running"), keys.index("succeeded"))


class CollectorAndServiceTests(unittest.TestCase):
    def test_collector_keeps_last_good_evidence_and_route_history(self) -> None:
        source = fixture("healthy")
        calls = {"admin": 0}

        def read_admin() -> sources.AdminReads:
            calls["admin"] += 1
            if calls["admin"] == 3:
                raise SourceError("the admin read timed out over the pinned SSH bridge")
            return source.read_admin()

        collector = service.Collector(
            PLATFORM,
            read_admin=read_admin,
            read_operator=source.read_operator,
            probe=source.probe,
        )
        collector.collect()
        collector.collect()
        stale = collector.collect()

        self.assertEqual(len(stale["applications"]), len(preview.APPS))
        self.assertEqual(signal(role(stale, "admin"), "Controller API")["value"], "Unreachable")
        planner = app(stale, "course-planner")
        self.assertEqual(len(planner["checks"]), 3)
        self.assertEqual({item["tone"] for item in planner["checks"]}, {"good"})
        self.assertEqual(app(stale, "alumni-directory")["checks"], [])

        def broken_admin() -> sources.AdminReads:
            raise RuntimeError("unexpected")

        fresh = service.Collector(
            PLATFORM,
            read_admin=broken_admin,
            read_operator=source.read_operator,
            probe=source.probe,
        ).collect()
        controller = next(item for item in fresh["sources"] if item["key"] == "controller")
        self.assertIn("RuntimeError", controller["error"])

    def test_refresh_is_single_flight_and_manual_requests_are_bounded(self) -> None:
        clock = [100.0]
        gate = threading.Event()
        entered = threading.Event()
        snapshots = iter([{"state": "ready", "n": 1}, {"state": "ready", "n": 2}])

        def collect() -> dict[str, Any]:
            entered.set()
            gate.wait(5)
            return next(snapshots)

        dashboard = service.DashboardService(
            collect, {"state": "pending"}, interval_seconds=60, clock=lambda: clock[0]
        )
        worker = threading.Thread(target=dashboard.refresh)
        worker.start()
        entered.wait(5)
        self.assertTrue(json.loads(dashboard.document())["refresh"]["inProgress"])
        self.assertFalse(dashboard.request_refresh())
        dashboard.refresh()  # returns immediately; the first refresh owns the slot
        gate.set()
        worker.join(5)
        document = json.loads(dashboard.document())
        self.assertEqual(document["n"], 1)
        self.assertFalse(document["refresh"]["inProgress"])
        self.assertFalse(dashboard.request_refresh())
        clock[0] += service.MINIMUM_MANUAL_REFRESH_SECONDS + 1
        self.assertTrue(dashboard.request_refresh())

        def explode() -> dict[str, Any]:
            raise ValueError("bug")

        failing = service.DashboardService(explode, {"state": "ready", "n": 0}, interval_seconds=60)
        failing.refresh()
        document = json.loads(failing.document())
        self.assertEqual(document["n"], 0)
        self.assertIn("ValueError", document["refresh"]["error"])


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=5)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.temporary.name) / "dashboard.sock")
        self.dashboard = service.DashboardService(
            lambda: {"state": "ready"}, {"state": "ready", "value": 1}, interval_seconds=60
        )
        self.refreshes: list[bool] = []
        original = self.dashboard.request_refresh

        def record() -> bool:
            result = original()
            self.refreshes.append(result)
            return result

        self.dashboard.request_refresh = record  # type: ignore[method-assign]
        self.server = server.UnixDashboardServer(
            self.socket_path, self.dashboard, server.load_assets()
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.001}, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        if body is not None:
            # The server may answer and close before reading a rejected body;
            # http.client sends the body in a second write that can then hit
            # EPIPE. Send the whole request in one write instead.
            lines = [f"{method} {path} HTTP/1.1", "Host: localhost:8470"]
            lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
            lines += [f"Content-Length: {len(body)}", "Connection: close", "", ""]
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
                raw.settimeout(5)
                raw.connect(self.socket_path)
                raw.sendall("\r\n".join(lines).encode() + body)
                reply = http.client.HTTPResponse(raw)
                reply.begin()
                return (
                    reply.status,
                    {k.lower(): v for k, v in reply.getheaders()},
                    reply.read(),
                )
        connection = UnixHTTPConnection(self.socket_path)
        try:
            connection.request(
                method, path, body=body, headers={"Host": "localhost:8470", **(headers or {})}
            )
            response = connection.getresponse()
            return (
                response.status,
                {k.lower(): v for k, v in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    def test_static_assets_and_snapshot_carry_strict_headers(self) -> None:
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertRegex(body, rb'/assets/[^" ]+\.js')
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        status, headers, body = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(json.loads(body)["value"], 1)
        status, _headers, body = self.request(
            "GET", "/api/snapshot", {"If-None-Match": headers["etag"]}
        )
        self.assertEqual((status, body), (304, b""))
        css_route = next(route for route in server.load_assets() if route.endswith(".css"))
        status, headers, body = self.request("HEAD", css_route)
        self.assertEqual((status, body), (200, b""))
        self.assertTrue(headers["content-type"].startswith("text/css"))

    def test_unknown_paths_methods_hosts_and_bodies_are_rejected(self) -> None:
        self.assertEqual(self.request("GET", "/../../etc/passwd")[0], 404)
        self.assertEqual(self.request("GET", "/static/dashboard.js")[0], 404)
        self.assertEqual(self.request("GET", "/.vite/manifest.json")[0], 404)
        self.assertEqual(self.request("GET", "/apps/some-app")[0], 404)
        self.assertEqual(self.request("PUT", "/api/snapshot")[0], 405)
        self.assertEqual(self.request("GET", "/", {"Host": "attacker.example"})[0], 421)
        self.assertEqual(self.request("GET", "/", {"Host": "localhost.attacker.example"})[0], 421)
        status, _headers, _body = self.request(
            "POST", "/api/refresh", {"X-Dashboard-Refresh": "1"}, b"{}"
        )
        self.assertEqual(status, 413)

    def test_refresh_requires_a_same_origin_dashboard_request(self) -> None:
        self.assertEqual(self.request("POST", "/api/refresh")[0], 403)
        cross = {"X-Dashboard-Refresh": "1", "Origin": "https://attacker.example"}
        self.assertEqual(self.request("POST", "/api/refresh", cross)[0], 403)
        site = {"X-Dashboard-Refresh": "1", "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.request("POST", "/api/refresh", site)[0], 403)
        self.assertEqual(self.refreshes, [])
        allowed = {"X-Dashboard-Refresh": "1", "Origin": "http://localhost:8470"}
        status, _headers, body = self.request("POST", "/api/refresh", allowed)
        self.assertEqual((status, json.loads(body)), (202, {"accepted": True}))
        self.assertEqual(self.refreshes, [True])

    def test_peers_with_another_uid_are_disconnected(self) -> None:
        foreign_path = str(Path(self.temporary.name) / "foreign.sock")
        foreign = server.UnixDashboardServer(
            foreign_path,
            self.dashboard,
            server.load_assets(),
            peer_credentials=lambda _connection: (os.geteuid() + 1, os.getegid()),
        )
        thread = threading.Thread(target=foreign.serve_forever, daemon=True)
        thread.start()
        try:
            connection = UnixHTTPConnection(foreign_path)
            # The server may drop the peer before or after the request is sent.
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
                connection.request("GET", "/", headers={"Host": "localhost"})
                connection.getresponse()
            connection.close()
        finally:
            foreign.shutdown()
            foreign.server_close()
        self.assertFalse(os.path.exists(foreign_path))

    def test_socket_directory_must_be_private(self) -> None:
        with tempfile.TemporaryDirectory() as shared:
            os.chmod(shared, 0o755)
            with self.assertRaises(PermissionError):
                server.UnixDashboardServer(str(Path(shared) / "d.sock"), self.dashboard, {})


class OperatorCommandTests(unittest.TestCase):
    def test_dispatch_serves_operator_state_reads_without_a_command_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o700)
            args = operator.build_parser().parse_args(
                [
                    "--platform-config",
                    str(ROOT / "config/platform.example.json"),
                    "--state-directory",
                    str(state),
                    "dashboard",
                ]
            )
            with (
                mock.patch.object(operator, "_load_config", return_value=configuration()),
                mock.patch.object(operator.dashboard_server, "run_operator_dashboard") as run,
                mock.patch.object(
                    operator.status.openstack, "list_persistent_hosts", side_effect=OSError
                ),
            ):
                operator.dispatch(args, stdout=io.StringIO())
                kwargs = run.call_args.kwargs
                self.assertEqual(kwargs["socket_path"], state / "run" / "dashboard.sock")
                self.assertEqual(kwargs["interval_seconds"], 60)
                reads = kwargs["read_operator"]()
            self.assertEqual({host.role for host in reads.hosts}, set(sources.IMAGE_ROLES))
            self.assertFalse(any(host.available for host in reads.hosts))
            self.assertEqual(reads.operations, ())


class StaticAssetTests(unittest.TestCase):
    def inventory(self, root: Path) -> Path:
        directory = root / "static"
        directory.mkdir()
        (directory / "assets").mkdir()
        (directory / "assets/entry-123.js").write_text("console.log('fixture');")
        (directory / "assets/entry-123.css").write_text("body {color: black;}")
        (directory / "theme.js").write_text("/* external preference bootstrap */")
        (directory / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (directory / "index.html").write_text(
            '<!doctype html><script src="/theme.js"></script>'
            '<script type="module" src="/assets/entry-123.js"></script>'
            '<link rel="stylesheet" href="/assets/entry-123.css">'
        )
        return directory

    def test_loader_rejects_missing_extra_indirect_and_unbounded_files(self) -> None:
        for damage in ("missing", "extra", "symlink", "directory", "file-limit", "size"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = self.inventory(root)
                if damage == "missing":
                    (directory / "theme.js").unlink()
                elif damage == "extra":
                    (directory / "receipt.json").write_text("{}")
                elif damage == "symlink":
                    (directory / "theme.js").unlink()
                    (directory / "theme.js").symlink_to(directory / "assets/entry-123.js")
                elif damage == "directory":
                    (directory / "assets/fake.js").mkdir()
                elif damage == "file-limit":
                    for index in range(server.MAXIMUM_ASSETS):
                        (directory / f"assets/extra-{index}.js").write_text("// extra")
                else:
                    with (directory / "assets/entry-123.js").open("wb") as stream:
                        stream.truncate(server.MAXIMUM_ASSET_BYTES + 1)
                with self.assertRaises((ValueError, OSError)):
                    server.load_assets(directory)

        bad_entries = (
            '<script src="https://untrusted.example.invalid/code.js"></script>',
            '<script src="/assets/missing.js"></script>',
            '<script src="/assets/../theme.js"></script>',
            '<script src="/assets/entry-123.js?alternate"></script>',
            '<script src="#entry"></script>',
            "<img src>",
            '<img srcset="https://untrusted.example.invalid/image.svg 1x">',
            "<script>window.alert(1)</script>",
            "<style>body {display:none}</style>",
            '<div style="color:red">Inline</div>',
            '<img src="/favicon.svg" onload="window.alert(1)">',
            '<script src="/theme.js" src="/assets/entry-123.js"></script>',
            '<base href="/">',
        )
        for entry in bad_entries:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as temporary:
                directory = self.inventory(Path(temporary))
                (directory / "index.html").write_text(entry)
                with self.assertRaises(ValueError):
                    server.load_assets(directory)


if __name__ == "__main__":
    unittest.main()
