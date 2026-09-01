#!/usr/bin/env bash
# Self-bootstrap one committed helper release through the pinned admin alias.
set -euo pipefail

commit=${1:-}
readonly admin_python=/run/current-system/sw/bin/python3.14
readonly helper_config_root=/etc

[[ $commit =~ ^[0-9a-f]{40}$ ]] || {
  echo "usage: $0 FULL_SOURCE_COMMIT" >&2
  exit 64
}

root=$(git rev-parse --show-toplevel)
mapfile -t installation_values < <(python3 - "$root/infra/lib/platform_contract.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    installation = json.load(stream)["installation"]
root = installation["operatorRoot"]
inventory = installation["inventoryFilename"]
ssh_alias = installation["sshAlias"]
if not isinstance(root, str) or not root.startswith("/"):
    raise SystemExit("platform operator root is malformed")
if not isinstance(inventory, str) or "/" in inventory or not inventory:
    raise SystemExit("platform inventory filename is malformed")
if not isinstance(ssh_alias, str) or not ssh_alias or any(char.isspace() for char in ssh_alias):
    raise SystemExit("platform SSH alias is malformed")
print(root)
print(inventory)
print(ssh_alias)
PY
)
[[ ${#installation_values[@]} -eq 3 ]] || {
  echo "platform installation contract is incomplete" >&2
  exit 64
}
operator_root=${installation_values[0]}
inventory_filename=${installation_values[1]}
ssh_alias=${installation_values[2]}
readonly operator_root inventory_filename ssh_alias
ssh_config=$operator_root/.secrets/ssh/config
default_platform_config=$operator_root/config/$inventory_filename
platform_config=${PLATFORM_CONFIG:-$default_platform_config}
readonly ssh_config default_platform_config platform_config

[[ -f $ssh_config && ! -L $ssh_config && -r $ssh_config ]] || {
  echo "the pinned SSH configuration is not a direct readable file" >&2
  exit 64
}

test "$(git -C "$root" rev-parse HEAD)" = "$commit" || {
  echo "the requested commit is not the checkout HEAD" >&2
  exit 1
}
test -z "$(git -C "$root" status --porcelain --untracked-files=no)" || {
  echo "tracked source changes must be committed before deployment" >&2
  exit 1
}

release_manifest=${PLATFORM_RELEASE_MANIFEST:-}
release_signature=${PLATFORM_RELEASE_SIGNATURE:-}
release_trust_root=${PLATFORM_RELEASE_TRUST_ROOT:-}
unsigned_ack=${PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT:-}
[[ -f $release_manifest && ! -L $release_manifest ]] || {
  echo "PLATFORM_RELEASE_MANIFEST must select verified release evidence" >&2
  exit 1
}
mapfile -t evidence_files < <(python3 - "$release_manifest" <<'PY'
import json
import os
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
document = json.loads(manifest.read_text())
for name in ("sbom", "provenance"):
    filename = document["evidence"][name]["file"]
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise SystemExit("release evidence filename is unsafe")
    path = manifest.parent / filename
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"release {name} evidence is unavailable")
    print(path)
PY
)
[[ ${#evidence_files[@]} -eq 2 ]] || {
  echo "release SBOM/provenance evidence is incomplete" >&2
  exit 1
}
if [[ -n $unsigned_ack ]]; then
  [[ $unsigned_ack == I_UNDERSTAND_THIS_IS_NOT_PRODUCTION && -z $release_signature && -z $release_trust_root ]] || {
    echo "unsigned development release evidence is inconsistent" >&2
    exit 1
  }
else
  [[ -f $release_signature && ! -L $release_signature && -f $release_trust_root && ! -L $release_trust_root ]] || {
    echo "production helper release requires signature and trust root" >&2
    exit 1
  }
fi

# Read deployment identity and paths from the installed, private operator
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
    raise SystemExit("stable operator platform config is unavailable") from error
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > 1_048_576
    ):
        raise SystemExit("stable operator platform config ownership or mode is invalid")
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
    raise SystemExit("stable operator platform config has invalid deployment identity") from error

path_pattern = re.compile(r"/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*")
if not isinstance(paths, dict) or set(paths) != {"root", "adminState", "backups", "data"}:
    raise SystemExit("stable operator platform config has invalid deployment paths")
for value in (*paths.values(), sys.argv[2]):
    if not isinstance(value, str) or len(value) > 256 or not path_pattern.fullmatch(value):
        raise SystemExit("stable operator platform config has unsafe deployment paths")
if len(set(paths.values())) != len(paths):
    raise SystemExit("configured deployment paths must differ")
if not isinstance(namespace, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]", namespace):
    raise SystemExit("stable operator platform namespace is invalid")
if not isinstance(project, str) or not project or len(project) > 128 or "\n" in project:
    raise SystemExit("stable operator platform project is invalid")
if not isinstance(project_id, str) or not re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    project_id,
):
    raise SystemExit("stable operator platform project ID is invalid")
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
  echo "stable operator platform config did not yield deployment identity and paths" >&2
  exit 1
}
admin_root=${platform_paths[0]}
admin_state=${platform_paths[1]}
live_platform_config=${platform_paths[2]}
platform_namespace=${platform_paths[3]}
platform_identity_sha256=${platform_paths[4]}
release_root="$admin_state/operator/helper-releases"
incoming="$release_root/incoming"
bin_root="$admin_root/bin"
remote_archive="$incoming/${commit}.tar"
remote_installer="$incoming/${commit}.install_release.py"
remote_evidence="$incoming/evidence-$commit"
remote_manifest="$remote_evidence/release-manifest.json"
remote_signature="$remote_evidence/release-manifest.sig"
remote_trust_root="$remote_evidence/release-trust-root.pem"

