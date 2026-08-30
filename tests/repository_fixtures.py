"""Builders for repository-sensitive tests that must also run in a dirty worktree."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def clean_repository(source: Path, destination: Path) -> tuple[Path, str]:
    """Commit the current tracked/non-ignored source into a private test repository."""
    destination.mkdir(mode=0o700)
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=source,
        check=True,
        capture_output=True,
    ).stdout
    for encoded in listed.split(b"\0"):
        if not encoded:
            continue
        relative = Path(os.fsdecode(encoded))
        source_path = source / relative
        if not source_path.exists() and not source_path.is_symlink():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_symlink():
            target.symlink_to(os.readlink(source_path))
        else:
            shutil.copy2(source_path, target)
    subprocess.run(["git", "init", "--quiet"], cwd=destination, check=True)
    subprocess.run(["git", "add", "--all"], cwd=destination, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Repository Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "test source",
        ],
        cwd=destination,
        check=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=destination,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return destination, commit
