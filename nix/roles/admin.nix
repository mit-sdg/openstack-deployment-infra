{
  lib,
  pkgs,
  platform,
  ...
}:
let
  packages = import ../pkgs { inherit pkgs platform; };
  namespace = platform.namespace;
  configRoot = "/etc/${namespace}";
  systemdEscapePath =
    path: lib.replaceStrings [ "-" "/" ] [ "\\x2d" "-" ] (lib.removePrefix "/" path);
  infra = pkgs.runCommand "${namespace}-infra" { } ''
    mkdir -p "$out"
    cp -R ${../../infra}/. "$out/"
    chmod -R u+w "$out"
    patchShebangs "$out"
  '';
  state = platform.paths.adminState;
  backups = platform.paths.backups;
  root = platform.paths.root;
  stateMountUnit = "${systemdEscapePath state}.mount";

  openstackSdg = pkgs.writeShellScriptBin "platform-openstack" ''
    set -euo pipefail
    set -a
    source ${root}/secrets/openstack.env
    set +a
    export OS_USER_DOMAIN_NAME="''${OS_USER_DOMAIN_NAME:-Default}"
    export OS_PROJECT_DOMAIN_NAME="''${OS_PROJECT_DOMAIN_NAME:-Default}"
    exec ${packages.python}/bin/openstack "$@"
  '';
  nomadCli = pkgs.writeShellScriptBin "${namespace}-nomad" ''
    set -euo pipefail
    source ${root}/nomad.env
    set -a
    source ${root}/secrets/nomad-tokens.env
    set +a
    export NOMAD_TOKEN="$NOMAD_CONTROLLER_TOKEN"
    exec ${packages.nomad}/bin/nomad "$@"
  '';
  workerCli = pkgs.writeShellScriptBin "${namespace}-worker" ''
    set -euo pipefail
    set -a
    source ${root}/secrets/openstack.env
    set +a
    exec env \
      PLATFORM_CONFIG=/etc/${namespace}/platform.json \
      OSC=${packages.python}/bin/openstack \
      IMAGE_NAME="''${IMAGE_NAME:-${platform.images.worker}}" \
      FLAVOR_NAME="''${FLAVOR_NAME:-${platform.flavors.worker}}" \
      NOMAD="''${NOMAD:-${nomadCli}/bin/${namespace}-nomad}" \
      TEMPLATE=${infra}/cloud-init-nixos/worker.yaml \
      PKI_DIR=${root}/persistent/secrets/provisioning-pki \
      STORAGE_SECRETS_FILE=${root}/secrets/storage-bootstrap.env \
      ${infra}/openstack/worker_lifecycle.sh "$@"
  '';
  setupManagementBridgeCli = pkgs.writeShellScriptBin "openstack-platform-setup-bridge" ''
    set -euo pipefail
    exec ${packages.platformCliPython}/bin/python ${../../deploy/platform-cli/setup_management_bridge.py} "$@"
  '';
  pinBuilderHostKeyCli = pkgs.writeShellScriptBin "${namespace}-pin-builder-host-key" ''
    set -euo pipefail
    set -a
    source ${root}/secrets/openstack.env
    set +a
    exec env \
      PLATFORM_CONFIG=/etc/${namespace}/platform.json \
      OSC=${packages.python}/bin/openstack \
      ${infra}/openstack/pin_ephemeral_host_key.sh "$@"
  '';
  builderCli = pkgs.writeShellScriptBin "${namespace}-builder" ''
    set -euo pipefail
    set -a
    source ${root}/secrets/openstack.env
    set +a
    exec env \
      PLATFORM_CONFIG=/etc/${namespace}/platform.json \
      OSC=${packages.python}/bin/openstack \
      IMAGE_NAME="''${IMAGE_NAME:-${platform.images.builder}}" \
      FLAVOR_NAME="''${FLAVOR_NAME:-${platform.flavors.builder}}" \
      TEMPLATE=${infra}/cloud-init-nixos/builder.yaml \
      PKI_DIR=${root}/persistent/secrets/provisioning-pki \
      STORAGE_SECRETS_FILE=${root}/secrets/storage-bootstrap.env \
      BUILDER_OPERATOR_PUBLIC_KEY=${root}/secrets/builder_operator_ed25519.pub \
      ${infra}/openstack/builder_lifecycle.sh "$@"
  '';
