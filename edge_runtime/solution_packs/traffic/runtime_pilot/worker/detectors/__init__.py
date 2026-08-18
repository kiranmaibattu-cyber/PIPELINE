"""Detector helpers. The active worker (stream_fleet_sdk) does detection on the
AIPU via the Voyager SDK and OCR via backends.openvino_ocr_async; only the base
helpers are re-exported here."""
from __future__ import annotations

from .base import AXELERA, OPENVINO, openvino_model_path, resolve_backend

__all__ = ["AXELERA", "OPENVINO", "openvino_model_path", "resolve_backend"]
