from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .desired_state import DesiredCamera, DesiredState

TRACK_APPS = {
    "vehicle_counting",
    "vehicle_entry_exit_counts",
    "pedestrian_counting",
    "face_recognition",
}
PLATE_APPS = {"plate_detection"}
SMOKE_APPS = {"fire_smoke_detection"}
FACE_APPS = {"face_recognition"}
NODE_CATALOG = {
    "camera_source": {"label": "Camera Source", "kind": "input"},
    "decode": {"label": "Decode", "kind": "runtime"},
    "vehicle_detector": {"label": "Vehicle Detector", "kind": "model", "device": "GPU"},
    "vehicle_tracker": {"label": "Vehicle Tracker", "kind": "runtime"},
    "plate_detector": {"label": "Plate Detector", "kind": "model", "device": "GPU"},
    "ocr_service": {"label": "OCR Service", "kind": "model", "device": "GPU,NPU"},
    "smoke_fire_detector": {"label": "Smoke/Fire Detector", "kind": "model", "device": "GPU"},
    "face_detector": {"label": "Face Detector", "kind": "model", "device": "GPU"},
    "face_alignment": {"label": "Face Alignment", "kind": "runtime"},
    "face_embedder": {"label": "Face Embedder", "kind": "model", "device": "NPU"},
    "face_sample_outbox": {"label": "Face Sample Outbox", "kind": "state"},
    "face_management_uploader": {"label": "Face Management Uploader", "kind": "output"},
    "vehicle_counting": {"label": "Vehicle Counting", "kind": "app"},
    "vehicle_entry_exit_counts": {"label": "Vehicle Entry/Exit Counts", "kind": "app"},
    "vehicle_crossing_evaluator": {"label": "Vehicle Crossing Evaluator", "kind": "runtime"},
    "vehicle_crossing_evidence": {"label": "Vehicle Crossing Evidence", "kind": "output"},
    "vehicle_crossing_outbox": {"label": "Vehicle Crossing Outbox", "kind": "state"},
    "vehicle_crossing_uploader": {"label": "Vehicle Crossing Uploader", "kind": "output"},
    "pedestrian_counting": {"label": "Pedestrian Counting", "kind": "app"},
    "plate_detection": {"label": "Plate Detection", "kind": "app"},
    "fire_smoke_detection": {"label": "Fire/Smoke Alert", "kind": "app"},
    "face_recognition": {"label": "Face Recognition Sample", "kind": "app"},
    "snapshot_storage": {"label": "Snapshot Storage", "kind": "output"},
    "event_sink": {"label": "Event Output", "kind": "output"},
}


@dataclass(frozen=True)
class CameraGraph:
    camera_id: str
    apps: tuple[str, ...]
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, str], ...]
    node_ids: tuple[str, ...]
    edge_ids: tuple[tuple[str, str], ...]
    devices: dict[str, str]
    config_mode: str


@dataclass(frozen=True)
class RuntimePlan:
    edge_id: str
    revision: int
    status: str
    cameras: tuple[CameraGraph, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "revision": self.revision,
            "status": self.status,
            "warnings": list(self.warnings),
            "cameras": [asdict(camera) for camera in self.cameras],
        }


def compile_runtime_plan(desired: DesiredState) -> RuntimePlan:
    cameras = tuple(_camera_graph(camera) for camera in desired.cameras)
    return RuntimePlan(
        edge_id=desired.edge_id,
        revision=desired.revision,
        status="accepted",
        cameras=cameras,
    )


