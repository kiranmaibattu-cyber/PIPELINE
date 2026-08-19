from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from edge_runtime.agent.desired_state import DesiredStateLoader
from edge_runtime.graph.graph_builder import CameraGraphBuilder, EdgeGraphBuilder
from edge_runtime.graph.manifest_loader import ManifestRepository
from edge_runtime.graph.models import HardwareProfile
from edge_runtime.graph.planner import CapacityPlanner, PlacementPolicy, RuntimePlanner
from edge_runtime.runtime.container_commands import ContainerCommandBuilder
from edge_runtime.runtime.supervisor import RuntimeSupervisor


ROOT = Path(__file__).resolve().parents[1]


def _plans():
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
    return compiled.solution_plans


class RuntimeSupervisorTest(unittest.TestCase):
    def test_runtime_plan_preserves_pipeline_device_placement(self) -> None:
        plans = {plan.solution_pack: plan for plan in _plans()}
        surveillance = {service.name: service.device for service in plans["surveillance"].shared_services}
        traffic = {service.name: service.device for service in plans["traffic"].shared_services}

        self.assertEqual("GPU", surveillance["person_detector"])
        self.assertEqual("NPU", surveillance["body_embedder"])
        self.assertEqual("GPU", surveillance["face_embedder"])
        self.assertEqual("GPU", surveillance["gait_segmenter"])
        self.assertEqual("NPU", surveillance["gait_embedder"])

        self.assertEqual("GPU", traffic["vehicle_detector"])
        self.assertEqual("NPU", traffic["plate_detector"])
        self.assertEqual("GPU", traffic["ocr_service"])

    def test_container_command_has_edge_runtime_contract(self) -> None:
        plans = {plan.solution_pack: plan for plan in _plans()}
        command = ContainerCommandBuilder(ROOT, engine="docker").build(plans["surveillance"])
        self.assertIn("-d", command.command)
        self.assertIn("--network=host", command.command)
        self.assertIn("pipeline-surveillance", command.command)
        self.assertIn("surveillance-edge-runtime:intel-285h", command.command)
        self.assertNotIn("/usr/lib/x86_64-linux-gnu/libze_intel_npu.so", command.command)
        self.assertIn(f"{ROOT / 'run' / 'plans'}:/plans", command.command)
        self.assertIn(f"{ROOT / 'models' / 'surveillance'}:/models/surveillance:ro", command.command)
        self.assertIn(f"{ROOT / 'state' / 'surveillance'}:/state/surveillance", command.command)

    def test_container_command_mounts_resolved_bundle_read_only(self) -> None:
        plan = {plan.solution_pack: plan for plan in _plans()}["traffic"]
        bundle = Path("/var/lib/pipeline/models/.bundles/traffic/v2-deadbeef")
        command = ContainerCommandBuilder(ROOT, engine="docker").build(plan, bundle)
        self.assertIn(f"{bundle}:/models/traffic:ro", command.command)

    def test_supervisor_dry_run_restarts_wanted_solution_packs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = RuntimeSupervisor(
                Path(tmp),
                root=ROOT,
                dry_run=True,
                engine="docker",
            ).apply(_plans())
        actions = {(result.solution_pack, result.action) for result in results}
        self.assertIn(("surveillance", "dry_run_restart"), actions)
        self.assertIn(("surveillance", "dry_run_start"), actions)
        self.assertIn(("traffic", "dry_run_restart"), actions)
        self.assertIn(("traffic", "dry_run_start"), actions)


if __name__ == "__main__":
    unittest.main()
