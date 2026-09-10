"""Staff-only, app-scoped stable outbound IPv4 reservation and handover.

The reservation survives worker slots and disabled apps. Provider calls never run
inside a SQLite transaction. All entry points use the existing app operation lock;
internal acceptance/deletion hooks must be called while holding that lock.
"""

from __future__ import annotations

import json
import sqlite3
import uuid as uuid_module
from pathlib import Path
from typing import Any

from .. import floating_ip, openstack, runtime
from ..config import Config
from ..validation import ValidationError, uuid
from . import application_runtime as app
from . import database as db
from .service_support import operation_deadline, wall_deadline


def get(connection: sqlite3.Connection, application_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT record_json FROM application_floating_ips WHERE application_id = ?",
        (uuid(application_id, field="application UUID"),),
    ).fetchone()
    return None if row is None else json.loads(row["record_json"])


def _save(connection: sqlite3.Connection, application_id: str, record: dict[str, Any]) -> None:
    with db.transaction(connection):
        connection.execute(
            "INSERT INTO application_floating_ips(application_id, floating_ip_id, record_json) "
            "VALUES (?, ?, ?) ON CONFLICT(application_id) DO UPDATE SET "
            "floating_ip_id = excluded.floating_ip_id, record_json = excluded.record_json",
            (application_id, record["floating_ip_id"], json.dumps(record, sort_keys=True)),
        )


def _forget(connection: sqlite3.Connection, application_id: str) -> None:
    with db.transaction(connection):
        connection.execute(
            "DELETE FROM application_floating_ips WHERE application_id = ?", (application_id,)
        )


def model(connection: sqlite3.Connection, application_id: str) -> dict[str, Any]:
    record = get(connection, application_id)
    if record is None:
        return {"applicationId": application_id, "enabled": False}
    return {
        "applicationId": application_id,
        "enabled": True,
        "floatingIpId": record["floating_ip_id"],
        "address": record["address"],
        "externalNetworkId": record["network_id"],
        "phase": record["phase"],
        "reservationId": record["request_id"],
        "allocationMarker": record["description"] if record["allocated"] else None,
        "ownership": "allocated" if record["allocated"] else "supplied",
        "portId": (record["port"] or {}).get("port_id"),
        "pendingPortId": (record["pending_port"] or {}).get("port_id"),
        "ingressPolicy": "unsupported",
        "purpose": "stable-outbound-ipv4",
    }


def _target(
    connection: sqlite3.Connection, config: Config, application_id: str
) -> dict[str, Any] | None:
    application = db.get_application(connection, application_id)
    deployment = db.get_deployment(connection, application_id)
    if application is None or deployment is None:
        return None
    # Maintenance can stop the accepted process while retaining its exact VM
    # and attached address. Desired process state is not worker absence: keep
    # validating that target so reconciliation cannot strand enable/disable.
    # Only a fully cleared disabled runtime has no target. Partial identities
    # must pass the same strict checks below, never become an unbound reservation.
    if not application.desired_running and all(
        value is None
        for value in (
            application.worker_server_id,
            application.worker_server_name,
            application.worker_port_id,
            application.worker_port_name,
        )
    ):
        return None
    slot = app.nomad_placement_id(deployment.nomad_job)
    if slot not in {application_id, *app.deployment_worker_ids(application_id)}:
        raise openstack.DriftError("accepted worker slot does not belong to this application")
    server_name = f"{config.platform.prefix}-worker-{slot.replace('-', '')[:12]}"
    if (
        application.worker_server_name != server_name
        or application.worker_port_name != f"{server_name}-v4"
    ):
        raise openstack.DriftError("accepted worker names do not match the owned slot")
    return dict(
        slot_id=slot,
        slug=application.slug,
        server_id=uuid(application.worker_server_id, field="accepted worker server UUID"),
        server_name=server_name,
        port_id=uuid(application.worker_port_id, field="accepted worker port UUID"),
        port_name=f"{server_name}-v4",
    )


def _record_identity(record: dict[str, Any], config: Config) -> None:
    if record["project_id"] != config.platform.project_id:
        raise openstack.DriftError("floating IP reservation belongs to a different project")
    uuid(record["network_id"], field="recorded external network UUID")
    if record["floating_ip_id"] is not None:
        uuid(record["floating_ip_id"], field="recorded floating IP UUID")
        floating_ip.ipv4(record["address"])