def _camera_graph(camera: DesiredCamera) -> CameraGraph:
    apps = set(camera.apps)
    node_ids = ["camera_source", "decode", "vehicle_detector"]
    edge_ids = [("camera_source", "decode"), ("decode", "vehicle_detector")]
    devices = {"vehicle_detector": "GPU"}

    if apps & TRACK_APPS:
        node_ids.append("vehicle_tracker")
        edge_ids.append(("vehicle_detector", "vehicle_tracker"))

    if apps & PLATE_APPS:
        node_ids.extend(["plate_detector", "ocr_service"])
        edge_ids.extend([
            ("vehicle_detector", "plate_detector"),
            ("plate_detector", "ocr_service"),
        ])
        devices.update({"plate_detector": "GPU", "ocr_service": "GPU,NPU"})

    if apps & SMOKE_APPS:
        node_ids.extend(["smoke_fire_detector", "snapshot_storage"])
        edge_ids.append(("decode", "smoke_fire_detector"))
        devices["smoke_fire_detector"] = "GPU"

    if apps & FACE_APPS:
        node_ids.extend(["face_detector", "face_alignment", "face_embedder",
                         "face_sample_outbox", "face_management_uploader", "snapshot_storage"])
        edge_ids.extend([
            ("vehicle_tracker", "face_detector"),
            ("face_detector", "face_alignment"),
            ("face_alignment", "face_embedder"),
            ("face_embedder", "face_recognition"),
            ("face_recognition", "face_sample_outbox"),
            ("face_sample_outbox", "face_management_uploader"),
            ("face_recognition", "snapshot_storage"),
        ])
        devices.update({"face_detector": "GPU", "face_embedder": "NPU"})

    if "vehicle_entry_exit_counts" in apps:
        node_ids.extend([
            "vehicle_crossing_evaluator", "vehicle_crossing_evidence",
            "vehicle_crossing_outbox", "vehicle_crossing_uploader",
        ])
        edge_ids.extend([
            ("vehicle_tracker", "vehicle_crossing_evaluator"),
            ("vehicle_crossing_evaluator", "vehicle_entry_exit_counts"),
            ("vehicle_entry_exit_counts", "vehicle_crossing_evidence"),
            ("vehicle_crossing_evidence", "vehicle_crossing_outbox"),
            ("vehicle_crossing_outbox", "vehicle_crossing_uploader"),
        ])

    for app in camera.apps:
        node_ids.append(app)
        if app in PLATE_APPS:
            edge_ids.append(("ocr_service", app))
        elif app in SMOKE_APPS:
            edge_ids.append(("smoke_fire_detector", app))
            edge_ids.append((app, "snapshot_storage"))
        elif app in FACE_APPS:
            pass
        elif app in TRACK_APPS:
            edge_ids.append(("vehicle_tracker", app))
        if app != "vehicle_entry_exit_counts":
            edge_ids.append((app, "event_sink"))
    if any(app != "vehicle_entry_exit_counts" for app in camera.apps):
        node_ids.append("event_sink")

    node_ids = tuple(_dedupe(node_ids))
    edge_ids = tuple(_dedupe(edge_ids))
    return CameraGraph(
        camera_id=camera.camera_id,
        apps=camera.apps,
        nodes=tuple(_node_payload(node_id, devices) for node_id in node_ids),
        edges=tuple(_edge_payload(source, target) for source, target in edge_ids),
        node_ids=node_ids,
        edge_ids=edge_ids,
        devices=devices,
        config_mode=_config_mode(camera.config),
    )


def _node_payload(node_id: str, devices: dict[str, str]) -> dict[str, Any]:
    payload = {"id": node_id, **NODE_CATALOG.get(node_id, {"label": node_id, "kind": "app"}), "active": True}
    if node_id in devices:
        payload["device"] = devices[node_id]
    return payload


def _edge_payload(source: str, target: str) -> dict[str, str]:
    return {"source": source, "target": target}


def _config_mode(config: dict[str, Any]) -> str:
    if config.get("counting_lines"):
        return "mixed" if config.get("zones") else "line"
    if config.get("line") or config.get("lines"):
        return "line"
    if config.get("zone") or config.get("zones"):
        return "zone"
    return "whole_frame"


def _dedupe(values):
    out = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        out.append(value)
        seen.add(value)
    return out
