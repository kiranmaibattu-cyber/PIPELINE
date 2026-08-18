from __future__ import annotations

import unittest
from pathlib import Path

from edge_runtime.agent.desired_state import DesiredStateLoader
from edge_runtime.graph.graph_builder import CameraGraphBuilder, EdgeGraphBuilder
from edge_runtime.graph.manifest_loader import ManifestRepository


ROOT = Path(__file__).resolve().parents[1]


class GraphBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        manifests = ManifestRepository.from_directory(ROOT / "edge_runtime" / "solution_packs")
        self.builder = EdgeGraphBuilder(CameraGraphBuilder(manifests))
        self.desired = DesiredStateLoader().load(ROOT / "configs" / "desired_state.example.json")

    def test_reid_camera_gets_multimodal_identity_nodes(self) -> None:
        graphs = {g.camera_id: g for g in self.builder.build_camera_graphs(self.desired)}
        cam1 = graphs["cam1"]
        self.assertTrue(cam1.feature_flags["body"])
        self.assertTrue(cam1.feature_flags["face"])
        self.assertTrue(cam1.feature_flags["gait"])
        self.assertTrue(cam1.feature_flags["reid"])
        self.assertIn("gait_segmenter", cam1.nodes)
        self.assertIn("global_reid_service", cam1.nodes)

    def test_intrusion_only_camera_does_not_get_embedding_nodes(self) -> None:
        graphs = {g.camera_id: g for g in self.builder.build_camera_graphs(self.desired)}
        cam3 = graphs["cam3"]
        self.assertTrue(cam3.feature_flags["person_detect"])
        self.assertTrue(cam3.feature_flags["track"])
        self.assertFalse(cam3.feature_flags["body"])
        self.assertFalse(cam3.feature_flags["face"])
        self.assertFalse(cam3.feature_flags["gait"])
        self.assertFalse(cam3.feature_flags["reid"])
        self.assertNotIn("body_embedder", cam3.nodes)
        self.assertNotIn("global_reid_service", cam3.nodes)

    def test_anpr_camera_gets_plate_and_ocr_nodes(self) -> None:
        graphs = {g.camera_id: g for g in self.builder.build_camera_graphs(self.desired)}
        cam4 = graphs["cam4"]
        self.assertTrue(cam4.feature_flags["vehicle_detect"])
        self.assertTrue(cam4.feature_flags["plate"])
        self.assertTrue(cam4.feature_flags["ocr"])
        self.assertIn("plate_detector", cam4.nodes)
        self.assertIn("ocr_service", cam4.nodes)


if __name__ == "__main__":
    unittest.main()
