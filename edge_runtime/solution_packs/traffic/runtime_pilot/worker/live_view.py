"""On-demand annotated MJPEG live view (no mediamtx/WebRTC).

When the UI opens a camera's live page, config-api sets redis key live:want:<cam>;
the worker draws detections on that camera at a low fps and pushes JPEGs to
live:frame:<cam>, which config-api streams as MJPEG. Idle cameras cost nothing —
the worker only draws when a viewer is present.
"""
from __future__ import annotations

import time

import cv2
import numpy as np


class LiveView:
    def __init__(self, redis_client, fps: int = 3, max_w: int = 1280, camera_configs=None):
        self.r = redis_client
        self.interval = 1.0 / max(1, fps)
        self.max_w = max_w
        self.camera_configs = camera_configs or {}
        self._last: dict[str, float] = {}
        self._want: dict[str, tuple[bool, float]] = {}

    def wanted(self, cam: str, now: float) -> bool:
        cached = self._want.get(cam)
        if cached is None or now - cached[1] > 1.0:   # re-check each cam ~1/s
            try:
                v = bool(self.r and self.r.exists(f"live:want:{cam}"))
            except Exception:  # noqa: BLE001
                v = False
            self._want[cam] = (v, now)
            return v
        return cached[0]

    def publish(self, cam: str, frame, detections, now: float) -> None:
        if now - self._last.get(cam, 0.0) < self.interval:
            return
        self._last[cam] = now
        ok, buf = cv2.imencode(".jpg", self._draw(frame, detections, cam),
                               [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            try:
                self.r.setex(f"live:frame:{cam}", 5, buf.tobytes())
            except Exception:  # noqa: BLE001
                pass

    def _draw(self, frame, detections, cam=None):
        h, w = frame.shape[:2]
        scale = self.max_w / w if w > self.max_w else 1.0
        img = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1 else frame.copy()
        self._draw_geometry(img, frame.shape, cam, scale)
        for d in detections:
            x1, y1, x2, y2 = (int(v * scale) for v in d.bbox)
            if d.model_name == "vehicle":
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
            elif d.model_name == "license_plate":
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 165, 255), 2)
                txt = d.metadata.get("ocr_text")
                if txt:
                    cv2.putText(img, txt, (x1, max(12, y1 - 5)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)
        return img

    def _draw_geometry(self, img, frame_shape, cam, scale):
        """Overlay the camera's authored geometry so an operator can confirm a line
        or zone is where they drew it: lines yellow, zones cyan, masks magenta."""
        runtime = (self.camera_configs.get(cam) or {}).get("runtime_analytics") or {}
        if not runtime:
            return
        from pipeline.analytics import geometry_points
        cam_cfg = self.camera_configs.get(cam) or {}

        def scaled(geom):
            pts = geometry_points(geom, frame_shape, cam_cfg)
            return np.array([[int(x * scale), int(y * scale)] for x, y in pts], np.int32)

        for cfg in runtime.values():
            for line in (cfg.get("lines") or []):
                p = scaled(line)
                if len(p) >= 2:
                    cv2.polylines(img, [p], False, (0, 255, 255), 2)
            for key, color in (("zones", (255, 255, 0)),
                               ("constraint_zones", (255, 255, 0)),
                               ("masks", (255, 0, 255))):
                for zone in (cfg.get(key) or []):
                    p = scaled(zone)
                    if len(p) >= 3:
                        cv2.polylines(img, [p], True, color, 2)
