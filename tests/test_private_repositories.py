"""Private GitHub repositories through per-app deploy keys."""

from __future__ import annotations

import base64
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from openstack_platform.config import load_platform
from openstack_platform.controller import application_runtime as app
from openstack_platform.helper import production
from openstack_platform.helper.main import HelperActionError
from openstack_platform.runtime import CommandFailure, CommandResult
from openstack_platform.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
BUILD = "11111111-1111-4111-8111-111111111111"


class DeployKeyFetchTests(unittest.TestCase):
    def test_pinned_host_key_is_githubs_published_ed25519_key(self) -> None:
        kind, blob = app.GITHUB_SSH_HOST_KEY.split()
        digest = base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode()
        self.assertEqual(
            (kind, digest.rstrip("=")),
            ("ssh-ed25519", "+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"),
        )
        self.assertEqual(
            app.github_ssh_url("https://github.com/ada/notes"),
            "ssh://git@ssh.github.com:443/ada/notes.git",
        )
        with self.assertRaises(ValidationError):
            app.github_ssh_url("https://example.com/ada/notes")

    def test_fetch_with_a_key_uses_only_that_key_and_githubs_host_key(self) -> None:
        seen: dict[str, object] = {}

        def runner(argv: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
            destination = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path(argv[-1])
            if "init" in argv:
                (destination / ".git").mkdir()
            if "fetch" in argv:
                seen["fetch"] = argv
                command = kwargs["env"]["GIT_SSH_COMMAND"]  # type: ignore[index]
                seen["ssh"] = command
                known = command.split("UserKnownHostsFile=")[1].split()[0]
                seen["known_hosts"] = Path(known)
                seen["known_text"] = Path(known).read_text()
            return SimpleNamespace(stdout=(COMMIT + "\n").encode() if "rev-parse" in argv else b"")

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "id_ed25519"
            key.write_text("private")
            app.acquire_github_commit(
                "https://github.com/ada/private-notes",
                COMMIT,
                Path(directory) / "source",
                command_runner=runner,
                ssh_key=key,
            )
        self.assertIn("ssh://git@ssh.github.com:443/ada/private-notes.git", seen["fetch"])
        command = str(seen["ssh"])
        for option in (
            f"-i {key}",
            "IdentitiesOnly=yes",
            "IdentityAgent=none",
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "GlobalKnownHostsFile=/dev/null",
            "HostKeyAlgorithms=ssh-ed25519",
            "-F /dev/null",
        ):
            self.assertIn(option, command)
        self.assertEqual(seen["known_text"], f"[ssh.github.com]:443 {app.GITHUB_SSH_HOST_KEY}\n")
        self.assertFalse(Path(str(seen["known_hosts"])).exists())

    def test_recent_commits_use_shallow_key_fetch_and_fixed_bounded_fields(self) -> None:
        calls = []

        def runner(argv, **bounds):
            calls.append((argv, bounds))
            output = (
                (
                    COMMIT + "\0" + "Change " + "x" * 250 + "\0Ada\0" + "2026-10-04T00:00:00Z\0"
                ).encode()
                if "log" in argv
                else b""
            )
            return SimpleNamespace(stdout=output)

        key = Path("/private/id_ed25519")
        result = app.recent_github_commits(
            "https://github.com/ada/notes", "main", key, command_runner=runner
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sha"], COMMIT)
        self.assertEqual(len(result[0]["message"]), 200)
        self.assertIn("--depth=5", calls[1][0])
        self.assertIn("--filter=blob:none", calls[1][0])
        self.assertIn("refs/heads/main", calls[1][0])
        self.assertTrue(all(0 < bounds["timeout_seconds"] <= 30 for _, bounds in calls))
        self.assertIn("IdentitiesOnly=yes", calls[1][1]["env"]["GIT_SSH_COMMAND"])
        with self.assertRaises(app.ApplicationError):
            app.recent_github_commits(
                "https://github.com/ada/notes",
                "main",
                key,
                command_runner=lambda *_a, **_k: SimpleNamespace(stdout=b"not parseable"),
            )

    def test_checkout_checks_fetch_exact_source_with_real_build_rules(self) -> None:
        configuration = {
            "schemaVersion": 1,
            "build": {
                "runtime": "node",
                "packages": ["."],
                "buildScript": "build",
                "startScript": "start",
            },
            "runtime": {"port": 3000, "healthPath": "/health"},
            "storageBindings": [],
        }

        def runner(argv, **bounds):
            root = Path(argv[argv.index("-C") + 1]) if "-C" in argv else Path(argv[-1])
            if "init" in argv:
                (root / ".git").mkdir()
            if "checkout" in argv:
                (root / "package.json").write_text('{"scripts":{"start":"node ."}}')
            return SimpleNamespace(stdout=(COMMIT + "\n").encode() if "rev-parse" in argv else b"")

        checked = app.check_github_checkout(
            "https://github.com/ada/notes",
            COMMIT,
            configuration,
            Path("/private/key"),
            command_runner=runner,
        )
        self.assertEqual(
            [item["id"] for item in checked if item["state"] == "problem"],
            ["script:build", "lockfile:."],
        )
        self.assertNotIn("node .", repr(checked))

    def test_access_check_names_problems_without_echoing_github(self) -> None:
        def failing(stderr: bytes):
            def runner(argv: tuple[str, ...], **_kwargs: object) -> object:
                raise CommandFailure(
                    "git failed", CommandResult(tuple(argv), 128, b"", stderr, False, False)
                )

            return runner

        key = Path("/nonexistent/id_ed25519")
        for stderr, problem in (
            (b"git@ssh.github.com: Permission denied (publickey).\n", "key-refused"),
            (b"ERROR: Repository not found.\nfatal: Could not read", "not-found"),
            (b"ssh: connect to host ssh.github.com port 443: Connection timed out", "unavailable"),
        ):
            with self.subTest(problem=problem):
                result = app.check_github_access(
                    "https://github.com/ada/notes", "main", key, command_runner=failing(stderr)
                )
                self.assertEqual(result, {"reachable": False, "head": None, "problem": problem})
        listed = SimpleNamespace(stdout=f"{'b' * 40}\trefs/heads/main\n".encode())
        self.assertEqual(
            app.check_github_access(
                "https://github.com/ada/notes", "main", key, command_runner=lambda *_a, **_k: listed
            ),
            {"reachable": True, "head": "b" * 40, "problem": None},
        )
        empty = SimpleNamespace(stdout=b"")
        self.assertEqual(
            app.check_github_access(
                "https://github.com/ada/notes", "gone", key, command_runner=lambda *_a, **_k: empty
            )["problem"],
            "branch-missing",
        )


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class HelperDeployKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = SimpleNamespace(
            platform=load_platform(ROOT / "config/platform.example.json"),
            root=self.root,
            admin_state=self.root,
        )
        patch = mock.patch.object(production, "helper_runtime", return_value=self.runtime)
        patch.start()
        self.addCleanup(patch.stop)

    def key(self, mode: str) -> dict[str, object]:
        return dict(production._provider_app("app.source.key", {"slug": "notes", "mode": mode}))

    def test_keys_are_created_once_replaced_on_request_and_only_public_halves_leave(self) -> None:
        self.assertEqual(self.key("read"), {"slug": "notes", "present": False})
        created = self.key("create")
        private = self.root / "controller/source-keys/notes/id_ed25519"
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertTrue(str(created["publicKey"]).startswith("ssh-ed25519 "))
        blob = base64.b64decode(str(created["publicKey"]).split()[1])
        self.assertEqual(
            created["fingerprint"],
            "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("="),
        )
        self.assertNotIn("PRIVATE", repr(created))
        self.assertEqual(self.key("create")["publicKey"], created["publicKey"])
        self.assertEqual(self.key("read")["publicKey"], created["publicKey"])
        replaced = self.key("replace")
        self.assertNotEqual(replaced["publicKey"], created["publicKey"])
        self.assertEqual(
            sorted(path.name for path in (self.root / "controller/source-keys").iterdir()),
            ["notes"],
        )
        with self.assertRaises(ValidationError):
            self.key("remove")

    def test_removing_a_key_takes_the_whole_pair_at_once_and_repeats_quietly(self) -> None:
        self.key("create")
        keys = self.root / "controller/source-keys"
        seen: list[list[str]] = []
        original = production.shutil.rmtree

        def rmtree(path, *args, **kwargs):
            # By now the pair has left the app's name in one rename.
            seen.append(sorted(item.name for item in keys.iterdir()))
            return original(path, *args, **kwargs)

        with mock.patch.object(production.shutil, "rmtree", rmtree):
            self.assertEqual(self.key("delete"), {"slug": "notes", "present": False})
        self.assertEqual(len(seen), 1)
        self.assertNotIn("notes", seen[0])
        self.assertTrue(seen[0][0].startswith(".old-"))
        self.assertEqual(list(keys.iterdir()), [])
        self.assertEqual(self.key("delete"), {"slug": "notes", "present": False})
        self.assertEqual(self.key("read"), {"slug": "notes", "present": False})
        self.assertTrue(self.key("create")["present"])

    def test_removing_a_key_refuses_a_link_in_place_of_the_key_directory(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "id_ed25519").write_text("not a deploy key")
        keys = self.root / "controller/source-keys"
        keys.mkdir(parents=True, mode=0o700)
        (self.root / "controller").chmod(0o700)
        (keys / "notes").symlink_to(elsewhere)
        with self.assertRaises(HelperActionError) as error:
            self.key("delete")
        self.assertEqual(error.exception.code, "INVALID_STATE")
        self.assertEqual((elsewhere / "id_ed25519").read_text(), "not a deploy key")
        self.assertTrue((keys / "notes").is_symlink())

    def test_generated_key_archive_preserves_public_creation_time(self) -> None:
        from openstack_platform.controller import database as controller_db
        from openstack_platform.controller.source_key_backup import (
            commit_source_key_restore,
            prepare_source_key_restore,
            write_source_key_archive,
        )

        created = self.key("create")
        connection = controller_db.connect(self.root / "platform.sqlite3")
        controller_db.migrate(connection)
        try:
            controller_db.put_application(
                connection,
                application_id=BUILD,
                application_slug="notes",
                worker_flavor="small",
                scheduler_cpu_mhz=500,
                scheduler_memory_mib=512,
            )
            archive = self.root / "keys.tar"
            write_source_key_archive(connection, self.root / "controller/source-keys", archive)
            target = self.root / "restored"
            commit_source_key_restore(
                prepare_source_key_restore(archive, connection, target), target
            )
            self.assertEqual(
                " ".join((target / "notes/id_ed25519.pub").read_text().split()[:2]),
                created["publicKey"],
            )
            self.assertEqual(
                (target / "notes/id_ed25519.pub").stat().st_mtime_ns,
                (self.root / "controller/source-keys/notes/id_ed25519.pub").stat().st_mtime_ns,
            )
        finally:
            connection.close()

    def test_private_source_reads_require_a_key_and_suppress_git_errors(self) -> None:
        args = {"slug": "notes", "repository": "https://github.com/ada/notes", "branch": "main"}
        self.assertEqual(
            production._provider_app("app.source.commits", args), {"keyPresent": False}
        )
        self.key("create")
        with mock.patch.object(
            app, "recent_github_commits", side_effect=CommandFailure("private stderr secret")
        ):
            with self.assertRaises(HelperActionError) as error:
                production._provider_app("app.source.commits", args)
        self.assertEqual(error.exception.code, "SOURCE_UNAVAILABLE")
        self.assertNotIn("secret", str(error.exception))

    def test_access_check_uses_the_apps_key_and_needs_one(self) -> None:
        args = {"slug": "notes", "repository": "https://github.com/ada/notes", "branch": "main"}
        self.assertEqual(production._provider_app("app.source.check", args), {"keyPresent": False})
        self.key("create")
        evidence = {"reachable": True, "head": "c" * 40, "problem": None}
        with mock.patch.object(app, "check_github_access", return_value=evidence) as check:
            result = production._provider_app("app.source.check", args)
        self.assertEqual(result, {"keyPresent": True, **evidence})
        self.assertEqual(
            check.call_args.args[2], self.root / "controller/source-keys/notes/id_ed25519"
        )


class BuildSourceFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = SimpleNamespace(
            platform=load_platform(ROOT / "config/platform.example.json"),
            root=self.root,
            admin_state=self.root,
        )
        self.arguments = {
            "buildId": BUILD,
            "slug": "notes",
            "repository": "https://github.com/ada/private-notes",
            "requestedRef": "main",
            "commit": COMMIT,
            "configurationRevision": 1,
            "configuration": {
                "schemaVersion": 1,
                "build": {
                    "runtime": "node",
                    "packages": ["."],
                    "buildScript": None,
                    "startScript": "start",
                },
                "runtime": {"port": 8080, "healthPath": "/"},
                "storageBindings": [],
            },
            "builderImageId": "22222222-2222-4222-8222-222222222222",
            "builderFlavor": "builder-small",
            "runtimeImages": {
                "node": "registry.example/node@sha256:" + "a" * 64,
                "bun": "registry.example/bun@sha256:" + "b" * 64,
            },
            "sourceLimit": 1024,
            "buildLogLimit": 4096,
            "connectSeconds": 5,
            "deadlineAt": "2030-01-01T00:00:00Z",
        }

    def build(self, fetches: list[object], key: bool) -> tuple[mock.Mock, str]:
        if key:
            directory = production._source_key_directory(self.runtime, "notes")
            directory.mkdir(mode=0o700)
            (directory / "id_ed25519").write_text("private")
            (directory / "id_ed25519").chmod(0o600)
        with (
            mock.patch.object(production, "helper_runtime", return_value=self.runtime),
            mock.patch.object(app, "acquire_github_commit", side_effect=fetches) as acquire,
            mock.patch.object(production, "validate_checkout", return_value=None),
            mock.patch.object(
                app,
                "build_with_disposable_builder",
                side_effect=app.BuildRejected("stop after the fetch"),
            ),
        ):
            with self.assertRaises(HelperActionError) as caught:
                production._build_application(self.arguments)
        log = (self.root / f"controller/build-logs/notes/{BUILD}.log").read_text()
        self.code = caught.exception.code
        return acquire, log

    def test_private_repository_falls_back_to_the_deploy_key(self) -> None:
        acquire, log = self.build([CommandFailure("https refused"), None], key=True)
        self.assertEqual(self.code, "BUILD_REJECTED")  # the fake builder's rejection
        self.assertEqual(
            [call.kwargs["ssh_key"] for call in acquire.call_args_list],
            [
                None,
                self.root / "controller/source-keys/notes/id_ed25519",
            ],
        )
        self.assertIn("using the app's deploy key", log)

    def test_unfetchable_source_is_a_named_rejection(self) -> None:
        acquire, log = self.build([CommandFailure("https refused")], key=False)
        self.assertEqual((self.code, acquire.call_count), ("SOURCE_REJECTED", 1))
        self.assertIn("add the app's deploy key", log)

    def test_a_refused_key_is_also_a_named_rejection(self) -> None:
        acquire, log = self.build(
            [CommandFailure("https refused"), CommandFailure("key refused")], key=True
        )
        self.assertEqual((self.code, acquire.call_count), ("SOURCE_REJECTED", 2))
        self.assertIn("couldn't fetch this commit either", log)


if __name__ == "__main__":
    unittest.main()
