"""Verified, atomic host-key pinning for the fixed admin SSH transport."""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import hmac
import ipaddress
import os
import re
import shlex
import stat
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import remote, runtime

Runner = Callable[..., Any]
_MAXIMUM_OUTPUT = 1_048_576
_FINGERPRINT = re.compile(rb"SHA256:([A-Za-z0-9+/]{43})=?(?![A-Za-z0-9+/=])")


class HostKeyError(RuntimeError):
    """An operator-safe failure which never contains key material or fingerprints."""


def _run(
    argv: tuple[str, ...], *, timeout_seconds: float, command_runner: Runner
) -> runtime.CommandResult:
    try:
        result = command_runner(
            argv,
            timeout_seconds=timeout_seconds,
            stdin=None,
            stdout_limit=_MAXIMUM_OUTPUT,
            stderr_limit=65_536,
            inherit_env=("HOME", "USER"),
            check=True,
        )
    except Exception as error:
        raise HostKeyError("host-key verification command failed") from error
    if not isinstance(result, runtime.CommandResult):
        raise HostKeyError("host-key verification command returned a malformed result")
    if result.stdout_truncated or result.stderr_truncated:
        raise HostKeyError("host-key verification command exceeded its output limit")
    return result


def _resolved_pin(
    fixed_address: str,
    ssh_config_path: str | os.PathLike[str],
    *,
    timeout_seconds: float,
    command_runner: Runner,
) -> tuple[Path, str]:
    try:
        address = str(ipaddress.ip_address(fixed_address))
    except ValueError as error:
        raise HostKeyError("admin fixed address is malformed") from error
    config = os.fspath(ssh_config_path)
    if not config or "\x00" in config or not Path(config).is_absolute():
        raise HostKeyError("admin SSH config path is malformed")
    result = _run(
        ("ssh", "-G", "-F", config, remote.SSH_TARGET),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
    )
    settings: dict[str, str] = {}
    try:
        for raw_line in result.stdout.decode("utf-8", errors="strict").splitlines():
            key, separator, value = raw_line.partition(" ")
            if separator and key.lower() in {
                "hostname",
                "hostkeyalias",
                "port",
                "stricthostkeychecking",
                "userknownhostsfile",
            }:
                settings[key.lower()] = value.strip()
        hostname = str(ipaddress.ip_address(settings["hostname"]))
        known_hosts_values = shlex.split(settings["userknownhostsfile"])
    except (KeyError, UnicodeDecodeError, ValueError) as error:
        raise HostKeyError("resolved admin SSH configuration is malformed") from error
    if hostname != address or settings.get("port") != "22":
        raise HostKeyError("admin SSH configuration does not resolve to the fixed endpoint")
    if settings.get("stricthostkeychecking", "").lower() not in ("yes", "true"):
        raise HostKeyError("admin SSH configuration does not require strict host-key checking")
    if len(known_hosts_values) != 1:
        raise HostKeyError("admin SSH configuration must use exactly one pinned known-hosts file")
    known_hosts = Path(known_hosts_values[0])
    if not known_hosts.is_absolute() or "\x00" in os.fspath(known_hosts):
        raise HostKeyError("resolved admin known-hosts path is malformed")
    host_key_alias = settings.get("hostkeyalias")
    if host_key_alias:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", host_key_alias):
            raise HostKeyError("resolved admin host-key alias is malformed")
        lookup_name = host_key_alias
    else:
        lookup_name = address
    return known_hosts, lookup_name


def _decode_base64(value: bytes, *, expected_length: int | None = None) -> bytes:
    try:
        decoded = base64.b64decode(value + b"=" * (-len(value) % 4), validate=True)
    except (binascii.Error, ValueError) as error:
        raise HostKeyError("host-key verification data is malformed") from error
    if expected_length is not None and len(decoded) != expected_length:
        raise HostKeyError("host-key verification data is malformed")
    return decoded


