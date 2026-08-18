"""Hardware-aware graph planner."""
from __future__ import annotations

from collections import defaultdict

from edge_runtime.graph.manifest_loader import ManifestRepository
from edge_runtime.graph.models import (
    CameraGraph,
    CompiledGraph,
    DesiredState,
    HardwareProfile,
    SharedService,
    SolutionRuntimePlan,
)
from edge_runtime.runtime.api_tags import ApiTagBuilder


SERVICE_MODEL = {
    "person_detector": ("person_detector",),
    "body_embedder": ("body_reid_embedder",),
    "face_embedder": ("face_embedder",),
    "gait_segmenter": ("segmenter",),
    "gait_embedder": ("gait_embedder",),
    "global_reid_service": (),
    "enrolled_face_gallery": (),
    "identity_anchor": (),
    "vehicle_detector": ("vehicle_detector",),
    "plate_detector": ("plate_detector",),
    "ocr_service": ("plate_ocr",),
}

SERVICE_STATE = {
    "global_reid_service": ("reid_gallery", "persistent_identity_store"),
    "enrolled_face_gallery": ("enrolled_face_gallery",),
    "identity_anchor": ("identity_store",),
}

MODEL_SERVICE = {
    "person_detector": "person_detector",
    "body_reid_embedder": "body_embedder",
    "face_embedder": "face_embedder",
    "segmenter": "gait_segmenter",
    "gait_embedder": "gait_embedder",
    "vehicle_detector": "vehicle_detector",
    "plate_detector": "plate_detector",
    "plate_ocr": "ocr_service",
}


class PlacementPolicy:
    """Selects a device for graph services from manifest requirements."""

    def __init__(self, manifests: ManifestRepository) -> None:
        self._manifests = manifests

    def choose_device(self, solution_pack: str, apps: set[str], service: str, hardware: HardwareProfile) -> str:
        if service in SERVICE_STATE and not SERVICE_MODEL.get(service):
            return "CPU"

        preferred = []
        for app in sorted(apps):
            manifest = self._manifests.get(solution_pack, app)
            preferred.extend(_devices_for(service, manifest.preferred_hardware))

        for device in preferred:
            if hardware.has_device(device):
                return device.upper()

        required = ", ".join(dict.fromkeys(d.upper() for d in preferred)) or "no device declared"
        available = ", ".join(hardware.devices) or "none"
        raise RuntimeError(
            f"{solution_pack}/{service} cannot be placed; required {required}, available {available}"
        )


class CapacityPlanner:
    """First-pass capacity guard.

    This is intentionally conservative and replaceable. Later it should be fed by
    measured model latencies per hardware profile.
    """

    def evaluate(self, cameras: tuple[CameraGraph, ...], hardware: HardwareProfile) -> tuple[str, tuple[str, ...]]:
        warnings = []
        status = "accepted"
        heavy_score = 0.0
        for camera in cameras:
            flags = camera.feature_flags
            score = camera.fps
            if flags.get("body"):
                score += camera.fps * 1.5
            if flags.get("face"):
                score += camera.fps * 1.2
            if flags.get("gait"):
                score += camera.fps * 1.0
            if flags.get("plate"):
                score += camera.fps * 1.0
            if flags.get("ocr"):
                score += camera.fps * 0.8
            heavy_score += score

        accelerator_factor = 1.0
        if hardware.has_device("GPU"):
            accelerator_factor += 1.0
        if hardware.has_device("NPU"):
            accelerator_factor += 0.8
        rough_capacity = max(30.0, hardware.cpu_cores * 4.0 * accelerator_factor)
        if heavy_score > rough_capacity:
            status = "accepted_degraded"
            warnings.append(
                f"estimated load {heavy_score:.1f} exceeds rough capacity {rough_capacity:.1f}; runtime should reduce feature cadence"
            )
        if hardware.ram_gb and hardware.ram_gb < 16:
            status = "accepted_degraded"
            warnings.append("RAM below 16GB; prefer lower FPS and fewer embedding apps")
        return status, tuple(warnings)


class RuntimePlanner:
    def __init__(self, placement: PlacementPolicy, capacity: CapacityPlanner) -> None:
        self._placement = placement
        self._capacity = capacity
        self._api_tags = ApiTagBuilder()

    def compile(self, desired: DesiredState, hardware: HardwareProfile, camera_graphs: tuple[CameraGraph, ...]) -> CompiledGraph:
        by_pack: dict[str, list[CameraGraph]] = defaultdict(list)
        for camera in camera_graphs:
            by_pack[camera.solution_pack].append(camera)

        plans = []
        for solution_pack, cameras in sorted(by_pack.items()):
            camera_tuple = tuple(cameras)
            apps = {app for camera in camera_tuple for app in camera.apps}
            services = self._shared_services(
                desired.edge_id,
                desired.revision,
                solution_pack,
                apps,
                camera_tuple,
                hardware,
            )
            status, warnings = self._capacity.evaluate(camera_tuple, hardware)
            plans.append(SolutionRuntimePlan(
                edge_id=desired.edge_id,
                revision=desired.revision,
                solution_pack=solution_pack,
                cameras=camera_tuple,
                shared_services=services,
                status=status,
                warnings=warnings,
                api_tags=self._api_tags.solution_tags(desired.edge_id, desired.revision, solution_pack),
            ))
        return CompiledGraph(desired.edge_id, desired.revision, hardware, tuple(plans))

    def _shared_services(
        self,
        edge_id: str,
        revision: int,
        solution_pack: str,
        apps: set[str],
        cameras: tuple[CameraGraph, ...],
        hardware: HardwareProfile,
    ) -> tuple[SharedService, ...]:
        service_names = set()
        for camera in cameras:
            for node in camera.nodes:
                if node in SERVICE_MODEL or node in SERVICE_STATE:
                    service_names.add(node)
            for model in camera.required_models:
                service = MODEL_SERVICE.get(model)
                if service:
                    service_names.add(service)

        services = []
        for service in sorted(service_names):
            device = self._placement.choose_device(solution_pack, apps, service, hardware)
            services.append(SharedService(
                name=service,
                solution_pack=solution_pack,
                device=device,
                models=SERVICE_MODEL.get(service, ()),
                state=SERVICE_STATE.get(service, ()),
                api_tags=self._api_tags.service_tags(
                    edge_id=edge_id,
                    revision=revision,
                    solution_pack=solution_pack,
                    service=service,
                    device=device,
                ),
            ))
        return tuple(services)


def _devices_for(service: str, mapping: dict[str, str]) -> list[str]:
    devices = []
    if service in mapping:
        devices.append(mapping[service])
    for model in SERVICE_MODEL.get(service, ()):
        if model in mapping:
            devices.append(mapping[model])
    return devices
