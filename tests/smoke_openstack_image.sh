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
# shellcheck disable=SC2317,SC2329 # Invoked through the EXIT trap.
cleanup() {
  if [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  if [[ -n ${PLATFORM_QEMU_SERIAL_LOG:-} && -f $work/serial.log ]]; then
    cp -- "$work/serial.log" "$PLATFORM_QEMU_SERIAL_LOG"
  fi
  rm -rf "$work"
}
trap cleanup EXIT

marker="platform-${role}-qcow-smoke-passed"
instance_uuid=00000000-0000-4000-8000-000000000001
mkdir -p "$work/config/openstack/latest"
cat > "$work/config/openstack/latest/meta_data.json" <<EOF
{"uuid":"$instance_uuid","hostname":"${role}-qcow-smoke","name":"${role}-qcow-smoke"}
EOF
cat > "$work/config/openstack/latest/user_data" <<EOF
#cloud-config
final_message: "$marker"
EOF
genisoimage -quiet -rock -joliet -volid config-2 \
  -output "$work/config.iso" "$work/config"

# Admin and storage expect deployment-owned Cinder filesystems. Supplying
# empty, disposable volumes exercises those mounts rather than waiting for
# their 60-second missing-device timeout before cloud-init can start.
volume_args=()
volumes=()
case "$role" in
  admin) volumes=(adminState backup) ;;
  storage) volumes=(data) ;;
esac
if [[ ${#volumes[@]} -gt 0 ]]; then
  for command in python3 mkfs.xfs truncate; do
    command -v "$command" >/dev/null || {
      echo "required command is unavailable: $command" >&2
      exit 2
    }
  done
  repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
  platform_config=${PLATFORM_CONFIG:-$repository/config/platform.example.json}
  for volume in "${volumes[@]}"; do
    label=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["volumes"][sys.argv[2]]["label"])' "$platform_config" "$volume")
    disk="$work/$volume.raw"
    truncate -s 512M "$disk"
    mkfs.xfs -q -f -L "$label" "$disk"
    volume_args+=(-drive "file=$disk,if=virtio,format=raw")
  done
fi

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
  -uuid "$instance_uuid" \
  -drive "file=$work/root.qcow2,if=virtio,format=qcow2" \
  -drive "file=$work/config.iso,media=cdrom,readonly=on" \
  "${volume_args[@]}" \
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
