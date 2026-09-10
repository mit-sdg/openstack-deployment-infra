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
  payload=$("$OSC" "$kind" list --name "$name" -f json -c ID -c Name) || return
  python3 -c '
import json,os,sys,uuid
name,kind=sys.argv[1:]; rows=json.load(sys.stdin)
if not isinstance(rows,list): raise SystemExit(f"{kind} lookup was malformed")
def field(row,key): return next((v for k,v in row.items() if str(k).lower()==key.lower()),None)
matches=[row for row in rows if isinstance(row,dict) and field(row,"Name")==name]
if len(matches)>1: raise SystemExit(f"refusing ambiguous duplicate {kind} name: {name}")
if matches:
 value=str(uuid.UUID(str(field(matches[0],"ID"))))
 raw=field(matches[0],"ID")
 if value != raw and not (os.environ.get("RETAINED_PORT_JSON") and value.replace("-","")==raw):
  raise SystemExit(f"{kind} UUID was not canonical")
 print(value)
' "$name" "$kind" <<<"$payload"
}
server_id_for_name() { resolve_named_id server "$server_name"; }
port_id_for_name() {
  if [[ -n ${RETAINED_PORT_JSON:-} ]]; then
    python3 -c 'import json,os; print(json.loads(os.environ["RETAINED_PORT_JSON"])["port_id"])'
  else resolve_named_id port "$port_name"; fi
}

