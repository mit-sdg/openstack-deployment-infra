#!/bin/bash
set -euo pipefail

OSC=${OSC:-openstack}
SERVER_NAME=${1:?usage: pin_ephemeral_host_key.sh SERVER_NAME IP KNOWN_HOSTS}
IP=${2:?usage: pin_ephemeral_host_key.sh SERVER_NAME IP KNOWN_HOSTS}
KNOWN_HOSTS=${3:?usage: pin_ephemeral_host_key.sh SERVER_NAME IP KNOWN_HOSTS}

python3 - "$SERVER_NAME" "$IP" "$KNOWN_HOSTS" <<'PY'
import ipaddress,pathlib,re,sys
name,address,known_hosts=sys.argv[1:]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",name):
    raise SystemExit("server name is malformed")
ipaddress.ip_address(address)
path=pathlib.Path(known_hosts)
if not path.name or "\x00" in known_hosts:
    raise SystemExit("known-hosts path is malformed")
PY
umask 077
install -d -m 0700 "$(dirname "$KNOWN_HOSTS")"
touch "$KNOWN_HOSTS"
chmod 0600 "$KNOWN_HOSTS"
log=$(mktemp)
scan=$(mktemp)
trap 'rm -f "$log" "$scan"' EXIT
trusted=""
for _ in $(seq 1 90); do
  "$OSC" console log show --lines 2000 "$SERVER_NAME" >"$log"
  trusted=$(grep -E 'ED25519.*SHA256:|SHA256:.*ED25519' "$log" | \
    grep -oE 'SHA256:[A-Za-z0-9+/]{43}=?' | tail -1 || true)
  [[ -n $trusted ]] && break
  sleep 5
done
[[ -n $trusted ]] || { echo "trusted ED25519 console fingerprint not found" >&2; exit 1; }
for _ in $(seq 1 30); do
  ssh-keyscan -T 10 -t ed25519 "$IP" 2>/dev/null >"$scan" || true
  [[ -s $scan ]] && break
  sleep 2
done
[[ -s $scan ]] || { echo "could not scan the ephemeral host key" >&2; exit 1; }
observed=$(ssh-keygen -lf "$scan" | awk '{print $2}')
[[ $trusted == "$observed" ]] || {
  echo "ephemeral host key does not match the trusted console" >&2
  exit 1
}
ssh-keygen -q -R "$IP" -f "$KNOWN_HOSTS" >/dev/null 2>&1 || true
cat "$scan" >>"$KNOWN_HOSTS"
sort -u "$KNOWN_HOSTS" -o "$KNOWN_HOSTS"
chmod 0600 "$KNOWN_HOSTS"
echo "ephemeral-host-key=verified"
