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

    def test_rejects_wrong_way_without_roi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "cam1.rtsp"
            secret.write_text("rtsp://camera.test/stream\n", encoding="utf-8")
            desired = self._write_desired(
                root,
                f"file:{secret}",
                cameras=[{
                    "camera_id": "cam1",
                    "source": f"file:{secret}",
                    "solution_pack": "traffic",
                    "fps": 8,
                    "apps": ["wrong_way"],
                }],
            )
            validator = ApexFabricV1DesiredStateValidator("traffic", self.manifests, root)

            with self.assertRaisesRegex(ValueError, "requires config.lines.wrong_way"):
                validator.validate(desired)

    def test_accepts_counting_apps_without_roi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "cam1.rtsp"
            secret.write_text("rtsp://camera.test/stream\n", encoding="utf-8")
            desired = self._write_desired(
                root,
                f"file:{secret}",
                cameras=[{
                    "camera_id": "cam1",
                    "source": f"file:{secret}",
                    "solution_pack": "traffic",
                    "fps": 8,
                    "apps": ["vehicle_counting", "pedestrian_counting"],
                }],
            )
            validator = ApexFabricV1DesiredStateValidator("traffic", self.manifests, root)

            validator.validate(desired)

    def test_accepts_traffic_apps_with_roi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / "cam1.rtsp"
            secret.write_text("rtsp://camera.test/stream\n", encoding="utf-8")
            desired = self._write_desired(
                root,
                f"file:{secret}",
                cameras=[{
                    "camera_id": "cam1",
                    "source": f"file:{secret}",
                    "solution_pack": "traffic",
                    "fps": 8,
                    "apps": ["wrong_way", "vehicle_counting", "pedestrian_counting", "illegal_parking"],
                    "config": {
                        "lines": {
                            "wrong_way": [{
                                "name": "wrong_way_line",
                                "a": [0.15, 0.58],
                                "b": [0.85, 0.58],
                                "direction": "a_to_b",
                            }],
                            "vehicle_counting": [{
                                "name": "vehicle_count_line",
                                "a": [0.15, 0.68],
                                "b": [0.85, 0.68],
                            }],
                            "pedestrian_counting": [{
                                "name": "pedestrian_count_line",
                                "a": [0.15, 0.78],
                                "b": [0.85, 0.78],
                            }],
                        },
                        "zones": {
                            "illegal_parking": [{
                                "name": "no_parking",
                                "poly": [[0.15, 0.30], [0.85, 0.30], [0.85, 0.90], [0.15, 0.90]],
                            }],
                        },
                    },
                }],
            )
            validator = ApexFabricV1DesiredStateValidator("traffic", self.manifests, root)

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