in
{
  networking.hostName = platform.hosts.admin;
  networking.firewall.allowedTCPPorts = [
    22
    4646
    4647
    4648
    8080
  ];

  users.groups.platform-admin.gid = 987;
  users.groups.nomad.gid = 988;
  users.users.agentops.extraGroups = [ "platform-admin" ];
  users.users.nomad = {
    isSystemUser = true;
    uid = 999;
    group = "nomad";
    extraGroups = [ "platform-admin" ];
  };

  fileSystems.${state} = {
    device = "/dev/disk/by-label/${platform.volumes.adminState.label}";
    fsType = "xfs";
    options = [
      "nofail"
      "x-systemd.device-timeout=60s"
    ];
    neededForBoot = false;
  };
  fileSystems.${backups} = {
    device = "/dev/disk/by-label/${platform.volumes.backup.label}";
    fsType = "xfs";
    options = [
      "nofail"
      "x-systemd.device-timeout=60s"
    ];
    neededForBoot = false;
  };

  virtualisation.podman.enable = true;

  environment.systemPackages = [
    pkgs.git
    packages.nomad
    packages.python
    packages.platformCliPython
    packages.platformCliInstaller
    openstackSdg
    nomadCli
    workerCli
    builderCli
    pinBuilderHostKeyCli
    setupManagementBridgeCli
  ];

  environment.etc."${namespace}/nomad/10-server.hcl".text = ''
    name = "${platform.hosts.admin}"
    region = "${platform.region}"
    datacenter = "${platform.datacenter}"
    data_dir = "${state}/nomad"
    bind_addr = "0.0.0.0"

    server {
      enabled = true
      bootstrap_expect = 1
    }
    client { enabled = false }
    acl {
      enabled = true
      token_ttl = "30s"
      policy_ttl = "30s"
    }
    tls {
      http = true
      rpc = true
      ca_file = "${configRoot}/pki/internal-ca.pem"
      cert_file = "/etc/${namespace}/pki/nomad-server.pem"
      key_file = "/etc/${namespace}/pki/nomad-server-key.pem"
      verify_server_hostname = true
      verify_https_client = true
    }
    telemetry {
      collection_interval = "15s"
      disable_hostname = true
      prometheus_metrics = true
      publish_allocation_metrics = true
      publish_node_metrics = true
    }
  '';

  environment.etc."${namespace}/nomad.env".text = ''
    export NOMAD_ADDR=https://127.0.0.1:4646
    export NOMAD_CACERT=${configRoot}/pki/internal-ca.pem
    export NOMAD_CLIENT_CERT=/etc/${namespace}/pki/nomad-cli.pem
    export NOMAD_CLIENT_KEY=/etc/${namespace}/pki/nomad-cli-key.pem
  '';

  systemd.tmpfiles.rules = [
    "z /etc/${namespace} 0750 root platform-admin -"
    "z /etc/${namespace}/pki 0750 root platform-admin -"
    "z /etc/${namespace}/secrets 0750 root nomad -"
    "d ${state}/nomad 0750 nomad nomad -"
    "d ${state}/controller 0700 agentops agentops -"
    "d ${state}/controller/secrets 0700 agentops agentops -"
    "d ${state}/controller/status 0750 agentops agentops -"
    "d ${state}/controller/platform-cli 0750 agentops agentops -"
    "d ${state}/controller/platform-cli/releases 0750 agentops agentops -"
    "d ${state}/controller/platform-cli/incoming 0700 agentops agentops -"
    "d ${state}/controller/helper-diagnostics 0700 agentops agentops -"
    "d ${backups} 0700 agentops agentops -"
    "d ${backups}/m1 0700 agentops agentops -"
    "d ${backups}/m1/.staging 0700 agentops agentops -"
    "L+ ${root}/persistent - - - - ${state}/controller"
    "d ${root}/bin 0750 agentops agentops -"
    "L+ ${root}/infra - - - - ${infra}"
    "L+ ${root}/secrets - - - - ${state}/controller/secrets"
    "L+ ${state}/controller/secrets/nomad-cli - - - - ${state}/controller/secrets/provisioning-pki"
    "L+ ${root}/nomad.env - - - - /etc/${namespace}/nomad.env"
    "L+ ${root}/bin/platform-openstack - - - - ${openstackSdg}/bin/platform-openstack"
    "L+ ${root}/bin/${namespace}-nomad - - - - ${nomadCli}/bin/${namespace}-nomad"
    "L+ ${root}/bin/${namespace}-worker - - - - ${workerCli}/bin/${namespace}-worker"
    "L+ ${root}/bin/${namespace}-builder - - - - ${builderCli}/bin/${namespace}-builder"
    "L+ ${root}/bin/${namespace}-pin-builder-host-key - - - - ${pinBuilderHostKeyCli}/bin/${namespace}-pin-builder-host-key"
    "L+ ${root}/bin/openstack-platform-helper - - - - ${packages.platformCliHelperLauncher}/bin/openstack-platform-helper"
    "L+ ${root}/bin/age - - - - ${pkgs.age}/bin/age"
    "L+ ${root}/bin/age-keygen - - - - ${pkgs.age}/bin/age-keygen"
  ];

  systemd.services.nomad = {
    description = "Nomad ${platform.displayName} control-plane server";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" ];
    after = [
      "network-online.target"
      "cloud-final.service"
      stateMountUnit
    ];
    requires = [
      "cloud-final.service"
      stateMountUnit
    ];
    preStart = ''
      install -d -m 0700 -o nomad -g nomad /run/${namespace}-nomad
      secret=${root}/secrets/admin-bootstrap.env
      if [[ ! -r "$secret" ]]; then
        secret=/etc/${namespace}/secrets/admin-bootstrap.env
      fi
      set -a
      source "$secret"
      set +a
      source /etc/${namespace}/attached-volumes.env
      [[ $ADMIN_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
      umask 077
      {
        printf 'advertise { http = "%s:4646" rpc = "%s:4647" serf = "%s:4648" }\n' \
          "$ADMIN_IP" "$ADMIN_IP" "$ADMIN_IP"
        printf 'server { encrypt = "%s" }\n' "$NOMAD_GOSSIP_KEY"
      } > /run/${namespace}-nomad/90-runtime.hcl
      chown nomad:nomad /run/${namespace}-nomad/90-runtime.hcl
    '';
    serviceConfig = {
      User = "nomad";
      Group = "nomad";
      RuntimeDirectory = "${namespace}-nomad";
      RuntimeDirectoryMode = "0700";
      ExecStart = "${packages.nomad}/bin/nomad agent -config=/etc/${namespace}/nomad -config=/run/${namespace}-nomad/90-runtime.hcl";
      ExecReload = "${pkgs.coreutils}/bin/kill -HUP $MAINPID";
      KillMode = "process";
      KillSignal = "SIGINT";
      Restart = "on-failure";
      RestartSec = 2;
      StandardOutput = "journal+console";
      StandardError = "journal+console";
      LimitNOFILE = 1048576;
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectHome = true;
      ProtectSystem = "strict";
      ReadWritePaths = [
        "${state}/nomad"
        "/run/${namespace}-nomad"
      ];
    };
  };

  systemd.services."${namespace}-platform-backup" = {
    description = "Create encrypted logical platform backups";
    unitConfig.ConditionPathExists = "${root}/persistent/secrets/backup-age-key.txt";
    serviceConfig = {
      Type = "oneshot";
      User = "agentops";
      Group = "agentops";
      Environment = [
        "AGE=${pkgs.age}/bin/age"
        "AGE_KEYGEN=${pkgs.age}/bin/age-keygen"
        "AGE_KEY=${root}/persistent/secrets/backup-age-key.txt"
        "PLATFORM_CONFIG=/etc/${namespace}/platform.json"
        "BACKUP_ROOT=${backups}/${namespace}"
        "EMIT_SCRIPT=${infra}/backup/emit_logical_backup.sh"
        "SERVICE_CHECK_PYTHON=${packages.python}/bin/python"
        "GARAGE_EMIT_SCRIPT=${infra}/backup/emit_garage_backup.py"
        "PATH=${
          lib.makeBinPath [
            pkgs.coreutils
            pkgs.findutils
            pkgs.podman
            pkgs.util-linux
            packages.python
          ]
        }"
      ];
      ExecStart = "${infra}/backup/run_platform_backup.sh";
    };
  };
  systemd.timers."${namespace}-platform-backup" = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 03:15:00 UTC";
      RandomizedDelaySec = "30m";
      Persistent = true;
    };
  };

  systemd.services."${namespace}-platform-health" = {
    description = "Check ${platform.displayName} platform health";
    unitConfig.ConditionPathExists = "${root}/secrets/openstack.env";
    serviceConfig = {
      Type = "oneshot";
      User = "agentops";
      Group = "agentops";
      Environment = [
        "PLATFORM_CONFIG=/etc/${namespace}/platform.json"
        "OPENSTACK=${openstackSdg}/bin/platform-openstack"
        "NOMAD=${nomadCli}/bin/${namespace}-nomad"
        "SERVICE_CHECK_PYTHON=${packages.python}/bin/python"
        "CHECK_SERVICES=${infra}/monitor/check_services.py"
        "PATH=${
          lib.makeBinPath [
            openstackSdg
            nomadCli
            packages.python
          ]
        }"
      ];
      ExecStart = "${packages.python}/bin/python ${infra}/monitor/check_platform.py";
    };
  };
  systemd.timers."${namespace}-platform-health" = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5m";
      OnUnitActiveSec = "5m";
      Persistent = true;
    };
  };

  systemd.services."${namespace}-admin-readiness" = {
    description = "Verify ${platform.displayName} admin and Nomad after first boot and reboot";
    wantedBy = [ "multi-user.target" ];
    after = [
      "cloud-final.service"
      "nomad.service"
      stateMountUnit
    ];
    requires = [
      "cloud-final.service"
      "nomad.service"
      stateMountUnit
    ];
    path = [
      pkgs.coreutils
      pkgs.curl
      pkgs.systemd
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      StandardOutput = "journal+console";
      StandardError = "journal+console";
    };
    script = ''
      set -euo pipefail
      for attempt in {1..24}; do
        if systemctl is-active --quiet nomad.service && \
          curl --fail --silent --show-error --max-time 5 \
            --cacert ${configRoot}/pki/internal-ca.pem \
            --cert /etc/${namespace}/pki/nomad-cli.pem \
            --key /etc/${namespace}/pki/nomad-cli-key.pem \
            https://127.0.0.1:4646/v1/status/leader >/dev/null; then
          sleep 2
          echo "${namespace} NixOS admin services ready"
          exit 0
        fi
        echo "admin readiness elapsed=$((attempt * 5))s"
        sleep 5
      done
      systemctl --failed --no-pager || true
      journalctl --boot --no-pager --lines 120 || true
      echo "${namespace} NixOS admin readiness failed"
      exit 1
    '';
  };
}
