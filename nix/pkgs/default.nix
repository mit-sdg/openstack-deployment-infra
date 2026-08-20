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

  python = pkgs.python3.withPackages (
    ps: with ps; [
      bcrypt
      boto3
      openstacksdk
      psycopg
      pymongo
      python-openstackclient
    ]
  );

  platformCliPython = pkgs.python314.withPackages (ps: [
    ps.boto3
    ps.psycopg
    ps.pymongo
    ps.pyyaml
  ]);

  platformCliInstaller = pkgs.writeShellApplication {
    name = "openstack-platform-install-release";
    runtimeInputs = [
      pkgs.git
      pkgs.uv
      platformCliPython
    ];
    text = ''
      exec ${platformCliPython}/bin/python ${../../deploy/platform-cli/install_release.py} "$@"
    '';
  };

  # The control plane reaches the helper at <paths.root>/bin, and a tmpfiles
  # rule repoints that name at this launcher on every boot. Running the helper
  # module from here would drop PLATFORM_CONFIG, which the helper requires, so
  # every helper call would fail after any admin replacement. Hand over to the
  # accepted release's own launcher, which validates and exports it.
  platformCliHelperLauncher = pkgs.writeShellScriptBin "openstack-platform-helper" ''
    set -eu
    release=${platform.paths.adminState}/controller/platform-cli/current
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
in
{
  inherit
    age
    nomad
    traefik
    buildkit
    python
    platformCliPython
    platformCliInstaller
    platformCliHelperLauncher
    ;
}
