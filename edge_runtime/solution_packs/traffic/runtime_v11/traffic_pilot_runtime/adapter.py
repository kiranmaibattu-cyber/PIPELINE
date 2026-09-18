from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .desired_state import DesiredCamera, DesiredState

ZONE_TYPE = {
    "vehicle_counting": "vehicle_counting",
    "pedestrian_counting": "pedestrian_counting",
    "plate_detection": "plate_roi",
    "fire_smoke_detection": "fire_smoke",
    "face_recognition": "face_recognition",
}
ZONE_CONFIG_KEY = {
    "vehicle_counting": "vehicle_counting",
    "pedestrian_counting": "pedestrian_counting",
    "plate_detection": "anpr",
    "fire_smoke_detection": "fire_smoke_detection",
    "face_recognition": "face_recognition",
}


def write_worker_config(desired: DesiredState, output_path: Path) -> dict[str, Any]:
    payload = {"cameras": [_camera_to_worker(camera) for camera in desired.cameras]}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(output_path)
    return payload


def _camera_to_worker(camera: DesiredCamera) -> dict[str, Any]:
    uri = Path(camera.source.removeprefix("file:")).read_text(encoding="utf-8").strip()
    analytics = {app: _use_case_config(app, camera.config) for app in camera.apps}
    return {
        "camera_id": camera.camera_id,
        "name": camera.name or camera.camera_id,
        "enabled": True,
        "source": {"type": _source_type(uri), "uri": uri},
        "processing": {"fps": camera.fps},
        "analytics": analytics,
    }


def _use_case_config(app: str, config: dict[str, Any]) -> dict[str, Any]:
    zones = _items_for(config, app)
    return {
        "enabled": True,
        "lines": [],
        "zones": [_zone_geometry(item, app) for item in zones],
        "masks": [],
    }


def _items_for(config: dict[str, Any], app: str) -> list[dict[str, Any]]:
    zones = config.get("zones") or {}
    if not isinstance(zones, dict):
        return []
    key = ZONE_CONFIG_KEY.get(app, app)
    items = zones.get(key) or []
    return items if isinstance(items, list) else []


def _zone_geometry(item: dict[str, Any], app: str) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name") or "zone_1",
        "shape": "polygon",
        "points": _point_dicts(_zone_points(item)),
        "type": ZONE_TYPE.get(app),
        "normalized": True,
    }


def _zone_points(item: dict[str, Any]) -> list[Any]:
    return item.get("poly") or []


def _point_dicts(points) -> list[dict[str, float]]:
    out = []
    for point in points:
        if isinstance(point, dict):
            out.append({"x": float(point.get("x", 0)), "y": float(point.get("y", 0))})
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            out.append({"x": float(point[0]), "y": float(point[1])})
    return out


def _source_type(uri: str) -> str:
    lowered = uri.lower()
    if lowered.startswith(("rtsp://", "rtsps://")):
        return "rtsp"
    if lowered.startswith("http://"):
        return "http"
    if lowered.startswith("https://"):
        return "https"
    return "file"
