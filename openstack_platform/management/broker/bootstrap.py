"""Operator-owned hash-only admin enrollment, readable through existing broker paths."""

from __future__ import annotations

import argparse
import hmac
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from ...validation import uuid as checked_uuid
from ..common import digest, opaque, strict_json
from ..config import Config, management_peer
from ..settings import write_document
from .local_security import token_hash

FORMAT = "openstack-platform-admin-enrollment-v1"


def enrollment_file(config: Config) -> Path:
    return config.state_directory.parent / "management-broker-releases/config/admin-enrollment.json"


def issue(config: Config, *, now: float | None = None) -> str:
    if not config.development and (
        os.geteuid() == 0 or os.geteuid() != management_peer("operator")[0]
    ):
        raise ValueError("admin enrollment must run as the unprivileged operator")
    path = enrollment_file(config)
    folder = path.parent
    metadata = folder.lstat()
    expected_gid = os.getegid() if config.development else management_peer("managementBroker")[1]
    if (
        folder.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != expected_gid
        or stat.S_IMODE(metadata.st_mode) != 0o2750
    ):
        raise ValueError(
            "admin enrollment requires the existing operator-owned broker setgid config directory"
        )
    timestamp = time.time() if now is None else now
    identifier, token = str(uuid.uuid4()), opaque()
    document = {
        "format": FORMAT,
        "id": identifier,
        "tokenHash": token_hash(token),
        "created": timestamp,
        "expires": timestamp + 86400,
        "realm": digest(config.portal_origin + "\n" + config.issuer),
    }
    from ..common import canonical

    write_document(folder, path, (canonical(document) + "\n").encode())
    return config.portal_origin + "/setup#" + identifier + "." + token


def read(config: Config, link: object, now: float, valid_after: float) -> dict[str, Any]:
    if not isinstance(link, str) or len(link) > 80 or link.count(".") != 1:
        raise ValueError("invalid enrollment token")
    identifier, token = link.split(".")
    checked_uuid(identifier)
    supplied = token_hash(token)
    path = enrollment_file(config)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        expected_uid = os.geteuid() if config.development else management_peer("operator")[0]
        expected_gid = (
            os.getegid() if config.development else management_peer("managementBroker")[1]
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o640
            or metadata.st_uid != expected_uid
            or metadata.st_gid != expected_gid
            or metadata.st_size > 4096
        ):
            raise ValueError("invalid enrollment file ownership, mode or bounds")
        body = strict_json(os.read(fd, 4097))
    finally:
        os.close(fd)
    if not isinstance(body, dict) or set(body) != {
        "format",
        "id",
        "tokenHash",
        "created",
        "expires",
        "realm",
    }:
        raise ValueError("invalid enrollment file")
    if (
        body["format"] != FORMAT
        or body["id"] != identifier
        or body["realm"] != digest(config.portal_origin + "\n" + config.issuer)
    ):
        raise ValueError("invalid enrollment binding")
    if (
        type(body["created"]) not in (float, int)
        or type(body["expires"]) not in (float, int)
        or not valid_after < body["created"] <= now
        or not now < body["expires"] <= body["created"] + 86400
    ):
        raise ValueError("expired enrollment token")
    if (
        not isinstance(body["tokenHash"], str)
        or len(body["tokenHash"]) != 64
        or not hmac.compare_digest(body["tokenHash"], supplied)
    ):
        raise ValueError("invalid enrollment token")
    return body


def run(config: Config, arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Issue a single-use 24-hour local admin setup URL")
    parser.parse_args(arguments)
    print(issue(config))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    run(Config.load(args.config), [])


if __name__ == "__main__":
    main()
