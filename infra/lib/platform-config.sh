#!/bin/bash
# shellcheck shell=bash

load_platform_config() {
  local helper transport name value expected key
  local -a allowed=(
    PLATFORM_PROJECT PLATFORM_PROJECT_ID PLATFORM_DISPLAY_NAME PLATFORM_ORGANIZATION
    PLATFORM_PREFIX PLATFORM_NAMESPACE PLATFORM_METADATA_PREFIX PLATFORM_INTERNAL_CA_FILE
    PLATFORM_DOMAIN PLATFORM_RECOVERY_DOMAIN_1 PLATFORM_RECOVERY_DOMAIN_2 PLATFORM_DATACENTER
    PLATFORM_REGION PLATFORM_NETWORK PLATFORM_STORAGE_INTERNAL_NAME
    PLATFORM_OBJECT_STORAGE_INTERNAL_NAME PLATFORM_OPERATOR_CIDR PLATFORM_METADATA_ADDRESS
    PLATFORM_ADMIN_IP PLATFORM_INGRESS_IP PLATFORM_STORAGE_IP PLATFORM_ADMIN_HOST
    PLATFORM_INGRESS_HOST PLATFORM_STORAGE_HOST PLATFORM_ADMIN_PORT PLATFORM_INGRESS_PORT
    PLATFORM_STORAGE_PORT PLATFORM_ADMIN_VOLUME PLATFORM_ADMIN_VOLUME_SIZE PLATFORM_BACKUP_VOLUME
    PLATFORM_BACKUP_VOLUME_SIZE PLATFORM_DATA_VOLUME PLATFORM_DATA_VOLUME_SIZE PLATFORM_VOLUME_TYPE
    PLATFORM_ADMIN_IMAGE PLATFORM_INGRESS_IMAGE PLATFORM_STORAGE_IMAGE PLATFORM_WORKER_IMAGE
    PLATFORM_BUILDER_IMAGE PLATFORM_ADMIN_FLAVOR PLATFORM_INGRESS_FLAVOR PLATFORM_STORAGE_FLAVOR
    PLATFORM_WORKER_FLAVOR PLATFORM_BUILDER_FLAVOR PLATFORM_POSTGRES_IMAGE PLATFORM_MONGODB_IMAGE
    PLATFORM_REGISTRY_IMAGE PLATFORM_ROOT PLATFORM_ADMIN_STATE PLATFORM_BACKUPS PLATFORM_DATA
    PLATFORM_APPLICATION_PORT PLATFORM_NOMAD_HTTP_PORT PLATFORM_NOMAD_RPC_PORT PLATFORM_NOMAD_SERF_PORT
    PLATFORM_REGISTRY_PORT PLATFORM_POSTGRES_PORT PLATFORM_MONGODB_PORT PLATFORM_GARAGE_RPC_PORT
    PLATFORM_GARAGE_S3_PORT PLATFORM_OPERATOR_USER PLATFORM_OPERATOR_UID PLATFORM_CONTROLLER_USER
  )
  local -A accepted=() values=()

  helper=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/platform_config.py
  umask 077
  transport=$(mktemp "${TMPDIR:-/tmp}/platform-config.XXXXXXXX") || return 1
  if ! python3 "$helper" shell0 >"$transport"; then
    rm -f -- "$transport"
    return 1
  fi
  if [[ -L $transport || ! -f $transport ]] ||
    [[ $(stat -c %u:%a:%s -- "$transport") != "$(id -u):600:"* ]] ||
    (( $(stat -c %s -- "$transport") > 65536 )); then
    echo "platform configuration transport is not a private bounded regular file" >&2
    rm -f -- "$transport"
    return 1
  fi
  for expected in "${allowed[@]}"; do accepted["$expected"]=1; done

  exec 9<"$transport"
  rm -f -- "$transport"
  while true; do
    name=
    if ! IFS= read -r -d '' name <&9; then
      if [[ -n $name ]]; then
        echo "platform configuration transport ended mid-key" >&2
        exec 9<&-
        return 1
      fi
      break
    fi
    value=
    if ! IFS= read -r -d '' value <&9; then
      echo "platform configuration transport ended mid-value" >&2
      exec 9<&-
      return 1
    fi
    if [[ -z ${accepted[$name]+x} || -n ${values[$name]+x} ]]; then
      echo "platform configuration transport has an unknown or duplicate key" >&2
      exec 9<&-
      return 1
    fi
    values["$name"]=$value
  done
  exec 9<&-

  for expected in "${allowed[@]}"; do
    if [[ -z ${values[$expected]+x} ]]; then
      echo "platform configuration transport is missing a required key" >&2
      return 1
    fi
  done
  for key in "${allowed[@]}"; do
    printf -v "$key" '%s' "${values[$key]}"
    export "$key"
  done
}

verify_openstack_project() {
  local osc=${1:-${OSC:-openstack}}
  local observed_project observed_id observed_name extra
  token_project_id=$("$osc" token issue -f value -c project_id) || {
    echo "could not obtain an authenticated OpenStack project" >&2
    return 1
  }
  python3 - "$token_project_id" "$PLATFORM_PROJECT_ID" <<'PY' || {
import sys
import uuid

try:
    actual = str(uuid.UUID(sys.argv[1]))
    expected = str(uuid.UUID(sys.argv[2]))
except (AttributeError, ValueError):
    raise SystemExit(1)
if actual != expected:
    raise SystemExit(1)
PY
    echo "authenticated OpenStack project UUID does not match configuration" >&2
    return 1
  }
  observed_project=$("$osc" project show "$token_project_id" -f value -c id -c name) || {
    echo "could not verify the authenticated OpenStack project" >&2
    return 1
  }
  mapfile -t observed_lines <<< "$observed_project"
  case ${#observed_lines[@]} in
    1)
      read -r observed_id observed_name extra <<< "${observed_lines[0]}"
      [[ -n $observed_id && -n $observed_name && -z $extra ]] || {
        echo "authenticated OpenStack project response is malformed" >&2
        return 1
      }
      ;;
    2)
      observed_id=${observed_lines[0]}
      observed_name=${observed_lines[1]}
      [[ -n $observed_id && -n $observed_name ]] || {
        echo "authenticated OpenStack project response is malformed" >&2
        return 1
      }
      ;;
    *)
      echo "authenticated OpenStack project response is malformed" >&2
      return 1
      ;;
  esac
  python3 - "$observed_id" "$PLATFORM_PROJECT_ID" <<'PY' || {
import sys
import uuid

try:
    actual = str(uuid.UUID(sys.argv[1]))
    expected = str(uuid.UUID(sys.argv[2]))
except (AttributeError, ValueError):
    raise SystemExit(1)
if actual != expected:
    raise SystemExit(1)
PY
    echo "observed OpenStack project UUID does not match configuration" >&2
    return 1
  }
  [[ ${OS_PROJECT_NAME:-} == "$PLATFORM_PROJECT" && $observed_name == "$PLATFORM_PROJECT" ]] || {
    echo "authenticated OpenStack project name does not match configuration" >&2
    return 1
  }
  # Keep the authenticated UUID available to callers that record their
  # preflight evidence; callers must not treat the configured value as a token.
}
