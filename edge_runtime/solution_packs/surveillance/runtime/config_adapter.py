"""Generate 8090-compatible config files from a surveillance runtime plan."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class SurveillanceConfigAdapter:
    """Converts the graph plan to the config shape expected by the 8090 runtime."""

    def render(self, plan: dict[str, Any]) -> dict[str, Any]:
        cameras = plan.get("cameras") or []
        streams = []
        usecases = {
            "reid": [],
            "face": [],
            "intrusion": [],
            "loitering": [],
            "counting": [],
            "absence": [],
        }
        zones_by_camera = {}
        camera_features = {}

        for camera in cameras:
            camera_id = str(camera["camera_id"])
            flags = camera.get("feature_flags") or {}
            apps = set(camera.get("apps") or [])
            streams.append({
                "source": camera["source"],
                "camera": camera_id,
                "face": bool(flags.get("face")),
                "gait": bool(flags.get("gait")),
            })
            camera_features[camera_id] = {
                "body": bool(flags.get("body")),
                "face": bool(flags.get("face")),
                "gait": bool(flags.get("gait")),
                "reid": bool(flags.get("reid")),
            }
            if flags.get("reid"):
                usecases["reid"].append(camera_id)
            if "face_recognition" in apps:
                usecases["face"].append(camera_id)
            if "intrusion" in apps:
                usecases["intrusion"].append(camera_id)
            if "people_counting" in apps:
                usecases["counting"].append(camera_id)

            zone_block = self._zone_block(camera.get("config") or {})
            if zone_block["zones"] or zone_block["lines"]:
                zones_by_camera[camera_id] = zone_block

        return {
            "streams": {"streams": streams},
            "runtime_usecases": {
                "usecases": {k: _scope(v, len(cameras)) for k, v in usecases.items()},
                "zones": {
                    "frame": [1920, 1080],
                    "cameras": zones_by_camera,
                },
                "face_groups": {},
                "autocall": {"enabled": False},
                "mapper": {"on": True},
            },
            "camera_features": camera_features,
        }

    def write(self, plan: dict[str, Any], output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        rendered = self.render(plan)
        (output_dir / "streams.generated.yaml").write_text(
            yaml.safe_dump(rendered["streams"], sort_keys=False),
            encoding="utf-8",
        )
        (output_dir / "runtime_usecases.generated.json").write_text(
            json.dumps(rendered["runtime_usecases"], indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (output_dir / "camera_features.generated.json").write_text(
            json.dumps(rendered["camera_features"], indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _zone_block(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        zones = []
        lines = []
        for item in ((config.get("zones") or {}).get("intrusion") or []):
            zones.append({
                "name": str(item.get("name") or "intrusion"),
                "kind": "intrusion",
                "poly": item.get("poly") or [],
            })
        for item in ((config.get("lines") or {}).get("people_counting") or []):
            lines.append({
                "name": str(item.get("name") or "people_count"),
                "a": item.get("a") or [0.0, 0.0],
                "b": item.get("b") or [1.0, 0.0],
                "in_side": item.get("in_side") or "right",
            })
        return {"zones": zones, "lines": lines}


def _scope(camera_ids: list[str], total_cameras: int) -> list[str] | str:
    if total_cameras and len(camera_ids) == total_cameras:
        return "all"
    return sorted(camera_ids)
