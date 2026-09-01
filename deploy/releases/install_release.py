#!/usr/bin/env python3
"""Install one immutable operator or helper release without privileges."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, NoReturn, cast

_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PAX_COMMIT = re.compile(rb"([1-9][0-9]*) comment=([0-9a-f]{40})\n")
_NIX_STORE = Path("/nix/store")
_HELPER_CONFIG_ROOT = Path("/etc")
_ROOT_UID = 0
_JSON_MAX_BYTES = 1_048_576


@dataclass(frozen=True)
class _Layout:
    release_root: Path
    bin_root: Path
    state_root: Path | None


def _operator_defaults() -> dict[str, _Layout]:
    contract_path = Path(__file__).resolve().parents[2] / "infra/lib/platform_contract.json"
    if not contract_path.is_file():
        return {}
    try:
        document = json.loads(contract_path.read_text(encoding="utf-8"))
        root = Path(document["installation"]["operatorRoot"])
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("platform installation contract is malformed") from error
    if not root.is_absolute():
        raise RuntimeError("platform operator root must be absolute")
    return {
        "operator": _Layout(
            release_root=root / "operator-releases",
            bin_root=root / "bin",
            state_root=root / "state",
        )
    }


_DEFAULTS = _operator_defaults()


class InstallFailure(RuntimeError):
    """The candidate could not be installed without weakening a release check."""


def _fail(message: str) -> NoReturn:
    raise InstallFailure(message)


def _run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    env: dict[str, str] | None = None,
    stdin: int | IO[Any] | None = None,
) -> None:
    try:
        subprocess.run(
            [os.fspath(argument) for argument in argv],
            check=True,
            env=env,
            stdin=stdin,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        rendered = shlex.join(os.fspath(argument) for argument in argv[:2])
        raise InstallFailure(f"release command failed: {rendered}") from error


def _ensure_directory(path: Path, mode: int) -> None:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail(f"release path must be a direct directory: {path}")
    if metadata.st_uid != os.geteuid():
        _fail(f"release path must be owned by the current user: {path}")
    path.chmod(mode)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_symlink(target: Path, link: Path) -> None:
    _ensure_directory(link.parent, 0o750)
    if os.path.lexists(link):
        metadata = link.lstat()
        if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _fail("release selector must be a current-user-owned direct symlink")
    temporary = link.parent / f".{link.name}.tmp"
    if os.path.lexists(temporary):
        metadata = temporary.lstat()
        if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _fail("release selector has an unsafe stale temporary")
        temporary.unlink()
        _fsync_directory(link.parent)
    temporary.symlink_to(target)
    os.replace(temporary, link)
    _fsync_directory(link.parent)


def _verify_archive_file(archive: Path) -> None:
    try:
        metadata = archive.lstat()
    except OSError as error:
        raise InstallFailure("release artifact is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        _fail("release artifact must be a direct file owned by the current user")


def _tar_octal(field: bytes) -> int:
    value = field.rstrip(b"\0 ")
    if not value or not re.fullmatch(rb"[0-7]+", value):
        _fail("release artifact has a malformed global pax header")
    return int(value, 8)


def _archive_commit(archive: Path) -> str:
    """Read Git's canonical commit comment without requiring a Git executable."""
    try:
        with archive.open("rb") as input_stream:
            header = input_stream.read(512)
            if len(header) != 512:
                _fail("release artifact is not a commit-addressed git archive")
            stored_checksum = _tar_octal(header[148:156])
            calculated_checksum = sum(header[:148]) + (8 * ord(" ")) + sum(header[156:])
            name = header[:100].rstrip(b"\0")
            if (
                name != b"pax_global_header"
                or header[156:157] != tarfile.XGLTYPE
                or header[257:263] != b"ustar\0"
                or header[263:265] != b"00"
                or stored_checksum != calculated_checksum
            ):
                _fail("release artifact is not a commit-addressed git archive")
            size = _tar_octal(header[124:136])
            if size != 52:
                _fail("release artifact has no canonical full source commit")
            payload = input_stream.read(size)
    except OSError as error:
        raise InstallFailure("release artifact is unavailable") from error

    match = _PAX_COMMIT.fullmatch(payload)
    if match is None or int(match.group(1)) != len(payload):
        _fail("release artifact has no canonical full source commit")
    return match.group(2).decode("ascii")


def _archive_sha256(archive: Path) -> str:
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as input_stream:
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise InstallFailure("release artifact is unavailable") from error
    return digest.hexdigest()


def _verify_archive_identity(archive: Path, *, expected_commit: str, expected_sha256: str) -> None:
    _verify_archive_file(archive)
    if not _COMMIT.fullmatch(expected_commit):
        _fail("expected commit must be a full lowercase source commit")
    if not _SHA256.fullmatch(expected_sha256):
        _fail("archive SHA-256 must be a full lowercase digest")
    if _archive_sha256(archive) != expected_sha256:
        _fail("release artifact SHA-256 does not match the trusted checksum")
    if _archive_commit(archive) != expected_commit:
        _fail("release artifact commit does not match --commit")


