"""Session lifetimes for setup and disposable image publication processes."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from . import provider
from .provider_api import API
from .provider_protocol import ProviderError
from .provider_service import Server


@lru_cache(maxsize=1)
def _api(environment: tuple[tuple[str, str], ...]) -> API:
    return API(dict(environment))


def session(environment: Mapping[str, str]) -> API:
    return _api(
        tuple(
            sorted(
                (key, value)
                for key, value in environment.items()
                if key.startswith("OS_") or key == "PLATFORM_ALLOW_HTTP_GLANCE"
            )
        )
    )


def read(
    arguments: tuple[str, ...], environment: Mapping[str, str], *, timeout_seconds: float = 60
) -> bytes:
    request = provider.parse(arguments)
    api = _api(
        tuple(
            sorted(
                (key, value)
                for key, value in environment.items()
                if key.startswith("OS_") or key == "PLATFORM_ALLOW_HTTP_GLANCE"
            )
        )
    )
    return provider.render(
        request,
        api.execute(request.action, request.params, time.monotonic() + min(60, timeout_seconds)),
    )


@contextmanager
def publication(environment: Mapping[str, str], mutation_command: str) -> Iterator[dict[str, str]]:
    """Share one local provider over concurrent publication subprocesses."""
    resolved_command = shutil.which(mutation_command, path=environment.get("PATH", os.defpath))
    if resolved_command is None:
        raise ProviderError("configuration")
    mutation_command = str(Path(resolved_command).absolute())
    with tempfile.TemporaryDirectory(prefix="platform-provider-") as directory:
        socket_path = directory + "/provider.sock"
        server = Server(socket_path, API(environment), frozenset({(os.geteuid(), os.getegid())}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        wrapper = Path(directory) / "provider"
        wrapper.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " -m openstack_platform.provider_command"
            + " --socket "
            + shlex.quote(socket_path)
            + " --provider-uid "
            + str(os.geteuid())
            + " --mutation-command "
            + shlex.quote(mutation_command)
            + ' -- "$@"\n'
        )
        wrapper.chmod(0o700)
        try:
            yield {
                **environment,
                "OSC": str(wrapper),
                "PLATFORM_PROVIDER_SOCKET": socket_path,
                "PLATFORM_PROVIDER_UID": str(os.geteuid()),
            }
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args().arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        return 2
    with publication(os.environ, os.environ.get("OSC", "openstack")) as environment:
        return subprocess.run(arguments, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
