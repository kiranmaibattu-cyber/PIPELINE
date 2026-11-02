"""Structured logging for the solution-image supervisor process."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
from typing import Any


_URL = re.compile(r"(?i)\b(?:rtsp|rtsps|rtmp|rtmps|http|https|tcp)://[^\s\"']+")
_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+")
_SECRET = re.compile(
    r"(?i)(token|password|passwd|credential|secret|api[_-]?key)(\s*[:=]\s*)[^\s,;&]+"
)


def _sanitize(value: Any, limit: int = 8192) -> str:
    text = str(value).replace("\r", " ").replace("\x00", "")
    text = _URL.sub("<redacted-url>", text)
    text = _BEARER.sub(r"\1<redacted>", text)
    text = _SECRET.sub(r"\1\2<redacted>", text)
    return text[:limit]


class SupervisorJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": _sanitize(record.getMessage()),
            "pid": os.getpid(),
            "camera_id": getattr(record, "camera_id", None),
            "stream_session_id": getattr(record, "stream_session_id", None),
            "event": getattr(record, "event", None) or "log_message",
        }
        for key in (
            "worker_pid", "revision", "reason", "exit_code",
            "retry_backoff_seconds", "camera_count",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = _sanitize(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = _sanitize(self.formatException(record.exc_info))
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_supervisor_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SupervisorJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
