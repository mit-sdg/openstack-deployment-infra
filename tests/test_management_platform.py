"""Configuration and Nix hosting boundary assertions for owner integration."""

from __future__ import annotations

import json
from pathlib import Path

from openstack_platform.management.config import Config
from tests.test_management import ROOT, ManagementCase


class ManagementPlatformTests(ManagementCase):
    def test_infra_validator_copy_matches_package(self) -> None:
        # Storage and other roles ship infra/ without openstack_platform/.
        self.assertEqual(
            (ROOT / "infra/lib/owner_portal_config.py").read_bytes(),
            (ROOT / "openstack_platform/owner_portal_config.py").read_bytes(),
        )

    def production_config(self) -> dict:
        from openstack_platform.config import load_platform
        from openstack_platform.management.settings import configuration

        value = json.loads((ROOT / "config/platform.example.json").read_text())
        value["paths"]["adminState"] = str(self.root / "production")
        value["domain"] = "platform.example.com"
        value["namespace"] = "auth-test"
        value["ownerPortal"] = {
            "enabled": True,
            "commonsOrigin": "https://class.example.com",
            "classLabel": "class account",
        }
        path = self.root / "platform.json"
        path.write_text(json.dumps(value))
        return configuration(load_platform(path), path)

    def test_production_address_derives_trusted_ingress_and_requires_cf_header(self) -> None:
        path = self.root / "management.json"
        value = self.production_config()
        path.write_text(json.dumps(value))
        config = Config.load(path)
        self.assertEqual(config.trusted_ingress_peers, ("192.0.2.12",))
        self.assertEqual(config.client_address_header, "cf-connecting-ip")
        for header in (None, "x-forwarded-for", "x-real-ip"):
            value["clientAddressHeader"] = header
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                Config.load(path)

    def test_rate_configuration_is_validated_and_inventory_bound(self) -> None:
        path = self.root / "management.json"
        value = self.production_config()
        inventory = Path(value["platformConfig"])
        document = json.loads(inventory.read_text())
        document["ownerPortal"].update(anonymousOptionsPerMinute=800, anonymousStartsPerMinute=500)
        inventory.write_text(json.dumps(document))
        value.update(anonymousOptionsPerMinute=800, anonymousStartsPerMinute=500)
        path.write_text(json.dumps(value))
        self.assertEqual(Config.load(path).anonymous_starts_per_minute, 500)
        value["anonymousStartsPerMinute"] = 400
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            Config.load(path)
        document["ownerPortal"]["anonymousStartsPerMinute"] = False
        inventory.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            Config.load(path)

    def test_platform_rejects_development_origins_and_obsolete_assertion_keys(self) -> None:
        from openstack_platform.config import load_platform

        value = self.production_config()
        path = Path(value["platformConfig"])
        document = json.loads(path.read_text())
        document["ownerPortal"]["commonsOrigin"] = "https://localhost:9444"
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            load_platform(path)
        document["ownerPortal"]["commonsOrigin"] = "https://class.example.com"
        document["ownerPortal"]["verificationKeys"] = {}
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            load_platform(path)

    def test_ingress_does_not_override_backend_referrer_policy(self) -> None:
        source = (ROOT / "nix/roles/ingress.nix").read_text()
        section = source.split("middlewares.platform-security-headers.headers =", 1)[1].split(
            "};", 1
        )[0]
        self.assertNotIn("referrerPolicy", section)
        self.assertIn("contentTypeNosniff = true", section)
        self.assertIn("frameDeny = true", section)

    def test_admin_management_hardening_activation_and_backup_are_declared(self) -> None:
        source = (ROOT / "nix/roles/admin.nix").read_text()
        for marker in (
            "d ${managementBrokerReleaseRoot} :2750 :${operatorAccount.name} :${managementBrokerUser}",
            "systemd-tmpfiles --create ${managementDirectories}",
            "managementBrokerReleaseRoot",
            "managementWebReleaseRoot",
            "managementBrokerConfig",
            "RequiresMountsFor",
            "management-activate",
            "PathChanged = managementActivationMarker",
            "management-broker-backup",
            "MemoryDenyWriteExecute = true",
            'IPAddressDeny = "any"',
        ):
            self.assertIn(marker, source)
        packages = (
            (ROOT / "nix/pkgs/default.nix")
            .read_text()
            .split("platformPython =", 1)[1]
            .split("controllerPackage =", 1)[0]
        )
        self.assertNotIn("ps.pyjwt", packages)
        self.assertNotIn("ps.cryptography", packages)
        self.assertIn("management-python3.14", packages)
        self.assertIn("exec ${platformPython}/bin/python", packages)
        tmpfiles = source.split("systemd.tmpfiles.rules =", 1)[1].split("]", 1)[0]
        self.assertNotIn("managementBrokerState", tmpfiles)
        self.assertNotIn("managementWebReleaseRoot", tmpfiles)
        self.assertIn(
            "python -I ${../../deploy/releases/install_release.py}",
            (ROOT / "nix/pkgs/default.nix").read_text(),
        )
        self.assertNotIn("install -d -m 2750", source)
        self.assertNotIn("chgrp ${controllerGroup}", source)
        self.assertNotIn('chmod 0640 "$path"', source)
        self.assertIn("${hostPaths} apply --plan ${packages.rootPathPlan}", source)
        self.assertIn("${hostPaths} directory-check", source)
        self.assertNotIn("environment.sessionVariables.PLATFORM_ENVIRONMENT", source)
        self.assertIn(
            "PLATFORM_MANAGEMENT_ENVIRONMENT=production",
            (ROOT / "nix/pkgs/default.nix").read_text(),
        )

    def test_identity_is_ordered_and_wanted_but_not_required_by_broker(self) -> None:
        source = (ROOT / "nix/roles/admin.nix").read_text()
        broker = source.split('systemd.services."${namespace}-management-broker" = {', 1)[1].split(
            'systemd.services."${namespace}-management-web"', 1
        )[0]
        identity = '"${namespace}-management-identity.service"'
        self.assertIn(identity, broker.split("wants = [", 1)[1].split("]", 1)[0])
        self.assertIn(identity, broker.split("after = [", 1)[1].split("]", 1)[0])
        self.assertNotIn(identity, broker.split("requires = [", 1)[1].split("]", 1)[0])

    def test_backup_uses_packaged_entrypoint_and_portal_is_independent_of_backups(self) -> None:
        source = (ROOT / "nix/roles/admin.nix").read_text()
        self.assertNotIn("PYTHONPATH=${../..}", source)
        self.assertNotIn("PYTHONPATH=${../../.}", source)
        prepare = source.split('systemd.services."${namespace}-management-prepare"', 1)[1].split(
            'systemd.services."${namespace}-management-broker"', 1
        )[0]
        self.assertNotIn("backupMountUnit", prepare)
        self.assertNotIn("backups", prepare)
        script = source.split("managementPrepare =", 1)[1].split("managementBackupPrepare =", 1)[0]
        self.assertNotIn("managementBackupRoot", script)
        backup = source.split('systemd.services."${namespace}-management-broker-backup"', 1)[
            1
        ].split("systemd.timers.", 1)[0]
        self.assertIn("backupMountUnit", backup)
        self.assertIn('ExecStartPre = "+${managementBackupPrepare}"', backup)
        self.assertIn(
            "${packages.controllerPackage}/bin/openstack-platform-management-broker-backup backup",
            backup,
        )
        vm = (ROOT / "nix/tests/default.nix").read_text()
        self.assertNotIn("PYTHONPATH=${../..}", vm)
        self.assertIn("grep -Fx 'management-broker controller-api'", vm)
