"""Credential-safe JSON logging for the multi-process camera runtime."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import threading
from typing import Any


_context_lock = threading.Lock()
_camera_id: str | None = None
_stream_session_id: str | None = None

_URL = re.compile(r"(?i)\b(?:rtsp|rtsps|rtmp|rtmps|http|https|tcp)://[^\s\"']+")
_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+")
_SECRET = re.compile(
    r"(?i)(token|password|passwd|credential|secret|api[_-]?key)(\s*[:=]\s*)[^\s,;&]+"
)


def sanitize_text(value: Any, *, limit: int = 4096) -> str:
    """Redact complete URLs and common credential forms from diagnostics."""
    text = str(value).replace("\r", " ").replace("\x00", "")
    text = _URL.sub("<redacted-url>", text)
    text = _BEARER.sub(r"\1<redacted>", text)
    text = _SECRET.sub(r"\1\2<redacted>", text)
    return text[:limit]


def set_log_context(
    camera_id: str | None = None, stream_session_id: str | None = None
) -> None:
    global _camera_id, _stream_session_id
    with _context_lock:
        _camera_id = str(camera_id) if camera_id else None
        _stream_session_id = str(stream_session_id) if stream_session_id else None


def set_stream_session_id(value: str | None) -> None:
    global _stream_session_id
    with _context_lock:
        _stream_session_id = str(value) if value else None


class JsonLogFormatter(logging.Formatter):
    _reserved = frozenset(
        {
            "args", "asctime", "created", "exc_info", "exc_text", "filename",
            "funcName", "levelname", "levelno", "lineno", "module", "msecs",
            "message", "msg", "name", "pathname", "process", "processName",
            "relativeCreated", "stack_info", "thread", "threadName", "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        with _context_lock:
            camera_context = _camera_id
            session_context = _stream_session_id
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_text(record.getMessage()),
            "pid": os.getpid(),
            "camera_id": getattr(record, "camera_id", None) or camera_context,
            "stream_session_id": (
                getattr(record, "stream_session_id", None) or session_context
            ),
            "event": getattr(record, "event", None) or "log_message",
        }
        for key, value in record.__dict__.items():
            if key in self._reserved or key in payload or key.startswith("_"):
                continue
            if isinstance(value, str):
                payload[key] = sanitize_text(value)
            elif value is None or isinstance(value, (bool, int, float, list, dict)):
                payload[key] = value
            else:
                payload[key] = sanitize_text(value)
        if record.exc_info:
            payload["exception"] = sanitize_text(
                self.formatException(record.exc_info), limit=8192
            )
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_structured_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    safe_fields = {key: value for key, value in fields.items() if value is not None}
    logger.log(level, message, extra={"event": event, **safe_fields})
