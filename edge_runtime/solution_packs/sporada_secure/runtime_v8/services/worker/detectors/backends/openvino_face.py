"""Native OpenVINO face detection, alignment, and embedding.

SCRFD runs on the Intel iGPU. AdaFace runs on the Intel NPU. The module emits
anonymous samples only; identity matching belongs to management.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
import time

import cv2
import numpy as np
from PIL import Image

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
    pitch_degrees: float
    yaw_degrees: float
    roll_degrees: float
    pose_quality: float


def _l2(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 else vector


def _quality(chip: np.ndarray, score: float, face_width: float, pose_quality: float) -> float:
    gray = cv2.cvtColor(chip, cv2.COLOR_BGR2GRAY)
    sharpness = min(1.0, float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 1200.0)
    exposure = max(0.0, 1.0 - abs(float(gray.mean()) - 127.5) / 127.5)
    size = min(1.0, max(0.0, (face_width - 24.0) / 80.0))
    confidence = min(1.0, max(0.0, (score - 0.4) / 0.55))
    visual_quality = 0.4 * confidence + 0.25 * size + 0.25 * sharpness + 0.1 * exposure
    return float(0.7 * visual_quality + 0.3 * min(1.0, max(0.0, pose_quality)))


def _rotation_to_euler(rotation: np.ndarray) -> tuple[float, float, float]:
    """Return SixDRepNet pitch, yaw and roll in degrees."""
    matrix = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    if not np.isfinite(matrix).all():
        raise ValueError("head-pose rotation matrix is not finite")
    sy = math.hypot(float(matrix[0, 0]), float(matrix[1, 0]))
    if sy >= 1e-6:
        pitch = math.atan2(float(matrix[2, 1]), float(matrix[2, 2]))
        yaw = math.atan2(float(-matrix[2, 0]), sy)
        roll = math.atan2(float(matrix[1, 0]), float(matrix[0, 0]))
    else:
        pitch = math.atan2(float(-matrix[1, 2]), float(matrix[1, 1]))
        yaw = math.atan2(float(-matrix[2, 0]), sy)
        roll = 0.0
    return tuple(float(math.degrees(value)) for value in (pitch, yaw, roll))


def _axis_quality(angle: float, preferred: float, maximum: float) -> float:
    value = abs(float(angle))
    if value <= preferred:
        return 1.0
    if value >= maximum:
        return 0.0
    return 1.0 - (value - preferred) / max(1e-6, maximum - preferred)


def _pose_quality(pitch: float, yaw: float, roll: float,
                  max_pitch: float, max_yaw: float, max_roll: float) -> float:
    return float(
        0.5 * _axis_quality(yaw, min(15.0, max_yaw * 0.6), max_yaw)
        + 0.3 * _axis_quality(pitch, min(12.0, max_pitch * 0.6), max_pitch)
        + 0.2 * _axis_quality(roll, min(12.0, max_roll * 0.6), max_roll)
    )


class OpenVINOHeadPoseEstimator:
    """SixDRepNet A0 FP16 head-pose inference with no CPU fallback."""

    def __init__(self, model_path: str, telemetry=None) -> None:
        self.telemetry = telemetry
        core = shared_core()
        model = core.read_model(model_path)
        if list(model.input(0).shape) != [1, 3, 224, 224]:
            raise ValueError("SixDRepNet input must be float32 [1,3,224,224]")
        if list(model.output(0).shape) != [1, 3, 3]:
            raise ValueError("SixDRepNet output must be float32 [1,3,3]")
        self.device = os.getenv("HEAD_POSE_DEVICE", "NPU")
        config = {"PERFORMANCE_HINT": "LATENCY"}
        if self.device.split(".", 1)[0] == "NPU":
            config["INFERENCE_PRECISION_HINT"] = "f16"
        self._compiled = core.compile_model(model, self.device, config)
        self._output = self._compiled.output(0)

    def warmup(self) -> None:
        self._compiled([np.zeros((1, 3, 224, 224), dtype=np.float32)])

    def estimate(self, bgr: np.ndarray) -> tuple[float, float, float]:
        if bgr.size == 0:
            raise ValueError("head-pose crop is empty")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        width, height = image.size
        if width <= height:
            resized = (256, max(256, int(round(256 * height / width))))
        else:
            resized = (max(256, int(round(256 * width / height))), 256)
        image = image.resize(resized, Image.Resampling.BILINEAR)
        left = round((resized[0] - 224) / 2)
        top = round((resized[1] - 224) / 2)
        image = image.crop((left, top, left + 224, top + 224))
        tensor = np.asarray(image, dtype=np.float32) / 255.0
        tensor = (tensor - np.asarray([0.485, 0.456, 0.406], dtype=np.float32)) / np.asarray(
            [0.229, 0.224, 0.225], dtype=np.float32
        )
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])
        started = time.perf_counter()
        result = self._compiled([tensor])
        if self.telemetry is not None:
            self.telemetry.observe_latency(
                "person_identity.face_pose", time.perf_counter() - started,
                device=self.device,
            )
        return _rotation_to_euler(np.asarray(result[self._output])[0])


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
    model_id = "face-embedding-model-v1"
    embedding_space = "face-embedding-model-v1:512:bgr-aligned-112"

    def __init__(self, detector_model: str, embedder_model: str, pose_model: str,
                 telemetry=None) -> None:
        self.telemetry = telemetry
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
        self._pose = OpenVINOHeadPoseEstimator(pose_model, telemetry=telemetry)
        self.detector_threshold = float(os.getenv("FACE_DETECTOR_CONF", "0.55"))
        self.nms_threshold = float(os.getenv("FACE_NMS_THRESHOLD", "0.4"))
        self.min_face_px = int(os.getenv("FACE_MIN_SIZE", "28"))
        self.max_pitch = float(os.getenv("HEAD_POSE_MAX_PITCH_DEGREES", "25"))
        self.max_yaw = float(os.getenv("HEAD_POSE_MAX_YAW_DEGREES", "35"))
        self.max_roll = float(os.getenv("HEAD_POSE_MAX_ROLL_DEGREES", "25"))
        self.pose_context = float(os.getenv("HEAD_POSE_CONTEXT_RATIO", "0.20"))
        self._suppressions: dict[str, int] = {}
        logger.info(
            "face extractor ready: detector=%s embedder=%s pose=%s limits(p/y/r)=%.1f/%.1f/%.1f",
            detector_device, embedder_device, self._pose.device,
            self.max_pitch, self.max_yaw, self.max_roll,
        )

    def warmup(self) -> None:
        self._detector([np.zeros((1, 3, 320, 320), dtype=np.float32)])
        self._embedder([np.zeros((1, 3, 112, 112), dtype=np.float32)])
        self._pose.warmup()

    def drain_suppressions(self) -> dict[str, int]:
        values, self._suppressions = self._suppressions, {}
        return values

    def _suppress(self, reason: str) -> None:
        self._suppressions[reason] = self._suppressions.get(reason, 0) + 1

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
                self._suppress("face_size")
                continue
            source_box = (
                int(max(0, min(width, x1 + box[0]))),
                int(max(0, min(height, y1 + box[1]))),
                int(max(0, min(width, x1 + box[2]))),
                int(max(0, min(height, y1 + box[3]))),
            )
            pose_crop = self._pose_crop(frame, source_box)
            try:
                pitch, yaw, roll = self._pose.estimate(pose_crop)
            except (ValueError, RuntimeError):
                self._suppress("head_pose_invalid")
                logger.warning("head-pose inference rejected a face", exc_info=True)
                continue
            if (abs(pitch) > self.max_pitch or abs(yaw) > self.max_yaw
                    or abs(roll) > self.max_roll):
                self._suppress("head_pose_out_of_range")
                continue
            pose_quality = _pose_quality(
                pitch, yaw, roll, self.max_pitch, self.max_yaw, self.max_roll
            )
            chip = self._align(crop, landmarks)
            embedding = self._embed(chip)
            samples.append(FaceSample(
                bbox=source_box,
                embedding=embedding,
                detector_confidence=float(score),
                quality=_quality(chip, float(score), face_width, pose_quality),
                track_id=int(track_id),
                chip=chip,
                pitch_degrees=pitch,
                yaw_degrees=yaw,
                roll_degrees=roll,
                pose_quality=pose_quality,
            ))
        return samples

    def _pose_crop(self, frame: np.ndarray, bbox) -> np.ndarray:
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = [int(round(value)) for value in bbox]
        box_width, box_height = max(1, x2 - x1), max(1, y2 - y1)
        pad_x = int(round(self.pose_context * box_width))
        pad_y = int(round(self.pose_context * box_height))
        left, top = max(0, x1 - pad_x), max(0, y1 - pad_y)
        right, bottom = min(width, x2 + pad_x), min(height, y2 + pad_y)
        return frame[top:bottom, left:right]

    def _detect(self, bgr: np.ndarray) -> list[tuple[np.ndarray, np.ndarray, float]]:
        source_height, source_width = bgr.shape[:2]
        scale = min(320.0 / source_height, 320.0 / source_width)
        resized_width = max(1, int(round(source_width * scale)))
        resized_height = max(1, int(round(source_height * scale)))
        canvas = np.zeros((320, 320, 3), dtype=np.uint8)
        canvas[:resized_height, :resized_width] = cv2.resize(bgr, (resized_width, resized_height))
        blob = cv2.dnn.blobFromImage(canvas, 1.0 / 128.0, (320, 320),
                                     (127.5, 127.5, 127.5), swapRB=True)
        started = time.perf_counter()
        result = self._detector([blob])
        if self.telemetry is not None:
            self.telemetry.observe_latency(
                "person_identity.face_detection", time.perf_counter() - started,
                device=os.getenv("FACE_DETECTOR_DEVICE", "GPU"),
            )
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
        started = time.perf_counter()
        result = self._embedder([tensor])
        if self.telemetry is not None:
            self.telemetry.observe_latency(
                "person_identity.face_embedding", time.perf_counter() - started,
                device=os.getenv("FACE_EMBEDDER_DEVICE", "NPU"),
            )
        return _l2(np.asarray(result[self._embedder_output]))