def _trusted_digest(console_output: bytes) -> bytes:
    if len(console_output) > _MAXIMUM_OUTPUT:
        raise HostKeyError("Nova console output exceeded the host-key verification limit")
    digests: set[bytes] = set()
    for line in console_output.splitlines():
        if b"ED25519" not in line.upper():
            continue
        for encoded in _FINGERPRINT.findall(line):
            digests.add(_decode_base64(encoded, expected_length=32))
    if len(digests) != 1:
        raise HostKeyError("Nova console did not provide one trusted ED25519 fingerprint")
    return digests.pop()


def _scanned_key(scan_output: bytes, address: str) -> tuple[bytes, bytes]:
    records: set[tuple[bytes, bytes]] = set()
    expected_host = address.encode("ascii")
    for raw_line in scan_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(b"#"):
            continue
        fields = line.split()
        if len(fields) < 3 or fields[0] != expected_host or fields[1] != b"ssh-ed25519":
            raise HostKeyError("admin ED25519 key scan was malformed")
        blob = _decode_base64(fields[2])
        if (
            len(blob) != 51
            or blob[:4] != struct.pack(">I", 11)
            or blob[4:15] != b"ssh-ed25519"
            or blob[15:19] != struct.pack(">I", 32)
        ):
            raise HostKeyError("admin ED25519 key scan was malformed")
        records.add((fields[2], blob))
    if len(records) != 1:
        raise HostKeyError("admin ED25519 key scan did not return one key")
    return records.pop()


def _hashed_host_matches(token: bytes, lookup_name: bytes) -> bool:
    fields = token.split(b"|")
    if len(fields) != 4 or fields[:2] != [b"", b"1"]:
        return False
    try:
        salt = base64.b64decode(fields[2], validate=True)
        expected = base64.b64decode(fields[3], validate=True)
    except (binascii.Error, ValueError):
        raise HostKeyError("pinned known-hosts file contains a malformed hashed host") from None
    return hmac.compare_digest(hmac.new(salt, lookup_name, hashlib.sha1).digest(), expected)


def _plain_host_matches(pattern: bytes, lookup: bytes, bracketed: bytes) -> bool:
    try:
        value = pattern.decode("ascii").lower()
        candidates = (lookup.decode("ascii").lower(), bracketed.decode("ascii").lower())
    except UnicodeDecodeError as error:
        raise HostKeyError("pinned known-hosts file contains a malformed host pattern") from error
    return any(
        candidate == value or fnmatch.fnmatchcase(candidate, value) for candidate in candidates
    )


def _line_matches_lookup(line: bytes, lookup_name: str) -> bool:
    fields = line.split()
    if not fields:
        return False
    marked = fields[0].startswith(b"@")
    if len(fields) < (4 if marked else 3):
        raise HostKeyError("pinned known-hosts file contains a malformed record")
    host_field = fields[1] if marked else fields[0]
    lookup = lookup_name.encode("ascii")
    bracketed = b"[" + lookup + b"]:22"
    positive_match = False
    negated_match = False
    for token in host_field.split(b","):
        if token.startswith(b"|1|"):
            positive_match = positive_match or _hashed_host_matches(token, lookup)
            positive_match = positive_match or _hashed_host_matches(token, bracketed)
        elif token.startswith(b"!"):
            negated_match = negated_match or _plain_host_matches(token[1:], lookup, bracketed)
        else:
            positive_match = positive_match or _plain_host_matches(token, lookup, bracketed)
    matched = positive_match and not negated_match
    if matched and marked:
        raise HostKeyError("pinned known-hosts file uses a marker for the admin endpoint")
    return matched


def _updated_known_hosts(current: bytes, lookup_name: str, encoded_key: bytes) -> bytes:
    if len(current) > _MAXIMUM_OUTPUT:
        raise HostKeyError("pinned known-hosts file exceeds its size limit")
    kept: list[bytes] = []
    for line in current.splitlines(keepends=True):
        content = line.rstrip(b"\r\n")
        if (
            not content
            or content.lstrip().startswith(b"#")
            or not _line_matches_lookup(content, lookup_name)
        ):
            kept.append(line)
    if kept and not kept[-1].endswith((b"\n", b"\r")):
        kept[-1] += b"\n"
    record = lookup_name.encode("ascii") + b" ssh-ed25519 " + encoded_key + b"\n"
    updated = b"".join(kept) + record
    if len(updated) > _MAXIMUM_OUTPUT:
        raise HostKeyError("updated pinned known-hosts file exceeds its size limit")
    return updated