def _adopt_result(
    connection: sqlite3.Connection,
    application_id: str,
    record: dict[str, Any],
    value: dict[str, Any],
    provider: floating_ip.Provider,
) -> None:
    identifier = uuid(value.get("id"), field="floating IP UUID")
    record.update(
        floating_ip_id=identifier, address=floating_ip.ipv4(value.get("floating_ip_address"))
    )
    provider.check(value, record)
    if value.get("port_id") is not None or value.get("fixed_ip_address") is not None:
        raise openstack.DriftError("new or supplied floating IP must be unassociated")
    record["phase"] = "reserved"
    _save(connection, application_id, record)


def _recover_allocation(
    connection: sqlite3.Connection,
    application_id: str,
    record: dict[str, Any],
    provider: floating_ip.Provider,
) -> None:
    matches = [
        value for value in provider.inventory() if value.get("description") == record["description"]
    ]
    if len(matches) != 1:
        # A timed-out create can still commit later. Zero is NOT permission to
        # create again, delete by network/name, or silently forget the intent.
        raise openstack.RecoveryRequired(
            "allocation outcome unresolved; inspect exact reservation marker before recovery",
            refs={},
        )
    _adopt_result(connection, application_id, record, matches[0], provider)


def _association(value: dict[str, Any], target: dict[str, Any] | None) -> bool:
    if target is None:
        return value.get("port_id") is None and value.get("fixed_ip_address") is None
    return bool(
        value.get("port_id") == target["port_id"]
        and value.get("fixed_ip_address") == target["fixed_ip"]
    )


def reconcile_accepted(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    *,
    deadline: float,
    provider: floating_ip.Provider | None = None,
) -> None:
    """After durable healthy acceptance, move/verify before predecessor cleanup.

    An unaccepted candidate never gets this address. This is deliberately a
    forward-only handover, not a floating-IP rollback of a failed candidate.
    """
    record = get(connection, application_id)
    if record is None:
        return  # Default path does not even contact Neutron.
    _record_identity(record, config)
    provider = provider or floating_ip.Provider(config.platform, deadline=deadline)
    provider.verify()
    if record["phase"] == "allocating":
        _recover_allocation(connection, application_id, record, provider)
    if record["phase"] not in {"reserved", "active", "assigning"}:
        raise openstack.RecoveryRequired(
            "floating IP release must finish before deployment", refs={}
        )
    target = _target(connection, config, application_id)
    value = provider.show(record["floating_ip_id"])
    provider.check(value, record)
    if target is None:
        if record["port"] is not None or not _association(value, None):
            raise openstack.DriftError("unbound reservation unexpectedly has an association")
        return
    target["fixed_ip"] = provider.port(target)
    old = record["port"]
    pending = record["pending_port"]
    if pending is not None and pending != target:
        raise openstack.DriftError("accepted worker differs from journaled floating IP target")
    if old is not None:
        provider.port(old)  # Also prove exact predecessor ownership on retries.
    if not _association(value, old) and not (pending is not None and _association(value, pending)):
        raise openstack.DriftError(
            "floating IP association drifted outside the recorded transition"
        )
    if old == target and pending is None:
        if value.get("status") != "ACTIVE" or not value.get("router_id"):
            raise openstack.RecoveryRequired(
                "floating IP router activation was not confirmed", refs={}
            )
        return
    record.update(phase="assigning", pending_port=target)
    _save(connection, application_id, record)
    if not _association(value, target):
        # Reobserve immediately before the mutation; Neutron cannot make this
        # compare atomic. Exclusive project mutation authority is a prerequisite.
        value = provider.show(record["floating_ip_id"])
        provider.check(value, record)
        if not _association(value, old):
            raise openstack.DriftError("floating IP association changed before reassignment")
        provider.assign(record["floating_ip_id"], target)
    value = provider.show(record["floating_ip_id"])
    provider.check(value, record)
    provider.port(target)
    if (
        not _association(value, target)
        or value.get("status") != "ACTIVE"
        or not value.get("router_id")
    ):
        raise openstack.RecoveryRequired(
            "floating IP reassociation and router activation were not confirmed", refs={}
        )
    record.update(phase="active", port=target, pending_port=None)
    _save(connection, application_id, record)


