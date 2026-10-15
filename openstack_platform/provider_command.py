"""Read launcher for reviewed infrastructure scripts; mutations use fixed OSC."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from . import provider
from .provider_protocol import ProviderError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--provider-uid", type=int, required=True)
    parser.add_argument("--mutation-command", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        return 2
    if not provider.is_read(arguments):
        # This launcher is installed under precisely the existing principals
        # and retains their reviewed mutation wrapper. The socket cannot exec.
        os.execv(args.mutation_command, [args.mutation_command, *arguments])
    os.environ["PLATFORM_PROVIDER_SOCKET"] = args.socket
    os.environ["PLATFORM_PROVIDER_UID"] = str(args.provider_uid)
    try:
        read = provider.parse(arguments)
        result = provider.call(read.action, read.params, timeout_seconds=60)
        sys.stdout.buffer.write(provider.render(read, result))
        return 0
    except ProviderError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
