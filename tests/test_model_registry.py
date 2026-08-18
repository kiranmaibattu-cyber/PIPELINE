from __future__ import annotations

import unittest
from pathlib import Path

from edge_runtime.agent.desired_state import DesiredStateLoader
from edge_runtime.graph.graph_builder import CameraGraphBuilder, EdgeGraphBuilder
from edge_runtime.graph.manifest_loader import ManifestRepository
from edge_runtime.graph.models import HardwareProfile
from edge_runtime.graph.planner import CapacityPlanner, PlacementPolicy, RuntimePlanner
from edge_runtime.model_registry.registry import ModelPreparer, ModelRegistry


ROOT = Path(__file__).resolve().parents[1]


class ModelRegistryTest(unittest.TestCase):
    def test_required_external_model_bundles_are_ready(self) -> None:
        desired = DesiredStateLoader().load(ROOT / "configs" / "desired_state.example.json")
        manifests = ManifestRepository.from_directory(ROOT / "edge_runtime" / "solution_packs")
        camera_graphs = EdgeGraphBuilder(CameraGraphBuilder(manifests)).build_camera_graphs(desired)
        hardware = HardwareProfile(
            edge_id=desired.edge_id,
            cpu_cores=16,
            ram_gb=32.0,
            devices=("CPU", "GPU", "NPU"),
            runtimes=("openvino", "vaapi_decode"),
        )
        compiled = RuntimePlanner(PlacementPolicy(manifests), CapacityPlanner()).compile(
            desired,
            hardware,
            camera_graphs,
        )
        registry = ModelRegistry.from_file(ROOT / "edge_runtime" / "model_registry" / "models.yaml")
        results = ModelPreparer(registry, ROOT / "models").prepare(compiled.solution_plans)
        ready = {(result.solution_pack, result.model_id): result.status for result in results}

        self.assertEqual("ready", ready[("surveillance", "person_detector")])
        self.assertEqual("ready", ready[("surveillance", "segmenter")])
        self.assertEqual("ready", ready[("surveillance", "face_reid_assets")])
        self.assertEqual("ready", ready[("traffic", "vehicle_detector")])
        self.assertEqual("ready", ready[("traffic", "plate_ocr")])


if __name__ == "__main__":
    unittest.main()
