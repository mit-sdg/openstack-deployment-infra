{ pkgs, platform }:
let
  lib = pkgs.lib;
  constants = import ../lib/constants.nix;
  namespace = platform.namespace;
  root = platform.paths.root;
  state = platform.paths.adminState;
  backups = platform.paths.backups;
  packages = import ../pkgs { inherit pkgs platform; };
  # The VM test runs MongoDB natively (no image pulls); only this package is unfree.
  # runNixOSTest owns each node's nixpkgs, so allow it on a separate import.
  mongodbPkgs = import pkgs.path {
    inherit (pkgs.stdenv.hostPlatform) system;
    config.allowUnfreePredicate = package: lib.getName package == "mongodb-ce";
  };
  # The one Commons Connect code the fake redeems, issued to the portal's origin
  # with the challenge of this verifier (the RFC 7636 Appendix B example).
  managementRedeemRequest = builtins.toJSON {
    code = "11111111-1111-4111-8111-111111111111.vm-fixture";
    app = "https://${platform.domain}";
    code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
  };
  managementIdentityBootstrap = pkgs.writeText "management-identity-bootstrap.py" ''
    import sys
    sys.path.insert(0, "${packages.controllerPackage}/${pkgs.python314.sitePackages}")
    from openstack_platform.management.identity.main import main
    main()
  '';
  managementIdentityProbe = pkgs.writeText "management-identity-probe.py" ''
    import os
    import sys
    import time
    from pathlib import Path
    sys.path.insert(0, "${packages.controllerPackage}/${pkgs.python314.sitePackages}")
    from openstack_platform.management.broker.client import ControllerUnavailable, ProjectClient
    from openstack_platform.controller.http import ControllerServer, PeerPolicy, Response, Router
    proof=Path("${state}/management-broker/identity-sandbox-ok")
    # Redeem through the real identity service from the broker sandbox.
    client = ProjectClient(Path("/run/${namespace}-management-identity/identity.sock"), timeout=10)
    deadline = time.monotonic() + 30
    last = "no attempt"
    while True:
        try:
            status, result = client.request("POST", "/v1/redeem", ${managementRedeemRequest})
            last = f"status={status} error={result.get('error', {}).get('code') if isinstance(result, dict) else None}"
            if status != 503:
                break
        except ControllerUnavailable as error:
            last = f"unavailable: {type(error).__name__}: {error}"
        if time.monotonic() >= deadline:
            raise RuntimeError(f"identity/Commons did not become ready in the VM ({last})")
        time.sleep(0.1)
    assert status == 200 and result["data"]["subject"] == "11111111-1111-4111-8111-111111111111"
    assert "email" not in result["data"]
    proof.write_text("verified\n")
    os.chmod(proof,0o600)
    router = Router()
    router.add("GET", "/v1/health", lambda request: Response(200, {"status":"ok"}))
    server = ControllerServer("/run/${namespace}-management-broker/broker.sock", router,
        peer_policy=PeerPolicy(frozenset({(${toString constants.accounts.managementWeb.uid}, ${toString constants.accounts.managementWeb.gid})})), socket_gid=${toString constants.accounts.managementWeb.gid})
    server.serve_forever()
  '';
  managementWebProbe = pkgs.writeText "management-web-probe.py" ''
    import sys
    sys.path.insert(0, "${packages.controllerPackage}/${pkgs.python314.sitePackages}")
    from openstack_platform.controller.http import ControllerServer, Response, Router
    router = Router()
    router.add("GET", "/v1/health", lambda request: Response(200, {"status": "ok"}))
    server = ControllerServer("${state}/management-web/web-health.sock", router)
    server.serve_forever()
  '';
  managementActivationRequest = pkgs.writeText "vm-management-activation-request.py" ''
    import sys
    from pathlib import Path
    sys.path.insert(0, "${packages.controllerPackage}/${pkgs.python314.sitePackages}")
    from openstack_platform.management.installation import request_activation
    # The fixture has test entrypoints rather than signed release archives,
    # but activation requests use the installer's real atomic operator writer.
    request_activation(
        Path("${state}/management-broker-releases"), sys.argv[1], sys.argv[2],
        ${toString constants.accounts.managementBroker.gid},
    )
  '';
  managementFakeCommons = pkgs.writeText "vm-fake-commons.py" ''
    import json, ssl, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("${testPki}/commons.pem","${testPki}/commons-key.pem")
    class Handler(BaseHTTPRequestHandler):
        timeout=10
        def setup(self):
            # Handshake per connection thread, never inside accept().
            self.request.settimeout(10)
            self.request=context.wrap_socket(self.request,server_side=True)
            super().setup()
        def log_message(self, format, *args):
            print("fake-commons "+(format % args),file=sys.stderr,flush=True)
        def do_POST(self):
            body=json.loads(self.rfile.read(min(4096,int(self.headers.get("Content-Length","0")))))
            accepted=self.path=="/api/connect/redeem" and body==${managementRedeemRequest}
            value={"user":"11111111-1111-4111-8111-111111111111","username":"alice","displayName":"Alice","email":"alice@example.com"} if accepted else {"error":"CONNECT_CODE_INVALID"}
            raw=json.dumps(value).encode()
            self.send_response(200 if accepted else 400)
            self.send_header("Content-Type","application/json")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
    server=ThreadingHTTPServer(("127.0.0.1",9444),Handler)
    server.daemon_threads=True
    server.serve_forever()
  '';
  storageInstanceProbe = pkgs.writeText "storage-instance-probe.py" ''
    import json, socket, ssl, subprocess, sys, urllib.error, urllib.request, uuid
    sys.path.insert(0, "${packages.controllerPackage}/${pkgs.python314.sitePackages}")
    import psycopg
    from pymongo import MongoClient
    from pymongo.monitoring import ServerHeartbeatListener
    from openstack_platform.helper import storage
    ca="/etc/${namespace}/pki/internal-ca.pem"
    host="${platform.internalNames.storage}"
    import urllib.parse
    class Heartbeats(ServerHeartbeatListener):
        def started(self, event):
            pass
        def succeeded(self, event):
            pass
        def failed(self, event):
            print("Mongo heartbeat failed endpoint="+repr(event.connection_id)+" duration="+str(event.duration)+" reason="+str(event.reply)[:1000],file=sys.stderr,flush=True)
    def connect_app_mongo(*, uri):
        parsed=urllib.parse.urlsplit(uri)
        query=[(k,v) for k,v in urllib.parse.parse_qsl(parsed.query) if k.lower() != "tlscafile"]
        safe_uri=urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
        # Retain the strict selection deadline. Bound individual connects so
        # monitor errors become visible before server selection times out.
        return MongoClient(safe_uri,tlsCAFile=ca,serverSelectionTimeoutMS=5000,connectTimeoutMS=2000,event_listeners=[Heartbeats()])
    context=ssl.create_default_context(cafile=ca)
    def call(action, instance, **values):
        body=json.dumps({"action":action,"instanceId":instance,**values}).encode()
        request=urllib.request.Request("https://127.0.0.1:${toString constants.ports.garageRpc}/platform/instances",data=body,headers={"Authorization":"Bearer vm-instance-token","Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(request,context=context,timeout=150) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            print("instance action="+action+" HTTP="+str(error.code)+" response="+error.read(65536).decode(errors="replace"),file=sys.stderr,flush=True)
            raise
    import os
    geometry=os.statvfs("${platform.paths.data}")
    assert geometry.f_blocks*geometry.f_frsize > 900*1024**3
    owner=str(uuid.uuid4())
    limits={"sizeBytes":2147483648,"connections":10,"memoryBytes":536870912,"cpuMillicores":500}
    ids=[str(uuid.uuid4()),str(uuid.uuid4())]
    instances=[call("create",ident,applicationId=owner,type=kind,quotas=limits,allowIps=[],reservations={"databaseBytes":6442450944,"garageBytes":0}) for ident,kind in zip(ids,["postgres","mongo"])]
    credentials=[call("credentials",ident) for ident in ids]
    from openstack_platform.helper.instances import connect_ready
    ready=connect_ready(lambda: psycopg.connect(host=host,port=credentials[0]["port"],dbname="platform",user="platform_admin",password=credentials[0]["adminPassword"],sslmode="verify-full",sslrootcert=ca,connect_timeout=2))
    ready.close()
    for ident in ids:
        subprocess.run(["${pkgs.openssl}/bin/openssl","verify","-CAfile",ca,"-verify_hostname","${platform.internalNames.storage}","${platform.paths.data}/instances/"+ident+"/pki/storage.pem"],check=True)
        unit="${namespace}-database@"+ident+".service"
        result=subprocess.check_output(["systemctl","show",unit,"-p","MemoryMax","-p","MemorySwapMax","-p","CPUWeight","-p","IOWeight","-p","TasksMax","-p","CPUQuotaPerSecUSec"],text=True)
        properties=dict(line.split("=",1) for line in result.splitlines())
        assert properties["MemoryMax"] == "536870912"
        assert properties["MemorySwapMax"] == "0"
        assert properties["CPUWeight"] == "100" and properties["IOWeight"] == "100"
        assert properties["TasksMax"] == "256"
        assert properties["CPUQuotaPerSecUSec"] == "500ms"
        group=subprocess.check_output(["systemctl","show",unit,"-p","ControlGroup","--value"],text=True).strip()
        pid=json.loads(subprocess.check_output(["podman","inspect","${namespace}-db-"+ident]))[0]["State"]["Pid"]
        from pathlib import Path
        actual=Path("/proc/"+str(pid)+"/cgroup").read_text().strip().split("0::",1)[1]
        assert actual == group or actual.startswith(group+"/")
        cgroup=Path("/sys/fs/cgroup"+actual)
        assert cgroup.joinpath("memory.max").read_text().strip() == "536870912"
        data="${platform.paths.data}/instances/"+ident+"/data"
        probe=data+"/quota-enforcement-probe"
        assigned=subprocess.check_output(["lsattr","-pd",data],text=True).split()[0]
        assert int(assigned) >= 10000
        call("stop",ident)
        assert subprocess.run(["fallocate","-l","4G",probe],capture_output=True).returncode != 0
        Path(probe).unlink(missing_ok=True)
        call("start",ident)
    # Driver server-selection retries provide a bounded startup wait.
    from openstack_platform.helper.instances import connect_ready
    pg=connect_ready(lambda: psycopg.connect(host=host,port=credentials[0]["port"],dbname="platform",user="platform_admin",password=credentials[0]["adminPassword"],sslmode="verify-full",sslrootcert=ca,connect_timeout=2,autocommit=True))
    storage._PORT_CONTEXT.set((credentials[0]["port"],credentials[1]["port"]))
    pg_operation=str(uuid.uuid4())
    postgres=storage.postgres_create(pg,application_id=owner,host=host,connections=10,measured_target_bytes=2147483648,generation="abcdef12",operation_id=pg_operation)
    assert storage._postgres_creation_evidence(pg,application_id=owner,operation_id=pg_operation,generation="abcdef12")[0] == postgres.provider_name
    storage.postgres_verify(lambda **kwargs: psycopg.connect(**kwargs,sslrootcert=ca,connect_timeout=5,autocommit=True),postgres,host=host)
    assert pg.execute("show max_connections").fetchone()[0] == "15"
    pg.close()
    mongo=MongoClient(host,credentials[1]["port"],username="platform_admin",password=credentials[1]["adminPassword"],authSource="admin",tls=True,tlsCAFile=ca,serverSelectionTimeoutMS=2000)
    connect_ready(lambda: mongo.admin.command("ping"))
    scoped=storage.mongo_create(mongo,application_id=owner,host=host,measured_target_bytes=2147483648,generation="abcdef12",operation_id=str(uuid.uuid4()))
    try:
        storage.mongo_verify(connect_app_mongo,scoped,host=host)
    except Exception:
        import faulthandler
        print("Mongo verify failed uid="+str(os.geteuid())+" host="+host+" port="+str(credentials[1]["port"])+" addresses="+repr(socket.getaddrinfo(host,credentials[1]["port"])),file=sys.stderr,flush=True)
        faulthandler.dump_traceback(file=sys.stderr,all_threads=True)
        subprocess.run(["nft","list","table","inet","${namespace}".replace("-","_")+"_instances"],check=False)
        subprocess.run(["ss","-tn"],check=False)
        raise
    options=mongo.admin.command("getCmdLineOpts")["parsed"]
    assert options["net"]["maxIncomingConnections"] == 20
    assert options["storage"]["wiredTiger"]["engineConfig"]["cacheSizeGB"] == 0.25
    # Exercise repeated atomic rule replacements while a real client exchanges
    # authenticated traffic. Worker revocation still applies to existing flows.
    from concurrent.futures import ThreadPoolExecutor
    def pings():
        for _ in range(100):
            mongo.admin.command("ping")
    with ThreadPoolExecutor(max_workers=1) as pool:
        traffic=pool.submit(pings)
        for index in range(8):
            call("allow",ids[1],applicationId=owner,addresses=["127.0.0.2"] if index % 2 else [],mode="replace")
        traffic.result()
    mongo.close()
    # Use the same native exporters/importers as nightly backup, through the
    # authenticated manager's root credentials and certificate DNS name.
    from openstack_platform.database_backups import Native, digest
    import socket, tempfile, urllib.parse
    assert socket.gethostbyname(host) == "127.0.0.1"
    native=Native(host,ca)
    from openstack_platform.controller.storage_contract import canonicalize_environment
    with psycopg.connect(host=host,port=credentials[0]["port"],dbname=postgres.provider_name,user=postgres.credential_name,password=postgres.environment["PGPASSWORD"],sslmode="verify-full",sslrootcert=ca,autocommit=True) as app_pg:
        app_pg.execute("CREATE TABLE backup_probe(value integer)")
        app_pg.execute("INSERT INTO backup_probe VALUES (42)")
    dbname=scoped.provider_name
    app_mongo=connect_app_mongo(uri=scoped.environment["MONGODB_URI"])
    app_mongo[dbname].backup_probe.insert_one({"value":42})
    # Exercise the actual friendly block: find/delete remain authorized, while
    # insert is denied; raising the soft limit restores readWrite atomically.
    from openstack_platform.helper.storage_limits import mongo_reconcile
    from pymongo.errors import OperationFailure
    import urllib.parse
    login=urllib.parse.unquote(urllib.parse.urlsplit(scoped.environment["MONGODB_URI"]).username)
    admin_mongo=MongoClient(host,credentials[1]["port"],username="platform_admin",password=credentials[1]["adminPassword"],authSource="admin",tls=True,tlsCAFile=ca,serverSelectionTimeoutMS=5000)
    stats=admin_mongo[dbname].command("dbStats",scale=1)
    assert stats["dataSize"]+stats["indexSize"] > 0
    assert mongo_reconcile(admin_mongo[dbname],login,dbname,used=2,limit=1)
    try:
        app_mongo[dbname].backup_probe.insert_one({"value":43})
    except OperationFailure as error:
        assert error.code == 13
    else:
        raise AssertionError("blocked Mongo user could insert")
    assert app_mongo[dbname].backup_probe.find_one()["value"] == 42
    assert app_mongo[dbname].backup_probe.delete_one({"value":42}).deleted_count == 1
    assert not mongo_reconcile(admin_mongo[dbname],login,dbname,used=0,limit=1)
    app_mongo[dbname].backup_probe.insert_one({"value":42})
    admin_mongo.close()
    app_mongo.close()
    with tempfile.TemporaryDirectory(dir="${platform.paths.data}") as temporary:
        for index,kind in enumerate(["postgres","mongo"]):
            credential=postgres if index==0 else scoped
            entry={"type":kind,"port":credentials[index]["port"],"databases":[credential.provider_name],"resources":[{"name":"default","providerName":credential.provider_name,"bindings":dict(canonicalize_environment(kind,"default",credential.environment)),"writeBlock":{"blocked":False}}]}
            payload=Path(temporary)/kind
            native.dump(entry,credentials[index]["adminPassword"],payload)
            # Explicit delete/recreate models an entirely lost isolated instance.
            call("remove",ids[index],deleteData=True)
            entry["quotas"]={**limits,"connections":20}
            call("restore-create",ids[index],applicationId=owner,type=kind,quotas=entry["quotas"],port=entry["port"],allowIps=[],reservations={"databaseBytes":6442450944,"garageBytes":0})
            call("restore-begin",ids[index],backupSha256=digest(payload))
            replacement=call("credentials",ids[index])
            native.restore(entry,replacement["adminPassword"],payload)
            call("restore-finish",ids[index],migrationState=None)
            credentials[index]=replacement
    with psycopg.connect(host=host,port=credentials[0]["port"],dbname=postgres.provider_name,user=postgres.credential_name,password=postgres.environment["PGPASSWORD"],sslmode="verify-full",sslrootcert=ca) as app_pg:
        assert app_pg.execute("SELECT value FROM backup_probe").fetchone()[0] == 42
        assert app_pg.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname=current_user").fetchone()[0] == 20
    app_mongo=connect_app_mongo(uri=scoped.environment["MONGODB_URI"])
    assert app_mongo[dbname].backup_probe.find_one()["value"] == 42
    app_mongo.close()
    # Exercise real provider limits/usage, with only the Nomad Variable boundary
    # in memory. Both instance restarts must retain authenticated app access.
    from openstack_platform.helper.storage_limits import resource_action
    from openstack_platform.helper.nomad import SecretItems, VariableSnapshot
    class Variables:
        def __init__(self, kind, credential):
            self.items=dict(canonicalize_environment(kind,"default",credential.environment))
        def read_variable(self, path):
            return VariableSnapshot(path,1,SecretItems(self.items))
        def compare_and_set(self, path, index, items):
            self.items=dict(items); return 2
    storage._PORT_CONTEXT.set((credentials[0]["port"],credentials[1]["port"]))
    for index,kind in enumerate(["postgres","mongo"]):
        changed={**limits,"connections":12}
        call("limits",ids[index],quotas=changed,reservations={"databaseBytes":6442450944,"garageBytes":0})
        if kind=="postgres":
            admin=connect_ready(lambda: psycopg.connect(host=host,port=credentials[index]["port"],dbname="platform",user="platform_admin",password=credentials[index]["adminPassword"],sslmode="verify-full",sslrootcert=ca,autocommit=True,connect_timeout=2))
            assert admin.execute("SHOW max_connections").fetchone()[0] == "17"
        else:
            admin=MongoClient(host,credentials[index]["port"],username="platform_admin",password=credentials[index]["adminPassword"],authSource="admin",tls=True,tlsCAFile=ca,serverSelectionTimeoutMS=2000,connectTimeoutMS=2000)
            connect_ready(lambda: admin.admin.command("ping"))
            assert admin.admin.command("getCmdLineOpts")["parsed"]["net"]["maxIncomingConnections"] == 22
        credential=postgres if index==0 else scoped
        result=resource_action({"applicationId":owner,"applicationSlug":"vm-instance","resourceName":"default","providerId":credential.provider_id,"providerName":credential.provider_name,"quotas":changed,"operationId":str(uuid.uuid4()),"recover":False},resource_type=kind,mutate=True,admin=admin,nomad=Variables(kind,credential),host=host,endpoint="unused")
        assert result["applied"] and result["usage"]["usedBytes"] > 0
        admin.close()
    # Rehearse the real manager copy through nginx, including an admin-owned
    # extension comment and an app function that rejects admin restore sessions.
    source_secret=Path("/etc/${namespace}/secrets/postgres-password")
    source_secret.parent.mkdir(parents=True,exist_ok=True)
    source_secret.write_text("vm-shared-password")
    source_secret.chmod(0o400); os.chown(source_secret,999,999)
    fixture=json.loads(Path("/etc/vm-instance-platform.json").read_text())
    from openstack_platform.storage_instances import database_command, POSTGRES_SOCKET_DIRECTORY
    source_limits={**limits,"memoryBytes":8589934592,"connections":95}
    subprocess.run(["podman","run","-d","--name","vm-shared-postgres","--network=host","--cgroups=disabled","--tmpfs="+POSTGRES_SOCKET_DIRECTORY+":rw,size=16m,mode=1777","--volume","${platform.paths.data}/postgres:/var/lib/postgresql/data","--volume",str(source_secret)+":/run/secrets/admin-password:ro","--volume","${platform.paths.data}/instances/"+ids[0]+"/pki:/run/${namespace}-pki:ro",fixture["containers"]["postgres"],*database_command({"type":"postgres","port":5432,"quotas":source_limits},"${namespace}")],check=True,stdout=subprocess.DEVNULL)
    source=connect_ready(lambda: psycopg.connect(host=host,port=5432,dbname="platform",user="platform_admin",password="vm-shared-password",sslmode="verify-full",sslrootcert=ca,autocommit=True))
    storage._PORT_CONTEXT.set((5432,credentials[1]["port"]))
    storage.postgres_create(source,application_id=owner,host=host,connections=10,measured_target_bytes=2147483648,generation="abcdef12",operation_id=str(uuid.uuid4()),password_factory=lambda:postgres.environment["PGPASSWORD"] )
    source.close()
    with psycopg.connect(host=host,port=5432,dbname=postgres.provider_name,user="platform_admin",password="vm-shared-password",sslmode="verify-full",sslrootcert=ca,autocommit=True) as source:
        source.execute("COMMENT ON EXTENSION plpgsql IS 'owner-sensitive comment'")
    with psycopg.connect(host=host,port=5432,dbname=postgres.provider_name,user=postgres.credential_name,password=postgres.environment["PGPASSWORD"],sslmode="verify-full",sslrootcert=ca,autocommit=True) as source_app:
        source_app.execute("CREATE FUNCTION public.restore_guard() RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$ BEGIN IF session_user='platform_admin' THEN RAISE EXCEPTION 'admin restore session'; END IF; RETURN true; END $$")
        source_app.execute("CREATE TABLE public.guarded(value integer CHECK(public.restore_guard()))")
        source_app.execute("INSERT INTO public.guarded VALUES(42)")
        source_app.execute("CREATE SCHEMA pgdata")
        source_app.execute("CREATE TABLE pgdata.items(value integer)")
        source_app.execute("INSERT INTO pgdata.items VALUES(84)")
    with psycopg.connect(host=host,port=credentials[0]["port"],dbname=postgres.provider_name,user=postgres.credential_name,password=postgres.environment["PGPASSWORD"],sslmode="verify-full",sslrootcert=ca,autocommit=True) as target:
        target.execute("CREATE SCHEMA pgdata")
        target.execute("CREATE TABLE pgdata.stale(value integer)")
    call("copy",ids[0],database=postgres.provider_name,seconds=120,operationId=str(uuid.uuid4()),applicationLogin=postgres.credential_name,applicationPassword=postgres.environment["PGPASSWORD"])
    with psycopg.connect(host=host,port=credentials[0]["port"],dbname=postgres.provider_name,user=postgres.credential_name,password=postgres.environment["PGPASSWORD"],sslmode="verify-full",sslrootcert=ca) as target:
        assert target.execute("SELECT value FROM public.guarded").fetchone()[0] == 42
        assert target.execute("SELECT value FROM pgdata.items").fetchone()[0] == 84
        assert target.execute("SELECT to_regclass('pgdata.stale')").fetchone()[0] is None
        assert target.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='guarded'").fetchone()[0] == "o_"+postgres.provider_name[2:]
    subprocess.run(["podman","rm","-f","vm-shared-postgres"],check=True,stdout=subprocess.DEVNULL)
    # Mongo copy must also remove namespaces left by an interrupted attempt,
    # while preserving the target's pre-created app user and soft-block role.
    mongo_secret=source_secret.with_name("mongodb-password")
    mongo_secret.write_text("vm-shared-mongo-password"); mongo_secret.chmod(0o400)
    mongo_data=Path("${platform.paths.data}/mongodb")
    mongo_data.mkdir(exist_ok=True); os.chown(mongo_data,999,999)
    subprocess.run(["podman","run","-d","--name","vm-shared-mongo","--network=host","--cgroups=disabled","--read-only","--user=999:999","--entrypoint=mongod","--tmpfs=/tmp:rw,size=64m,mode=1777","--volume",str(mongo_data)+":/data/db","--volume","${platform.paths.data}/instances/"+ids[1]+"/pki:/run/${namespace}-pki:ro",fixture["containers"]["mongodb"],*database_command({"type":"mongo","port":27017,"quotas":limits},"${namespace}")[1:]],check=True,stdout=subprocess.DEVNULL)
    anonymous=MongoClient(host,27017,tls=True,tlsCAFile=ca,serverSelectionTimeoutMS=2000,connectTimeoutMS=2000)
    connect_ready(lambda: anonymous.admin.command("ping"))
    anonymous.admin.command("createUser","platform_admin",pwd="vm-shared-mongo-password",roles=[{"role":"root","db":"admin"}])
    anonymous.close()
    source_mongo=MongoClient(host,27017,username="platform_admin",password="vm-shared-mongo-password",authSource="admin",tls=True,tlsCAFile=ca,serverSelectionTimeoutMS=5000,connectTimeoutMS=2000)
    parsed=urllib.parse.urlsplit(scoped.environment["MONGODB_URI"])
    storage._PORT_CONTEXT.set((credentials[0]["port"],27017))
    source_credential=storage.mongo_create(source_mongo,application_id=owner,host=host,measured_target_bytes=2147483648,generation="abcdef12",operation_id=str(uuid.uuid4()),password_factory=lambda:urllib.parse.unquote(parsed.password))
    # A user-bearing DB without any collections must still be backed up and
    # restored. Native inventory must include it before the first app write.
    assert native.databases("mongo",27017,"vm-shared-mongo-password") == [scoped.provider_name]
    with tempfile.TemporaryDirectory(dir="${platform.paths.data}") as empty_backup:
        from pathlib import Path
        empty_entry={"type":"mongo","port":27017,"databases":[scoped.provider_name]}
        empty_payload=Path(empty_backup)/"empty.archive"
        native.dump(empty_entry,"vm-shared-mongo-password",empty_payload)
        native.restore(empty_entry,"vm-shared-mongo-password",empty_payload)
    source_mongo[dbname].copied.insert_one({"value":84})
    source_mongo[dbname].copied.create_index("value")
    source_mongo.close()
    target_mongo=connect_app_mongo(uri=scoped.environment["MONGODB_URI"])
    target_mongo[dbname].stale.insert_one({"value":0}); target_mongo.close()
    call("copy",ids[1],database=dbname,seconds=120,operationId=str(uuid.uuid4()),applicationLogin=scoped.credential_name,applicationPassword=urllib.parse.unquote(parsed.password))
    target_mongo=connect_app_mongo(uri=scoped.environment["MONGODB_URI"])
    assert target_mongo[dbname].copied.find_one()["value"] == 84
    assert "stale" not in target_mongo[dbname].list_collection_names()
    assert "value_1" in target_mongo[dbname].copied.index_information()
    target_mongo.close()
    subprocess.run(["podman","rm","-f","vm-shared-mongo"],check=True,stdout=subprocess.DEVNULL)
    for ident in ids:
        call("remove",ident,deleteData=True)
    print("instance lifecycle, authenticated copy, backup/restore and cgroup caps verified")
  '';
  backupReceiptProbe = pkgs.writeText "backup-receipt-probe.py" ''
    import json, os, sys
    from pathlib import Path
    sys.path.insert(0, "${packages.controllerPackage}/${pkgs.python314.sitePackages}")
    from openstack_platform.database_backups import fresh_backup, receipt_root, timestamp
    config=json.loads(Path("/etc/${namespace}/platform.json").read_text())
    root=receipt_root(config)
    database="p_"+"a"*20
    receipt=root/("postgres-"+database+".json")
    receipt.write_text(json.dumps({"namespace":config["namespace"],"projectId":config["projectId"],"verified":True,"shared":True,"type":"postgres","database":database,"backedUpAt":timestamp(),"payloadSha256":"a"*64}))
    os.chown(receipt,${toString constants.accounts.operator.uid},${toString constants.accounts.controller.gid})
    receipt.chmod(0o640)
    payload=Path("${backups}/${namespace}/vm-private-payload")
    payload.write_text("encrypted backup remains private")
    os.chown(payload,${toString constants.accounts.operator.uid},${toString constants.accounts.operator.gid})
    payload.chmod(0o600)
    os.setgroups([${toString constants.accounts.controller.gid}])
    os.setgid(${toString constants.accounts.controller.gid})
    os.setuid(${toString constants.accounts.controller.uid})
    assert fresh_backup(root,"postgres",database,60,config=config)["verified"]
    for private in (payload,Path("${state}/operator/secrets/backup-age-key.txt")):
        try:
            private.read_bytes()
        except PermissionError:
            pass
        else:
            raise AssertionError("controller could read private backup payload/key")
  '';
  imageCompatibilityHash = builtins.hashString "sha256" (
    builtins.toJSON {
      format = 1;
      inherit namespace;
      pkiInternalCaFile = platform.pki.internalCaFile;
      prefix = platform.prefix;
      projectId = platform.projectId;
    }
  );
  systemdEscapePath =
    path: lib.replaceStrings [ "-" "/" ] [ "\\x2d" "-" ] (lib.removePrefix "/" path);
  testPki = pkgs.runCommand "${namespace}-test-pki" { nativeBuildInputs = [ pkgs.openssl ]; } ''
        set -euo pipefail
        install -d -m 0755 "$out"
        # Python 3.13+ verifies with VERIFY_X509_STRICT, which rejects a CA
        # certificate without an explicit keyCertSign key usage.
        openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2 \
          -keyout "$out/ca-key.pem" -out "$out/ca.pem" \
          -subj "/CN=Platform VM Test CA/O=Platform Tests" \
          -addext "basicConstraints=critical,CA:TRUE" \
          -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1

        issue() {
          name=$1
          common_name=$2
          usage=$3
          sans=$4
          openssl req -newkey rsa:2048 -nodes -sha256 \
            -keyout "$out/$name-key.pem" -out "$out/$name.csr" \
            -subj "/CN=$common_name/O=Platform Tests" >/dev/null 2>&1
          cat > "$out/$name.ext" <<EOF
    basicConstraints=critical,CA:FALSE
    keyUsage=critical,digitalSignature,keyEncipherment
    extendedKeyUsage=$usage
    subjectAltName=$sans
    EOF
          openssl x509 -req -sha256 -days 2 \
            -in "$out/$name.csr" -CA "$out/ca.pem" -CAkey "$out/ca-key.pem" \
            -CAcreateserial -extfile "$out/$name.ext" -out "$out/$name.pem" \
            >/dev/null 2>&1
        }

        issue nomad-server server.global.nomad 'serverAuth,clientAuth' \
          'DNS:server.global.nomad,DNS:localhost,IP:127.0.0.1'
        issue nomad-cli client.global.nomad clientAuth \
          'DNS:client.global.nomad,DNS:localhost'
        issue nomad-worker client.global.nomad clientAuth \
          'DNS:client.global.nomad,DNS:localhost'
        issue nomad-ingress client.global.nomad clientAuth \
          'DNS:client.global.nomad,DNS:localhost'
        issue commons class.example.com serverAuth 'DNS:class.example.com,DNS:localhost,IP:127.0.0.1'
        issue storage storage.example.internal serverAuth \
          'DNS:storage.example.internal,DNS:s3.example.internal,DNS:localhost,IP:127.0.0.1'
        cat "$out/storage.pem" "$out/storage-key.pem" > "$out/mongodb-combined.pem"
        rm -f "$out"/*.csr "$out"/*.ext "$out"/*.srl
  '';

  managedBackupCredentialProbe = pkgs.writeText "managed-backup-credential-probe.py" ''
    import os
    import shutil
    import subprocess
    import stat
    from pathlib import Path

    Path("${state}/operator/status/managed-backup-probe-ran").touch()
    for name in ("newuidmap", "newgidmap"):
        assert shutil.which(name) == "/run/wrappers/bin/" + name
    subprocess.run(
        ["${pkgs.podman}/bin/podman", "unshare", "${pkgs.coreutils}/bin/true"],
        check=True, capture_output=True, timeout=30,
    )
    loaded = Path("${state}/operator/secrets/storage-bootstrap.env")
    private = Path("/run/${namespace}-backup-private/storage-bootstrap.env")
    assert Path(os.environ["SECRETS_FILE"]) == private
    assert private.read_bytes() == loaded.read_bytes()
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
    assert private.stat().st_uid == os.geteuid()
    assert Path(os.environ["AGE_KEY"]).is_file()
    shared = Path("${root}/secrets/storage-bootstrap.env")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o640
  '';

  pkiEtc = {
    "${namespace}/pki/internal-ca.pem".source = "${testPki}/ca.pem";
    "${namespace}/pki/nomad-server.pem".source = "${testPki}/nomad-server.pem";
    "${namespace}/pki/nomad-server-key.pem".source = "${testPki}/nomad-server-key.pem";
    "${namespace}/pki/nomad-cli.pem".source = "${testPki}/nomad-cli.pem";
    "${namespace}/pki/nomad-cli-key.pem".source = "${testPki}/nomad-cli-key.pem";
    "${namespace}/pki/nomad-worker.pem".source = "${testPki}/nomad-worker.pem";
    "${namespace}/pki/nomad-worker-key.pem".source = "${testPki}/nomad-worker-key.pem";
    "${namespace}/pki/nomad-ingress.pem".source = "${testPki}/nomad-ingress.pem";
    "${namespace}/pki/nomad-ingress-key.pem".source = "${testPki}/nomad-ingress-key.pem";
    "${namespace}/pki/storage.pem".source = "${testPki}/storage.pem";
    "${namespace}/pki/storage-key.pem".source = "${testPki}/storage-key.pem";
    "${namespace}/pki/mongodb-combined.pem".source = "${testPki}/mongodb-combined.pem";
  };

  mkRoleTest =
    role:
    pkgs.testers.runNixOSTest {
      name = "${namespace}-${role}-vm";

      nodes.machine =
        {
          lib,
          pkgs,
          config,
          ...
        }:
        let
          # Small cached native images exercise the manager end to end without
          # fetching the production OCI pins from inside a networkless test VM.
          dbNss = pkgs.runCommand "vm-db-nss" { } ''
            mkdir -p "$out/etc"
            printf 'root:x:0:0:root:/root:/bin/sh\npostgres:x:999:999:postgres:/var/lib/postgresql:/bin/sh\n' > "$out/etc/passwd"
            printf 'root:x:0:\npostgres:x:999:\n' > "$out/etc/group"
          '';
          postgresEntry = pkgs.writeShellScriptBin "vm-postgres-entry" ''
            set -eu
            export PATH=${
              lib.makeBinPath [
                pkgs.postgresql_17
                pkgs.coreutils
                pkgs.util-linux
              ]
            }
            export PGDATA=/var/lib/postgresql/data
            if [ ! -f "$PGDATA/PG_VERSION" ]; then
              setpriv --reuid=999 --regid=999 --clear-groups initdb -D "$PGDATA" -U platform_admin --pwfile=/run/secrets/admin-password --auth-local=trust --auth-host=scram-sha-256 --locale=C --encoding=UTF8
              printf 'CREATE DATABASE platform;\n' | setpriv --reuid=999 --regid=999 --clear-groups postgres --single -D "$PGDATA" postgres
            fi
            exec setpriv --reuid=999 --regid=999 --clear-groups "$@"
          '';
          postgresImage = pkgs.dockerTools.buildLayeredImage {
            name = "vm-instance-postgres";
            tag = "latest";
            contents = [
              # initdb checks postgres -V through libc popen(), which requires
              # /bin/sh even though the entrypoint has a store-path shebang.
              pkgs.dockerTools.binSh
              pkgs.postgresql_17
              pkgs.coreutils
              pkgs.util-linux
              dbNss
              postgresEntry
            ];
            config.Entrypoint = [ "/bin/vm-postgres-entry" ];
          };
          mongoImage = pkgs.dockerTools.buildLayeredImage {
            name = "vm-instance-mongo";
            tag = "latest";
            contents = [
              mongodbPkgs.mongodb-ce
              pkgs.coreutils
              dbNss
            ];
            config.Entrypoint = [ "/bin/mongod" ];
          };
          instancePlatform = platform // {
            addresses = platform.addresses // {
              admin = "127.0.0.1";
              storage = "127.0.0.1";
            };
            containers = platform.containers // {
              postgres = "localhost/vm-instance-postgres:latest";
              mongodb = "localhost/vm-instance-mongo:latest";
            };
          };
        in
        {
          imports = [
            ../modules/common.nix
            (../roles + "/${role}.nix")
          ];

          _module.args = { inherit constants platform role; };

          virtualisation = {
            memorySize = if role == "storage" then 4096 else 2048;
            # podman load unpacks the database test images under /var/tmp.
            diskSize = lib.mkIf (role == "storage") 8192;
            emptyDiskImages = lib.optionals (role == "storage") [ 1048576 ];
            cores = 2;
            qemu.options = lib.optionals (role == "storage") [ "-cpu max" ];
          };

          # Use cached native binaries for a startup smoke of the role's exact
          # arguments; no container image pulls or full provider scenario.
          environment.systemPackages = lib.optionals (role == "storage") [
            pkgs.e2fsprogs # lsattr -p verifies the XFS project assignment.
            pkgs.postgresql_17
            mongodbPkgs.mongodb-ce
            pkgs.mongodb-tools
          ];
          networking.extraHosts = lib.mkIf (role == "storage") (
            lib.mkForce "127.0.0.1 ${platform.internalNames.storage}"
          );
          security.pki.certificateFiles = lib.optionals (role == "admin") [ "${testPki}/ca.pem" ];
          networking.hosts = lib.mkIf (role == "admin") { "127.0.0.1" = [ "class.example.com" ]; };
          services.cloud-init.settings.datasource_list = lib.mkForce [ "None" ];

          # The test VM supplies disposable local mounts in place of
          # deployment-owned Cinder volumes. Use explicit mount units because
          # the NixOS VM harness replaces fileSystems with its virtual disks.
          systemd.mounts =
            lib.optionals (role == "admin") [
              {
                what = "tmpfs";
                where = platform.paths.adminState;
                type = "tmpfs";
                options = "mode=0755";
                wantedBy = [ "multi-user.target" ];
              }
              {
                what = "tmpfs";
                where = platform.paths.backups;
                type = "tmpfs";
                options = "mode=0700";
                wantedBy = [ "multi-user.target" ];
              }
            ]
            ++ lib.optionals (role == "storage") [
              {
                what = "/dev/vdb";
                where = platform.paths.data;
                type = "xfs";
                options = "prjquota";
                after = [ "vm-storage-format.service" ];
                requires = [ "vm-storage-format.service" ];
                wantedBy = [ "multi-user.target" ];
              }
            ];

          systemd.services = lib.mkMerge [
            (lib.mkIf (role == "admin") {
              # Exercise the real backup unit's User, Environment, private copy
              # and source guards without contacting any managed service.
              "${namespace}-platform-backup".serviceConfig.ExecStart =
                lib.mkForce "${packages.python}/bin/python ${managedBackupCredentialProbe}";
              # Exercise authorization and the template's real operator identity.
              "${namespace}-resource-backup@".serviceConfig.ExecStart =
                lib.mkForce "${pkgs.coreutils}/bin/touch ${backups}/${namespace}/vm-resource-backup-probe-ran";
              "${namespace}-management-identity".serviceConfig = {
                # Mirror production: the fake class app on loopback plus the
                # local resolver stubs used for name resolution.
                IPAddressAllow = lib.mkForce [
                  "127.0.0.1/32"
                  "127.0.0.53/32"
                  "127.0.0.54/32"
                ];
              };
              "vm-restore-active-portal" = {
                # Restore the saved active pair before production preparation
                # and path units see this VM's disposable tmpfs state.
                wantedBy = [ "multi-user.target" ];
                after = [ "${systemdEscapePath state}.mount" ];
                before = [
                  "${namespace}-management-prepare.service"
                  "${namespace}-management-broker.path"
                  "${namespace}-management-web.path"
                  "${namespace}-management-activate.path"
                ];
                unitConfig = {
                  DefaultDependencies = false;
                  RequiresMountsFor = [ state ];
                  ConditionPathExists = "/var/lib/portal-boot-fixture";
                };
                serviceConfig = {
                  Type = "oneshot";
                  RemainAfterExit = true;
                };
                script = ''
                  umask 0077
                  # Copy children, preserving the state mount's root metadata.
                  cp -a --preserve=all /var/lib/portal-boot-fixture/management-* ${state}/
                  rm -f ${state}/management-broker/identity-sandbox-ok
                '';
              };
              "vm-fake-commons" = {
                wantedBy = [ "multi-user.target" ];
                serviceConfig.ExecStart = "${packages.platformPython}/bin/python ${managementFakeCommons}";
              };
              nomad.preStart = lib.mkForce ''
                install -d -m 0750 -o nomad -g nomad ${platform.paths.adminState}/nomad
                install -d -m 0700 -o nomad -g nomad /run/${namespace}-nomad
                printf '%s\n' \
                  'advertise { http = "127.0.0.1:4646" rpc = "127.0.0.1:4647" serf = "127.0.0.1:4648" }' \
                  > /run/${namespace}-nomad/90-runtime.hcl
                chown nomad:nomad /run/${namespace}-nomad/90-runtime.hcl
              '';
              "${namespace}-controller-test-fixture" = {
                description = "Install disposable controller policy and helper release";
                before = [ "${namespace}-controller-prepare.service" ];
                after = [
                  "systemd-tmpfiles-setup.service"
                  "${systemdEscapePath state}.mount"
                ];
                requires = [ "${systemdEscapePath state}.mount" ];
                serviceConfig = {
                  Type = "oneshot";
                  RemainAfterExit = true;
                };
                script = ''
                  install -d -m 0750 -o agentops -g agentops \
                    ${state}/operator/helper-releases/current/bin
                  install -m 0600 -o agentops -g agentops \
                    ${../../config/platform-policy.example.json} \
                    ${state}/operator/policy.json
                  cat > ${state}/operator/image-selections.json <<'EOF'
                  {
                    "schemaVersion": 1,
                    "projectId": "${platform.projectId}",
                    "namespace": "${namespace}",
                    "images": {
                      "admin": {"imageId":"00000000-0000-4000-8000-000000000001","displayName":"${platform.images.admin}","sourceCommit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","compatibilityHash":"${imageCompatibilityHash}"},
                      "ingress": {"imageId":"00000000-0000-4000-8000-000000000002","displayName":"${platform.images.ingress}","sourceCommit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","compatibilityHash":"${imageCompatibilityHash}"},
                      "storage": {"imageId":"00000000-0000-4000-8000-000000000003","displayName":"${platform.images.storage}","sourceCommit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","compatibilityHash":"${imageCompatibilityHash}"},
                      "worker": {"imageId":"00000000-0000-4000-8000-000000000004","displayName":"${platform.images.worker}","sourceCommit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","compatibilityHash":"${imageCompatibilityHash}"},
                      "builder": {"imageId":"00000000-0000-4000-8000-000000000005","displayName":"${platform.images.builder}","sourceCommit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","compatibilityHash":"${imageCompatibilityHash}"}
                    }
                  }
                  EOF
                  chown agentops:agentops ${state}/operator/image-selections.json
                  chmod 0600 ${state}/operator/image-selections.json
                  cat > ${state}/operator/helper-releases/current/bin/openstack-platform-helper <<'EOF'
                  #!/bin/sh
                  printf '%s\n' '{"version":1,"requestId":"00000000-0000-0000-0000-000000000000","ok":false,"error":{"code":"INVALID_REQUEST","message":"helper request is invalid"}}'
                  EOF
                  chown agentops:agentops \
                    ${state}/operator/helper-releases/current/bin/openstack-platform-helper
                  chmod 0550 \
                    ${state}/operator/helper-releases/current/bin/openstack-platform-helper
                  printf 'vm-test\n' > ${state}/operator/helper-releases/current/.complete
                  chown agentops:agentops ${state}/operator/helper-releases/current/.complete
                  chmod 0440 ${state}/operator/helper-releases/current/.complete
                  for credential in \
                    openstack.env nomad-tokens.env storage-bootstrap.env \
                    builder_operator_ed25519 backup-age-key.txt; do
                    printf 'controller-secret\n' > ${state}/operator/secrets/$credential
                    chown agentops:agentops ${state}/operator/secrets/$credential
                    chmod 0600 ${state}/operator/secrets/$credential
                  done
                  printf 'REGISTRY_BUILDER_PASSWORD=controller-secret\n' \
                    > ${state}/operator/secrets/storage-bootstrap.env
                  printf 'ssh-ed25519 vm-test\n' \
                    > ${state}/operator/secrets/builder_operator_ed25519.pub
                  chown agentops:agentops \
                    ${state}/operator/secrets/builder_operator_ed25519.pub
                  chmod 0644 ${state}/operator/secrets/builder_operator_ed25519.pub
                  install -d -m 0700 -o agentops -g agentops \
                    ${state}/operator/secrets/provisioning-pki
                  install -m 0644 -o agentops -g agentops ${testPki}/ca.pem \
                    ${state}/operator/secrets/provisioning-pki/internal-ca.pem
                  for name in nomad-cli nomad-worker; do
                    install -m 0644 -o agentops -g agentops ${testPki}/$name.pem \
                      ${state}/operator/secrets/provisioning-pki/$name.pem
                    install -m 0600 -o agentops -g agentops ${testPki}/$name-key.pem \
                      ${state}/operator/secrets/provisioning-pki/$name-key.pem
                  done
                '';
              };
              "${namespace}-controller" = {
                after = [ "${namespace}-controller-test-fixture.service" ];
                requires = [ "${namespace}-controller-test-fixture.service" ];
              };
            })
            (lib.mkIf (role == "ingress") {
              "${namespace}-ingress-readiness".wantedBy = lib.mkForce [ ];
            })
            (lib.mkIf (role == "storage") {
              "${namespace}-storage-readiness".wantedBy = lib.mkForce [ ];
              "${namespace}-storage-host-status".wantedBy = lib.mkForce [ ];
              "podman-${namespace}-postgres".wantedBy = lib.mkForce [ ];
              "podman-${namespace}-mongodb".wantedBy = lib.mkForce [ ];
              "podman-${namespace}-garage".wantedBy = lib.mkForce [ ];
              "podman-${namespace}-registry".wantedBy = lib.mkForce [ ];

              "${namespace}-storage-instance-manager" = {
                wantedBy = lib.mkForce [ ];
                serviceConfig.ExecStart = lib.mkForce "${packages.controllerPackage}/bin/openstack-platform-storage-manager --config /etc/vm-instance-platform.json";
              };
              "${namespace}-database@".serviceConfig.ExecStart =
                lib.mkForce "${packages.controllerPackage}/bin/openstack-platform-storage-manager --config /etc/vm-instance-platform.json --run-instance %i";
              "vm-storage-format" = {
                after = [ "dev-vdb.device" ];
                requires = [ "dev-vdb.device" ];
                serviceConfig.Type = "oneshot";
                serviceConfig.RemainAfterExit = true;
                script = ''
                  ${pkgs.xfsprogs}/bin/mkfs.xfs -f -d size=32g -L ${platform.volumes.data.label} /dev/vdb
                '';
              };
            })
          ];

          systemd.user.services = lib.mkIf (role == "builder") {
            buildkit.serviceConfig.ExecStartPre = lib.mkForce (
              pkgs.writeShellScript "${namespace}-test-builder-ca-bundle" ''
                install -d -m 0700 "$XDG_RUNTIME_DIR/buildkit"
                cat ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt \
                  ${testPki}/ca.pem \
                  > "$XDG_RUNTIME_DIR/buildkit/ca-bundle.crt"
                chmod 0600 "$XDG_RUNTIME_DIR/buildkit/ca-bundle.crt"
              ''
            );
          };

          environment.etc = lib.mkMerge [
            pkiEtc
            (lib.mkIf (role == "admin") {
              "${namespace}/secrets/nomad-gossip-key" = {
                text = "dGVzdC1ub21hZC1nb3NzaXAta2V5\n";
                mode = "0600";
              };
            })
            (lib.mkIf (role == "storage") {
              "vm-instance-platform.json".text = builtins.toJSON instancePlatform;
              "vm-instance-images/postgres.tar".source = postgresImage;
              "vm-instance-images/mongo.tar".source = mongoImage;
              "${namespace}/garage.toml" = {
                text = ''
                  [admin]
                                  admin_token = "vm-instance-token"
                '';
                mode = "0600";
              };
            })
            (lib.mkIf (role == "worker") {
              "${namespace}/docker-auth.json".text = ''{"auths":{}}'';
              "nomad.d/90-test.hcl".text = ''
                client {
                  enabled = true
                  servers = ["127.0.0.1:4647"]
                  node_class = "${namespace}-app"
                  meta {
                    project_id = "00000000-0000-4000-8000-000000000001"
                    project_slug = "vm-test"
                    managed_by = "${namespace}-platform"
                  }
                }
              '';
            })
            (lib.mkIf (role == "ingress") {
              "${namespace}/secrets/traefik.env" = {
                text = "NOMAD_TOKEN=vm-test-token\n";
                mode = "0600";
              };
            })
          ];
        };

      testScript = ''
        machine.start(allow_reboot=True)
        machine.wait_for_unit("multi-user.target")
        machine.succeed("getent hosts ${platform.internalNames.storage}")
        machine.wait_for_unit("cloud-final.service")
        machine.succeed("python3 -c 'import json; json.load(open(\"/etc/${namespace}/platform.json\"))'")

        ${
          if role == "admin" then
            ''
              # Real controller/Nomad APIs and the role's trust boundaries.
              machine.wait_for_unit("nomad.service")
              machine.wait_for_unit("${namespace}-admin-readiness.service")
              machine.wait_for_unit("${namespace}-controller-readiness.service")
              machine.succeed("${packages.python}/bin/python ${backupReceiptProbe}")
              machine.succeed("runuser -u platform-controller -- systemctl --no-ask-password start ${namespace}-resource-backup@postgres-p_aaaaaaaaaaaaaaaaaaaa.service")
              machine.succeed("test $(stat -c %U ${backups}/${namespace}/vm-resource-backup-probe-ran) = agentops")
              machine.fail("runuser -u management-broker -- systemctl --no-ask-password start ${namespace}-resource-backup@mongo-p_bbbbbbbbbbbbbbbbbbbb.service")
              machine.fail("runuser -u platform-controller -- systemctl --no-ask-password start ${namespace}-platform-backup.service")
              machine.succeed("${pkgs.curl}/bin/curl --fail --silent --max-time 5 --cacert /etc/${namespace}/pki/internal-ca.pem --cert /etc/${namespace}/pki/nomad-cli.pem --key /etc/${namespace}/pki/nomad-cli-key.pem https://127.0.0.1:4646/v1/status/leader >/dev/null")
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-controller/project.sock) = platform-controller:controller-api:660")
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-controller/privileged.sock) = platform-controller:platform-admin:660")
              machine.succeed("runuser -u management-broker -- ${pkgs.curl}/bin/curl --fail --silent --max-time 5 --unix-socket /run/${namespace}-controller/project.sock 'http://localhost/v1/health' | grep -F '\"status\":\"ok\"'")
              machine.succeed("runuser -u agentops -- ${pkgs.curl}/bin/curl --fail --silent --max-time 5 --unix-socket /run/${namespace}-controller/privileged.sock 'http://localhost/v1/admin/applications?limit=1' >/dev/null")
              machine.fail("runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --max-time 5 --unix-socket /run/${namespace}-controller/project.sock 'http://localhost/v1/health'")
              machine.fail("runuser -u management-broker -- ${pkgs.curl}/bin/curl --fail --silent --max-time 5 --unix-socket /run/${namespace}-controller/project.sock 'http://localhost/v1/admin/applications?limit=1'")
              machine.fail("runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --max-time 5 --unix-socket /run/${namespace}-controller/privileged.sock 'http://localhost/v1/admin/applications?limit=1'")
              machine.fail("runuser -u management-web -- cat ${state}/controller/policy.json")
              machine.succeed("runuser -u management-web -- sh -c 'for name in openstack.env nomad-tokens.env storage-bootstrap.env builder_operator_ed25519 backup-age-key.txt; do test ! -r ${state}/operator/secrets/\"$name\" || exit 1; done'")
              machine.fail("runuser -u management-web -- cat /etc/${namespace}/pki/nomad-cli-key.pem")
              machine.fail("runuser -u management-web -- cat /run/credentials/nomad.service/nomad-gossip-key")
              machine.succeed("runuser -u platform-controller -- cat ${state}/operator/secrets/openstack.env >/dev/null")
              machine.fail("runuser -u nomad -- cat ${state}/operator/secrets/openstack.env")
              # Run the real managed-backup unit with a local credential consumer.
              machine.succeed("systemctl start ${namespace}-platform-backup.service && test -f ${state}/operator/status/managed-backup-probe-ran")
              machine.fail("test -e /run/${namespace}-backup-private")
              # One refusal proves a world-readable shared secret never reaches ExecStart.
              machine.succeed("rm ${state}/operator/status/managed-backup-probe-ran; chmod 0644 ${root}/secrets/storage-bootstrap.env")
              machine.fail("systemctl start ${namespace}-platform-backup.service")
              machine.fail("test -e ${state}/operator/status/managed-backup-probe-ran")
              machine.succeed("chmod 0640 ${root}/secrets/storage-bootstrap.env; systemctl reset-failed ${namespace}-platform-backup.service")
              machine.wait_for_unit("${namespace}-management-prepare.service")
              # Privileged preparation must reject operator-controlled links
              # without touching their root-owned target (one case per path).
              victim = "${state}/root-preparation-victim"
              machine.succeed(f"install -d -m 0755 -o root -g root {victim}; printf 'protected fixture\\n' > {victim}/sentinel; chmod 0600 {victim}/sentinel")
              victim_intact = f"test $(stat -c %u:%g:%a {victim}) = 0:0:755 && test $(stat -c %u:%g:%a {victim}/sentinel) = 0:0:600 && grep -Fx 'protected fixture' {victim}/sentinel && test ! -e {victim}/platform.json"
              for child in ("config", "releases"):
                  path = f"${state}/management-broker-releases/{child}"
                  machine.succeed(f"runuser -u agentops -- sh -c 'mv {path} {path}.saved; ln -s {victim} {path}'")
                  machine.fail("systemctl restart ${namespace}-management-prepare.service")
                  machine.succeed(victim_intact)
                  machine.succeed(f"runuser -u agentops -- sh -c 'rm {path}; mv {path}.saved {path}'; systemctl reset-failed ${namespace}-management-prepare.service")
              machine.succeed("systemctl start ${namespace}-management-prepare.service")
              # Invoke the real privileged backup pre-start without a broker DB.
              backup_prepare = machine.succeed("systemctl cat ${namespace}-management-broker-backup.service | sed -n 's/^ExecStartPre=+//p'").strip()
              machine.succeed(backup_prepare)
              backup_dir = "${backups}/${constants.directories.managementBrokerBackup}"
              machine.succeed(f"runuser -u agentops -- sh -c 'mv {backup_dir} {backup_dir}.saved; ln -s {victim} {backup_dir}'")
              machine.fail(backup_prepare)
              machine.succeed(victim_intact)
              machine.succeed(f"runuser -u agentops -- sh -c 'rm {backup_dir}; mkdir -m 2750 {backup_dir}'")
              backup_metadata = machine.succeed(f"stat -c %u:%g:%a:%h {backup_dir}")
              machine.fail(backup_prepare)
              assert machine.succeed(f"stat -c %u:%g:%a:%h {backup_dir}") == backup_metadata
              machine.succeed(f"runuser -u agentops -- sh -c 'rmdir {backup_dir}; mv {backup_dir}.saved {backup_dir}'")
              # Complete fixture pairs follow the same staged/active/config
              # layout as installed releases; only their entrypoints are doubles.
              import json
              commit = "a" * 40
              pair = "b" * 64
              descriptor = json.dumps({"sourceCommit": commit, "pairIdentity": pair, "compatibility": {"brokerProtocolVersion": 3, "webProtocolVersion": 3, "authProtocolVersion": 3, "brokerSchemaVersion": 3, "controllerApiVersion": 1}}, separators=(",", ":"))
              for component in ("broker", "web"):
                  release = f"${state}/management-{component}-releases/releases/vm-test"
                  machine.succeed(f"install -d -m 2750 -o agentops -g management-{component} {release} {release}/bin {release}/config {release}/evidence")
                  machine.succeed(f"install -m 0440 -o agentops -g management-{component} /etc/${namespace}/platform.json {release}/config/platform.json")
                  machine.succeed(f"printf '%s\\n' '{commit}' > {release}/.complete; printf '%s\\n' '{descriptor}' > {release}/evidence/management-artifacts.json; printf '{{}}\\n' > {release}/config/management.json")
                  machine.succeed(f"chown agentops:management-{component} {release}/.complete {release}/evidence/management-artifacts.json {release}/config/management.json; chmod 0440 {release}/.complete {release}/evidence/management-artifacts.json {release}/config/management.json")
                  machine.succeed(f"ln -s releases/vm-test ${state}/management-{component}-releases/current; chown -h agentops:management-{component} ${state}/management-{component}-releases/current")
              machine.succeed("printf '#!/bin/sh\\nexec /run/current-system/sw/bin/management-python3.14 ${managementIdentityProbe}\\n' > ${state}/management-broker-releases/releases/vm-test/bin/management-broker")
              machine.succeed("printf '#!/bin/sh\\nexec /run/current-system/sw/bin/management-python3.14 ${managementIdentityBootstrap} --config ${state}/management-active/current/broker/config/identity.json\\n' > ${state}/management-broker-releases/releases/vm-test/bin/management-identity")
              machine.succeed("chown agentops:management-broker ${state}/management-broker-releases/releases/vm-test/bin/*; chmod 0550 ${state}/management-broker-releases/releases/vm-test/bin/*")
              machine.succeed("printf '%s\\n' '{\"commonsOrigin\":\"https://class.example.com:9444\",\"socket\":\"/run/${namespace}-management-identity/identity.sock\",\"development\":false}' > ${state}/management-broker-releases/releases/vm-test/config/identity.json; chown agentops:management-broker ${state}/management-broker-releases/releases/vm-test/config/identity.json; chmod 0440 ${state}/management-broker-releases/releases/vm-test/config/identity.json")
              machine.succeed("printf '#!/bin/sh\\nexec /run/current-system/sw/bin/management-python3.14 ${managementWebProbe}\\n' > ${state}/management-web-releases/releases/vm-test/bin/management-web; chown agentops:management-web ${state}/management-web-releases/releases/vm-test/bin/management-web; chmod 0550 ${state}/management-web-releases/releases/vm-test/bin/management-web")
              machine.succeed("install -m 0600 -o agentops -g management-broker /dev/null ${state}/management-broker-releases/.install.lock")
              machine.wait_for_unit("vm-fake-commons.service")
              machine.succeed("systemctl reset-failed ${namespace}-management-broker.path ${namespace}-management-web.path ${namespace}-management-activate.path; systemctl start ${namespace}-management-broker.path ${namespace}-management-web.path ${namespace}-management-activate.path")
              for component in ("broker", "web", "activate"):
                  machine.wait_for_unit(f"${namespace}-management-{component}.path")
              machine.succeed(f"runuser -u agentops -- /run/current-system/sw/bin/management-python3.14 ${managementActivationRequest} {commit} {pair}")
              machine.wait_for_unit("${namespace}-management-broker.service")
              machine.wait_until_succeeds("test -f ${state}/management-broker/identity-sandbox-ok")
              broker_health = "runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --max-time 2 --unix-socket /run/${namespace}-management-broker/broker.sock http://localhost/v1/health | grep -F '\"status\":\"ok\"'"
              machine.wait_until_succeeds(broker_health)
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-management-broker) = management-broker:management-web:750")
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-management-broker/broker.sock) = management-broker:management-web:660")
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-management-identity) = management-identity:management-broker:750")
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-management-identity/identity.sock) = management-identity:management-broker:660")
              machine.succeed("runuser -u management-broker -- ${pkgs.curl}/bin/curl --fail --silent --max-time 5 --unix-socket /run/${namespace}-management-identity/identity.sock http://localhost/v1/health")
              machine.fail("runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --max-time 5 --unix-socket /run/${namespace}-management-identity/identity.sock http://localhost/v1/health")
              machine.fail("runuser -u management-identity -- cat ${state}/management-broker/identity-sandbox-ok")
              machine.wait_for_unit("${namespace}-management-web.service")
              machine.wait_until_succeeds("test $(systemctl show ${namespace}-management-activate.service -p ActiveState --value) = inactive && test $(systemctl show ${namespace}-management-activate.service -p Result --value) = success")
              web_health = "runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --max-time 2 --unix-socket ${state}/management-web/web-health.sock http://localhost/v1/health | grep -F '\"status\":\"ok\"'"
              machine.wait_until_succeeds(web_health)
              machine.succeed("runuser -u management-web -- cat ${state}/management-active/current/web/config/platform.json >/dev/null")
              machine.fail("runuser -u management-web -- cat ${state}/management-active/current/broker/config/platform.json")
              machine.fail("runuser -u management-web -- systemctl restart ${namespace}-management-broker.service")
              machine.fail("runuser -u management-broker -- systemctl restart ${namespace}-management-web.service")
              machine.succeed("${root}/bin/openstack-platform-helper </dev/null | grep -F INVALID_REQUEST")

              # The active pair must boot without reset-failed or reactivation.
              selected_pair = machine.succeed("readlink ${state}/management-active/current").strip()
              portal_units = "multi-user.target ${namespace}-admin-readiness.service ${namespace}-controller-readiness.service ${namespace}-management-identity.service ${namespace}-management-broker.service ${namespace}-management-web.service ${namespace}-management-broker.path ${namespace}-management-web.path"
              portal_ready = f'for unit in {portal_units}; do systemctl is-active --quiet "$unit" || exit 1; done; {broker_health} && {web_health}'
              machine.succeed("umask 0077; install -d -m 0700 /var/lib/portal-boot-fixture; cp -a --preserve=all ${state}/management-active ${state}/management-broker-releases ${state}/management-web-releases ${state}/management-broker ${state}/management-web /var/lib/portal-boot-fixture/; rm -f /var/lib/portal-boot-fixture/management-broker-releases/activate-request")
              machine.reboot()
              machine.wait_until_succeeds(portal_ready)
              machine.succeed("test ! -e ${state}/management-broker-releases/activate-request")
              assert machine.succeed("readlink ${state}/management-active/current").strip() == selected_pair

              # systemd removes the controller socket directory during restart;
              # its dependency chain must restart both portal processes.
              before = {component: machine.succeed(f"systemctl show ${namespace}-management-{component}.service -p MainPID --value").strip() for component in ("broker", "web")}
              machine.succeed("systemctl restart ${namespace}-controller.service")
              machine.wait_until_succeeds(portal_ready)
              for component, pid in before.items():
                  assert machine.succeed(f"systemctl show ${namespace}-management-{component}.service -p MainPID --value").strip() != pid

              # A mismatched staged pair is refused synchronously and remains
              # inactive when services/path units restart; do not reactivate it.
              next_broker = "${state}/management-broker-releases/releases/vm-next"
              next_descriptor = descriptor.replace(pair, "c" * 64)
              machine.succeed("systemctl stop ${namespace}-management-activate.path")
              machine.succeed(f"cp -a ${state}/management-broker-releases/releases/vm-test {next_broker}; printf '#!/bin/sh\\nexit 77\\n' > {next_broker}/bin/management-broker; printf '%s\\n' '{next_descriptor}' > {next_broker}/evidence/management-artifacts.json; ln -sfn releases/vm-next ${state}/management-broker-releases/current; chown -h agentops:management-broker ${state}/management-broker-releases/current")
              machine.succeed(f"runuser -u agentops -- /run/current-system/sw/bin/management-python3.14 ${managementActivationRequest} {commit} {'c' * 64}")
              machine.fail("systemctl restart ${namespace}-management-activate.service")
              assert machine.succeed("readlink ${state}/management-active/current").strip() == selected_pair
              machine.succeed("systemctl restart ${namespace}-management-broker.path ${namespace}-management-web.path ${namespace}-management-broker.service ${namespace}-management-web.service")
              machine.wait_until_succeeds(portal_ready)
              assert machine.succeed("readlink ${state}/management-active/current").strip() == selected_pair
              machine.succeed("test $(readlink -f ${state}/management-active/current/broker) = ${state}/management-broker-releases/releases/vm-test")
              machine.succeed("ln -sfn releases/vm-test ${state}/management-broker-releases/current; chown -h agentops:management-broker ${state}/management-broker-releases/current; rm ${state}/management-broker-releases/activate-request; systemctl reset-failed ${namespace}-management-activate.service; systemctl start ${namespace}-management-activate.path")

              # Make the backup volume unstartable, then prove the portal can
              # still restart and backup ExecStart cannot reach the root disk.
              backup_mount = machine.succeed("systemd-escape --path --suffix=mount ${backups}").strip()
              machine.succeed(f"systemctl stop '{backup_mount}'; install -d '/run/systemd/system/{backup_mount}.d'; printf '[Unit]\\nAssertPathExists=/run/vm-test-backup-outage-never-exists\\n' > '/run/systemd/system/{backup_mount}.d/outage.conf'; systemctl daemon-reload; rm -f ${state}/operator/status/managed-backup-probe-ran")
              machine.fail(f"systemctl start '{backup_mount}'")
              machine.fail("mountpoint -q ${backups}")
              machine.succeed(portal_ready)
              machine.succeed("systemctl restart ${namespace}-management-broker.service ${namespace}-management-web.service")
              machine.wait_until_succeeds(portal_ready)
              machine.fail("systemctl start ${namespace}-management-broker-backup.service")
              machine.fail("systemctl start ${namespace}-platform-backup.service")
              machine.fail("test -e ${state}/operator/status/managed-backup-probe-ran")
              machine.succeed(f"rm -r '/run/systemd/system/{backup_mount}.d'; systemctl daemon-reload; systemctl reset-failed '{backup_mount}' ${namespace}-platform-backup.service ${namespace}-management-broker-backup.service; systemctl start '{backup_mount}'")
            ''
          else if role == "ingress" then
            ''
              machine.wait_for_unit("traefik.service")
              machine.wait_until_succeeds("${pkgs.curl}/bin/curl --fail --silent http://127.0.0.1:8082/ping | grep -Fx OK", timeout=30)
              # CORS must permit application origins and reject foreign origins.
              preflight = "${pkgs.curl}/bin/curl --silent --include --request OPTIONS --header 'Host: s3.${platform.domain}' --header 'Access-Control-Request-Method: PUT' http://127.0.0.1/app-bucket/key --header Origin:"
              machine.wait_until_succeeds(f"{preflight}https://demo.${platform.domain} | tr -d '\\r' | grep -Fix 'access-control-allow-origin: https://demo.${platform.domain}'", timeout=30)
              machine.fail(f"{preflight}https://evil.example | grep -Fi access-control-allow-origin")
              machine.fail(f"{preflight}https://${platform.domain} | grep -Fi access-control-allow-origin")
              # A hostile client reaching a non-loopback origin address cannot
              # select the management router merely by supplying its Host.
              machine.fail("ip=$(hostname -I | awk '{print $1}'); ${pkgs.curl}/bin/curl --fail --silent --max-time 2 --header 'Host: ${platform.domain}' http://$ip/")
            ''
          else if role == "storage" then
            ''
              machine.wait_for_unit("nginx.service")
              machine.succeed("${pkgs.nginx}/bin/nginx -t -c /etc/nginx/nginx.conf")
              machine.succeed("mountpoint -q ${platform.paths.data}")
              machine.succeed("${pkgs.podman}/bin/podman load --input /etc/vm-instance-images/postgres.tar >/dev/null")
              # Check the image's shell/ELF execution under a read-only rootfs
              # before initdb's shell-based version probe obscures the cause.
              machine.succeed("${pkgs.podman}/bin/podman run --rm --read-only --network=none --cgroups=disabled --user=999:999 --entrypoint=/bin/sh localhost/vm-instance-postgres:latest -ec 'exec /bin/postgres -V'")
              machine.succeed("${pkgs.podman}/bin/podman load --input /etc/vm-instance-images/mongo.tar >/dev/null")
              machine.succeed("systemctl start ${namespace}-storage-instance-manager.service")
              machine.wait_for_open_port(19002)
              machine.succeed("${packages.platformPython}/bin/python ${storageInstanceProbe}")
            ''
          else if role == "worker" then
            ''
              machine.wait_for_unit("docker.service")
              machine.wait_for_unit("nomad.service")
              machine.succeed("${pkgs.docker}/bin/docker info >/dev/null")
              machine.succeed("${pkgs.iptables}/bin/iptables -C OUTPUT -d ${platform.metadataAddress}/32 -j REJECT")
              machine.succeed("test -x /etc/cni/bin/bridge")
            ''
          else
            ''
              machine.wait_for_unit("default.target", "agentops")
              machine.wait_for_unit("buildkit.service", "agentops")
              machine.succeed("runuser -u agentops -- env XDG_RUNTIME_DIR=/run/user/1000 ${packages.buildkit}/bin/buildctl --addr unix:///run/user/1000/buildkit/buildkitd.sock debug workers")
              machine.succeed("${pkgs.iptables}/bin/iptables -C OUTPUT -d ${platform.metadataAddress}/32 -j REJECT")
              machine.succeed("systemctl is-active --quiet ${namespace}-builder-expiry.timer")
            ''
        }

        machine.succeed("test -z \"$(systemctl --failed --no-legend)\"")
      '';
    };
in
lib.genAttrs constants.roles mkRoleTest
