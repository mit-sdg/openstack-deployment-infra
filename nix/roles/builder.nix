{
  lib,
  pkgs,
  platform,
  ...
}:
let
  packages = import ../pkgs { inherit pkgs platform; };
  namespace = platform.namespace;
  builderRoot = "/srv/${namespace}-build";
  caPath = "/usr/local/share/ca-certificates/${namespace}-internal-ca.crt";
  builderExecute = pkgs.writeTextFile {
    name = "app-platform-builder-execute";
    destination = "/bin/app-platform-builder-execute";
    executable = true;
    text = builtins.readFile ../../infra/openstack/builder_execute.py;
  };
  prepareCaBundle = pkgs.writeShellScript "${namespace}-builder-ca-bundle" ''
    set -euo pipefail
    install -d -m 0700 "$XDG_RUNTIME_DIR/buildkit"
    cat ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt \
      ${caPath} \
      > "$XDG_RUNTIME_DIR/buildkit/ca-bundle.crt"
    chmod 0600 "$XDG_RUNTIME_DIR/buildkit/ca-bundle.crt"
  '';
in
{
  networking.firewall.allowedTCPPorts = [ 22 ];

  users.users.agentops = {
    subUidRanges = [
      {
        startUid = 100000;
        count = 65536;
      }
    ];
    subGidRanges = [
      {
        startGid = 100000;
        count = 65536;
      }
    ];
  };
  security.unprivilegedUsernsClone = true;

  environment.systemPackages = [
    builderExecute
    packages.buildkit
    pkgs.fuse-overlayfs
    pkgs.fuse3
    pkgs.iptables
    pkgs.rootlesskit
    pkgs.slirp4netns
  ];

  systemd.user.services.buildkit = {
    description = "Rootless BuildKit for one disposable build";
    wantedBy = [ "default.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      ExecStartPre = prepareCaBundle;
      Environment = [
        "PATH=/run/wrappers/bin:${
          lib.makeBinPath [
            packages.buildkit
            pkgs.coreutils
            pkgs.fuse-overlayfs
            pkgs.fuse3
            pkgs.iproute2
            pkgs.iptables
            pkgs.rootlesskit
            pkgs.slirp4netns
            pkgs.util-linux
          ]
        }"
        "SSL_CERT_FILE=%t/buildkit/ca-bundle.crt"
      ];
      ExecStart = lib.concatStringsSep " " [
        "${pkgs.rootlesskit}/bin/rootlesskit"
        "--net=slirp4netns"
        "--copy-up=/etc"
        "--disable-host-loopback"
        "${packages.buildkit}/bin/buildkitd"
        "--addr unix://%t/buildkit/buildkitd.sock"
        "--oci-worker-rootless=true"
        "--oci-worker-snapshotter=fuse-overlayfs"
        "--oci-worker-gc"
        "--oci-worker-gc-keepstorage=20000"
      ];
      Restart = "on-failure";
      RestartSec = 2;
      LimitNOFILE = 1048576;
      TasksMax = 4096;
    };
  };

  systemd.services."${namespace}-builder-network-guard" = {
    description = "Block builder access to cloud metadata";
    wantedBy = [ "multi-user.target" ];
    before = [ "user@1000.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = [ pkgs.iptables ];
    script = ''
      iptables -C OUTPUT -d ${platform.metadataAddress}/32 -j REJECT 2>/dev/null || \
        iptables -I OUTPUT 1 -d ${platform.metadataAddress}/32 -j REJECT
    '';
  };

  systemd.services."${namespace}-builder-expiry" = {
    description = "Stop an expired disposable builder";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.systemd}/bin/systemctl poweroff";
    };
  };
  systemd.timers."${namespace}-builder-expiry" = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnActiveSec = "75m";
      AccuracySec = "1m";
    };
  };

  systemd.tmpfiles.rules = [
    "d ${builderRoot} 0750 agentops agentops -"
    "d /home/agentops/.docker 0700 agentops agentops -"
    "d /home/agentops/.config 0750 agentops agentops -"
  ];
}
