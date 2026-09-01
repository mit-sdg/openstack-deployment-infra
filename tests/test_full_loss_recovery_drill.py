from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


class FullLossRecoveryDrillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.bundle = self.root / "production-20260830T120000Z"
        for component in ("hosted-controller", "operator-state", "managed-data"):
            (self.bundle / component).mkdir(parents=True, mode=0o700)
        self._database(self.bundle / "operator-state/platform.sqlite3.age", hosted=False)
        self._database(self.bundle / "hosted-controller/platform.sqlite3.age", hosted=True)
        self._managed_archives()
        self.identity = self._private("controller.key", b"controller")
        self.managed_identity = self._private("managed.key", b"managed")
        self.platform_config = self._private("platform.json", b"{}\n")
        self.log = self.root / "launchers.log"
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        self._fake_tools()
        self.script = (
            Path(__file__).resolve().parents[1] / "infra/backup/full_loss_recovery_drill.sh"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _private(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        path.chmod(0o600)
        return path

    @staticmethod
    def _database(path: Path, *, hosted: bool) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER, checksum TEXT);
            CREATE TABLE operations(status TEXT);
            CREATE TABLE operation_dispatches(status TEXT);
            CREATE TABLE image_selections(role TEXT);
            CREATE TABLE applications(application_id TEXT);
            CREATE TABLE deployment_attempts(
              application_id TEXT, deployment_id TEXT, status TEXT
            );
            CREATE TABLE active_deployments(application_id TEXT, deployment_id TEXT);
            """
        )
        connection.execute("INSERT INTO schema_migrations VALUES (2, ?)", ("a" * 64,))
        if hosted:
            connection.execute("INSERT INTO applications VALUES ('app-1')")
            connection.execute(
                "INSERT INTO deployment_attempts VALUES ('app-1','deploy-1','succeeded')"
            )
            connection.execute("INSERT INTO active_deployments VALUES ('app-1','deploy-1')")
        else:
            connection.execute("INSERT INTO image_selections VALUES ('admin')")
        connection.commit()
        connection.close()
        path.chmod(0o600)

    def _managed_archives(self) -> None:
        managed = self.bundle / "managed-data"
        (managed / "registry.age").write_bytes(b"registry archive")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            payload = json.dumps({"format_version": 1, "objects": []}).encode()
            member = tarfile.TarInfo("manifest.json")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        (managed / "garage.age").write_bytes(stream.getvalue())
        for path in managed.iterdir():
            path.chmod(0o600)

    def _tool(self, name: str, source: str) -> Path:
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + source)
        path.chmod(0o700)
        return path

    def _fake_tools(self) -> None:
        self.recovery = self._tool(
            "recovery",
            """
if [[ $1 == verify ]]; then exit 0; fi
[[ $1 == import && $3 == --destination ]]
cp -R -- "$2" "$4/"
""",
        )
        self.age = self._tool(
            "age",
            """
output=
source=
while (($#)); do
  case $1 in
    --output) output=$2; shift 2 ;;
    --identity) shift 2 ;;
    --decrypt) shift ;;
    *) source=$1; shift ;;
  esac
done
if [[ -n $output ]]; then cp -- "$source" "$output"; else cat -- "$source"; fi
""",
        )
        self.operator = self._tool(
            "operator-restore",
            """
printf 'operator' >>"$DRILL_TEST_LOG"; printf ' %q' "$@" >>"$DRILL_TEST_LOG"; printf '\n' >>"$DRILL_TEST_LOG"
[[ ${FAIL_OPERATOR:-0} != 1 ]] || exit 1
state=
backup=
while (($#)); do
 case $1 in
  --replacement-state-directory) state=$2; shift 2 ;;
  --yes) shift ;;
  *) [[ -z $backup ]] || exit 64; backup=$1; shift ;;
 esac
