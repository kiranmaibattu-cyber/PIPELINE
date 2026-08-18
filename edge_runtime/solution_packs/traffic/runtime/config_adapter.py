"""Generate Traffic-Pilot-compatible camera config from a traffic runtime plan."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


APP_TO_ANALYTICS = {
    "anpr": "plate_detection",
    "wrong_way": "wrong_way_driving_detection",
    "vehicle_counting": "vehicle_counting",
    "pedestrian_counting": "pedestrian_counting",
    "illegal_parking": "parking_violation_detection",
}


class TrafficConfigAdapter:
    """Converts the graph plan to the config shape expected by Traffic Pilot."""

    def render(self, plan: dict[str, Any]) -> dict[str, Any]:
        cameras = []
        for camera in plan.get("cameras") or []:
            camera_id = str(camera["camera_id"])
            analytics = {}
            for app in camera.get("apps") or []:
                use_case = APP_TO_ANALYTICS.get(app)
                if use_case:
                    analytics[use_case] = self._analytics_config(use_case, camera.get("config") or {})
            cameras.append({
                "camera_id": camera_id,
                "name": camera_id,
                "enabled": True,
                "source": {
                    "type": _source_type(str(camera["source"])),
                    "uri": camera["source"],
                    "fps": camera.get("fps"),
                },
                "processing": {"fps": camera.get("fps")},
                "analytics": analytics,
            })
        return {"cameras": cameras}

    def write(self, plan: dict[str, Any], output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        rendered = self.render(plan)
        (output_dir / "cameras.generated.json").write_text(
            json.dumps(rendered, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _analytics_config(self, use_case: str, config: dict[str, Any]) -> dict[str, Any]:
        block = {"enabled": True, "lines": [], "zones": [], "masks": []}
        if use_case == "plate_detection":
            block["zones"] = [_geometry_zone(z, "plate_roi") for z in _items(config, "zones", "anpr")]
        elif use_case == "wrong_way_driving_detection":
            block["lines"] = [_geometry_line(line, "wrong_way_direction") for line in _items(config, "lines", "wrong_way")]
        elif use_case == "vehicle_counting":
            block["lines"] = [_geometry_line(line, "object_counting") for line in _items(config, "lines", "vehicle_counting")]
        elif use_case == "pedestrian_counting":
            block["lines"] = [_geometry_line(line, "pedestrian_counting") for line in _items(config, "lines", "pedestrian_counting")]
        elif use_case == "parking_violation_detection":
            block["zones"] = [_geometry_zone(z, "no_parking") for z in _items(config, "zones", "illegal_parking")]
        return block


def _source_type(source: str) -> str:
    if source.startswith("rtsp://") or source.startswith("rtsps://"):
        return "rtsp"
    if source.startswith("http://"):
        return "http"
    if source.startswith("https://"):
        return "https"
    return "file"


def _items(config: dict[str, Any], group: str, app: str) -> list[dict[str, Any]]:
    return list(((config.get(group) or {}).get(app) or []))


def _geometry_line(item: dict[str, Any], purpose: str) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "shape": "line",
        "points": _points([item.get("a") or [0, 0], item.get("b") or [1, 0]]),
        "purpose": purpose,
        "direction": item.get("direction"),
    }


def _geometry_zone(item: dict[str, Any], zone_type: str) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "shape": "polygon",
        "points": _points(item.get("poly") or []),
        "type": zone_type,
    }


def _points(points: list) -> list[dict[str, float]]:
    return [{"x": float(point[0]), "y": float(point[1])} for point in points if len(point) >= 2]
