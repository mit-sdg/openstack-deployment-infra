from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openstack_platform import openstack
from openstack_platform.config import load_platform
from openstack_platform.contracts import IMAGE_ROLES
from openstack_platform.controller import database as db
from openstack_platform.controller import seed_images

ROOT = Path(__file__).resolve().parents[1]


class HostedImageSeedTests(unittest.TestCase):
    def test_seed_verifies_records_and_then_replays_without_provider_access(self) -> None:
        platform_path = ROOT / "config/platform.example.json"
        platform = load_platform(platform_path)
        references = {
            role: f"00000000-0000-4000-8000-{index:012d}"
            for index, role in enumerate(IMAGE_ROLES, 1)
        }
        selected = {
            role: openstack.ImageSelection(
                role=role,
                image_id=image_id,
                display_name=str(platform.get(f"images.{role}")),
                source_commit="a" * 40,
                compatibility_hash="b" * 64,
            )
            for role, image_id in references.items()
        }
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
                        "images": references,
                    }
                )
            )
            manifest.chmod(0o600)
            with mock.patch.object(
                seed_images.openstack, "select_images", return_value=selected
            ) as select:
                seed_images.seed(
                    platform_config=platform_path,
                    state_directory=state,
                    manifest=manifest,
                    openstack_command="/protected/openstack",
                    timeout_seconds=30,
                )
                seed_images.seed(
                    platform_config=platform_path,
                    state_directory=state,
                    manifest=manifest,
                    openstack_command="/protected/openstack",
                    timeout_seconds=30,
                )
            select.assert_called_once_with(
                platform,
                references,
                timeout_seconds=30,
                executable="/protected/openstack",
            )
            connection = db.connect(
                state / "platform.sqlite3", identity=db.deployment_identity(platform)
            )
            try:
                self.assertEqual(
                    {item.role: item.image_id for item in db.list_image_selections(connection)},
                    references,
                )
            finally:
                connection.close()

    def test_seed_rejects_a_different_deployment_identity(self) -> None:
        platform_path = ROOT / "config/platform.example.json"
        platform = load_platform(platform_path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "images.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "projectId": "00000000-0000-4000-8000-000000000099",
                        "namespace": platform.namespace,
                        "images": {
                            role: f"00000000-0000-4000-8000-{index:012d}"
                            for index, role in enumerate(IMAGE_ROLES, 1)
                        },
                    }
                )
            )
            manifest.chmod(0o600)
            with self.assertRaisesRegex(seed_images.SeedFailure, "identity"):
                seed_images.seed(
                    platform_config=platform_path,
                    state_directory=root / "state",
                    manifest=manifest,
                    openstack_command="/protected/openstack",
                    timeout_seconds=30,
                )


if __name__ == "__main__":
    unittest.main()
