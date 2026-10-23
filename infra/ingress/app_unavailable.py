#!/usr/bin/env python3
"""Explain an app address that has no live route instead of a bare 404.

Traefik sends a request here only when no app router matches its host: the
app is being deployed or restarted, it has stopped, or no app has that name.
The exact Nomad job decides which page a visitor sees. Nomad's HTTP API looks
up exact job IDs, unlike the CLI's prefix matching.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STARTING, ABSENT, UNKNOWN = "starting", "absent", "unknown"
# The page's reload script looks for this header; any response without it
# came from the app itself.
MARKER = "X-Platform-Unavailable"
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
CACHE_SECONDS = 5.0
CACHE_ENTRIES = 1024
LOOKUP_SECONDS = 3.0
# A burst of requests for random names must not become a burst of Nomad calls.
LOOKUPS_AT_ONCE = 4

STYLE = """
:root{color-scheme:light dark;--bg:#f7f6f3;--card:#fff;--text:#1c1b1a;--muted:#5f5b55;--line:#e3e0da;--accent:#c4501b}
@media (prefers-color-scheme:dark){:root{--bg:#141312;--card:#1d1c1a;--text:#eeebe6;--muted:#a8a29a;--line:#33302c;--accent:#ee7a45}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:100%;max-width:460px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:32px 28px}
.host{display:inline-block;margin:0 0 18px;padding:3px 10px;border:1px solid var(--line);border-radius:999px;color:var(--muted);font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
h1{margin:0 0 10px;font-size:22px;line-height:1.3}
p{margin:0 0 12px;color:var(--muted)}
.wait{display:flex;align-items:center;gap:10px;margin-top:20px;font-size:14px;color:var(--muted)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--accent);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.25;transform:scale(.8)}50%{opacity:1;transform:scale(1)}}
@media (prefers-reduced-motion:reduce){.dot{animation:none}}
footer{margin-top:22px;padding-top:16px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
a{color:var(--accent)}
""".strip()

SCRIPT = """
(function(){var b=document.body,s=Number(b.dataset.every)||15;
function check(){fetch(location.href,{cache:"no-store",headers:{Accept:"text/html"}}).then(function(r){
if(!r.headers.has("X-Platform-Unavailable")){location.reload();}}).catch(function(){}).then(function(){setTimeout(check,s*1000);});}
setTimeout(check,s*1000);})();
""".strip()


def _digest(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


POLICY = (
    "default-src 'none'; "
    f"style-src '{_digest(STYLE)}'; "
    f"script-src '{_digest(SCRIPT)}'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def app_label(host: str | None, domain: str) -> str | None:
    """The single DNS label before the platform domain, or None."""
    if not host:
        return None
    host = host.strip().lower()
    name, _, port = host.rpartition(":")
    if name and port.isdigit():
        host = name
    suffix = "." + domain
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    return label if _LABEL.fullmatch(label) else None


def job_ids(label: str) -> tuple[str, str]:
    """A preview host belongs to the same app; its job is the app's candidate."""
    slug = label.removesuffix("-preview") or label
    return slug, f"{slug}-candidate"


class NomadJobs:
    """Exact job reads with the ingress's read-only Nomad identity."""

    def __init__(self, address: str, token: str, context: ssl.SSLContext) -> None:
        self.address = address.rstrip("/")
        self.token = token
        self.context = context

    def get(self, job_id: str) -> dict[str, object] | None:
        request = urllib.request.Request(
            f"{self.address}/v1/job/{urllib.parse.quote(job_id, safe='')}",
            headers={"X-Nomad-Token": self.token, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=LOOKUP_SECONDS, context=self.context
            ) as response:
                value = json.loads(response.read(1_048_576))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        if not isinstance(value, dict) or value.get("ID") != job_id:
            raise ValueError("Nomad returned a different job")
        return value


def app_state(label: str, read_job: Callable[[str], dict[str, object] | None]) -> str:
    """STARTING while either deployment job is meant to run, else ABSENT."""
    try:
        jobs = [read_job(job_id) for job_id in job_ids(label)]
    except Exception:
        return UNKNOWN
    for job in jobs:
        if job is not None and job.get("Stop") is not True and job.get("Status") != "dead":
            return STARTING
    return ABSENT


class StateCache:
    def __init__(
        self,
        lookup: Callable[[str], str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lookup = lookup
        self._clock = clock
        self._guard = threading.Lock()
        self._slots = threading.BoundedSemaphore(LOOKUPS_AT_ONCE)
        self._entries: dict[str, tuple[float, str]] = {}

    def get(self, label: str) -> str:
        now = self._clock()
        with self._guard:
            entry = self._entries.get(label)
            if entry is not None and entry[0] > now:
                return entry[1]
        if not self._slots.acquire(timeout=LOOKUP_SECONDS):
            return UNKNOWN
        try:
            state = self._lookup(label)
        finally:
            self._slots.release()
        with self._guard:
            if len(self._entries) >= CACHE_ENTRIES:
                self._entries.clear()
            self._entries[label] = (self._clock() + CACHE_SECONDS, state)
        return state


def page(state: str, host: str, platform_name: str, portal: str) -> tuple[int, str, dict[str, str]]:
    """Status, HTML body, and extra headers for one state."""
    shown = html.escape(host)
    name = html.escape(platform_name)
    link = f'<a href="{html.escape(portal, quote=True)}">{name}</a>'
    if state == STARTING:
        status, every, title = 503, 10, "This app is updating"
        body = (
            "<p>It's being deployed or restarted, so it's offline for a moment. "
            "It should be back within a few minutes.</p>"
            '<div class="wait"><span class="dot"></span>'
            "This page will reload by itself when the app is back.</div>"
            f"<footer>If it doesn't come back, the app's owner can check its logs on {link}.</footer>"
        )
    elif state == ABSENT:
        status, every, title = 404, 30, "No app is running here"
        body = (
            "<p>Nothing is running at this address right now. The app may have been "
            "stopped, or the address may be mistyped.</p>"
            f"<footer>Apps for this class are hosted on {link}.</footer>"
        )
    else:
        status, every, title = 503, 15, "This app isn't available right now"
        body = (
            "<p>It isn't answering at the moment. Please try again in a few minutes.</p>"
            '<div class="wait"><span class="dot"></span>'
            "This page will reload by itself when the app is back.</div>"
            f"<footer>Apps for this class are hosted on {link}.</footer>"
        )
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f'<body data-every="{every}"><main><div class="host">{shown}</div>'
        f"<h1>{html.escape(title)}</h1>{body}</main><script>{SCRIPT}</script></body></html>"
    )
    headers = {"Retry-After": "30"} if status == 503 else {}
    return status, document, headers


def make_handler(
    domain: str, platform_name: str, portal: str, states: StateCache
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "platform"
        sys_version = ""

        def _answer(self, include_body: bool) -> None:
            host = self.headers.get("Host")
            label = app_label(host, domain)
            state = ABSENT if label is None else states.get(label)
            shown = f"{label}.{domain}" if label is not None else domain
            status, document, extra = page(state, shown, platform_name, portal)
            accept = self.headers.get("Accept", "")
            if "text/html" in accept:
                payload, kind = document.encode(), "text/html; charset=utf-8"
            else:
                code = {STARTING: "APP_STARTING", ABSENT: "APP_NOT_FOUND"}.get(
                    state, "APP_UNAVAILABLE"
                )
                payload = json.dumps({"error": code}).encode()
                kind = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(MARKER, state)
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            if include_body:
                self.wfile.write(payload)

        def do_GET(self) -> None:
            self._answer(True)

        def do_HEAD(self) -> None:
            self._answer(False)

        def do_POST(self) -> None:
            self._answer(True)

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_POST

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def read_token(path: Path) -> str:
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    token = values.get("TRAEFIK_PROVIDERS_NOMAD_ENDPOINT_TOKEN") or values.get("NOMAD_TOKEN")
    if not token:
        raise SystemExit("no Nomad token in the ingress credential")
    return token


def main() -> int:
    env = os.environ
    context = ssl.create_default_context(cafile=env["NOMAD_CACERT"])
    context.load_cert_chain(env["NOMAD_CLIENT_CERT"], env["NOMAD_CLIENT_KEY"])
    credential = Path(env["CREDENTIALS_DIRECTORY"]) / "traefik.env"
    jobs = NomadJobs(env["NOMAD_ADDR"], read_token(credential), context)
    states = StateCache(lambda label: app_state(label, jobs.get))
    domain = env["PLATFORM_DOMAIN"]
    handler = make_handler(domain, env["PLATFORM_NAME"], f"https://{domain}/", states)
    server = ThreadingHTTPServer(("127.0.0.1", int(env["APP_UNAVAILABLE_PORT"])), handler)
    server.daemon_threads = True
    print(f"app-unavailable listening on 127.0.0.1:{env['APP_UNAVAILABLE_PORT']}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
