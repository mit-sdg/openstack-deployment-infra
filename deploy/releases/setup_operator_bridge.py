#!/usr/bin/env python3
"""Generate and validate the pinned operator SSH/provider bridge."""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid as uuid_module
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openstack_platform.contracts import (  # noqa: E402
    OPERATOR_ACCOUNT_NAME,
    OPERATOR_SSH_ALIAS,
)
from openstack_platform.installation import (  # noqa: E402
    DEFAULT_OPERATOR_INVENTORY,
    OPERATOR_BIN,
    OPERATOR_ROOT,
    OPERATOR_SSH,
)

SSH_ALIAS = OPERATOR_SSH_ALIAS
DEFAULT_PLATFORM_CONFIG = DEFAULT_OPERATOR_INVENTORY
DEFAULT_SSH_IDENTITY = OPERATOR_SSH / "id_ed25519"
DEFAULT_SSH_CONFIG = OPERATOR_SSH / "config"
DEFAULT_KNOWN_HOSTS = OPERATOR_SSH / "known_hosts"
DEFAULT_PROVIDER_COMMAND = OPERATOR_BIN / "platform-openstack"
_MAX_CONFIG_BYTES = 1_048_576
_MAX_COMMAND_OUTPUT = 65_536
_MAX_CONSOLE_OUTPUT = 2_097_152
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}=?")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_NIX_STORE = Path("/nix/store")


class BridgeError(RuntimeError):
    """A bridge preflight or generation failure with no provider payload."""


def _fail(message: str) -> NoReturn:
    raise BridgeError(message)


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("platform configuration contains duplicate keys")
        result[key] = value
    return result


def _safe_path(path: Path, *, label: str) -> Path:
    if (
        not path.is_absolute()
        or path != Path(os.path.normpath(path))
        or "\x00" in str(path)
        or any(character.isspace() for character in str(path))
    ):
        _fail(f"{label} must be a canonical absolute path")
    return path


def _private_file(
    path: Path, *, label: str, create: bool = False, maximum_bytes: int | None = None
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            _fail(f"{label} is unavailable")
        _fail(f"{label} is unavailable")
    except OSError:
        _fail(f"{label} is unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        _fail(f"{label} must be a direct current-user-owned mode-0600 file")
    if maximum_bytes is not None and metadata.st_size > maximum_bytes:
        _fail(f"{label} exceeds its size limit")
    return metadata


def _private_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    try:
        while True:
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                missing.append(current)
                parent = current.parent
                if parent == current:
                    _fail("SSH bridge directory has no existing safe parent")
                current = parent
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail("SSH bridge directory has a symlink or non-directory parent")
            break
        for candidate in reversed(missing):
            candidate.mkdir(mode=0o700)
        metadata = path.lstat()
    except OSError:
        _fail("SSH bridge directory is unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        _fail("SSH bridge directory must be a direct current-user-owned mode-0700 directory")


def _read_private_json(path: Path) -> dict[str, Any]:
    _private_file(path, label="platform configuration")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        _fail("platform configuration is unavailable")
    try:
        raw = b""
        while chunk := os.read(descriptor, min(65_536, _MAX_CONFIG_BYTES + 1 - len(raw))):
            raw += chunk
            if len(raw) > _MAX_CONFIG_BYTES:
                _fail("platform configuration exceeds its size limit")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw, object_pairs_hook=_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("platform configuration is malformed")
    if not isinstance(value, dict):
        _fail("platform configuration must be a JSON object")
    return value


def _canonical_uuid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} is malformed")
    try:
        return str(uuid_module.UUID(value))
    except (AttributeError, ValueError):
        _fail(f"{label} is malformed")


def _platform_identity(path: Path) -> tuple[str, str, str, str]:
    document = _read_private_json(path)
    project = document.get("project")
    project_id = _canonical_uuid(document.get("projectId"), label="configured project UUID")
    namespace = document.get("namespace")
    hosts = document.get("hosts")
    addresses = document.get("addresses")
    server_name = hosts.get("admin") if isinstance(hosts, Mapping) else None
    address = addresses.get("admin") if isinstance(addresses, Mapping) else None
    if (
        not isinstance(project, str)
        or not 1 <= len(project) <= 128
        or "\x00" in project
        or "\n" in project
        or "\r" in project
        or not isinstance(namespace, str)
        or not _HOST.fullmatch(namespace)
        or not isinstance(server_name, str)
        or not _HOST.fullmatch(server_name)
        or not isinstance(address, str)
    ):
        _fail("platform configuration has an invalid operator identity")
    try:
        ipaddress.ip_address(address)
    except ValueError:
        _fail("configured admin address is not an IP address")
    return project, project_id, server_name, address


def _verify_local_dependencies() -> None:
    for command in ("ssh", "ssh-keyscan", "ssh-keygen"):
        resolved_name = shutil.which(command)
        if resolved_name is None:
            _fail(f"operator bridge dependency is unavailable: {command}")
        resolved = Path(resolved_name).resolve(strict=True)
        try:
            metadata = resolved.lstat()
        except OSError:
            _fail(f"operator bridge dependency is unavailable: {command}")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not os.access(resolved, os.X_OK)
        ):
            _fail(f"operator bridge dependency is not a protected executable: {command}")


def _verify_executable(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        _fail(f"{label} is unavailable")
    if stat.S_ISLNK(metadata.st_mode):
        try:
            resolved = path.resolve(strict=True)
            target = resolved.lstat()
        except (OSError, RuntimeError):
            _fail(f"{label} is unavailable")
        if (
            not resolved.is_absolute()
            or not resolved.is_relative_to(_NIX_STORE)
            or not stat.S_ISREG(target.st_mode)
            or target.st_uid != 0
            or stat.S_IMODE(target.st_mode) & 0o022
            or not os.access(resolved, os.X_OK)
        ):
            _fail(f"{label} must resolve to a protected Nix-store executable")
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) not in {0o500, 0o700}
        or not os.access(path, os.X_OK)
    ):
        _fail(f"{label} must be a direct current-user-owned executable with mode 0500 or 0700")


