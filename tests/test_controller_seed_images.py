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
from openstack_platform.controller.api import ControllerAPI
from tests.test_platform_openstack import FakeCloud, canonical_image

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

    def test_admin_replacement_retained_seed_then_all_role_cas_then_new_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_document = json.loads((ROOT / "config/platform.example.json").read_text())
            commits = {role: ("1" if role == "admin" else "9") * 40 for role in IMAGE_ROLES}
            old_document["images"] = {
                role: f"example-{role}-{commits[role][:12]}" for role in IMAGE_ROLES
            }
            old_config_path = root / "old-guest-platform.json"
            old_config_path.write_text(json.dumps(old_document))
            old_platform = load_platform(old_config_path)
            old_ids, old_records = self.records(old_platform)
            for role in IMAGE_ROLES:
                old_records[role]["sourceCommit"] = commits[role]
            operator_seed = root / "operator-image-selections.json"
            controller_seed = root / "controller-image-selections.json"
            state = root / "retained-controller-state"
            seed_document = {
                "schemaVersion": 1,
                "projectId": old_platform.project_id,
                "namespace": old_platform.namespace,
                "images": old_records,
            }
            operator_seed.write_text(json.dumps(seed_document))
            operator_seed.chmod(0o600)

            def prepare(config_path, *, state_directory=state):
                # Match Nix prepareController: copy retained operator seed to a
                # controller-owned private file, then invoke the actual seed entrypoint.
                controller_seed.write_bytes(operator_seed.read_bytes())
                controller_seed.chmod(0o600)
                return seed_images.main(
                    [
                        "--platform-config",
                        str(config_path),
                        "--state-directory",
                        str(state_directory),
                        "--manifest",
                        str(controller_seed),
                    ]
                )

            self.assertEqual(prepare(old_config_path), 0)
            new_document = json.loads(old_config_path.read_text())
            new_document["images"] = {role: f"example-{role}-{'a' * 12}" for role in IMAGE_ROLES}
            new_config_path = root / "new-guest-platform.json"
            new_config_path.write_text(json.dumps(new_document))
            new_platform = load_platform(new_config_path)
            config = Config(
                new_platform,
                load_policy(ROOT / "config/platform-policy.example.json", require_private=False),
            )
            # The very first replacement boot needs neither the new API nor a DB edit.
            self.assertEqual(prepare(new_config_path), 0)
            self.assertEqual(prepare(old_config_path), 0)  # old inventory, not an older binary
            self.assertEqual(prepare(new_config_path, state_directory=root / "fresh-state"), 1)
            connection = db.connect(
                state / "platform.sqlite3",
                identity=db.deployment_identity(new_platform),
                check_same_thread=False,
            )
            try:
                self.assertEqual(
                    {item.role: item.image_id for item in db.list_image_selections(connection)},
                    old_ids,
                )
                new_ids = {
                    role: f"00000000-0000-4000-8000-{index + 100:012d}"
                    for index, role in enumerate(IMAGE_ROLES)
                }
                images = [
                    dict(
                        canonical_image(new_platform, new_ids[role], role=role),
                        name=new_document["images"][role],
                    )
                    for role in IMAGE_ROLES
                ]
                premature = {
                    **seed_document,
                    "images": {
                        role: {
                            **old_records[role],
                            "imageId": new_ids[role],
                            "displayName": new_document["images"][role],
                        }
                        for role in IMAGE_ROLES
                    },
                }
                operator_seed.write_text(json.dumps(premature))
                self.assertEqual(prepare(new_config_path), 1)
                self.assertEqual(
                    {item.role: item.image_id for item in db.list_image_selections(connection)},
                    old_ids,
                )
                operator_seed.write_text(json.dumps(seed_document))
                cloud = FakeCloud(new_platform, images)
                real_select = openstack.select_image
                helper = mock.Mock(
                    side_effect=AssertionError("metadata rollover cannot call helper")
                )
                api = ControllerAPI(connection, config, state, helper_caller=helper)
                try:
                    with mock.patch.object(
                        openstack,
                        "select_image",
                        side_effect=lambda *args, **kwargs: real_select(
                            *args, **kwargs, command_runner=cloud
                        ),
                    ):
                        for index, role in enumerate(IMAGE_ROLES):
                            response = api.router("privileged").dispatch(
                                "POST",
                                f"/v1/admin/images/{role}/selection",
                                {"Idempotency-Key": f"00000000-0000-4000-8000-{index + 200:012d}"},
                                {"imageId": new_ids[role], "expectedImageId": old_ids[role]},
                            )
                            api.wait_for_operations()
                            self.assertEqual(
                                api.router("privileged")
                                .dispatch("GET", response.body["statusUrl"], {}, None)
                                .body["status"],
                                "succeeded",
                            )
                            # Every partial all-role transition remains restartable
                            # against the unchanged retained seed and the new guest.
                            self.assertEqual(prepare(new_config_path), 0)
                    saved = db.list_image_selections(connection)
                    selected = (
                        api.router("privileged")
                        .dispatch("GET", "/v1/admin/images", {}, None)
                        .body["items"]
                    )
                    seed_document["images"] = {
                        item["role"]: {
                            key: item[key]
                            for key in (
                                "imageId",
                                "displayName",
                                "sourceCommit",
                                "compatibilityHash",
                            )
                        }
                        for item in selected
                    }
                    operator_seed.write_text(json.dumps(seed_document))
                    self.assertEqual(prepare(new_config_path), 0)
                    self.assertEqual(db.list_image_selections(connection), saved)
                    helper.assert_not_called()
                    self.assertTrue(
                        all(
                            call[1:3] in {("token", "issue"), ("image", "list"), ("image", "show")}
                            for call in cloud.calls
                        )
                    )
                    # A divergent manifest cannot overwrite a proven current record.
                    seed_document["images"]["admin"]["imageId"] = (
                        "00000000-0000-4000-8000-000000000999"
                    )
                    operator_seed.write_text(json.dumps(seed_document))
                    # Existing API proof wins over a different seed; the DB is never overwritten.
                    self.assertEqual(prepare(new_config_path), 0)
                    self.assertEqual(db.list_image_selections(connection), saved)
                finally:
                    api.close()
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
