#!/usr/bin/env bash
# Self-bootstrap one committed helper release through the pinned admin alias.
set -euo pipefail

commit=${1:-}
readonly ssh_config=/srv/openstack-platform/.secrets/ssh/config
readonly default_platform_config=/srv/openstack-platform/config/platform.json
readonly admin_python=/run/current-system/sw/bin/python3.14
platform_config=${PLATFORM_CONFIG:-$default_platform_config}
readonly helper_config_root=/etc

[[ -f $ssh_config && ! -L $ssh_config && -r $ssh_config ]] || {
  echo "the pinned SSH configuration is not a direct readable file" >&2
  exit 64
}
[[ $commit =~ ^[0-9a-f]{40}$ ]] || {
  echo "usage: $0 FULL_SOURCE_COMMIT" >&2
  exit 64
}

root=$(git rev-parse --show-toplevel)
test "$(git -C "$root" rev-parse HEAD)" = "$commit" || {
  echo "the requested commit is not the checkout HEAD" >&2
  exit 1
}
test -z "$(git -C "$root" status --porcelain --untracked-files=no)" || {
  echo "tracked source changes must be committed before deployment" >&2
  exit 1
}

# Read deployment identity and paths from the installed, private management
# inventory. Restrict remote arguments to shell-safe canonical values.
paths_output=$(python3 - "$platform_config" "$helper_config_root" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
except OSError as error:
    raise SystemExit("stable management platform config is unavailable") from error
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > 1_048_576
    ):
        raise SystemExit("stable management platform config ownership or mode is invalid")
    chunks = []
    remaining = 1_048_577
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
finally:
    os.close(descriptor)

def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result

try:
    document = json.loads(raw, object_pairs_hook=pairs)
    paths = document["paths"]
    root, admin_state = paths["root"], paths["adminState"]
    namespace = document["namespace"]
    project = document["project"]
    project_id = document["projectId"]
except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
    raise SystemExit("stable management platform config has invalid deployment identity") from error

path_pattern = re.compile(r"/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*")
if not isinstance(paths, dict) or set(paths) != {"root", "adminState", "backups", "data"}:
    raise SystemExit("stable management platform config has invalid deployment paths")
for value in (*paths.values(), sys.argv[2]):
    if not isinstance(value, str) or len(value) > 256 or not path_pattern.fullmatch(value):
        raise SystemExit("stable management platform config has unsafe deployment paths")
if len(set(paths.values())) != len(paths):
    raise SystemExit("configured deployment paths must differ")
if not isinstance(namespace, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]", namespace):
    raise SystemExit("stable management platform namespace is invalid")
if not isinstance(project, str) or not project or len(project) > 128 or "\n" in project:
    raise SystemExit("stable management platform project is invalid")
if not isinstance(project_id, str) or not re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    project_id,
):
    raise SystemExit("stable management platform project ID is invalid")
