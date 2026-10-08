"""Configuration and Nix hosting boundary assertions for owner integration."""

from __future__ import annotations

import json
from pathlib import Path

from openstack_platform.management.config import Config
from tests.test_management import ROOT, ManagementCase


class ManagementPlatformTests(ManagementCase):
    def production_config(self, **owner_portal: object) -> dict:
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
            **owner_portal,
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
