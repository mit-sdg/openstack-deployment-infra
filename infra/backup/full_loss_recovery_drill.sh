#!/bin/bash
# Exercise recovery using only one off-site bundle and escrowed age identities.
set -euo pipefail

usage() {
  echo "usage: full_loss_recovery_drill.sh (--full|--verify-only) BUNDLE ABSENT_WORK_DIRECTORY CONTROLLER_AGE_IDENTITY MANAGED_AGE_IDENTITY PLATFORM_CONFIG" >&2
  exit 64
}
[[ $# == 6 && ( $1 == --full || $1 == --verify-only ) ]] || usage
MODE=$1
BUNDLE=$2
WORK=$3
CONTROLLER_IDENTITY=$4
MANAGED_IDENTITY=$5
PLATFORM_CONFIG=$6
[[ $BUNDLE == /* && -d $BUNDLE && ! -L $BUNDLE ]] || usage
[[ $WORK == /* && ! -e $WORK ]] || {
  echo "drill work directory must be an absent absolute path" >&2
  exit 64
}
for private_file in "$CONTROLLER_IDENTITY" "$MANAGED_IDENTITY" "$PLATFORM_CONFIG"; do
  [[ $private_file == /* && -f $private_file && ! -L $private_file && $(stat -c '%U:%a' "$private_file") == "$(id -un):600" ]] || {
    echo "drill identities and platform config must be direct current-user-owned mode-0600 files" >&2
    exit 77
  }
done

RECOVERY=${RECOVERY_COMMAND:-openstack-platform-recovery}
AGE=${AGE:-age}
OPERATOR_RESTORE_LAUNCHER=${OPERATOR_RESTORE_LAUNCHER:-openstack-platform-restore}
HOSTED_RESTORE_LAUNCHER=${HOSTED_RESTORE_LAUNCHER:-openstack-platform-controller-restore}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MANAGED_RESTORE_LAUNCHER=${MANAGED_RESTORE_LAUNCHER:-$SCRIPT_DIR/restore_managed_data.sh}
REGISTRY_ARTIFACT_SCRIPT=${REGISTRY_ARTIFACT_SCRIPT:-$SCRIPT_DIR/registry_artifact.py}
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
  python3 - "$scratch/$component.sqlite3" <<'PY'
import sqlite3,sys
connection=sqlite3.connect(f"file:{sys.argv[1]}?mode=ro",uri=True)
try:
 if connection.execute("PRAGMA integrity_check").fetchall()!=[("ok",)]:
  raise SystemExit("decrypted SQLite integrity check failed")
 required={"schema_migrations","operations","operation_dispatches"}
 observed={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 if not required <= observed:
  raise SystemExit("decrypted SQLite schema evidence is incomplete")
finally:
 connection.close()
PY
done

managed="$IMPORTED/managed-data"
"$AGE" --decrypt --identity "$MANAGED_IDENTITY" "$managed/registry.age" | \
  python3 "$REGISTRY_ARTIFACT_SCRIPT" verify
"$AGE" --decrypt --identity "$MANAGED_IDENTITY" "$managed/garage.age" | python3 -c '
import json,sys,tarfile
archive=tarfile.open(fileobj=sys.stdin.buffer,mode="r|gz")
member=archive.next()
assert member is not None and member.name=="manifest.json"
manifest=json.load(archive.extractfile(member))
assert manifest["format_version"]==1 and isinstance(manifest["objects"],list)
seen=0
while (member:=archive.next()) is not None:
 assert member.name==f"objects/{seen:012d}.bin"
 payload=archive.extractfile(member)
 while payload.read(1024*1024): pass
 seen+=1
assert seen==len(manifest["objects"])
'
echo "recovery archives=verified"

if [[ $MODE == --verify-only ]]; then
  echo "full-loss-drill=verify-only evidence=none"
  exit 0
fi

# Both destinations are children of the caller-supplied absent workspace.  Do
# not use either fixed live destination: exercise the same supported launchers
# against explicit, empty, private replacement directories instead.
operator_state="$WORK/replacements/operator-state"
hosted_state="$WORK/replacements/hosted-controller"
install -d -m 0700 "$WORK/replacements" "$operator_state" "$hosted_state"
operator_destination="$operator_state/platform.sqlite3"
hosted_destination="$hosted_state/platform.sqlite3"
[[ ! -e $operator_destination && ! -L $operator_destination ]]
[[ ! -e $hosted_destination && ! -L $hosted_destination ]]

operator_output="$(
  "$OPERATOR_RESTORE_LAUNCHER" \
    --replacement-state-directory "$operator_state" \
    "$scratch/operator-state.sqlite3" --yes
)"
printf '%s\n' "$operator_output"
grep -Eq '^restore=verified schema-version=[0-9]+ integrity=ok$' <<<"$operator_output"

hosted_output="$(
  "$HOSTED_RESTORE_LAUNCHER" \
    "$scratch/hosted-controller.sqlite3" \
    --destination "$hosted_destination" \
    --platform-config "$PLATFORM_CONFIG" \
    --yes
)"
printf '%s\n' "$hosted_output"
grep -Eq '^restore=verified schema-version=[0-9]+ integrity=ok$' <<<"$hosted_output"

# Prove useful records came through the replacement files, in addition to the
# launchers' deployment-identity, complete-schema, integrity, foreign-key, and
# unfinished-operation validation.
record_counts="$(python3 - "$operator_destination" "$hosted_destination" <<'PY'
import json,sqlite3,sys

def open_verified(path):
 connection=sqlite3.connect(f"file:{path}?mode=ro",uri=True)
 if connection.execute("PRAGMA integrity_check").fetchall()!=[("ok",)]:
  raise SystemExit("restored SQLite integrity check failed")
 unfinished=connection.execute(
  "SELECT count(*) FROM operations WHERE status IN ('running','recovery_required')"
 ).fetchone()[0]
 dispatch=connection.execute(
  "SELECT count(*) FROM operation_dispatches WHERE status IN ('pending','running','recovery_required')"
 ).fetchone()[0]
 if unfinished or dispatch:
  raise SystemExit("restored SQLite database has unfinished operations")
 return connection

operator=open_verified(sys.argv[1])
hosted=open_verified(sys.argv[2])
try:
 images=operator.execute("SELECT count(*) FROM image_selections").fetchone()[0]
 accepted=hosted.execute(
  "SELECT count(*) FROM active_deployments a "
  "JOIN deployment_attempts d ON d.application_id=a.application_id "
  "AND d.deployment_id=a.deployment_id WHERE d.status='succeeded'"
 ).fetchone()[0]
 applications=hosted.execute("SELECT count(*) FROM applications").fetchone()[0]
 if images < 1 or applications < 1 or accepted < 1:
  raise SystemExit("replacement databases do not contain required restored records")
 print(json.dumps({"acceptedDeployments":accepted,"applications":applications,"imageSelections":images},sort_keys=True,separators=(",",":")))
finally:
 operator.close(); hosted.close()
PY
)"

managed_output="$(PLATFORM_CONFIG="$PLATFORM_CONFIG" AGE_KEY="$MANAGED_IDENTITY" "$MANAGED_RESTORE_LAUNCHER" --yes "$managed")"
printf '%s\n' "$managed_output"
grep -Eq '^managed-data-restore=verified source=' <<<"$managed_output"

python3 - "$WORK/DRILL-EVIDENCE.json" "$(basename "$BUNDLE")" "$record_counts" <<'PY'
import json,os,sys
path,bundle,counts=sys.argv[1:]
evidence={
 "bundle":bundle,
 "controllerState":{
  "deploymentIdentity":"verified",
  "integrity":"ok",
  "schema":"verified",
  "unfinishedOperations":"none",
 },
 "format":"openstack-platform-full-loss-drill-v2",
 "managedData":"restored",
 "records":json.loads(counts),
 "registryArtifacts":"restored",
}
descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
with os.fdopen(descriptor,"w") as output:
 json.dump(evidence,output,sort_keys=True,separators=(",",":")); output.write("\n")
 output.flush(); os.fsync(output.fileno())
PY
echo "full-loss-drill=verified evidence=$WORK/DRILL-EVIDENCE.json"
