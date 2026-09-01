{
  constants,
  lib,
  pkgs,
  platform,
  ...
}:
let
  namespace = platform.namespace;
  ports = constants.ports;
  data = platform.paths.data;
  infra = ../../infra;
  systemdEscapePath =
    path: lib.replaceStrings [ "-" "/" ] [ "\\x2d" "-" ] (lib.removePrefix "/" path);
  mountUnit = "${systemdEscapePath data}.mount";
  dataLayoutUnit = "${namespace}-storage-data-layout.service";
  credentialGuard = pkgs.writeShellScript "${namespace}-storage-credential-guard" ''
    set -euo pipefail
    path=$1
    test -f "$path" && test ! -L "$path"
    test "$(stat -c %U:%a "$path")" = root:600
    test "$(stat -c %s "$path")" -le 65536
  '';
  mongodbRuntimeDirectory = "/run/${namespace}-mongodb-credential";
  mongodbRuntimeSecret = "${mongodbRuntimeDirectory}/mongodb-password";
  stageMongoCredential = pkgs.writeShellScript "${namespace}-mongodb-credential-stage" ''
    set -euo pipefail
    source="''${CREDENTIALS_DIRECTORY:?}/mongodb-password"
    ${pkgs.coreutils}/bin/install -d -m 0710 -o root -g storage-service \
      ${lib.escapeShellArg mongodbRuntimeDirectory}
    ${pkgs.coreutils}/bin/install -m 0400 -o storage-service -g storage-service \
      "$source" ${lib.escapeShellArg mongodbRuntimeSecret}
  '';
  mkContainerDependencies = name: {
    "podman-${name}" = {
      after = [
        "cloud-final.service"
        mountUnit
        dataLayoutUnit
      ];
      requires = [
        "cloud-final.service"
        mountUnit
        dataLayoutUnit
      ];
      serviceConfig = {
        StandardOutput = "journal+console";
        StandardError = "journal+console";
        LimitCORE = 0;
      };
    };
  };
in
{
  networking.hostName = platform.hosts.storage;
  networking.firewall.allowedTCPPorts = with constants.ports; [
    ssh
    garageRpc
    registry
    postgres
    garageS3
    mongodb
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
      environment = {
        POSTGRES_USER = "platform_admin";
        POSTGRES_PASSWORD_FILE = "/run/secrets/postgres-password";
        POSTGRES_DB = "platform";
        POSTGRES_INITDB_ARGS = "--auth-host=scram-sha-256";
      };
      volumes = [
        "${data}/postgres:/var/lib/postgresql/data"
        "/run/credentials/podman-${namespace}-postgres.service/postgres-password:/run/secrets/postgres-password:ro"
        "/etc/${namespace}/pki:/run/${namespace}-pki:ro"
        "/etc/${namespace}/pg_hba.conf:/run/${namespace}-pg_hba.conf:ro"
        "/etc/${namespace}/postgres-init:/docker-entrypoint-initdb.d:ro"
      ];
      ports = [ "${toString ports.postgres}:${toString ports.postgres}" ];
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
      environment = {
        MONGO_INITDB_ROOT_USERNAME = "platform_admin";
        MONGO_INITDB_ROOT_PASSWORD_FILE = "/run/secrets/mongodb-password";
      };
      volumes = [
        "${data}/mongodb:/data/db"
        "${mongodbRuntimeDirectory}:/run/secrets:ro"
        "/etc/${namespace}/pki:/run/${namespace}-pki:ro"
      ];
      ports = [ "${toString ports.mongodb}:${toString ports.mongodb}" ];
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
        "/run/credentials/podman-${namespace}-garage.service/garage-config:/etc/garage.toml:ro"
        "${data}/object-storage:/var/lib/garage"
      ];
      ports = [
        "127.0.0.1:19000:3900"
        "127.0.0.1:${toString ports.garageAdminProxy}:${toString ports.garageRpc}"
      ];
      cmd = [
        "/garage"
        "server"
        "--single-node"
      ];
    };
    "${namespace}-registry" = {
      image = platform.containers.registry;
      environmentFiles = [ "/run/credentials/podman-${namespace}-registry.service/registry.env" ];
      volumes = [
        "${data}/registry:/var/lib/registry"
        "/etc/${namespace}/registry.htpasswd:/auth/htpasswd:ro"
        "/etc/${namespace}/pki:/pki:ro"
      ];
      ports = [ "${toString ports.registry}:${toString ports.registry}" ];
    };
  };

  systemd.services = lib.mkMerge [
    (mkContainerDependencies "${namespace}-postgres")
    (mkContainerDependencies "${namespace}-mongodb")
    (mkContainerDependencies "${namespace}-garage")
    (mkContainerDependencies "${namespace}-registry")
    {
      "${namespace}-storage-data-layout" = {
        description = "Prepare ${platform.displayName} mounted storage layout";
        after = [ mountUnit ];
        requires = [ mountUnit ];
        before = [
          "podman-${namespace}-postgres.service"
          "podman-${namespace}-mongodb.service"
          "podman-${namespace}-garage.service"
          "podman-${namespace}-registry.service"
        ];
        path = [
          pkgs.coreutils
          pkgs.util-linux
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        script = ''
          set -euo pipefail
          mountpoint -q ${data}
          install -d -m 0700 -o 999 -g 999 ${data}/postgres ${data}/mongodb
          install -d -m 0750 -o root -g root ${data}/object-storage ${data}/registry
        '';
      };
      nginx = {
        after = [ "cloud-final.service" ];
        requires = [ "cloud-final.service" ];
        serviceConfig = {
          SupplementaryGroups = [ "storage-service" ];
          StandardOutput = "journal+console";
          StandardError = "journal+console";
        };
      };
      "podman-${namespace}-postgres".serviceConfig = {
        ExecStartPre = [ "${credentialGuard} /etc/${namespace}/secrets/postgres-password" ];
        LoadCredential = "postgres-password:/etc/${namespace}/secrets/postgres-password";
      };
      "podman-${namespace}-mongodb".serviceConfig = {
        ExecStartPre = [
          "${credentialGuard} /etc/${namespace}/secrets/mongodb-password"
          stageMongoCredential
        ];
        LoadCredential = "mongodb-password:/etc/${namespace}/secrets/mongodb-password";
      };
      "podman-${namespace}-garage".serviceConfig = {
        ExecStartPre = [ "${credentialGuard} /etc/${namespace}/garage.toml" ];
        LoadCredential = "garage-config:/etc/${namespace}/garage.toml";
      };
      "podman-${namespace}-registry".serviceConfig = {
        ExecStartPre = [ "${credentialGuard} /etc/${namespace}/registry.env" ];
        LoadCredential = "registry.env:/etc/${namespace}/registry.env";
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
            port = ports.garageS3;
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
            port = ports.garageRpc;
            ssl = true;
          }
        ];
        sslCertificate = "/etc/${namespace}/pki/storage.pem";
        sslCertificateKey = "/etc/${namespace}/pki/storage-key.pem";
        locations."/" = {
          proxyPass = "http://127.0.0.1:${toString ports.garageAdminProxy}";
          extraConfig = ''
            proxy_http_version 1.1;
            proxy_set_header Host ${platform.internalNames.storage}:${toString ports.garageRpc};
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
  ];
}
