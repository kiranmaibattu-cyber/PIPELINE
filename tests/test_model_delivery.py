from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

from edge_runtime.graph.models import ModelBundleReference, SharedService, SolutionRuntimePlan
from edge_runtime.model_registry.delivery import ModelBundleResolver, ModelBundleStore
from edge_runtime.model_registry.manager import ModelManager
from edge_runtime.model_registry.registry import ModelBundle, ModelFile, ModelPreparer, ModelRegistry


class CopyDownloader:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls = 0

    def download(self, reference: ModelBundleReference, destination: Path) -> None:
        self.calls += 1
        shutil.copyfile(self.source, destination)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan() -> SolutionRuntimePlan:
    return SolutionRuntimePlan(
        edge_id="edge-1",
        revision=4,
        solution_pack="traffic",
        cameras=(),
        shared_services=(
            SharedService("vehicle_detector", "traffic", "GPU", models=("vehicle_detector",)),
        ),
        status="accepted",
    )


class ModelDeliveryTest(unittest.TestCase):
    def _archive(self, root: Path, payload: bytes = b"openvino-model") -> Path:
        archive = root / "traffic-models.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            info = tarfile.TarInfo("openvino/vehicle.xml")
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
        return archive

    def test_downloads_verifies_caches_and_maps_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._archive(root)
            downloader = CopyDownloader(archive)
            models_root = root / "models"
            reference = ModelBundleReference(
                solution_pack="traffic",
                version="v2",
                url="http://management.test/models/traffic-v2.tar.gz",
                sha256=_sha256(archive),
            )
            store = ModelBundleStore(models_root, downloader)
            installed = store.ensure(reference)
            cached = store.ensure(reference)

            self.assertEqual(1, downloader.calls)
            self.assertEqual("downloaded", installed.source)
            self.assertEqual("cache", cached.source)
            self.assertEqual(b"openvino-model", (installed.path / "openvino/vehicle.xml").read_bytes())

            model_digest = hashlib.sha256(b"openvino-model").hexdigest()
            registry = ModelRegistry((ModelBundle(
                model_id="vehicle_detector",
                solution_pack="traffic",
                version="v2",
                mount_dir="/models/traffic",
                files=(ModelFile("openvino/vehicle.xml", model_digest),),
            ),))
            resolver = ModelBundleResolver(models_root, store)
            manager = ModelManager(
                resolver,
                ModelPreparer(registry, models_root),
                models_root,
                Path("/host/models"),
            )
            plan = _plan()
            plan = SolutionRuntimePlan(**{**plan.__dict__, "cameras": (object(),)})
            prepared = manager.prepare((plan,), (reference,))

            self.assertEqual("ready", prepared.results[0].status)
            self.assertEqual(
                Path("/host/models") / installed.path.relative_to(models_root),
                prepared.runtime_mounts["traffic"],
            )

    def test_rejects_archive_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = self._archive(root)
            store = ModelBundleStore(root / "models", CopyDownloader(archive))
            reference = ModelBundleReference(
                solution_pack="traffic",
                version="v2",
                url="https://management.test/models/traffic-v2.tar.gz",
                sha256="0" * 64,
            )
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                store.ensure(reference)

    def test_rejects_archive_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                info = tarfile.TarInfo("../escaped.xml")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))
            store = ModelBundleStore(root / "models", CopyDownloader(archive))
            reference = ModelBundleReference(
                solution_pack="traffic",
                version="v2",
                url="https://management.test/models/unsafe.tar.gz",
                sha256=_sha256(archive),
            )
            with self.assertRaisesRegex(ValueError, "escapes destination"):
                store.ensure(reference)


if __name__ == "__main__":
    unittest.main()
