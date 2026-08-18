#!/usr/bin/env bash
# Generate the exact pinned SSH alias after validating the provider bridge.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$SCRIPT_DIR/setup_management_bridge.py" "$@"
