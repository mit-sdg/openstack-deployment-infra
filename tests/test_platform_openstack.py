from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from platform_cli import openstack
from platform_cli.config import load_platform
from platform_cli.runtime import CommandFailure, CommandResult, HttpResult
from platform_cli.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "00000000-0000-4000-8000-000000000000"
IMAGE_1 = "11111111-1111-4111-8111-111111111111"
IMAGE_2 = "22222222-2222-4222-8222-222222222222"
IMAGE_3 = "33333333-3333-4333-8333-333333333333"
REVIEW_IMAGE = "44444444-4444-4444-8444-444444444444"
SERVER = "55555555-5555-4555-8555-555555555555"
REPLACEMENT = "66666666-6666-4666-8666-666666666666"
PORT = "77777777-7777-4777-8777-777777777777"
FLAVOR = "88888888-8888-4888-8888-888888888888"
OPERATION = "99999999-9999-4999-8999-999999999999"
OLD_IMAGE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
VOLUME = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
VOLUME_2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PROVIDER_UUID_FIXTURE = json.loads(
    (ROOT / "tests/fixtures/openstack/provider_uuid_outputs.json").read_text()
)


@contextmanager
def protected_user_data(value: bytes = b"private cloud-init"):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "user-data"
        path.write_bytes(value)
        path.chmod(0o600)
        yield path


def result(argv: tuple[str, ...], value: object = None, *, returncode: int = 0) -> CommandResult:
    if isinstance(value, bytes):
        output = value
    elif value is None:
        output = b""
    else:
        output = json.dumps(value).encode()
    return CommandResult(argv, returncode, output, b"", False, False)


class FakeCloud:
    def __init__(
        self, platform, images: list[dict] | None = None, *, role: str = "ingress"
    ) -> None:
        self.platform = platform
        self.role = role
        self.images = {item["id"]: item for item in (images or [])}
        self.calls: list[tuple[str, ...]] = []
        self.server = {
            "id": SERVER,
            "name": platform.get(f"hosts.{role}"),
            "status": "ACTIVE",
            "image": {"id": OLD_IMAGE},
            "flavor": {"id": FLAVOR, "original_name": "example.2c2g"},
            "addresses": {"example-network": [platform.get(f"addresses.{role}")]},
        }
        self.replacement: dict | None = None
        self.port_device = SERVER
        self.user_data_seen = False
        self.user_data_payload = b""
        self.user_data_path: Path | None = None
        self.ready_markers = {SERVER: 1}
        self.failed_markers: dict[str, int] = {}
        self.ambiguous_create = False
        self.retain_old_delete = False
        # Real power-off is asynchronous: the command returns before the server
        # reaches SHUTOFF. 0 keeps the historical synchronous behaviour.
        self.stop_settle_reads = 0
        self.stop_never_settles = False
        self._pending_stop_reads = 0
        self.start_calls: list[str] = []
        self.volume_attachments = (
            [
                {
                    "ID": VOLUME,
                    "Device": "/dev/vdb",
                    "Delete On Termination": False,
                    "server_id": SERVER,
                    "name": platform.get("volumes.adminState.name"),
                },
                {
                    "ID": VOLUME_2,
                    "Device": "/dev/vdc",
                    "Delete On Termination": False,
                    "server_id": SERVER,
                    "name": platform.get("volumes.backup.name"),
                },
            ]
            if role == "admin"
            else []
        )
        self.server["volumes_attached"] = [
            {
                "id": item["ID"],
                "delete_on_termination": item["Delete On Termination"],
            }
            for item in self.volume_attachments
        ]

    def image_document(self, image_id: str) -> dict:
        image = self.images[image_id]
        return {
            "id": image_id,
            "name": image["name"],
            "status": image.get("status", "active"),
            "created_at": image["created_at"],
            "owner": image.get("owner", self.platform.project_id),
            "properties": image.get("properties", {}),
        }

    def __call__(self, argv, **kwargs):
        argv = tuple(argv)
        self.calls.append(argv)
        self.assert_safe_call(argv, kwargs)
        args = argv[1:]
        if args[:2] == ("token", "issue"):
            return result(argv, {"project_id": self.platform.project_id})
        if args[:2] == ("project", "show"):
            return result(
                argv, {"id": self.platform.project_id, "name": self.platform.project_name}
            )
        if args[:2] == ("image", "list"):
            rows = [
                {
                    "ID": image_id,
                    "Name": image["name"],
                    "Status": image.get("status", "active"),
                    **(
                        {
                            "Created At": image["created_at"],
                            "Project": image.get("owner", self.platform.project_id),
                            "Properties": image.get("properties", {}),
                        }
                        if "--long" in args
                        else {}
                    ),
                }
                for image_id, image in self.images.items()
            ]
            return result(argv, rows)
        if args[:2] == ("image", "show"):
            image_id = args[2]
            if image_id not in self.images:
                return result(argv, returncode=1)
            return result(argv, self.image_document(image_id))
        if args[:2] == ("image", "delete"):
            self.images.pop(args[2], None)
            return result(argv)
        if args[:2] == ("server", "list"):
            rows = []
            name = args[args.index("--name") + 1] if "--name" in args else None
            for server in (self.server, self.replacement):
                if server is not None and (name is None or server["name"] == name):
                    row = {"ID": server["id"], "Name": server["name"], "Status": server["status"]}
                    if "Image" in args:
                        row["Image"] = server["image"]
                    rows.append(row)
            return result(argv, rows)
        if args[:2] == ("server", "show"):
            server_id = args[2]
            if (
                self._pending_stop_reads
                and self.server is not None
                and self.server["id"] == server_id
            ):
                # Report the server as still running until it has settled.
                self._pending_stop_reads -= 1
                if self._pending_stop_reads == 0:
                    self.server["status"] = "SHUTOFF"
            for server in (self.server, self.replacement):
                if server is not None and server["id"] == server_id:
                    return result(argv, server)
            return result(argv, returncode=1)
        if args[:2] == ("port", "list"):
            if "--server" in args:
                server_id = args[args.index("--server") + 1]
                rows = [{"ID": PORT}] if self.port_device == server_id else []
                return result(argv, rows)
            return result(argv, [{"ID": PORT, "Name": self.platform.get(f"ports.{self.role}")}])
        if args[:2] == ("port", "show"):
            return result(
                argv,
                {
                    "id": PORT,
                    "name": self.platform.get(f"ports.{self.role}"),
                    "device_id": self.port_device,
                    "fixed_ips": [{"ip_address": self.platform.get(f"addresses.{self.role}")}],
                },
            )
        if args[:2] == ("volume", "list"):
            name = args[args.index("--name") + 1]
            rows = [
                {"ID": item["ID"], "Name": item["name"]}
                for item in self.volume_attachments
                if item["name"] == name
            ]
            return result(argv, rows)
        if args[:3] == ("server", "volume", "list"):
            server_id = args[3]
            rows = [
                {"ID": item["ID"], "Device": item["Device"]}
                for item in self.volume_attachments
                if item["server_id"] == server_id
            ]
            return result(argv, rows)
        if args[:2] == ("server", "stop"):
            if self.stop_never_settles:
                return result(argv)
            if self.stop_settle_reads:
                self._pending_stop_reads = self.stop_settle_reads
            else:
                self.server["status"] = "SHUTOFF"
            return result(argv)
        if args[:2] == ("server", "start"):
            self.start_calls.append(args[2])
            self.server["status"] = "ACTIVE"
            self.ready_markers[args[2]] = self.ready_markers.get(args[2], 0) + 1
            return result(argv)
        if args[:2] == ("server", "reboot"):
            self.server["status"] = "ACTIVE"
            self.ready_markers[args[2]] = self.ready_markers.get(args[2], 0) + 1
            return result(argv)
        if args[:2] == ("server", "set"):
            name = args[args.index("--name") + 1]
            server_id = args[-1]
            target = self.server if self.server["id"] == server_id else self.replacement
            assert target is not None
            target["name"] = name
            return result(argv)
        if args[:3] == ("server", "remove", "port"):
            self.port_device = ""
            return result(argv)
        if args[:3] == ("server", "add", "port"):
            self.port_device = args[3]
            return result(argv)
        if args[:3] == ("server", "remove", "volume"):
            for item in self.volume_attachments:
                if item["ID"] == args[4] and item["server_id"] == args[3]:
                    item["server_id"] = ""
            return result(argv)
        if args[:3] == ("server", "add", "volume"):
            server_id, volume_id = args[-2:]
            for item in self.volume_attachments:
                if item["ID"] == volume_id:
                    item["server_id"] = server_id
            return result(argv)
        if args[:2] == ("server", "create"):
            assert args[args.index("--key-name") + 1] == f"{self.platform.prefix}-admin"
            path = Path(args[args.index("--user-data") + 1])
            self.user_data_payload = path.read_bytes()
            self.user_data_path = path
            self.user_data_seen = self.user_data_payload == b"private cloud-init"
            self.assertEqualMode(path)
            name = args[-1]
            properties = {
                item.split("=", 1)[0]: item.split("=", 1)[1]
                for index, item in enumerate(args)
                if index and args[index - 1] == "--property" and "=" in item
            }
            self.replacement = {
                "id": REPLACEMENT,
                "name": name,
                "status": "ACTIVE",
                "image": {"id": IMAGE_1},
                "flavor": {"id": FLAVOR, "original_name": "example.2c2g"},
                "addresses": {"example-network": [self.platform.get(f"addresses.{self.role}")]},
                "properties": properties,
                "volumes_attached": [
                    {
                        "id": item["ID"],
                        "delete_on_termination": item["Delete On Termination"],
                    }
                    for item in self.volume_attachments
                ],
            }
            self.port_device = REPLACEMENT
            for item in self.volume_attachments:
                item["server_id"] = REPLACEMENT
            self.ready_markers[REPLACEMENT] = 1
            if self.ambiguous_create:
                return result(argv, b"not-json")
            return result(argv, {"id": REPLACEMENT})
        if args[:4] == ("console", "log", "show", "--lines"):
            server_id = args[-1]
            marker = f"{self.platform.namespace} NixOS {self.role} services ready\n"
            failed = f"{self.platform.namespace} NixOS {self.role} readiness failed\n"
            output = marker * self.ready_markers.get(server_id, 0)
            output += failed * self.failed_markers.get(server_id, 0)
            return result(argv, output.encode())
        if args[:2] == ("server", "delete"):
            server_id = args[2]
            if self.replacement is not None and self.replacement["id"] == server_id:
                self.replacement = None
                self.port_device = ""
                for item in self.volume_attachments:
                    if item["server_id"] == server_id:
                        item["server_id"] = ""
            elif self.server["id"] == server_id and not self.retain_old_delete:
                self.server = None  # type: ignore[assignment]
            return result(argv)
        if args[:2] == ("flavor", "show"):
            return result(argv, {"id": FLAVOR, "name": "example.1c2g", "vcpus": 1, "ram": 2048})
        raise AssertionError(f"unexpected fake OpenStack call: {argv}")

    def assert_safe_call(self, argv: tuple[str, ...], kwargs: dict) -> None:
        assert kwargs["timeout_seconds"] > 0
        assert kwargs["stdout_limit"] in {32_768, 1_048_576}
        assert "OS_PASSWORD" in kwargs["inherit_env"]
        assert "private cloud-init" not in " ".join(argv)

    @staticmethod
    def assertEqualMode(path: Path) -> None:
        assert path.stat().st_mode & 0o777 == 0o600


