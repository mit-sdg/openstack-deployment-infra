#!/bin/bash
# Install CI-only APT packages without allowing a transient mirror/lock hang to
# consume the whole image smoke-test job. The caller supplies only fixed package
# names from a reviewed workflow.
set -euo pipefail

(($# > 0)) || {
  echo "usage: $0 PACKAGE..." >&2
  exit 2
}

# GitHub Ubuntu runners also ship third-party sources (for example Chrome).
# Those repositories are unrelated to these packages and can break all CI when
# their indexes disagree. Select only the runner's official Ubuntu source file
# for BOTH commands, without editing global sources or weakening verification.
[[ ${GITHUB_ACTIONS:-} == true ]] || {
  echo "this package installer is only for ephemeral GitHub Actions runners" >&2
  exit 2
}
ubuntu_sources=/etc/apt/sources.list.d/ubuntu.sources
[[ -f $ubuntu_sources && ! -L $ubuntu_sources && -r $ubuntu_sources ]] || {
  echo "the runner's official Ubuntu sources file is unavailable" >&2
  exit 2
}
for package in "$@"; do
  [[ $package =~ ^[a-z0-9][a-z0-9+.-]*$ ]] || {
    echo "only fixed package names are accepted, not APT options" >&2
    exit 2
  }
done
apt_options=(
  -o "Dir::Etc::sourcelist=$ubuntu_sources"
  -o "Dir::Etc::sourceparts=-"
  -o "Dir::Cache::pkgcache="
  -o "Dir::Cache::srcpkgcache="
  -o "APT::Get::List-Cleanup=0"
  -o "APT::Update::Error-Mode=any"
)

for attempt in 1 2 3; do
  if timeout --foreground --kill-after=30s 5m sudo apt-get "${apt_options[@]}" update \
    && timeout --foreground --kill-after=30s 10m sudo apt-get "${apt_options[@]}" install --yes "$@"; then
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
