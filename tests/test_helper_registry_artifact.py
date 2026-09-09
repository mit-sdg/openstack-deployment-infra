from __future__ import annotations

import hashlib
import json
import unittest

from openstack_platform.helper.main import HelperActionError
from openstack_platform.helper.registry_artifact import verify_image
from openstack_platform.runtime import HttpResult


class RegistryArtifactTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.status = 200
        self.bad_content = False
        self.blob = "sha256:" + "a" * 64
        self.manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": self.blob, "size": 12},
                "layers": [],
            }
        ).encode()
        self.digest = "sha256:" + hashlib.sha256(self.manifest).hexdigest()
        self.index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [{"digest": self.digest, "size": len(self.manifest)}],
            }
        ).encode()
        self.index_digest = "sha256:" + hashlib.sha256(self.index).hexdigest()
        self.image = "storage.internal:5000/projects/commons/app@" + self.index_digest

    def http(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.assertIn("https://storage.internal:5000/v2/projects/commons/app/", url)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertLessEqual(kwargs["timeout_seconds"], 10)
        if kwargs["method"] == "HEAD":
            return HttpResult(
                self.status, {"docker-content-digest": self.blob, "content-length": "12"}, b""
            )
        self.assertEqual(kwargs["method"], "GET")
        body = self.index if url.endswith(self.index_digest) else self.manifest
        return HttpResult(200, {}, b"corrupt" if self.bad_content else body)

    def verify(self, image=None):
        return verify_image(
            "storage.internal:5000",
            "commons",
            image or self.image,
            authorization="Basic sentinel-secret",
            ssl_context=object(),
            http=self.http,
        )

    def test_read_only_index_manifest_and_blob_availability_returns_no_content_or_credentials(self):
        result = self.verify()
        self.assertEqual(result, {"slug": "commons", "image": self.image, "available": True})
        self.assertNotIn("sentinel-secret", json.dumps(result))
        self.assertEqual([kwargs["method"] for _, kwargs in self.calls], ["GET", "GET", "HEAD"])

    def test_missing_or_redirected_blob_fails_closed(self):
        for status in (404, 302, 401):
            self.status = status
            with self.subTest(status=status), self.assertRaises(HelperActionError) as raised:
                self.verify()
            self.assertNotIn("sentinel-secret", str(raised.exception))

    def test_wrong_manifest_digest_fails_before_any_blob_probe(self):
        self.bad_content = True
        with self.assertRaises(HelperActionError):
            self.verify()
        self.assertEqual(len(self.calls), 1)

    def test_foreign_repository_or_host_cannot_select_a_network_destination(self):
        for image in (
            self.image.replace("commons", "other"),
            self.image.replace("storage.internal", "attacker.test"),
        ):
            with self.subTest(image=image), self.assertRaises(HelperActionError):
                self.verify(image)
        self.assertEqual(self.calls, [])
