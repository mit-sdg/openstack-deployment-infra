#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config

IMAGE=${REGISTRY_IMAGE:-$PLATFORM_REGISTRY_IMAGE}
DATA=${REGISTRY_DATA:-$PLATFORM_DATA/registry}
DATA_MOUNT=${DATA_MOUNT:-$PLATFORM_DATA}
REGISTRY_SERVICE=${REGISTRY_SERVICE:-podman-$PLATFORM_NAMESPACE-registry.service}
LOCK_FILE=${LOCK_FILE:-/run/lock/$PLATFORM_NAMESPACE-registry-gc.lock}
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "registry garbage collection is already running" >&2; exit 1; }
mountpoint -q "$DATA_MOUNT"
systemctl stop "$REGISTRY_SERVICE"
restart_registry() {
  systemctl start "$REGISTRY_SERVICE" || true
}
trap restart_registry EXIT
podman run --rm --network none \
  --volume "$DATA:/var/lib/registry" \
  "$IMAGE" garbage-collect /etc/docker/registry/config.yml
restart_registry
trap - EXIT
systemctl is-active --quiet "$REGISTRY_SERVICE"
echo "registry-garbage-collection=complete"