def _command(
    argv: Sequence[str],
    *,
    stdin: bytes | None = None,
    timeout: float = 30,
    maximum_output: int = _MAX_COMMAND_OUTPUT,
) -> bytes:
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail("bridge dependency command was unavailable or timed out")
    if len(completed.stdout) > maximum_output:
        _fail("bridge dependency command output exceeded its size limit")
    if completed.returncode != 0:
        _fail("bridge dependency command failed")
    return completed.stdout


def _verify_provider_wrapper(wrapper: Path, *, project_id: str) -> None:
    _verify_executable(wrapper, label="protected OpenStack wrapper")
    token = _command((str(wrapper), "token", "issue", "-f", "value", "-c", "project_id"))
    try:
        token_id = token.decode("utf-8").strip()
    except UnicodeDecodeError:
        _fail("protected OpenStack wrapper returned malformed project identity")
    if _canonical_uuid(token_id, label="authenticated project UUID") != project_id:
        _fail("protected OpenStack wrapper is scoped to a different project")


def _console_fingerprint(output: bytes) -> str:
    try:
        text = output.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("admin console output was not UTF-8")
    candidates = [
        match.group(0)
        for line in text.splitlines()
        if "ed25519" in line.lower()
        for match in _FINGERPRINT.finditer(line)
    ]
    if not candidates:
        _fail("admin console did not expose an ED25519 host-key fingerprint")
    return candidates[-1]


def _scan_host_key(address: str) -> tuple[str, str]:
    output = _command(("ssh-keyscan", "-T", "10", "-t", "ed25519", address), timeout=20)
    lines = [line for line in output.decode("utf-8", errors="strict").splitlines() if line]
    accepted: list[str] = []
    for line in lines:
        if line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 3 or fields[0] != address or fields[1] != "ssh-ed25519":
            continue
        try:
            base64.b64decode(fields[2], validate=True)
        except (ValueError, binascii.Error):
            continue
        accepted.append(line)
    if len(accepted) != 1:
        _fail("ssh-keyscan did not return exactly one admin ED25519 host key")
    fingerprint_output = _command(("ssh-keygen", "-lf", "-"), stdin=(accepted[0] + "\n").encode())
    match = _FINGERPRINT.search(fingerprint_output.decode("utf-8", errors="strict"))
    if match is None:
        _fail("ssh-keygen did not return an admin host-key fingerprint")
    return accepted[0], match.group(0)


