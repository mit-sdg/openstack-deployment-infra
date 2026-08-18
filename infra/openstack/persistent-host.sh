#!/bin/bash
# shellcheck shell=bash

resolve_persistent_server_id() {
  local server_name=$1 payload
  payload=$("$OSC" server list --name "$server_name" -f json -c ID -c Name)
  python3 -c '
import json,sys,uuid
name=sys.argv[1]
rows=json.load(sys.stdin)
if not isinstance(rows,list): raise SystemExit("configured server lookup was malformed")
def field(row,key):
    return next((value for field,value in row.items() if str(field).lower().replace(" ","_") == key),None)
matches=[row for row in rows if isinstance(row,dict) and field(row,"name") == name]
if len(matches) > 1: raise SystemExit("configured server name must resolve exactly once")
if not matches: raise SystemExit(0)
value=field(matches[0],"id")
try: parsed=str(uuid.UUID(str(value)))
except (AttributeError,ValueError): raise SystemExit("configured server UUID was malformed")
if parsed != value: raise SystemExit("configured server UUID was not canonical")
print(parsed)
' "$server_name" <<<"$payload"
}

resolve_persistent_image_id() {
  local image_name=$1 payload
  payload=$(
    "$OSC" image list --private --name "$image_name" -f json -c ID -c Name
  )
  python3 -c '
import json,sys,uuid
name=sys.argv[1]
rows=json.load(sys.stdin)
if not isinstance(rows,list): raise SystemExit("configured image lookup was malformed")
def field(row,key):
    return next((value for name,value in row.items() if str(name).lower().replace(" ","_") == key),None)
matches=[row for row in rows if isinstance(row,dict) and field(row,"name") == name]
if len(matches) != 1: raise SystemExit("configured image name must resolve exactly once")
value=field(matches[0],"id")
try: parsed=str(uuid.UUID(str(value)))
except (AttributeError,ValueError): raise SystemExit("configured image UUID was malformed")
if parsed != value: raise SystemExit("configured image UUID was not canonical")
print(parsed)
' "$image_name" <<<"$payload"
}

resolve_persistent_flavor_id() {
  local flavor_name=$1 payload
  payload=$("$OSC" flavor show "$flavor_name" -f json -c id -c name)
  python3 -c '
import json,sys,uuid
name=sys.argv[1]
value=json.load(sys.stdin)
if not isinstance(value,dict): raise SystemExit("configured flavor lookup was malformed")
def field(key):
    return next((item for field,item in value.items() if str(field).lower().replace(" ","_") == key),None)
if field("name") != name: raise SystemExit("configured flavor name did not match")
identifier=field("id")
try: parsed=str(uuid.UUID(str(identifier)))
except (AttributeError,ValueError): raise SystemExit("configured flavor UUID was malformed")
if parsed != identifier: raise SystemExit("configured flavor UUID was not canonical")
print(parsed)
' "$flavor_name" <<<"$payload"
}

resolve_persistent_volume_id() {
  local volume_name=$1 expected_size=$2 expected_type=$3 payload
  payload=$("$OSC" volume show "$volume_name" -f json -c id -c name -c size -c type)
  python3 -c '
import json,sys,uuid
name,size,volume_type=sys.argv[1:]
value=json.load(sys.stdin)
if not isinstance(value,dict): raise SystemExit("configured volume lookup was malformed")
def field(key):
    return next((item for field,item in value.items() if str(field).lower().replace(" ","_") == key),None)
if field("name") != name: raise SystemExit("configured volume name did not match")
try: observed_size=int(field("size"))
except (TypeError,ValueError): raise SystemExit("configured volume size was malformed")
try: expected=int(size)
except ValueError: raise SystemExit("configured volume size is malformed")
if observed_size != expected: raise SystemExit("configured volume size did not match")
observed_type=field("volume_type") or field("type")
if observed_type != volume_type: raise SystemExit("configured volume type did not match")
identifier=field("id")
try: parsed=str(uuid.UUID(str(identifier)))
except (AttributeError,ValueError): raise SystemExit("configured volume UUID was malformed")
if parsed != identifier: raise SystemExit("configured volume UUID was not canonical")
print(parsed)
' "$volume_name" "$expected_size" "$expected_type" <<<"$payload"
}

persistent_host_metadata_args() {
  local role=$1 image_id=$2 flavor_id=$3
  # The array is intentionally exported through the sourced shell scope to
  # the caller's server-create command.
  # shellcheck disable=SC2034
  PERSISTENT_HOST_METADATA_ARGS=(
    --property "${PLATFORM_METADATA_PREFIX}_managed_by=platform"
    --property "${PLATFORM_METADATA_PREFIX}_role=$role"
    --property "${PLATFORM_METADATA_PREFIX}_project_id=$PLATFORM_PROJECT_ID"
    --property "${PLATFORM_METADATA_PREFIX}_namespace=$PLATFORM_NAMESPACE"
    --property "${PLATFORM_METADATA_PREFIX}_prefix=$PLATFORM_PREFIX"
    --property "${PLATFORM_METADATA_PREFIX}_metadata_version=1"
    --property "${PLATFORM_METADATA_PREFIX}_image_id=$image_id"
    --property "${PLATFORM_METADATA_PREFIX}_flavor_id=$flavor_id"
  )
}

verify_existing_persistent_host() {
  local role=$1 server_id=$2 server_name=$3 image_id=$4 flavor_id=$5 flavor_name=$6
  local port_id=$7 port_name=$8 address=$9
  shift 9
  local server_json port_json server_ports_json attachments_json
  server_json=$(mktemp)
  port_json=$(mktemp)
  server_ports_json=$(mktemp)
  attachments_json=$(mktemp)
  chmod 0600 "$server_json" "$port_json" "$server_ports_json" "$attachments_json"
  cleanup_persistent_host_verification() {
    rm -f "$server_json" "$port_json" "$server_ports_json" "$attachments_json"
  }
  trap cleanup_persistent_host_verification RETURN

  "$OSC" server show "$server_id" -f json \
    -c id -c name -c image -c flavor -c properties -c volumes_attached >"$server_json"
  "$OSC" port show "$port_id" -f json \
    -c id -c name -c device_id -c fixed_ips >"$port_json"
  "$OSC" port list --server "$server_id" -f json -c ID >"$server_ports_json"
  "$OSC" server volume list "$server_id" -f json -c ID -c Device >"$attachments_json"

  local volume
  local -a verifier_args=(
    --role "$role"
    --server-json "$server_json"
    --port-json "$port_json"
    --server-ports-json "$server_ports_json"
    --attachments-json "$attachments_json"
    --server-name "$server_name"
    --server-id "$server_id"
    --image-id "$image_id"
    --flavor-id "$flavor_id"
    --flavor-name "$flavor_name"
    --port-id "$port_id"
    --port-name "$port_name"
    --address "$address"
    --metadata-prefix "$PLATFORM_METADATA_PREFIX"
    --project-id "$PLATFORM_PROJECT_ID"
    --namespace "$PLATFORM_NAMESPACE"
    --platform-prefix "$PLATFORM_PREFIX"
  )
  for volume in "$@"; do
    verifier_args+=(--volume "$volume")
  done
  python3 "$SCRIPT_DIR/verify_persistent_host.py" "${verifier_args[@]}"
  local result=$?
  trap - RETURN
  cleanup_persistent_host_verification
  return "$result"
}
