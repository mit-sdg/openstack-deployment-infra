#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config

SERVICE=${1:?usage: emit_logical_backup.sh postgres|mongodb|garage}
SECRETS_FILE=${SECRETS_FILE:-$PLATFORM_ROOT/secrets/storage-bootstrap.env}
CA_FILE=${CA_FILE:-$PLATFORM_ROOT/secrets/nomad-cli/internal-ca.pem}
STORAGE_HOST=${STORAGE_HOST:-$PLATFORM_STORAGE_IP}
SERVICE_CHECK_PYTHON=${SERVICE_CHECK_PYTHON:-$PLATFORM_ROOT/tools/service-check-venv/bin/python}
GARAGE_EMIT_SCRIPT=${GARAGE_EMIT_SCRIPT:-$PLATFORM_ROOT/persistent/platform/infra/backup/emit_garage_backup.py}
POSTGRES_IMAGE=${POSTGRES_IMAGE:-$PLATFORM_POSTGRES_IMAGE}
MONGODB_IMAGE=${MONGODB_IMAGE:-$PLATFORM_MONGODB_IMAGE}

# shellcheck source=/dev/null
set -a
. "$SECRETS_FILE"
set +a

DATABASE_BACKUP_COMMAND=${DATABASE_BACKUP_COMMAND:-openstack-platform-storage-backup}
case "$SERVICE" in
  postgres|mongodb)
    kind=$SERVICE
    [[ $kind != mongodb ]] || kind=mongo
    catalog_arguments=()
    if [[ -n ${DATABASE_BACKUP_CATALOG_DIR:-} ]]; then
      catalog_arguments=(--catalog "$DATABASE_BACKUP_CATALOG_DIR/$SERVICE-catalog.json")
    fi
    exec "$DATABASE_BACKUP_COMMAND" emit --type "$kind" "${catalog_arguments[@]}"
    ;;
  garage)
    exec "$SERVICE_CHECK_PYTHON" "$GARAGE_EMIT_SCRIPT"
    ;;
  *)
    echo "unsupported backup service: $SERVICE" >&2
    exit 2
    ;;
esac
