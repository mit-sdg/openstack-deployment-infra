#!/usr/bin/env bash
# Install the pinned release tooling below /srv/openstack-platform without sudo.
set -euo pipefail

if [[ $(id -u) -eq 0 || -n ${SUDO_USER:-} ]]; then
  echo "run management runtime bootstrap as the unprivileged /srv/openstack-platform owner" >&2
  exit 77
fi

root=${PLATFORM_RUNTIME_ROOT:-/srv/openstack-platform/runtime}
bin_root=${PLATFORM_BIN_ROOT:-/srv/openstack-platform/bin}
ssh_directory=${PLATFORM_SSH_DIRECTORY:-/srv/openstack-platform/.secrets/ssh}
ssh_identity=${PLATFORM_SSH_IDENTITY:-$ssh_directory/id_ed25519}
openstack_wrapper=${PLATFORM_OPENSTACK_WRAPPER:-$bin_root/platform-openstack}
bridge_script=${PLATFORM_BRIDGE_SCRIPT:-$PWD/deploy/platform-cli/setup_management_bridge.py}
uv_version=0.12.2
python_version=3.14.7
uv_sha256=d66e96b5f1ca3b99806eee283a8125d33a0bd669e6e6d9bc4ab7ffda63c41bf4
uv_url="https://github.com/astral-sh/uv/releases/download/${uv_version}/uv-x86_64-unknown-linux-gnu.tar.gz"

install -d -m 0750 "$root" "$root/uv" "$root/python" "$bin_root"
temporary=$(mktemp -d "$root/.bootstrap.XXXXXX")
trap 'rm -rf "$temporary"' EXIT

curl --fail --location --silent --show-error "$uv_url" -o "$temporary/uv.tar.gz"
printf '%s  %s\n' "$uv_sha256" "$temporary/uv.tar.gz" | sha256sum --check --status
install -d -m 0750 "$root/uv/$uv_version"
tar -xzf "$temporary/uv.tar.gz" --strip-components=1 -C "$root/uv/$uv_version"
test -x "$root/uv/$uv_version/uv"

"$root/uv/$uv_version/uv" python install \
  --no-bin \
  --install-dir "$root/python" \
  "$python_version"
python_path="$root/python/cpython-${python_version}-linux-x86_64-gnu/bin/python3.14"
test -x "$python_path"

action_link() {
  local target=$1
  local link=$2
  local pending="${link}.tmp.$$"
  rm -f "$pending"
  ln -s "$target" "$pending"
  mv -Tf "$pending" "$link"
}
action_link "$root/uv/$uv_version/uv" "$bin_root/uv"
action_link "$python_path" "$root/python3.14"

