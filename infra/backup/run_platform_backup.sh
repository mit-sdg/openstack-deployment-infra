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
GARAGE_VERIFY_SCRIPT=${GARAGE_VERIFY_SCRIPT:-$SCRIPT_DIR/verify_garage_backup.py}
SERVICE_CHECK_PYTHON=${SERVICE_CHECK_PYTHON:-$PLATFORM_ROOT/tools/service-check-venv/bin/python}
EMIT_SCRIPT=${EMIT_SCRIPT:-$PLATFORM_ROOT/persistent/platform/infra/backup/emit_logical_backup.sh}
RETENTION_DAYS=${RETENTION_DAYS:-14}
DATABASE_BACKUP_COMMAND=${DATABASE_BACKUP_COMMAND:-openstack-platform-storage-backup}

umask 077
install -d -m 0700 "$BACKUP_ROOT"
exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || { echo "another platform backup is running" >&2; exit 1; }

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp="$BACKUP_ROOT/.${timestamp}.tmp"
final="$BACKUP_ROOT/$timestamp"
rm -rf "$tmp"
install -d -m 0700 "$tmp"
write_status() {
  python3 - "$BACKUP_ROOT" "$timestamp" "$1" <<'PYSTATUS'
import json,os,sys
from pathlib import Path
root,started,status=sys.argv[1:]
path=Path(root)/".STATUS.tmp"
with path.open("w") as output:
 json.dump({"startedAt":started,"status":status},output)
 output.flush(); os.fsync(output.fileno())
path.chmod(0o600)
path.replace(Path(root)/"STATUS.json")
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
PYSTATUS
}
cleanup() { rm -rf "$tmp"; write_status failed; }
trap cleanup EXIT
write_status running
export DATABASE_BACKUP_CATALOG_DIR="$tmp"
export DATABASE_BACKUP_TMPDIR="$tmp"
export DATABASE_BACKUP_COMMAND

recipient=$("$AGE_KEYGEN" -y "$AGE_KEY")
for service in postgres mongodb garage; do
  "$EMIT_SCRIPT" "$service" | \
    "$AGE" --encrypt --recipient "$recipient" \
      --output "$tmp/${service}.age"
  test -s "$tmp/${service}.age"
  "$AGE" --decrypt --identity "$AGE_KEY" "$tmp/${service}.age" >/dev/null
  if [[ $service == postgres || $service == mongodb ]]; then
    kind=$service; [[ $kind != mongodb ]] || kind=mongo
    "$AGE" --decrypt --identity "$AGE_KEY" "$tmp/$service.age" | "$DATABASE_BACKUP_COMMAND" verify --type "$kind"
    test -s "$tmp/$service-catalog.json"
  fi
  if [[ $service == garage ]]; then
    "$AGE" --decrypt --identity "$AGE_KEY" "$tmp/garage.age" | \
      "$SERVICE_CHECK_PYTHON" "$GARAGE_VERIFY_SCRIPT"
  fi
  echo "$service backup verified"
done
(
  cd "$tmp"
  sha256sum postgres.age mongodb.age garage.age postgres-catalog.json mongodb-catalog.json >SHA256SUMS
  cat >MANIFEST <<EOF
created_at=$timestamp
format_version=4
postgres=database-logical-tar-v1
mongodb=database-logical-tar-v1
object_storage=garage-s3-catalog-tar-gzip
EOF
)
python3 - "$tmp" <<'PYSYNC'
import os,sys
from pathlib import Path
root=Path(sys.argv[1])
for path in root.iterdir():
 if path.is_file():
  with path.open("rb") as handle: os.fsync(handle.fileno())
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
PYSYNC
mv "$tmp" "$final"
python3 - "$BACKUP_ROOT" <<'PYPARENT'
import os,sys
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
PYPARENT
trap - EXIT
write_status succeeded
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??????T??????Z' \
  -mtime "+$RETENTION_DAYS" -exec rm -rf -- {} +
if [[ -d $BACKUP_ROOT/migration-checkpoints ]]; then
  find "$BACKUP_ROOT/migration-checkpoints" -mindepth 2 -maxdepth 2 -type f -name '*.age' \
    -mtime "+$RETENTION_DAYS" -delete
fi
echo "platform backup complete: $final"
