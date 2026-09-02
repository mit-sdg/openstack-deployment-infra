{ pkgs, platform }:
let
  lib = pkgs.lib;
  constants = import ../lib/constants.nix;
  namespace = platform.namespace;
  root = platform.paths.root;
  state = platform.paths.adminState;
  backups = platform.paths.backups;
  packages = import ../pkgs { inherit pkgs platform; };
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

          _module.args = { inherit constants platform role; };

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
        machine.succeed("getent passwd agentops >/dev/null")
        machine.succeed("getent passwd ubuntu >/dev/null")
        machine.succeed("test -r /etc/${namespace}/platform.json")
        machine.succeed("python3 -c 'import json; json.load(open(\"/etc/${namespace}/platform.json\"))'")

        ${
          if role == "admin" then
            ''
              machine.wait_for_unit("nomad.service")
              machine.wait_for_unit("${namespace}-admin-readiness.service")
              machine.wait_for_unit("${namespace}-controller.service")
              machine.wait_for_unit("${namespace}-controller-readiness.service")
              machine.succeed("systemctl is-active --quiet nomad.service")
              machine.succeed("systemctl is-active --quiet ${namespace}-controller.service")
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
