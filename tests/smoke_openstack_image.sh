#!/bin/bash
set -euo pipefail

role=${1:?usage: smoke_openstack_image.sh ROLE QCOW2}
image=${2:?usage: smoke_openstack_image.sh ROLE QCOW2}
[[ $role =~ ^(admin|ingress|storage|worker|builder)$ ]] || {
  echo "invalid role: $role" >&2
  exit 2
}
[[ -r $image ]] || {
  echo "image is not readable: $image" >&2
  exit 2
}

for command in genisoimage qemu-img qemu-system-x86_64; do
  command -v "$command" >/dev/null || {
    echo "required command is unavailable: $command" >&2
    exit 2
  }
done

work=$(mktemp -d)
pid=
# shellcheck disable=SC2317 # Invoked through the EXIT trap.
cleanup() {
  if [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

marker="platform-${role}-qcow-smoke-passed"
mkdir -p "$work/config/openstack/latest"
cat > "$work/config/openstack/latest/meta_data.json" <<EOF
{"uuid":"00000000-0000-4000-8000-000000000001","hostname":"${role}-qcow-smoke","name":"${role}-qcow-smoke"}
EOF
cat > "$work/config/openstack/latest/user_data" <<EOF
#cloud-config
final_message: "$marker"
EOF
genisoimage -quiet -rock -joliet -volid config-2 \
  -output "$work/config.iso" "$work/config"

qemu-img create -q -f qcow2 -F qcow2 -b "$(realpath "$image")" "$work/root.qcow2"
qemu-img check "$work/root.qcow2" >/dev/null

accel=tcg
cpu=max
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
  accel=kvm
  cpu=host
fi

qemu-system-x86_64 \
  -machine "accel=$accel" \
  -cpu "$cpu" \
  -smp 2 \
  -m 3072 \
  -drive "file=$work/root.qcow2,if=virtio,format=qcow2" \
  -drive "file=$work/config.iso,media=cdrom,readonly=on" \
  -nic user,model=virtio-net-pci \
  -display none \
  -monitor none \
  -serial stdio \
  -no-reboot \
  >"$work/serial.log" 2>&1 &
pid=$!

for _ in $(seq 1 180); do
  if grep -Fq "$marker" "$work/serial.log"; then
    echo "qcow-config-drive-smoke=passed role=$role accelerator=$accel"
    exit 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "QEMU exited before the smoke marker appeared: $role" >&2
    tail -100 "$work/serial.log" >&2
    exit 1
  fi
  sleep 2
done

echo "timed out waiting for QCOW2 config-drive smoke marker: $role" >&2
tail -100 "$work/serial.log" >&2
exit 1