def _read_private_file(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise HostKeyError(
                "pinned known-hosts directory must be private and current-user-owned"
            )
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    except HostKeyError:
        raise
    except OSError as error:
        raise HostKeyError("could not safely open the pinned known-hosts file") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAXIMUM_OUTPUT
        ):
            raise HostKeyError("pinned known-hosts file must be a private regular file")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                raise HostKeyError("pinned known-hosts file changed while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), metadata
    finally:
        os.close(descriptor)


def _same_file(path: Path, expected: os.stat_result) -> bool:
    try:
        observed = path.lstat()
    except OSError:
        return False
    return (
        observed.st_dev == expected.st_dev
        and observed.st_ino == expected.st_ino
        and observed.st_uid == expected.st_uid
        and observed.st_mode == expected.st_mode
        and observed.st_size == expected.st_size
        and observed.st_mtime_ns == expected.st_mtime_ns
    )


def _atomic_replace(path: Path, payload: bytes, expected: os.stat_result) -> None:
    temporary_fd = -1
    temporary_name: str | None = None
    directory_fd = -1
    try:
        temporary_name = str(path.parent / ".known-hosts.tmp")
        if os.path.lexists(temporary_name):
            stale = Path(temporary_name).lstat()
            if (
                not stat.S_ISREG(stale.st_mode)
                or stale.st_uid != os.geteuid()
                or stat.S_IMODE(stale.st_mode) != 0o600
            ):
                raise HostKeyError("known-hosts directory contains an unsafe stale temporary")
            os.unlink(temporary_name)
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(temporary_fd, 0o600)
        written = 0
        while written < len(payload):
            written += os.write(temporary_fd, payload[written:])
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        if not _same_file(path, expected):
            raise HostKeyError("pinned known-hosts file changed before atomic replacement")
        os.replace(temporary_name, path)
        temporary_name = None
        directory_fd = os.open(
            path.parent, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
        )
        os.fsync(directory_fd)
    except HostKeyError:
        raise
    except OSError as error:
        raise HostKeyError("could not atomically replace the pinned known-hosts file") from error
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def pin_verified_admin_host_key(
    fixed_address: str,
    console_output: bytes,
    *,
    ssh_config_path: str | os.PathLike[str] = remote.DEFAULT_SSH_CONFIG,
    timeout_seconds: float = 15,
    command_runner: Runner = runtime.run,
) -> None:
    """Verify the scanned key against Nova console evidence, then atomically pin it.

    Key material and fingerprints stay inside this boundary and are never
    returned or included in operator-facing errors.
    """
    if timeout_seconds <= 0:
        raise HostKeyError("host-key verification deadline is malformed")
    known_hosts, lookup_name = _resolved_pin(
        fixed_address,
        ssh_config_path,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
    )
    trusted = _trusted_digest(console_output)
    result = _run(
        (
            "ssh-keyscan",
            "-T",
            str(max(1, min(15, int(timeout_seconds)))),
            "-t",
            "ed25519",
            fixed_address,
        ),
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
    )
    encoded_key, key_blob = _scanned_key(result.stdout, fixed_address)
    if not hmac.compare_digest(trusted, hashlib.sha256(key_blob).digest()):
        raise HostKeyError("admin host key did not match authenticated Nova console evidence")
    current, metadata = _read_private_file(known_hosts)
    updated = _updated_known_hosts(current, lookup_name, encoded_key)
    confirmed_pin = _resolved_pin(
        fixed_address,
        ssh_config_path,
        timeout_seconds=timeout_seconds,
        command_runner=command_runner,
    )
    if confirmed_pin != (known_hosts, lookup_name):
        raise HostKeyError("admin SSH pin target changed during host-key verification")
    _atomic_replace(known_hosts, updated, metadata)
