from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time


class VehicleCrossingMetrics:
    """Persist per-camera crossing delivery metrics for the runtime API."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/state")) / "metrics"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"vehicle-crossing-{camera_id}.json"
        self.lock = threading.Lock()
        self.data = self._load()
        self._save()

    def _load(self) -> dict:
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}
        return {
            "camera_id": self.camera_id,
            "outbox_records": int(existing.get("outbox_records", 0)),
            "acknowledgements": int(existing.get("acknowledgements", 0)),
            "submission_failures": int(existing.get("submission_failures", 0)),
        }

    def outbox(self, records: int) -> None:
        with self.lock:
            self.data["outbox_records"] = max(0, int(records))
            self._save_locked()

    def acknowledged(self) -> None:
        self._increment("acknowledgements")

    def failed(self) -> None:
        self._increment("submission_failures")

    def _increment(self, field: str) -> None:
        with self.lock:
            self.data[field] += 1
            self._save_locked()

    def _save(self) -> None:
        with self.lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self.data["updated_at"] = time.time()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.path)
