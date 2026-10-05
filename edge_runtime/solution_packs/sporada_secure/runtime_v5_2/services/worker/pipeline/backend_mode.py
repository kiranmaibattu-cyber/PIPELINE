"""Global inference backend mode, sourced from config-api (/api/system).

The whole box runs ONE backend mode (hybrid / all_openvino / all_axelera). The
mode maps to a per-stage backend assignment, which we apply by setting the
VEHICLE_BACKEND / PLATE_BACKEND / OCR_BACKEND env vars BEFORE the detector
factories run (they read those via detectors.base.resolve_backend).

Switching mode reloads models on different engines, and adding/removing cameras
or editing zones changes the pipeline sources/config, so neither can be hot-
swapped in place. The worker polls config-api (mode) and cameras.json (mtime);
on a change it re-execs itself (host-native — no container restart policy).
"""
from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

SYSTEM_URL = os.getenv("SYSTEM_CONFIG_URL", "http://proxy/api/system")
_STAGE_ENV = {"vehicle": "VEHICLE_BACKEND", "plate": "PLATE_BACKEND", "ocr": "OCR_BACKEND"}


def fetch_backend_mode(url: str | None = None, timeout: float = 3.0):
    """Return (mode, backends_dict) from config-api, or (None, {}) on failure."""
    url = url or SYSTEM_URL
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        return data.get("backend_mode"), data.get("backends") or {}
    except Exception as exc:  # noqa: BLE001 — config-api may be briefly unavailable
        logger.warning("backend-mode: could not fetch %s (%s); using local config", url, exc)
        return None, {}


def apply_backend_mode() -> str | None:
    """Fetch the global mode and export per-stage backend env vars. Returns the
    active mode, or None if config-api was unreachable (worker.json/env win)."""
    mode, backends = fetch_backend_mode()
    if mode and backends:
        for stage, backend in backends.items():
            env = _STAGE_ENV.get(stage)
            if env and backend:
                os.environ[env] = backend
        logger.info("backend-mode: active=%s backends=%s", mode, backends)
    return mode


def _mtime(path: str | None) -> float:
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def make_config_watcher(active_mode: str | None, cameras_file: str | None = None,
                        interval: float = 5.0):
    """Return a no-arg tick() to call frequently; every `interval`s it polls the
    backend mode and the cameras.json mtime. Returns True when either changed (so
    the caller can clean up and re-exec), False otherwise."""
    state = {"last": time.monotonic(), "mode": active_mode, "mtime": _mtime(cameras_file)}

    def tick() -> bool:
        now = time.monotonic()
        if now - state["last"] < interval:
            return False
        state["last"] = now
        mode, _ = fetch_backend_mode()
        if mode and active_mode and mode != state["mode"]:
            logger.warning("backend-mode changed %s -> %s; re-execing worker", state["mode"], mode)
            return True
        m = _mtime(cameras_file)
        if m and m != state["mtime"]:
            state["mtime"] = m
            logger.warning("cameras.json changed; re-execing worker to apply")
            return True
        return False

    return tick
