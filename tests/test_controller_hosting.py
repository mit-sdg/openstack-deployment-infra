from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / "nix/roles/admin.nix"
PACKAGES = ROOT / "nix/pkgs/default.nix"
HELPER_DEPLOY = ROOT / "deploy/releases/deploy_helper_release.sh"


class ControllerHostingStaticTests(unittest.TestCase):
    def test_web_identity_has_only_its_private_and_socket_groups(self) -> None:
        source = ADMIN.read_text(encoding="utf-8")
        match = re.search(
            r"users\.users\.\$\{managementWebUser\} = \{(?P<body>.*?)\n  \};",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("group = managementWebUser;", body)
        self.assertIn("extraGroups = [ controllerSocketGroup ];", body)
        for trusted_group in ("agentops", "platform-admin", "nomad", "platform-controller"):
            self.assertNotIn(f'"{trusted_group}"', body)

    def test_controller_is_packaged_and_uses_fixed_local_paths(self) -> None:
        admin = ADMIN.read_text(encoding="utf-8")
        packages = PACKAGES.read_text(encoding="utf-8")
        self.assertIn('pname = "openstack-platform-controller";', packages)
        self.assertIn("${packages.controllerPackage}/bin/openstack-platform-controller", admin)
        self.assertIn('"--platform-config /etc/${namespace}/platform.json"', admin)
        self.assertIn('"--policy ${controllerPolicy}"', admin)
        self.assertIn('"--socket ${controllerSocket}"', admin)
        self.assertIn('helperReleaseRoot = "${operatorRoot}/helper-releases";', admin)
        self.assertIn('controllerSocket = "/run/${controllerSocketDirectory}/project.sock";', admin)
        self.assertIn(
            'controllerPrivilegedSocket = "/run/${controllerSocketDirectory}/privileged.sock";',
            admin,
        )
        self.assertIn(
            '"--project-peer ${toString managementWebAccount.uid}:${toString managementWebAccount.gid}"',
            admin,
        )
        self.assertIn(
            '"--privileged-peer ${toString operatorAccount.uid}:${toString operatorAccount.gid}"',
            admin,
        )
        self.assertIn(
            "release=${platform.paths.adminState}/operator/helper-releases/current", packages
        )
        self.assertIn(
            'release_root="$admin_state/operator/helper-releases"',
            HELPER_DEPLOY.read_text(encoding="utf-8"),
        )
        self.assertIn('"d ${controllerRoot} 0700 ${controllerUser} ${controllerGroup} -"', admin)
        self.assertIn(
            '"d ${helperReleaseRoot} 0750 ${operatorAccount.name} ${operatorAccount.name} -"',
            admin,
        )
        self.assertNotIn("ssh", admin[admin.index('systemd.services."${namespace}-controller"') :])

    def test_hosted_database_has_distinct_encrypted_backup_and_offline_restore_units(self) -> None:
        source = ADMIN.read_text(encoding="utf-8")
        self.assertIn(
            'hostedControllerBackupRoot = "${backups}/${constants.directories.hostedControllerBackup}";',
            source,
        )
        self.assertIn('systemd.services."${namespace}-hosted-controller-backup"', source)
        self.assertIn('systemd.timers."${namespace}-hosted-controller-backup"', source)
        self.assertIn('"--state-directory ${controllerState}"', source)
        self.assertIn('"--backup-root ${hostedControllerBackupRoot}"', source)
        self.assertIn('"--age-command ${pkgs.age}/bin/age"', source)
        self.assertIn("openstack-platform-hosted-controller-restore", source)
        self.assertIn("openstack-platform-controller-restore", source)
        self.assertIn("refusing restore while $unit is active", source)
        self.assertIn("${namespace}-hosted-controller-backup.timer", source)
        self.assertIn("--destination ${controllerState}/platform.sqlite3", source)
        self.assertIn(
            "restore input must be a direct ${controllerUser}-owned mode-0600 file", source
        )
        contract = (ROOT / "infra/lib/platform_contract.json").read_text(encoding="utf-8")
        self.assertIn('"hostedControllerBackup": "hosted-controller"', contract)
        self.assertIn('"controllerBackup": "controller"', contract)

    def test_controller_sandbox_and_ingress_only_web_boundary_are_explicit(self) -> None:
        source = ADMIN.read_text(encoding="utf-8")
        service = source[
            source.index('systemd.services."${namespace}-controller"') : source.index(
                'systemd.paths."${namespace}-controller"'
            )
        ]
        for setting in (
            "NoNewPrivileges = true;",
            'ProtectSystem = "strict";',
            "ProtectHome = true;",
            "PrivateDevices = true;",
            'CapabilityBoundingSet = "";',
            "ReadOnlyPaths = [",
            "ReadWritePaths = [",
        ):
            self.assertIn(setting, service)
        allowed_ports = source[
            source.index("networking.firewall.allowedTCPPorts") : source.index(
                "users.groups.${controllerSocketGroup}"
            )
        ]
        self.assertNotIn("\n    8080\n", allowed_ports.split("extraCommands", 1)[0])
        self.assertIn(
            "iptables -A nixos-fw -p tcp -s ${platform.addresses.ingress}/32 "
            "--dport ${toString constants.ports.managementWeb} -j nixos-fw-accept",
            allowed_ports,
        )
        management = source[
            source.index('systemd.services."${namespace}-management-web"') : source.index(
                'systemd.services."${namespace}-controller-readiness"'
            )
        ]
        for setting in (
            "CONTROLLER_PROJECT_SOCKET = controllerSocket;",
            'IPAddressDeny = "any";',
            'IPAddressAllow = "${platform.addresses.ingress}/32";',
            'SocketBindAllow = "tcp:${toString constants.ports.managementWeb}";',
            'SocketBindDeny = "any";',
            "InaccessiblePaths = [",
            "operatorRoot",
            "controllerRoot",
            '"/etc/${namespace}/pki"',
        ):
            self.assertIn(setting, management)
        self.assertNotIn("controllerPrivilegedSocket", management)

    def test_nix_uses_named_inventory_validation_and_runtime_constants(self) -> None:
        flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        constants = (ROOT / "nix/lib/constants.nix").read_text(encoding="utf-8")
        inventory = (ROOT / "nix/lib/inventory.nix").read_text(encoding="utf-8")
        self.assertIn("platform = inventory.load platformConfigPath;", flake)
        self.assertIn("roles = constants.roles;", flake)
        self.assertIn("contract.roles.all", constants)
        self.assertIn("../../infra/lib/platform_contract.json", constants)
        self.assertNotIn("managementWeb = 8080;", constants)
        self.assertIn("platform inventory is missing required values", inventory)


if __name__ == "__main__":
    unittest.main()
