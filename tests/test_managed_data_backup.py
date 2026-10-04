from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ManagedDataBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        config = json.loads((ROOT / "config/platform.example.json").read_text())
        self.config = self.root / "platform.json"
        self.config.write_text(json.dumps(config))
        self.backups = self.root / "backups"
        self.key = self.root / "key"
        self.key.write_text("fixture")
        self.key.chmod(0o600)
        self.secrets = self.root / "secrets"
        self.secrets.write_text("POSTGRES_PASSWORD=fixture\nMONGO_PASSWORD=fixture\n")
        self.secrets.chmod(0o600)
        self._tool(
            "age",
            """
if [[ $1 == --encrypt ]]; then
  printf 'age-encryption.org/v1\n' > "$5"
  cat >> "$5"
else
  tail -n +2 "$4"
fi
""",
        )
        self._tool("age-keygen", "echo age1fixture\n")
        self._tool("emit", 'echo "$1 fixture data"\n')
        self._tool("podman", "cat >/dev/null\n")
        self.environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "PLATFORM_CONFIG": str(self.config),
            "BACKUP_ROOT": str(self.backups),
            "AGE": str(self.bin / "age"),
            "AGE_KEY": str(self.key),
            "AGE_KEYGEN": str(self.bin / "age-keygen"),
            "EMIT_SCRIPT": str(self.bin / "emit"),
            "SECRETS_FILE": str(self.secrets),
            "SERVICE_CHECK_PYTHON": str(self.bin / "podman"),
            "MANAGED_RESTORE_LOCK": str(self.root / "restore.lock"),
        }

    def _tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
        path.chmod(0o700)

    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "infra/backup" / name), *args],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def backup(self) -> Path:
        result = self.run_script("run_platform_backup.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        return next(self.backups.glob("20*T*Z"))

    def test_new_set_contains_data_only_and_restores_without_registry_tools(self) -> None:
        evidence = self.backup()
        self.assertEqual(
            {path.name for path in evidence.iterdir()},
            {"MANIFEST", "SHA256SUMS", "postgres.age", "mongodb.age", "garage.age"},
        )
        self.assertIn("format_version=3", (evidence / "MANIFEST").read_text())
        result = self.run_script("restore_managed_data.sh", "--yes", str(evidence))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("managed-data-restore=verified", result.stdout)

    def test_legacy_set_checksums_images_but_skips_import_and_rejects_missing_data(self) -> None:
        evidence = self.backup()
        manifest = evidence / "MANIFEST"
        manifest.write_text(
            manifest.read_text().replace("format_version=3", "format_version=2")
            + "registry=distribution-artifacts-tar-gzip\n"
        )
        payload = b"age-encryption.org/v1\nlegacy image fixture"
        (evidence / "registry.age").write_bytes(payload)
        sums = evidence / "SHA256SUMS"
        sums.write_text(sums.read_text() + f"{hashlib.sha256(payload).hexdigest()}  registry.age\n")
        result = self.run_script("restore_managed_data.sh", "--yes", str(evidence))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("legacy registry.age skipped", result.stdout)
        (evidence / "garage.age").unlink()
        result = self.run_script("restore_managed_data.sh", "--yes", str(evidence))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("managed restore=complete", result.stdout)
