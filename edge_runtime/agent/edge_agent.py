"""Edge-agent entrypoint for compiling and applying runtime graph plans."""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from edge_runtime.agent.contract_validation import ApexFabricV1DesiredStateValidator
from edge_runtime.agent.desired_state import DesiredStateLoader
from edge_runtime.graph.graph_builder import CameraGraphBuilder, EdgeGraphBuilder
from edge_runtime.graph.hardware_probe import HardwareProbe
from edge_runtime.graph.manifest_loader import ManifestRepository
from edge_runtime.graph.planner import CapacityPlanner, PlacementPolicy, RuntimePlanner
from edge_runtime.graph.serializer import GraphPlanWriter
from edge_runtime.graph.models import CompiledGraph, SolutionRuntimePlan
from edge_runtime.model_registry.baked import BakedModelValidator
from edge_runtime.model_registry.delivery import HttpBundleDownloader, ModelBundleResolver, ModelBundleStore
from edge_runtime.model_registry.manager import ModelManager
from edge_runtime.model_registry.registry import ModelPreparer, ModelRegistry
from edge_runtime.runtime.event_uploader import EventUploader, ManagementEvent
from edge_runtime.runtime.api_tags import ApiTagBuilder
from edge_runtime.runtime.supervisor import RuntimeSupervisor


