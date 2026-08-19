from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2


class LocalManagementEventSink:
    """Writes Traffic Pilot analytics events and snapshots to /state/traffic."""

    def __init__(self, state_dir: str | Path, solution_pack: str = "traffic") -> None:
        self.state_dir = Path(state_dir)
        self.solution_pack = solution_pack
        self.events_path = self.state_dir / "events.jsonl"
        self.snapshot_dir = self.state_dir / "snapshots"
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "LocalManagementEventSink | None":
        state_dir = os.getenv("MANAGEMENT_STATE_DIR") or os.getenv("STATE_DIR")
        if not state_dir:
            return None
        return cls(state_dir)

    def publish_packet(self, packet, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        for index, event in enumerate(events):
            row = dict(event)
            row.setdefault("solution_pack", self.solution_pack)
            row.setdefault("camera_id", (row.get("camera") or {}).get("id") or packet.name)
            row.setdefault("app_id", row.get("use_case"))
            row["event_type"] = row.get("event_type") or row.get("type")
            row.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
            snapshot_ref = self._save_snapshot(packet, row, index)
            if snapshot_ref:
                row["snapshot_ref"] = snapshot_ref
            self._write(row)

    def _save_snapshot(self, packet, event: dict[str, Any], index: int) -> str | None:
        if packet.frame is None:
            return None
        observed = str(event.get("observed_at") or event.get("timestamp") or datetime.now(timezone.utc).isoformat())
        safe_ts = "".join(ch if ch.isalnum() else "_" for ch in observed)[:40]
        safe_type = "".join(ch if ch.isalnum() else "_" for ch in str(event.get("event_type") or "event"))[:40]
        filename = f"{packet.name}_{packet.index}_{safe_type}_{index}_{safe_ts}.jpg"
        rel = Path("snapshots") / filename
        path = self.state_dir / rel
        ok = cv2.imwrite(str(path), packet.frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return str(rel) if ok else None

    def _write(self, row: dict[str, Any]) -> None:
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