def _atomic_write(path: Path, value: bytes, *, mode: int) -> None:
    _private_directory(path.parent)
    if os.path.lexists(path):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            _fail("SSH bridge destination has an unexpected type, owner, or mode")
    temporary = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temporary):
        metadata = temporary.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            _fail("SSH bridge has an unsafe stale temporary")
        temporary.unlink()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        written = 0
        while written < len(value):
            written += os.write(descriptor, value[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError:
        _fail("could not atomically install the SSH bridge file")
    finally:
        if descriptor != -1:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _validate_ssh_config(path: Path, known_hosts: Path, address: str, identity: Path) -> None:
    output = _command(("ssh", "-F", str(path), "-G", SSH_ALIAS))
    values: dict[str, list[str]] = {}
    for line in output.decode("utf-8", errors="strict").splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2:
            values.setdefault(fields[0].lower(), []).append(fields[1])
    expected = {
        "hostname": address,
        "user": OPERATOR_ACCOUNT_NAME,
        "identitiesonly": "yes",
        "stricthostkeychecking": "true",
        "hostkeyalgorithms": "ssh-ed25519",
        "userknownhostsfile": str(known_hosts),
        "identityfile": str(identity),
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "forwardagent": "no",
        "pubkeyauthentication": "true",
        "preferredauthentications": "publickey",
        "hashknownhosts": "no",
        "checkhostip": "no",
        "canonicalizehostname": "false",
        "clearallforwardings": "yes",
        "permitlocalcommand": "no",
        "connecttimeout": "10",
        "connectionattempts": "1",
    }
    if any(
        expected_key not in values or expected_value not in values[expected_key]
        for expected_key, expected_value in expected.items()
    ):
        _fail("generated SSH configuration did not preserve its strict pinned settings")
    if _command(("ssh-keygen", "-F", address, "-f", str(known_hosts))):
        return
    _fail("generated known-hosts file does not contain the observed admin key")


def preflight(args: argparse.Namespace) -> None:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        _fail(f"run operator bridge setup as the unprivileged {OPERATOR_ROOT} owner")
    for path, label in (
        (args.ssh_identity, "SSH identity path"),
        (args.ssh_config, "SSH config path"),
        (args.known_hosts, "known-hosts path"),
        (args.provider_command, "provider wrapper path"),
    ):
        _safe_path(path, label=label)
    _private_directory(args.ssh_config.parent)
    _private_file(args.ssh_identity, label="SSH identity", maximum_bytes=65_536)
    _verify_executable(args.provider_command, label="protected OpenStack wrapper")
    _verify_local_dependencies()
    print("operator-bridge=prerequisites-ready")


def configure(args: argparse.Namespace) -> None:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        _fail(f"run operator bridge setup as the unprivileged {OPERATOR_ROOT} owner")
    for path, label in (
        (args.platform_config, "platform configuration path"),
        (args.ssh_identity, "SSH identity path"),
        (args.ssh_config, "SSH config path"),
        (args.known_hosts, "known-hosts path"),
        (args.provider_command, "provider wrapper path"),
    ):
        _safe_path(path, label=label)
    _verify_local_dependencies()
    _project_name, project_id, server_name, address = _platform_identity(args.platform_config)
    _private_file(args.ssh_identity, label="SSH identity", maximum_bytes=65_536)
    _verify_provider_wrapper(args.provider_command, project_id=project_id)
    trusted = _console_fingerprint(
        _command(
            (str(args.provider_command), "console", "log", "show", "--lines", "2000", server_name),
            timeout=45,
            maximum_output=_MAX_CONSOLE_OUTPUT,
        )
    )
    known_host, observed = _scan_host_key(address)
    if trusted != observed:
        _fail("admin host key does not match the console-observed fingerprint")

    known_hosts = known_host + "\n"
    _atomic_write(args.known_hosts, known_hosts.encode("utf-8"), mode=0o600)
    config = (
        f"Host {SSH_ALIAS}\n"
        f"    HostName {address}\n"
        f"    User {OPERATOR_ACCOUNT_NAME}\n"
        f"    IdentityFile {args.ssh_identity}\n"
        "    IdentitiesOnly yes\n"
        f"    UserKnownHostsFile {args.known_hosts}\n"
        "    StrictHostKeyChecking yes\n"
        "    HostKeyAlgorithms ssh-ed25519\n"
        "    HashKnownHosts no\n"
        "    CheckHostIP no\n"
        "    PasswordAuthentication no\n"
        "    KbdInteractiveAuthentication no\n"
        "    PubkeyAuthentication yes\n"
        "    PreferredAuthentications publickey\n"
        "    ConnectTimeout 10\n"
        "    ConnectionAttempts 1\n"
        "    CanonicalizeHostname no\n"
        "    ProxyCommand none\n"
        "    ForwardAgent no\n"
        "    ClearAllForwardings yes\n"
        "    PermitLocalCommand no\n"
    )
    _atomic_write(args.ssh_config, config.encode("utf-8"), mode=0o600)
    _validate_ssh_config(args.ssh_config, args.known_hosts, address, args.ssh_identity)
    print("operator-bridge=verified")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Generate and validate the pinned operator SSH/provider bridge"
    )
    result.add_argument("--platform-config", type=Path, default=DEFAULT_PLATFORM_CONFIG)
    result.add_argument("--ssh-identity", type=Path, default=DEFAULT_SSH_IDENTITY)
    result.add_argument("--ssh-config", type=Path, default=DEFAULT_SSH_CONFIG)
    result.add_argument("--known-hosts", type=Path, default=DEFAULT_KNOWN_HOSTS)
    result.add_argument("--provider-command", type=Path, default=DEFAULT_PROVIDER_COMMAND)
    result.add_argument(
        "--preflight",
        action="store_true",
        help="validate local bridge prerequisites without contacting OpenStack or SSH",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.preflight:
            preflight(args)
        else:
            configure(args)
        return 0
    except BridgeError as error:
        print(f"operator bridge setup failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("operator bridge setup failed unexpectedly", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
