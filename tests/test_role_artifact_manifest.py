from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from openstack_platform import release_manifest

ROOT = Path(__file__).resolve().parents[1]
COMMIT = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
).stdout.strip()


class RoleArtifactManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.component_dir = self.root / "component"
        self.component_manifest = release_manifest.generate(
            ROOT, COMMIT, self.component_dir, signing_key=None, unsigned=True
        )
        self.inputs: dict[str, dict[str, object]] = {}
        self.files: dict[str, tuple[Path, Path, Path]] = {}
        for index, role in enumerate(release_manifest.ROLES):
            store_name = f"{index + 1:032x}-{role}-openstack-image"
            output = Path("/nix/store") / store_name
            qcow = self.root / f"{role}.qcow2"
            qcow.write_bytes((f"real-{role}-qcow2\n".encode()) * 32)
            nar_hash = (
                "sha256-"
                + base64.b64encode(hashlib.sha256(f"nar-{role}".encode()).digest()).decode()
            )
            path_info = self.root / f"{role}.path-info.json"
            path_info.write_text(
                json.dumps(
                    {
                        str(output): {
                            "narHash": nar_hash,
                            "narSize": 1000 + index,
                            "references": [],
                        }
                    }
                )
            )
            metadata = {
                "test_compatibility_sha256": "a" * 64,
                "test_managed_by": "platform",
                "test_metadata_version": "1",
                "test_namespace": "test",
                "test_prefix": "test",
                "test_project_id": "00000000-0000-4000-8000-000000000001",
                "test_role": role,
                "test_source_commit": COMMIT,
            }
            self.inputs[role] = {
                "qcow2": str(qcow),
                "pathInfo": str(path_info),
                "outputStorePath": str(output),
                "publicationMetadata": metadata,
            }
            self.files[role] = (qcow, path_info, output)
        self.inputs_path = self.root / "artifact-inputs.json"
        self.inputs_path.write_text(json.dumps(self.inputs))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _signed(self) -> tuple[Path, Path, Path]:
        key = self.root / "key.pem"
        public = self.root / "public.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", key], check=True)
        subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", public], check=True)
        output = self.root / "artifacts"
        manifest = release_manifest.generate_artifact_manifest(
            self.component_manifest,
            self.inputs_path,
            output,
            signing_key=key,
            unsigned=False,
        )
        return manifest, output / "role-artifacts.sig", public

    def test_signed_manifest_binds_real_qcow_closures_metadata_and_source(self) -> None:
        manifest_path, signature, public = self._signed()
        manifest = release_manifest.verify_artifact_manifest(
            self.component_manifest,
            manifest_path,
            signature=signature,
            trust_root=public,
        )
        self.assertEqual(set(manifest["roleArtifacts"]), set(release_manifest.ROLES))
        worker_qcow, worker_info, worker_output = self.files["worker"]
        record = release_manifest.verify_role_artifact(
            manifest,
            "worker",
            qcow2=worker_qcow,
            path_info=worker_info,
            output_store_path=worker_output,
            publication_metadata=self.inputs["worker"]["publicationMetadata"],
        )
        self.assertEqual(
            record["qcow2Sha256"], hashlib.sha256(worker_qcow.read_bytes()).hexdigest()
        )

        sbom = json.loads((manifest_path.parent / "role-artifacts.sbom.spdx.json").read_text())
        self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
        references = {
            ref["referenceLocator"]
            for package in sbom["packages"]
            for ref in package.get("externalRefs", [])
        }
        self.assertTrue(any(value.startswith("pkg:pypi/") for value in references))
        self.assertTrue(any(value.startswith("pkg:generic/") for value in references))

    def test_qcow_closure_metadata_source_and_evidence_tampering_are_rejected(self) -> None:
        manifest_path, signature, public = self._signed()
        manifest = release_manifest.verify_artifact_manifest(
            self.component_manifest,
            manifest_path,
            signature=signature,
            trust_root=public,
        )
        qcow, path_info, output = self.files["admin"]

        qcow.write_bytes(qcow.read_bytes() + b"tamper")
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "does not match"):
            release_manifest.verify_role_artifact(
                manifest,
                "admin",
                qcow2=qcow,
                path_info=path_info,
                output_store_path=output,
                publication_metadata=self.inputs["admin"]["publicationMetadata"],
            )
        qcow.write_bytes(qcow.read_bytes()[: -len(b"tamper")])

        original_path_info = path_info.read_bytes()
        closure = json.loads(original_path_info)
        closure[str(output)]["narSize"] += 1
        path_info.write_text(json.dumps(closure))
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "does not match"):
            release_manifest.verify_role_artifact(
                manifest,
                "admin",
                qcow2=qcow,
                path_info=path_info,
                output_store_path=output,
                publication_metadata=self.inputs["admin"]["publicationMetadata"],
            )
        path_info.write_bytes(original_path_info)

        changed_metadata = dict(self.inputs["admin"]["publicationMetadata"])
        changed_metadata["test_role"] = "worker"
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "does not match"):
            release_manifest.verify_role_artifact(
                manifest,
                "admin",
                qcow2=qcow,
                path_info=path_info,
                output_store_path=output,
                publication_metadata=changed_metadata,
            )

        component_bytes = self.component_manifest.read_bytes()
        self.component_manifest.write_bytes(component_bytes + b" ")
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "source component"):
            release_manifest.verify_artifact_manifest(
                self.component_manifest,
                manifest_path,
                signature=signature,
                trust_root=public,
            )
        self.component_manifest.write_bytes(component_bytes)

        signature_bytes = signature.read_bytes()
        signature.write_bytes(bytes([signature_bytes[0] ^ 1]) + signature_bytes[1:])
        with self.assertRaises(release_manifest.ReleaseVerificationError):
            release_manifest.verify_artifact_manifest(
                self.component_manifest,
                manifest_path,
                signature=signature,
                trust_root=public,
            )
        signature.write_bytes(signature_bytes)

        sbom = manifest_path.parent / "role-artifacts.sbom.spdx.json"
        sbom.write_bytes(sbom.read_bytes() + b" ")
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "SBOM|sbom"):
            release_manifest.verify_artifact_manifest(
                self.component_manifest,
                manifest_path,
                signature=signature,
                trust_root=public,
            )

    def test_evidence_bundle_has_exact_safe_inventory(self) -> None:
        source = self.root / "bundle-source"
        (source / "artifacts").mkdir(parents=True)
        for name in release_manifest._BUNDLE_FILES:  # noqa: SLF001 - exact format contract
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"evidence:{name}".encode())
            path.chmod(0o600)
        bundle = release_manifest.create_evidence_bundle(source, self.root / "evidence.tar")
        extracted = release_manifest.extract_evidence_bundle(bundle, self.root / "extracted")
        self.assertEqual(
            {
                path.relative_to(extracted).as_posix()
                for path in extracted.rglob("*")
                if path.is_file()
            },
            set(release_manifest._BUNDLE_FILES),  # noqa: SLF001
        )

        hostile = self.root / "hostile.tar"
        with tarfile.open(hostile, "w") as archive:
            member = tarfile.TarInfo("release-manifest.json")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            archive.addfile(member)
        hostile.chmod(0o600)
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "inventory"):
            release_manifest.extract_evidence_bundle(hostile, self.root / "hostile-output")
        self.assertFalse((self.root / "hostile-output").exists())

    def test_unsigned_artifact_manifest_remains_development_only(self) -> None:
        output = self.root / "unsigned"
        manifest = release_manifest.generate_artifact_manifest(
            self.component_manifest,
            self.inputs_path,
            output,
            signing_key=None,
            unsigned=True,
        )
        with self.assertRaisesRegex(release_manifest.ReleaseVerificationError, "explicit"):
            release_manifest.verify_artifact_manifest(
                self.component_manifest,
                manifest,
                signature=None,
                trust_root=None,
            )
        verified = release_manifest.verify_artifact_manifest(
            self.component_manifest,
            manifest,
            signature=None,
            trust_root=None,
            allow_unsigned_development=True,
        )
        self.assertEqual(verified["releaseChannel"], "development-unsigned")


if __name__ == "__main__":
    unittest.main()
