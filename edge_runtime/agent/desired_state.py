"""Desired-state loading and validation."""
from __future__ import annotations

import json
from pathlib import Path

from edge_runtime.graph.models import CameraDesiredState, DesiredState, ModelBundleReference


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
        model_bundles = tuple(
            ModelBundleReference(
                solution_pack=str(solution_pack),
                version=str(spec["version"]),
                url=str(spec["url"]),
                sha256=str(spec["sha256"]).lower(),
                archive_format=str(spec.get("archive_format", "tar.gz")),
                auth_token_env=(
                    str(spec["auth_token_env"])
                    if spec.get("auth_token_env")
                    else None
                ),
            )
            for solution_pack, spec in (data.get("model_bundles") or {}).items()
        )
        return DesiredState(
            edge_id=str(data.get("edge_id") or "edge-unknown"),
            revision=int(data.get("revision", 1)),
            cameras=tuple(cameras),
            management_url=data.get("management_url"),
            model_bundles=model_bundles,
        )
