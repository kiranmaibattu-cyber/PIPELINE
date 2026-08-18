"""Edge-agent entrypoint for compiling and applying runtime graph plans."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from edge_runtime.agent.desired_state import DesiredStateLoader
from edge_runtime.graph.graph_builder import CameraGraphBuilder, EdgeGraphBuilder
from edge_runtime.graph.hardware_probe import HardwareProbe
from edge_runtime.graph.manifest_loader import ManifestRepository
from edge_runtime.graph.planner import CapacityPlanner, PlacementPolicy, RuntimePlanner
from edge_runtime.graph.serializer import GraphPlanWriter
from edge_runtime.model_registry.registry import ModelPreparer, ModelRegistry
from edge_runtime.runtime.event_uploader import EventUploader, ManagementEvent
from edge_runtime.runtime.supervisor import RuntimeSupervisor


class EdgeAgent:
    """Coordinates graph compilation without owning lower-level responsibilities."""

    def __init__(
        self,
        desired_loader: DesiredStateLoader,
        manifest_repo: ManifestRepository,
        hardware_probe: HardwareProbe,
        graph_builder: EdgeGraphBuilder,
        planner: RuntimePlanner,
        writer: GraphPlanWriter,
        model_preparer: ModelPreparer,
        supervisor: RuntimeSupervisor,
        uploader: EventUploader,
    ) -> None:
        self._desired_loader = desired_loader
        self._manifest_repo = manifest_repo
        self._hardware_probe = hardware_probe
        self._graph_builder = graph_builder
        self._planner = planner
        self._writer = writer
        self._model_preparer = model_preparer
        self._supervisor = supervisor
        self._uploader = uploader

    def run(self, desired_state_path: Path, output_dir: Path) -> int:
        desired = self._desired_loader.load(desired_state_path)
        hardware = self._hardware_probe.probe(desired.edge_id)
        camera_graphs = self._graph_builder.build_camera_graphs(desired)
        compiled = self._planner.compile(desired, hardware, camera_graphs)
        model_results = self._model_preparer.prepare(compiled.solution_plans)
        self._writer.write(compiled, output_dir)
        supervision = self._supervisor.apply(compiled.solution_plans)
        hardware_payload = {
            "devices": list(hardware.devices),
            "runtimes": list(hardware.runtimes),
            "cpu_cores": hardware.cpu_cores,
            "ram_gb": hardware.ram_gb,
        }
        for plan in compiled.solution_plans:
            plan_models = [
                asdict(result)
                for result in model_results
                if result.solution_pack == plan.solution_pack
            ]
            self._uploader.publish(ManagementEvent(
                edge_id=desired.edge_id,
                revision=desired.revision,
                event_type="graph_compiled",
                payload={
                    "solution_pack": plan.solution_pack,
                    "status": plan.status,
                    "camera_count": len(plan.cameras),
                    "cameras": [camera.camera_id for camera in plan.cameras],
                    "shared_services": [
                        {"name": service.name, "device": service.device}
                        for service in plan.shared_services
                    ],
                    "api_tags": plan.api_tags,
                    "model_bundles": plan_models,
                    "hardware": hardware_payload,
                    "supervision": [
                        result.__dict__
                        for result in supervision
                        if result.solution_pack == plan.solution_pack
                    ],
                },
            ))
        for plan in compiled.solution_plans:
            print(f"{plan.solution_pack}: {plan.status} ({len(plan.cameras)} cameras)")
            for warning in plan.warnings:
                print(f"  warning: {warning}")
        for result in supervision:
            print(f"{result.solution_pack}: {result.action}: {result.detail}")
        print(f"plans written to {output_dir}")
        return 0


def build_agent(args) -> EdgeAgent:
    root = Path(args.root).resolve()
    host_root = Path(args.host_root or args.root).resolve()
    manifests = ManifestRepository.from_directory(root / "edge_runtime" / "solution_packs")
    camera_builder = CameraGraphBuilder(manifests)
    graph_builder = EdgeGraphBuilder(camera_builder)
    planner = RuntimePlanner(PlacementPolicy(manifests), CapacityPlanner())
    output_dir = Path(args.output_dir).resolve()
    return EdgeAgent(
        desired_loader=DesiredStateLoader(),
        manifest_repo=manifests,
        hardware_probe=HardwareProbe(),
        graph_builder=graph_builder,
        planner=planner,
        writer=GraphPlanWriter(),
        model_preparer=ModelPreparer(
            ModelRegistry.from_file(root / "edge_runtime" / "model_registry" / "models.yaml"),
            Path(args.models_root or (host_root / "models")).resolve(),
        ),
        supervisor=RuntimeSupervisor(output_dir, root=host_root, dry_run=not args.apply, engine=args.container_engine),
        uploader=EventUploader(output_dir / "management_outbox.jsonl"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile and apply edge runtime graph")
    parser.add_argument("--root", default="/opt/pipeline", help="PIPELINE project root")
    parser.add_argument("--host-root", help="host-visible PIPELINE root for runtime container volume mounts")
    parser.add_argument("--desired-state", required=True, help="desired_state JSON path")
    parser.add_argument("--output-dir", default="/plans", help="compiled plan output directory")
    parser.add_argument("--models-root", help="host or mounted root containing external model bundles")
    parser.add_argument("--container-engine", default="docker", help="docker-compatible CLI to use in generated commands")
    parser.add_argument("--apply", action="store_true", help="apply plan instead of dry-run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    agent = build_agent(args)
    return agent.run(Path(args.desired_state).resolve(), Path(args.output_dir).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
