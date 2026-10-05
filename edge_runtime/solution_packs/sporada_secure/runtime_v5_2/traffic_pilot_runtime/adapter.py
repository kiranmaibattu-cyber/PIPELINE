from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .desired_state import DesiredCamera, DesiredState


def write_worker_config(desired: DesiredState, output_path: Path) -> dict[str, Any]:
    payload = {"cameras": [_camera_to_worker(camera) for camera in desired.cameras]}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(output_path)
    return payload


def _camera_to_worker(camera: DesiredCamera) -> dict[str, Any]:
    uri = Path(camera.source.removeprefix("file:")).read_text(encoding="utf-8").strip()
    v5 = camera.config["v5"]
    zones = {item["id"]: item for item in (v5.get("geometry") or {}).get("zones") or []}
    analytics: dict[str, Any] = {
        "v5_events": {"enabled": True, "v5": v5, "zones": [], "lines": [], "masks": []}
    }
    for app in camera.apps:
        if app == "v5_events":
            continue
        source_app = {
            "plate_detection": "anpr", "fire_smoke_detection": "fire_smoke",
            "face_recognition": "person_identity", "person_reid": "person_identity",
            "scene_embeddings": "scene_search",
        }[app]
        bindings = [item for item in v5["bindings"] if item["app"] == source_app]
        selected_ids = {identifier for binding in bindings for identifier in binding["geometry_ids"]}
        full_frame = any(binding["scope"] == "full_frame" for binding in bindings)
        geometry = [] if full_frame else [
            {"id": zone["id"], "name": zone["name"], "shape": "polygon",
             "points": [{"x": point[0], "y": point[1]} for point in zone["poly"]],
             "normalized": True, "type": {
                 "plate_detection": "plate_roi", "fire_smoke_detection": "fire_smoke",
                 "face_recognition": "face_recognition", "person_reid": "person_reid",
                 "scene_embeddings": "scene_embeddings",
             }[app]}
            for identifier, zone in zones.items() if identifier in selected_ids
        ]
        analytics[app] = {"enabled": True, "zones": geometry, "lines": [], "masks": []}
        if app == "face_recognition":
            analytics[app]["embedding"] = {"model_id": "face-embedding-model-v1", "dimensions": 512}
        if app == "scene_embeddings":
            analytics[app]["embedding"] = {"model_id": "google/siglip2-base-patch16-224",
                                           "model_version": "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2",
                                           "dimensions": 768}
    return {
        "camera_id": camera.camera_id, "name": camera.name or camera.camera_id,
        "enabled": True, "source": {"type": "rtsp" if uri.startswith(("rtsp://", "rtsps://")) else "http", "uri": uri},
        "processing": {"fps": camera.fps}, "analytics": analytics,
    }
