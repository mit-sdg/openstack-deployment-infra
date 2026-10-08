"""Offline OSC/Nomad double, shared by CLI requests without interpreter startup."""

import json
import shlex
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from socketserver import UnixStreamServer


class CommandResponse(Exception):
    def __init__(self, output, status):
        self.output = output
        self.status = status


def command(path, a):
    s = json.loads(path.read_text())
    s["calls"].append(a)

    def finish(value=None, status=0):
        path.write_text(json.dumps(s))
        if value is not None:
            value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        raise CommandResponse(value or "", status)

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
            flavor=dict(
                original_name="xl.4core" if option("--flavor") == "4200" else "worker-small"
            ),
        )
        s["servers"][sid] = v
        p.update(
            device_id=sid,
            device_owner="compute:nova",
            status="ACTIVE",
            **{"binding:host_id": "host"},
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


def start_cli(path, executable, *, execute=command):
    """Keep the real subprocess boundary; curl transports NUL-separated argv."""
    socket_path = executable.with_suffix(".sock")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            arguments = self.rfile.read(int(self.headers["Content-Length"]))
            argv = [value.decode() for value in arguments.split(b"\0")[:-1]]
            try:
                execute(path, argv)
            except CommandResponse as result:
                payload = (result.output + "\n").encode() if result.output else b""
                self.send_response(200 if result.status == 0 else 500)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = UnixStreamServer(str(socket_path), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    executable.write_text(
        "#!/bin/bash\nset -o pipefail\n"
        'printf "%s\\0" "$@" | curl --silent --show-error --fail '
        f"--unix-socket {shlex.quote(str(socket_path))} --data-binary @- http://localhost/\n"
    )
    executable.chmod(0o755)

    def close():
        server.shutdown()
        server.server_close()
        thread.join()

    return close
