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
in
{
  networking.firewall.allowedTCPPorts = [ 8080 ];
  services.openssh.enable = lib.mkForce false;
  # Workers have no SSH and consume only config-drive cloud-init. Disable the
  # EC2 key/data units inherited by the generic OpenStack image profile.
  systemd.services.print-host-key.enable = false;
  systemd.services.apply-ec2-data.enable = false;
  systemd.services.amazon-init.enable = false;

  virtualisation.docker = {
    enable = true;
    liveRestore = true;
    daemon.settings = {
      icc = false;
      "log-driver" = "json-file";
      "log-opts" = {
        "max-file" = "3";
        "max-size" = "10m";
      };
      "no-new-privileges" = true;
      "userland-proxy" = false;
    };
  };

  environment.systemPackages = [
    packages.nomad
    pkgs.cni-plugins
    pkgs.iptables
  ];
  # Nomad executes every entry in cni_path while probing plugins. Expose only
  # the CNI package directory; pointing it at the system profile would execute
  # unrelated commands such as halt during client startup.
  environment.etc."cni/bin".source = "${pkgs.cni-plugins}/bin";
  environment.etc."nomad.d/10-base.hcl".text = ''
    region = "${platform.region}"
    datacenter = "${platform.datacenter}"
    data_dir = "/var/lib/nomad"
    bind_addr = "0.0.0.0"

    server { enabled = false }

    plugin "docker" {
      config {
        endpoint = "unix:///var/run/docker.sock"
        auth { config = "${configRoot}/docker-auth.json" }
        allow_privileged = false
        allow_caps = ["AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE", "SETFCAP", "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT"]
        volumes { enabled = false }
        logging {
          type = "json-file"
          config { max-file = "3" max-size = "10m" }
        }
        gc { image = true image_delay = "10m" container = true }
        pids_limit = 512
      }
    }

    tls {
      http = true
      rpc = true
      ca_file = "${configRoot}/pki/internal-ca.pem"
      cert_file = "${configRoot}/pki/nomad-worker.pem"
      key_file = "${configRoot}/pki/nomad-worker-key.pem"
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

  systemd.services."${namespace}-network-guard" = {
    description = "Block workload access to cloud metadata";
    wantedBy = [ "multi-user.target" ];
    after = [ "docker.service" ];
    before = [ "nomad.service" ];
    wants = [ "docker.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = [ pkgs.iptables ];
    script = ''
      iptables -C OUTPUT -d ${platform.metadataAddress}/32 -j REJECT 2>/dev/null || \
        iptables -I OUTPUT 1 -d ${platform.metadataAddress}/32 -j REJECT
      iptables -N DOCKER-USER 2>/dev/null || true
      iptables -C DOCKER-USER -d ${platform.metadataAddress}/32 -j REJECT 2>/dev/null || \
        iptables -I DOCKER-USER 1 -d ${platform.metadataAddress}/32 -j REJECT
    '';
  };

  systemd.services.nomad = {
    description = "Nomad application worker";
    wantedBy = [ "multi-user.target" ];
    wants = [
      "network-online.target"
      "docker.service"
    ];
    after = [
      "network-online.target"
      "cloud-final.service"
      "docker.service"
      "${namespace}-network-guard.service"
    ];
    requires = [ "cloud-final.service" ];
    # Nomad's bridge-network and CNI hooks execute host networking helpers.
    # Keep these in the service PATH without exposing the general system
    # profile as a CNI plugin directory.
    path = [
      pkgs.coreutils
      pkgs.iproute2
      pkgs.iptables
      pkgs.util-linux
    ];
    serviceConfig = {
      User = "root";
      Group = "root";
      ExecStartPre = "${packages.nomad}/bin/nomad config validate /etc/nomad.d";
      ExecStart = "${packages.nomad}/bin/nomad agent -config=/etc/nomad.d";
      ExecReload = "${pkgs.coreutils}/bin/kill -HUP $MAINPID";
      KillMode = "process";
      KillSignal = "SIGINT";
      Restart = "on-failure";
      RestartSec = 2;
      # Workers intentionally have no SSH. Keep Nomad failures observable on
      # the trusted Nova serial console as well as in the guest journal.
      StandardOutput = "journal+console";
      StandardError = "journal+console";
      LimitNOFILE = 1048576;
      TasksMax = "infinity";
      OOMScoreAdjust = -500;
    };
  };

  systemd.tmpfiles.rules = [
    "d /etc/nomad.d 0700 root root -"
    "d /var/lib/nomad 0700 root root -"
    "d ${configRoot} 0750 root root -"
    "d ${configRoot}/pki 0750 root root -"
    "d /etc/docker/certs.d/${platform.addresses.storage}:5000 0755 root root -"
    "L+ /etc/docker/certs.d/${platform.addresses.storage}:5000/ca.crt - - - - ${configRoot}/pki/internal-ca.pem"
  ];
}
