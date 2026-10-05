from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time


class FaceMetrics:
    """Small per-camera metrics store shared with the API through /state."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/state")) / "metrics"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"face-{camera_id}.json"
        self.lock = threading.Lock()
        self.data = {
            "camera_id": camera_id,
            "tracks_active": 0,
            "samples_emitted": 0,
            "samples_suppressed": {},
            "submissions": {},
            "submission_latency_seconds": 0.0,
            "snapshot_write_failures": {},
            "outbox_records": 0,
            "outbox_bytes": 0,
            "outbox_drops": {},
        }
        self._save()

    def tracks(self, count: int) -> None:
        self._update("tracks_active", max(0, int(count)))

    def emitted(self) -> None:
        self._increment("samples_emitted")

    def suppressed(self, reason: str) -> None:
        self._increment_map("samples_suppressed", reason)

    def submitted(self, outcome: str, elapsed: float) -> None:
        with self.lock:
            self.data["submissions"][outcome] = self.data["submissions"].get(outcome, 0) + 1
            self.data["submission_latency_seconds"] = max(0.0, float(elapsed))
            self._save_locked()

    def snapshot_failure(self, asset: str) -> None:
        self._increment_map("snapshot_write_failures", asset)

    def outbox(self, records: int, size_bytes: int) -> None:
        with self.lock:
            self.data["outbox_records"] = max(0, int(records))
            self.data["outbox_bytes"] = max(0, int(size_bytes))
            self._save_locked()

    def outbox_drop(self, reason: str) -> None:
        self._increment_map("outbox_drops", reason)

    def _update(self, field: str, value) -> None:
        with self.lock:
            self.data[field] = value
            self._save_locked()

    def _increment(self, field: str) -> None:
        with self.lock:
            self.data[field] += 1
            self._save_locked()

    def _increment_map(self, field: str, key: str) -> None:
        with self.lock:
            self.data[field][key] = self.data[field].get(key, 0) + 1
            self._save_locked()

    def _save(self) -> None:
        with self.lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self.data["updated_at"] = time.time()
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.path)
