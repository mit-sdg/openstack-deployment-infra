{ pkgs, platform }:
let
  inherit (platform) versions checksums;

  nomad = pkgs.stdenvNoCC.mkDerivation {
    pname = "nomad";
    version = versions.nomad;
    src = pkgs.fetchurl {
      url = "https://releases.hashicorp.com/nomad/${versions.nomad}/nomad_${versions.nomad}_linux_amd64.zip";
      sha256 = checksums.nomadLinuxAmd64Zip;
    };
    nativeBuildInputs = [
      pkgs.autoPatchelfHook
      pkgs.unzip
    ];
    buildInputs = [ pkgs.glibc ];
    sourceRoot = ".";
    installPhase = ''
      install -Dm755 nomad "$out/bin/nomad"
    '';
    doInstallCheck = true;
    installCheckPhase = ''
      "$out/bin/nomad" version >/dev/null
    '';
  };

  traefik = pkgs.stdenvNoCC.mkDerivation {
    pname = "traefik";
    version = versions.traefik;
    src = pkgs.fetchurl {
      url = "https://github.com/traefik/traefik/releases/download/v${versions.traefik}/traefik_v${versions.traefik}_linux_amd64.tar.gz";
      sha256 = checksums.traefikLinuxAmd64TarGz;
    };
    sourceRoot = ".";
    installPhase = ''
      install -Dm755 traefik "$out/bin/traefik"
    '';
    doInstallCheck = true;
    installCheckPhase = ''
      "$out/bin/traefik" version >/dev/null
    '';
  };

  buildkit = pkgs.stdenvNoCC.mkDerivation {
    pname = "buildkit";
    version = versions.buildkit;
    src = pkgs.fetchurl {
      url = "https://github.com/moby/buildkit/releases/download/v${versions.buildkit}/buildkit-v${versions.buildkit}.linux-amd64.tar.gz";
      sha256 = checksums.buildkitLinuxAmd64TarGz;
    };
    sourceRoot = ".";
    installPhase = ''
      install -Dm755 bin/buildkitd "$out/bin/buildkitd"
      install -Dm755 bin/buildctl "$out/bin/buildctl"
      install -Dm755 bin/buildkit-runc "$out/bin/buildkit-runc"
    '';
    doInstallCheck = true;
    installCheckPhase = ''
      "$out/bin/buildctl" --version >/dev/null
      "$out/bin/buildkitd" --version >/dev/null
      "$out/bin/buildkit-runc" --version >/dev/null
    '';
  };

  age = pkgs.age;

  # Older Neutron exposes security-group ownership only as tenant_id. OSC hides
  # that deprecated column; SDK 4.13 must project it as project_id, as Port and
  # Subnet already do. Override the package scope so OSC and direct SDK consumers
  # use the same patched dependency in every admin/helper/provider launcher.
  openstackPython = pkgs.python3.override {
    packageOverrides = _final: prev: {
      openstacksdk = prev.openstacksdk.overridePythonAttrs (old: {
        patches = (old.patches or [ ]) ++ [ ./openstacksdk-security-group-project-alias.patch ];
        # The patched runtime does not need to regenerate upstream manuals.
        outputs = [ "out" ];
        nativeBuildInputs = builtins.filter (
          input: input != prev.sphinxHook && input != prev.openstackdocstheme
        ) (old.nativeBuildInputs or [ ]);
      });
      python-openstackclient = prev.python-openstackclient.overridePythonAttrs (old: {
        build-system = builtins.filter (
          input:
          input != prev.sphinxHook && input != prev.openstackdocstheme && input != prev.sphinxcontrib-apidoc
        ) old.build-system;
        # Our exact patched SDK/CLI is exercised by package-smoke. Re-running
        # thousands of unrelated upstream CLI tests on every image adds no
        # coverage of the local ownership projection patch.
        doCheck = false;
      });
    };
  };

  python = openstackPython.withPackages (
    ps: with ps; [
      bcrypt
      boto3
      openstacksdk
      psycopg
      pymongo
      python-openstackclient
    ]
  );

  platformPython = pkgs.python314.withPackages (ps: [
    ps.boto3
    ps.psycopg
    ps.pymongo
  ]);
  # The OpenStack CLI also ships Python 3.14. Use an unambiguous stable name,
  # rather than relying on the system profile's colliding python3.14 links.
  managementPython = pkgs.writeShellScriptBin "management-python3.14" ''
    exec ${platformPython}/bin/python "$@"
  '';

  rootPathPlan = pkgs.writeText "${platform.namespace}-controller-path-plan.json" (
    builtins.toJSON (
      import ../lib/controller-paths.nix {
        inherit platform;
        constants = import ../lib/constants.nix;
      }
    )
  );
  rootPathPreflight = pkgs.symlinkJoin {
    name = "${platform.namespace}-root-path-preflight";
    paths = [
      (pkgs.writeShellScriptBin "openstack-platform-root-path-preflight" ''
        exec ${platformPython}/bin/python -I -B ${../../openstack_platform/host_paths.py} preflight --plan ${rootPathPlan} "$@"
      '')
      (pkgs.runCommand "${platform.namespace}-root-path-review-files" { } ''
        mkdir -p "$out/share/root-path-preflight"
        cp ${../../openstack_platform/host_paths.py} "$out/share/root-path-preflight/host_paths.py"
        cp ${rootPathPlan} "$out/share/root-path-preflight/controller-path-plan.json"
      '')
    ];
  };

  controllerPackage = pkgs.python314Packages.buildPythonApplication {
    pname = "openstack-platform-controller";
    version = "0.1.0";
    pyproject = true;
    src = pkgs.lib.cleanSource ../..;
    build-system = [ pkgs.python314Packages.hatchling ];
    dependencies = with pkgs.python314Packages; [
      bcrypt
      boto3
      psycopg
      pymongo
    ];
    doCheck = false;
    postInstall = ''
      rm "$out/bin/openstack-platform" \
        "$out/bin/openstack-platform-helper" \
        "$out/bin/openstack-platform-restore"
    '';
    pythonImportsCheck = [ "openstack_platform.controller.main" ];
  };

  releaseInstaller = pkgs.writeShellApplication {
    name = "openstack-platform-install-release";
    runtimeInputs = [
      pkgs.git
      pkgs.uv
      platformPython
    ];
    text = ''
      export PLATFORM_MANAGEMENT_ENVIRONMENT=production
      exec ${platformPython}/bin/python -I ${../../deploy/releases/install_release.py} "$@"
    '';
  };

  # The control plane reaches the helper at <paths.root>/bin, and a tmpfiles
  # rule repoints that name at this launcher on every boot. Running the helper
  # module from here would drop PLATFORM_CONFIG, which the helper requires, so
  # every helper call would fail after any admin replacement. Hand over to the
  # accepted release's own launcher, which validates and exports it.
  helperLauncher = pkgs.writeShellScriptBin "openstack-platform-helper" ''
    set -eu
    release=${platform.paths.adminState}/operator/helper-releases/current
    if [[ ! -f "$release/.complete" ]]; then
      echo "no accepted helper release" >&2
      exit 69
    fi
    launcher="$release/bin/openstack-platform-helper"
    if [[ ! -x "$launcher" ]]; then
      echo "accepted helper release has no launcher" >&2
      exit 69
    fi
    exec "$launcher" "$@"
  '';

  imageSmoke = pkgs.writeShellApplication {
    name = "openstack-platform-image-smoke";
    runtimeInputs = [
      pkgs.cdrkit
      pkgs.xfsprogs
      pkgs.python3
      pkgs.qemu
    ];
    text = ''
      export PLATFORM_CONFIG="''${PLATFORM_CONFIG:-${pkgs.writeText "smoke-platform.json" (builtins.toJSON platform)}}"
      exec ${../../tests/smoke_openstack_image.sh} "$@"
    '';
  };
in
{
  inherit
    age
    nomad
    traefik
    buildkit
    python
    platformPython
    managementPython
    rootPathPlan
    rootPathPreflight
    controllerPackage
    releaseInstaller
    helperLauncher
    imageSmoke
    ;
}
