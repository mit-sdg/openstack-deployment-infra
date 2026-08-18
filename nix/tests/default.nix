{ pkgs, platform }:
let
  lib = pkgs.lib;
  namespace = platform.namespace;
  root = platform.paths.root;
  state = platform.paths.adminState;
  backups = platform.paths.backups;
  packages = import ../pkgs { inherit pkgs platform; };
  testPki = pkgs.runCommand "${namespace}-test-pki" { nativeBuildInputs = [ pkgs.openssl ]; } ''
        set -euo pipefail
        install -d -m 0755 "$out"
        openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2 \
          -keyout "$out/ca-key.pem" -out "$out/ca.pem" \
          -subj "/CN=Platform VM Test CA/O=Platform Tests" >/dev/null 2>&1

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
        issue storage storage.example.internal serverAuth \
          'DNS:storage.example.internal,DNS:s3.example.internal,DNS:localhost,IP:127.0.0.1'
        cat "$out/storage.pem" "$out/storage-key.pem" > "$out/mongodb-combined.pem"
        rm -f "$out"/*.csr "$out"/*.ext "$out"/*.srl
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

          _module.args = { inherit platform role; };

          virtualisation = {
            memorySize = if role == "storage" then 3072 else 2048;
            cores = 2;
          };

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
              nomad.preStart = lib.mkForce ''
                install -d -m 0750 -o nomad -g nomad ${platform.paths.adminState}/nomad
                install -d -m 0700 -o nomad -g nomad /run/${namespace}-nomad
                printf '%s\n' \
                  'advertise { http = "127.0.0.1:4646" rpc = "127.0.0.1:4647" serf = "127.0.0.1:4648" }' \
                  > /run/${namespace}-nomad/90-runtime.hcl
                chown nomad:nomad /run/${namespace}-nomad/90-runtime.hcl
              '';
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
              "${namespace}/secrets/traefik.env".text = "NOMAD_TOKEN=vm-test-token\n";
            })
          ];
        };

      testScript = ''
        machine.start()
        machine.wait_for_unit("multi-user.target")
        machine.wait_for_unit("cloud-final.service")
        machine.succeed("getent passwd agentops >/dev/null")
        machine.succeed("getent passwd ubuntu >/dev/null")
        machine.succeed("test -r /etc/${namespace}/platform.json")
        machine.succeed("python3 -c 'import json; json.load(open(\"/etc/${namespace}/platform.json\"))'")

        ${
          if role == "admin" then
            ''
              machine.wait_for_unit("nomad.service")
              machine.wait_for_unit("${namespace}-admin-readiness.service")
              machine.succeed("systemctl is-active --quiet nomad.service")
              machine.succeed("${pkgs.curl}/bin/curl --fail --silent --cacert /etc/${namespace}/pki/internal-ca.pem --cert /etc/${namespace}/pki/nomad-cli.pem --key /etc/${namespace}/pki/nomad-cli-key.pem https://127.0.0.1:4646/v1/status/leader >/dev/null")
              machine.succeed("${packages.platformCliPython}/bin/python -c 'import sys, yaml; assert sys.version_info[:2] == (3, 14)'")
              machine.succeed("openstack-platform-install-release --help >/dev/null")
              machine.fail("${root}/bin/openstack-platform-helper </dev/null")
              machine.succeed("test -d ${state}/controller/platform-cli/releases")
              machine.succeed("test -d ${state}/controller/platform-cli/incoming")
              machine.succeed("test -d ${backups}/m1/.staging")
              machine.fail("systemctl cat ${namespace}-managed-usage.service")
              machine.fail("systemctl cat ${namespace}-managed-usage.timer")
            ''
          else if role == "ingress" then
            ''
              machine.wait_for_unit("traefik.service")
              machine.wait_until_succeeds("${pkgs.curl}/bin/curl --fail --silent http://127.0.0.1:8082/ping | grep -Fx OK", timeout=30)
              machine.succeed("grep -F 'one-off.apps.example.com' /etc/traefik/dynamic/platform.yaml")
              machine.succeed("grep -F 'http://192.0.2.14:4444' /etc/traefik/dynamic/platform.yaml")
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
lib.genAttrs [ "admin" "ingress" "storage" "worker" "builder" ] mkRoleTest