class CompactProviderUUIDCloud(FakeCloud):
    """Replay compact UUID projections and require their raw project lookup token."""

    @classmethod
    def _compact_provider_ids(cls, value: object, *, field: str | None = None) -> object:
        if isinstance(value, list):
            return [cls._compact_provider_ids(item) for item in value]
        if isinstance(value, dict):
            return {
                key: cls._compact_provider_ids(item, field=key.lower().replace(" ", "_"))
                for key, item in value.items()
            }
        if (
            isinstance(value, str)
            and field in {"id", "owner", "owner_id", "project_id", "device_id"}
            and len(value) == 36
            and value.count("-") == 4
        ):
            return value.replace("-", "")
        return value

    def __call__(self, argv, **kwargs):
        argv = tuple(argv)
        if argv[1:3] == ("project", "show"):
            compact_project_id = self.platform.project_id.replace("-", "")
            if argv[3] != compact_project_id:
                self.calls.append(argv)
                self.assert_safe_call(argv, kwargs)
                raise CommandFailure(
                    "fake provider accepts only its exact compact project lookup token",
                    result(argv, returncode=1),
                )
        completed = super().__call__(argv, **kwargs)
        try:
            document = json.loads(completed.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return completed
        return result(
            tuple(argv),
            self._compact_provider_ids(document),
            returncode=completed.returncode,
        )


def canonical_image(
    platform, image_id: str, *, role: str = "worker", created: str = "2026-01-01T00:00:00Z"
) -> dict:
    return {
        "id": image_id,
        "name": f"example-{role}-{image_id[:4]}",
        "created_at": created,
        "properties": dict(openstack.publisher_metadata(platform, role, "a" * 40)),
    }


class OpenStackTests(unittest.TestCase):
    @staticmethod
    def role_health(_role: str, _host: openstack.PersistentHost, remaining: float) -> None:
        if remaining <= 0:
            raise AssertionError("role health check must receive a positive deadline")

    @classmethod
    def setUpClass(cls) -> None:
        cls.platform = load_platform(ROOT / "config/platform.example.json")

    def test_openstack_deadline_is_absolute_and_refuses_a_late_phase(self) -> None:
        current = [100.0]
        timeouts: list[float] = []

        def clock() -> float:
            return current[0]

        def runner(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            timeouts.append(float(kwargs["timeout_seconds"]))
            if len(timeouts) == 1:
                current[0] = 106.0
                return result(argv, {"project_id": self.platform.project_id})
            raise AssertionError("the expired second phase must not invoke OpenStack")

        with self.assertRaisesRegex(openstack.OpenStackError, "deadline"):
            openstack.verify_project(
                self.platform,
                timeout_seconds=5,
                command_runner=runner,
                clock=clock,
            )
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0], 5.0)

    def test_openstack_deadline_is_carried_as_remaining_time_between_commands(self) -> None:
        current = [200.0]
        timeouts: list[float] = []

        def clock() -> float:
            return current[0]

        def runner(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            timeouts.append(float(kwargs["timeout_seconds"]))
            if len(timeouts) == 1:
                current[0] += 3.5
                return result(argv, {"project_id": self.platform.project_id})
            return result(
                argv, {"id": self.platform.project_id, "name": self.platform.project_name}
            )

        identity = openstack.verify_project(
            self.platform,
            timeout_seconds=5,
            command_runner=runner,
            clock=clock,
        )
        self.assertEqual(identity.project_id, self.platform.project_id)
        self.assertEqual(timeouts, [5.0, 1.5])

    def test_publisher_script_emits_canonical_metadata_and_verifies_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "openstack"
            log = root / "create.json"
            image = root / "image.qcow2"
            image.write_bytes(b"qcow")
            fake.write_text(
                """#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] == ["token", "issue"]:
    print("00000000000040008000000000000000")
elif args[:2] == ["project", "show"]:
    print("00000000000040008000000000000000")
    print("example-project")
elif args[:2] == ["image", "list"]:
    print("[]")
elif args[:2] == ["image", "show"]:
    created = json.loads(pathlib.Path(os.environ["FAKE_LOG"]).read_text())
    properties = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for index, item in enumerate(created)
        if index and created[index - 1] == "--property" and "=" in item
    }
    image_file = created[created.index("--file") + 1]
    digest = hashlib.md5(pathlib.Path(image_file).read_bytes(), usedforsecurity=False).hexdigest()
    print(json.dumps({
        "id": "11111111-1111-4111-8111-111111111111",
        "name": "example-nixos-worker",
        "status": "active",
        "owner": "00000000000040008000000000000000",
        "checksum": digest,
        "properties": properties,
    }))
elif args[:2] == ["image", "create"]:
    pathlib.Path(os.environ["FAKE_LOG"]).write_text(json.dumps(args))
    print("11111111-1111-4111-8111-111111111111")
else:
    raise SystemExit(2)
"""
            )
            fake.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "OSC": str(fake),
                    "PLATFORM_CONFIG": str(ROOT / "config/platform.example.json"),
                    "SOURCE_COMMIT": "a" * 40,
                    "FAKE_LOG": str(log),
                }
            )
            completed = subprocess.run(
                [str(ROOT / "infra/openstack/publish_nixos_image.sh"), "worker", str(image)],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            expected_checksum = hashlib.md5(image.read_bytes(), usedforsecurity=False).hexdigest()
            self.assertIn(f"checksum={expected_checksum}", completed.stdout.decode())
            create = json.loads(log.read_text())
            properties = [
                create[index + 1] for index, item in enumerate(create) if item == "--property"
            ]
            expected = openstack.publisher_metadata(self.platform, "worker", "a" * 40)
            for key, value in expected.items():
                self.assertIn(f"{key}={value}", properties)
            self.assertIn("hw_qemu_guest_agent=yes", properties)

    def test_publisher_and_selector_share_complete_stable_metadata(self) -> None:
        metadata = openstack.publisher_metadata(self.platform, "worker", "a" * 40)
        self.assertEqual(metadata["app_platform_project_id"], PROJECT)
        self.assertEqual(metadata["app_platform_metadata_version"], "1")
        self.assertEqual(len(metadata["app_platform_compatibility_sha256"]), 64)

        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1)])
        selected = openstack.select_image(self.platform, "worker", IMAGE_1, command_runner=cloud)
        self.assertEqual(selected.image_id, IMAGE_1)
        self.assertTrue(
            all("--column" in call or call[1:3] == ("image", "list") for call in cloud.calls[2:])
        )

        document = json.loads((ROOT / "config/platform.example.json").read_text())
        document["images"]["worker"] = "a-derived-publication-name"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.json"
            path.write_text(json.dumps(document))
            changed_names = load_platform(path)
        self.assertEqual(
            openstack.image_compatibility_hash(changed_names),
            openstack.image_compatibility_hash(self.platform),
        )

    def test_compact_provider_uuids_are_normalized_at_openstack_boundaries(self) -> None:
        fixture = PROVIDER_UUID_FIXTURE
        self.assertEqual(fixture["project_id"], "7a3c91d24b8e42f09c156de0f28a15b3")
        self.assertEqual(fixture["project_uuid"], "7a3c91d2-4b8e-42f0-9c15-6de0f28a15b3")
        self.assertEqual(fixture["server_id"], SERVER.replace("-", ""))
        self.assertEqual(fixture["glance_image_id"], IMAGE_1.replace("-", ""))
        self.assertEqual(fixture["server_image_id"], OLD_IMAGE.replace("-", ""))
        self.assertEqual(fixture["port_id"], PORT.replace("-", ""))
        self.assertEqual(
            fixture["volume_ids"], [VOLUME.replace("-", ""), VOLUME_2.replace("-", "")]
        )

        platform = replace(self.platform, project_id=fixture["project_uuid"])
        cloud = CompactProviderUUIDCloud(
            platform,
            [canonical_image(platform, IMAGE_1, role="admin")],
            role="admin",
        )
        identity = openstack.verify_project(platform, command_runner=cloud)
        images = openstack.list_images(platform, command_runner=cloud)
        resources = openstack.observe_host_resources(platform, "admin", command_runner=cloud)

        self.assertEqual(identity.project_id, fixture["project_uuid"])
        project_lookups = [call[3] for call in cloud.calls if call[1:3] == ("project", "show")]
        self.assertEqual(project_lookups, [fixture["project_id"]] * 3)
        self.assertNotIn(fixture["project_uuid"], project_lookups)
        self.assertEqual(images[0].image_id, IMAGE_1)
        self.assertEqual(images[0].owner_id, fixture["project_uuid"])
        self.assertEqual(resources.host.server_id, SERVER)
        self.assertEqual(resources.host.image_id, OLD_IMAGE)
        self.assertEqual(resources.port_id, PORT)
        self.assertEqual([volume.volume_id for volume in resources.volumes], [VOLUME, VOLUME_2])

    def test_malformed_provider_uuid_is_rejected_without_weakening_config_inputs(self) -> None:
        class MalformedProjectCloud(FakeCloud):
            def __init__(self, *args, malformed_command: tuple[str, str], **kwargs):
                super().__init__(*args, **kwargs)
                self.malformed_command = malformed_command

            def __call__(self, argv, **kwargs):
                completed = super().__call__(argv, **kwargs)
                if tuple(argv)[1:3] != self.malformed_command:
                    return completed
                document = json.loads(completed.stdout)
                field = "project_id" if self.malformed_command == ("token", "issue") else "id"
                document[field] = "7A3C91D24B8E42F09C156DE0F28A15B3"
                return result(tuple(argv), document, returncode=completed.returncode)

        for malformed_command in (("token", "issue"), ("project", "show")):
            cloud = MalformedProjectCloud(self.platform, malformed_command=malformed_command)
            with (
                self.subTest(malformed_command=malformed_command),
                self.assertRaisesRegex(openstack.OpenStackError, "malformed project UUID"),
            ):
                openstack.verify_project(self.platform, command_runner=cloud)
            if malformed_command == ("token", "issue"):
                self.assertFalse(any(call[1:3] == ("project", "show") for call in cloud.calls))

        class MalformedResourceCloud(FakeCloud):
            def __init__(self, *args, target: tuple[str, ...], **kwargs):
                super().__init__(*args, **kwargs)
                self.target = target

            def __call__(self, argv, **kwargs):
                completed = super().__call__(argv, **kwargs)
                if tuple(argv)[1 : 1 + len(self.target)] != self.target:
                    return completed
                document = json.loads(completed.stdout)
                document[0]["ID"] = "1111111111114111811111111111111g"
                return result(tuple(argv), document, returncode=completed.returncode)

        for target, expected_error, operation in (
            (("image", "list"), "image UUID", "images"),
            (("server", "list"), "server UUID", "resources"),
            (("port", "list"), "port UUID", "resources"),
            (("server", "volume", "list"), "volume UUID", "resources"),
        ):
            cloud = MalformedResourceCloud(
                self.platform,
                [canonical_image(self.platform, IMAGE_1, role="admin")],
                role="admin",
                target=target,
            )
            with (
                self.subTest(target=target),
                self.assertRaisesRegex(openstack.OpenStackError, f"malformed {expected_error}"),
            ):
                if operation == "images":
                    openstack.list_images(self.platform, command_runner=cloud)
                else:
                    openstack.observe_host_resources(self.platform, "admin", command_runner=cloud)

        document = json.loads((ROOT / "config/platform.example.json").read_text())
        document["projectId"] = PROVIDER_UUID_FIXTURE["project_id"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.json"
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValidationError, "canonical lowercase UUID"):
                load_platform(path)

    def test_long_inventory_handles_25_images_without_per_image_shows(self) -> None:
        images = [
            canonical_image(
                self.platform,
                f"10000000-0000-4000-8000-{index:012d}",
                created=f"2026-01-{index:02d}T00:00:00Z",
            )
            for index in range(1, 26)
        ]
        images.append(
            {
                "id": REVIEW_IMAGE,
                "name": "unrelated-private-image",
                "created_at": "2025-01-01T00:00:00Z",
                "properties": {},
            }
        )

        class SlowShowCloud(FakeCloud):
            def __call__(self, argv, **kwargs):
                if tuple(argv)[1:3] == ("image", "show"):
                    time.sleep(0.05)
                return super().__call__(argv, **kwargs)

        cloud = SlowShowCloud(self.platform, images)
        started = time.monotonic()
        observed = openstack.list_images(self.platform, command_runner=cloud)
        elapsed = time.monotonic() - started

        self.assertEqual(len(observed), 25)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(len([call for call in cloud.calls if call[1:3] == ("image", "list")]), 1)
        self.assertFalse(any(call[1:3] == ("image", "show") for call in cloud.calls))
        self.assertNotIn("unrelated-private-image", {image.name for image in observed})

    def test_older_long_inventory_uses_fixed_width_detail_batches(self) -> None:
        images = [
            canonical_image(
                self.platform,
                f"20000000-0000-4000-8000-{index:012d}",
                created=f"2026-01-{index:02d}T00:00:00Z",
            )
            for index in range(1, 25)
        ]

        class OlderSlowCloud(FakeCloud):
            detail_output_limits: list[int] = []

            def __call__(self, argv, **kwargs):
                completed = super().__call__(argv, **kwargs)
                args = tuple(argv)[1:]
                if args[:2] == ("image", "list") and "--long" in args:
                    rows = json.loads(completed.stdout)
                    for row in rows:
                        row.pop("Created At")
                        row.pop("Properties")
                    return result(tuple(argv), rows)
                if args[:2] == ("image", "show"):
                    self.detail_output_limits.append(kwargs["stdout_limit"])
                    time.sleep(0.05)
                return completed

        cloud = OlderSlowCloud(self.platform, images)
        started = time.monotonic()
        observed = openstack.list_images(self.platform, command_runner=cloud)
        elapsed = time.monotonic() - started

        self.assertEqual(len(observed), 24)
        self.assertLess(elapsed, 0.8, "detail reads regressed to serial list-plus-N latency")
        self.assertEqual(len([call for call in cloud.calls if call[1:3] == ("image", "show")]), 24)
        self.assertEqual(set(cloud.detail_output_limits), {32_768})

    def test_project_uuid_mismatch_stops_before_inventory_and_malformed_metadata_is_safe(
        self,
    ) -> None:
        cloud = FakeCloud(self.platform)
        wrong = replace(self.platform, project_id="ffffffff-ffff-4fff-8fff-ffffffffffff")
        with self.assertRaisesRegex(openstack.OpenStackError, "project UUID"):
            openstack.list_images(wrong, command_runner=cloud)
        self.assertFalse(any(call[1:3] == ("image", "list") for call in cloud.calls))

        malformed = canonical_image(self.platform, IMAGE_1)
        malformed["properties"]["app_platform_role"] = "sentinel-provider-secret"
        observed = openstack.list_images(
            self.platform, command_runner=FakeCloud(self.platform, [malformed])
        )[0]
        self.assertNotIn("sentinel-provider-secret", repr(observed))
        self.assertEqual(observed.role, "<incompatible>")

    def test_malformed_properties_are_never_selected(self) -> None:
        for properties in (
            "app_platform_role='worker'",
            ["app_platform_role=worker"],
            17,
        ):
            malformed = {
                "id": IMAGE_1,
                "name": self.platform.get("images.worker"),
                "created_at": "2026-01-01T00:00:00Z",
                "properties": properties,
            }
            cloud = FakeCloud(self.platform, [malformed])
            with (
                self.subTest(properties_type=type(properties).__name__),
                self.assertRaisesRegex(openstack.OpenStackError, "incompatible"),
            ):
                openstack.select_image(
                    self.platform,
                    "worker",
                    malformed["name"],
                    command_runner=cloud,
                )
            observed = openstack.list_images(
                self.platform, command_runner=FakeCloud(self.platform, [malformed])
            )
            self.assertEqual(len(observed), 1)
            self.assertTrue(observed[0].platform_metadata_present)
            self.assertIsNone(observed[0].role)

    def test_prune_rejects_malformed_server_images_but_accepts_explicit_volume_boot(self) -> None:
        malformed_values = (
            "not-an-image",
            "image (11111111-1111-4111-8111-111111111111) (22222222-2222-4222-8222-222222222222)",
            {"id": ""},
            {"name": "missing-id"},
        )
        for value in malformed_values:
            cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1)])
            cloud.server["image"] = value
            with self.subTest(value=value), self.assertRaises(openstack.OpenStackError):
                openstack.plan_image_prune(
                    self.platform,
                    selected_image_ids=[],
                    retain_newest=1,
                    command_runner=cloud,
                )

        volume_boot = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1)])
        volume_boot.server["image"] = None
        plan = openstack.plan_image_prune(
            self.platform,
            selected_image_ids=[],
            retain_newest=1,
            command_runner=volume_boot,
        )
        self.assertEqual(plan.server_image_ids, ())

    def test_prune_protects_selected_server_newest_and_reports_malformed(self) -> None:
        images = [
            canonical_image(self.platform, IMAGE_1, created="2026-03-01T00:00:00Z"),
            canonical_image(self.platform, IMAGE_2, created="2026-02-01T00:00:00Z"),
            canonical_image(self.platform, IMAGE_3, created="2026-01-01T00:00:00Z"),
            {
                "id": REVIEW_IMAGE,
                "name": "platform-looking-bad",
                "created_at": "2026-01-01T00:00:00Z",
                "properties": {
                    "app_platform_managed_by": "platform",
                    "app_platform_role": "worker",
                },
            },
        ]
        cloud = FakeCloud(self.platform, images)
        plan = openstack.plan_image_prune(
            self.platform,
            selected_image_ids=[IMAGE_1],
            operation_image_ids=[IMAGE_2],
            retain_newest=1,
            command_runner=cloud,
        )
        self.assertEqual(plan.image_ids, (IMAGE_3,))
        self.assertIn(REVIEW_IMAGE, plan.review_image_ids)
        self.assertEqual(len(plan.drift_hash), 64)
        self.assertEqual(plan.operation_refs()["image_ids"], [IMAGE_3])

        checkpoints: list[tuple[str, dict]] = []
        applied = openstack.apply_image_prune(
            self.platform,
            plan,
            selected_image_ids=[IMAGE_1],
            operation_image_ids=[IMAGE_2],
            checkpoint=lambda phase, refs: checkpoints.append((phase, dict(refs))),
            command_runner=cloud,
        )
        self.assertEqual(applied.deleted_image_ids, (IMAGE_3,))
        delete_calls = [call for call in cloud.calls if call[1:3] == ("image", "delete")]
        self.assertEqual(delete_calls, [("openstack", "image", "delete", IMAGE_3)])
        self.assertEqual(checkpoints[-1][0], "image_deleted")

    def test_partial_or_malformed_metadata_never_selects_or_prunes(self) -> None:
        partial = canonical_image(self.platform, IMAGE_1)
        del partial["properties"]["app_platform_metadata_version"]
        malformed_time = canonical_image(self.platform, IMAGE_2)
        malformed_time["created_at"] = "not-a-time"
        cloud = FakeCloud(self.platform, [partial, malformed_time])
        with self.assertRaisesRegex(openstack.OpenStackError, "incompatible"):
            openstack.select_image(self.platform, "worker", IMAGE_1, command_runner=cloud)
        plan = openstack.plan_image_prune(
            self.platform, selected_image_ids=[], retain_newest=1, command_runner=cloud
        )
        self.assertEqual(plan.image_ids, ())
        self.assertEqual(set(plan.review_image_ids), {IMAGE_1, IMAGE_2})

    def test_prune_apply_refuses_drift_before_deletion(self) -> None:
        cloud = FakeCloud(
            self.platform,
            [
                canonical_image(self.platform, IMAGE_1, created="2026-02-01T00:00:00Z"),
                canonical_image(self.platform, IMAGE_2, created="2026-01-01T00:00:00Z"),
            ],
        )
        plan = openstack.plan_image_prune(
            self.platform, selected_image_ids=[IMAGE_1], retain_newest=1, command_runner=cloud
        )
        cloud.images[IMAGE_3] = canonical_image(
            self.platform, IMAGE_3, created="2026-03-01T00:00:00Z"
        )
        with self.assertRaises(openstack.DriftError):
            openstack.apply_image_prune(
                self.platform,
                plan,
                selected_image_ids=[IMAGE_1],
                checkpoint=lambda *_: None,
                command_runner=cloud,
            )
        self.assertFalse(any(call[1:3] == ("image", "delete") for call in cloud.calls))

    def test_flavor_observation_enforces_one_vcpu(self) -> None:
        cloud = FakeCloud(self.platform)
        flavor_name = openstack.observe_flavor(
            self.platform, "example.1c2g", require_one_vcpu=True, command_runner=cloud
        )
        self.assertEqual(flavor_name, "example.1c2g")

    def test_power_uses_selected_server_uuid_and_requires_health(self) -> None:
        cloud = FakeCloud(self.platform)
        checked: list[tuple[str, str]] = []
        powered = openstack.power_host(
            self.platform,
            "ingress",
            "reboot",
            health_check=lambda role, host, remaining: checked.append((role, host.server_id or "")),
            command_runner=cloud,
        )
        self.assertEqual(powered.server_id, SERVER)
        self.assertEqual(checked, [("ingress", SERVER)])
        self.assertIn(("openstack", "server", "reboot", SERVER), cloud.calls)
        started = openstack.power_host(
            self.platform,
            "ingress",
            "start",
            health_check=self.role_health,
            command_runner=cloud,
        )
        self.assertEqual(started.status, "ACTIVE")

    def test_admin_reboot_readiness_is_independent_of_admin_helper(self) -> None:
        cloud = FakeCloud(self.platform, role="admin")
        powered = openstack.power_host(
            self.platform,
            "admin",
            "reboot",
            health_check=self.role_health,
            command_runner=cloud,
        )
        self.assertEqual(powered.status, "ACTIVE")
        self.assertEqual(
            [call for call in cloud.calls if call[1:3] == ("server", "reboot")],
            [("openstack", "server", "reboot", SERVER)],
        )
        self.assertTrue(all(call[0] == "openstack" for call in cloud.calls))

    def test_reboot_recovery_observes_saved_action_without_a_second_reboot(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        cloud = FakeCloud(self.platform)
        durable_refs: dict[str, object] | None = None

        def checkpoint(phase: str, refs: object) -> None:
            nonlocal durable_refs
            assert isinstance(refs, dict)
            if phase == "power_requested":
                durable_refs = dict(refs)
                raise SimulatedCrash

        with self.assertRaises(SimulatedCrash):
            openstack.power_host(
                self.platform,
                "ingress",
                "reboot",
                checkpoint=checkpoint,
                health_check=self.role_health,
                command_runner=cloud,
            )
        assert durable_refs is not None
        recovered = openstack.recover_power_host(
            self.platform,
            "ingress",
            "reboot",
            refs=durable_refs,
            health_check=self.role_health,
            command_runner=cloud,
        )
        self.assertEqual(recovered.status, "ACTIVE")
        self.assertEqual(
            [call for call in cloud.calls if call[1:3] == ("server", "reboot")],
            [("openstack", "server", "reboot", SERVER)],
        )

    def test_admin_replacement_preserves_exact_volume_ids_devices_and_delete_policy(self) -> None:
        cloud = FakeCloud(
            self.platform,
            [canonical_image(self.platform, IMAGE_1, role="admin")],
            role="admin",
        )
        with (
            protected_user_data() as user_data_path,
            mock.patch.object(openstack.host_keys, "pin_verified_admin_host_key") as pin,
        ):
            replaced = openstack.replace_host(
                self.platform,
                "admin",
                selected_image_id=IMAGE_1,
                selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                operation_id=OPERATION,
                user_data_path=user_data_path,
                checkpoint=lambda *_: None,
                health_check=self.role_health,
                command_runner=cloud,
            )
        pin.assert_called_once()
        self.assertTrue(replaced.accepted)
        create = next(call for call in cloud.calls if call[1:3] == ("server", "create"))
        block_devices = [
            create[index + 1] for index, value in enumerate(create) if value == "--block-device"
        ]
        self.assertEqual(len(block_devices), 2)
        self.assertTrue(
            any(
                f"uuid={VOLUME}," in value and "device_name=/dev/vdb" in value
                for value in block_devices
            )
        )
        self.assertTrue(
            any(
                f"uuid={VOLUME_2}," in value and "device_name=/dev/vdc" in value
                for value in block_devices
            )
        )
        self.assertTrue(all("delete_on_termination=false" in value for value in block_devices))

    def test_replacement_rejects_candidate_image_or_flavor_drift_before_old_deletion(self) -> None:
        class MismatchCloud(FakeCloud):
            def __init__(self, *args, mismatch: str, **kwargs):
                super().__init__(*args, **kwargs)
                self.mismatch = mismatch

            def __call__(self, argv, **kwargs):
                result_value = super().__call__(argv, **kwargs)
                if tuple(argv)[1:3] == ("server", "create") and self.replacement is not None:
                    if self.mismatch == "image":
                        self.replacement["image"] = {"id": OLD_IMAGE}
                    else:
                        self.replacement["flavor"] = {"id": OLD_IMAGE, "original_name": "wrong"}
                return result_value

        for mismatch in ("image", "flavor"):
            with self.subTest(mismatch=mismatch):
                cloud = MismatchCloud(
                    self.platform,
                    [canonical_image(self.platform, IMAGE_1, role="ingress")],
                    mismatch=mismatch,
                )
                with protected_user_data() as user_data_path:
                    replaced = openstack.replace_host(
                        self.platform,
                        "ingress",
                        selected_image_id=IMAGE_1,
                        selected_compatibility_hash=openstack.image_compatibility_hash(
                            self.platform
                        ),
                        operation_id=OPERATION,
                        user_data_path=user_data_path,
                        health_check=self.role_health,
                        checkpoint=lambda *_: None,
                        command_runner=cloud,
                    )
                self.assertFalse(replaced.accepted)
                self.assertIsNotNone(cloud.server)
                self.assertIsNone(cloud.replacement)
                self.assertFalse(
                    any(
                        call[1:3] == ("server", "delete") and call[3] == SERVER
                        for call in cloud.calls
                    )
                )

    def test_replacement_keeps_old_until_health_then_deletes_by_uuid(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        checkpoints: list[str] = []

        def health(role, host, remaining):
            self.assertEqual(role, "ingress")
            self.assertEqual(host.server_id, REPLACEMENT)
            self.assertIsNotNone(cloud.server, "old server must remain through acceptance checks")
            self.assertGreater(remaining, 0)

        with protected_user_data() as user_data_path:
            replaced = openstack.replace_host(
                self.platform,
                "ingress",
                selected_image_id=IMAGE_1,
                selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                operation_id=OPERATION,
                user_data_path=user_data_path,
                health_check=health,
                checkpoint=lambda phase, refs: checkpoints.append(phase),
                command_runner=cloud,
            )
        self.assertTrue(replaced.accepted)
        self.assertEqual(replaced.active_server_id, REPLACEMENT)
        self.assertTrue(cloud.user_data_seen)
        self.assertIsNone(cloud.server)
        self.assertEqual(checkpoints[-2:], ["accepted", "complete"])
        self.assertLess(checkpoints.index("accepted"), checkpoints.index("complete"))

    def test_default_replacement_renders_current_protected_ingress_inputs(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        checkpoints: list[tuple[str, dict]] = []
        sentinel = "sentinel-default-rendered-traefik-token"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pki = root / "pki"
            pki.mkdir()
            public_key = root / "agentops.pub"
            public_key.write_text("ssh-ed25519 " + "A" * 48 + " agentops\n")
            for name, mode in (
                ("internal-ca.pem", 0o644),
                ("nomad-ingress.pem", 0o644),
                ("nomad-ingress-key.pem", 0o600),
            ):
                path = pki / name
                path.write_text(f"sentinel-pki-{name}\n")
                path.chmod(mode)
            tokens = root / "nomad-tokens.env"
            tokens.write_text(
                "NOMAD_CONTROLLER_TOKEN=sentinel-unused-controller\n"
                f"NOMAD_TRAEFIK_TOKEN={sentinel}\n"
            )
            tokens.chmod(0o600)
            environment = {
                "AGENTOPS_PUBLIC_KEY": str(public_key),
                "NOMAD_TOKENS_FILE": str(tokens),
                "PKI_DIR": str(pki),
                "ENABLE_CLOUDFLARED": "false",
            }
            previous = {name: os.environ.get(name) for name in environment}
            os.environ.update(environment)
            try:
                replaced = openstack.replace_host(
                    self.platform,
                    "ingress",
                    selected_image_id=IMAGE_1,
                    selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                    operation_id=OPERATION,
                    checkpoint=lambda phase, refs: checkpoints.append((phase, dict(refs))),
                    health_check=self.role_health,
                    command_runner=cloud,
                )
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        self.assertTrue(replaced.accepted)
        self.assertIn(sentinel.encode(), cloud.user_data_payload)
        self.assertIsNotNone(cloud.user_data_path)
        assert cloud.user_data_path is not None
        self.assertFalse(cloud.user_data_path.exists())
        self.assertNotIn(sentinel, repr(cloud.calls))
        self.assertNotIn(sentinel, repr(checkpoints))
        self.assertNotIn(sentinel, repr(replaced))
        with tempfile.TemporaryDirectory() as evidence_directory:
            evidence = Path(evidence_directory)
            operation_database = evidence / "operations.sqlite3"
            connection = sqlite3.connect(operation_database)
            try:
                connection.execute("CREATE TABLE checkpoints (phase TEXT, refs_json TEXT)")
                connection.executemany(
                    "INSERT INTO checkpoints VALUES (?, ?)",
                    ((phase, json.dumps(refs, sort_keys=True)) for phase, refs in checkpoints),
                )
                connection.commit()
            finally:
                connection.close()
            operation_log = evidence / "operation.log"
            operation_log.write_text(repr(cloud.calls) + repr(replaced))
            self.assertNotIn(sentinel.encode(), operation_database.read_bytes())
            self.assertNotIn(sentinel, operation_log.read_text())

    def test_replacement_health_failure_rolls_back_retained_old_server(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        health_calls: list[str] = []

        def health(role, host, remaining):
            assert host.server_id is not None
            health_calls.append(host.server_id)
            if host.server_id == REPLACEMENT:
                raise openstack.OpenStackError("fixed safe readiness failure")

        with protected_user_data() as user_data_path:
            replaced = openstack.replace_host(
                self.platform,
                "ingress",
                selected_image_id=IMAGE_1,
                selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                operation_id=OPERATION,
                user_data_path=user_data_path,
                health_check=health,
                checkpoint=lambda *_: None,
                command_runner=cloud,
            )
        self.assertFalse(replaced.accepted)
        self.assertEqual(replaced.active_server_id, SERVER)
        self.assertEqual(cloud.server["name"], self.platform.get("hosts.ingress"))
        self.assertEqual(cloud.server["status"], "ACTIVE")
        self.assertEqual(cloud.port_device, SERVER)
        self.assertIsNone(cloud.replacement)
        self.assertEqual(health_calls, [REPLACEMENT, SERVER])

    def test_admin_replacement_repins_replacement_then_old_host_on_rollback(self) -> None:
        cloud = FakeCloud(
            self.platform,
            [canonical_image(self.platform, IMAGE_1, role="admin")],
            role="admin",
        )
        pinned_console_outputs: list[bytes] = []

        def pin(_address: str, console_output: bytes, **_kwargs: object) -> None:
            pinned_console_outputs.append(console_output)

        def health(_role: str, host: openstack.PersistentHost, _remaining: float) -> None:
            if host.server_id == REPLACEMENT:
                raise openstack.OpenStackError("fixed safe readiness failure")

        with (
            protected_user_data() as user_data_path,
            mock.patch.object(openstack.host_keys, "pin_verified_admin_host_key", side_effect=pin),
        ):
            replaced = openstack.replace_host(
                self.platform,
                "admin",
                selected_image_id=IMAGE_1,
                selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                operation_id=OPERATION,
                user_data_path=user_data_path,
                checkpoint=lambda *_: None,
                health_check=health,
                command_runner=cloud,
            )

        self.assertFalse(replaced.accepted)
        self.assertEqual(replaced.active_server_id, SERVER)
        self.assertEqual(len(pinned_console_outputs), 2)
        self.assertTrue(
            all(self.platform.namespace.encode() in item for item in pinned_console_outputs)
        )

    def test_rollback_of_a_still_running_host_does_not_await_a_new_boot_marker(self) -> None:
        # The prior host stays ACTIVE through rollback, so it never reboots and
        # emits no further readiness marker. Requiring one made rollback report
        # a healthy, serving host as unverified once its original marker had
        # scrolled out of the bounded console window.
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        cloud.ready_markers[SERVER] = 0  # original marker already aged out
        health_checked: list[str] = []

        def health(_role: str, host: openstack.PersistentHost, _remaining: float) -> None:
            if host.server_id == REPLACEMENT:
                raise openstack.OpenStackError("fixed safe readiness failure")
            health_checked.append(str(host.server_id))

        with protected_user_data() as user_data_path:
            replaced = openstack.replace_host(
                self.platform,
                "ingress",
                selected_image_id=IMAGE_1,
                selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                operation_id=OPERATION,
                user_data_path=user_data_path,
                checkpoint=lambda *_: None,
                health_check=health,
                command_runner=cloud,
            )

        # Rollback completes and the prior host is verified by its concrete
        # health check rather than by waiting for a marker that cannot appear.
        self.assertFalse(replaced.accepted)
        self.assertEqual(replaced.active_server_id, SERVER)
        self.assertEqual(health_checked, [SERVER])

    def test_an_asynchronous_power_off_is_not_reported_as_ambiguous(self) -> None:
        # The provider returns from "server stop" before the server is off.
        # Reading the status once, immediately, observes it still running.
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        cloud.stop_settle_reads = 3

        with protected_user_data() as user_data_path:
            replaced = openstack.replace_host(
                self.platform,
                "ingress",
                selected_image_id=IMAGE_1,
                selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                operation_id=OPERATION,
                user_data_path=user_data_path,
                checkpoint=lambda *_: None,
                health_check=lambda *_: None,
                command_runner=cloud,
                sleep=lambda _seconds: None,
            )

        self.assertTrue(replaced.accepted)
        self.assertEqual(replaced.active_server_id, REPLACEMENT)

    def test_a_failed_stop_phase_powers_the_role_back_on(self) -> None:
        # A stop that applied but could not be confirmed used to leave the role
        # powered off with no rollback to restore it, taking its public route
        # down until an operator noticed.
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        cloud.server["status"] = "SHUTOFF"

        openstack._restore_stopped_host_power(
            "ingress",
            cloud.server["name"],
            cloud.server["id"],
            timeout_seconds=30,
            command_runner=cloud,
            executable="openstack",
        )

        self.assertEqual(cloud.start_calls, [cloud.server["id"]])
        self.assertEqual(cloud.server["status"], "ACTIVE")

    def test_power_restore_leaves_a_running_role_alone(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        self.assertEqual(cloud.server["status"], "ACTIVE")

        openstack._restore_stopped_host_power(
            "ingress",
            cloud.server["name"],
            cloud.server["id"],
            timeout_seconds=30,
            command_runner=cloud,
            executable="openstack",
        )

        self.assertEqual(cloud.start_calls, [])

    def test_replacement_rejects_unprotected_user_data_before_provider_calls(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user-data"
            path.write_bytes(b"sentinel-private-user-data")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValidationError, "owner-only"):
                openstack.replace_host(
                    self.platform,
                    "ingress",
                    selected_image_id=IMAGE_1,
                    selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                    operation_id=OPERATION,
                    user_data_path=path,
                    checkpoint=lambda *_: None,
                    health_check=self.role_health,
                    command_runner=cloud,
                )
        self.assertEqual(cloud.calls, [])
        self.assertNotIn("sentinel-private-user-data", repr(cloud.calls))

    def test_exact_resources_reject_extra_volume_and_ambiguous_delete_flag(self) -> None:
        extra = FakeCloud(self.platform)
        extra.volume_attachments.append(
            {
                "ID": VOLUME,
                "Device": "/dev/vdb",
                "Delete On Termination": False,
                "server_id": SERVER,
                "name": "unexpected",
            }
        )
        with self.assertRaisesRegex(openstack.OpenStackError, "unexpected volume"):
            openstack.observe_host_resources(self.platform, "ingress", command_runner=extra)

        admin = FakeCloud(self.platform, role="admin")
        admin.server["volumes_attached"][0]["delete_on_termination"] = "unknown"
        with self.assertRaisesRegex(openstack.OpenStackError, "missing or ambiguous"):
            openstack.observe_host_resources(self.platform, "admin", command_runner=admin)

    def test_prune_stops_with_recovery_refs_if_server_uses_later_candidate(self) -> None:
        class RacingCloud(FakeCloud):
            server_image_observations = 0

            def __call__(self, argv, **kwargs):
                args = tuple(argv)[1:]
                if args[:2] == ("server", "list") and "Image" in args:
                    self.server_image_observations += 1
                    if self.server_image_observations == 4:
                        self.server["image"] = {"id": IMAGE_3}
                return super().__call__(argv, **kwargs)

        cloud = RacingCloud(
            self.platform,
            [
                canonical_image(self.platform, IMAGE_1, created="2026-03-01T00:00:00Z"),
                canonical_image(self.platform, IMAGE_2, created="2026-02-01T00:00:00Z"),
                canonical_image(self.platform, IMAGE_3, created="2026-01-01T00:00:00Z"),
            ],
        )
        plan = openstack.plan_image_prune(
            self.platform, selected_image_ids=[IMAGE_1], retain_newest=1, command_runner=cloud
        )
        with self.assertRaises(openstack.RecoveryRequired) as caught:
            openstack.apply_image_prune(
                self.platform,
                plan,
                selected_image_ids=[IMAGE_1],
                checkpoint=lambda *_: None,
                command_runner=cloud,
            )
        self.assertEqual(caught.exception.refs["deleted_image_ids"], [IMAGE_2])
        self.assertEqual(caught.exception.refs["pending_image_id"], IMAGE_3)
        self.assertNotIn(IMAGE_2, cloud.images)
        self.assertIn(IMAGE_3, cloud.images)

    def test_ambiguous_create_can_recover_only_by_phase_specific_rollback(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        cloud.ambiguous_create = True
        with protected_user_data() as user_data_path:
            with self.assertRaises(openstack.RecoveryRequired) as caught:
                openstack.replace_host(
                    self.platform,
                    "ingress",
                    selected_image_id=IMAGE_1,
                    selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                    operation_id=OPERATION,
                    user_data_path=user_data_path,
                    checkpoint=lambda *_: None,
                    health_check=self.role_health,
                    command_runner=cloud,
                )
        refs = caught.exception.refs
        with self.assertRaisesRegex(ValidationError, "not safe"):
            openstack.recover_host_replacement(
                self.platform,
                "ingress",
                phase="ambiguous",
                refs=refs,
                action="cleanup_old",
                checkpoint=lambda *_: None,
                health_check=self.role_health,
                command_runner=cloud,
            )
        recovered = openstack.recover_host_replacement(
            self.platform,
            "ingress",
            phase="ambiguous",
            refs=refs,
            action="rollback",
            checkpoint=lambda *_: None,
            health_check=self.role_health,
            command_runner=cloud,
        )
        self.assertEqual(recovered.active_server_id, SERVER)
        self.assertIsNone(cloud.replacement)
        self.assertEqual(cloud.port_device, SERVER)

    def test_prune_recovery_reconciles_delete_before_checkpoint_and_refuses_drift(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        images = [
            canonical_image(self.platform, IMAGE_1, created="2026-03-01T00:00:00Z"),
            canonical_image(self.platform, IMAGE_2, created="2026-02-01T00:00:00Z"),
            canonical_image(self.platform, IMAGE_3, created="2026-01-01T00:00:00Z"),
        ]
        cloud = FakeCloud(self.platform, images)
        plan = openstack.plan_image_prune(
            self.platform,
            selected_image_ids=[IMAGE_1],
            retain_newest=1,
            command_runner=cloud,
        )
        durable: tuple[str, dict] | None = None

        def crash_after_delete(phase: str, refs: object) -> None:
            nonlocal durable
            assert isinstance(refs, dict)
            if phase == "image_deleted":
                raise SimulatedCrash
            durable = (phase, dict(refs))

        with self.assertRaises(SimulatedCrash):
            openstack.apply_image_prune(
                self.platform,
                plan,
                selected_image_ids=[IMAGE_1],
                checkpoint=crash_after_delete,
                command_runner=cloud,
            )
        assert durable is not None
        self.assertEqual(durable[0], "image_deleting")
        self.assertNotIn(IMAGE_2, cloud.images)
        inspected = openstack.recover_image_prune(
            self.platform,
            plan,
            refs=durable[1],
            action="inspect",
            selected_image_ids=[IMAGE_1],
            checkpoint=lambda *_: None,
            command_runner=cloud,
        )
        self.assertEqual(inspected.deleted_image_ids, (IMAGE_2,))
        self.assertIn(IMAGE_3, cloud.images)

        cloud.images[REVIEW_IMAGE] = canonical_image(
            self.platform, REVIEW_IMAGE, created="2025-12-01T00:00:00Z"
        )
        with self.assertRaisesRegex(openstack.DriftError, "inventory drifted"):
            openstack.recover_image_prune(
                self.platform,
                plan,
                refs=durable[1],
                action="continue",
                selected_image_ids=[IMAGE_1],
                checkpoint=lambda *_: None,
                command_runner=cloud,
            )
        cloud.images.pop(REVIEW_IMAGE)
        recovered = openstack.recover_image_prune(
            self.platform,
            plan,
            refs=durable[1],
            action="continue",
            selected_image_ids=[IMAGE_1],
            checkpoint=lambda *_: None,
            command_runner=cloud,
        )
        self.assertEqual(recovered.deleted_image_ids, (IMAGE_2, IMAGE_3))
        self.assertNotIn(IMAGE_3, cloud.images)

    def test_prune_records_operation_protection_and_rejects_apply_reference_drift(self) -> None:
        cloud = FakeCloud(
            self.platform,
            [
                canonical_image(self.platform, IMAGE_1, created="2026-03-01T00:00:00Z"),
                canonical_image(self.platform, IMAGE_2, created="2026-02-01T00:00:00Z"),
                canonical_image(self.platform, IMAGE_3, created="2026-01-01T00:00:00Z"),
            ],
        )
        plan = openstack.plan_image_prune(
            self.platform,
            selected_image_ids=[IMAGE_1],
            operation_image_ids=[IMAGE_3],
            retain_newest=1,
            command_runner=cloud,
        )
        self.assertEqual(plan.operation_image_ids, (IMAGE_3,))
        self.assertIn(IMAGE_3, plan.protected_image_ids)
        self.assertNotIn(IMAGE_3, plan.image_ids)
        with self.assertRaisesRegex(openstack.DriftError, "operation image protection"):
            openstack.apply_image_prune(
                self.platform,
                plan,
                selected_image_ids=[IMAGE_1],
                operation_image_ids=[],
                checkpoint=lambda *_: None,
                command_runner=cloud,
            )
        self.assertIn(IMAGE_2, cloud.images)

    def test_stop_and_rename_crash_checkpoints_restore_prior_active_host(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        for crash_phase in ("old_stopped", "old_renamed"):
            with self.subTest(crash_phase=crash_phase):
                cloud = FakeCloud(
                    self.platform,
                    [canonical_image(self.platform, IMAGE_1, role="ingress")],
                )
                durable: tuple[str, dict] | None = None

                def checkpoint(phase: str, refs: object, crash_at: str = crash_phase) -> None:
                    nonlocal durable
                    assert isinstance(refs, dict)
                    if phase == crash_at:
                        raise SimulatedCrash
                    durable = (phase, dict(refs))

                with protected_user_data() as user_data_path:
                    with self.assertRaises(SimulatedCrash):
                        openstack.replace_host(
                            self.platform,
                            "ingress",
                            selected_image_id=IMAGE_1,
                            selected_compatibility_hash=openstack.image_compatibility_hash(
                                self.platform
                            ),
                            operation_id=OPERATION,
                            user_data_path=user_data_path,
                            checkpoint=checkpoint,
                            health_check=self.role_health,
                            command_runner=cloud,
                        )
                assert durable is not None
                expected_phase = "observed" if crash_phase == "old_stopped" else "old_stopped"
                self.assertEqual(durable[0], expected_phase)
                inspected = openstack.recover_host_replacement(
                    self.platform,
                    "ingress",
                    phase=expected_phase,
                    refs=durable[1],
                    action="inspect",
                    checkpoint=lambda *_: None,
                    command_runner=cloud,
                )
                self.assertEqual(inspected.cleanup_state, "rollback_required")
                recovered = openstack.recover_host_replacement(
                    self.platform,
                    "ingress",
                    phase=expected_phase,
                    refs=durable[1],
                    action="rollback",
                    checkpoint=lambda *_: None,
                    health_check=self.role_health,
                    command_runner=cloud,
                )
                self.assertEqual(recovered.active_server_id, SERVER)
                self.assertEqual(cloud.server["status"], "ACTIVE")
                self.assertEqual(cloud.server["name"], self.platform.get("hosts.ingress"))

    def test_created_checkpoint_can_continue_acceptance_instead_of_guessing(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        created_refs: dict | None = None

        def checkpoint(phase: str, refs: object) -> None:
            nonlocal created_refs
            assert isinstance(refs, dict)
            if phase == "replacement_created":
                created_refs = dict(refs)
                raise SimulatedCrash

        with protected_user_data() as user_data_path:
            with self.assertRaises(SimulatedCrash):
                openstack.replace_host(
                    self.platform,
                    "ingress",
                    selected_image_id=IMAGE_1,
                    selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                    operation_id=OPERATION,
                    user_data_path=user_data_path,
                    checkpoint=checkpoint,
                    health_check=self.role_health,
                    command_runner=cloud,
                )
        assert created_refs is not None
        recovered = openstack.recover_host_replacement(
            self.platform,
            "ingress",
            phase="replacement_created",
            refs=created_refs,
            action="continue",
            checkpoint=lambda *_: None,
            health_check=self.role_health,
            command_runner=cloud,
        )
        self.assertEqual(recovered.active_server_id, REPLACEMENT)
        self.assertIsNone(cloud.server)

    def test_accepted_delete_before_complete_checkpoint_continues_exactly(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        accepted_refs: dict | None = None

        def checkpoint(phase: str, refs: object) -> None:
            nonlocal accepted_refs
            assert isinstance(refs, dict)
            if phase == "accepted":
                accepted_refs = dict(refs)
            if phase == "complete":
                raise SimulatedCrash

        with protected_user_data() as user_data_path:
            with self.assertRaises(SimulatedCrash):
                openstack.replace_host(
                    self.platform,
                    "ingress",
                    selected_image_id=IMAGE_1,
                    selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                    operation_id=OPERATION,
                    user_data_path=user_data_path,
                    checkpoint=checkpoint,
                    health_check=self.role_health,
                    command_runner=cloud,
                )
        assert accepted_refs is not None
        self.assertIsNone(cloud.server)
        recovered = openstack.recover_host_replacement(
            self.platform,
            "ingress",
            phase="accepted",
            refs=accepted_refs,
            action="continue",
            checkpoint=lambda *_: None,
            health_check=self.role_health,
            command_runner=cloud,
        )
        self.assertEqual(recovered.active_server_id, REPLACEMENT)
        self.assertEqual(recovered.cleanup_state, "confirmed")

    def _health_host(self, role: str) -> openstack.PersistentHost:
        return openstack.PersistentHost(
            role,
            self.platform.get(f"hosts.{role}"),
            SERVER,
            "ACTIVE",
            OLD_IMAGE,
            FLAVOR,
            "example.2c2g",
            (),
        )

    def test_scrolled_out_readiness_marker_is_not_treated_as_failure(self) -> None:
        # A long-running host scrolls its boot marker out of the bounded console
        # window. That is absence of evidence, not evidence of failure, and the
        # concrete per-role checks still have to run and pass.
        http_calls: list[str] = []

        def http_get(url: str, **bounds: object) -> HttpResult:
            http_calls.append(url)
            return HttpResult(200, {}, b"OK")

        cloud = FakeCloud(self.platform, role="ingress")
        cloud.ready_markers[SERVER] = 0  # marker aged out of the window
        cloud.failed_markers[SERVER] = 0

        openstack.check_role_health(
            self.platform,
            "ingress",
            self._health_host("ingress"),
            30,
            provider_runner=cloud,
            service_runner=lambda argv, **_kwargs: result(argv),
            http_get=http_get,
        )
        self.assertEqual(len(http_calls), 2)

    def test_explicit_failure_marker_still_fails_hard(self) -> None:
        cloud = FakeCloud(self.platform, role="ingress")
        cloud.ready_markers[SERVER] = 0
        cloud.failed_markers[SERVER] = 1

        with self.assertRaisesRegex(openstack.OpenStackError, "reported failed units"):
            openstack.check_role_health(
                self.platform,
                "ingress",
                self._health_host("ingress"),
                30,
                provider_runner=cloud,
                service_runner=lambda argv, **_kwargs: result(argv),
                http_get=lambda url, **_kwargs: HttpResult(200, {}, b"OK"),
            )

    def test_failure_after_ready_fails_but_ready_after_failure_passes(self) -> None:
        # Ordering, not mere presence, decides: the newest marker wins.
        cloud = FakeCloud(self.platform, role="ingress")
        cloud.ready_markers[SERVER] = 1
        cloud.failed_markers[SERVER] = 1  # emitted after ready in the fake output

        with self.assertRaisesRegex(openstack.OpenStackError, "reported failed units"):
            openstack.check_role_health(
                self.platform,
                "ingress",
                self._health_host("ingress"),
                30,
                provider_runner=cloud,
                service_runner=lambda argv, **_kwargs: result(argv),
                http_get=lambda url, **_kwargs: HttpResult(200, {}, b"OK"),
            )

    def test_concrete_role_health_checks_use_bounded_authenticated_paths(self) -> None:
        service_calls: list[tuple[str, ...]] = []
        http_calls: list[str] = []

        def service_runner(argv: object, **bounds: object) -> CommandResult:
            assert isinstance(argv, tuple)
            self.assertLessEqual(bounds["stdout_limit"], 65_536)
            self.assertLessEqual(bounds["stderr_limit"], 65_536)
            self.assertEqual(
                argv[:5],
                (
                    "ssh",
                    "-F",
                    "/srv/openstack-platform/.secrets/ssh/config",
                    "platform-admin",
                    "--",
                ),
            )
            self.assertFalse(argv[0].startswith("/srv/app-platform"))
            service_calls.append(argv)
            return result(argv)

        def http_get(url: str, **bounds: object) -> HttpResult:
            self.assertEqual(bounds["response_limit"], 64)
            self.assertFalse(bounds["allow_redirects"])
            http_calls.append(url)
            return HttpResult(200, {}, b"OK")

        for role in openstack.PERSISTENT_ROLES:
            cloud = FakeCloud(self.platform, role=role)
            host = openstack.PersistentHost(
                role,
                self.platform.get(f"hosts.{role}"),
                SERVER,
                "ACTIVE",
                OLD_IMAGE,
                FLAVOR,
                "example.2c2g",
                (),
            )
            openstack.check_role_health(
                self.platform,
                role,
                host,
                30,
                provider_runner=cloud,
                service_runner=service_runner,
                http_get=http_get,
            )
        self.assertEqual(len(service_calls), 2)
        self.assertTrue(any("nomad" in call[-1] for call in service_calls))
        storage_check = next(
            call[-1] for call in service_calls if call[-1].endswith("check_services.py")
        )
        self.assertTrue(storage_check.startswith("/run/current-system/sw/bin/python "))
        self.assertNotIn("service-check-venv", storage_check)
        self.assertTrue(
            all(
                not argument.startswith("/srv/app-platform")
                for call in service_calls
                for argument in call[:-1]
            )
        )
        self.assertEqual(
            http_calls,
            [
                f"http://{self.platform.get('addresses.ingress')}/healthz",
                f"https://{self.platform.domain}/healthz",
            ],
        )

    def test_current_failed_readiness_blocks_role_acceptance(self) -> None:
        class FailedBootCloud(FakeCloud):
            def __call__(self, argv, **kwargs):
                args = tuple(argv)[1:]
                result_value = super().__call__(argv, **kwargs)
                if args[:2] == ("server", "reboot"):
                    self.failed_markers[args[2]] = 1
                return result_value

        failed = FailedBootCloud(self.platform)
        with self.assertRaisesRegex(openstack.OpenStackError, "failed service readiness"):
            openstack.power_host(
                self.platform,
                "ingress",
                "reboot",
                health_check=self.role_health,
                command_runner=failed,
            )

    def test_post_acceptance_recovery_cleans_only_retained_old_uuid(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        cloud.retain_old_delete = True
        checkpoints: list[tuple[str, dict]] = []
        with protected_user_data() as user_data_path:
            replaced = openstack.replace_host(
                self.platform,
                "ingress",
                selected_image_id=IMAGE_1,
                selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                operation_id=OPERATION,
                user_data_path=user_data_path,
                checkpoint=lambda phase, refs: checkpoints.append((phase, dict(refs))),
                health_check=self.role_health,
                command_runner=cloud,
            )
        self.assertEqual(replaced.cleanup_state, "old_server_retained")
        phase, refs = checkpoints[-1]
        self.assertEqual(phase, "complete")
        cloud.retain_old_delete = False
        recovered = openstack.recover_host_replacement(
            self.platform,
            "ingress",
            phase=phase,
            refs=refs,
            action="cleanup_old",
            checkpoint=lambda *_: None,
            health_check=self.role_health,
            command_runner=cloud,
        )
        self.assertEqual(recovered.active_server_id, REPLACEMENT)
        self.assertIsNone(cloud.server)
        self.assertIsNotNone(cloud.replacement)


if __name__ == "__main__":
    unittest.main()
