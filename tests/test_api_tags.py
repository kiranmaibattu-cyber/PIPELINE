from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edge_runtime.agent.desired_state import DesiredStateLoader
from edge_runtime.graph.graph_builder import CameraGraphBuilder, EdgeGraphBuilder
from edge_runtime.graph.manifest_loader import ManifestRepository
from edge_runtime.graph.models import HardwareProfile
from edge_runtime.graph.planner import CapacityPlanner, PlacementPolicy, RuntimePlanner
from edge_runtime.graph.serializer import GraphPlanWriter
from edge_runtime.runtime.api_tags import OUTPUT_EVENT, STATUS_GRAPH
from edge_runtime.runtime.event_uploader import EventUploader, ManagementEvent


ROOT = Path(__file__).resolve().parents[1]


def _compiled_graph():
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
    return RuntimePlanner(PlacementPolicy(manifests), CapacityPlanner()).compile(
        desired,
        hardware,
        camera_graphs,
    )


class ApiTagsTest(unittest.TestCase):
    def test_camera_app_and_service_tags_are_written_for_management(self) -> None:
        compiled = _compiled_graph()
        with tempfile.TemporaryDirectory() as tmp:
            GraphPlanWriter().write(compiled, Path(tmp))
            contract = json.loads((Path(tmp) / "management_api_tags.json").read_text())

        surveillance = contract["solution_packs"]["surveillance"]
        self.assertEqual("edge-api/v1", contract["api_version"])
        self.assertEqual(STATUS_GRAPH, surveillance["solution"]["graph_status"]["tag"])
        self.assertEqual(OUTPUT_EVENT, surveillance["cameras"]["cam1"]["apps"]["reid"]["tag"])
        self.assertEqual("GPU", surveillance["services"]["person_detector"]["metrics"]["device"])
        self.assertEqual("NPU", surveillance["services"]["body_embedder"]["metrics"]["device"])

    def test_outbox_uses_management_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "management_outbox.jsonl"
            EventUploader(outbox).publish(ManagementEvent(
                edge_id="edge-box-01",
                revision=7,
                event_type="graph_compiled",
                payload={"solution_pack": "surveillance", "status": "accepted"},
            ))
            row = json.loads(outbox.read_text().splitlines()[0])

        self.assertEqual("edge-api/v1", row["api_version"])
        self.assertEqual("edge-box-01", row["edge_id"])
        self.assertEqual("surveillance", row["solution_pack"])
        self.assertEqual(STATUS_GRAPH, row["tag"])
        self.assertIn("timestamp_utc", row)


if __name__ == "__main__":
    unittest.main()
