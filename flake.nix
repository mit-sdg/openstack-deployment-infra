{
  description = "OpenStack deployment platform NixOS role images";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      lib = nixpkgs.lib;
      platformConfigOverride = builtins.getEnv "PLATFORM_CONFIG";
      platformConfigPath =
        if platformConfigOverride == "" then
          ./config/platform.example.json
        else
          builtins.toPath platformConfigOverride;
      platform = builtins.fromJSON (builtins.readFile platformConfigPath);
      roles = [
        "admin"
        "ingress"
        "storage"
        "worker"
        "builder"
      ];

      mkConfiguration =
        role:
        lib.nixosSystem {
          inherit system;
          specialArgs = { inherit platform role; };
          modules = [
            "${nixpkgs}/nixos/maintainers/scripts/openstack/openstack-image.nix"
            {
              image.baseName = platform.images.${role};
              nixpkgs.hostPlatform = system;
            }
            ./nix/modules/common.nix
            (./nix/roles + "/${role}.nix")
          ];
        };

      configurations = lib.genAttrs roles mkConfiguration;
      pkgs = import nixpkgs { inherit system; };
      rolePackages = import ./nix/pkgs { inherit pkgs platform; };
      roleVmTests = import ./nix/tests { inherit pkgs platform; };
      evaluationChecks = lib.mapAttrs' (
        role: configuration:
        lib.nameValuePair "eval-${role}" (
          pkgs.runCommand "${platform.namespace}-eval-${role}" { } ''
            printf '%s\n' '${builtins.unsafeDiscardStringContext configuration.config.system.build.toplevel.drvPath}' > "$out"
          ''
        )
      ) configurations;
    in
    {
      nixosConfigurations = lib.mapAttrs' (
        role: configuration: lib.nameValuePair "${platform.prefix}-${role}" configuration
      ) configurations;

      packages.${system} = {
        admin-image = configurations.admin.config.system.build.openstackImage;
        ingress-image = configurations.ingress.config.system.build.openstackImage;
        storage-image = configurations.storage.config.system.build.openstackImage;
        worker-image = configurations.worker.config.system.build.openstackImage;
        builder-image = configurations.builder.config.system.build.openstackImage;
        inherit (rolePackages)
          age
          python
          nomad
          traefik
          buildkit
          platformCliPython
          platformCliInstaller
          platformCliHelperLauncher
          ;
        default = configurations.admin.config.system.build.openstackImage;
      };

      checks.${system} =
        evaluationChecks
        // lib.mapAttrs' (role: test: lib.nameValuePair "vm-${role}" test) roleVmTests
        // {
          package-smoke = pkgs.runCommand "${platform.namespace}-package-smoke" { } ''
            ${rolePackages.nomad}/bin/nomad version >/dev/null
            ${rolePackages.traefik}/bin/traefik version >/dev/null
            ${rolePackages.buildkit}/bin/buildctl --version >/dev/null
            ${rolePackages.buildkit}/bin/buildkitd --version >/dev/null
            ${rolePackages.buildkit}/bin/buildkit-runc --version >/dev/null
            ${rolePackages.age}/bin/age --version >/dev/null
            ${rolePackages.python}/bin/openstack --version >/dev/null
            ${rolePackages.platformCliPython}/bin/python -c 'import sys, yaml; assert sys.version_info[:2] == (3, 14)'
            ${rolePackages.platformCliInstaller}/bin/openstack-platform-install-release --help >/dev/null
            touch "$out"
          '';
        };

      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          deadnix
          jq
          nixfmt-rfc-style
          python3
          shellcheck
          statix
        ];
      };

      formatter.${system} = pkgs.nixfmt-rfc-style;
    };
}
