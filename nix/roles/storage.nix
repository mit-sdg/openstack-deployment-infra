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
  # SigV4 covers Host, so each S3 name forwards the value its clients signed.
  garageS3VirtualHost =
    { host, default }:
    {
      onlySSL = true;
      listen = [
        {
          addr = "0.0.0.0";
          port = ports.garageS3;
          ssl = true;
          extraParameters = lib.optional default "default_server";
        }
      ];
      sslCertificate = "/etc/${namespace}/pki/storage.pem";
      sslCertificateKey = "/etc/${namespace}/pki/storage-key.pem";
      # Trust only ingress's appended client address; workers cannot forge it.
      extraConfig = ''
        set_real_ip_from ${platform.addresses.ingress};
        real_ip_header X-Forwarded-For;
      '';
      locations."/" = {
        proxyPass = "http://127.0.0.1:19000";
        extraConfig = ''
          limit_req zone=storage_bucket_rate burst=1000 nodelay;
          limit_conn storage_bucket_connections 200;
          # A peer cannot evade fairness by flooding invented bucket names.
          ${lib.optionalString default "limit_req zone=storage_peer_rate burst=200 nodelay; limit_conn storage_peer_connections 50;"}
          limit_req_status 429;
          limit_conn_status 429;
          proxy_http_version 1.1;
          proxy_set_header Host ${host};
          proxy_set_header X-Forwarded-Proto https;
          proxy_request_buffering off;
          proxy_buffering off;
          proxy_read_timeout 7200s;
          proxy_send_timeout 900s;
          client_max_body_size 0;
        '';
      };
    };
  # xl.16core: 16 vCPU / 64 GiB. 100 default 512 MiB DB instances use
  # 50 GiB after source removal. During migration admit 36 GiB + a separate
  # 2 GiB restore reserve, with two legacy 8 GiB sources + Garage 4 GiB +
  # registry 2 GiB: at most 60 GiB, leaving 4 GiB for OS/nginx/page cache.
  # The follow-up removes sources and raises admission/slice to 50/52 GiB.
  packages = import ../pkgs { inherit pkgs platform; };
  data = platform.paths.data;
  infra = ../../infra;
  systemdEscapePath =
    path: lib.replaceStrings [ "-" "/" ] [ "\\x2d" "-" ] (lib.removePrefix "/" path);
  mountUnit = "${systemdEscapePath data}.mount";
  dataLayoutUnit = "${namespace}-storage-data-layout.service";
  growUnit = "${namespace}-storage-growfs.service";
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
  networking.nftables.enable = true;
  # Reload only declarative tables; preserve the manager's isolated dynamic table.
  networking.nftables.flushRuleset = false;
  # The manager's earlier nft chain admits only the owning worker/admin IPs.
  networking.firewall.allowedTCPPortRanges = [
    {
      from = 30000;
      to = 30999;
    }
  ];
  # Bound host logs outside database projects: 1 GiB persistent history and
  # 256 MiB volatile logs fit the 4 GiB host-services/page-cache allowance.
  services.journald.extraConfig = ''
    SystemMaxUse=1G
    RuntimeMaxUse=256M
  '';
  boot.kernelModules = [ "bfq" ];
  # BFQ makes cgroup IOWeight effective on the virtual data-volume device.
  services.udev.extraRules = ''
    ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="vd[a-z]|sd[a-z]", ATTR{queue/scheduler}="bfq"
  '';
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
        # Temporary shared source: 100 connections covers the six live apps
        # and dumps. Buffers 2 GiB + 2 MiB work_mem fits its 8 GiB cap.
        "-c"
        "max_connections=100"
        "-c"
        "shared_buffers=2048MB"
        "-c"
        "work_mem=2MB"
        "-c"
        "maintenance_work_mem=64MB"
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
        # Four cores / 8 GiB serves existing apps while dumps run.
        "--memory=8192m"
        "--memory-swap=8192m"
        "--cpus=4"
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
        # Temporary source: 2 GiB cache leaves 6 GiB for the existing
        # app pools and migration; 800 connections bounds their thread growth.
        "--wiredTigerCacheSizeGB"
        "2"
        "--maxConns"
        "800"
        # Log operations over 100 ms without the overhead of profiling writes.
        "--slowms"
        "100"
        "--bind_ip_all"
        "--tlsMode"
        "requireTLS"
        "--tlsCertificateKeyFile"
        "/run/${namespace}-pki/mongodb-combined.pem"
        "--tlsCAFile"
        "/run/${namespace}-pki/internal-ca.pem"
        "--tlsAllowConnectionsWithoutCertificates"
      ];
      # The pinned image is MongoDB 8.0.29. defaultMaxTimeMS exists in 8.0,
      # but setClusterParameter is unsupported on this standalone deployment:
      # https://www.mongodb.com/docs/v8.0/reference/command/setclusterparameter/
      extraOptions = [
        "--memory=8192m"
        "--memory-swap=8192m"
        "--cpus=4"
        # TLS ping checks mongod readiness without secrets in process arguments.
        "--health-cmd=mongosh --quiet --tls --tlsCAFile /run/${namespace}-pki/internal-ca.pem --host 127.0.0.1 --tlsAllowInvalidHostnames --eval 'quit(db.runCommand({ping:1}).ok === 1 ? 0 : 1)'"
        "--health-interval=30s"
        "--health-start-period=90s"
        "--health-timeout=5s"
        "--health-retries=5"
      ];
    };
    "${namespace}-garage" = {
      image = platform.containers.garage;
      # 4 GiB gives 50 buckets metadata/cache headroom while bounding S3 memory.
      extraOptions = [
        "--memory=4096m"
        "--memory-swap=4096m"
        "--cpus=2"
      ];
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
      # 2 GiB supports concurrent builder image streams; blobs stay on disk.
      extraOptions = [
        "--memory=2048m"
        "--memory-swap=2048m"
        "--cpus=1"
      ];
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
    {
      # nsncd listens through /var/run; RuntimeDirectory only exempts /run/nscd
      # from its strict sandbox. Permit its socket directory at either alias,
      # including fresh images with a real /var/run directory.
      nscd.serviceConfig.ReadWritePaths = [ "/var/run/nscd" ];
    }
    (mkContainerDependencies "${namespace}-postgres")
    (mkContainerDependencies "${namespace}-mongodb")
    (mkContainerDependencies "${namespace}-garage")
    (mkContainerDependencies "${namespace}-registry")
    {
      "${namespace}-storage-growfs" = {
        description = "Grow mounted storage XFS to the attached Cinder volume";
        after = [ mountUnit ];
        requires = [ mountUnit ];
        before = [ dataLayoutUnit ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${pkgs.xfsprogs}/bin/xfs_growfs ${data}";
        };
      };
      "${namespace}-storage-data-layout" = {
        description = "Prepare ${platform.displayName} mounted storage layout";
        after = [
          mountUnit
          growUnit
        ];
        requires = [
          mountUnit
          growUnit
        ];
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
        CPUQuota = "400%";
        IOWeight = 100;
        ExecStartPre = [ "${credentialGuard} /etc/${namespace}/secrets/postgres-password" ];
        LoadCredential = "postgres-password:/etc/${namespace}/secrets/postgres-password";
      };
      "podman-${namespace}-mongodb".serviceConfig = {
        CPUQuota = "400%";
        IOWeight = 100;
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
      "${namespace}-database@" = {
        description = "Isolated database instance %i";
        after = [
          "${namespace}-storage-instance-manager.service"
          mountUnit
        ];
        requires = [ mountUnit ];
        wantedBy = [ ];
        serviceConfig = {
          ExecStart = "${packages.controllerPackage}/bin/openstack-platform-storage-manager --config /etc/${namespace}/platform.json --run-instance %i";
          # --rm normally removes it; also clean up after a killed Podman CLI.
          # The fixed name retains bind-mounted data and never deletes a volume.
          ExecStopPost = "${pkgs.podman}/bin/podman rm --force --ignore ${namespace}-db-%i";
          Slice = "${namespace}-databases.slice";
          Restart = "always";
          # 5 seconds avoids a tight OOM/crash restart loop; only this DB restarts.
          RestartSec = 5;
          TimeoutStopSec = 180;
          KillSignal = "SIGINT";
          KillMode = "mixed";
          LimitCORE = 0;
          # Per-instance drop-ins supplied by the manager set MemoryMax,
          # MemorySwapMax=0, CPUQuota, CPUWeight=100, IOWeight=100, TasksMax=256.
          # At most 200 messages per 30 seconds per DB prevents log floods.
          LogRateLimitIntervalSec = 30;
          LogRateLimitBurst = 200;
        };
        path = [ pkgs.podman ];
      };
      "${namespace}-storage-instance-manager" = {
        description = "Authenticated isolated database instance manager";
        wantedBy = [ "multi-user.target" ];
        after = [
          "cloud-final.service"
          "nftables.service"
          mountUnit
          dataLayoutUnit
        ];
        requires = [
          "nftables.service"
          mountUnit
          dataLayoutUnit
        ];
        # pg_dump 17 matches the pinned PostgreSQL 17.11 source image.
        path = [
          pkgs.systemd
          pkgs.nftables
          pkgs.xfsprogs
          pkgs.coreutils
          pkgs.postgresql_17
          pkgs.mongodb-tools
        ];
        serviceConfig = {
          ExecStart = "${packages.controllerPackage}/bin/openstack-platform-storage-manager --config /etc/${namespace}/platform.json";
          LoadCredential = "garage-config:/etc/${namespace}/garage.toml";
          Restart = "on-failure";
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          ReadWritePaths = [
            data
            "/run/systemd"
            "/etc/systemd/system"
          ];
          RestrictAddressFamilies = [
            "AF_UNIX"
            "AF_INET"
            "AF_NETLINK"
          ];
          # Manager tools may reach only local databases; TLS/bearer requests
          # arrive through nginx on loopback. No SSH command execution surface.
          IPAddressDeny = "any";
          IPAddressAllow = [
            "localhost"
            platform.addresses.storage
            "10.88.0.0/16"
          ];
          # Streaming tools/JSON fit 512 MiB; one core/128 tasks bound control
          # work inside the 4 GiB host allowance instead of competing with DBs.
          MemoryMax = "512M";
          MemorySwapMax = 0;
          CPUQuota = "100%";
          TasksMax = 128;
          LimitCORE = 0;
        };
      };
      "${namespace}-storage-host-status" = {
        description = "Authenticated read-only storage host metrics";
        wantedBy = [ "multi-user.target" ];
        after = [
          "cloud-final.service"
          mountUnit
        ];
        requires = [
          "cloud-final.service"
          mountUnit
        ];
        path = [ pkgs.podman ];
        serviceConfig = {
          ExecStart = "${pkgs.python3}/bin/python3 ${infra}/monitor/storage_host.py --data ${data} --namespace ${namespace}";
          LoadCredential = "garage-config:/etc/${namespace}/garage.toml";
          Restart = "on-failure";
          # Podman inspect and cgroup reads need the host's root namespace.
          ProtectSystem = "strict";
          # podman inspect takes local metadata locks; it receives fixed names.
          ReadWritePaths = [
            "/run/libpod"
            "-/run/containers"
            "-/run/crun"
            "-/run/runc"
            "-/var/lib/containers"
          ];
          ProtectHome = true;
          PrivateTmp = true;
          NoNewPrivileges = true;
          RestrictAddressFamilies = [
            "AF_UNIX"
            "AF_INET"
          ];
          IPAddressDeny = "any";
          IPAddressAllow = "localhost";
          LimitCORE = 0;
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

  systemd.slices."${namespace}-databases".sliceConfig = {
    # Twelve of sixteen cores bound aggregate DB load, leaving four for
    # Garage, the registry, the migration source and host administration.
    CPUQuota = "1200%";
    MemoryMax = "38G";
    MemorySwapMax = 0;
  };

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

  # Never allocate a client ephemeral source port in the instance range. This
  # also protects hosts whose default ephemeral range changes in the future.
  boot.kernel.sysctl."net.ipv4.ip_local_reserved_ports" = "30000-30999";

  services.nginx = {
    enable = true;
    recommendedProxySettings = false;
    appendHttpConfig = ''
      # 200 r/s + 1000 burst + 200 requests per bucket accommodates a class
      # loading normal browser assets. Public NAT peers share only bucket caps.
      # Trusted admin logical backups bypass these student fairness budgets.
      map $uri $storage_bucket {
        "~^/([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])(?:/|$)" $1;
        default $remote_addr;
      }
      map $remote_addr $storage_bucket_key {
        ${platform.addresses.admin} "";
        default $storage_bucket;
      }
      map $remote_addr $storage_peer_key {
        ${platform.addresses.admin} "";
        default $binary_remote_addr;
      }
      limit_req_zone $storage_bucket_key zone=storage_bucket_rate:16m rate=200r/s;
      limit_conn_zone $storage_bucket_key zone=storage_bucket_connections:16m;
      limit_req_zone $storage_peer_key zone=storage_peer_rate:16m rate=100r/s;
      limit_conn_zone $storage_peer_key zone=storage_peer_connections:16m;
    '';
    virtualHosts = {
      # Apps sign Host as <storage IP>:port; this stays the default server.
      "${platform.internalNames.objectStorage}" = garageS3VirtualHost {
        host = "$host:$server_port";
        default = true;
      };
      # Public ingress keeps the browser's Host, so presigned URLs signed for
      # s3.<domain> verify. The same certificate serves both names.
      "s3.${platform.domain}" = garageS3VirtualHost {
        host = "$host";
        default = false;
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
        # Existing trusted TLS channel and Garage admin bearer authentication;
        # the loopback server exposes one fixed GET and no provider mutations.
        locations."= /platform/instances" = {
          proxyPass = "http://127.0.0.1:19002";
          extraConfig = ''
            limit_except POST { deny all; }
            proxy_set_header Authorization $http_authorization;
            proxy_read_timeout 7200s;
            client_max_body_size 64k;
          '';
        };
        locations."= /platform/host-status" = {
          proxyPass = "http://127.0.0.1:19001";
          extraConfig = ''
            limit_except GET { deny all; }
            proxy_set_header Authorization $http_authorization;
            proxy_read_timeout 10s;
            client_max_body_size 1k;
          '';
        };
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
    "d /var/run/nscd 0755 nscd nscd -"
    # Podman opens /run/libpod/alive.lck even for exec/inspect. Prepare runtime
    # paths before the strict service sandboxes install their writable mounts.
    "d /run/libpod 0751 root root -"
    "d /run/crun 0700 root root -"
    "d /run/runc 0700 root root -"
    "z /etc/${namespace} 0750 root storage-service -"
    "z /etc/${namespace}/pki 0750 root storage-service -"
  ];
}
