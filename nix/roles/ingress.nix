{
  constants,
  lib,
  pkgs,
  platform,
  ...
}:
let
  packages = import ../pkgs { inherit pkgs platform; };
  credentialGuard = pkgs.writeShellScript "${namespace}-ingress-credential-guard" ''
    set -euo pipefail
    path=$1
    test -f "$path" && test ! -L "$path"
    test "$(stat -c %U:%a "$path")" = root:600
    test "$(stat -c %s "$path")" -le 65536
  '';
  traefikStart = pkgs.writeShellScript "${namespace}-traefik-start" ''
    set -euo pipefail
    set -a
    source "$CREDENTIALS_DIRECTORY/traefik.env"
    set +a
    exec ${packages.traefik}/bin/traefik --configFile=/etc/traefik/traefik.yaml
  '';
  namespace = platform.namespace;
  configRoot = "/etc/${namespace}";
  ingressMode = platform.publicIngress.mode;
  providerCidrs = platform.publicIngress.providerCidrs;
  directIngress = ingressMode == "direct";
  webAddress = if directIngress then "0.0.0.0" else "127.0.0.1";
  yaml = pkgs.formats.yaml { };
  hostRule = lib.concatMapStringsSep " || " (host: "Host(`${host}`)") (
    [ platform.domain ] ++ platform.recoveryDomains
  );
  staticIngressRoutes = platform.staticIngressRoutes or { };
  staticIngressRouters = lib.mapAttrs' (
    name: route:
    lib.nameValuePair "static-${name}" {
      entryPoints = [ "web" ];
      rule = "Host(`${route.hostname}`)";
      service = "static-${name}";
    }
  ) staticIngressRoutes;
  staticIngressServices = lib.mapAttrs' (
    name: route:
    lib.nameValuePair "static-${name}" {
      loadBalancer.servers = [ { url = route.origin; } ];
    }
  ) staticIngressRoutes;
  staticConfig = yaml.generate "traefik.yaml" {
    global = {
      checkNewVersion = false;
      sendAnonymousUsage = false;
    };
    entryPoints = {
      web = {
        address = "${webAddress}:${toString constants.ports.http}";
        forwardedHeaders.trustedIPs =
          if directIngress then
            providerCidrs
          else
            [
              "127.0.0.1/32"
              "::1/128"
            ];
        transport.respondingTimeouts = {
          readTimeout = "30s";
          writeTimeout = "0s";
          idleTimeout = "90s";
        };
      };
      health.address = "127.0.0.1:${toString constants.ports.traefikHealth}";
    };
    ping.entryPoint = "health";
    api.dashboard = false;
    providers = {
      providersThrottleDuration = "2s";
      file = {
        directory = "/etc/traefik/dynamic";
        watch = true;
      };
      nomad = {
        exposedByDefault = false;
        watch = true;
        throttleDuration = "1s";
        constraints = "Tag(`${namespace}.platform=true`)";
        endpoint = {
          address = "https://${platform.addresses.admin}:${toString constants.ports.nomadHttp}";
          region = platform.region;
          tls = {
            ca = "${configRoot}/pki/internal-ca.pem";
            cert = "/etc/${namespace}/pki/nomad-ingress.pem";
            key = "/etc/${namespace}/pki/nomad-ingress-key.pem";
          };
        };
      };
    };
    log = {
      level = "INFO";
      format = "json";
    };
    accessLog = {
      format = "json";
      bufferingSize = 100;
      fields = {
        defaultMode = "keep";
        headers = {
          defaultMode = "drop";
          names = {
            User-Agent = "keep";
            X-Forwarded-For = "drop";
          };
        };
      };
    };
  };
  dynamicConfig = yaml.generate "platform.yaml" {
    http = {
      routers = {
        healthz = {
          entryPoints = [ "web" ];
          rule = "Path(`/healthz`)";
          priority = 10000;
          service = "ping@internal";
        };
        platform-portal = {
          entryPoints = [ "web" ];
          rule = hostRule;
          service = "platform-portal";
          middlewares = [ "platform-security-headers" ];
        };
      }
      // staticIngressRouters;
      services = {
        platform-portal.loadBalancer.servers = [
          { url = "http://${platform.addresses.admin}:${toString constants.ports.managementWeb}"; }
        ];
      }
      // staticIngressServices;
      middlewares.platform-security-headers.headers = {
        contentTypeNosniff = true;
        referrerPolicy = "no-referrer";
        frameDeny = true;
      };
    };
  };
