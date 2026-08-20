from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edge_runtime.agent.contract_validation import ApexFabricV1DesiredStateValidator
from edge_runtime.graph.manifest_loader import ManifestRepository
from edge_runtime.model_registry.baked import BakedModelValidator
from edge_runtime.model_registry.registry import ModelRegistry


ROOT = Path(__file__).resolve().parents[1]


class ApexFabricV1ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifests = ManifestRepository.from_directory(
            ROOT / "edge_runtime" / "solution_packs"
        )

    def _write_desired(self, directory: Path, source: str, **updates) -> Path:
        data = {
            "edge_id": "edge-test",
            "revision": 1,
            "cameras": [{
                "camera_id": "cam1",
                "source": source,
                "solution_pack": "traffic",
                "fps": 8,
                "apps": ["anpr"],
            }],
        }
        data.update(updates)
        path = directory / "desired.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_validates_secret_without_replacing_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "cam1.rtsp"
            secret.write_text("rtsp://fake-user:fake-pass@camera.test/stream\n", encoding="utf-8")
            desired = self._write_desired(root, f"file:{secret}")
            validator = ApexFabricV1DesiredStateValidator("traffic", self.manifests, root)

            validator.validate(desired)

            loaded = json.loads(desired.read_text(encoding="utf-8"))
            self.assertEqual(f"file:{secret}", loaded["cameras"][0]["source"])

    def test_rejects_plaintext_rtsp_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desired = self._write_desired(root, "rtsp://user:pass@camera/stream")
            validator = ApexFabricV1DesiredStateValidator("traffic", self.manifests, root)

            with self.assertRaisesRegex(ValueError, "mounted Secret"):
                validator.validate(desired)

    def test_rejects_unknown_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desired = self._write_desired(root, "file:/missing", unexpected=True)
            validator = ApexFabricV1DesiredStateValidator("traffic", self.manifests, root)

            with self.assertRaisesRegex(ValueError, "unknown desired-state fields"):
                validator.validate(desired)

    def test_all_baked_intel_models_match_registry(self) -> None:
        registry = ModelRegistry.from_file(
            ROOT / "edge_runtime" / "model_registry" / "models.yaml"
        )
        validator = BakedModelValidator(registry, ROOT / "models")
        validator.validate("surveillance")
        validator.validate("traffic")


if __name__ == "__main__":
    unittest.main()
