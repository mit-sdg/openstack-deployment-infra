"""Root-owned storage instance manager; one authenticated, fixed JSON endpoint.

Controller journals own feature intent. This manager persists instance identity and
capacity before commands, reconciles assignments on replay, and never removes data
on failed creation, stop, quota change, reboot or migration.
"""

from __future__ import annotations

import argparse
import fcntl
import hmac
import ipaddress
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError

from . import durable
from .instance_contract import (
    CONNECTION_BUDGET,
    CPU_WEIGHT,
    IO_WEIGHT,
    MAINTENANCE_MEMORY,
    MEMORY_BUDGET,
    MIB,
    TASKS_MAX,
    TRANSITION_MEMORY_BUDGET,
    CapacityError,
    hard_quota,
    validate_limits,
)
from .validation import ValidationError, uuid

ENDPOINT = "/platform/instances"
PORT_MIN = 30000
PORT_MAX = 30999
POSTGRES_SOCKET_DIRECTORY = "/var/run/postgresql"
Run = Callable[..., Any]


_COPY_CANCEL: ContextVar[Callable[[], bool] | None] = ContextVar("copy_cancel", default=None)


def command_failure_reason(error: subprocess.CalledProcessError) -> str:
    """Classify provider output without journaling commands, credentials or JS."""
    output = b""
    for value in (error.stdout, error.stderr):
        if isinstance(value, str):
            value = value.encode()
        if isinstance(value, bytes):
            output += value[:65536].lower()
    for needle, reason in (
        (b"read-only file system", "read_only_filesystem"),
        (b"permission denied", "permission_denied"),
        (b"eacces", "permission_denied"),
        (b"cannot find module", "runtime_dependency_missing"),
        (b"no such file or directory", "required_path_missing"),
        (b"authentication failed", "authentication_failed"),
        (b"certificate", "tls_certificate_error"),
        (b"connection refused", "connection_refused"),
        (b"unknown option", "unsupported_option"),
    ):
        if needle in output:
            return reason
    return "command_exit_nonzero"


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    cancel = _COPY_CANCEL.get()
    if cancel is None or argv[0] not in {"pg_dump", "pg_restore", "mongodump", "mongorestore"}:
        return subprocess.run(
            tuple(argv),
            check=kwargs.pop("check", True),
            capture_output=True,
            timeout=kwargs.pop("timeout", 60),
            **kwargs,
        )
    checked = kwargs.pop("check", True)
    deadline = time.monotonic() + kwargs.pop("timeout", 60)
    process = subprocess.Popen(
        tuple(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        **kwargs,
    )
    try:
        while True:
            if cancel() or time.monotonic() >= deadline:
                raise TimeoutError("instance copy cancelled or deadline expired")
            try:
                out, err = process.communicate(
                    timeout=min(1, max(0.01, deadline - time.monotonic()))
                )
                break
            except subprocess.TimeoutExpired:
                continue
        if checked and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, argv)
        return subprocess.CompletedProcess(argv, process.returncode, out, err)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()


def unit_limits(quotas: Mapping[str, int]) -> str:
    return (
        "[Service]\n"
        f"MemoryMax={quotas['memoryBytes']}\nMemorySwapMax=0\n"
        f"CPUQuota={quotas['cpuMillicores'] / 10:g}%\nCPUWeight={CPU_WEIGHT}\n"
        f"IOWeight={IO_WEIGHT}\nTasksMax={TASKS_MAX}\n"
    )


def database_command(config: Mapping[str, Any], namespace: str) -> list[str]:
    quotas = config["quotas"]
    memory = quotas["memoryBytes"]
    pki = f"/run/{namespace}-pki"
    if config["type"] == "postgres":
        settings = {
            "port": config["port"],
            "listen_addresses": "*",
            "max_connections": quotas["connections"] + 5,
            "superuser_reserved_connections": 5,
            "shared_buffers": f"{memory // 4 // MIB}MB",
            "work_mem": "2MB",
            "maintenance_work_mem": f"{min(memory // 16 // MIB, 64)}MB",
            "autovacuum_max_workers": 1,
            # Bound normal WAL growth inside the 25% disk headroom. WAL may
            # exceed this during a transaction, so migration also reserves room.
            "max_wal_size": "256MB",
            "min_wal_size": "80MB",
            "ssl": "on",
            "ssl_cert_file": f"{pki}/storage.pem",
            "ssl_key_file": f"{pki}/storage-key.pem",
            "ssl_ca_file": f"{pki}/internal-ca.pem",
            "ssl_min_protocol_version": "TLSv1.2",
            "hba_file": f"{pki}/pg_hba.conf",
            # Images disagree on the default (/run vs /var/run); use the tmpfs.
            "unix_socket_directories": POSTGRES_SOCKET_DIRECTORY,
        }
        return [
            "postgres",
            *(part for key, value in settings.items() for part in ("-c", f"{key}={value}")),
        ]
    return [
        "mongod",
        "--auth",
        "--port",
        str(config["port"]),
        "--bind_ip_all",
        "--maxConns",
        str(quotas["connections"] + 10),
        "--wiredTigerCacheSizeGB",
        str(max(memory / 4 / 1024**3, 0.25)),
        "--slowms",
        "100",
        "--tlsMode",
        "requireTLS",
        "--tlsCertificateKeyFile",
        f"{pki}/mongodb-combined.pem",
        "--tlsCAFile",
        f"{pki}/internal-ca.pem",
        "--tlsAllowConnectionsWithoutCertificates",
        *(
            ["--setParameter", "enableLocalhostAuthBypass=false"]
            if config.get("adminInitialized")
            else []
        ),
    ]


