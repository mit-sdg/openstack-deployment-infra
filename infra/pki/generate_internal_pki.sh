#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../lib/platform-config.sh
source "$SCRIPT_DIR/../lib/platform-config.sh"
load_platform_config

OUTPUT_DIR=${1:?usage: generate_internal_pki.sh OUTPUT_DIR}
CA_DAYS=${CA_DAYS:-3650}
LEAF_DAYS=${LEAF_DAYS:-825}

umask 077
install -d -m 0700 "$OUTPUT_DIR"

ca_cert="$OUTPUT_DIR/$PLATFORM_INTERNAL_CA_FILE"
ca_key="$OUTPUT_DIR/${PLATFORM_INTERNAL_CA_FILE%.pem}-key.pem"
if [[ ! -f $ca_key || ! -f $ca_cert ]]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out "$ca_key"
  openssl req -x509 -new -sha256 -days "$CA_DAYS" \
    -key "$ca_key" -out "$ca_cert" \
    -subj "/CN=${PLATFORM_DISPLAY_NAME} Platform Internal CA/O=${PLATFORM_ORGANIZATION}"
  echo "created internal CA"
else
  openssl x509 -checkend 15552000 -noout -in "$ca_cert" >/dev/null || {
    echo "existing CA expires in less than 180 days" >&2
    exit 1
  }
  echo "using existing internal CA"
fi

issue_cert() {
  local name=$1 common_name=$2 eku=$3 sans=$4
  local key="$OUTPUT_DIR/${name}-key.pem"
  local cert="$OUTPUT_DIR/${name}.pem"
  local csr ext
  csr=$(mktemp)
  ext=$(mktemp)
  trap 'rm -f "$csr" "$ext"' RETURN
  if [[ -f $key || -f $cert ]]; then
    [[ -f $key && -f $cert ]] || {
      echo "incomplete certificate pair for $name" >&2
      exit 1
    }
    openssl verify -CAfile "$ca_cert" "$cert" >/dev/null
    openssl x509 -checkend 2592000 -noout -in "$cert" >/dev/null || {
      echo "$name expires in less than 30 days" >&2
      exit 1
    }
    echo "using existing certificate: $name"
    return
  fi
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$key"
  openssl req -new -sha256 -key "$key" -out "$csr" -subj "/CN=${common_name}/O=${PLATFORM_ORGANIZATION}"
  cat >"$ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=${eku}
subjectAltName=${sans}
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
  openssl x509 -req -sha256 -days "$LEAF_DAYS" \
    -in "$csr" -CA "$ca_cert" -CAkey "$ca_key" -CAcreateserial \
    -extfile "$ext" -out "$cert" >/dev/null
  echo "created certificate: $name"
}

issue_cert nomad-server server.global.nomad 'serverAuth,clientAuth' \
  "DNS:server.global.nomad,DNS:${PLATFORM_ADMIN_HOST},DNS:nomad.${PLATFORM_PREFIX}.internal,DNS:localhost,IP:${PLATFORM_ADMIN_IP},IP:127.0.0.1"
issue_cert nomad-cli client.global.nomad clientAuth \
  "DNS:client.global.nomad,DNS:${PLATFORM_ADMIN_HOST}"
issue_cert nomad-ingress client.global.nomad clientAuth \
  "DNS:client.global.nomad,DNS:${PLATFORM_INGRESS_HOST}"
issue_cert nomad-worker client.global.nomad clientAuth \
  "DNS:client.global.nomad,DNS:${PLATFORM_PREFIX}-worker"
issue_cert storage "storage.${PLATFORM_PREFIX}.internal" serverAuth \
  "DNS:storage.${PLATFORM_PREFIX}.internal,DNS:postgres.${PLATFORM_PREFIX}.internal,DNS:mongo.${PLATFORM_PREFIX}.internal,DNS:s3.${PLATFORM_PREFIX}.internal,DNS:registry.${PLATFORM_PREFIX}.internal,DNS:${PLATFORM_STORAGE_HOST},IP:${PLATFORM_STORAGE_IP}"

chmod 0600 "$OUTPUT_DIR"/*-key.pem
chmod 0644 "$OUTPUT_DIR"/*.pem
chmod 0600 "$ca_key"
openssl verify -CAfile "$ca_cert" \
  "$OUTPUT_DIR/nomad-server.pem" "$OUTPUT_DIR/nomad-cli.pem" \
  "$OUTPUT_DIR/nomad-ingress.pem" "$OUTPUT_DIR/nomad-worker.pem" \
  "$OUTPUT_DIR/storage.pem" >/dev/null
echo "internal PKI ready: $OUTPUT_DIR"
