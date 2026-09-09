#!/usr/bin/env python3
"""Offline OSC/Nomad process double; real helper and lifecycle shell run unchanged."""

import json
import os
import sys
import uuid
from pathlib import Path

path = Path(os.environ["FAKE_OPENSTACK_STATE"])
s = json.loads(path.read_text())
a = sys.argv[1:]
s["calls"].append(a)


def compact(value, key=None):
    if isinstance(value, dict):
        return {k: compact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [compact(v, key) for v in value]
    if isinstance(value, str) and key in {
        "id",
        "ID",
        "project_id",
        "network_id",
        "subnet_id",
        "device_id",
        "security_group_ids",
        "subnets",
    }:
        return value.replace("-", "")
    return value


def finish(value=None, status=0):
    if s.get("compact") and a[:2] != ["node", "status"]:
        value = compact(value)
    path.write_text(json.dumps(s))
    if value is not None:
        print(json.dumps(value) if isinstance(value, (dict, list)) else value)
    raise SystemExit(status)


def option(name):
    return a[a.index(name) + 1]


def fault(kind, after=False):
    if s.get("fault") == kind + (".after" if after else ".before"):
        s["fault"] = None
        finish(status=1)


if a[:3] == ["node", "status", "-json"]:
    nodes = []
    for server in s["servers"].values():
        props = server["properties"]
        large = server["flavor"]["original_name"] == "xl.4core"
        nodes.append(
            dict(
                ID=server["id"],
                Name=server["name"],
                Status="ready",
                SchedulingEligibility="eligible",
                Drain=False,
                NodeClass=s["namespace"] + "-app",
                Meta=dict(
                    application_id=props[s["metadata"] + "_application_id"],
                    application_slug=props[s["metadata"] + "_application_slug"],
                    managed_by=s["namespace"] + "-platform",
                ),
                Drivers=dict(docker=dict(Detected=True, Healthy=True)),
                NodeResources=dict(
                    Cpu=dict(CpuShares=10000 if large else 2000),
                    Memory=dict(MemoryMB=16000 if large else 2048),
                ),
                ReservedResources=dict(
                    Cpu=dict(CpuShares=1000 if large else 200),
                    Memory=dict(MemoryMB=1600 if large else 512),
                ),
            )
        )
    finish(nodes if len(a) == 3 else next(node for node in nodes if node["ID"] == a[3]))
if a[:2] == ["token", "issue"]:
    finish(s["project"] if "value" in a else dict(project_id=s["project"]))
if a[:2] == ["network", "show"]:
    finish(dict(id=s["network"], name=s["network_name"], subnets=[s["subnet"]]))
if a[:2] == ["subnet", "show"]:
    finish(
        dict(
            id=s["subnet"],
            network_id=s["network"],
            ip_version=4,
            cidr="128.52.128.0/18",
            gateway_ip="128.52.128.1",
            allocation_pools=[dict(start="128.52.140.1", end="128.52.150.250")],
        )
    )
if a[:3] == ["security", "group", "show"]:
    finish(dict(id=s["group"], name=s["prefix"] + "-worker", project_id=s["project"]))
if a[:2] == ["flavor", "show"]:
    large = a[2] in ("4200", "xl.4core")
    finish(
        dict(
            id="4200" if large else "100",
            name="xl.4core" if large else "worker-small",
            vcpus=4 if large else 1,
        )
    )
if a[:2] in (["port", "list"], ["server", "list"]):
    values = list(s[a[0] + "s"].values())
    if "--name" in a:
        values = [v for v in values if v["name"] == option("--name")]
    if "--server" in a:
        values = [v for v in values if v["device_id"] == option("--server")]
    finish([dict(ID=v["id"], Name=v["name"]) for v in values])
if a[:2] in (["port", "show"], ["server", "show"]):
    value = s[a[0] + "s"].get(a[2])
    if value is None:
        finish(status=1)
    if "value" in a:
        finish(value.get("status", "ACTIVE"))
    finish(value)
if a[:3] == ["console", "log", "show"]:
    finish(s["namespace"] + " NixOS Nomad worker provisioning data installed")
if a[:2] == ["port", "create"]:
    fault("port.create")
    fixed = (
        dict(item.split("=", 1) for item in option("--fixed-ip").split(","))
        if "--fixed-ip" in a
        else {}
    )
    address = fixed.get("ip-address", "128.52.140." + str(len(s["ports"]) + 10))
    if any(p["fixed_ips"][0]["ip_address"] == address for p in s["ports"].values()):
        finish(status=1)
    name = a[-3] if a[-2:] == ["--format", "json"] else a[-1]
    pid = str(uuid.uuid4())
    p = dict(
        id=pid,
        name=name,
        project_id=s["project"],
        network_id=s["network"],
        description=option("--description"),
        device_id="",
        device_owner="",
        status="DOWN",
        fixed_ips=[dict(subnet_id=s["subnet"], ip_address=address)],
        security_group_ids=[s["group"]],
        port_security_enabled=True,
        allowed_address_pairs=[],
        **{"binding:host_id": ""},
    )
    s["ports"][pid] = p
    fault("port.create", True)
    finish(p)
if a[:2] == ["server", "create"]:
    fault("server.create")
    p = s["ports"][option("--port")]
    if p["device_id"]:
        finish(status=1)
    sid = str(uuid.uuid4())
    props = dict(a[i + 1].split("=", 1) for i, item in enumerate(a) if item == "--property")
    v = dict(
        id=sid,
        name=a[-1],
        project_id=s["project"],
        properties=props,
        status="ACTIVE",
        image=dict(id=option("--image")),
        flavor=dict(original_name="xl.4core" if option("--flavor") == "4200" else "worker-small"),
    )
    s["servers"][sid] = v
    p.update(
        device_id=sid, device_owner="compute:nova", status="ACTIVE", **{"binding:host_id": "host"}
    )
    fault("server.create", True)
    finish(v)
if a[:2] == ["server", "delete"]:
    fault("server.delete")
    del s["servers"][a[-1]]
    for p in s["ports"].values():
        if p["device_id"] == a[-1]:
            p.update(device_id="", device_owner="", status="DOWN", **{"binding:host_id": ""})
    fault("server.delete", True)
    finish()
if a[:2] == ["port", "delete"]:
    fault("port.delete")
    if s["ports"][a[2]]["device_id"]:
        finish(status=1)
    del s["ports"][a[2]]
    fault("port.delete", True)
    finish()
raise AssertionError(a)
