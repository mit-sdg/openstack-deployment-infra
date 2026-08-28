from __future__ import annotations

import base64
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openstack_platform import host_user_data
from openstack_platform.config import load_platform
from openstack_platform.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]
ADMIN_VOLUME = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BACKUP_VOLUME = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
DATA_VOLUME = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


class HostUserDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform = load_platform(ROOT / "config/platform.example.json")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pki = self.root / "pki"
        self.pki.mkdir()
        self.public_key = self.root / "agentops.pub"
        self.public_key.write_text("ssh-ed25519 " + "A" * 48 + " agentops\n")
        self.public_key.chmod(0o644)
        for name in (
            "internal-ca.pem",
            "nomad-server.pem",
            "nomad-cli.pem",
            "nomad-ingress.pem",
            "storage.pem",
        ):
            path = self.pki / name
            path.write_bytes(f"public-{name}\n".encode())
            path.chmod(0o644)
        for name in (
            "nomad-server-key.pem",
            "nomad-cli-key.pem",
            "nomad-ingress-key.pem",
            "storage-key.pem",
        ):
            path = self.pki / name
            path.write_bytes(f"private-{name}\n".encode())
            path.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def secret_file(self, name: str, values: dict[str, str]) -> Path:
        path = self.root / name
        path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        path.chmod(0o600)
        return path

    def inputs(
        self,
        role: str,
        secrets: dict[str, str],
        *,
        cloudflared: bool = False,
    ) -> host_user_data.HostUserDataInputs:
        tunnel = None
        if cloudflared:
            tunnel = self.root / "tunnel-token"
            tunnel.write_text("sentinel-cloudflare-" + "x" * 64)
            tunnel.chmod(0o600)
        return host_user_data.HostUserDataInputs(
            template=ROOT / "infra" / "cloud-init-nixos" / f"{role}.yaml",
            operator_public_key=self.public_key,
            secret_file=self.secret_file(f"{role}.env", secrets),
            pki_directory=self.pki,
            cloudflare_tunnel_token_file=tunnel,
            enable_cloudflared=cloudflared,
        )

    def render(
        self,
        role: str,
        inputs: host_user_data.HostUserDataInputs,
        volumes: dict[str, str],
    ) -> tuple[Path, str]:
        output = self.root / f"{role}-user-data"
        host_user_data.render_host_user_data_file(self.platform, role, inputs, volumes, output)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        text = output.read_text()
        self.assertTrue(text.startswith("#cloud-config\n"))
        self.assertIsNone(re.search(r"__[A-Z0-9_]+__", text))
        return output, text

    def test_role_renderers_have_exact_contracts_and_protected_outputs(self) -> None:
        _, admin = self.render(
            "admin",
            self.inputs("admin", {"NOMAD_GOSSIP_KEY": "sentinel-admin-gossip"}),
            {
                self.platform.get("volumes.adminState.name"): ADMIN_VOLUME,
                self.platform.get("volumes.backup.name"): BACKUP_VOLUME,
            },
        )
        self.assertIn("NOMAD_GOSSIP_KEY=sentinel-admin-gossip", admin)
        self.assertIn(f"ADMIN_VOLUME_ID={ADMIN_VOLUME}", admin)
        self.assertIn(f"BACKUP_VOLUME_ID={BACKUP_VOLUME}", admin)
        self.assertIn(f"/dev/vdb:{self.platform.get('volumes.adminState.label')}", admin)
        self.assertIn(f"/dev/vdc:{self.platform.get('volumes.backup.label')}", admin)
        self.assertIn('mkfs.xfs -f -L "$label" "$device"', admin)
        self.assertIn("systemctl reboot", admin)
        self.assertEqual(admin.count(base64.b64encode(b"public-internal-ca.pem\n").decode()), 2)

        _, ingress = self.render(
            "ingress",
            self.inputs(
                "ingress",
                {
                    "NOMAD_CONTROLLER_TOKEN": "sentinel-controller-unused",
                    "NOMAD_TRAEFIK_TOKEN": "sentinel-traefik-token",
                },
                cloudflared=True,
            ),
            {},
        )
        self.assertEqual(ingress.count("sentinel-traefik-token"), 2)
        self.assertNotIn("sentinel-controller-unused", ingress)
        self.assertNotIn("sentinel-cloudflare", ingress)
        tunnel_line = next(
            line.split("content: ", 1)[1]
            for line in ingress.splitlines()
            if line.strip().startswith("content: VFVOTkVMX1RPS0VOPX")
        )
        self.assertIn("sentinel-cloudflare", base64.b64decode(tunnel_line).decode())

        storage_secrets = {
            "POSTGRES_PASSWORD": "sentinel-postgres",
            "MONGO_PASSWORD": "sentinel-mongo",
            "GARAGE_RPC_SECRET": "sentinel-garage-rpc",
            "GARAGE_ADMIN_TOKEN": "sentinel-garage-admin",
            "GARAGE_METRICS_TOKEN": "sentinel-garage-metrics",
            "REGISTRY_HTTP_SECRET": "sentinel-registry-http",
            "REGISTRY_BUILDER_PASSWORD": "sentinel-builder-password",
            "REGISTRY_RUNTIME_PASSWORD": "sentinel-runtime-password",
        }
        _, storage = self.render(
            "storage",
            self.inputs("storage", storage_secrets),
            {self.platform.get("volumes.data.name"): DATA_VOLUME},
        )
        self.assertIn("POSTGRES_PASSWORD=sentinel-postgres", storage)
        self.assertIn("MONGO_INITDB_ROOT_PASSWORD=sentinel-mongo", storage)
        self.assertIn(f"DATA_VOLUME_ID={DATA_VOLUME}", storage)
        self.assertIn(f"label={self.platform.get('volumes.data.label')}", storage)
        self.assertIn('mkfs.xfs -f -L "$label" "$device"', storage)
        self.assertIn("systemctl reboot", storage)
        self.assertNotIn("sentinel-builder-password", storage)
        self.assertNotIn("sentinel-runtime-password", storage)
        encoded_htpasswd = next(
            line.split("content: ", 1)[1]
            for line in storage.splitlines()
            if line.strip().startswith("content: YnVpbGRlcjo")
        )
        decoded_htpasswd = base64.b64decode(encoded_htpasswd)
        self.assertRegex(decoded_htpasswd, rb"^builder:\$2[aby]\$")
        self.assertIn(b"\nruntime:$2", decoded_htpasswd)

    def test_standalone_renderer_uses_the_same_admin_contract(self) -> None:
        sentinel = "sentinel-standalone-admin"
        inputs = self.inputs("admin", {"NOMAD_GOSSIP_KEY": sentinel})
        output = self.root / "standalone-user-data"
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "infra" / "openstack" / "render_host_user_data.py"),
                "--role",
                "admin",
                "--platform-config",
                str(ROOT / "config" / "platform.example.json"),
                "--template",
                str(inputs.template),
                "--operator-public-key",
                str(inputs.operator_public_key),
                "--secret-file",
                str(inputs.secret_file),
                "--pki-directory",
                str(inputs.pki_directory),
                "--volume",
                self.platform.get("volumes.adminState.name"),
                ADMIN_VOLUME,
                "--volume",
                self.platform.get("volumes.backup.name"),
                BACKUP_VOLUME,
                "--output",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertIn(sentinel, output.read_text())

    def test_template_placeholder_count_drift_fails_without_secret_in_error(self) -> None:
        sentinel = "sentinel-renderer-error-secret"
        inputs = self.inputs("admin", {"NOMAD_GOSSIP_KEY": sentinel})
        changed = self.root / "changed.yaml"
        changed.write_text(inputs.template.read_text() + "\n# __ADMIN_HOST__\n")
        changed_inputs = host_user_data.HostUserDataInputs(
            template=changed,
            operator_public_key=inputs.operator_public_key,
            secret_file=inputs.secret_file,
            pki_directory=inputs.pki_directory,
        )
        with self.assertRaises(ValidationError) as caught:
            host_user_data.render_host_user_data_file(
                self.platform,
                "admin",
                changed_inputs,
                {
                    self.platform.get("volumes.adminState.name"): ADMIN_VOLUME,
                    self.platform.get("volumes.backup.name"): BACKUP_VOLUME,
                },
                self.root / "output",
            )
        self.assertIn("placeholder count changed", str(caught.exception))
        self.assertNotIn(sentinel, str(caught.exception))

    def test_private_inputs_are_direct_owner_only_files(self) -> None:
        inputs = self.inputs("admin", {"NOMAD_GOSSIP_KEY": "sentinel-weak-mode"})
        inputs.secret_file.chmod(0o640)
        with self.assertRaisesRegex(ValidationError, "owner-only"):
            host_user_data.render_host_user_data_file(
                self.platform,
                "admin",
                inputs,
                {
                    self.platform.get("volumes.adminState.name"): ADMIN_VOLUME,
                    self.platform.get("volumes.backup.name"): BACKUP_VOLUME,
                },
                self.root / "output",
            )
        self.assertFalse((self.root / "output").exists())

    def test_staged_payload_is_mode_0600_and_always_removed(self) -> None:
        inputs = self.inputs(
            "ingress",
            {
                "NOMAD_CONTROLLER_TOKEN": "sentinel-controller",
                "NOMAD_TRAEFIK_TOKEN": "sentinel-cleanup",
            },
        )
        staged = ""
        with self.assertRaisesRegex(RuntimeError, "injected interruption"):
            with host_user_data.staged_host_user_data(
                self.platform, "ingress", {}, inputs=inputs
            ) as name:
                staged = name
                path = Path(name)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn("sentinel-cleanup", path.read_text())
                raise RuntimeError("injected interruption")
        self.assertFalse(Path(staged).exists())

    def test_environment_contract_uses_protected_paths_not_secret_values(self) -> None:
        secret = self.secret_file(
            "tokens.env",
            {
                "NOMAD_CONTROLLER_TOKEN": "sentinel-controller",
                "NOMAD_TRAEFIK_TOKEN": "sentinel-traefik",
            },
        )
        environment = {
            "OPERATOR_PUBLIC_KEY": str(self.public_key),
            "NOMAD_TOKENS_FILE": str(secret),
            "PKI_DIR": str(self.pki),
            "ENABLE_CLOUDFLARED": "false",
        }
        inputs = host_user_data.inputs_from_environment("ingress", environment=environment)
        self.assertEqual(inputs.secret_file, secret)
        self.assertNotIn("sentinel", repr(inputs))
        self.assertFalse(inputs.enable_cloudflared)


if __name__ == "__main__":
    unittest.main()
