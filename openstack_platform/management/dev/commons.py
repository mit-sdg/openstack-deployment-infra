"""Loopback HTTPS double pinned to the Commons Connect contract: approve, then redeem.

Every sign-in carries an S256 challenge, and a code redeems only with its verifier.
"""

from __future__ import annotations

import html
import re
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from ..common import canonical, strict_json
from ..config import origin
from ..identity.client import CODE_VERIFIER, code_challenge
from ..web.server import Reply, WebServer

USERS = {
    "alice": ("11111111-1111-4111-8111-111111111111", "Alice Student"),
    "bob": ("22222222-2222-4222-8222-222222222222", "Bob Student"),
    "carol": ("33333333-3333-4333-8333-333333333333", "Carol Student"),
    "taylor": ("44444444-4444-4444-8444-444444444444", "Taylor Instructor"),
}
# Carol's class account is archived: Commons still signs her in to approve,
# but refuses to redeem her codes, like any other refusal.
ARCHIVED = {"carol"}


class Commons(WebServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.passwords = {name: f"local-{name}-password" for name in USERS}
        # Who /connect approves at once, as if they approved this app before.
        # None shows a sign-in and approval page, as for a first sign-in.
        self.approve: str | None = "alice"
        self.deny = False
        self.codes: dict[str, tuple[str, str, float, str]] = {}
        self.code_lock = threading.Lock()
        self.override: tuple[int, bytes] | None = None
        self.delay_seconds = 0.0
        self.calls = 0
        super().__init__(*args, **kwargs)

    def csp(self) -> str:
        return "default-src 'none'; frame-ancestors 'none'"

    def issue(self, name: str, app: str, challenge: str) -> str:
        """Issue a single-use code for one person, app and challenge, valid for 60 s."""
        code = f"{uuid.uuid4()}.{secrets.token_urlsafe(32)}"
        with self.code_lock:
            self.codes[code] = (name, app, time.monotonic() + 60, challenge)
        return code

    def handle(self, method: str, target: str, headers: dict[str, str], raw: bytes) -> Reply:
        parsed = urlsplit(target)
        if target == "/healthz" and method == "GET":
            return Reply(200, b'{"ready":true}')
        if parsed.path == "/connect" and method in {"GET", "POST"}:
            return self.connect(method, parsed.query, headers, raw)
        if target != "/api/connect/redeem" or method != "POST":
            return Reply(404, b'{"error":"INTERNAL_ERROR"}')
        return self.redeem(headers, raw)

    def connect(self, method: str, query: str, headers: dict[str, str], raw: bytes) -> Reply:
        try:
            if method == "POST" and headers.get("content-type") != (
                "application/x-www-form-urlencoded"
            ):
                raise ValueError("invalid media type")
            fields = parse_qs(
                raw.decode() if method == "POST" else query,
                keep_blank_values=True,
                strict_parsing=bool(raw or query),
            )
            values = {name: items[0] for name, items in fields.items() if len(items) == 1}
            app, state = values["app"], values["state"]
            challenge = values["code_challenge"]
            if (
                len(values) != len(fields)
                or app != app.lower()
                or origin(app, development=True) != app
                or not re.fullmatch(r"[A-Za-z0-9._~-]{16,256}", state)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge)
                or values["code_challenge_method"] != "S256"
            ):
                raise ValueError("invalid app, state or challenge")
        except (KeyError, ValueError, UnicodeError):
            return Reply(
                400,
                b"<!doctype html><title>Commons</title><p>This app can't use Commons sign-in.",
                "text/html; charset=utf-8",
            )
        callback = app + "/auth/commons/callback?"
        if self.deny or values.get("decision") == "cancel":
            return self.send(callback + urlencode({"error": "access_denied", "state": state}))
        if method == "GET" and self.approve is not None:
            code = self.issue(self.approve, app, challenge)
            return self.send(callback + urlencode({"code": code, "state": state}))
        name = values.get("username", "")
        if (
            method == "POST"
            and name in self.passwords
            and values.get("password") == self.passwords[name]
        ):
            code = self.issue(name, app, challenge)
            return self.send(callback + urlencode({"code": code, "state": state}))
        return Reply(
            401 if method == "POST" else 200,
            self.page(app, state, challenge, failed=method == "POST").encode(),
            "text/html; charset=utf-8",
        )

    @staticmethod
    def send(location: str) -> Reply:
        return Reply(303, b"", "", (("Location", location),))

    @staticmethod
    def page(app: str, state: str, challenge: str, *, failed: bool) -> str:
        host = html.escape(urlsplit(app).netloc)
        hidden = "".join(
            f'<input type="hidden" name="{name}" value="{html.escape(value)}">'
            for name, value in (
                ("app", app),
                ("state", state),
                ("code_challenge", challenge),
                ("code_challenge_method", "S256"),
            )
        )
        return (
            "<!doctype html><html lang=en><title>Commons (development)</title>"
            f"<h1>Sign in to {host} with Commons</h1>"
            + ("<p role=alert>Username or password is incorrect.</p>" if failed else "")
            + f"<p>{host} will learn your name, username and email.</p>"
            '<form method="post" action="/connect">'
            + hidden
            + '<p><label for="username">Username</label> '
            '<input id="username" name="username" autocomplete="off"></p>'
            '<p><label for="password">Password</label> '
            '<input id="password" name="password" type="password"></p>'
            '<button name="decision" value="allow">Allow</button> '
            '<button name="decision" value="cancel">Cancel</button>'
            "</form></html>"
        )

    def redeem(self, headers: dict[str, str], raw: bytes) -> Reply:
        self.calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.override:
            status, body = self.override
            return Reply(status, body, headers=(("Cache-Control", "no-store"),))
        try:
            if headers.get("content-type") != "application/json":
                raise ValueError("invalid media type")
            value = strict_json(raw)
            if (
                not isinstance(value, dict)
                or not {"code", "app"} <= set(value) <= {"code", "app", "code_verifier"}
                or not all(isinstance(item, str) for item in value.values())
            ):
                raise ValueError("invalid redeem fields")
        except (ValueError, UnicodeError):
            return Reply(
                400, b'{"error":"INVALID_REQUEST"}', headers=(("Cache-Control", "no-store"),)
            )
        # Like Commons, spend the code first; a missing, wrong or malformed
        # verifier then gets the same refusal as every other failure.
        with self.code_lock:
            issued = self.codes.pop(value["code"], None)
        verifier = value.get("code_verifier", "")
        if (
            issued is None
            or issued[1] != value["app"]
            or issued[2] <= time.monotonic()
            or not CODE_VERIFIER.fullmatch(verifier)
            or code_challenge(verifier) != issued[3]
            or issued[0] in ARCHIVED
        ):
            return Reply(
                400, b'{"error":"CONNECT_CODE_INVALID"}', headers=(("Cache-Control", "no-store"),)
            )
        subject, display = USERS[issued[0]]
        return Reply(
            200,
            canonical(
                {
                    "user": subject,
                    "username": issued[0],
                    "displayName": display,
                    "email": issued[0] + "@example.com",
                }
            ).encode(),
            headers=(("Cache-Control", "no-store"),),
        )
