#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG_HELPER="$SCRIPT_DIR/../lib/platform_config.py"
CONTRACT="$SCRIPT_DIR/../lib/platform_contract.json"
OSC=${OSC:-openstack}

usage() {
  echo "usage: $0 ROLE QCOW2_FILE NIX_OUTPUT PATH_INFO_JSON" >&2
  exit 2
}
[[ $# == 4 ]] || usage
role=$1
image_file=$2
nix_output=$3
path_info=$4
python3 - "$CONTRACT" "$role" <<'PY' || usage
import json
import sys

contract_path, role = sys.argv[1:]
with open(contract_path, encoding="utf-8") as stream:
    roles = json.load(stream)["roles"]["all"]
raise SystemExit(0 if role in roles else 1)
PY
[[ -f $image_file && ! -L $image_file && -r $image_file ]] || {
  echo "image must be a readable direct regular file: $image_file" >&2
  exit 2
}
[[ $nix_output == /nix/store/* && -f $path_info && ! -L $path_info ]] || {
  echo "Nix output and closure evidence are required" >&2
  exit 2
}
for name in \
  PLATFORM_ARTIFACT_MANIFEST_SHA256 \
  PLATFORM_ARTIFACT_QCOW2_SHA256 \
  PLATFORM_ARTIFACT_NIX_CLOSURE_SHA256; do
  [[ ${!name:-} =~ ^[0-9a-f]{64}$ ]] || {
    echo "$name must be a full lowercase SHA-256" >&2
    exit 2
  }
done
[[ ${PLATFORM_ARTIFACT_NIX_OUTPUT:-} == "${nix_output##*/}" ]] || {
  echo "Nix output identity does not match verified artifact evidence" >&2
  exit 2
}

project=$("$CONFIG_HELPER" get project)
project_id=$("$CONFIG_HELPER" get projectId)
image_name=$("$CONFIG_HELPER" get "images.$role")
[[ ${SOURCE_COMMIT:-} =~ ^[0-9a-f]{40}$ ]] || {
  echo "SOURCE_COMMIT must be a full lowercase commit SHA" >&2
  exit 2
}
repository_root=$(cd -- "$SCRIPT_DIR/../.." && pwd)
artifact_trust_arguments=()
if [[ -n ${PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT:-} ]]; then
  [[ $PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT == I_UNDERSTAND_THIS_IS_NOT_PRODUCTION ]] || {
    echo "unsigned artifact acknowledgement is invalid" >&2
    exit 2
  }
  artifact_trust_arguments+=(--allow-unsigned-development)
else
  artifact_trust_arguments+=(
    --signature "${PLATFORM_ARTIFACT_SIGNATURE:?PLATFORM_ARTIFACT_SIGNATURE is required}"
    --trust-root "${PLATFORM_ARTIFACT_TRUST_ROOT:?PLATFORM_ARTIFACT_TRUST_ROOT is required}"
  )
fi
artifact_output=$(
  PYTHONPATH="$repository_root" python3 -m openstack_platform.release_manifest verify-role \
    --component-manifest "${PLATFORM_RELEASE_MANIFEST:?PLATFORM_RELEASE_MANIFEST is required}" \
    --manifest "${PLATFORM_ARTIFACT_MANIFEST:?PLATFORM_ARTIFACT_MANIFEST is required}" \
    "${artifact_trust_arguments[@]}" \
    --role "$role" \
    --qcow2 "$image_file" \
    --path-info "$path_info" \
    --output-store-path "$nix_output" \
    --platform "${PLATFORM_CONFIG:-$repository_root/config/platform.json}" \
    --commit "$SOURCE_COMMIT"
)
mapfile -t verified_artifact <<<"$artifact_output"
[[ ${verified_artifact[0]:-} == "artifact_manifest_sha256=$PLATFORM_ARTIFACT_MANIFEST_SHA256" && \
   ${verified_artifact[1]:-} == "qcow2_sha256=$PLATFORM_ARTIFACT_QCOW2_SHA256" && \
   ${verified_artifact[2]:-} == "nix_closure_sha256=$PLATFORM_ARTIFACT_NIX_CLOSURE_SHA256" && \
   ${verified_artifact[3]:-} == "nix_output=$PLATFORM_ARTIFACT_NIX_OUTPUT" && \
   ${#verified_artifact[@]} -eq 4 ]] || {
  echo "signed role artifact verification did not match publication inputs" >&2
  exit 2
}
metadata_output=$(
  PYTHONPATH="$repository_root" python3 -m openstack_platform.openstack \
    --platform "${PLATFORM_CONFIG:-$repository_root/config/platform.json}" \
    --role "$role" \
    --source-commit "$SOURCE_COMMIT"
)
mapfile -t image_metadata <<<"$metadata_output"
[[ ${#image_metadata[@]} == 8 ]] || {
  echo "canonical image metadata projection is incomplete" >&2
  exit 2
}
metadata_properties=()
for property in "${image_metadata[@]}"; do
  metadata_properties+=(--property "$property")
done
metadata_key=$(python3 "$CONFIG_HELPER" get namespace)
metadata_key=${metadata_key//-/_}
metadata_properties+=(
  --property "${metadata_key}_artifact_manifest_sha256=$PLATFORM_ARTIFACT_MANIFEST_SHA256"
  --property "${metadata_key}_qcow2_sha256=$PLATFORM_ARTIFACT_QCOW2_SHA256"
  --property "${metadata_key}_nix_closure_sha256=$PLATFORM_ARTIFACT_NIX_CLOSURE_SHA256"
  --property "${metadata_key}_nix_output=$PLATFORM_ARTIFACT_NIX_OUTPUT"
)

canonical_uuid() {
  python3 - "$1" <<'PY'
import sys
from uuid import UUID

try:
    print(str(UUID(sys.argv[1])))
except ValueError as error:
    raise SystemExit("invalid OpenStack project UUID") from error
PY
}

token_project_id_raw=$("$OSC" token issue -f value -c project_id)
token_project_id=$(canonical_uuid "$token_project_id_raw") || {
  echo "OpenStack token returned an invalid project UUID" >&2
  exit 2
}
# Authentication was scoped with OS_PROJECT_NAME. The resulting token UUID is
# the authoritative provider observation; avoid `project show`, which
# openstackclient implements through a project-list API unavailable to common
# project-scoped publication credentials.
if [[ $token_project_id != "$project_id" || ${OS_PROJECT_NAME:-} != "$project" ]]; then
  echo "refusing to publish outside configured OpenStack project $project ($project_id)" >&2
  exit 2
fi
existing_images=$("$OSC" image list --private --name "$image_name" -f json -c ID -c Name)
existing_count=$(python3 -c '
import json, sys
name = sys.argv[1]
try:
    rows = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    raise SystemExit("OpenStack image inventory is malformed")
if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
    raise SystemExit("OpenStack image inventory is malformed")
print(sum(row.get("Name") == name for row in rows))
' "$image_name" <<<"$existing_images")
if [[ $existing_count != 0 ]]; then
  echo "refusing to replace or ambiguously resolve existing image: $image_name" >&2
  echo "publish a versioned name and update config/platform.json explicitly" >&2
  exit 1
fi

local_checksum=$(python3 - "$image_file" <<'PY'
import hashlib
import sys

try:
    digest = hashlib.md5(usedforsecurity=False)
    with open(sys.argv[1], "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
except OSError as error:
    raise SystemExit("could not checksum the image file") from error
print(digest.hexdigest())
PY
)
[[ $local_checksum =~ ^[0-9a-f]{32}$ ]] || {
  echo "could not checksum the image file" >&2
  exit 2
}

image_id=$("$OSC" image create \
  --disk-format qcow2 \
  --container-format bare \
  --file "$image_file" \
  --property hw_qemu_guest_agent=yes \
  "${metadata_properties[@]}" \
  -f value -c id \
  "$image_name")
python3 - "$image_id" <<'PY'
import sys, uuid
try:
    value = str(uuid.UUID(sys.argv[1]))
except (ValueError, AttributeError):
    raise SystemExit("OpenStack image create returned a malformed UUID")
if value != sys.argv[1]:
    raise SystemExit("OpenStack image create returned a non-canonical UUID")
PY
observed=$("$OSC" image show "$image_id" -f json -c id -c name -c status -c owner -c checksum -c os_hash_algo -c os_hash_value -c properties)
OBSERVED_IMAGE="$observed" EXPECTED_METADATA="$metadata_output" EXPECTED_CHECKSUM="$local_checksum" python3 - "$image_id" "$image_name" "$project_id" <<'PY'
import json
import os
import re
import sys
from uuid import UUID

image_id, image_name, project_id = sys.argv[1:]
try:
    image = json.loads(os.environ["OBSERVED_IMAGE"])
    expected = dict(
        line.split("=", 1)
        for line in os.environ["EXPECTED_METADATA"].splitlines()
        if "=" in line
    )
    observed_id = str(UUID(image["id"]))
    observed_owner = str(UUID(image["owner"]))
    observed_name = image["name"]
    observed_status = image["status"]
    observed_checksum = image["checksum"]
    observed_hash_algorithm = image["os_hash_algo"]
    observed_hash_value = image["os_hash_value"]
    properties = image["properties"]
    if not isinstance(properties, dict) or not isinstance(observed_checksum, str):
        raise ValueError("properties or checksum is malformed")
except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
    raise SystemExit("published image projection is malformed") from error
if (
    observed_id != image_id
    or observed_name != image_name
    or observed_status != "active"
    or observed_owner != project_id
    or not re.fullmatch(r"[0-9a-f]{32}", observed_checksum)
    or observed_checksum != os.environ["EXPECTED_CHECKSUM"]
    or observed_hash_algorithm != "sha256"
    or observed_hash_value != os.environ["PLATFORM_ARTIFACT_QCOW2_SHA256"]
    or any(properties.get(key) != value for key, value in expected.items())
    or properties.get(next(key for key in properties if key.endswith("_artifact_manifest_sha256")), "") != os.environ["PLATFORM_ARTIFACT_MANIFEST_SHA256"]
    or properties.get(next(key for key in properties if key.endswith("_qcow2_sha256")), "") != os.environ["PLATFORM_ARTIFACT_QCOW2_SHA256"]
    or properties.get(next(key for key in properties if key.endswith("_nix_closure_sha256")), "") != os.environ["PLATFORM_ARTIFACT_NIX_CLOSURE_SHA256"]
    or properties.get(next(key for key in properties if key.endswith("_nix_output")), "") != os.environ["PLATFORM_ARTIFACT_NIX_OUTPUT"]
):
    raise SystemExit("published image identity, owner, active status, checksum, or metadata could not be verified")
PY
echo "published role=$role image=$image_id status=active checksum=$local_checksum source_commit=$SOURCE_COMMIT"
