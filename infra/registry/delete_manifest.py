#!/usr/bin/env python3
"""Delete one controller-owned registry manifest so offline GC can reclaim it."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.platform_config import load  # noqa: E402
from lib.tls import internal_ca_context  # noqa: E402

CONFIG = load()
HOST = f"{CONFIG['addresses']['storage']}:5000"
ROOT = Path(CONFIG["paths"]["root"])
CA = str(ROOT / "secrets/nomad-cli/internal-ca.pem")
SECRETS = ROOT / "secrets/storage-bootstrap.env"


def env() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in SECRETS.read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("delete",))
    parser.add_argument("repository", help="controller repository under projects/")
    parser.add_argument("digest", help="sha256 manifest digest")
    arguments = sys.argv[1:]
    if len(arguments) == 2:
        arguments.insert(0, "delete")
    args = parser.parse_args(arguments)
    if not re.fullmatch(
        r"projects/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*", args.repository
    ):
        parser.error("repository must be a valid lowercase name under projects/")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.digest):
        parser.error("digest must be a sha256 OCI digest")
    password = env()["REGISTRY_BUILDER_PASSWORD"]
    basic = base64.b64encode(f"builder:{password}".encode()).decode()
    repository = urllib.parse.quote(args.repository, safe="/")
    digest = urllib.parse.quote(args.digest, safe=":")
    request = urllib.request.Request(
        f"https://{HOST}/v2/{repository}/manifests/{digest}",
        method="DELETE",
        headers={"Authorization": f"Basic {basic}"},
    )
    context = internal_ca_context(CA)
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            if response.status != 202:
                raise RuntimeError(f"registry returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            exc.read(65_536)
            raise RuntimeError(f"registry deletion failed with HTTP {exc.code}") from None
    verify = urllib.request.Request(
        f"https://{HOST}/v2/{repository}/manifests/{digest}",
        method="HEAD",
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json",
        },
    )
    try:
        with urllib.request.urlopen(verify, context=context, timeout=15) as response:
            raise RuntimeError(f"registry manifest remained with HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            exc.read(65_536)
            raise RuntimeError(f"registry absence check failed with HTTP {exc.code}") from None
    print(
        json.dumps(
            {"repository": args.repository, "digest": args.digest, "absent": True},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
