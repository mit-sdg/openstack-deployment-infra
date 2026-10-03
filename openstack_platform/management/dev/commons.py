"""Loopback HTTPS double pinned to Commons authenticate contract bb78c5e."""

from __future__ import annotations

from typing import Any

from ..common import canonical, strict_json
from ..identity.client import credentials
from ..web.server import Reply, WebServer

USERS = {
    "alice": ("11111111-1111-4111-8111-111111111111", "Alice Student"),
    "bob": ("22222222-2222-4222-8222-222222222222", "Bob Student"),
    "carol": ("33333333-3333-4333-8333-333333333333", "Carol Student"),
    "taylor": ("44444444-4444-4444-8444-444444444444", "Taylor Instructor"),
}


class Commons(WebServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.passwords = {name: f"local-{name}-password" for name in USERS}
        self.override: tuple[int, bytes] | None = None
        self.delay_seconds = 0.0
        self.calls = 0
        super().__init__(*args, **kwargs)

    def csp(self) -> str:
        return "default-src 'none'; frame-ancestors 'none'"

    def handle(self, method: str, target: str, headers: dict[str, str], raw: bytes) -> Reply:
        import time

        if target == "/healthz" and method == "GET":
            return Reply(200, b'{"ready":true}')
        if target != "/api/auth/authenticate" or method != "POST":
            return Reply(404, b'{"error":"INTERNAL_ERROR"}')
        self.calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.override:
            status, body = self.override
            return Reply(status, body, headers=(("Cache-Control", "no-store"),))
        try:
            if headers.get("content-type") != "application/json":
                raise ValueError("invalid media type")
            value = credentials(strict_json(raw))
        except ValueError:
            return Reply(400, b'{"error":"INVALID_REQUEST"}')
        name = value["username"]
        if name not in self.passwords or value["password"] != self.passwords[name]:
            return Reply(401, b'{"error":"UNAUTHORIZED"}')
        if name == "carol":
            return Reply(403, b'{"error":"FORBIDDEN"}')
        subject, display = USERS[name]
        return Reply(
            200,
            canonical(
                {
                    "user": subject,
                    "username": name,
                    "displayName": display,
                    "email": name + "@example.com",
                }
            ).encode(),
            headers=(("Cache-Control", "no-store"),),
        )