def _extract_archive(archive: Path, destination: Path) -> None:
    _verify_archive_file(archive)
    with tarfile.open(archive, mode="r:") as bundle:
        members = bundle.getmembers()
        if not members:
            _fail("release artifact is empty")
        if any(member.issym() or member.islnk() for member in members):
            _fail("release artifact must not contain links")
        bundle.extractall(destination, filter="data")


def _check_runtime(python: Path) -> Path:
    resolved = python.resolve(strict=True)
    result = subprocess.run(
        [resolved, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "3.14":
        _fail("release runtime must be Python 3.14")
    return resolved


def _sync_operator_environment(*, source: Path, release: Path, python: Path, uv: Path) -> Path:
    if not (source / "pyproject.toml").is_file() or not (source / "uv.lock").is_file():
        _fail("operator release is missing pyproject.toml or uv.lock")
    environment = release / ".venv"
    sync_environment = os.environ.copy()
    sync_environment["UV_PROJECT_ENVIRONMENT"] = str(environment)
    sync_environment["UV_PYTHON_DOWNLOADS"] = "never"
    _run(
        (
            uv,
            "sync",
            "--frozen",
            "--no-dev",
            "--no-install-project",
            "--project",
            source,
            "--python",
            python,
        ),
        env=sync_environment,
    )
    return _check_runtime(environment / "bin/python")


def _launcher(
    mode: str,
    python: Path,
    *,
    platform_config: Path | None = None,
    config_root: Path | None = None,
    state_root: Path | None = None,
    openstack_command: Path | None = None,
) -> str:
    module = (
        "openstack_platform.operator" if mode == "operator" else "openstack_platform.helper.main"
    )
    executable = '"$release/.venv/bin/python"' if mode == "operator" else shlex.quote(str(python))
    launcher_environment = ""
    arguments = '"$@"'
    if mode == "operator":
        assert config_root is not None and state_root is not None and openstack_command is not None
        platform = config_root / "platform.json"
        policy = state_root / "policy.json"
        launcher_environment = f"""
verify_private_directory() {{
  path=$1
  test -d "$path" && test ! -L "$path" || {{ echo "operator configuration is unavailable" >&2; exit 78; }}
  test "$(stat -c %u -- "$path")" = "$(id -u)" && test "$(stat -c %a -- "$path")" = 700 || {{
    echo "operator configuration ownership or mode is invalid" >&2
    exit 78
  }}
}}
verify_private_file() {{
  path=$1
  test -f "$path" && test ! -L "$path" || {{ echo "operator configuration is unavailable" >&2; exit 78; }}
  test "$(stat -c %u -- "$path")" = "$(id -u)" && test "$(stat -c %a -- "$path")" = 600 || {{
    echo "operator configuration ownership or mode is invalid" >&2
    exit 78
  }}
}}
config_directory={shlex.quote(str(config_root))}
platform_config={shlex.quote(str(platform))}
state_directory={shlex.quote(str(state_root))}
policy={shlex.quote(str(policy))}
openstack_command={shlex.quote(str(openstack_command))}
verify_private_directory "$config_directory"
verify_private_file "$platform_config"
verify_private_file "$policy"
test -f "$openstack_command" && test ! -L "$openstack_command" && test -x "$openstack_command" || {{
  echo "protected OpenStack command is unavailable" >&2
  exit 78
}}
test "$(stat -c %u -- "$openstack_command")" = "$(id -u)" || {{
  echo "protected OpenStack command ownership is invalid" >&2
  exit 78
}}
case "$(stat -c %a -- "$openstack_command")" in
  500|700) ;;
  *) echo "protected OpenStack command mode is invalid" >&2; exit 78 ;;
esac
export PLATFORM_OPENSTACK_COMMAND="$openstack_command"
export PLATFORM_CONFIG="$platform_config"
# The release owns these three paths. Accept the exact paths used by packaged
# systemd units, but reject every attempt to substitute another value.
previous_fixed_option=
for argument in "$@"; do
  if test -n "$previous_fixed_option"; then
    if {{
      test "$previous_fixed_option" = --platform-config && test "$argument" = "$platform_config"
    }} || {{
      test "$previous_fixed_option" = --state-directory && test "$argument" = "$state_directory"
    }} || {{
      test "$previous_fixed_option" = --policy && test "$argument" = "$policy"
    }}; then
      previous_fixed_option=
      continue
    fi
    echo "operator launcher refuses a fixed-path override" >&2
    exit 78
  fi
  case "$argument" in
    --platform-config|--state-directory|--policy)
      previous_fixed_option=$argument
      ;;
    --platform-config=*|--state-directory=*|--policy=*)
      option=${{argument%%=*}}
      value=${{argument#*=}}
      if {{
        test "$option" = --platform-config && test "$value" = "$platform_config"
      }} || {{
        test "$option" = --state-directory && test "$value" = "$state_directory"
      }} || {{
        test "$option" = --policy && test "$value" = "$policy"
      }}; then
        :
      else
        echo "operator launcher refuses a fixed-path override" >&2
        exit 78
      fi
      ;;
  esac
done
if test -n "$previous_fixed_option"; then
  echo "operator launcher refuses an incomplete fixed-path option" >&2
  exit 78
fi
"""
        arguments = '"--platform-config" "$platform_config" "--state-directory" "$state_directory" "--policy" "$policy" "$@"'
    else:
        assert platform_config is not None
        launcher_environment = f"""
platform_config={shlex.quote(str(platform_config))}
if test -L "$platform_config"; then
  resolved_platform_config=$(readlink -f -- "$platform_config") || {{
    echo "live helper platform configuration is unavailable" >&2
    exit 78
  }}
  case "$resolved_platform_config" in
    /nix/store/*) ;;
    *) echo "live helper platform configuration has an invalid symlink target" >&2; exit 78 ;;
  esac
  test -f "$resolved_platform_config" && test ! -L "$resolved_platform_config" && test -r "$resolved_platform_config" || {{
    echo "live helper platform configuration is unavailable" >&2
    exit 78
  }}
  test "$(stat -c %u -- "$resolved_platform_config")" = 0 || {{
    echo "live helper platform configuration target ownership is invalid" >&2
    exit 78
  }}
  target_mode=$(stat -c %a -- "$resolved_platform_config")
  test $((0$target_mode & 022)) -eq 0 || {{
    echo "live helper platform configuration target mode is invalid" >&2
    exit 78
  }}
  platform_config=$resolved_platform_config
else
  test -f "$platform_config" && test -r "$platform_config" || {{
    echo "live helper platform configuration is unavailable" >&2
    exit 78
  }}
fi
export PLATFORM_CONFIG="$platform_config"
"""
    return f"""#!/bin/sh
set -eu
launcher=$(readlink -f -- "$0")
release=$(dirname "$(dirname "$launcher")")
test -f "$release/.complete" || test -f "$release/.candidate" || {{ echo "no accepted {mode} release" >&2; exit 69; }}
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$release/source"
{launcher_environment}exec {executable} -P -m {module} {arguments}
"""


def _config_launcher(config_root: Path, state_root: Path) -> str:
    return f"""#!/bin/sh
set -eu
launcher=$(readlink -f -- "$0")
release=$(dirname "$(dirname "$launcher")")
test -f "$release/.complete" || {{ echo "no accepted operator release" >&2; exit 69; }}
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH
for argument in "$@"; do
  case "$argument" in
    --config-root|--config-root=*|--state-root|--state-root=*)
      echo "operator configuration launcher refuses a fixed-path override" >&2
      exit 78
      ;;
  esac
done
exec "$release/.venv/bin/python" -P "$release/source/deploy/releases/install_operator_config.py" "$@" --config-root {shlex.quote(str(config_root))} --state-root {shlex.quote(str(state_root))}
"""


def _restore_launcher(state_root: Path, platform_config: Path) -> str:
    destination = state_root / "platform.sqlite3"
    return f"""#!/bin/sh
set -eu
launcher=$(readlink -f -- "$0")
release=$(dirname "$(dirname "$launcher")")
test -f "$release/.complete" || test -f "$release/.candidate" || {{ echo "no accepted operator release" >&2; exit 69; }}
state_directory={shlex.quote(str(state_root))}
platform_config={shlex.quote(str(platform_config))}
test -f "$platform_config" && test ! -L "$platform_config" || {{ echo "operator platform configuration is unavailable" >&2; exit 78; }}
# A drill may select only an explicitly absent database below a private
# replacement directory. Ordinary restore retains the fixed live destination.
destination={shlex.quote(str(destination))}
if test "${{1:-}}" = --replacement-state-directory; then
  test "$#" -ge 3 || {{ echo "operator replacement restore directory is missing" >&2; exit 64; }}
  replacement_state_directory=$2
  shift 2
  case "$replacement_state_directory" in /*) ;; *) echo "operator replacement restore directory must be absolute" >&2; exit 78 ;; esac
  test -d "$replacement_state_directory" && test ! -L "$replacement_state_directory" || {{ echo "operator replacement state is unavailable" >&2; exit 78; }}
  replacement_state_directory=$(readlink -f -- "$replacement_state_directory") || {{ echo "operator replacement state is unavailable" >&2; exit 78; }}
  live_state_directory=$(readlink -f -- "$state_directory") || {{ echo "operator state is unavailable" >&2; exit 78; }}
  test "$replacement_state_directory" != "$live_state_directory" || {{ echo "operator replacement state must not be the live state directory" >&2; exit 78; }}
  test "$(stat -c %u -- "$replacement_state_directory")" = "$(id -u)" && test "$(stat -c %a -- "$replacement_state_directory")" = 700 || {{
    echo "operator replacement state ownership or mode is invalid" >&2
    exit 78
  }}
  destination="$replacement_state_directory/platform.sqlite3"
  test ! -e "$destination" && test ! -L "$destination" || {{ echo "operator replacement destination must be absent" >&2; exit 78; }}
else
  test -d "$state_directory" && test ! -L "$state_directory" || {{ echo "operator state is unavailable" >&2; exit 78; }}
  test "$(stat -c %u -- "$state_directory")" = "$(id -u)" && test "$(stat -c %a -- "$state_directory")" = 700 || {{
    echo "operator state ownership or mode is invalid" >&2
    exit 78
  }}
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$release/source"
for argument in "$@"; do
  case "$argument" in
    --destination|--destination=*|--platform-config|--platform-config=*|--replacement-state-directory|--replacement-state-directory=*)
      echo "operator restore launcher refuses a fixed-path override" >&2
      exit 78
      ;;
  esac
done
exec "$release/.venv/bin/python" -P -m openstack_platform.restore "$@" \
  --platform-config "$platform_config" --destination "$destination"
"""


def _write_text(path: Path, text: str, mode: int) -> None:
    raw = text.encode("utf-8")
    if len(raw) > 1_048_576:
        _fail("release evidence exceeds its size limit")
    if os.path.lexists(path):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            _fail("release evidence destination has an unexpected type, owner, or mode")
    temporary = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temporary):
        metadata = temporary.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            _fail("release evidence has an unsafe stale temporary")
        temporary.unlink()
        _fsync_directory(path.parent)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
    )
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _smoke(mode: str, release: Path, python: Path) -> None:
    smoke = release / "source/deploy/releases/release_smoke.py"
    if not smoke.is_file():
        _fail("release is missing its integration smoke check")
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("PYTHONPATH", None)
    arguments: tuple[str | Path, ...] = (
        python,
        "-P",
        smoke,
        mode,
        "--source",
        release / "source",
    )
    if mode == "operator":
        arguments += (
            "--launcher",
            release / "bin/openstack-platform",
            "--restore-launcher",
            release / "bin/openstack-platform-restore",
        )
    _run(arguments, env=environment)


