"""OpenVINO fire/smoke detector.

Runs the INT8 YOLOv8 fire/smoke model exported as OpenVINO IR. The model used
here emits raw YOLO output [1, 6, 2100], so this backend does letterbox
preprocessing, confidence filtering, and NMS directly without Ultralytics.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import cv2
import numpy as np

from pipeline.types import Detection

from .openvino_detector import letterbox, shared_core

logger = logging.getLogger(__name__)


CLASS_NAMES = {0: "fire", 1: "smoke"}


def _device_with_fallback(core, requested: str) -> str:
    available = list(core.available_devices)

    def present(device: str) -> bool:
        return any(item == device or item.startswith(device + ".") for item in available)

    if ":" in requested:
        prefix, rest = requested.split(":", 1)
        wanted = [item.strip() for item in rest.split(",") if item.strip()]
        usable = [item for item in wanted if present(item)]
        if usable:
            return usable[0] if len(usable) == 1 else f"{prefix}:{','.join(usable)}"
        return "GPU" if present("GPU") else "CPU"
    if requested == "AUTO" or present(requested):
        return requested
    return "GPU" if present("GPU") else "CPU"


class OpenVINOSmokeFireDetector:
    def __init__(
        self,
        model_xml: str,
        *,
        device: str = "AUTO:GPU,NPU,CPU",
        imgsz: int = 320,
        confidence: float = 0.35,
        iou_threshold: float = 0.45,
        max_detections: int = 50,
        processing_interval: int = 5,
        cache_dir: str = "/tmp/ov_cache",
    ):
        self.imgsz = int(imgsz)
        self.confidence = float(confidence)
        self.iou_threshold = float(iou_threshold)
        self.max_detections = int(max_detections)
        self.processing_interval = max(1, int(processing_interval))
        self._tls = threading.local()

        core = shared_core(cache_dir)
        resolved = _device_with_fallback(core, device)
        model = core.read_model(model_xml)
        self._compiled = core.compile_model(model, resolved, {"PERFORMANCE_HINT": "THROUGHPUT"})
        self._inp = self._compiled.input(0)
        self._out = self._compiled.output(0)
        self.requested_device = device
        try:
            self.exec_devices = ",".join(self._compiled.get_property("EXECUTION_DEVICES"))
        except Exception:  # noqa: BLE001
            self.exec_devices = resolved
        logger.info(
            "OV smoke_fire on %s (requested=%s) imgsz=%d conf=%.2f interval=%d",
            self.exec_devices,
            self.requested_device,
            self.imgsz,
            self.confidence,
            self.processing_interval,
        )

    def _request(self):
        req = getattr(self._tls, "req", None)
        if req is None:
            req = self._compiled.create_infer_request()
            self._tls.req = req
        return req

    def warmup(self, n: int = 1) -> None:
        dummy = np.zeros((1, 3, self.imgsz, self.imgsz), np.float32)
        req = self._request()
        for _ in range(n):
            req.infer({self._inp: dummy})

    def should_process(self, frame_index: int) -> bool:
        return frame_index % self.processing_interval == 0

    def detect(self, bgr: np.ndarray) -> list[Detection]:
        if bgr is None or bgr.size == 0:
            return []

        canvas, scale, pad_x, pad_y = letterbox(bgr, self.imgsz)
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        tensor = (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
        output = self._request().infer({self._inp: tensor})[self._out]
        rows = self._as_rows(np.asarray(output))

        h, w = bgr.shape[:2]
        boxes = []
        scores = []
        class_ids = []
        for row in rows:
            if row.shape[0] < 6:
                continue
            cls_scores = row[4:]
            class_id = int(np.argmax(cls_scores))
            score = float(cls_scores[class_id])
            if score < self.confidence:
                continue
            cx, cy, bw, bh = [float(value) for value in row[:4]]
            x1 = (cx - bw / 2.0 - pad_x) / scale
            y1 = (cy - bh / 2.0 - pad_y) / scale
            x2 = (cx + bw / 2.0 - pad_x) / scale
            y2 = (cy + bh / 2.0 - pad_y) / scale
            x1 = int(max(0, min(w, x1)))
            y1 = int(max(0, min(h, y1)))
            x2 = int(max(0, min(w, x2)))
            y2 = int(max(0, min(h, y2)))
            if x2 <= x1 or y2 <= y1:
                continue
            boxes.append([x1, y1, x2 - x1, y2 - y1])
            scores.append(score)
            class_ids.append(class_id)

        keep = cv2.dnn.NMSBoxes(boxes, scores, self.confidence, self.iou_threshold)
        if len(keep) == 0:
            return []
        indices = np.asarray(keep).reshape(-1)[: self.max_detections]
        detections: list[Detection] = []
        for idx in indices:
            x, y, bw, bh = boxes[int(idx)]
            class_id = int(class_ids[int(idx)])
            detections.append(
                Detection(
                    bbox=[x, y, x + bw, y + bh],
                    class_id=class_id,
                    class_name=CLASS_NAMES.get(class_id, str(class_id)),
                    confidence=float(scores[int(idx)]),
                    model_name="smoke_fire",
                    metadata={"reported": True},
                )
            )
        return detections

    @staticmethod
    def _as_rows(output: np.ndarray) -> np.ndarray:
        data = np.squeeze(output)
        if data.ndim != 2:
            return np.empty((0, 0), dtype=np.float32)
        if data.shape[0] <= data.shape[1] and data.shape[0] in {6, 7, 84}:
            return data.T
        return data
