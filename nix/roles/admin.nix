{
  constants,
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
  backupMountUnit = "${systemdEscapePath backups}.mount";
  controllerAccount = constants.accounts.controller;
  controllerUser = controllerAccount.name;
  controllerGroup = controllerAccount.name;
  controllerSocketAccount = constants.accounts.controllerSocket;
  controllerSocketGroup = controllerSocketAccount.name;
  managementWebAccount = constants.accounts.managementWeb;
  managementWebUser = managementWebAccount.name;
  managementBrokerAccount = constants.accounts.managementBroker;
  managementBrokerUser = managementBrokerAccount.name;
  operatorAccount = constants.accounts.operator;
  platformAdminAccount = constants.accounts.platformAdmin;
  nomadAccount = constants.accounts.nomad;
  controllerRoot = "${state}/controller";
  controllerState = "${controllerRoot}/state";
  controllerPolicy = "${controllerRoot}/policy.json";
  operatorRoot = "${state}/operator";
  operatorPolicy = "${operatorRoot}/policy.json";
  helperReleaseRoot = "${operatorRoot}/helper-releases";
  controllerSocketDirectory = "${namespace}-controller";
  controllerSocket = "/run/${controllerSocketDirectory}/project.sock";
  controllerPrivilegedSocket = "/run/${controllerSocketDirectory}/privileged.sock";
  managementWebState = "${state}/management-web";
  managementWebReleaseRoot = "${state}/management-web-releases";
  managementWebExecutable = "${managementWebReleaseRoot}/current/bin/management-web";
  managementBrokerState = "${state}/management-broker";
  managementBrokerReleaseRoot = "${state}/management-broker-releases";
  managementBrokerExecutable = "${managementBrokerReleaseRoot}/current/bin/management-broker";
  managementBrokerRuntime = "${namespace}-management-broker";
  managementBrokerSocket = "/run/${managementBrokerRuntime}/broker.sock";
  helperReleaseMarker = "${helperReleaseRoot}/current/.complete";
  credentialGuard = pkgs.writeShellScript "${namespace}-credential-guard" ''
    set -euo pipefail
    path=$1
    owner=$2
    test -f "$path" && test ! -L "$path"
    test "$(stat -c %U:%a "$path")" = "$owner:600"
    test "$(stat -c %s "$path")" -le 65536
  '';
  controllerBackupRoot = "${backups}/${constants.directories.controllerBackup}";
  hostedControllerBackupRoot = "${backups}/${constants.directories.hostedControllerBackup}";
  offsiteExportConfig = "${operatorRoot}/offsite-export.json";
  offsiteExportReceipt = "${operatorRoot}/status/offsite-export.json";
  hostedControllerRestoreInput = "${controllerRoot}/restore-input.sqlite3";
  hostedControllerRestore = pkgs.writeShellScriptBin "openstack-platform-hosted-controller-restore" ''
    set -euo pipefail
    if [[ $(id -u) != 0 ]]; then
      echo "hosted controller restore must run as root from the recovery console" >&2
      exit 77
    fi
    if [[ $# != 1 || $1 != --yes ]]; then
      echo "usage: openstack-platform-hosted-controller-restore --yes" >&2
      exit 64
    fi
    for unit in \
      ${namespace}-controller.service \
      ${namespace}-hosted-controller-backup.service \
      ${namespace}-hosted-controller-backup.timer; do
      if ${pkgs.systemd}/bin/systemctl is-active --quiet "$unit"; then
        echo "refusing restore while $unit is active" >&2
        exit 69
      fi
    done
    input=${lib.escapeShellArg hostedControllerRestoreInput}
    [[ -f "$input" && ! -L "$input" ]]
    [[ $(${pkgs.coreutils}/bin/stat -c %U:%a "$input") == ${controllerUser}:600 ]] || {
      echo "restore input must be a direct ${controllerUser}-owned mode-0600 file" >&2
      exit 77
    }
    ${pkgs.util-linux}/bin/runuser -u ${controllerUser} -- \
      ${packages.controllerPackage}/bin/openstack-platform-controller-restore \
      "$input" \
      --destination ${controllerState}/platform.sqlite3 \
      --platform-config /etc/${namespace}/platform.json \
      --yes
    ${pkgs.coreutils}/bin/rm -f -- "$input"
  '';

  openstackClient = pkgs.writeShellScriptBin "platform-openstack" ''
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
  setupOperatorBridgeCli = pkgs.writeShellScriptBin "openstack-platform-setup-bridge" ''
    set -euo pipefail
    exec ${packages.platformPython}/bin/python ${../../deploy/releases/setup_operator_bridge.py} "$@"
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
  prepareController = pkgs.writeShellScript "${namespace}-prepare-controller" ''
    set -euo pipefail

    policy_source=${lib.escapeShellArg operatorPolicy}
    policy=${lib.escapeShellArg controllerPolicy}
    test -f "$policy_source" && test ! -L "$policy_source"
    test "$(stat -c %U:%a "$policy_source")" = ${operatorAccount.name}:600
    install -m 0600 -o ${controllerUser} -g ${controllerGroup} \
      "$policy_source" "$policy"

    # Keep credentials owned by their installer or operator. Grant only the
    # dedicated trusted controller group read/traverse access.
    tree=${lib.escapeShellArg "${operatorRoot}/secrets"}
    if test -d "$tree" && test ! -L "$tree"; then
      chgrp ${controllerGroup} "$tree"
      chmod 0750 "$tree"
      for name in openstack.env nomad-tokens.env storage-bootstrap.env builder_operator_ed25519; do
        path="$tree/$name"
        test -f "$path" && test ! -L "$path"
        test "$(stat -c %U:%a "$path")" = ${operatorAccount.name}:600
        chgrp ${controllerGroup} "$path"
        chmod 0640 "$path"
      done
    fi
  '';
in
{
  networking.hostName = platform.hosts.admin;
  networking.firewall.allowedTCPPorts = with constants.ports; [
    ssh
    nomadHttp
    nomadRpc
    nomadSerf
  ];
  # The future management application owns this unprivileged port. Only the
  # ingress host may cross the host firewall boundary to reach it.
  networking.firewall.extraCommands = ''
    iptables -A nixos-fw -p tcp -s ${platform.addresses.ingress}/32 --dport ${toString constants.ports.managementWeb} -j nixos-fw-accept
  '';

  users.groups.${controllerSocketGroup}.gid = controllerSocketAccount.gid;
  users.groups.${controllerGroup}.gid = controllerAccount.gid;
  users.groups.${managementWebUser}.gid = managementWebAccount.gid;
  users.groups.${managementBrokerUser}.gid = managementBrokerAccount.gid;
  users.groups.${platformAdminAccount.name}.gid = platformAdminAccount.gid;
  users.groups.${nomadAccount.name}.gid = nomadAccount.gid;
  users.users.${operatorAccount.name}.extraGroups = [
    platformAdminAccount.name
    controllerSocketGroup
  ];
  users.users.${controllerUser} = {
    isSystemUser = true;
    uid = controllerAccount.uid;
    group = controllerGroup;
    extraGroups = [
      operatorAccount.name
      platformAdminAccount.name
      controllerSocketGroup
    ];
  };
  users.users.${managementWebUser} = {
    isSystemUser = true;
    uid = managementWebAccount.uid;
    group = managementWebUser;
  };
  users.users.${managementBrokerUser} = {
    isSystemUser = true;
    uid = managementBrokerAccount.uid;
    group = managementBrokerUser;
    extraGroups = [ controllerSocketGroup ];
  };
  users.users.${nomadAccount.name} = {
    isSystemUser = true;
    uid = nomadAccount.uid;
    group = nomadAccount.name;
    extraGroups = [ platformAdminAccount.name ];
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
    packages.platformPython
    packages.releaseInstaller
    openstackClient
    nomadCli
    workerCli
    builderCli
    pinBuilderHostKeyCli
    setupOperatorBridgeCli
    hostedControllerRestore
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
    export NOMAD_ADDR=https://127.0.0.1:${toString constants.ports.nomadHttp}
    export NOMAD_CACERT=${configRoot}/pki/internal-ca.pem
    export NOMAD_CLIENT_CERT=/etc/${namespace}/pki/nomad-cli.pem
    export NOMAD_CLIENT_KEY=/etc/${namespace}/pki/nomad-cli-key.pem
  '';

  systemd.tmpfiles.rules = [
    "z /etc/${namespace} 0750 root ${platformAdminAccount.name} -"
    "z /etc/${namespace}/pki 0750 root ${platformAdminAccount.name} -"
    "z /etc/${namespace}/secrets 0750 root nomad -"
    "d ${state}/nomad 0750 nomad nomad -"
    "d ${controllerRoot} 0700 ${controllerUser} ${controllerGroup} -"
    "d ${controllerState} 0700 ${controllerUser} ${controllerGroup} -"
    "d ${controllerRoot}/build-logs 0700 ${controllerUser} ${controllerGroup} -"
    "d ${controllerRoot}/helper-diagnostics 0700 ${controllerUser} ${controllerGroup} -"
    "d ${managementWebState} 0700 ${managementWebUser} ${managementWebUser} -"
    "d ${managementWebReleaseRoot} 0750 ${operatorAccount.name} ${managementWebUser} -"
    "d ${managementBrokerState} 0700 ${managementBrokerUser} ${managementBrokerUser} -"
    "d ${managementBrokerReleaseRoot} 0750 ${operatorAccount.name} ${managementBrokerUser} -"
    "d ${operatorRoot} 0750 ${operatorAccount.name} ${operatorAccount.name} -"
    "d ${operatorRoot}/secrets 0700 ${operatorAccount.name} ${operatorAccount.name} -"
    "d ${operatorRoot}/status 0750 ${operatorAccount.name} ${operatorAccount.name} -"
    "d ${helperReleaseRoot} 0750 ${operatorAccount.name} ${operatorAccount.name} -"
    "d ${helperReleaseRoot}/releases 0750 ${operatorAccount.name} ${operatorAccount.name} -"
    "d ${helperReleaseRoot}/incoming 0700 ${operatorAccount.name} ${operatorAccount.name} -"
    "d ${backups} 0710 ${operatorAccount.name} ${controllerGroup} -"
    "d ${controllerBackupRoot} 0770 ${operatorAccount.name} ${controllerGroup} -"
    "d ${controllerBackupRoot}/.staging 0770 ${operatorAccount.name} ${controllerGroup} -"
    "d ${hostedControllerBackupRoot} 0750 ${controllerUser} ${operatorAccount.name} -"
    "d ${hostedControllerBackupRoot}/.staging 0750 ${controllerUser} ${operatorAccount.name} -"
    "L+ ${root}/persistent - - - - ${operatorRoot}"
    "d ${root}/bin 0750 ${operatorAccount.name} ${operatorAccount.name} -"
    "L+ ${root}/infra - - - - ${infra}"
    "L+ ${root}/secrets - - - - ${operatorRoot}/secrets"
    "L+ ${operatorRoot}/secrets/nomad-cli - - - - ${operatorRoot}/secrets/provisioning-pki"
    "L+ ${root}/nomad.env - - - - /etc/${namespace}/nomad.env"
    "L+ ${root}/bin/platform-openstack - - - - ${openstackClient}/bin/platform-openstack"
    "L+ ${root}/bin/${namespace}-nomad - - - - ${nomadCli}/bin/${namespace}-nomad"
    "L+ ${root}/bin/${namespace}-worker - - - - ${workerCli}/bin/${namespace}-worker"
    "L+ ${root}/bin/${namespace}-builder - - - - ${builderCli}/bin/${namespace}-builder"
    "L+ ${root}/bin/${namespace}-pin-builder-host-key - - - - ${pinBuilderHostKeyCli}/bin/${namespace}-pin-builder-host-key"
    "L+ ${root}/bin/openstack-platform-helper - - - - ${packages.helperLauncher}/bin/openstack-platform-helper"
    "L+ ${root}/bin/age - - - - ${pkgs.age}/bin/age"
    "L+ ${root}/bin/age-keygen - - - - ${pkgs.age}/bin/age-keygen"
  ];

  systemd.services."${namespace}-controller-prepare" = {
    description = "Prepare private ${platform.displayName} controller paths";
    after = [
      "cloud-final.service"
      stateMountUnit
    ];
    requires = [ stateMountUnit ];
    unitConfig.ConditionPathExists = [
      operatorPolicy
      helperReleaseMarker
    ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = prepareController;
      UMask = "0027";
    };
  };

  systemd.services."${namespace}-hosted-controller-backup" = {
    description = "Encrypted backup of the admin-hosted ${platform.displayName} controller database";
    after = [
      "${namespace}-controller.service"
      backupMountUnit
      stateMountUnit
    ];
    requires = [
      backupMountUnit
      stateMountUnit
    ];
    unitConfig.ConditionPathExists = [
      "${controllerState}/platform.sqlite3"
      controllerPolicy
    ];
    serviceConfig = {
      Type = "oneshot";
      User = controllerUser;
      Group = operatorAccount.name;
      SupplementaryGroups = [ controllerGroup ];
      UMask = "0027";
      TimeoutStartSec = "2h";
      LimitCORE = 0;
      ExecStart = lib.concatStringsSep " " [
        "${packages.controllerPackage}/bin/openstack-platform-hosted-controller-backup"
        "--platform-config /etc/${namespace}/platform.json"
        "--policy ${controllerPolicy}"
        "--state-directory ${controllerState}"
        "--backup-root ${hostedControllerBackupRoot}"
        "--age-command ${pkgs.age}/bin/age"
      ];
      NoNewPrivileges = true;
      PrivateDevices = true;
      PrivateTmp = true;
      ProtectHome = true;
      ProtectSystem = "strict";
      ReadOnlyPaths = [
        "/etc/${namespace}/platform.json"
        controllerPolicy
      ];
      ReadWritePaths = [
        controllerState
        hostedControllerBackupRoot
      ];
    };
  };

  systemd.timers."${namespace}-hosted-controller-backup" = {
    description = "Daily encrypted backup of the admin-hosted controller database";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 02:15:00 UTC";
      RandomizedDelaySec = "30m";
      Persistent = true;
      Unit = "${namespace}-hosted-controller-backup.service";
    };
  };

  systemd.services."${namespace}-controller" = {
    description = "Local ${platform.displayName} application controller";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" ];
    after = [
      "network-online.target"
      "nomad.service"
      "${namespace}-controller-prepare.service"
      stateMountUnit
    ];
    requires = [
      "nomad.service"
      "${namespace}-controller-prepare.service"
      stateMountUnit
    ];
    unitConfig.ConditionPathExists = [
      controllerPolicy
      helperReleaseMarker
    ];
    environment = {
      PLATFORM_OPENSTACK_COMMAND = "${openstackClient}/bin/platform-openstack";
      PYTHONDONTWRITEBYTECODE = "1";
    };
    serviceConfig = {
      Type = "simple";
      User = controllerUser;
      Group = controllerSocketGroup;
      SupplementaryGroups = [
        controllerGroup
        operatorAccount.name
        platformAdminAccount.name
      ];
      RuntimeDirectory = controllerSocketDirectory;
      RuntimeDirectoryMode = "0750";
      UMask = "0077";
      LimitCORE = 0;
      ExecStart = lib.concatStringsSep " " [
        "${packages.controllerPackage}/bin/openstack-platform-controller"
        "--platform-config /etc/${namespace}/platform.json"
        "--state-directory ${controllerState}"
        "--policy ${controllerPolicy}"
        "--socket ${controllerSocket}"
        "--socket-group ${controllerSocketGroup}"
        "--project-peer ${toString managementBrokerAccount.uid}:${toString managementBrokerAccount.gid}"
        "--privileged-socket ${controllerPrivilegedSocket}"
        "--privileged-socket-group ${platformAdminAccount.name}"
        "--privileged-peer ${toString operatorAccount.uid}:${toString operatorAccount.gid}"
        "--max-connections-per-peer 8"
      ];
      Restart = "on-failure";
      RestartSec = 2;

      AmbientCapabilities = "";
      CapabilityBoundingSet = "";
      DevicePolicy = "closed";
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      NoNewPrivileges = true;
      PrivateDevices = true;
      PrivateMounts = true;
      PrivateTmp = true;
      ProcSubset = "pid";
      ProtectClock = true;
      ProtectControlGroups = true;
      ProtectHome = true;
      ProtectHostname = true;
      ProtectKernelLogs = true;
      ProtectKernelModules = true;
      ProtectKernelTunables = true;
      ProtectProc = "invisible";
      ProtectSystem = "strict";
      RemoveIPC = true;
      RestrictAddressFamilies = [
        "AF_UNIX"
        "AF_INET"
        "AF_INET6"
      ];
      RestrictNamespaces = true;
      RestrictRealtime = true;
      RestrictSUIDSGID = true;
      SystemCallArchitectures = "native";
      ReadOnlyPaths = [
        "/etc/${namespace}/platform.json"
        controllerPolicy
        "${root}/bin"
        "${root}/infra"
        helperReleaseRoot
        "${operatorRoot}/secrets"
      ];
      ReadWritePaths = [
        controllerState
        "${controllerRoot}/build-logs"
        "${controllerRoot}/helper-diagnostics"
        controllerBackupRoot
      ];
    };
  };

  # Installing the operator-owned helper release or policy after boot starts
  # the controller without granting the operator service-management rights.
  systemd.services."${namespace}-controller-activate" = {
    description = "Activate ${platform.displayName} controller after release installation";
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.systemd}/bin/systemctl start --no-block ${namespace}-controller.service";
    };
  };

  systemd.paths."${namespace}-controller" = {
    wantedBy = [ "multi-user.target" ];
    pathConfig = {
      PathExists = [
        operatorPolicy
        helperReleaseMarker
      ];
      Unit = "${namespace}-controller-activate.service";
    };
  };

  # The trusted broker owns authorization/session/project state and is the only
  # future management component permitted to reach the controller. It has no
  # TCP access; authentication and source resolution require a separately
  # reviewed typed Unix integration service.
  systemd.services."${namespace}-management-broker" = {
    description = "Trusted ${platform.displayName} management authorization broker";
    after = [ "${namespace}-controller.service" ];
    requires = [ "${namespace}-controller.service" ];
    unitConfig.ConditionPathIsExecutable = managementBrokerExecutable;
    environment = {
      CONTROLLER_PROJECT_SOCKET = controllerSocket;
      MANAGEMENT_BROKER_SOCKET = managementBrokerSocket;
      MANAGEMENT_STATE_DIRECTORY = managementBrokerState;
    };
    serviceConfig = {
      Type = "simple";
      User = managementBrokerUser;
      Group = managementBrokerUser;
      SupplementaryGroups = [
        controllerSocketGroup
        managementWebUser
      ];
      RuntimeDirectory = managementBrokerRuntime;
      RuntimeDirectoryMode = "0755";
      ExecStart = managementBrokerExecutable;
      UMask = "0007";
      Restart = "on-failure";
      NoNewPrivileges = true;
      CapabilityBoundingSet = "";
      AmbientCapabilities = "";
      DevicePolicy = "closed";
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      PrivateDevices = true;
      PrivateMounts = true;
      PrivateTmp = true;
      ProcSubset = "pid";
      ProtectHome = true;
      ProtectProc = "invisible";
      ProtectSystem = "strict";
      RestrictAddressFamilies = [ "AF_UNIX" ];
      RestrictNamespaces = true;
      RestrictSUIDSGID = true;
      IPAddressDeny = "any";
      InaccessiblePaths = [
        operatorRoot
        "/etc/${namespace}/pki"
        "/etc/${namespace}/secrets"
        controllerPrivilegedSocket
      ];
      ReadOnlyPaths = [ managementBrokerReleaseRoot ];
      ReadWritePaths = [ managementBrokerState ];
    };
  };

  # Browser rendering is intentionally a separate, disposable process. A web
  # compromise cannot connect to either controller socket or read durable
  # authorization/session state.
  systemd.services."${namespace}-management-web" = {
    description = "Browser-facing ${platform.displayName} management application";
    after = [ "${namespace}-management-broker.service" ];
    requires = [ "${namespace}-management-broker.service" ];
    unitConfig.ConditionPathIsExecutable = managementWebExecutable;
    environment = {
      MANAGEMENT_BROKER_SOCKET = managementBrokerSocket;
      MANAGEMENT_WEB_STATE_DIRECTORY = managementWebState;
    };
    serviceConfig = {
      Type = "simple";
      User = managementWebUser;
      Group = managementWebUser;
      ExecStart = managementWebExecutable;
      UMask = "0077";
      Restart = "on-failure";
      NoNewPrivileges = true;
      CapabilityBoundingSet = "";
      AmbientCapabilities = "";
      DevicePolicy = "closed";
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      PrivateDevices = true;
      PrivateMounts = true;
      PrivateTmp = true;
      ProcSubset = "pid";
      ProtectHome = true;
      ProtectProc = "invisible";
      ProtectSystem = "strict";
      RestrictAddressFamilies = [
        "AF_UNIX"
        "AF_INET"
        "AF_INET6"
      ];
      RestrictNamespaces = true;
      RestrictSUIDSGID = true;
      SocketBindAllow = "tcp:${toString constants.ports.managementWeb}";
      SocketBindDeny = "any";
      IPAddressDeny = "any";
      IPAddressAllow = "${platform.addresses.ingress}/32";
      InaccessiblePaths = [
        controllerRoot
        operatorRoot
        "/run/${controllerSocketDirectory}"
        managementBrokerState
        "/etc/${namespace}/pki"
        "/etc/${namespace}/secrets"
      ];
      ReadOnlyPaths = [ managementWebReleaseRoot ];
      ReadWritePaths = [ managementWebState ];
    };
  };

  systemd.paths."${namespace}-management-broker" = {
    wantedBy = [ "multi-user.target" ];
    pathConfig = {
      PathExists = managementBrokerExecutable;
      Unit = "${namespace}-management-broker.service";
    };
  };

  systemd.paths."${namespace}-management-web" = {
    wantedBy = [ "multi-user.target" ];
    pathConfig = {
      PathExists = managementWebExecutable;
      Unit = "${namespace}-management-web.service";
    };
  };

  systemd.services."${namespace}-controller-readiness" = {
    description = "Verify the restricted local ${platform.displayName} controller API";
    after = [ "${namespace}-controller.service" ];
    requires = [ "${namespace}-controller.service" ];
    partOf = [ "${namespace}-controller.service" ];
    unitConfig.ConditionPathExists = [
      controllerPolicy
      helperReleaseMarker
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      User = managementBrokerUser;
      Group = managementBrokerUser;
      SupplementaryGroups = [ controllerSocketGroup ];
      ExecStart = pkgs.writeShellScript "${namespace}-controller-readiness" ''
        set -euo pipefail
        for attempt in {1..24}; do
          if ${pkgs.curl}/bin/curl --fail --silent --show-error --max-time 5 \
            --unix-socket ${lib.escapeShellArg controllerSocket} \
            'http://localhost/v1/health' >/dev/null; then
            echo "${namespace} controller ready"
            exit 0
          fi
          sleep 1
        done
        echo "${namespace} controller readiness failed" >&2
        exit 1
      '';
      NoNewPrivileges = true;
      PrivateDevices = true;
      PrivateTmp = true;
      ProtectHome = true;
      ProtectSystem = "strict";
      RestrictAddressFamilies = [ "AF_UNIX" ];
    };
  };

  systemd.paths."${namespace}-controller-readiness" = {
    wantedBy = [ "multi-user.target" ];
    pathConfig = {
      PathExists = controllerSocket;
      Unit = "${namespace}-controller-readiness.service";
    };
  };

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
      credential="$CREDENTIALS_DIRECTORY/nomad-gossip-key"
      test -f "$credential" && test ! -L "$credential"
      NOMAD_GOSSIP_KEY=$(cat -- "$credential")
      [[ $NOMAD_GOSSIP_KEY =~ ^[A-Za-z0-9+/]{20,128}={0,2}$ ]]
      source /etc/${namespace}/attached-volumes.env
      [[ $ADMIN_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
      umask 077
      {
        printf 'advertise { http = "%s:${toString constants.ports.nomadHttp}" rpc = "%s:${toString constants.ports.nomadRpc}" serf = "%s:${toString constants.ports.nomadSerf}" }\n' \
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
      ExecStartPre = "${credentialGuard} /etc/${namespace}/secrets/nomad-gossip-key root";
      LoadCredential = "nomad-gossip-key:/etc/${namespace}/secrets/nomad-gossip-key";
      LimitCORE = 0;
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
      User = operatorAccount.name;
      Group = operatorAccount.name;
      TimeoutStartSec = "24h";
      Environment = [
        "AGE=${pkgs.age}/bin/age"
        "AGE_KEYGEN=${pkgs.age}/bin/age-keygen"
        "AGE_KEY=%d/backup-age-key"
        "PLATFORM_CONFIG=/etc/${namespace}/platform.json"
        "BACKUP_ROOT=${backups}/${namespace}"
        "EMIT_SCRIPT=${infra}/backup/emit_logical_backup.sh"
        "SERVICE_CHECK_PYTHON=${packages.python}/bin/python"
        "GARAGE_EMIT_SCRIPT=${infra}/backup/emit_garage_backup.py"
        "REGISTRY_ARTIFACT_SCRIPT=${infra}/backup/registry_artifact.py"
        "REGISTRY_BACKUP_MAX_FILE_BYTES=1099511627776"
        "REGISTRY_BACKUP_MAX_TOTAL_BYTES=4398046511104"
        "REGISTRY_BACKUP_MAX_MANIFEST_BYTES=67108864"
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
      ExecStartPre = "${credentialGuard} ${root}/persistent/secrets/backup-age-key.txt ${operatorAccount.name}";
      LoadCredential = "backup-age-key:${root}/persistent/secrets/backup-age-key.txt";
      LimitCORE = 0;
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

  systemd.services."${namespace}-offsite-export" = {
    description = "Export latest committed ${platform.displayName} recovery evidence off site";
    after = [
      backupMountUnit
      "${namespace}-hosted-controller-backup.service"
      "${namespace}-platform-backup.service"
    ];
    requires = [ backupMountUnit ];
    unitConfig.ConditionPathExists = offsiteExportConfig;
    serviceConfig = {
      Type = "oneshot";
      User = operatorAccount.name;
      Group = operatorAccount.name;
      UMask = "0077";
      TimeoutStartSec = "24h";
      ExecStart = lib.concatStringsSep " " [
        "${packages.controllerPackage}/bin/openstack-platform-recovery"
        "scheduled-export"
        "--platform-config /etc/${namespace}/platform.json"
        "--config ${offsiteExportConfig}"
        "--receipt ${offsiteExportReceipt}"
      ];
      NoNewPrivileges = true;
      PrivateDevices = true;
      PrivateTmp = true;
      ProtectHome = true;
      ProtectSystem = "full";
    };
  };
  systemd.timers."${namespace}-offsite-export" = {
    description = "Daily off-site recovery evidence export";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "*-*-* 05:00:00 UTC";
      RandomizedDelaySec = "30m";
      Persistent = true;
      Unit = "${namespace}-offsite-export.service";
    };
  };

  systemd.services."${namespace}-platform-health" = {
    description = "Check ${platform.displayName} platform health";
    unitConfig.ConditionPathExists = "${root}/secrets/openstack.env";
    serviceConfig = {
      Type = "oneshot";
      User = operatorAccount.name;
      Group = operatorAccount.name;
      Environment = [
        "PLATFORM_CONFIG=/etc/${namespace}/platform.json"
        "OPENSTACK=${openstackClient}/bin/platform-openstack"
        "NOMAD=${nomadCli}/bin/${namespace}-nomad"
        "SERVICE_CHECK_PYTHON=${packages.python}/bin/python"
        "CHECK_SERVICES=${infra}/monitor/check_services.py"
        "RECOVERY=${packages.controllerPackage}/bin/openstack-platform-recovery"
        "PATH=${
          lib.makeBinPath [
            openstackClient
            nomadCli
            packages.python
          ]
        }"
      ];
      LimitCORE = 0;
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
      LimitCORE = 0;
    };
    script = ''
      set -euo pipefail
      for attempt in {1..24}; do
        if systemctl is-active --quiet nomad.service && \
          curl --fail --silent --show-error --max-time 5 \
            --cacert ${configRoot}/pki/internal-ca.pem \
            --cert /etc/${namespace}/pki/nomad-cli.pem \
            --key /etc/${namespace}/pki/nomad-cli-key.pem \
            https://127.0.0.1:${toString constants.ports.nomadHttp}/v1/status/leader >/dev/null; then
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
