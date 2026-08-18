#!/bin/bash
# shellcheck shell=bash

load_platform_config() {
  local helper
  helper=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/platform_config.py
  eval "$(python3 "$helper" shell)"
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
