#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config
# shellcheck source=persistent-host.sh
source "$SCRIPT_DIR/persistent-host.sh"

OSC=${OSC:-openstack}
TEMPLATE=${TEMPLATE:-$SCRIPT_DIR/../cloud-init-nixos/storage.yaml}
OPERATOR_PUBLIC_KEY=${OPERATOR_PUBLIC_KEY:?set OPERATOR_PUBLIC_KEY to the operator public-key path}
STORAGE_SECRETS_FILE=${STORAGE_SECRETS_FILE:?set STORAGE_SECRETS_FILE to the storage bootstrap secret path}
PKI_DIR=${PKI_DIR:?set PKI_DIR to the internal PKI directory}
SERVER_NAME=${SERVER_NAME:-$PLATFORM_STORAGE_HOST}
PORT_NAME=${PORT_NAME:-$PLATFORM_STORAGE_PORT}
VOLUME_NAME=${VOLUME_NAME:-$PLATFORM_DATA_VOLUME}
VOLUME_SIZE=${VOLUME_SIZE:-$PLATFORM_DATA_VOLUME_SIZE}
VOLUME_TYPE=${VOLUME_TYPE:-$PLATFORM_VOLUME_TYPE}
KEYPAIR_NAME=${KEYPAIR_NAME:-$PLATFORM_PREFIX-admin}
IMAGE_NAME=${IMAGE_NAME:-$PLATFORM_STORAGE_IMAGE}
FLAVOR_NAME=${FLAVOR_NAME:-$PLATFORM_STORAGE_FLAVOR}
BOOTSTRAP_MARKER=${BOOTSTRAP_MARKER:-$PLATFORM_NAMESPACE NixOS storage services ready}
BOOTSTRAP_ATTEMPTS=${BOOTSTRAP_ATTEMPTS:-120}
BOOTSTRAP_POLL_INTERVAL=${BOOTSTRAP_POLL_INTERVAL:-10}

verify_openstack_project "$OSC" || exit 2
required_files=(
  "$TEMPLATE" "$OPERATOR_PUBLIC_KEY" "$STORAGE_SECRETS_FILE"
  "$PKI_DIR/$PLATFORM_INTERNAL_CA_FILE" "$PKI_DIR/storage.pem" "$PKI_DIR/storage-key.pem"
)
for path in "${required_files[@]}"; do
  [[ -f $path && ! -L $path && -r $path ]] || {
    echo "required input must be a readable direct regular file: $path" >&2
    exit 2
  }
done
image_id=$(resolve_persistent_image_id "$IMAGE_NAME")
flavor_id=$(resolve_persistent_flavor_id "$FLAVOR_NAME")
persistent_host_metadata_args storage "$image_id" "$flavor_id"
server_id=$(resolve_persistent_server_id "$SERVER_NAME")
if ! "$OSC" volume show "$VOLUME_NAME" >/dev/null 2>&1; then
  [[ -z $server_id ]] || {
    echo "$VOLUME_NAME is missing while the configured server already exists" >&2
    exit 1
  }
  "$OSC" volume create --size "$VOLUME_SIZE" --type "$VOLUME_TYPE" "$VOLUME_NAME" >/dev/null
  echo "created volume: $VOLUME_NAME (${VOLUME_SIZE}GiB)"
else
  size=$("$OSC" volume show "$VOLUME_NAME" -f value -c size)
  test "$size" = "$VOLUME_SIZE" || {
    echo "$VOLUME_NAME has unexpected size ${size}GiB" >&2
    exit 1
  }
  echo "existing volume: $VOLUME_NAME (${size}GiB)"
fi

for _ in $(seq 1 120); do
  volume_status=$("$OSC" volume show "$VOLUME_NAME" -f value -c status)
  case "$volume_status" in
    available|in-use) break ;;
    error|error_*) echo "$VOLUME_NAME entered status $volume_status" >&2; exit 1 ;;
  esac
  sleep 5
done
case "$volume_status" in available|in-use) ;; *) echo "timed out waiting for $VOLUME_NAME" >&2; exit 1 ;; esac
volume_id=$(resolve_persistent_volume_id "$VOLUME_NAME" "$VOLUME_SIZE" "$VOLUME_TYPE")

