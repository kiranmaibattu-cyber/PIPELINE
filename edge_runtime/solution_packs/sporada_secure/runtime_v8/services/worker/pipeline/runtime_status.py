"""Atomic per-camera decoder and process-supervisor status snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_camera_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:160] or "camera"


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8"
    )
    os.replace(temporary, path)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class DecoderRuntimeStatus:
    """Status that remains useful while a decoder has no frames to report."""

    def __init__(self, camera_id: str, stream_session_id: str) -> None:
        self.camera_id = str(camera_id)
        self.stream_session_id = str(stream_session_id)
        root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp/sentinel/state")) / "metrics"
        self.path = root / f"decoder-{safe_camera_name(self.camera_id)}.json"
        previous = _read(self.path)
        self.lock = threading.Lock()
        self.payload: dict[str, Any] = {
            "schema_version": "1.0",
            "camera_id": self.camera_id,
            "stream_session_id": self.stream_session_id,
            "pid": os.getpid(),
            "decoder_pid": None,
            "state": "starting",
            "reason": "decoder_starting",
            "observed_at": utc_now(),
            "last_frame_at": None,
            "decoder_starts": int(previous.get("decoder_starts", 0)),
            "reconnects": int(previous.get("reconnects", 0)),
            "decoder_errors": int(previous.get("decoder_errors", 0)),
            "last_ffmpeg_exit_code": None,
            "retry_reason": None,
            "retry_backoff_seconds": 0.0,
            "sanitized_stderr": None,
        }
        self._last_frame_write = 0.0
        self.write()

    def set_session(self, stream_session_id: str) -> None:
        with self.lock:
            self.stream_session_id = str(stream_session_id)
            self.payload["stream_session_id"] = self.stream_session_id
        self.write()

    def update(self, **fields: Any) -> None:
        with self.lock:
            self.payload.update(fields)
        self.write()

    def increment(self, field: str, value: int = 1, **fields: Any) -> None:
        with self.lock:
            self.payload[field] = int(self.payload.get(field, 0)) + int(value)
            self.payload.update(fields)
        self.write()

    def frame(self, observed_at: float | None = None) -> None:
        now = time.time() if observed_at is None else float(observed_at)
        with self.lock:
            self.payload.update(
                state="healthy", reason="none", last_frame_at=datetime.fromtimestamp(
                    now, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                retry_reason=None, retry_backoff_seconds=0.0,
            )
            due = time.monotonic() - self._last_frame_write >= 1.0
            if due:
                self._last_frame_write = time.monotonic()
        if due:
            self.write()

    def write(self) -> None:
        with self.lock:
            payload = dict(self.payload)
            payload["observed_at"] = utc_now()
        atomic_json(self.path, payload)


def write_supervisor_status(camera_id: str, **fields: Any) -> None:
    root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp/sentinel/state")) / "metrics"
    path = root / f"camera-supervisor-{safe_camera_name(camera_id)}.json"
    payload = _read(path)
    payload.update(
        schema_version="1.0", camera_id=str(camera_id), observed_at=utc_now(), **fields
    )
    atomic_json(path, payload)


def read_decoder_status(camera_id: str) -> dict[str, Any]:
    root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp/sentinel/state")) / "metrics"
    return _read(root / f"decoder-{safe_camera_name(camera_id)}.json")


def write_camera_health_status(camera_id: str, event: dict[str, Any]) -> None:
    root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp/sentinel/state")) / "metrics"
    path = root / f"camera-health-{safe_camera_name(camera_id)}.json"
    atomic_json(path, dict(event))


def increment_supervisor_status(
    camera_id: str, counter: str, value: int = 1, **fields: Any
) -> None:
    root = Path(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp/sentinel/state")) / "metrics"
    path = root / f"camera-supervisor-{safe_camera_name(camera_id)}.json"
    payload = _read(path)
    payload[counter] = int(payload.get(counter, 0)) + int(value)
    payload.update(
        schema_version="1.0", camera_id=str(camera_id), observed_at=utc_now(), **fields
    )
    atomic_json(path, payload)