done
[[ -n $state && ! -e $state/platform.sqlite3 ]]
cp -- "$backup" "$state/platform.sqlite3"
echo 'restore=verified schema-version=2 integrity=ok'
""",
        )
        self.hosted = self._tool(
            "hosted-restore",
            """
printf 'hosted' >>"$DRILL_TEST_LOG"; printf ' %q' "$@" >>"$DRILL_TEST_LOG"; printf '\n' >>"$DRILL_TEST_LOG"
[[ ${FAIL_HOSTED:-0} != 1 ]] || exit 1
backup=$1; shift
destination=
while (($#)); do
 case $1 in
  --destination) destination=$2; shift 2 ;;
  --platform-config) shift 2 ;;
  --yes) shift ;;
  *) exit 64 ;;
 esac
done
[[ -n $destination && ! -e $destination ]]
cp -- "$backup" "$destination"
echo 'restore=verified schema-version=2 integrity=ok'
""",
        )
        self.managed = self._tool(
            "managed-restore",
            """
printf 'managed' >>"$DRILL_TEST_LOG"; printf ' %q' "$@" >>"$DRILL_TEST_LOG"; printf '\n' >>"$DRILL_TEST_LOG"
[[ ${FAIL_MANAGED:-0} != 1 ]] || exit 1
[[ $1 == --yes && -d $2 ]]
echo 'managed-data-restore=verified source=fake'
""",
        )
        self.registry = self.bin / "registry.py"
        self.registry.write_text(
            "import sys\nassert sys.argv[1] == 'verify'\nsys.stdin.buffer.read()\n"
        )
        self.registry.chmod(0o600)

    def _run(
        self, mode: str, *, failure: str | None = None
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        work = self.root / f"work-{mode.removeprefix('--')}-{failure or 'ok'}"
        environment = {
            **os.environ,
            "RECOVERY_COMMAND": str(self.recovery),
            "AGE": str(self.age),
            "OPERATOR_RESTORE_LAUNCHER": str(self.operator),
            "HOSTED_RESTORE_LAUNCHER": str(self.hosted),
            "MANAGED_RESTORE_LAUNCHER": str(self.managed),
            "REGISTRY_ARTIFACT_SCRIPT": str(self.registry),
            "DRILL_TEST_LOG": str(self.log),
        }
        if failure:
            environment[failure] = "1"
        result = subprocess.run(
            (
                self.script,
                mode,
                self.bundle,
                work,
                self.identity,
                self.managed_identity,
                self.platform_config,
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        return result, work

    def test_full_mode_invokes_all_restore_launchers_on_absent_replacements(self) -> None:
        result, work = self._run("--full")
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads((work / "DRILL-EVIDENCE.json").read_text())
        self.assertEqual(evidence["format"], "openstack-platform-full-loss-drill-v2")
        self.assertEqual(evidence["managedData"], "restored")
        self.assertEqual(evidence["records"]["acceptedDeployments"], 1)
        calls = self.log.read_text()
        self.assertIn(f"--replacement-state-directory {work}/replacements/operator-state", calls)
        self.assertIn(
            f"--destination {work}/replacements/hosted-controller/platform.sqlite3", calls
        )
        self.assertIn("managed --yes", calls)
        self.assertNotIn("/srv/openstack-platform/state", calls)

    def test_restore_failures_cannot_emit_complete_evidence(self) -> None:
        for failure in ("FAIL_OPERATOR", "FAIL_HOSTED", "FAIL_MANAGED"):
            with self.subTest(failure=failure):
                result, work = self._run("--full", failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((work / "DRILL-EVIDENCE.json").exists())
                self.assertNotIn("full-loss-drill=verified", result.stdout)

    def test_verify_only_cannot_launch_restores_or_emit_complete_evidence(self) -> None:
        result, work = self._run("--verify-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("full-loss-drill=verify-only evidence=none", result.stdout)
        self.assertFalse((work / "DRILL-EVIDENCE.json").exists())
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
