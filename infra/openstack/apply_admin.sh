#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config
# shellcheck source=persistent-host.sh
source "$SCRIPT_DIR/persistent-host.sh"

OSC=${OSC:-openstack}
TEMPLATE=${TEMPLATE:-$SCRIPT_DIR/../cloud-init-nixos/admin.yaml}
ADMIN_PUBLIC_KEY=${ADMIN_PUBLIC_KEY:?set ADMIN_PUBLIC_KEY to the admin public-key path}
OPERATOR_PUBLIC_KEY=${OPERATOR_PUBLIC_KEY:?set OPERATOR_PUBLIC_KEY to the operator public-key path}
ADMIN_SECRETS_FILE=${ADMIN_SECRETS_FILE:?set ADMIN_SECRETS_FILE to the admin bootstrap secret path}
PKI_DIR=${PKI_DIR:?set PKI_DIR to the internal PKI directory}
SERVER_NAME=${SERVER_NAME:-$PLATFORM_ADMIN_HOST}
PORT_NAME=${PORT_NAME:-$PLATFORM_ADMIN_PORT}
VOLUME_NAME=${VOLUME_NAME:-$PLATFORM_ADMIN_VOLUME}
VOLUME_SIZE=${VOLUME_SIZE:-$PLATFORM_ADMIN_VOLUME_SIZE}
BACKUP_VOLUME_NAME=${BACKUP_VOLUME_NAME:-$PLATFORM_BACKUP_VOLUME}
BACKUP_VOLUME_SIZE=${BACKUP_VOLUME_SIZE:-$PLATFORM_BACKUP_VOLUME_SIZE}
VOLUME_TYPE=${VOLUME_TYPE:-$PLATFORM_VOLUME_TYPE}
KEYPAIR_NAME=${KEYPAIR_NAME:-$PLATFORM_PREFIX-admin}
IMAGE_NAME=${IMAGE_NAME:-$PLATFORM_ADMIN_IMAGE}
FLAVOR_NAME=${FLAVOR_NAME:-$PLATFORM_ADMIN_FLAVOR}
BOOTSTRAP_MARKER=${BOOTSTRAP_MARKER:-$PLATFORM_NAMESPACE NixOS admin services ready}
BOOTSTRAP_ATTEMPTS=${BOOTSTRAP_ATTEMPTS:-120}
BOOTSTRAP_POLL_INTERVAL=${BOOTSTRAP_POLL_INTERVAL:-10}

verify_openstack_project "$OSC" || exit 2
required_files=(
  "$TEMPLATE" "$ADMIN_PUBLIC_KEY" "$OPERATOR_PUBLIC_KEY" "$ADMIN_SECRETS_FILE"
  "$PKI_DIR/$PLATFORM_INTERNAL_CA_FILE"
  "$PKI_DIR/nomad-server.pem" "$PKI_DIR/nomad-server-key.pem"
  "$PKI_DIR/nomad-cli.pem" "$PKI_DIR/nomad-cli-key.pem"
)
for path in "${required_files[@]}"; do
  [[ -f $path && ! -L $path && -r $path ]] || {
    echo "required input must be a readable direct regular file: $path" >&2
    exit 2
  }
done
image_id=$(resolve_persistent_image_id "$IMAGE_NAME")
flavor_id=$(resolve_persistent_flavor_id "$FLAVOR_NAME")
persistent_host_metadata_args admin "$image_id" "$flavor_id"
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

if ! "$OSC" volume show "$BACKUP_VOLUME_NAME" >/dev/null 2>&1; then
  [[ -z $server_id ]] || {
    echo "$BACKUP_VOLUME_NAME is missing while the configured server already exists" >&2
    exit 1
  }
  "$OSC" volume create --size "$BACKUP_VOLUME_SIZE" --type "$VOLUME_TYPE" "$BACKUP_VOLUME_NAME" >/dev/null
  echo "created volume: $BACKUP_VOLUME_NAME (${BACKUP_VOLUME_SIZE}GiB)"
else
  backup_size=$("$OSC" volume show "$BACKUP_VOLUME_NAME" -f value -c size)
  test "$backup_size" = "$BACKUP_VOLUME_SIZE" || {
    echo "$BACKUP_VOLUME_NAME has unexpected size ${backup_size}GiB" >&2
    exit 1
  }
  echo "existing volume: $BACKUP_VOLUME_NAME (${backup_size}GiB)"
fi
for _ in $(seq 1 120); do
  backup_volume_status=$("$OSC" volume show "$BACKUP_VOLUME_NAME" -f value -c status)
  case "$backup_volume_status" in
    available|in-use) break ;;
    error|error_*) echo "$BACKUP_VOLUME_NAME entered status $backup_volume_status" >&2; exit 1 ;;
  esac
  sleep 5