# Only the exact generation selected by the controller receives this identity.
# An ordinary predecessor never receives the newly reserved port's identity.
if [[ -n ${RETAINED_PORT_JSON:-} ]]; then
  retained_identity=$(python3 - "$application_id" "$PLATFORM_PROJECT_ID" "$PLATFORM_PREFIX" "$PLATFORM_NAMESPACE" <<'PY'
import json,os,sys,uuid
slot,project,prefix,namespace=sys.argv[1:]
r=json.loads(os.environ["RETAINED_PORT_JSON"])
keys={"application_id","request_id","project_id","network_id","subnet_id","address","security_group_id","port_id","name","description"}
if r.keys()!=keys: raise SystemExit("invalid retained primary port fields")
for key in keys-{"address","name","description"}:
 if str(uuid.UUID(r[key]))!=r[key]: raise SystemExit("invalid retained primary port UUID")
app=r["application_id"]
slots={app,*(str(uuid.uuid5(uuid.UUID(app),kind)) for kind in ("stable","candidate"))}
if (slot not in slots or r["project_id"]!=project or r["name"]!=f"{prefix}-app-{app}-primary-v4"
 or r["description"]!=f"{namespace}:app-fixed-port:{app}:{r['request_id']}"):
 raise SystemExit("retained primary port ownership mismatch")
print(r["name"]); print(r["description"])
PY
  )
  port_name=${retained_identity%%$'\n'*}
  port_description=${retained_identity#*$'\n'}
fi

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

observed_port_address() {
  python3 -c '
import json,os,sys
port=json.load(sys.stdin)["port"]
# An absent worker deliberately hides its positively verified reservation in
# the public observation. Its address is still the exact retained identity.
if port is None and os.environ.get("RETAINED_PORT_JSON"):
 port=json.loads(os.environ["RETAINED_PORT_JSON"])
address=port["address"] if isinstance(port,dict) else None
if not isinstance(address,str) or not address: raise SystemExit("worker port has no fixed address")
print(address)
'
}

wait_for_bootstrap() {
  local server_id=$1 observation=$2 attempts=${BOOTSTRAP_ATTEMPTS:-90}
  local interval=${BOOTSTRAP_POLL_INTERVAL:-10}
  local status log
  # Seed only the first poll from the just-validated, post-mutation observation.
  status=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["server"]["status"])' <<<"$observation") || return
  for ((i=1; i<=attempts; i++)); do
    if (( i > 1 )); then
      status=$("$OSC" server show "$server_id" -f value -c status 2>/dev/null || true)
    fi
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

emit_observation() (
  local check_ready=${1:-true} server_json port_json server_id port_id ready=false status
  server_json=$(mktemp) || return
  port_json=$(mktemp) || { rm -f "$server_json"; return 1; }
  trap 'rm -f "$server_json" "$port_json"' EXIT
  # These guards also apply inside command substitution; cleanup cannot mask
  # a failed provider read, duplicate-name refusal, or ownership validation.
  server_id=$(server_id_for_name) || return
  port_id=$(port_id_for_name) || return
  if [[ -n $server_id ]]; then
    if [[ -n ${RETAINED_PORT_JSON:-} ]]; then
      "$OSC" server show "$server_id" -f json >"$server_json" || return
      "$OSC" port list --server "$server_id" -f json -c ID | python3 -c '
import json,sys
rows=json.load(sys.stdin)
if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict) or rows[0].get("ID") not in (sys.argv[1],sys.argv[1].replace("-","")):
 raise SystemExit("retained worker must have exactly its primary port")
' "$port_id" || return
    else
      "$OSC" server show "$server_id" -f json -c id -c name -c status -c image -c flavor -c properties >"$server_json" || return
    fi
    status=$(python3 -c 'import json,sys; print({str(k).lower():v for k,v in json.load(open(sys.argv[1])).items()}.get("status",""))' "$server_json") || return
    if [[ $check_ready == true && $status == ACTIVE ]] &&
       "$OSC" console log show --lines 2000 "$server_id" | grep -Fq "$BOOTSTRAP_MARKER"; then ready=true; fi
  else printf 'null' >"$server_json"; fi
  if [[ $ready == true ]]; then
    ready=false
    if NOMAD_ATTEMPTS=1 NOMAD_POLL_INTERVAL=0 wait_for_nomad >/dev/null 2>&1; then ready=true; fi
  fi
  if [[ -n $port_id ]]; then
    if [[ -n ${RETAINED_PORT_JSON:-} ]]; then
      "$OSC" port show "$port_id" -f json >"$port_json" || return
    else
      "$OSC" port show "$port_id" -f json -c id -c name -c device_id -c fixed_ips -c description >"$port_json" || return
    fi
  else printf 'null' >"$port_json"; fi
  python3 - "$application_id" "$application_slug" "$ready" "$server_json" "$port_json" \
    "$server_name" "$port_name" "$PLATFORM_METADATA_PREFIX" "$port_description" "$server_id" "$port_id" <<'PY'
import ast,json,os,re,sys,uuid
application_id,slug,ready,server_path,port_path,server_name,port_name,prefix,description,server_id,port_id=sys.argv[1:]
server=json.load(open(server_path)); port=json.load(open(port_path))
retained=json.loads(os.environ["RETAINED_PORT_JSON"]) if os.environ.get("RETAINED_PORT_JSON") else None
def provider_id(raw):
 try: parsed=str(uuid.UUID(str(raw)))
 except ValueError: raise SystemExit("malformed provider UUID")
 if raw not in (parsed,parsed.replace("-","")): raise SystemExit("malformed provider UUID")
 return parsed
if retained is not None:
 if isinstance(server,dict):
  for key in ("id","project_id","tenant_id"):
   if key in server: server[key]=provider_id(server[key])
  if isinstance(server.get("image"),dict) and "id" in server["image"]:
   server["image"]["id"]=provider_id(server["image"]["id"])
 if isinstance(port,dict):
  for key in ("id","project_id","network_id"):
   port[key]=provider_id(port.get(key))
  if port.get("device_id"): port["device_id"]=provider_id(port["device_id"])
  groups,fixed=port.get("security_group_ids"),port.get("fixed_ips")
  if not isinstance(groups,list) or not isinstance(fixed,list) or any(not isinstance(v,dict) for v in fixed):
   raise SystemExit("malformed retained port address/group projection")
  port["security_group_ids"]=[provider_id(v) for v in groups]
  port["fixed_ips"]=[{**v,"subnet_id":provider_id(v.get("subnet_id"))} for v in fixed]
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
if server_id and (not isinstance(server,dict) or rid(field(server,"id")) != server_id):
 raise SystemExit("worker server show UUID does not match resolved UUID")
if port_id and (not isinstance(port,dict) or rid(field(port,"id")) != port_id):
 raise SystemExit("worker port show UUID does not match resolved UUID")
server_out=None
if server is not None:
 sid=rid(field(server,"id")); props=field(server,"properties")
 expected={f"{prefix}_managed_by":"platform",f"{prefix}_application_id":application_id,f"{prefix}_application_slug":slug}
 if sid is None or field(server,"name") != server_name or any(prop(props,k) != v for k,v in expected.items()):
  raise SystemExit("refusing worker with mismatched full application metadata or managed-by identity")
 server_out={"id":sid,"name":server_name,"status":str(field(server,"status","")),"imageId":image_id(field(server,"image")),"flavorName":flavor(field(server,"flavor")),"managedBy":"platform","applicationId":application_id,"applicationSlug":slug}
port_out=None
if retained is not None:
 if server_out is not None and field(server,"project_id",field(server,"tenant_id"))!=retained["project_id"]:
  raise SystemExit("retained worker server project drifted")
 expected=dict(id=retained["port_id"],name=retained["name"],project_id=retained["project_id"],
  network_id=retained["network_id"],description=retained["description"],
  fixed_ips=[dict(subnet_id=retained["subnet_id"],ip_address=retained["address"])],
  security_group_ids=[retained["security_group_id"]],port_security_enabled=True,allowed_address_pairs=[],
  device_id=server_out["id"] if server_out else "")
 if not isinstance(port,dict) or port.get("port_security_enabled") is not True or any(port.get(k)!=v for k,v in expected.items()):
  raise SystemExit("retained primary port identity/security/attachment drifted")
 owner=port.get("device_owner")
 if (server_out is None and owner!="") or (server_out is not None and (not isinstance(owner,str) or not owner.startswith("compute:") or owner=="compute:")):
  raise SystemExit("retained primary port device owner drifted")
 if port.get("trunk_details") not in (None,{}): raise SystemExit("retained primary port is a trunk")
 if server_out is None:
  if any(port.get(key) not in (None,"") for key in ("binding:host_id","binding_host_id")):
   raise SystemExit("retained primary port still host bound")
  # Worker absence is distinct from reservation absence. The retained port was
  # positively verified above and must remain allocated while detached.
  port=None
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
)

