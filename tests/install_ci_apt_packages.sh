#!/bin/bash
# Install CI-only APT packages without allowing a transient mirror/lock hang to
# consume the whole image smoke-test job. The caller supplies only fixed package
# names from a reviewed workflow.
set -euo pipefail

(($# > 0)) || {
  echo "usage: $0 PACKAGE..." >&2
  exit 2
}

for attempt in 1 2 3; do
  if timeout --foreground --kill-after=30s 5m sudo apt-get update \
    && timeout --foreground --kill-after=30s 10m sudo apt-get install --yes "$@"; then
    exit 0
  fi

  if ((attempt == 3)); then
    echo "APT setup failed after ${attempt} attempts" >&2
    exit 1
  fi

  # A killed apt-get leaves dpkg half-configured, and every later apt-get run
  # refuses to do anything until that is repaired. Without this the retry is
  # guaranteed to fail with "dpkg was interrupted".
  echo "APT setup attempt ${attempt} failed or timed out; repairing dpkg state" >&2
  timeout --foreground --kill-after=30s 5m sudo dpkg --configure -a || true
  sudo rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock-frontend || true
  sleep 15
done