done
case "$backup_volume_status" in available|in-use) ;; *) echo "timed out waiting for $BACKUP_VOLUME_NAME" >&2; exit 1 ;; esac
backup_volume_id=$(resolve_persistent_volume_id "$BACKUP_VOLUME_NAME" "$BACKUP_VOLUME_SIZE" "$VOLUME_TYPE")

umask 077
tmp=$(mktemp)
chmod 0600 "$tmp"
trap 'rm -f "$tmp"' EXIT
python3 "$SCRIPT_DIR/render_host_user_data.py" \
  --role admin --template "$TEMPLATE" \
  --operator-public-key "$OPERATOR_PUBLIC_KEY" \
  --secret-file "$ADMIN_SECRETS_FILE" --pki-directory "$PKI_DIR" \
  --volume "$VOLUME_NAME" "$volume_id" \
  --volume "$BACKUP_VOLUME_NAME" "$backup_volume_id" --output "$tmp"

if [[ -z $server_id ]]; then
  if ! "$OSC" keypair show "$KEYPAIR_NAME" >/dev/null 2>&1; then
    "$OSC" keypair create --public-key "$ADMIN_PUBLIC_KEY" "$KEYPAIR_NAME" >/dev/null
    echo "created keypair: $KEYPAIR_NAME"
  else
    echo "existing keypair: $KEYPAIR_NAME"
  fi
fi

wait_for_bootstrap() {
  [[ -n $BOOTSTRAP_MARKER ]] || return 0
  local status log report_every elapsed
  for ((i=1; i<=BOOTSTRAP_ATTEMPTS; i++)); do
    status=$("$OSC" server show "$SERVER_NAME" -f value -c status 2>/dev/null || true)
    [[ $status != ERROR ]] || { echo "$SERVER_NAME entered ERROR state" >&2; return 1; }
    log=$("$OSC" console log show "$SERVER_NAME" 2>/dev/null || true)
    if grep -Fq "$BOOTSTRAP_MARKER" <<<"$log"; then
      echo "admin bootstrap ready: $SERVER_NAME"
      return 0
    fi
    # A fresh retained-volume initialization deliberately reboots once. Console
    # output from the pre-reboot boot is retained, so an early readiness failure
    # is diagnostic only; a later success marker remains authoritative.
    report_every=$((30 / BOOTSTRAP_POLL_INTERVAL))
    (( report_every > 0 )) || report_every=1
    if (( i == 1 || i % report_every == 0 )); then
      elapsed=$((i * BOOTSTRAP_POLL_INTERVAL))
      echo "waiting for admin bootstrap: $SERVER_NAME elapsed=${elapsed}s status=${status:-unknown}"
      grep -Ei "cloud-init|nomad|$PLATFORM_NAMESPACE" <<<"$log" | tail -6 || true
    fi
    sleep "$BOOTSTRAP_POLL_INTERVAL"
  done
  echo "timed out waiting for admin bootstrap: $SERVER_NAME" >&2
  grep -Ei 'FAILED|error|dependency|fatal|permission' <<<"${log:-}" | tail -20 >&2 || true
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
    admin "$server_id" "$SERVER_NAME" "$image_id" "$flavor_id" "$FLAVOR_NAME" \
    "$port_id" "$PORT_NAME" "$PLATFORM_ADMIN_IP" \
    "$volume_id=/dev/vdb" "$backup_volume_id=/dev/vdc"
  test "$volume_status" = in-use || {
    echo "$VOLUME_NAME is not attached to the existing admin server" >&2
    exit 1
  }
  test "$backup_volume_status" = in-use || {
    echo "$BACKUP_VOLUME_NAME is not attached to the existing admin server" >&2
    exit 1
  }
  echo "existing server: $SERVER_NAME ($status)"
else
  test "$volume_status" = available || {
    echo "$VOLUME_NAME is not available for initial attachment" >&2
    exit 1
  }
  test "$backup_volume_status" = available || {
    echo "$BACKUP_VOLUME_NAME is not available for initial attachment" >&2
    exit 1
  }
  "$OSC" server create \
    --image "$IMAGE_NAME" \
    --flavor "$FLAVOR_NAME" \
    --port "$port_id" \
    "${PERSISTENT_HOST_METADATA_ARGS[@]}" \
    --key-name "$KEYPAIR_NAME" \
    --block-device "uuid=$volume_id,source_type=volume,destination_type=volume,device_type=disk,device_name=/dev/vdb,boot_index=-1,delete_on_termination=false" \
    --block-device "uuid=$backup_volume_id,source_type=volume,destination_type=volume,device_type=disk,device_name=/dev/vdc,boot_index=-1,delete_on_termination=false" \
    --use-config-drive \
    --user-data "$tmp" \
    --wait \
    "$SERVER_NAME" >/dev/null
  echo "created server: $SERVER_NAME"
fi

wait_for_bootstrap
"$OSC" server show "$SERVER_NAME" -f value -c status -c addresses
"$OSC" volume show "$VOLUME_NAME" -f value -c status -c size
"$OSC" volume show "$BACKUP_VOLUME_NAME" -f value -c status -c size
