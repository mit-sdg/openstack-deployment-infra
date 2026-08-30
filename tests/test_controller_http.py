from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from openstack_platform.controller.http import (
    ControllerServer,
    HttpError,
    PeerPolicy,
    Response,
    Router,
    TransportLimits,
    linux_peer_credentials,
)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=5)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class ControllerTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.temporary.name) / "controller.sock")
        self.router = Router()
        self.server = ControllerServer(self.socket_path, self.router)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        Path(self.socket_path).unlink(missing_ok=True)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        target: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object], http.client.HTTPMessage]:
        connection = UnixHTTPConnection(self.socket_path)
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read())
        result = response.status, payload, response.headers
        connection.close()
        return result

    def test_routes_path_query_and_idempotency_without_public_listener(self) -> None:
        def show(request):
            return Response(
                200,
                {
                    "id": request.path_parameters["id"],
                    "cursor": request.query["cursor"][0],
                },
            )

        def mutate(request):
            assert isinstance(request.body, dict)
            return Response(
                202,
                {"operationId": request.idempotency_key(), "name": request.body["name"]},
                {"Location": "/v1/operations/operation-1"},
            )

        self.router.add("GET", "/v1/applications/{id}", show)
        self.router.add("POST", "/v1/applications", mutate)
        status, body, headers = self.request(
            "GET",
            "/v1/applications/app-1?cursor=next",
        )
        self.assertEqual((status, body), (200, {"cursor": "next", "id": "app-1"}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(headers["X-Correlation-ID"])

        status, body, headers = self.request(
            "POST",
            "/v1/applications",
            body=b'{"name":"demo"}',
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "00000000-0000-4000-8000-000000000001",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(
            body,
            {"name": "demo", "operationId": "00000000-0000-4000-8000-000000000001"},
        )
        self.assertEqual(headers["Location"], "/v1/operations/operation-1")

    def test_invalid_json_method_and_route_return_only_safe_json(self) -> None:
        self.router.add("POST", "/v1/applications", lambda _request: Response(201, {}))
        status, body, _headers = self.request(
            "POST",
            "/v1/applications",
            body=b'{"name":"one","name":"two"}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_JSON")

        status, body, _headers = self.request("GET", "/v1/applications")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"]["code"], "METHOD_NOT_ALLOWED")
        status, body, _headers = self.request("GET", "/outside")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

    def test_seeded_malformed_framing_never_reaches_mutation_or_leaks_body(self) -> None:
        calls = 0

        def mutate(_request):
            nonlocal calls
            calls += 1
            return Response(202, {})

        self.router.add("POST", "/v1/framing", mutate)
        secret = "framing-secret-that-must-not-escape"
        requests = (
            f"POST /v1/framing HTTP/1.1\r\nHost: local\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n{secret}\r\n",
            f"POST /v1/framing HTTP/1.1\r\nHost: local\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\n{secret}",
            f"POST /v1/framing HTTP/1.1\r\nHost: local\r\nContent-Length: -1\r\n\r\n{secret}",
            f"POST /v1/framing HTTP/1.1\r\nHost: local\r\nContent-Length: 1048577\r\n\r\n{secret}",
            f"POST /v1/framing HTTP/1.1\r\nHost: local\r\nContent-Length: nope\r\n\r\n{secret}",
            "POST /v1/framing HTTP/1.1\r\nHost: local\r\nContent-Length: 8\r\n\r\n{}",
        )
        for request in requests:
            with self.subTest(request=request.split("\r\n")[2]):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(5)
                client.connect(self.socket_path)
                client.sendall(request.encode())
                client.shutdown(socket.SHUT_WR)
                response = b""
                while chunk := client.recv(65_536):
                    response += chunk
                client.close()
                self.assertIn(b"HTTP/1.1 4", response)
                self.assertNotIn(secret.encode(), response)
        self.assertEqual(calls, 0)

    def test_handler_errors_are_bounded_and_unexpected_values_do_not_escape(self) -> None:
        sentinel = "provider-secret-that-must-not-escape"

        def safe_failure(_request):
            raise HttpError(409, "CONFLICT", "operation already running", operation_id="op-1")

        def unexpected(_request):
            raise RuntimeError(sentinel)

        self.router.add("GET", "/v1/safe", safe_failure)
        self.router.add("GET", "/v1/unexpected", unexpected)
        status, body, _headers = self.request("GET", "/v1/safe")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["operationId"], "op-1")
        status, body, _headers = self.request("GET", "/v1/unexpected")
        rendered = json.dumps(body)
        self.assertEqual(status, 500)
        self.assertNotIn(sentinel, rendered)
        self.assertEqual(body["error"]["code"], "INTERNAL_ERROR")


class ControllerResourceLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.socket_path = str(Path(self.temporary.name) / "controller.sock")
        router = Router()
        router.add("GET", "/v1/ready", lambda _request: Response(200, {"ready": True}))
        router.add("POST", "/v1/body", lambda request: Response(200, {"body": request.body}))
        self.server = ControllerServer(
            self.socket_path,
            router,
            limits=TransportLimits(
                global_connections=2,
                peer_connections=2,
                header_seconds=0.15,
                body_seconds=0.15,
                idle_seconds=0.15,
                write_seconds=0.15,
                requests_per_connection=2,
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def connect(self) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(self.socket_path)
        return client

    @staticmethod
    def response(client: socket.socket) -> tuple[int, dict[str, object], bytes]:
        received = b""
        while b"\r\n\r\n" not in received:
            block = client.recv(4096)
            if not block:
                raise AssertionError("connection closed before HTTP response headers")
            received += block
        raw_headers, body = received.split(b"\r\n\r\n", 1)
        header_lines = raw_headers.split(b"\r\n")
        status = int(header_lines[0].split()[1])
        headers = {
            key.decode().lower(): value.decode().strip()
            for key, value in (line.split(b":", 1) for line in header_lines[1:])
        }
        length = int(headers["content-length"])
        while len(body) < length:
            block = client.recv(length - len(body))
            if not block:
                raise AssertionError("connection closed before HTTP response body")
            body += block
        return status, json.loads(body[:length]), raw_headers

    def wait_for_connections(self, expected: int) -> None:
        deadline = time.monotonic() + 1
        while self.server.active_connections != expected and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.server.active_connections, expected)

    def test_http_parser_errors_remain_strict_json(self) -> None:
        client = self.connect()
        client.sendall(b"not-http\r\n\r\n")
        status, body, headers = self.response(client)
        self.assertEqual((status, body["error"]["code"]), (400, "INVALID_REQUEST"))
        self.assertIn(b"Content-Type: application/json", headers)
        self.assertNotIn(b"text/html", headers)
        client.close()

    def test_slow_headers_have_an_absolute_deadline(self) -> None:
        client = self.connect()
        try:
            for byte in b"GET /v1/ready HTTP/1.1\r\nHost: local":
                try:
                    client.send(bytes([byte]))
                except BrokenPipeError:
                    break
                time.sleep(0.02)
            self.assertEqual(client.recv(1), b"")
        finally:
            client.close()

    def test_incomplete_and_stalled_bodies_are_bounded(self) -> None:
        incomplete = self.connect()
        incomplete.sendall(
            b"POST /v1/body HTTP/1.1\r\nHost: local\r\n"
            b"Content-Type: application/json\r\nContent-Length: 5\r\n\r\n{}"
        )
        incomplete.shutdown(socket.SHUT_WR)
        status, body, _headers = self.response(incomplete)
        self.assertEqual((status, body["error"]["code"]), (400, "INVALID_REQUEST"))
        incomplete.close()

        stalled = self.connect()
        stalled.sendall(
            b"POST /v1/body HTTP/1.1\r\nHost: local\r\n"
            b"Content-Type: application/json\r\nContent-Length: 5\r\n\r\n{"
        )
        status, body, headers = self.response(stalled)
        self.assertEqual((status, body["error"]["code"]), (408, "REQUEST_TIMEOUT"))
        self.assertIn(b"Connection: close", headers)
        stalled.close()

    def test_idle_keepalive_and_request_count_close_connections(self) -> None:
        idle = self.connect()
        idle.sendall(b"GET /v1/ready HTTP/1.1\r\nHost: local\r\n\r\n")
        self.assertEqual(self.response(idle)[0], 200)
        time.sleep(0.25)
        self.assertEqual(idle.recv(1), b"")
        idle.close()

        capped = self.connect()
        request = b"GET /v1/ready HTTP/1.1\r\nHost: local\r\n\r\n"
        capped.sendall(request)
        self.assertEqual(self.response(capped)[0], 200)
        capped.sendall(request)
        status, _body, headers = self.response(capped)
        self.assertEqual(status, 200)
        self.assertIn(b"Connection: close", headers)
        self.assertEqual(capped.recv(1), b"")
        capped.close()

    def test_connection_flood_gets_deterministic_json_overload(self) -> None:
        held = [self.connect(), self.connect()]
        try:
            for client in held:
                client.sendall(b"G")
            self.wait_for_connections(2)
            excess_connections = [self.connect() for _ in range(12)]
            for excess in excess_connections:
                status, body, headers = self.response(excess)
                self.assertEqual((status, body["error"]["code"]), (503, "CONNECTION_LIMIT"))
                self.assertIn(b"Connection: close", headers)
                excess.close()
                self.assertLessEqual(self.server.active_connections, 2)
        finally:
            for client in held:
                client.close()

    def test_shutdown_does_not_wait_for_stalled_clients(self) -> None:
        clients = [self.connect(), self.connect()]
        for client in clients:
            client.sendall(b"POST /v1/body HTTP/1.1\r\nHost: local\r\nContent-Length: 100\r\n\r\n{")
        self.wait_for_connections(2)
        started = time.monotonic()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.thread.is_alive())
        for client in clients:
            client.close()


class ControllerSocketSecurityTests(unittest.TestCase):
    def test_linux_peer_credentials_report_exact_process_identity(self) -> None:
        first, second = socket.socketpair(socket.AF_UNIX)
        try:
            self.assertEqual(linux_peer_credentials(first), (os.geteuid(), os.getegid()))
        finally:
            first.close()
            second.close()

    def test_peer_allowlist_rejects_wrong_or_unavailable_identity(self) -> None:
        for reader in (
            lambda _connection: (1234, 5678),
            lambda _connection: (_ for _ in ()).throw(OSError("unavailable")),
        ):
            with self.subTest(reader=reader), tempfile.TemporaryDirectory() as temporary:
                path = str(Path(temporary) / "controller.sock")
                server = ControllerServer(
                    path,
                    Router(),
                    peer_policy=PeerPolicy(frozenset({(100, 200)})),
                    peer_credentials=reader,
                )
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    connection = UnixHTTPConnection(path)
                    connection.request("GET", "/v1/health")
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    self.assertEqual(response.status, 503)
                    self.assertIn(
                        body["error"]["code"],
                        {"PEER_IDENTITY_REJECTED", "PEER_CREDENTIALS_UNAVAILABLE"},
                    )
                    connection.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

    def test_per_peer_concurrency_is_bounded_and_capacity_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "controller.sock")
            peer = (100, 200)
            accepted = threading.Event()

            def credentials(_connection: socket.socket) -> tuple[int, int]:
                accepted.set()
                return peer

            router = Router()
            router.add("GET", "/v1/health", lambda _request: Response(200, {"ok": True}))
            server = ControllerServer(
                path,
                router,
                peer_policy=PeerPolicy(frozenset({peer}), max_connections_per_peer=1),
                peer_credentials=credentials,
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            first = UnixHTTPConnection(path)
            try:
                first.request("GET", "/v1/health")
                first_response = first.getresponse()
                self.assertEqual(first_response.status, 200)
                first_response.read()
                self.assertTrue(accepted.wait(2))
                while not server._peer_connections:  # noqa: SLF001 - security invariant probe
                    time.sleep(0.01)
                second = UnixHTTPConnection(path)
                second.request("GET", "/v1/health")
                second_response = second.getresponse()
                self.assertEqual(second_response.status, 503)
                self.assertEqual(
                    json.loads(second_response.read())["error"]["code"], "CONNECTION_LIMIT"
                )
                second.close()
                first.close()
                deadline = time.monotonic() + 2
                while server._peer_connections and time.monotonic() < deadline:  # noqa: SLF001
                    time.sleep(0.01)
                third = UnixHTTPConnection(path)
                third.request("GET", "/v1/health")
                self.assertEqual(third.getresponse().status, 200)
                third.close()
            finally:
                first.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_owned_stale_socket_is_replaced_with_restricted_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "controller.sock")
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(path)
            stale.close()
            server = ControllerServer(path, Router())
            try:
                self.assertEqual(stat.S_IMODE(Path(path).lstat().st_mode), 0o660)
            finally:
                server.server_close()
            self.assertFalse(Path(path).exists())

    def test_regular_file_at_socket_path_is_never_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "controller.sock"
            path.write_text("do-not-remove")
            with self.assertRaises(FileExistsError):
                ControllerServer(str(path), Router())
            self.assertEqual(path.read_text(), "do-not-remove")


if __name__ == "__main__":
    unittest.main()
