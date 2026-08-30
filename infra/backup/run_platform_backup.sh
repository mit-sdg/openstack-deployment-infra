#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config

BACKUP_ROOT=${BACKUP_ROOT:-$PLATFORM_BACKUPS/$PLATFORM_NAMESPACE}
AGE=${AGE:-$PLATFORM_ROOT/bin/age}
AGE_KEY=${AGE_KEY:-$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt}
AGE_KEYGEN=${AGE_KEYGEN:-$PLATFORM_ROOT/bin/age-keygen}
EMIT_SCRIPT=${EMIT_SCRIPT:-$PLATFORM_ROOT/persistent/platform/infra/backup/emit_logical_backup.sh}
REGISTRY_ARTIFACT_SCRIPT=${REGISTRY_ARTIFACT_SCRIPT:-$PLATFORM_ROOT/persistent/platform/infra/backup/registry_artifact.py}
SERVICE_CHECK_PYTHON=${SERVICE_CHECK_PYTHON:-$PLATFORM_ROOT/tools/service-check-venv/bin/python}
RETENTION_DAYS=${RETENTION_DAYS:-14}

umask 077
install -d -m 0700 "$BACKUP_ROOT"
exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || { echo "another platform backup is running" >&2; exit 1; }

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp="$BACKUP_ROOT/.${timestamp}.tmp"
final="$BACKUP_ROOT/$timestamp"
rm -rf "$tmp"
install -d -m 0700 "$tmp"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

recipient=$("$AGE_KEYGEN" -y "$AGE_KEY")
for service in postgres mongodb garage; do
  "$EMIT_SCRIPT" "$service" | \
    "$AGE" --encrypt --recipient "$recipient" \
      --output "$tmp/${service}.age"
  test -s "$tmp/${service}.age"
  "$AGE" --decrypt --identity "$AGE_KEY" "$tmp/${service}.age" >/dev/null
  echo "$service backup verified"
done
"$SERVICE_CHECK_PYTHON" "$REGISTRY_ARTIFACT_SCRIPT" export | \
  "$AGE" --encrypt --recipient "$recipient" --output "$tmp/registry.age"
test -s "$tmp/registry.age"
"$AGE" --decrypt --identity "$AGE_KEY" "$tmp/registry.age" | \
  "$SERVICE_CHECK_PYTHON" "$REGISTRY_ARTIFACT_SCRIPT" verify >/dev/null
echo "registry artifacts verified"
(
  cd "$tmp"
  sha256sum postgres.age mongodb.age garage.age registry.age >SHA256SUMS
  cat >MANIFEST <<EOF
created_at=$timestamp
format_version=2
postgres=pg_dumpall-clean-if-exists
mongodb=mongodump-archive-gzip
object_storage=garage-s3-catalog-tar-gzip
registry=distribution-artifacts-tar-gzip
EOF
)
mv "$tmp" "$final"
trap - EXIT
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??????T??????Z' \
  -mtime "+$RETENTION_DAYS" -exec rm -rf -- {} +
echo "platform backup complete: $final"