# Explicit fencing only: a vanished name is not evidence that the expected
# provider UUID vanished (it may have been renamed). Inventory failure is fatal.
require_uuid_absent() {
  local kind=$1 identifier=$2 payload
  payload=$("$OSC" "$kind" list -f json -c ID) || return
  python3 -c '
import json,sys,uuid
rows=json.load(sys.stdin)
if not isinstance(rows,list): raise SystemExit("malformed UUID absence inventory")
for row in rows:
 if not isinstance(row,dict): raise SystemExit("malformed UUID absence row")
 raw=next((v for k,v in row.items() if str(k).lower()=="id"),None)
 parsed=str(uuid.UUID(str(raw)))
 if raw not in (parsed,parsed.replace("-","")): raise SystemExit("malformed provider UUID")
 if parsed==sys.argv[1]: raise SystemExit("expected worker resource still exists outside its observed name")
' "$identifier" <<<"$payload"
}

verify_delete_target() {
  local server_id=$1 port_id=$2
  [[ -n ${EXPECTED_SERVER_ID+x} || -n ${EXPECTED_PORT_ID+x} ]] || return 0
  python3 - "$server_id" "$port_id" <<'PY' || return
import json,os,sys,uuid
server_id,port_id=sys.argv[1:]
expected_server=os.environ.get("EXPECTED_SERVER_ID")
expected_port=os.environ.get("EXPECTED_PORT_ID")
for value in (expected_server,expected_port):
 if str(uuid.UUID(str(value)))!=value: raise SystemExit("expected worker UUID pair must be canonical")
if (server_id and server_id!=expected_server) or (port_id and port_id!=expected_port):
 raise SystemExit("worker UUID does not match expected deletion target")
if os.environ.get("RETAINED_PORT_JSON") and json.loads(os.environ["RETAINED_PORT_JSON"])["port_id"]!=expected_port:
 raise SystemExit("retained primary port does not match expected deletion target")
PY
  [[ -n $server_id ]] || require_uuid_absent server "$EXPECTED_SERVER_ID" || return
  if [[ -z $port_id && -z ${RETAINED_PORT_JSON:-} ]]; then
    require_uuid_absent port "$EXPECTED_PORT_ID" || return
  fi
}

