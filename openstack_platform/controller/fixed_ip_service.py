"""App-locked journal for dedicated retained worker primary IPv4 ports."""

from __future__ import annotations

import json
import sqlite3
import uuid as uuid_module
from pathlib import Path
from typing import Any

from .. import fixed_ip, openstack, runtime
from ..config import Config
from ..validation import ValidationError, uuid
from . import application_runtime as app
from . import database as db
from .service_support import HelperCaller, operation_deadline, wall_deadline


def get(connection: sqlite3.Connection, application_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT record_json FROM application_fixed_ports WHERE application_id = ?",
        (application_id,),
    ).fetchone()
    return None if row is None else json.loads(row["record_json"])


def _save(connection: sqlite3.Connection, record: dict[str, Any]) -> None:
    with db.transaction(connection):
        connection.execute(
            "INSERT INTO application_fixed_ports(application_id, port_id, record_json) "
            "VALUES (?, ?, ?) ON CONFLICT(application_id) DO UPDATE SET "
            "port_id=excluded.port_id, record_json=excluded.record_json",
            (record["application_id"], record["port_id"], json.dumps(record, sort_keys=True)),
        )


def _forget(connection: sqlite3.Connection, application_id: str) -> None:
    with db.transaction(connection):
        connection.execute(
            "DELETE FROM application_fixed_ports WHERE application_id = ?", (application_id,)
        )


def model(connection: sqlite3.Connection, application_id: str) -> dict[str, Any]:
    record = get(connection, application_id)
    if record is None:
        return dict(applicationId=application_id, enabled=False)
    return dict(
        applicationId=application_id,
        enabled=True,
        portId=record["port_id"],
        networkId=record["network_id"],
        subnetId=record["subnet_id"],
        address=record["address"],
        reservationId=record["request_id"],
        phase=record["phase"],
        workerSlotId=record["worker_slot_id"],
        workerCreatePending=record["worker_create_pending"],
        replacementPolicy="disabled-predecessor-absent",
        purpose="retained-primary-ipv4",
    )


def helper_identity(record: dict[str, Any]) -> dict[str, Any]:
    if record["phase"] != "reserved" or record["port_id"] is None:
        raise openstack.RecoveryRequired(
            "finish retained primary port reservation/release first", refs={}
        )
    return {
        key: value
        for key, value in record.items()
        if key not in {"phase", "worker_slot_id", "worker_create_pending"}
    }


def worker_helper(connection: sqlite3.Connection, caller: HelperCaller) -> HelperCaller:
    """Carry reservation identity through every slot create/read/cleanup/recovery.

    No feature row means byte-for-byte ordinary helper arguments. Slot ownership
    is derived from the bounded app slot set, never from slug or name prefixes.
    """

    def call(config: Config, action: str, args: Any, *, deadline: float) -> Any:
        selected = None
        if action in {
            "app.worker.create",
            "app.worker.observe",
            "app.worker.delete",
            "app.worker.capacity",
        }:
            slot = args["applicationId"]
            for row in connection.execute("SELECT record_json FROM application_fixed_ports"):
                record = json.loads(row["record_json"])
                owner = record["application_id"]
                if (
                    action == "app.worker.delete"
                    and slot == owner
                    and not args.get("single", False)
                ):
                    results = [
                        call(
                            config,
                            action,
                            {**args, "applicationId": identity, "single": True},
                            deadline=deadline,
                        )
                        for identity in (owner, *app.deployment_worker_ids(owner))
                    ]
                    if any(result.get("absent") is not True for result in results):
                        raise app.ApplicationError("bounded worker-slot cleanup was not confirmed")
                    return results[0]
                if slot == record["worker_slot_id"]:
                    current = db.get_application(connection, owner)
                    if current is None or current.slug != args["slug"]:
                        raise openstack.DriftError(
                            "retained port worker request belongs to another app"
                        )
                    selected = record
                    args = {**args, "retainedPort": helper_identity(record)}
                    break
        if selected is not None and action in {"app.worker.create", "app.worker.delete"}:
            if selected["worker_create_pending"]:
                # Zero after a timed-out Nova request is not permission to
                # create again, declare cleanup complete, or release the port.
                call(
                    config,
                    "app.worker.observe",
                    {"applicationId": args["applicationId"], "slug": args["slug"]},
                    deadline=deadline,
                )
                selected = get(connection, selected["application_id"])
                assert selected is not None
            if action == "app.worker.create":
                selected["worker_create_pending"] = True
                _save(connection, selected)
        result = caller(config, action, args, deadline=deadline)
        if selected is not None and selected["worker_create_pending"]:
            if result.get("serverId") is None or result.get("portId") != selected["port_id"]:
                raise openstack.RecoveryRequired(
                    "retained worker create outcome unresolved; exact server observation required",
                    refs={},
                )
            uuid(result["serverId"], field="retained worker server UUID")
            selected["worker_create_pending"] = False
            _save(connection, selected)
        return result

    return call


