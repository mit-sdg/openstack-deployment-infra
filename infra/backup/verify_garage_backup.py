#!/usr/bin/env python3
"""Verify a Garage archive, optionally against the current admin bucket inventory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup.garage_catalog import runtime, verify_archive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        if args.offline:
            manifest = verify_archive(sys.stdin.buffer)
        else:
            admin, s3, prefix, _creds = runtime()
            try:
                manifest = verify_archive(sys.stdin.buffer, admin=admin, prefix=prefix)
            finally:
                s3.close()
        print(
            f"garage-archive=verified buckets={len(manifest['buckets'])} objects={manifest.get('objectCount', len(manifest['objects']))}"
        )
        return 0
    except Exception:
        print(
            "Garage archive verification failed; catalog or app bucket coverage is incomplete",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
