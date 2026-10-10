"""Local privileged isolated-instance migration command."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from ..validation import uuid as check_uuid
from .storage_repair import repair


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate shared databases to isolated instances without deleting old data"
    )
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--request-id")
    parser.add_argument(
        "--abort",
        action="store_true",
        help="Unfreeze selected unpublished migrations; never delete data",
    )
    parser.add_argument("--application", action="append", default=None, metavar="UUID")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    if not args.socket.is_absolute() or not 1 <= args.timeout <= 7200:
        parser.error("socket must be absolute; timeout must be 1..7200 seconds")
    try:
        repair(
            args.socket,
            args.request_id or str(uuid.uuid4()),
            seconds=args.timeout,
            output=sys.stdout,
            route="/v1/admin/storage/abort-migration"
            if args.abort
            else "/v1/admin/storage/migrate-instances",
            body=None
            if args.application is None
            else {
                "applicationIds": sorted(
                    {check_uuid(value, field="application ID") for value in args.application}
                )
            },
        )
    except (OSError, ValueError, RuntimeError):
        print(
            "Instance migration did not complete; replay the printed request ID. Old shared data is retained.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
