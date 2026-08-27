from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ROOT / "nix/roles/admin.nix"
PACKAGES = ROOT / "nix/pkgs/default.nix"
HELPER_DEPLOY = ROOT / "deploy/platform-cli/deploy_helper_release.sh"


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
        self.assertIn("${packages.platformController}/bin/openstack-platform-controller", admin)
        self.assertIn('"--platform-config /etc/${namespace}/platform.json"', admin)
        self.assertIn('"--policy ${controllerPolicy}"', admin)
        self.assertIn('"--socket ${controllerSocket}"', admin)
        self.assertIn('helperReleaseRoot = "${operatorRoot}/platform-cli";', admin)
        self.assertIn(
            "release=${platform.paths.adminState}/operator/platform-cli/current", packages
        )
        self.assertIn(
            'release_root="$admin_state/operator/platform-cli"',
            HELPER_DEPLOY.read_text(encoding="utf-8"),
        )
        self.assertIn('"d ${controllerRoot} 0700 ${controllerUser} ${controllerGroup} -"', admin)
        self.assertIn('"d ${helperReleaseRoot} 0750 agentops agentops -"', admin)
        self.assertNotIn("ssh", admin[admin.index('systemd.services."${namespace}-controller"') :])

    def test_controller_sandbox_and_ingress_only_web_boundary_are_explicit(self) -> None:
        source = ADMIN.read_text(encoding="utf-8")
        service = source[
            source.index('systemd.services."${namespace}-controller"') : source.index(
                'systemd.paths."${namespace}-controller"'
            )
        ]
        for setting in (
            'NoNewPrivileges = true;',
            'ProtectSystem = "strict";',
            'ProtectHome = true;',
            'PrivateDevices = true;',
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
            "--dport 8080 -j nixos-fw-accept",
            allowed_ports,
        )
        self.assertNotIn('systemd.services."management-web"', source)


if __name__ == "__main__":
    unittest.main()
