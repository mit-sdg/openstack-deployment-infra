#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config

OSC=${OSC:-openstack}
TEMPLATE=${TEMPLATE:-$SCRIPT_DIR/../cloud-init-nixos/builder.yaml}
PKI_DIR=${PKI_DIR:?set PKI_DIR to the internal PKI directory}
STORAGE_SECRETS_FILE=${STORAGE_SECRETS_FILE:?set STORAGE_SECRETS_FILE to the storage bootstrap secret path}
BUILDER_OPERATOR_PUBLIC_KEY=${BUILDER_OPERATOR_PUBLIC_KEY:?set BUILDER_OPERATOR_PUBLIC_KEY to the builder SSH public key}
IMAGE_NAME=${IMAGE_NAME:-$PLATFORM_BUILDER_IMAGE}
FLAVOR_NAME=${FLAVOR_NAME:-$PLATFORM_BUILDER_FLAVOR}
NETWORK_NAME=${NETWORK_NAME:-$PLATFORM_NETWORK}
SECURITY_GROUP=${SECURITY_GROUP:-$PLATFORM_PREFIX-builder}
BOOTSTRAP_MARKER=${BOOTSTRAP_MARKER:-$PLATFORM_NAMESPACE NixOS rootless builder ready}

usage() {
  echo "usage: $0 create|delete|show BUILD_UUID" >&2
  exit 2
}
[[ $# == 2 ]] || usage
action=$1
build_id=$2
configured_project_id=$PLATFORM_PROJECT_ID
expected_project_name=${EXPECTED_PROJECT_NAME:-$PLATFORM_PROJECT}
expected_project_id=${EXPECTED_PROJECT_ID:-$configured_project_id}
if [[ $PLATFORM_PROJECT != "$expected_project_name" || $configured_project_id != "$expected_project_id" ||
      ${OS_PROJECT_NAME:-} != "$expected_project_name" ]]; then
  echo "refusing to run outside the exact configured OpenStack project name and UUID" >&2
  exit 2
fi
verify_openstack_project "$OSC" || exit 2
[[ -n ${token_project_id:-} ]] || exit 2
build_id=$(python3 - "$build_id" <<'PY'
import sys,uuid
try:
    value=str(uuid.UUID(sys.argv[1]))
except ValueError:
    raise SystemExit("build ID must be a UUID")
if value != sys.argv[1]:
    raise SystemExit("build ID must be a canonical lowercase UUID")
print(value)
PY
)
short_id=${build_id//-/}
short_id=${short_id:0:12}
server_name="${PLATFORM_PREFIX}-builder-${short_id}"
port_name="${server_name}-v4"
metadata_args=(
  --property "${PLATFORM_METADATA_PREFIX}_build_id=$build_id"
  --property "${PLATFORM_METADATA_PREFIX}_managed_by=platform"
)
port_description="managed-by=platform;build-id=$build_id"

resolve_named_id() {
  local kind=$1 name=$2 payload
  payload=$("$OSC" "$kind" list --name "$name" -f json -c ID -c Name)
  python3 -c '
import json,sys,uuid
name,kind=sys.argv[1:]
rows=json.load(sys.stdin)
if not isinstance(rows,list): raise SystemExit(f"{kind} lookup was malformed")
def field(row,key): return next((v for k,v in row.items() if str(k).lower()==key.lower()),None)
matches=[row for row in rows if isinstance(row,dict) and field(row,"Name")==name]
if len(matches)>1: raise SystemExit(f"refusing ambiguous duplicate {kind} name: {name}")
if matches:
 value=str(uuid.UUID(str(field(matches[0],"ID"))))
 if value != field(matches[0],"ID"): raise SystemExit(f"{kind} UUID was not canonical")
 print(value)
' "$name" "$kind" <<<"$payload"
}

server_id_for_name() { resolve_named_id server "$server_name"; }
port_id_for_name() { resolve_named_id port "$port_name"; }

observed_resource_id() {
  local resource=$1
  python3 -c '
import json,sys,uuid
resource=sys.argv[1]; value=json.load(sys.stdin).get(resource)
if value is None: raise SystemExit(0)
if not isinstance(value,dict): raise SystemExit(f"observed {resource} was malformed")
raw=value.get("id")
try: parsed=str(uuid.UUID(str(raw)))
except ValueError: raise SystemExit(f"observed {resource} UUID was malformed")
if parsed != raw: raise SystemExit(f"observed {resource} UUID was not canonical")
print(parsed)
' "$resource"
}

wait_for_bootstrap() {
  local server_id=$1 attempts=${BOOTSTRAP_ATTEMPTS:-90}
  local interval=${BOOTSTRAP_POLL_INTERVAL:-10}
  local status log
  for ((i=1; i<=attempts; i++)); do
    status=$("$OSC" server show "$server_id" -f value -c status 2>/dev/null || true)
    [[ $status != ERROR ]] || { echo "$server_id entered ERROR state" >&2; return 1; }
    log=$("$OSC" console log show "$server_id" 2>/dev/null || true)
    if grep -Fq "$BOOTSTRAP_MARKER" <<<"$log"; then
      echo "builder bootstrap ready: $server_id"
      return 0
    fi
    sleep "$interval"
  done
  echo "timed out waiting for builder bootstrap: $server_id" >&2
  return 1
}

emit_observation() {
  local server_json port_json server_id port_id ready=false
  server_json=$(mktemp); port_json=$(mktemp)
  server_id=$(server_id_for_name); port_id=$(port_id_for_name)
  if [[ -n $server_id ]]; then
    "$OSC" server show "$server_id" -f json -c id -c name -c status -c image -c flavor -c properties >"$server_json"
    if [[ $("$OSC" server show "$server_id" -f value -c status) == ACTIVE ]] &&
       "$OSC" console log show --lines 2000 "$server_id" | grep -Fq "$BOOTSTRAP_MARKER"; then ready=true; fi
  else printf 'null' >"$server_json"; fi
  if [[ -n $port_id ]]; then
    "$OSC" port show "$port_id" -f json -c id -c name -c device_id -c fixed_ips -c description >"$port_json"
  else printf 'null' >"$port_json"; fi
  python3 - "$build_id" "$ready" "$server_json" "$port_json" "$server_name" "$port_name" \
    "$PLATFORM_METADATA_PREFIX" "$port_description" <<'PY'
import ast,json,re,sys,uuid
build_id,ready,server_path,port_path,server_name,port_name,prefix,description=sys.argv[1:]
server=json.load(open(server_path)); port=json.load(open(port_path))
def field(value,name,default=None):
 if not isinstance(value,dict): return default
 return {str(k).lower().replace(" ","_"):v for k,v in value.items()}.get(name,default)
def rid(value):
 if isinstance(value,dict): value=field(value,"id")
 try: parsed=str(uuid.UUID(str(value)))
 except ValueError: return None
 return parsed if parsed == value else None
def image_id(value):
 if isinstance(value,dict): return rid(field(value,"id"))
 match=re.fullmatch(r".* \(([0-9a-f]{8}-[0-9a-f-]{27,36})\)",str(value or ""))
 return rid(match.group(1)) if match else rid(value)
def prop(value,key):
 if isinstance(value,dict): return value.get(key)
 text=str(value or "")
 match=re.search(rf"(?:^|,\s*){re.escape(key)}=(?:'([^']*)'|\"([^\"]*)\"|([^,]*))",text)
 return next((item for item in match.groups() if item is not None),None) if match else None
def flavor(value):
 if isinstance(value,dict): return field(value,"original_name",field(value,"name"))
 return str(value).split(" (",1)[0] if value else None
def address(value):
 values=field(value,"fixed_ips",[]) or []
 if isinstance(values,str):
  try: values=ast.literal_eval(values)
  except (SyntaxError,ValueError): values=[]
 return field(values[0],"ip_address") if values else None
server_out=None
if server is not None:
 sid=rid(field(server,"id")); props=field(server,"properties")
 if sid is None or field(server,"name") != server_name or prop(props,f"{prefix}_managed_by") != "platform" or prop(props,f"{prefix}_build_id") != build_id:
  raise SystemExit("refusing builder with mismatched UUID metadata or managed-by identity")
 server_out={"id":sid,"name":server_name,"status":str(field(server,"status","")),"imageId":image_id(field(server,"image")),"flavorName":flavor(field(server,"flavor")),"managedBy":"platform","buildId":build_id}
port_out=None
if port is not None:
 pid=rid(field(port,"id")); device=rid(field(port,"device_id"))
 if pid is None or field(port,"name") != port_name or field(port,"description") != description:
  raise SystemExit("refusing builder port with mismatched ownership identity")
 if server_out is None:
  if device is not None: raise SystemExit("refusing attached orphan builder port")
 elif device != server_out["id"]: raise SystemExit("builder port attachment does not match server UUID")
 port_out={"id":pid,"name":port_name,"deviceId":device,"address":address(port),"description":description}
if server_out is not None and port_out is None: raise SystemExit("builder server has no verified fixed port")
print(json.dumps({"buildId":build_id,"server":server_out,"port":port_out,"ready":ready=="true" and server_out is not None and port_out is not None},sort_keys=True,separators=(",",":")))
PY
  rm -f "$server_json" "$port_json"
}

case "$action" in
  delete)
    # Resolve once, validate complete ownership/attachment evidence, then mutate
    # only immutable provider UUIDs. Names are never destructive selectors.
    observation=$(emit_observation)
    # Resolve independently: an absent optional server must never shift the
    # required-or-optional port UUID into the server's destructive selector.
    server_id=$(observed_resource_id server <<<"$observation")
    port_id=$(observed_resource_id port <<<"$observation")
    failed=false
    if [[ -n $server_id ]]; then "$OSC" server delete --wait "$server_id" || failed=true; fi
    if [[ -n $port_id ]]; then "$OSC" port delete "$port_id" || failed=true; fi
    [[ $failed == false ]]
    emit_observation >/dev/null
    exit
    ;;
  show)
    emit_observation
    exit 0
    ;;
  create) ;;
  *) usage ;;
