"""Private files, locks, bounded processes/HTTP, and safe diagnostics."""

from __future__ import annotations

import fcntl
import math
import os
import re
import signal
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from . import durable


class RuntimeFailure(RuntimeError):
    """A failure whose string form is safe for operator output."""


class LockBusy(RuntimeFailure):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    stdout_truncated: bool
    stderr_truncated: bool


class CommandFailure(RuntimeFailure):
    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        self.result = result
        super().__init__(message)


class CommandTimedOut(CommandFailure):
    pass


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|credential|private[_-]?key|access[_-]?key)\s*([=:])\s*([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URI_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)
_PEM = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)
_SENSITIVE_FLAGS = {
    "--password",
    "--passwd",
    "--secret",
    "--token",
    "--credential",
    "--private-key",
    "--access-key",
    "--identity",
}
_LOCK_NAME = re.compile(
    r"(?:infrastructure|database-maintenance|"
    r"app-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_CORRELATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def redact_text(value: object, *, secrets: Sequence[str | bytes] = ()) -> str:
    """Redact explicitly supplied values and common accidental secret forms."""
    text = str(value)
    for secret in sorted(
        (
            item.decode("utf-8", errors="ignore") if isinstance(item, bytes) else item
            for item in secrets
        ),
        key=len,
        reverse=True,
    ):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _URI_USERINFO.sub(lambda match: f"{match.group('scheme')}[REDACTED]@", text)
    text = _PEM.sub("[REDACTED PEM]", text)
    return text


def safe_summary(
    error: BaseException | str, *, secrets: Sequence[str | bytes] = (), maximum: int = 1_024
) -> str:
    """Return one bounded, single-line diagnostic suitable for SQLite/output.

    Arbitrary exception text is not trusted because provider exceptions often
    embed response bodies or credentials. Callers may pass a deliberate fixed
    string when an operator-safe explanation is available.
    """
    if isinstance(error, BaseException):
        summary = f"{error.__class__.__name__}: details redacted"
    else:
        summary = redact_text(error, secrets=secrets).replace("\x00", "?")
    summary = " ".join(summary.splitlines()).strip() or error.__class__.__name__
    encoded = summary.encode("utf-8")
    if len(encoded) > maximum:
        summary = encoded[: maximum - 3].decode("utf-8", errors="ignore") + "..."
    return summary


def safe_argv(argv: Sequence[str], *, secrets: Sequence[str | bytes] = ()) -> tuple[str, ...]:
    """Produce a diagnostic argv with sensitive flags and values redacted."""
    result: list[str] = []
    redact_next = False
    for argument in argv:
        if redact_next:
            result.append("[REDACTED]")
            redact_next = False
            continue
        flag, separator, _value = argument.partition("=")
        if flag.lower() in _SENSITIVE_FLAGS:
            if separator:
                result.append(f"{flag}=[REDACTED]")
            else:
                result.append(argument)
                redact_next = True
            continue
        result.append(redact_text(argument, secrets=secrets))
    return tuple(result)


def ensure_private_directory(path: str | Path, *, create: bool = True) -> Path:
    """Require a direct, current-user-owned mode-0700 directory."""
    directory = Path(path)
    if create:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeFailure(f"private state path is not a direct directory: {directory}")
    if metadata.st_uid != os.geteuid():
        raise RuntimeFailure(
            f"private state directory is not owned by the current user: {directory}"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeFailure(f"private state directory permissions must be 0700: {directory}")
    return directory


def _private_file(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise RuntimeFailure(f"could not safely open private file: {path}") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise RuntimeFailure(f"private file must be current-user-owned mode 0600: {path}")
    return descriptor


def write_private_stack_diagnostic(
    directory: str | Path,
    error: BaseException,
    *,
    correlation_id: str,
    maximum_frames: int = 64,
) -> Path:
    """Write an exception's safe stack locations to one mode-0600 file.

    Traceback rendering is deliberately avoided because it can include exception
    text, source lines, provider payloads, and locals. Only package-relative
    source locations and line numbers are retained. Untrusted or external file
    names are represented by the fixed ``<external>`` marker.
    """
    if not _CORRELATION_ID.fullmatch(correlation_id):
        raise ValueError("diagnostic correlation ID must be a canonical UUID")
    if not 1 <= maximum_frames <= 256:
        raise ValueError("diagnostic frame limit must be from 1 through 256")
    destination_directory = ensure_private_directory(directory)
    trusted_root = Path(__file__).resolve().parent
    locations: list[str] = []
    traceback = error.__traceback__
    while traceback is not None and len(locations) < maximum_frames:
        source = Path(traceback.tb_frame.f_code.co_filename)
        try:
            resolved = source.resolve(strict=True)
            relative = resolved.relative_to(trusted_root)
            location = f"openstack_platform/{relative.as_posix()}"
        except (OSError, ValueError):
            location = "<external>"
        locations.append(f"{location}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    if traceback is not None:
        locations.append("<truncated>")
    if not locations:
        locations.append("<unavailable>")

    path = destination_directory / f"{correlation_id}.trace"
    payload = (f"correlation-id={correlation_id}\n" + "\n".join(locations) + "\n").encode("ascii")
    try:
        durable.atomic_write(path, payload, mode=0o600, maximum_bytes=32_768)
    except durable.DurableReplaceError as replacement_error:
        raise RuntimeFailure("could not persist private diagnostic safely") from replacement_error
    return path


@contextmanager
def lock(
    state_directory: str | Path,
    scope: str,
    *,
    wait: bool = False,
    deadline: float | None = None,
) -> Iterator[None]:
    """Hold one long-lived feature ``flock`` without involving SQLite.

    A waiting lock must use non-blocking probes when a monotonic absolute
    ``deadline`` is supplied.  A blocking ``flock`` cannot be interrupted by
    the command's deadline, so using it for a CLI operation would let a
    second command outlive every other bound in that operation.
    """
    if not _LOCK_NAME.fullmatch(scope):
        raise ValueError("lock scope must be infrastructure, database-maintenance, or app-<UUID>")
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(float(deadline))
    ):
        raise ValueError("lock deadline must be a finite monotonic timestamp")
    selected_deadline = None if deadline is None else float(deadline)
    directory = ensure_private_directory(state_directory)
    descriptor = _private_file(directory / f"{scope}.lock", create=True)
    acquired = False
    try:
        if selected_deadline is None or not wait:
            if selected_deadline is not None and time.monotonic() >= selected_deadline:
                raise LockBusy(f"could not acquire the {scope} lock before the deadline")
            flags = fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, flags)
            except BlockingIOError as error:
                raise LockBusy(f"another operation holds the {scope} lock") from error
            acquired = True
            if selected_deadline is not None and time.monotonic() >= selected_deadline:
                raise LockBusy(f"could not acquire the {scope} lock before the deadline")
        else:
            while True:
                remaining = selected_deadline - time.monotonic()
                if remaining <= 0:
                    raise LockBusy(f"could not acquire the {scope} lock before the deadline")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(min(0.05, remaining))
                    continue
                acquired = True
                break
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        else:
            os.close(descriptor)


# The role images are NixOS, where system commands live in the activated system
# profile and /usr/bin holds almost nothing. A child environment that offered
# only the FHS directories could not start git at all, so source acquisition
# failed before it began. Both layouts are listed so the same default works on
# a NixOS guest and on an FHS operator host.
DEFAULT_CHILD_PATH = "/run/current-system/sw/bin:/usr/bin:/bin"


def child_environment(
    *,
    inherit: Sequence[str] = (),
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment only from explicitly allowlisted names."""
    environment = {name: os.environ[name] for name in inherit if name in os.environ}
    environment.setdefault("PATH", DEFAULT_CHILD_PATH)
    environment.setdefault("LANG", "C.UTF-8")
    for name, value in (overrides or {}).items():
        if not name or "=" in name or "\x00" in name or "\x00" in value:
            raise ValueError("child environment contains an invalid name or value")
        environment[name] = value
    return environment


class _Collector:
    def __init__(self, maximum: int, *, sink: BinaryIO | None = None) -> None:
        if maximum < 0:
            raise ValueError("output limit must not be negative")
        self.maximum = maximum
        self.sink = sink
        self.parts: list[bytes] = []
        self.size = 0
        self.truncated = False
        self.sink_error: OSError | None = None

    def read(self, stream: BinaryIO) -> None:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                return
            remaining = self.maximum - self.size
            if remaining > 0:
                kept = chunk[:remaining]
                self.parts.append(kept)
                self.size += len(kept)
                if self.sink is not None:
                    try:
                        self.sink.write(kept)
                        self.sink.flush()
                    except OSError as error:
                        self.sink_error = error
                        self.sink = None
            if len(chunk) > max(remaining, 0):
                self.truncated = True

    def value(self) -> bytes:
        return b"".join(self.parts)


def run(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    stdin: bytes | None = None,
    stdout_limit: int = 1_048_576,
    stderr_limit: int = 262_144,
    inherit_env: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    secrets: Sequence[str | bytes] = (),
    check: bool = True,
    stderr_sink: BinaryIO | None = None,
) -> CommandResult:
    """Run an argument vector with a deadline and continuously bounded output.

    Output is drained after the limit and discarded, preventing child-process
    deadlock without retaining unbounded or secret-bearing diagnostics.
    Exception messages include only a redacted argument vector and status.
    """
    arguments = tuple(argv)
    if (
        not arguments
        or timeout_seconds <= 0
        or any(not isinstance(item, str) or not item or "\x00" in item for item in arguments)
    ):
        raise ValueError("command requires a non-empty NUL-free argv and positive timeout")
    diagnostic = safe_argv(arguments, secrets=secrets)
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            cwd=cwd,
            env=child_environment(inherit=inherit_env, overrides=env),
            start_new_session=True,
        )
    except OSError as error:
        raise CommandFailure(
            f"could not start command {diagnostic[0]!r}: {error.__class__.__name__}"
        ) from error

    assert process.stdout is not None and process.stderr is not None
    stdout = _Collector(stdout_limit)
    stderr = _Collector(stderr_limit, sink=stderr_sink)
    readers = [
        threading.Thread(target=stdout.read, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.read, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()

    writer: threading.Thread | None = None
    if stdin is not None:
        assert process.stdin is not None

        def write_input() -> None:
            try:
                process.stdin.write(stdin)
                process.stdin.close()
            except BrokenPipeError:
                pass

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()

    def stop_process() -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    timed_out = False
    interrupted: BaseException | None = None
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_process()
    except BaseException as error:
        interrupted = error
        stop_process()
    finally:
        if writer is not None:
            writer.join(timeout=1)
        for reader in readers:
            reader.join(timeout=2)
        process.stdout.close()
        process.stderr.close()

    if interrupted is not None:
        raise interrupted
    if stderr.sink_error is not None:
        raise CommandFailure("could not persist command diagnostic output") from stderr.sink_error

    result = CommandResult(
        argv=diagnostic,
        returncode=process.returncode,
        stdout=stdout.value(),
        stderr=stderr.value(),
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
    )
    if timed_out:
        raise CommandTimedOut(
            f"command {diagnostic[0]!r} exceeded its {timeout_seconds:g}-second deadline",
            result,
        )
    if check and result.returncode != 0:
        raise CommandFailure(
            f"command {diagnostic[0]!r} exited with status {result.returncode}",
            result,
        )
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        _req: object,
        _fp: object,
        _code: int,
        _msg: str,
        _headers: object,
        _newurl: str,
    ) -> None:
        return None


HTTP_USER_AGENT = "openstack-platform-health/1"


def bounded_http(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 30,
    response_limit: int = 1_048_576,
    ssl_context: object | None = None,
    allow_redirects: bool = False,
) -> HttpResult:
    """Perform one HTTP phase with bounded response data and no redirects by default."""
    if timeout_seconds <= 0 or response_limit < 0:
        raise ValueError("HTTP timeout and response limit must be bounded")
    # Identify the client. Public routes commonly sit behind a bot filter that
    # rejects the language default agent outright: the platform's own public
    # ingress health check received 403 from the edge for every probe while the
    # same URL answered 200 to any named agent, which made a healthy ingress
    # impossible to verify through its public route. Callers may override it.
    sent_headers = dict(headers or {})
    if not any(key.lower() == "user-agent" for key in sent_headers):
        sent_headers["User-Agent"] = HTTP_USER_AGENT
    request = urllib.request.Request(url, data=data, method=method, headers=sent_headers)
    handlers: list[object] = [] if allow_redirects else [_NoRedirect()]
    if ssl_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read(response_limit + 1)
            if len(body) > response_limit:
                raise RuntimeFailure("HTTP response exceeded its configured size limit")
            return HttpResult(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=body,
            )
    except urllib.error.HTTPError as error:
        error.read(min(response_limit, 65_536))
        raise RuntimeFailure(f"HTTP request failed with status {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeFailure(f"HTTP request failed: {error.__class__.__name__}") from None
