"""OpenVINO YOLO detector for the all-OpenVINO worker (AIPU-less box).

Runs vehicle / plate detection on the Arc iGPU (or any OpenVINO device). Mirrors
the Axelera cascade's models: yolo26s @640 vehicle, yolo26n @224 plate, both
exported with NMS baked in (output [1, 300, 6] = x1,y1,x2,y2,score,class in
letterboxed-input pixel coords, rows sorted by score descending).

Best practices baked in (docs.openvino.ai, 2024/2025):
- ONE shared ov.Core with CACHE_DIR, so GPU `.cl_cache` / NPU `.blob` are reused
  across worker restarts (compile is otherwise re-paid every launch).
- Compile with PERFORMANCE_HINT=THROUGHPUT: the per-camera threads each issue
  inference concurrently, so OV sizes streams to saturate the device.
- PrePostProcessor folds color (BGR->RGB) + layout (NHWC->NCHW) + scale (/255)
  ONTO the device. We feed a uint8 NHWC letterboxed tensor and do zero per-pixel
  float work in Python. (Letterbox itself stays in cv2 — PPP `resize` would not
  preserve aspect ratio.)
- InferRequests are created lazily per thread (threading.local) from the shared,
  thread-safe CompiledModel — so N camera threads never contend on one request.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Iterable, Optional

import cv2
import numpy as np

from pipeline.types import Detection

logger = logging.getLogger(__name__)

_core = None
_core_lock = threading.Lock()


def shared_core(cache_dir: str = "/tmp/ov_cache"):
    """Process-wide singleton ov.Core with model caching enabled."""
    global _core
    if _core is None:
        with _core_lock:
            if _core is None:
                import openvino as ov
                core = ov.Core()
                try:
                    os.makedirs(cache_dir, exist_ok=True)
                    core.set_property({"CACHE_DIR": cache_dir})
                except Exception:  # noqa: BLE001
                    logger.warning("could not set OpenVINO CACHE_DIR=%s", cache_dir)
                _core = core
    return _core


def letterbox(image: np.ndarray, size: int):
    """Aspect-preserving resize into a square `size` canvas padded with gray 114.
    Returns (canvas, scale, pad_x, pad_y) for un-letterboxing boxes back to frame
    coords. Matches the Axelera cascade's letterbox preprocessing."""
    h, w = image.shape[:2]
    s = size / max(h, w)
    nw, nh = round(w * s), round(h * s)
    resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, np.uint8)
    px, py = (size - nw) // 2, (size - nh) // 2
    canvas[py:py + nh, px:px + nw] = resized
    return canvas, s, px, py


class OpenVINOYOLODetector:
    """YOLO detector (NMS-baked, [1,N,6] output) on OpenVINO. detect(bgr) -> [Detection]."""

    def __init__(
        self,
        model_xml: str,
        model_name: str,
        *,
        device: str = "GPU",
        imgsz: int = 640,
        confidence: float = 0.3,
        class_ids: Optional[Iterable[int]] = None,
        class_names: Optional[dict] = None,
        max_detections: Optional[int] = None,
        cache_dir: str = "/tmp/ov_cache",
    ):
        import openvino as ov
        from openvino import Layout, Type
        from openvino.preprocess import ColorFormat, PrePostProcessor

        self.model_name = model_name
        self.imgsz = int(imgsz)
        self.confidence = float(confidence)
        self.class_ids = set(class_ids) if class_ids is not None else None
        self.class_names = class_names or {}
        self.max_detections = max_detections
        self._tls = threading.local()

        core = shared_core(cache_dir)
        model = core.read_model(model_xml)
        # Fold preprocessing into the graph: accept uint8 NHWC BGR, let the device
        # convert to the model's f32 NCHW RGB /255 input.
        ppp = PrePostProcessor(model)
        inp = ppp.input()
        inp.tensor().set_element_type(Type.u8).set_layout(Layout("NHWC")).set_color_format(ColorFormat.BGR)
        inp.model().set_layout(Layout("NCHW"))
        inp.preprocess().convert_element_type(Type.f32).convert_color(ColorFormat.RGB).scale(255.0)
        model = ppp.build()

        self._compiled = core.compile_model(model, device, {"PERFORMANCE_HINT": "THROUGHPUT"})
        self._out = self._compiled.output(0)
        self.device = device
        try:
            self.exec_devices = ",".join(self._compiled.get_property("EXECUTION_DEVICES"))
        except Exception:  # noqa: BLE001
            self.exec_devices = device
        logger.info("OV detector '%s' on %s (exec=%s) imgsz=%d conf=%.2f",
                    model_name, device, self.exec_devices, self.imgsz, self.confidence)

    def _request(self):
        """Per-thread InferRequest from the shared CompiledModel (thread-safe)."""
        req = getattr(self._tls, "req", None)
        if req is None:
            req = self._compiled.create_infer_request()
            self._tls.req = req
        return req

    def warmup(self, n: int = 2) -> None:
        dummy = np.zeros((1, self.imgsz, self.imgsz, 3), np.uint8)
        req = self._request()
        for _ in range(n):
            req.infer({0: dummy})

    def detect(self, bgr: np.ndarray) -> list[Detection]:
        if bgr is None or bgr.size == 0:
            return []
        canvas, scale, px, py = letterbox(bgr, self.imgsz)
        out = self._request().infer({0: canvas[None]})[self._out]  # [1, N, 6]
        h, w = bgr.shape[:2]
        dets: list[Detection] = []
        for row in out[0]:
            score = float(row[4])
            if score < self.confidence:
                break  # NMS output is sorted by score descending
            cls = int(row[5])
            if self.class_ids is not None and cls not in self.class_ids:
                continue
            x1 = int(max(0, min(w, (row[0] - px) / scale)))
            y1 = int(max(0, min(h, (row[1] - py) / scale)))
            x2 = int(max(0, min(w, (row[2] - px) / scale)))
            y2 = int(max(0, min(h, (row[3] - py) / scale)))
            if x2 <= x1 or y2 <= y1:
                continue
            dets.append(Detection(
                bbox=[x1, y1, x2, y2], class_id=cls,
                class_name=self.class_names.get(cls, self.model_name),
                confidence=score, model_name=self.model_name,
            ))
            if self.max_detections and len(dets) >= self.max_detections:
                break
        return dets
