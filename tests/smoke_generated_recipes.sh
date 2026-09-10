#!/usr/bin/env bash
# Build and run the real generated recipes with an unprivileged OCI runtime.
set -euo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root"

[[ $(podman info --format '{{.Host.Security.Rootless}}') == true ]] || {
  echo "generated recipe smoke tests require rootless Podman" >&2
  exit 1
}

containers=()
recipe_root="$root/.generated-recipes"
cleanup() {
  for container in "${containers[@]}"; do
    podman rm --force "$container" >/dev/null 2>&1 || true
  done
  rm -rf "$recipe_root"
}
trap cleanup EXIT

resolve_pin() {
  local tagged_image=$1 repository digest
  timeout 180 podman pull --quiet "$tagged_image" >/dev/null
  repository=${tagged_image%:*}
  digest=$(podman image inspect --format '{{.Digest}}' "$tagged_image")
  [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "Podman did not resolve a registry digest for $tagged_image" >&2
    exit 1
  }
  printf '%s@%s\n' "$repository" "$digest"
}

export BUN_RUNTIME_IMAGE
export NODE_RUNTIME_IMAGE
BUN_RUNTIME_IMAGE=$(resolve_pin docker.io/oven/bun:1-alpine)
NODE_RUNTIME_IMAGE=$(resolve_pin docker.io/library/node:22-alpine)

rm -rf "$recipe_root"
mkdir -m 0700 "$recipe_root"
uv run --no-sync python - <<'PY'
import os
import shutil
from dataclasses import replace
from pathlib import Path

from openstack_platform.config import RuntimeImages
from openstack_platform.controller.application_runtime import Manifest, generate_recipe

fixtures = Path("tests/fixtures/apps")
output = Path(".generated-recipes")
images = RuntimeImages(
    bun=os.environ["BUN_RUNTIME_IMAGE"],
    node=os.environ["NODE_RUNTIME_IMAGE"],
)
manifests = {
    "bun": Manifest("bun", (".",), "build", "start", 3000, "/health"),
    "node": Manifest("node", (".",), None, "serve", 8080, "/ready"),
}
for runtime, manifest in manifests.items():
    for slim in (False, True):
        case = runtime + ("-slim" if slim else "")
        selected = (
            replace(manifest, runtime_files=(
                "package.json",
                "dist" if runtime == "bun" else "server.js",
                "nested/assets",
                "nested/config/message.txt",
            ))
            if slim else manifest
        )
        recipe = generate_recipe(selected, images)
        destination = output / case
        destination.mkdir(mode=0o700)
        source = destination / "source"
        shutil.copytree(fixtures / runtime, source)
        (source / "build-only.txt").write_text("must not ship in slim images")
        for nested in ("nested/assets", "nested/config"):
            (source / nested).mkdir(parents=True)
            (source / nested / "message.txt").write_text("included")
        (source / "nested/omitted.txt").write_text("must not ship in slim images")
        (destination / "Dockerfile").write_bytes(recipe.dockerfile)
        print(f"generated-recipe case={case} recipe-sha256={recipe.sha256}")
PY

for recipe_case in bun node bun-slim node-slim; do
  runtime=${recipe_case%%-*}
  image="localhost/generated-recipe-$recipe_case:ci"
  image_environment=""
  timeout 240 podman build \
    --pull=never \
    --file "$recipe_root/$recipe_case/Dockerfile" \
    --tag "$image" \
    "$recipe_root/$recipe_case/source"
  image_environment=$(podman image inspect \
    --format '{{range .Config.Env}}{{println .}}{{end}}' "$image")
  if ! grep --quiet --line-regexp 'NODE_ENV=production' <<<"$image_environment"; then
    echo "generated $runtime image did not fix NODE_ENV=production" >&2
    exit 1
  fi

  if [[ $recipe_case == *-slim ]]; then
    check_container="generated-recipe-check-$recipe_case-$$"
    containers+=("$check_container")
    timeout 30 podman run --rm --name "$check_container" --entrypoint /bin/sh "$image" -ec '
      test "$(cat /app/nested/assets/message.txt)" = included
      test "$(cat /app/nested/config/message.txt)" = included
      test ! -e /app/build-only.txt
      test ! -e /app/nested/omitted.txt
      test ! -e /app/node_modules
      test ! -e /app/bun.lock
      test ! -e /app/package-lock.json
      test ! -e /app/server.ts
    '
  fi

  case $runtime in
    bun)
      port=3000
      health_path=/health
      expected=healthy
      ;;
    node)
      port=8080
      health_path=/ready
      expected=ready
      ;;
  esac

  container=$(podman run --detach --publish "127.0.0.1::$port" "$image")
  containers+=("$container")
  endpoint=$(podman port "$container" "$port/tcp")
  url="http://$endpoint$health_path"
  body=""
  for _attempt in {1..30}; do
    if body=$(curl --fail --silent --show-error --max-time 2 "$url"); then
      break
    fi
    sleep 1
  done
  if [[ $body != "$expected" ]]; then
    echo "generated $runtime recipe did not become healthy" >&2
    podman logs "$container" >&2 || true
    exit 1
  fi
  podman container exists "$container"
  printf 'generated-recipe case=%s build=passed environment=production start=passed health=passed url=%s\n' \
    "$recipe_case" "$url"
done
