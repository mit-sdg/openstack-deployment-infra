{ pkgs, platform }:
let
  lib = pkgs.lib;
  constants = import ../lib/constants.nix;
  namespace = platform.namespace;
  root = platform.paths.root;
  state = platform.paths.adminState;
  backups = platform.paths.backups;
  packages = import ../pkgs { inherit pkgs platform; };
  managementIdentityBootstrap = pkgs.writeText "management-identity-bootstrap.py" ''
    import faulthandler
    import signal
    import sys
    # Test-only: SIGUSR1 dumps every thread's stack without stopping the service.
    stacks = open("/run/${namespace}-management-identity/stacks.txt", "w")
    faulthandler.register(signal.SIGUSR1, file=stacks, all_threads=True)
    sys.path.insert(0, "${packages.controllerPackage}/${pkgs.python314.sitePackages}")
    from openstack_platform.management.identity.main import main
    main()
  '';
  managementIdentityDiagnostics = pkgs.writeShellScript "management-identity-diagnostics" ''
    set -u
    unit=${namespace}-management-identity.service
    sock=/run/${namespace}-management-identity/identity.sock
    body='{"username":"alice","password":"vm-fixture"}'
    echo "== dns"; getent hosts class.example.com
    echo "== commons"; ${pkgs.curl}/bin/curl -sS --max-time 5 -o /dev/null -w 'commons-http=%{http_code}\n' -H 'Content-Type: application/json' --data "$body" https://class.example.com:9444/api/auth/authenticate
    echo "== socket"; ls -ln /run/${namespace}-management-identity; id management-broker
    pid=$(systemctl show -p MainPID --value "$unit"); echo "identity-pid=$pid"
    grep -E '^(State|Threads):' "/proc/$pid/status"
    echo "== paths"; stat -c '%n %U:%G %a inode=%i type=%F' /run /run/${namespace}-management-identity "$sock"
    echo "== listening"; ${pkgs.iproute2}/bin/ss -xlpn | grep -F management-identity || echo "no listener on identity path"
    echo "== fds"; ls -l "/proc/$pid/fd" 2>&1 | grep -F socket || true
    echo "== mountinfo"; grep -F management-identity "/proc/$pid/mountinfo" || echo "no identity-specific mount"
    echo "== connect-errno"; runuser -u management-broker -- ${packages.platformPython}/bin/python -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); print('broker connect ok')" "$sock" 2>&1 | tail -n 1
    echo "== root-health"; ${pkgs.curl}/bin/curl -sS --max-time 5 -w '\nroot-health-http=%{http_code}\n' --unix-socket "$sock" http://localhost/v1/health
    echo "== health"; runuser -u management-broker -- ${pkgs.curl}/bin/curl -sS --max-time 5 -w '\nhealth-http=%{http_code}\n' --unix-socket "$sock" http://localhost/v1/health
    echo "== authenticate"; runuser -u management-broker -- ${pkgs.curl}/bin/curl -sS --max-time 10 -w '\nidentity-http=%{http_code}\n' --unix-socket "$sock" -H 'Content-Type: application/json' --data "$body" http://localhost/v1/authenticate
    echo "== tcp-from-identity"; ${pkgs.iproute2}/bin/ss -tanp | grep -F "pid=$pid," || echo "no identity tcp sockets"
    echo "== stacks"; kill -USR1 "$pid"; sleep 1; cat /run/${namespace}-management-identity/stacks.txt
    echo "== journal"; journalctl --no-pager -o short-monotonic -u "$unit" | tail -n 40
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
    # Verify the integration once, then permit restart during identity outages.
    if not proof.exists():
        client = ProjectClient(Path("/run/${namespace}-management-identity/identity.sock"), timeout=10)
        deadline = time.monotonic() + 30
        last = "no attempt"
        while True:
            try:
                status, result = client.request("POST", "/v1/authenticate", {"username":"alice","password":"vm-fixture"})
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
  delayedController = pkgs.writeShellScript "vm-delayed-controller" ''
    # Type=simple is active before its controller sockets exist. Hold this gap
    # open so the broker's real sandbox must wait on API readiness, not After.
    ${pkgs.coreutils}/bin/sleep 3
    exec ${packages.controllerPackage}/bin/openstack-platform-controller "$@"
  '';
  managementFakeCommons = pkgs.writeText "vm-fake-commons.py" ''
    import json, socket, ssl, sys
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
            accepted=self.path=="/api/auth/authenticate" and body=={"username":"alice","password":"vm-fixture"}
            value={"user":"11111111-1111-4111-8111-111111111111","username":"alice","displayName":"Alice","email":"alice@example.com"} if accepted else {"error":"UNAUTHORIZED"}
            raw=json.dumps(value).encode()
            self.send_response(200 if accepted else 401)
            self.send_header("Content-Type","application/json")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
    server=ThreadingHTTPServer(("127.0.0.1",9444),Handler)
    server.daemon_threads=True
    server.serve_forever()
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

  registryBackupCredentialProbe = pkgs.writeText "registry-backup-credential-probe.py" ''
    import os
    import shutil
    import subprocess
    import stat
    import sys
    from pathlib import Path

    sys.path.insert(0, "${../../infra}")
    from backup.registry_artifact import credentials, _runtime_paths

    Path("${state}/operator/status/registry-backup-probe-ran").touch()
    for name in ("newuidmap", "newgidmap"):
        assert shutil.which(name) == "/run/wrappers/bin/" + name
    subprocess.run(
        ["${pkgs.podman}/bin/podman", "unshare", "${pkgs.coreutils}/bin/true"],
        check=True, capture_output=True, timeout=30,
    )
    loaded = Path("${state}/operator/secrets/storage-bootstrap.env")
    private = Path("/run/${namespace}-backup-private/storage-bootstrap.env")
    assert _runtime_paths()[0] == private
    assert private.read_bytes() == loaded.read_bytes()
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
    assert private.stat().st_uid == os.geteuid()
    assert credentials(private).startswith("Basic ")
    assert Path(os.environ["AGE_KEY"]).is_file()
    shared = Path("${root}/secrets/storage-bootstrap.env")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o640
    try:
        credentials(shared)
    except RuntimeError:
        pass
    else:
        raise AssertionError("registry accepted a group-readable shared source")
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
        { lib, pkgs, ... }:
        {
          imports = [
            ../modules/common.nix
            (../roles + "/${role}.nix")
          ];

          _module.args = { inherit constants platform role; };

          virtualisation = {
            memorySize = if role == "storage" then 3072 else 2048;
            cores = 2;
          };

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
                what = "tmpfs";
                where = platform.paths.data;
                type = "tmpfs";
                options = "mode=0750";
                wantedBy = [ "multi-user.target" ];
              }
            ];

          systemd.services = lib.mkMerge [
            (lib.mkIf (role == "admin") {
              # Exercise the real backup unit's User, Environment, private copy
              # and source guards without contacting any managed service.
              "${namespace}-platform-backup".serviceConfig.ExecStart =
                lib.mkForce "${packages.python}/bin/python ${registryBackupCredentialProbe}";
              "${namespace}-management-identity".serviceConfig = {
                # Mirror production: the fake class app on loopback plus the
                # local resolver stubs used for name resolution.
                IPAddressAllow = lib.mkForce [
                  "127.0.0.1/32"
                  "127.0.0.53/32"
                  "127.0.0.54/32"
                ];
                # Keep a deliberately broken Type=simple process failed so the
                # outage assertion can observe it without racing automatic retries.
                Restart = lib.mkForce "no";
              };
              "vm-restore-active-portal" = {
                # The admin VM uses disposable tmpfs state. Restore a saved
                # active pair before path units evaluate it on the second boot.
                wantedBy = [ "multi-user.target" ];
                after = [ "${systemdEscapePath state}.mount" ];
                before = [
                  "${namespace}-management-broker.path"
                  "${namespace}-management-web.path"
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
                  cp -a /var/lib/portal-boot-fixture/. ${state}/
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
                serviceConfig.ExecStart = lib.mkForce (
                  lib.concatStringsSep " " [
                    delayedController
                    "--platform-config /etc/${namespace}/platform.json"
                    "--state-directory ${state}/controller/state"
                    "--policy ${state}/controller/policy.json"
                    "--socket /run/${namespace}-controller/project.sock"
                    "--socket-group controller-api"
                    "--project-peer ${toString constants.accounts.managementBroker.uid}:${toString constants.accounts.managementBroker.gid}"
                    "--privileged-socket /run/${namespace}-controller/privileged.sock"
                    "--privileged-socket-group platform-admin"
                    "--privileged-peer ${toString constants.accounts.operator.uid}:${toString constants.accounts.operator.gid}"
                    "--max-connections-per-peer 8"
                  ]
                );
              };
            })
            (lib.mkIf (role == "ingress") {
              "${namespace}-ingress-readiness".wantedBy = lib.mkForce [ ];
            })
            (lib.mkIf (role == "storage") {
              "${namespace}-storage-readiness".wantedBy = lib.mkForce [ ];
              "podman-${namespace}-postgres".wantedBy = lib.mkForce [ ];
              "podman-${namespace}-mongodb".wantedBy = lib.mkForce [ ];
              "podman-${namespace}-garage".wantedBy = lib.mkForce [ ];
              "podman-${namespace}-registry".wantedBy = lib.mkForce [ ];
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
        machine.wait_for_unit("cloud-final.service")
        machine.succeed("getent passwd agentops >/dev/null")
        machine.succeed("getent passwd ubuntu >/dev/null")
        machine.succeed("test -r /etc/${namespace}/platform.json")
        machine.succeed("python3 -c 'import json; json.load(open(\"/etc/${namespace}/platform.json\"))'")

        ${
          if role == "admin" then
            ''
              # systemd silently drops a job to break an ordering cycle; never
              # accept a boot that needed that.
              machine.succeed("! journalctl --boot --output=cat | grep -F 'Found ordering cycle'")
              machine.wait_for_unit("nomad.service")
              machine.wait_for_unit("${namespace}-admin-readiness.service")
              machine.wait_for_unit("${namespace}-controller.service")
              machine.wait_for_unit("${namespace}-controller-readiness.service")
              machine.succeed("systemctl is-active --quiet nomad.service")
              machine.succeed("systemctl is-active --quiet ${namespace}-controller.service")
              machine.succeed("systemctl cat ${namespace}-controller.path | grep -Fx 'PathExists=${state}/operator/helper-releases/current/.complete'")
              machine.fail("systemctl cat ${namespace}-controller.path | grep -F 'PathExists=${state}/operator/policy.json'")
              machine.fail("systemctl cat ${namespace}-controller.path | grep -F 'PathExists=${state}/operator/image-selections.json'")
              machine.succeed("${pkgs.curl}/bin/curl --fail --silent --cacert /etc/${namespace}/pki/internal-ca.pem --cert /etc/${namespace}/pki/nomad-cli.pem --key /etc/${namespace}/pki/nomad-cli-key.pem https://127.0.0.1:4646/v1/status/leader >/dev/null")
              machine.succeed("${packages.platformPython}/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 14)'")
              machine.succeed("${packages.controllerPackage}/bin/openstack-platform-controller --help >/dev/null")
              machine.succeed("openstack-platform-install-release --help >/dev/null")
              machine.succeed("test $(stat -c %a /run/${namespace}-controller/project.sock) = 660")
              machine.succeed("test $(stat -c %U /run/${namespace}-controller/project.sock) = platform-controller")
              machine.succeed("test $(stat -c %G /run/${namespace}-controller/project.sock) = controller-api")
              machine.succeed("test $(stat -c %G /run/${namespace}-controller/privileged.sock) = platform-admin")
              machine.succeed("runuser -u management-broker -- ${pkgs.curl}/bin/curl --fail --silent --unix-socket /run/${namespace}-controller/project.sock 'http://localhost/v1/health' | grep -F '\"status\":\"ok\"'")
              machine.fail("runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --unix-socket /run/${namespace}-controller/project.sock 'http://localhost/v1/health'")
              machine.fail("runuser -u management-broker -- ${pkgs.curl}/bin/curl --fail --silent --unix-socket /run/${namespace}-controller/project.sock 'http://localhost/v1/admin/applications?limit=1'")
              machine.fail("runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --unix-socket /run/${namespace}-controller/privileged.sock 'http://localhost/v1/admin/applications?limit=1'")
              machine.succeed("runuser -u agentops -- ${pkgs.curl}/bin/curl --fail --silent --unix-socket /run/${namespace}-controller/privileged.sock 'http://localhost/v1/admin/applications?limit=1' >/dev/null")
              machine.fail("runuser -u management-web -- cat ${state}/controller/policy.json")
              machine.succeed("runuser -u management-web -- sh -c 'for name in openstack.env nomad-tokens.env storage-bootstrap.env builder_operator_ed25519 backup-age-key.txt; do test ! -r ${state}/operator/secrets/\"$name\" || exit 1; done'")
              machine.fail("runuser -u management-web -- cat /etc/${namespace}/pki/nomad-cli-key.pem")
              machine.fail("runuser -u management-web -- cat /run/credentials/nomad.service/nomad-gossip-key")
              machine.fail("runuser -u management-web -- sh -c 'cat /run/credentials/nomad.service/nomad-gossip-key'")
              machine.succeed("pid=$(systemctl show ${namespace}-controller.service -p MainPID --value); ! tr '\\0' '\\n' </proc/$pid/environ | grep -F controller-secret")
              machine.succeed("! journalctl --boot --output=cat | grep -F controller-secret")
              machine.succeed("! grep -R -a -F controller-secret ${state}/controller ${backups}/${constants.directories.controllerBackup}")
              machine.succeed("systemctl show ${namespace}-controller.service nomad.service -p LimitCORE --value | grep -vFx infinity")
              machine.succeed("test ! -e /proc/sys/kernel/core_pattern || ! systemctl is-enabled systemd-coredump.socket 2>/dev/null")
              machine.succeed("! systemctl cat ${namespace}-platform-backup.service | grep -F 'LoadCredential='")
              machine.succeed("systemctl cat ${namespace}-platform-backup.service | grep -F 'REGISTRY_BACKUP_SECRETS=%t/${namespace}-backup-private/storage-bootstrap.env'")
              machine.succeed("systemctl start ${namespace}-platform-backup.service && test -f ${state}/operator/status/registry-backup-probe-ran && rm ${state}/operator/status/registry-backup-probe-ran")
              machine.succeed("test $(stat -c %U:%G:%a ${root}/secrets/storage-bootstrap.env) = agentops:platform-controller:640")
              machine.fail("test -e /run/credentials/${namespace}-platform-backup.service/storage-bootstrap")
              machine.fail("test -e /run/${namespace}-backup-private")
              machine.succeed("chmod 0644 ${root}/secrets/storage-bootstrap.env")
              machine.fail("systemctl start ${namespace}-platform-backup.service")
              machine.fail("test -e ${state}/operator/status/registry-backup-probe-ran")
              machine.succeed("chmod 0640 ${root}/secrets/storage-bootstrap.env; chgrp agentops ${root}/secrets/storage-bootstrap.env; systemctl reset-failed ${namespace}-platform-backup.service")
              machine.fail("systemctl start ${namespace}-platform-backup.service")
              machine.fail("test -e ${state}/operator/status/registry-backup-probe-ran")
              machine.succeed("chown root:platform-controller ${root}/secrets/storage-bootstrap.env; systemctl reset-failed ${namespace}-platform-backup.service")
              machine.fail("systemctl start ${namespace}-platform-backup.service")
              machine.fail("test -e ${state}/operator/status/registry-backup-probe-ran")
              machine.succeed("chown agentops:platform-controller ${root}/secrets/storage-bootstrap.env; mv ${root}/secrets/storage-bootstrap.env ${root}/secrets/storage-bootstrap.real; ln -s storage-bootstrap.real ${root}/secrets/storage-bootstrap.env; systemctl reset-failed ${namespace}-platform-backup.service")
              machine.fail("systemctl start ${namespace}-platform-backup.service")
              machine.fail("test -e ${state}/operator/status/registry-backup-probe-ran")
              machine.succeed("rm ${root}/secrets/storage-bootstrap.env; mv ${root}/secrets/storage-bootstrap.real ${root}/secrets/storage-bootstrap.env; systemctl reset-failed ${namespace}-platform-backup.service && systemctl start ${namespace}-platform-backup.service && test -f ${state}/operator/status/registry-backup-probe-ran")
              machine.succeed("! journalctl --boot --output=cat | grep -F controller-secret")
              machine.succeed("systemctl cat nomad.service | grep -F 'LoadCredential=nomad-gossip-key:/etc/${namespace}/secrets/nomad-gossip-key'")
              machine.succeed("systemctl cat nomad.service | grep -F '${namespace}-credential-guard /etc/${namespace}/secrets/nomad-gossip-key root'")
              machine.succeed("runuser -u platform-controller -- cat ${state}/operator/secrets/openstack.env >/dev/null")
              machine.fail("runuser -u nomad -- cat ${state}/operator/secrets/openstack.env")
              machine.succeed("id -nG management-web | grep -Fx 'management-web'")
              machine.succeed("id -nG management-broker | grep -Fx 'management-broker controller-api'")
              machine.succeed("test $(stat -c %U:%G:%a ${state}/controller) = platform-controller:platform-controller:700")
              machine.succeed("test $(stat -c %U:%G:%a ${state}/controller/state) = platform-controller:platform-controller:700")
              machine.succeed("test $(stat -c %U:%G:%a ${state}/operator/helper-releases) = agentops:agentops:750")
              machine.succeed("test $(stat -c %U:%a ${state}/operator/policy.json) = agentops:600")
              machine.succeed("systemctl show ${namespace}-controller.service -p ProtectSystem --value | grep -Fx strict")
              machine.succeed("systemctl show ${namespace}-controller.service -p NoNewPrivileges --value | grep -Fx yes")
              machine.succeed("systemctl cat ${namespace}-management-broker.service | grep -F 'CONTROLLER_PROJECT_SOCKET=/run/${namespace}-controller/project.sock'")
              machine.succeed("systemctl cat ${namespace}-management-web.service | grep -F 'MANAGEMENT_BROKER_SOCKET=/run/${namespace}-management-broker/broker.sock'")
              machine.succeed("! systemctl cat ${namespace}-management-web.service | grep -F 'CONTROLLER_PROJECT_SOCKET='")
              machine.succeed("systemctl show ${namespace}-management-web.service -p IPAddressDeny --value | grep -F 0.0.0.0/0")
              machine.succeed("systemctl show ${namespace}-management-web.service -p InaccessiblePaths --value | grep -F '${state}/operator'")
              machine.wait_for_unit("${namespace}-management-prepare.service")
              # Root preparation must not chmod/chown a target reached through
              # an operator-controlled release/config or backup directory link.
              victim = "${state}/root-preparation-victim"
              machine.succeed(f"install -d -m 0755 -o root -g root {victim}; printf 'protected fixture\\n' > {victim}/sentinel; chmod 0600 {victim}/sentinel")
              for child in ("config", "releases"):
                  path = f"${state}/management-broker-releases/{child}"
                  for kind in ("symlink", "fifo", "file"):
                      create = f"ln -s {victim} {path}" if kind == "symlink" else (f"mkfifo {path}" if kind == "fifo" else f"touch {path}")
                      machine.succeed(f"runuser -u agentops -- sh -c 'mv {path} {path}.saved; {create}'")
                      machine.fail("systemctl restart ${namespace}-management-prepare.service")
                      machine.succeed(f"test $(stat -c %u:%g:%a {victim}) = 0:0:755; grep -Fx 'protected fixture' {victim}/sentinel; test ! -e {victim}/platform.json")
                      machine.succeed(f"runuser -u agentops -- sh -c 'rm {path}; mv {path}.saved {path}'; systemctl reset-failed ${namespace}-management-prepare.service")
              machine.succeed("systemctl reset-failed ${namespace}-management-prepare.service; systemctl restart ${namespace}-management-prepare.service")
              backup_prepare = machine.succeed("systemctl cat ${namespace}-management-broker-backup.service | sed -n 's/^ExecStartPre=+//p'").strip()
              machine.succeed(backup_prepare)
              backup_dir = "${backups}/${constants.directories.managementBrokerBackup}"
              machine.succeed(f"runuser -u agentops -- sh -c 'mv {backup_dir} {backup_dir}.saved; ln -s {victim} {backup_dir}'")
              machine.fail(backup_prepare)
              machine.succeed(f"test $(stat -c %u:%g:%a {victim}) = 0:0:755; grep -Fx 'protected fixture' {victim}/sentinel")
              machine.succeed(f"runuser -u agentops -- sh -c 'rm {backup_dir}; mv {backup_dir}.saved {backup_dir}'")
              # Existing operator-owned directories must not be given to a service.
              machine.succeed(f"runuser -u agentops -- sh -c 'mv {backup_dir} {backup_dir}.saved; mkdir -m 2750 {backup_dir}'")
              backup_metadata = machine.succeed(f"stat -c %u:%g:%a:%h {backup_dir}").strip()
              machine.fail(backup_prepare)
              assert machine.succeed(f"stat -c %u:%g:%a:%h {backup_dir}").strip() == backup_metadata
              machine.succeed(f"runuser -u agentops -- sh -c 'rmdir {backup_dir}; mv {backup_dir}.saved {backup_dir}'")
              backup_mount = machine.succeed("systemd-escape --path --suffix=mount ${backups}").strip()
              machine.succeed(f"! systemctl show ${namespace}-management-prepare.service -p Requires -p After -p RequiresMountsFor | grep -F '{backup_mount}'")
              machine.succeed("systemctl show ${namespace}-management-prepare.service -p RequiresMountsFor --value | grep -Fx '${state}'")
              machine.succeed("systemctl show ${namespace}-management-broker-backup.service -p RequiresMountsFor --value | tr ' ' '\\n' | grep -Fx '${backups}'")
              machine.succeed("systemctl cat ${namespace}-management-broker-backup.service | grep -F 'ExecStartPre=+'")
              for component in ("web", "broker"):
                  machine.succeed(f"test $(stat -c %U:%G:%a ${state}/management-{component}-releases) = agentops:management-{component}:2750")
                  machine.succeed(f"test ! -e ${state}/management-{component}-releases/config/platform.json")
                  machine.succeed(f"systemctl show ${namespace}-management-{component}.service -p LimitCORE --value | grep -Fx 0")
                  machine.succeed(f"systemctl show ${namespace}-management-{component}.service -p UMask --value | grep -Fx 0077")
                  machine.succeed(f"systemctl cat ${namespace}-management-{component}.service | grep -F RequiresMountsFor=${state}")
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
              # Restarting the required prepare unit can stop its path units.
              # Re-arm them after the deliberate preparation failures above.
              machine.succeed("systemctl reset-failed ${namespace}-management-broker.path ${namespace}-management-web.path ${namespace}-management-activate.path; systemctl start ${namespace}-management-broker.path ${namespace}-management-web.path ${namespace}-management-activate.path")
              for component in ("broker", "web", "activate"):
                  machine.wait_for_unit(f"${namespace}-management-{component}.path")
              machine.succeed(f"runuser -u agentops -- sh -c 'umask 0027; printf \"{commit}\\n{pair}\\n\" > ${state}/management-broker-releases/activate-request'")
              machine.wait_for_unit("${namespace}-management-broker.service")
              # Record reachability from outside the sandbox before waiting, so a
              # failure separates host DNS/TLS problems from sandbox restrictions.
              print(machine.execute("${managementIdentityDiagnostics} 2>&1")[1])
              machine.wait_until_succeeds("test -f ${state}/management-broker/identity-sandbox-ok")
              broker_health = "runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --max-time 2 --unix-socket /run/${namespace}-management-broker/broker.sock http://localhost/v1/health | grep -F '\"status\":\"ok\"'"
              machine.wait_until_succeeds(broker_health)
              machine.succeed("systemctl show ${namespace}-management-broker.service -p MemoryDenyWriteExecute --value | grep -Fx yes")
              machine.succeed("systemctl show ${namespace}-management-broker.service -p RestrictAddressFamilies --value | grep -Fx AF_UNIX")
              machine.succeed("systemctl show ${namespace}-management-broker.service -p IPAddressDeny --value | grep -F 0.0.0.0/0")
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-management-broker) = management-broker:management-web:750")
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-management-broker/broker.sock) = management-broker:management-web:660")
              machine.succeed(broker_health)
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-management-identity) = management-identity:management-broker:750")
              machine.succeed("test $(stat -c %U:%G:%a /run/${namespace}-management-identity/identity.sock) = management-identity:management-broker:660")
              machine.succeed("runuser -u management-broker -- ${pkgs.curl}/bin/curl --fail --silent --unix-socket /run/${namespace}-management-identity/identity.sock http://localhost/v1/health")
              machine.fail("runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --unix-socket /run/${namespace}-management-identity/identity.sock http://localhost/v1/health")
              machine.fail("runuser -u management-identity -- cat ${state}/management-broker/identity-sandbox-ok")
              machine.succeed("systemctl show ${namespace}-management-identity.service -p InaccessiblePaths --value | grep -F '${state}/management-broker'")
              machine.succeed("systemctl is-enabled ${namespace}-management-broker-backup.timer")
              machine.fail("runuser -u management-web -- systemctl restart ${namespace}-management-broker.service")
              machine.fail("runuser -u management-broker -- systemctl restart ${namespace}-management-web.service")
              machine.succeed("systemctl cat ${namespace}-management-activate.path | grep -F 'PathChanged=${state}/management-broker-releases/activate-request'")
              machine.wait_for_unit("${namespace}-management-web.service")
              machine.wait_until_succeeds("test $(systemctl show ${namespace}-management-activate.service -p ActiveState --value) = inactive && test $(systemctl show ${namespace}-management-activate.service -p Result --value) = success")
              machine.succeed("systemctl cat ${namespace}-management-web.service | grep -F '${state}/management-active/current/web/bin/management-web'")
              machine.succeed("runuser -u management-web -- cat ${state}/management-active/current/web/config/platform.json >/dev/null")
              machine.fail("runuser -u management-web -- cat ${state}/management-active/current/broker/config/platform.json")
              machine.succeed("! grep -F PLATFORM_ENVIRONMENT= /etc/profile")
              selected_pair = machine.succeed("readlink ${state}/management-active/current").strip()
              web_health = "runuser -u management-web -- ${pkgs.curl}/bin/curl --fail --silent --max-time 2 --unix-socket ${state}/management-web/web-health.sock http://localhost/v1/health | grep -F '\"status\":\"ok\"'"
              # Persist this already-active pair across the VM's disposable
              # state mount, then boot without any reset-failed/reactivation.
              machine.succeed("mkdir -p /var/lib/portal-boot-fixture; cp -a ${state}/management-active ${state}/management-broker-releases ${state}/management-web-releases ${state}/management-broker ${state}/management-web /var/lib/portal-boot-fixture/; rm -f /var/lib/portal-boot-fixture/management-broker-releases/activate-request")
              machine.reboot()
              machine.wait_for_unit("multi-user.target")
              machine.wait_for_unit("${namespace}-controller-readiness.service")
              for component in ("identity", "broker", "web"):
                  machine.wait_for_unit(f"${namespace}-management-{component}.service")
              for component in ("broker", "web"):
                  machine.wait_for_unit(f"${namespace}-management-{component}.path")
              machine.wait_until_succeeds(broker_health)
              machine.wait_until_succeeds(web_health)
              machine.succeed("test -z \"$(systemctl --failed --no-legend)\"")
              assert machine.succeed("readlink ${state}/management-active/current").strip() == selected_pair
              # Force a controller restart, including socket directory removal,
              # and verify the dependency restart chain heals both portal units.
              before_broker = machine.succeed("systemctl show ${namespace}-management-broker.service -p MainPID --value").strip()
              before_web = machine.succeed("systemctl show ${namespace}-management-web.service -p MainPID --value").strip()
              machine.succeed("systemctl restart ${namespace}-controller.service")
              machine.wait_for_unit("${namespace}-controller-readiness.service")
              machine.wait_until_succeeds(broker_health)
              machine.wait_until_succeeds(web_health)
              for component in ("identity", "broker", "web"):
                  machine.succeed(f"systemctl is-active --quiet ${namespace}-management-{component}.service")
              for component in ("broker", "web"):
                  machine.succeed(f"systemctl is-active --quiet ${namespace}-management-{component}.path")
                  machine.succeed(f"! systemctl is-failed --quiet ${namespace}-management-{component}.path")
              assert machine.succeed("systemctl show ${namespace}-management-broker.service -p MainPID --value").strip() != before_broker
              assert machine.succeed("systemctl show ${namespace}-management-web.service -p MainPID --value").strip() != before_web
              machine.succeed("test -z \"$(systemctl --failed --no-legend)\"")

              # A staged broker upgrade remains inactive through service and
              # boot-path restarts. The VM state mount is disposable tmpfs.
              next_descriptor = descriptor.replace(pair, "c" * 64)
              next_broker = "${state}/management-broker-releases/releases/vm-next"
              # Exercise refusal synchronously; the watcher is tested separately.
              machine.succeed("systemctl stop ${namespace}-management-activate.path")
              machine.succeed(f"cp -a ${state}/management-broker-releases/releases/vm-test {next_broker}; printf '%s\\n' '{next_descriptor}' > {next_broker}/evidence/management-artifacts.json")
              machine.succeed("ln -sfn releases/vm-next ${state}/management-broker-releases/current; chown -h agentops:management-broker ${state}/management-broker-releases/current")
              machine.succeed(f"runuser -u agentops -- sh -c 'printf \"{commit}\\n{'c' * 64}\\n\" > ${state}/management-broker-releases/activate-request'")
              machine.fail("systemctl restart ${namespace}-management-activate.service")
              assert machine.succeed("readlink ${state}/management-active/current").strip() == selected_pair
              machine.succeed("systemctl restart ${namespace}-management-broker.path ${namespace}-management-web.path ${namespace}-management-broker.service ${namespace}-management-web.service")
              assert machine.succeed("readlink ${state}/management-active/current").strip() == selected_pair
              machine.succeed("test $(readlink -f ${state}/management-active/current/broker) = ${state}/management-broker-releases/releases/vm-test")
              machine.succeed("ln -sfn releases/vm-test ${state}/management-broker-releases/current; chown -h agentops:management-broker ${state}/management-broker-releases/current; systemctl reset-failed ${namespace}-management-activate.service")
              machine.succeed(f"runuser -u agentops -- sh -c 'printf \"{commit}\\n{pair}\\n\" > ${state}/management-broker-releases/activate-request'")
              machine.succeed("systemctl restart ${namespace}-management-activate.service; systemctl start ${namespace}-management-activate.path")
              machine.wait_until_succeeds(broker_health)
              machine.succeed("systemctl show ${namespace}-management-broker.service -p Wants --value | tr ' ' '\\n' | grep -Fx '${namespace}-management-identity.service'")
              machine.succeed("systemctl show ${namespace}-management-broker.service -p After --value | tr ' ' '\\n' | grep -Fx '${namespace}-management-identity.service'")
              machine.succeed("! systemctl show ${namespace}-management-broker.service -p Requires --value | tr ' ' '\\n' | grep -Fx '${namespace}-management-identity.service'")
              # A real identity start failure preserves existing broker/web units
              # and does not prevent starting the broker again.
              machine.succeed("cp -p ${state}/management-broker-releases/releases/vm-test/config/identity.json ${state}/management-broker-releases/releases/vm-test/config/identity.vm-save; printf '{}\\n' > ${state}/management-broker-releases/releases/vm-test/config/identity.json")
              # Type=simple reports the start job's success before Python exits.
              machine.execute("systemctl restart ${namespace}-management-identity.service")
              machine.wait_until_succeeds("systemctl is-failed --quiet ${namespace}-management-identity.service")
              machine.succeed("test $(systemctl show ${namespace}-management-identity.service -p Result --value) = exit-code")
              machine.succeed("systemctl is-active ${namespace}-management-broker.service ${namespace}-management-web.service")
              machine.succeed("systemctl restart ${namespace}-management-broker.service; systemctl start ${namespace}-management-web.service")
              machine.wait_until_succeeds(broker_health)
              machine.succeed("mv ${state}/management-broker-releases/releases/vm-test/config/identity.vm-save ${state}/management-broker-releases/releases/vm-test/config/identity.json; systemctl reset-failed ${namespace}-management-identity.service; systemctl restart ${namespace}-management-identity.service")
              before = machine.succeed("systemctl show ${namespace}-management-broker.service -p MainPID --value").strip()
              machine.succeed("systemctl reset-failed ${namespace}-management-activate.service ${namespace}-management-broker.service ${namespace}-management-web.service")
              machine.succeed(f"runuser -u agentops -- sh -c 'umask 0027; printf \"{commit}\\n{pair}\\n\" > ${state}/management-broker-releases/activate-request'")
              machine.wait_until_succeeds(f"pid=$(systemctl show ${namespace}-management-broker.service -p MainPID --value); test $pid -gt 0 && test $pid != {before}")
              machine.wait_for_unit("${namespace}-management-web.service")
              machine.wait_until_succeeds("test $(systemctl show ${namespace}-management-activate.service -p ActiveState --value) = inactive && test $(systemctl show ${namespace}-management-activate.service -p Result --value) = success")
              machine.wait_until_succeeds(broker_health)
              before = machine.succeed("systemctl show ${namespace}-management-broker.service -p MainPID --value").strip()
              machine.succeed("systemctl reset-failed ${namespace}-management-activate.service ${namespace}-management-broker.service ${namespace}-management-web.service")
              machine.succeed(f"runuser -u agentops -- sh -c 'printf \"{commit}\\n{pair}\\n\" > ${state}/management-broker-releases/activate-request'")
              machine.wait_until_succeeds(f"pid=$(systemctl show ${namespace}-management-broker.service -p MainPID --value); test $pid -gt 0 && test $pid != {before}")
              machine.wait_for_unit("${namespace}-management-web.service")
              machine.wait_until_succeeds("test $(systemctl show ${namespace}-management-activate.service -p ActiveState --value) = inactive && test $(systemctl show ${namespace}-management-activate.service -p Result --value) = success")
              machine.wait_until_succeeds(broker_health)
              machine.fail("openstack-platform-management-broker-restore --yes")
              # Backup outages must not pull down the owner portal.
              # "mask --runtime" cannot override NixOS units in /etc; a runtime
              # drop-in can. A failing Assert keeps the volume unstartable.
              machine.succeed(f"systemctl stop '{backup_mount}'; install -d '/run/systemd/system/{backup_mount}.d'; printf '[Unit]\\nAssertPathExists=/run/vm-test-backup-outage-never-exists\\n' > '/run/systemd/system/{backup_mount}.d/outage.conf'; systemctl daemon-reload")
              machine.fail(f"systemctl start '{backup_mount}'")
              machine.succeed("systemctl restart ${namespace}-management-broker.service ${namespace}-management-web.service")
              machine.wait_for_unit("${namespace}-management-broker.service")
              machine.wait_for_unit("${namespace}-management-web.service")
              machine.wait_until_succeeds(broker_health)
              machine.fail("systemctl start ${namespace}-management-broker-backup.service")
              machine.succeed(f"rm -r '/run/systemd/system/{backup_mount}.d'; systemctl daemon-reload; systemctl reset-failed '{backup_mount}'; systemctl start '{backup_mount}'")
              # Remounting disposable tmpfs loses its fixture directories.
              # Recreate only backup paths before the remaining assertions.
              machine.succeed("${pkgs.systemd}/bin/systemd-tmpfiles --create --prefix=${backups}")
              machine.succeed("systemctl reset-failed ${namespace}-management-broker-backup.service")
              machine.succeed("${pkgs.iptables}/bin/iptables -C nixos-fw -p tcp -s ${platform.addresses.ingress}/32 --dport 8080 -j nixos-fw-accept")
              machine.fail("${pkgs.curl}/bin/curl --fail --silent --max-time 1 http://127.0.0.1:8080/")
              machine.succeed("${root}/bin/openstack-platform-helper </dev/null | grep -F INVALID_REQUEST")
              # The control plane calls the helper by this name, and a tmpfiles
              # rule owns it, so it must reach the accepted release rather than
              # run the helper module without PLATFORM_CONFIG.
              machine.succeed(
                  "install -d -m 0750 ${state}/operator/helper-releases/current/bin"
              )
              machine.succeed(
                  "printf '#!/bin/sh\\necho delegated-to-release\\n' "
                  "> ${state}/operator/helper-releases/current/bin/openstack-platform-helper"
              )
              machine.succeed(
                  "chmod 0550 ${state}/operator/helper-releases/current/bin/openstack-platform-helper"
              )
              machine.succeed("printf 'commit\\n' > ${state}/operator/helper-releases/current/.complete")
              machine.succeed(
                  "${root}/bin/openstack-platform-helper </dev/null | grep -Fx delegated-to-release"
              )
              machine.succeed("rm -rf ${state}/operator/helper-releases/current")
              machine.succeed("test -d ${state}/operator/helper-releases/releases")
              machine.succeed("test -d ${state}/operator/helper-releases/incoming")
              machine.succeed("test -d ${backups}/${constants.directories.controllerBackup}/.staging")
              machine.succeed("test $(stat -c %U:%G:%a ${backups}/${constants.directories.hostedControllerBackup}) = platform-controller:agentops:750")
              machine.succeed("systemctl is-enabled ${namespace}-hosted-controller-backup.timer")
              machine.succeed("systemctl cat ${namespace}-hosted-controller-backup.service | grep -F -- '--backup-root ${backups}/${constants.directories.hostedControllerBackup}'")
              machine.succeed("test -x /run/current-system/sw/bin/openstack-platform-hosted-controller-restore")
              machine.fail("runuser -u agentops -- openstack-platform-hosted-controller-restore --yes")
              machine.fail("systemctl cat ${namespace}-managed-usage.service")
              machine.fail("systemctl cat ${namespace}-managed-usage.timer")
            ''
          else if role == "ingress" then
            ''
              machine.wait_for_unit("traefik.service")
              machine.wait_until_succeeds("${pkgs.curl}/bin/curl --fail --silent http://127.0.0.1:8082/ping | grep -Fx OK", timeout=30)
              machine.succeed("grep -F 'one-off.apps.example.com' /etc/traefik/dynamic/platform.yaml")
              machine.succeed("grep -F 'http://${platform.addresses.admin}:8080' /etc/traefik/dynamic/platform.yaml")
              machine.succeed("grep -F 'http://192.0.2.14:4444' /etc/traefik/dynamic/platform.yaml")
              machine.succeed("grep -F '127.0.0.1:80' /etc/traefik/traefik.yaml")
              machine.fail("grep -F referrerPolicy /etc/traefik/dynamic/platform.yaml")
              machine.succeed("grep -F contentTypeNosniff /etc/traefik/dynamic/platform.yaml")
              machine.succeed("grep -F frameDeny /etc/traefik/dynamic/platform.yaml")
              # A hostile client reaching a non-loopback origin address cannot
              # select the management router merely by supplying its Host.
              machine.fail("ip=$(hostname -I | awk '{print $1}'); ${pkgs.curl}/bin/curl --fail --silent --max-time 2 --header 'Host: ${platform.domain}' http://$ip/")
            ''
          else if role == "storage" then
            ''
              machine.wait_for_unit("nginx.service")
              machine.succeed("${pkgs.nginx}/bin/nginx -t -c /etc/nginx/nginx.conf")
              machine.succeed("mountpoint -q ${platform.paths.data}")
            ''
          else if role == "worker" then
            ''
              machine.wait_for_unit("docker.service")
              machine.wait_for_unit("nomad.service")
              machine.succeed("${pkgs.docker}/bin/docker info >/dev/null")
              machine.succeed("${pkgs.iptables}/bin/iptables -C OUTPUT -d ${platform.metadataAddress}/32 -j REJECT")
              machine.succeed("grep -F 'allow_privileged = false' /etc/nomad.d/10-base.hcl")
              machine.succeed("test -x /etc/cni/bin/bridge")
            ''
          else
            ''
              machine.wait_for_unit("default.target", "agentops")
              machine.wait_for_unit("buildkit.service", "agentops")
              machine.succeed("runuser -u agentops -- env XDG_RUNTIME_DIR=/run/user/1000 ${packages.buildkit}/bin/buildctl --addr unix:///run/user/1000/buildkit/buildkitd.sock debug workers")
              machine.succeed("test -x /run/current-system/sw/bin/mount.fuse3")
              machine.succeed("${pkgs.iptables}/bin/iptables -C OUTPUT -d ${platform.metadataAddress}/32 -j REJECT")
              machine.succeed("systemctl is-active --quiet ${namespace}-builder-expiry.timer")
            ''
        }

        machine.succeed("test -z \"$(systemctl --failed --no-legend)\"")
      '';
    };
in
lib.genAttrs constants.roles mkRoleTest
