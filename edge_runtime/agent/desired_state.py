"""Desired-state loading and validation."""
from __future__ import annotations

import json
from pathlib import Path

from edge_runtime.graph.models import CameraDesiredState, DesiredState


class DesiredStateLoader:
    def load(self, path: Path) -> DesiredState:
        data = json.loads(path.read_text(encoding="utf-8"))
        cameras = []
        for cam in data.get("cameras") or []:
            cameras.append(CameraDesiredState(
                camera_id=str(cam["camera_id"]),
                source=str(cam["source"]),
                solution_pack=str(cam["solution_pack"]),
                apps=tuple(str(app) for app in cam.get("apps") or ()),
                fps=float(cam.get("fps", 10.0)),
                config=dict(cam.get("config") or {}),
            ))
        return DesiredState(
            edge_id=str(data.get("edge_id") or "edge-unknown"),
            revision=int(data.get("revision", 1)),
            cameras=tuple(cameras),
            management_url=data.get("management_url"),
        )