in
{
  networking.hostName = platform.hosts.ingress;
  # Tunnel mode has no network-facing HTTP listener. Direct mode admits only
  # the exact configured provider sources at both Neutron and host boundaries.
  networking.firewall.allowedTCPPorts = [ constants.ports.ssh ];
  networking.firewall.extraCommands = lib.optionalString directIngress (
    lib.concatMapStringsSep "\n" (
      cidr:
      "iptables -A nixos-fw -p tcp -s ${lib.escapeShellArg cidr} --dport ${toString constants.ports.http} -j nixos-fw-accept"
    ) providerCidrs
  );

  environment.systemPackages = [ packages.traefik ];
  environment.etc."traefik/traefik.yaml".source = staticConfig;
  environment.etc."traefik/dynamic/platform.yaml".source = dynamicConfig;

  systemd.services.traefik = {
    description = "Traefik ${platform.displayName} application ingress";
    wantedBy = [ "multi-user.target" ];
    wants = [ "network-online.target" ];
    after = [
      "network-online.target"
      "cloud-final.service"
    ];
    requires = [ "cloud-final.service" ];
    serviceConfig = {
      User = "traefik";
      Group = "traefik";
      ExecStartPre = "${credentialGuard} /etc/${namespace}/secrets/traefik.env";
      LoadCredential = "traefik.env:/etc/${namespace}/secrets/traefik.env";
      LimitCORE = 0;
      ExecStart = traefikStart;
      Restart = "on-failure";
      StandardOutput = "journal+console";
      StandardError = "journal+console";
      RestartSec = 2;
      AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
      CapabilityBoundingSet = [ "CAP_NET_BIND_SERVICE" ];
      NoNewPrivileges = true;
      PrivateDevices = true;
      PrivateTmp = true;
      ProtectHome = true;
      ProtectSystem = "strict";
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      RestrictSUIDSGID = true;
      LockPersonality = true;
      MemoryDenyWriteExecute = true;
      LimitNOFILE = 1048576;
    };
  };
  users.groups.traefik = { };
  users.users.traefik = {
    isSystemUser = true;
    group = "traefik";
  };

  virtualisation.podman.enable = true;
  virtualisation.oci-containers.backend = "podman";
  virtualisation.oci-containers.containers."${namespace}-cloudflared" = {
    image = platform.containers.cloudflared;
    environmentFiles = [
      "/run/credentials/podman-${namespace}-cloudflared.service/cloudflared.env"
    ];
    cmd = [
      "tunnel"
      "--no-autoupdate"
      "run"
    ];
    extraOptions = [
      "--network=host"
      "--read-only"
      "--cap-drop=all"
      "--security-opt=no-new-privileges"
    ];
  };
  systemd.services."podman-${namespace}-cloudflared" = {
    unitConfig.ConditionPathExists = "/etc/${namespace}/secrets/cloudflared.env";
    serviceConfig = {
      ExecStartPre = [ "${credentialGuard} /etc/${namespace}/secrets/cloudflared.env" ];
      LoadCredential = "cloudflared.env:/etc/${namespace}/secrets/cloudflared.env";
      LimitCORE = 0;
    };
    after = [
      "cloud-final.service"
      "traefik.service"
    ];
    requires = [ "cloud-final.service" ];
    wants = [ "traefik.service" ];
  };

  systemd.services."${namespace}-ingress-readiness" = {
    description = "Verify ${platform.displayName} ingress after first boot and reboot";
    wantedBy = [ "multi-user.target" ];
    after = [
      "cloud-final.service"
      "traefik.service"
    ];
    requires = [
      "cloud-final.service"
      "traefik.service"
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
      ExecStartPre = "${credentialGuard} /etc/${namespace}/secrets/traefik.env";
      LoadCredential = "traefik.env:/etc/${namespace}/secrets/traefik.env";
      LimitCORE = 0;
    };
    script = ''
      set -euo pipefail
      set -a
      source "$CREDENTIALS_DIRECTORY/traefik.env"
      set +a
      for attempt in {1..24}; do
        if systemctl is-active --quiet traefik.service && \
          curl --fail --silent --show-error --max-time 5 http://127.0.0.1:${toString constants.ports.traefikHealth}/ping >/dev/null && \
          curl --fail --silent --show-error --max-time 5 \
            --cacert ${configRoot}/pki/internal-ca.pem \
            --cert /etc/${namespace}/pki/nomad-ingress.pem \
            --key /etc/${namespace}/pki/nomad-ingress-key.pem \
            --header "X-Nomad-Token: $TRAEFIK_PROVIDERS_NOMAD_ENDPOINT_TOKEN" \
            "https://${platform.addresses.admin}:${toString constants.ports.nomadHttp}/v1/services" >/dev/null; then
          sleep 2
          curl --fail --silent --show-error --max-time 5 http://127.0.0.1:${toString constants.ports.traefikHealth}/ping >/dev/null
          echo "${namespace} NixOS ingress services ready"
          exit 0
        fi
        echo "ingress readiness elapsed=$((attempt * 5))s"
        sleep 5
      done
      systemctl --failed --no-pager || true
      journalctl --boot --no-pager --lines 120 || true
      echo "${namespace} NixOS ingress readiness failed"
      exit 1
    '';
  };

  systemd.tmpfiles.rules = [
    "z /etc/${namespace} 0750 root traefik -"
    "z /etc/${namespace}/pki 0750 root traefik -"
  ];
}
