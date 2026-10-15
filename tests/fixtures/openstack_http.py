"""Local Keystone/Nova/Glance/Neutron/Cinder server for provider evidence."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PROJECT = "00000000-0000-4000-8000-000000000000"
SERVER = "55555555-5555-4555-8555-555555555555"
IMAGE = "11111111-1111-4111-8111-111111111111"
PORT = "77777777-7777-4777-8777-777777777777"
VOLUME = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class Cloud:
    def __init__(self):
        self.calls = Counter()
        self.clients = set()
        self.tokens = []
        self.token_lifetime = 3600
        self.delay = {}
        self.responses = {}
        self.unauthorized = 0
        self.trickle = False
        self.slow_headers = False
        # The class cloud's Nova: Mitaka-era, newest microversion 2.25.
        self.nova_version = "2.25"
        self.nova_echo = None
        self.nova_requests = []
        self.port = 0
        self.scheme = "http"
        self.records = {
            "servers": [
                {
                    "id": SERVER,
                    "name": "example-admin",
                    "status": "ACTIVE",
                    "image": {"id": IMAGE},
                    "flavor": {"id": "small"},
                    "metadata": {"owned": "yes"},
                    "addresses": {"net": [{"addr": "10.0.0.2"}]},
                    "os-extended-volumes:volumes_attached": [
                        {"id": VOLUME, "delete_on_termination": False}
                    ],
                }
            ],
            "flavors": [{"id": "small", "name": "small.2c", "vcpus": 2, "ram": 4096, "disk": 20}],
            "images": [
                {
                    "id": IMAGE,
                    "name": "image",
                    "status": "active",
                    "owner": PROJECT,
                    "created_at": "2026-10-01T00:00:00Z",
                    "example_managed_by": "platform",
                }
            ],
            "ports": [
                {
                    "id": PORT,
                    "name": "example-admin-v4",
                    "device_id": SERVER,
                    "fixed_ips": [{"ip_address": "10.0.0.2"}],
                    "tenant_id": PROJECT,
                }
            ],
            "volumes": [
                {
                    "id": VOLUME,
                    "name": "data",
                    "volume_type": "fast",
                    "status": "in-use",
                    "size": 20,
                }
            ],
            "networks": [{"id": PORT, "name": "net", "router:external": True, "subnets": [PORT]}],
            "subnets": [{"id": PORT, "name": "subnet", "network_id": PORT, "ip_version": 4}],
            "routers": [{"id": PORT, "name": "router", "tenant_id": PROJECT}],
            "floatingips": [{"id": PORT, "floating_ip_address": "192.0.2.1", "tenant_id": PROJECT}],
            "security-groups": [{"id": PORT, "name": "worker", "tenant_id": PROJECT}],
            "security-group-rules": [{"id": PORT, "security_group_id": PORT, "tenant_id": PROJECT}],
            "types": [{"id": PORT, "name": "fast"}],
            "os-keypairs": [{"name": "operator", "public_key": "fixture"}],
        }

    @property
    def environment(self):
        return {
            "OS_AUTH_URL": f"{self.scheme}://127.0.0.1:{self.port}/v3",
            "OS_AUTH_TYPE": "password",
            "OS_USERNAME": "operator",
            "OS_PASSWORD": "fixture-password",
            "OS_PROJECT_NAME": "example-project",
            "OS_PROJECT_ID": PROJECT,
            "OS_REGION_NAME": "RegionA",
        }

    def token(self):
        token = f"private-token-{len(self.tokens)}"
        self.tokens.append(token)
        return token, {
            "token": {
                "methods": ["password"],
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=self.token_lifetime)
                ).isoformat(),
                "issued_at": datetime.now(UTC).isoformat(),
                "project": {
                    "id": PROJECT,
                    "name": "example-project",
                    "domain": {"id": "default", "name": "Default"},
                },
                "user": {
                    "id": PORT,
                    "name": "operator",
                    "domain": {"id": "default", "name": "Default"},
                },
                "catalog": [
                    {
                        "id": service,
                        "name": service,
                        "type": service,
                        "endpoints": [
                            {
                                "id": service,
                                "interface": "public",
                                "region": "RegionA",
                                "region_id": "RegionA",
                                "url": f"{self.scheme}://127.0.0.1:{self.port}/{prefix}",
                            }
                        ],
                    }
                    for service, prefix in (
                        ("compute", "v2.1"),
                        ("image", "v2"),
                        ("network", "v2.0"),
                        ("volumev3", f"v3/{PROJECT}"),
                    )
                ],
            }
        }


@contextmanager
def serve(cloud=None, *, tls_context=None):
    cloud = cloud or Cloud()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            import socket

            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                pass

        def handle_request(self):
            cloud.clients.add(self.client_address)
            path = urlsplit(self.path).path
            cloud.calls[(self.command, path)] += 1
            time.sleep(cloud.delay.get(path, 0))
            if (
                path not in {"/v3", "/v2.1", "/v3/auth/tokens"}
                and self.headers.get("X-Auth-Token") not in cloud.tokens
            ):
                return 401, {"message": "PRIVATE missing authentication"}, {}
            if self.command == "POST":
                payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if path == "/v3/auth/tokens" and path in cloud.responses:
                    return cloud.responses[path]
                if path == "/v3/auth/tokens":
                    cloud.auth_body = json.loads(payload)
                    token, document = cloud.token()
                    return 201, document, {"X-Subject-Token": token}
                if path.endswith("/action"):
                    cloud.console_body = json.loads(payload)
                    return 200, {"output": "fixture readiness\n"}, {}
            if cloud.unauthorized:
                cloud.unauthorized -= 1
                return 401, {"message": "PRIVATE provider error"}, {}
            if path == "/v2.1/":
                return (
                    200,
                    {
                        "version": {
                            "id": "v2.1",
                            "version": cloud.nova_version,
                            "min_version": "2.1",
                            "status": "CURRENT",
                        }
                    },
                    {},
                )
            if path.startswith("/v2.1/"):
                # Like Nova before 2.27, only the legacy header selects a version.
                requested = self.headers.get("X-OpenStack-Nova-API-Version", "2.1")
                cloud.nova_requests.append(requested)
                newest = tuple(int(part) for part in cloud.nova_version.split("."))
                if tuple(int(part) for part in requested.split(".")) > newest:
                    return 406, {"message": "PRIVATE unsupported version"}, {}
            if path in cloud.responses:
                response = cloud.responses[path]
                return (
                    response(parse_qs(urlsplit(self.path).query))
                    if callable(response)
                    else response
                )
            if path.endswith("/info/usage"):
                return (
                    200,
                    {"limits": {"image_count_total": 20}, "usage": {"image_count_total": 3}},
                    {},
                )
            if path.endswith("/os-extra_specs"):
                return 200, {"extra_specs": {}}, {}
            if "os-volume_attachments" in path:
                return (
                    200,
                    {
                        "volumeAttachments": [
                            {
                                "id": VOLUME,
                                "volumeId": VOLUME,
                                "device": "/dev/vdb",
                                "delete_on_termination": False,
                            }
                        ]
                    },
                    {},
                )
            if path.endswith("/limits"):
                return (
                    200,
                    {
                        "limits": {
                            "absolute": {
                                "totalCoresUsed": 2,
                                "maxTotalRAMSize": 4096,
                                "totalRAMUsed": 1024,
                                "totalSecurityGroupsUsed": 1,
                            }
                        }
                    },
                    {},
                )
            if "/quotas/" in path:
                return 200, {"quota": {"floatingip": 5, "port": 50}}, {}
            if "/os-quota-sets/" in path:
                return 200, {"quota_set": {"cores": {"in_use": 2, "limit": 20}}}, {}
            parts = path.split("/")
            for kind, rows in cloud.records.items():
                if kind not in parts:
                    continue
                reference = parts[-1]
                envelope = {
                    "security-groups": "security_groups",
                    "security-group-rules": "security_group_rules",
                    "types": "volume_types",
                    "os-keypairs": "keypairs",
                }.get(kind, kind)
                if reference in {kind, "detail"}:
                    query = parse_qs(urlsplit(self.path).query)
                    matching = [
                        row
                        for row in rows
                        if all(
                            str(row.get(key)) == values[0]
                            for key, values in query.items()
                            if key
                            in {"name", "visibility", "device_id", "network_id", "project_id"}
                        )
                    ]
                    if kind == "images" and "visibility" in query:
                        matching = [
                            row
                            for row in rows
                            if row.get("visibility", "private") == query["visibility"][0]
                        ]
                    if kind == "os-keypairs":
                        matching = [{"keypair": row} for row in matching]
                    return 200, {envelope: matching}, {}
                matching = [row for row in rows if str(row.get("id", row.get("name"))) == reference]
                if len(matching) == 1:
                    singular = {
                        "security-groups": "security_group",
                        "security-group-rules": "security_group_rule",
                        "types": "volume_type",
                        "os-keypairs": "keypair",
                        "floatingips": "floatingip",
                    }.get(kind, kind[:-1])
                    return 200, matching[0] if kind == "images" else {singular: matching[0]}, {}
            return 404, {"message": "PRIVATE missing resource"}, {}

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            status, document, headers = self.handle_request()
            if urlsplit(self.path).path.startswith("/v2.1/"):
                headers = {
                    **headers,
                    "X-OpenStack-Nova-API-Version": cloud.nova_echo
                    or self.headers.get("X-OpenStack-Nova-API-Version", "2.1"),
                }
            payload = document if isinstance(document, bytes) else json.dumps(document).encode()
            if cloud.slow_headers and self.command == "GET":
                try:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                    self.wfile.flush()
                    for _ in range(100):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
                return
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                if cloud.trickle and self.command == "GET":
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.01)
                else:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    cloud.port = server.server_port
    if tls_context is not None:
        cloud.scheme = "https"
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cloud
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