class Manager:
    def __init__(
        self,
        platform: Mapping[str, Any],
        *,
        command: Run = run,
        units: Path = Path("/run/systemd/system"),
        memory_budget: int = TRANSITION_MEMORY_BUDGET,
        mongo_connect: Callable[..., Any] = MongoClient,
    ):
        self.platform = platform
        self.namespace = str(platform["namespace"])
        self.data = Path(platform["paths"]["data"])
        self.root = self.data / "instances"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.command = command
        self.mongo_connect = mongo_connect
        self.units = units
        self.memory_budget = memory_budget
        self.copies: dict[str, tuple[threading.Event, threading.Event, Callable[[], bool]]] = {}
        self.copy_lock = threading.Lock()

    def directory(self, identifier: str) -> Path:
        return self.root / uuid(identifier, field="instance ID")

    def read(self, identifier: str) -> dict[str, Any]:
        value = json.loads((self.directory(identifier) / "config.json").read_text())
        if not isinstance(value, dict) or value.get("instanceId") != identifier:
            raise ValidationError("instance identity is invalid")
        return value

    def configs(self) -> list[dict[str, Any]]:
        return [
            self.read(path.name)
            for path in self.root.iterdir()
            if path.is_dir()
            and not path.name.endswith(".deleting")
            and (path / "config.json").is_file()
        ]

    @contextmanager
    def locked(self) -> Iterator[None]:
        with (self.root / "manager.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def sync_root(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def deleting_record(self, identifier: str) -> Path:
        return self.root / (identifier + ".deleting.json")

    def cleanup_deleted(self, identifier: str) -> None:
        trash = self.root / (identifier + ".deleting")
        if trash.exists():
            self.command(("rm", "-rf", "--", str(trash)))
        # Keep the independent port/project reservation until all data is gone,
        # even when recursive deletion removed config.json before interruption.
        with self.locked():
            self.deleting_record(identifier).unlink(missing_ok=True)
            self.sync_root()

    def save(self, value: Mapping[str, Any]) -> None:
        path = self.directory(value["instanceId"])
        path.mkdir(mode=0o700, exist_ok=True)
        durable.atomic_write(
            path / "config.json",
            (json.dumps(dict(value), sort_keys=True) + "\n").encode(),
            mode=0o600,
            maximum_bytes=65536,
        )

    def capacity(
        self, identifier: str, target: dict[str, int], *, maintenance: bool = False
    ) -> None:
        others = [item for item in self.configs() if item["instanceId"] != identifier]
        if sum(
            max(
                item["quotas"]["memoryBytes"],
                item.get("acceptedQuotas", item["quotas"])["memoryBytes"],
                item.get("reservedQuotas", item["quotas"])["memoryBytes"],
            )
            for item in others
        ) + target["memoryBytes"] > self.memory_budget + (MAINTENANCE_MEMORY if maintenance else 0):
            raise CapacityError(
                "MEMORY_BUDGET_EXCEEDED", "instance memory caps exceed the configured host budget"
            )
        if (
            sum(
                max(
                    item["quotas"]["connections"],
                    item.get("acceptedQuotas", item["quotas"])["connections"],
                    item.get("reservedQuotas", item["quotas"])["connections"],
                )
                for item in others
            )
            + target["connections"]
            > CONNECTION_BUDGET
        ):
            raise CapacityError(
                "CONNECTION_BUDGET_EXCEEDED",
                "instance connection caps exceed the 2000-slot host budget",
            )

    def reserve_disk(self, values: Any) -> None:
        from .instance_contract import GIB

        if (
            not isinstance(values, dict)
            or set(values) != {"databaseBytes", "garageBytes"}
            or any(
                isinstance(number, bool) or not isinstance(number, int) or number < 0
                for number in values.values()
            )
        ):
            raise ValidationError("disk reservations are invalid")
        path = self.root / "disk-budget.json"
        previous = json.loads(path.read_text()) if path.exists() else {"garageBytes": 0}
        garage = max(previous["garageBytes"], values["garageBytes"] + 16 * GIB)
        volume = os.statvfs(self.data)
        if (
            values["databaseBytes"] + garage + 181 * GIB
            > volume.f_blocks * volume.f_frsize * 85 // 100
        ):
            raise CapacityError(
                "DISK_BUDGET_EXCEEDED", "hard disk reservations exceed 85% of the data volume"
            )
        initialized = previous.get("initialized", False)
        durable.atomic_write(
            path,
            json.dumps({"garageBytes": garage, "initialized": initialized}).encode(),
            mode=0o600,
            maximum_bytes=65536,
        )
        projects = (
            ("object-storage", 9001, garage),
            ("registry", 9002, 100 * GIB),
            ("postgres", 9003, 32 * GIB),
            ("mongodb", 9004, 32 * GIB),
            ("instance-migrations", 9005, 16 * GIB),
        )
        if not initialized:
            # Check every live shared directory before assigning any project
            # cap. A capacity mistake must not crash un-migrated applications.
            for name, _project, size in projects:
                directory = self.data / name
                directory.mkdir(mode=0o700, exist_ok=True)
                output = self.command(("du", "--summarize", "--block-size=1", "--", str(directory)))
                used = int(output.stdout.split()[0])
                if used + 384 * MIB > size:
                    raise CapacityError(
                        "DISK_BUDGET_EXCEEDED",
                        "shared directory exceeds initial project cap; enlarge its reservation before rollout",
                    )
        for name, project, size in projects:
            directory = self.data / name
            directory.mkdir(mode=0o700, exist_ok=True)
            if not initialized:
                self.command(
                    (
                        "xfs_quota",
                        "-x",
                        "-c",
                        f"project -s -p {directory} {project}",
                        str(self.data),
                    )
                )
            self.command(
                (
                    "xfs_quota",
                    "-x",
                    "-c",
                    f"limit -p bhard={size // 1024}k {project}",
                    str(self.data),
                )
            )

        if not initialized:
            durable.atomic_write(
                path,
                json.dumps({"garageBytes": garage, "initialized": True}).encode(),
                mode=0o600,
                maximum_bytes=65536,
            )

    def unit(self, identifier: str) -> str:
        return f"{self.namespace}-database@{identifier}.service"

    def check_reduction(self, config: dict[str, Any], size: int) -> None:
        output = self.command(
            (
                "du",
                "--summarize",
                "--block-size=1",
                "--",
                str(self.directory(config["instanceId"]) / "data"),
            )
        )
        physical = int(output.stdout.split()[0])
        margin = (384 if config["type"] == "postgres" else 128) * MIB
        if hard_quota(size) < physical + margin:
            raise CapacityError(
                "SIZE_BELOW_USAGE", "hard quota would fall below physical data plus headroom"
            )

    def apply(self, config: dict[str, Any], *, restart: bool) -> None:
        identifier = config["instanceId"]
        directory = self.directory(identifier)
        data = directory / "data"
        data.mkdir(mode=0o700, exist_ok=True)
        pki = directory / "pki"
        pki.mkdir(mode=0o755, exist_ok=True)
        for name in ("storage.pem", "storage-key.pem", "internal-ca.pem", "mongodb-combined.pem"):
            self.command(
                (
                    "install",
                    "-m",
                    "0400" if "key" in name or "combined" in name else "0444",
                    "-o",
                    "999",
                    "-g",
                    "999",
                    f"/etc/{self.namespace}/pki/{name}",
                    str(pki / name),
                )
            )
        durable.atomic_write(
            pki / "pg_hba.conf",
            b"local all all trust\nhostssl all all 0.0.0.0/0 scram-sha-256\nhostssl all all ::/0 scram-sha-256\nhostnossl all all 0.0.0.0/0 reject\nhostnossl all all ::/0 reject\n",
            mode=0o644,
            maximum_bytes=65536,
        )
        self.command(("chown", "999:999", str(data)))
        # project -s recursively assigns project IDs and enables inheritance;
        # -p makes the durable config the path registry, independent of /etc/projects.
        if not config.get("projectInitialized", False):
            self.command(
                (
                    "xfs_quota",
                    "-x",
                    "-c",
                    f"project -s -p {data} {config['projectId']}",
                    str(self.data),
                )
            )
            config["projectInitialized"] = True
            self.save(config)
        self.command(
            (
                "xfs_quota",
                "-x",
                "-c",
                f"limit -p bhard={hard_quota(config['quotas']['sizeBytes']) // 1024}k ihard={max(4096, config['quotas']['sizeBytes'] // 32768)} {config['projectId']}",
                str(self.data),
            )
        )
        dropin = self.units / (self.unit(identifier) + ".d")
        dropin.mkdir(parents=True, exist_ok=True)
        durable.atomic_write(
            dropin / "limits.conf",
            unit_limits(
                {
                    **config["quotas"],
                    "memoryBytes": max(
                        config["quotas"]["memoryBytes"], config.get("restoreMemoryBytes", 0)
                    ),
                }
            ).encode(),
            mode=0o644,
            maximum_bytes=65536,
        )
        durable.atomic_write(
            dropin / "shutdown.conf",
            (
                "[Service]\nKillMode=mixed\nKillSignal="
                + ("SIGINT" if config["type"] == "postgres" else "SIGTERM")
                + "\nTimeoutStopSec=180\n"
            ).encode(),
            mode=0o644,
            maximum_bytes=65536,
        )
        self.firewall()
        self.command(("systemctl", "daemon-reload"))
        limits = {
            **config["quotas"],
            "memoryBytes": max(
                config["quotas"]["memoryBytes"], config.get("restoreMemoryBytes", 0)
            ),
        }
        self.command(
            (
                "systemctl",
                "set-property",
                "--runtime",
                self.unit(identifier),
                f"MemoryMax={limits['memoryBytes']}",
                "MemorySwapMax=0",
                f"CPUQuota={limits['cpuMillicores'] / 10:g}%",
                f"CPUWeight={CPU_WEIGHT}",
                f"IOWeight={IO_WEIGHT}",
                f"TasksMax={TASKS_MAX}",
            )
        )
        if config.get("desiredRunning", True):
            self.command(("systemctl", "restart" if restart else "start", self.unit(identifier)))

    def firewall(self) -> None:
        # The normal Nix firewall opens this port range; this earlier chain drops
        # every connection except a resource's allowlist and fixed administration.
        table = self.namespace.replace("-", "_") + "_instances"
        fixed = [
            self.platform["addresses"]["admin"],
            self.platform["addresses"]["storage"],
            "127.0.0.1",
        ]
        rules = [
            f"table inet {table} {{ chain ingress {{ type filter hook input priority -50; policy accept;"
        ]
        for config in self.configs():
            addresses = [
                str(ipaddress.ip_address(value)) for value in [*fixed, *config["allowIps"]]
            ]
            v4 = sorted({value for value in addresses if ":" not in value})
            v6 = sorted({value for value in addresses if ":" in value})
            for family, values in (("ip", v4), ("ip6", v6)):
                if values:
                    rules.append(
                        f"tcp dport {config['port']} {family} saddr {{ {', '.join(values)} }} accept"
                    )
        ports = sorted({config["port"] for config in self.configs()})
        if ports:
            # Apply the allowlist to established sessions too: revoking a worker
            # must close its access immediately. Reserved ports cannot be client
            # ephemeral ports; unrelated host replies never enter these rules.
            rules.append(f"tcp dport {{ {', '.join(map(str, ports))} }} drop")
        rules.append("}")
        fresh = [
            config["port"]
            for config in self.configs()
            if config["type"] == "mongo" and not config.get("adminInitialized")
        ]
        if fresh:
            rules.append("chain bootstrap { type filter hook output priority -50; policy accept;")
            rules.append(f"tcp dport {{ {', '.join(map(str, fresh))} }} meta skuid != 0 drop")
            rules.append("}")
        rules.append("}")
        # Create the table once, then atomically replace its chains. The manager
        # lock serializes all callers; nft -f is an atomic kernel transaction.
        self.command(("nft", "add", "table", "inet", table), check=False)
        script = f"flush table inet {table}\n" + "\n".join(rules)
        self.command(("nft", "-f", "-"), input=script.encode())

    @contextmanager
    def instance_locked(self, identifier: str) -> Iterator[None]:
        with (self.root / (identifier + ".lock")).open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def remove(self, identifier: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if set(args) != {"action", "instanceId", "deleteData"} or args["deleteData"] is not True:
            raise ValidationError("instance deletion requires explicit deleteData")
        with self.locked():
            try:
                config = self.read(identifier)
            except FileNotFoundError:
                config = None
            if config is not None:
                config["desiredRunning"] = False
                self.save(config)
        # Neither process shutdown nor recursive deletion holds the global lock.
        # The per-instance lock excludes same-resource mutations throughout.
        self.command(("systemctl", "disable", "--now", self.unit(identifier)))
        # ExecStopPost removes the container in the database unit, outside this
        # manager's strict filesystem namespace. Explicit deletion owns data.
        with self.locked():
            if config is not None:
                durable.atomic_write(
                    self.deleting_record(identifier),
                    json.dumps({"port": config["port"], "projectId": config["projectId"]}).encode(),
                    mode=0o600,
                    maximum_bytes=65536,
                )
                self.directory(identifier).rename(self.root / (identifier + ".deleting"))
                self.sync_root()
            self.firewall()
        self.cleanup_deleted(identifier)
        self.command(("systemctl", "revert", self.unit(identifier)))
        return {"confirmedAbsent": True}

    def dispatch(
        self, args: Mapping[str, Any], *, disconnected: Callable[[], bool] = lambda: False
    ) -> dict[str, Any]:
        if args.get("action") == "ping":
            if set(args) != {"action"}:
                raise ValidationError("instance ping fields are invalid")
            return {"available": True}
        if args.get("action") in {"list", "backup-inventory"}:
            return self._dispatch(args)
        identifier = uuid(args.get("instanceId"), field="instance ID")
        with self.copy_lock:
            active = self.copies.get(identifier)
        if active is not None:
            cancel, done, gone = active
            if args.get("action") == "cancel-copy" or gone():
                cancel.set()
                if not done.wait(10):
                    raise CapacityError(
                        "INSTANCE_COPY_IN_PROGRESS", "copy cancellation is pending; retry"
                    )
            elif args.get("action") not in {"observe", "credentials"}:
                raise CapacityError(
                    "INSTANCE_COPY_IN_PROGRESS", "instance copy is active; retry or abort"
                )
        if args.get("action") == "cancel-copy":
            if set(args) != {"action", "instanceId"}:
                raise ValidationError("copy cancellation fields are invalid")
            return {"cancelled": True}
        if args.get("action") in {"observe", "credentials"}:
            return self._dispatch(args)
        with self.instance_locked(identifier):
            if args.get("action") == "remove":
                return self.remove(identifier, args)
            if args.get("action") == "restore-begin":
                if (
                    set(args) != {"action", "instanceId", "backupSha256"}
                    or not isinstance(args["backupSha256"], str)
                    or len(args["backupSha256"]) != 64
                ):
                    raise ValidationError("restore checkpoint fields are invalid")
                with self.locked():
                    config = self.read(identifier)
                    if config.get("restoredBackupSha256") == args["backupSha256"]:
                        return {"alreadyRestored": True}
                    if not config.get("restorePending"):
                        config["pendingAllowIps"] = config["allowIps"]
                    config["allowIps"] = []
                    config["restorePending"] = args["backupSha256"]
                    limits = dict(config["quotas"])
                    if config["type"] == "mongo":
                        limits["memoryBytes"] = max(limits["memoryBytes"], 1024**3)
                    self.capacity(identifier, limits, maintenance=True)
                    config["restoreMemoryBytes"] = limits["memoryBytes"]
                    config["reservedQuotas"] = {
                        **config["reservedQuotas"],
                        "memoryBytes": limits["memoryBytes"],
                    }
                    self.save(config)
                    self.firewall()
                self.command(
                    (
                        "systemctl",
                        "set-property",
                        "--runtime",
                        self.unit(identifier),
                        f"MemoryMax={limits['memoryBytes']}",
                    )
                )
                return {"alreadyRestored": False}
            if args.get("action") == "restore-finish":
                if set(args) != {"action", "instanceId", "migrationState"} or args[
                    "migrationState"
                ] not in {None, "switched"}:
                    raise ValidationError("restore completion fields are invalid")
                with self.locked():
                    config = self.read(identifier)
                    if not config.get("restorePending"):
                        raise ValidationError("restore checkpoint is absent")
                    # Keep the restore lease and closed worker firewall until
                    # the normal memory cap is confirmed. A lost response is
                    # replayable and never advertises an unfinished restore.
                    completed_hash = config["restorePending"]
                self.command(
                    (
                        "systemctl",
                        "set-property",
                        "--runtime",
                        self.unit(identifier),
                        f"MemoryMax={config['quotas']['memoryBytes']}",
                    )
                )
                with self.locked():
                    config["restoredBackupSha256"] = completed_hash
                    config.pop("restorePending")
                    config.pop("restoreMemoryBytes", None)
                    config["reservedQuotas"] = config["acceptedQuotas"]
                    config["allowIps"] = config.pop("pendingAllowIps", [])
                    config["migrationState"] = args["migrationState"]
                    self.save(config)
                    self.firewall()
                return self.observe(config)
            if args.get("action") == "copy":
                from .storage_migration import copy_instance

                config = self.read(identifier)
                # Long copies serialize only this resource. Other apps' network,
                # lifecycle and usage calls retain access to the global lock.
                cancel, done = threading.Event(), threading.Event()
                with self.copy_lock:
                    self.copies[identifier] = (cancel, done, disconnected)
                marker = _COPY_CANCEL.set(lambda: cancel.is_set() or disconnected())
                try:
                    copy_instance(self, config, args)
                    return self.observe(config)
                finally:
                    _COPY_CANCEL.reset(marker)
                    with self.copy_lock:
                        self.copies.pop(identifier, None)
                    done.set()
            result = self._dispatch(args)
            if args.get("action") in {"create", "restore-create"}:
                config = self.read(identifier)
                if config["type"] == "mongo":
                    self.bootstrap_mongo(config)
            return result

    def _dispatch(self, args: Mapping[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        if action in {"list", "backup-inventory"}:
            if set(args) != {"action"}:
                raise ValidationError("instance list fields are invalid")
            with self.locked():
                return {
                    "items": [
                        (
                            {**self.observe(config), "applicationId": config["applicationId"]}
                            if action == "backup-inventory"
                            else self.observe(config)
                        )
                        for config in self.configs()
                    ],
                    "instanceMemoryBudgetBytes": self.memory_budget,
                }
        identifier = uuid(args.get("instanceId"), field="instance ID")
        if action not in {
            "create",
            "restore-create",
            "start",
            "stop",
            "limits",
            "observe",
            "credentials",
            "allow",
            "seal",
            "freeze",
            "reserve",
            "abort-copy",
        }:
            raise ValidationError("instance action is invalid")
        with self.locked():
            if action == "reserve":
                if set(args) != {"action", "instanceId", "reservations"}:
                    raise ValidationError("disk reservation fields are invalid")
                self.reserve_disk(args["reservations"])
                return {"reserved": True}
            if action in {"create", "restore-create"}:
                required = {
                    "action",
                    "instanceId",
                    "applicationId",
                    "type",
                    "quotas",
                    "allowIps",
                    "reservations",
                }
                if action == "restore-create":
                    required.add("port")
                    port = args.get("port")
                    if (
                        not isinstance(port, int)
                        or isinstance(port, bool)
                        or not PORT_MIN <= port <= PORT_MAX
                    ):
                        raise ValidationError("restore port is invalid")
                if set(args) != required or args["type"] not in {"postgres", "mongo"}:
                    raise ValidationError("instance create fields are invalid")
                if (
                    self.deleting_record(identifier).exists()
                    or (self.root / (identifier + ".deleting")).exists()
                ):
                    raise ValidationError(
                        "finish instance deletion replay before recreating its identity"
                    )
                owner = uuid(args["applicationId"], field="application ID")
                target = validate_limits(args["quotas"])
                self.capacity(identifier, target)
                self.reserve_disk(args["reservations"])
                try:
                    config = self.read(identifier)
                    if (
                        config["applicationId"] != owner
                        or config["type"] != args["type"]
                        or (action == "create" and config["quotas"] != target)
                        or (action == "restore-create" and config["port"] != args["port"])
                    ):
                        raise ValidationError(
                            "instance creation intent conflicts with durable identity"
                        )
                except FileNotFoundError:
                    occupied = {item["port"] for item in self.configs()}
                    occupied.update(
                        json.loads(path.read_text())["port"]
                        for path in self.root.glob("*.deleting.json")
                    )
                    port = (
                        args["port"]
                        if action == "restore-create"
                        else next(
                            (
                                value
                                for value in range(PORT_MIN, PORT_MAX + 1)
                                if value not in occupied
                            ),
                            None,
                        )
                    )
                    if port in occupied:
                        raise ValidationError("restore port belongs to another instance") from None
                    if port is None:
                        raise ValidationError("instance ports are exhausted") from None
                    config = {
                        "instanceId": identifier,
                        "applicationId": owner,
                        "type": args["type"],
                        "port": port,
                        "projectId": 10000 + port - 30000,
                        "quotas": target,
                        "acceptedQuotas": target,
                        "reservedQuotas": target,
                        "allowIps": [],
                        "migrationState": None,
                        "desiredRunning": True,
                    }
                    self.save(config)
                    secret = self.directory(identifier) / "admin-password"
                    durable.atomic_write(
                        secret, secrets.token_urlsafe(32).encode(), mode=0o400, maximum_bytes=65536
                    )
                    self.command(("chown", "999:999", str(secret)))
                secret = self.directory(identifier) / "admin-password"
                if not secret.exists():
                    data = self.directory(identifier) / "data"
                    if data.exists() and any(data.iterdir()):
                        raise ValidationError("instance credential is missing for existing data")
                    durable.atomic_write(
                        secret, secrets.token_urlsafe(32).encode(), mode=0o400, maximum_bytes=65536
                    )
                    self.command(("chown", "999:999", str(secret)))
                if action == "create":
                    config["desiredRunning"] = True
                if action == "create" or not config.get("allowIps"):
                    config["allowIps"] = self.addresses(args["allowIps"])
                self.save(config)
                self.apply(config, restart=False)
            else:
                try:
                    config = self.read(identifier)
                except FileNotFoundError:
                    if action == "credentials":
                        return {"absent": True}
                    raise
                if action == "limits":
                    if set(args) != {"action", "instanceId", "quotas", "reservations"}:
                        raise ValidationError("instance limit fields are invalid")
                    target = validate_limits(args["quotas"])
                    if target["sizeBytes"] < config["quotas"]["sizeBytes"]:
                        self.check_reduction(config, target["sizeBytes"])
                    self.capacity(identifier, target)
                    self.reserve_disk(args["reservations"])
                    if target["sizeBytes"] < config["quotas"]["sizeBytes"]:
                        # Reject mistakes while serving; quiesce only an admitted
                        # reduction, then account for writes since the live check.
                        self.command(("systemctl", "stop", self.unit(identifier)))
                        try:
                            self.check_reduction(config, target["sizeBytes"])
                        except Exception:
                            if config.get("desiredRunning", True):
                                self.command(("systemctl", "start", self.unit(identifier)))
                            raise
                    changed = config.get("acceptedQuotas") != target
                    old_reserved = config.get("reservedQuotas", config["quotas"])
                    config["reservedQuotas"] = {
                        key: max(old_reserved[key], target[key]) for key in target
                    }
                    config["quotas"] = target
                    self.save(config)  # reserve before any provider assignment
                    if changed:
                        restart = any(
                            config.get("acceptedQuotas", {}).get(field) != target[field]
                            for field in ("connections", "memoryBytes")
                        )
                        self.apply(config, restart=restart)
                        config["acceptedQuotas"] = target
                        config["reservedQuotas"] = target
                        self.save(config)
                elif action == "allow":
                    if set(args) != {
                        "action",
                        "instanceId",
                        "applicationId",
                        "addresses",
                        "mode",
                    } or args["mode"] not in {"add", "remove", "replace"}:
                        raise ValidationError("instance network fields are invalid")
                    if config["applicationId"] != args["applicationId"]:
                        raise ValidationError("instance network owner does not match")
                    supplied = set(self.addresses(args["addresses"]))
                    field = "pendingAllowIps" if config.get("restorePending") else "allowIps"
                    previous = set(config[field])
                    config[field] = sorted(
                        supplied
                        if args["mode"] == "replace"
                        else previous | supplied
                        if args["mode"] == "add"
                        else previous - supplied
                    )
                    self.save(config)
                    self.firewall()
                elif action in {"start", "stop"}:
                    if set(args) != {"action", "instanceId"}:
                        raise ValidationError("instance lifecycle fields are invalid")
                    config["desiredRunning"] = action == "start"
                    self.save(config)
                    self.command(("systemctl", action, self.unit(identifier)))
                elif action == "credentials":
                    if set(args) != {"action", "instanceId"}:
                        raise ValidationError("instance credential fields are invalid")
                    return {
                        "port": config["port"],
                        "adminPassword": (
                            self.directory(identifier) / "admin-password"
                        ).read_text(),
                    }
                elif action == "freeze":
                    if set(args) != {"action", "instanceId", "sourceRole"} or args[
                        "sourceRole"
                    ] not in {"readWrite", "platform_size_blocked"}:
                        raise ValidationError("source role checkpoint is invalid")
                    if config.get("sourceRole") is None:
                        config["sourceRole"] = args["sourceRole"]
                        self.save(config)
                elif action == "abort-copy":
                    if set(args) != {"action", "instanceId", "operationId"}:
                        raise ValidationError("abort checkpoint fields are invalid")
                    config["abortedOperationId"] = uuid(args["operationId"], field="operation ID")
                    config["migrationState"] = "aborted"
                    config["allowIps"] = []
                    self.save(config)
                    self.firewall()
                elif action == "seal":
                    if (
                        args.get("operationId") is not None
                        and config.get("abortedOperationId") == args["operationId"]
                    ):
                        raise ValidationError("migration was aborted; start a fresh intent")
                    if set(args) not in (
                        {"action", "instanceId"},
                        {"action", "instanceId", "operationId"},
                    ):
                        raise ValidationError("instance seal fields are invalid")
                    if config.get("migrationState") not in {"verified", "switched"}:
                        raise ValidationError("instance copy has not been verified")
                    config["migrationState"] = "switched"
                    self.save(config)
                elif set(args) != {"action", "instanceId"}:
                    raise ValidationError("instance observation fields are invalid")
            return self.observe(config)

    @staticmethod
    def addresses(values: Any) -> list[str]:
        if (
            not isinstance(values, list)
            or len(values) > 8
            or any(not isinstance(value, str) for value in values)
        ):
            raise ValidationError("instance allowlist is invalid")
        try:
            return sorted({str(ipaddress.ip_address(value)) for value in values})
        except ValueError:
            raise ValidationError("instance allowlist address is invalid") from None

    def observe(self, config: dict[str, Any]) -> dict[str, Any]:
        result = {key: config[key] for key in ("instanceId", "port", "quotas", "migrationState")}
        result["sourceRole"] = config.get("sourceRole")
        result["type"] = config["type"]
        result["hardQuotaBytes"] = hard_quota(config["quotas"]["sizeBytes"])
        output = self.command(
            ("systemctl", "show", self.unit(config["instanceId"]), "-p", "ControlGroup", "--value")
        )
        value = output.stdout.decode().strip()
        if not value.startswith("/"):
            result.update(instanceMemoryBytes=None, cpuTimeMilliseconds=None)
            return result
        group = (Path("/sys/fs/cgroup") / value.lstrip("/")).resolve()
        if not group.is_relative_to("/sys/fs/cgroup"):
            raise ValidationError("instance cgroup is invalid")
        try:
            result["instanceMemoryBytes"] = int((group / "memory.current").read_text())
            cpu = dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())
            result["cpuTimeMilliseconds"] = int(cpu["usage_usec"]) // 1000
        except (FileNotFoundError, ValueError):
            result.update(instanceMemoryBytes=None, cpuTimeMilliseconds=None)
        return result

    def restore(self) -> None:
        with self.locked():
            for config in self.configs():
                try:
                    self.apply(config, restart=False)

                except Exception:
                    logging.warning(
                        "instance %s restore requires reconciliation", config["instanceId"]
                    )
            self.firewall()

    def bootstrap_mongo(self, config: dict[str, Any]) -> None:
        if config.get("adminInitialized"):
            return
        password = (self.directory(config["instanceId"]) / "admin-password").read_text()
        options = dict(
            host="127.0.0.1",
            port=config["port"],
            tls=True,
            tlsCAFile=f"/etc/{self.namespace}/pki/internal-ca.pem",
            # The leaf names the inventory host, not localhost. Only this fixed
            # loopback bootstrap relaxes hostname matching; CA validation stays.
            tlsAllowInvalidHostnames=True,
            serverSelectionTimeoutMS=1000,
            connectTimeoutMS=1000,
            socketTimeoutMS=5000,
        )

        def authenticate() -> bool:
            try:
                with self.mongo_connect(
                    **options, username="platform_admin", password=password, authSource="admin"
                ) as client:
                    client.admin.command("ping")
                return True
            except OperationFailure as error:
                if error.code != 18:
                    raise
                return False

        deadline = time.monotonic() + 60
        logged_failure = False
        while True:
            try:
                if not authenticate():
                    # The root manager's loopback socket passes the bootstrap
                    # nft rule. No password enters argv, scripts or a shell.
                    with self.mongo_connect(**options) as client:
                        try:
                            client.admin.command(
                                "createUser",
                                "platform_admin",
                                pwd=password,
                                roles=[{"role": "root", "db": "admin"}],
                            )
                        except OperationFailure as error:
                            if error.code != 51003:  # Replay after a lost create reply.
                                raise
                    if not authenticate():
                        raise OperationFailure("bootstrap authentication failed", code=18)
                break
            except PyMongoError as error:
                expired = time.monotonic() >= deadline
                if not logged_failure or expired:
                    logging.warning(
                        "Mongo bootstrap instance=%s reason=%s",
                        config["instanceId"],
                        type(error).__name__,
                    )
                    logged_failure = True
                if expired:
                    raise ValidationError(
                        "MongoDB admin initialization requires reconciliation"
                    ) from None
                time.sleep(0.25)
        config["adminInitialized"] = True
        with self.locked():
            self.save(config)
            self.firewall()

    def launch(self, identifier: str) -> None:
        config = self.read(identifier)
        directory = self.directory(identifier)
        name = f"{self.namespace}-db-{identifier}"
        kind = config["type"]
        argv = [
            "podman",
            "run",
            "--rm",
            "--replace",
            "--name",
            name,
            "--network=host",
            "--cgroups=disabled",
            # Journal storage is bounded; a database cannot fill /var/lib with
            # an unbounded Podman log file outside its XFS project.
            "--log-driver=journald",
            "--read-only",
            # These bounded tmpfs mounts are charged to the unit memory cap;
            # database files can grow only inside the XFS project mount.
            "--tmpfs=/tmp:rw,size=64m,nosuid,nodev,noexec",
            # Podman 5.8 accepts mode here, but rejects explicit uid/gid.
            # The sticky directory lets the database UID create its socket.
            f"--tmpfs={POSTGRES_SOCKET_DIRECTORY}:rw,size=16m,mode=1777,nosuid,nodev,noexec",
            "--security-opt=no-new-privileges",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--cap-add=SETUID",
            "--cap-add=SETGID",
            "--cap-add=DAC_OVERRIDE",
            "--volume",
            f"{directory}/data:{'/var/lib/postgresql/data' if kind == 'postgres' else '/data/db'}",
            "--volume",
            f"{directory}/admin-password:/run/secrets/admin-password:ro",
            "--volume",
            f"{directory}/pki:/run/{self.namespace}-pki:ro",
        ]
        if kind == "postgres":
            for key, value in (
                ("USER", "platform_admin"),
                ("PASSWORD_FILE", "/run/secrets/admin-password"),
                ("DB", "platform"),
                ("INITDB_ARGS", "--auth-host=scram-sha-256"),
            ):
                argv += ["--env", f"POSTGRES_{key}={value}"]
        else:
            # The image init script hardcodes port 27017. Bypass it on host
            # networking and initialize the admin at the allocated port instead.
            argv += ["--entrypoint", "mongod", "--user", "999:999"]
        argv += [
            self.platform["containers"]["postgres" if kind == "postgres" else "mongodb"],
            *(
                database_command(config, self.namespace)[1:]
                if kind == "mongo"
                else database_command(config, self.namespace)
            ),
        ]
        os.execvp(argv[0], argv)


def http_handler(manager: Manager, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def send_error(
            self, code: int, message: str | None = None, explain: str | None = None
        ) -> None:
            # Authentication/framing failures precede JSON parsing. Never log
            # request headers, URI, payloads or caller-supplied reason strings.
            logging.warning("storage instance HTTP rejection status=%d code=HTTP_%d", code, code)
            super().send_error(code, message, explain)

        def do_POST(self) -> None:
            if self.path != ENDPOINT:
                self.send_error(404)
                return
            if not hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()
            ):
                self.send_error(403)
                return
            length = self.headers.get("Content-Length", "")
            if (
                not length.isdigit()
                or not 0 < int(length) <= 65536
                or self.headers.get("Transfer-Encoding")
            ):
                self.send_error(400)
                return
            try:
                args = json.loads(self.rfile.read(int(length)))
                if not isinstance(args, dict):
                    raise ValidationError("instance request must be an object")

                def disconnected() -> bool:
                    try:
                        return bool(
                            self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
                        )
                    except BlockingIOError:
                        return False
                    except OSError:
                        return True

                result = manager.dispatch(args, disconnected=disconnected)
                status = 200
            except (ValidationError, FileNotFoundError, json.JSONDecodeError) as error:
                result = {
                    "error": {
                        "code": error.code
                        if isinstance(error, CapacityError)
                        else "INVALID_INSTANCE_REQUEST",
                        "summary": str(error)
                        if isinstance(error, ValidationError)
                        else "instance request must be valid JSON"
                        if isinstance(error, json.JSONDecodeError)
                        else "instance is absent",
                    }
                }
                status = 400
                logging.warning(
                    "storage instance request rejected status=%d code=%s reason=%s",
                    status,
                    result["error"]["code"],
                    type(error).__name__,
                )
            except Exception as error:
                result = {
                    "error": {
                        "code": "INSTANCE_OPERATION_FAILED",
                        "summary": "instance operation requires reconciliation",
                    }
                }
                status = 503
                reason = (
                    command_failure_reason(error)
                    if isinstance(error, subprocess.CalledProcessError)
                    else type(error).__name__
                )
                logging.warning(
                    "storage instance request failed status=%d code=%s reason=%s",
                    status,
                    result["error"]["code"],
                    reason,
                )
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: Any) -> None:
            pass

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-instance")
    parser.add_argument("--memory-budget-bytes", type=int, default=TRANSITION_MEMORY_BUDGET)
    args = parser.parse_args()
    if not 1 <= args.memory_budget_bytes <= MEMORY_BUDGET:
        parser.error("memory budget exceeds the host allowance")
    manager = Manager(json.loads(args.config.read_text()), memory_budget=args.memory_budget_bytes)
    if args.run_instance:
        manager.launch(uuid(args.run_instance, field="instance ID"))
        return 0
    garage_config = tomllib.loads(
        (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "garage-config").read_text()
    )
    manager.restore()
    for config in manager.configs():
        if config["type"] == "mongo" and config.get("desiredRunning", True):
            try:
                manager.bootstrap_mongo(config)
            except Exception:
                logging.warning("Mongo bootstrap requires reconciliation")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 19002), http_handler(manager, garage_config["admin"]["admin_token"])
    )
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
