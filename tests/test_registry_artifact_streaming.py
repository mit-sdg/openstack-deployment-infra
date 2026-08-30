from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from typing import BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from backup import registry_artifact as artifact  # noqa: E402

MIB = 1024 * 1024
MEDIA = "application/vnd.oci.image.manifest.v1+json"


class PatternStream:
    def __init__(self, size: int, pattern: bytes = b"x") -> None:
        self.remaining = size
        self.pattern = pattern
        self.closed = False
        self.maximum_read = 0

    def read(self, amount: int = -1) -> bytes:
        if self.remaining == 0:
            return b""
        selected = self.remaining if amount < 0 else min(amount, self.remaining)
        self.maximum_read = max(self.maximum_read, selected)
        repeats = (selected + len(self.pattern) - 1) // len(self.pattern)
        payload = (self.pattern * repeats)[:selected]
        self.remaining -= selected
        return payload

    def close(self) -> None:
        self.closed = True


def pattern_digest(size: int) -> str:
    stream = PatternStream(size)
    digest = hashlib.sha256()
    while payload := stream.read(MIB):
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


class FakeExportRegistry:
    def __init__(self, size: int) -> None:
        self.size = size
        self.layer_digest = pattern_digest(size)
        self.manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": MEDIA,
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": self.layer_digest,
                        "size": size,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.opened_streams: list[PatternStream] = []

    def paginated(self, path: str, field: str) -> list[object]:
        if field == "repositories":
            return ["projects/demo/app"]
        return ["build-accepted"]

    def get_manifest(
        self, repository: str, reference: str, *, expected_size: int | None = None
    ) -> tuple[bytes, str]:
        self.assert_identity(repository)
        if expected_size is not None:
            assert expected_size == len(self.manifest)
        return self.manifest, MEDIA

    def open_blob(
        self, repository: str, digest: str, size: int
    ) -> contextlib.AbstractContextManager[BinaryIO]:
        self.assert_identity(repository)
        assert digest == self.layer_digest and size == self.size
        stream = PatternStream(size)
        self.opened_streams.append(stream)
        return contextlib.closing(stream)  # type: ignore[arg-type,return-value]

    @staticmethod
    def assert_identity(repository: str) -> None:
        assert repository == "projects/demo/app"

    def upload_blob(self, repository: str, digest: str, path: Path, size: int) -> None:
        raise AssertionError("export must not upload")

    def put_manifest(
        self, repository: str, reference: str, path: Path, size: int, media_type: str
    ) -> None:
        raise AssertionError("export must not import manifests")


class FakeImportRegistry:
    def __init__(self) -> None:
        self.uploaded_bytes = 0
        self.maximum_chunk = 0
        self.manifests: list[str] = []

    def paginated(self, path: str, field: str) -> list[object]:
        raise AssertionError("import must use only the archive")

    def get_manifest(
        self, repository: str, reference: str, *, expected_size: int | None = None
    ) -> tuple[bytes, str]:
        raise AssertionError("import must use only the archive")

    def open_blob(
        self, repository: str, digest: str, size: int
    ) -> contextlib.AbstractContextManager[BinaryIO]:
        raise AssertionError("import must use only the archive")

    def upload_blob(self, repository: str, digest: str, path: Path, size: int) -> None:
        observed = hashlib.sha256()
        count = 0
        with path.open("rb") as handle:
            while payload := handle.read(MIB):
                self.maximum_chunk = max(self.maximum_chunk, len(payload))
                count += len(payload)
                observed.update(payload)
        assert count == size
        assert "sha256:" + observed.hexdigest() == digest
        self.uploaded_bytes += count

    def put_manifest(
        self, repository: str, reference: str, path: Path, size: int, media_type: str
    ) -> None:
        assert path.stat().st_size == size
        assert media_type == MEDIA
        self.manifests.append(reference)


class RegistryArtifactStreamingTests(unittest.TestCase):
    def test_large_layer_export_and_import_have_bounded_memory(self) -> None:
        size = 80 * MIB
        bounds = artifact.Bounds(1024**4, 4 * 1024**4, 64 * MIB)
        exporter = FakeExportRegistry(size)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "registry.tar.gz"
            tracemalloc.start()
            with archive_path.open("wb") as output:
                index = artifact.export_archive(exporter, output=output, bounds=bounds)
            _, export_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertEqual(len(index["blobs"]), 2)
            self.assertEqual(len(exporter.opened_streams), 1)
            self.assertTrue(exporter.opened_streams[0].closed)
            self.assertLessEqual(exporter.opened_streams[0].maximum_read, MIB)
            self.assertLess(export_peak, 24 * MIB)
            self.assertLess(export_peak * 2, size)

            importer = FakeImportRegistry()
            spool = root / "spool"
            spool.mkdir(mode=0o700)
            tracemalloc.start()
            with archive_path.open("rb") as source:
                restored = artifact.process_archive(
                    source=source,
                    registry=importer,
                    bounds=bounds,
                    spool_root=spool,
                )
            _, import_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            self.assertEqual(restored["format"], artifact.FORMAT)
            self.assertEqual(importer.uploaded_bytes, size)
            self.assertLessEqual(importer.maximum_chunk, MIB)
            self.assertEqual(len(importer.manifests), 2)  # digest, then retained tag
            self.assertEqual(list(spool.iterdir()), [])
            self.assertLess(import_peak, 48 * MIB)
            self.assertLess(import_peak * 2, size)

    def test_registry_content_length_is_required_and_must_match_descriptor(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Content-Length"):
            artifact.Registry._content_length({}, maximum=1024)
        with self.assertRaisesRegex(RuntimeError, "expected bound"):
            artifact.Registry._content_length(
                {"Content-Length": "1023"}, maximum=2048, expected=1024
            )

    def test_nix_service_sets_production_streaming_bounds(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "nix/roles/admin.nix").read_text()
        self.assertIn('"REGISTRY_BACKUP_MAX_FILE_BYTES=1099511627776"', source)
        self.assertIn('"REGISTRY_BACKUP_MAX_TOTAL_BYTES=4398046511104"', source)
        self.assertIn('"REGISTRY_BACKUP_MAX_MANIFEST_BYTES=67108864"', source)

    def test_descriptor_without_size_is_refused_before_blob_read(self) -> None:
        bounds = artifact.Bounds(1024**4, 4 * 1024**4, 64 * MIB)
        registry = FakeExportRegistry(MIB)
        document = json.loads(registry.manifest)
        del document["layers"][0]["size"]
        registry.manifest = json.dumps(document).encode()
        with tempfile.TemporaryFile() as output:
            with self.assertRaisesRegex(RuntimeError, "digest and size"):
                artifact.export_archive(registry, output=output, bounds=bounds)
        self.assertEqual(registry.opened_streams, [])


if __name__ == "__main__":
    unittest.main()
