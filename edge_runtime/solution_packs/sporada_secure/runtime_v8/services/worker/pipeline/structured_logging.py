"""Credential-safe JSON logging for the multi-process camera runtime."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import threading
from typing import Any


_context = threading.local()

_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*:(?:\\*/){2}[^\s\"'<>]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;\"'\\}\]]+")
_SECRET_NAME = (
    r"(?:[\w.-]*(?:token|password|passwd|credentials?|secret|api[_-]?key"
    r"|private[_-]?key|secret[_-]?key)"
    r"|authorization|proxy[_-]?authorization|cookie|set-cookie)"
)
_SECRET_KEY = re.compile(rf"(?i){_SECRET_NAME}\Z")
# Match whole quoted values, including spaces/commas and escaped quotes. The
# delimiter can itself be escaped when JSON is embedded in another string.
_SECRET = re.compile(
    rf"(?i)(\b{_SECRET_NAME}(?:\\*[\"'])?\s*[:=]\s*)"
    r"(?:(?P<quote>\\*[\"'])(?:(?!(?<!\\)(?P=quote)).)*"
    r"(?:(?<!\\)(?P=quote)|\Z)"
    r"|[^\s,;&}\]]+)",
    re.DOTALL,
)
_MAX_DEPTH = 8
_MAX_ITEMS = 64
_MAX_NODES = 512


def sanitize_text(value: Any, *, limit: int = 4096) -> str:
    """Redact complete URLs and common credential forms from diagnostics."""
    # Bound regex work as well as output. Unterminated quoted assignments are
    # redacted through end-of-input, including values cut off at this limit.
    text = str(value)[:max(0, limit)].replace("\r", " ").replace("\x00", "")
    text = _URL.sub("<redacted-url>", text)
    text = _SECRET.sub(r"\1<redacted>", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    return text[:limit]


def sanitize_value(value: Any) -> Any:
    """Copy diagnostic fields with bounded depth/size and no secret values.

    Keep telemetry scalar types intact. A shared node budget also bounds wide
    trees; cycles reach the depth limit without invoking JSON's fallback repr.
    """
    remaining = _MAX_NODES

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            return "<truncated>"
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, dict):
            if depth >= _MAX_DEPTH:
                return "<truncated>"
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= _MAX_ITEMS or remaining <= 0:
                    result["<truncated>"] = True
                    break
                key_text = str(key)
                safe_key = sanitize_text(key_text, limit=256)
                result[safe_key] = (
                    visit("<redacted>", depth + 1) if _SECRET_KEY.fullmatch(key_text)
                    else visit(child, depth + 1)
                )
            return result
        if isinstance(item, (list, tuple)):
            if depth >= _MAX_DEPTH:
                return "<truncated>"
            result = []
            for index, child in enumerate(item):
                if index >= _MAX_ITEMS or remaining <= 0:
                    result.append("<truncated>")
                    break
                result.append(visit(child, depth + 1))
            return result
        return sanitize_text(item)

    return visit(value, 0)


def set_log_context(
    camera_id: str | None = None, stream_session_id: str | None = None
) -> None:
    # Camera workers may share one process when DL Streamer model instances are
    # shared.  Keep context thread-local so one camera cannot overwrite the
    # camera/session fields attached to another camera's records.
    _context.camera_id = str(camera_id) if camera_id else None
    _context.stream_session_id = (
        str(stream_session_id) if stream_session_id else None
    )


def set_stream_session_id(value: str | None) -> None:
    _context.stream_session_id = str(value) if value else None


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
        camera_context = getattr(_context, "camera_id", None)
        session_context = getattr(_context, "stream_session_id", None)
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
            payload[key] = value
        safe_payload = sanitize_value(payload)
        if record.exc_info:
            safe_payload["exception"] = sanitize_text(
                self.formatException(record.exc_info), limit=8192
            )
        return json.dumps(safe_payload, separators=(",", ":"))


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