class DesiredStateValidator(Protocol):
    def validate(self, path: Path) -> None:
        """Validate desired state without resolving Secret values."""


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
        model_manager: ModelManager,
        supervisor: RuntimeSupervisor,
        uploader: EventUploader,
        contract_validator: DesiredStateValidator | None = None,
        baked_model_validator: BakedModelValidator | None = None,
        expected_solution_pack: str | None = None,
        compile_only: bool = False,
    ) -> None:
        self._desired_loader = desired_loader
        self._manifest_repo = manifest_repo
        self._hardware_probe = hardware_probe
        self._graph_builder = graph_builder
        self._planner = planner
        self._writer = writer
        self._model_manager = model_manager
        self._supervisor = supervisor
        self._uploader = uploader
        self._contract_validator = contract_validator
        self._baked_model_validator = baked_model_validator
        self._expected_solution_pack = expected_solution_pack
        self._compile_only = compile_only

    def run(self, desired_state_path: Path, output_dir: Path) -> int:
        if self._contract_validator:
            self._contract_validator.validate(desired_state_path)
        desired = self._desired_loader.load(desired_state_path)
        if self._baked_model_validator and self._expected_solution_pack:
            self._baked_model_validator.validate(self._expected_solution_pack)
        hardware = self._hardware_probe.probe(desired.edge_id)
        camera_graphs = self._graph_builder.build_camera_graphs(desired)
        compiled = self._planner.compile(desired, hardware, camera_graphs)
        compiled = self._with_empty_solution_plan(compiled)
        try:
            prepared_models = self._model_manager.prepare(
                compiled.solution_plans,
                desired.model_bundles,
            )
        except (FileNotFoundError, OSError, KeyError, ValueError) as exc:
            referenced_packs = sorted(
                {reference.solution_pack for reference in desired.model_bundles}
                or {plan.solution_pack for plan in compiled.solution_plans}
            )
            for solution_pack in referenced_packs:
                self._uploader.publish(ManagementEvent(
                    edge_id=desired.edge_id,
                    revision=desired.revision,
                    event_type="model_delivery_failed",
                    payload={
                        "solution_pack": solution_pack,
                        "status": "failed",
                        "reason": str(exc),
                    },
                ))
            print(f"model delivery failed: {exc}")
            return 2
        self._writer.write(compiled, output_dir)
        supervision = () if self._compile_only else self._supervisor.apply(
            compiled.solution_plans, prepared_models.runtime_mounts
        )
        hardware_payload = {
            "devices": list(hardware.devices),
            "runtimes": list(hardware.runtimes),
            "cpu_cores": hardware.cpu_cores,
            "ram_gb": hardware.ram_gb,
        }
        for plan in compiled.solution_plans:
            plan_models = [
                asdict(result)
                for result in prepared_models.results
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
                    "model_delivery": prepared_models.deliveries.get(plan.solution_pack, {}),
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

    def _with_empty_solution_plan(self, compiled: CompiledGraph) -> CompiledGraph:
        expected = self._expected_solution_pack
        if not expected or any(plan.solution_pack == expected for plan in compiled.solution_plans):
            return compiled
        empty = SolutionRuntimePlan(
            edge_id=compiled.edge_id,
            revision=compiled.revision,
            solution_pack=expected,
            cameras=(),
            shared_services=(),
            status="accepted",
            api_tags=ApiTagBuilder().solution_tags(compiled.edge_id, compiled.revision, expected),
        )
        return CompiledGraph(
            edge_id=compiled.edge_id,
            revision=compiled.revision,
            hardware=compiled.hardware,
            solution_plans=compiled.solution_plans + (empty,),
        )


def build_agent(args) -> EdgeAgent:
    root = Path(args.root).resolve()
    host_root = Path(args.host_root or args.root).resolve()
    manifests = ManifestRepository.from_directory(root / "edge_runtime" / "solution_packs")
    camera_builder = CameraGraphBuilder(manifests)
    graph_builder = EdgeGraphBuilder(camera_builder)
    planner = RuntimePlanner(PlacementPolicy(manifests), CapacityPlanner())
    output_dir = Path(args.output_dir).resolve()
    local_models_root = Path(args.models_root or (host_root / "models")).resolve()
    host_models_root = Path(args.host_models_root or (host_root / "models")).resolve()
    registry = ModelRegistry.from_file(root / "edge_runtime" / "model_registry" / "models.yaml")
    model_resolver = ModelBundleResolver(
        local_models_root,
        ModelBundleStore(local_models_root, HttpBundleDownloader()),
    )
    expected_solution_pack = args.solution_pack
    contract_validator = None
    baked_model_validator = None
    if args.apexfabric_v1:
        if expected_solution_pack not in {"surveillance", "traffic"}:
            raise ValueError("ApexFabric V1 requires SOLUTION_PACK=surveillance or traffic")
        contract_validator = ApexFabricV1DesiredStateValidator(
            expected_solution_pack,
            manifests,
            Path(args.secrets_root),
        )
        baked_model_validator = BakedModelValidator(registry, local_models_root)
    return EdgeAgent(
        desired_loader=DesiredStateLoader(),
        manifest_repo=manifests,
        hardware_probe=HardwareProbe(),
        graph_builder=graph_builder,
        planner=planner,
        writer=GraphPlanWriter(),
        model_manager=ModelManager(
            resolver=model_resolver,
            verifier=ModelPreparer(registry, local_models_root),
            local_models_root=local_models_root,
            host_models_root=host_models_root,
        ),
        supervisor=RuntimeSupervisor(output_dir, root=host_root, dry_run=not args.apply, engine=args.container_engine),
        uploader=EventUploader(output_dir / "management_outbox.jsonl"),
        contract_validator=contract_validator,
        baked_model_validator=baked_model_validator,
        expected_solution_pack=expected_solution_pack,
        compile_only=args.compile_only,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile and apply edge runtime graph")
    parser.add_argument("--root", default="/opt/pipeline", help="PIPELINE project root")
    parser.add_argument("--host-root", help="host-visible PIPELINE root for runtime container volume mounts")
    parser.add_argument("--desired-state", required=True, help="desired_state JSON path")
    parser.add_argument("--output-dir", default="/plans", help="compiled plan output directory")
    parser.add_argument("--models-root", help="host or mounted root containing external model bundles")
    parser.add_argument(
        "--host-models-root",
        help="same model root as seen by the host container engine (defaults to HOST_ROOT/models)",
    )
    parser.add_argument("--container-engine", default="docker", help="docker-compatible CLI to use in generated commands")
    parser.add_argument("--apply", action="store_true", help="apply plan instead of dry-run")
    parser.add_argument("--solution-pack", default=os.getenv("SOLUTION_PACK"))
    parser.add_argument(
        "--secrets-root",
        default=os.getenv("APEXFABRIC_SECRETS_ROOT", "/run/secrets/apexfabric"),
    )
    parser.add_argument(
        "--apexfabric-v1",
        action="store_true",
        default=os.getenv("APEXFABRIC_CONTRACT_VERSION") == "v1",
    )
    parser.add_argument(
        "--compile-only",
        action="store_true",
        default=os.getenv("EDGE_AGENT_COMPILE_ONLY") == "1",
        help="write plans without generating or invoking solution-container commands",
    )
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        agent = build_agent(args)
        return agent.run(Path(args.desired_state).resolve(), Path(args.output_dir).resolve())
    except (FileNotFoundError, OSError, KeyError, RuntimeError, ValueError) as exc:
        print(f"edge graph compilation failed: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