def bind_worker(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    slot_id: str,
    *,
    helper_caller: HelperCaller,
    deadline: float,
) -> None:
    """Journal generation use only after predecessor absence, never on reservation alone."""
    record = get(connection, application_id)
    if record is None:
        return
    helper_identity(record)
    if slot_id not in {application_id, *app.deployment_worker_ids(application_id)}:
        raise openstack.DriftError("retained port target is outside application worker slots")
    if record["worker_slot_id"] == slot_id:
        return  # Existing generation retry; helper proves exact attachment or absence.
    current = db.get_application(connection, application_id)
    assert current is not None
    for identity in (application_id, *app.deployment_worker_ids(application_id)):
        result = helper_caller(
            config,
            "app.worker.observe",
            {"applicationId": identity, "slug": current.slug},
            deadline=deadline,
        )
        if result.get("absent") is not True:
            raise ValidationError("retained primary port requires every predecessor worker absent")
    record["worker_slot_id"] = slot_id
    _save(connection, record)


def require_maintenance(connection: sqlite3.Connection, application_id: str) -> None:
    record = get(connection, application_id)
    if record is None:
        return
    helper_identity(record)
    current = db.get_application(connection, application_id)
    if current is None or current.desired_running:
        raise ValidationError(
            "retained primary port replacement requires disable first; rolling overlap is unsupported"
        )


def _recover(
    connection: sqlite3.Connection, record: dict[str, Any], provider: fixed_ip.Provider
) -> None:
    matches = [
        value
        for value in provider.inventory(name=record["name"])
        if value.get("description") == record["description"]
    ]
    if len(matches) != 1:
        raise openstack.RecoveryRequired(
            "retained port allocation outcome unresolved; retry exact reservation, never allocate again",
            refs={},
        )
    _accept(connection, record, matches[0], provider)


def _accept(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    value: dict[str, Any],
    provider: fixed_ip.Provider,
) -> None:
    candidate = {**record, "port_id": uuid(value.get("id"), field="retained port UUID")}
    provider.check(value, candidate)
    record.update(port_id=candidate["port_id"], phase="reserved")
    _save(connection, record)


def release_locked(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    *,
    deadline: float,
    provider: fixed_ip.Provider | None = None,
) -> None:
    record = get(connection, application_id)
    if record is None:
        return
    if (
        record["application_id"] != application_id
        or record["project_id"] != config.platform.project_id
        or record["description"]
        != fixed_ip.marker(config.platform, application_id, record["request_id"])
        or record["name"] != f"{config.platform.prefix}-app-{application_id}-primary-v4"
    ):
        raise openstack.DriftError("retained port journal ownership drifted")
    if record["worker_create_pending"]:
        raise openstack.RecoveryRequired(
            "resolve retained worker create before releasing its primary port", refs={}
        )
    provider = provider or fixed_ip.Provider(config.platform, deadline=deadline)
    provider.verify()
    if record["phase"] == "allocating":
        _recover(connection, record, provider)
    matches = provider.inventory(identifier=record["port_id"])
    if not matches:
        if record["phase"] != "deleting":
            raise openstack.DriftError("retained port disappeared without deletion intent")
        _forget(connection, application_id)
        return
    provider.check(matches[0], record)  # Unbound only; never detach another VM.
    record["phase"] = "deleting"
    _save(connection, record)
    provider.check(provider.show(record["port_id"]), record)
    provider.delete(record["port_id"])
    if provider.inventory(identifier=record["port_id"]):
        raise openstack.RecoveryRequired("retained port deletion was not confirmed", refs={})
    _forget(connection, application_id)


