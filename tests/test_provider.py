from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from openstack_platform import openstack, provider, runtime
from openstack_platform.provider_api import API, MAX_RESPONSE, ProviderError
from openstack_platform.provider_service import Server, load_credentials
from tests.fixtures.openstack_http import IMAGE, PORT, PROJECT, SERVER, VOLUME, serve


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.context = serve()
        self.cloud = self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.api = API(self.cloud.environment, allow_http=True)

    def read(self, action, params=None, seconds=2):
        return self.api.execute(action, params or {}, time.monotonic() + seconds)

    def test_authentication_and_connection_reuse(self):
        for _ in range(6):
            self.assertEqual(self.read("flavor.show", {"reference": "small"})["ram"], 4096)
        self.assertEqual(len(self.cloud.tokens), 1)
        self.assertEqual(len(self.cloud.clients), 1)
        self.assertEqual(self.cloud.calls[("GET", "/v2.1/flavors/small")], 6)
        self.assertEqual(self.cloud.auth_body["auth"]["scope"]["project"]["id"], PROJECT)

    def test_nova_microversion_is_negotiated_and_verified(self):
        self.assertEqual(self.read("flavor.show", {"reference": "small"})["ram"], 4096)
        self.assertEqual(self.cloud.nova_requests, ["2.25"])
        self.assertEqual(self.cloud.calls[("GET", "/v2.1/")], 1)
        self.read("flavor.show", {"reference": "small"})
        self.assertEqual(self.cloud.calls[("GET", "/v2.1/")], 1)
        self.cloud.nova_version = "2.90"
        self.api = API(self.cloud.environment, allow_http=True)
        self.read("flavor.show", {"reference": "small"})
        self.assertEqual(self.cloud.nova_requests[-1], "2.46")
        self.cloud.nova_echo = "2.1"
        with self.assertRaises(ProviderError):
            self.read("flavor.show", {"reference": "small"})
        self.cloud.nova_echo = None
        for newest in ("2.1", "2.2"):
            self.cloud.nova_version = newest
            self.api = API(self.cloud.environment, allow_http=True)
            with self.assertRaises(ProviderError):
                self.read("flavor.show", {"reference": "small"})

    def test_slow_response_may_use_the_whole_deadline(self):
        self.read("flavor.show", {"reference": "small"})
        self.cloud.delay["/v2.1/flavors/small"] = 5.5
        self.assertEqual(self.read("flavor.show", {"reference": "small"}, seconds=10)["ram"], 4096)
        with self.assertRaises(ProviderError):
            self.read("flavor.show", {"reference": "small"}, seconds=2)

    def test_refresh_before_expiry_and_revocation(self):
        self.cloud.token_lifetime = 30
        self.read("identity")
        self.cloud.token_lifetime = 3600
        self.read("identity")
        self.assertEqual(len(self.cloud.tokens), 2)
        self.cloud.unauthorized = 1
        self.read("flavor.show", {"reference": "small"})
        self.assertEqual(len(self.cloud.tokens), 3)
        self.cloud.unauthorized = 2
        with self.assertRaises(ProviderError):
            self.read("flavor.show", {"reference": "small"})
        self.assertEqual(len(self.cloud.tokens), 4)

    def test_expired_issued_token_cannot_verify_project_identity(self):
        self.cloud.token_lifetime = -1
        with self.assertRaises(ProviderError):
            self.read("identity")
        self.cloud.token_lifetime = 3600
        self.assertEqual(self.read("identity")["project_id"], PROJECT)
        self.assertEqual(len(self.cloud.tokens), 2)

    def test_application_credentials(self):
        env = {
            **self.cloud.environment,
            "OS_AUTH_TYPE": "v3applicationcredential",
            "OS_APPLICATION_CREDENTIAL_ID": PORT,
            "OS_APPLICATION_CREDENTIAL_SECRET": "fixture-secret",
        }
        self.api = API(env, allow_http=True)
        self.read("identity")
        self.assertEqual(
            self.cloud.auth_body["auth"]["identity"]["methods"], ["application_credential"]
        )

    def test_concurrent_reads_share_one_token(self):
        with ThreadPoolExecutor(max_workers=16) as pool:
            values = list(
                pool.map(lambda _: self.read("flavor.show", {"reference": "small"}), range(32))
            )
        self.assertEqual(len(values), 32)
        self.assertEqual(len(self.cloud.tokens), 1)
        self.assertLessEqual(len(self.cloud.clients), 8)

    def test_endpoints_and_metadata_projections(self):
        cases = {
            "server": SERVER,
            "flavor": "small",
            "image": IMAGE,
            "port": PORT,
            "volume": VOLUME,
            "network": PORT,
            "subnet": PORT,
            "router": PORT,
            "floating_ip": PORT,
            "security_group": PORT,
            "security_group_rule": PORT,
            "volume_type": PORT,
            "keypair": "operator",
        }
        for kind, reference in cases.items():
            with self.subTest(kind=kind):
                shown = self.read(kind + ".show", {"reference": reference})
                self.assertTrue(shown)
                self.assertEqual(len(self.read(kind + ".list")), 1)
        self.assertEqual(
            self.read("server.show", {"reference": SERVER})["properties"], {"owned": "yes"}
        )
        self.assertEqual(
            self.read("image.show", {"reference": IMAGE})["properties"],
            {"example_managed_by": "platform"},
        )
        self.assertEqual(self.read("port.show", {"reference": PORT})["project_id"], PROJECT)
        self.assertEqual(self.read("volume.show", {"reference": VOLUME})["type"], "fast")
        self.assertEqual(
            self.read("attachments", {"reference": SERVER})[0]["delete_on_termination"], False
        )
        self.assertIn("readiness", self.read("console", {"reference": SERVER, "lines": 2000}))
        self.assertEqual(self.cloud.console_body, {"os-getConsoleOutput": {"length": 2000}})

    def test_usage_quota_limits_and_default_console_projection(self):
        self.assertEqual(self.read("image.usage")["usage"]["image_count_total"], 3)
        quota = self.read("quota", {"network": True})
        self.assertIn({"Resource": "floating_ips", "Limit": 5}, quota)
        self.assertEqual(self.read("quota", {"usage": True})["cores"], {"in_use": 2, "limit": 20})
        self.assertEqual(
            {row["Name"]: row["Value"] for row in self.read("limits")},
            {
                "total_cores_used": 2,
                "max_total_ram_size": 4096,
                "total_ram_used": 1024,
                "total_security_groups_used": 1,
                "security_groups_used": 1,
            },
        )
        self.read("console", {"reference": SERVER, "lines": None})
        self.assertEqual(self.cloud.console_body, {"os-getConsoleOutput": {}})
        self.assertIsNone(provider.parse(("console", "log", "show", SERVER)).params["lines"])

    def test_name_resolution_and_ambiguity(self):
        self.assertEqual(self.read("flavor.show", {"reference": "small.2c"})["id"], "small")
        self.cloud.records["flavors"].append(
            {**self.cloud.records["flavors"][0], "id": "duplicate"}
        )
        with self.assertRaises(ProviderError):
            self.read("flavor.show", {"reference": "small.2c"})

    def test_binary_image_download_reuses_auth_and_streams_with_a_limit(self):
        payload = b"qcow2-fixture" * 200_000
        self.cloud.responses[f"/v2/images/{IMAGE}/file"] = (200, payload, {})
        with tempfile.TemporaryFile() as output:
            self.api.download_image(
                IMAGE, output, deadline=time.monotonic() + 2, maximum_bytes=len(payload)
            )
            output.seek(0)
            self.assertEqual(output.read(), payload)
        self.assertEqual(len(self.cloud.tokens), 1)
        with (
            tempfile.TemporaryFile() as output,
            self.assertRaisesRegex(ProviderError, "safety limit"),
        ):
            self.api.download_image(IMAGE, output, deadline=time.monotonic() + 2, maximum_bytes=100)
        self.assertEqual(len(self.cloud.tokens), 1)
        self.cloud.unauthorized = 1
        with tempfile.TemporaryFile() as output:
            self.api.download_image(IMAGE, output, deadline=time.monotonic() + 2)
            output.seek(0)
            self.assertEqual(output.read(), payload)
        self.assertEqual(len(self.cloud.tokens), 2)
        self.cloud.responses[f"/v2/images/{IMAGE}/file"] = (500, b"PRIVATE error", {})
        with tempfile.TemporaryFile() as output:
            with self.assertRaises(ProviderError):
                self.api.download_image(IMAGE, output, deadline=time.monotonic() + 2)
            self.assertEqual(output.tell(), 0)

    def test_size_limits_for_auth_resources_and_rows(self):
        self.cloud.responses["/v2.1/flavors/small"] = (200, b"x" * (MAX_RESPONSE + 1), {})
        with self.assertRaisesRegex(ProviderError, "safety limit"):
            self.read("flavor.show", {"reference": "small"})
        self.cloud.records["flavors"] = [{"id": str(index)} for index in range(501)]
        with self.assertRaisesRegex(ProviderError, "safety limit"):
            self.read("flavor.list")

    def test_provider_errors_malformed_json_and_redirects_are_withheld(self):
        for status, payload, headers in (
            (500, {"secret": "PRIVATE"}, {}),
            (200, b"PRIVATE malformed", {}),
            (302, {"secret": "PRIVATE"}, {"Location": "https://attacker.invalid/"}),
        ):
            with self.subTest(status=status):
                self.cloud.responses["/v2.1/flavors/small"] = (status, payload, headers)
                with self.assertRaises(ProviderError) as caught:
                    self.read("flavor.show", {"reference": "small"})
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    def test_unrequested_compression_cannot_bypass_the_byte_limit(self):
        self.cloud.responses["/v2.1/flavors/small"] = (
            200,
            {"PRIVATE": "payload"},
            {"Content-Encoding": "gzip"},
        )
        with self.assertRaises(ProviderError):
            self.read("flavor.show", {"reference": "small"})

    def test_deadlines_bound_auth_and_slow_response(self):
        self.cloud.delay["/v3/auth/tokens"] = 0.3
        before = time.monotonic()
        with self.assertRaises(ProviderError):
            self.read("identity", seconds=0.05)
        self.assertLess(time.monotonic() - before, 0.25)
        self.cloud.delay.clear()
        self.read("identity")
        self.cloud.trickle = True
        before = time.monotonic()
        with self.assertRaises(ProviderError):
            self.read("flavor.show", {"reference": "small"}, seconds=0.05)
        self.assertLess(time.monotonic() - before, 0.25)

    def test_trickled_headers_cannot_extend_the_absolute_deadline(self):
        self.read("identity")
        self.cloud.slow_headers = True
        started = time.monotonic()
        with self.assertRaises(ProviderError):
            self.read("flavor.show", {"reference": "small"}, seconds=0.08)
        self.assertLess(time.monotonic() - started, 0.4)
        self.cloud.slow_headers = False
        # The interrupted connection must not poison the pool or cached token.
        self.assertEqual(self.read("flavor.show", {"reference": "small"})["ram"], 4096)
        self.assertEqual(len(self.cloud.tokens), 1)

    def test_dns_wait_is_bounded_and_cannot_authenticate_after_timeout(self):
        original = socket.getaddrinfo
        release = threading.Event()

        def delayed(*args):
            release.wait(2)
            return original(*args)

        try:
            with mock.patch.object(socket, "getaddrinfo", side_effect=delayed):
                started = time.monotonic()
                with self.assertRaises(ProviderError):
                    self.read("identity", seconds=0.05)
                self.assertLess(time.monotonic() - started, 0.4)
                self.assertEqual(len(self.cloud.tokens), 0)
        finally:
            release.set()
        self.read("identity")
        self.assertEqual(len(self.cloud.tokens), 1)

    def test_catalog_region_interface_tls_and_project_fail_closed(self):
        for change in (
            {"OS_REGION_NAME": "other"},
            {"OS_INTERFACE": "internal"},
            {"OS_PROJECT_ID": PORT},
            {"OS_PROJECT_NAME": "wrong"},
        ):
            self.api = API({**self.cloud.environment, **change}, allow_http=True)
            with self.assertRaises(ProviderError):
                self.read("flavor.show", {"reference": "small"})
        with self.assertRaises(ProviderError):
            API(self.cloud.environment)
        with self.assertRaises(ProviderError):
            API({**self.cloud.environment, "OS_INSECURE": "true"}, allow_http=True)

    def test_http_catalog_endpoints_are_followed_like_the_cli(self):
        api = API(self.cloud.environment, allow_http=True)
        # The fixture's auth transport is loopback HTTP; production auth
        # construction independently enforces HTTPS. Exercise catalog policy.
        api.allow_http = False
        self.assertEqual(
            api.execute("flavor.show", {"reference": "small"}, time.monotonic() + 2)["ram"],
            4096,
        )

    def test_glance_http_requires_exact_acknowledgement(self):
        for acknowledgement, accepted in (
            ("true", False),
            ("I_UNDERSTAND_GLANCE_CREDENTIALS_USE_HTTP", True),
        ):
            api = API(
                {**self.cloud.environment, "PLATFORM_ALLOW_HTTP_GLANCE": acknowledgement},
                allow_http=True,
            )
            # The fixture's auth transport is loopback HTTP; production auth
            # construction independently enforces HTTPS. Exercise catalog policy.
            api.allow_http = False
            if accepted:
                self.assertEqual(
                    api.execute("image.usage", {}, time.monotonic() + 2)["usage"][
                        "image_count_total"
                    ],
                    3,
                )
            else:
                with self.assertRaises(ProviderError):
                    api.execute("image.usage", {}, time.monotonic() + 2)

    def test_pagination_preserves_filters_and_never_follows_next_urls(self):
        rows = self.cloud.records["images"]
        other = {**rows[0], "id": PORT}

        def pages(query):
            if "marker" not in query:
                return 200, {"images": rows, "next": "https://attacker.invalid/steal-token"}, {}
            self.assertEqual(query["marker"], [IMAGE])
            self.assertEqual(query["visibility"], ["private"])
            return 200, {"images": [other]}, {}

        self.cloud.responses["/v2/images"] = pages
        self.assertEqual(len(self.read("image.list", {"visibility": "private"})), 2)
        self.cloud.responses["/v2/images"] = (200, {"images": rows, "next": "anything"}, {})
        with self.assertRaises(ProviderError):
            self.read("image.list")

    def test_authentication_response_is_bounded(self):
        self.cloud.responses["/v3/auth/tokens"] = (201, b"x" * (MAX_RESPONSE + 1), {})
        with self.assertRaisesRegex(ProviderError, "safety limit"):
            self.read("identity")

    def test_verified_tls_and_environment_proxy_isolation(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        with tempfile.TemporaryDirectory() as directory:
            certificate = Path(directory) / "ca.pem"
            certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            private = Path(directory) / "key.pem"
            private.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, private)
            with serve(tls_context=context) as cloud:
                api = API(cloud.environment)
                with self.assertRaises(ProviderError):
                    api.execute("identity", {}, time.monotonic() + 2)
                api = API({**cloud.environment, "OS_CACERT": str(certificate)})
                with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://attacker.invalid:1"}):
                    self.assertEqual(
                        api.execute("identity", {}, time.monotonic() + 2)["project_id"], PROJECT
                    )

    def test_legacy_neutron_ownership_does_not_invent_or_overwrite(self):
        row = self.cloud.records["security-groups"][0]
        row["project_id"] = None
        self.assertIsNone(self.read("security_group.show", {"reference": PORT})["project_id"])
        del row["project_id"]
        del row["tenant_id"]
        self.assertNotIn("project_id", self.read("security_group.show", {"reference": PORT}))


