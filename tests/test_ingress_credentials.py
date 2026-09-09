from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openstack_platform import ingress_credentials, openstack, operator
from openstack_platform.config import load_platform
from openstack_platform.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]
TUNNEL = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def connector_token(tunnel: str = TUNNEL, secret: bytes = b"s" * 32) -> bytes:
    return base64.b64encode(
        json.dumps(
            {
                "a": "a" * 32,
                "t": tunnel,
                "s": base64.b64encode(secret).decode(),
            }
        ).encode()
    )


class IngressCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.platform = load_platform(ROOT / "config/platform.example.json")
        self.token = self.root / "token"
        self.token.write_bytes(connector_token() + b"\n")
        self.token.chmod(0o600)
        pki = self.root / "pki"
        pki.mkdir()
        for name in ("internal-ca.pem", "nomad-ingress.pem", "nomad-ingress-key.pem"):
            path = pki / name
            path.write_text("fixture-pki")
            path.chmod(0o600)
        public_key = self.root / "operator.pub"
        public_key.write_text("ssh-ed25519 " + "A" * 48)
        nomad = self.root / "nomad.env"
        nomad.write_text("NOMAD_CONTROLLER_TOKEN=controller\nNOMAD_TRAEFIK_TOKEN=traefik\n")
        nomad.chmod(0o600)
        self.environment = {
            "OPERATOR_PUBLIC_KEY": str(public_key),
            "PKI_DIR": str(pki),
            "NOMAD_TOKENS_FILE": str(nomad),
            "ENABLE_CLOUDFLARED": "false",
            "CLOUDFLARE_TUNNEL_TOKEN_FILE": "/missing/untrusted-input",
        }

    def replace(self, token_file=None, **kwargs):
        return openstack.replace_host(
            self.platform,
            "ingress",
            selected_image_id=OTHER,
            selected_compatibility_hash="a" * 64,
            operation_id=OTHER,
            cloudflare_tunnel_token_file=token_file,
            checkpoint=mock.Mock(),
            **kwargs,
        )

    def assert_rejected(self, token_file):
        runner = mock.Mock()
        with self.assertRaises(ValidationError) as caught:
            self.replace(token_file, command_runner=runner)
        runner.assert_not_called()
        return str(caught.exception)

    def test_missing_file_and_environment_fallback_fail_before_provider_calls(self):
        with mock.patch.dict(
            os.environ,
            {
                "ENABLE_CLOUDFLARED": "false",
                "CLOUDFLARE_TUNNEL_TOKEN_FILE": str(self.token),
            },
        ):
            self.assertIn("--cloudflare-tunnel-token-file", self.assert_rejected(None))
            self.assert_rejected(self.root / "missing")

    def test_malformed_tokens_fail_before_provider_calls_without_echoing_input(self):
        for token in (
            b"sentinel-invalid-token" * 10,
            b"\x00" * 60,
            b"",
            connector_token() + b"\nINJECT=value",
            connector_token(secret=b"short"),
            connector_token(tunnel="bad-tunnel"),
            base64.b64encode(b'{"a":1,"a":2}'),
            base64.b64encode(b"[]"),
            base64.b64encode(b'"sentinel"'),
            b"TUNNEL_TOKEN=" + connector_token(),
        ):
            with self.subTest(length=len(token)):
                self.token.write_bytes(token)
                message = self.assert_rejected(self.token)
                if token:
                    self.assertNotIn(token.decode(errors="replace"), message)
                self.assertNotIn("sentinel", message)

    def test_unsafe_files_fail_before_provider_calls(self):
        for mode in (0o644, 0o640, 0o400):
            self.token.chmod(mode)
            self.assert_rejected(self.token)
        self.token.chmod(0o600)
        with mock.patch.object(ingress_credentials.os, "geteuid", return_value=os.geteuid() + 1):
            self.assert_rejected(self.token)
        link = self.root / "link"
        link.symlink_to(self.token)
        self.assert_rejected(link)
        link.unlink()
        os.link(self.token, link)
        self.assert_rejected(self.token)
        link.unlink()
        self.token.unlink()
        os.mkfifo(self.token, 0o600)
        self.assert_rejected(self.token)
        self.token.unlink()
        self.token.write_bytes(b"x" * 16385)
        self.token.chmod(0o600)
        self.assert_rejected(self.token)
        self.assert_rejected(self.root)

    def test_opaque_override_is_refused_even_with_valid_token(self):
        runner = mock.Mock()
        with self.assertRaisesRegex(ValidationError, "overrides are refused"):
            self.replace(self.token, user_data_path=self.token, command_runner=runner)
        runner.assert_not_called()

    def test_successive_tokens_are_rendered_without_persistence_or_leaks(self):
        temporary = self.root / "temporary"
        temporary.mkdir()
        before = set(self.root.rglob("*"))
        for token in (connector_token(), connector_token(secret=b"z" * 32), connector_token(OTHER)):
            self.token.write_bytes(token)
            staged_paths = []
            expected = openstack.ReplacementResult("ingress", True, OTHER, OTHER, "confirmed")

            def replace_candidate(
                *args, token=token, staged_paths=staged_paths, expected=expected, **kwargs
            ):
                path = Path(kwargs["user_data_path"])
                staged_paths.append(path)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn(base64.b64encode(b"TUNNEL_TOKEN=" + token + b"\n"), path.read_bytes())
                self.assertNotIn(token.decode(), repr(args) + repr(kwargs))
                self.assertNotIn(str(self.token), repr(kwargs))
                return expected

            with (
                mock.patch.dict(os.environ, self.environment),
                mock.patch.object(tempfile, "tempdir", str(temporary)),
                mock.patch.object(openstack, "_replace_host", side_effect=replace_candidate),
            ):
                result = self.replace(self.token)
            self.assertEqual(result, expected)
            self.assertNotIn(token.decode(), repr(result))
            self.assertTrue(staged_paths)
            self.assertTrue(all(not path.exists() for path in staged_paths))
            self.assertEqual(self.token.read_bytes(), token)
            self.assertEqual(set(self.root.rglob("*")), before)

    def test_captured_input_is_used_and_temporary_secrets_cleaned_on_failure(self):
        real_resolve = ingress_credentials.host_user_data.inputs_from_environment
        snapshot_paths = []

        def change_source(*args, **kwargs):
            snapshot = Path(kwargs["environment"]["CLOUDFLARE_TUNNEL_TOKEN_FILE"])
            snapshot_paths.append(snapshot)
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.token.write_bytes(b"changed-after-validation")
            return real_resolve(*args, **kwargs)

        with (
            mock.patch.dict(os.environ, self.environment),
            mock.patch.object(
                ingress_credentials.host_user_data,
                "inputs_from_environment",
                side_effect=change_source,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated provider failure"):
                with ingress_credentials.staged_replacement_user_data(
                    self.platform, self.token, maximum_bytes=1048576
                ) as staged:
                    path = Path(staged)
                    self.assertIn(
                        base64.b64encode(b"TUNNEL_TOKEN=" + connector_token() + b"\n"),
                        path.read_bytes(),
                    )
                    raise RuntimeError("simulated provider failure")
        self.assertFalse(path.exists())
        self.assertTrue(all(not snapshot.exists() for snapshot in snapshot_paths))

    def test_render_failure_removes_temporary_token_before_provider_calls(self):
        temporary = self.root / "temporary"
        temporary.mkdir()
        with (
            mock.patch.dict(os.environ, self.environment),
            mock.patch.object(tempfile, "tempdir", str(temporary)),
            mock.patch.object(
                ingress_credentials.host_user_data,
                "render_host_user_data_file",
                side_effect=ValidationError("simulated rendering failure"),
            ),
        ):
            self.assert_rejected(self.token)
        self.assertEqual(list(temporary.iterdir()), [])

    def test_recovery_neither_reads_token_nor_renders(self):
        with (
            mock.patch.object(ingress_credentials, "_read", side_effect=AssertionError("no read")),
            mock.patch.object(
                ingress_credentials,
                "staged_replacement_user_data",
                side_effect=AssertionError("no render"),
            ),
            mock.patch.object(openstack, "_recover_host_replacement") as recover,
        ):
            for phase, action in (
                ("old_stopped", "rollback"),
                ("accepted", "continue"),
                ("complete", "cleanup_old"),
            ):
                openstack.recover_host_replacement(
                    self.platform,
                    "ingress",
                    phase=phase,
                    refs={},
                    action=action,
                    checkpoint=mock.Mock(),
                )
        self.assertEqual(recover.call_count, 3)

    def test_cli_exposes_file_option_not_escrow_or_token_value(self):
        parser = operator.build_parser()
        args = parser.parse_args(
            [
                "infra",
                "replace",
                "ingress",
                "--cloudflare-tunnel-token-file",
                str(self.token),
                "--yes",
            ]
        )
        self.assertEqual(args.cloudflare_tunnel_token_file, self.token)
        # Optional in the parser so recorded recovery does not require the file.
        args = parser.parse_args(["infra", "replace", "ingress", "--yes"])
        self.assertIsNone(args.cloudflare_tunnel_token_file)
        with mock.patch("sys.stderr", io.StringIO()):
            for arguments in (
                ["infra", "ingress-credentials", "verify"],
                ["infra", "replace", "ingress", "--cloudflare-token", "value"],
            ):
                with self.assertRaises(SystemExit):
                    parser.parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
