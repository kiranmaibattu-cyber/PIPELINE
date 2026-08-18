"""Manual investigation status for alert-history rows."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class InvestigationStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path(__file__).resolve().parent / "manual_investigations.json"
        self._lock = threading.RLock()
        self._data = self._load()

    def mark(self, event_id: int, note: str = "Marked investigated in the dashboard") -> dict[str, Any]:
        row = {
            "status": "investigated",
            "note": str(note or "Marked investigated in the dashboard"),
            "wall": round(time.time(), 3),
            "source": "manual",
        }
        with self._lock:
            self._data[str(int(event_id))] = row
            self._save()
        return row

    def annotate(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not events:
            return events
        with self._lock:
            data = dict(self._data)
        for event in events:
            manual = data.get(str(event.get("id")))
            if not manual:
                continue
            payload = event.setdefault("payload", {})
            payload["manual_status"] = manual.get("status", "investigated")
            payload["manual_note"] = manual.get("note", "")
            payload["manual_wall"] = manual.get("wall")
            if not payload.get("autocall_status"):
                payload["autocall_status"] = manual.get("status", "investigated")
                payload["autocall_note"] = manual.get("note", "")
        return events

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