identity = {
    "namespace": namespace,
    "project": project,
    "projectId": project_id,
    "paths": paths,
}
identity_sha256 = hashlib.sha256(
    json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
live_config = f"{sys.argv[2].rstrip('/')}/{namespace}/platform.json"
print(root, admin_state, live_config, namespace, identity_sha256, sep="\n")
PY
)
mapfile -t platform_paths <<<"$paths_output"
[[ ${#platform_paths[@]} -eq 5 ]] || {
  echo "stable management platform config did not yield deployment identity and paths" >&2
  exit 1
}
admin_root=${platform_paths[0]}
admin_state=${platform_paths[1]}
live_platform_config=${platform_paths[2]}
platform_namespace=${platform_paths[3]}
platform_identity_sha256=${platform_paths[4]}
release_root="$admin_state/controller/platform-cli"
incoming="$release_root/incoming"
bin_root="$admin_root/bin"
remote_archive="$incoming/${commit}.tar"
remote_installer="$incoming/${commit}.install_release.py"

# The live admin inventory is image/configuration state. Verify it in place;
# deployment never copies the management host's private config into /etc.
ssh -F "$ssh_config" platform-admin -- "$admin_python" - \
  "$live_platform_config" "$platform_namespace" "$platform_identity_sha256" <<'PY'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_absolute() or path != Path(os.path.normpath(path)):
    raise SystemExit("live admin platform config path is not canonical and absolute")
try:
    path_metadata = path.lstat()
except OSError as error:
    raise SystemExit("live admin platform config is unavailable") from error

read_path = path
expected_target = None
if stat.S_ISLNK(path_metadata.st_mode):
    try:
        resolved = path.resolve(strict=True)
        target_metadata = resolved.lstat()
    except (OSError, RuntimeError) as error:
        raise SystemExit("live admin platform config symlink target is unavailable") from error
    store = Path("/nix/store")
    if not resolved.is_absolute() or resolved == store or not resolved.is_relative_to(store):
        raise SystemExit("live admin platform config symlink target is outside /nix/store")
    if not stat.S_ISREG(target_metadata.st_mode) or stat.S_ISLNK(target_metadata.st_mode):
        raise SystemExit("live admin platform config symlink target is not a direct regular file")
    if target_metadata.st_uid != 0:
        raise SystemExit("live admin platform config symlink target is not owned by root")
    if stat.S_IMODE(target_metadata.st_mode) & 0o022:
        raise SystemExit("live admin platform config symlink target is group or world writable")
    if not os.access(resolved, os.R_OK):
        raise SystemExit("live admin platform config symlink target is not readable by agentops")
    read_path = resolved
    expected_target = target_metadata
elif not stat.S_ISREG(path_metadata.st_mode) or not os.access(path, os.R_OK):
    raise SystemExit("live admin platform config is not a direct readable regular file")

try:
    descriptor = os.open(read_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
except OSError as error:
    raise SystemExit("live admin platform config target is unavailable") from error
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1_048_576:
        raise SystemExit("live admin platform config target is not a bounded regular file")
    if expected_target is not None and (
        (metadata.st_dev, metadata.st_ino) != (expected_target.st_dev, expected_target.st_ino)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise SystemExit("live admin platform config Nix store target changed during validation")
    raw = b""
    while chunk := os.read(descriptor, min(65_536, 1_048_577 - len(raw))):
        raw += chunk
        if len(raw) > 1_048_576:
            raise SystemExit("live admin platform config exceeds its size limit")
finally:
    os.close(descriptor)

def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result

try:
    document = json.loads(raw, object_pairs_hook=pairs)
    namespace = document["namespace"]
    identity = {
        "namespace": namespace,
        "project": document["project"],
        "projectId": document["projectId"],
        "paths": document["paths"],
    }
    identity_sha256 = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
except (KeyError, TypeError, ValueError, UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as error:
    raise SystemExit("live admin platform config has invalid identity") from error
if namespace != sys.argv[2] or identity_sha256 != sys.argv[3]:
    raise SystemExit(
        "live admin platform config does not match management project, namespace, and paths"
    )
PY

archive=$(mktemp "${TMPDIR:-/tmp}/platform-helper.XXXXXX.tar")
installer=$(mktemp "${TMPDIR:-/tmp}/platform-installer.XXXXXX.py")
uploaded=false
cleanup() {
  rm -f -- "$archive" "$installer"
  if [[ $uploaded == true ]]; then
    ssh -F "$ssh_config" platform-admin -- rm -f -- "$remote_archive" "$remote_installer" \
      >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

git -C "$root" archive --format=tar --output="$archive" "$commit"
archive_sha256=$(python3 - "$archive" <<'PY'
import hashlib
import sys

with open(sys.argv[1], "rb") as stream:
    print(hashlib.file_digest(stream, "sha256").hexdigest())
PY
)
[[ $archive_sha256 =~ ^[0-9a-f]{64}$ ]] || {
  echo "could not checksum the exact helper release archive" >&2
  exit 1
}
git -C "$root" show "$commit:deploy/platform-cli/install_release.py" >"$installer"
chmod 0600 "$archive" "$installer"

remote_uid=$(ssh -F "$ssh_config" platform-admin -- id -u)
[[ $remote_uid =~ ^[0-9]+$ && $remote_uid -ne 0 ]] || {
  echo "the pinned admin alias must select an unprivileged account" >&2
  exit 77
}
ssh -F "$ssh_config" platform-admin -- install -d -m 0750 \
  "$release_root" "$release_root/releases" "$bin_root"
ssh -F "$ssh_config" platform-admin -- install -d -m 0700 "$incoming"
scp -F "$ssh_config" -- "$archive" "platform-admin:$remote_archive"
uploaded=true
scp -F "$ssh_config" -- "$installer" "platform-admin:$remote_installer"
ssh -F "$ssh_config" platform-admin -- chmod 0600 "$remote_archive" "$remote_installer"

ssh -F "$ssh_config" platform-admin -- "$admin_python" "$remote_installer" \
  --mode helper \
  --archive "$remote_archive" \
  --archive-sha256 "$archive_sha256" \
  --commit "$commit" \
  --python "$admin_python" \
  --platform-config "$live_platform_config" \
  --expected-platform-namespace "$platform_namespace" \
  --expected-platform-identity-sha256 "$platform_identity_sha256" \
  --release-root "$release_root" \
  --bin-root "$bin_root" \
  --remove-archive

response=$(printf '{}\n' | ssh -F "$ssh_config" platform-admin -- \
  "$bin_root/openstack-platform-helper")
python3 - "$commit" "$response" <<'PY'
import json
import sys

commit, payload = sys.argv[1:]
document = json.loads(payload)
assert document == {
    "version": 1,
    "requestId": "00000000-0000-0000-0000-000000000000",
    "ok": False,
    "error": {"code": "INVALID_REQUEST", "message": "helper request is invalid"},
}
print(f"helper-release={commit}:verified")
PY
