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
BROKER_RESTORE_LAUNCHER=${BROKER_RESTORE_LAUNCHER:-openstack-platform-management-broker-backup}
GARAGE_VERIFY_SCRIPT=${GARAGE_VERIFY_SCRIPT:-}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
GARAGE_VERIFY_SCRIPT=${GARAGE_VERIFY_SCRIPT:-$SCRIPT_DIR/verify_garage_backup.py}
MANAGED_RESTORE_LAUNCHER=${MANAGED_RESTORE_LAUNCHER:-$SCRIPT_DIR/restore_managed_data.sh}
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

if [[ -d $IMPORTED/management-broker ]]; then
  source=$(find "$IMPORTED/management-broker" -maxdepth 1 -type f -name '*.sqlite3.age' -print -quit)
  [[ -n $source ]]
  "$AGE" --decrypt --identity "$CONTROLLER_IDENTITY" --output "$scratch/management-broker.sqlite3" "$source"
  chmod 0600 "$scratch/management-broker.sqlite3"
  "$BROKER_RESTORE_LAUNCHER" verify "$scratch/management-broker.sqlite3"
fi

keys_arguments=()
key_archive=$(find "$IMPORTED/hosted-controller" -maxdepth 1 -type f -name '*.tar.age' -print -quit)
if [[ -n $key_archive ]]; then
  "$AGE" --decrypt --identity "$CONTROLLER_IDENTITY" --output "$scratch/source-keys.tar" "$key_archive"
  chmod 0600 "$scratch/source-keys.tar"
  python3 - "$scratch/source-keys.tar" "$scratch/hosted-controller.sqlite3" <<'PYKEYS'
import os,sqlite3,sys,tarfile
assert os.stat(sys.argv[1]).st_size<=32*1024*1024
connection=sqlite3.connect(f"file:{sys.argv[2]}?mode=ro",uri=True)
allowed={row[0] for row in connection.execute("SELECT slug FROM applications")}
connection.close()
pairs={}
with tarfile.open(sys.argv[1],"r:") as archive:
 for member in archive:
  parts=member.name.split("/")
  assert len(parts)==2 and parts[0] in allowed and parts[1] in {"id_ed25519","id_ed25519.pub"}
  assert member.isfile() and 0 < member.size <= 16384
  seen=pairs.setdefault(parts[0],set())
  assert parts[1] not in seen
  seen.add(parts[1])
  assert len(archive.extractfile(member).read())==member.size
assert all(pair=={"id_ed25519","id_ed25519.pub"} for pair in pairs.values())
PYKEYS
  keys_arguments=(--source-keys-archive "$scratch/source-keys.tar" --source-keys-directory "$WORK/replacements/source-keys")
fi

managed="$IMPORTED/managed-data"
"$AGE" --decrypt --identity "$MANAGED_IDENTITY" "$managed/garage.age" | \
  "${SERVICE_CHECK_PYTHON:-python3}" "$GARAGE_VERIFY_SCRIPT" --offline
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
    "${keys_arguments[@]}" \
    --yes
)"
printf '%s\n' "$hosted_output"
grep -Eq '^restore=verified schema-version=[0-9]+ integrity=ok$' <<<"$hosted_output"

if [[ -f $scratch/management-broker.sqlite3 ]]; then
  broker_state="$WORK/replacements/management-broker"
  install -d -m 0700 "$broker_state"
  "$BROKER_RESTORE_LAUNCHER" restore "$scratch/management-broker.sqlite3" --destination "$broker_state/management.sqlite3" --yes
  "$BROKER_RESTORE_LAUNCHER" verify "$broker_state/management.sqlite3"
fi

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

managed_output="$(PLATFORM_CONFIG="$PLATFORM_CONFIG" AGE_KEY="$MANAGED_IDENTITY" GARAGE_RESTORE_CONTROLLER_DATABASE="$hosted_destination" "$MANAGED_RESTORE_LAUNCHER" --yes "$managed")"
printf '%s\n' "$managed_output"
grep -Eq '^managed-data-restore=verified source=' <<<"$managed_output"

python3 - "$WORK/DRILL-EVIDENCE.json" "$(basename "$BUNDLE")" "$record_counts" "$WORK/replacements/management-broker/management.sqlite3" "$scratch/source-keys.tar" <<'PY'
import json,os,sys
path,bundle,counts,broker_path,keys_path=sys.argv[1:]
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
 "appImages":"rebuild-by-redeploy",
}
if os.path.isfile(keys_path):
 evidence["sourceKeys"]="restored"
if os.path.isfile(broker_path):
 import sqlite3
 connection=sqlite3.connect(f"file:{broker_path}?mode=ro",uri=True)
 try:
  if connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]:
   raise SystemExit("restored broker authentication state was not invalidated")
  evidence["managementBroker"]={"integrity":"ok","sessions":"invalidated"}
 finally:connection.close()
descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
with os.fdopen(descriptor,"w") as output:
 json.dump(evidence,output,sort_keys=True,separators=(",",":")); output.write("\n")
 output.flush(); os.fsync(output.fileno())
PY
echo "full-loss-drill=verified evidence=$WORK/DRILL-EVIDENCE.json"