# The live admin inventory is image/configuration state. Verify it in place;
# deployment never copies the operator host's private config into /etc.
ssh -F "$ssh_config" "$ssh_alias" -- "$admin_python" - \
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
        raise SystemExit("live admin platform config symlink target is not readable by the operator account")
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
        "live admin platform config does not match operator project, namespace, and paths"
    )
PY

archive=$(mktemp "${TMPDIR:-/tmp}/platform-helper.XXXXXX.tar")
installer=$(mktemp "${TMPDIR:-/tmp}/platform-installer.XXXXXX.py")
uploaded=false
cleanup() {
  rm -f -- "$archive" "$installer"
  if [[ $uploaded == true ]]; then
    ssh -F "$ssh_config" "$ssh_alias" -- rm -rf -- "$remote_archive" "$remote_installer" "$remote_evidence" \
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
git -C "$root" show "$commit:deploy/releases/install_release.py" >"$installer"
chmod 0600 "$archive" "$installer"

remote_uid=$(ssh -F "$ssh_config" "$ssh_alias" -- id -u)
[[ $remote_uid =~ ^[0-9]+$ && $remote_uid -ne 0 ]] || {
  echo "the pinned admin alias must select an unprivileged account" >&2
  exit 77
}
ssh -F "$ssh_config" "$ssh_alias" -- install -d -m 0750 \
  "$release_root" "$release_root/releases" "$bin_root"
ssh -F "$ssh_config" "$ssh_alias" -- install -d -m 0700 "$incoming" "$remote_evidence"
scp -F "$ssh_config" -- "$archive" "${ssh_alias}:$remote_archive"
uploaded=true
scp -F "$ssh_config" -- "$installer" "${ssh_alias}:$remote_installer"
scp -F "$ssh_config" -- "$release_manifest" "${ssh_alias}:$remote_manifest"
for evidence in "${evidence_files[@]}"; do
  scp -F "$ssh_config" -- "$evidence" "${ssh_alias}:$remote_evidence/$(basename "$evidence")"
done
release_trust_arguments=()
if [[ -n $unsigned_ack ]]; then
  release_trust_arguments+=(--allow-unsigned-development)
else
  scp -F "$ssh_config" -- "$release_signature" "${ssh_alias}:$remote_signature"
  scp -F "$ssh_config" -- "$release_trust_root" "${ssh_alias}:$remote_trust_root"
  release_trust_arguments+=(--release-signature "$remote_signature" --release-trust-root "$remote_trust_root")
fi
ssh -F "$ssh_config" "$ssh_alias" -- chmod -R go-rwx "$remote_archive" "$remote_installer" "$remote_evidence"

ssh -F "$ssh_config" "$ssh_alias" -- "$admin_python" "$remote_installer" \
  --mode helper \
  --archive "$remote_archive" \
  --archive-sha256 "$archive_sha256" \
  --commit "$commit" \
  --release-manifest "$remote_manifest" \
  "${release_trust_arguments[@]}" \
  --python "$admin_python" \
  --platform-config "$live_platform_config" \
  --expected-platform-namespace "$platform_namespace" \
  --expected-platform-identity-sha256 "$platform_identity_sha256" \
  --release-root "$release_root" \
  --bin-root "$bin_root" \
  --remove-archive

response=$(printf '{}\n' | ssh -F "$ssh_config" "$ssh_alias" -- \
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
