{ pkgs, platform }:
let
  lib = pkgs.lib;
  constants = import ../lib/constants.nix;
  namespace = platform.namespace;
  root = platform.paths.root;
  state = platform.paths.adminState;
  backups = platform.paths.backups;
  packages = import ../pkgs { inherit pkgs platform; };
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
                lib.mkForce "${packages.python}/bin/python ${managedBackupCredentialProbe}";
              "${namespace}-management-identity".serviceConfig = {
                # Mirror production: the fake class app on loopback plus the
                # local resolver stubs used for name resolution.
                IPAddressAllow = lib.mkForce [
                  "127.0.0.1/32"
                  "127.0.0.53/32"
                  "127.0.0.54/32"
                ];
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
        machine.start()
        machine.wait_for_unit("multi-user.target")
        machine.wait_for_unit("cloud-final.service")
        machine.succeed("python3 -c 'import json; json.load(open(\"/etc/${namespace}/platform.json\"))'")

        ${
          if role == "admin" then
            ''
              # One boot, real controller/Nomad APIs, and the role's trust boundaries.
              machine.wait_for_unit("nomad.service")
              machine.wait_for_unit("${namespace}-admin-readiness.service")
              machine.wait_for_unit("${namespace}-controller-readiness.service")
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
              machine.wait_for_unit("${namespace}-management-prepare.service")
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
