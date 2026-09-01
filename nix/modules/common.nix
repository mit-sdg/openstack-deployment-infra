{
  constants,
  lib,
  pkgs,
  platform,
  ...
}:
let
  namespace = platform.namespace;
  operator = constants.accounts.operator;
  recovery = constants.accounts.recovery;
  configRoot = "/etc/${namespace}";
  platformJson = pkgs.writeText "${namespace}-platform.json" (builtins.toJSON platform);
in
{
  system.stateVersion = "25.11";

  networking.useNetworkd = true;
  networking.useDHCP = lib.mkDefault true;
  networking.firewall.enable = true;

  time.timeZone = "UTC";
  i18n.defaultLocale = "C.UTF-8";

  nix.settings = {
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    auto-optimise-store = true;
  };

  # The operator key is injected through OpenStack config-drive at first boot.
  users.allowNoPasswordLogin = true;
  users.mutableUsers = false;
  users.groups.${operator.name}.gid = operator.gid;
  users.groups.${recovery.name}.gid = recovery.gid;
  users.users.${operator.name} = {
    isNormalUser = true;
    uid = operator.uid;
    group = operator.name;
    home = "/home/${operator.name}";
    createHome = true;
    shell = pkgs.bashInteractive;
    linger = true;
  };
  # OpenStack's keypair is installed for this approval-gated recovery user.
  users.users.${recovery.name} = {
    isNormalUser = true;
    uid = recovery.uid;
    group = recovery.name;
    extraGroups = [ "wheel" ];
    home = "/home/${recovery.name}";
    createHome = true;
    shell = pkgs.bashInteractive;
  };

  services.openssh = {
    enable = lib.mkDefault true;
    settings = {
      PermitRootLogin = lib.mkForce "no";
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PubkeyAuthentication = true;
      AuthenticationMethods = "publickey";
      AllowUsers = [
        operator.name
        recovery.name
      ];
    };
  };

  # The upstream OpenStack image module enables a metadata fetcher with
  # unbounded retries. These hosts use config-drive cloud-init instead, and
  # intentionally block workload access to the configured metadata endpoint.
  systemd.services.openstack-init.enable = false;

  services.cloud-init = {
    enable = true;
    network.enable = false;
    settings = {
      # Do not fall back to None: make-disk-image boots the target once while
      # assembling it. The None datasource would consume once-per-instance
      # write_files state in the published QCOW2 before Nova first boot.
      datasource_list = [ "ConfigDrive" ];
      # NixOS owns /etc/hostname in the immutable system closure. Asking
      # cloud-init to replace it aborts the config stage before write_files.
      preserve_hostname = true;
      disable_root = true;
      ssh_pwauth = false;
      # NixOS sshd-keygen owns first-boot host key generation. Prevent
      # cloud-init from deleting/regenerating the same keys concurrently.
      ssh_deletekeys = false;
      ssh_genkeytypes = [ ];
      system_info.default_user = {
        name = recovery.name;
        gecos = "${platform.displayName} approval-gated recovery operator";
        groups = [ "wheel" ];
        lock_passwd = true;
        shell = "/run/current-system/sw/bin/bash";
        sudo = [ "ALL=(ALL) NOPASSWD:ALL" ];
      };
      users = [
        "default"
        {
          name = operator.name;
          gecos = "${platform.displayName} platform operator";
          lock_passwd = true;
          shell = "/run/current-system/sw/bin/bash";
        }
      ];
    };
  };

  # make-disk-image boots the target once without platform provisioning data,
  # leaving a complete cached no-datasource cloud-init instance in the QCOW2.
  # Clean it in cloud-init-local's own pre-start while the real ConfigDrive
  # sentinel is absent. Successful provisioning writes the sentinel, so later
  # reboots retain state and never replay bootstrap secrets.
  systemd.services.cloud-init-local.serviceConfig.ExecStartPre = [
    (pkgs.writeShellScript "${namespace}-cloud-init-image-state-reset" ''
      if [[ ! -e ${configRoot}/.provisioned ]]; then
        ${pkgs.coreutils}/bin/rm -rf /var/lib/cloud
        ${pkgs.coreutils}/bin/install -d -m 0755 /var/lib/cloud
      fi
    '')
  ];
  systemd.services.cloud-init.requires = [ "cloud-init-local.service" ];

  services.qemuGuest.enable = true;
  security.auditd.enable = true;
  # Secret-bearing units also set LimitCORE=0 explicitly. Disable the host
  # collector and set the kernel guard so crashes cannot persist credentials.
  systemd.coredump.enable = false;
  security.sudo = {
    enable = true;
    wheelNeedsPassword = false;
  };

  environment.etc."${namespace}/platform.json".source = platformJson;
  environment.systemPackages = with pkgs; [
    age
    bash
    cacert
    curl
    jq
    openssl
    python3
    tmux
    xfsprogs
  ];

  systemd.tmpfiles.rules = [
    "d ${configRoot} 0750 root root -"
    "d ${configRoot}/pki 0750 root root -"
    "d ${configRoot}/secrets 0700 root root -"
    "d ${platform.paths.root} 0750 ${operator.name} ${operator.name} -"
  ];

  # The target OpenStack deployment exposes Nova console logs through the first serial port.
  # Keep the VGA console while making ttyS0 the primary kernel/systemd console
  # so first-boot and cloud-init failures remain observable without SSH.
  boot.kernelParams = [ "console=ttyS0,115200n8" ];
  boot.loader.grub.extraConfig = ''
    serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1
    terminal_input serial console
    terminal_output serial console
  '';

  systemd.services."${namespace}-metadata-guard" = {
    description = "Block host and forwarded workload access to cloud metadata";
    wantedBy = [ "multi-user.target" ];
    before = [ "network-online.target" ];
    after = [ "network-pre.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = [ pkgs.iptables ];
    script = ''
      iptables -C OUTPUT -d ${platform.metadataAddress}/32 -j REJECT 2>/dev/null || \
        iptables -I OUTPUT 1 -d ${platform.metadataAddress}/32 -j REJECT
      iptables -C FORWARD -d ${platform.metadataAddress}/32 -j REJECT 2>/dev/null || \
        iptables -I FORWARD 1 -d ${platform.metadataAddress}/32 -j REJECT
    '';
  };

  boot.kernel.sysctl = {
    "fs.file-max" = 1048576;
    "fs.suid_dumpable" = 0;
  };
  systemd.settings.Manager.DefaultLimitNOFILE = 1048576;
}