case "$action" in
  delete)
    observation=$(emit_observation false)
    # Resolve independently: an absent optional server must never shift the
    # required-or-optional port UUID into the server's destructive selector.
    server_id=$(observed_resource_id server <<<"$observation")
    port_id=$(observed_resource_id port <<<"$observation")
    verify_delete_target "$server_id" "$port_id"
    failed=false
    if [[ -n $server_id ]]; then
      "$OSC" server delete --wait "$server_id" || failed=true
      if [[ -n ${EXPECTED_SERVER_ID+x} ]]; then
        # A guarded ordinary port deletion needs fresh detached ownership after
        # the server mutation. A lost server-delete reply must be retried first.
        [[ $failed == false ]] || exit 1
        observation=$(emit_observation false)
        server_id=$(observed_resource_id server <<<"$observation")
        port_id=$(observed_resource_id port <<<"$observation")
        verify_delete_target "$server_id" "$port_id"
        [[ -z $server_id ]] || { echo "expected worker still exists after deletion" >&2; exit 1; }
      fi
    fi
    if [[ -n $port_id && -z ${RETAINED_PORT_JSON:-} ]]; then "$OSC" port delete "$port_id" || failed=true; fi
    [[ $failed == false ]]
    observation=$(emit_observation false)
    server_id=$(observed_resource_id server <<<"$observation")
    port_id=$(observed_resource_id port <<<"$observation")
    verify_delete_target "$server_id" "$port_id"
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
allowed_groups = {os.getegid(), *os.getgroups()}
for value in sys.argv[1:]:
    metadata = os.stat(value, follow_symlinks=False)
    mode = stat.S_IMODE(metadata.st_mode)
    owner_private = metadata.st_uid == os.geteuid() and mode in {0o600, 0o640}
    controller_group_private = mode == 0o640 and metadata.st_gid in allowed_groups
    if not owner_private and not controller_group_private:
        raise SystemExit(f"private input is not restricted to its owner or controller group: {value}")
PY

# Existing worker image/flavor and UUIDs are authoritative. Selection is read
# only below if no managed worker exists and a new server must be allocated.
observation=$(emit_observation false)
server_id=$(observed_resource_id server <<<"$observation")
port_id=$(observed_resource_id port <<<"$observation")
# Detached retained ports are validated by emit_observation but intentionally
# omitted from its public absence result. This selector is local, not a lookup.
if [[ -n ${RETAINED_PORT_JSON:-} ]]; then port_id=$(port_id_for_name); fi
if [[ -n $server_id ]]; then
  wait_for_bootstrap "$server_id" "$observation"
  wait_for_nomad
  "$OSC" server show "$server_id" -f value -c status -c addresses
  exit 0