class FixedIPService:
    def __init__(
        self, connection: sqlite3.Connection, config: Config, state_directory: Path
    ) -> None:
        self.connection, self.config, self.state_directory = connection, config, state_directory

    def read(self, application_id: str) -> dict[str, Any]:
        deadline = operation_deadline(self.config)
        with runtime.lock(self.state_directory, f"app-{application_id}", deadline=deadline):
            record = get(self.connection, application_id)
            result = model(self.connection, application_id)
            if record is None or record["phase"] != "reserved":
                return result
            provider = fixed_ip.Provider(self.config.platform, deadline=deadline)
            value = provider.show(record["port_id"])
            device = value.get("device_id")
            if not isinstance(device, str):
                raise openstack.DriftError("retained port attachment evidence is malformed")
            provider.check(value, record, device_id=device)
            if device:
                slot = uuid(record["worker_slot_id"], field="retained worker slot UUID")
                current = db.get_application(self.connection, application_id)
                assert current is not None
                server = provider.json(("server", "show", uuid(device, field="worker server UUID")))
                prefix = openstack._metadata_prefix(self.config.platform)
                expected = {
                    f"{prefix}_managed_by": "platform",
                    f"{prefix}_application_id": slot,
                    f"{prefix}_application_slug": current.slug,
                }
                if (
                    not isinstance(server, dict)
                    or openstack._provider_uuid(server.get("id"), field="worker server UUID")
                    != device
                    or openstack._provider_uuid(
                        server.get("project_id", server.get("tenant_id")),
                        field="worker project UUID",
                    )
                    != record["project_id"]
                    or server.get("name")
                    != f"{self.config.platform.prefix}-worker-{slot.replace('-', '')[:12]}"
                    or not isinstance(server.get("properties"), dict)
                    or any(server["properties"].get(key) != item for key, item in expected.items())
                ):
                    raise openstack.DriftError("retained port attached worker ownership drifted")
                ports = provider.json(("port", "list", "--server", device))
                if (
                    not isinstance(ports, list)
                    or len(ports) != 1
                    or openstack._provider_uuid(
                        openstack._field(ports[0], "id"), field="worker port UUID"
                    )
                    != record["port_id"]
                ):
                    raise openstack.DriftError("retained worker must have exactly its primary port")
            return {
                **result,
                "serverId": device or None,
                "attachment": "attached" if device else "detached",
            }

    def plan(
        self, application_id: str, network_id: str, subnet_id: str, address: str
    ) -> dict[str, Any]:
        if (
            db.get_application(self.connection, uuid(application_id, field="application UUID"))
            is None
        ):
            raise ValidationError("application does not exist")
        provider = fixed_ip.Provider(self.config.platform, deadline=operation_deadline(self.config))
        return dict(applicationId=application_id, **provider.plan(network_id, subnet_id, address))

    def mutate(
        self,
        application_id: str,
        *,
        action: str,
        network_id: str | None = None,
        subnet_id: str | None = None,
        address: str | None = None,
        request_id: str | None = None,
        provider: fixed_ip.Provider | None = None,
    ) -> dict[str, Any]:
        application_id = uuid(application_id, field="application UUID")
        if action not in {"reserve", "release"}:
            raise ValidationError("invalid retained fixed IP action")
        if action == "reserve":
            network_id = uuid(network_id, field="worker network UUID")
            subnet_id = uuid(subnet_id, field="worker subnet UUID")
            address = fixed_ip.ipv4(address)
        elif any(value is not None for value in (network_id, subnet_id, address)):
            raise ValidationError("release accepts no allocation fields")
        request_id = uuid(request_id or str(uuid_module.uuid4()), field="operation UUID")
        deadline = operation_deadline(self.config)
        scope = f"app-{application_id}"
        refs = dict(
            application_id=application_id,
            action=action,
            network_id=network_id,
            subnet_id=subnet_id,
            address=address,
        )
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            if db.get_application(self.connection, application_id) is None:
                raise ValidationError("application does not exist")
            operation = db.get_operation(self.connection, request_id)
            if operation is not None:
                if (
                    operation.kind != "app.fixed-ip"
                    or operation.scope != scope
                    or operation.refs != refs
                ):
                    raise db.IdempotencyConflictError("retained IP request identity differs")
                if operation.status == "succeeded":
                    return model(self.connection, application_id)
                if operation.status == "failed":
                    raise db.DatabaseError("retained IP request already failed")
                db.renew_operation_deadline(self.connection, request_id, wall_deadline(deadline))
            else:
                db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind="app.fixed-ip",
                    scope=scope,
                    phase="validated",
                    deadline_at=wall_deadline(deadline),
                    refs=refs,
                )
            provider = provider or fixed_ip.Provider(self.config.platform, deadline=deadline)
            try:
                from .public_ip_service import get as get_floating

                if action == "reserve":
                    if get_floating(self.connection, application_id) is not None:
                        raise ValidationError(
                            "floating and retained fixed IP reservations are mutually exclusive"
                        )
                    record = get(self.connection, application_id)
                    if record is not None and record["request_id"] != request_id:
                        raise ValidationError(
                            "release the existing retained port before replacing it"
                        )
                    if record is None:
                        assert (
                            network_id is not None and subnet_id is not None and address is not None
                        )
                        plan = provider.plan(network_id, subnet_id, address)
                        record = dict(
                            application_id=application_id,
                            request_id=request_id,
                            project_id=self.config.platform.project_id,
                            network_id=network_id,
                            subnet_id=subnet_id,
                            address=address,
                            security_group_id=plan["securityGroupId"],
                            port_id=None,
                            name=f"{self.config.platform.prefix}-app-{application_id}-primary-v4",
                            description=fixed_ip.marker(
                                self.config.platform, application_id, request_id
                            ),
                            phase="allocating",
                            worker_slot_id=None,
                            worker_create_pending=False,
                        )
                        _save(self.connection, record)  # Durable before the only create.
                        result = provider.create(record)
                        _accept(
                            self.connection,
                            record,
                            provider.show(uuid(result.get("id"), field="created port UUID")),
                            provider,
                        )
                    elif record["phase"] == "allocating":
                        _recover(self.connection, record, provider)
                    else:
                        provider.check(provider.show(record["port_id"]), record)
                else:
                    release_locked(
                        self.connection,
                        self.config,
                        application_id,
                        deadline=deadline,
                        provider=provider,
                    )
                db.mark_succeeded(self.connection, request_id)
            except Exception as error:
                record = get(self.connection, application_id)
                if record is None or (action == "reserve" and record["request_id"] != request_id):
                    db.mark_failed(self.connection, request_id, error, cleanup_state="not_required")
                else:
                    db.mark_recovery_required(self.connection, request_id, error)
                raise
        return model(self.connection, application_id)
