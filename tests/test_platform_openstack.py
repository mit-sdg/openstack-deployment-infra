from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from openstack_platform import ingress_credentials, openstack, release_manifest
from openstack_platform.config import load_platform
from openstack_platform.runtime import CommandFailure, CommandResult, HttpResult
from openstack_platform.validation import ValidationError
from tests.repository_fixtures import clean_repository
from tests.test_ingress_credentials import connector_token

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
TARGET_FLAVOR = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
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
        # These tests exercise the provider state machine with opaque fixtures.
        # Real protected-token rendering and bypass refusal are covered separately.
        real_replace = openstack.replace_host

        def replace_fixture(platform, role, **kwargs):
            if role == "ingress":
                kwargs["user_data_path"] = None
                with mock.patch.object(
                    ingress_credentials,
                    "staged_replacement_user_data",
                    side_effect=lambda *args, **kw: openstack._protected_user_data_copy(
                        path, maximum_bytes=kw["maximum_bytes"]
                    ),
                ):
                    return real_replace(platform, role, **kwargs)
            return real_replace(platform, role, **kwargs)

        with mock.patch.object(openstack, "replace_host", side_effect=replace_fixture):
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
        flavor_name = platform.get(f"flavors.{role}")
        self.flavors = {
            flavor_name: {"id": FLAVOR, "name": flavor_name, "vcpus": 4, "ram": 8192, "disk": 20}
        }
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
        volume_keys = {"admin": ("adminState", "backup"), "storage": ("data",)}.get(role, ())
        self.volume_attachments = [
            {
                "ID": (VOLUME, VOLUME_2)[index],
                "Device": ("/dev/vdb", "/dev/vdc")[index],
                "Delete On Termination": False,
                "server_id": SERVER,
                "name": platform.get(f"volumes.{key}.name"),
            }
            for index, key in enumerate(volume_keys)
        ]
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
            flavor_id = args[args.index("--flavor") + 1]
            flavor_name = next(
                item["name"] for item in self.flavors.values() if item["id"] == flavor_id
            )
            self.replacement = {
                "id": REPLACEMENT,
                "name": name,
                "status": "ACTIVE",
                "image": {"id": IMAGE_1},
                "flavor": {"id": flavor_id, "original_name": flavor_name},
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
            if args[2] not in self.flavors:
                raise CommandFailure("provider flavor lookup failed", result(argv, returncode=1))
            return result(argv, self.flavors[args[2]])
        raise AssertionError(f"unexpected fake OpenStack call: {argv}")

    def assert_safe_call(self, argv: tuple[str, ...], kwargs: dict) -> None:
        assert kwargs["timeout_seconds"] > 0
        assert kwargs["stdout_limit"] in {32_768, 1_048_576}
        assert "OS_PASSWORD" in kwargs["inherit_env"]
        assert "private cloud-init" not in " ".join(argv)

    @staticmethod
    def assertEqualMode(path: Path) -> None:
        assert path.stat().st_mode & 0o777 == 0o600


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

    def test_publisher_verifies_unsigned_opt_in_content_and_signed_reattestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "openstack"
            log = root / "create.json"
            image = root / "image.qcow2"
            image.write_bytes(b"qcow")
            repository, commit = clean_repository(ROOT, root / "repository")
            component_dir = root / "component"
            component_manifest = release_manifest.generate(
                repository,
                commit,
                component_dir,
                signing_key=None,
                unsigned=False,
                unsigned_production=True,
            )
            artifact_inputs: dict[str, object] = {}
            worker_output = Path("/nix/store/00000000000000000000000000000000-worker-image")
            worker_path_info = root / "worker.path-info.json"
            for index, role in enumerate(release_manifest.ROLES):
                output = (
                    worker_output
                    if role == "worker"
                    else Path("/nix/store") / f"{index + 1:032x}-{role}-image"
                )
                path_info = (
                    worker_path_info if role == "worker" else root / f"{role}.path-info.json"
                )
                path_info.write_text(
                    json.dumps(
                        {
                            str(output): {
                                "narHash": "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                                "narSize": 4,
                                "references": [],
                            }
                        }
                    )
                )
                artifact_inputs[role] = {
                    "qcow2": str(image),
                    "pathInfo": str(path_info),
                    "outputStorePath": str(output),
                    "publicationMetadata": dict(
                        openstack.publisher_metadata(self.platform, role, commit)
                    ),
                }
            inputs_path = root / "artifact-inputs.json"
            inputs_path.write_text(json.dumps(artifact_inputs))
            artifact_dir = root / "artifact-evidence"
            artifact_manifest_path = release_manifest.generate_artifact_manifest(
                component_manifest,
                inputs_path,
                artifact_dir,
                signing_key=None,
                unsigned=False,
                unsigned_production=True,
            )
            artifact_manifest = json.loads(artifact_manifest_path.read_text())
            worker_artifact = artifact_manifest["roleArtifacts"]["worker"]
            fake.write_text(
                """#!/usr/bin/env python3
import hashlib, json, os, pathlib, shutil, sys
args = sys.argv[1:]
if args[:2] == ["token", "issue"]:
    print("00000000000040008000000000000000")
elif args[:2] == ["project", "show"]:
    print("00000000000040008000000000000000")
    print("example-project")
elif args[:2] == ["image", "list"]:
    print(json.dumps([{"ID": "11111111-1111-4111-8111-111111111111", "Name": "example-nixos-worker"}] if os.environ.get("FAKE_EXISTING_IMAGE") else []))
elif args[:2] == ["image", "show"]:
    created = json.loads(pathlib.Path(os.environ["FAKE_LOG"]).read_text())
    properties = {
        item.split("=", 1)[0]: item.split("=", 1)[1]
        for index, item in enumerate(created)
        if index and created[index - 1] == "--property" and "=" in item
    }
    if os.environ.get("FAKE_BAD_METADATA"):
        properties = {key: "wrong" for key in properties}
    image_file = created[created.index("--file") + 1]
    digest = hashlib.md5(pathlib.Path(image_file).read_bytes(), usedforsecurity=False).hexdigest()
    print(json.dumps({
        "id": "11111111-1111-4111-8111-111111111111",
        "name": "example-nixos-worker",
        "status": "active",
        "owner": "00000000000040008000000000000000",
        "checksum": digest,
        "os_hash_algo": os.environ.get("FAKE_HASH_ALGO"),
        "os_hash_value": hashlib.sha512(pathlib.Path(image_file).read_bytes()).hexdigest() if os.environ.get("FAKE_HASH_ALGO") == "sha512" else None,
        "properties": properties,
    }))
elif args[:2] == ["image", "save"]:
    created = json.loads(pathlib.Path(os.environ["FAKE_LOG"]).read_text())
    source = created[created.index("--file") + 1]
    destination = args[args.index("--file") + 1]
    shutil.copyfile(source, destination)
    if os.environ.get("FAKE_BAD_DOWNLOAD"):
        pathlib.Path(destination).write_bytes(b"wrong provider bytes")
elif args[:2] == ["image", "set"]:
    path = pathlib.Path(os.environ["FAKE_LOG"])
    path.with_suffix(".set.json").write_text(json.dumps(args))
    created = json.loads(path.read_text())
    for index, item in enumerate(args):
        if index and args[index - 1] == "--property":
            key = item.split("=", 1)[0]
            existing = next((i for i, value in enumerate(created) if i and created[i - 1] == "--property" and value.split("=", 1)[0] == key), None)
            if existing is None:
                created.extend(["--property", item])
            else:
                created[existing] = item
    path.write_text(json.dumps(created))
elif args[:2] == ["image", "create"]:
    if os.environ.get("FAKE_EXISTING_IMAGE"):
        raise SystemExit("must verify and reuse rather than recreate")
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
                    "SOURCE_COMMIT": commit,
                    "FAKE_LOG": str(log),
                    "PLATFORM_RELEASE_MANIFEST": str(component_manifest),
                    "PLATFORM_ARTIFACT_MANIFEST": str(artifact_manifest_path),
                    "OS_PROJECT_NAME": "example-project",
                    "PLATFORM_ARTIFACT_MANIFEST_SHA256": hashlib.sha256(
                        artifact_manifest_path.read_bytes()
                    ).hexdigest(),
                    "PLATFORM_ARTIFACT_QCOW2_SHA256": worker_artifact["qcow2Sha256"],
                    "PLATFORM_ARTIFACT_NIX_CLOSURE_SHA256": worker_artifact["nixClosureSha256"],
                    "PLATFORM_ARTIFACT_NIX_OUTPUT": worker_artifact["nixOutput"],
                }
            )
            environment["PLATFORM_ALLOW_UNSIGNED_PRODUCTION"] = (
                release_manifest.UNSIGNED_PRODUCTION_ACKNOWLEDGEMENT
            )
            environment["PLATFORM_ENVIRONMENT"] = "production"
            environment["FAKE_HASH_ALGO"] = "sha512"
            completed = subprocess.run(
                [
                    str(ROOT / "infra/openstack/publish_nixos_image.sh"),
                    "worker",
                    str(image),
                    str(worker_output),
                    str(worker_path_info),
                ],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            expected_checksum = hashlib.md5(image.read_bytes(), usedforsecurity=False).hexdigest()
            self.assertIn(f"checksum={expected_checksum}", completed.stdout.decode())
            self.assertIn("sha256=download", completed.stdout.decode())
            create = json.loads(log.read_text())
            properties = [
                create[index + 1] for index, item in enumerate(create) if item == "--property"
            ]
            expected = openstack.publisher_metadata(self.platform, "worker", commit)
            for key, value in expected.items():
                self.assertIn(f"{key}={value}", properties)
            self.assertIn("hw_qemu_guest_agent=yes", properties)
            retry_environment = {**environment, "FAKE_EXISTING_IMAGE": "1"}
            set_log = log.with_suffix(".set.json")
            # Unsigned evidence cannot replace an existing evidence digest,
            # even when the QCOW2 identity itself is unchanged.
            unsigned_bytes = artifact_manifest_path.read_bytes()
            artifact_manifest_path.write_bytes(unsigned_bytes + b" ")
            refused = subprocess.run(
                completed.args,
                cwd=ROOT,
                env={
                    **retry_environment,
                    "PLATFORM_ARTIFACT_MANIFEST_SHA256": hashlib.sha256(
                        artifact_manifest_path.read_bytes()
                    ).hexdigest(),
                },
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(set_log.exists())
            artifact_manifest_path.write_bytes(unsigned_bytes)

            key, public = root / "private.pem", root / "public.pem"
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", key], check=True)
            subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", public], check=True)
            signed_component = release_manifest.generate(
                repository, commit, root / "signed", signing_key=key, unsigned=False
            )
            signed_artifact = release_manifest.generate_artifact_manifest(
                signed_component,
                inputs_path,
                root / "signed/artifacts",
                signing_key=key,
                unsigned=False,
            )
            key.unlink()  # The publisher never needs the signing key.
            signed_environment = {
                **retry_environment,
                "PLATFORM_RELEASE_MANIFEST": str(signed_component),
                "PLATFORM_RELEASE_SIGNATURE": str(root / "signed/release-manifest.sig"),
                "PLATFORM_RELEASE_TRUST_ROOT": str(public),
                "PLATFORM_ARTIFACT_MANIFEST": str(signed_artifact),
                "PLATFORM_ARTIFACT_SIGNATURE": str(root / "signed/artifacts/role-artifacts.sig"),
                "PLATFORM_ARTIFACT_TRUST_ROOT": str(public),
                "PLATFORM_ARTIFACT_MANIFEST_SHA256": hashlib.sha256(
                    signed_artifact.read_bytes()
                ).hexdigest(),
            }
            signed_environment.pop("PLATFORM_ALLOW_UNSIGNED_PRODUCTION")
            refused = subprocess.run(
                completed.args,
                cwd=ROOT,
                env={**signed_environment, "FAKE_BAD_DOWNLOAD": "1"},
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(set_log.exists(), "failed content gate reached metadata mutation")
            promoted = subprocess.run(
                completed.args,
                cwd=ROOT,
                env=signed_environment,
                capture_output=True,
                check=False,
            )
            self.assertEqual(promoted.returncode, 0, promoted.stderr.decode())
            self.assertIn("signed-reattestation=verified", promoted.stdout.decode())
            self.assertEqual(image.read_bytes(), b"qcow")
            updates = json.loads(set_log.read_text())
            self.assertIn(
                "app_platform_artifact_manifest_sha256="
                + signed_environment["PLATFORM_ARTIFACT_MANIFEST_SHA256"],
                updates,
            )
            self.assertIn(
                "app_platform_previous_artifact_manifest_sha256="
                + environment["PLATFORM_ARTIFACT_MANIFEST_SHA256"],
                updates,
            )
            log.unlink()
            refused = subprocess.run(
                completed.args,
                cwd=ROOT,
                env={**environment, "PLATFORM_ALLOW_UNSIGNED_PRODUCTION": "true"},
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(log.exists(), "trust failure reached image creation")
            image.write_bytes(b"changed after verification")
            refused = subprocess.run(
                completed.args, cwd=ROOT, env=environment, capture_output=True, check=False
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(log.exists(), "changed QCOW2 reached image creation")

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

        cloud = MalformedProjectCloud(self.platform, malformed_command=("token", "issue"))
        with self.assertRaisesRegex(openstack.OpenStackError, "malformed project UUID"):
            openstack.verify_project(self.platform, command_runner=cloud)
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

    def test_unknown_replacement_flavor_fails_before_stopping_the_old_host(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        cloud.flavors.clear()
        cloud.server["flavor"]["id"] = "1000"
        checkpoints = mock.Mock()
        with (
            protected_user_data() as path,
            self.assertRaisesRegex(openstack.OpenStackError, "provider details were withheld"),
        ):
            openstack.replace_host(
                self.platform,
                "ingress",
                selected_image_id=IMAGE_1,
                selected_compatibility_hash=openstack.image_compatibility_hash(self.platform),
                operation_id=OPERATION,
                user_data_path=path,
                checkpoint=checkpoints,
                health_check=self.role_health,
                command_runner=cloud,
            )
        checkpoints.assert_not_called()
        self.assertEqual(cloud.server["status"], "ACTIVE")
        self.assertEqual(cloud.port_device, SERVER)
        self.assertFalse(
            any(
                call[1:3]
                in {
                    ("server", "stop"),
                    ("server", "set"),
                    ("server", "create"),
                    ("server", "delete"),
                }
                for call in cloud.calls
            )
        )

    def test_failed_create_preserves_hosts_when_candidate_state_is_uncertain(self) -> None:
        class FailedCreateCloud(FakeCloud):
            create_failed = False

            def __init__(self, *args, failure_mode: str, **kwargs):
                super().__init__(*args, **kwargs)
                self.failure_mode = failure_mode

            def __call__(self, argv, **kwargs):
                args = tuple(argv)[1:]
                if args[:2] == ("server", "create"):
                    if self.failure_mode == "candidate_exists":
                        super().__call__(argv, **kwargs)
                    else:
                        self.calls.append(tuple(argv))
                        self.assert_safe_call(tuple(argv), kwargs)
                    self.create_failed = True
                    if self.failure_mode == "timeout":
                        raise openstack.runtime.CommandTimedOut("create timed out")
                    return result(tuple(argv), returncode=1)
                if (
                    self.create_failed
                    and self.failure_mode == "lookup_unavailable"
                    and args[:2] == ("server", "list")
                ):
                    raise CommandFailure("provider observation unavailable")
                return super().__call__(argv, **kwargs)

        for failure_mode in ("candidate_exists", "lookup_unavailable", "timeout"):
            with self.subTest(failure_mode=failure_mode):
                cloud = FailedCreateCloud(
                    self.platform,
                    [canonical_image(self.platform, IMAGE_1, role="ingress")],
                    failure_mode=failure_mode,
                )
                cloud.flavors[self.platform.get("flavors.ingress")]["id"] = TARGET_FLAVOR
                with (
                    protected_user_data() as path,
                    self.assertRaises(openstack.RecoveryRequired) as caught,
                ):
                    openstack.replace_host(
                        self.platform,
                        "ingress",
                        selected_image_id=IMAGE_1,
                        selected_compatibility_hash=openstack.image_compatibility_hash(
                            self.platform
                        ),
                        operation_id=OPERATION,
                        user_data_path=path,
                        checkpoint=lambda *_: None,
                        health_check=self.role_health,
                        command_runner=cloud,
                    )
                self.assertEqual(caught.exception.refs["target_flavor_id"], TARGET_FLAVOR)
                self.assertIsNotNone(cloud.server)
                self.assertFalse(any(call[1:3] == ("server", "delete") for call in cloud.calls))
                self.assertEqual(cloud.start_calls, [])
                self.assertEqual(cloud.replacement is not None, failure_mode == "candidate_exists")

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
                    elif self.mismatch == "flavor":
                        self.replacement["flavor"] = {"id": FLAVOR, "original_name": "example.2c2g"}
                if (
                    self.mismatch == "late_flavor"
                    and tuple(argv)[1:4] == ("server", "show", REPLACEMENT)
                    and "status" in argv
                ):
                    self.replacement["flavor"] = {"id": FLAVOR, "original_name": "example.2c2g"}
                    return result(tuple(argv), self.replacement)
                return result_value

        for mismatch in ("image", "flavor", "late_flavor"):
            with self.subTest(mismatch=mismatch):
                cloud = MismatchCloud(
                    self.platform,
                    [canonical_image(self.platform, IMAGE_1, role="ingress")],
                    mismatch=mismatch,
                )
                cloud.flavors[self.platform.get("flavors.ingress")]["id"] = TARGET_FLAVOR
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
        create = next(call for call in cloud.calls if call[1:3] == ("server", "create"))
        self.assertEqual(create[create.index("--flavor") + 1], FLAVOR)
        self.assertEqual(cloud.replacement["flavor"]["id"], FLAVOR)
        self.assertEqual(len([call for call in cloud.calls if call[1:3] == ("flavor", "show")]), 1)
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
            tunnel = root / "tunnel-token"
            tunnel.write_bytes(connector_token())
            tunnel.chmod(0o600)
            environment = {
                "OPERATOR_PUBLIC_KEY": str(public_key),
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
                    cloudflare_tunnel_token_file=tunnel,
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
        self.assertIn(
            base64.b64encode(b"TUNNEL_TOKEN=" + connector_token() + b"\n"), cloud.user_data_payload
        )
        self.assertNotIn(connector_token().decode(), repr(cloud.calls))
        self.assertNotIn(connector_token().decode(), repr(checkpoints))
        self.assertNotIn(connector_token().decode(), repr(replaced))
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
            self.assertNotIn(connector_token(), operation_database.read_bytes())
            self.assertNotIn(connector_token().decode(), operation_log.read_text())

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

    def test_replacement_rejects_unprotected_user_data_before_provider_calls(self) -> None:
        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user-data"
            path.write_bytes(b"sentinel-private-user-data")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValidationError, "overrides are refused"):
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

    def test_created_checkpoint_can_continue_acceptance_instead_of_guessing(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        cloud = FakeCloud(self.platform, [canonical_image(self.platform, IMAGE_1, role="ingress")])
        cloud.flavors[self.platform.get("flavors.ingress")]["id"] = "4200"
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
        recovery_platform = replace(
            self.platform,
            document={
                **self.platform.document,
                "flavors": {**self.platform.get("flavors"), "ingress": "unknown-later-flavor"},
            },
        )
        cloud.flavors.clear()
        before_recovery = len(cloud.calls)
        recovered = openstack.recover_host_replacement(
            recovery_platform,
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

        self.assertEqual(cloud.replacement["flavor"]["id"], "4200")
        self.assertFalse(
            any(call[1:3] == ("flavor", "show") for call in cloud.calls[before_recovery:])
        )

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
        admin_check = next(call[-1] for call in service_calls if "nomad.service" in call[-1])
        self.assertIn("/run/current-system/sw/bin/systemctl", admin_check)
        self.assertNotIn("operator raft", admin_check)
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
            [f"https://{self.platform.domain}/healthz"],
        )


if __name__ == "__main__":
    unittest.main()
