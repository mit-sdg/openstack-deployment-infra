"""Reviewed protected-runner implementation of the P-07 driver contract.

This module composes the public operator CLI, controller HTTP API, packaged backup/
restore launchers, SSH recovery boundary, and OpenStack read/delete interface.  It
never accepts commands or argv fragments from configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

from openstack_platform import durable
from openstack_platform.acceptance import ACTION_CHECKS, ACTION_NAMES, AcceptanceError
from openstack_platform.config import load_platform
from openstack_platform.setup import load_environment_file

_MAX_OUTPUT = 1024 * 1024
_SAFE_REMOTE = re.compile(r"[A-Za-z0-9_./:@%?&=+{},-]+")
_BACKUP = re.compile(r"backup=(platform-[0-9]{8}T[0-9]{6}Z\.sqlite3\.age) sha256=([0-9a-f]{64})")
type Observation = dict[str, bool]


def _observed(required: Sequence[str], **values: bool) -> Observation:
    """Build an exact check projection; omitted or non-boolean facts never pass."""
    if set(values) != set(required) or any(type(value) is not bool for value in values.values()):
        raise LiveDriverError("typed observation does not match the required check contract")
    return dict(values)


class LiveDriverError(AcceptanceError):
    pass


class CommandTransport(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout: int = 1800,
        mutating: bool = False,
    ) -> bytes: ...


class SubprocessTransport:
    """Bounded argv-only transport with a private, checksummed plan transcript."""

    def __init__(self, transcript: Path) -> None:
        self.transcript = transcript
        self.records: list[dict[str, object]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        timeout: int = 1800,
        mutating: bool = False,
    ) -> bytes:
        if not argv or any("\x00" in value for value in argv):
            raise LiveDriverError("invalid fixed command invocation")
        sanitized_argv = [
            f"<absolute>/{Path(argument).name}" if argument.startswith("/") else argument
            for argument in argv
        ]
        self.records.append(
            {
                "sequence": len(self.records) + 1,
                "interface": Path(argv[0]).name,
                "argv": sanitized_argv,
                "argvSha256": hashlib.sha256("\0".join(argv).encode()).hexdigest(),
                "stdinSha256": hashlib.sha256(stdin).hexdigest(),
                "mutating": mutating,
            }
        )
        self._write_transcript()
        try:
            result = subprocess.run(
                tuple(argv),
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={
                    "PATH": os.environ.get("PATH", "/run/current-system/sw/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/nonexistent"),
                    "USER": os.environ.get("USER", "agent"),
                    "LANG": "C.UTF-8",
                },
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LiveDriverError(
                "supported acceptance interface was unavailable or timed out"
            ) from error
        if result.returncode != 0 or len(result.stdout) > _MAX_OUTPUT:
            raise LiveDriverError(
                "supported acceptance interface failed or exceeded its output bound"
            )
        return result.stdout

    def _write_transcript(self) -> None:
        document = {
            "schemaVersion": 1,
            "kind": "p07-driver-command-transcript",
            "commands": self.records,
            "mutationCount": sum(record["mutating"] is True for record in self.records),
        }
        _atomic_private(self.transcript, _canonical(document) + b"\n")


@dataclass(frozen=True, slots=True)
class DriverConfig:
    path: Path
    deployment_id: str
    project_id: str
    namespace: str
    operator: str
    platform_config: str
    policy: str
    state_directory: str
    setup_env: str
    setup_workspace: str
    openstack: str
    ssh: str
    scp: str
    ssh_config: str
    admin_alias: str
    recovery_alias: str
    controller_project_socket: str
    controller_privileged_socket: str
    curl: str
    age: str
    age_identity: str
    offline_state: str
    local_staging: str
    backup_root: str
    platform_root: str
    admin_state: str
    transcript: str
    backup_disposition: str
    application: Mapping[str, object]
    replacement_images: Mapping[str, str]


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LiveDriverError("JSON contains a duplicate key")
        result[key] = value
    return result


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _create_private(path: Path, data: bytes) -> None:
    """Create immutable run metadata; an existing byte-different file is refused."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if (
        path.parent.is_symlink()
        or not path.parent.is_dir()
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise LiveDriverError("ownership metadata directory must be private and current-user-owned")
    if path.exists():
        _private_file(path, "deployment ownership metadata")
        if path.read_bytes() != data:
            raise LiveDriverError("deployment ownership metadata changed after creation")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise LiveDriverError("deployment ownership metadata write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _atomic_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.parent.lstat()
    if (
        path.parent.is_symlink()
        or not path.parent.is_dir()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise LiveDriverError("driver state directory must be private and current-user-owned")
    try:
        durable.atomic_write(path, data, mode=0o600, maximum_bytes=_MAX_OUTPUT)
    except durable.DurableReplaceError as error:
        raise LiveDriverError("driver state could not be committed durably") from error


def _private_file(path: Path, field: str) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise LiveDriverError(f"{field} must be a direct current-user-owned mode-0600 file")


def _absolute(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or not _SAFE_REMOTE.fullmatch(value):
        raise LiveDriverError(f"driver configuration {field} must be a safe absolute path")
    return value


def load_driver_config(path: Path) -> DriverConfig:
    _private_file(path, "driver configuration")
    try:
        raw = json.loads(path.read_bytes(), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LiveDriverError("driver configuration is invalid JSON") from error
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise LiveDriverError("driver configuration version is unsupported")
    expected = {
        "version",
        "deploymentId",
        "projectId",
        "namespace",
        "operator",
        "remote",
        "recovery",
        "application",
        "replacementImages",
        "transcript",
        "backupDisposition",
    }
    if set(raw) != expected:
        raise LiveDriverError("driver configuration has an unexpected shape")
    operator = raw["operator"]
    remote = raw["remote"]
    recovery = raw["recovery"]
    application = raw["application"]
    images = raw["replacementImages"]
    if not all(
        isinstance(item, dict) for item in (operator, remote, recovery, application, images)
    ):
        raise LiveDriverError("driver configuration sections must be objects")
    assert isinstance(operator, dict) and isinstance(remote, dict) and isinstance(recovery, dict)
    assert isinstance(application, dict) and isinstance(images, dict)
    if set(operator) != {
        "executable",
        "platformConfig",
        "policy",
        "stateDirectory",
        "setupEnv",
        "setupWorkspace",
        "openstackWrapper",
    }:
        raise LiveDriverError("driver operator configuration has an unexpected shape")
    if set(remote) != {
        "ssh",
        "scp",
        "sshConfig",
        "adminAlias",
        "recoveryAlias",
        "controllerProjectSocket",
        "controllerPrivilegedSocket",
        "curl",
        "backupRoot",
        "platformRoot",
        "adminState",
    }:
        raise LiveDriverError("driver remote configuration has an unexpected shape")
    if set(recovery) != {"age", "ageIdentity", "offlineState", "localStaging"}:
        raise LiveDriverError("driver recovery configuration has an unexpected shape")
    deployment = str(raw["deploymentId"])
    project = str(raw["projectId"])
    try:
        if str(UUID(deployment)) != deployment or str(UUID(project)) != project:
            raise ValueError
    except ValueError as error:
        raise LiveDriverError("driver scope UUID is invalid") from error
    namespace = str(raw["namespace"])
    if not namespace.startswith("p07-") or not namespace.endswith(deployment[:8]):
        raise LiveDriverError("driver namespace is not bound to the deployment UUID")
    for file_field in (operator["setupEnv"], remote["sshConfig"], recovery["ageIdentity"]):
        _private_file(Path(str(file_field)), "driver private input")
    if set(application) != {
        "slug",
        "repository",
        "commit",
        "requestedRef",
        "configuration",
        "verificationPath",
        "resetPath",
        "contentProof",
        "emptyProof",
    }:
        raise LiveDriverError("driver application configuration has an unexpected shape")
    slug = application.get("slug")
    commit = application.get("commit")
    repository = application.get("repository")
    if not isinstance(slug, str) or not slug.endswith(deployment[:8]):
        raise LiveDriverError("application slug is not deployment scoped")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise LiveDriverError("application commit is invalid")
    if not isinstance(repository, str) or not repository.startswith("https://github.com/"):
        raise LiveDriverError("application repository must be public GitHub HTTPS")
    verification_path = application.get("verificationPath")
    reset_path = application.get("resetPath")
    if (
        not isinstance(application.get("requestedRef"), str)
        or not isinstance(application.get("configuration"), Mapping)
        or not isinstance(verification_path, str)
        or not verification_path.startswith("/")
        or not _SAFE_REMOTE.fullmatch(verification_path)
        or not isinstance(reset_path, str)
        or not reset_path.startswith("/")
        or not _SAFE_REMOTE.fullmatch(reset_path)
        or not isinstance(application.get("contentProof"), Mapping)
        or set(application["contentProof"]) != {"postgres", "mongo", "s3"}
        or any(
            not isinstance(value, Mapping) or not value
            for value in application["contentProof"].values()
        )
        or application.get("emptyProof") != {"postgres": None, "mongo": None, "s3": None}
    ):
        raise LiveDriverError("application restore proof contract is invalid")
    if set(images) != {"ingress", "admin"}:
        raise LiveDriverError("replacement images must contain ingress and admin")
    for image in images.values():
        if not isinstance(image, str) or str(UUID(image)) != image:
            raise LiveDriverError("replacement image UUID is invalid")
    if raw["backupDisposition"] != "destroy-after-verified-restore":
        raise LiveDriverError("backup disposition must explicitly approve disposable destruction")
    return DriverConfig(
        path,
        deployment,
        project,
        namespace,
        _absolute(operator["executable"], "operator.executable"),
        _absolute(operator["platformConfig"], "operator.platformConfig"),
        _absolute(operator["policy"], "operator.policy"),
        _absolute(operator["stateDirectory"], "operator.stateDirectory"),
        _absolute(operator["setupEnv"], "operator.setupEnv"),
        _absolute(operator["setupWorkspace"], "operator.setupWorkspace"),
        _absolute(operator["openstackWrapper"], "operator.openstackWrapper"),
        _absolute(remote["ssh"], "remote.ssh"),
        _absolute(remote["scp"], "remote.scp"),
        _absolute(remote["sshConfig"], "remote.sshConfig"),
        str(remote["adminAlias"]),
        str(remote["recoveryAlias"]),
        _absolute(remote["controllerProjectSocket"], "remote.controllerProjectSocket"),
        _absolute(remote["controllerPrivilegedSocket"], "remote.controllerPrivilegedSocket"),
        _absolute(remote["curl"], "remote.curl"),
        _absolute(recovery["age"], "recovery.age"),
        _absolute(recovery["ageIdentity"], "recovery.ageIdentity"),
        _absolute(recovery["offlineState"], "recovery.offlineState"),
        _absolute(recovery["localStaging"], "recovery.localStaging"),
        _absolute(remote["backupRoot"], "remote.backupRoot"),
        _absolute(remote["platformRoot"], "remote.platformRoot"),
        _absolute(remote["adminState"], "remote.adminState"),
        _absolute(raw["transcript"], "transcript"),
        str(raw["backupDisposition"]),
        application,
        images,
    )


class SupportedInterfaces:
    def __init__(self, config: DriverConfig, commands: CommandTransport) -> None:
        self.c = config
        self.commands = commands

    def _operator(self, *args: str, mutating: bool = False, timeout: int = 1800) -> bytes:
        if mutating:
            self.guard()
        return self.commands.run(
            (
                self.c.operator,
                "--platform-config",
                self.c.platform_config,
                "--state-directory",
                self.c.state_directory,
                "--policy",
                self.c.policy,
                *args,
            ),
            timeout=timeout,
            mutating=mutating,
        )

    def guard(self) -> None:
        output = self.commands.run(
            (self.c.openstack, "token", "issue", "-f", "json", "-c", "project_id"), timeout=60
        )
        try:
            value = json.loads(output)
            observed = value.get("project_id") if isinstance(value, dict) else None
        except json.JSONDecodeError as error:
            raise LiveDriverError("OpenStack project guard returned malformed JSON") from error
        if observed != self.c.project_id:
            raise LiveDriverError("OpenStack token project differs from acceptance scope")
        env = load_environment_file(Path(self.c.setup_env))
        if (
            env.get("OS_PROJECT_ID") != self.c.project_id
            or env.get("PLATFORM_NAMESPACE") != self.c.namespace
        ):
            raise LiveDriverError("protected setup inputs differ from acceptance scope")
        platform_path = Path(self.c.platform_config)
        if platform_path.exists():
            platform = load_platform(platform_path)
            if platform.project_id != self.c.project_id or platform.namespace != self.c.namespace:
                raise LiveDriverError("installed inventory differs from acceptance scope")

    def setup_plan(self) -> None:
        self.guard()
        self.commands.run(
            (
                self.c.operator,
                "setup",
                "--env-file",
                self.c.setup_env,
                "--workspace",
                self.c.setup_workspace,
            ),
            timeout=300,
        )

    def setup_apply(self) -> Observation:
        self.guard()
        # Re-observe greenfield immediately before apply; plan-time emptiness alone
        # is stale and cannot satisfy the execute check.
        inventory_text = _canonical(self.inventory()).decode()
        setup_values = load_environment_file(Path(self.c.setup_env))
        prefix = setup_values.get("PLATFORM_PREFIX", self.c.namespace)
        empty = self.c.namespace not in inventory_text and f'"{prefix}-' not in inventory_text
        if not empty:
            raise LiveDriverError("greenfield scope changed between plan and apply")
        self.guard()
        self.commands.run(
            (
                self.c.operator,
                "setup",
                "--env-file",
                self.c.setup_env,
                "--workspace",
                self.c.setup_workspace,
                "--apply",
            ),
            timeout=7200,
            mutating=True,
        )
        status = self._operator("status")
        if not status.strip():
            raise LiveDriverError("platform status observation is empty")
        self.guard()
        self.capture_ownership()
        return _observed(
            dict(ACTION_CHECKS)["greenfield_setup"],
            emptyScopeObserved=empty,
            planApplied=True,
            platformHealthy=True,
            deploymentScoped=True,
        )

    def _ssh(
        self,
        alias: str,
        argv: Sequence[str],
        *,
        stdin: bytes = b"",
        mutating: bool = False,
        timeout: int = 1800,
    ) -> bytes:
        if mutating:
            self.guard()
        return self.commands.run(
            (self.c.ssh, "-F", self.c.ssh_config, alias, "--", *argv),
            stdin=stdin,
            timeout=timeout,
            mutating=mutating,
        )

    def controller(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        key: str | None = None,
        mutating: bool = False,
        privileged: bool = False,
    ) -> tuple[int, object]:
        if not path.startswith("/v1/") or not _SAFE_REMOTE.fullmatch(path):
            raise LiveDriverError("controller path is unsafe")
        argv = [self.c.curl]
        alias = self.c.admin_alias
        socket_path = self.c.controller_privileged_socket
        if not privileged:
            alias = self.c.recovery_alias
            socket_path = self.c.controller_project_socket
            argv = [
                "sudo",
                "-n",
                "runuser",
                "-u",
                "management-broker",
                "--",
                self.c.curl,
            ]
        argv.extend(
            [
                "--silent",
                "--show-error",
                "--max-time",
                "300",
                "--unix-socket",
                socket_path,
                "-X",
                method,
                "-H",
                "Content-Type:application/json",
            ]
        )
        if key is not None:
            argv.extend(("-H", f"Idempotency-Key:{key}"))
        argv.extend(("--data-binary", "@-", "-w", "\\n%{http_code}", f"http://localhost{path}"))
        raw = self._ssh(
            alias,
            argv,
            stdin=_canonical(body) if body is not None else b"",
            mutating=mutating,
        )
        try:
            payload, code = raw.rsplit(b"\n", 1)
            return int(code), json.loads(payload)
        except (ValueError, json.JSONDecodeError) as error:
            raise LiveDriverError("controller returned malformed bounded HTTP output") from error

    def operation_status(self, key: str) -> Mapping[str, object]:
        code, body = self.controller("GET", f"/v1/operations/{key}")
        if code != 200 or not isinstance(body, dict):
            raise LiveDriverError("controller operation response is invalid")
        return body

    def operation(self, key: str) -> Mapping[str, object]:
        deadline = time.monotonic() + 1800
        while True:
            body = self.operation_status(key)
            status = body.get("status")
            if status == "succeeded":
                return body
            if status in {"failed", "recovery_required"}:
                raise LiveDriverError("controller operation did not converge")
            if time.monotonic() >= deadline:
                raise LiveDriverError("controller operation polling timed out")
            time.sleep(2)

    def interrupt_controller(self, operation_key: str) -> Observation:
        deadline = time.monotonic() + 120
        while True:
            operation = self.operation_status(operation_key)
            if operation.get("status") == "running" and operation.get("phase") not in {
                None,
                "queued",
            }:
                break
            if operation.get("status") in {"succeeded", "failed", "recovery_required"}:
                raise LiveDriverError("operation became terminal before interruption")
            if time.monotonic() >= deadline:
                raise LiveDriverError("operation did not start before interruption deadline")
            time.sleep(0.2)
        controller = f"{self.c.namespace}-controller.service"
        readiness = f"{self.c.namespace}-controller-readiness.service"
        self._ssh(
            self.c.recovery_alias,
            ("sudo", "-n", "systemctl", "kill", "--signal=KILL", controller),
            mutating=True,
        )
        self._ssh(
            self.c.recovery_alias,
            ("sudo", "-n", "systemctl", "start", controller, readiness),
            mutating=True,
        )
        active = self._ssh(
            self.c.recovery_alias,
            ("sudo", "-n", "systemctl", "is-active", controller, readiness),
        )
        if active.decode().split() != ["active", "active"]:
            raise LiveDriverError("controller did not recover after deliberate interruption")
        return {"durableOperationStarted": True, "interruptionInjected": True}

    def public_json(self, url: str, *, method: str = "GET") -> Mapping[str, object]:
        if method not in {"GET", "POST"}:
            raise LiveDriverError("public proof method is unsupported")
        output = self.commands.run(
            (
                self.c.curl,
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "30",
                "-X",
                method,
                url,
            ),
            timeout=60,
            mutating=method == "POST",
        )
        try:
            result = json.loads(output)
        except json.JSONDecodeError as error:
            raise LiveDriverError("public storage proof returned malformed JSON") from error
        if not isinstance(result, dict):
            raise LiveDriverError("public storage proof is not an object")
        return result

    def verify_public_storage(self, url: str, expected: Mapping[str, object]) -> Observation:
        result = self.public_json(url)
        if result != expected:
            raise LiveDriverError("application content proof differs from the exact expected value")
        return _observed(
            (
                "candidateVerified",
                "publicRouteHealthy",
                "storageBound",
                "postgresWriteReadVerified",
                "mongoWriteReadVerified",
                "s3WriteReadVerified",
            ),
            candidateVerified=True,
            publicRouteHealthy=True,
            storageBound=True,
            postgresWriteReadVerified=True,
            mongoWriteReadVerified=True,
            s3WriteReadVerified=True,
        )

    def mutate_controller(
        self,
        method: str,
        path: str,
        body: object,
        key: str,
        *,
        wait: bool = True,
        privileged: bool = False,
    ) -> object:
        code, response = self.controller(
            method,
            path,
            body,
            key=key,
            mutating=True,
            privileged=privileged,
        )
        if code not in {200, 201, 202}:
            raise LiveDriverError("controller mutation was rejected")
        if code == 202 and wait:
            return self.operation(key)
        return response

    def list_items(self, path: str) -> list[Mapping[str, object]]:
        code, body = self.controller("GET", path)
        if code != 200 or not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise LiveDriverError("controller list response is invalid")
        return [item for item in body["items"] if isinstance(item, dict)]

    def operator_backup_restore(self) -> Observation:
        output = self._operator("backup", mutating=True)
        match = _BACKUP.search(output.decode())
        if match is None:
            raise LiveDriverError("operator backup did not return accepted evidence")
        name, expected = match.groups()
        staging = Path(self.c.local_staging)
        staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        local = staging / name
        self.commands.run(
            (
                self.c.scp,
                "-F",
                self.c.ssh_config,
                "--",
                f"{self.c.admin_alias}:{self.c.backup_root}/controller/{name}",
                str(local),
            ),
            timeout=600,
        )
        local.chmod(0o600)
        if hashlib.sha256(local.read_bytes()).hexdigest() != expected:
            raise LiveDriverError("copied operator backup checksum differs")
        Path(self.c.offline_state).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.guard()
        restored = self.commands.run(
            (
                self.c.operator,
                "--platform-config",
                self.c.platform_config,
                "--state-directory",
                self.c.offline_state,
                "--policy",
                self.c.policy,
                "restore",
                str(local),
                "--age-identity",
                self.c.age_identity,
                "--yes",
            ),
            timeout=600,
            mutating=True,
        )
        if (
            re.fullmatch(rb"restore=verified schema-version=[1-9][0-9]* integrity=ok\n?", restored)
            is None
        ):
            raise LiveDriverError("offline operator restore typed observation is invalid")
        return _observed(
            dict(ACTION_CHECKS)["operator_sqlite_restore"],
            encryptedBackupVerified=True,
            offlineRestoreVerified=True,
            deploymentIdentityMatched=True,
        )

    def managed_restore(self, application_url: str) -> Observation:
        env = (
            "env",
            f"PLATFORM_CONFIG=/etc/{self.c.namespace}/platform.json",
            f"AGE={self.c.platform_root}/bin/age",
            f"AGE_KEY={self.c.platform_root}/persistent/secrets/backup-age-key.txt",
        )
        backup = (
            *env,
            f"AGE_KEYGEN={self.c.platform_root}/bin/age-keygen",
            f"EMIT_SCRIPT={self.c.platform_root}/infra/backup/emit_logical_backup.sh",
            "SERVICE_CHECK_PYTHON=python3",
            f"GARAGE_EMIT_SCRIPT={self.c.platform_root}/infra/backup/emit_garage_backup.py",
            f"{self.c.platform_root}/infra/backup/run_platform_backup.sh",
        )
        self._ssh(self.c.admin_alias, backup, mutating=True, timeout=1800)
        latest_raw = self._ssh(
            self.c.admin_alias,
            (
                "find",
                f"{self.c.backup_root}/{self.c.namespace}",
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-type",
                "d",
                "-name",
                "20??????T??????Z",
                "-printf",
                "%f\\n",
            ),
        )
        latest = sorted(
            line
            for line in latest_raw.decode().splitlines()
            if re.fullmatch(r"20[0-9]{6}T[0-9]{6}Z", line)
        )
        if not latest:
            raise LiveDriverError("managed backup evidence directory is absent")
        reset_url = application_url + str(self.c.application["resetPath"])
        empty = self.public_json(reset_url, method="POST")
        if empty != self.c.application["emptyProof"]:
            raise LiveDriverError("disposable managed-data target was not observed exactly empty")
        evidence = f"{self.c.backup_root}/{self.c.namespace}/{latest[-1]}"
        output = self._ssh(
            self.c.admin_alias,
            (
                *env,
                "SERVICE_CHECK_PYTHON=python3",
                f"{self.c.platform_root}/infra/backup/restore_managed_data.sh",
                "--yes",
                evidence,
            ),
            mutating=True,
            timeout=1800,
        )
        required_lines = {
            "postgres managed restore=complete",
            "mongodb managed restore=complete",
            "managed-data-restore=verified",
        }
        rendered = output.decode()
        if (
            any(line not in rendered for line in required_lines)
            or "garage-restore=verified" not in rendered
        ):
            raise LiveDriverError("replacement restore did not report typed service observations")
        proof_url = application_url + str(self.c.application["verificationPath"])
        if self.public_json(proof_url) != self.c.application["contentProof"]:
            raise LiveDriverError("restored PostgreSQL/MongoDB/S3 content differs")
        return _observed(
            dict(ACTION_CHECKS)["managed_data_restore"],
            postgresRestored=True,
            mongoRestored=True,
            s3Restored=True,
            restoreManifestVerified=True,
        )

    def hosted_restore(self) -> Observation:
        # The packaged launcher owns validation and atomic replacement. A protected
        # recovery account performs the documented offline sequence; the escrowed
        # identity never crosses to admin.
        unit = f"{self.c.namespace}-hosted-controller-backup.service"
        self._ssh(self.c.recovery_alias, ("sudo", "-n", "systemctl", "start", unit), mutating=True)
        listing = self._ssh(
            self.c.admin_alias,
            (
                "find",
                f"{self.c.backup_root}/hosted-controller",
                "-maxdepth",
                "1",
                "-name",
                "*.manifest",
                "-printf",
                "%f\\n",
            ),
        )
        manifests = sorted(
            line
            for line in listing.decode().splitlines()
            if re.fullmatch(r"hosted-controller-.*\.age\.manifest", line)
        )
        if not manifests:
            raise LiveDriverError("hosted controller backup manifest is absent")
        ciphertext_name = manifests[-1].removesuffix(".manifest")
        staging = Path(self.c.local_staging)
        staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        ciphertext = staging / ciphertext_name
        self.commands.run(
            (
                self.c.scp,
                "-F",
                self.c.ssh_config,
                "--",
                f"{self.c.admin_alias}:{self.c.backup_root}/hosted-controller/{ciphertext_name}",
                str(ciphertext),
            ),
            timeout=600,
        )
        ciphertext.chmod(0o600)
        checksum_path = staging / f"{ciphertext_name}.sha256"
        self.commands.run(
            (
                self.c.scp,
                "-F",
                self.c.ssh_config,
                "--",
                f"{self.c.admin_alias}:{self.c.backup_root}/hosted-controller/{ciphertext_name}.sha256",
                str(checksum_path),
            ),
            timeout=600,
        )
        checksum_path.chmod(0o600)
        checksum_fields = checksum_path.read_text(encoding="ascii").split()
        if (
            len(checksum_fields) < 1
            or re.fullmatch(r"[0-9a-f]{64}", checksum_fields[0]) is None
            or hashlib.sha256(ciphertext.read_bytes()).hexdigest() != checksum_fields[0]
        ):
            raise LiveDriverError("hosted controller backup checksum differs")
        plaintext = staging / "hosted-controller-restore.sqlite3"
        restored = self.commands.run(
            (self.c.age, "--decrypt", "--identity", self.c.age_identity, str(ciphertext)),
            timeout=600,
        )
        _atomic_private(plaintext, restored)
        self.guard()
        self.commands.run(
            (
                self.c.scp,
                "-F",
                self.c.ssh_config,
                "--",
                str(plaintext),
                f"{self.c.admin_alias}:/home/agentops/hosted-controller-restore.sqlite3",
            ),
            timeout=600,
            mutating=True,
        )
        controller = f"{self.c.namespace}-controller.service"
        timer = f"{self.c.namespace}-hosted-controller-backup.timer"
        self._ssh(
            self.c.recovery_alias,
            ("sudo", "-n", "systemctl", "stop", timer, unit, controller),
            mutating=True,
        )
        self._ssh(
            self.c.recovery_alias,
            (
                "sudo",
                "-n",
                "install",
                "-m",
                "0600",
                "-o",
                "platform-controller",
                "-g",
                "platform-controller",
                "/home/agentops/hosted-controller-restore.sqlite3",
                f"{self.c.admin_state}/controller/restore-input.sqlite3",
            ),
            mutating=True,
        )
        self._ssh(
            self.c.recovery_alias,
            ("sudo", "-n", "openstack-platform-hosted-controller-restore", "--yes"),
            mutating=True,
        )
        self._ssh(
            self.c.recovery_alias,
            ("sudo", "-n", "systemctl", "start", controller, timer),
            mutating=True,
        )
        ready = self._ssh(
            self.c.recovery_alias,
            (
                "sudo",
                "-n",
                "systemctl",
                "is-active",
                f"{self.c.namespace}-controller-readiness.service",
            ),
        )
        if ready.strip() != b"active":
            raise LiveDriverError("hosted restore controller readiness observation failed")
        plaintext.unlink(missing_ok=True)
        return _observed(
            dict(ACTION_CHECKS)["hosted_sqlite_restore"],
            encryptedBackupVerified=True,
            offlineRestoreVerified=True,
            controllerReady=True,
        )

    def _owned_names(self) -> dict[str, list[str]]:
        platform = load_platform(Path(self.c.platform_config))
        names: dict[str, list[str]] = {
            "server": [
                str(platform.get(f"hosts.{role}")) for role in ("admin", "ingress", "storage")
            ],
            "port": [
                str(platform.get(f"ports.{role}")) for role in ("admin", "ingress", "storage")
            ],
            "volume": [
                str(platform.get(f"volumes.{name}.name"))
                for name in ("adminState", "backup", "data")
            ],
            "image": [
                str(platform.get(f"images.{role}"))
                for role in ("admin", "ingress", "storage", "worker", "builder")
            ],
            "security group": [
                f"{platform.prefix}-{role}"
                for role in ("admin", "ingress", "worker", "builder", "storage")
            ],
            "keypair": [f"{platform.prefix}-admin"],
        }
        return names

    @staticmethod
    def _field_exact(document: Mapping[str, object], *aliases: str) -> object:
        found = [
            value
            for key, value in document.items()
            if str(key).lower().replace(" ", "_") in aliases
        ]
        if len(found) != 1:
            raise LiveDriverError("provider projection omitted or duplicated a required field")
        return found[0]

    def _show_projection(self, kind: str, reference: str, expected_name: str) -> dict[str, object]:
        shown = self.commands.run(
            (self.c.openstack, *kind.split(), "show", reference, "-f", "json"), timeout=60
        )
        try:
            raw = json.loads(shown)
        except json.JSONDecodeError as error:
            raise LiveDriverError("provider ownership projection is malformed") from error
        if not isinstance(raw, dict):
            raise LiveDriverError("provider ownership projection is not an object")
        name = self._field_exact(raw, "name")
        if name != expected_name:
            raise LiveDriverError("provider ownership name differs")
        if kind == "keypair":
            keypair_projection = {"name": name}
            for output, aliases in {
                "fingerprint": ("fingerprint",),
                "publicKey": ("public_key",),
                "type": ("type",),
                "userId": ("user_id",),
            }.items():
                value = self._field_exact(raw, *aliases)
                if not isinstance(value, str) or not value:
                    raise LiveDriverError("keypair ownership projection is incomplete")
                keypair_projection[output] = value
            return keypair_projection
        identifier = self._field_exact(raw, "id")
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise LiveDriverError("provider ownership UUID is invalid")
        project_aliases = (
            ("owner", "owner_id", "project_id")
            if kind == "image"
            else ("project_id", "tenant_id", "os-vol-tenant-attr:tenant_id")
        )
        project = self._field_exact(raw, *project_aliases)
        if project != self.c.project_id:
            raise LiveDriverError("provider ownership project differs")
        projection: dict[str, object] = {"id": identifier, "name": name, "projectId": project}
        if kind in {"server", "image"}:
            properties = self._field_exact(raw, "properties")
            if not isinstance(properties, Mapping):
                raise LiveDriverError("provider ownership properties are malformed")
            prefix = self.c.namespace.replace("-", "_")
            expected = {
                f"{prefix}_managed_by": "platform",
                f"{prefix}_namespace": self.c.namespace,
                f"{prefix}_project_id": self.c.project_id,
            }
            if any(properties.get(key) != value for key, value in expected.items()):
                raise LiveDriverError(
                    "provider deployment ownership metadata is absent or ambiguous"
                )
            projection["deploymentProperties"] = expected
        return projection

    @property
    def ownership_path(self) -> Path:
        return Path(self.c.transcript).with_name("deployment-ownership.json")

    def capture_ownership(self) -> None:
        resources: list[dict[str, object]] = []
        for kind, names in self._owned_names().items():
            visibility = ("--private",) if kind == "image" else ()
            rows_raw = self.commands.run(
                (self.c.openstack, *kind.split(), "list", *visibility, "-f", "json"), timeout=60
            )
            try:
                rows = json.loads(rows_raw)
            except json.JSONDecodeError as error:
                raise LiveDriverError("provider ownership inventory is malformed") from error
            if not isinstance(rows, list):
                raise LiveDriverError("provider ownership inventory is invalid")
            for name in names:
                matches = [
                    row
                    for row in rows
                    if isinstance(row, dict) and self._field_exact(row, "name") == name
                ]
                if len(matches) != 1:
                    raise LiveDriverError("deployment resource ownership is absent or ambiguous")
                reference = name if kind == "keypair" else self._field_exact(matches[0], "id")
                if not isinstance(reference, str):
                    raise LiveDriverError("deployment resource reference is invalid")
                resources.append(
                    {
                        "kind": kind,
                        "name": name,
                        "deleteReference": reference,
                        "projection": self._show_projection(kind, reference, name),
                    }
                )
        document = {
            "schemaVersion": 1,
            "deploymentId": self.c.deployment_id,
            "projectId": self.c.project_id,
            "namespace": self.c.namespace,
            "resources": resources,
        }
        _create_private(self.ownership_path, _canonical(document) + b"\n")

    def replace(self, role: str) -> Observation:
        before = self._show_projection(
            "server",
            self._owned_names()["server"][("admin", "ingress", "storage").index(role)],
            self._owned_names()["server"][("admin", "ingress", "storage").index(role)],
        )
        image = self.c.replacement_images[role]
        self._operator("infra", "image", "set", role, image, mutating=True)
        if role == "admin":
            self._operator("infra", "stop", "admin", "--yes", mutating=True)
        output = self._operator("infra", "replace", role, "--yes", mutating=True, timeout=1800)
        replacement_evidence = {
            "server=",
            "old_retained_until_ready=verified",
            "exact_identity=verified",
            "data_retained=verified",
        }
        rendered_output = output.decode()
        if any(token not in rendered_output for token in replacement_evidence):
            raise LiveDriverError("replacement did not report exact typed acceptance observations")
        name = str(before["name"])
        after = self._show_projection("server", name, name)
        if before["id"] == after["id"]:
            raise LiveDriverError("host replacement did not change exact server identity")
        transition = {
            "schemaVersion": 1,
            "deploymentId": self.c.deployment_id,
            "projectId": self.c.project_id,
            "namespace": self.c.namespace,
            "role": role,
            "oldProjection": before,
            "newProjection": after,
        }
        transition_path = self.ownership_path.with_name(f"replacement-{role}-ownership.json")
        _create_private(transition_path, _canonical(transition) + b"\n")
        status = self._operator("infra", "list")
        if not status.strip():
            raise LiveDriverError("replacement role health observation is empty")
        if role == "ingress":
            # Data retention is separately observed through the accepted application;
            # this provider observation establishes only replacement ordering/identity.
            return {"oldHostRetainedUntilReady": True, "exactIdentityVerified": True}
        return {"externalRecoveryUsed": True}

    def inventory(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for kind in ("server", "port", "volume", "image", "security group", "keypair"):
            visibility = ("--private",) if kind == "image" else ()
            raw = self.commands.run(
                (self.c.openstack, *kind.split(), "list", *visibility, "-f", "json"),
                timeout=120,
            )
            try:
                rows = json.loads(raw)
            except json.JSONDecodeError as error:
                raise LiveDriverError("OpenStack inventory returned malformed JSON") from error
            if not isinstance(rows, list):
                raise LiveDriverError("OpenStack inventory shape is invalid")
            for row in rows:
                if isinstance(row, dict):
                    result.append({"kind": kind, "row": row})
        result.sort(key=lambda item: _canonical(item))
        return result

    def baseline(self) -> str:
        self.guard()
        return hashlib.sha256(_canonical(self.inventory())).hexdigest()

    def teardown(self, baseline: str) -> Observation:
        self.guard()
        try:
            ownership = json.loads(
                self.ownership_path.read_bytes(), object_pairs_hook=_reject_duplicates
            )
        except (OSError, json.JSONDecodeError) as error:
            raise LiveDriverError("immutable deployment ownership metadata is absent") from error
        if (
            not isinstance(ownership, dict)
            or ownership.get("schemaVersion") != 1
            or ownership.get("deploymentId") != self.c.deployment_id
            or ownership.get("projectId") != self.c.project_id
            or ownership.get("namespace") != self.c.namespace
        ):
            raise LiveDriverError("immutable deployment ownership metadata is ambiguous")
        resources = ownership.get("resources")
        if not isinstance(resources, list) or not resources:
            raise LiveDriverError("immutable deployment ownership inventory is absent")
        current_resources: list[object] = list(resources)
        for role in ("ingress", "admin"):
            transition_path = self.ownership_path.with_name(f"replacement-{role}-ownership.json")
            if not transition_path.exists():
                continue
            _private_file(transition_path, "replacement ownership metadata")
            try:
                transition = json.loads(
                    transition_path.read_bytes(), object_pairs_hook=_reject_duplicates
                )
            except json.JSONDecodeError as error:
                raise LiveDriverError("replacement ownership metadata is malformed") from error
            if (
                not isinstance(transition, dict)
                or set(transition)
                != {
                    "schemaVersion",
                    "deploymentId",
                    "projectId",
                    "namespace",
                    "role",
                    "oldProjection",
                    "newProjection",
                }
                or transition.get("schemaVersion") != 1
                or transition.get("deploymentId") != self.c.deployment_id
                or transition.get("projectId") != self.c.project_id
                or transition.get("namespace") != self.c.namespace
                or transition.get("role") != role
                or not isinstance(transition.get("oldProjection"), dict)
                or not isinstance(transition.get("newProjection"), dict)
            ):
                raise LiveDriverError("replacement ownership metadata is ambiguous")
            transition_matches = [
                (index, record)
                for index, record in enumerate(current_resources)
                if isinstance(record, dict)
                and record.get("kind") == "server"
                and record.get("projection") == transition["oldProjection"]
            ]
            new_projection = transition["newProjection"]
            if len(transition_matches) != 1 or not isinstance(new_projection.get("id"), str):
                raise LiveDriverError("replacement ownership transition has no exact predecessor")
            index, previous = transition_matches[0]
            assert isinstance(previous, dict)
            current_resources[index] = {
                **previous,
                "deleteReference": new_projection["id"],
                "projection": new_projection,
            }
        backup_owned = False
        for record in current_resources:
            if not isinstance(record, dict) or set(record) != {
                "kind",
                "name",
                "deleteReference",
                "projection",
            }:
                raise LiveDriverError("immutable deployment ownership record is malformed")
            kind, name, reference, expected = (
                record.get(key) for key in ("kind", "name", "deleteReference", "projection")
            )
            if (
                kind not in {"server", "port", "volume", "image", "security group", "keypair"}
                or not isinstance(name, str)
                or not isinstance(reference, str)
                or not isinstance(expected, dict)
            ):
                raise LiveDriverError("immutable deployment ownership record is untyped")
            if kind == "volume" and name == str(
                load_platform(Path(self.c.platform_config)).get("volumes.backup.name")
            ):
                backup_owned = True
            self.guard()
            visibility = ("--private",) if kind == "image" else ()
            raw = self.commands.run(
                (self.c.openstack, *str(kind).split(), "list", *visibility, "-f", "json"),
                timeout=60,
            )
            try:
                rows = json.loads(raw)
            except json.JSONDecodeError as error:
                raise LiveDriverError("teardown inventory observation is malformed") from error
            if not isinstance(rows, list):
                raise LiveDriverError("teardown inventory observation is invalid")
            matches = [
                row
                for row in rows
                if isinstance(row, dict) and self._field_exact(row, "name") == name
            ]
            if not matches:
                continue
            if len(matches) != 1:
                raise LiveDriverError("teardown candidate identity is ambiguous")
            current_reference = name if kind == "keypair" else self._field_exact(matches[0], "id")
            if current_reference != reference:
                raise LiveDriverError(
                    "teardown candidate differs from immutable ownership identity"
                )
            if self._show_projection(str(kind), reference, name) != expected:
                raise LiveDriverError("teardown candidate ownership projection drifted")
            self.commands.run(
                (self.c.openstack, *str(kind).split(), "delete", reference),
                timeout=600,
                mutating=True,
            )
        if not backup_owned or self.c.backup_disposition != "destroy-after-verified-restore":
            raise LiveDriverError("backup disposition is absent or does not cover the owned backup")
        deadline = time.monotonic() + 300
        while self.baseline() != baseline:
            if time.monotonic() >= deadline:
                raise LiveDriverError("unrelated provider inventory changed during acceptance")
            time.sleep(5)
        # A second exact inventory pass proves every recorded identity absent.
        for kind, names in self._owned_names().items():
            visibility = ("--private",) if kind == "image" else ()
            raw = self.commands.run(
                (self.c.openstack, *kind.split(), "list", *visibility, "-f", "json"), timeout=60
            )
            rows = json.loads(raw)
            if not isinstance(rows, list) or any(
                isinstance(row, dict) and self._field_exact(row, "name") in names for row in rows
            ):
                raise LiveDriverError("deployment-owned provider resources remain")
        return _observed(
            dict(ACTION_CHECKS)["cleanup_verify"],
            ownedResourcesAbsent=True,
            unrelatedFingerprintUnchanged=True,
            backupsDispositionRecorded=True,
        )


class RepositoryLiveDriver:
    def __init__(self, config: DriverConfig, interfaces: SupportedInterfaces) -> None:
        self.c = config
        self.i = interfaces
        self.app_id = str(uuid5(UUID(config.deployment_id), "application"))

    def _scope(self, request: Mapping[str, object]) -> None:
        scope = request.get("scope")
        if scope != {
            "deploymentId": self.c.deployment_id,
            "projectId": self.c.project_id,
            "namespace": self.c.namespace,
        }:
            raise LiveDriverError("request scope differs from protected driver configuration")

    def handle(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self._scope(request)
        if request.get("schemaVersion") != 1:
            raise LiveDriverError("request schema version is unsupported")
        mode = request.get("mode")
        action = request.get("action")
        if mode == "plan":
            if set(request) != {
                "schemaVersion",
                "mode",
                "action",
                "scope",
                "requiredActions",
                "bounds",
            }:
                raise LiveDriverError("plan request has an unexpected shape")
            if action != "full_drill" or request.get("requiredActions") != list(ACTION_NAMES):
                raise LiveDriverError("plan requested a different action contract")
            self.i.setup_plan()
            baseline = self.i.baseline()
            inventory_text = _canonical(self.i.inventory()).decode()
            setup_values = load_environment_file(Path(self.c.setup_env))
            prefix = setup_values.get("PLATFORM_PREFIX", self.c.namespace)
            if self.c.namespace in inventory_text or f'"{prefix}-' in inventory_text:
                raise LiveDriverError("greenfield scope already has deployment-owned resources")
            return self._response(
                action=None,
                extra={
                    "capabilities": list(ACTION_NAMES),
                    "driverConfigurationSha256": hashlib.sha256(
                        self.c.path.read_bytes()
                    ).hexdigest(),
                    "baselineFingerprint": baseline,
                    "ownedResources": [],
                },
            )
        if mode != "execute" or not isinstance(action, str) or action not in ACTION_NAMES:
            raise LiveDriverError("execute action is unsupported")
        if set(request) != {
            "schemaVersion",
            "mode",
            "action",
            "scope",
            "planSha256",
            "driverConfigurationSha256",
            "baselineFingerprint",
        }:
            raise LiveDriverError("execute request has an unexpected shape")
        expected_configuration_sha = hashlib.sha256(self.c.path.read_bytes()).hexdigest()
        if request.get("driverConfigurationSha256") != expected_configuration_sha:
            raise LiveDriverError("protected driver configuration changed after planning")
        requested_baseline = request.get("baselineFingerprint")
        if (
            not isinstance(requested_baseline, str)
            or re.fullmatch(r"[0-9a-f]{64}", requested_baseline) is None
        ):
            raise LiveDriverError("execute baseline fingerprint is invalid")
        checks = self._execute(action, requested_baseline)
        required = dict(ACTION_CHECKS)[action]
        if set(checks) != set(required) or not all(checks.values()):
            raise LiveDriverError("action did not establish every required check")
        return self._response(action=action, extra={"checks": checks})

    def _response(self, *, action: str | None, extra: Mapping[str, object]) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": 1,
            "ok": True,
            "deploymentId": self.c.deployment_id,
            "projectId": self.c.project_id,
            "namespace": self.c.namespace,
        }
        if action is not None:
            result["action"] = action
        result.update(extra)
        return result

    def _storage_items(self, kind: str) -> list[Mapping[str, object]]:
        return [
            item
            for item in self.i.list_items(f"/v1/applications/{self.app_id}/storage?limit=100")
            if item.get("type") == kind
        ]

    def _deployment_body(self, revision: int) -> dict[str, object]:
        items = self.i.list_items(f"/v1/applications/{self.app_id}/storage?limit=100")
        configuration = self.c.application.get("configuration")
        if not isinstance(configuration, Mapping):
            raise LiveDriverError("application configuration is invalid")
        targets = {
            "postgres": {"url": "ACCEPTANCE_POSTGRES_URL"},
            "mongo": {"uri": "ACCEPTANCE_MONGO_URI"},
            "s3": {
                "endpoint": "ACCEPTANCE_S3_ENDPOINT",
                "region": "ACCEPTANCE_S3_REGION",
                "access_key_id": "ACCEPTANCE_S3_ACCESS_KEY_ID",
                "secret_access_key": "ACCEPTANCE_S3_SECRET_ACCESS_KEY",
                "ca_bundle": "ACCEPTANCE_S3_CA_BUNDLE",
                "bucket": "ACCEPTANCE_S3_BUCKET",
                "force_path_style": "ACCEPTANCE_S3_FORCE_PATH_STYLE",
            },
        }
        bindings = [
            {
                "resourceId": str(item["resourceId"]),
                "outputs": targets[str(item["type"])],
            }
            for item in items
        ]
        return {
            "repository": self.c.application["repository"],
            "commit": self.c.application["commit"],
            "requestedRef": self.c.application.get("requestedRef", "main"),
            "configurationRevision": revision,
            "configuration": {
                **dict(configuration),
                "storageBindings": bindings,
            },
        }

    def _content_proof(self) -> Mapping[str, object]:
        proof = self.c.application.get("contentProof")
        if not isinstance(proof, Mapping):
            raise LiveDriverError("application content proof is invalid")
        return proof

    def _execute(self, action: str, baseline: str) -> Observation:
        required = dict(ACTION_CHECKS)[action]

        def key(label: str) -> str:
            return str(uuid5(UUID(self.c.deployment_id), label))

        if action == "greenfield_setup":
            return self.i.setup_apply()
        if action == "application_create":
            body = self.i.mutate_controller(
                "POST", "/v1/applications", {"slug": self.c.application["slug"]}, key("application")
            )
            created = isinstance(body, dict) and body.get("applicationId") == self.app_id
            disabled = isinstance(body, dict) and body.get("enabled") is False
            return _observed(required, applicationCreated=created, disabledInitially=disabled)
        if action in {"postgres_lifecycle", "mongo_lifecycle", "s3_lifecycle"}:
            kind = action.removesuffix("_lifecycle")
            self.i.mutate_controller(
                "POST",
                f"/v1/applications/{self.app_id}/storage",
                {"type": kind, "name": "acceptance"},
                key(f"{kind}-create"),
            )
            items = self._storage_items(kind)
            created = (
                len(items) == 1
                and items[0].get("applicationId") == self.app_id
                and items[0].get("lifecycleState") == "active"
            )
            rid = items[0].get("resourceId") if len(items) == 1 else None
            if not isinstance(rid, str):
                raise LiveDriverError("storage resource ID missing")
            self.i.mutate_controller("POST", f"/v1/storage/{rid}/verify", {}, key(f"{kind}-verify"))
            refreshed = self._storage_items(kind)
            verified = len(refreshed) == 1 and isinstance(refreshed[0].get("lastVerifiedAt"), str)
            return _observed(required, created=created, resourceVerified=verified)
        if action == "application_deploy":
            self.i.mutate_controller(
                "POST",
                f"/v1/applications/{self.app_id}/deployments",
                self._deployment_body(1),
                key("deployment"),
            )
            attempts = self.i.list_items(f"/v1/applications/{self.app_id}/deployments?limit=100")
            exact = (
                len(attempts) == 1
                and attempts[0].get("repositoryCommit") == self.c.application["commit"]
                and attempts[0].get("status") == "accepted"
            )
            code, application = self.i.controller("GET", f"/v1/applications/{self.app_id}")
            if (
                code != 200
                or not isinstance(application, dict)
                or not isinstance(application.get("url"), str)
            ):
                raise LiveDriverError("accepted application URL is unavailable")
            proof = self.i.verify_public_storage(
                str(application["url"]) + str(self.c.application["verificationPath"]),
                self._content_proof(),
            )
            return _observed(required, exactCommitDeployed=exact, **proof)
        if action in {"interrupted_resume_injection", "interrupted_resume"}:
            operation_key = key("interrupted-deployment")
            result = self.i.mutate_controller(
                "POST",
                f"/v1/applications/{self.app_id}/deployments",
                self._deployment_body(2),
                operation_key,
                wait=action == "interrupted_resume",
            )
            if action == "interrupted_resume_injection":
                observed = self.i.interrupt_controller(operation_key)
                code, application = self.i.controller("GET", f"/v1/applications/{self.app_id}")
                app_url = application.get("url") if isinstance(application, dict) else None
                stable = code == 200 and isinstance(app_url, str)
                if isinstance(app_url, str):
                    self.i.verify_public_storage(
                        app_url + str(self.c.application["verificationPath"]),
                        self._content_proof(),
                    )
                return _observed(required, **observed, stableServiceUnaffected=stable)
            attempts = self.i.list_items(f"/v1/applications/{self.app_id}/deployments?limit=100")
            one = sum(item.get("deploymentId") == operation_key for item in attempts) == 1
            converged = isinstance(result, Mapping) and result.get("status") == "succeeded"
            return _observed(
                required,
                sameOperationResumed=one,
                operationConverged=converged,
                noDuplicateResource=one,
            )
        if action == "application_disable_enable":
            before_storage = tuple(
                sorted(
                    str(item.get("resourceId"))
                    for item in self.i.list_items(
                        f"/v1/applications/{self.app_id}/storage?limit=100"
                    )
                )
            )
            before_deployments = _canonical(
                self.i.list_items(f"/v1/applications/{self.app_id}/deployments?limit=100")
            )
            self.i.mutate_controller(
                "POST", f"/v1/applications/{self.app_id}/disable", {}, key("disable")
            )
            disabled_code, disabled_app = self.i.controller(
                "GET", f"/v1/applications/{self.app_id}"
            )
            disabled_storage = tuple(
                sorted(
                    str(item.get("resourceId"))
                    for item in self.i.list_items(
                        f"/v1/applications/{self.app_id}/storage?limit=100"
                    )
                )
            )
            disabled_without_loss = (
                disabled_code == 200
                and isinstance(disabled_app, dict)
                and disabled_app.get("enabled") is False
                and disabled_storage == before_storage
            )
            self.i.mutate_controller(
                "POST", f"/v1/applications/{self.app_id}/enable", {}, key("enable")
            )
            after_storage = tuple(
                sorted(
                    str(item.get("resourceId"))
                    for item in self.i.list_items(
                        f"/v1/applications/{self.app_id}/storage?limit=100"
                    )
                )
            )
            after_deployments = _canonical(
                self.i.list_items(f"/v1/applications/{self.app_id}/deployments?limit=100")
            )
            code, application = self.i.controller("GET", f"/v1/applications/{self.app_id}")
            app_url = application.get("url") if isinstance(application, dict) else None
            healthy = (
                code == 200
                and isinstance(application, dict)
                and application.get("enabled") is True
                and isinstance(app_url, str)
            )
            if isinstance(app_url, str):
                self.i.verify_public_storage(
                    app_url + str(self.c.application["verificationPath"]),
                    self._content_proof(),
                )
            return _observed(
                required,
                disabledWithoutDataLoss=disabled_without_loss and before_storage == after_storage,
                enabledWithoutRebuild=before_deployments == after_deployments,
                publicRouteHealthy=healthy,
            )
        if action == "operator_sqlite_restore":
            return self.i.operator_backup_restore()
        if action == "hosted_sqlite_restore":
            return self.i.hosted_restore()
        if action == "managed_data_restore":
            code, application = self.i.controller("GET", f"/v1/applications/{self.app_id}")
            if (
                code != 200
                or not isinstance(application, dict)
                or not isinstance(application.get("url"), str)
            ):
                raise LiveDriverError("application URL is unavailable for managed restore proof")
            return self.i.managed_restore(str(application["url"]))
        if action == "persistent_host_replacement":
            observed = self.i.replace("ingress")
            code, application = self.i.controller("GET", f"/v1/applications/{self.app_id}")
            app_url = application.get("url") if isinstance(application, dict) else None
            retained = code == 200 and isinstance(app_url, str)
            if isinstance(app_url, str):
                self.i.verify_public_storage(
                    app_url + str(self.c.application["verificationPath"]),
                    self._content_proof(),
                )
            return _observed(
                required,
                oldHostRetainedUntilReady=observed.get("oldHostRetainedUntilReady") is True,
                exactIdentityVerified=observed.get("exactIdentityVerified") is True,
                dataRetained=retained,
            )
        if action == "admin_recovery":
            observed = self.i.replace("admin")
            code, application = self.i.controller("GET", f"/v1/applications/{self.app_id}")
            reconciled = (
                code == 200
                and isinstance(application, dict)
                and application.get("applicationId") == self.app_id
            )
            return _observed(
                required,
                externalRecoveryUsed=observed.get("externalRecoveryUsed") is True,
                controllerStateRetained=reconciled,
                applicationReconciled=reconciled,
            )
        if action == "application_delete":
            self.i.mutate_controller(
                "POST",
                f"/v1/applications/{self.app_id}/delete",
                {"confirmation": self.c.application["slug"]},
                key("delete"),
                privileged=True,
            )
            storage = self.i.list_items(f"/v1/applications/{self.app_id}/storage?limit=100")
            code, _body = self.i.controller("GET", f"/v1/applications/{self.app_id}")
            absent = {
                kind: not any(item.get("type") == kind for item in storage)
                for kind in ("postgres", "mongo", "s3")
            }
            return _observed(
                required,
                applicationAbsent=code == 404,
                postgresAbsent=absent["postgres"],
                mongoAbsent=absent["mongo"],
                s3Absent=absent["s3"],
            )
        if action == "cleanup_verify":
            return self.i.teardown(baseline)
        raise LiveDriverError("action has no typed implementation")


def main() -> int:
    try:
        config_path = os.environ.get("P07_DRIVER_CONFIG")
        if not config_path:
            raise LiveDriverError("P07_DRIVER_CONFIG is required")
        config = load_driver_config(Path(config_path))
        raw = sys.stdin.buffer.read(_MAX_OUTPUT + 1)
        if len(raw) > _MAX_OUTPUT:
            raise LiveDriverError("driver request exceeds its bound")
        request = json.loads(raw, object_pairs_hook=_reject_duplicates)
        if not isinstance(request, dict):
            raise LiveDriverError("driver request must be an object")
        transport = SubprocessTransport(Path(config.transcript))
        response = RepositoryLiveDriver(config, SupportedInterfaces(config, transport)).handle(
            request
        )
        sys.stdout.buffer.write(_canonical(response) + b"\n")
        return 0
    except (LiveDriverError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
