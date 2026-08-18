#!/usr/bin/env bash
# Build the deterministic git archive consumed by install_release.py.
set -euo pipefail

commit=${1:-}
output=${2:-}
[[ $commit =~ ^[0-9a-f]{40}$ && -n $output ]] || {
  echo "usage: $0 FULL_SOURCE_COMMIT OUTPUT.tar" >&2
  exit 64
}

root=$(git rev-parse --show-toplevel)
test "$(git -C "$root" rev-parse HEAD)" = "$commit" || {
  echo "the requested commit is not the checkout HEAD" >&2
  exit 1
}
test -z "$(git -C "$root" status --porcelain --untracked-files=no)" || {
  echo "tracked source changes must be committed before packaging" >&2
  exit 1
}

temporary="${output}.tmp.$$"
trap 'rm -f "$temporary"' EXIT
git -C "$root" archive --format=tar --output="$temporary" "$commit"
chmod 0600 "$temporary"
mv -f -- "$temporary" "$output"
sha256sum -- "$output"
