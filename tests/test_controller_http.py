from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from platform_cli.controller_http import ControllerServer, HttpError, Response, Router


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


if __name__ == "__main__":
    unittest.main()
