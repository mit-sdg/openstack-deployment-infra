#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config
# shellcheck source=persistent-host.sh
source "$SCRIPT_DIR/persistent-host.sh"

OSC=${OSC:-openstack}
TEMPLATE=${TEMPLATE:-$SCRIPT_DIR/../cloud-init-nixos/ingress.yaml}
OPERATOR_PUBLIC_KEY=${OPERATOR_PUBLIC_KEY:?set OPERATOR_PUBLIC_KEY to the operator public-key path}
NOMAD_TOKENS_FILE=${NOMAD_TOKENS_FILE:?set NOMAD_TOKENS_FILE to the Nomad tokens file}
CLOUDFLARE_TUNNEL_TOKEN_FILE=${CLOUDFLARE_TUNNEL_TOKEN_FILE:-}
PKI_DIR=${PKI_DIR:?set PKI_DIR to the internal PKI directory}
SERVER_NAME=${SERVER_NAME:-$PLATFORM_INGRESS_HOST}
PORT_NAME=${PORT_NAME:-$PLATFORM_INGRESS_PORT}
KEYPAIR_NAME=${KEYPAIR_NAME:-$PLATFORM_PREFIX-admin}
IMAGE_NAME=${IMAGE_NAME:-$PLATFORM_INGRESS_IMAGE}
FLAVOR_NAME=${FLAVOR_NAME:-$PLATFORM_INGRESS_FLAVOR}
ENABLE_CLOUDFLARED=${ENABLE_CLOUDFLARED:-true}
BOOTSTRAP_MARKER=${BOOTSTRAP_MARKER:-$PLATFORM_NAMESPACE NixOS ingress services ready}
BOOTSTRAP_ATTEMPTS=${BOOTSTRAP_ATTEMPTS:-120}
BOOTSTRAP_POLL_INTERVAL=${BOOTSTRAP_POLL_INTERVAL:-10}

if [[ $ENABLE_CLOUDFLARED != true && $ENABLE_CLOUDFLARED != false ]]; then
  echo "ENABLE_CLOUDFLARED must be true or false" >&2
  exit 2
fi
verify_openstack_project "$OSC" || exit 2
required_files=(
  "$TEMPLATE" "$OPERATOR_PUBLIC_KEY" "$NOMAD_TOKENS_FILE"
  "$PKI_DIR/$PLATFORM_INTERNAL_CA_FILE"
  "$PKI_DIR/nomad-ingress.pem" "$PKI_DIR/nomad-ingress-key.pem"
)
[[ $ENABLE_CLOUDFLARED == false ]] || required_files+=("$CLOUDFLARE_TUNNEL_TOKEN_FILE")
for path in "${required_files[@]}"; do
  [[ -f $path && ! -L $path && -r $path ]] || {
    echo "required input must be a readable direct regular file: $path" >&2
    exit 2
  }
done
image_id=$(resolve_persistent_image_id "$IMAGE_NAME")
flavor_id=$(resolve_persistent_flavor_id "$FLAVOR_NAME")
persistent_host_metadata_args ingress "$image_id" "$flavor_id"
server_id=$(resolve_persistent_server_id "$SERVER_NAME")
umask 077
tmp=$(mktemp)
chmod 0600 "$tmp"
trap 'rm -f "$tmp"' EXIT
cloudflared_args=()
[[ $ENABLE_CLOUDFLARED == true ]] || cloudflared_args+=(--disable-cloudflared)
[[ $ENABLE_CLOUDFLARED == false ]] || cloudflared_args+=(
  --cloudflare-tunnel-token-file "$CLOUDFLARE_TUNNEL_TOKEN_FILE"
)
python3 "$SCRIPT_DIR/render_host_user_data.py" \
  --role ingress --template "$TEMPLATE" \
  --operator-public-key "$OPERATOR_PUBLIC_KEY" \
  --secret-file "$NOMAD_TOKENS_FILE" --pki-directory "$PKI_DIR" \
  "${cloudflared_args[@]}" --output "$tmp"

wait_for_bootstrap() {
  [[ -n $BOOTSTRAP_MARKER ]] || return 0
  local status log report_every elapsed
  for ((i=1; i<=BOOTSTRAP_ATTEMPTS; i++)); do
    status=$("$OSC" server show "$SERVER_NAME" -f value -c status 2>/dev/null || true)
    [[ $status != ERROR ]] || { echo "$SERVER_NAME entered ERROR state" >&2; return 1; }
    log=$("$OSC" console log show "$SERVER_NAME" 2>/dev/null || true)
    if grep -Fq "$BOOTSTRAP_MARKER" <<<"$log"; then
      echo "ingress bootstrap ready: $SERVER_NAME"
      return 0
    fi
    if grep -Fq "$PLATFORM_NAMESPACE NixOS ingress readiness failed" <<<"$log"; then
      echo "ingress bootstrap reported a definitive failure: $SERVER_NAME" >&2
      grep -Ei 'FAILED|error|dependency|fatal|permission' <<<"$log" | tail -20 >&2 || true
      return 1
    fi
    report_every=$((30 / BOOTSTRAP_POLL_INTERVAL))
    (( report_every > 0 )) || report_every=1
    if (( i == 1 || i % report_every == 0 )); then
      elapsed=$((i * BOOTSTRAP_POLL_INTERVAL))
      echo "waiting for ingress bootstrap: $SERVER_NAME elapsed=${elapsed}s status=${status:-unknown}"
      grep -Ei "cloud-init|traefik|cloudflared|$PLATFORM_NAMESPACE" <<<"$log" | tail -6 || true
    fi
    sleep "$BOOTSTRAP_POLL_INTERVAL"
  done
  echo "timed out waiting for ingress bootstrap: $SERVER_NAME" >&2
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
    ingress "$server_id" "$SERVER_NAME" "$image_id" "$flavor_id" "$FLAVOR_NAME" \
    "$port_id" "$PORT_NAME" "$PLATFORM_INGRESS_IP"
  echo "existing server: $SERVER_NAME ($status)"
else
  "$OSC" server create \
    --image "$IMAGE_NAME" \
    --flavor "$FLAVOR_NAME" \
    --port "$port_id" \
    "${PERSISTENT_HOST_METADATA_ARGS[@]}" \
    --key-name "$KEYPAIR_NAME" \
    --config-drive true \
    --user-data "$tmp" \
    --wait \
    "$SERVER_NAME" >/dev/null
  echo "created server: $SERVER_NAME"
fi

wait_for_bootstrap
"$OSC" server show "$SERVER_NAME" -f value -c status -c addresses
