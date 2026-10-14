"""Local-only startup/session benchmark; never reads ambient cloud credentials.

Run: uv run --with python-openstackclient python tests/benchmark_provider.py
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openstack_platform.provider import call  # noqa: E402
from openstack_platform.provider_api import API  # noqa: E402
from openstack_platform.provider_service import Server  # noqa: E402
from tests.fixtures.openstack_http import serve  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=10)
    args = parser.parse_args()
    with serve() as cloud, tempfile.TemporaryDirectory() as directory:
        environment = {"PATH": os.defpath, "HOME": directory, **cloud.environment}
        cli = []
        for _ in range(args.calls):
            start = time.perf_counter()
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from openstackclient.shell import main; raise SystemExit(main())",
                    "flavor",
                    "show",
                    "small",
                    "-f",
                    "json",
                ],
                env=environment,
                cwd=directory,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            cli.append(time.perf_counter() - start)
        cli_tokens = len(cloud.tokens)
        api = API(cloud.environment, allow_http=True)
        start = time.perf_counter()
        api.execute("flavor.show", {"reference": "small"}, time.monotonic() + 20)
        cold = time.perf_counter() - start
        persistent = []
        for _ in range(args.calls):
            start = time.perf_counter()
            api.execute("flavor.show", {"reference": "small"}, time.monotonic() + 20)
            persistent.append(time.perf_counter() - start)
        socket_path = directory + "/provider.sock"
        service = Server(socket_path, api, frozenset({(os.geteuid(), os.getegid())}))
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        socket_samples = []
        try:
            for _ in range(args.calls):
                start = time.perf_counter()
                call("flavor.show", {"reference": "small"}, timeout_seconds=20, path=socket_path)
                socket_samples.append(time.perf_counter() - start)
        finally:
            service.shutdown()
            thread.join(timeout=2)
            service.server_close()
        print(
            json.dumps(
                {
                    "calls": args.calls,
                    "cli_median_ms": round(statistics.median(cli) * 1000, 3),
                    "session_cold_ms": round(cold * 1000, 3),
                    "session_warm_median_ms": round(statistics.median(persistent) * 1000, 3),
                    "socket_warm_median_ms": round(statistics.median(socket_samples) * 1000, 3),
                    "cli_authentications": cli_tokens,
                    "session_authentications": len(cloud.tokens) - cli_tokens,
                    "python": sys.version.split()[0],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