esac

for path in "$TEMPLATE" "$PKI_DIR/$PLATFORM_INTERNAL_CA_FILE" \
  "$STORAGE_SECRETS_FILE" "$BUILDER_OPERATOR_PUBLIC_KEY"; do
  [[ -f $path && ! -L $path && -r $path ]] || {
    echo "required input must be a readable direct regular file: $path" >&2
    exit 2
  }
done
python3 - "$STORAGE_SECRETS_FILE" <<'PY'
import os, stat, sys
for value in sys.argv[1:]:
    metadata = os.stat(value, follow_symlinks=False)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SystemExit(f"private input must be owner-only and owned by this operator: {value}")
PY
# Validate any pre-existing resources before create/reconcile uses them.
emit_observation >/dev/null
created_port=false
port_id=$(port_id_for_name)
if [[ -z $port_id ]]; then
  "$OSC" port create \
    --network "$NETWORK_NAME" \
    --security-group "$SECURITY_GROUP" \
    --description "$port_description" \
    "$port_name" >/dev/null
  created_port=true
  echo "created port: $port_name"
fi
port_id=$(port_id_for_name)
builder_ip=$("$OSC" port show "$port_id" -f json | python3 -c 'import json,sys; print(json.load(sys.stdin)["fixed_ips"][0]["ip_address"])')