class SocketTests(ProviderTests):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory(prefix="provider-")
        self.addCleanup(self.directory.cleanup)
        self.path = self.directory.name + "/read.sock"
        self.server = Server(self.path, self.api, frozenset({(os.geteuid(), os.getegid())}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def call(self, action, params=None, seconds=2):
        return provider.call(action, params or {}, timeout_seconds=seconds, path=self.path)

    def test_socket_concurrency(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(
                pool.map(lambda _: self.call("flavor.show", {"reference": "small"}), range(16))
            )
        self.assertEqual(len(values), 16)
        self.assertEqual(len(self.cloud.tokens), 1)

    def test_peer_denied_before_request_parsing(self):
        self.server.peers = frozenset({(os.geteuid() + 1, os.getegid())})
        with self.assertRaises(ProviderError):
            self.call("identity")
        self.assertEqual(len(self.cloud.tokens), 0)

    def test_action_allowlist_cannot_select_urls_methods_or_exec(self):
        for action, params in (
            ("server.delete", {"reference": SERVER}),
            ("flavor.show", {"reference": "small", "url": "https://attacker.invalid"}),
            ("console", {"reference": SERVER, "lines": 2001}),
        ):
            with self.subTest(action=action), socket.socket(socket.AF_UNIX) as connection:
                connection.connect(self.path)
                connection.sendall(
                    json.dumps(
                        {"version": 1, "action": action, "params": params, "timeout": 2}
                    ).encode()
                    + b"\n"
                )
                response = json.loads(provider.receive(connection, 512, time.monotonic() + 2))
                self.assertFalse(response["ok"])
                self.assertEqual(response["code"], "invalid")
        self.assertEqual(len(self.cloud.tokens), 0)

    def test_python_runner_avoids_exec_and_has_no_outage_fallback(self):
        with (
            mock.patch.dict(os.environ, {"PLATFORM_PROVIDER_SOCKET": self.path}),
            mock.patch.object(runtime, "run", side_effect=AssertionError("must not exec")),
        ):
            # The production seam uses the original callable, not this mock.
            result = provider.run(
                (
                    "protected-wrapper",
                    "flavor",
                    "show",
                    "small",
                    "--format",
                    "json",
                    "--column",
                    "ram",
                )
            )
            self.assertEqual(json.loads(result.stdout), {"ram": 4096})
            with self.assertRaises(runtime.CommandFailure):
                provider.run(("protected-wrapper", "token", "issue", "--column", "id"))
        self.server.peers = frozenset()
        with (
            mock.patch.dict(os.environ, {"PLATFORM_PROVIDER_SOCKET": self.path}),
            self.assertRaises(runtime.CommandFailure),
        ):
            provider.run(("protected-wrapper", "flavor", "list"))

    def test_server_image_projection_cannot_hide_malformed_prune_evidence(self):
        row = self.cloud.records["servers"][0]
        with mock.patch.dict(os.environ, {"PLATFORM_PROVIDER_SOCKET": self.path}):
            row["image"] = {"unexpected": "PRIVATE"}
            with self.assertRaises(runtime.CommandFailure):
                provider.run(("wrapper", "server", "list", "--column", "Image"))
            row["image"] = {}
            result = provider.run(("wrapper", "server", "list", "--column", "Image"))
            self.assertEqual(json.loads(result.stdout), [{"Image": None}])

    def test_output_projection_limits_and_withheld_errors(self):
        with mock.patch.dict(os.environ, {"PLATFORM_PROVIDER_SOCKET": self.path}):
            with self.assertRaisesRegex(runtime.CommandFailure, "safety limit"):
                provider.run(("wrapper", "flavor", "list"), stdout_limit=10)
            self.cloud.responses["/v2.1/flavors/small"] = (500, {"PRIVATE": "secret"}, {})
            result = provider.run(("wrapper", "flavor", "show", "small"), check=False)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(b"PRIVATE", result.stderr)
            with self.assertRaisesRegex(openstack.OpenStackError, "provider details were withheld"):
                openstack._json_command(
                    ("flavor", "show", "small"),
                    timeout_seconds=2,
                    command_runner=provider.run,
                    executable="wrapper",
                )

    def test_request_size_is_rejected_before_authentication(self):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(self.path)
            connection.sendall(b"x" * 16_385 + b"\n")
            response = json.loads(provider.receive(connection, 512, time.monotonic() + 2))
            self.assertEqual(response["code"], "size")
        self.assertEqual(len(self.cloud.tokens), 0)

    def test_process_readiness_restart_and_safe_shutdown(self):
        directory = Path(self.directory.name)
        credentials = directory / "service.env"
        credentials.write_text(
            "OS_AUTH_URL=https://identity.invalid/v3\nOS_USERNAME=fixture\nOS_PASSWORD=fixture\nOS_PROJECT_NAME=fixture\n"
        )
        credentials.chmod(0o600)
        notify_path = str(directory / "notify.sock")
        read_path = str(directory / "process.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
            notify.bind(notify_path)
            notify.settimeout(5)
            for _ in range(2):
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "openstack_platform.provider_service",
                        "--socket",
                        read_path,
                        "--credentials",
                        str(credentials),
                        "--peer",
                        f"{os.geteuid()}:{os.getegid()}",
                    ],
                    env={**os.environ, "NOTIFY_SOCKET": notify_path},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    self.assertEqual(notify.recv(128), b"READY=1")
                    self.assertTrue(Path(read_path).is_socket())
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.connect(read_path)
                        connection.sendall(
                            b'{"version":1,"action":"server.delete","params":{},"timeout":1}\n'
                        )
                        self.assertEqual(
                            json.loads(provider.receive(connection, 512, time.monotonic() + 2))[
                                "code"
                            ],
                            "invalid",
                        )
                    # Admin re-applies the same owner and mode on every controller
                    # start; that changes ctime, not the credentials.
                    credentials.chmod(0o600)
                    os.utime(credentials)

                    def identity_code():
                        with socket.socket(socket.AF_UNIX) as connection:
                            connection.connect(read_path)
                            connection.sendall(
                                b'{"version":1,"action":"identity","params":{},"timeout":1}\n'
                            )
                            return json.loads(
                                provider.receive(connection, 512, time.monotonic() + 3)
                            )["code"]

                    self.assertNotEqual(identity_code(), "configuration")
                    credentials.write_text(
                        credentials.read_text().replace(
                            "OS_PASSWORD=fixture", "OS_PASSWORD=rotated", 1
                        )
                        + "# changed\n"
                    )
                    self.assertEqual(identity_code(), "configuration")
                    # It exits for the service manager to restart it with them.
                    self.assertEqual(process.wait(timeout=5), 1)
                finally:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(stdout, b"")
                self.assertIn(b"credentials changed", stderr)
                self.assertFalse(Path(read_path).exists())

    def test_owned_credentials_and_socket_paths(self):
        path = Path(self.directory.name) / "credentials"
        path.write_text("OS_PASSWORD='fixture-password'\n")
        path.chmod(0o600)
        self.assertEqual(load_credentials(path), {"OS_PASSWORD": "fixture-password"})
        path.chmod(0o644)
        with self.assertRaises(ProviderError):
            load_credentials(path)
        path.unlink()
        path.symlink_to("/etc/passwd")
        with self.assertRaises(OSError):
            load_credentials(path)
        with self.assertRaises(ProviderError):
            Server(self.path, self.api, self.server.peers)


if __name__ == "__main__":
    unittest.main()
