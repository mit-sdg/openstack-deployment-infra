from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openstack_platform import openstack
from openstack_platform.config import PlatformConfig, load_platform
from openstack_platform.contracts import IMAGE_ROLES
from openstack_platform.controller import database as db
from openstack_platform.controller import seed_images

ROOT = Path(__file__).resolve().parents[1]


class HostedImageSeedTests(unittest.TestCase):
    @staticmethod
    def records(platform: PlatformConfig) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        image_ids = {
            role: f"00000000-0000-4000-8000-{index:012d}"
            for index, role in enumerate(IMAGE_ROLES, 1)
        }
        compatibility = openstack.image_compatibility_hash(platform)
        records = {
            role: {
                "imageId": image_id,
                "displayName": str(platform.get(f"images.{role}")),
                "sourceCommit": "a" * 40,
                "compatibilityHash": compatibility,
            }
            for role, image_id in image_ids.items()
        }
        return image_ids, records

    def test_seed_records_all_roles_and_is_idempotent(self) -> None:
        platform_path = ROOT / "config/platform.example.json"
        platform = load_platform(platform_path)
        image_ids, records = self.records(platform)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            manifest = root / "images.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "projectId": platform.project_id,
                        "namespace": platform.namespace,
                        "images": records,
                    }
                )
            )
            manifest.chmod(0o600)
            seed_images.seed(
                platform_config=platform_path,
                state_directory=state,
                manifest=manifest,
            )
            seed_images.seed(
                platform_config=platform_path,
                state_directory=state,
                manifest=manifest,
            )
            connection = db.connect(
                state / "platform.sqlite3", identity=db.deployment_identity(platform)
            )
            try:
                self.assertEqual(
                    {item.role: item.image_id for item in db.list_image_selections(connection)},
                    image_ids,
                )
            finally:
                connection.close()

    def test_seed_rejects_a_different_deployment_identity(self) -> None:
        platform_path = ROOT / "config/platform.example.json"
        platform = load_platform(platform_path)
        _image_ids, records = self.records(platform)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "images.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "projectId": "00000000-0000-4000-8000-000000000099",
                        "namespace": platform.namespace,
                        "images": records,
                    }
                )
            )
            manifest.chmod(0o600)
            with self.assertRaisesRegex(seed_images.SeedFailure, "identity"):
                seed_images.seed(
                    platform_config=platform_path,
                    state_directory=root / "state",
                    manifest=manifest,
                )


if __name__ == "__main__":
    unittest.main()