def _verify_protected_executable(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        _fail(f"{label} must be an absolute path")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail(f"{label} is missing: {path}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail(f"{label} must be a direct regular file: {path}")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) not in (0o500, 0o700):
        _fail(f"{label} must be current-user-owned with mode 0500 or 0700: {path}")


def _reject_duplicate_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _platform_identity_sha256(document: dict[str, Any]) -> str:
    try:
        identity = {
            "namespace": document["namespace"],
            "project": document["project"],
            "projectId": document["projectId"],
            "paths": document["paths"],
        }
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (KeyError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise InstallFailure("live helper platform configuration has invalid identity") from error
    return hashlib.sha256(encoded).hexdigest()


def _read_bounded_json(descriptor: int, *, label: str) -> dict[str, Any]:
    raw = b""
    while chunk := os.read(descriptor, min(65_536, _JSON_MAX_BYTES + 1 - len(raw))):
        raw += chunk
        if len(raw) > _JSON_MAX_BYTES:
            _fail(f"{label} exceeds its size limit")
    try:
        document = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, ValueError) as error:
        raise InstallFailure(f"{label} has invalid JSON") from error
    if not isinstance(document, dict):
        _fail(f"{label} must contain a JSON object")
    return document


def _verify_helper_platform_configuration(
    path: Path,
    *,
    expected_namespace: str,
    expected_identity_sha256: str,
    nix_store: Path = _NIX_STORE,
    helper_config_root: Path = _HELPER_CONFIG_ROOT,
) -> None:
    label = "live helper platform configuration"
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        _fail(f"{label} must be a canonical absolute path")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}[a-z0-9]", expected_namespace):
        _fail("expected helper platform namespace is invalid")
    if not _SHA256.fullmatch(expected_identity_sha256):
        _fail("expected helper platform identity digest is invalid")
    try:
        path_metadata = path.lstat()
    except FileNotFoundError:
        _fail(f"{label} is missing: {path}")
    except OSError as error:
        raise InstallFailure(f"{label} is unavailable: {path}") from error

    read_path = path
    expected_target: os.stat_result | None = None
    if stat.S_ISLNK(path_metadata.st_mode):
        expected_path = helper_config_root / expected_namespace / "platform.json"
        if path != expected_path:
            _fail(f"{label} symlink is allowed only at /etc/<namespace>/platform.json")
        try:
            resolved = path.resolve(strict=True)
            target_metadata = resolved.lstat()
        except (OSError, RuntimeError) as error:
            raise InstallFailure(f"{label} symlink target is unavailable") from error
        if (
            not resolved.is_absolute()
            or resolved == nix_store
            or not resolved.is_relative_to(nix_store)
        ):
            _fail(f"{label} symlink target must be under /nix/store")
        if not stat.S_ISREG(target_metadata.st_mode) or stat.S_ISLNK(target_metadata.st_mode):
            _fail(f"{label} symlink target must be a direct regular file")
        if target_metadata.st_uid != _ROOT_UID:
            _fail(f"{label} symlink target must be owned by root")
        if stat.S_IMODE(target_metadata.st_mode) & 0o022:
            _fail(f"{label} symlink target must not be group or world writable")
        if not os.access(resolved, os.R_OK):
            _fail(f"{label} symlink target must be readable by the operator account")
        read_path = resolved
        expected_target = target_metadata
    elif not stat.S_ISREG(path_metadata.st_mode) or not os.access(path, os.R_OK):
        _fail(f"{label} must be a direct readable regular file or an accepted Nix store symlink")

    try:
        descriptor = os.open(read_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise InstallFailure(f"{label} is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f"{label} target must be a direct regular file")
        if expected_target is not None and (
            (metadata.st_dev, metadata.st_ino) != (expected_target.st_dev, expected_target.st_ino)
            or metadata.st_uid != _ROOT_UID
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            _fail(f"{label} Nix store target changed during validation")
        if metadata.st_size > _JSON_MAX_BYTES:
            _fail(f"{label} exceeds its size limit")
        document = _read_bounded_json(descriptor, label=label)
    finally:
        os.close(descriptor)

    if document.get("namespace") != expected_namespace:
        _fail(f"{label} does not match the expected namespace")
    if _platform_identity_sha256(document) != expected_identity_sha256:
        _fail(f"{label} does not match the expected project, namespace, and paths")


def _verify_private_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fail(f"{label} is missing: {path}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail(f"{label} must be a direct regular file: {path}")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        _fail(f"{label} must be owned by the current user with mode 0600: {path}")


def _verify_operator_configuration(config_root: Path, state_root: Path) -> None:
    try:
        root_metadata = config_root.lstat()
    except FileNotFoundError:
        _fail("install current operator configuration before installing a release")
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        _fail(f"configuration directory must be direct, owned, and mode 0700: {config_root}")
    _verify_private_file(config_root / "platform.json", label="platform inventory")
    _verify_private_file(state_root / "policy.json", label="private policy")


def _install_user_units(
    source: Path,
    destination: Path,
    *,
    bin_root: Path,
    config_root: Path,
    state_root: Path,
) -> None:
    _ensure_directory(destination, 0o700)
    unit_source = source / "deploy/releases/systemd"
    replacements = {
        "@BIN_ROOT@": str(bin_root),
        "@CONFIG_ROOT@": str(config_root),
        "@STATE_ROOT@": str(state_root),
    }
    for name in ("openstack-platform-backup.service", "openstack-platform-backup.timer"):
        source_path = unit_source / name
        if not source_path.is_file():
            _fail(f"release is missing {name}")
        rendered = source_path.read_text(encoding="utf-8")
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        if re.search(r"@[A-Z_]+@", rendered):
            _fail(f"release unit {name} has an unknown path placeholder")
        _write_text(destination / name, rendered, 0o600)


def _candidate_wheel_inputs_sha256(source: Path) -> str:
    paths = [source / "pyproject.toml", source / "infra/lib/platform_contract.json"]
    paths.extend(
        path
        for path in (source / "openstack_platform").rglob("*")
        if path.is_file() and path.suffix in (".py", ".txt")
    )
    digest = hashlib.sha256(b"operator-wheel-inputs-v1\0")
    for path in sorted(paths, key=lambda item: item.relative_to(source).as_posix()):
        if path.is_symlink():
            _fail("release wheel input must not be a symlink")
        relative = path.relative_to(source).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


def _trusted_manifest_preflight(
    source: Path,
    manifest: Path,
    *,
    signature: Path | None,
    trust_root: Path | None,
    allow_unsigned_development: bool,
) -> None:
    """Establish trust and verifier integrity without candidate code execution."""
    try:
        document = json.loads(manifest.read_bytes(), object_pairs_hook=_reject_duplicate_pairs)
        trust = document["trust"]
        channel = document["releaseChannel"]
        expected_wheel = document["components"]["operatorWheel"]["sha256"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InstallFailure("release compatibility manifest is malformed") from error
    if not isinstance(trust, dict) or not _SHA256.fullmatch(str(expected_wheel)):
        _fail("release compatibility manifest trust or wheel identity is malformed")
    if trust.get("mode") == "production-ed25519" and channel == "production":
        if signature is None or trust_root is None:
            _fail("production release requires a signature and explicit trust root")
        _verify_archive_file(manifest)
        _verify_archive_file(signature)
        _verify_archive_file(trust_root)
        try:
            public_der = subprocess.run(
                [
                    "openssl",
                    "pkey",
                    "-pubin",
                    "-in",
                    trust_root,
                    "-pubout",
                    "-outform",
                    "DER",
                ],
                check=False,
                capture_output=True,
            )
            verified = subprocess.run(
                [
                    "openssl",
                    "pkeyutl",
                    "-verify",
                    "-rawin",
                    "-pubin",
                    "-inkey",
                    trust_root,
                    "-sigfile",
                    signature,
                    "-in",
                    manifest,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise InstallFailure(
                "OpenSSL is required for release signature verification"
            ) from error
        if public_der.returncode or hashlib.sha256(public_der.stdout).hexdigest() != trust.get(
            "publicKeySha256"
        ):
            _fail("release trust root does not match the signed manifest")
        if verified.returncode:
            _fail("release manifest signature verification failed")
    elif trust.get("mode") == "development-unsigned" and channel == "development-unsigned":
        if (
            not allow_unsigned_development
            or os.environ.get("PLATFORM_ENVIRONMENT") == "production"
            or signature is not None
            or trust_root is not None
        ):
            _fail("unsigned development release requires explicit non-production mode")
    else:
        _fail("release trust mode and channel are inconsistent")
    if _candidate_wheel_inputs_sha256(source) != expected_wheel:
        _fail("release candidate verifier or wheel inputs do not match the trusted manifest")


def _verify_release_gate(
    source: Path,
    *,
    commit: str,
    manifest: Path,
    signature: Path | None,
    trust_root: Path | None,
    allow_unsigned_development: bool,
) -> None:
    """Load the candidate's verifier and check evidence before install mutation."""
    _trusted_manifest_preflight(
        source,
        manifest,
        signature=signature,
        trust_root=trust_root,
        allow_unsigned_development=allow_unsigned_development,
    )
    verifier_path = source / "openstack_platform/release_manifest.py"
    if not verifier_path.is_file() or verifier_path.is_symlink():
        _fail("release candidate is missing its compatibility verifier")
    spec = importlib.util.spec_from_file_location("_candidate_release_manifest", verifier_path)
    if spec is None or spec.loader is None:
        _fail("release compatibility verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        module.verify(
            source,
            manifest,
            expected_commit=commit,
            signature=signature,
            trust_root=trust_root,
            allow_unsigned_development=allow_unsigned_development,
        )
    except Exception as error:
        raise InstallFailure(f"release compatibility verification failed: {error}") from error


def _preflight_release_gate(args: argparse.Namespace, commit: str) -> None:
    manifest = cast(Path | None, args.release_manifest)
    if manifest is None:
        _fail("--release-manifest is required before release mutation")
    source = cast(Path | None, args.source)
    archive = cast(Path | None, args.archive)
    signature = cast(Path | None, args.release_signature)
    trust_root = cast(Path | None, args.release_trust_root)
    allow_unsigned = cast(bool, args.allow_unsigned_development)
    if source is not None:
        _verify_release_gate(
            source.resolve(strict=True),
            commit=commit,
            manifest=manifest.absolute(),
            signature=signature.absolute() if signature else None,
            trust_root=trust_root.absolute() if trust_root else None,
            allow_unsigned_development=allow_unsigned,
        )
        return
    assert archive is not None
    archive_sha256 = cast(str | None, args.archive_sha256)
    if archive_sha256 is None:
        _fail("--archive requires --archive-sha256")
    _verify_archive_identity(
        archive.absolute(), expected_commit=commit, expected_sha256=archive_sha256
    )
    with tempfile.TemporaryDirectory(prefix="platform-release-preflight-") as temporary:
        candidate = Path(temporary)
        _extract_archive(archive.absolute(), candidate)
        _verify_release_gate(
            candidate,
            commit=commit,
            manifest=manifest.absolute(),
            signature=signature.absolute() if signature else None,
            trust_root=trust_root.absolute() if trust_root else None,
            allow_unsigned_development=allow_unsigned,
        )


def _retain_release_evidence(stage: Path, args: argparse.Namespace) -> None:
    manifest = cast(Path, args.release_manifest).absolute()
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        names = [document["evidence"][kind]["file"] for kind in ("sbom", "provenance")]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise InstallFailure("release evidence could not be retained") from error
    if any(not isinstance(name, str) or Path(name).name != name for name in names):
        _fail("release evidence filename is unsafe")
    destination = stage / "evidence"
    destination.mkdir(mode=0o750)
    copies = [(manifest, "release-manifest.json")]
    copies.extend((manifest.parent / name, name) for name in names)
    signature = cast(Path | None, args.release_signature)
    trust_root = cast(Path | None, args.release_trust_root)
    if signature is not None:
        copies.append((signature.absolute(), "release-manifest.sig"))
    if trust_root is not None:
        copies.append((trust_root.absolute(), "release-trust-root.pem"))
    for source, name in copies:
        if not source.is_file() or source.is_symlink():
            _fail("release evidence changed before retention")
        shutil.copyfile(source, destination / name)
        (destination / name).chmod(0o440)


def _build_source_archive(source: Path, commit: str, output: Path) -> None:
    try:
        head = subprocess.run(
            ["git", "-C", source, "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", source, "status", "--porcelain", "--untracked-files=no"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise InstallFailure("source must be a clean git checkout") from error
    if head != commit:
        _fail("requested release commit is not the checkout HEAD")
    if dirty:
        _fail("tracked source changes must be committed before installation")
    _run(("git", "-C", source, "archive", "--format=tar", f"--output={output}", commit))


def install(args: argparse.Namespace) -> Path:
    if os.geteuid() == 0 or os.environ.get("SUDO_USER"):
        _fail("run the release installer as the unprivileged platform owner")
    mode: str = args.mode
    commit: str = args.commit
    if not _COMMIT.fullmatch(commit):
        _fail("commit must be a full lowercase source commit")
    _preflight_release_gate(args, commit)

    defaults = _DEFAULTS.get(mode)
    requested_release_root = cast(Path | None, args.release_root)
    requested_bin_root = cast(Path | None, args.bin_root)
    requested_state_root = cast(Path | None, args.state_root)
    requested_config_root = cast(Path | None, args.config_root)
    requested_platform_config = cast(Path | None, args.platform_config)
    expected_platform_namespace = cast(str | None, args.expected_platform_namespace)
    expected_platform_identity_sha256 = cast(str | None, args.expected_platform_identity_sha256)
    if defaults is None:
        if requested_release_root is None or requested_bin_root is None:
            _fail("helper installation requires explicit --release-root and --bin-root")
        release_root = requested_release_root.absolute()
        bin_root = requested_bin_root.absolute()
        state_root = requested_state_root
    else:
        release_root = (requested_release_root or defaults.release_root).absolute()
        bin_root = (requested_bin_root or defaults.bin_root).absolute()
        state_root = requested_state_root or defaults.state_root
    config_root = (
        requested_config_root.absolute()
        if requested_config_root is not None
        else (
            state_root.parent / "config"
            if defaults is not None and state_root is not None
            else None
        )
    )
    requested_openstack_command = cast(Path | None, args.openstack_command)
    openstack_command = (
        requested_openstack_command.absolute()
        if requested_openstack_command is not None
        else (bin_root / "platform-openstack" if mode == "operator" else None)
    )
    _ensure_directory(release_root, 0o750)
    _ensure_directory(release_root / "releases", 0o750)
    _ensure_directory(bin_root, 0o750)
    if mode == "operator":
        assert config_root is not None and openstack_command is not None and state_root is not None
        state_root = state_root.absolute()
        _ensure_directory(state_root, 0o700)
        _ensure_directory(state_root / "logs", 0o700)
        _verify_operator_configuration(config_root, state_root)
        _verify_protected_executable(openstack_command, label="protected OpenStack command")
    else:
        if (
            requested_platform_config is None
            or expected_platform_namespace is None
            or expected_platform_identity_sha256 is None
        ):
            _fail(
                "helper installation requires explicit platform configuration and expected identity"
            )
        _verify_helper_platform_configuration(
            requested_platform_config,
            expected_namespace=expected_platform_namespace,
            expected_identity_sha256=expected_platform_identity_sha256,
        )

    python = _check_runtime(cast(Path, args.python))
    remove_archive = cast(bool, args.remove_archive)
    temporary_archive: Path | None = None
    requested_archive = cast(Path | None, args.archive)
    requested_source = cast(Path | None, args.source)
    requested_archive_sha256 = cast(str | None, args.archive_sha256)
    if requested_archive is None:
        if requested_source is None:
            _fail("either --archive or --source is required")
        if requested_archive_sha256 is not None:
            _fail("--archive-sha256 requires --archive")
        descriptor, name = tempfile.mkstemp(prefix="platform-release-", suffix=".tar")
        os.close(descriptor)
        temporary_archive = Path(name)
        archive = temporary_archive
        _build_source_archive(requested_source.resolve(strict=True), commit, archive)
        expected_archive_sha256 = _archive_sha256(archive)
    else:
        if requested_archive_sha256 is None:
            _fail("--archive requires --archive-sha256")
        archive = requested_archive.absolute()
        expected_archive_sha256 = requested_archive_sha256

    try:
        _verify_archive_identity(
            archive, expected_commit=commit, expected_sha256=expected_archive_sha256
        )
        final = release_root / "releases" / commit
        if final.exists():
            if not (final / ".complete").is_file():
                _fail("an incomplete release already exists for this commit")
            runtime = final / ".venv/bin/python" if mode == "operator" else python
            _smoke(mode, final, runtime)
        else:
            stage = Path(tempfile.mkdtemp(prefix=f".{commit}.", dir=release_root / "releases"))
            stage.chmod(0o750)
            try:
                source = stage / "source"
                source.mkdir(mode=0o750)
                _extract_archive(archive, source)
                runtime = python
                if mode == "operator":
                    runtime = _sync_operator_environment(
                        source=source,
                        release=stage,
                        python=python,
                        uv=cast(Path, args.uv),
                    )
                (stage / "bin").mkdir(mode=0o750)
                launcher_name = (
                    "openstack-platform" if mode == "operator" else "openstack-platform-helper"
                )
                _write_text(
                    stage / "bin" / launcher_name,
                    _launcher(
                        mode,
                        runtime,
                        platform_config=(requested_platform_config if mode == "helper" else None),
                        config_root=config_root if mode == "operator" else None,
                        state_root=state_root if mode == "operator" else None,
                        openstack_command=openstack_command if mode == "operator" else None,
                    ),
                    0o550,
                )
                if mode == "operator":
                    config_installer = source / "deploy/releases/install_operator_config.py"
                    if not config_installer.is_file():
                        _fail("operator release is missing its configuration installer")
                    _write_text(
                        stage / "bin/openstack-platform-install-config",
                        _config_launcher(cast(Path, config_root), cast(Path, state_root)),
                        0o550,
                    )
                    _write_text(
                        stage / "bin/openstack-platform-restore",
                        _restore_launcher(
                            cast(Path, state_root),
                            cast(Path, config_root) / "platform.json",
                        ),
                        0o550,
                    )
                _retain_release_evidence(stage, args)
                _write_text(stage / ".candidate", f"{commit}\n", 0o400)
                try:
                    _smoke(mode, stage, runtime)
                finally:
                    (stage / ".candidate").unlink(missing_ok=True)
                _write_text(stage / ".complete", f"{commit}\n", 0o440)
                os.rename(stage, final)
            except BaseException:
                shutil.rmtree(stage, ignore_errors=True)
                raise

        _atomic_symlink(final, release_root / "current")
        launcher_name = "openstack-platform" if mode == "operator" else "openstack-platform-helper"
        _atomic_symlink(release_root / "current/bin" / launcher_name, bin_root / launcher_name)
        if mode == "operator":
            _atomic_symlink(
                release_root / "current/bin/openstack-platform-install-config",
                bin_root / "openstack-platform-install-config",
            )
            _atomic_symlink(
                release_root / "current/bin/openstack-platform-restore",
                bin_root / "openstack-platform-restore",
            )

        if mode == "operator" and cast(bool, args.install_user_units):
            unit_directory = cast(Path | None, args.user_unit_dir) or (
                Path.home() / ".config/systemd/user"
            )
            assert config_root is not None and state_root is not None
            _install_user_units(
                final / "source",
                unit_directory.absolute(),
                bin_root=bin_root,
                config_root=config_root,
                state_root=state_root,
            )
            if cast(bool, args.enable_backup_timer):
                _run(("systemctl", "--user", "daemon-reload"))
                _run(
                    (
                        "systemctl",
                        "--user",
                        "enable",
                        "--now",
                        "openstack-platform-backup.timer",
                    )
                )
        print(f"installed={mode} commit={commit} release={final}")
        return final
    finally:
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)
        if remove_archive and requested_archive is not None:
            requested_archive.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install and atomically select a complete operator or helper release."
    )
    parser.add_argument("--mode", choices=("operator", "helper"), required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source", type=Path)
    source.add_argument("--archive", type=Path)
    parser.add_argument("--archive-sha256")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--release-signature", type=Path)
    parser.add_argument("--release-trust-root", type=Path)
    parser.add_argument(
        "--allow-unsigned-development",
        action="store_true",
        help="accept only a manifest marked development-unsigned (never production)",
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--uv", type=Path, default=Path(shutil.which("uv") or "uv"))
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--bin-root", type=Path)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--platform-config", type=Path)
    parser.add_argument("--expected-platform-namespace")
    parser.add_argument("--expected-platform-identity-sha256")
    parser.add_argument("--openstack-command", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--remove-archive", action="store_true")
    parser.add_argument("--install-user-units", action="store_true")
    parser.add_argument("--enable-backup-timer", action="store_true")
    parser.add_argument("--user-unit-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.enable_backup_timer:
        args.install_user_units = True
    install(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallFailure as error:
        print(f"release install failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
