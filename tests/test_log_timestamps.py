from __future__ import annotations

import io
import json
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openstack_platform.log_timestamps import MAX_LINE_BYTES, TimestampedBuildLog
from openstack_platform.runtime import run

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "openstack_platform/controller/log_timestamps.cjs"
PREFIX = re.compile(rb"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ")
RUNTIMES = [runtime for runtime in ("node", "bun") if shutil.which(runtime)]


def messages(test: unittest.TestCase, output: bytes) -> list[bytes]:
    lines = output.split(b"\n")
    test.assertEqual(lines.pop(), b"", "every output record ends with a newline")
    for line in lines:
        test.assertRegex(line, PREFIX)
    return [PREFIX.sub(b"", line, count=1) for line in lines]


@unittest.skipUnless(RUNTIMES, "Node.js or Bun is required for wrapper tests")
class RuntimeTimestampTests(unittest.TestCase):
    def run_app(self, runtime: str, code: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [runtime, str(WRAPPER), runtime, "-e", code],
            capture_output=True,
            timeout=15,
            check=False,
        )

    def ready(self, process: subprocess.Popen[bytes]) -> bytes:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5), "app did not print before the deadline")
            line = process.stdout.readline()
            self.assertTrue(line, "app closed output before becoming ready")
            return line

    def test_lines_streams_binary_and_exit_flush(self) -> None:
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                result = self.run_app(
                    runtime,
                    "process.stdout.write('one\\n\\n');"
                    "process.stdout.write(Buffer.from([0xff,0,0xfe,10]));"
                    "process.stderr.write('error\\npartial');process.exit(23)",
                )
                self.assertEqual(result.returncode, 23)
                self.assertEqual(messages(self, result.stdout), [b"one", b"", b"\xff\0\xfe"])
                self.assertEqual(messages(self, result.stderr), [b"error", b"partial"])

    def test_partial_line_is_visible_while_app_is_alive(self) -> None:
        for runtime in RUNTIMES:
            with (
                self.subTest(runtime=runtime),
                subprocess.Popen(
                    [
                        runtime,
                        str(WRAPPER),
                        runtime,
                        "-e",
                        "process.stdout.write('partial');"
                        "process.stdin.once('data',()=>{process.stdout.write('\\nnext\\n');process.stdin.pause()})",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ) as process,
            ):
                first = self.ready(process)
                self.assertIsNone(process.poll())
                output, errors = process.communicate(input=b"continue", timeout=5)
                self.assertEqual(messages(self, first + output), [b"partial", b"next"])
                self.assertEqual(errors, b"")

    def test_long_lines_and_utf8_boundaries_are_bounded_and_lossless(self) -> None:
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                result = self.run_app(
                    runtime,
                    "process.stdout.write('x'.repeat(8191)+'€'.repeat(10000)+'\\n');"
                    "process.stderr.write(Buffer.alloc(30000,255))",
                )
                self.assertEqual(result.returncode, 0)
                out = messages(self, result.stdout)
                err = messages(self, result.stderr)
                self.assertTrue(all(len(line) <= MAX_LINE_BYTES for line in out + err))
                self.assertEqual(b"".join(out), b"x" * 8191 + "€".encode() * 10000)
                for line in out:
                    line.decode("utf-8", errors="strict")
                self.assertEqual(b"".join(err), b"\xff" * 30000)

    def test_signal_is_forwarded_through_the_package_script_to_the_app(self) -> None:
        for runtime in RUNTIMES:
            for sig in (signal.SIGTERM,):
                with (
                    self.subTest(runtime=runtime, signal=sig),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    (root / "package.json").write_text(
                        json.dumps({"scripts": {"start": f"{shutil.which(runtime)} app.cjs"}})
                    )
                    (root / "app.cjs").write_text(
                        "const fs=require('node:fs');"
                        "for(const s of ['SIGTERM','SIGINT'])process.on(s,()=>{"
                        "fs.writeFileSync('signal',s);process.stdout.write('stopped\\n',()=>process.exit(0))});"
                        "console.log('ready');setInterval(()=>{},1000)"
                    )
                    command = (
                        ["bun", "run", "start"] if runtime == "bun" else ["npm", "run", "start"]
                    )
                    with subprocess.Popen(
                        [runtime, str(WRAPPER), *command],
                        cwd=root,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    ) as process:
                        while b"ready" not in self.ready(process):
                            pass
                        process.send_signal(sig)
                        output, _errors = process.communicate(timeout=5)
                        self.assertEqual((root / "signal").read_text(), sig.name)
                        self.assertIn(b"stopped", output, "graceful shutdown output must drain")


class BuildTimestampTests(unittest.TestCase):
    def test_fragmented_lines_binary_and_partial_visibility(self) -> None:
        output = io.BytesIO()
        with TimestampedBuildLog(output, 10000) as log:
            log.write(b"first")
            log.flush()
            self.assertTrue(output.getvalue().endswith(b" first"))
            log.write(b" line\n\n\xff\0\npartial")
        self.assertEqual(
            messages(self, output.getvalue()), [b"first line", b"", b"\xff\0", b"partial"]
        )

    def test_build_collection_records_small_chunks_before_the_builder_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            acknowledgement = Path(directory) / "recorded"

            class RecordingSink(io.BytesIO):
                def write(self, data):
                    size = super().write(data)
                    acknowledgement.touch()
                    return size

            destination = RecordingSink()
            result = run(
                [
                    sys.executable,
                    "-c",
                    "import sys,time;from pathlib import Path;"
                    "sys.stderr.write('ready\\n');sys.stderr.flush();"
                    f"ack=Path({str(acknowledgement)!r});deadline=time.monotonic()+2;"
                    "\nwhile not ack.exists() and time.monotonic()<deadline: time.sleep(.01)"
                    "\nsys.exit(0 if ack.exists() else 42)",
                ],
                timeout_seconds=5,
                stderr_sink=destination,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(destination.getvalue(), b"ready\n")

    def test_long_lines_split_and_file_limit_includes_prefixes(self) -> None:
        output = io.BytesIO()
        with TimestampedBuildLog(output, 100000) as log:
            log.write(b"x" * 20000 + b"\n\n")
        lines = messages(self, output.getvalue())
        self.assertEqual(b"".join(lines), b"x" * 20000)
        self.assertEqual(lines[-1], b"")
        self.assertTrue(all(len(line) <= MAX_LINE_BYTES for line in lines))
        output = io.BytesIO()
        with TimestampedBuildLog(output, 100) as log:
            self.assertEqual(log.write(b"x" * 10000), 10000)
            self.assertEqual(log.tell(), 100)
        self.assertEqual(len(output.getvalue()), 100)


if __name__ == "__main__":
    unittest.main()
