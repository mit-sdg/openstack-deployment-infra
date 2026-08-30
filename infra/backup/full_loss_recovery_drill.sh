#!/bin/bash
# Exercise recovery using only one off-site bundle and escrowed age identities.
set -euo pipefail

usage() {
  echo "usage: full_loss_recovery_drill.sh BUNDLE EMPTY_WORK_DIRECTORY CONTROLLER_AGE_IDENTITY MANAGED_AGE_IDENTITY [--apply-managed]" >&2
  exit 64
}
[[ $# == 4 || ( $# == 5 && $5 == --apply-managed ) ]] || usage
BUNDLE=$1
WORK=$2
CONTROLLER_IDENTITY=$3
MANAGED_IDENTITY=$4
APPLY_MANAGED=${5:-}
[[ $BUNDLE == /* && -d $BUNDLE && ! -L $BUNDLE ]] || usage
[[ $WORK == /* && ! -e $WORK ]] || {
  echo "drill work directory must be an absent absolute path" >&2
  exit 64
}
for identity in "$CONTROLLER_IDENTITY" "$MANAGED_IDENTITY"; do
  [[ $identity == /* && -f $identity && ! -L $identity && $(stat -c '%a' "$identity") == 600 ]] || {
    echo "drill age identities must be direct absolute mode-0600 files" >&2
    exit 77
  }
done

RECOVERY=${RECOVERY_COMMAND:-openstack-platform-recovery}
AGE=${AGE:-age}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
umask 077
install -d -m 0700 "$WORK"
"$RECOVERY" verify "$BUNDLE"
"$RECOVERY" import "$BUNDLE" --destination "$WORK"
IMPORTED="$WORK/$(basename "$BUNDLE")"

scratch="$WORK/decrypted"
install -d -m 0700 "$scratch"
for component in hosted-controller operator-state; do
  source=$(find "$IMPORTED/$component" -maxdepth 1 -type f -name '*.sqlite3.age' -print -quit)
  [[ -n $source ]]
  "$AGE" --decrypt --identity "$CONTROLLER_IDENTITY" --output "$scratch/$component.sqlite3" "$source"
  chmod 0600 "$scratch/$component.sqlite3"
  python3 - "$scratch/$component.sqlite3" "$component" <<'PY'
import sqlite3,sys
connection=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
try:
 assert connection.execute("PRAGMA integrity_check").fetchall()==[("ok",)]
 assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deployments'").fetchone()
 if sys.argv[2]=="hosted-controller":
  assert connection.execute("SELECT count(*) FROM deployments").fetchone()[0] >= 1
finally: connection.close()
PY
  echo "$component database=verified"
done

managed="$IMPORTED/managed-data"
"$AGE" --decrypt --identity "$MANAGED_IDENTITY" "$managed/registry.age" | \
  python3 "$SCRIPT_DIR/registry_artifact.py" verify
"$AGE" --decrypt --identity "$MANAGED_IDENTITY" "$managed/garage.age" | python3 -c '
import json,sys,tarfile
archive=tarfile.open(fileobj=sys.stdin.buffer,mode="r|gz")
member=archive.next()
assert member is not None and member.name=="manifest.json"
manifest=json.load(archive.extractfile(member))
assert manifest["format_version"]==1 and isinstance(manifest["objects"],list)
seen=0
for index,member in enumerate(archive):
 assert member.name==f"objects/{index:012d}.bin"
 payload=archive.extractfile(member)
 while payload.read(1024*1024): pass
 seen+=1
assert seen==len(manifest["objects"])
'
echo "managed archives=verified"

if [[ $APPLY_MANAGED == --apply-managed ]]; then
  AGE_KEY=$MANAGED_IDENTITY "$SCRIPT_DIR/restore_managed_data.sh" --yes "$managed"
fi

cat >"$WORK/DRILL-EVIDENCE.json" <<EOF
{"bundle":"$(basename "$BUNDLE")","controllerState":"verified","format":"openstack-platform-full-loss-drill-v1","managedData":"$([[ $APPLY_MANAGED == --apply-managed ]] && echo restored || echo verified)","registryArtifacts":"verified"}
EOF
chmod 0600 "$WORK/DRILL-EVIDENCE.json"
echo "full-loss-drill=verified evidence=$WORK/DRILL-EVIDENCE.json"
