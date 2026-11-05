"""Low-overhead per-camera runtime telemetry shared with the HTTP supervisor.

Camera workers are separate processes, so snapshots are written atomically below
``/tmp/sentinel/state/metrics`` and read by the solution-image HTTP process. The counters are
diagnostic only: they never participate in event production decisions.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterator

import psutil


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:160] or "camera"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _parse_observed_at(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


class CameraTelemetry:
    """Collect interval rates, cumulative counts, and stage latency per camera."""

    def __init__(self, camera_id: str, *, target_fps: float,
                 devices: dict[str, str] | None = None,
                 use_case_catalog: dict[str, str] | None = None,
                 interval_seconds: float | None = None) -> None:
        self.camera_id = str(camera_id)
        self.target_fps = max(0.0, float(target_fps))
        self.devices = {str(key): str(value) for key, value in (devices or {}).items()}
        self.use_case_catalog = {
            str(key): str(value) for key, value in (use_case_catalog or {}).items()
        }
        self.interval = max(0.5, float(
            interval_seconds if interval_seconds is not None
            else os.getenv("SENTINEL_TELEMETRY_INTERVAL_SECONDS", "2")
        ))
        root = Path(os.getenv(
            "APEXFABRIC_STATE_ROOT", "/tmp/sentinel/state"
        )) / "metrics"
        self.path = root / f"runtime-camera-{_safe_name(self.camera_id)}.json"
        self.started_wall = time.time()
        self.started_mono = time.monotonic()
        self._window_started = self.started_mono
        self._last_flush = self.started_mono
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._counters: dict[str, int] = defaultdict(int)
        self._window_counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._event_types: dict[str, int] = defaultdict(int)
        self._window_event_types: dict[str, int] = defaultdict(int)
        self._use_cases: dict[str, int] = defaultdict(int)
        self._window_use_cases: dict[str, int] = defaultdict(int)
        self._stages: dict[str, dict[str, float]] = {}
        self._window_stages: dict[str, dict[str, float]] = {}
        self._event_latency: dict[str, dict[str, float]] = {}
        self._window_event_latency: dict[str, dict[str, float]] = {}
        self._device_work_seconds: dict[str, float] = defaultdict(float)
        self._window_device_work_seconds: dict[str, float] = defaultdict(float)
        self._device_calls: dict[str, int] = defaultdict(int)
        self._window_device_calls: dict[str, int] = defaultdict(int)
        self._proc = psutil.Process()
        self._resolved_policy: dict[str, Any] | None = None
        self._timestamp_source = "edge_received_at"
        self._proc.cpu_percent(interval=None)
        self.latest_snapshot: dict[str, Any] | None = None
        self.flush(force=True)
        self._reporter = threading.Thread(
            target=self._report_periodically, daemon=True,
            name=f"camera-telemetry-{self.camera_id}",
        )
        self._reporter.start()

    def count(self, name: str, value: int = 1) -> None:
        if value <= 0:
            return
        with self._lock:
            self._counters[name] += int(value)
            self._window_counters[name] += int(value)

    def set_gauge(self, name: str, value: float | int) -> None:
        """Publish current diagnostic state without affecting event policy."""
        numeric = float(value)
        if not math.isfinite(numeric):
            return
        with self._lock:
            self._gauges[str(name)] = numeric

    def set_resolved_policy(self, policy: dict[str, Any]) -> None:
        """Publish the effective bounded gates without putting them in desired state."""
        with self._lock:
            self._resolved_policy = deepcopy(policy)

    def set_timestamp_source(self, source: str) -> None:
        if source not in {"source_pts", "rtsp_ntp", "edge_received_at"}:
            source = "edge_received_at"
        with self._lock:
            self._timestamp_source = source

    def frame(self, *, inferred: bool | None) -> None:
        """Record a decoded frame and its completed processing disposition.

        ``None`` means inference was requested but did not complete, so the
        frame must not inflate either successful inference FPS or quiet FPS.
        """
        self.count("decoded_frames")
        if inferred is not None:
            self.count("inference_frames" if inferred else "quiet_frames")

    def inference_succeeded(self) -> None:
        self.count("inference_frames")

    @contextmanager
    def measure(self, stage: str, *, device: str | None = None) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe_latency(stage, time.perf_counter() - started, device=device)

    def observe_latency(self, stage: str, seconds: float,
                        *, device: str | None = None) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            return
        with self._lock:
            self._update_latency(self._stages, stage, seconds)
            self._update_latency(self._window_stages, stage, seconds)
            if device:
                for accelerator in self._accelerators(device):
                    self._device_work_seconds[accelerator] += seconds
                    self._window_device_work_seconds[accelerator] += seconds
                    self._device_calls[accelerator] += 1
                    self._window_device_calls[accelerator] += 1

    def event_persisted(self, event: dict[str, Any], persist_seconds: float) -> None:
        event_type = str(event.get("event_type") or "unknown")
        use_case_id = str(event.get("use_case_id") or "runtime")
        observed = _parse_observed_at(event.get("observed_at"))
        production_seconds = max(0.0, time.time() - observed) if observed is not None else None
        with self._lock:
            self._event_types[event_type] += 1
            self._window_event_types[event_type] += 1
            self._use_cases[use_case_id] += 1
            self._window_use_cases[use_case_id] += 1
            self._counters["events_persisted"] += 1
            self._window_counters["events_persisted"] += 1
            self._update_latency(self._stages, "event_persistence", persist_seconds)
            self._update_latency(self._window_stages, "event_persistence", persist_seconds)
            if production_seconds is not None:
                self._update_latency(self._event_latency, event_type, production_seconds)
                self._update_latency(self._window_event_latency, event_type, production_seconds)

    def event_handoff_admitted(self, event: dict[str, Any], admission_seconds: float) -> None:
        """Record CV output admission without claiming delivery-agent durability."""
        event_type = str(event.get("event_type") or "unknown")
        use_case_id = str(event.get("use_case_id") or "runtime")
        observed = _parse_observed_at(event.get("observed_at"))
        production_seconds = max(0.0, time.time() - observed) if observed is not None else None
        with self._lock:
            self._event_types[event_type] += 1
            self._window_event_types[event_type] += 1
            self._use_cases[use_case_id] += 1
            self._window_use_cases[use_case_id] += 1
            self._counters["events_handoff_admitted"] += 1
            self._window_counters["events_handoff_admitted"] += 1
            self._update_latency(
                self._stages, "event_handoff_admission", admission_seconds
            )
            self._update_latency(
                self._window_stages, "event_handoff_admission", admission_seconds
            )
            if production_seconds is not None:
                self._update_latency(self._event_latency, event_type, production_seconds)
                self._update_latency(self._window_event_latency, event_type, production_seconds)

    def maybe_flush(self) -> None:
        if time.monotonic() - self._last_flush >= self.interval:
            self.flush()

    def flush(self, *, force: bool = False) -> dict[str, Any] | None:
        # Serialize snapshot reset AND atomic file replacement. The periodic
        # reporter must not race a camera-triggered flush on the same tmp file.
        with self._flush_lock:
            return self._flush_serialized(force=force)

    def _flush_serialized(self, *, force: bool = False) -> dict[str, Any] | None:
        now_mono = time.monotonic()
        if not force and now_mono - self._last_flush < self.interval:
            return None
        with self._lock:
            window_seconds = max(1e-6, now_mono - self._window_started)
            payload = self._snapshot_locked(window_seconds)
            self.latest_snapshot = payload
            self._window_started = now_mono
            self._last_flush = now_mono
            self._window_counters.clear()
            self._window_event_types.clear()
            self._window_use_cases.clear()
            self._window_stages.clear()
            self._window_event_latency.clear()
            self._window_device_work_seconds.clear()
            self._window_device_calls.clear()
        _atomic_json(self.path, payload)
        return payload

    def close(self) -> None:
        self._stop.set()
        self._reporter.join(timeout=max(2, self.interval * 2))
        self.flush(force=True)

    def _report_periodically(self) -> None:
        # Model waits must not freeze the last non-zero FPS indefinitely.
        # This thread reads counters only; it never emits analytic observations.
        while not self._stop.wait(self.interval):
            try:
                self.flush()
            except Exception:
                logging.getLogger(__name__).exception(
                    "Camera telemetry publication failed",
                    extra={"camera_id": self.camera_id},
                )

    def _snapshot_locked(self, window_seconds: float) -> dict[str, Any]:
        try:
            with self._proc.oneshot():
                memory = self._proc.memory_info()
                try:
                    full_memory = self._proc.memory_full_info()
                except (psutil.Error, OSError, AttributeError):
                    full_memory = None
                cpu_pct = self._proc.cpu_percent(interval=None)
                grouped_process = os.getenv(
                    "SENTINEL_CAMERA_GROUP_MODE", "false"
                ).lower() in {"1", "true", "yes", "on"}
                process = {
                    "scope": (
                        "shared_camera_group_process"
                        if grouped_process else "camera_process"
                    ),
                    "pid": int(self._proc.pid),
                    "shared_by_multiple_cameras": grouped_process,
                    "cpu_cores_used": round(cpu_pct / 100.0, 3),
                    "rss_mb": round(memory.rss / 1024 / 1024, 2),
                    "uss_mb": (
                        round(full_memory.uss / 1024 / 1024, 2)
                        if full_memory is not None and hasattr(full_memory, "uss") else None
                    ),
                    "pss_mb": (
                        round(full_memory.pss / 1024 / 1024, 2)
                        if full_memory is not None and hasattr(full_memory, "pss") else None
                    ),
                    "threads": self._proc.num_threads(),
                }
        except (psutil.Error, OSError):
            process = {
                "scope": "camera_process", "cpu_cores_used": None,
                "pid": os.getpid(), "shared_by_multiple_cameras": False,
                "rss_mb": None, "uss_mb": None,
                "pss_mb": None, "threads": None,
            }

        decoded_rate = self._window_counters.get("decoded_frames", 0) / window_seconds
        inferred_rate = self._window_counters.get("inference_frames", 0) / window_seconds
        output_rate = (
            self._window_counters.get("events_handoff_admitted", 0)
            + self._window_counters.get("events_persisted", 0)
        ) / window_seconds
        accelerators = {}
        configured = {accelerator for value in self.devices.values()
                      for accelerator in self._accelerators(value)}
        for accelerator in sorted(configured | set(self._window_device_work_seconds)):
            work = self._window_device_work_seconds.get(accelerator, 0.0)
            accelerators[accelerator] = {
                "scope": "camera_attributed_model_stages",
                # Includes synchronous model-call wall time. It is deliberately
                # not presented as accelerator utilization.
                "attributed_stage_wall_ms_per_second": round(
                    1000.0 * work / window_seconds, 3
                ),
                "model_calls_total": int(self._device_calls.get(accelerator, 0)),
                "model_calls_in_window": int(self._window_device_calls.get(accelerator, 0)),
            }
        use_case_rates = self._rate_items(
            self._use_cases, self._window_use_cases, window_seconds
        )
        for use_case_id, values in use_case_rates.items():
            values["app"] = self.use_case_catalog.get(use_case_id, "runtime")
        return {
            "schema_version": "1.0",
            "camera_id": self.camera_id,
            "pid": os.getpid(),
            "observed_at": _utc_now(),
            "started_at": datetime.fromtimestamp(
                self.started_wall, timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "uptime_seconds": round(max(0.0, time.monotonic() - self.started_mono), 3),
            "window_seconds": round(window_seconds, 3),
            "processing": {
                "target_fps": self.target_fps,
                "decoded_fps": round(decoded_rate, 3),
                "frames_received_fps": round(decoded_rate, 3),
                "inference_fps": round(inferred_rate, 3),
                "quiet_fps": round(self._window_counters.get("quiet_frames", 0) / window_seconds, 3),
                "output_events_per_second": round(output_rate, 3),
                "totals": dict(sorted(self._counters.items())),
                "gauges": {
                    key: int(value) if value.is_integer() else round(value, 6)
                    for key, value in sorted(self._gauges.items())
                },
            },
            "outputs": {
                "by_event_type": self._rate_items(
                    self._event_types, self._window_event_types, window_seconds
                ),
                "by_use_case_id": use_case_rates,
                "production_latency_by_event_type": self._latency_items(
                    self._event_latency, self._window_event_latency,
                    window_seconds,
                    measurement_type="source_to_cv_output_acceptance_wall_time",
                ),
            },
            "use_case_processing": self._latency_items(
                self._stages, self._window_stages, window_seconds
            ),
            "resources": {"process": process, "accelerators": accelerators},
            "device_placement": dict(sorted(self.devices.items())),
            "timestamp_source": self._timestamp_source,
            "resolved_policy": deepcopy(self._resolved_policy),
        }

    @staticmethod
    def _update_latency(target: dict[str, dict[str, float]], key: str, seconds: float) -> None:
        item = target.setdefault(key, {"calls": 0.0, "total": 0.0, "max": 0.0, "last": 0.0})
        item["calls"] += 1
        item["total"] += seconds
        item["max"] = max(item["max"], seconds)
        item["last"] = seconds

    @staticmethod
    def _rate_items(total: dict[str, int], window: dict[str, int],
                    seconds: float) -> dict[str, dict[str, float | int]]:
        return {
            key: {
                "total": int(value),
                "per_second": round(window.get(key, 0) / seconds, 4),
                "in_window": int(window.get(key, 0)),
            }
            for key, value in sorted(total.items())
        }

    @staticmethod
    def _latency_items(total: dict[str, dict[str, float]],
                       window: dict[str, dict[str, float]],
                       window_seconds: float | None = None,
                       measurement_type: str | None = None) -> dict[str, dict[str, Any]]:
        result = {}
        for key, lifetime in sorted(total.items()):
            recent = window.get(key)
            recent_calls = int((recent or {}).get("calls", 0))
            lifetime_calls = int(lifetime["calls"])
            item_type, includes_children = CameraTelemetry._stage_semantics(key)
            if measurement_type is not None:
                item_type, includes_children = measurement_type, False
            result[key] = {
                "calls_total": lifetime_calls,
                "calls_in_window": recent_calls,
                "calls_per_second": (
                    round(recent_calls / window_seconds, 4)
                    if window_seconds and window_seconds > 0 else None
                ),
                "avg_ms": (
                    round(1000.0 * recent["total"] / recent_calls, 3)
                    if recent_calls and recent is not None else None
                ),
                "max_ms": (
                    round(1000.0 * recent["max"], 3) if recent is not None else None
                ),
                "last_ms": (
                    round(1000.0 * recent["last"], 3) if recent is not None else None
                ),
                "lifetime_avg_ms": round(
                    1000.0 * lifetime["total"] / max(1, lifetime_calls), 3
                ),
                "measurement_type": item_type,
                "includes_child_stages": includes_children,
            }
        return result

    @staticmethod
    def _stage_semantics(stage: str) -> tuple[str, bool]:
        model_stages = {
            "detection.vehicle_person", "anpr.plate_detection",
            "fire_smoke.detection", "person_identity.face_detection",
            "person_identity.face_pose", "person_identity.face_embedding",
            "person_identity.body_embedding", "person_identity.gait_processing",
            "scene_search.embedding",
        }
        parent_stages = {
            "pipeline.frame_processing_total", "event_policy.active_frame",
            "person_identity.face_pipeline",
        }
        if stage == "anpr.ocr":
            return "async_submit_to_callback_wall_time", False
        if stage in model_stages:
            return "model_invocation_wall_time", False
        if stage == "event_persistence":
            return "durable_io_wall_time", False
        if stage in parent_stages:
            return "pipeline_span_wall_time", True
        return "pipeline_stage_wall_time", False

    @staticmethod
    def _accelerators(device: str) -> set[str]:
        value = str(device).upper()
        return {item for item in ("GPU", "NPU") if item in value}
