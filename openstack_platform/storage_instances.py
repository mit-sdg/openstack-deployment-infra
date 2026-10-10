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
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import durable
from .instance_contract import (
    CONNECTION_BUDGET,
    CPU_WEIGHT,
    IO_WEIGHT,
    MEMORY_BUDGET,
    MIB,
    TASKS_MAX,
    CapacityError,
    hard_quota,
    validate_limits,
)
from .validation import ValidationError, uuid

ENDPOINT = "/platform/instances"
PORT_MIN = 30000
PORT_MAX = 30999
Run = Callable[..., Any]


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        tuple(argv),
        check=kwargs.pop("check", True),
        capture_output=True,
        timeout=kwargs.pop("timeout", 60),
        **kwargs,
    )


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
    ]


class Manager:
    def __init__(
        self,
        platform: Mapping[str, Any],
        *,
        command: Run = run,
        units: Path = Path("/run/systemd/system"),
    ):
        self.platform = platform
        self.namespace = str(platform["namespace"])
        self.data = Path(platform["paths"]["data"])
        self.root = self.data / "instances"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.command = command
        self.units = units

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

    def save(self, value: Mapping[str, Any]) -> None:
        path = self.directory(value["instanceId"])
        path.mkdir(mode=0o700, exist_ok=True)
        durable.atomic_write(
            path / "config.json",
            (json.dumps(dict(value), sort_keys=True) + "\n").encode(),
            mode=0o600,
            maximum_bytes=65536,
        )

    def capacity(self, identifier: str, target: dict[str, int]) -> None:
        others = [item for item in self.configs() if item["instanceId"] != identifier]
        if (
            sum(
                max(
                    item["quotas"]["memoryBytes"],
                    item.get("acceptedQuotas", item["quotas"])["memoryBytes"],
                    item.get("reservedQuotas", item["quotas"])["memoryBytes"],
                )
                for item in others
            )
            + target["memoryBytes"]
            > MEMORY_BUDGET
        ):
            raise CapacityError(
                "MEMORY_BUDGET_EXCEEDED", "instance memory caps exceed the 50 GiB host budget"
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
        for name, project, size in (
            ("object-storage", 9001, garage),
            ("registry", 9002, 100 * GIB),
            ("postgres", 9003, 32 * GIB),
            ("mongodb", 9004, 32 * GIB),
            ("instance-migrations", 9005, 16 * GIB),
        ):
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
            unit_limits(config["quotas"]).encode(),
            mode=0o644,
            maximum_bytes=65536,
        )
        self.firewall()
        self.command(("systemctl", "daemon-reload"))
        limits = config["quotas"]
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
        rules.append("} }")
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

    def dispatch(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if args.get("action") == "list":
            return self._dispatch(args)
        identifier = uuid(args.get("instanceId"), field="instance ID")
        with self.instance_locked(identifier):
            if args.get("action") == "copy":
                from .storage_migration import copy_instance

                config = self.read(identifier)
                # Long copies serialize only this resource. Other apps' network,
                # lifecycle and usage calls retain access to the global lock.
                copy_instance(self, config, args)
                return self.observe(config)
            return self._dispatch(args)

    def _dispatch(self, args: Mapping[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        if action == "list":
            if set(args) != {"action"}:
                raise ValidationError("instance list fields are invalid")
            with self.locked():
                return {"items": [self.observe(config) for config in self.configs()]}
        identifier = uuid(args.get("instanceId"), field="instance ID")
        if action not in {
            "create",
            "start",
            "stop",
            "remove",
            "limits",
            "observe",
            "credentials",
            "allow",
            "copy",
            "seal",
            "freeze",
            "reserve",
        }:
            raise ValidationError("instance action is invalid")
        with self.locked():
            if action == "reserve":
                if set(args) != {"action", "instanceId", "reservations"}:
                    raise ValidationError("disk reservation fields are invalid")
                self.reserve_disk(args["reservations"])
                return {"reserved": True}
            if action == "create":
                if set(args) != {
                    "action",
                    "instanceId",
                    "applicationId",
                    "type",
                    "quotas",
                    "allowIps",
                    "reservations",
                } or args["type"] not in {"postgres", "mongo"}:
                    raise ValidationError("instance create fields are invalid")
                owner = uuid(args["applicationId"], field="application ID")
                target = validate_limits(args["quotas"])
                self.capacity(identifier, target)
                self.reserve_disk(args["reservations"])
                try:
                    config = self.read(identifier)
                    if (
                        config["applicationId"] != owner
                        or config["type"] != args["type"]
                        or config["quotas"] != target
                    ):
                        raise ValidationError(
                            "instance creation intent conflicts with durable identity"
                        )
                except FileNotFoundError:
                    occupied = {item["port"] for item in self.configs()}
                    port = next(
                        (value for value in range(PORT_MIN, PORT_MAX + 1) if value not in occupied),
                        None,
                    )
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
                config["allowIps"] = self.addresses(args["allowIps"])
                self.save(config)
                self.apply(config, restart=False)
                if config["type"] == "mongo":
                    self.bootstrap_mongo(config)
            else:
                try:
                    config = self.read(identifier)
                except FileNotFoundError:
                    if action == "remove" and args.get("deleteData") is True:
                        trash = self.root / (identifier + ".deleting")
                        if trash.exists():
                            self.command(("rm", "-rf", "--", str(trash)))
                        self.firewall()
                        return {"confirmedAbsent": True}
                    if action == "credentials":
                        return {"absent": True}
                    raise
                if action == "limits":
                    if set(args) != {"action", "instanceId", "quotas", "reservations"}:
                        raise ValidationError("instance limit fields are invalid")
                    target = validate_limits(args["quotas"])
                    self.capacity(identifier, target)
                    self.reserve_disk(args["reservations"])
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
                    previous = set(config["allowIps"])
                    config["allowIps"] = sorted(
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
                elif action == "remove":
                    if (
                        set(args) != {"action", "instanceId", "deleteData"}
                        or args["deleteData"] is not True
                    ):
                        raise ValidationError("instance deletion requires explicit deleteData")
                    self.command(("systemctl", "disable", "--now", self.unit(identifier)))
                    trash = self.root / (identifier + ".deleting")
                    self.directory(identifier).rename(trash)
                    descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    self.command(("rm", "-rf", "--", str(trash)))
                    self.firewall()
                    return {"confirmedAbsent": True}
                elif action == "credentials":
                    if set(args) != {"action", "instanceId"}:
                        raise ValidationError("instance credential fields are invalid")
                    return {
                        "port": config["port"],
                        "adminPassword": (
                            self.directory(identifier) / "admin-password"
                        ).read_text(),
                    }
                elif action == "copy":
                    from .storage_migration import copy_instance

                    copy_instance(self, config, args)
                elif action == "freeze":
                    if set(args) != {"action", "instanceId", "sourceRole"} or args[
                        "sourceRole"
                    ] not in {"readWrite", "platform_size_blocked"}:
                        raise ValidationError("source role checkpoint is invalid")
                    if config.get("sourceRole") is None:
                        config["sourceRole"] = args["sourceRole"]
                        self.save(config)
                elif action == "seal":
                    if set(args) != {"action", "instanceId"}:
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
        if not isinstance(values, list) or len(values) > 8:
            raise ValidationError("instance allowlist is invalid")
        return sorted({str(ipaddress.ip_address(value)) for value in values})

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
                    if config["type"] == "mongo" and config.get("desiredRunning", True):
                        self.bootstrap_mongo(config)
                except Exception:
                    logging.warning(
                        "instance %s restore requires reconciliation", config["instanceId"]
                    )
            self.firewall()

    def bootstrap_mongo(self, config: dict[str, Any]) -> None:
        if config.get("adminInitialized"):
            return
        password = (self.directory(config["instanceId"]) / "admin-password").read_text()
        # stdin keeps the password out of argv, logs and container metadata.
        script = (
            "const a=db.getSiblingDB('admin'); const p=" + json.dumps(password) + ";"
            "let ok=false; try {ok=!!a.auth('platform_admin',p);} catch(e) {}"
            "if(!ok) a.createUser({user:'platform_admin',pwd:p,roles:[{role:'root',db:'admin'}]});"
            "if(!a.auth('platform_admin',p)) throw Error('admin authentication failed');"
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                self.command(
                    (
                        "podman",
                        "exec",
                        "--interactive",
                        f"{self.namespace}-db-{config['instanceId']}",
                        "mongosh",
                        "--quiet",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(config["port"]),
                        "--tls",
                        "--tlsAllowInvalidHostnames",
                        "--tlsCAFile",
                        f"/run/{self.namespace}-pki/internal-ca.pem",
                    ),
                    input=script.encode(),
                    timeout=10,
                )
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise ValidationError(
                        "MongoDB admin initialization requires reconciliation"
                    ) from None
                time.sleep(0.25)
        config["adminInitialized"] = True
        self.save(config)

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
            "--tmpfs=/var/run/postgresql:rw,size=16m,uid=999,gid=999,mode=0775",
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
                result = manager.dispatch(args)
                status = 200
            except (ValidationError, FileNotFoundError) as error:
                result = {
                    "error": {
                        "code": error.code
                        if isinstance(error, CapacityError)
                        else "INVALID_INSTANCE_REQUEST",
                        "summary": str(error)
                        if isinstance(error, ValidationError)
                        else "instance is absent",
                    }
                }
                status = 400
            except Exception:
                result = {
                    "error": {
                        "code": "INSTANCE_OPERATION_FAILED",
                        "summary": "instance operation requires reconciliation",
                    }
                }
                status = 503
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
    args = parser.parse_args()
    manager = Manager(json.loads(args.config.read_text()))
    if args.run_instance:
        manager.launch(uuid(args.run_instance, field="instance ID"))
        return 0
    config = tomllib.loads(
        (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "garage-config").read_text()
    )
    manager.restore()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 19002), http_handler(manager, config["admin"]["admin_token"])
    )
    server.daemon_threads = True
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