verify_executable() {
  local path=$1
  [[ $path = /* && -f $path && -x $path ]] || return 1
  local resolved owner mode
  resolved=$(readlink -f -- "$path") || return 1
  [[ -f $resolved && -x $resolved ]] || return 1
  read -r owner mode < <(stat -c '%u %a' -- "$resolved")
  [[ $owner == 0 || $owner == $(id -u) ]] || return 1
  (( (0$mode & 0022) == 0 )) || return 1
}

find_openstack_cli() {
  if [[ -e $openstack_wrapper || -L $openstack_wrapper ]]; then
    verify_executable "$openstack_wrapper" || return 1
    printf '%s\n' "$openstack_wrapper"
    return 0
  fi
  local candidate=${PLATFORM_OPENSTACK_CLI:-${PLATFORM_OPENSTACK_COMMAND:-}}
  if [[ -z $candidate ]]; then
    candidate=$(command -v openstack || true)
  fi
  if [[ -z $candidate && -n $nix_command && -f flake.nix ]]; then
    local python_store
    python_store=$($nix_command build --no-link --print-out-paths "$PWD#python" 2>/dev/null) || true
    if [[ -n $python_store ]]; then
      candidate="$python_store/bin/openstack"
    fi
  fi
  [[ -n $candidate && $candidate = /* && -x $candidate ]] || return 1
  verify_executable "$candidate" || return 1
  printf '%s\n' "$candidate"
}

install -d -m 0700 "$ssh_directory"
for dependency in ssh ssh-keyscan ssh-keygen; do
  command -v "$dependency" >/dev/null 2>&1 || {
    echo "management bridge dependency is unavailable: $dependency" >&2
    exit 78
  }
done

if [[ -e $ssh_identity || -L $ssh_identity ]]; then
  identity_metadata=$(stat -c '%u %a' -- "$ssh_identity" 2>/dev/null || true)
  [[ -f $ssh_identity && ! -L $ssh_identity ]] || {
    echo "management SSH identity must be a direct private file" >&2
    exit 78
  }
  read -r identity_owner identity_mode <<<"$identity_metadata"
  [[ $identity_owner == $(id -u) && $identity_mode == 600 ]] || {
    echo "management SSH identity ownership or mode is invalid" >&2
    exit 78
  }
else
  umask 077
  ssh-keygen -q -t ed25519 -N '' -f "$ssh_identity" >/dev/null 2>&1 || {
    echo "could not create the management SSH identity" >&2
    exit 78
  }
  chmod 0600 "$ssh_identity"
fi

age_command=${PLATFORM_AGE_COMMAND:-}
if [[ -n $age_command ]]; then
  [[ $age_command = /* ]] || {
    echo "PLATFORM_AGE_COMMAND must be an absolute path" >&2
    exit 2
  }
else
  age_command=$(command -v age || true)
fi
nix_command=$(command -v nix || true)
if [[ -z $age_command && -n $nix_command && -f flake.nix ]]; then
  age_store_path=$($nix_command build --no-link --print-out-paths "$PWD#age")
  age_command="$age_store_path/bin/age"
fi
[[ -n $age_command && -x $age_command ]] || {
  echo "age is unavailable; install a system-managed age package or build .#age" >&2
  exit 78
}
age_command=$(readlink -f -- "$age_command")
[[ -x $age_command && -f $age_command ]] || {
  echo "age must resolve to an executable regular file" >&2
  exit 78
}
read -r age_owner age_mode < <(stat -c '%u %a' -- "$age_command")
[[ $age_owner == 0 || $age_owner == $(id -u) ]] || {
  echo "age must be root-owned or owned by the management user" >&2
  exit 78
}
(( 0$age_mode & 0022 )) && {
  echo "age executable must not be group- or world-writable" >&2
  exit 78
}
"$age_command" --version >/dev/null
action_link "$age_command" "$bin_root/age"

openstack_cli=$(find_openstack_cli) || {
  echo "OpenStack CLI is unavailable; install python-openstackclient or provide PLATFORM_OPENSTACK_CLI" >&2
  exit 78
}
if [[ -e $openstack_wrapper || -L $openstack_wrapper ]]; then
  verify_executable "$openstack_wrapper" || {
    echo "protected OpenStack wrapper is unavailable or unsafe" >&2
    exit 78
  }
else
  temporary_wrapper="$bin_root/.platform-openstack.$$.tmp"
  umask 077
  printf '#!/bin/sh\nset -eu\nexec %q "$@"\n' "$openstack_cli" >"$temporary_wrapper"
  chmod 0700 "$temporary_wrapper"
  mv -T -- "$temporary_wrapper" "$openstack_wrapper"
fi
"$openstack_wrapper" --version >/dev/null 2>&1 || {
  echo "protected OpenStack wrapper is unavailable" >&2
  exit 78
}
if [[ -f $bridge_script ]]; then
  "$root/python3.14" "$bridge_script" \
    --preflight \
    --ssh-identity "$ssh_identity" \
    --ssh-config "$ssh_directory/config" \
    --known-hosts "$ssh_directory/known_hosts" \
    --provider-command "$openstack_wrapper" >/dev/null
fi

test "$("$root/python3.14" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')" = "$python_version"
test "$("$bin_root/uv" --version)" = "uv $uv_version (x86_64-unknown-linux-gnu)"
printf 'management-runtime=python-%s uv-%s\n' "$python_version" "$uv_version"
