"""Ingress page for app hosts with no live route."""

from __future__ import annotations

import http.client
import io
import json
import ssl
import sys
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

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

    def test_a_recent_cutover_marker_covers_the_gap_with_no_job(self) -> None:
        marks = {"kumba": 1000.0}
        at = lambda now: lambda: now  # noqa: E731
        state = lambda now: page.app_state("kumba-preview", {}.get, marks.get, clock=at(now))  # noqa: E731
        self.assertEqual(state(1000.0 + 60), page.STARTING)
        self.assertEqual(state(1000.0 + page.MARKER_SECONDS + 1), page.ABSENT)
        self.assertEqual(page.app_state("other", {}.get, marks.get, clock=at(1001.0)), page.ABSENT)
        stopped = {"kumba": {"ID": "kumba", "Status": "dead", "Stop": True}}
        self.assertEqual(
            page.app_state("kumba", stopped.get, marks.get, clock=at(1001.0)), page.STARTING
        )

    def test_a_running_job_needs_no_marker_and_a_broken_marker_read_is_unknown(self) -> None:
        jobs = {"kumba": {"ID": "kumba", "Status": "running", "Stop": False}}

        def broken(_slug):
            raise OSError("Nomad unreachable")

        self.assertEqual(page.app_state("kumba", jobs.get, broken), page.STARTING)
        self.assertEqual(page.app_state("kumba", {}.get, broken), page.UNKNOWN)

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


class MarkerReadTests(unittest.TestCase):
    def read(self, outcome):
        jobs = page.NomadJobs("https://nomad.test:4646", "token", ssl.create_default_context())
        with mock.patch.object(page.urllib.request, "urlopen", side_effect=[outcome]) as opened:
            since = jobs.updating_since("kumba")
        self.assertTrue(
            opened.call_args.args[0].full_url.endswith("/v1/var/platform/updating/kumba")
        )
        return since

    def test_reads_the_marker_time_and_treats_denied_or_missing_as_none(self) -> None:
        body = {"Path": "platform/updating/kumba", "ModifyTime": 1_700_000_000_500_000_000}
        self.assertEqual(self.read(io.BytesIO(json.dumps(body).encode())), 1_700_000_000.5)
        for code in (403, 404):
            error = urllib.error.HTTPError("https://nomad.test", code, "no", {}, None)
            self.assertIsNone(self.read(error))
        error = urllib.error.HTTPError("https://nomad.test", 500, "down", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            self.read(error)
        other = {"Path": "platform/updating/other", "ModifyTime": 1}
        with self.assertRaises(ValueError):
            self.read(io.BytesIO(json.dumps(other).encode()))


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
