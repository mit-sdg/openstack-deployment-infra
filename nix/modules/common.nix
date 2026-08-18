{
  lib,
  pkgs,
  platform,
  ...
}:
let
  namespace = platform.namespace;
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
  users.groups.agentops.gid = 1000;
  users.groups.ubuntu.gid = 1001;
  users.users.agentops = {
    isNormalUser = true;
    uid = 1000;
    group = "agentops";
    home = "/home/agentops";
    createHome = true;
    shell = pkgs.bashInteractive;
    linger = true;
  };
  # OpenStack's keypair is installed for this approval-gated recovery user.
  users.users.ubuntu = {
    isNormalUser = true;
    uid = 1001;
    group = "ubuntu";
    extraGroups = [ "wheel" ];
    home = "/home/ubuntu";
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
        "agentops"
        "ubuntu"
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
      datasource_list = [
        "ConfigDrive"
        "OpenStack"
        "None"
      ];
      preserve_hostname = false;
      disable_root = true;
      ssh_pwauth = false;
      # NixOS sshd-keygen owns first-boot host key generation. Prevent
      # cloud-init from deleting/regenerating the same keys concurrently.
      ssh_deletekeys = false;
      ssh_genkeytypes = [ ];
      system_info.default_user = {
        name = "ubuntu";
        gecos = "${platform.displayName} approval-gated recovery operator";
        groups = [ "wheel" ];
        lock_passwd = true;
        shell = "/run/current-system/sw/bin/bash";
        sudo = [ "ALL=(ALL) NOPASSWD:ALL" ];
      };
      users = [
        "default"
        {
          name = "agentops";
          gecos = "${platform.displayName} platform operator";
          lock_passwd = true;
          shell = "/run/current-system/sw/bin/bash";
        }
      ];
    };
  };

  services.qemuGuest.enable = true;
  security.auditd.enable = true;
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
    "d ${platform.paths.root} 0750 agentops agentops -"
  ];

  # CSAIL OpenStack exposes Nova console logs through the first serial port.
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
  };
  systemd.settings.Manager.DefaultLimitNOFILE = 1048576;
}
