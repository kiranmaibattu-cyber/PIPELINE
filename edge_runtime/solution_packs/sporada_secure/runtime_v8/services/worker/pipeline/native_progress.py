"""Bounded observations of native progress, independent of camera policy.

Snapshots only read Python counters. Ages describe the last observed buffers;
they do not identify the cause of a timeout or imply that a stage is faulty.
"""
import logging
import math
import threading
import time

from .structured_logging import sanitize_text


class NativeProgressTrace:
    MAX_STAGES = 64
    PROBES = tuple(
        (element, direction)
        for element in (
            "videoparser", "decoder", "postproc", "fpsgate", "policygate",
            "objecttracker", "primarydet", "facedet", "faceclass",
            "firefpsgate", "firedet",
        )
        for direction in ("sink", "src")
    ) + (("vasink", "sink"), ("firesink", "sink"))

    def __init__(self, camera_id, session_id, interval_seconds=30.0):
        interval_seconds = float(interval_seconds)
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        self.camera_id = sanitize_text(camera_id, limit=256)
        self.session_id = (
            sanitize_text(session_id, limit=256) if session_id is not None else None
        )
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._stages = {}
        self._phase = "startup"
        self._phase_at = time.monotonic()
        self._stop = threading.Event()
        self._attached = False
        self._thread = threading.Thread(
            target=self._report, daemon=True, name=f"native-progress-{self.camera_id}"
        )

    def mark(self, stage, pts=None):
        if self._stop.is_set():
            return
        with self._lock:
            # Attached pads register trusted constant names once. Keep regex
            # work off their per-buffer path; never cache raw diagnostic names.
            prior = self._stages.get(stage) if isinstance(stage, str) else None
            if prior is None:
                if len(self._stages) >= self.MAX_STAGES:
                    return
                stage = sanitize_text(stage, limit=128)
                prior = self._stages.get(stage)
            # Gst already supplies Python integers. Coerce only external values
            # so snapshots cannot retain arbitrary objects or credential text.
            if pts is not None and not isinstance(pts, int):
                pts = int(pts)
            self._stages[stage] = (
                (prior[0] + 1) if prior else 1, time.monotonic(), pts
            )

    def phase(self, name):
        with self._lock:
            self._phase = sanitize_text(name, limit=128)
            self._phase_at = time.monotonic()

    def attach(self, pipeline, Gst):
        """Observe available named pads; missing elements/pads are optional."""
        if self._attached or self._stop.is_set():
            return
        for element, direction in self.PROBES:
            obj = pipeline.get_by_name(element)
            if obj is None:
                continue
            pad = obj.get_static_pad(direction)
            if pad is None:
                continue
            stage = f"{element}.{direction}"
            with self._lock:
                if len(self._stages) < self.MAX_STAGES:
                    self._stages.setdefault(stage, (0, None, None))

            def observe(_pad, info, stage=stage):
                buffer = info.get_buffer()
                if buffer is not None:
                    self.mark(stage, buffer.pts)
                return Gst.PadProbeReturn.OK

            pad.add_probe(Gst.PadProbeType.BUFFER, observe)
        self._attached = True
        self._thread.start()

    def snapshot(self):
        """Return detached, on-demand timeout evidence without native queries."""
        with self._lock:
            now = time.monotonic()
            return {
                "camera_id": self.camera_id,
                "stream_session_id": self.session_id,
                "camera_phase": self._phase,
                "phase_age_seconds": round(max(0, now - self._phase_at), 3),
                "stages": {
                    name: {
                        "count": value[0],
                        "age_seconds": (
                            round(max(0, now - value[1]), 3)
                            if value[1] is not None else None
                        ),
                        "pts_ns": value[2],
                    }
                    for name, value in self._stages.items()
                },
            }

    def log_snapshot(self, *, event="native_pipeline_progress", level=logging.INFO):
        snapshot = self.snapshot()
        logging.getLogger(__name__).log(
            level, "Native pipeline progress",
            extra={"event": sanitize_text(event, limit=128), **snapshot},
        )
        return snapshot

    def _report(self):
        while not self._stop.wait(self.interval_seconds):
            self.log_snapshot()

    def close(self):
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)
