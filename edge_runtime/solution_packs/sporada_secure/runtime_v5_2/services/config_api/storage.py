"""JSON-on-disk persistence for cameras + use-case geometry.

Single source of truth. Worker pulls this via GET /api/cameras/processor-config;
UI mutates it via POST/PATCH. File is atomically replaced on every write.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict


_REPO = Path(__file__).resolve().parents[2]   # services/config_api/ -> repo root
DEFAULT_PATH = Path(os.getenv("CAMERAS_FILE", str(_REPO / "config" / "cameras.json")))


class CameraStore:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"cameras": []})

    def read(self) -> Dict[str, Any]:
        with self._lock:
            with self.path.open() as handle:
                return json.load(handle)

    def replace_cameras(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._write(payload)
            return payload

    def update_camera(self, camera_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            data = self.read()
            for camera in data.get("cameras", []):
                if camera.get("camera_id") == camera_id:
                    _deep_merge(camera, patch)
                    self._write(data)
                    return camera
            raise KeyError(camera_id)

    def upsert_geometry(
        self,
        camera_id: str,
        use_case: str,
        kind: str,
        geometry: Dict[str, Any],
    ) -> Dict[str, Any]:
        if kind not in {"line", "zone", "mask"}:
            raise ValueError(f"Unknown geometry kind: {kind}")
        bucket = {"line": "lines", "zone": "zones", "mask": "masks"}[kind]
        with self._lock:
            data = self.read()
            camera = self._find_camera(data, camera_id)
            analytics = camera.setdefault("analytics", {})
            use_case_config = analytics.setdefault(use_case, {"enabled": True, "lines": [], "zones": [], "masks": []})
            use_case_config.setdefault(bucket, [])
            geometry_id = geometry.get("id")
            replaced = False
            if geometry_id:
                for index, existing in enumerate(use_case_config[bucket]):
                    if existing.get("id") == geometry_id:
                        use_case_config[bucket][index] = geometry
                        replaced = True
                        break
            if not replaced:
                use_case_config[bucket].append(geometry)
            self._write(data)
            return geometry

    def delete_geometry(self, camera_id: str, use_case: str, kind: str, geometry_id: str) -> None:
        bucket = {"line": "lines", "zone": "zones", "mask": "masks"}[kind]
        with self._lock:
            data = self.read()
            camera = self._find_camera(data, camera_id)
            use_case_config = (camera.get("analytics") or {}).get(use_case) or {}
            items = use_case_config.get(bucket) or []
            use_case_config[bucket] = [item for item in items if item.get("id") != geometry_id]
            self._write(data)

    @staticmethod
    def _find_camera(data: Dict[str, Any], camera_id: str) -> Dict[str, Any]:
        for camera in data.get("cameras", []):
            if camera.get("camera_id") == camera_id:
                return camera
        raise KeyError(camera_id)

    def _write(self, data: Dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(self.path.parent), delete=False
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, self.path)


STORE = CameraStore()


# --- System settings: global inference backend mode ------------------------
# The whole box runs ONE backend mode (per product decision). Each mode maps to
# a per-stage backend assignment the worker applies at startup; switching mode
# requires a worker restart (models reload on different engines).
SYSTEM_PATH = Path(os.getenv("SYSTEM_FILE", str(_REPO / "run" / "system.json")))
VALID_MODES = ("all_openvino",)
MODE_BACKENDS = {
    "all_openvino": {"vehicle": "openvino", "plate": "openvino", "ocr": "openvino"},
}
MODE_LABELS = {
    "all_openvino": "All-OpenVINO (Intel iGPU/NPU/CPU)",
}


class SystemStore:
    def __init__(self, path: Path = SYSTEM_PATH):
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"backend_mode": os.getenv("DEFAULT_BACKEND_MODE", "all_openvino")})

    def read(self) -> Dict[str, Any]:
        with self._lock:
            with self.path.open() as handle:
                data = json.load(handle)
        if data.get("backend_mode") not in VALID_MODES:
            data["backend_mode"] = "all_openvino"
        return data

    def set_mode(self, mode: str) -> Dict[str, Any]:
        if mode not in VALID_MODES:
            raise ValueError(f"Unknown backend_mode {mode!r}; valid: {VALID_MODES}")
        with self._lock:
            self._write({"backend_mode": mode})
        return {"backend_mode": mode}

    def _write(self, data: Dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(self.path.parent), delete=False
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, self.path)


SYSTEM_STORE = SystemStore()


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
