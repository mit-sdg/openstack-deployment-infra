{
  lib,
  pkgs,
  platform,
  ...
}:
let
  namespace = platform.namespace;
  data = platform.paths.data;
  infra = ../../infra;
  systemdEscapePath =
    path: lib.replaceStrings [ "-" "/" ] [ "\\x2d" "-" ] (lib.removePrefix "/" path);
  mountUnit = "${systemdEscapePath data}.mount";
  mkContainerDependencies = name: {
    "podman-${name}" = {
      after = [
        "cloud-final.service"
        mountUnit
      ];
      requires = [
        "cloud-final.service"
        mountUnit
      ];
      serviceConfig = {
        StandardOutput = "journal+console";
        StandardError = "journal+console";
      };
    };
  };
in
{
  networking.hostName = platform.hosts.storage;
  networking.firewall.allowedTCPPorts = [
    22
    3903
    5000
    5432
    9000
    27017
  ];

  # The pinned PostgreSQL and MongoDB containers both persist data as UID/GID
  # 999. Give cloud-init a resolvable host identity for private-key ownership;
  # numeric owner strings are interpreted as account names by cloud-init.
  users.groups.storage-service.gid = 999;
  users.users.storage-service = {
    isSystemUser = true;
    uid = 999;
    group = "storage-service";
  };

  fileSystems.${data} = {
    device = "/dev/disk/by-label/${platform.volumes.data.label}";
    fsType = "xfs";
    options = [
      "nofail"
      "prjquota"
      "x-systemd.device-timeout=60s"
    ];
    neededForBoot = false;
  };

  virtualisation.podman = {
    enable = true;
    dockerCompat = false;
  };
  virtualisation.oci-containers.backend = "podman";
  virtualisation.oci-containers.containers = {
    "${namespace}-postgres" = {
      image = platform.containers.postgres;
      environmentFiles = [ "/etc/${namespace}/postgres.env" ];
      volumes = [
        "${data}/postgres:/var/lib/postgresql/data"
        "/etc/${namespace}/pki:/run/${namespace}-pki:ro"
        "/etc/${namespace}/pg_hba.conf:/run/${namespace}-pg_hba.conf:ro"
        "/etc/${namespace}/postgres-init:/docker-entrypoint-initdb.d:ro"
      ];
      ports = [ "5432:5432" ];
      cmd = [
        "postgres"
        "-c"
        "ssl=on"
        "-c"
        "ssl_cert_file=/run/${namespace}-pki/storage.pem"
        "-c"
        "ssl_key_file=/run/${namespace}-pki/storage-key.pem"
        "-c"
        "ssl_ca_file=/run/${namespace}-pki/internal-ca.pem"
        "-c"
        "ssl_min_protocol_version=TLSv1.2"
        "-c"
        "hba_file=/run/${namespace}-pg_hba.conf"
      ];
      extraOptions = [
        "--health-cmd=pg_isready -U platform_admin -d platform"
        "--health-interval=30s"
        "--health-start-period=90s"
        "--health-timeout=5s"
        "--health-retries=5"
      ];
    };
    "${namespace}-mongodb" = {
      image = platform.containers.mongodb;
      environmentFiles = [ "/etc/${namespace}/mongodb.env" ];
      volumes = [
        "${data}/mongodb:/data/db"
        "/etc/${namespace}/pki:/run/${namespace}-pki:ro"
      ];
      ports = [ "27017:27017" ];
      cmd = [
        "mongod"
        "--bind_ip_all"
        "--tlsMode"
        "requireTLS"
        "--tlsCertificateKeyFile"
        "/run/${namespace}-pki/mongodb-combined.pem"
        "--tlsCAFile"
        "/run/${namespace}-pki/internal-ca.pem"
        "--tlsAllowConnectionsWithoutCertificates"
      ];
    };
    "${namespace}-garage" = {
      image = platform.containers.garage;
      volumes = [
        "/etc/${namespace}/garage.toml:/etc/garage.toml:ro"
        "${data}/object-storage:/var/lib/garage"
      ];
      ports = [
        "127.0.0.1:19000:3900"
        "127.0.0.1:13903:3903"
      ];
      cmd = [
        "/garage"
        "server"
        "--single-node"
      ];
    };
    "${namespace}-registry" = {
      image = platform.containers.registry;
      environmentFiles = [ "/etc/${namespace}/registry.env" ];
      volumes = [
        "${data}/registry:/var/lib/registry"
        "/etc/${namespace}/registry.htpasswd:/auth/htpasswd:ro"
        "/etc/${namespace}/pki:/pki:ro"
      ];
      ports = [ "5000:5000" ];
    };
  };

  systemd.services = lib.mkMerge [
    (mkContainerDependencies "${namespace}-postgres")
    (mkContainerDependencies "${namespace}-mongodb")
    (mkContainerDependencies "${namespace}-garage")
    (mkContainerDependencies "${namespace}-registry")
    {
      nginx = {
        after = [ "cloud-final.service" ];
        requires = [ "cloud-final.service" ];
        serviceConfig = {
          SupplementaryGroups = [ "storage-service" ];
          StandardOutput = "journal+console";
          StandardError = "journal+console";
        };
      };
      "${namespace}-storage-readiness" = {
        description = "Verify ${platform.displayName} storage services after first boot and reboot";
        wantedBy = [ "multi-user.target" ];
        after = [
          "cloud-final.service"
          mountUnit
          "podman-${namespace}-postgres.service"
          "podman-${namespace}-mongodb.service"
          "podman-${namespace}-garage.service"
          "podman-${namespace}-registry.service"
          "nginx.service"
        ];
        requires = [
          "cloud-final.service"
          mountUnit
          "podman-${namespace}-postgres.service"
          "podman-${namespace}-mongodb.service"
          "podman-${namespace}-garage.service"
          "podman-${namespace}-registry.service"
          "nginx.service"
        ];
        path = [
          pkgs.coreutils
          pkgs.podman
          pkgs.systemd
          pkgs.util-linux
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          StandardOutput = "journal+console";
          StandardError = "journal+console";
        };
        script = ''
          set -euo pipefail
          units=(
            "${mountUnit}"
            podman-${namespace}-postgres.service
            podman-${namespace}-mongodb.service
            podman-${namespace}-garage.service
            podman-${namespace}-registry.service
            nginx.service
          )
          containers=(${namespace}-postgres ${namespace}-mongodb ${namespace}-garage ${namespace}-registry)
          for attempt in {1..12}; do
            ready=true
            mountpoint -q ${data} || ready=false
            for unit in "''${units[@]}"; do
              systemctl is-active --quiet "$unit" || ready=false
            done
            for container in "''${containers[@]}"; do
              [[ $(podman inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true) == true ]] || ready=false
            done
            if [[ $ready == true ]]; then
              sleep 5
              for container in "''${containers[@]}"; do
                [[ $(podman inspect --format '{{.State.Running}}' "$container") == true ]]
              done
              echo "${namespace} NixOS storage services ready"
              exit 0
            fi
            echo "storage readiness elapsed=$((attempt * 5))s"
            sleep 5
          done
          systemctl --failed --no-pager || true
          journalctl --boot --no-pager --lines 120 || true
          echo "${namespace} NixOS storage readiness failed"
          exit 1
        '';
      };
      "${namespace}-registry-gc" = {
        description = "Garbage collect unreferenced ${platform.displayName} registry blobs";
        after = [ "podman-${namespace}-registry.service" ];
        requires = [ mountUnit ];
        serviceConfig = {
          Type = "oneshot";
          Environment = [
            "PLATFORM_CONFIG=/etc/${namespace}/platform.json"
            "REGISTRY_IMAGE=${platform.containers.registry}"
            "REGISTRY_DATA=${data}/registry"
            "REGISTRY_SERVICE=podman-${namespace}-registry.service"
            "DATA_MOUNT=${data}"
            "LOCK_FILE=/run/lock/${namespace}-registry-gc.lock"
            "PATH=${
              lib.makeBinPath [
                pkgs.coreutils
                pkgs.podman
                pkgs.util-linux
              ]
            }"
          ];
          ExecStart = "${infra}/registry/registry-gc.sh";
        };
      };
    }
  ];

  systemd.timers."${namespace}-registry-gc" = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "Sun *-*-01..07 04:30:00 UTC";
      RandomizedDelaySec = "30m";
      Persistent = true;
    };
  };

  # pg_hba lets any authenticated role reach any database, and a fresh database
  # inherits CONNECT for PUBLIC from template1. Together those let one project
  # open another project's database and read its catalogues. Close it at
  # initialisation so a new deployment is right from the start; the per-database
  # revoke in the storage helper covers databases created later.
  environment.etc."${namespace}/postgres-init/00-restrict-connect.sql".text = ''
    REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
    REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
    DO $$
    BEGIN
      EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', current_database());
    END
    $$;
  '';

  environment.etc."${namespace}/pg_hba.conf".text = ''
    local   all   all                              trust
    hostssl all   all   0.0.0.0/0                  scram-sha-256
    hostssl all   all   ::/0                       scram-sha-256
    hostnossl all  all   0.0.0.0/0                 reject
    hostnossl all  all   ::/0                      reject
  '';

  services.nginx = {
    enable = true;
    recommendedProxySettings = false;
    virtualHosts = {
      "${platform.internalNames.objectStorage}" = {
        onlySSL = true;
        listen = [
          {
            addr = "0.0.0.0";
            port = 9000;
            ssl = true;
          }
        ];
        sslCertificate = "/etc/${namespace}/pki/storage.pem";
        sslCertificateKey = "/etc/${namespace}/pki/storage-key.pem";
        locations."/" = {
          proxyPass = "http://127.0.0.1:19000";
          extraConfig = ''
            proxy_http_version 1.1;
            proxy_set_header Host $host:$server_port;
            proxy_set_header X-Forwarded-Proto https;
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_read_timeout 900s;
            proxy_send_timeout 900s;
            client_max_body_size 0;
          '';
        };
      };
      "${platform.internalNames.storage}" = {
        onlySSL = true;
        listen = [
          {
            addr = "0.0.0.0";
            port = 3903;
            ssl = true;
          }
        ];
        sslCertificate = "/etc/${namespace}/pki/storage.pem";
        sslCertificateKey = "/etc/${namespace}/pki/storage-key.pem";
        locations."/" = {
          proxyPass = "http://127.0.0.1:13903";
          extraConfig = ''
            proxy_http_version 1.1;
            proxy_set_header Host ${platform.internalNames.storage}:3903;
            proxy_set_header X-Forwarded-Proto https;
            client_max_body_size 1m;
          '';
        };
      };
    };
  };

  systemd.tmpfiles.rules = [
    "z /etc/${namespace} 0750 root storage-service -"
    "z /etc/${namespace}/pki 0750 root storage-service -"
    "d ${data}/postgres 0700 999 999 -"
    "d ${data}/mongodb 0700 999 999 -"
    "d ${data}/object-storage 0750 root root -"
    "d ${data}/registry 0750 root root -"
  ];
}
