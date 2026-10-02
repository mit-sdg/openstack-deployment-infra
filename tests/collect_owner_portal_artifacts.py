"""Collect only bounded fixture screenshots and credential-free error metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path

MODES = ("desktop-light", "desktop-dark", "mobile-light", "mobile-dark")
PAGES = ("configuration", "review", "build-log", "dashboard", "sign-in")
MAXIMUM_FILE = 2 * 1024**2
MAXIMUM_TOTAL = 20 * 1024**2


def sanitized_diagnostic(payload: bytes) -> bytes:
    value = json.loads(payload)
    failures = []
    if value.get("version") != 1 or not isinstance(value.get("failures"), list):
        raise ValueError("invalid diagnostic")
    for row in value["failures"][:24]:
        if (
            not isinstance(row, dict)
            or row.get("method") not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            or type(row.get("status")) is not int
            or not 400 <= row["status"] <= 599
            or not isinstance(row.get("path"), str)
            or not re.fullmatch(r"/api/v1/[a-z0-9/-]{1,160}", row["path"])
            or not isinstance(row.get("error"), dict)
            or not isinstance(row["error"].get("code"), str)
            or not re.fullmatch(r"[A-Z_]{1,64}", row["error"]["code"])
        ):
            raise ValueError("invalid diagnostic row")
        # Never copy headers, bodies, query strings, summary text or arbitrary
        # fields, even when a failed test accidentally put them in its JSON.
        failures.append(
            {
                "method": row["method"],
                "path": row["path"],
                "status": row["status"],
                "code": row["error"]["code"],
            }
        )
    return (json.dumps({"version": 1, "failures": failures}, indent=2) + "\n").encode()


def collect(root: Path) -> tuple[int, int]:
    if root.is_symlink():
        raise ValueError("artifact root must be direct")
    if not root.is_dir():
        return 0, 0
    output = root / "upload"
    output.mkdir(mode=0o700)
    count = total = 0
    for area, names in (
        ("screenshots", [f"{mode}-{page}.png" for mode in MODES for page in PAGES]),
        ("diagnostics", [f"{mode}.json" for mode in MODES]),
    ):
        source = root / area
        if source.is_symlink():
            continue
        for name in names:
            path = source / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAXIMUM_FILE:
                continue
            payload = path.read_bytes()
            if area == "diagnostics":
                if len(payload) > 8192:
                    continue
                try:
                    payload = sanitized_diagnostic(payload)
                except (ValueError, TypeError, AttributeError):
                    continue
            elif not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                continue
            if len(payload) > MAXIMUM_FILE or total + len(payload) > MAXIMUM_TOTAL:
                continue
            target = output / name
            with target.open("xb") as stream:
                stream.write(payload)
            target.chmod(0o600)
            count += 1
            total += len(payload)
    return count, total


if __name__ == "__main__":
    files, size = collect(Path(".tmp/owner-portal-playwright"))
    print(f"owner-portal-artifacts files={files} bytes={size}")
