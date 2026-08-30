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
                    with self.assertRaises((ConnectionError, http.client.RemoteDisconnected)):
                        connection.request("GET", "/v1/health")
                        connection.getresponse()
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
                while not server._peer_counts:  # noqa: SLF001 - security invariant probe
                    time.sleep(0.01)
                second = UnixHTTPConnection(path)
                with self.assertRaises((ConnectionError, http.client.RemoteDisconnected)):
                    second.request("GET", "/v1/health")
                    second.getresponse()
                second.close()
                first.close()
                deadline = time.monotonic() + 2
                while server._peer_counts and time.monotonic() < deadline:  # noqa: SLF001
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
