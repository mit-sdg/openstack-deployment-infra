#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config

OSC=${OSC:-openstack}
TEMPLATE=${TEMPLATE:-$SCRIPT_DIR/../cloud-init-nixos/worker.yaml}
PKI_DIR=${PKI_DIR:?set PKI_DIR to the internal PKI directory}
STORAGE_SECRETS_FILE=${STORAGE_SECRETS_FILE:?set STORAGE_SECRETS_FILE to the storage bootstrap secret path}
IMAGE_NAME=${IMAGE_NAME:-$PLATFORM_WORKER_IMAGE}
NETWORK_NAME=${NETWORK_NAME:-$PLATFORM_NETWORK}
SECURITY_GROUP=${SECURITY_GROUP:-$PLATFORM_PREFIX-worker}
BOOTSTRAP_MARKER=${BOOTSTRAP_MARKER:-$PLATFORM_NAMESPACE NixOS Nomad worker provisioning data installed}
NOMAD=${NOMAD:-}

usage() {
  echo "usage: $0 create|delete|show APPLICATION_UUID APPLICATION_SLUG" >&2
  exit 2
}
[[ $# == 3 ]] || usage
action=$1
application_id=$2
application_slug=$3

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
application_id=$(python3 - "$application_id" "$application_slug" <<'PY'
import re,sys,uuid
application_id,slug=sys.argv[1:]
try: value=str(uuid.UUID(application_id))
except ValueError: raise SystemExit("application ID must be a UUID")
if value != application_id: raise SystemExit("application ID must be a canonical lowercase UUID")
if not re.fullmatch(r"[a-z][a-z0-9-]{1,38}[a-z0-9]",slug):
    raise SystemExit("slug must be 3-40 lowercase letters, numbers, or hyphens")
if "--" in slug: raise SystemExit("slug cannot contain consecutive hyphens")
print(value)
PY
)

short_id=${application_id//-/}
short_id=${short_id:0:12}
server_name="${PLATFORM_PREFIX}-worker-${short_id}"
port_name="${server_name}-v4"
metadata_args=(
  --property "${PLATFORM_METADATA_PREFIX}_application_id=$application_id"
  --property "${PLATFORM_METADATA_PREFIX}_application_slug=$application_slug"
  --property "${PLATFORM_METADATA_PREFIX}_managed_by=platform"
)
port_description="managed-by=platform;application-id=$application_id;application-slug=$application_slug"

resolve_named_id() {
  local kind=$1 name=$2 payload
  payload=$("$OSC" "$kind" list --name "$name" -f json -c ID -c Name)
  python3 -c '
import json,sys,uuid
name,kind=sys.argv[1:]; rows=json.load(sys.stdin)
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
      echo "worker bootstrap ready: $server_id"
      return 0
    fi
    sleep "$interval"
  done
  echo "timed out waiting for worker bootstrap: $server_id" >&2
  return 1
}

wait_for_nomad() {
  local attempts=${NOMAD_ATTEMPTS:-90}
  local interval=${NOMAD_POLL_INTERVAL:-5}
  local nodes node_id detail
  [[ -x $NOMAD ]] || {
    echo "NOMAD must name the executable Nomad control wrapper" >&2
    return 2
  }
  for ((i=1; i<=attempts; i++)); do
    nodes=$("$NOMAD" node status -json 2>/dev/null || true)
    node_id=$(python3 -c '
import json,sys
name=sys.argv[1]
try: nodes=json.load(sys.stdin)
except json.JSONDecodeError: raise SystemExit(1)
for node in nodes:
    if node.get("Name") == name and node.get("Status") == "ready":
        print(node.get("ID", "")); break
' "$server_name" <<<"$nodes" || true)
    if [[ -n $node_id ]]; then
      detail=$("$NOMAD" node status -json "$node_id" 2>/dev/null || true)
      if python3 -c '
import json,sys
application_id,slug,namespace=sys.argv[1:]
try: node=json.load(sys.stdin)
except json.JSONDecodeError: raise SystemExit(1)
meta=node.get("Meta") or {}
docker=(node.get("Drivers") or {}).get("docker") or {}
ok=(node.get("Status") == "ready" and
    node.get("NodeClass") == f"{namespace}-app" and
    docker.get("Detected") is True and
    docker.get("Healthy") is True and
    meta.get("application_id") == application_id and
    meta.get("application_slug") == slug and
    meta.get("managed_by") == f"{namespace}-platform")
raise SystemExit(0 if ok else 1)
' "$application_id" "$application_slug" "$PLATFORM_NAMESPACE" <<<"$detail"; then
        echo "worker Nomad registration ready: $server_name"
        return 0
      fi
    fi
    if (( i == 1 || i % 6 == 0 )); then
      echo "waiting for worker Nomad registration: $server_name ($i/$attempts)"
    fi
    sleep "$interval"
  done
  echo "timed out waiting for worker Nomad registration: $server_name" >&2
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
  if [[ $ready == true ]]; then
    ready=false
    if NOMAD_ATTEMPTS=1 NOMAD_POLL_INTERVAL=0 wait_for_nomad >/dev/null 2>&1; then ready=true; fi
  fi
  if [[ -n $port_id ]]; then
    "$OSC" port show "$port_id" -f json -c id -c name -c device_id -c fixed_ips -c description >"$port_json"
  else printf 'null' >"$port_json"; fi
  python3 - "$application_id" "$application_slug" "$ready" "$server_json" "$port_json" \
    "$server_name" "$port_name" "$PLATFORM_METADATA_PREFIX" "$port_description" <<'PY'
import ast,json,re,sys,uuid
application_id,slug,ready,server_path,port_path,server_name,port_name,prefix,description=sys.argv[1:]
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
 match=re.search(rf"(?:^|,\s*){re.escape(key)}=(?:'([^']*)'|\"([^\"]*)\"|([^,]*))",str(value or ""))
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
 expected={f"{prefix}_managed_by":"platform",f"{prefix}_application_id":application_id,f"{prefix}_application_slug":slug}
 if sid is None or field(server,"name") != server_name or any(prop(props,k) != v for k,v in expected.items()):
  raise SystemExit("refusing worker with mismatched full application metadata or managed-by identity")
 server_out={"id":sid,"name":server_name,"status":str(field(server,"status","")),"imageId":image_id(field(server,"image")),"flavorName":flavor(field(server,"flavor")),"managedBy":"platform","applicationId":application_id,"applicationSlug":slug}
port_out=None
if port is not None:
 pid=rid(field(port,"id")); device=rid(field(port,"device_id"))
 if pid is None or field(port,"name") != port_name or field(port,"description") != description:
  raise SystemExit("refusing worker port with mismatched ownership identity")
 if server_out is None:
  if device is not None: raise SystemExit("refusing attached orphan worker port")
 elif device != server_out["id"]: raise SystemExit("worker port attachment does not match server UUID")
 port_out={"id":pid,"name":port_name,"deviceId":device,"address":address(port),"description":description}
if server_out is not None and port_out is None: raise SystemExit("worker server has no verified fixed port")
print(json.dumps({"applicationId":application_id,"slug":slug,"server":server_out,"port":port_out,"ready":ready=="true" and server_out is not None and port_out is not None},sort_keys=True,separators=(",",":")))
PY
  rm -f "$server_json" "$port_json"
}

case "$action" in
  delete)
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
  "$PKI_DIR/nomad-worker.pem" "$PKI_DIR/nomad-worker-key.pem" \
  "$STORAGE_SECRETS_FILE"; do
  [[ -f $path && ! -L $path && -r $path ]] || {
    echo "required input must be a readable direct regular file: $path" >&2
    exit 2
  }
done
python3 - "$PKI_DIR/nomad-worker-key.pem" "$STORAGE_SECRETS_FILE" <<'PY'
import os, stat, sys
for value in sys.argv[1:]:
    metadata = os.stat(value, follow_symlinks=False)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SystemExit(f"private input must be owner-only and owned by this operator: {value}")
PY

# Existing worker image/flavor and UUIDs are authoritative. Selection is read
# only below if no managed worker exists and a new server must be allocated.
emit_observation >/dev/null
server_id=$(server_id_for_name)
port_id=$(port_id_for_name)
if [[ -n $server_id ]]; then
  wait_for_bootstrap "$server_id"
  wait_for_nomad
  "$OSC" server show "$server_id" -f value -c status -c addresses
  exit 0
fi
flavor=${FLAVOR_NAME:?set FLAVOR_NAME to the standard worker flavor for a new worker}
image=${IMAGE_NAME:?set IMAGE_NAME to the selected image UUID for a new worker}
"$OSC" flavor show "$flavor" -f json -c name -c vcpus | python3 -c '
import json,sys
expected=sys.argv[1]; value=json.load(sys.stdin)
fields={str(key).lower(): item for key,item in value.items()} if isinstance(value,dict) else {}
name=fields.get("name"); vcpus=fields.get("vcpus")
try: vcpus=int(vcpus)
except (TypeError,ValueError): raise SystemExit("worker flavor vCPU count is malformed")
if name != expected or vcpus != 1:
 raise SystemExit("worker flavor must resolve to the exact configured one-vCPU flavor")
' "$flavor"
created_port=false
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
worker_ip=$("$OSC" port show "$port_id" -f json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["fixed_ips"][0]["ip_address"])')

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
  "$STORAGE_SECRETS_FILE" "$server_name" "$worker_ip" "$application_id" \
  "$application_slug" "$PLATFORM_STORAGE_IP" "$PLATFORM_ADMIN_IP" \
  "$PLATFORM_DATACENTER" "$PLATFORM_NAMESPACE" "$tmp" <<'PY'
from base64 import b64encode
from pathlib import Path
import json,re,sys
(template,pki_dir,ca_file,secrets_path,worker_name,worker_ip,application_id,slug,
 storage_ip,admin_ip,datacenter,namespace,output)=sys.argv[1:]
secrets={}
for line in Path(secrets_path).read_text().splitlines():
    if line and not line.startswith("#"):
        k,v=line.split("=",1); secrets[k]=v
password=secrets.get("REGISTRY_RUNTIME_PASSWORD")
if not password: raise SystemExit("registry runtime password is missing")
pki=Path(pki_dir)
def b64(data: bytes)->str: return b64encode(data).decode()
auth=b64(f"runtime:{password}".encode())
registry=f"{storage_ip}:5000"
docker_auth=json.dumps({"auths":{registry:{"auth":auth}}},separators=(",",":")).encode()
text=Path(template).read_text()
replacements={
 "__WORKER_NAME__":worker_name,
 "__WORKER_IP__":worker_ip,
 "__APPLICATION_ID__":application_id,
 "__APPLICATION_SLUG__":slug,
 "__INTERNAL_CA_B64__":b64((pki/ca_file).read_bytes()),
 "__NOMAD_WORKER_CERT_B64__":b64((pki/"nomad-worker.pem").read_bytes()),
 "__NOMAD_WORKER_KEY_B64__":b64((pki/"nomad-worker-key.pem").read_bytes()),
 "__DOCKER_AUTH_B64__":b64(docker_auth),
 "__ADMIN_IP__":admin_ip,
 "__STORAGE_IP__":storage_ip,
 "__DATACENTER__":datacenter,
 "__PLATFORM_NAMESPACE__":namespace,
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
  --image "$image" \
  --flavor "$flavor" \
  --port "$port_id" \
  "${metadata_args[@]}" \
  --use-config-drive \
  --user-data "$tmp" \
  --wait \
  "$server_name" >/dev/null
create_failed=false
server_id=$(server_id_for_name)
[[ -n $server_id ]] || { echo "created worker UUID could not be resolved" >&2; exit 1; }
emit_observation >/dev/null
echo "created server: $server_id ($worker_ip)"
wait_for_bootstrap "$server_id"
wait_for_nomad
"$OSC" server show "$server_id" -f value -c status -c addresses
