"""Ingress page for app hosts with no live route."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from ingress import app_unavailable as page  # noqa: E402

DOMAIN = "apps.example.com"


class HostTests(unittest.TestCase):
    def test_only_one_valid_label_under_the_domain_names_an_app(self) -> None:
        self.assertEqual(page.app_label("kumba.apps.example.com", DOMAIN), "kumba")
        self.assertEqual(page.app_label("Kumba.Apps.Example.com:443", DOMAIN), "kumba")
        for host in (
            None,
            "",
            "apps.example.com",
            "a.b.apps.example.com",
            "-bad.apps.example.com",
            "kumba.example.org",
            "kumba.apps.example.com.evil.org",
        ):
            with self.subTest(host=host):
                self.assertIsNone(page.app_label(host, DOMAIN))

    def test_preview_hosts_belong_to_their_app(self) -> None:
        self.assertEqual(page.job_ids("kumba"), ("kumba", "kumba-candidate"))
        self.assertEqual(page.job_ids("kumba-preview"), ("kumba", "kumba-candidate"))


class StateTests(unittest.TestCase):
    def test_a_job_meant_to_run_is_starting_and_no_job_is_absent(self) -> None:
        jobs = {"kumba-candidate": {"ID": "kumba-candidate", "Status": "pending", "Stop": False}}
        self.assertEqual(page.app_state("kumba", jobs.get), page.STARTING)
        self.assertEqual(page.app_state("other", jobs.get), page.ABSENT)
        stopped = {"kumba": {"ID": "kumba", "Status": "dead", "Stop": True}}
        self.assertEqual(page.app_state("kumba", stopped.get), page.ABSENT)

    def test_a_failed_lookup_is_unknown_rather_than_absent(self) -> None:
        def broken(_job_id):
            raise OSError("Nomad unreachable")

        self.assertEqual(page.app_state("kumba", broken), page.UNKNOWN)

    def test_lookups_are_cached_briefly_and_the_cache_is_bounded(self) -> None:
        now = [0.0]
        calls: list[str] = []

        def lookup(label):
            calls.append(label)
            return page.ABSENT

        cache = page.StateCache(lookup, clock=lambda: now[0])
        cache.get("one")
        cache.get("one")
        self.assertEqual(calls, ["one"])
        now[0] += page.CACHE_SECONDS + 1
        cache.get("one")
        self.assertEqual(calls, ["one", "one"])
        for index in range(page.CACHE_ENTRIES + 5):
            cache.get(f"app{index}")
        self.assertLessEqual(len(cache._entries), page.CACHE_ENTRIES)


class HandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        states = {"kumba": page.STARTING, "gone": page.ABSENT, "flaky": page.UNKNOWN}
        cache = page.StateCache(lambda label: states.get(label, page.ABSENT))
        handler = page.make_handler(DOMAIN, "6.1040 Apps", f"https://{DOMAIN}/", cache)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, host: str, accept: str = "text/html", method: str = "GET"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request(method, "/some/path", headers={"Host": host, "Accept": accept})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_a_deploying_app_gets_a_reloading_page_that_says_it_will_be_back(self) -> None:
        response, body = self.request(f"kumba.{DOMAIN}")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.getheader("Retry-After"), "30")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader(page.MARKER), page.STARTING)
        self.assertIn("text/html", response.getheader("Content-Type"))
        text = body.decode()
        self.assertIn("This app is updating", text)
        self.assertIn(f"kumba.{DOMAIN}", text)
        self.assertIn("reload by itself", text)
        # The inline style and script are allowed by hash, nothing else is.
        policy = response.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", policy)
        self.assertIn(page._digest(page.SCRIPT), policy)

    def test_an_unknown_name_is_a_plain_404_and_api_clients_get_json(self) -> None:
        response, body = self.request(f"gone.{DOMAIN}")
        self.assertEqual(response.status, 404)
        self.assertIsNone(response.getheader("Retry-After"))
        self.assertIn("No app is running here", body.decode())
        response, body = self.request(f"kumba.{DOMAIN}", accept="application/json")
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(body), {"error": "APP_STARTING"})
        response, body = self.request(f"flaky.{DOMAIN}", accept="*/*", method="POST")
        self.assertEqual((response.status, json.loads(body)), (503, {"error": "APP_UNAVAILABLE"}))

    def test_head_has_headers_but_no_body_and_bad_hosts_never_look_up(self) -> None:
        response, body = self.request(f"kumba.{DOMAIN}", method="HEAD")
        self.assertEqual((response.status, body), (503, b""))
        response, body = self.request("evil.org")
        self.assertEqual(response.status, 404)
        self.assertNotIn("evil.org", body.decode())


if __name__ == "__main__":
    unittest.main()
