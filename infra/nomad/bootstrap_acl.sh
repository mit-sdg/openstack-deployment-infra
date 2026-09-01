#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config

NOMAD_ENV=${NOMAD_ENV:-$PLATFORM_ROOT/nomad.env}
SECRETS_DIR=${SECRETS_DIR:-$PLATFORM_ROOT/secrets}
POLICY_FILE=${POLICY_FILE:-$PLATFORM_ROOT/traefik-policy.hcl}

# shellcheck source=/dev/null
. "$NOMAD_ENV"
if [[ ! -s $SECRETS_DIR/nomad-bootstrap.json ]]; then
  umask 077
  nomad acl bootstrap -json >"$SECRETS_DIR/nomad-bootstrap.json"
fi
bootstrap=$(python3 - "$SECRETS_DIR/nomad-bootstrap.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1]))["SecretID"])
PY
)
export NOMAD_TOKEN=$bootstrap
cat >"$POLICY_FILE" <<'EOF'
namespace "*" {
  policy = "read"
  capabilities = ["read-job", "list-jobs"]
}
agent {
  policy = "read"
}
node {
  policy = "read"
}
EOF
nomad acl policy apply \
  -description "Read-only service discovery for Traefik" \
  traefik "$POLICY_FILE" >/dev/null

if [[ ! -s $SECRETS_DIR/nomad-tokens.env ]]; then
  controller=$(nomad acl token create -name "$PLATFORM_CONTROLLER_USER" -type management -json | \
    python3 -c 'import json,sys; print(json.load(sys.stdin)["SecretID"])')
  traefik=$(nomad acl token create -name ingress-traefik -policy traefik -type client -json | \
    python3 -c 'import json,sys; print(json.load(sys.stdin)["SecretID"])')
  umask 077
  printf 'NOMAD_CONTROLLER_TOKEN=%s\nNOMAD_TRAEFIK_TOKEN=%s\n' \
    "$controller" "$traefik" >"$SECRETS_DIR/nomad-tokens.env"
fi
chmod 0600 "$SECRETS_DIR/nomad-bootstrap.json" "$SECRETS_DIR/nomad-tokens.env"
nomad server members >/dev/null
nomad operator raft list-peers >/dev/null
echo "nomad-acl-and-raft=healthy"
