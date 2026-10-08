from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openstack_platform import openstack
from openstack_platform.config import Config, PlatformConfig, load_platform, load_policy
from openstack_platform.contracts import IMAGE_ROLES
from openstack_platform.controller import database as db
from openstack_platform.controller import image_service, seed_images

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

    def test_seed_preserves_only_journal_proven_rollovers_including_interrupted_commit(
        self,
    ) -> None:
        platform_path = ROOT / "config/platform.example.json"
        platform = load_platform(platform_path)
        image_ids, records = self.records(platform)
        config = Config(
            platform,
            load_policy(ROOT / "config/platform-policy.example.json", require_private=False),
        )
        for role, interrupted in (("worker", False), ("builder", True)):
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
                    platform_config=platform_path, state_directory=state, manifest=manifest
                )
                connection = db.connect(
                    state / "platform.sqlite3", identity=db.deployment_identity(platform)
                )
                try:
                    new_id = "00000000-0000-4000-8000-000000000080"
                    selected = openstack.ImageSelection(
                        role,
                        new_id,
                        "published-role-image",
                        "b" * 40,
                        openstack.image_compatibility_hash(platform),
                    )
                    service = image_service.ImageSelectionService(connection, config, state)
                    with mock.patch.object(openstack, "select_image", return_value=selected):
                        if interrupted:
                            with mock.patch.object(
                                db, "mark_succeeded", side_effect=OSError("after selection commit")
                            ):
                                with self.assertRaises(OSError):
                                    service.select(role, new_id, image_ids[role], request_id=new_id)
                        else:
                            service.select(role, new_id, image_ids[role], request_id=new_id)
                    before = db.get_image_selection(connection, role)
                    seed_images.seed(
                        platform_config=platform_path, state_directory=state, manifest=manifest
                    )
                    self.assertEqual(db.get_image_selection(connection, role), before)
                    # A matching UUID alone cannot justify changing the saved provenance.
                    db.put_image_selection(
                        connection,
                        role=role,
                        image_id=new_id,
                        display_name=selected.display_name,
                        source_commit="d" * 40,
                        compatibility_hash=selected.compatibility_hash,
                    )
                    with self.assertRaisesRegex(seed_images.SeedFailure, "unproven"):
                        seed_images.seed(
                            platform_config=platform_path, state_directory=state, manifest=manifest
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
