"""Actual Nix SDK/OSC regression for legacy Neutron security-group ownership.

The ordinary Python suite has no SDK dependency. package-smoke runs this file
with the exact patched rolePackages.python and requires these tests, using only
a loopback HTTP fixture. No credentials, project defaults, or adapter mocks.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HAVE_SDK = importlib.util.find_spec("openstack") is not None
HAVE_OSC = importlib.util.find_spec("openstackclient") is not None
if os.environ.get("PLATFORM_REQUIRE_OPENSTACK_PROJECTION") == "1" and not (HAVE_SDK and HAVE_OSC):
    raise RuntimeError("Nix package-smoke must provide the actual SDK and OSC packages")

FIXTURE = Path(
    os.environ.get(
        "PLATFORM_NEUTRON_PROJECTION_FIXTURE",
        str(Path(__file__).parent / "fixtures/openstack/neutron_security_group_tenant_only.json"),
    )
)


@unittest.skipUnless(HAVE_SDK and HAVE_OSC, "actual SDK/OSC projection runs in Nix package-smoke")
class NeutronSecurityGroupProjectionTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(FIXTURE.read_text())["security_group"]

    def cli(self, raw):
        calls = []
        expected_path = f"/v2.0/security-groups/{raw['id']}"

        class Neutron(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                path = urlsplit(self.path).path
                if path.rstrip("/") == "/v2.0":
                    body = {
                        "version": {
                            "id": "v2.0",
                            "status": "CURRENT",
                            "links": [
                                {
                                    "rel": "self",
                                    "href": f"http://127.0.0.1:{self.server.server_port}/v2.0/",
                                }
                            ],
                        }
                    }
                elif path == expected_path:
                    body = {"security_group": raw}
                else:
                    self.send_error(404)
                    return
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Neutron)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from openstackclient.shell import main; raise SystemExit(main())",
                        "security",
                        "group",
                        "show",
                        raw["id"],
                        "--format",
                        "json",
                    ],
                    env={
                        "PATH": os.defpath,
                        "HOME": directory,
                        "OS_AUTH_TYPE": "none",
                        "OS_NETWORK_ENDPOINT_OVERRIDE": f"http://127.0.0.1:{server.server_port}/v2.0",
                        "OS_NETWORK_API_VERSION": "2",
                    },
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected_path, calls)
            self.assertTrue(
                all(urlsplit(path).path in {"/v2.0", "/v2.0/", expected_path} for path in calls)
            )
            return json.loads(result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_actual_sdk_alias_preserves_authoritative_project_and_null(self):
        from openstack.network.v2.security_group import SecurityGroup

        self.assertNotIn("project_id", self.raw)
        resource = SecurityGroup.existing(**self.raw)
        self.assertEqual(resource.project_id, self.raw["tenant_id"])
        self.assertEqual(resource.tenant_id, self.raw["tenant_id"])
        for project in (None, "ffffffffffffffffffffffffffffffff"):
            resource = SecurityGroup.existing(**{**self.raw, "project_id": project})
            self.assertEqual(
                resource.project_id, project
            )  # Do not repair explicit null/wrong ownership.
        absent = {key: value for key, value in self.raw.items() if key != "tenant_id"}
        self.assertIsNone(SecurityGroup.existing(**absent).project_id)

    def test_actual_cli_projects_raw_tenant_owner_not_null_or_authenticated_default(self):
        result = self.cli(self.raw)
        self.assertEqual(result["id"], self.raw["id"])
        self.assertEqual(result["name"], self.raw["name"])
        self.assertEqual(result["project_id"], self.raw["tenant_id"])
        self.assertNotIn("tenant_id", result)  # OSC hides the deprecated SDK column.
        self.assertIsNone(result["created_at"])
        self.assertIsNone(result["revision_number"])
        self.assertIsNone(result["stateful"])
        foreign = copy.deepcopy(self.raw)
        foreign["tenant_id"] = "ffffffffffffffffffffffffffffffff"
        self.assertEqual(self.cli(foreign)["project_id"], foreign["tenant_id"])

    def test_actual_cli_does_not_invent_missing_ownership_or_replace_explicit_null(self):
        absent = {key: value for key, value in self.raw.items() if key != "tenant_id"}
        self.assertIsNone(self.cli(absent)["project_id"])
        self.assertIsNone(self.cli({**self.raw, "project_id": None})["project_id"])


if __name__ == "__main__":
    unittest.main()
