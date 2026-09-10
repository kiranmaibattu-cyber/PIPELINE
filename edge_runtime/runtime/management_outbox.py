"""Write solution-pack analytics into the mounted management event contract."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ManagementEventWriter:
    def __init__(self, state_dir: Path, solution_pack: str) -> None:
        self._solution_pack = solution_pack
        self._path = state_dir / "events.jsonl"
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: dict[str, Any]) -> None:
        row = dict(event)
        row.setdefault("solution_pack", self._solution_pack)
        row.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
        row.setdefault("event_id", str(uuid.uuid4()))
        row.setdefault("runtime_session_id", os.getenv("EDGE_RUNTIME_SESSION_ID", "unknown"))
        if os.getenv("MANAGEMENT_SYNC_ROOT"):
            from edge_runtime.runtime.sync_store import SyncStore
            SyncStore(os.environ["MANAGEMENT_SYNC_ROOT"]).enqueue(
                "event:" + row["event_id"], "event", row)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