server_id=$(server_id_for_name)
if [[ -n $server_id ]]; then
  emit_observation >/dev/null
  wait_for_bootstrap "$server_id"
  "$OSC" server show "$server_id" -f value -c status -c addresses
  exit 0
fi

umask 077
tmp=$(mktemp)
chmod 0600 "$tmp"
cleanup() {
  rm -f "$tmp"
  if [[ ${create_failed:-false} == true && $created_port == true ]]; then
    "$OSC" port delete "$port_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
python3 - "$TEMPLATE" "$PKI_DIR" "$PLATFORM_INTERNAL_CA_FILE" \
  "$STORAGE_SECRETS_FILE" "$BUILDER_OPERATOR_PUBLIC_KEY" "$server_name" \
  "$PLATFORM_STORAGE_IP" "$PLATFORM_NAMESPACE" "$PLATFORM_REGISTRY_PORT" \
  "$PLATFORM_OPERATOR_USER" "$PLATFORM_OPERATOR_UID" "$tmp" <<'PY'
from base64 import b64encode
from pathlib import Path
import json,re,sys
(template,pki_dir,ca_file,secrets_path,key_path,builder_name,storage_ip,
 namespace,registry_port,operator_user,operator_uid,output)=sys.argv[1:]
key=Path(key_path).read_text().strip()
if "\n" in key or not key.startswith("ssh-ed25519 "):
    raise SystemExit("builder operator key must be one Ed25519 public-key line")
secrets={}
for line in Path(secrets_path).read_text().splitlines():
    if line and not line.startswith("#"):
        k,v=line.split("=",1); secrets[k]=v
password=secrets.get("REGISTRY_BUILDER_PASSWORD")
if not password: raise SystemExit("registry builder password is missing")
def b64(data: bytes)->str: return b64encode(data).decode()
auth=b64(f"builder:{password}".encode())
docker_auth=json.dumps(
    {"auths": {f"{storage_ip}:{registry_port}": {"auth": auth}}},
    separators=(",", ":"),
).encode()
text=Path(template).read_text()
replacements={
 "__BUILDER_NAME__":builder_name,
 "__BUILDER_OPERATOR_PUBLIC_KEY__":json.dumps(key),
 "__INTERNAL_CA_B64__":b64((Path(pki_dir)/ca_file).read_bytes()),
 "__DOCKER_AUTH_B64__":b64(docker_auth),
 "__STORAGE_IP__":storage_ip,
 "__PLATFORM_NAMESPACE__":namespace,
 "__OPERATOR_USER__":operator_user,
 "__OPERATOR_UID__":operator_uid,
}
found=set(re.findall(r"__[A-Z0-9_]+__",text))
if found != replacements.keys():
    raise SystemExit("cloud-init template placeholders do not match the reviewed renderer")
for placeholder,value in replacements.items():
    text=text.replace(placeholder,value)
if len(text.encode()) > 1_048_576:
    raise SystemExit("generated cloud-init exceeds its safety limit")
Path(output).write_text(text)
PY
create_failed=true
"$OSC" server create \
  --image "$IMAGE_NAME" \
  --flavor "$FLAVOR_NAME" \
  --port "$port_id" \
  "${metadata_args[@]}" \
  --use-config-drive \
  --user-data "$tmp" \
  --wait \
  "$server_name" >/dev/null
create_failed=false
server_id=$(server_id_for_name)
[[ -n $server_id ]] || { echo "created builder UUID could not be resolved" >&2; exit 1; }
emit_observation >/dev/null
echo "created server: $server_id ($builder_ip)"
wait_for_bootstrap "$server_id"
"$OSC" server show "$server_id" -f value -c status -c addresses
