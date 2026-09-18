"""Native OpenVINO face detection, alignment, and embedding.

SCRFD runs on the Intel iGPU. AdaFace runs on the Intel NPU. The module emits
anonymous samples only; identity matching belongs to management.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os

import cv2
import numpy as np

from detectors.backends.openvino_detector import shared_core

logger = logging.getLogger(__name__)

_REFERENCE_LANDMARKS = np.asarray([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float32)


@dataclass(frozen=True)
class FaceSample:
    bbox: tuple[int, int, int, int]
    embedding: np.ndarray
    detector_confidence: float
    quality: float
    track_id: int
    chip: np.ndarray


def _l2(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else vector


def _quality(chip: np.ndarray, score: float, face_width: float) -> float:
    gray = cv2.cvtColor(chip, cv2.COLOR_BGR2GRAY)
    sharpness = min(1.0, float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 1200.0)
    exposure = max(0.0, 1.0 - abs(float(gray.mean()) - 127.5) / 127.5)
    size = min(1.0, max(0.0, (face_width - 24.0) / 80.0))
    confidence = min(1.0, max(0.0, (score - 0.4) / 0.55))
    return float(0.4 * confidence + 0.25 * size + 0.25 * sharpness + 0.1 * exposure)


def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    if boxes.size == 0:
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        index = int(order[0])
        keep.append(index)
        xx1 = np.maximum(x1[index], x1[order[1:]])
        yy1 = np.maximum(y1[index], y1[order[1:]])
        xx2 = np.minimum(x2[index], x2[order[1:]])
        yy2 = np.minimum(y2[index], y2[order[1:]])
        intersection = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[index] + areas[order[1:]] - intersection
        overlap = np.divide(intersection, union, out=np.zeros_like(intersection), where=union > 0)
        order = order[np.where(overlap <= threshold)[0] + 1]
    return keep


class OpenVINOFaceExtractor:
    dimension = 512
    model_id = "adaface-ir101-int8-v1"
    embedding_space = "adaface-ir101-int8-v1:512:bgr-aligned-112"

    def __init__(self, detector_model: str, embedder_model: str) -> None:
        core = shared_core()
        detector = core.read_model(detector_model)
        detector.reshape({detector.input(0): [1, 3, 320, 320]})
        detector_device = os.getenv("FACE_DETECTOR_DEVICE", "GPU")
        embedder_device = os.getenv("FACE_EMBEDDER_DEVICE", "NPU")
        self._detector = core.compile_model(detector, detector_device, {"PERFORMANCE_HINT": "LATENCY"})
        self._detector_outputs = list(self._detector.outputs)
        self._embedder = core.compile_model(core.read_model(embedder_model), embedder_device,
                                            {"PERFORMANCE_HINT": "LATENCY"})
        self._embedder_output = self._embedder.output(0)
        self.detector_threshold = float(os.getenv("FACE_DETECTOR_CONF", "0.55"))
        self.nms_threshold = float(os.getenv("FACE_NMS_THRESHOLD", "0.4"))
        self.min_face_px = int(os.getenv("FACE_MIN_SIZE", "28"))
        logger.info("face extractor ready: detector=%s embedder=%s", detector_device, embedder_device)

    def warmup(self) -> None:
        self._detector([np.zeros((1, 3, 320, 320), dtype=np.float32)])
        self._embedder([np.zeros((1, 3, 112, 112), dtype=np.float32)])

    def extract(self, frame: np.ndarray, people: list) -> list[FaceSample]:
        samples: list[FaceSample] = []
        height, width = frame.shape[:2]
        for person in people:
            track_id = person.metadata.get("track_id")
            if track_id is None:
                continue
            x1, y1, x2, y2 = [int(value) for value in person.bbox]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            if x2 - x1 < self.min_face_px or y2 - y1 < self.min_face_px * 2:
                continue
            crop = frame[y1:y2, x1:x2]
            detections = self._detect(crop)
            if not detections:
                continue
            box, landmarks, score = max(detections, key=lambda item: item[2])
            face_width = float(box[2] - box[0])
            if face_width < self.min_face_px:
                continue
            chip = self._align(crop, landmarks)
            embedding = self._embed(chip)
            source_box = (
                int(max(0, min(width, x1 + box[0]))),
                int(max(0, min(height, y1 + box[1]))),
                int(max(0, min(width, x1 + box[2]))),
                int(max(0, min(height, y1 + box[3]))),
            )
            samples.append(FaceSample(
                bbox=source_box,
                embedding=embedding,
                detector_confidence=float(score),
                quality=_quality(chip, float(score), face_width),
                track_id=int(track_id),
                chip=chip,
            ))
        return samples

    def _detect(self, bgr: np.ndarray) -> list[tuple[np.ndarray, np.ndarray, float]]:
        source_height, source_width = bgr.shape[:2]
        scale = min(320.0 / source_height, 320.0 / source_width)
        resized_width = max(1, int(round(source_width * scale)))
        resized_height = max(1, int(round(source_height * scale)))
        canvas = np.zeros((320, 320, 3), dtype=np.uint8)
        canvas[:resized_height, :resized_width] = cv2.resize(bgr, (resized_width, resized_height))
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128.0, (320, 320),
                                     (127.5, 127.5, 127.5), swapRB=True)
        result = self._detector([blob])
        outputs = [np.asarray(result[output]) for output in self._detector_outputs]
        scores_all, boxes_all, landmarks_all = [], [], []
        for level, stride in enumerate((8, 16, 32)):
            scores = outputs[level].reshape(-1)
            distances = outputs[level + 3].reshape(-1, 4) * stride
            landmark_distances = outputs[level + 6].reshape(-1, 10) * stride
            grid_height = grid_width = 320 // stride
            grid_y, grid_x = np.mgrid[0:grid_height, 0:grid_width]
            centers = np.stack([grid_x, grid_y], axis=-1).astype(np.float32).reshape(-1, 2) * stride
            centers = np.repeat(centers, 2, axis=0)
            selected = np.where(scores >= self.detector_threshold)[0]
            if selected.size == 0:
                continue
            boxes = np.stack([
                centers[:, 0] - distances[:, 0], centers[:, 1] - distances[:, 1],
                centers[:, 0] + distances[:, 2], centers[:, 1] + distances[:, 3],
            ], axis=1)
            landmarks = np.empty((centers.shape[0], 5, 2), dtype=np.float32)
            for point in range(5):
                landmarks[:, point, 0] = centers[:, 0] + landmark_distances[:, point * 2]
                landmarks[:, point, 1] = centers[:, 1] + landmark_distances[:, point * 2 + 1]
            scores_all.append(scores[selected])
            boxes_all.append(boxes[selected] / scale)
            landmarks_all.append(landmarks[selected] / scale)
        if not scores_all:
            return []
        scores = np.concatenate(scores_all)
        boxes = np.vstack(boxes_all)
        landmarks = np.vstack(landmarks_all)
        keep = _nms(boxes, scores, self.nms_threshold)
        return [(boxes[index], landmarks[index], float(scores[index])) for index in keep]

    @staticmethod
    def _align(bgr: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
        matrix, _ = cv2.estimateAffinePartial2D(
            np.asarray(landmarks, dtype=np.float32), _REFERENCE_LANDMARKS,
            method=cv2.LMEDS,
        )
        if matrix is None:
            x1, y1 = np.maximum(landmarks.min(axis=0) - 16, 0).astype(int)
            x2, y2 = np.minimum(landmarks.max(axis=0) + 16, [bgr.shape[1], bgr.shape[0]]).astype(int)
            fallback = bgr[y1:y2, x1:x2]
            return cv2.resize(fallback, (112, 112))
        return cv2.warpAffine(bgr, matrix, (112, 112), borderValue=0.0)

    def _embed(self, chip: np.ndarray) -> np.ndarray:
        tensor = ((chip.astype(np.float32) / 255.0) - 0.5) / 0.5
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])
        result = self._embedder([tensor])
        return _l2(np.asarray(result[self._embedder_output]))