umask 077
tmp=$(mktemp)
chmod 0600 "$tmp"
trap 'rm -f "$tmp"' EXIT
python3 "$SCRIPT_DIR/render_host_user_data.py" \
  --role storage --template "$TEMPLATE" \
  --operator-public-key "$OPERATOR_PUBLIC_KEY" \
  --secret-file "$STORAGE_SECRETS_FILE" --pki-directory "$PKI_DIR" \
  --volume "$VOLUME_NAME" "$volume_id" --output "$tmp"

wait_for_bootstrap() {
  [[ -n $BOOTSTRAP_MARKER ]] || return 0
  local status log
  for ((i=1; i<=BOOTSTRAP_ATTEMPTS; i++)); do
    status=$("$OSC" server show "$SERVER_NAME" -f value -c status 2>/dev/null || true)
    [[ $status != ERROR ]] || {
      echo "$SERVER_NAME entered ERROR state" >&2
      return 1
    }
    log=$("$OSC" console log show "$SERVER_NAME" 2>/dev/null || true)
    if grep -Fq "$BOOTSTRAP_MARKER" <<<"$log"; then
      echo "storage bootstrap ready: $SERVER_NAME"
      return 0
    fi
    # The retained-volume boot intentionally reboots once and Nova preserves
    # console output across that boundary. Treat an earlier failure marker as
    # diagnostic until a later success marker arrives or the deadline expires.
    report_every=$((30 / BOOTSTRAP_POLL_INTERVAL))
    (( report_every > 0 )) || report_every=1
    if (( i == 1 || i % report_every == 0 )); then
      elapsed=$((i * BOOTSTRAP_POLL_INTERVAL))
      echo "waiting for storage bootstrap: $SERVER_NAME elapsed=${elapsed}s status=${status:-unknown}"
      grep -Ei "cloud-init|podman|postgres|mongodb|garage|registry|nginx|mount|$PLATFORM_NAMESPACE" <<<"$log" | tail -6 || true
    fi
    sleep "$BOOTSTRAP_POLL_INTERVAL"
  done
  echo "timed out waiting for storage bootstrap: $SERVER_NAME" >&2
  grep -Ei 'cloud-init.*FAIL:|FAILED|error|dependency|fatal|permission' <<<"${log:-}" | tail -20 >&2 || true
  return 1
}

port_id=$("$OSC" port show "$PORT_NAME" -f value -c id)
if [[ -n $server_id ]]; then
  status=$("$OSC" server show "$server_id" -f value -c status)
  attached=$("$OSC" port show "$PORT_NAME" -f value -c device_id)
  test "$attached" = "$server_id" || {
    echo "$PORT_NAME is not attached to $SERVER_NAME" >&2
    exit 1
  }
  verify_existing_persistent_host \
    storage "$server_id" "$SERVER_NAME" "$image_id" "$flavor_id" "$FLAVOR_NAME" \
    "$port_id" "$PORT_NAME" "$PLATFORM_STORAGE_IP" "$volume_id=/dev/vdb"
  test "$volume_status" = in-use || {
    echo "$VOLUME_NAME is not attached to the existing storage server" >&2
    exit 1
  }
  echo "existing server: $SERVER_NAME ($status)"
else
  test "$volume_status" = available || {
    echo "$VOLUME_NAME is not available for initial attachment" >&2
    exit 1
  }
  "$OSC" server create \
    --image "$IMAGE_NAME" \
    --flavor "$FLAVOR_NAME" \
    --port "$port_id" \
    "${PERSISTENT_HOST_METADATA_ARGS[@]}" \
    --key-name "$KEYPAIR_NAME" \
    --block-device "uuid=$volume_id,source_type=volume,destination_type=volume,device_type=disk,device_name=/dev/vdb,boot_index=-1,delete_on_termination=false" \
    --use-config-drive \
    --user-data "$tmp" \
    --wait \
    "$SERVER_NAME" >/dev/null
  echo "created server: $SERVER_NAME"
fi

wait_for_bootstrap
"$OSC" server show "$SERVER_NAME" -f value -c status -c addresses
"$OSC" volume show "$VOLUME_NAME" -f value -c status -c size