fi
flavor=${FLAVOR_NAME:?set FLAVOR_NAME to the standard worker flavor for a new worker}
image=${IMAGE_NAME:?set IMAGE_NAME to the selected image UUID for a new worker}
flavor_id=$("$OSC" flavor show "$flavor" -f json -c id -c name -c vcpus | python3 -c '
import json,re,sys
expected=sys.argv[1]; value=json.load(sys.stdin)
fields={str(key).lower(): item for key,item in value.items()} if isinstance(value,dict) else {}
name=fields.get("name"); vcpus=fields.get("vcpus")
try: vcpus=int(vcpus)
except (TypeError,ValueError): raise SystemExit("worker flavor vCPU count is malformed")
if name != expected or vcpus < 1:
 raise SystemExit("worker flavor must resolve to the exact configured flavor with at least one vCPU")
identifier=fields.get("id")
if not isinstance(identifier,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}",identifier):
 raise SystemExit("worker flavor ID was malformed")
print(identifier)
' "$flavor")
if [[ -n ${FLAVOR_ID:-} && $flavor_id != "$FLAVOR_ID" ]]; then
  echo "reviewed worker flavor ID drifted" >&2
  exit 2
fi
created_port=false
if [[ -z $port_id ]]; then
  [[ -z ${RETAINED_PORT_JSON:-} ]] || { echo "retained port must already exist" >&2; exit 1; }
  "$OSC" port create \
    --network "$NETWORK_NAME" \
    --security-group "$SECURITY_GROUP" \
    --description "$port_description" \
    "$port_name" >/dev/null
  created_port=true
  echo "created port: $port_name"
  observation=$(emit_observation false)
  server_id=$(observed_resource_id server <<<"$observation")
  [[ -z $server_id ]] || { echo "worker appeared during port creation" >&2; exit 1; }
  port_id=$(observed_resource_id port <<<"$observation")
fi
[[ -n $port_id ]] || { echo "worker fixed port UUID could not be resolved" >&2; exit 1; }
worker_ip=$(observed_port_address <<<"$observation")

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
  "$PLATFORM_DATACENTER" "$PLATFORM_NAMESPACE" \
  "$PLATFORM_NOMAD_HTTP_PORT" "$PLATFORM_NOMAD_RPC_PORT" \
  "$PLATFORM_NOMAD_SERF_PORT" "$PLATFORM_REGISTRY_PORT" "$tmp" <<'PY'
from base64 import b64encode
from pathlib import Path
import json,re,sys
(template,pki_dir,ca_file,secrets_path,worker_name,worker_ip,application_id,slug,
 storage_ip,admin_ip,datacenter,namespace,nomad_http_port,nomad_rpc_port,
 nomad_serf_port,registry_port,output)=sys.argv[1:]
secrets={}
for line in Path(secrets_path).read_text().splitlines():
    if line and not line.startswith("#"):
        k,v=line.split("=",1); secrets[k]=v
password=secrets.get("REGISTRY_RUNTIME_PASSWORD")
if not password: raise SystemExit("registry runtime password is missing")
pki=Path(pki_dir)
def b64(data: bytes)->str: return b64encode(data).decode()
auth=b64(f"runtime:{password}".encode())
registry=f"{storage_ip}:{registry_port}"
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
 "__NOMAD_HTTP_PORT__":nomad_http_port,
 "__NOMAD_RPC_PORT__":nomad_rpc_port,
 "__NOMAD_SERF_PORT__":nomad_serf_port,
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

# Reobserve immediately before attaching. An explicit existing --port is Nova's
# primary NIC with delete_on_termination=false; never ask Nova to allocate it.
observation=$(emit_observation false)
observed_server_id=$(observed_resource_id server <<<"$observation")
observed_port_id=$(observed_resource_id port <<<"$observation")
observed_ip=$(observed_port_address <<<"$observation")
if [[ -n $observed_server_id || $observed_ip != "$worker_ip" ||
      ( -z ${RETAINED_PORT_JSON:-} && $observed_port_id != "$port_id" ) ]]; then
  echo "worker server or fixed port changed before attachment" >&2
  exit 1
fi
create_failed=true
"$OSC" server create \
  --image "$image" \
  --flavor "$flavor_id" \
  --port "$port_id" \
  "${metadata_args[@]}" \
  --use-config-drive \
  --user-data "$tmp" \
  --wait \
  "$server_name" >/dev/null
create_failed=false
observation=$(emit_observation false)
server_id=$(observed_resource_id server <<<"$observation")
[[ -n $server_id ]] || { echo "created worker UUID could not be resolved" >&2; exit 1; }
[[ $(observed_resource_id port <<<"$observation") == "$port_id" ]] || {
  echo "worker fixed port UUID changed during server creation" >&2; exit 1;
}
echo "created server: $server_id ($worker_ip)"
wait_for_bootstrap "$server_id" "$observation"
wait_for_nomad
"$OSC" server show "$server_id" -f value -c status -c addresses
