from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from platform_cli import runtime
from platform_cli.helper.main import (
    HelperActionError,
    accept_staged_backup,
    serve_once,
)
from platform_cli.remote import (
    DependencyUnavailable,
    ProtocolError,
    call_helper,
    encode_failure,
    encode_request,
    encode_success,
    parse_request,
    parse_response,
    pinned_admin_scp,
)
from platform_cli.runtime import (
    CommandFailure,
    CommandTimedOut,
    LockBusy,
    append_private_log,
    child_environment,
    lock,
    run,
    write_private_stack_diagnostic,
)


class RuntimeTests(unittest.TestCase):
    def test_feature_locks_conflict_only_in_the_same_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            app_scope = "app-00000000-0000-4000-8000-000000000001"
            with lock(state, "infrastructure"):
                with self.assertRaises(LockBusy), lock(state, "infrastructure"):
                    pass
                with lock(state, app_scope), self.assertRaises(LockBusy):
                    with lock(state, app_scope):
                        pass
            with lock(state, "database-maintenance"):
                pass
            self.assertEqual((state / "infrastructure.lock").stat().st_mode & 0o777, 0o600)

    def test_waiting_lock_honors_an_absolute_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with lock(state, "infrastructure"):
                started = time.monotonic()
                with (
                    self.assertRaises(LockBusy),
                    lock(
                        state,
                        "infrastructure",
                        wait=True,
                        deadline=started + 0.1,
                    ),
                ):
                    pass
                self.assertLess(time.monotonic() - started, 1)

    def test_subprocess_output_and_time_are_bounded(self) -> None:
        result = run(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.write('x'*10000);sys.stderr.write('y'*10000)",
            ],
            timeout_seconds=5,
            stdout_limit=100,
            stderr_limit=80,
        )
        self.assertEqual(len(result.stdout), 100)
        self.assertEqual(len(result.stderr), 80)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        started = time.monotonic()
        with self.assertRaises(CommandTimedOut):
            run(
                [sys.executable, "-c", "import time;time.sleep(10)"],
                timeout_seconds=0.1,
            )
        self.assertLess(time.monotonic() - started, 2)

    def test_command_failures_and_logs_do_not_leak_explicit_secrets(self) -> None:
        sentinel = "sentinel-credential-value"
        with self.assertRaises(CommandFailure) as caught:
            run(
                [sys.executable, "-c", "raise SystemExit(2)", "--token", sentinel],
                timeout_seconds=5,
                secrets=(sentinel,),
            )
        self.assertNotIn(sentinel, str(caught.exception))
        assert caught.exception.result is not None
        self.assertNotIn(sentinel, repr(caught.exception.result))
        self.assertEqual(caught.exception.result.argv[-1], "[REDACTED]")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "operations.log"
            append_private_log(
                path, f"password={sentinel}", maximum_bytes=1024, secrets=(sentinel,)
            )
            self.assertNotIn(sentinel, path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_private_stack_diagnostic_contains_locations_without_exception_data(self) -> None:
        sentinel = "provider-payload-and-local-secret"
        try:
            exec(
                compile(
                    f"private_local={sentinel!r}; raise RuntimeError({sentinel!r})",
                    f"/tmp/{sentinel}.py",
                    "exec",
                ),
                {},
            )
        except RuntimeError as error:
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "diagnostics"
                path = write_private_stack_diagnostic(
                    directory,
                    error,
                    correlation_id="00000000-0000-4000-8000-000000000123",
                )
                payload = path.read_text()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                self.assertIn(
                    "correlation-id=00000000-0000-4000-8000-000000000123",
                    payload,
                )
                self.assertIn("<external>:", payload)
                self.assertNotIn(sentinel, payload)
                self.assertNotIn("RuntimeError", payload)
                self.assertNotIn("private_local", payload)

    def test_child_environment_uses_an_explicit_allowlist(self) -> None:
        os.environ["FOUNDATION_ALLOWED"] = "yes"
        os.environ["FOUNDATION_SECRET"] = "no"
        environment = child_environment(
            inherit=("FOUNDATION_ALLOWED",), overrides={"FIXED": "value"}
        )
        self.assertEqual(environment["FOUNDATION_ALLOWED"], "yes")
        self.assertEqual(environment["FIXED"], "value")
        self.assertNotIn("FOUNDATION_SECRET", environment)


class ProtocolTests(unittest.TestCase):
    REQUEST_ID = "00000000-0000-4000-8000-000000000123"

    def test_request_and_response_round_trip_is_strict(self) -> None:
        request_id, payload = encode_request(
            "app.observe", {"slug": "demo-app"}, request_id=self.REQUEST_ID
        )
        request = parse_request(payload)
        self.assertEqual(request.request_id, request_id)
        self.assertEqual(request.action, "app.observe")
        response = parse_response(
            encode_success(request_id, {"state": "ready"}),
            expected_request_id=request_id,
        )
        self.assertEqual(response.result, {"state": "ready"})
        failure = parse_response(
            encode_failure(request_id, "UNAVAILABLE", "Nomad is unavailable"),
            expected_request_id=request_id,
        )
        self.assertEqual(failure.error_code, "UNAVAILABLE")

    def test_malformed_version_unknown_fields_duplicates_extra_output_and_size_are_rejected(
        self,
    ) -> None:
        base = {
            "version": 1,
            "requestId": self.REQUEST_ID,
            "action": "app.observe",
            "args": {},
        }
        invalid_payloads = []
        for update in (
            {"version": 2},
            {"extra": True},
            {"args": []},
            {"action": "invalid action"},
        ):
            value = dict(base)
            value.update(update)
            invalid_payloads.append(json.dumps(value).encode())
        invalid_payloads.append(
            b'{"version":1,"version":1,"requestId":"'
            + self.REQUEST_ID.encode()
            + b'","action":"app.observe","args":{}}'
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises((ProtocolError, ValueError)):
                parse_request(payload)
        with self.assertRaises(ProtocolError):
            parse_request(json.dumps(base).encode(), maximum_bytes=10)
        with self.assertRaisesRegex(ProtocolError, "extra output"):
            parse_response(
                encode_success(self.REQUEST_ID, {}) + b"\nprogress",
                expected_request_id=self.REQUEST_ID,
            )

    def test_response_request_id_and_shape_must_match(self) -> None:
        other = "00000000-0000-4000-8000-000000000124"
        with self.assertRaisesRegex(ProtocolError, "does not match"):
            parse_response(encode_success(other, {}), expected_request_id=self.REQUEST_ID)
        value = json.loads(encode_success(self.REQUEST_ID, {}))
        value["debug"] = "not allowed"
        with self.assertRaises(ProtocolError):
            parse_response(json.dumps(value), expected_request_id=self.REQUEST_ID)

    def test_call_helper_uses_only_fixed_ssh_argv_and_stdin(self) -> None:
        captured: dict[str, object] = {}

        class Completed:
            stdout = encode_success(self.REQUEST_ID, {"keys": ["PORT"]})

        def runner(argv: object, **kwargs: object) -> Completed:
            captured["argv"] = argv
            captured.update(kwargs)
            return Completed()

        result = call_helper(
            "app.observe",
            {"slug": "demo-app"},
            timeout_seconds=5,
            request_id=self.REQUEST_ID,
            command_runner=runner,
        )
        self.assertEqual(
            captured["argv"],
            (
                "ssh",
                "-F",
                "/srv/openstack-platform/.secrets/ssh/config",
                "platform-admin",
                "--",
                "openstack-platform-helper",
            ),
        )
        self.assertNotIn("demo-app", captured["argv"])
        self.assertIn(b"demo-app", captured["stdin"])
        self.assertEqual(result, {"keys": ["PORT"]})
        self.assertEqual(
            pinned_admin_scp("/tmp/backup.age", "/srv/backups/.staging/backup.age"),
            (
                "scp",
                "-F",
                "/srv/openstack-platform/.secrets/ssh/config",
                "--",
                "/tmp/backup.age",
                "platform-admin:/srv/backups/.staging/backup.age",
            ),
        )

    def test_call_helper_types_ssh_and_helper_dependency_outages(self) -> None:
        def unavailable_runner(_argv: object, **_kwargs: object) -> None:
            raise CommandFailure("private SSH diagnostic")

        with self.assertRaises(DependencyUnavailable) as ssh_failure:
            call_helper(
                "app.observe",
                {},
                timeout_seconds=5,
                request_id=self.REQUEST_ID,
                command_runner=unavailable_runner,
            )
        self.assertEqual(ssh_failure.exception.code, "DEPENDENCY_UNAVAILABLE")
        self.assertNotIn("private SSH diagnostic", str(ssh_failure.exception))

        class Completed:
            stdout = encode_failure(
                self.REQUEST_ID,
                "DEPENDENCY_UNAVAILABLE",
                "Nomad credentials are unavailable",
            )

        with self.assertRaises(DependencyUnavailable) as helper_failure:
            call_helper(
                "app.observe",
                {},
                timeout_seconds=5,
                request_id=self.REQUEST_ID,
                command_runner=lambda _argv, **_kwargs: Completed(),
            )
        self.assertEqual(helper_failure.exception.code, "DEPENDENCY_UNAVAILABLE")
        self.assertEqual(helper_failure.exception.message, "Nomad credentials are unavailable")

    def test_helper_dispatch_rejects_unknown_and_never_renders_handler_exception(self) -> None:
        def exploding(_args: object) -> object:
            raise RuntimeError("password=sentinel-secret")

        request_id, request = encode_request("app.observe", {}, request_id=self.REQUEST_ID)
        output = BytesIO()
        with tempfile.TemporaryDirectory() as temporary:
            diagnostic_directory = Path(temporary) / "helper-diagnostics"
            serve_once(
                BytesIO(request),
                output,
                {"app.observe": exploding},  # type: ignore[arg-type]
                diagnostic_directory=diagnostic_directory,
            )
            diagnostic = diagnostic_directory / f"{request_id}.trace"
            self.assertTrue(diagnostic.is_file())
            self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
            private_payload = diagnostic.read_text()
            self.assertIn("platform_cli/helper/main.py:", private_payload)
            self.assertNotIn("sentinel-secret", private_payload)
            self.assertNotIn("RuntimeError", private_payload)
        response = parse_response(output.getvalue(), expected_request_id=request_id)
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "ACTION_FAILED")
        self.assertIn(request_id, response.error_message or "")
        self.assertNotIn(b"sentinel-secret", output.getvalue())

        output = BytesIO()
        serve_once(BytesIO(request), output, {})
        response = parse_response(output.getvalue(), expected_request_id=request_id)
        self.assertEqual(response.error_code, "UNKNOWN_ACTION")

    def test_helper_replaces_an_oversized_result_with_a_safe_failure(self) -> None:
        request_id, request = encode_request("app.observe", {}, request_id=self.REQUEST_ID)
        output = BytesIO()
        serve_once(
            BytesIO(request),
            output,
            {"app.observe": lambda _args: {"data": "x" * 1_000}},
            response_limit=200,
        )
        response = parse_response(output.getvalue(), expected_request_id=request_id)
        self.assertEqual(response.error_code, "RESPONSE_TOO_LARGE")
        self.assertLessEqual(len(output.getvalue()), 200)

    def test_helper_returns_bounded_failure_for_bad_or_oversized_input(self) -> None:
        output = BytesIO()
        serve_once(BytesIO(b"not-json"), output, {}, request_limit=100)
        response = parse_response(
            output.getvalue(), expected_request_id="00000000-0000-0000-0000-000000000000"
        )
        self.assertEqual(response.error_code, "INVALID_REQUEST")
        output = BytesIO()
        serve_once(BytesIO(b"x" * 101), output, {}, request_limit=100)
        response = parse_response(
            output.getvalue(), expected_request_id="00000000-0000-0000-0000-000000000000"
        )
        self.assertEqual(response.error_code, "REQUEST_TOO_LARGE")


class BackupAcceptanceTests(unittest.TestCase):
    def test_checksum_atomic_acceptance_and_retention_are_real(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            staging = root / ".staging"
            staging.mkdir(mode=0o700)
            names = [
                "platform-20260101T000000Z.sqlite3.age",
                "platform-20260102T000000Z.sqlite3.age",
            ]
            for name in names:
                source = staging / name
                source.write_bytes(b"age-encryption.org/v1\n" + name.encode())
                source.chmod(0o600)
                result = accept_staged_backup(
                    staging_directory=staging,
                    backup_directory=root,
                    name=name,
                    expected_sha256=hashlib.sha256(
                        b"age-encryption.org/v1\n" + name.encode()
                    ).hexdigest(),
                    plaintext_sha256="a" * 64,
                    integrity_checked_at=name[9:25],
                    retention_count=1,
                )
                self.assertEqual(result["name"], name)
                self.assertFalse(source.exists())
            self.assertFalse((root / names[0]).exists())
            self.assertTrue((root / names[1]).exists())
            self.assertEqual((root / names[1]).stat().st_mode & 0o777, 0o600)
            self.assertTrue((root / f"{names[1]}.sha256").is_file())
            manifest = (root / f"{names[1]}.manifest").read_text()
            self.assertIn("sqlite_integrity=ok\n", manifest)
            self.assertIn("encryption=age-v1\n", manifest)

    def test_interrupted_promotion_is_reconciled_by_a_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            staging = root / ".staging"
            staging.mkdir(mode=0o700)
            name = "platform-20260101T000000Z.sqlite3.age"
            payload = b"age-encryption.org/v1\ncrash-safe"
            source = staging / name
            source.write_bytes(payload)
            source.chmod(0o600)
            expected = hashlib.sha256(payload).hexdigest()
            original_replace = os.replace
            for failure_number in (1, 2, 3):
                if failure_number != 1:
                    source.write_bytes(payload)
                    source.chmod(0o600)
                    for suffix in ("", ".sha256", ".manifest"):
                        (root / f"{name}{suffix}").unlink(missing_ok=True)
                calls = 0

                def fail_once(
                    left: object, right: object, *, failure: int = failure_number
                ) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == failure:
                        raise RuntimeError("injected crash")
                    original_replace(left, right)

                with mock.patch.object(os, "replace", side_effect=fail_once):
                    with self.assertRaisesRegex(RuntimeError, "injected crash"):
                        accept_staged_backup(
                            staging_directory=staging,
                            backup_directory=root,
                            name=name,
                            expected_sha256=expected,
                            plaintext_sha256="a" * 64,
                            integrity_checked_at="20260101T000000Z",
                        )
                result = accept_staged_backup(
                    staging_directory=staging,
                    backup_directory=root,
                    name=name,
                    expected_sha256=expected,
                    plaintext_sha256="a" * 64,
                    integrity_checked_at="20260101T000000Z",
                )
                self.assertEqual(result["sha256"], expected)
                self.assertFalse(source.exists())
                self.assertTrue((root / name).is_file())
                self.assertTrue((root / f"{name}.sha256").is_file())
                self.assertTrue((root / f"{name}.manifest").is_file())

    def test_non_age_content_is_not_promoted_even_with_matching_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            staging = root / ".staging"
            staging.mkdir(mode=0o700)
            name = "platform-20260101T000000Z.sqlite3.age"
            source = staging / name
            source.write_bytes(b"plausible-sized-but-not-age")
            source.chmod(0o600)
            with self.assertRaisesRegex(HelperActionError, "age v1"):
                accept_staged_backup(
                    staging_directory=staging,
                    backup_directory=root,
                    name=name,
                    expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    plaintext_sha256="a" * 64,
                    integrity_checked_at="20260101T000000Z",
                )
            self.assertTrue(source.exists())
            self.assertFalse((root / name).exists())

    def test_bad_checksum_is_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            staging = root / ".staging"
            staging.mkdir(mode=0o700)
            name = "platform-20260101T000000Z.sqlite3.age"
            source = staging / name
            source.write_bytes(b"ciphertext")
            source.chmod(0o600)
            with self.assertRaisesRegex(HelperActionError, "checksum"):
                accept_staged_backup(
                    staging_directory=staging,
                    backup_directory=root,
                    name=name,
                    expected_sha256="0" * 64,
                    plaintext_sha256="a" * 64,
                    integrity_checked_at="20260101T000000Z",
                )
            self.assertTrue(source.exists())
            self.assertFalse((root / name).exists())


class BoundedHttpAgentTests(unittest.TestCase):
    def _captured_request(self, **kwargs: object) -> object:
        captured: list[object] = []

        class _Response:
            status = 200
            headers: dict[str, str] = {}

            def read(self, _limit: int) -> bytes:
                return b"ok"

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *_: object) -> None:
                return None

        class _Opener:
            def open(self, request: object, timeout: float) -> object:
                captured.append(request)
                return _Response()

        with mock.patch.object(runtime.urllib.request, "build_opener", return_value=_Opener()):
            runtime.bounded_http("https://example.test/healthz", **kwargs)  # type: ignore[arg-type]
        return captured[0]

    def test_a_named_agent_is_sent_by_default(self) -> None:
        # Public routes often sit behind a bot filter that rejects the language
        # default agent with 403, which would make a healthy route unverifiable.
        request = self._captured_request()
        self.assertEqual(
            request.get_header("User-agent"),  # type: ignore[attr-defined]
            runtime.HTTP_USER_AGENT,
        )
        self.assertNotIn("python-urllib", runtime.HTTP_USER_AGENT.lower())

    def test_an_explicit_agent_is_preserved(self) -> None:
        request = self._captured_request(headers={"User-Agent": "caller/9"})
        self.assertEqual(request.get_header("User-agent"), "caller/9")  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
