#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONFIG_HELPER="$SCRIPT_DIR/../lib/platform_config.py"
OSC=${OSC:-openstack}

usage() {
  echo "usage: $0 admin|ingress|storage|worker|builder QCOW2_FILE" >&2
  exit 2
}
[[ $# == 2 ]] || usage
role=$1
image_file=$2
case "$role" in admin|ingress|storage|worker|builder) ;; *) usage ;; esac
[[ -f $image_file && ! -L $image_file && -r $image_file ]] || {
  echo "image must be a readable direct regular file: $image_file" >&2
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
metadata_output=$(
  PYTHONPATH="$repository_root" python3 -m platform_cli.openstack \
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
observed_project=$("$OSC" project show "$token_project_id_raw" -f value -c id -c name)
mapfile -t observed_project_fields <<<"$observed_project"
observed_project_id=$(canonical_uuid "${observed_project_fields[0]:-}") || {
  echo "OpenStack project lookup returned an invalid project UUID" >&2
  exit 2
}
if [[ $token_project_id != "$project_id" || $observed_project_id != "$project_id" || ${observed_project_fields[1]:-} != "$project" ]]; then
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
observed=$("$OSC" image show "$image_id" -f json -c id -c name -c status -c owner -c checksum -c properties)
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
    or any(properties.get(key) != value for key, value in expected.items())
):
    raise SystemExit("published image identity, owner, active status, checksum, or metadata could not be verified")
PY
echo "published role=$role image=$image_id status=active checksum=$local_checksum source_commit=$SOURCE_COMMIT"
