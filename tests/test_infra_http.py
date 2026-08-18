from __future__ import annotations

import unittest
from unittest import mock

from infra.lib import http


class _Response:
    status = 200
    headers = {}

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, *_args: object, **_kwargs: object) -> _Response:
        return self.response


class BoundedHttpTests(unittest.TestCase):
    def test_response_ceiling_is_enforced_without_following_redirects(self) -> None:
        with mock.patch.object(
            http.urllib.request,
            "build_opener",
            return_value=_Opener(_Response(b"abcd")),
        ):
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                http.bounded_request("https://example.invalid/health", response_limit=3)
        self.assertIsNone(
            http._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil")
        )

    def test_json_rejects_nonfinite_values(self) -> None:
        with mock.patch.object(
            http.urllib.request,
            "build_opener",
            return_value=_Opener(_Response(b'{"ok":NaN}')),
        ):
            with self.assertRaisesRegex(RuntimeError, "malformed JSON"):
                http.bounded_json("https://example.invalid/status")

    def test_unbounded_limits_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            http.bounded_request("https://example.invalid/status", response_limit=17 * 1024 * 1024)
        with self.assertRaises(ValueError):
            http.bounded_request("https://example.invalid/status", timeout_seconds=121)


if __name__ == "__main__":
    unittest.main()
