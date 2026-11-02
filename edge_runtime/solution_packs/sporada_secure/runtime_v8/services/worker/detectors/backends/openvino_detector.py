"""OpenVINO YOLO detector for the Sentinel V7.0 worker.

Runs vehicle and plate detection on the configured OpenVINO device. Models are
exported with NMS baked in (output [1, 300, 6] = x1,y1,x2,y2,score,class in
letterboxed-input pixel coordinates, rows sorted by score descending).

Best practices baked in (docs.openvino.ai, 2024/2025):
- ONE shared ov.Core with CACHE_DIR, so GPU `.cl_cache` / NPU `.blob` are reused
  across worker restarts (compile is otherwise re-paid every launch).
- Compile with PERFORMANCE_HINT=LATENCY and one GPU stream.  A detector in this
  runtime receives one synchronous frame at a time; allocating a throughput
  stream pool in every camera process multiplies GPU resources without adding
  per-camera concurrency.
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

import fcntl

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
    coordinates."""
    h, w = image.shape[:2]
    s = size / max(h, w)
    nw, nh = round(w * s), round(h * s)
    resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, np.uint8)
    px, py = (size - nw) // 2, (size - nh) // 2
    canvas[py:py + nh, px:px + nw] = resized
    return canvas, s, px, py


def yolo_detections(
    output: np.ndarray,
    *,
    image_shape: tuple[int, int],
    scale: float,
    pad_x: int,
    pad_y: int,
    confidence: float,
    class_ids: Optional[set[int]],
    class_names: dict,
    model_name: str,
    max_detections: Optional[int] = None,
) -> list[Detection]:
    """Convert one NMS-baked YOLO result back to source-frame coordinates."""
    h, w = image_shape
    detections: list[Detection] = []
    for row in output[0]:
        score = float(row[4])
        if score < confidence:
            break
        cls = int(row[5])
        if class_ids is not None and cls not in class_ids:
            continue
        x1 = int(max(0, min(w, (row[0] - pad_x) / scale)))
        y1 = int(max(0, min(h, (row[1] - pad_y) / scale)))
        x2 = int(max(0, min(w, (row[2] - pad_x) / scale)))
        y2 = int(max(0, min(h, (row[3] - pad_y) / scale)))
        if x2 <= x1 or y2 <= y1:
            continue
        detections.append(Detection(
            bbox=[x1, y1, x2, y2], class_id=cls,
            class_name=class_names.get(cls, model_name),
            confidence=score, model_name=model_name,
        ))
        if max_detections and len(detections) >= max_detections:
            break
    return detections


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
        performance_hint: str = "LATENCY",
    ):
        import openvino as ov
        from openvino import Layout, Type
        from openvino.preprocess import ColorFormat, PrePostProcessor

        performance_hint = performance_hint.upper()
        if performance_hint not in {"LATENCY", "THROUGHPUT"}:
            raise ValueError("YOLO performance_hint must be LATENCY or THROUGHPUT")
        self.model_name = model_name
        self.imgsz = int(imgsz)
        self.confidence = float(confidence)
        self.class_ids = set(class_ids) if class_ids is not None else None
        self.class_names = class_names or {}
        self.max_detections = max_detections
        self._tls = threading.local()
        self._infer_lock_fd = None

        core = shared_core(cache_dir)
        model = core.read_model(model_xml)
        # The vehicle IR accepts a dynamic batch, but the Intel GPU path is
        # compiled for the batch actually used by this detector.  The current
        # per-camera worker submits one frame per request.  A future batching
        # worker must reshape its own model instance to its selected fixed
        # batch (for example 2 or 4) before compilation.
        model.reshape({model.input(0): [1, 3, self.imgsz, self.imgsz]})
        # Fold preprocessing into the graph: accept uint8 NHWC BGR, let the device
        # convert to the model's f32 NCHW RGB /255 input.
        ppp = PrePostProcessor(model)
        inp = ppp.input()
        inp.tensor().set_element_type(Type.u8).set_layout(Layout("NHWC")).set_color_format(ColorFormat.BGR)
        inp.model().set_layout(Layout("NCHW"))
        inp.preprocess().convert_element_type(Type.f32).convert_color(ColorFormat.RGB).scale(255.0)
        model = ppp.build()

        options = {"PERFORMANCE_HINT": performance_hint, "ALLOW_AUTO_BATCHING": False}
        if performance_hint == "LATENCY" and device.split(".")[0] == "GPU":
            options["NUM_STREAMS"] = "1"
        self._compiled = core.compile_model(model, device, options)
        self._out = self._compiled.output(0)
        self.device = device
        if (
            device.split(".")[0] == "GPU"
            and os.getenv("SENTINEL_SERIALIZE_GPU_DETECTION", "true").lower()
            in {"1", "true", "yes", "on"}
        ):
            # Multiple camera processes fed from the same TCP MPEG-TS source
            # receive frames in lockstep. Intel's GPU plugin can reject
            # concurrent clFinish calls with CL_OUT_OF_RESOURCES even when each
            # process owns only one InferRequest. A container-local file lock
            # keeps the short synchronous detector calls bounded while decode,
            # tracking, policy and delivery remain parallel per camera.
            lock_path = os.getenv(
                "SENTINEL_GPU_DETECTION_LOCK",
                "/tmp/sentinel/gpu-detection.lock",
            )
            os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
            self._infer_lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
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

    def reset_request(self) -> None:
        """Discard a failed thread-local request before a bounded retry.

        Intel's GPU plugin can leave a synchronous request unusable after
        ``CL_OUT_OF_RESOURCES``.  The compiled model remains valid, so the next
        call can create a fresh request without recompiling the model.
        """
        if hasattr(self._tls, "req"):
            del self._tls.req

    def warmup(self, n: int = 2) -> None:
        dummy = np.zeros((1, self.imgsz, self.imgsz, 3), np.uint8)
        req = self._request()
        for _ in range(n):
            req.infer({0: dummy})

    def infer_letterboxed(self, canvas: np.ndarray, *, request=None) -> np.ndarray:
        """Infer one uint8 NHWC letterboxed image and return a detached result."""
        if self._infer_lock_fd is not None:
            fcntl.flock(self._infer_lock_fd, fcntl.LOCK_EX)
        try:
            req = request if request is not None else self._request()
            return np.asarray(req.infer({0: canvas[None]})[self._out]).copy()
        finally:
            if self._infer_lock_fd is not None:
                fcntl.flock(self._infer_lock_fd, fcntl.LOCK_UN)

    def detect(self, bgr: np.ndarray) -> list[Detection]:
        if bgr is None or bgr.size == 0:
            return []
        canvas, scale, px, py = letterbox(bgr, self.imgsz)
        h, w = bgr.shape[:2]
        return yolo_detections(
            self.infer_letterboxed(canvas), image_shape=(h, w), scale=scale,
            pad_x=px, pad_y=py, confidence=self.confidence,
            class_ids=self.class_ids, class_names=self.class_names,
            model_name=self.model_name, max_detections=self.max_detections,
        )
