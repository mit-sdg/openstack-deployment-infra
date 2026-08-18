#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config
export POSTGRES_IMAGE=${POSTGRES_IMAGE:-$PLATFORM_POSTGRES_IMAGE}
export MONGODB_IMAGE=${MONGODB_IMAGE:-$PLATFORM_MONGODB_IMAGE}

test -x "$PLATFORM_ROOT/bin/age"
mountpoint -q "$PLATFORM_BACKUPS"
BACKUP_ROOT=${BACKUP_ROOT:-$PLATFORM_BACKUPS/$PLATFORM_NAMESPACE}
AGE=${AGE:-$PLATFORM_ROOT/bin/age}
AGE_KEY=${AGE_KEY:-$PLATFORM_ROOT/persistent/secrets/backup-age-key.txt}
PG_RESTORE_CONTAINER=${PG_RESTORE_CONTAINER:-$PLATFORM_NAMESPACE-pg-restore-test}
MONGO_RESTORE_CONTAINER=${MONGO_RESTORE_CONTAINER:-$PLATFORM_NAMESPACE-mongo-restore-test}
export PG_RESTORE_CONTAINER MONGO_RESTORE_CONTAINER
latest=$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??????T??????Z' | sort | tail -1)
[[ -n $latest ]] || { echo "no backup to restore" >&2; exit 1; }
python3 - "$latest" <<'PY'
from pathlib import Path
import re
import sys
root = Path(sys.argv[1])
values = {}
for line in (root / "MANIFEST").read_text().splitlines():
    key, separator, value = line.partition("=")
    if not separator or key in values:
        raise SystemExit("backup manifest is malformed")
    values[key] = value
expected = {
    "created_at": root.name,
    "format_version": "1",
    "postgres": "pg_dumpall-clean-if-exists",
    "mongodb": "mongodump-archive-gzip",
    "object_storage": "garage-s3-catalog-tar-gzip",
    "registry": "not-included-rebuild-from-source",
}
if values != expected:
    raise SystemExit("backup manifest does not match the restore contract")
checksum_lines = (root / "SHA256SUMS").read_text().splitlines()
checksum_names = []
for line in checksum_lines:
    match = re.fullmatch(r"[0-9a-f]{64}  (postgres\.age|mongodb\.age|garage\.age)", line)
    if match is None:
        raise SystemExit("backup checksums are malformed")
    checksum_names.append(match.group(1))
if sorted(checksum_names) != ["garage.age", "mongodb.age", "postgres.age"]:
    raise SystemExit("backup checksum inventory does not match")
for name in ("postgres.age", "mongodb.age", "garage.age"):
    with (root / name).open("rb") as handle:
        if handle.read(22) != b"age-encryption.org/v1\n":
            raise SystemExit(f"{name} is not age v1 ciphertext")
PY
(
  cd "$latest"
  sha256sum --strict --check SHA256SUMS >/dev/null
)

admin_shell() {
  bash -c "$1"
}

remote_cleanup() {
  admin_shell 'podman rm -f $PG_RESTORE_CONTAINER $MONGO_RESTORE_CONTAINER >/dev/null 2>&1 || true' || true
}
trap remote_cleanup EXIT
remote_cleanup

admin_shell 'podman run -d --name $PG_RESTORE_CONTAINER -e POSTGRES_PASSWORD=restore-test-only "$POSTGRES_IMAGE" >/dev/null'
for _ in $(seq 1 30); do
  admin_shell 'podman exec $PG_RESTORE_CONTAINER pg_isready -U postgres >/dev/null 2>&1' && break
  sleep 2
done
"$AGE" --decrypt --identity "$AGE_KEY" "$latest/postgres.age" | \
  admin_shell 'podman exec -i $PG_RESTORE_CONTAINER psql -v ON_ERROR_STOP=1 -U postgres -d postgres >/dev/null'
admin_shell 'test "$(podman exec $PG_RESTORE_CONTAINER psql -At -U postgres -d postgres -c "SELECT count(*) FROM pg_database WHERE datname='"'"'platform'"'"'")" = 1'
echo "postgres restore=verified"

admin_shell 'podman run -d --name $MONGO_RESTORE_CONTAINER "$MONGODB_IMAGE" >/dev/null'
for _ in $(seq 1 30); do
  admin_shell 'podman exec $MONGO_RESTORE_CONTAINER mongosh --quiet --eval '"'"'db.adminCommand("ping")'"'"' >/dev/null 2>&1' && break
  sleep 2
done
"$AGE" --decrypt --identity "$AGE_KEY" "$latest/mongodb.age" | \
  admin_shell 'podman exec -i $MONGO_RESTORE_CONTAINER mongorestore --archive --gzip --drop >/dev/null'
admin_shell 'podman exec $MONGO_RESTORE_CONTAINER mongosh --quiet --eval '"'"'if (db.getSiblingDB("admin").getCollectionNames().length < 1) quit(1)'"'"''
echo "mongodb restore=verified"

"$AGE" --decrypt --identity "$AGE_KEY" "$latest/garage.age" | python3 -c '
import json,sys,tarfile
archive=tarfile.open(fileobj=sys.stdin.buffer,mode="r|gz")
manifest_member=archive.next()
assert manifest_member.name=="manifest.json"
manifest=json.load(archive.extractfile(manifest_member))
assert manifest["format_version"]==1
seen=0
for member in archive:
    if member.name=="manifest.json":
        continue
    expected=manifest["objects"][seen]
    assert member.name==f"objects/{seen:012d}.bin"
    assert member.size==expected["size"]
    payload=archive.extractfile(member)
    while payload.read(1024*1024): pass
    seen+=1
assert seen==len(manifest["objects"])
assert isinstance(manifest["buckets"], list)
'
echo "garage restore archive=verified"
remote_cleanup
trap - EXIT
verified_at=$(date -u +%Y%m%dT%H%M%SZ)
restore_evidence="$latest/.RESTORE-MANIFEST.tmp"
umask 077
cat >"$restore_evidence" <<EOF
format_version=1
backup=$(basename "$latest")
verified_at=$verified_at
postgres=verified
mongodb=verified
garage=verified
EOF
chmod 0600 "$restore_evidence"
mv "$restore_evidence" "$latest/RESTORE-MANIFEST"
echo "latest platform restore=verified evidence=$latest/RESTORE-MANIFEST"