def release_locked(
    connection: sqlite3.Connection,
    config: Config,
    application_id: str,
    *,
    deadline: float,
    provider: floating_ip.Provider | None = None,
    reserve_only: bool = False,
) -> None:
    """Detach supplied IPs; delete only addresses allocated by this reservation."""
    record = get(connection, application_id)
    if record is None:
        return
    _record_identity(record, config)
    provider = provider or floating_ip.Provider(config.platform, deadline=deadline)
    provider.verify()
    if record["phase"] == "allocating":
        _recover_allocation(connection, application_id, record, provider)
    if record["pending_port"] is not None:
        raise openstack.RecoveryRequired("finish floating IP handover before release", refs={})
    matches = [value for value in provider.inventory() if value["id"] == record["floating_ip_id"]]
    if not matches:
        if record["allocated"] and record["phase"] == "deleting":
            _forget(connection, application_id)
            return
        raise openstack.DriftError("recorded floating IP is missing without deletion intent")
    value = matches[0]
    provider.check(value, record)
    old = record["port"]
    if old is not None:
        provider.port(old)
    if not _association(value, old) and not (
        record["phase"] in {"releasing", "detached", "deleting"} and _association(value, None)
    ):
        raise openstack.DriftError("refusing release of a floating IP with unrelated association")
    if not _association(value, None):
        record["phase"] = "releasing"
        _save(connection, application_id, record)
        value = provider.show(record["floating_ip_id"])
        provider.check(value, record)
        if not _association(value, old):
            raise openstack.DriftError("floating IP association changed before release")
        provider.detach(record["floating_ip_id"])
        value = provider.show(record["floating_ip_id"])
        provider.check(value, record)
        if not _association(value, None):
            raise openstack.RecoveryRequired("floating IP detach was not confirmed", refs={})
    record.update(phase="reserved" if reserve_only else "detached", port=None)
    _save(connection, application_id, record)
    if reserve_only:
        return
    if record["allocated"]:
        record["phase"] = "deleting"
        _save(connection, application_id, record)
        value = provider.show(record["floating_ip_id"])
        provider.check(value, record)
        if not _association(value, None):
            raise openstack.DriftError("floating IP acquired an association before deletion")
        provider.delete(record["floating_ip_id"])
        if any(value["id"] == record["floating_ip_id"] for value in provider.inventory()):
            raise openstack.RecoveryRequired("floating IP deletion was not confirmed", refs={})
    _forget(connection, application_id)


