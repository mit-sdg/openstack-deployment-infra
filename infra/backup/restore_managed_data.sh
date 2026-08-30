#!/bin/bash
# Destructively restore one verified managed-data evidence set to replacement services.
set -euo pipefail

[[ $# == 2 && $1 == --yes ]] || {
  echo "usage: restore_managed_data.sh --yes BUNDLE_MANAGED_DATA_DIRECTORY" >&2
  exit 64
}
EVIDENCE=$2
[[ $EVIDENCE == /* && -d $EVIDENCE && ! -L $EVIDENCE ]] || {
  echo "managed-data evidence must be a direct absolute directory" >&2
  exit 64
}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config

AGE=${AGE:-$PLATFORM_ROOT/bin/age}
AGE_KEY=${AGE_KEY:-$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt}
SECRETS_FILE=${SECRETS_FILE:-$PLATFORM_ROOT/secrets/storage-bootstrap.env}
SERVICE_CHECK_PYTHON=${SERVICE_CHECK_PYTHON:-$PLATFORM_ROOT/tools/service-check-venv/bin/python}
GARAGE_RESTORE_SCRIPT=${GARAGE_RESTORE_SCRIPT:-$SCRIPT_DIR/restore_garage_backup.py}
REGISTRY_ARTIFACT_SCRIPT=${REGISTRY_ARTIFACT_SCRIPT:-$SCRIPT_DIR/registry_artifact.py}
POSTGRES_IMAGE=${POSTGRES_IMAGE:-$PLATFORM_POSTGRES_IMAGE}
MONGODB_IMAGE=${MONGODB_IMAGE:-$PLATFORM_MONGODB_IMAGE}
CA_FILE=${CA_FILE:-$PLATFORM_ROOT/secrets/nomad-cli/internal-ca.pem}
LOCK=${MANAGED_RESTORE_LOCK:-/run/lock/$PLATFORM_NAMESPACE-managed-restore.lock}

[[ -f $AGE_KEY && ! -L $AGE_KEY && $(stat -c '%a' "$AGE_KEY") == 600 ]]
[[ -f $SECRETS_FILE && ! -L $SECRETS_FILE && $(stat -c '%a' "$SECRETS_FILE") == 600 ]]
exec 9>"$LOCK"
flock -n 9 || { echo "another managed-data restore is running" >&2; exit 1; }

python3 - "$EVIDENCE" <<'PY'
from pathlib import Path
import re,sys
root=Path(sys.argv[1])
expected={
 "created_at": re.compile(r"[0-9]{8}T[0-9]{6}Z"),
 "format_version": re.compile("2"),
 "postgres": re.compile("pg_dumpall-clean-if-exists"),
 "mongodb": re.compile("mongodump-archive-gzip"),
 "object_storage": re.compile("garage-s3-catalog-tar-gzip"),
 "registry": re.compile("distribution-artifacts-tar-gzip"),
}
values={}
for line in (root/"MANIFEST").read_text().splitlines():
 key,sep,value=line.partition("=")
 if not sep or key in values: raise SystemExit("managed-data manifest is malformed")
 values[key]=value
if values.keys()!=expected.keys() or any(not expected[k].fullmatch(v) for k,v in values.items()):
 raise SystemExit("managed-data manifest is unsupported")
lines=(root/"SHA256SUMS").read_text().splitlines()
names=[]
for line in lines:
 match=re.fullmatch(r"[0-9a-f]{64}  (postgres\.age|mongodb\.age|garage\.age|registry\.age)",line)
 if match is None: raise SystemExit("managed-data checksums are malformed")
 names.append(match.group(1))
if sorted(names)!=["garage.age","mongodb.age","postgres.age","registry.age"]:
 raise SystemExit("managed-data checksum inventory is incomplete")
PY
(cd "$EVIDENCE" && sha256sum --strict --check SHA256SUMS >/dev/null)

# Secrets enter tools only through inherited environment or protected files.
set -a
# shellcheck source=/dev/null
source "$SECRETS_FILE"
set +a
export PGPASSWORD=$POSTGRES_PASSWORD PGSSLMODE=verify-full PGSSLROOTCERT=/run/internal-ca.pem
"$AGE" --decrypt --identity "$AGE_KEY" "$EVIDENCE/postgres.age" | \
  podman run --rm --network=host -i \
    --env PGPASSWORD --env PGSSLMODE --env PGSSLROOTCERT \
    --volume "$CA_FILE:/run/internal-ca.pem:ro" "$POSTGRES_IMAGE" \
    psql -v ON_ERROR_STOP=1 --host="$PLATFORM_STORAGE_IP" \
      --port="$PLATFORM_POSTGRES_PORT" --username=platform_admin --dbname=postgres >/dev/null
echo "postgres managed restore=complete"

export MONGODB_URI="mongodb://platform_admin:${MONGO_PASSWORD}@${PLATFORM_STORAGE_IP}:${PLATFORM_MONGODB_PORT}/?authSource=admin&tls=true&tlsCAFile=/run/internal-ca.pem"
"$AGE" --decrypt --identity "$AGE_KEY" "$EVIDENCE/mongodb.age" | \
  podman run --rm --network=host -i --env MONGODB_URI \
    --volume "$CA_FILE:/run/internal-ca.pem:ro" "$MONGODB_IMAGE" \
    sh -ec 'exec mongorestore --uri "$MONGODB_URI" --archive --gzip --drop' >/dev/null
echo "mongodb managed restore=complete"

"$AGE" --decrypt --identity "$AGE_KEY" "$EVIDENCE/garage.age" | \
  "$SERVICE_CHECK_PYTHON" "$GARAGE_RESTORE_SCRIPT"
"$AGE" --decrypt --identity "$AGE_KEY" "$EVIDENCE/registry.age" | \
  "$SERVICE_CHECK_PYTHON" "$REGISTRY_ARTIFACT_SCRIPT" import

echo "managed-data-restore=verified source=$(basename "$EVIDENCE")"
