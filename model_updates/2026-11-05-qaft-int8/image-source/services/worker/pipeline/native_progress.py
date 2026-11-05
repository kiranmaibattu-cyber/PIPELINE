"""Optional bounded native-stage diagnostics, independent of camera policy."""
import logging
import threading
import time


class NativeProgressTrace:
    def __init__(self, camera_id, session_id):
        self.camera_id = camera_id
        self.session_id = session_id
        self._lock = threading.Lock()
        self._stages = {}
        self._phase = "startup"
        self._phase_at = time.monotonic()
        self._stop = threading.Event()
        self._appsrc = None
        self._thread = threading.Thread(target=self._report, daemon=True,
                                        name=f"native-progress-{camera_id}")

    def mark(self, stage, pts=None):
        with self._lock:
            prior = self._stages.get(stage)
            self._stages[stage] = ((prior[0] + 1) if prior else 1,
                                   time.monotonic(), pts)

    def phase(self, name):
        with self._lock:
            self._phase = name
            self._phase_at = time.monotonic()

    def attach(self, pipeline, Gst):
        self._appsrc = pipeline.get_by_name("tcpappsrc")
        for element, direction in [
            ("videoparser", "sink"), ("videoparser", "src"),
            ("decoder", "src"), ("fpsgate", "sink"),
            ("primarydet", "sink"), ("primarydet", "src"),
            ("facedet", "sink"), ("facedet", "src"),
            ("faceclass", "sink"), ("faceclass", "src"),
            ("vasink", "sink"), ("firesink", "sink"),
        ]:
            obj = pipeline.get_by_name(element)
            if obj is None:
                continue
            pad = obj.get_static_pad(direction)
            if pad is None:
                continue

            def observe(_pad, info, stage=f"{element}.{direction}"):
                buffer = info.get_buffer()
                if buffer is not None:
                    self.mark(stage, int(buffer.pts))
                return Gst.PadProbeReturn.OK

            pad.add_probe(Gst.PadProbeType.BUFFER, observe)
        self._thread.start()

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            result = {
                "camera_phase": self._phase,
                "phase_age_seconds": round(now - self._phase_at, 3),
                "stages": {name: {"count": v[0], "age_seconds": round(now-v[1], 3),
                                  "pts_ns": v[2]} for name, v in self._stages.items()},
            }
        if self._appsrc is not None:
            result["tcp_queue"] = {
                key: int(self._appsrc.get_property(key))
                for key in ("current-level-bytes", "dropped", "in", "out")
                if self._appsrc.find_property(key) is not None
            }
        return result

    def _report(self):
        while not self._stop.wait(2):
            logging.getLogger(__name__).info("Native pipeline progress", extra={
                "event": "native_pipeline_progress", "camera_id": self.camera_id,
                "stream_session_id": self.session_id, **self.snapshot(),
            })

    def close(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
