"""Run unittest discovery in isolated module processes, without extra dependencies."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_module(path: Path) -> subprocess.CompletedProcess[str]:
    # Match serial discovery exactly, including imports under both test_foo and
    # tests.test_foo. Separate interpreters isolate mocks, environment and cwd.
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", path.name, "-q"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-j", "--jobs", type=int, default=min(os.cpu_count() or 1, 4))
    parser.add_argument("--pattern", default="test_*.py", help="discovery filename glob")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    modules = sorted(
        (ROOT / "tests").glob(args.pattern), key=lambda p: p.stat().st_size, reverse=True
    )
    if not modules:
        parser.error("no test modules match --pattern")
    started = time.monotonic()
    failures = []
    tests = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        pending = {executor.submit(run_module, path): path for path in modules}
        for future in as_completed(pending):
            path = pending[future]
            result = future.result()
            summary = re.search(r"Ran (\d+) tests? in ([\d.]+)s", result.stdout)
            if summary:
                tests += int(summary[1])
            if result.returncode:
                failures.append(path.name)
                print(f"FAIL {path.name}\n{result.stdout}", flush=True)
            else:
                print(
                    f"OK {path.name}: {summary[0] if summary else result.stdout.strip()}",
                    flush=True,
                )
    print(
        f"{tests} tests in {len(modules)} modules, {time.monotonic() - started:.2f}s, {args.jobs} workers"
    )
    if failures:
        print(f"Failed modules: {', '.join(failures)}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
