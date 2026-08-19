"""Resolve platform-managed camera source references inside the runtime container."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class CameraSourceResolver:
    """Resolves camera source references without leaking credentials into images.

    Supported forms:
      - rtsp://... or file path: returned unchanged
      - env:VARIABLE_NAME: value is read from the container environment
      - file:/mounted/secret/path: value is read from a mounted Secret file
      - secret:/mounted/secret/path: alias for file:
    """

    def resolve_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(plan)
        cameras = []
        for camera in plan.get("cameras") or []:
            updated = dict(camera)
            updated["source"] = self.resolve(str(updated.get("source") or ""))
            cameras.append(updated)
        resolved["cameras"] = cameras
        return resolved

    def resolve(self, source: str) -> str:
        if source.startswith("env:"):
            key = source.removeprefix("env:").strip()
            value = os.environ.get(key)
            if not value:
                raise RuntimeError(f"camera source env var is not set: {key}")
            return value
        if source.startswith("file:") or source.startswith("secret:"):
            path = source.split(":", 1)[1].strip()
            value = Path(path).read_text(encoding="utf-8").strip()
            if not value:
                raise RuntimeError(f"camera source secret file is empty: {path}")
            return value
        return source