class PublicIPService:
    def __init__(
        self, connection: sqlite3.Connection, config: Config, state_directory: Path
    ) -> None:
        self.connection = connection
        self.config = config
        self.state_directory = state_directory

    def plan(self, application_id: str, network_id: str) -> dict[str, Any]:
        if (
            db.get_application(self.connection, uuid(application_id, field="application UUID"))
            is None
        ):
            raise ValidationError("application does not exist")
        provider = floating_ip.Provider(
            self.config.platform, deadline=operation_deadline(self.config)
        )
        return {
            "applicationId": application_id,
            **provider.plan(uuid(network_id, field="external network UUID")),
        }

    def mutate(
        self,
        application_id: str,
        *,
        action: str,
        network_id: str | None = None,
        floating_ip_id: str | None = None,
        request_id: str | None = None,
        provider: floating_ip.Provider | None = None,
    ) -> dict[str, Any]:
        application_id = uuid(application_id, field="application UUID")
        if action not in {"allocate", "attach", "release", "reconcile"}:
            raise ValidationError("invalid public IP action")
        if (action in {"allocate", "attach"}) != (network_id is not None):
            raise ValidationError("allocate/attach require the exact external network UUID")
        if (action == "attach") != (floating_ip_id is not None):
            raise ValidationError("only attach requires an existing floating IP UUID")
        if network_id is not None:
            uuid(network_id, field="external network UUID")
        if floating_ip_id is not None:
            uuid(floating_ip_id, field="floating IP UUID")
        request_id = uuid(request_id or str(uuid_module.uuid4()), field="operation UUID")
        deadline = operation_deadline(self.config)
        scope = f"app-{application_id}"
        refs = dict(
            application_id=application_id,
            action=action,
            network_id=network_id,
            floating_ip_id=floating_ip_id,
        )
        with runtime.lock(self.state_directory, scope, deadline=deadline):
            application = db.get_application(self.connection, application_id)
            if application is None:
                raise ValidationError("application does not exist")
            operation = db.get_operation(self.connection, request_id)
            if operation is not None:
                if (
                    operation.kind != "app.public-ip"
                    or operation.scope != scope
                    or operation.refs != refs
                ):
                    raise db.IdempotencyConflictError("public IP request identity differs")
                if operation.status == "succeeded":
                    return model(self.connection, application_id)
                if operation.status == "failed":
                    raise db.DatabaseError("public IP request already failed")
                db.renew_operation_deadline(self.connection, request_id, wall_deadline(deadline))
            else:
                operation = db.begin_operation(
                    self.connection,
                    operation_id=request_id,
                    kind="app.public-ip",
                    scope=scope,
                    phase="validated",
                    deadline_at=wall_deadline(deadline),
                    refs=refs,
                )
            provider = provider or floating_ip.Provider(self.config.platform, deadline=deadline)
            try:
                if action in {"allocate", "attach"}:
                    from .fixed_ip_service import get as get_fixed

                    if get_fixed(self.connection, application_id) is not None:
                        raise ValidationError(
                            "floating and retained fixed IP reservations are mutually exclusive"
                        )
                    record = get(self.connection, application_id)
                    if record is not None and record["request_id"] != request_id:
                        raise ValidationError(
                            "release the existing reservation before replacing it"
                        )
                    if record is None:
                        assert network_id is not None
                        capability = provider.plan(network_id, allocating=action == "allocate")
                        if capability["supported"] is not True:
                            raise floating_ip.CapabilityUnavailable(
                                "stable outbound IPv4 unavailable: "
                                + ", ".join(capability["reasons"])
                            )
                        description = f"{self.config.platform.namespace}:app-public-ip:{application_id}:{request_id}"
                        value = None
                        if floating_ip_id is not None:
                            value = provider.show(floating_ip_id)
                            description = value.get("description") or ""
                            if (
                                value.get("port_id") is not None
                                or value.get("fixed_ip_address") is not None
                            ):
                                raise openstack.DriftError(
                                    "supplied floating IP must be unassociated"
                                )
                            if (
                                self.connection.execute(
                                    "SELECT 1 FROM application_floating_ips WHERE floating_ip_id = ?",
                                    (floating_ip_id,),
                                ).fetchone()
                                is not None
                            ):
                                raise openstack.DriftError(
                                    "floating IP is already reserved for another app"
                                )
                        record = dict(
                            request_id=request_id,
                            project_id=self.config.platform.project_id,
                            network_id=network_id,
                            floating_ip_id=floating_ip_id,
                            address=None,
                            description=description,
                            allocated=action == "allocate",
                            phase="allocating",
                            port=None,
                            pending_port=None,
                        )
                        if value is not None:
                            _adopt_result(self.connection, application_id, record, value, provider)
                        else:
                            _save(self.connection, application_id, record)
                            value = provider.create(network_id, description)
                            # Even a successful create response is reobserved by UUID.
                            value = provider.show(
                                uuid(value.get("id"), field="allocated floating IP UUID")
                            )
                            _adopt_result(self.connection, application_id, record, value, provider)
                    reconcile_accepted(
                        self.connection,
                        self.config,
                        application_id,
                        deadline=deadline,
                        provider=provider,
                    )
                elif action == "release":
                    release_locked(
                        self.connection,
                        self.config,
                        application_id,
                        deadline=deadline,
                        provider=provider,
                    )
                else:
                    reconcile_accepted(
                        self.connection,
                        self.config,
                        application_id,
                        deadline=deadline,
                        provider=provider,
                    )
                db.mark_succeeded(self.connection, request_id)
            except Exception as error:
                saved = get(self.connection, application_id)
                if saved is None or (
                    action in {"allocate", "attach"} and saved["request_id"] != request_id
                ):
                    db.mark_failed(self.connection, request_id, error, cleanup_state="not_required")
                else:
                    db.mark_recovery_required(self.connection, request_id, error)
                raise
        return model(self.connection, application_id)
