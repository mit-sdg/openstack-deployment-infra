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

case "$SERVICE" in
  postgres)
    export PGPASSWORD=$POSTGRES_PASSWORD
    export PGSSLMODE=verify-full
    export PGSSLROOTCERT=/run/internal-ca.pem
    exec podman run --rm --network=host \
      --env PGPASSWORD --env PGSSLMODE --env PGSSLROOTCERT \
      --volume "$CA_FILE:/run/internal-ca.pem:ro" \
      "$POSTGRES_IMAGE" \
      pg_dumpall --clean --if-exists \
        --host="$STORAGE_HOST" --port="$PLATFORM_POSTGRES_PORT" --username=platform_admin
    ;;
  mongodb)
    export MONGODB_URI="mongodb://platform_admin:${MONGO_PASSWORD}@${STORAGE_HOST}:${PLATFORM_MONGODB_PORT}/?authSource=admin&tls=true&tlsCAFile=/run/internal-ca.pem"
    exec podman run --rm --network=host \
      --env MONGODB_URI \
      --volume "$CA_FILE:/run/internal-ca.pem:ro" \
      "$MONGODB_IMAGE" \
      sh -ec 'exec mongodump --uri "$MONGODB_URI" --archive --gzip'
    ;;
  garage)
    exec "$SERVICE_CHECK_PYTHON" "$GARAGE_EMIT_SCRIPT"
    ;;
  *)
    echo "unsupported backup service: $SERVICE" >&2
    exit 2
    ;;
esac
