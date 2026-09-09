from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openstack_platform import ingress_credentials as escrow
from openstack_platform import openstack, operator
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
        self.state = self.root / "state"
        self.platform = load_platform(ROOT / "config/platform.example.json")
        self.token = self.root / "token"
        self.token.write_bytes(connector_token() + b"\n")
        self.token.chmod(0o600)

    def import_token(self):
        escrow.import_escrow(self.platform, self.state, self.token, tunnel_id=TUNNEL)

    def test_import_verify_idempotency_and_permissions(self):
        self.import_token()
        path = self.state / "credentials/ingress-credentials.json"
        first = path.stat()
        self.import_token()
        self.assertEqual(first.st_ino, path.stat().st_ino)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        escrow.verify_escrow(self.platform, self.state, tunnel_id=TUNNEL)
        with self.assertRaises(ValidationError):
            escrow.verify_escrow(self.platform, self.state, tunnel_id=OTHER)
        with self.assertRaises(ValidationError):
            escrow.verify_escrow(
                replace(self.platform, domain="wrong.example"), self.state, tunnel_id=TUNNEL
            )
        self.token.write_bytes(connector_token(secret=b"z" * 32))
        with self.assertRaises(ValidationError):
            self.import_token()
        self.assertEqual(first.st_ino, path.stat().st_ino)

    def test_invalid_tokens_do_not_create_escrow_or_echo_secrets(self):
        for token in (
            b"sentinel-invalid-token" * 10,
            b"\x00" * 60,
            connector_token() + b"\nINJECT=value",
            connector_token(OTHER),
            connector_token(secret=b"short"),
            base64.b64encode(b'{"a":1,"a":2}'),
        ):
            with self.subTest(token_length=len(token)):
                self.token.write_bytes(token)
                with self.assertRaises(ValidationError) as caught:
                    self.import_token()
                self.assertNotIn(token.decode(errors="replace"), str(caught.exception))
                self.assertFalse(self.state.exists())

    def test_rejects_weak_modes_symlinks_hardlinks_fifo_and_oversize(self):
        self.token.chmod(0o644)
        with self.assertRaises(ValidationError):
            self.import_token()
        self.token.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.token)
        with self.assertRaises(ValidationError):
            escrow.import_escrow(self.platform, self.state, link, tunnel_id=TUNNEL)
        link.unlink()
        os.link(self.token, link)
        with self.assertRaises(ValidationError):
            self.import_token()
        link.unlink()
        self.token.unlink()
        os.mkfifo(self.token, 0o600)
        with self.assertRaises(ValidationError):
            self.import_token()
        self.token.unlink()
        self.token.write_bytes(b"x" * 16385)
        self.token.chmod(0o600)
        with self.assertRaises(ValidationError):
            self.import_token()

    def test_wrong_owner_and_weak_or_symlinked_directories_are_refused(self):
        with mock.patch.object(escrow.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(ValidationError):
                self.import_token()
        self.import_token()
        directory = self.state / "credentials"
        directory.chmod(0o755)
        with self.assertRaises(ValidationError):
            escrow.verify_escrow(self.platform, self.state, tunnel_id=TUNNEL)
        directory.chmod(0o700)
        moved = self.state / "moved"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValidationError):
            escrow.verify_escrow(self.platform, self.state, tunnel_id=TUNNEL)

    def test_corruption_and_weak_escrow_fail_before_any_provider_call(self):
        self.import_token()
        path = self.state / "credentials/ingress-credentials.json"
        original = path.read_bytes()
        for payload in (b"sentinel-corrupt", original.replace(TUNNEL.encode(), OTHER.encode(), 1)):
            path.write_bytes(payload)
            runner = mock.Mock()
            with self.assertRaises(ValidationError):
                openstack.replace_host(
                    self.platform,
                    "ingress",
                    selected_image_id=OTHER,
                    selected_compatibility_hash="a" * 64,
                    operation_id=OTHER,
                    ingress_escrow_state_directory=self.state,
                    checkpoint=mock.Mock(),
                    command_runner=runner,
                )
            runner.assert_not_called()
        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaises(ValidationError):
            escrow.verify_escrow(self.platform, self.state, tunnel_id=TUNNEL)

    def test_missing_escrow_and_override_fail_before_provider_calls(self):
        for override in (None, self.token):
            runner = mock.Mock()
            with mock.patch.dict(
                os.environ,
                {"ENABLE_CLOUDFLARED": "false", "CLOUDFLARE_TUNNEL_TOKEN_FILE": str(self.token)},
            ):
                with self.assertRaises(ValidationError):
                    openstack.replace_host(
                        self.platform,
                        "ingress",
                        selected_image_id=OTHER,
                        selected_compatibility_hash="a" * 64,
                        operation_id=OTHER,
                        user_data_path=override,
                        ingress_escrow_state_directory=self.state,
                        checkpoint=mock.Mock(),
                        command_runner=runner,
                    )
            runner.assert_not_called()

    def test_recovery_cleanup_requires_escrow_but_rollback_is_not_blocked(self):
        runner = mock.Mock()
        for action in ("continue", "cleanup_old"):
            with self.assertRaises(ValidationError):
                openstack.recover_host_replacement(
                    self.platform,
                    "ingress",
                    phase="accepted",
                    refs={},
                    action=action,
                    ingress_escrow_state_directory=self.state,
                    checkpoint=mock.Mock(),
                    command_runner=runner,
                )
        runner.assert_not_called()
        with mock.patch.object(openstack, "_recover_host_replacement") as recover:
            openstack.recover_host_replacement(
                self.platform,
                "ingress",
                phase="old_stopped",
                refs={},
                action="rollback",
                ingress_escrow_state_directory=self.state,
                checkpoint=mock.Mock(),
            )
        recover.assert_called_once()

    def test_staging_forces_escrow_and_cleans_on_exception(self):
        self.import_token()
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
        environment = {
            "OPERATOR_PUBLIC_KEY": str(public_key),
            "PKI_DIR": str(pki),
            "NOMAD_TOKENS_FILE": str(nomad),
            "ENABLE_CLOUDFLARED": "false",
            "CLOUDFLARE_TUNNEL_TOKEN_FILE": "/missing/untrusted-input",
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(
                escrow.host_user_data,
                "inputs_from_environment",
                wraps=escrow.host_user_data.inputs_from_environment,
            ) as resolve,
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated provider failure"):
                with escrow.staged_replacement_user_data(
                    self.platform, self.state, maximum_bytes=1048576
                ) as staged:
                    path = Path(staged)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertIn(
                        base64.b64encode(b"TUNNEL_TOKEN=" + connector_token() + b"\n"),
                        path.read_bytes(),
                    )
                    temporary_token = Path(
                        resolve.call_args.kwargs["environment"]["CLOUDFLARE_TUNNEL_TOKEN_FILE"]
                    )
                    self.assertEqual(temporary_token.stat().st_mode & 0o777, 0o600)
                    raise RuntimeError("simulated provider failure")
        self.assertFalse(path.exists())
        self.assertFalse(temporary_token.exists())
        escrow.verify_escrow(self.platform, self.state, tunnel_id=TUNNEL)

    def test_interrupted_import_is_retryable_without_partial_escrow(self):
        real_write = escrow.durable.atomic_write
        for stage in ("after_write", "after_file_fsync", "after_rename"):
            state = self.root / stage

            def interrupt(observed, expected=stage):
                if observed == expected:
                    raise OSError("simulated interrupted write")

            def write(*args, **kwargs):
                return real_write(*args, **kwargs, fault=interrupt)

            with mock.patch.object(escrow.durable, "atomic_write", side_effect=write):
                with self.assertRaisesRegex(ValidationError, "committed durably"):
                    escrow.import_escrow(self.platform, state, self.token, tunnel_id=TUNNEL)
            destination = state / "credentials/ingress-credentials.json"
            self.assertEqual(destination.exists(), stage == "after_rename")
            escrow.import_escrow(self.platform, state, self.token, tunnel_id=TUNNEL)
            escrow.verify_escrow(self.platform, state, tunnel_id=TUNNEL)
            self.assertFalse(destination.with_name(".ingress-credentials.json.tmp").exists())

    def test_invalid_existing_escrow_is_not_overwritten(self):
        self.import_token()
        path = self.state / "credentials/ingress-credentials.json"
        path.write_bytes(b"broken-escrow")
        with self.assertRaises(ValidationError):
            self.import_token()
        self.assertEqual(path.read_bytes(), b"broken-escrow")
        path.unlink()
        path.symlink_to(self.token)
        with self.assertRaises(ValidationError):
            self.import_token()
        self.assertTrue(path.is_symlink())

    def test_cli_is_offline_and_does_not_print_token(self):
        output = io.StringIO()
        common = [
            "--platform-config",
            str(ROOT / "config/platform.example.json"),
            "--state-directory",
            str(self.state),
            "infra",
            "ingress-credentials",
        ]
        with mock.patch.object(operator, "_database", side_effect=AssertionError("no database")):
            for arguments in (
                ["import", "--token-file", str(self.token), "--tunnel-id", TUNNEL],
                ["verify", "--tunnel-id", TUNNEL],
            ):
                operator.dispatch(
                    operator.build_parser().parse_args(common + arguments), stdout=output
                )
        self.assertNotIn(connector_token().decode(), output.getvalue())
        self.assertNotIn("s" * 32, output.getvalue())
        self.assertIn("remote credential validity not checked", output.getvalue())


if __name__ == "__main__":
    unittest.main()
