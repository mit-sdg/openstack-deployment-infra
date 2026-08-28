#!/usr/bin/env python3
"""Fixed, rootless BuildKit command installed only on disposable builders.

The application controller sends a verified tar archive on stdin and invokes
this program through a console-pinned SSH connection. No remote shell text,
Dockerfile command, credential, or host path is accepted as an argument.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

# The role image substitutes the deployment build root, which
# nix/roles/builder.nix provisions as /srv/<namespace>-build. This literal is
# never a usable path outside that image.
ROOT = Path("__PLATFORM_BUILD_ROOT__")
SOCKET = "unix:///run/user/1000/buildkit/buildkitd.sock"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"[a-z0-9.-]+(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+")
MAX_ARCHIVE = 53_477_376
MAX_MEMBERS = 100_000


def build_id(value: str) -> str:
    return str(uuid.UUID(value))


def safe_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("archive member path is unsafe")
    if path.parts[0] not in {"source", "recipe"}:
        raise ValueError("archive member is outside the build roots")
    if not (member.isdir() or member.isfile() or member.issym()):
        raise ValueError("archive contains an unsupported member")
    if member.issym():
        target = PurePosixPath(member.linkname)
        if target.is_absolute() or ".." in target.parts:
            raise ValueError("archive symlink target is unsafe")


def deadline_remaining(deadline_at: str) -> float:
    if not isinstance(deadline_at, str) or len(deadline_at) > 64:
        raise ValueError("builder operation deadline is malformed")
    try:
        parsed = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("builder operation deadline is malformed") from None
    if parsed.tzinfo is None:
        raise ValueError("builder operation deadline needs a timezone")
    remaining = (parsed - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("builder operation deadline was reached")
    return remaining


def read_archive(expected_size: int, deadline_at: str) -> bytes:
    stream = sys.stdin.buffer
    chunks: list[bytes] = []
    total = 0
    while total <= expected_size:
        remaining = deadline_remaining(deadline_at)
        try:
            ready, _, _ = select.select([stream], [], [], remaining)
        except (OSError, ValueError):
            ready = [stream]
        if not ready:
            raise TimeoutError("builder operation deadline was reached")
        amount = min(1_048_576, expected_size + 1 - total)
        chunk = os.read(stream.fileno(), amount)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > expected_size:
            break
    return b"".join(chunks)


def receive(identifier: str, expected_sha: str, expected_size_text: str, deadline_at: str) -> int:
    identifier = build_id(identifier)
    if not SHA.fullmatch(expected_sha):
        raise ValueError("archive SHA-256 is malformed")
    expected_size = int(expected_size_text)
    if not 1 <= expected_size <= MAX_ARCHIVE:
        raise ValueError("archive size is outside its limit")
    payload = read_archive(expected_size, deadline_at)
    deadline_remaining(deadline_at)
    if len(payload) != expected_size:
        raise ValueError("archive transfer size did not match")
    observed = hashlib.sha256(payload).hexdigest()
    if observed != expected_sha:
        raise ValueError("archive transfer SHA-256 did not match")

    ROOT.mkdir(mode=0o750, parents=True, exist_ok=True)
    destination = ROOT / identifier
    marker = destination / ".archive-sha256"
    if marker.is_file() and marker.read_text().strip() == expected_sha:
        print(json.dumps({"buildId": identifier, "sha256": observed}, separators=(",", ":")))
        return 0
    if destination.exists() or destination.is_symlink():
        raise ValueError("build destination already contains different input")
    staging = Path(tempfile.mkdtemp(prefix=f".{identifier}.", dir=ROOT))
    archive_path = staging / "input.tar"
    try:
        archive_path.write_bytes(payload)
        os.chmod(archive_path, 0o600)
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise ValueError("archive contains too many members")
            for member in members:
                safe_member(member)
            archive.extractall(staging / "tree", members=members, filter="data")
        tree = staging / "tree"
        if not (tree / "source").is_dir() or not (tree / "recipe" / "Dockerfile").is_file():
            raise ValueError("archive omitted source or generated recipe")
        archive_path.unlink()
        (staging / ".archive-sha256").write_text(expected_sha + "\n")
        os.chmod(staging / ".archive-sha256", 0o600)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps({"buildId": identifier, "sha256": observed}, separators=(",", ":")))
    return 0


def build(identifier: str, image_name: str, deadline_at: str) -> int:
    identifier = build_id(identifier)
    deadline_remaining(deadline_at)
    if not IMAGE.fullmatch(image_name) or len(image_name) > 255:
        raise ValueError("image repository is malformed")
    registry = image_name.split("/", 1)[0]
    if ":" in registry and not 1 <= int(registry.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("registry port is malformed")
    directory = ROOT / identifier
    context = directory / "tree" / "source"
    dockerfile = directory / "tree" / "recipe"
    if not context.is_dir() or not (dockerfile / "Dockerfile").is_file():
        raise ValueError("verified build input is unavailable")
    metadata = directory / "metadata.json"
    metadata.unlink(missing_ok=True)
    executable = shutil.which("buildctl")
    if executable is None:
        raise RuntimeError("buildctl is unavailable")
    reference = f"{image_name}:build-{identifier.replace('-', '')}"
    home = os.environ.get("HOME")
    runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
    if not home or not Path(home).is_absolute():
        raise RuntimeError("builder HOME is unavailable")
    if not runtime_directory or not Path(runtime_directory).is_absolute():
        raise RuntimeError("builder runtime directory is unavailable")
    environment = {
        "HOME": home,
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/run/current-system/sw/bin:/usr/bin:/bin"),
        "SSL_CERT_FILE": str(Path(runtime_directory) / "buildkit/ca-bundle.crt"),
        "XDG_RUNTIME_DIR": runtime_directory,
    }
    try:
        build_timeout = deadline_remaining(deadline_at)
        completed = subprocess.run(
            (
                executable,
                "--addr",
                SOCKET,
                "build",
                "--frontend",
                "dockerfile.v0",
                "--local",
                f"context={context}",
                "--local",
                f"dockerfile={dockerfile}",
                "--opt",
                "filename=Dockerfile",
                "--output",
                f"type=image,name={reference},push=true",
                "--metadata-file",
                str(metadata),
                "--progress",
                "plain",
            ),
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            env=environment,
            check=False,
            start_new_session=True,
            timeout=build_timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("BuildKit build exceeded the operation deadline") from error
    if completed.returncode != 0:
        raise RuntimeError("rootless BuildKit build failed")
    deadline_remaining(deadline_at)
    raw = metadata.read_bytes()
    if len(raw) > 65_536:
        raise RuntimeError("BuildKit metadata exceeded its limit")
    value = json.loads(raw)
    digest = value.get("containerimage.digest") if isinstance(value, dict) else None
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise RuntimeError("BuildKit did not return a pushed image digest")
    sys.stdout.buffer.write(raw)
    return 0


def main() -> int:
    try:
        if len(sys.argv) == 6 and sys.argv[2] == "receive":
            return receive(sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[1])
        if len(sys.argv) == 5 and sys.argv[2] == "build":
            return build(sys.argv[3], sys.argv[4], sys.argv[1])
        raise ValueError(f"usage: {Path(sys.argv[0]).name} receive|build ...")
    except (ValueError, OSError, tarfile.TarError, json.JSONDecodeError, RuntimeError) as error:
        print(f"builder execution failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
